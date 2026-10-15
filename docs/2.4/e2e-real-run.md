# Runbook: первый реальный технический E2E Run (PPS-138)

**Назначение.** Один исполнитель проходит путь start → вопрос и ответ того же Run → доработка → результат → durable save → ZIP → повторное открытие после рестарта, плюс сбой и повтор, и заполняет обезличенный протокол (раздел 7).
**Границы.** Это технический E2E. Пользовательский замер (время, удобство, мнение участника) — отдельно, в этом документе он `NOT_RUN` (раздел 9).
**Связанные документы.** Ревью PR #11 — [`pr11-runner-review.md`](pr11-runner-review.md); протокол runner ↔ gateway — [`protocol.md`](protocol.md); публичная лента — [`../contracts/events-v0.md`](../contracts/events-v0.md); runner — [`../../runner/README.md`](../../runner/README.md); восстановление — [`../recovery/RUNNER-RECOVERY.md`](../recovery/RUNNER-RECOVERY.md).

## 0. Что реально выполнимо сегодня

Сверено с кодом ветки `facilitator-question-resume` (`0ac2f62`) на 2026-10-15.

| Часть пути | Выполнимо | Чем |
|---|---|---|
| Runner: старт, вопрос/ответ, `docker kill`, продолжение | да, нужны Docker, образ, ключ модели | автономный режим (`sessions send/answer/logs`) без gateway; либо с фейковым gateway (`runner/evals/test_question.py`, `-m e2e`) |
| Runner ↔ настоящий gateway | **нет** (B-1): в `backend/` нет WebSocket-эндпоинта для runner | ждёт реализации раздела 11 `events-v0.md` (Дмитрий) |
| Файлы с байтами и hash, роли, этапы, approve, ZIP | **нет** (B-2 и пункты 1-3, 8, 13 ревью) | шаги помечены `NOT_RUN: нет реализации`, их надо оставить в протоколе, не вычёркивать |
| Фейковый gateway и перевод в v0 без Docker и модели | да, локально | `uv run pytest -q` в `runner/` (раздел 8) |

Протокол честно различает три причины `NOT_RUN`: `нет реализации`, `нет предусловия` (стенд, секрет, образ), `не запускалось по решению`. Пишите причину в колонке «факт».

## 1. Предусловия

Отметьте каждое в протоколе (шаг P0-P8). Значений секретов в протокол и в чат не записывать.

| № | Предусловие | Как проверить (команда без секретов) |
|---|---|---|
| P0 | Коммит репозитория и чистое дерево | `git rev-parse HEAD`, `git status --short` |
| P1 | Docker 28+, демон запущен | `docker version --format '{{.Server.Version}}'` |
| P2 | uv 0.9+ , зависимости runner | из `runner/`: `uv sync` |
| P3 | Образ runner собран; зафиксирован id и коммит Core Team Framework | `docker build -t coreteams-runner:dev .` (из `runner/`); `docker image inspect coreteams-runner:dev --format '{{.Id}}'`; коммит CTF — `ARG CTF_COMMIT` в `runner/Dockerfile` |
| P4 | Env шлюза моделей (cliproxy) загружен в оболочку | env-файл по пути из `runner/README.md`; проверка только наличия имён: `env \| cut -d= -f1 \| grep -E '^(ANTHROPIC_BASE_URL\|ANTHROPIC_AUTH_TOKEN\|ANTHROPIC_DEFAULT_(OPUS\|SONNET\|HAIKU)_MODEL)$'` (не печатать значения) |
| P5 | Секрет проекта записан файлом (0600) | `uv run sessions secrets init <id>` (читает `ANTHROPIC_AUTH_TOKEN`, при заданном `GATEWAY_AUTH_TOKEN` пишет и `gateway_token`) |
| P6 | Gateway с эндпоинтом для runner доступен контейнеру | `GATEWAY_URL` (`ws://`/`wss://`). `localhost` подменяется на `host.docker.internal` (Docker Desktop); на Linux нужен `--add-host`, `sessions` его не добавляет. **Сегодня не выполнимо** (B-1) |
| P7 | Хранилище 2.3 доступно gateway (подтверждает `artifact.saved`) | определяется владельцем (Арина); сегодня канал содержимого не определён (`events-v0.md` §11 п. 11) |
| P8 | Общий стенд Филиппа: адрес, доступ, версия сборки web и gateway | запросить у Филиппа; в протоколе записать только обезличенное имя стенда и версию сборки |

