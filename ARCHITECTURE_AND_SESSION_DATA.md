# Архитектура и данные сессии Claude Web

## 1. Что в приложении называется сессией

В системе существуют два независимых, но связанных вида состояния.

### 1.1. Conversation приложения

Conversation — серверная запись чата в SQLite. Она используется для:

- отображения истории во frontend;
- хранения canonical user/assistant messages;
- группировки запусков Claude;
- восстановления интерфейса после reload;
- хранения статуса контекста;
- привязки к Claude Code session.

Conversation является источником истины для UI и аудита приложения.

### 1.2. Claude Code session

Claude Code session — внутренний контекст Claude Code CLI. В нём находятся:

- предыдущие пользовательские turns;
- ответы Claude;
- сведения об agent/tool execution;
- контекст рабочего каталога;
- внутреннее состояние Claude Code, необходимое для продолжения работы.

Приложение не читает и не изменяет внутренний файл session напрямую. Backend хранит только непрозрачный `claude_session_id` и передаёт его CLI:

```bash
claude --print --verbose --output-format stream-json \
  --resume <claude_session_id>
```

Для первого запроса `--resume` не используется. Session ID принимается из `system/init` или финального `result` Claude CLI и сохраняется на сервере.

## 2. Формат данных сессии

### 2.1. Идентификатор Claude session

Для приложения Claude session представлена строкой:

```json
{
  "claude_session_id": "654bb864-d96b-4383-a099-28c069541e3e"
}
```

Это непрозрачный server-side идентификатор. Frontend его не получает и не может прислать произвольный session ID.

Backend проверяет, что session ID, полученный после `--resume`, совпадает с ID, сохранённым в conversation. При несовпадении context переводится в `unavailable`.

### 2.2. Состояние контекста conversation

Поле `context_state` принимает значения:

| Значение | Описание |
|---|---|
| `new` | Для conversation ещё не создана подтверждённая Claude session |
| `active` | Session ID сохранён и может использоваться через `--resume` |
| `reset` | Пользователь явно сбросил старый контекст; новая session ещё не подтверждена |
| `unavailable` | Session потеряна, недоступна или вернула неожиданный ID |

Frontend получает `context_state`, но не получает сам `claude_session_id`.

### 2.3. Данные conversation в БД

Основные поля таблицы `conversations`:

```text
id
 title
 status
 claude_session_id       # только backend
 context_state
 next_message_seq
 created_at
 updated_at
```

`next_message_seq` атомарно увеличивается для каждого canonical сообщения внутри чата.

### 2.4. Данные run

Run — одна попытка выполнить пользовательский запрос:

```text
id
conversation_id
client_request_id
status                   # pending | running | completed | failed | cancelled
context_mode             # new | resume | reset
resume_session_id        # ID, который backend пытался продолжить
claude_session_id        # ID, фактически полученный от CLI
error_code
next_event_seq
started_at
completed_at
duration_ms
cost_usd
turns
exit_code
error
```

`client_request_id` глобально уникален в текущем single-user backend и обеспечивает идемпотентность retry/reconnect.

Partial unique index запрещает два `pending`/`running` run внутри одной conversation.

### 2.5. Canonical messages

Сообщения хранятся в таблице `messages`:

```text
id
conversation_id
run_id
role                     # user | assistant | system
kind                     # text | context_reset
sequence
content
created_at
```

Пример:

```json
{
  "id": "message-uuid",
  "conversation_id": "conversation-uuid",
  "run_id": "run-uuid",
  "role": "user",
  "kind": "text",
  "sequence": 3,
  "content": "Продолжи предыдущую задачу",
  "created_at": "2026-09-29T12:00:00Z"
}
```

### 2.6. Потоковые events

Events — append-only журнал одного run:

```text
id
run_id
seq
 type
 payload JSON
 created_at
```

Пара `(run_id, seq)` уникальна. Sequence выделяется атомарно.

Каждое событие сначала сохраняется в БД и только затем отправляется frontend. Terminal event сохраняется в одной транзакции с terminal status и assistant message.

### 2.7. Файловые артефакты

Текстовые snapshots файлов хранятся отдельно от events:

```text
artifacts
- id
- run_id
- event_seq
- path
- content                 # server-side, не входит в event payload
- size_bytes
- truncated
- created_at
```

Пара `(run_id, event_seq)` уникальна. Public `tool_result` event содержит только metadata:

