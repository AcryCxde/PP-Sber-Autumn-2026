# 2.4. Восстановление runner после рестарта — дизайн

Статус: утверждён 2026-10-01. Задачи: PPS-116 (перезапуск контейнера без потери работы), PPS-133 (S3 Runner restart/recovery). Ветка: `PPS-116-runner-recovery`.
Основа: этап 2 из [дизайна runner](2026-09-25-runner-design.md) (`state.json`, checkpoint, resume, автопродолжение).

## Цель

- подтверждённый результат не теряется;
- состояние хода после рестарта определяется как resumed / failed / unknown, а не угадывается;
- после рестарта ход продолжается либо безопасно повторяется.

## Решение: `state.json` + git trailer + чистый планировщик

Отвергнуто: вывод состояния из `events.jsonl` (разбор журнала при каждом старте, «ход открыт» неявно); только транскрипт SDK (`continue_conversation`) — не определяет исход шага.

### Исходы после рестарта

| Исход | Событие | Условие |
|---|---|---|
| завершён | `turn_completed` | `HEAD` содержит `Turn-Id` текущего хода: commit успел, не успели state и события. Ход не повторяется, дописываются `checkpoint` и `turn_completed`. |
| unknown → resumed | `turn_interrupted` → `turn_resumed{attempt, mode}` | `turn_open`, ход не подтверждён. `mode=resume`: SDK-сессия продолжается по `session_id`. `mode=fresh`: новая сессия, прерванный промпт повторяется с пометкой «сначала проверь git status/diff, часть работы уже сделана». |
| failed | `turn_failed{resume_limit}` | три автопродолжения подряд уже исполнены (`autoresume_count >= 3` при старте): `health=crashed`, runner ждёт человека. |

`turn_interrupted` значит «исход неизвестен» и без commit никогда не становится `completed`. Дерево не откатывается: неподтверждённая работа остаётся. `autoresume_count` сбрасывается на `turn_completed`.

### Порядок записи в конце хода

1. `git add -A && git commit --allow-empty` с trailer `Turn-Id: <id>` (в `to_thread`; во время commit ход в состоянии `Committing`, новые команды ждут в очереди).
2. События `checkpoint{sha}` и `turn_completed` (fsync).
3. `state.json`: открытого хода нет, `last_sha` (атомарно, fsync).

События пишутся раньше state: если бы state закрывался первым, падение между шагами 3 и 2 потеряло бы события навсегда (при следующем старте хода уже нет). При падении после commit `state.json` ещё хранит открытый ход, `HEAD` содержит его `Turn-Id`, и восстановление (`Finalize`) дописывает только отсутствующие в журнале события, затем закрывает state.
Открытие хода — наоборот: state с открытым ходом пишется до `query`, а ack команды в inbox — после записи state.

### Команды не теряются

`Inbox.take` переносит файл в `processed/` до записи состояния, команда может пропасть. Двухфазный приём: `peek` → запись `turn_open=true`, `turn_id`, `prompt` и очереди в `state.json` → `ack` (перенос в `processed/`). Дубли по `id` отсекаются как раньше. Очередь хранится в `state.json`.

### Компоненты

| Файл | Роль |
|---|---|
| `state.py` | `PersistedState` (`session_id`, `open_turn{turn_id, cmd}` или `null`, `last_sha`, `autoresume_count`, `queue`), загрузка и атомарная запись через `fsio` |
| `checkpoint.py` | commit с trailer, чтение trailer у `HEAD`, снятие stale `.git/index.lock` при старте (процессов git после рестарта нет) |
| `recovery.py` | чистая `plan(state, head, *, resumable) → Clean \| Finalize \| Continue(attempt, mode) \| GiveUp` |
| `protocol.py` | `EventKind`: `CHECKPOINT`, `TURN_INTERRUPTED`, `TURN_RESUMED` |
| `session.py`, `main.py` | сохранять `session_id` (`session_id_of`), планировать при старте, `ClaudeAgentOptions(resume=…)`, при ошибке resume переходить в `mode=fresh` |

### Отказы

| Случай | Поведение |
|---|---|
| ENOSPC при commit | `turn_failed{disk_full}`, ход не завершён, команды принимаются дальше |
| ENOSPC или другая ошибка записи `state.json` | runner падает (exit 1, `turn_failed{disk_full}` через общий `crash`): без записи состояния восстановление не гарантируется |
| commit упал по другой причине | `turn_failed{checkpoint_failed}`, дерево не трогаем |
| `state.json` повреждён | не угадываем: `turn_failed{state_corrupt}`, exit 65 (рестарт не поможет; файл чинит человек) |
| транскрипта нет или `resume` вернул ошибку | `mode=fresh` |

