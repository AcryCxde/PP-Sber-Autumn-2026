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

## Вопрос фасилитатора и рестарт

- Ожидание ответа (`AskUserQuestion`) живёт только в памяти процесса. Рестарт его прерывает, ход остаётся открытым и продолжается как обычно (`turn_interrupted` → `turn_resumed`).
- Из журнала берутся вопросы, оставшиеся без ответа; `continuation_prompt` просит модель задать их заново через `AskUserQuestion` (оба режима, `resume` и `fresh`). Проверено живым прогоном: без подсказки модель после resume не переспрашивает и идёт по умолчаниям, с подсказкой переспрашивает; API принимает resume после оборванного вызова (CLI сам дописывает `[Request interrupted by user for tool use]`).
- Новый вопрос получает новый `fork_id`; ответ на прежний отвергается (`command_ack rejected: unknown_fork`), а не пропадает молча.
- Каждый рестарт во время ожидания тратит одну из трёх попыток автопродолжения.
- Ожидание ответа проверено живым прогоном до 15 минут (CLI не обрывает); дольше не проверялось.
- Не доходит до gateway: `state_corrupt` (выход 65 до подключения клиента), остаётся `heartbeat_lost`.
- Известное ограничение: пока модель не задала вопрос заново, пользователь не видит запроса (R2 — удержание вопроса без обращения к модели — не реализован).

## Проверки

| Что | Где |
|---|---|
| все ветки планировщика | `runner/tests/test_recovery.py` |
| `state.json`: запись, повреждение | `runner/tests/test_state.py` |
| commit с trailer, stale `index.lock` | `runner/tests/test_checkpoint.py` |
| двухфазный inbox | `runner/tests/test_inbox.py` |
| рестарт при открытом ходе, лимит 3, падение между commit и state | `runner/tests/test_session.py`, `runner/tests/test_main_recovery.py` |
| вопрос и рестарт: журнал, подсказка, тот же ход | `runner/tests/test_eventlog.py`, `runner/tests/test_recovery.py`, `runner/tests/test_session.py` |
| `docker kill` во время вопроса → новый вопрос → ответ → завершение | `runner/evals/test_question.py` (e2e, фейковый gateway) |
| `docker kill` посреди хода → `turn_interrupted` → `turn_resumed` → `turn_completed`, новый `checkpoint.sha` | `runner/evals/test_restart.py` (e2e, нужны Docker и env шлюза) |

Unit: `cd runner && uv run pytest` (278 passed).

## Остаточные риски

- После `resume` прерванный вызов инструмента может выполниться повторно (побочные эффекты дважды). Проверено на одной модели и одном прогоне.
- Агент (тот же uid) может подделать trailer текущего хода или испортить `.git` своего тома: вред ограничен его же проектом.
- `state.json` хранит текст команд как есть (права 0600, том проекта); в журнале события редактируются.

## Вне границ

серверная часть gateway (требования — [events-v0.md, раздел 11](../contracts/events-v0.md)); откат дерева; автоубийство зависших сессий.