```json
{
  "file": {
    "path": "plans/result.md",
    "content_size_bytes": 2048,
    "content_truncated": false,
    "num_lines": 42
  }
}
```

Claude Code CLI для `Write` передаёт path/content во входе tool call, а result возвращает только текстовое подтверждение. Backend связывает события по `tool_id`, создаёт Artifact после успешного result и удаляет `content` из публичного tool event.

## 3. Схема передачи данных

## 3.1. Создание первого запроса

Frontend отправляет:

```json
{
  "type": "run.create",
  "conversation_id": null,
  "client_request_id": "client-generated-uuid",
  "content": "Первый запрос",
  "context_mode": "resume"
}
```

Так как conversation ещё нет, backend:

1. создаёт conversation;
2. создаёт pending run;
3. сохраняет canonical user message;
4. сохраняет event `run.accepted`;
5. запускает Claude CLI без `--resume`.

## 3.2. Canonical acknowledgement

Backend отвечает persisted-событием:

```json
{
  "type": "run.accepted",
  "schema_version": 1,
  "conversation_id": "conversation-uuid",
  "run_id": "run-uuid",
  "seq": 1,
  "client_request_id": "client-generated-uuid",
  "context_mode": "new",
  "message": {
    "id": "message-uuid",
    "conversation_id": "conversation-uuid",
    "run_id": "run-uuid",
    "role": "user",
    "kind": "text",
    "sequence": 1,
    "content": "Первый запрос",
    "created_at": "2026-09-29T12:00:00Z"
  }
}
```

Frontend заменяет optimistic message на canonical message по `client_request_id`.

## 3.3. Следующий запрос в том же чате

Frontend отправляет существующий conversation ID:

```json
{
  "type": "run.create",
  "conversation_id": "conversation-uuid",
  "client_request_id": "new-client-request-uuid",
  "content": "Следующий вопрос с учётом предыдущего контекста",
  "context_mode": "resume"
}
```

Backend берёт `claude_session_id` из БД и запускает:

```bash
claude --print --verbose --output-format stream-json \
  --resume <server-side-session-id>
```

Клиент не может передать session ID самостоятельно.

## 3.4. Потоковое событие

Все persisted events передаются в плоском WebSocket envelope:

```json
{
  "type": "message",
  "schema_version": 1,
  "conversation_id": "conversation-uuid",
  "run_id": "run-uuid",
  "seq": 3,
  "role": "claude",
  "content": "Потоковая часть ответа"
}
```

Основные типы:

- `run.accepted`;
- `session`;
- `message`;
- `tool`;
- `tool_result`;
- `thinking`;
- `context.reset`;
- `result`;
- `run.failed`;
- `raw_event`.

## 3.5. Успешное завершение

```json
{
  "type": "result",
  "conversation_id": "conversation-uuid",
  "run_id": "run-uuid",
  "seq": 8,
  "status": "completed",
  "client_request_id": "client-request-uuid",
  "session_state": "active",
  "assistant_message": {
    "id": "assistant-message-uuid",
    "role": "assistant",
    "kind": "text",
    "sequence": 4,
    "content": "Финальный canonical ответ"
  },
  "duration_ms": 1250,
  "cost_usd": 0.0123,
  "num_turns": 1,
  "is_error": false
}
```

## 3.6. Ошибка run

```json
{
  "type": "run.failed",
  "conversation_id": "conversation-uuid",
  "run_id": "run-uuid",
  "seq": 6,
  "status": "failed",
  "client_request_id": "client-request-uuid",
  "error_code": "claude_session_unavailable",
  "message": "Сохранённая сессия Claude недоступна.",
  "session_state": "unavailable",
  "can_reset_context": true
}
```

## 3.7. Context reset

Frontend отправляет новый `run.create` для существующей conversation:

```json
{
  "type": "run.create",
  "conversation_id": "conversation-uuid",
  "client_request_id": "new-client-request-uuid",
  "content": "Начать заново",
  "context_mode": "reset"
}
```

Backend:

1. не использует старый session ID в CLI;
2. сохраняет system message `kind=context_reset`;
3. отправляет persisted event `context.reset`;
4. запускает новую Claude session;
5. после успеха заменяет server-side session ID новым.

## 3.8. Reconnect и replay

Frontend подписывается:

```json
{
  "type": "run.subscribe",
  "run_id": "run-uuid",
  "after_seq": 5
}
```