### Проверки

- Unit: все ветки `plan`; `state`; `checkpoint` на временном git-репозитории; двухфазный inbox; `Session` с `FakeClient`: рестарт при `turn_open`, лимит 3, падение между commit и state.
- Eval `test_restart`: `docker kill` посреди хода → `turn_interrupted` → `turn_resumed` → `turn_completed`, новый `checkpoint.sha`.
- Первым шагом реализации: проверить `resume` при прерванном tool call через cliproxy на GPT (в дизайне runner это непроверенный риск). Результат определяет, как часто срабатывает `mode=fresh`.

### Документы

Обновить ограничения этапа 1 в `runner/README.md`, `docs/2.4/protocol.md` (события этапа 2) и отметить выполненный этап 2 в дизайне runner.

## Вне границ

WS-клиент и повтор событий (гейтвей, MR #4); откат дерева; автоубийство зависших сессий; перенос развилок между рестартами сверх общего пути (развилку прерывает рестарт → `turn_interrupted` → агент спрашивает заново).

## Процесс

Ветка `PPS-116-runner-recovery` от `master` после слияния MR #3 (этап 1). Отдельный MR; в описании: «Closes PPS-116, PPS-133».

## Findings

Спайк Task 0: `claude-agent-sdk==0.2.158`, шлюз cliproxy (модель sonnet-класса), скрипт вне репозитория. Запуск с очищенным окружением (без унаследованных `CLAUDE_CODE_*`), `CLAUDE_CONFIG_DIR` во временном каталоге.

1. **Первым `session_id` приходит в `SystemMessage(subtype="init")`**, самом первом сообщении потока `receive_response()` (до `StreamEvent`), в `message.data["session_id"]`. Атрибута `.session_id` у `SystemMessage` нет (`getattr(msg, "session_id", None)` вернёт `None`), у `StreamEvent`, `ResultMessage` атрибут есть. Следствие: `session_id_of` обязана читать `msg.data["session_id"]` для `SystemMessage`; сохранять id в `state.json` можно сразу по `init`, до первого токена. Таблица «Правила последствий» не затронута.
2. **Транскрипт: `<CLAUDE_CONFIG_DIR>/projects/<slug>/<session_id>.jsonl`**, где `<slug>` — абсолютный realpath `cwd`, в котором каждый символ вне `[A-Za-z0-9]` заменён на `-` (пример: `projects/-private-tmp-claude-501--Users-romanov-...-abc-work/97c962f1-....jsonl`). Совпадает с ожиданием плана. Следствие: glob `projects/*/<id>.jsonl` в `resumable_session` (Task 8) верен, правка не нужна. Slug из `cwd` не вычисляем, ищем по `*`.
3. **Resume несуществующей сессии падает при `connect()`**: `ClaudeSDKClient.connect()` бросает `claude_agent_sdk._errors.ResultError` (подкласс `ProcessError`, затем `ClaudeSDKError`; сообщение `No conversation found with session ID: <id>`, exit code 1). До `query` дело не доходит, молчаливой новой сессии нет. Следствие по таблице: ошибка при `connect` — в Task 8 выполнить условный шаг «фолбэк на fresh»; ловить `ResultError`/`ProcessError` вокруг входа в `async with ClaudeSDKClient(...)` и повторять с `resume=None` (`mode=fresh`).
4. **Resume после оборванного tool call принимается API.** `ask(...)` с «Выполни Bash: `sleep 120`»; когда пришёл `AssistantMessage` с `ToolUseBlock(Bash)`, CLI убит `SIGKILL` (исключение читателя сообщений: `ProcessError`, exit code -9). В транскрипте остался `tool_use` без результата. `resume=<session_id>` с «Продолжи»: `connect` и `query` прошли, CLI сам дописал в транскрипт синтетический `tool_result` «[Request interrupted by user for tool use]», API ответил без ошибок, модель заново вызвала `Bash sleep 120`, довела до `ResultMessage(success)`. Следствие по таблице: строка «resume даёт ошибку API» не сработала, `resumable` не обнуляем, режим `resume` остаётся основным. Дополнительно в Task 8 и README: после resume модель может повторить прерванный вызов инструмента (побочные эффекты выполнятся дважды), промпт `turn_resumed` должен просить сперва проверить состояние дерева. Проверено на одной модели и одном прогоне.

Изоляция шага D: убивался не `pkill` по имени, а дочерний процесс (`pgrep -P <pid скрипта>`) самого скрипта; чужие процессы Claude Code не затрагивались.
