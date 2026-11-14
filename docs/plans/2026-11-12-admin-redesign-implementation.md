# Admin Redesign V1 Implementation Plan

> **For the implementer:** Use `executing-plans` to execute this plan task-by-task.

**Goal:** Перенести только админку в основной Shell и дать администратору безопасный обзор Run и бессрочную ручную AI-only заморозку без прерывания уже допущенной работы.

**Architecture:** Основной React frontend получает Overview/Users и read-only панель Run; backend предоставляет отдельный versioned admin API, сохраняя старые read-контракты на время миграции потребителей. Freeze и окончательный допуск конкретного Run сериализуются одной транзакционной блокировкой User в основной SQL БД; admission выполняется до передачи работы CLI/runner. Обновление UI — HTTP polling, существующий runner WebSocket используется только для совместимого переноса allowlist metadata, не создаётся новый канал.

**Tech stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy async 2.x, Alembic, SQLite/PostgreSQL; runner Python/SDK 0.2.158; React 19, TypeScript, Vite, Vitest, ESLint; npm для frontend, pnpm 10.11.0 для web-product.

**Constraints:** Утверждённый дизайн `docs/plans/2026-11-12-admin-redesign-design.md`, APPROVED 2026-11-12, явное «Утверждаем». Текущая ветка `feat/admin-redesign`; не менять пользовательские изменения и `homework3/`. Этот документ — разрешённое планирование, не реализация. Нет commit/push/deploy, запуска приложения/моделей, чтения `.env.gateway`, runtime journals или диалогов. Все пути ниже относительно `/Users/romanov/PycharmProjects/PP-Sber-Autumn-2026`; номера строк относятся к исходникам на `a1d6179` и служат якорями рядом с символами, не координатами после правок. Каждый новый файл/символ ниже явно обозначен как **Create/new**, а не выдан за существующий.

**Non-goals:** Перенос остальных страниц web-product; общий shared-shell пакет; WebSocket для админки; charts, проценты, ETA/«зависло» по времени; admin retry/cancel; raw prompts/errors/logs/files; бан аккаунта/auth revoke; квоты, денежные KPI, суммы cumulative costs по Run/пользователям/периодам; upstream attribution/прайсы/LiteLLM reconciliation; новые платные эксперименты.

---

## Evidence, среда и порядок исполнения

Прочитаны definitions/callers/tests, manifests и конфигурация тестов, а не только исследовательский отчёт:

| Граница | Проверенные исходники и исходное поведение |
|---|---|
| Run/связи | `backend/models.py:51–70,103–190,341–461`: User.last_seen_at существует; Run.stage отсутствует; Run.cost_usd nullable; ProjectRun.stage — индекс; ProductRun.stage_id — строка; accepted отдельно |
| Принятие ≠ admission | `backend/repository.py:245–398,411–499`: accept создаёт pending; mark_run_running сейчас не проверяет пользователя/политику, возвращает None для не-pending, а callers всё равно могут продолжать исполнение |
| Все входы | `backend/main.py:269–379,491–526,789–819,853–1090`: raw legacy text, run.create, project.run(stage/revision/answer), фоновые задачи и execute_accepted_run; `backend/api/product.py:161–230,341–421,549–594`: V2 start/rework/answer и replay |
| Runner | `backend/runner_executor.py:63–79,157–188,202–216`: answer нового backend Run может продолжать старый transport Run; `backend/runner_transport.py:174–319`: transport queued сам по себе не является политикой допуска; `backend/runner_app.py:114–143`: уже существующий канал команд |
| Admin/auth | `backend/api/admin.py:38–102,127–251,263–348`: старые engagement DTO, нет серверной пагинации; `backend/auth.py:112–179`: AdminUser — 401/403, activity может commit до handler; `backend/api/auth.py:25–34`: HttpOnly/SameSite=Lax cookie |
| Harness | `runner/src/ctrunner/v0.py:109–122,140–146,440–453`, `runner/src/ctrunner/gateway.py:36–64,89–122,239–274`, `backend/runner_transport.py:24–39,70–96,149–152`: v0 cost — scalar без scope; лишние поля отклоняются; `backend/parser.py:243–255` — legacy result тоже без доказанного scope |
| UI | `frontend/src/coreteams/router.ts:4–29`, `Shell.tsx:37–82,94–117`, `api.ts:103–127`, `frontend/src/auth.ts:18–46`; старый вход: `web-product/src/main.tsx:1–36`, ссылка `web-product/src/App.tsx:846–847` |
| DB/tests | `backend/db.py:36–82`, `backend/migrations/versions/20261111_0005_selected_materials.py:10–21`, `backend/tests/test_migrations.py:369–412`, `backend/tests/databases.py:19–66`; текущая head — `20261111_0005` |
| Проверки | Корневой `pyproject.toml:23–35`, `runner/pyproject.toml:17–52`, `frontend/package.json:6–31`, `frontend/vite.config.ts`, `web-product/package.json:5–26`, `.github/workflows/ci.yml:18–66` |

Класс будущего изменения **Boundary**: API + SQL storage + межпроцессный transport + UI. Сохранять текущие ownership/auth/idempotency/artifact-confirmation contracts. Не переносить product chain statuses на backend Run.

**Проверено в этой сессии:** `runner/.venv/bin/python -m pytest --version` → pytest 9.1.1; Node v20.19.0, npm 10.8.2, pnpm найден. Скрипты тестов/сборок существуют в manifest/CI. **Не проверено запуском:** тесты/сборки. В runner venv отсутствует FastAPI (`ModuleNotFoundError` при безопасном import), frontend node_modules отсутствует; Node 20 не удовлетворяет web-product `>=22.12`. Не называть текущую среду готовой или тесты зелёными.

### Подготовка будущего исполнения (не выполнять при написании плана)

Из корня repo, Python 3.12 и Node >=22.12 (подходит обеим сборкам):

```sh
python3.12 -m venv /tmp/coreteams-admin-plan-venv
/tmp/coreteams-admin-plan-venv/bin/python -m pip install '.[test]' './runner'
npm --prefix frontend ci
pnpm --dir web-product install --frozen-lockfile
```

Для команд ниже `PY=/tmp/coreteams-admin-plan-venv/bin/python`; в новой shell задать заново. Зависимости устанавливать по существующим lockfiles; не обновлять версии попутно. Все model/process/Docker boundaries в новых backend тестах подменять FakeClaude/FakeDocker/FakeRunner из существующих тестов. Offline runner tests запускаются с `-m 'not e2e'`; не запускать probe, evals или `docs/2.4/e2e-real-run.md`. Проверки с реальным PostgreSQL использовать только отдельную тестовую БД через существующий `TEST_POSTGRES_URL`, никогда production. Нужен PostgreSQL principal с CREATE DATABASE: fixture создаёт/удаляет отдельные базы.

### Зависимости

`1 schema → 2 policy mutation → 3 atomic admission → 4 ingress/runner integration`; `1 → 5 operational reads`; `1 → 6 harness diagnostics`; `2+5+6 → 7 client/polling`; `7 → 8 Overview`; `2+7 → 9 Users`; `4+8+9 → 10 compatibility/rollout/gates`. Task 6 можно выполнять параллельно 3–5, но запускать новую metadata projection только после совместимого backend receiver. Каждый RED/GREEN кейс ниже добавлять отдельно; не писать весь набор перед первой проверкой. Большой task — последовательность коротких шагов/кейсов, не один неделимый patch.

## Фиксированные контракты будущей реализации

### Storage и admission

Новые поля User: `ai_frozen: bool NOT NULL DEFAULT false`, `ai_policy_version: int NOT NULL DEFAULT 0 CHECK >=0`. Reason хранится **только** в новой таблице `ai_policy_audits`, не в обычном User DTO. Новые nullable поля Run: `admitted_at`, `admission_policy_version`, `harness_estimate` (JSON allowlist). Никакого backfill ложного admission/scope историческим Run.