Backend передаёт events с `seq > 5`. Если run ещё выполняется, подписка продолжает polling БД до terminal status.

После завершения:

```json
{
  "type": "replay.complete",
  "run_id": "run-uuid",
  "after_seq": 5,
  "last_seq": 8,
  "status": "completed"
}
```

Если соединение потеряно до `run.accepted`, frontend повторяет исходный `run.create` с тем же `client_request_id`. Backend возвращает существующий run как idempotent replay и не запускает второй процесс.

## 4. Что доступно frontend

Frontend получает:

- conversation ID, title и timestamps;
- `context_state`;
- `active_run_id` для восстановления после reload;
- canonical user/assistant/system messages;
- run ID и event sequence;
- текстовые части ответа;
- названия и безопасные metadata tool events;
- основную роль Claude Code, доступные agents и фактически привлечённые subagent roles;
- список файловых артефактов, path/size/line count и download URL;
- duration, cost и number of turns;
- typed error codes;
- возможность explicit context reset.

Frontend не получает:

- `claude_session_id`;
- `resume_session_id`;
- путь к локальному transcript Claude Code;
- auth tokens и API credentials;
- внутренние настройки Claude Code;
- необработанный chain of thought;
- скрытый thinking text;
- содержимое Artifact через общий events/history API — оно доступно только отдельным download endpoint.

Событие `thinking` передаётся без внутреннего содержимого.

## 5. Что доступно backend

Backend имеет доступ к:

- server-side Claude session ID;
- conversation/run/message/event records;
- CLI stream-json событиям;
- model/cwd metadata из init;
- tool input/result, если они присутствуют в CLI event;
- server-side Artifact snapshots и их metadata;
- available agents из init и фактические роли из `subagent_stats.by_type`;
- cost/duration/turn metrics;
- CLI exit code;
- статусу context resume/reset/unavailable.

Backend не интерпретирует внутренний файл session и не гарантирует его переносимость между машинами. SQLite history не является полной заменой Claude session, поскольку не содержит всё внутреннее tool/session state.

## 6. REST API

### Conversations

```http
GET /api/conversations?limit=100&offset=0
```

Ответ:

```json
[
  {
    "id": "conversation-uuid",
    "title": "Первый запрос",
    "status": "active",
    "context_state": "active",
    "active_run_id": null,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

### Messages

```http
GET /api/conversations/{conversation_id}/messages
```

Возвращает canonical messages по возрастанию `sequence`.

### Events

```http
GET /api/runs/{run_id}/events?after_seq=10&limit=100
```

Возвращает persisted event records:

```json
{
  "run_id": "run-uuid",
  "seq": 11,
  "type": "message",
  "payload": {},
  "created_at": "..."
}
```

### Файловый артефакт

```http
GET /api/runs/{run_id}/events/{event_seq}/file
```

Endpoint доступен только для зарегистрированного файлового артефакта, связанного с persisted `tool_result` event по `(run_id, event_seq)`. Claude Code CLI для `Write` возвращает путь в `tool_result`, но полный snapshot находится во входе предшествующего `tool` event. Backend связывает их по `tool_id`, переносит snapshot в отдельную таблицу `artifacts` и удаляет содержимое из публичного tool input/event payload. Публичный event содержит только path/size/truncated metadata. Backend отдаёт сохранённый snapshot и не перечитывает live filesystem. Это исключает path traversal, symlink TOCTOU, подмену файла и загрузку большого file content через history/replay API. Размер snapshot ограничен `ARTIFACT_MAX_DOWNLOAD_BYTES`; oversized content не сохраняется. Миграции `0004` и `0005` переносят и санитизируют legacy events.

## 7. Архитектура backend

Backend — FastAPI-приложение с SQLite/SQLAlchemy/Alembic.

### Основные компоненты

- `backend/main.py`
  - FastAPI composition root;
  - WebSocket `/ws`;
  - orchestration lifecycle run;
  - запуск Claude Code CLI;
  - reconnect/replay;
  - проверка session ID;
  - корреляция `Write` tool/tool_result по `tool_id`;
  - запуск CLI в `acceptEdits` без unsafe bypass;
  - WebSocket protocol dispatch.

- `backend/parser.py`
  - преобразует Claude CLI `stream-json` в нормализованные события;
  - обрабатывает все content blocks;
  - скрывает внутренний thinking text;
  - ограничивает размер file snapshot до записи;
  - безопасно нормализует неизвестные JSON shapes.

- `backend/repository.py`
  - атомарное создание/повтор run;
  - message/event sequence allocation;
  - idempotency;
  - canonical messages;
  - terminal transaction;
  - выделение Artifact из tool result и redaction file content в public event;
  - чтение conversations/messages/events.

- `backend/models.py`
  - SQLAlchemy models `Conversation`, `Run`, `Message`, `Event`, `Artifact`;
  - constraints и partial unique index активного run.

- `backend/db.py`
  - async SQLAlchemy engine/session factory;
  - миграции;
  - SQLite foreign keys.

- `backend/api/history.py`
  - REST API истории;
  - active run discovery;
  - download persisted Artifact по `(run_id, event_seq)`.

- `backend/migrations/`
  - версионирование схемы через Alembic.

### Backend data flow

```text
WebSocket command
    ↓
