# План исправления замечаний `plans/review.md`

## Context

Текущий gateway напрямую запускает Claude CLI, трактует его `stream-json` как публичную модель событий и использует корень собственного репозитория как рабочий каталог. Из-за этого внутренние данные CLI, пути и содержимое инструментов попадают в БД/REST/WebSocket, перезапуск backend оставляет диалоги навсегда занятыми, а подписки могут бесконечно блокировать сокет.

Цель — закрыть все 8 замечаний до демо, сохранив существующую прямую интеграцию с CLI. Полную миграцию на `events.v0` сейчас не выполнять: контракт предполагает отдельный runner, heartbeat, стабильные runner `event_id`, `project_id`, checkpoint-backed `artifact.created` и подтверждение storage, которых в этой ветке нет. При этом использовать его ключевые правила: внутренние события не являются публичными, терминальное событие одно и последнее, неизвестный исход отличается от ошибки, артефакт содержит только безопасные метаданные.

## 1. Отделить внутренний поток CLI от публичной ленты

- Добавить модуль публичных событий (например, `backend/public_events.py`) с типизированными builders/allowlist для разрешённых payload и единой сериализацией REST/WS.
- Оставить `backend/parser.py` внутренним адаптером CLI: `session`, tool calls/results, thinking, raw diagnostics и result metrics могут использоваться только оркестратором и никогда напрямую не передаются в `append_event()`.
- Публиковать только безопасные продуктовые события: принятие запуска, текст ответа, метаданные сохранённого артефакта и явные terminal events. Не публиковать `session`, `tool`, `tool_result`, `thinking`, `raw`, `raw_event`.
- В `backend/repository.py` заменить приём произвольного `Mapping` на публичный event draft либо обязательную валидацию типа и полей до записи. Безопасная проекция должна происходить **до** persistence, чтобы одинаково защищать live delivery и replay.
- В `finalize_run()` перестать разворачивать `**result_event`. Метрики и `claude_session_id` при необходимости оставить только в полях `Run`; terminal payload собирать явным allowlist без `session_id`, cwd, cost/tokens, permission denials, subagent stats, raw result и exception text.
- Сохранить существующий wire format только там, где это снижает риск перед демо, но привести terminal semantics к `run.completed`, `run.failed` и `run.outcome_unknown`; не маркировать эту промежуточную схему как `events.v0`.

## 2. Изолировать рабочие каталоги и обезопасить пути артефактов

- В `backend/config.py` добавить `workspace_root`, расположенный вне `PROJECT_ROOT` и каталога БД; на startup канонизировать пути и отказывать в запуске при пересечении.
- Добавить `backend/workspace.py`: стабильный workspace на `conversation_id`, его создание и единая функция нормализации artifact path.
- Передавать workspace в `start_claude()` и использовать его как `cwd`; убрать любой fallback на `PROJECT_ROOT`. Один диалог должен повторно использовать тот же workspace, потому что resume-сессия зависит от cwd.
- При миграции инвалидировать старые `claude_session_id` и пометить существующий контекст как unavailable: сессии, созданные в cwd репозитория gateway, нельзя безопасно продолжать в новом workspace.
- Для артефакта разрешать только успешный, коррелированный `Write` (либо другой инструмент только после отдельного надёжного extractor). Разрешать путь только внутри workspace после canonical/symlink checks; исключить `.git` и `.runner`; хранить и публиковать POSIX-relative path.
- Не перечитывать файл при скачивании: использовать уже существующий persisted snapshot. Путь вне workspace не должен создавать `Artifact` или публичное событие и не должен попадать в диагностический текст клиента.

## 3. Исправить subprocess и parser

