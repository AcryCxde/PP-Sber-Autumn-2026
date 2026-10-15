# 2.4. Facilitator question + resume — дизайн

Статус: **утверждён 2026-10-13**; задачи плана 0–11 выполнены (клиент проверен фейковым gateway), итоговая проверка — в отчёте исполнителя. Задача YouGile «S2 Runner: facilitator question + resume», срок 15.10.2026 10:00. Смежные: «Регистрация сессии в gateway», «Первый реальный E2E Run». Ветка `facilitator-question-resume`.
Основа: [дизайн runner](2026-09-25-runner-design.md), [дизайн восстановления](2026-10-01-runner-recovery-design.md), [events v0](../contracts/events-v0.md), [протокол](../2.4/protocol.md).
Текст задачи взят из постановки: YouGile MCP в этой сессии не подключился (нет открытой вкладки), саму карточку я не читал.

## Цель

Готово, когда:

1. runner выдаёт structured fork/question;
2. событие проходит через gateway;
3. ответ пользователя возвращается в runner;
4. продолжается тот же Run (без нового `run_id`, без второго `turn_started`);
5. recovery/checkpoint не ломается.

Приоритет: MVP к 15.10. Если срез по времени, режется то, что помечено «если успеем».

## Что есть сейчас (по коду)

| Звено | Состояние | Опора |
|---|---|---|
| `AskUserQuestion` → `fork_question` → ожидание → `fork_answered` → `PermissionResultAllow(updated_input+answers)` | работает в unit-тесте с фейковым вызовом. **Вживую не проверено**: журнал `happy.events.jsonl` составлен вручную (`events-v0.md` §7), eval на вопрос нет | `session.py:408-428`, `tests/test_session.py:241-265`, `evals/` без вопросов |
| Ожидание без таймаута, `phase=awaiting_answer`, `health=ok` | работает; таймаута нет ни в нашем коде, ни в `Query._handle_control_request` SDK (`await self.can_use_tool(...)` без `wait_for`). Таймаут на стороне CLI не проверен | `session.py:421`, `turn.py:35-37`, `liveness.py:25` |
| Ответ по inbox: `ctrunner-inbox fork-answer` / `sessions answer`, метки сопоставляются вопросам по журналу | работает | `inbox.py:86-112`, `sessions.py:167-168` |
| Команда `fork_answer` в Session | подтверждается в inbox **до** исполнения (at-most-once); неизвестный или закрытый `fork_id` — только `diag("fork_answer_dropped")`, пользователь и gateway ничего не узнают | `session.py:269-271`, `364-369` |
| Адаптер v0: `fork_question`→`facilitator.question`, `fork_answered`→`user.answer.accepted`, `question_id = fork_id`, `allow_custom` всегда true | работает, покрыт тестами | `v0.py:156-165`, `365-382` |
| Состояние `waiting_for_input` | в v0 **выводится** потребителем из `facilitator.question` (`events-v0.md` §3); runner его не публикует. В heartbeat/`health.json` есть `phase=awaiting_answer` | `events-v0.md:45`, `health.py` |
| `run_id` ↔ `turn_id` | `turn_id = uuid4().hex`, генерирует runner в момент старта хода; gateway свой `run_id` передать не может (открытый вопрос 6 events v0) | `session.py:94-95,265,319` |
| WebSocket-клиент, `hello`, `welcome`, `command_ack`, heartbeat по сети, отправка событий | **нет**. Только текст в `docs/2.4/protocol.md`. В зависимостях нет `websockets` | `pyproject.toml:4`, `uv.lock` |
| Вопрос на рестарте | не переносится (сознательный non-goal восстановления) | `docs/plans/2026-10-01-runner-recovery-design.md:71` |

## Главный риск: открытый вопрос и рестарт

Разобрано по коду, что происходит, когда процесс падает или контейнер перезапускают, пока `can_use_tool` ждёт ответа.