Новый `AiPolicyAudit`: UUID id, actor_id/target_id FK users (RESTRICT, не каскадно стирать audit), server created_at, action `freeze|unfreeze`, private reason nullable только у unfreeze, before_version, after_version, idempotency_key (1–64), request_hash, response JSON allowlist. UNIQUE(actor_id,idempotency_key); CHECK action/version/reason. Политика, audit и replay response фиксируются одним commit. Audit не содержит отдельных дублирующих email-полей, prompts или секретов. Исключение — минимальный admin-only allowlist receipt `response`: он сохраняет email из уже возвращаемого AdminUserRow для точного идемпотентного replay; другие PII не добавляются. Отдельного публичного audit feed нет; admin-only `GET /api/admin/v1/users/{id}/audit` достаточен для минимального разбора, 25 записей/страница.

Новый service `backend/ai_access.py`:

- `change_ai_access(session, *, actor_id, target_id, frozen, reason, expected_version, idempotency_key) -> PolicyMutationResult` — flush-only, commit принадлежит API service.
- `admit_run(session, run_id) -> AdmissionResult`, где `state = admitted|already_admitted|terminal|denied`, `event: Event|None`; транзакционный owner — wrapper исполнения. Только `admitted` даёт право этому worker перейти к исполнению. `already_admitted` — не разрешение второму worker снова исполнить Run. Восстановление после crash — существующий reconcile/orphan flow, не автоматическое повторное исполнение.
- Блокировка: короткий `UPDATE users SET ai_policy_version=ai_policy_version WHERE id=:owner RETURNING ...` на обеих БД как serialization point (PostgreSQL row lock; SQLite write lock). Значения получать после блокировки, не из stale identity map. Затем Conversation → Run в том же порядке, что `_lock_run_context`; lock order User → Conversation → Run, audit insert последним. Freeze блокирует тот же target User; никакой process-local mutex вместо SQL. Не держать lock во время model/container/network I/O. SQLite busy/PG deadlock — bounded whole-transaction retry, максимум 3 попытки; затем нейтральный 503, не исполнение без проверки. Не переиспользовать session после failed transaction без rollback.
- Проверить owner по Conversation из БД, не по клиентскому User/role/session_id. Для нового исполнения без owner или без существующего User — fail closed `ai_policy_unavailable`; исторические orphan records остаются видимыми в reads/метриках.
- Под lock: terminal → terminal; уже admitted → already_admitted; pending+allowed → записать admitted_at + policy version + running/started_at + единственное run.started; frozen → pending становится failed с `error_code=ai_access_frozen`, completed_at, started_at остаётся null, единственное run.failed. При denied не трогать сохранённый conversation session/context и не вызывать model/container/transport. Использовать `finalize_run(..., commit=False)` или общий transaction helper, не вкладывать commit из старого mark_run_running.
- Договорная точка допуска — commit перед передачей работы executor. Transport `queued` после этого может означать **уже допущенный** Run; backend pending без admitted_at — никогда. Это не проверка на приёме HTTP и не повторный check на каждом model/tool call. Freeze после admission не отменяет queued доставку допущенной команды и внутренние retries/subcalls этого Run. Новый backend Run для answer/resume/rework всегда проходит новый admission, даже если transport.run_id и SDK session прежние.
- Ранний check в ingress даёт быстрый 403 до создания нового pending; он не заменяет атомарный check при исполнении. Replay уже существующего client key возвращает прежний Run/receipt, не стартует заново и не становится 403 только из-за более позднего freeze. Pending replay остаётся pending/denied по окончательному admission; он не выдаёт новое разрешение.

### HTTP API (все новые admin paths требуют AdminUser)

Новый prefix `/api/admin/v1`, не заменять существующие `/api/admin/overview` и `/api/admin/users` несовместимым DTO.

| Method/path | Вход → выход |
|---|---|
| GET `/overview` | `start`, `end` RFC3339 с timezone, start<end; default last 7×24h; `active_page`, `attention_page` >=1, `attention_start/end` независимо от cohort, `attention_no_start=false`, `page_size=25` (max100). Ответ `{as_of,period:{start,end},completeness:'complete',totals:{runs,failed,outcome_unknown,running},without_started_at,active:{items,total,page,page_size},attention:{items,total,page,page_size,period:{start,end},no_start}}` |
| GET `/runs` | `start/end` и `status=all|failed|outcome_unknown|running`, `page`, `page_size`; та же когорта для drilldown N/F/U/R; `{as_of,period,items,total,page,page_size,completeness}` |
| GET `/runs/{run_id}` | `{as_of,run:AdminRunRow,harness_estimate:HarnessEstimate}`; нет пользовательского workspace/content endpoint внутри response |
| GET `/users` | `q` email substring case-insensitive (trim, max254), `page=1`, `page_size=25` max100 → `{as_of,items,total,page,page_size}`; порядок created_at DESC,id DESC |
| GET `/users/{id}/audit` | `page=1,page_size=25` → `{as_of,items:[{id,actor_id,target_id,created_at,action,reason,before_version,after_version}],total,page,page_size}` |
| PATCH `/users/{id}/ai-access` | Header `Idempotency-Key` 1–64, JSON `{frozen:boolean,reason:string|null,expected_version:int>=0}` → 200 `{user:AdminUserRow,audit_id:string,changed_at:timestamp}`; точный replay возвращает сохранённое тело |

`AdminUserRow = {id,email,created_at,last_seen_at:null|timestamp,ai_access:{frozen,version}}`. Причина читается только через admin audit/форму просмотра, не добавлять её в общие `/api/auth/me`, history, product snapshots, WS events. Ordinary User DTO не менять без необходимости; эффективный доступ можно не проецировать до нужды UI.

`AdminRunRow = {id,conversation_id,project_id:null|id,task_label,user:null|{id,email},status:'pending'|'running'|'completed'|'failed'|'outcome_unknown',stage:null|{index,id:null|string,title,source:'scenario_catalog'|'product_stage'},started_at,completed_at,error_code:null|string,project_href:null|string}`. `task_label` — безопасный фиксированный «Проект <short-id>»/«Диалог <short-id>», не Conversation.title (выводится из prompt) и не problem/display_text. Короткий ID в списке, полный копируемый ID в деталях. project_href формировать только когда admin одновременно owner и есть поддерживаемый маршрут основного frontend; иначе null, особенно для чужого V2 workspace. error_code — серверная allowlist существующих публичных кодов и нового ai_access_frozen; неизвестный raw code → `unclassified`, не строка из Exception.

Ошибки новых endpoints: существующие auth 401/403 могут оставаться `{detail:string}`; domain errors `{code,detail}`. 404 `user_not_found|run_not_found`; 409 `policy_version_conflict` с `{current:{frozen,version}}` без автоматического retry, 409 `idempotency_conflict`; 403 `self_freeze_forbidden`, 503 `ai_policy_unavailable|policy_management_unavailable`; 422 `invalid_policy_request|invalid_period|invalid_pagination|invalid_idempotency_key`. Не возвращать Pydantic `input` с private reason при validation error: локально санитизировать ошибки новых mutation routes. frozen=true требует trim(reason) длиной 1..1000; unfreeze note null или <=1000. Same-state PATCH с новой key и верной version возвращает 409 `policy_state_conflict`, не добавляет audit/version; повтор того же key проверяется раньше expected_version. Hash привязан к actor+target+нормализованному телу; другой target/body с той же key — conflict. Другой admin с той же строкой key имеет независимое пространство ключей. Для нового подтверждения после 409 — обновить строку, сохранить введённую причину, новый key и явное повторное подтверждение.

Mutation — JSON only, same-origin browser protection: требовать `X-CoreTeams-Admin: 1`, не включать permissive CORS; если Origin присутствует, сверять с origin запроса, отклонять cross-origin до mutation; cross-site form/missing header — 403 `invalid_admin_origin`. Не доверять произвольным forwarded-host headers. Cookie SameSite=Lax сохраняется, auth sessions не менять. Клиент не логирует request/response тела mutation, не отправляет reason в analytics/localStorage.

### Метрики и снимки

Единый SQL statement для overview totals+списков+as_of (CTE + scalar JSON/aggregation с dialect-specific SQL только если нужна, либо один плоский typed результат и группировка в Python): важно получить **один database statement snapshot** при PostgreSQL READ COMMITTED, не несколько несогласованных SELECT в обычной транзакции. Не менять глобальную isolation/DB pooling. Все перечисления ограничены SQL LIMIT/OFFSET; totals считаются по всей выборке. Новый helper возвращает domain DTO, не SQLAlchemy Row. Для detail/users свой as_of; не обещать один вечный snapshot между страницами polling. Стабильные tie-breakers по Run.id/User.id; страницы живого списка могут меняться при новых данных, это отражено новым as_of.

