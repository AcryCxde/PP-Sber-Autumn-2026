# Итоги исправления замечаний `plans/review.md`

Реализация плана завершена. Все замечания, найденные в дополнительных раундах code review, также исправлены.

## 1. Изоляция workspace

- Claude CLI больше не запускается в корне gateway.
- Добавлен отдельный workspace на conversation в `backend/workspace.py`.
- Проверяется пересечение workspace с репозиторием и каталогом БД.
- Артефакты допускаются только внутри workspace.
- Блокируются absolute escape, `..`, symlink escape, `.git` и `.runner`.
- Публично сохраняются только POSIX-relative paths.
- Добавлен OS file lock: одновременно с одной SQLite-базой может работать только один backend process.
- Non-SQLite deployment запрещён до появления durable executor lease.

## 2. Безопасная публичная лента

Добавлен `backend/public_events.py` со строгим allowlist.

Публичные события:

- `run.accepted`;
- `run.started`;
- `message.created`;
- `context.reset`;
- `artifact.saved`;
- `run.completed`;
- `run.failed`;
- `run.outcome_unknown`;
- `legacy.redacted`.

Больше не публикуются:

- `session`;
- tool inputs/results;
- thinking;
- raw stdout/stderr;
- cwd и host paths;
- Claude session ID;
- cost/tokens;
- permission denials;
- subagent statistics;
- произвольные exception messages.

REST и WebSocket применяют одинаковую защитную проекцию. Stored payload не может подменить тип события из колонки БД.

## 3. Subprocess и parser

- stdout и stderr читаются раздельно и конкурентно.
- stderr не сохраняется и не возвращается браузеру.
- Безопасные stderr-маркеры позволяют определить недоступную resume-сессию без сохранения диагностического текста.
- Исправлен разбор top-level `tool_use_result`.
- Добавлен признак подтверждённого результата инструмента.
- Malformed/unconfirmed tool result не считается успешным.
- Артефакт создаётся только после подтверждённого успешного `Write`.
- Добавлен stream-json fixture.

## 4. Lifecycle и восстановление

- Добавлен статус `outcome_unknown`.
- Terminal event теперь ровно одно и последнее.
- Запись событий после terminal status отклоняется.
- Run status, context state и terminal event сохраняются атомарно.
- При startup осиротевшие `pending/running` переводятся в `outcome_unknown`.
- Resume-контекст инвалидируется после:
  - рестарта;
  - прерывания процесса;
  - ошибки artifact storage;
  - повреждённого или неполного CLI stream;
  - недоступной Claude session.
- Exit code `0` без terminal `result` больше не считается успехом.

## 5. WebSocket и replay

- `run.create` и `run.subscribe` больше не блокируют receive loop.
- Runs принадлежат приложению, а не WebSocket-соединению.
- Subscription tasks принадлежат соединению и отменяются при disconnect.
- Отправка сериализована через per-socket lock.
- Subscription имеет конечное long-poll окно.
- Deadline применяется и к большим многостраничным replay.
- Frontend повторяет подписку после `terminal: false`.
- Sequence gap не применяется до восстановления пропущенных событий.
- Run task больше не удерживает disconnected WebSocket сильной ссылкой.
- Добавлен глобальный лимит `MAX_CONCURRENT_RUNS`.
- Idempotent retry допускается даже при исчерпанном лимите.

## 6. Legacy security migration

Добавлена миграция:

`backend/migrations/versions/20261012_0006_redact_legacy_data.py`

Она:

- полностью редактирует старые tool/session/raw payload;
- конвертирует безопасные event types;
- очищает старые session IDs и `runs.error`;
- удаляет старые небезопасные artifact snapshots;
- сохраняет event sequence;
- делает существующий контекст недоступным до явного reset.

Миграция необратима по security-причинам. Перед deployment требуется backup БД, но небезопасная backup-копия не должна обслуживаться приложением.

## 7. Frontend

- Добавлен строгий decoder: `frontend/src/events.ts`.
- Убрана обработка private CLI events.
- Добавлена поддержка:
  - `run.completed`;
  - `run.failed`;
  - `run.outcome_unknown`;
  - `artifact.saved`;
  - sequence gaps;
  - bounded resubscription;
  - redacted legacy events.
- Терминальные карточки восстанавливаются после reload.
- Canonical messages отображаются даже при временной ошибке event metadata.
- Добавлен conversation-level events endpoint вместо REST fan-out по каждому run.
- История сообщений и metadata загружаются параллельно.
- Replay cursors очищаются при переключении диалога.
- Добавлены Vitest-тесты event decoder/reducer.

## 8. Документация и конфигурация

Обновлены:

- `ARCHITECTURE_AND_SESSION_DATA.md`;
- `backend/.env.example`;
- `docs/RUN_APPLICATION.md`;
- `docs/review-fix-plan.md`.

Добавлены параметры:

```dotenv
WORKSPACE_ROOT=~/.claude-web/workspaces
SUBSCRIPTION_WINDOW_SECONDS=15
SUBSCRIPTION_POLL_INTERVAL_SECONDS=0.25
MAX_CONCURRENT_RUNS=4
```

## Проверки

Успешно выполнены:

- Backend: **58 passed, 1 skipped**;
- frontend production build;
- ESLint;
- TypeScript-проверка frontend event tests;
- `git diff --check`;
- несколько последовательных high-effort code review с исправлением всех найденных замечаний.

## Что осталось вне реализации

1. **Полная интеграция `events.v0`** намеренно оставлена отдельным этапом. Нужны runner transport, heartbeat, runner-generated `event_id`, настоящий `project_id`, checkpoint/storage confirmation и role/facilitator events.
2. Сам `npm run test` с Vitest не был запущен: permission classifier потребовал отдельного явного разрешения на исполнение нового пакета Vitest из npm. Зависимость установлена, тесты компилируются. Команда для проверки:

   ```bash
   npm --prefix frontend run test
   ```

3. Live fixture через реальный Claude CLI не записывался — добавлен санитизированный fixture нужного stream-json формата. Реальный прогон требует обращения к внешнему Claude-сервису и доступных credentials.
4. `npm install` сообщил об **1 high severity vulnerability** в dependency tree; автоматический `npm audit fix` не применялся, чтобы не менять версии зависимостей без отдельного решения.
5. Коммит не создавался.