1. **Ожидание живёт только в памяти.** `_forks: dict[str, Future]` (`session.py:137`) не попадает в `PersistedState` (`session.py:342-350`, `state.py:34-47`). Новый процесс его не знает.
2. **Ход остаётся открытым.** `state.json` хранит `open_turn` (`session.py:469-476`), поэтому `plan()` даёт `Continue(attempt+1)` (`recovery.py:56-65`); `head.turn_id` не совпадает, commit в середине хода не делается, дерево с неподтверждённой работой остаётся.
3. **Что видит потребитель.** `crash()` пишет `turn_failed{reason, fatal}` → v0 `progress.updated{interrupted}` (`session.py:189`, `v0.py:291-293`); затем `turn_interrupted` → `{recovering}`, `turn_resumed` → `{working}`. Предыдущий `facilitator.question` по таблице §3 больше не актуален (состояние `running`), нового вопроса нет, пока модель не спросит снова.
4. **Что видит модель.** `continuation_prompt` повторяет только исходную задачу (`recovery.py:68-83`). При `mode=resume` в транскрипте висит `tool_use AskUserQuestion` без результата. Для `Bash` спайк показал, что CLI сам дописывает синтетический `tool_result` «[Request interrupted by user for tool use]» и API принимает такой resume (recovery-design, Findings п.4). Для `AskUserQuestion` не проверено. Модель может спросить заново, а может принять «interrupted» за отказ отвечать и пойти по умолчаниям.
5. **Потеря ответа.** Если пользователь ответил на старый `fork_id` (команда лежит в inbox или пришла после рестарта), `_answer` её молча отбросит (`session.py:365-368`), а команда уже подтверждена. Пользователь не получит `user.answer.accepted` и не поймёт, что его ответ пропал.
6. **Счётчик попыток.** Ожидание само по себе попытки не тратит: `autoresume_count` сбрасывается только завершением хода (`session.py:352-355`). Каждый рестарт во время ожидания тратит одну из трёх попыток (`MAX_AUTORESUME`, `recovery.py:11`). После трёх — `GiveUp`.
7. **Git checkpoint.** Вопрос коммитов не создаёт. Commit с `Turn-Id` делается только после `result` (`turn.py:141`, `session.py:322-334`). Значит, `Finalize` для хода с вопросом невозможен, дерево целиком достаётся продолжению. Это безопасно.
8. **Живость.** Пока ждём, `health=ok` и `STALL_AFTER_S` не применяется. После рестарта `phase` снова `working`: если модель не спросит заново и зависнет, сработает обычная проверка `stalled`.

Вывод: нужно, чтобы (1) модель надёжно спросила снова, (2) пользователь узнавал, что его ответ на мёртвый вопрос не принят, (3) поведение было проверено тестом и вживую.

## Дефекты ревью PR #8: подтверждены

Оба воспроизводятся по коду. Для (а) я прогнал сценарий через `Session` с фейковым клиентом (скрипт вне репозитория): после `crash()` и последующего `GiveUp("t1")` журнал остаётся `turn_interrupted, turn_resumed, turn_failed(fatal)`, `resume_limit` не дописывается.

**(а) `resume_limit` не пишется после краша.** `crash()` кладёт в журнал открытого хода `turn_failed{reason:"sdk_crashed", fatal:true}` (`session.py:189-190`). Ветка `GiveUp` пишет `turn_interrupted` и `resume_limit` только если `TURN_FAILED not in self._turn_kinds(turn_id)` (`session.py:235`), а `kinds_of_turn` собирает все виды строк хода без разбора `fatal` (`eventlog.py:85-96`). Лимит в три попытки достижим только после падений процесса, значит, в боевом сценарии журнал всегда содержит такой `turn_failed`. Итог: потребитель видит `interrupted` и `working` бесконечно и никогда `run.failed{resume_limit}`. Существующий тест `test_give_up_fails_turn_...` (`tests/test_session.py:542`) стартует с пустым журналом и дефект не ловит.