Когорта COUNT DISTINCT Run.id, started_at >=start AND <end, без обязательного owner/Project join. F=failed, U=outcome_unknown, R=running в той же когорте; pending с timestamp входит только N. Active независимо от периода: running (started_at ASC NULLS LAST,id), затем pending (id). Attention по started_at собственного периода (default 7 суток), failed и unknown раздельно; сортировка completed_at DESC NULLS LAST,started_at DESC NULLS LAST,id. `attention_no_start=true` — отдельная выборка started_at IS NULL без временного фильтра. `without_started_at` — все Run без start, вне когорты. Нет coalesce(started_at,completed_at), inner join owner, подсчёта событий или accepted как completed.

Stage: legacy link без product_run_id — проверить точный ключ `Project.scenario` и валидный `ProjectRun.stage` в `stages_for`; не использовать fallback `scenario_of` для неизвестного сценария. V2 link — `ProductRun.stage_id` и `Project.product.stages`, индекс ProjectRun.stage должен указывать тот же stage ID; invalid/missing/mismatch → null. Не Project.stage и не runner progress.updated.stage. Каталог текущий, historical version не выдумывать; source указывает основание, не обещает восстановление удалённого плана.

### HarnessEstimate (новый DTO, не billing)

```text
{ state:'unknown'|'known', value_usd:null|decimal-string,
  scope:'unknown'|'session_total'|'run_delta', session_ref:null|string,
  segment_ref:null|string, observed_at:null|timestamp,
  source:{kind:'harness_result', field:'total_cost_usd',
          event_id:null|string,journal_seq:null|int,runner_version:null|string},
  baseline:null|{event_id:string,value_usd:decimal-string},
  continuity:'unverified'|'verified', unavailable_reason:null|string }
```

V1 default **unknown**, `scope_unconfirmed`; Run.cost_usd отдельно не достаточен. Для новых runner result можно подтвердить только session snapshot: непрозрачный session_ref, полученный на стороне runner через project-scoped UUID5 от действительно присутствующего result.session_id, timestamp/seq из Event, value из finite nonnegative total_cost_usd. `segment_ref=null`, continuity unverified, baseline null; явно «Накопительный снимок сессии; границы сегмента и resume baseline не проверены». Не утверждать полноту/deployment/model/provider или цену текущего Run. Отсутствует session_id → unknown. Старые senders/legacy CLI без metadata → unknown, даже если cost_usd=0. Информация о сбросе сама по себе не восстанавливается из падения числа.

Никакой production run_delta до доказанного segment/baseline: в V1 не генерировать этот вариант; UI/DTO reserve допустим только с проверкой обязательных baseline/segment/continuity=verified. Тест отклоняет forged run_delta. Session total 0 допустим лишь при явном нуле и доказанном session scope. Значения .015435 и .062745 показывать как два snapshot, **не** .078180; отрицательные/NaN/missing → unknown, не 0. Не выводить model_usage целиком, pricing alias не выдавать за upstream. Build/version передавать только реально доступную runner_version из существующего hello; иначе null, не guessed CLI version.

Совместимость metadata: добавить optional `harness_snapshot` в payload существующего usage.reported **после** receiver support; это строго allowlist `{session_ref,observed_at,journal_seq,value_usd}` без raw session_id/model/content. Для старого receiver sender должен сохранять старый shape: optional capability `harness_snapshot_v1` в welcome; старый runner игнорирует welcome additions, новый runner включает поле только при capability, иначе strips до прежних 8 полей. Не добавлять event types/новый WebSocket. Метаданные хранить в Run.harness_estimate при проекции; не включать их автоматически в публичные browser events (сохранять текущую redaction). Session_ref обозначает session total, не ledger и не upstream request ID.

---

### Task 1: Добавочная схема политики, audit, admission и estimate

**Dependencies:** нет.

**Files:**
- Modify: `backend/models.py:51–70,140–190` (User, Run); новые mapped class `AiPolicyAudit` рядом с User.
- Create: `backend/migrations/versions/20261112_0001_admin_ai_access.py` (new revision `20261112_0001`, down_revision `20261111_0005`).
- Test/Modify: `backend/tests/test_migrations.py:369–412` (migration_config/metadata compare), `backend/tests/test_migration_bridge.py` (существующий regression gate).

**Step 1 — RED fixture.** На временной SQLite upgrade до `20261111_0005`, seed существующего User и исторического completed Run с null start/cost; upgrade head. Assert default false/version0; nullable admission/estimate остались null, история не изменилась; новая audit таблица/unique/check constraints есть; duplicate actor/key запрещён; invalid version/reason rejected. Сравнить metadata после head, добавить isolated downgrade до `20261111_0005` и повторный upgrade на fixture.

**Step 2 — Verify RED.** `"$PY" -m pytest backend/tests/test_migrations.py -q` → FAIL: новых колонок/таблицы нет (не считать ImportError валидным RED).

**Step 3 — GREEN.** Добавить только перечисленную схему; named constraints, индексы `ai_policy_audits(target_id,created_at,id)` и `runs(started_at,id)` для когорты. Существующий индекс status/active-per-conversation сохранить. Check reason на audit не уводит note в публичную таблицу. Не менять старые revisions и не заполнять исторические цены/admission. Проверить Alembic SQLite batch where necessary; типы timezone/numeric/JSON по соседним моделям. Downgrade удаляет только новое, но запрещён как operational rollback при действующих freezes — task 10.

**Step 4 — Verify GREEN.** Та же команда → PASS; `"$PY" -m pytest backend/tests/test_migration_bridge.py -q` → PASS.

**Step 5 — Refactor/gates.** Убрать fixture duplication через существующий migration_config. `"$PY" -m pytest backend/tests/test_migrations.py backend/tests/test_migration_bridge.py -q` → PASS. Повторить metadata/default/constraints тесты с TEST_POSTGRES_URL; SQLite alone не доказывает PG DDL/locks. DDL plan review: короткий additive migration, table growth/lock budget оценить на staging clone, без production запуска в этой сессии.

### Task 2: Атомарный private freeze API и идемпотентный audit

**Dependencies:** 1.

**Files:**
- Create: `backend/ai_access.py` (new policy errors/result/change_ai_access).
- Modify: `backend/api/admin.py:14–36,344–348` (добавить отдельный v1 router/DTO handlers, старые symbols сохранить); `backend/main.py:1177–1188` (mount v1 router + domain error mapping); `backend/config.py:48–55` (new `admin_policy_management_enabled: bool = False`).
- Test/Modify: `backend/tests/test_admin_api.py:17–62` (grant admin fixture + existing endpoints).
- Create/Test: `backend/tests/test_ai_access.py` (new service/transaction tests).

**Step 1 — RED.** По образцу explicit role grant, не admin email. В mutation fixtures явно задавать `app.state.settings.admin_policy_management_enabled=True`; отдельный тест оставляет default false и ждёт503/no changes. Добавить параметризованные anonymous/user/admin tests для новых users/audit/PATCH. Первый patch freeze target version0 key K → frozen true/version1/один audit; exact K повтор → то же тело и audit_id, version не меняется. Другой body/target с K →409; другой admin stale version0 →409 и current; same-state fresh key →409. Обязательность reason, 1000/1001, whitespace, bool/int version, missing header/key, cross-origin; self-freeze403, unfreeze после чужой freeze разрешён; DB audit insert failure → no policy change.

**Step 2 — Verify RED.** `"$PY" -m pytest backend/tests/test_admin_api.py backend/tests/test_ai_access.py -q` → FAIL: новый endpoint 404/service отсутствует; разбить кейсы по одному до реализации.

**Step 3 — GREEN.** Реализовать storage/service и API по контракту выше. При management flag false PATCH 503 и не меняет состояние; GET доступен. Проверять actor role на каждом HTTP request, потом блокировать target; не блокировать actor дополнительно и не создавать обратный порядок двух User locks. Under lock повторно читать receipt K до expected_version, проверить target/body hash. Hash и ключ без reason в логах; успешный audit response сохраняет только allowlist. Явно commit в handler service; ошибки rollback; exception handler не печатает payload. Полный audit доступен только AdminUser. Ошибки/422 не возвращают private reason через validation echo. Обычные sign-in/me/project reads работают у frozen User.

