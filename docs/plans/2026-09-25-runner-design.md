# 2.4. Запуск Claude Code в контейнере — дизайн

Статус: утверждён 2026-09-25. Владелец: Илья. Ветка: `romanov`.
Основа: спецификация 2.4, песочница `coreteams-sandbox` (замеры 2026-09-23).

## Решения

| Вопрос | Решение | Отвергнуто |
|---|---|---|
| Жизненный цикл | Постоянный контейнер на проект, долгоживущий `ClaudeSDKClient`; после рестарта — `resume(session_id)` | контейнер на ход; постоянный контейнер + процесс SDK на ход — оба не удерживают развилку |
| Связь с гейтвеем | Runner сам подключается по WebSocket, у контейнера 0 входящих портов | HTTP API в runner + обнаружение по docker labels; общая БД |
| Живость | Heartbeat 10 с + сторож прогресса: `stalled`, если `phase=working` и нет прогресса дольше `STALL_AFTER_S` (720 с) | лимит на весь ход; слежение за CPU/сетью процессов |
| Подтверждённая работа | git commit в конце хода (`result` и нет фоновых задач) + событие `checkpoint{sha}`; после рестарта посреди хода — `turn_interrupted`, resume и автопродолжение (не больше 3 подряд) | откат и ожидание человека; только транскрипт SDK |
| Изоляция | Физическая (только свой том, non-root, read-only rootfs, cap-drop ALL, no-new-privileges) + PreToolUse-сторож с журналом `access_denied` | только физическая; песочница Claude Code (bubblewrap) |
| Секреты | Файлы в `/run/secrets` (ro), fail-fast с exit 78 до `hello`, redact в событиях, отзыв ротацией в cliproxy | env; ключ только вне контейнера (sidecar) — следующий шаг |
| Запуск | Свой CLI-оркестратор `sessions` поверх docker CLI | docker compose на проект; запуск целиком на стороне гейтвея |

## Стек

Python 3.12 (как образ и Agent SDK 0.2.158 из песочницы), uv, ruff, mypy strict, pytest, `websockets`.
Используемые опции SDK проверены на 0.2.158: `resume`, `include_partial_messages`, `hooks` (PreToolUse, `permissionDecision: deny`, срабатывает и для субагентов), `can_use_tool`, `env`, `setting_sources`.

## Раскладка репозитория

```
docs/2.4/spec.md                  спецификация с заполненными контрактами
docs/2.4/protocol.md              протокол runner↔gateway (контракт для 2.1)
runner/
  pyproject.toml  Dockerfile
  src/ctrunner/
    config.py      env + /run/secrets → RunnerConfig (граница, fail-fast)
    protocol.py    типы WS-сообщений, разбор на границе
    liveness.py    чистая: (phase, last_progress, now, T) → Health
    guard.py       чистая: вызов инструмента → Allow | Deny(reason)
    redact.py      чистая: вырезать значения секретов
    state.py       state.json: атомарная запись (tmp + fsync + rename)
    session.py     обёртка SDK: ход, фоновые задачи, развилка, resume
    checkpoint.py  git commit в конце хода
    gateway.py     WS-клиент: переподключение, повтор неподтверждённых событий
    main.py        сборка (императивная оболочка)
    sessions/      CLI: start | stop | restart | status | logs
    stub/          заглушка гейтвея для демо и тестов
    show.py        человекочитаемый вывод событий (из песочницы)
  tests/  evals/
```

## Том проекта

Том `proj-<id>` монтируется в `/workspace`:

```
/workspace/project/              git-репозиторий, cwd агента, внутри .claude/ (CTF)
/workspace/.runner/state.json    session_id, open_turn, last_sha, autoresume_count, queue (обработанные id — в inbox/processed/)
/workspace/.runner/events.jsonl  журнал: сначала запись, потом отправка
/workspace/claude/               CLAUDE_CONFIG_DIR — транскрипты для resume
```

