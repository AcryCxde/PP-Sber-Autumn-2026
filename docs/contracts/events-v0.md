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
| `usage.reported` | runner | `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `num_turns`, `duration_ms`, `duration_api_ms`, `cost_usd`; каждое поле — число \| null | без изменений |
| `artifact.created` | runner | `artifact_id`, `path` (от корня проекта), `commit_sha`, `role_id` \| null; необязательные `size_bytes`, `sha256` | без изменений |
| `artifact.saved` | gateway по подтверждению 2.3 | `artifact_id`, `version_id`, `size_bytes`, `download` | без изменений |
| `run.completed` | runner → gateway | `{}` | `completed` (терминальное) |
| `run.failed` | runner или gateway | `code`, `retryable` | `failed` (терминальное) |
| `run.outcome_unknown` | gateway; runner только при `state_corrupt` | `code` | `outcome_unknown` (терминальное) |

`message.created` нет в минимальном списке 03-team-spec. Он добавлен, потому что без него не показать ни ответ фасилитатора, ни сообщения ролей (2.1 §2, 2.5 §2).

Коды `run.failed`: `result_error`, `checkpoint_failed`, `resume_limit` (`retryable: true`), `disk_full` (`false`, нужен оператор), `artifact_save_failed` (gateway).
`run.failed{disk_full}` публикуется только при отказе commit; падение процесса по ENOSPC — `progress.updated{interrupted}`, как любое падение (§4.3).
Коды `run.outcome_unknown`: `heartbeat_lost`, `artifact_save_unknown` (gateway), `state_corrupt` (runner).

`size_bytes` и `sha256` (нижний регистр, 64 символа) в `artifact.created` — размер и хэш содержимого файла в коммите `commit_sha`. Runner считает их по объекту git сразу после commit, а не по рабочему дереву, и пишет в журнал как факт. Если прочитать файл не удалось (сбой git, путь не UTF-8 или с переводом строки, больше 2000 файлов в коммите, файл не менялся в этом коммите), полей в событии нет: они отсутствуют, а не равны `0` или пустой строке, и событие не теряется. Gateway сверяет с ними прочитанное из тома (`artifact.saved.size_bytes` должен совпасть).

`usage.reported` — только факты из `result` SDK: токены, число ходов модели (`num_turns`), длительность, стоимость в USD. Если SDK поле не передал или передал негодное значение, поле равно `null`; `0` означает, что SDK сообщил ноль. Runner ничего не оценивает и не досчитывает. Событие публикуется по одному на каждый `result` хода, до терминального события запуска (обычно одно; сложение нескольких — на стороне потребителя). `result`, пришедший после терминального события, не публикуется. Пример: `{"input_tokens": 18432, "output_tokens": 5210, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 40960, "num_turns": 9, "duration_ms": 95000, "duration_api_ms": 81200, "cost_usd": 0.41}`.

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

Постановка пользователя (она в истории сообщений gateway, а не в событиях runner), рассуждения модели (thinking), вызовы инструментов и их ввод/вывод, системные сообщения SDK, `session_id`, сырой `result` SDK (наружу идут только поля `usage.reported`), пути вне проекта, служебные каталоги (`.runner`, `.git`), содержимое файлов. Также не публикуются `access_denied`: это аудит оператора, а не продуктовое событие.

Артефакт в ленте представлен только метаданными. Содержимое выдаётся отдельным авторизованным запросом из подтверждённого снимка 2.3, а не из живой файловой системы.

## 6. Соответствие runner → v0

Реализация: `runner/src/ctrunner/v0.py`. CLI `ctrunner-v0 <project_id> < events.jsonl` печатает ленту с `seq`, как её запишет gateway.

| Журнал runner | v0 |
|---|---|
| `turn_started` | `run.started` (`run_id` = `turn_id`) |
| `sdk` assistant: текст | `message.created`; `role_id` по `parent_tool_use_id` |
| `sdk` assistant: `Agent`/`Task` | `role.started` (`role_id` = `subagent_type`) |
| `sdk` user: результат `Agent`/`Task` | `role.completed` |
| `sdk` result | `usage.reported` (поля из `usage`, `num_turns`, `duration_*`, `total_cost_usd`) |
| `sdk` assistant: `Write`/`Edit`/`MultiEdit`/`NotebookEdit` + успешный результат | запоминается до `checkpoint` |
| `checkpoint` | `artifact.created` на каждый успешно записанный файл проекта; `size_bytes` и `sha256` берутся из `checkpoint.files[path]` (runner пишет их после commit) |
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
6. ~~Передача `run_id` от gateway~~ решено: `run_id` = `turn_id` = `id` команды `message` (формат `[0-9A-Za-z_-]{1,64}`). Gateway выбирает `run_id` при создании запуска и шлёт его как `id`. Остаётся согласовать с PPS-114, что регистрация не делает то же иначе.

## 10. Фактический статус и следующий результат

- Готово: адаптер runner → v0, CLI, три fixture, тесты (`pytest`, `ruff`, `mypy` чистые); WebSocket-клиент runner (`hello`/`welcome`, досылка журнала, heartbeat, команды, `command_ack`), `run_id` = `id` команды, повторный вопрос после рестарта. Проверено фейковым gateway (`runner/tests/test_gateway_client.py`, e2e `runner/evals/test_question.py` с живым Claude в контейнере).
- Не сделано: серверная часть gateway (раздел 11), проверка настоящим gateway; согласование с владельцами; запись fixture с живого прогона (нужны Docker и cliproxy); поддержка v0 в gateway #4 и в web 2.5.
- Следующий проверяемый результат (14.10, «S2 Первый реальный E2E Run»): живой запуск runner проходит через gateway и даёт в web ту же последовательность типов, что `happy.v0.jsonl`.

## 11. Что требуется от gateway (Дмитрий)

Сторона runner готова и проверена фейковым gateway. Чтобы вопрос и ответ прошли через настоящий gateway, нужно на его стороне:

1. **Эндпоинт для runner.** WebSocket с проверкой `Authorization: Bearer <gateway_token>` (токен на проект). Формат кадров — `docs/2.4/protocol.md`.
2. **`hello` и `welcome`.** На `hello{project_id, last_seq}` отвечать `welcome{acked_seq}`; `acked_seq` — наибольший `journal_seq`, который gateway уже записал для проекта (0, если ничего). Хранить его надо по проекту.
3. **Приём `event`.** Тело — публичное событие v0 без `seq` и `schema`. Дедуплицировать по `event_id`, присваивать `seq` ленты при записи (раздел 4), добавлять `schema: "events.v0"` вместо `schema_version`. Один `journal_seq` может нести несколько событий.
4. **Статус `waiting_for_input`.** Включить его в число активных запусков (сейчас активны `pending`/`running`, и частичный уникальный индекс «один активный запуск на диалог» пропустил бы второй запуск во время вопроса). Ставить по `facilitator.question`, снимать по `user.answer.accepted`, по `progress.updated` и по новому `facilitator.question`.
5. **Проекция публичных типов.** Сейчас `facilitator.question`, `user.answer.accepted`, `progress.updated`, `artifact.created`, `usage.reported`, `role.*` превращаются в `legacy.redacted`. Они входят в контракт v0 и должны проходить.
6. **Создание запуска.** `command{kind: "message", id: <run_id>, text}`, где `run_id` выбирает gateway, формат `[0-9A-Za-z_-]{1,64}`. Ответ `command_ack`: `accepted` либо `rejected{reason}`.
7. **Ответ пользователя.** Команда клиента «ответ» превращается в `command{kind: "fork_answer", id: <новый>, fork_id: <question_id>, answers: {текст вопроса: метка или свой текст}}`. Ключи должны точно совпадать с `questions[].text`; multi-select склеивается через `", "`. Разбирать `command_ack`.
8. **Реакция на `rejected`.** `unknown_fork`: вопрос устарел (после рестарта runner спросит заново с новым `question_id`), ждать новый `facilitator.question`. `answers_mismatch`: ошибка клиента.
9. **Heartbeat.** Приходит каждые 10 с (`phase`, `health`, `last_progress_at`). Нет heartbeat дольше 30 с: `run.outcome_unknown{heartbeat_lost}`. `phase = awaiting_answer` при `health = ok` — штатное ожидание ответа, не зависание. Ожидание ответа живым прогоном проверено до 15 минут; при `state_corrupt` runner завершается (выход 65) до подключения клиента, `hello` и heartbeat не приходят, остаётся только `heartbeat_lost`.
10. **`run.completed` держать до `artifact.saved`** (правило 2 раздела 4).
11. **Открыто.** Канал передачи содержимого артефактов из тома проекта в хранилище 2.3 не определён: `artifact.created` несёт только метаданные (путь, `commit_sha`, `size_bytes`, `sha256`). При чтении файла gateway сверяет размер и sha256 с `artifact.created`; несовпадение — `run.failed{artifact_save_failed}`, а не тихая запись другого содержимого. Если полей нет, сверять нечем. Без него путь до download не замыкается.
