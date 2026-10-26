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
status                   # pending | running | completed | failed | cancelled | outcome_unknown
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

Пара `(run_id, event_seq)` уникальна. После успешного коррелированного `Write` backend атомарно сохраняет snapshot и публикует только безопасное событие `artifact.saved`:

```json
{
  "type": "artifact.saved",
  "path": "plans/result.md",
  "size_bytes": 2048,
  "truncated": false,
  "num_lines": 42
}
```

`path` всегда относителен к workspace диалога. Пути вне workspace, symlink escape и служебные каталоги `.git`/`.runner` отклоняются. Ввод/вывод `Write` и других инструментов в публичную ленту не попадает. Download endpoint возвращает сохранённый snapshot и не читает live filesystem.

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

Клиент не может передать session ID самостоятельно. CLI запускается в стабильном каталоге `<WORKSPACE_ROOT>/<conversation_id>`, расположенном вне репозитория gateway и каталога БД. Resume допускается только при наличии этого workspace.

## 3.4. Потоковое событие

Все persisted events передаются в плоском WebSocket envelope:

```json
{
  "type": "message.created",
  "schema_version": 1,
  "conversation_id": "conversation-uuid",
  "run_id": "run-uuid",
  "seq": 3,
  "role": "claude",
  "content": "Потоковая часть ответа"
}
```

Публичные persisted types текущего gateway:

- `run.accepted`;
- `run.started`;
- `message.created`;
- `context.reset`;
- `artifact.saved`;
- `run.completed`;
- `run.failed`;
- `run.outcome_unknown`;
- `legacy.redacted` только для сохранения последовательности старой истории.

`session`, tool calls/results, thinking, raw stdout/stderr и произвольные поля CLI являются внутренними данными оркестратора и никогда не записываются в публичную ленту. REST и WebSocket дополнительно применяют allowlist-проекцию при чтении.

## 3.5. Успешное завершение