**Step 4 — Verify GREEN.** Те же focused tests → PASS; SQL failure оставляет User.version0/no audit. `"$PY" -m pytest backend/tests/test_auth_api.py backend/tests/test_ownership_isolation.py -q` → PASS.

**Step 5 — Refactor/gates.** Константы code/message централизовать только внутри ai_access, не переделывать auth. Тесты caplog/HTTP JSON/public WS с sentinel reason/actor: содержимого причины нет вне admin audit. Два конкурентных admin с Barrier/Events дают один mutation и один version_conflict (real PG обязателен). `"$PY" -m pytest backend/tests/test_admin_api.py backend/tests/test_ai_access.py backend/tests/test_auth_api.py -q` → PASS.

### Task 3: Единственный атомарный admission и безопасный отказ pending

**Dependencies:** 1,2.

**Files:**
- Modify: `backend/ai_access.py` (new admit_run + errors); `backend/repository.py:484–499,553–575,577–698` (mark_run_running/_lock_run_context/finalize_run).
- Modify/Test: `backend/tests/test_ai_access.py`, `backend/tests/test_repository.py`.

**Step 1 — RED.** Async fixtures с двумя независимыми session и детерминированными Events, не sleep: freeze commit первым → pending denies, no start timestamp, один terminal event ai_access_frozen; admission commit первым → freeze не меняет running/admitted Run, finalize completed возможен. Два admit одного run → ровно один state admitted/run.started; остальные already_admitted. Missing owner → no execution; admitted record не используется для другого run. Failure перед commit → нет admission, повтор не исполняет без новой успешной транзакции. Restart с историческим running без admission не выдаёт новое разрешение молча: существующий reconcile переводит outcome_unknown.

**Step 2 — Verify RED.** `"$PY" -m pytest backend/tests/test_ai_access.py backend/tests/test_repository.py -q` → FAIL на missing admission/выполнении frozen pending.

**Step 3 — GREEN.** Реализовать сериализацию/lock order/AdmissionResult выше. Не использовать mark_run_running(None) как право исполнения. Старый helper заменить совместимым wrapper над admission либо убрать все production callers в task4; отдельно tests that used ownerless Run fixtures обновить реальным User, не ослаблять проверку. У finalize denied no session/context invalidation; no new status blocked/cancelled. Исполнение начинается только после successful admission commit. Run admission и единственное started event атомарны.

**Step 4 — Verify GREEN.** Та же команда → PASS на SQLite; с TEST_POSTGRES_URL повтор → PASS и доказанная блокировка между соединениями. Test проверяет winner по commit order, не по wallclock начала coroutine.

**Step 5 — Refactor/gates.** Повторить races 20 раз параметризацией, без pytest-repeat dependency. Проверить lock waits/deadlock timeout и bounded retry/503; не включать indefinite retry. `"$PY" -m pytest backend/tests/test_repository.py backend/tests/test_repository_confirmation_recovery.py backend/tests/test_ai_access.py -q` → PASS. Не удерживать User lock в finalize уже допущенной работы, кроме коротких admission/mutation transitions.

### Task 4: Enforcement во всех ingress, CLI, runner и continuation

**Dependencies:** 3. Без этого task mutation flag никогда не включать.

**Files:**
- Modify: `backend/main.py:269–379,491–526,853–1090` (handle_run_create, execute_accepted_run, dispatch_websocket_message, dispatch_project_run).
- Modify: `backend/api/product.py:161–230,341–421,549–594` (start, create_run, answer).
- Modify: `backend/runner_executor.py:63–79,157–188,221–244` (run_via_runner).
- Test/Modify: `backend/tests/test_product_api_reliability.py:24–99,134–176`, `backend/tests/test_runner_executor.py:26–89,513–596,687–729`, `backend/tests/test_runner_app.py:97+`, `backend/tests/test_history_api.py`.
- Create/Test: `backend/tests/test_ai_access_ingress.py` (new end-to-end fake executor matrix).

**Step 1 — RED.** Через HTTP/WS public contracts параметризовать raw text, run.create new/resume/reset, project.run stage/revision/answer, V2 initial/rework/answers. Заморозить User, сделать новый запрос: ранний HTTP403 `{code:'ai_access_frozen',detail:neutral}` либо WS command.error code; FakeClaude.prompts пуст, FakeRunner.received пуст, no newly dispatched transport message/answer. Подготовить pending, удержать worker до admission, freeze и отпустить → run.failed ai_access_frozen, started_at null. Повторить те же ключи и changed-body keys; сохраняются существующие replay200/201/409 semantics и ownership.

**Step 2 — Verify RED.** `"$PY" -m pytest backend/tests/test_ai_access_ingress.py backend/tests/test_product_api_reliability.py backend/tests/test_runner_executor.py -q` → FAIL: fake model/transport получает frozen запрос.

**Step 3 — GREEN (короткие изменения по одному ingress).**
1. Общий `execute_accepted_run` вызывает admission до ветвления executor; state denied доставляет сохранённый terminal event и возвращает, already_admitted/terminal возвращает без исполнения. run_via_runner — доверенный внутренний adapter, принимает **результат допуска конкретного acceptance.run_id** из wrapper; прямой вызов без verified admission запрещён тестом. Не разрешать caller-controlled bool bypass.
2. Удалить повторный mark_run_running из CLI/runner: start timestamp/admission уже committed wrapper. CLI workspace/model I/O и runner ensure_running/message/answer только после допуска. Внутренние model/tool calls/recovery одного допущенного Run не проверять freeze повторно.
3. Ранний policy check после определения ownership/replay, до нового accept/mutation продукта: HTTP ProductError403, WS neutral error. При позднем deny после pending приёма HTTP receipt остаётся корректным: последующий GET/WS terminal failed с ai_access_frozen, не обещать синхронный403 после уже возвращённого201.
4. Существующие start_run_task slot reservations/release, ready future cancellation и atomic ProjectRun link commit сохранить. Не переносить admission перед ready.set_result/commit связей. Сохранить client_request_id/body fingerprints.
5. Прямые фоновые/отложенные вызовы execute_accepted_run также защищены. Не добавлять проверку User к каждому transport internal command: answer нового backend Run проверен до transport.answer, а retries/confirm_saved допущенного Run должны завершаться. Transport SQL/API не выставлять публично; project-bound token проверки не ослаблять.

**Step 4 — Verify GREEN.** Focused команда → PASS. Дополнительно explicit tests: допущенный Run с двумя внутренними calls, freeze между ними → оба разрешены, completed; пользовательский answer после question → новый backend Run, denied даже при прежнем transport id; unfreeze → новый key допускается. Duplicate WS доставки/start worker/reconnect → ровно одно исполнение, один started event. Freeze не отзывает cookie и не закрывает проектные reads/auth; нет admin retry/cancel.

**Step 5 — Refactor/gates.** `"$PY" -m pytest backend/tests/test_ai_access_ingress.py backend/tests/test_runner_app.py backend/tests/test_runner_transport.py backend/tests/test_runner_executor.py backend/tests/test_product_api.py backend/tests/test_product_api_reliability.py backend/tests/test_history_api.py -q` → PASS. Повторить race matrix на реальном PG. Поиск `rg -n 'start_claude\(|run_via_runner\(|transport\.(message|answer)\(|mark_run_running\(' backend` — каждый production execution caller объяснён; нет обхода через legacy или background. Сам поиск не заменяет тесты.

### Task 5: Оперативный overview, когортные drilldowns и безопасные детали

**Dependencies:** 1. DTO estimate пока unknown; task6 добавит metadata.

**Files:**
- Modify: `backend/api/admin.py:36–102,263–348` (new v1 operational DTO/routes, старые оставить).
- Create: `backend/admin_queries.py` (new bounded queries/snapshot DTO conversion).
- Test/Modify: `backend/tests/test_admin_api.py:83–156` (старые regression assertions сохранить).
- Create/Test: `backend/tests/test_admin_operations.py`.
- Read/reference: `backend/scenarios.py:156–163`, `backend/product.py:279–328`, `backend/projects.py:110–158` — не использовать их product statuses/поле Project.stage вместо Run.