- В `backend/main.py` разделить stdout/stderr (`stderr=PIPE`) и дренировать оба потока конкурентно, чтобы исключить deadlock.
- stderr и невалидные строки stdout направлять только в bounded/redacted server logging; не создавать из них события и не переносить их текст в `Run.error`/`run.failed`.
- Зафиксировать санитизированный fixture реального `claude --output-format stream-json` в `backend/tests/fixtures/`.
- Обновить `backend/parser.py` по реальному формату: читать top-level `tool_use_result`, сохранять `is_error`, коррелировать результат с исходным tool call. Удалить синтетическое предположение, что file payload внутри каждого content block является доверенным источником артефакта.
- Удалить общий fallback, превращающий произвольный file-shaped `tool_result` в артефакт. Артефакт создавать только из успешного `Write`, используя его in-memory input после проверки пути; failed Write, Read, Bash, Edit и неизвестные инструменты не регистрируют скачиваемый файл.

## 4. Сделать lifecycle восстанавливаемым

- Расширить terminal statuses в `backend/repository.py` значением `outcome_unknown`; добавить `run.outcome_unknown` с безопасным кодом (`heartbeat_lost`/`backend_restarted`).
- Обобщить `finalize_run()` так, чтобы status update, context-state update и ровно одно terminal event записывались атомарно и идемпотентно.
- Запретить обычный `append_event()` после terminal status; terminal event должен быть последним.
- Добавить `reconcile_orphaned_runs()` и вызывать его в lifespan сразу после `migrate()`: все оставшиеся `pending`/`running` перевести в `outcome_unknown`, пометить conversation context unavailable, очистить resumable session ID и освободить unique active-run constraint.
- Явно задокументировать, что startup reconciliation рассчитан на текущий single-process deployment; для multi-worker/runner архитектуры позже потребуется owner/lease/heartbeat.
- При graceful shutdown остановить активные child processes и shield/await terminal transaction; при kill/crash восстановление выполнит следующий startup.

## 5. Убрать бесконечные и блокирующие WebSocket handlers

- В `backend/main.py` отделить lifetime запуска от сокета: `run.create` запускает application-owned task, поэтому disconnect не отменяет уже принятый run.
- `run.subscribe` запускать как socket-owned task; на соединении хранить набор subscription tasks и отменять их при disconnect. Повторная подписка на тот же run заменяет предыдущую.
- Сериализовать конкурентные `send_text()` через per-socket lock/очередь.
- Сделать одну подписку конечным long-poll окном: replay всех доступных страниц, затем либо terminal `replay.complete`, либо по deadline `replay.complete` с `terminal: false` и последним `seq`. Frontend повторно подписывается от последнего contiguous `seq`.
- Включить `outcome_unknown` в terminal handling. Проверять gap в последовательности: клиент не применяет события после дыры, а запрашивает replay от последнего непрерывного номера.

## 6. Очистить уже сохранённые чувствительные данные

Добавить Alembic migration под `backend/migrations/versions/`:

- полностью заменить payload старых `session`, `tool`, `tool_result`, `thinking`, `raw`, `raw_event` и неизвестных типов фиксированным redacted placeholder, сохраняя `seq`;
- для старых terminal/result events оставить только безопасный статус/код;
- очистить `runs.error`, поскольку там мог сохраниться merged stderr/raw result;
- удалить старые `Artifact` snapshots и file metadata: они могли быть получены из репозитория gateway и содержать абсолютные пути или секреты;
- очистить старые session IDs и сделать context unavailable до явного reset;
- оставить защитную публичную проекцию в runtime, чтобы даже вручную вставленная или частично мигрированная запись не выдала произвольный payload.

Миграция security-destructive: перед deployment сделать backup БД, но небезопасную копию не обслуживать приложением.

## 7. Обновить frontend под безопасную ленту

- Вынести decode/fold событий из `frontend/src/App.tsx` в отдельный тестируемый модуль (например, `frontend/src/events.ts`).
- Удалить UI-зависимости от session/tool/tool_result/raw/cost/subagent fields; отображать только текстовые сообщения, безопасные статусы и relative artifact metadata.
- Обработать `run.completed`, `run.failed` и `run.outcome_unknown`; unknown должен останавливать spinner и предлагать явный context reset, не изображая успешный или известный failed outcome.
- Инициализировать replay cursor из REST history, дедуплицировать по `(run_id, seq)`, не применять событие через gap и повторять bounded subscription после non-terminal `replay.complete`.
- Добавить Vitest и unit-тесты чистого reducer/decoder: replay + live duplicate, gap, reconnect, outcome unknown, artifact, redacted/unknown internal event.