```json
{
  "type": "run.completed",
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
  }
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

Если backend перезапущен во время активного run и не может доказать результат, startup reconciliation атомарно переводит его в `outcome_unknown`, делает context недоступным для resume и добавляет последнее событие:

```json
{
  "type": "run.outcome_unknown",
  "status": "outcome_unknown",
  "error_code": "backend_restarted",
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

Backend передаёт events с `seq > 5`. Одна подписка является конечным long-poll окном: она завершается либо при terminal status, либо по deadline. Обработчик подписки работает отдельной socket-owned задачей и не блокирует другие команды на соединении.

После завершения:

```json
{
  "type": "replay.complete",
  "run_id": "run-uuid",
  "after_seq": 5,
  "last_seq": 8,
  "status": "completed",
  "terminal": true
}
```

Если окно закончилось при работающем run, backend возвращает `terminal: false`; frontend повторяет подписку от последнего непрерывного `seq`. Событие после sequence gap не применяется до replay пропущенных номеров.

Если соединение потеряно до `run.accepted`, frontend повторяет исходный `run.create` с тем же `client_request_id`. Backend возвращает существующий run как idempotent replay и не запускает второй процесс.

## 4. Что доступно frontend

Frontend получает:

- conversation ID, title и timestamps;
- `context_state`;
- `active_run_id` для восстановления после reload;
- canonical user/assistant/system messages;
- run ID и event sequence;
- текстовые части ответа;
- основную роль Claude Code;
- список файловых артефактов с относительным path/size/line count и download URL;
- безопасные terminal status и typed error codes;
- возможность explicit context reset.

Frontend не получает:

- `claude_session_id`;
- `resume_session_id`;
- путь к локальному transcript Claude Code;
- auth tokens и API credentials;
- внутренние настройки Claude Code;
- необработанный chain of thought и thinking events;
- tool names, input и result content;
- init/session metadata, cwd, costs, tokens, permission denials и subagent statistics;
- raw stdout/stderr и внутренние exception messages;
- содержимое Artifact через общий events/history API — оно доступно только отдельным download endpoint.

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

### Events выбранного диалога

```http
GET /api/conversations/{conversation_id}/events?offset=0&limit=500
```

Возвращает безопасную публичную проекцию событий всех run диалога одной пагинированной последовательностью. Frontend использует этот endpoint вместо fan-out запроса на каждый исторический run; при временной ошибке metadata canonical messages всё равно отображаются.

### Events одного run

```http
GET /api/runs/{run_id}/events?after_seq=10&limit=100
```

Возвращает persisted event records:

```json
{
  "run_id": "run-uuid",
  "seq": 11,
  "type": "message.created",
  "payload": {},
  "created_at": "..."
}
```

### Файловый артефакт

```http
GET /api/runs/{run_id}/events/{event_seq}/file
```

Endpoint доступен только для зарегистрированного артефакта, связанного с persisted `artifact.saved` по `(run_id, event_seq)`. Backend коррелирует успешный `Write` с его result, проверяет путь относительно workspace и атомарно сохраняет snapshot вместе с metadata-only event. Backend отдаёт snapshot и не перечитывает live filesystem. Размер ограничен `ARTIFACT_MAX_DOWNLOAD_BYTES`; oversized content не сохраняется. Security migration `20261012_0006` удаляет старые snapshots и полностью редактирует legacy tool/session/raw payload, поскольку они могли относиться к репозиторию gateway или содержать секреты.

## 7. Архитектура backend

Backend — FastAPI-приложение с SQLite/SQLAlchemy/Alembic.

### Основные компоненты

- `backend/main.py`
  - FastAPI composition root;
  - WebSocket `/ws`;
  - orchestration lifecycle run;
  - запуск Claude Code CLI;
  - bounded reconnect/replay и независимые subscription tasks;
  - проверка session ID;
  - корреляция успешного `Write` по `tool_id`;
  - запуск CLI в отдельном workspace в `acceptEdits` без unsafe bypass;
  - раздельный drain stdout/stderr;
  - startup reconciliation активных run;
  - WebSocket protocol dispatch.

- `backend/parser.py`
  - внутренне преобразует Claude CLI `stream-json`;
  - обрабатывает все content blocks и top-level `tool_use_result`;
  - не определяет публичный контракт;
  - безопасно нормализует неизвестные JSON shapes.

- `backend/public_events.py`
  - allowlist публичных event types и payload fields;
  - единая защитная проекция для persistence, REST и WebSocket.

- `backend/workspace.py`
  - изоляция каталогов диалогов от gateway/БД;
  - canonical path и symlink checks для артефактов.

- `backend/repository.py`
  - атомарное создание/повтор run;
  - message/event sequence allocation;
  - idempotency;
  - canonical messages;
  - terminal-last transaction и `outcome_unknown`;
  - атомарное сохранение Artifact + `artifact.saved`;
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
isolated workspace → Claude Code CLI new/resume
    ↓
private parser events
    ↓
public allowlist projection / successful Write correlation
    ↓
event or Artifact snapshot persisted → WebSocket publish
    ↓
terminal-last transaction
    ↓
run.completed / run.failed / run.outcome_unknown
```

Claude process продолжает выполнение при разрыве WebSocket. Новый клиент обнаруживает `active_run_id` через REST и получает события через replay.

## 8. Архитектура frontend

Frontend — React 19 + TypeScript + Vite.

### Основные компоненты текущей реализации

- `frontend/src/App.tsx`
  - conversations/messages state;
  - optimistic/canonical messages;
  - WebSocket lifecycle;
  - reconnect и bounded replay;
  - contiguous sequence/gap detection и dedupe по `(run_id, seq)`;
  - context reset и outcome-unknown flow;
  - восстановление Artifact metadata и download links;
  - selected conversation persistence;
  - theme state.

- `frontend/src/events.ts`
  - строгий decoder публичных REST/WS событий;
  - sequence-gap решения и безопасная проекция артефактов;
  - legacy/internal events превращаются в sequence-consuming no-op без payload.

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
safe event metadata → Artifact list + contiguous replay cursor
    ↓
active_run_id? → run.subscribe
    ↓
user submit → optimistic message
    ↓
run.accepted → canonical replacement
    ↓
stream events → timeline
    ↓
run.completed/run.failed/run.outcome_unknown → terminal UI state
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
- in-process orchestration поддерживает ровно один backend process на SQLite-БД; startup удерживает OS file lock и отклоняет второй worker, а non-SQLite deployment запрещён до появления durable owner lease/heartbeat в runner-архитектуре;
- нет глобального run timeout/budget; число одновременно исполняемых CLI ограничено `MAX_CONCURRENT_RUNS`, а subscription имеет отдельное bounded long-poll окно;
- отдельный workspace снижает риск случайного доступа к gateway, но полноценной OS/container sandbox границей станет runner 2.4;
- текущая публичная схема безопасна, но не является полной реализацией `events.v0`: ещё нет runner `event_id`, project model, heartbeat/recovery и storage confirmation;
- Artifact хранит текстовый UTF-8 snapshot из успешного `Write`; произвольные бинарные файлы и Edit-family snapshots пока не поддерживаются;
- встроенный preview содержимого Artifact в браузере пока не реализован — доступен список metadata и скачивание;
- история БД не восстанавливает полный Claude context при утрате session;
- session reconstruction из сообщений пока не реализована.