**Step 1 — RED.** Фиксированный UTC S/E, fixtures Run в S−1us,S,E−1us,E; completed,failed,unknown,running,pending; owner null; no-start с completed_at внутри периода; один Run с несколькими Event/Artifact; Project.stage изменён после Run. Assert N/F/U/R, [S,E), K отдельно; running старше периода присутствует active; pending не выдаётся running; attention собственный период/no-start filter. Страница size2 не меняет totals. У failed no completed_at нейтральный timestamp placeholder. Stage tests legacy/V2/mismatch/missing catalogue/invalid index. В деталях sentinel prompts/Run.error/secret path/Conversation.title не встречаются.

**Step 2 — Verify RED.** `"$PY" -m pytest backend/tests/test_admin_operations.py -q` → FAIL: v1 routes404/нет точных когорт.

**Step 3 — GREEN.** SQL ограниченный выборками, DISTINCT Run PK до joins; единый statement snapshot overview, тот же predicate builder в `/runs`. LIMIT/OFFSET на сервере; total не len(page). Unknown raw error_code маппить allowlist → unclassified. Отсутствие подтверждённого stage → null. Detail 404 для несуществующего ID, 401/403 до чтения деталей, admin не получает новое ownership. Не возвращать Project.name/problem или raw errors для удобного task label. Все ответы `Cache-Control: no-store`.

**Step 4 — Verify GREEN.** Та же команда → PASS на SQLite и PG. Добавить concurrent status update между попытками чтения: в одном overview response F/U/R и N/list metadata принадлежат одному statement snapshot, не разные now. Malformed timezone/start>=end/pagination →422, не silently clamp; no-owner сохраняется в count/detail. Users query tests: 26+ users, same created_at tie-break id, case-insensitive q, literal `%`/`_` escaped как substring, empty q и no results; SQL limit присутствует, не загружается вся таблица.

**Step 5 — Refactor/gates.** `"$PY" -m pytest backend/tests/test_admin_api.py backend/tests/test_admin_operations.py backend/tests/test_ownership_isolation.py -q` → PASS. EXPLAIN на синтетической большой test DB проверяет bounded pages и индексы; нельзя обещать runtime latency без измерения. F/U/R drilldowns дают тот же набор/total, что счётчик соответствующего response snapshot (новый запрос может иметь новый as_of).

### Task 6: Traceable harness snapshot без денежной агрегации

**TECHNICALLY APPROVED for implementation, 2026-11-12 (не rollout):** [harness metadata contract revision](2026-11-12-harness-metadata-contract-revision.md). Независимое повторное review закрыло оба blockers; runtime matrix обязательна. Optional-enrichment ниже и в §HarnessEstimate — superseded history, не инструкция исполнения.

**Revised dependencies:** 1; Task5 detail DTO; independent review receipt/segment boundaries. Следующие шаги исполнять только после review; сейчас только план.

**Revised files:** Modify `runner/src/ctrunner/gateway.py` (`parse_welcome`, `_connection`, `_receive`: capability/private retry); Create `runner/src/ctrunner/harness_diagnostic.py` (metadata-only Decimal parsing + unchanged Translator replay; legacy `v0.py` не менять). Modify `backend/runner_app.py` (`handle`, `_receive`: private dispatch); `backend/runner_transport.py` (`__init__`, `message`, `answer`, `ingest`: additive private tables/binding); `backend/runner_executor.py` (`run_via_runner`: trusted segment registration/catch-up); `backend/harness_estimate.py` (persisted lineage/order), `backend/api/admin.py` (detail catch-up only). Tests: existing paths ниже. No PostgreSQL migration.

**Revised Step 1 — RED transport/compatibility.** В `backend/tests/test_runner_transport.py`, `backend/tests/test_runner_app.py`, `runner/tests/test_gateway.py`, `runner/tests/test_gateway_client.py` добавить по одному кейсу: unsupported capability → unchanged bytes; new private dispatch missing; lost ACK exact duplicate/conflict; source absent; terminal accepted-source receipt; multi-draft crash; persisted pending segment for answer; enqueue→user.answer.accepted gap usage → ambiguous unknown; source below cursor. Run `PYTHONPATH=runner/src:runner "$PY" -m pytest runner/tests/test_gateway.py runner/tests/test_gateway_client.py backend/tests/test_runner_transport.py backend/tests/test_runner_app.py -q` → новые assertions FAIL из-за отсутствующих private receipt/capability, существующие legacy PASS.

**Revised Step 2 — GREEN storage/receiver.** Additive CREATE TABLE IF NOT EXISTS для diagnostic_segments/sources/receipts по revision; register pending segment в той же SQLite transaction, что enqueue message/answer, trusted backend_run optional для legacy callers; answer binding активировать только после accepted user.answer.accepted, gap usage сохраняет ambiguous/unknown без последующего guessed reassignment; при ingest usage записывать source seq/public seq/immutable command binding без изменения existing canonical comparison/cursor/seq. Welcome capability включать только при готовом store и projection path; diagnostic ingest/ACK — отдельный метод, не public `_append`. ACK только после receipt commit; Run projection позже. Та же команда → PASS. Reopen transport между commit/ACK; existing DB сохраняет commands/events/cursor byte-identically.

**Revised Step 3 — RED/GREEN sender.** В `runner/tests/test_v0_usage.py`, `runner/tests/test_gateway_client.py` golden bytes/IDs для старого usage, decimal canonical strings, same-row drafts, reconnect capability toggle, source below cursor, bounded retry until private ACK; permanent terminal source_not_accepted с fair round-robin не мешает доставить второй valid diagnostic (никакого accepted ACK для missing source). Run `PYTHONPATH=runner/src:runner "$PY" -m pytest runner/tests/test_v0_usage.py runner/tests/test_gateway_client.py -q` → FAIL нового metadata pass, затем PASS после отдельного diagnostic builder/retry task; legacy translator и JournalFeed unchanged. Fixed namespaces и compact JSON-array encoding зафиксировать golden vectors. Не использовать float arithmetic или session_marker.

**Revised Step 4 — RED/GREEN projection.** В `backend/tests/test_runner_executor.py`, `backend/tests/test_admin_operations.py` покрыть answer/new backend Run binding, delayed diagnostic после terminal, receipt committed/projection crash+detail recovery, out-of-order/session change, unknown prefeature sources и no sum/no double count. Run `PYTHONPATH=runner/src:runner "$PY" -m pytest backend/tests/test_runner_executor.py backend/tests/test_admin_operations.py -q` → FAIL отсутствующего catch-up, затем PASS. Реализовать detail/finalize catch-up с SQLite receipt чтением без удержания transport transaction; backend wrapper `{storage_schema:"harness_estimate.v1",estimate:{...DTO},diagnostic_id,lineage,source_order}` без top-level scope/state/value_usd/source/session_ref. Новый mapper распаковывает известную версию; pinned old safe_estimate fixture на wrapper → None и old admin unknown. Extra поля flat DTO не являются rollback guard. Atomic update: target Run row lock с общим порядком User→Conversation→Run где применимо, fresh persisted JSON SELECT после lock, same-lineage order comparison/write внутри backend transaction; SQLite BEGIN IMMEDIATE + bounded whole-transaction busy retry, не неэффективный FOR UPDATE. Stale ORM запрещён. Equal order конфликтующего ID/body → conflict, incomparable lineage → unknown/no overwrite. Не обещать cross-DB atomicity. Deterministic test `test_harness_projection_monotonic_two_sessions` в `backend/tests/test_admin_operations.py`: A captures S1, B commits S2, release A → final S2, не S1; SQLite uses serialized write comparison, PostgreSQL row-lock path отдельно. Run `PYTHONPATH=runner/src:runner "$PY" -m pytest backend/tests/test_admin_operations.py -k harness_projection_monotonic_two_sessions -q` → RED на stale overwrite, GREEN после atomic guard. Для будущего PG gate выполнить ту же команду с уже предоставленным isolated PG test DSN по существующему fixture; SQLite PASS не доказывает PG path, без PG gate остаётся unverified. Сейчас PG не создавать и не запускать.