Минимальное исправление: `kinds_of_turn` не учитывает `turn_failed` с `fatal: true`, потому что падение процесса не итог хода (то же правило, что в `v0.py:291`). Идемпотентность остаётся: `resume_limit` — не fatal, повторный `GiveUp` его увидит. `Finalize` смотрит только `CHECKPOINT`/`TURN_COMPLETED` и не затронут. Тест: краш → новый процесс с `autoresume_count=3` → в журнале `turn_interrupted, turn_failed{resume_limit}`, адаптер даёт `run.failed{resume_limit}`.

**(б) `disk_full` из `crash()` становится `interrupted`.** `crash()` определяет `disk_full` по ENOSPC (`session.py:179-180`) и всегда добавляет `fatal: True` (`session.py:189`); `v0.py:291` превращает fatal в `progress.updated{interrupted}`. `run.failed{disk_full}` в v0 достижим только из ветки commit (`on_commit_failed` → `turn_failed{disk_full}` без `fatal`, `session.py:329`, тест `test_session.py:449`).

Рекомендую **не менять код, а закрепить поведение**: падение процесса не исход хода (`events-v0.md` §4.3), после рестарта ход продолжается, и итогом станет либо `completed`, либо `resume_limit` (с исправлением (а)). Если сделать `disk_full` не-fatal, адаптер объявит ход `failed`, а runner после рестарта продолжит его в `Continue`, и все дальнейшие события будут проглочены `_live` (`v0.py:189-192`): интерфейс покажет провал при фактически выполненной работе. Кроме того, при полном диске запись самого `turn_failed` в `crash()` скорее всего не удастся (`session.py:191-192` глотает `OSError`). Что делаем: тест `{"reason":"disk_full","fatal":true}` → `interrupted`, правка текста в `protocol.md:110`, `recovery-design:52` и `events-v0.md` §3 («`disk_full` как `run.failed` — только отказ commit»). Альтернатива (не рекомендую): `fatal` только для `sdk_crashed` плюс запрет автопродолжения хода, помеченного `disk_full`, то есть новая ветка планировщика и журнал в его входе.

## Что умеет gateway из `origin/demo-gateway` (PR #4, не слит)

Прочитано через `git show origin/demo-gateway:<путь>`.

- Это **не** gateway для runner. Он сам запускает локальный `claude` через `subprocess.Popen` в рабочем каталоге диалога (`backend/main.py:76-108`, `407-412`). Эндпоинта для runner, `hello`, `welcome`, heartbeat нет. Единственный WebSocket `/ws` для клиентов принимает `run.create` и `run.subscribe` (`main.py:738`, `798`); команды ответа на вопрос нет.
- Публичный allowlist: `run.accepted`, `run.started`, `context.reset`, `message.created`, `artifact.saved`, `run.completed`, `run.failed`, `run.outcome_unknown`, `legacy.redacted` (`backend/public_events.py:9-19`). Любой другой тип, включая `facilitator.question` и `user.answer.accepted`, проецируется в `legacy.redacted` (`public_events.py:165-172`).
- Статусы запуска: `pending|running|completed|failed|cancelled|outcome_unknown`, `waiting_for_input` нет. Частичный уникальный индекс разрешает один активный запуск (`pending`/`running`) на диалог (`models.py:69-74`, `repository.py:25`): если не добавить статус в активные, во время вопроса можно стартовать второй запуск.
- Версия поля `schema_version`, а не `schema: "events.v0"` (`public_events.py:7`); `seq` присваивается при записи (`repository.py` `append_event`) — совместимо с v0.
- Артефакты gateway берёт из успешных `Write` в потоке CLI (`main.py:336-369` `persist_artifact`), а не из `artifact.created` runner; «проекта» как сущности нет.

Следствие: сквозной путь «через gateway» требует работы Дмитрия (эндпоинт runner, приём v0, `waiting_for_input`, команда ответа, пропуск новых типов в проекции). Я проектирую контракт и сторону runner, а проверяю её **фейковым gateway** в тестах. Реальная интеграция — отдельная проверка после готовности его части.

## Подходы

### Транспорт runner ↔ gateway

