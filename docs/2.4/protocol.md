# Протокол runner ↔ gateway

Публичная лента для web — `docs/contracts/events-v0.md`; события ниже — внутренний журнал runner, их переводит `ctrunner.v0`.

Контракт для направления 2.1. Версия конверта `v: 1`.
Пометка **этап 2** — сообщение описано, но в runner этапа 1 не реализовано.
Исключение: события восстановления (`checkpoint`, `turn_interrupted`, `turn_resumed`, новые `reason` у `turn_failed`) runner уже пишет в `events.jsonl`; по WebSocket их отправит клиент этапа 2.

## Транспорт

- WebSocket, соединение открывает runner (у контейнера нет входящих портов). Адрес — `GATEWAY_URL`.
- Авторизация: заголовок `Authorization: Bearer <gateway_token>`; токен — файл в `/run/secrets`.
- Одно JSON-сообщение на кадр, UTF-8. Конверт: `{"v": 1, "type": "<тип>", ...поля}`.
- Без `GATEWAY_URL` runner работает автономно: события пишутся только в `events.jsonl`, команды — через inbox.

## Сообщения

| Направление | `type` | Поля | Этап |
|---|---|---|---|
| runner → gw | `hello` | `project_id`, `runner_version`, `session_id` \| null, `last_seq` | 2 |
| gw → runner | `welcome` | `acked_seq`: runner повторяет всё, что после него | 2 |
| runner → gw | `heartbeat` | `phase`, `health`, `last_progress_at`, `turn_id` \| null | 2 (этап 1 — `health.json`) |
| runner → gw | `event` | `seq`, `ts`, `turn_id` \| null, `kind`, `payload` | 1 (журнал), 2 (отправка) |
| gw → runner | `command` | `id`, `kind` и поля команды | 1 (inbox), 2 (WS) |
| runner → gw | `command_ack` | `id`, `status`: `accepted` \| `duplicate` \| `rejected`, `reason` | 2 |

Значения:

- `phase`: `idle` | `working` | `awaiting_answer` | `failed` (SDK завершился с ошибкой или исчерпан `resume_limit`).
- `health`: `ok` | `stalled` | `crashed`. Правило — `docs/2.4/spec.md`, раздел 3.
- `ts`, `last_progress_at` — Unix-время в секундах (float).

### hello (этап 2)

```json
{"v": 1, "type": "hello", "project_id": "demo-a", "runner_version": "0.1.0",
 "session_id": "749df2fe-1b50-4c46-92f3-e2cdf0ce322d", "last_seq": 41}
```

Отправляется после успешного preflight (конфиг, секрет, ключ проверен в cliproxy). Если секрета нет, `hello` не будет.

### welcome (этап 2)

```json
{"v": 1, "type": "welcome", "acked_seq": 37}
```

### heartbeat

Каждые 10 с. Сессия считается упавшей, если heartbeat не приходил дольше 30 с.

```json
{"v": 1, "type": "heartbeat", "phase": "working", "health": "ok",
 "last_progress_at": 1790336519.43, "turn_id": "4448abd17ad543b9aebb0eaea81df2f7"}
```

На этапе 1 то же содержимое (без `v`, `type`, `turn_id`, с `written_at`) лежит в `/run/ctrunner/health.json`.

### command

```json
{"v": 1, "type": "command", "id": "c1", "kind": "message", "text": "Сделай ревью README"}
{"v": 1, "type": "command", "id": "c2", "kind": "fork_answer", "fork_id": "9f1c…", "answers": {"Какой вариант?": "Первый"}}
{"v": 1, "type": "command", "id": "c3", "kind": "stop"}
```

- `message` во время хода ставится в очередь и стартует после его завершения.
- `fork_answer.answers`: текст вопроса → выбранная метка (или свой ответ).
- `id` — ключ идемпотентности: повтор с тем же `id` не исполняется.
- Этап 1: команда — это JSON-файл `/workspace/.runner/inbox/<ns>-<uuid>.json` без `v`/`type`;
  `ctrunner-inbox` (или `sessions send|answer`) пишет его атомарно (tmp + rename).
  `ctrunner-inbox fork-answer <fork_id> <label>...` сопоставляет метки вопросам развилки по порядку из журнала.

### command_ack (этап 2)

```json
{"v": 1, "type": "command_ack", "id": "c1", "status": "accepted"}
{"v": 1, "type": "command_ack", "id": "c1", "status": "duplicate"}
{"v": 1, "type": "command_ack", "id": "c9", "status": "rejected", "reason": "unknown command kind: 'bogus'"}
```

## События

Строка `events.jsonl` = тело `event` без `v`/`type`:

```json
{"seq": 4, "ts": 1790336514.45, "turn_id": "4448abd17ad543b9aebb0eaea81df2f7", "kind": "turn_started", "payload": {"prompt": "Ответь одним словом: ок"}}
```