**Revised Step 5 — Migration/rollback acceptance.** В существующих `backend/tests/test_runner_transport.py`/`test_runner_app.py`, `runner/tests/test_gateway_client.py` добавить upgrade existing SQLite/reopen, receiver rollback без удаления tables, pinned actual old sender replay после new sender/lost ACK. Старый sender импортировать/запускать из изолированного pinned HEAD checkout до изменений только в transport fake fixture, без model entrypoint; не заменять флагом нового sender. Receiver capability отключить прежде sender rollout; rollback: отключить sender feature/capability, сохранить receipts/bindings, никаких destructive downgrades/backfill guesses. В `backend/tests/test_admin_operations.py` добавить `test_harness_wrapper_pinned_old_mapper_unknown`: actual pinned pre-change safe_estimate(wrapper) → None и старый detail unknown, новый mapper → known session_total; flat payload с extra lineage использовать как regression counterexample, а не обещать unknown. Run `PYTHONPATH=runner/src:runner "$PY" -m pytest backend/tests/test_admin_operations.py -k harness_wrapper_pinned_old_mapper_unknown -q` → RED до wrapper, GREEN после. В `runner/tests/test_gateway_client.py` `test_harness_pinned_old_sender_lost_ack_capability_toggles`: new sends/receipt commits/ACK lost → actual pinned old sender replay → new reconnect capability off/on; assert exact legacy bytes/IDs, single diagnostic receipt и unchanged legacy cursor semantics. Run `PYTHONPATH=runner/src:runner "$PY" -m pytest runner/tests/test_gateway_client.py -k harness_pinned_old_sender_lost_ack_capability_toggles -q` → PASS; отсутствующая pinned fixture — blocker, не skip. Команда Step1 → PASS.

**Explicit gap/fairness acceptance:** `backend/tests/test_runner_transport.py::test_harness_answer_enqueue_acceptance_gap_unknown` — enqueue answer, ingest usage до user.answer.accepted, затем accepted и valid usage: первый остаётся unknown/unbound, второй связан только с новым backend Run. `runner/tests/test_gateway_client.py::test_harness_terminal_missing_source_does_not_starve` — permanent missing terminal source плюс valid source: valid получает receipt за bounded retry rounds, missing никогда не получает accepted ACK. Run `PYTHONPATH=runner/src:runner "$PY" -m pytest backend/tests/test_runner_transport.py::test_harness_answer_enqueue_acceptance_gap_unknown runner/tests/test_gateway_client.py::test_harness_terminal_missing_source_does_not_starve -q` → RED до pending-binding/fair scheduling, GREEN после; никаких runtime вызовов сейчас.

**Revised Step 6 — Gates/docs.** `PYTHONPATH=runner/src:runner "$PY" -m pytest runner/tests -m 'not e2e' -q` и `PYTHONPATH=runner/src:runner "$PY" -m pytest backend/tests/test_runner_transport.py backend/tests/test_runner_app.py backend/tests/test_runner_executor.py backend/tests/test_public_events.py backend/tests/test_admin_operations.py -q` → PASS; `git diff --check` → PASS. Обновить private envelope/receipt описание в `docs/2.4/protocol.md`, `docs/2.4/gateway-receiver.md`; legacy public schema не расширять. Перед code changes применить language/DB routing и security/reliability gates по доступности; полный аудит не подменять заметками. No model/credentials required; PG row-lock concurrency gate future-only на предоставленном isolated fixture, без него явно unverified (не создавать PG в текущей ревизии).

#### Superseded Task6 history — не исполнять

**Dependencies (historical):** 1; task5 detail DTO.

**Files:**
- Modify: `runner/src/ctrunner/v0.py:140–146,202,440–453` (new safe optional snapshot builder); `runner/src/ctrunner/gateway.py:45–51,54–64,239–274` (capability parsing/strip).
- Modify: `backend/runner_app.py:78–95` (welcome capability); `backend/runner_transport.py:24–39,93–96,149–152` (optional nested allowlist); `backend/runner_executor.py:142–154,202–203` (persist sanitized snapshot on current backend Run); `backend/api/admin.py` (detail estimate mapper).
- Modify/Test: `runner/tests/test_v0_usage.py:33–106`, `runner/tests/test_gateway.py`, `runner/tests/test_gateway_client.py`, `backend/tests/test_runner_transport.py`, `backend/tests/test_runner_executor.py`, `backend/tests/test_admin_operations.py`.
- Modify: `docs/contracts/events-v0.md:48,63,80,94` — убрать рекомендацию складывать result costs и описать compatibility/scope.

**Step 1 — RED.** Синтетические metadata-only result .015435/.062745 одной session, без session, cost0/null/NaN/negative, is_error с нулевым usage, reset/discontinuous totals. Assert legacy payload shape без capability; negotiated payload содержит только optional snapshot; повтор одного event_id не дублирует snapshot. Detail .062745 scope=session_total, baseline null/continuity unverified, не .078180 и не run_delta. Старый scalar Run.cost_usd → unknown. Scope metadata absent/invalid → unknown, не 0. Private raw session_id/result/model_usage/alias не выходит в public event/DTO.

**Step 2 — Verify RED.** `"$PY" -m pytest runner/tests/test_v0_usage.py runner/tests/test_gateway.py runner/tests/test_gateway_client.py backend/tests/test_runner_transport.py backend/tests/test_admin_operations.py -q` → FAIL на отсутствующей safe metadata/capability.

**Step 3 — GREEN.** Получать source seq/ts из действительно существующего journal Event на result feed; session_ref детерминирован project-scoped opaque id, не `session_marker(project)` (это не реальный SDK session). `value_usd` Decimal string через finite validation; только JSON-safe allowlist. Передавать runner_version из hello при наличии, не добавлять выдуманный cli_version. Optional nested validator не должен требовать поле у старого sender и не должен принимать посторонние поля/boolean money. Projection фиксирует latest observed snapshot, не суммирует и не использует max через resets; хранит event_id для idempotency, не перетирает новым replay старого seq. При answer projection относится к текущему backend Run, хотя transport Run прежний; не приписывать весь session total этому Run как его цену. Legacy CLI оставить unknown, не расширять raw parser/public result ради недоказанной attribution. Browser public events по-прежнему redacted; admin detail — единственное место новой оценки.

**Step 4 — Verify GREEN.** Та же команда → PASS. `"$PY" -m pytest backend/tests/test_runner_app.py backend/tests/test_runner_executor.py backend/tests/test_public_events.py -q` → PASS. Compatibility matrix old sender/new receiver, new sender/old welcome, new/new; protocol violations/private metadata rejected. Existing v0 assertions обновить только для negotiated optional field, не ослаблять общую allowlist.

**Step 5 — Refactor/gates.** `"$PY" -m pytest runner/tests -m 'not e2e' -q` → PASS; `"$PY" -m pytest backend/tests/test_runner_transport.py backend/tests/test_runner_executor.py backend/tests/test_public_events.py backend/tests/test_admin_operations.py -q` → PASS. Документ указывает client-side harness estimate, не actual spend/API equivalent; resume baseline остаётся unverified. Нет чтения runtime journals или нового model probe для «закрытия» пробела.

### Task 7: Typed admin client и polling state machine

**Dependencies:** 2,5,6 (контракт), implementation можно начать с fake responses.

**Files:**
- Create: `frontend/src/coreteams/adminApi.ts`, `frontend/src/coreteams/adminPolling.ts`, `frontend/src/coreteams/adminApi.test.ts`, `frontend/src/coreteams/adminPolling.test.ts`.
- Read/reference: `frontend/src/auth.ts:11–35`, `frontend/src/coreteams/api.ts:103–127` — apiFetch already throws UnauthorizedError on401; не ожидать Response401 после него.

**Step 1 — RED.** Vitest fake timers + injected fetch/visibility/clock: initial loading без нулей; success fresh as_of; 15s visible poll; не более одного in-flight; pause hidden/refresh foreground; failure сохраняет прежний snapshot, last success не двигается; >45s stale; retry bounded до60s; manual refresh; old q/page/period response не побеждает новый. UnauthorizedError и HTTP403 очищают все admin snapshots/selected details/form reason и останавливают таймеры. Mutation error сохраняет открытую reason без logging.

**Step 2 — Verify RED.** `npm --prefix frontend test -- src/coreteams/adminApi.test.ts src/coreteams/adminPolling.test.ts` → FAIL на отсутствующих модулях/состояниях.