| | Подход | За | Против |
|---|---|---|---|
| **T1 (выбран)** | WS-клиент в runner по `protocol.md`: `hello/welcome/event/heartbeat/command/command_ack`. События — драфты v0 из `Translator`, команды складываются в существующий файловый inbox | один путь в Session без правок; durable-цепочка «журнал → сеть» и «сеть → inbox → state» сохраняется; соответствует spec (0 входящих портов); закрывает смежную задачу регистрации | новая зависимость `websockets` (резолвится: 15.0.1), пересборка образа |
| T2 | gateway сам ходит `docker exec ctrunner-inbox …` и `tail events.jsonl | ctrunner-v0` | нет кода в runner, нет зависимостей | противоречит spec (регистрация, heartbeat, runner инициирует соединение), gateway обязан иметь docker-доступ, нет `hello` |
| T3 | WS как в `protocol.md` буквально: по сети идут строки журнала, перевод в gateway | меньше кода в runner | отвергнуто в `events-v0.md` §8: gateway начнёт знать формат SDK и журнала |

### Открытый вопрос при рестарте

| | Подход | За | Против |
|---|---|---|---|
| R0 | как сейчас: рестарт прерывает вопрос, промпт без подсказки | нулевая стоимость | модель может не спросить; ответ на мёртвый вопрос тихо теряется |
| **R1 (выбран)** | «Спросить заново с подсказкой»: из журнала вычисляется набор вопросов, оставшихся без ответа у прерванного хода; `continuation_prompt` прямо просит задать их снова; ответ на закрытый вопрос получает `command_ack rejected` | журнал уже durable, схема `state.json` не меняется; чистая функция, тестируется без Docker; новый `question_id` естественно вытесняет старый | поведение модели остаётся допущением до вживую-проверки; пользователь отвечает дважды, если успел ответить до падения |
| R2 | «Удержать и подставить»: после рестарта не вызывать модель, повторно опубликовать тот же вопрос с тем же `fork_id`, ждать ответ, вставить его в `continuation_prompt` | пользователь не отвечает дважды; нет зависимости от модели; попытка не тратится | новое состояние хода (ожидание без `query`), новая ветка планировщика и учёта попыток, тесты на гонки ответ/рестарт; на срок 15.10 не помещается |

R2 не отбрасываю навсегда: он надстраивается над R1 на тех же данных журнала (набор вопросов уже вычислен), поэтому выбор R1 его не закрывает.

## Решение

**T1 + R1 + два исправления дефектов + привязка `run_id` к команде.**

### Компоненты

| Файл | Изменение |
|---|---|
| `eventlog.py` | `kinds_of_turn` игнорирует `turn_failed{fatal}` (дефект а). Новая `open_forks(path, turn_id) -> tuple[OpenFork, ...]`: свёртка строк хода — `fork_question` добавляет, `fork_answered` убирает, `turn_interrupted` набор не очищает: вопрос переживает любое число рестартов подряд, пока модель не задаст новый `fork_question` (он вытесняет открытые до прерывания) или не придёт `fork_answered` |
| `recovery.py` | `continuation_prompt(step, asked)` добавляет блок «Ты задавал пользователю вопросы, ответа нет: … Задай их заново через AskUserQuestion до продолжения работы» (оба режима, `resume` и `fresh`) |
| `session.py` | в конструктор `open_forks: Callable[[str], Sequence[OpenFork]]` (как `turn_kinds`); в `Continue` подсказка берётся из него. Реестр `_fork_questions: dict[str, tuple[str, ...]]` рядом с `_forks` и публичные `fork_questions(fork_id) -> tuple[str, ...] | None`. `turn_id := cmd.id` вместо `_new_id()` на `session.py:265,319` |
| `protocol.py` | `parse_command` проверяет `id` по `[0-9A-Za-z_-]{1,64}`: он теперь попадает в trailer `Turn-Id` и в `run_id`, перевод строки в нём ломает `read_head` |
| `gateway.py` (новый) | `GatewayClient`: подключение с Bearer, `hello`, `welcome`, повторная отправка с `acked_seq`, heartbeat, приём команд, реконнект с backoff. Не падает вместе с Session; без `GATEWAY_URL` не запускается |
| `v0.py` | без изменения контракта. Если успеем: добавить `Feeder` (инкрементальная свёртка журнала поверх `Translator`) |
| `main.py` | чтение `GATEWAY_URL` и токена `/run/secrets/gateway_token`, запуск клиента задачей рядом с `run_with_resume`; `open_forks` в `build_session` |
| `sessions.py` | `GATEWAY_URL` в `ENV_ALLOW` (хост `localhost` переписывается так же, как `ANTHROPIC_BASE_URL`), `secrets init` пишет и токен gateway |
| `evals/` | `fake_gateway.py` (сервер `websockets`) и `test_question.py` (e2e: вопрос → ответ → тот же ход → артефакт; вариант с `docker kill` во время ожидания) |
| docs | `protocol.md`, `events-v0.md` §6/§9/§10, `RUNNER-RECOVERY.md`, `runner/README.md` |