Сторож разрешает инструментам только `/workspace/project` и `/tmp`: `.runner/` и `claude/` агенту недоступны.
Подготовка данных: если том пуст, `sessions start --data <dir>` копирует данные (от 2.3) в `project/`; если в них нет `.claude/`, кладётся шаблон CTF из образа; если это не git-репозиторий, выполняются `git init` и первый commit.

## Протокол runner ↔ gateway

WebSocket, одно JSON-сообщение на кадр, конверт `{v: 1, type, ...}`, авторизация `Authorization: Bearer <gateway_token>`.

| Направление | Тип | Поля |
|---|---|---|
| runner→gw | `hello` | project_id, runner_version, session_id \| null, last_seq |
| gw→runner | `welcome` | acked_seq: runner повторяет всё, что после него |
| runner→gw | `heartbeat` (10 с) | phase: idle \| working \| awaiting_answer; health: ok \| stalled \| crashed; last_progress_at; turn_id |
| runner→gw | `event` | seq (монотонный, на диске), turn_id, kind, payload |
| gw→runner | `command` | id; kind: message{text} \| fork_answer{fork_id, answers} \| stop |
| runner→gw | `command_ack` | id; accepted \| duplicate \| rejected{reason} |

`event.kind`: `session_started`, `turn_started`, `sdk` (нормализованное сообщение SDK с вырезанными секретами), `fork_question`, `fork_answered`, `checkpoint{sha}`, `turn_completed`, `turn_interrupted`, `turn_resumed`, `turn_failed{reason}`, `access_denied{tool, path}`.

Доставка событий — at-least-once, гейтвей убирает дубли по `seq`; команды идемпотентны по `id`. Потоковые токены (partial) наружу не отправляются, runner использует их только как признак прогресса.
Гейтвей считает сессию упавшей, если heartbeat не приходил дольше 30 с.

## Запуск контейнера

```
docker run -d --name ct-<id> --restart on-failure:5 \
  --user agent --read-only --cap-drop ALL --security-opt no-new-privileges \
  --memory 1g --cpus 1 --pids-limit 256 \
  --tmpfs /tmp --tmpfs /home/agent:uid=1000 --tmpfs /run/ctrunner:uid=1000 \
  -v proj-<id>:/workspace -v <secrets>/<id>:/run/secrets:ro \
  -e PROJECT_ID -e GATEWAY_URL -e ANTHROPIC_BASE_URL -e STALL_AFTER_S=720 \
  coreteams-runner:<ver>
```

Политика `on-failure:5`: ошибку конфигурации (exit 78) бесконечные рестарты не лечат.
`HEALTHCHECK` читает `/run/ctrunner/health.json`, поэтому `docker ps` показывает зависание и без гейтвея. Файл лежит на отдельном tmpfs вне разрешённых сторожем корней: в `/tmp` агент мог бы подделать собственный статус.

## Жизненный цикл runner

1. Прочитать конфиг и секреты. Нет секрета → exit 78, `hello` не отправляется.
2. Проверить ключ запросом к cliproxy. 401/403 → exit 78; сеть недоступна → exit 1, Docker перезапустит.
3. Загрузить `state.json`, подготовить `project/`.
4. В фоне подключиться к гейтвею. Если `GATEWAY_URL` не задан, работает только локальный журнал. Работа от соединения не зависит.
5. `ClaudeSDKClient(resume=state.session_id, include_partial_messages=True, hooks={PreToolUse: guard}, can_use_tool=fork)`.
6. Если `state.open_turn`: событие `turn_interrupted`, затем автопродолжение. После 3 автопродолжений подряд ход проваливается (`turn_failed{resume_limit}`), `health=crashed`, runner ждёт человека.
7. Цикл: команда → `open_turn` в state (fsync) → `query` → поток до `result` и `pending=∅` → `git commit` → `checkpoint` → `turn_completed` → state без открытого хода.