| `kind` | `payload` | Этап |
|---|---|---|
| `session_started` | `{"project_id", "runner_version"}` | 1 |
| `turn_started` | `{"prompt"}` | 1 |
| `sdk` | нормализованное сообщение SDK (формат `claude --output-format stream-json`) | 1 |
| `fork_question` | `{"fork_id", "questions": [...]}` — вопросы AskUserQuestion как есть | 1 |
| `fork_answered` | `{"fork_id", "answers": {вопрос: ответ}}` | 1 |
| `turn_completed` | `{}` | 1 |
| `turn_failed` | `{"reason": "result_error" \| "sdk_crashed" \| "disk_full" \| "checkpoint_failed" \| "resume_limit" \| "state_corrupt", "fatal"?: true}` — `fatal`: процесс упал, после рестарта ход может продолжиться | 1 (`checkpoint_failed`, `resume_limit`, `state_corrupt` — 2) |
| `access_denied` | `{"tool", "path", "agent_id" \| null}` — `path` после `realpath` | 1 |
| `checkpoint` | `{"sha"}` — git commit с trailer `Turn-Id` в конце хода | 2 |
| `turn_interrupted` | `{}` — рестарт посреди хода, исход неизвестен | 2 |
| `turn_resumed` | `{"attempt": 1..3, "mode": "resume" \| "fresh"}` — автопродолжение после рестарта: `resume` продолжает SDK-сессию, `fresh` — новая сессия (транскрипта нет или `resume` не подключился) | 2 |

`turn_failed.reason`:

| `reason` | Когда |
|---|---|
| `result_error` | SDK вернул `result` с ошибкой |
| `sdk_crashed` | runner упал (выход 1) |
| `disk_full` | ENOSPC при commit (runner работает дальше) или при записи `state.json`/журнала (runner падает) |
| `checkpoint_failed` | commit упал по другой причине; дерево не тронуто |
| `resume_limit` | 3 автопродолжения подряд не завершили ход; `turn_id` — провалившийся ход, runner жив с `health=crashed` до новой команды |
| `state_corrupt` | `state.json` не разобран; `turn_id: null`, выход 65 |

Восстановление после рестарта (`turn_id` во всех событиях — прерванный ход, повторно он не открывается):

- `turn_interrupted` → `turn_resumed{attempt, mode}` → дальше обычный ход, `turn_started` второй раз не пишется;
- `turn_interrupted` → `turn_failed{resume_limit}` — лимит исчерпан;
- `checkpoint` → `turn_completed` без `turn_interrupted` — commit успел до рестарта; дописываются только события, которых нет в журнале.

Примеры:

```json
{"seq": 1, "ts": 1790336493.2, "turn_id": null, "kind": "session_started", "payload": {"project_id": "demo-a", "runner_version": "0.1.0"}}
{"seq": 7, "ts": 1790336519.4, "turn_id": "4448…", "kind": "sdk", "payload": {"type": "assistant", "parent_tool_use_id": null, "message": {"model": "claude-5.6-sol", "content": [{"text": "ок"}]}}}
{"seq": 9, "ts": 1790336519.4, "turn_id": "4448…", "kind": "turn_completed", "payload": {}}
{"seq": 12, "ts": 1790336530.0, "turn_id": "4448…", "kind": "fork_question", "payload": {"fork_id": "9f1c…", "questions": [{"question": "Какой вариант?", "options": [{"label": "Первый"}, {"label": "Второй"}]}]}}
{"seq": 13, "ts": 1790336561.0, "turn_id": "4448…", "kind": "fork_answered", "payload": {"fork_id": "9f1c…", "answers": {"Какой вариант?": "Первый"}}}
{"seq": 20, "ts": 1790336600.0, "turn_id": "ae5c…", "kind": "turn_failed", "payload": {"reason": "sdk_crashed"}}
{"seq": 31, "ts": 1790336640.0, "turn_id": "4448…", "kind": "turn_interrupted", "payload": {}}
{"seq": 32, "ts": 1790336640.0, "turn_id": "4448…", "kind": "turn_resumed", "payload": {"attempt": 1, "mode": "resume"}}
{"seq": 40, "ts": 1790336702.0, "turn_id": "4448…", "kind": "checkpoint", "payload": {"sha": "3f9a1c7e0b52d84a6c1e9f03b7a5d2c48e6f1a09"}}
{"seq": 29, "ts": 1790336606.1, "turn_id": "bdee…", "kind": "access_denied", "payload": {"tool": "Read", "path": "/workspace/.runner/events.jsonl", "agent_id": null}}
```

Потоковые токены (partial) наружу не отправляются: runner использует их только как признак прогресса.
Значения секретов вырезаются (`***`) до записи на диск.

## Гарантии доставки

- `seq` монотонный, хранится на диске: событие сначала пишется в `events.jsonl` (`fsync`), потом отправляется.
  Недописанная последняя строка после обрыва отрезается при старте; её `seq` используется заново.
- События — at-least-once: после переподключения runner повторяет всё после `welcome.acked_seq`;
  гейтвей убирает дубли по `seq` (**этап 2**).
- Команды идемпотентны по `id`. Этап 1: inbox помнит обработанные `id` (каталог `processed/`) и между рестартами.
  Для `message` доставка — at-least-once до `ack`, исполнение идемпотентно по `id`: команда сначала записывается в `state.json`
  (очередь или открытый ход), и только затем файл переносится в `processed/`. Падение между записью и переносом
  не теряет команду и не исполняет её дважды: при старте она подтверждается без повторного исполнения.
  `fork_answer` и `stop` подтверждаются до исполнения (at-most-once): развилку рестарт всё равно прерывает.
- Порядок записи в конце хода: commit → `checkpoint` → `turn_completed` → `state.json` (события раньше state, чтобы падение
  между ними не потеряло их навсегда); в начале хода: `state.json` → `query`.
- Порядок событий внутри сессии совпадает с порядком `seq`.