### `run_id`

`turn_id = cmd.id`. Gateway шлёт `command{id: <run_id>, kind: message}`; события runner несут тот же `run_id`. Повтор команды с тем же `id` и так отбрасывается inbox (идемпотентность, правило 7 events v0). Схема `state.json` и `MessageCmd` не меняются: `OpenTurn.turn_id` уже хранится. Это закрывает вопрос 6 events v0 минимальным способом и пересекается с PPS-114: согласовать, что «регистрация» не делает то же иначе.

### Поток данных

Счастливый путь (один Run):

1. gateway: `run.accepted` (своя запись) → `command{id: run_id, kind:"message", text}`.
2. runner: `GatewayClient` проверяет команду (`parse_command`), кладёт файл в inbox (`inbox.put`), отвечает `command_ack{accepted}`.
3. Session: ход открыт (`state.json` → `query`), в журнал `turn_started` → драфт `run.started`.
4. Модель вызывает `AskUserQuestion` → `can_use_tool` → `fork_question` → `facilitator.question{question_id=fork_id}` → gateway ставит `waiting_for_input`. Heartbeat: `phase=awaiting_answer`.
5. Пользователь отвечает → gateway: `command{id: <новый>, kind:"fork_answer", fork_id=question_id, answers:{текст вопроса: метка | свой текст}}`; multi-select склеивается `", "` (формат проверить спайком).
6. runner: `GatewayClient` проверяет через `session.fork_questions(fork_id)`: вопрос открыт и ключи `answers` совпадают с текстами вопросов. Иначе `command_ack{rejected, reason: unknown_fork | answers_mismatch}`, в inbox ничего не кладётся. Иначе файл в inbox → `accepted`.
7. Session: `_answer` снимает future, `fork_answered` → `user.answer.accepted` (состояние `running`), SDK получает `updated_input.answers`, ход идёт дальше тем же `turn_id`.
8. Конец хода: commit → `checkpoint` → `turn_completed` → `state.json` (порядок восстановления не меняется); адаптер: `artifact.created`…, `run.completed`; gateway по правилу 2 ждёт `artifact.saved`.

Рестарт во время ожидания:

1. Падение: `turn_failed{fatal}` (best-effort) → `progress.updated{interrupted}`.
2. Новый процесс: `Continue` → `turn_interrupted` → `turn_resumed`; промпт содержит подсказку с текстами неотвеченных вопросов.
3. Модель спрашивает снова → новый `fork_id` → новый `facilitator.question` → `waiting_for_input`. Старый `question_id` у gateway уже недействителен (по `interrupted`).
4. Ответ на старый `fork_id` получает `rejected: unknown_fork` сразу, а не молчание.

### Протокол WS (изменения относительно `protocol.md`)

