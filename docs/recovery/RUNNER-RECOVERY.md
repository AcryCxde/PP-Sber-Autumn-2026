# Runner: восстановление после рестарта

Задачи: PPS-116 (перезапуск контейнера без потери работы), PPS-133 (S3 Runner restart/recovery).
Здесь только итог и проверки. Подробности: [дизайн](../plans/2026-10-01-runner-recovery-design.md), [протокол и события](../2.4/protocol.md), [README runner](../../runner/README.md).

## Гарантии

- Подтверждённый результат не теряется: ход подтверждён, когда в `HEAD` есть git commit с trailer `Turn-Id`.
- Состояние прерванного хода определяется по `state.json` и git, а не угадывается: `resumed`, `failed` или `unknown` (`turn_interrupted` без commit никогда не становится `completed`).
- После рестарта ход либо продолжается, либо безопасно повторяется. Дерево проекта не откатывается.

## Исходы после рестарта

| Исход | События | Условие |
|---|---|---|
| завершён | `checkpoint` + `turn_completed` (дописываются только недостающие) | `HEAD` содержит `Turn-Id` хода: commit успел, ход не повторяется |
| unknown → resumed | `turn_interrupted` → `turn_resumed{attempt, mode}` | ход открыт, commit нет. `mode=resume`: по `session_id`; `mode=fresh`: транскрипта нет или `resume` не подключился, промпт повторяется с просьбой сперва проверить `git status`/`git diff` |
| failed | `turn_failed{resume_limit}` | три автопродолжения подряд исчерпаны; runner жив, `health=crashed`, новая команда возвращает его в работу |
| state повреждён | `turn_failed{state_corrupt}`, exit 65 | `state.json` не разобран; файл чинит человек |

## Порядок записи

- Конец хода: git commit с `Turn-Id` → события `checkpoint`, `turn_completed` (fsync) → `state.json`. Падение после commit: при старте дописываются только отсутствующие события.
- Начало хода: `state.json` с открытым ходом → `query` → ack команды в inbox (команда не теряется).

## Проверки

| Что | Где |
|---|---|
| все ветки планировщика | `runner/tests/test_recovery.py` |
| `state.json`: запись, повреждение | `runner/tests/test_state.py` |
| commit с trailer, stale `index.lock` | `runner/tests/test_checkpoint.py` |
| двухфазный inbox | `runner/tests/test_inbox.py` |
| рестарт при открытом ходе, лимит 3, падение между commit и state | `runner/tests/test_session.py`, `runner/tests/test_main_recovery.py` |
| `docker kill` посреди хода → `turn_interrupted` → `turn_resumed` → `turn_completed`, новый `checkpoint.sha` | `runner/evals/test_restart.py` (e2e, нужны Docker и env шлюза) |

Unit: `cd runner && uv run pytest` (199 passed).

## Остаточные риски

- После `resume` прерванный вызов инструмента может выполниться повторно (побочные эффекты дважды). Проверено на одной модели и одном прогоне.
- Агент (тот же uid) может подделать trailer текущего хода или испортить `.git` своего тома: вред ограничен его же проектом.
- `state.json` хранит текст команд как есть (права 0600, том проекта); в журнале события редактируются.

## Вне границ

WebSocket-клиент и повтор событий после `welcome.acked_seq` (gateway, PR #4); откат дерева; автоубийство зависших сессий.