**Step 3 — GREEN.** New exported DTO types ровно из backend contract, decimal USD string не coercion null→0. `adminApi.overview/runs/run/users/audit/changeAiAccess`, AbortSignal и encoded query. Структурированная AdminApiError(status,code,current?) плюс UnauthorizedError; patch добавляет JSON + Idempotency-Key + X-CoreTeams-Admin, retry key хранится только в открытой форме. Controller state `loading|ready|error|unauthorized|forbidden` + stale/refreshError/lastSuccess, generation token/AbortController; timer после завершения, не overlapping setInterval; backoff15/30/60 cap. На фильтр abort предыдущий, на unmount dispose; private caches memory-only. as_of серверный, stale считается по возрасту последнего успешного ответа с учётом server clock anchor, не по времени последней попытки.

**Step 4 — Verify GREEN.** Та же команда → PASS; добавить 401 во время открытого reason modal/в полёте detail и late success после403: данные не возвращаются. Polling не меняет modal draft/key/фокус. Очистка reasons на auth loss не противоречит сохранению при обычной mutation network error.

**Step 5 — Refactor/gates.** `npm --prefix frontend test` и `npm --prefix frontend run build` → PASS. Не добавлять query library/новый WS. Общий apiFetch не ломать ради admin-specific errors.

### Task 8: Overview и read-only детали в одном Shell

**Dependencies:** 7.

**Files:**
- Modify: `frontend/src/coreteams/router.ts:4–29`, `Shell.tsx:8–14,71–99`, `coreteams-extra.css` (добавить scoped responsive стили).
- Create: `frontend/src/coreteams/AdminOverview.tsx`, `frontend/src/coreteams/AdminRunDetails.tsx`, `frontend/src/coreteams/adminView.ts`, `frontend/src/coreteams/adminView.test.ts`, `frontend/src/coreteams/adminRender.test.tsx`.
- Modify/Test: `frontend/src/coreteams/coreteams.test.ts:94–105`.

**Step 1 — RED.** Route roundtrip `#admin/overview`, `#admin/users`, `#admin` alias→overview, обычные project/chat routes прежние. Pure view-model tests: completed !=accepted, pending !=running, failed !=unknown, missing stage/time/user placeholders, no-start separate, unknown cost без $0, session-total label без Run price. Через `react-dom/server` (уже dependency) static markup tests проверяют один header/nav, H1, таблицы/подписи/отсутствие запрещённых кнопок; для focus/keyboard нужен browser acceptance task10, SSR не объявлять DOM integration.

**Step 2 — Verify RED.** `npm --prefix frontend test -- src/coreteams/coreteams.test.ts src/coreteams/adminView.test.ts src/coreteams/adminRender.test.tsx` → FAIL на routes/components.

**Step 3 — GREEN.** Route union new `{page:'admin';section:'overview'|'users'}`; routeHash специальный admin case **до** общего non-project return. Shell admin page не зависит от успешного scenario catalogue fetch: общий header/theme/account сохраняется, role!=admin → недостаточно прав/no admin API requests. Nav href `#admin/overview`. Overview order active→attention→totals, 25 rows/page, независимые attention/cohort filters, UTC interval fixed до выбора/«Обновить период»; локальная timezone подписана. Totals links открывают bounded `/runs` selection той же когорты/status, не перегружают attention своим периодом. Active не скрывается cohort. Detail panel readonly, allowed context IDs/error explanations, копирование Run ID, переход Users q=email; workspace href только backend non-null. Unknown message требует проверки результатов, без retry/cancel. Estimate — строго «Оценка расхода по харнессу, USD» + не списание/остаток/API equivalent + scope/observed/source; unknown с причиной. Никаких сумм.

**Step 4 — Verify GREEN.** Команда RED → PASS. Добавить tests fixed cohort period сохраняется при poll, custom UTC conversion DST spring gap/fall offset из явно выбранного offset, end exclusive; пользовательский ambiguous local time не угадывать: ввод datetime с явным offset/zone и показываемые UTC bounds. Empty/error/loading/partial/stale рендерить раздельно; full successful empty единственный источник «нет…»/0. Backend all-or-error overview не создаёт partial из обрезанного page; independent detail/users failure маркируется своим блоком.

**Step 5 — Refactor/gates.** `npm --prefix frontend test` и `npm --prefix frontend run build` → PASS. Текстовые статусы и семантические tables/cards, overflow wrap для email/ID, mobile layout без horizontal clipping, light/dark/forced-colors. Собственный header внутри admin components запрещён. Не подключать всю старую AdminPanel/engagement charts.

### Task 9: Users, подтверждения и version conflict UX

**Dependencies:** 2,7; общий admin nav из8.

**Files:**
- Create: `frontend/src/coreteams/AdminUsers.tsx`, `frontend/src/coreteams/adminUsersState.ts`, `frontend/src/coreteams/adminUsersState.test.ts`.
- Modify: `frontend/src/coreteams/Shell.tsx:76–82`, `frontend/src/coreteams/adminRender.test.tsx`, `frontend/src/coreteams/coreteams-extra.css`.
- Read/reference: `frontend/src/coreteams/ui.tsx:228–288` (`Modal` использует native dialog/showModal, Escape и возврат focus; fixed id="dialog" означает, что нельзя монтировать второй экземпляр одновременно). Использовать единственный Shell modal с вынесенным admin draft state либо отдельный dialog с уникальными IDs, не дублировать fixed IDs.

**Step 1 — RED.** Reducer/controller tests: q debounce300ms, поиск сбрасывает page1; ответы page1 старого q не заменяют новый q; same created_at order preserved сервером. Freeze confirm с empty/>1000 reason disabled; network retry сохраняет reason и key, double submit→один key;409 version обновляет строку, сохраняет draft и требует нового explicit confirm с новым key; no optimistic success. Unfreeze separate confirm/note; self freeze hidden/disabled, но API403 всё равно обрабатывается. Polling не закрывает modal/draft.

**Step 2 — Verify RED.** `npm --prefix frontend test -- src/coreteams/adminUsersState.test.ts src/coreteams/adminRender.test.tsx` → FAIL отсутствует UI/state.

**Step 3 — GREEN.** Колонки email/created_at/last_seen_at/AI-доступ/action; null last seen→«Нет данных». Freeze modal сообщает бессрочность, ручное снятие и завершение уже допущенного Run с внутренними calls. Reason private и не просит секретов; audit view admin-only по отдельному раскрытию (не reason в строке обычного пользователя). API успех обновляет строку/version, не по нажатию кнопки. Не показывать «Аккаунт заблокирован»/дату разморозки. При conflict сохранять причину только в открытой памяти; explicit re-confirm обязательно. При401/403 очистить данные/modal reason и stop polling.

**Step 4 — Verify GREEN.** Та же команда → PASS, plus backend route-shaped fixtures точных 200/409/422/403 responses. Role loss late success игнорируется. Frozen admin остаётся способен входить/снимать freeze; не трактовать freeze как revoke роли.

**Step 5 — Refactor/gates.** `npm --prefix frontend test`, `npm --prefix frontend run build`, `npm --prefix frontend run lint` → PASS либо зафиксированный до изменения baseline lint отдельно; не маскировать новые warnings. Keyboard Escape/focus return/inert background проверить на browser acceptance, не выдавать reducer tests за a11y audit.

### Task 10: Совместимость, обратимость и release acceptance без обходов

**Dependencies:** 4,8,9; real PG race tests обязательны перед enable mutation.

**Files:**
- Modify: `web-product/src/main.tsx:1–36`, `web-product/src/App.tsx:846–847` (старые admin entry/link); `frontend/src/coreteams/Shell.tsx:94–99` (new route flag), `backend/config.py` (management flag из2).
- Create: `web-product/src/adminRedirect.ts`, `web-product/test/admin-redirect.test.ts` (new pure helper/compat tests).
- Modify: `frontend/src/coreteams/adminRender.test.tsx`, `backend/tests/test_ai_access_ingress.py`, `backend/tests/test_admin_api.py`.
- Modify: `docs/contracts/events-v0.md` (task6), `docs/research/2026-11-12-admin-redesign.md` (только фактические результаты будущих проверок, не заранее зелёный отчёт).