| Сообщение | Изменение |
|---|---|
| `event` | вместо строки журнала: `{"v":1,"type":"event","journal_seq":N,"event":{event_id, project_id, run_id, occurred_at, type, payload}}`. Вложенный объект нужен, потому что `type` конверта и `type` события v0 совпадают по имени. `seq` v0 присваивает gateway (`events-v0.md` §4.4) |
| `hello.last_seq`, `welcome.acked_seq` | номера **журнала runner**; после `welcome` клиент заново переводит журнал с начала (`Translator` — свёртка с первой строки) и шлёт драфты строк с `seq > acked_seq`; повтор схлопывается по `event_id` |
| `heartbeat` | содержимое `health.json`; `turn_id` — `null` в MVP |
| `command_ack` | `accepted`, `rejected{reason}`; `duplicate` в MVP не различаем (дубль отсекает inbox) |

### Отказы

| Случай | Поведение |
|---|---|
| gateway недоступен | runner работает автономно, события копятся в журнале; реконнект 1→30 с, после `welcome` досылка |
| обрыв посреди отправки | at-least-once, дубли по `event_id` |
| команда не разобрана | `command_ack{rejected, reason}`, файл не создаётся |
| `fork_answer` для закрытого вопроса | `rejected`, журнал не пишется |
| рестарт во время вопроса | R1: переспрашивание; три рестарта подряд → `resume_limit` (после исправления (а) событие реально пишется) |
| запоздавший ответ между проверкой и `_answer` | остаётся `fork_answer_dropped` в `diag`; окно — доли секунды (опрос inbox 1 с) |
| WS-задача упала | логируется, переподключение; Session не затрагивается |

### Совместимость и миграция

`state.json` не меняется. Формат журнала не меняется (новых видов событий нет). Контракт v0 не меняется. `turn_id` после перехода на `cmd.id` для команд из `ctrunner-inbox` остаётся `uuid4().hex`. Существующие тома со старыми ходами читаются как раньше. Новая зависимость: `websockets` в `pyproject.toml` и `uv.lock`, образ пересобирается.

## Тестирование

- Unit: `open_forks` (вопрос, ответ, `turn_interrupted` очищает), `continuation_prompt` с подсказкой в обоих режимах, `kinds_of_turn` и `GiveUp` после краша (дефект а), `disk_full`+`fatal` → `interrupted` (дефект б, закрепляющий тест), `turn_id == cmd.id`, ограничение на `id`, `fork_questions` до/после ответа.
- `GatewayClient` против сервера `websockets` в тесте: `hello`→`welcome`→досылка с `acked_seq`, ответ на команду, `rejected` для закрытого вопроса, реконнект.
- Регресс восстановления: прогон всех 227 существующих тестов (`uv run pytest`) плюс сценарий «рестарт при открытом вопросе» на `FakeClient`: `turn_interrupted → turn_resumed`, в `prompts[-1]` текст вопроса, второй `fork_question`, ответ, один `turn_started` и один `turn_completed` на ход, `checkpoint` один.
- Живая проверка (до остального кода, см. спайк): реальный `AskUserQuestion` через cliproxy, формат `questions` и `answers`, поведение при ожидании 15+ минут, resume после `kill` во время ожидания.
- E2E на контейнере с фейковым gateway: `create project → start → run.started → waiting_for_input → answer → running → artifact.created → run.completed`. `artifact.saved` и download подставляет фейковый gateway: передача содержимого артефакта из тома не спроектирована (вне границ).
- Шлюзы: `uv run pytest`, `uv run ruff check`, `uv run ruff format --check .`, `uv run mypy src tests evals`.

## Вне границ (non-goals)

- Реализация серверной части gateway (эндпоинт runner, `waiting_for_input`, `run.answer`, проекция новых типов, смена `schema_version` на `schema`): Дмитрий. Для него будет список требований в `events-v0.md`.
- R2 (удержание вопроса через рестарт без обращения к модели).
- Передача содержимого артефакта из тома в хранилище 2.3 и download: нет спецификации канала; без него «Первый E2E Run» до `download` не доходит.
- Создание и запуск контейнера проекта из gateway (оркестрация `sessions`).
- Отмена запуска, таймаут вопроса, ответ обычным сообщением, редактирование ответа, несколько одновременных запусков в одном проекте.
- TLS и ротация токена gateway сверх Bearer.
- Правка типов v0, процентов/ETA.

## Риски