## 8. Тесты по каждому замечанию

Обновить `backend/tests/test_parser.py`, `test_repository.py`, `test_history_api.py`, `test_migrations.py` и добавить real-stream fixture:

1. `Popen.cwd` указывает на workspace диалога вне gateway; resume использует тот же cwd.
2. Startup атомарно закрывает seeded pending/running runs как outcome unknown, не создаёт второй terminal event и позволяет новый run после reset.
3. Subscription заканчивает long-poll окно; в это время тот же socket обрабатывает другую команду; disconnect не отменяет run.
4. Sentinel secrets в Read/Edit/Bash/Write input/result отсутствуют в SQLite events, REST и WS.
5. Session ID, cwd, cost, permission denials, subagent stats и raw result отсутствуют в terminal payload.
6. Absolute in-workspace path становится relative; outside/symlink/`.git`/`.runner` path не создаёт artifact и нигде не раскрывается.
7. Secret sentinel из stderr отсутствует в events, messages и `Run.error`.
8. Fixture с top-level `tool_use_result` корректно различает successful Write, failed Write и не-write tool results.

Дополнительно проверить: terminal event одно и последнее, append после terminal отклоняется, artifact snapshot сохраняется до completed, replay остаётся последовательным после redaction migration.

## 9. Документация и граница с полным `events.v0`

- Обновить `ARCHITECTURE_AND_SESSION_DATA.md`, `plans/RUN_APPLICATION.md` и `.env.example`: workspace policy, private/public boundary, unknown outcome, startup recovery и bounded subscriptions.
- В `plans/events-v0.md` не отмечать gateway как полностью совместимый. Отдельным follow-up оставить интеграцию runner branch: настоящий `project_id`, source-stable `event_id` dedupe, roles/facilitator/progress, checkpoint-backed `artifact.created`, storage-confirmed `artifact.saved`, heartbeat/recovery и окончательное согласование cancellation.

## Verification

Автоматические проверки:

```bash
python -m pytest
cd frontend
npm run test
npm run lint
npm run build
```

Ручной E2E перед демо:

1. Выполнить Read/Bash/Write и проверить WS, REST и SQLite: нет tool I/O, host paths, session IDs и stderr.
2. Скачать созданный файл: путь публично relative, содержимое берётся из snapshot и не меняется после изменения live-файла.
3. Попытаться записать файл вне workspace и через symlink: артефакт не регистрируется, путь не раскрывается.
4. Убить backend во время run, перезапустить и убедиться, что run стал outcome unknown, UI перестал ждать, а диалог разблокирован после reset.
5. Держать run дольше subscription window: задачи подписки не накапливаются, сокет продолжает принимать команды, replay возобновляется с последнего contiguous `seq`.
6. Обновить копию БД старой схемы и убедиться, что прежние tool/session/raw payload и artifacts больше не доступны.

## Critical files

- `backend/main.py` — subprocess, task ownership, WS dispatch/subscription, lifespan recovery.
- `backend/repository.py` — public persistence boundary, terminal transaction, orphan reconciliation.
- `backend/parser.py` — private CLI parsing and real stream shape.
- `backend/config.py`, новый `backend/workspace.py`, новый `backend/public_events.py`.
- `backend/models.py`, `backend/schemas.py`, `backend/api/history.py`, новая Alembic migration.
- `backend/tests/test_parser.py`, `backend/tests/test_repository.py`, `backend/tests/test_history_api.py`, `backend/tests/test_migrations.py`.
- `frontend/src/App.tsx`, новый `frontend/src/events.ts` и protocol tests.