validate / idempotency
    ↓
repository transaction
    ↓
run.accepted persisted
    ↓
Claude Code CLI new/resume
    ↓
parser → normalized events
    ↓
Write correlation → Artifact snapshot + metadata-only event
    ↓
event persisted → WebSocket publish
    ↓
terminal transaction
    ↓
result/run.failed publish
```

Claude process продолжает выполнение при разрыве WebSocket. Новый клиент обнаруживает `active_run_id` через REST и получает события через replay.

## 8. Архитектура frontend

Frontend — React 19 + TypeScript + Vite.

### Основные компоненты текущей реализации

- `frontend/src/App.tsx`
  - conversations/messages state;
  - optimistic/canonical messages;
  - WebSocket lifecycle;
  - reconnect/replay;
  - dedupe по `(run_id, seq)` и canonical `event_id`;
  - context reset flow;
  - отображение available/used agent roles;
  - восстановление Artifact metadata и download links;
  - selected conversation persistence;
  - theme state.

- `frontend/src/App.css`
  - desktop/mobile layout;
  - sidebar/chat/tool/result/context components;
  - semantic theme variables.

- `frontend/src/index.css`
  - глобальные semantic CSS variables;
  - light/dark palettes;
  - focus styles и color scheme.

- `frontend/index.html`
  - pre-paint theme initialization до React.

- `frontend/vite.config.ts`
  - `/api` HTTP proxy;
  - `/ws` WebSocket proxy.

### Frontend state flow

```text
REST initial load
    ↓
conversations + selected messages
    ↓
event metadata → roles + Artifact list
    ↓
active_run_id? → run.subscribe
    ↓
user submit → optimistic message
    ↓
run.accepted → canonical replacement
    ↓
stream events → timeline
    ↓
result/run.failed → terminal UI state
```

Тема хранится отдельно от chat state:

```text
localStorage claude-web.theme
    ↓
inline pre-paint script
    ↓
documentElement[data-theme]
    ↓
React theme toggle
```

## 9. Взаимодействие frontend и backend

В development frontend работает на Vite, а backend на Uvicorn:

```text
Browser → http://localhost:5173
             │
             ├─ /api/* ──Vite proxy──→ http://127.0.0.1:8000
             └─ /ws    ──Vite proxy──→ ws://127.0.0.1:8000
```

REST используется для canonical history и initial recovery. WebSocket используется для команд, live stream и replay.

Основной принцип согласованности:

1. frontend может показать optimistic state;
2. backend сохраняет canonical state;
3. persisted event подтверждает canonical state;
4. frontend заменяет optimistic данные;
5. после reload REST и replay полностью восстанавливают отображение.

## 10. Ограничения

- Claude Code sessions локальны машине и workspace;
- перенос одной SQLite-базы на другой host не переносит Claude session transcript;
- приложение пока не имеет authentication и ownership model;
- один uvicorn process использует in-process orchestration;
- нет глобального timeout/budget/concurrency configuration;
- file content удаляется из public events, но prompts и другие tool payloads всё ещё требуют универсальной redaction/retention policy;
- Artifact хранит текстовый UTF-8 snapshot из `Write`; произвольные бинарные файлы пока не поддерживаются;
- встроенный preview содержимого Artifact в браузере пока не реализован — доступен список metadata и скачивание;
- история БД не восстанавливает полный Claude context при утрате session;
- session reconstruction из сообщений пока не реализована.