1. **Живое поведение `AskUserQuestion` не проверено.** Журнал-образец рукописный. Если CTF/модель через cliproxy не вызывает инструмент или CLI не маршрутизирует его в `can_use_tool`, вся цепочка бессмысленна. Поэтому спайк — первая задача; при провале останавливаемся и решаем с заказчиком задачи.
2. **Серверной части gateway нет.** Без неё критерий 2 и 3 проверяются только на фейке. Срок 15.10 зависит от темпа Дмитрия; нужно подтвердить его участие сегодня.
3. **Переспрашивание зависит от модели.** R1 повышает вероятность подсказкой, но не гарантирует; `mode=fresh` ещё слабее. Смягчение: тест на `FakeClient` проверяет контракт промпта, спайк — поведение.
4. **Resume после оборванного `AskUserQuestion`.** Проверен только оборванный `Bash` (recovery-design, Findings п.4).
5. **Таймаут CLI на `can_use_tool`.** В SDK его нет, в CLI не проверено; длительное ожидание человека может оборваться.
6. **Трата попыток.** Каждый рестарт при ожидании съедает одну из трёх.
7. **`websockets` и образ.** Новая зависимость, пересборка, прогон `--require-hashes`.
8. **Срок.** До 15.10 10:00 около двух дней; WS-клиент и e2e самые объёмные части.
9. **Пересечение с PPS-114.** Привязка `run_id = cmd.id` и WS-клиент делаются в рамках этой задачи; нужно согласование, чтобы смежная задача не дублировала.

## Порядок и срез по времени

1. Спайк вживую (риск 1, 4, 5): без него остальное строится на допущении.
2. Дефекты (а), (б): мелкие, независимые.
3. `open_forks` + подсказка + реестр `fork_questions` + `turn_id = cmd.id`.
4. `GatewayClient` + фейковый gateway + тесты.
5. e2e и документы.
6. Если успеем: инкрементальная отправка без пересчёта журнала, R2.

Срез при нехватке времени: сохраняем 1, 2, 4 и ядро 3 (подсказка). Режем инкрементальный `Feeder` и e2e с `docker kill`, оставляя их ручными проверками.

## Допущения, которые нужно подтвердить при утверждении

- Серверную сторону gateway делает Дмитрий; в этой задаче проверяем контракт фейком.
- WS-клиент входит в эту задачу (иначе критерии 2 и 3 недостижимы) и закрывает «Регистрацию сессии в gateway».
- `disk_full` из `crash()` остаётся `interrupted` (рекомендация по (б)).
- Привязка `run_id = cmd.id` приемлема для gateway.

## Findings

Живой спайк задачи 0, 2026-10-13. Реальный Claude через cliproxy (модель `claude-5.6-sol`), `claude-agent-sdk==0.2.158`, скрипт вне репозитория. Ключи и токены в записи не попадают.

### Что подтверждено

1. **`can_use_tool` вызывается для `AskUserQuestion`.** Вызов приходит с `name == "AskUserQuestion"` до выполнения инструмента, модель ждёт возврата колбэка. Плановая остановка («колбэк не вызывается или модель не спрашивает») не наступила.
2. **Ключи `tool_input`:** `{"questions": [{"question": str, "header": str, "options": [{"label": str, "description": str}], "multiSelect": bool}]}`. `header` короткий («Формат», «Разделы»), `description` есть у каждого варианта. `allow_custom` в структуре нет (в v0 он всегда `true` по решению адаптера).
3. **Формат ответа принят.** `PermissionResultAllow(updated_input={**tool_input, "answers": {<текст вопроса>: <ответ>}})`; multi-select склеен через `", "` (`"Итоги, Риски"`). CLI превращает это в `tool_result`: `Your questions have been answered: "Какой формат?"="md", "Какие разделы?"="Итоги, Риски". You can now continue with these answers in mind.` Модель дальше создала `report.md` с обоими разделами. Правило склейки `", "` остаётся.
4. **Модель спрашивает при прямой формулировке.** Промпт «Сначала задай мне два вопроса ОДНИМ вызовом инструмента AskUserQuestion: …» сработал с первого раза во всех запусках. Одним вызовом пришли оба вопроса. Без явного указания инструмента поведение не проверялось.