### Имена переменных окружения (значения не записываются)

| Где | Имена |
|---|---|
| Хост, для `sessions` | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `GATEWAY_URL`, `GATEWAY_AUTH_TOKEN` |
| Контейнер (задаёт `sessions`) | `PROJECT_ID`, `STALL_AFTER_S`, `ANTHROPIC_BASE_URL`, `GATEWAY_URL`, модели; ключи только файлами в `/run/secrets` |
| E2E-тесты runner | `FAKE_GATEWAY_BIND` (адрес привязки фейкового gateway) |
| Backend (gateway) | `DATABASE_URL`, `CLAUDE_PERMISSION_MODE`, `WORKSPACE_ROOT`, `ARTIFACT_MAX_DOWNLOAD_BYTES`, `SUBSCRIPTION_WINDOW_SECONDS`, `SUBSCRIPTION_POLL_INTERVAL_SECONDS`, `MAX_CONCURRENT_RUNS` (`backend/config.py`) |
| Web-product (PR #11) | `VITE_CTF_API_URL`, `VITE_CTF_V2_ENABLED` |

## 2. Идентификаторы и версии, которые фиксируются

Заполняются в шапке протокола до шага T1. Реальные имена людей и проектов не использовать.

| Поле | Откуда |
|---|---|
| `build.repo_commit` | `git rev-parse HEAD` |
| `build.branch` | `git branch --show-current` |
| `build.runner_image_id` | `docker image inspect coreteams-runner:dev --format '{{.Id}}'` |
| `build.runner_version` | поле `runner_version` в `hello` или `session_started.payload` в `events.jsonl` (сейчас `0.1.0`) |
| `build.ctf_commit` | `ARG CTF_COMMIT` из `runner/Dockerfile` |
| `build.gateway_commit`, `build.web_commit` | у владельцев (Дмитрий, Катя/Филипп); для PR #11 — `8ab6c2a` |
| `env.stand` | обезличенное имя: `local`, `stand-1` |
| `ids.project` | `<id>` из `sessions start` (формат `[a-z0-9][a-z0-9-]{0,39}`), например `e2e-<дата>-<n>` |
| `ids.container` | `ct-<id>` |
| `ids.run` | `run_id` из `run.accepted` / `run.started` (= `id` команды `message`) |
| `ids.question` | `question_id` из `facilitator.question` (после рестарта меняется) |
| `ids.checkpoint` | `commit_sha` из `artifact.created` |
| `ids.artifact` | `artifact_id`, затем `version_id` из `artifact.saved` |
| `ids.restarts` | `docker inspect ct-<id> --format '{{.RestartCount}}'` |
| `time.t0` | UTC момент шага T1 (для привязки журналов) |

## 3. Подготовка (один раз)

Команды из каталога `runner/`. Все подкоманды `sessions` проверены по `uv run sessions --help` (`secrets`, `start`, `send`, `answer`, `status`, `stop`, `restart`, `rm`, `logs`).

```bash
uv sync
docker build -t coreteams-runner:dev .
uv run sessions secrets init <id>
```

Запуск с gateway (после появления эндпоинта) — перед `secrets init` и `start` задать `GATEWAY_URL` и `GATEWAY_AUTH_TOKEN`:

```bash
uv run sessions start <id> --data evals/fixtures/demo
```

Запуск без gateway (автономный режим, доступен сегодня): не задавать `GATEWAY_URL`.

```bash
uv run sessions start <id> --data evals/fixtures/demo
uv run sessions status <id>          # через ~15 с ожидается state=running health=healthy
```

Для прогона с коротким порогом зависания: `--stall-after S`.

## 4. Технический сценарий: шаги и наблюдения

Колонка «Команда» — для автономного режима (без gateway); при подключённом gateway те же действия выполняются через web или API gateway, а `sessions send/answer` не используются, чтобы не обходить проверяемый путь. Журнал runner смотреть так:

```bash
uv run sessions logs <id>                                  # журнал events.jsonl (внутренний формат)
docker exec ct-<id> cat /workspace/.runner/events.jsonl | uv run ctrunner-v0 <id>   # публичная лента events.v0
```

Заметка про `-f`: `sessions logs <id> -f` следит за журналом.

| Шаг | Действие | Команда | Ожидание | Что смотреть |
|---|---|---|---|---|
| T1 | Старт проекта | `sessions start <id> --data <каталог>` | контейнер `ct-<id>` `running`, `health=healthy` за ~15 с; в журнале `session_started` | `sessions status`; `RestartCount=0`; нет `access_denied` |
| T2 | Подключение к gateway | (автоматически при `GATEWAY_URL`) | `hello` → `welcome{acked_seq}`; heartbeat раз в 10 с | на стороне gateway: соединение, `last_seq`; без gateway шаг `NOT_RUN` |
| T3 | Файлы-входы с байтами и hash | нет команды | в проекте лежат файлы, hash на стороне хранилища и в томе совпадают | **нет реализации** (ревью п. 1); для автономного режима проверить только seed: `docker exec ct-<id> ls /workspace/project` и `sha256sum` против исходного каталога `--data` |
| T4 | Выбор ролей ИИ и этапов | нет команды | роли проверены по реестру CTF, неизвестные отклонены | **нет реализации** (ревью п. 2-3); зафиксировать, какие `role_id` реально появились в `role.started` |
| T5 | Запуск Run | `sessions send <id> "<обезличенная постановка>"` | `run.accepted` (gateway) → `run.started`; один `run_id` | ровно один `run.started` на `run_id`; `run_id` записан |
| T6 | Вопрос фасилитатора | (задача, провоцирующая `AskUserQuestion`; формулировка — `runner/evals/test_question.py`, константа `PROMPT`) | `facilitator.question` с `question_id`, статус `waiting_for_input`, `phase=awaiting_answer`, `health=ok` | в heartbeat нет `stalled`/`crashed`; gateway не объявил `heartbeat_lost` (ожидание до 15 мин проверено в `events-v0.md` §11 п. 9) |
| T7 | Ответ в тот же Run | `sessions answer <id> <fork_id> <метка>` (`fork_id` = `question_id` из журнала) | `user.answer.accepted`, затем работа; `run.started` по-прежнему один | тот же `run_id`; новых `run.accepted` нет |
| T8 | Роли и сообщения | — | пары `role.started`/`role.completed`, `message.created` | порядок `seq` без пропусков; нет `legacy.redacted` вместо публичных типов (ревью п. 5) |
| T9 | Результат хода | — | `artifact.created{path, commit_sha}`, затем (gateway) `artifact.saved`, затем `run.completed` | `run.completed` не раньше `artifact.saved` (`events-v0.md` §4.2); `git -C /workspace/project log -1` содержит `Turn-Id: <run_id>`: `docker exec ct-<id> git -C /workspace/project log -1 --format=%B` |
| T10 | Доработка | `sessions send <id> "<обезличенная доработка>"` | по текущему протоколу это **новый** `run_id`, не тот же Run (ревью п. 6) | записать фактическое поведение; ссылка на версию артефакта передаваться не может |
| T11 | Durable save | — | `artifact.saved{artifact_id, version_id, size_bytes}`; повторное чтение версии возвращает те же байты | **нет реализации канала** (`events-v0.md` §11 п. 11); для gateway из master: `GET /runs/{run_id}/events/{seq}/file` отдаёт снимок текста |
| T12 | ZIP | — | `application/zip`, начинается с `PK`, содержимое = подтверждённая версия | **нет реализации** (ревью п. 13) |
| T13 | Actual usage | — | значения только фактические; если нет — «неизвестно», не 0 | **нет реализации** (ревью п. 10); записать, что отображается при отсутствии |
| T14 | Повторное открытие | см. сценарий S4 | лента воспроизводится с `seq=1` без дыр | — |
| T15 | Остановка и очистка | `sessions stop <id>`; `sessions rm <id> --volume` | контейнер остановлен, код выхода 0; том удалён | не оставлять секреты: каталог `~/.config/coreteams/secrets/<id>` удалить вручную после прогона |

## 5. Сценарии сбоя и повтора

Каждый сценарий выполняется на отдельном свежем проекте (`<id>` новый), чтобы исходы не смешивались. Если сценарий требует модель, он выполняется в рамках разрешённого бюджета владельцем.

### S1. Падение runner (`docker kill`)

1. Дойти до T5 и дождаться идущего хода (желательно после `facilitator.question`, как `test_question.py`, второй тест; либо посреди долгой команды, как `runner/evals/test_restart.py`).
2. `docker kill ct-<id>`.
3. `docker start ct-<id>` (ручной kill не запускает политику `on-failure`, контейнер поднимается вручную).
4. Наблюдать: `turn_interrupted` → `turn_resumed{attempt, mode}`; в публичной ленте `progress.updated{recovering}` → `progress.updated{working, attempt, context_restored}`; для вопроса, висевшего при падении, — повторный `facilitator.question` с **новым** `question_id`.

Ожидания и проверки:

| Проверка | Ожидание |
|---|---|
| Число `run.started` для `run_id` | 1 |
| Терминальное событие | ровно одно и последнее; во время сбоя нет `run.failed` (сбой не провал, `events-v0.md` §4.3) |
| Ответ на старый `question_id` | `command_ack rejected unknown_fork` |
| Ответ на новый `question_id` | `accepted`, Run завершается тем же `run_id` |
| `checkpoint` | новый `sha`, `Turn-Id` совпадает с `run_id` |
| Дубли побочных эффектов | отметить: после `resume` инструмент мог выполниться дважды (`runner/README.md`, «Восстановление») |
| Рестарт-лимит | при трёх подряд — `run.failed{resume_limit, retryable=true}`, `health=crashed`, Docker `unhealthy` |
| Gateway при долгом отсутствии (>30 с без heartbeat) | `run.outcome_unknown{heartbeat_lost}`; зафиксировать, что UI показывает «исход неизвестен», а не ошибку и не успех |

### S2. Потеря WebSocket-соединения

Требует gateway (сегодня `NOT_RUN: нет реализации`). Фейковый gateway делает это в `runner/tests/test_gateway_client.py` (раздел 8).

1. Во время хода оборвать соединение: остановить gateway-процесс (вариант А) или `docker network disconnect <сеть> ct-<id>` и вернуть (вариант Б). Сделать два прогона: обрыв короче 10 с и дольше 45 с.
2. Вернуть связь.

| Проверка | Ожидание |
|---|---|
| Runner | не падает и не останавливает ход; переподключение с задержками 1, 2, 4, 8, 16, 30 с |
| После `welcome{acked_seq}` | досылаются только строки журнала с `journal_seq > acked_seq` |
| Лента gateway | нет дублей по `event_id`, `seq` без пропусков и без сдвига |
| Обрыв короче 30 с | Run продолжается; `heartbeat_lost` нет |
| Обрыв дольше 30 с | `run.outcome_unknown{heartbeat_lost}` как терминальное событие; зафиксировать, что произойдёт с Run после возврата runner (по правилу «терминальное — последнее» события не принимаются; это известный вопрос, записать факт) |
| Команда, отправленная во время обрыва | не теряется молча: клиент получает ошибку доставки или ack после восстановления; факт записать |

### S3. Повторная отправка ответа

1. На открытом вопросе отправить `fork_answer` с `id=A`, затем тот же `id=A` повторно.
2. Отправить ответ с новым `id=B` на тот же `question_id` после принятия.
3. Отправить ответ на `question_id`, которого нет.

| Проверка | Ожидание |
|---|---|
| Повтор с тем же `id` | не исполняется второй раз; ack `accepted` (дубль не различается, `protocol.md`) |
| `user.answer.accepted` | одно на `question_id` |
| Новый `id` на уже закрытую развилку | `rejected unknown_fork`; Run не стартует заново |
| Несовпадающие ключи `answers` | `rejected answers_mismatch`, ничего не записано |
| `run.started` для Run | по-прежнему 1; в автономном режиме `sessions answer` повторно — проверить, что второго `fork_answered` нет |
| Через web/API gateway | повтор запроса с тем же ключом идемпотентности возвращает то же состояние Run (ревью п. 4, 12) |

Дополнительно **S3b — повтор запуска**: отправить `message` с тем же `id` дважды (в gateway — тот же `client_request_id`). Ожидание: один Run, один `run.accepted`, один `run.started`.

### S4. Повторное открытие после restart

1. Довести Run до `run.completed` (или до `facilitator.question`).
2. Перезапустить gateway (и, отдельным прогоном, runner: `sessions restart <id>`).
3. Открыть проект заново: загрузить ленту `GET /runs/{run_id}/events?after_seq=0` (gateway из master, `backend/api/history.py:109`) и свернуть её.
4. Для web: открыть проект с другого браузерного контекста (кроме локальных данных браузера).

| Проверка | Ожидание |
|---|---|
| Лента | та же последовательность `seq` без дыр, событий не меньше, чем до перезапуска |
| Завершённый Run | остаётся `completed`, артефакт и `version_id` читаются, байты совпадают с до-рестартными |
| Незавершённый Run при рестарте gateway | gateway из master переводит активные Run в `outcome_unknown` (`backend/repository.py:641`); записать, не объявлен ли так Run, который runner на деле продолжает (ревью п. 15) |
| Рестарт runner (`sessions restart`) | `turn_interrupted` → `turn_resumed`, либо ход, завершённый коммитом, достраивается событиями без повторного исполнения |
| Web без локального состояния | список проектов и Run доступен с другого контекста; в PR #11 это не реализовано (контракт V2: «B only reopens browser snapshots») → `NOT_RUN: нет реализации` |

### S5. Ошибка без подмены успехом

Подготовить заведомо проваливаемый ход (например недоступный cliproxy при запуске на короткое время или ход с `--stall-after 30` и долгой командой, как `runner/evals/test_stall.py`).

| Проверка | Ожидание |
|---|---|
| Недоступный cliproxy дольше ~2,5 мин | выход `1`, перезапуск Docker; в ленте `progress.updated{interrupted}`, не `run.completed` |
| Нет секрета | выход `78`, `hello` не отправлен |
| Зависание | `health=stalled`, Docker `unhealthy` |
| `run.failed` | содержит `code` и `retryable`; UI показывает код, а не «готово» |
| Нет `artifact.saved` | `run.completed` не публикуется (`artifact_save_failed` / `artifact_save_unknown`) |

## 6. Правила записи наблюдений

- В протокол попадают: типы и счётчики событий, `seq`, `run_id`, `question_id`, `commit_sha`, коды ошибок, коды выхода, время UTC, размеры, hash файлов.
- В протокол не попадают: тексты постановок, ответы модели, содержимое файлов, имена людей и проектов, адреса стенда, значения env и секретов, токены, содержимое `events.jsonl` целиком.
- Фрагменты журнала хранить локально у исполнителя; в протоколе ссылаться номером шага и `seq`.
- Для постановки использовать синтетический вход (`runner/evals/fixtures/demo` и формулировку из `runner/evals/test_question.py`).
- Скриншоты UI: только со стендом и без персональных данных.

## 7. Шаблон обезличенного протокола

Копировать в отдельный файл прогона (не в этот документ). Статус: `PASS` / `FAIL` / `NOT_RUN`. Для `NOT_RUN` в «факте» причина: `нет реализации` / `нет предусловия` / `не запускалось по решению`.

```markdown
# Протокол технического E2E Run — <дата UTC>

Исполнитель: <инициалы или роль, без имён> · Стенд: <local|stand-N> · Тип: технический E2E

## Шапка
| Поле | Значение |
|---|---|
| build.repo_commit | |
| build.runner_image_id | |
| build.runner_version | |
| build.ctf_commit | |
| build.gateway_commit | |
| build.web_commit | |
| ids.project | |
| ids.run | |
| ids.question (до / после рестарта) | |
| ids.checkpoint | |
| ids.artifact / version | |
| time.t0 | |

## Предусловия
| Шаг | Ожидание | Факт | Доказательство | Статус |
|---|---|---|---|---|
| P0 коммит и дерево | | | | |
| P1 Docker | | | | |
| P3 образ | | | | |
| P4 имена env присутствуют | | | | |
| P5 секрет проекта | | | | |
| P6 gateway для runner | | | | |
| P7 хранилище | | | | |
| P8 стенд | | | | |

## Основной путь
| Шаг | Ожидание | Факт | Доказательство | Статус |
|---|---|---|---|---|
| T1 старт | running, healthy | | status, RestartCount | |
| T2 hello/welcome | | | | |
| T3 файлы bytes+hash | | | | |
| T4 роли и этапы | | | | |
| T5 запуск Run | один run.started | | seq, run_id | |
| T6 вопрос | facilitator.question, awaiting_answer | | seq, question_id | |
| T7 ответ в тот же Run | один run.started | | seq | |
| T8 роли и сообщения | | | | |
| T9 результат хода | checkpoint, artifact.created | | commit_sha | |
| T10 доработка | | | | |
| T11 durable save | artifact.saved до run.completed | | version_id, size | |
| T12 ZIP | PK, содержимое = версия | | hash | |
| T13 actual usage | факт или «неизвестно» | | | |
| T14 reopen | лента без дыр | | | |
| T15 остановка | exit 0 | | | |

## Сценарии сбоя и повтора
| Шаг | Ожидание | Факт | Доказательство | Статус |
|---|---|---|---|---|
| S1 docker kill: recovering→working, один run.started | | | | |
| S1 unknown_fork на старый question_id | | | | |
| S2 обрыв WS <10 с: без дублей, seq без дыр | | | | |
| S2 обрыв WS >45 с: heartbeat_lost | | | | |
| S3 повтор ответа тем же id | | | | |
| S3 новый id на закрытую развилку | | | | |
| S3b повтор запуска: один Run | | | | |
| S4 reopen после рестарта gateway | | | | |
| S4 reopen после рестарта runner | | | | |
| S5 ошибка не подменена успехом | | | | |

## Отклонения и дефекты
| № | Шаг | Описание без содержимого данных | Серьёзность | Владелец |
|---|---|---|---|---|

## Итог
Технический E2E: PASS / FAIL / PARTIAL. Пользовательский замер: NOT_RUN.
```

## 8. Выполнено локально (без внешних зависимостей)

Что можно запустить без модели, без секретов и без настоящего gateway: тесты `runner/` (фейковый gateway и перевод журнала в v0 работают в процессе теста) и тесты `backend/`. Выполнено 2026-10-15 в рабочем дереве ветки `e2e-runbook-pr11-review` (основа: `facilitator-question-resume`, `0ac2f62`), Docker CLI 28.3.2, **демон Docker не запущен** (`Cannot connect to the Docker daemon`), поэтому образ не собирался и контейнеры не запускались.

| Команда | Каталог | Результат |
|---|---|---|
| `uv run pytest -q` | `runner/` | `278 passed in 5.77s` |
| `uv run pytest -q tests/test_gateway_client.py tests/test_v0.py tests/test_recovery.py tests/test_main_recovery.py tests/test_inbox.py` | `runner/` | `69 passed in 1.68s` |
| `uv run ruff check .` | `runner/` | `All checks passed!` |
| `uv run mypy src tests evals` | `runner/` | `Success: no issues found in 55 source files` |
| `uv run pytest evals -m e2e -q` | `runner/` | `6 skipped` (нет Docker-демона и/или env шлюза; по условию задачи реальный прогон с моделью не запускался) |
| `uv run ctrunner-v0 demo-curriculum < tests/journals/happy.events.jsonl` | `runner/` | 13 событий: `run.started`, `message.created`, `facilitator.question`, `user.answer.accepted`, `role.started` ×2, `message.created`, `role.completed`, `message.created`, `role.completed`, `message.created`, `artifact.created`, `run.completed` |
| `uv run --no-project --with "pytest>=8.3" --with pytest-asyncio --with httpx --with aiosqlite --with alembic --with fastapi --with pydantic-settings --with "sqlalchemy[asyncio]" --with "uvicorn[standard]" python -m pytest -q backend/tests` | корень репозитория | `60 passed, 1 warning` (зависимости backend берутся на время запуска; `sqlalchemy[asyncio]` нужен ради `greenlet`) |

Какие шаги runbook это покрывает:

| Шаг runbook | Чем покрыто локально | Что не покрыто |
|---|---|---|
| T2 hello/welcome, досылка журнала | `tests/test_gateway_client.py`: `test_hello_then_events_after_welcome`, `test_events_up_to_acked_seq_are_not_resent` | настоящий gateway |
| T5-T7 команды и ack | `test_commands_are_acked_and_land_in_inbox`, `tests/test_inbox.py`, `tests/test_protocol.py` | вопрос от живой модели |
| T8-T9 перевод в публичную ленту | `tests/test_v0.py`, fixture `docs/contracts/fixtures/*.v0.jsonl`, `ctrunner-v0` на журналах | журнал с живого контейнера |
| S1 восстановление | `tests/test_recovery.py`, `tests/test_main_recovery.py`, `tests/test_checkpoint.py` | реальный `docker kill` |
| S2 обрыв и переподключение | `test_reconnect_resends_nothing_acked_and_continues`, `test_wrong_token_never_crashes_client` | сетевой обрыв контейнера, `heartbeat_lost` |
| S3 повтор команды | идемпотентность по `id` в `tests/test_inbox.py` | `unknown_fork` на живом вопросе (только e2e) |
| T14/S4 чтение ленты и история | `backend/tests/test_history_api.py`, `test_public_events.py` | реальный рестарт gateway под нагрузкой |
| Heartbeat и зависание | `tests/test_liveness.py`, `tests/test_healthcheck.py` | Docker `unhealthy` |

Повторить e2e на фейковом gateway **с живой моделью** (когда демон Docker запущен, образ собран, env шлюза загружен, бюджет согласован; стоит десятки тысяч токенов, ~2 мин, из `runner/`):

```bash
uv run pytest evals -m e2e -q
```

Покрывает T1, T5-T9 и S1 на фейке (`test_start`, `test_question` оба сценария, `test_restart`, `test_isolation`, `test_stall`). Это **не** E2E через настоящий gateway и не закрывает PPS-138.

## 9. Пользовательский замер (отдельно, `NOT_RUN`)

Технический E2E не доказывает пользу для пользователя. Замер выполняется отдельно с согласием участника, по [операторскому руководству PR #11](https://github.com/AcryCxde/PP-Sber-Autumn-2026/pull/11) (`web-product/docs/journey-metrics.md`, ветка PR) и владельцем исследования (Катя).

| Шаг | Ожидание | Факт | Доказательство | Статус |
|---|---|---|---|---|
| U1 согласие участника и канал выгрузки | согласие записано отдельно | не запускалось по решению | — | NOT_RUN |
| U2 базовая попытка без продукта | журнал baseline | не запускалось по решению | — | NOT_RUN |
| U3 попытка с продуктом на общем стенде | время до сохранённого результата, барьеры | нет предусловия: путь через gateway недоступен | — | NOT_RUN |
| U4 сопоставление baseline и продукта | одинаковый участник и задача | не запускалось по решению | — | NOT_RUN |

В протокол технического E2E данные пользовательского замера не переносятся.

## 10. Критерий завершения PPS-138

Технический E2E считается пройденным, когда в протоколе `PASS`: T1-T9 и T11 на настоящем gateway; S1-S4 выполнены; `artifact.saved` предшествует `run.completed`; последовательность типов совпадает с `docs/contracts/fixtures/happy.v0.jsonl`. До этого честный итог: `PARTIAL`, список `NOT_RUN` с причинами — в протоколе.
