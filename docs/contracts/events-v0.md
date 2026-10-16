# Контракт событий events v0: runner ↔ gateway ↔ storage ↔ web

**Статус:** черновик на согласование 07.10.2026. Нужно подтверждение владельцев 2.1 (Дмитрий), 2.3 (Арина), 2.4 (Илья), 2.5 (Катя).
**Задача:** YouGile «S2 events v0: gateway↔runner↔storage↔web».

## 1. Цель и границы

Одна публичная лента событий запуска. Её одинаково читают gateway (2.1), web (2.5) и хранилище (2.3); формат CLI/SDK остаётся внутри runner.

В границах: конверт события, типы, владельцы, порядок, дедупликация, replay, неизвестный исход, правило `completed` после сохранения артефакта, fixture.
Вне границ: транспорт runner → gateway (`docs/2.4/protocol.md`), auth и ownership, тексты UI, аналитика HADI, отмена запуска (см. открытые вопросы).

## 2. Конверт

Каждое событие — один JSON-объект; все поля обязательны.

```json
{"schema": "events.v0", "event_id": "50e7a5db-…", "project_id": "demo-curriculum",
 "run_id": "7c2e4a90…", "seq": 2, "occurred_at": "2026-10-06T08:00:03.5Z",
 "type": "message.created", "payload": {"role_id": null, "text": "…"}}
```

| Поле | Смысл |
|---|---|
| `schema` | `events.v0`. Несовместимое изменение — новая версия |
| `event_id` | UUID, глобально уникален, **стабилен при повторной отправке**: ключ дедупликации |
| `project_id` | Проект; непрозрачная строка |
| `run_id` | Запуск: одна команда пользователя от принятия до терминального события. Вопрос фасилитатора не создаёт новый `run_id` |
| `seq` | Номер в ленте запуска: с 1, без пропусков, монотонный. Присваивает gateway при записи |
| `occurred_at` | Время события, RFC 3339, UTC |
| `type` | Один из типов раздела 3 |
| `payload` | Только безопасные публичные данные (раздел 5) |