### Круги

| Круг | Что проверяли | Результат |
|---|---|---|
| A | обычный вопрос → ответ (задержка 2 с) | вопрос, ответ, `ResultMessage success`, файл `report.md`; 3 хода SDK, около 24 с |
| B | ожидание ответа 900 с (15 минут) | CLI не оборвал ожидание: ответ принят, `ResultMessage success`, `duration_ms=922834` (900 с ожидания + около 23 с работы), `report.md` создан. Предел таймаута CLI на `can_use_tool` в пределах 15 минут не обнаружен; более длинное ожидание не проверялось |
| C (resume) | убит дочерний CLI (`kill -9`) во время ожидания; новый процесс с `resume=<session_id>` и подсказкой «задай вопросы заново» | API принял resume; модель спросила заново (новый `tool_use` id), получила ответ, закончила `success` |
| C (fresh) | то же без `resume`, новый каталог конфигурации, с подсказкой | модель спросила один раз (промпт сам требует вопрос: слабая проверка) |
| C (resume, контроль) | resume того же оборванного хода, промпт «Продолжи работу: подготовь отчёт report.md с разделами Итоги и Риски» | **без подсказки** модель вопрос не задала и сразу записала файл по умолчаниям; **с подсказкой** задала вопрос заново и дождалась ответа |
| D | контейнер: `sessions start/send/answer` | в журнале `fork_question{fork_id, questions}` (seq 9) → `fork_answered{fork_id, answers}` (seq 10) → `checkpoint` → `turn_completed`; `answers` = `{"Какой формат?": "md", "Какие разделы?": "Итоги"}` |

Транскрипт оборванного хода (круг C): после `kill` в файле сессии остаётся `tool_use AskUserQuestion` без результата. При resume CLI сам дописывает `tool_result` с `is_error: true` и текстом `[Request interrupted by user for tool use]`, затем пользовательское сообщение `Continue from where you left off.` и ответ ассистента `No response requested.`; только после этого идёт наш промпт. API такую цепочку принимает.

Значение `questions` из журнала (круг D) для тестов задач 8–10:

```json
{"fork_id": "243b1ce928b748a2807e12e634cc15fd", "questions": [{"question": "Какой формат?", "header": "Формат", "options": [{"label": "md", "description": "Создать Markdown-файл report.md."}, {"label": "txt", "description": "Создать текстовый файл report.txt."}], "multiSelect": false}, {"question": "Какие разделы?", "header": "Разделы", "options": [{"label": "Итоги", "description": "Добавить раздел «Итоги»."}, {"label": "Риски", "description": "Добавить раздел «Риски»."}, {"label": "Планы", "description": "Добавить раздел «Планы»."}], "multiSelect": true}]}
```

### Последствия для плана

| Находка | Последствие |
|---|---|
| `can_use_tool` вызывается, формат `answers` и `", "` подтверждены | задачи 3–10 продолжаются без правок формата |
| без подсказки после resume модель не переспрашивает, с подсказкой переспрашивает | R1 оправдан: подсказка в `continuation_prompt` (задача 3) обязательна, текст из плана достаточен (в спайке использована та же формулировка) |
| resume после оборванного `AskUserQuestion` API принимает | `mode=resume` для хода с открытым вопросом остаётся допустимым |
| CLI дописывает синтетические `tool_result` и «Continue from where you left off.» | в транскрипте перед нашим промптом появляется служебный обмен; на логику runner не влияет, но `prompts[-1]` в тестах остаётся только нашим текстом |
| ожидание 15 минут CLI не обрывает | в `RUNNER-RECOVERY.md` (задача 11) записать «проверено до 15 минут»; мера R2 по этому риску не нужна (риск 5 снят до 15 минут, выше не проверено) |
| `header` и `description` вариантов приходят от модели | адаптер v0 их переносит как есть; тест задачи 10 не должен рассчитывать на конкретные тексты `description` |