Развилка: `can_use_tool(AskUserQuestion)` → phase `awaiting_answer`, событие `fork_question{fork_id}`, ожидание `fork_answer` без таймаута (это не зависание). Если развилку прервал рестарт, срабатывает общий путь `turn_interrupted` → автопродолжение, и агент задаёт вопрос заново.

## Негативные сценарии

| Сценарий | Поведение |
|---|---|
| Контейнер упал посреди хода | Docker перезапускает; `turn_interrupted` (исход неизвестен, «завершено» не ставится) → resume → автопродолжение |
| Кончился диск | ENOSPC при commit или записи state → `turn_failed{disk_full}`; ход не помечается завершённым; команды принимаются дальше |
| Секрет недоступен | exit 78 до `hello`, гейтвей сессию не видит |
| Выход за рабочую директорию | PreToolUse: `realpath` вне разрешённых корней → deny + `access_denied`. Для Bash — только эвристика по абсолютным путям и `..`; настоящая граница — то, что смонтировано |
| Долгая работа без событий | `phase=working` и нет прогресса (включая потоковые токены) дольше `STALL_AFTER_S` → `stalled`. Runner процесс не убивает |
| Обрыв WS | Переподключение с backoff; повтор событий из `events.jsonl` после `welcome.acked_seq` |
| Секрет в выводе | `redact` → `***` перед записью в журнал и отправкой |

## Проверки

Unit (pytest, без Docker и сети): `liveness`, `guard` (`..`, symlink, относительные пути субагентов), `redact`, разбор `protocol`, атомарность `state`, машина состояний хода на фейковом SDK-клиенте: `result` с фоновыми задачами, рестарт при `turn_open`, лимит автопродолжений.

Eval (`evals/`, нужны Docker и cliproxy):
- `eval_start` — старт с тестовым проектом, в течение N секунд есть `turn_started` и первое `sdk`;
- `eval_restart` — `docker kill` посреди хода → `turn_interrupted` → `turn_resumed` → `turn_completed`, новый `checkpoint.sha`;
- `eval_isolation` (полуавтомат) — промпт «прочитай ../proj-other и /etc/…» → `access_denied` в журнале;
- `eval_stall` — `STALL_AFTER_S=60`, промпт «выполни sleep 300» → `stalled`, `docker ps` показывает unhealthy.

## Этапы

**Этап 1 — демо 28.09:** спецификация и `protocol.md` без «заполнить»; образ с изоляцией; `sessions start|stop|status|logs`; runner: цикл SDK, локальный журнал, liveness + HEALTHCHECK, guard, redact, секреты fail-fast; unit-тесты; `eval_start`, `eval_isolation`, `eval_stall`.

**Этап 2 — до следующего демо:**
- выполнено (PPS-116, PPS-133; [дизайн](2026-10-01-runner-recovery-design.md)): checkpoint, resume, автопродолжение, `eval_restart` (в коде `test_restart`);
- осталось: WS-клиент, заглушка гейтвея, развилки через команды; повтор событий.

## Вне границ

Несколько сессий и узлов; автоматическое убийство зависшей сессии; фильтрация исходящего трафика (только cliproxy и гейтвей) — записано как риск; ключ вне контейнера (sidecar); изоляция Bash средствами ОС внутри контейнера.

## Риски

- Агент под тем же пользователем может прочитать ключ cliproxy через Bash → ключ свой на проект, отзываемый.
- Скрытое состояние процесса SDK: гарантией служит только путь resume, поэтому `eval_restart` обязателен.
- Поведение `resume` при прерванном вызове инструмента через cliproxy проверено спайком (одна модель, один прогон): API принимает транскрипт, но модель может повторить прерванный вызов, и побочные эффекты выполнятся дважды. Подробности — [дизайн восстановления](2026-10-01-runner-recovery-design.md), `## Findings`.
- Лимиты cliproxy (запросы и токены в минуту) узнать у владельца.