Имена в snake_case: в таком стиле уже написаны gateway (#4) и runner.

## 3. Типы событий

| `type` | Источник | `payload` | Состояние запуска после |
|---|---|---|---|
| `run.accepted` | gateway | `client_request_id` | `queued` |
| `run.started` | runner | `{}` | `running` |
| `role.started` | runner | `role_id`, `role_instance_id`, `title` \| null | `running` |
| `role.completed` | runner | `role_id`, `role_instance_id`, `outcome`: `completed` \| `failed` | `running` |
| `message.created` | runner | `role_id` \| null (null — фасилитатор), `text` | без изменений |
| `facilitator.question` | runner | `question_id`, `questions[]`: `text`, `header` \| null, `options[]` (`label`, `description` \| null), `multi_select`, `allow_custom` | `waiting_for_input` |
| `user.answer.accepted` | runner | `question_id`, `answers`: текст вопроса → ответ | `running` |
| `progress.updated` | runner | `stage`: `interrupted` \| `recovering` \| `working`; для `working` ещё `attempt`, `context_restored` | `running` (с пометкой о сбое) |
| `artifact.created` | runner | `artifact_id`, `path` (от корня проекта), `commit_sha`, `role_id` \| null | без изменений |
| `artifact.saved` | gateway по подтверждению 2.3 | `artifact_id`, `version_id`, `size_bytes`, `download` | без изменений |
| `run.completed` | runner → gateway | `{}` | `completed` (терминальное) |
| `run.failed` | runner или gateway | `code`, `retryable` | `failed` (терминальное) |
| `run.outcome_unknown` | gateway; runner только при `state_corrupt` | `code` | `outcome_unknown` (терминальное) |

`message.created` нет в минимальном списке 03-team-spec. Он добавлен, потому что без него не показать ни ответ фасилитатора, ни сообщения ролей (2.1 §2, 2.5 §2).

Коды `run.failed`: `result_error`, `checkpoint_failed`, `resume_limit` (`retryable: true`), `disk_full` (`false`, нужен оператор), `artifact_save_failed` (gateway).
Коды `run.outcome_unknown`: `heartbeat_lost`, `artifact_save_unknown` (gateway), `state_corrupt` (runner).

`progress.updated` не содержит процентов и ETA: runner их не знает, а выдумывать их запрещает 2.5 §9. Этап и роли — всё, что есть честного.

## 4. Правила

1. **Терминальное событие одно и последнее.** После `run.completed` / `run.failed` / `run.outcome_unknown` событий этого `run_id` нет. UI не выводит терминальное состояние из таймера или из отсутствия событий.
2. **`run.completed` только после `artifact.saved`.** Получив от runner `run.completed`, gateway держит его, пока 2.3 не подтвердит каждый `artifact.created` запуска. Каждое подтверждение публикуется как `artifact.saved`, и только затем `run.completed`. Отказ хранилища даёт `run.failed{artifact_save_failed}`. Ответ не получен (тайм-аут, обрыв) — `run.outcome_unknown{artifact_save_unknown}`.
3. **Падение процесса — не провал.** Runner упал посреди хода: `progress.updated{interrupted}`. После рестарта `{recovering}`, затем `{working}` с номером попытки или `run.failed{resume_limit}` после трёх попыток. Если runner не вернулся (нет heartbeat дольше 30 с, protocol.md), gateway публикует `run.outcome_unknown{heartbeat_lost}`.
4. **Порядок.** Внутри `run_id` порядок задаёт `seq`. Событие записывается в БД до публикации (как в gateway #4 и ADR-001 #7).
5. **Дедупликация.** Runner доставляет события at-least-once. Gateway игнорирует событие с уже записанным `event_id`, поэтому повтор после переподключения не создаёт дублей и не сдвигает `seq`. Runner строит `event_id` из `(project_id, run_id, seq журнала, номер)`, так что повторный перевод того же журнала даёт те же id.
6. **Replay.** Клиент запрашивает события с `seq > after_seq` (REST `GET …/runs/{run_id}/events?after_seq=N` или подписка WS/SSE). Состояние восстанавливается сворачиванием ленты с начала по таблице раздела 3. Живая доставка ничего не гарантирует, источник восстановления — сохранённая лента.
7. **Идемпотентность команд.** Повтор команды с тем же `client_request_id` возвращает существующий `run_id` и его `run.accepted`, а не новый запуск.
8. **Опоздавшее событие.** Событие с `seq` ≤ последнего применённого клиент отбрасывает. Пропуск `seq` клиент закрывает через replay и не применяет события через дыру.

## 5. Что не попадает в ленту

Постановка пользователя (она в истории сообщений gateway, а не в событиях runner), рассуждения модели (thinking), вызовы инструментов и их ввод/вывод, системные сообщения SDK, `session_id`, стоимость и токены, пути вне проекта, служебные каталоги (`.runner`, `.git`), содержимое файлов. Также не публикуются `access_denied`: это аудит оператора, а не продуктовое событие.

Артефакт в ленте представлен только метаданными. Содержимое выдаётся отдельным авторизованным запросом из подтверждённого снимка 2.3, а не из живой файловой системы.

## 6. Соответствие runner → v0

Реализация: `runner/src/ctrunner/v0.py`. CLI `ctrunner-v0 <project_id> < events.jsonl` печатает ленту с `seq`, как её запишет gateway.

| Журнал runner | v0 |
|---|---|
| `turn_started` | `run.started` (`run_id` = `turn_id`) |
| `sdk` assistant: текст | `message.created`; `role_id` по `parent_tool_use_id` |
| `sdk` assistant: `Agent`/`Task` | `role.started` (`role_id` = `subagent_type`) |
| `sdk` user: результат `Agent`/`Task` | `role.completed` |
| `sdk` assistant: `Write`/`Edit`/`MultiEdit`/`NotebookEdit` + успешный результат | запоминается до `checkpoint` |
| `checkpoint` | `artifact.created` на каждый успешно записанный файл проекта |
| `fork_question` / `fork_answered` | `facilitator.question` / `user.answer.accepted` |
| `turn_completed` | `run.completed` |
| `turn_failed{fatal}` (и `sdk_crashed` старых журналов) | `progress.updated{interrupted}` |
| `turn_failed` иначе | `run.failed{code=reason}` |
| `turn_failed{state_corrupt}` | `run.outcome_unknown` всех открытых запусков |
| `turn_interrupted` / `turn_resumed` | `progress.updated{recovering}` / `{working, attempt, context_restored}` |
| `session_started`, `access_denied`, `sdk` прочее | не публикуются |

Чтобы отличить падение процесса от провала, runner теперь пишет `turn_failed` при падении с `"fatal": true`.

## 7. Fixture

`docs/contracts/fixtures/*.v0.jsonl` — публичная лента в том виде, в каком её получает web после gateway:

| Файл | Сценарий |
|---|---|
| `happy.v0.jsonl` | пересмотр учебной программы: вопрос фасилитатора, две роли, артефакт, `artifact.saved` → `run.completed` |
| `failed.v0.jsonl` | падение runner, восстановление, затем `run.failed{result_error}` |
| `unknown.v0.jsonl` | падение runner без возврата → `run.outcome_unknown{heartbeat_lost}` |

События runner в fixture получены адаптером из журналов `runner/tests/journals/*.events.jsonl`. События gateway (`run.accepted`, `artifact.saved`, `run.outcome_unknown{heartbeat_lost}`) дописаны вручную по правилам раздела 4. Журналы составлены вручную в формате runner, а не записаны с живого контейнера. Тест `runner/tests/test_v0.py` проверяет, что fixture соблюдает правила раздела 4 и совпадает с выводом адаптера.

## 8. Рассмотренные альтернативы

| Вариант | Почему не выбран |
|---|---|
| Отдавать в web события gateway #4 (`message`, `tool`, `tool_result`, `thinking`, `raw_event`) | Это формат Claude CLI: публичный контракт сломается при обновлении CLI (2.4 §9), а `raw_event` и `tool` выносят внутренние данные |
| Адаптер в gateway | Gateway пришлось бы знать журнал runner и SDK. Адаптер в runner версионируется вместе с форматом журнала |
| `seq` присваивает runner | Gateway добавляет свои события (`run.accepted`, `artifact.saved`) в ту же ленту, поэтому владелец последовательности — тот, кто пишет ленту |
| Отдельный тип `run.interrupted` (терминальный) | После рестарта ход продолжается, а терминальное событие обязано быть последним |
| Проценты и ETA в `progress.updated` | Источника нет, значение было бы выдуманным |

## 9. Открытые вопросы

1. **Дмитрий:** подходит ли переход gateway #4 с `run.accepted`/`result`/`run.failed` на v0? Нужно ли сохранить `schema_version` gateway рядом со `schema`?
2. **Дмитрий, Катя:** отмена запуска (`run.cancelled`) — сейчас `stop` останавливает контейнер, а ход продолжается после рестарта. Добавляем в v0 или откладываем до v1?
3. **Арина:** `artifact_id` от runner стабилен (`commit_sha` + путь). Подходит ли он как `artifact_id` ADR-001, или 2.3 выдаёт свой, а `artifact.saved` связывает оба?
4. **Катя:** хватает ли `role_id` (`subagent_type`) для отображения, или нужен каталог ролей с `display_name` (ADR-001 `actor`)?
5. **Все:** нужен ли лимит длины `text` в `message.created` (большой ответ модели)?
6. Регистрация runner в gateway и передача `run_id` от gateway в команду — задача PPS-114. До неё `run_id` = `turn_id` runner.

## 10. Фактический статус и следующий результат

- Готово: адаптер runner → v0, CLI, три fixture, тесты (`pytest`, `ruff`, `mypy` чистые).
- Не сделано: согласование с владельцами; запись fixture с живого прогона (нужны Docker и cliproxy); поддержка v0 в gateway #4 и в web 2.5.
- Следующий проверяемый результат (14.10, «S2 Первый реальный E2E Run»): живой запуск runner проходит через gateway и даёт в web ту же последовательность типов, что `happy.v0.jsonl`.