**Step 1 — RED.** Pure redirect helper maps `/product/#admin` → `/#admin/overview`, не трогает остальные product hashes, не переносит arbitrary query/return URL. Frontend flag `VITE_ADMIN_SHELL_ENABLED` false → безопасная read-only unavailable fallback в основном Shell, без возврата старого управления; true → две новые страницы. Test старые GET `/api/admin/overview?days=7` и `/users` сохраняют shape/защиту во время миграции; новые admin страницы не обращаются к legacy engagement API и не отображают его cost totals. Flag off/backend management disabled **не** отключает admit_run для frozen users.

**Step 2 — Verify RED.** `npm --prefix frontend test` и `pnpm --dir web-product test` → FAIL на отсутствующем redirect/fallback; `"$PY" -m pytest backend/tests/test_ai_access_ingress.py -q` → FAIL если flag обходит enforcement.

**Step 3 — GREEN.** В web-product убрать активный render AdminPanel из Root, заменить `location.replace` на main route независимо от старой role (данные защищает main/backend). Удалить неиспользуемые imports admin.css/AdminPanel из entry, но не переустраивать остальные страницы. Legacy AdminPanel/API read код временно остаются для backward compatibility, не становятся fallback интерфейсом управления. Новые shell components/route flag не меняют chat/product behavior. Старые денежные DTO сохранены исключительно как deprecated wire compatibility; V1 UI их не потребляет, новые суммирующие endpoints не добавляются. Для физического удаления legacy contracts нужен отдельный consumer inventory/миграция; это не оправдание показывать их KPI в новой админке.

**Step 4 — Verify GREEN и совместимый порядок будущего выпуска (не deploy-команды).**
1. Additive schema; совместимый receiver optional metadata; policy enforcement на **всех** backend ingress/executors при management flag=false. Перед rolling update старого worker management остаётся off, новых freezes нет.
2. Убедиться, что не осталось pre-enforcement workers; drain уже допущенной работы штатно, миграция не делает её cancelled. Проверить pending/restart reconciliation. Подключить новый sender capability; старые runner payloads ещё работают, estimates unknown.
3. Включить новый admin UI route; старый URL redirects. Только после полной race/security acceptance разрешить operator включить `admin_policy_management_enabled`. В данном поручении разрешения на изменение deployment нет.
4. UI rollback: route flag false/read-only unavailable или последняя enforcement-compatible версия UI. Management flag false закрывает новые admin mutations, **сохраняет** текущие freezes и audit/admission enforcement. Не запускать старую AdminPanel как альтернативное управление.
5. Backend rollback только на enforcement-compatible release. Если его нет, остановить приём **новых** AI starts (нейтральный503) для всех users на небезопасном ingress; уже admitted Run и его internal calls должны завершиться. Не откатывать к pre-enforcement backend/runner path, не обнулять freezes. DB downgrade после enable freeze запрещён; schema additive можно оставить. Destructive downgrade допустим только на isolated fixture либо отдельной согласованной maintenance procedure после переноса политики, не здесь.

**Step 5 — Refactor и итоговые gates.** Из repo root с подготовленной средой:

```sh
PY=/tmp/coreteams-admin-plan-venv/bin/python
"$PY" -m pytest backend/tests -q
"$PY" -m pytest runner/tests -m 'not e2e' -q
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run lint
pnpm --dir web-product test
pnpm --dir web-product typecheck
pnpm --dir web-product build
git diff --check
```

Expected: PASS; исходные unrelated baseline failures отдельно воспроизвести, не исправлять попутно. Не запускать docker compose/app/probe ради этих команд. CI scripts проверены в manifests; локальный результат всех gates до исполнения **unverified**.

На заранее предоставленном **тестовом** PostgreSQL, с TEST_POSTGRES_URL в environment (не печатать URL/пароль):

```sh
"$PY" -m pytest backend/tests/test_migrations.py backend/tests/test_ai_access.py backend/tests/test_ai_access_ingress.py backend/tests/test_admin_api.py backend/tests/test_admin_operations.py backend/tests/test_product_api_reliability.py -q
```

Expected: PASS, без пропусков PG-specific concurrency assertions. Если test DB недоступна, freeze mutation enable блокирован; нельзя заменить этот gate SQLite или doc review.

### Acceptance checklist будущего исполнения

- [ ] Both orders freeze/admission; отдельные соединения PG; queued pending denied; admitted run completes all model/tool/retry/subcalls; duplicates не исполняются дважды; unfreeze позволяет новый admission; новый answer/resume/rework требует нового допуска. Смена UI/management flags не снимает enforcement.
- [ ] REST/WS/legacy/background deny до model invocation; replay body/ownership/slot-release и artifact confirmations не сломаны; неизвестный owner fail closed для старта, но виден в reads.
- [ ] 401/403 на всех admin read/mutation/audit, self-freeze403, cross-origin/custom-header gate, direct forged requests; private reason/actor не попали в ordinary DTO/validation echo/logs/events/analytics; raw error/prompt/files отсутствуют в admin payload; `Cache-Control: no-store`.
- [ ] Один statement snapshot overview; [start,end), UTC/DST conversion, all-page totals, no-owner/no-start, completed!=accepted, failed!=unknown, pending!=running, stage proof/mismatch fallback. Drilldown использует ту же когорту/status.
- [ ] Session total маркирован snapshot, source+seq+time доступны только по фактам; legacy/no-scope unknown, explicit0 отдельно; никакой суммы .015435+.062745, цен Run из cumulative или model/upstream guessing. Delta остаётся выключена до verified baseline.
- [ ] Один Shell, два маршрута, old URL redirect; страницы обычного продукта не перенесены; нет retry/cancel/charts/денежных KPI.
- [ ] Poll visible15s/no overlap/hidden pause/manual refresh/backoff<=60s; stale>45s; initial error не empty, successful empty отдельно; old responses не overwrites; modal сохраняется при network error/poll, очищается при auth loss.
- [ ] Browser acceptance после отдельного разрешения запуска (в этой сессии не выполнять): desktop 1440px/mobile 375px, light/dark/forced-colors, длинные email/ID, клавиатура/focus trap/Escape/focus return, screen-reader labels, обновления не крадут фокус. Vitest controller/SSR tests не заменяют эту проверку. Если браузерный прогон не разрешён — отметить unverified, не заявлять visual acceptance.

## Review routing и границы доказательств

При подготовке плана загружены writing-plans (прочитан текущий `references/plan-template.md`), brainstorming, terse-technical-style, existing-code (прочитан `references/existing-code.md`), python-rules и postgres-rules. Прочитаны Python `sqlalchemy.md`/`testing.md` и Postgres `deployment.md`; его deployment profile не является доказательством наличия PgBouncer в этом repo — не менять инфраструктуру по предположению skill. Текущий diff только Markdown/.gitignore: Python/SQL diff-routing rows не срабатывают; language/DB code review не заявляется.

Перед **будущими** Python/SQL изменениями выполнить routing по настоящему diff: Python main/developer/idioms/errors-common/legacy-errors/testing/sqlalchemy (observability при logging); exceptions branch — в manifests нет corrode/result/returns. PostgreSQL deployment сначала, затем migrations/squawk/locking при соответствующем DDL/locks. Это ссылки на установленный plugin, не заменители его чтения в implementation session. При code review нужно перечитать соответствующие references по реальному patch.

**Недоступны в текущем списке skills:** typescript-rules, security-review, reliability-review. Не реконструировать их правила и не заявлять проведённые gates. В будущей сессии использовать их только если реально появились; иначе явно отметить unavailable. Конкретные auth/privacy/CSRF/SQL-race/idempotency/rollout acceptance tests выше обязательны независимо от наличия skills. Документная consistency check не является security/reliability audit.

**Не проверено:** production routing/upstream/model/prices; фактическая deployed harness/runner version; resume/reset baseline/retry completeness; реальные DB locking/latency; test/build/browser results. Эти пробелы не разрешают новые платные вызовы и не блокируют unknown-safe estimate. Реальные PG race tests и весь fake-executor enforcement matrix, напротив, блокируют включение управления freeze.

**Передача исполнения:** план готов для отдельной execution session через `executing-plans`; не начинать реализацию автоматически по факту сохранения этого документа.
