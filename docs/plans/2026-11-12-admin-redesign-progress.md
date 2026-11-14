# Admin redesign — execution progress

Current scope: Tasks1–10 completed locally; final real-backend acceptance and independent review passed. Production deployment/enablement not performed. Branch verified: `feat/admin-redesign`.

| Task | Status | Evidence |
|---|---|---|
| 1 schema | completed | Additive migration, metadata alignment, selected-dialect structural constraints and isolated downgrade/re-upgrade. SQLite and PostgreSQL16 regression gates passed. Unicode normalization belongs to service, not SQL trim. |
| 2 policy mutation/audit | completed | Private normalized reason, actor/target/hash idempotency, conflict response, atomic audit/policy, opt-in flag, origin/header guards, admin-only reads. Final SQLite179pass/8skip and segmented PG183pass/4skip; exact matrix below. |
| 3 atomic admission | completed | Flush-only admission and bounded transaction owner; ordered User→Conversation→Run locks; one winner, freeze/commit ordering, rollback and context preservation tested. Final PG race/lock cases green. Integration of callers intentionally deferred to Task4. |
| 4 ingress integration | completed | Early public checks preserve replay; unique committed admission before CLI/runner I/O; typed adapter admission; admitted work completes. Full SQLite and segmented PG evidence below. Management flag stays false until rollout. |
| 5 operational reads | completed | Single-statement bounded overview, [start,end) cohort/drilldown, stage proof, safe details; PG concurrent-update snapshot and synthetic500-Run EXPLAIN passed. |
| 6 harness snapshot | completed | Revised private diagnostic receipt/segment/catch-up implemented; final combined suite900pass18skip, isolated PG5+24pass, independent review cleared. Legacy payload/ACK/dedup unchanged. No rollout approval. |
| 7 client/polling | completed | Runtime DTO validation, structured errors, abort/generation polling, memory-only snapshots and auth cleanup; focused Vitest suite green. |
| 8 Overview | completed | Shared Shell routes, operational lists/cohort drilldown/safe details and honest estimate labels; SSR/view tests and real Chrome with fake API passed. |
| 9 Users | completed | Immutable uncertain payload/key, explicit conflict reconfirm, private audit;55frontend tests/build/lint and fake-API Chrome flows passed. Independent reviewer closed network/503 uncertainty findings. |
| 10 rollout/gates | completed | Real-backend Chrome acceptance, legacy redirects/default-off fallback,903offline+181PG+57frontend+43web-product gates and independent review passed; deployment remains separate. |

Tasks1–10 completed locally; independent final review closed without confirmed findings. No active task remains. No commit/push/deploy/model calls. Staging-only rolling/nginx/lock-budget checks remain release conditions, not performed here.

## Task10 final evidence — 2026-11-12

- Compatibility: pure fixed redirect helper and real legacy entry test; legacy AdminPanel no longer mounts. New Shell build-time opt-in flag has read-only unavailable fallback and no admin reads when off. Backend management flag never disables enforcement.
- Observed RED: missing redirect module (web-product42pass1fail); missing disabled view (frontend4pass1fail). Three new Python rollout tests passed existing behavior directly; no retrospective RED claim.
- `PYTHONPATH=runner/src:runner /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests runner/tests -m 'not e2e' -q --tb=short` → **903passed18skipped**. Skipped cases require PostgreSQL test URLs (`TEST_POSTGRES_URL` or bridge-specific `PPS_TEST_POSTGRES_URL`); real e2e were separately deselected by marker, not counted as passed/skipped. No live provider calls.
- Isolated PostgreSQL16: `/tmp/coreteams-task10-pg.py` collected exact plan files (`test_migrations,test_ai_access,test_ai_access_ingress,test_admin_api,test_admin_operations,test_product_api_reliability`) plus `test_admin_rollout`;181unique nodeids partitioned into25×7+6 cases → **181passed0skipped**. Each process sessionfinish left zero test databases; own labelled tmpfs container inspected and removed.
- `npm exec --yes --package=node@22 -- npm --prefix frontend test` → **57passed**; run build/lint PASS. `npm exec --yes --package=node@22 --package=pnpm@10.11.0 -- pnpm --dir web-product test/typecheck/build` → **43tests passed**, typecheck/build PASS. Locked install unchanged; system pnpm shim missing path resolved by exact packageManager version, no machine-wide changes.
- Real Chrome against frontend5184 + realbackend18104, no API interception, isolated synthetic data/fake CLI boundary → PASS: cookieauth401403; freeze/privateaudit/WSdeny; current admitted completion; unfreeze/newstart; cohort statuses; honest unknown;375px/forced-colors/logoutcleanup. `/tmp/coreteams-task10-browser.mjs` and `-compat.mjs`; screenshots `/tmp/coreteams-admin-real-overview.png`, `-users-mobile.png`, `-forced-colors.png` inspected.
- Actual proxy acceptance observed invalid_admin_origin before preserving incoming Host; dev config corrected (no backend guard weakening), regression added, hostile Origin still403. New functional change limited to frontend dev config. Production nginx untouched.
- Independent final review: no confirmed findings; reviewer ran redirect1/Vitest6/Python3 cases. Default-off redirect→unavailable and Vite build-time flag documented honestly.
- Cleanup verified: own listener PIDs53908/56894/59236/59597 stopped after matching command/port;18104/5184/5185/4193 no listeners, browser contexts closed, own PG ownership listing empty. No commit/push/deploy. Local fixture files retained only under /tmp; no real credentials/data.
- Remaining release conditions: staging rolling mixed workers, actual nginx routing, lock-budget/latency not performed. Defaults `admin_policy_management_enabled=False`, `runner_harness_diagnostics_enabled=False`, frontend enabled only by explicit build-time `VITE_ADMIN_SHELL_ENABLED=true`. See `docs/research/2026-11-12-admin-rollout-checklist.md`. Full missing-plugin security/reliability audits not claimed.

## Tasks7–9 frontend evidence

- Scope: client/API runtime parsers, generation/abort polling, common Shell admin routes, operational overview/drilldown/detail, Users/private audit/policy dialog. No backend contracts or default feature flags changed; Task10 real-backend acceptance/legacy redirect/rollout remains pending.
- RED client/polling tests: missing modules; RED view/render/user-state tests: missing components; repeated-as_of stale regression failed false→true; uncertain-payload reducer regression failed key1→key2 before fix. Later tests of already implemented flows are not claimed as observed RED cycles.
- Node22.23.5 via `npm exec --yes --package=node@22 -- ...`, locked `npm --prefix frontend ci`; system Node20 incompatible with frontend toolchain. `package.json`/lockfile unchanged.
- Final `npm exec --yes --package=node@22 -- npm --prefix frontend test` → **55 passed,9 files**. `... npm --prefix frontend run build` → TypeScript/Vite PASS; `... npm --prefix frontend run lint` → PASS; `git diff --check` PASS.
- Real Chrome fake-API acceptance (not real backend/Task10): `/tmp/coreteams-admin-browser.mjs`, `/tmp/coreteams-admin-browser-edges.mjs`, `/tmp/coreteams-admin-lost-response.mjs`, `/tmp/coreteams-admin-commit503.mjs`. All PASS: one shared header despite catalogue500, unknown-safe detail, search link, no admin requests for nonadmin, network/lost-response and committed503 retries exact payload+key, explicit409 reconfirm newkey, successful refresh, Escape/focus, mobile390px no overflow,401/403 clears private data. Screenshots desktop/mobile inspected; no pageerror. Actual mutation boundaries are intercepted synthetic responses, not backend writes/model calls.
- Independent review found network-error reason-edit identity loss and generic503 uncertainty; fixed immutable attempted payload/key, locked reason/cancel until retry resolves, exact pretransaction503 code only may unlock. Reviewer recheck closed findings. Browser committed-then-abort and committed-then503 ai_policy_unavailable both replay same body/key to success. Auth-loss still unmounts private form immediately.
- `npm --prefix frontend audit --json`: **15** lockfile advisories (12moderate,3high), all development toolchain; `npm --prefix frontend audit --omit=dev --json`: **0**. No dependency updates/fixes mixed into feature. Audit output retained only in temporary local files.
- Cleanup: own Vite process `bs05ygx7k` listener PID26201 verified by port/command, stopped after screenshot handoff and tests; port5183 no longer listening. Browser scripts closed their own contexts; no backend/container resources started by frontend batch.
- Missing plugins: typescript-rules, security-review, reliability-review. Full checklist gates NOT run; concrete DTO/auth/privacy/state/browser tests above are limited evidence, not audit claims. Existing-code reference and actual diff routing applied. Dataviz form/anti-pattern references read: semantic tables and text totals use existing theme tokens, no chart/categorical palette.

## Verification evidence

`PY=/tmp/coreteams-admin-plan-venv/bin/python`

- Initial PostgreSQL migration/bridge gate: `TEST_POSTGRES_URL=<temporary-local-PG16> "$PY" -m pytest backend/tests/test_migrations.py backend/tests/test_migration_bridge.py -q` → **11 passed, 4 skipped**.
- Service RED: `"$PY" -m pytest backend/tests/test_ai_access.py -q` → **6 failed**, missing backend.ai_access. GREEN → **6 passed** for whitespace regression.
- Endpoint RED: `"$PY" -m pytest backend/tests/test_admin_api.py -k policy_management -q` → **1 failed**, 404 instead of 503. GREEN focused suite → **12 passed**.
- Expanded first PostgreSQL API test run: **105 passed, 1 failed**: test helper incorrectly registered existing user instead of login. Corrected test to real login; no production auth change.
- Final suite command:
  `"$PY" -m pytest backend/tests/test_migrations.py backend/tests/test_migration_bridge.py backend/tests/test_admin_api.py backend/tests/test_ai_access.py backend/tests/test_auth_api.py backend/tests/test_ownership_isolation.py backend/tests/test_repository.py backend/tests/test_repository_confirmation_recovery.py backend/tests/test_public_events.py -q`
  - SQLite: **169 passed, 8 skipped**.
  - Same command with isolated `TEST_POSTGRES_URL`: PostgreSQL16 **173 passed, 4 skipped**.
- Race cases: 20 repetitions each duplicate admission, competing admins, freeze-first and admission-first; real PostgreSQL lock timeout returns unavailable without partial mutation; synthetic retry exhausted at 3 attempts.
- Admission tests were added after initial service implementation, not a separately observed missing-admission RED; do not claim all Task3 steps followed strict RED-before-GREEN.
- `git diff --check` PASS; compileall on new service/admin/migration PASS.

## Disposable PostgreSQL

User explicitly approved local disposable DB. Created unique postgres:16-alpine container (image already local), published only 127.0.0.1 ephemeral port, no project volumes/networks. Verified full container ID, ownership label and image before cleanup. Stopped and removed only that container; final owned-label listing empty. No existing DB/container touched. Credentials were temporary test-only, not repo settings.

## Actual-diff rule routing / limitations

Existing-code Boundary guard applied. Python main/developer/idioms/errors-common/legacy-errors/testing/sqlalchemy and PostgreSQL deployment/migrations/squawk/locking references read before editing and checked against actual diff. No Result-library dependency, exceptions branch used. SQLAlchemy docs fetch unavailable; patterns use installed SQLAlchemy2 and were exercised on both dialects. No SQL files/Squawk configuration added.

security-review/reliability-review skills unavailable: full checklist gates NOT run. Concrete notes only: auth/role/origin/private reason/validation echo tested; transaction rollback, row locks, bounded retries/timeouts, terminal event idempotence and current-run completion tested. Remaining Tasks4+ ingress/WS enforcement, full privacy matrix and production lock budget unverified. New v1 users/audit validation errors now use local sanitized domain errors (invalid_pagination / invalid_policy_request); unrelated routes unchanged. Feature remains disabled.

## FINAL VERIFIED CHECKPOINT — supersedes pending PG statuses below

Tasks1–3 COMPLETED; Task4–10 pending. All accessible functional/database gates passed. Feature remains disabled. No in_progress work remains.

Final PostgreSQL16 segmented gate: offline nodeid collection confirmed **187 unique cases**, union of eight groups exactly equal full suite (no missing/duplicates). One new owned PG16 tmpfs1.5GB container with memory+swap cap2GB and loopback-only port; no volume mounts. Groups ran sequentially using exact commands below, each sessionfinish followed by SELECT count(*) FROM pg_database WHERE datname LIKE 'test_%' → **0**. Results:

| Group | Collected | Result |
|---|---:|---|
| migration + bridge |16|12 passed,4 skipped|
| admin API + auth + ownership |35|35 passed|
| AI service non-repeated |32|32 passed|
| duplicate admission races |20|20 passed|
| competing admin races |20|20 passed|
| freeze-first commit order |20|20 passed|
| admission-first commit order |20|20 passed|
| repository + recovery + public events |24|24 passed|

Aggregate **183 passed, 4 skipped**; all eight pytest return codes0, each test DB cleanup0. Previous failed monolithic disk/tmpfs runs remain historical infrastructure failures, not counted as passes. Final SQLite **179 passed,8 skipped**. `git diff --check` and compileall PASS. No production lock budget/staging clone check: unverified deployment prerequisite, not fabricated by unit tests.

Owned container full ID/label/image/mounts/memory inspected before stop/remove; final ownership-label listing empty. No shared resources/fixtures changed, no new code in segmented verification stage. security-review/reliability-review plugin checklists unavailable; concrete gates run as documented, full audits not claimed. Independent reviewer snapshot preceded last added coverage. No commit/push/deploy/model calls.

## Contract-completion follow-up (history; final pending statuses superseded above)

Final SQLite complete suite after additions: **179 passed, 8 skipped in 20.76s**; git diff --check PASS. Full command is the same nine-file suite above. Final PostgreSQL gate remains unpassed for capacity reasons described below.

Closed gaps: sanitized pagination error (observed RED KeyError missing code, then GREEN 1 passed); actor-specific key namespace; admin self-unfreeze after another admin freezes; non-admin service denial; strict freeze bool and unfreeze note length; concurrent same-key exact receipt; missing/terminal Run; HTTP audit failure rollback/private error sanitization; exact receipt allowlist. Additional tests of existing implemented behavior passed immediately and are not claimed as artificial RED cycles. Original Task3 RED-order limitation remains.

Implementation-plan clarification: no separate duplicate email snapshot fields in audit. The minimal admin-only receipt response may retain AdminUserRow.email for exact replay. Tests assert exact receipt/user/ai_access key sets; no reason/actor/private payload leaks into receipt or ordinary /me. Independent reviewer found no confirmed defects and passed 6 targeted SQLite tests, but their snapshot preceded these final additions.

Final PostgreSQL gate is PENDING VERIFICATION due infrastructure capacity, not a product-code assertion. Disk-backed run failed with DiskFullError: Docker VM filesystem 100% full. Owned anonymous volume was inspected and removed with its own container; this freed ~1.8GB. One bounded retry used owned tmpfs1.5GB with container memory/swap cap2GB; available VM memory was ~8GB. This run also failed DiskFullError because existing fixtures retain a separate database per case until sessionfinish. Console showed 118 consecutive passing tests before first setup errors, plus later failure markers; no reliable pytest final count was emitted because sessionfinish cleanup also failed. This partial run is NOT a passed gate and prior 173-pass gate is NOT a replacement for changed final matrix. No more retries performed. Shared fixtures unchanged. Owned retry containers removed, including verified disk anonymous volume; RAM container had no volume mounts.

Bounded next-stage final PG gate (commands listed, NOT executed here): set TEST_POSTGRES_URL to a newly approved isolated PG16 and PY=/tmp/coreteams-admin-plan-venv/bin/python; run these as separate pytest processes, sequentially, so existing sessionfinish cleans each group:

```sh
"$PY" -m pytest backend/tests/test_migrations.py backend/tests/test_migration_bridge.py -q
"$PY" -m pytest backend/tests/test_admin_api.py backend/tests/test_auth_api.py backend/tests/test_ownership_isolation.py -q
"$PY" -m pytest backend/tests/test_ai_access.py -k 'not concurrent_admissions and not concurrent_admins and not policy_admission_order' -q
"$PY" -m pytest backend/tests/test_ai_access.py -k concurrent_admissions -q
"$PY" -m pytest backend/tests/test_ai_access.py -k concurrent_admins -q
"$PY" -m pytest backend/tests/test_ai_access.py -k 'policy_admission_order and True' -q
"$PY" -m pytest backend/tests/test_ai_access.py -k 'policy_admission_order and False' -q
"$PY" -m pytest backend/tests/test_repository.py backend/tests/test_repository_confirmation_recovery.py backend/tests/test_public_events.py -q
```

AI service collection: 112 cases = 32 non-repeated +20 duplicate-admission +20 competing-admin +40 commit-order (20 freeze-first/20 admit-first). Keep each PG process group small, no simultaneous copies. Docker disk/tmpfs failures require no application fixture/production changes. Tasks2–3 remain pending final PG verification, not pending forever for unavailable skill plugins; no in_progress remains. Task4 untouched.



## Batch4–6 checkpoint

Tasks4–5 implemented; Task6 pending a narrow compatible diagnostic-event contract. No active task at checkpoint; Tasks7–10 pending. Flag admin_policy_management_enabled remains false: UI, rollout/compatibility and final acceptance gates precede operator enable; enforcement itself is not gated by that flag.

- Task4 RED: ingress3failed/1passed (after correcting nonexistent fixture initializer); GREEN public/shared boundaries110passed/8skipped. New explicit18 ingress cases cover raw/run.create/project kinds, V2 initial/rework/answer, fake runner no dispatch, pending deny, duplicate background, admitted completion, unfreeze and missing-owner fail closed.
- Task5 RED2failed404; GREEN operational9 cases includes PostgreSQL-only interleaved status update, synthetic500Run EXPLAIN, SQL-limit assertion, known-empty, no-owner/no-start, stage proof and safe estimates. Explicit outer ORDER BY corrected after independent review; reviewer rechecked source, not PG runtime.
- PostgreSQL16 final operations+ingress:27passed. Separate final runner/product regression40passed; freeze-first20passed and admission-first20passed. These commands overlap older runs; no combined unique-PG count is claimed. All tests use fake model/container boundaries.
- Initial PG fixture failures were cross-event-loop test-session bugs; corrected via TestClient.portal. Each successful pytest process ended with0 test_* databases. Both own bounded tmpfs PG16 containers inspected by full id/ownership label then removed; ownership listing empty, unrelated containers untouched.
- Task6 prototype tests passed but were removed with unsafe optional wire enrichment; those prototype counts are NOT final feature evidence. Retained safe mapper and unknown-only production default. Post-removal focused protocol/detail/ingress suite115passed/1skipped. Full final checkpoint suite result recorded separately below.
- Applicable existing-code Boundary and Python main/developer/idioms/errors-common/legacy-errors/testing/sqlalchemy plus PostgreSQL deployment/locking references read before edits and routed against actual diff; no new schema changes in this batch. security-review/reliability-review skills unavailable, full audits NOT run. Concrete privacy/auth, atomic admission, PG snapshot/race, protocol regressions executed. No models, commit/push/deploy or frontend changes.

### Final unchanged-wire checkpoint evidence

Commands from repo root, PY=/tmp/coreteams-admin-plan-venv/bin/python:
- `PYTHONPATH=runner/src:runner /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests -q --tb=short` → **435 passed,18 skipped**,69.71s. Skips are existing selected-dialect/concurrency/optional environment tests plus the new PG-only concurrent snapshot case on SQLite; real PG case passed separately.
- `PYTHONPATH=runner/src:runner /tmp/coreteams-admin-plan-venv/bin/python -m pytest runner/tests -m 'not e2e' -q --tb=short` → **437 passed**,6.91s. Real-model/e2e excluded.
- Post-removal focused protocol/admin/admission regression →115passed/1skipped. Protocol/source files in runner, backend runner_app/runner_transport are byte-identical HEAD; no unsafe ACK-skip or optional enrichment remains.
- Final PG16 ingress+operational matrix →27passed; operational controlled-update9cases (included in27), separate runner/product40passed before metadata-prototype removal, freeze-first20/admission-first20cases. Counts overlap previous runs; no inflated aggregate.
- compileall and `git diff --check` passed. Own Docker containers f655f38… and6e6f010… inspected by full ID/label/image/tmpfs and removed; final owned listings empty, DB count0 before deletion.

Historical checkpoint: production numeric harness estimate was unavailable before the revised Task6 implementation below. Tasks4–5 remain complete; this history is not rollout approval.

## Task6 revised independent diagnostic implementation — 2026-11-12

Independent review approved the revised technical contract after closing wrapper rollback and stale projection blockers. Implemented private WS envelope/receipt, trusted persisted source/command segments, exact decimal strings, standalone journal pass independent of public cursor, fair per-ID retry, and versioned monotonic projection/catch-up. Legacy v0 translator and public bytes/IDs/canonical dedup/ACK/replay remain unchanged. Both management and diagnostic feature flags remain false by default; Tasks7–10 untouched.

RED evidence: builder test collection failed missing ctrunner.harness_diagnostic; durable receipt test failed unexpected backend_run_id; projection test collection failed missing catch_up. Each targeted implementation reached GREEN. Later compatibility/failure cases exercised already implemented behavior and are not represented as artificial RED cycles.

Target matrix includes: receipt commit/lost ACK/reopen/exact conflict; authenticated project mismatch and strict private allowlist; terminal accepted vs unaccepted sources; source below cursor and same-row remaining public drafts; pending answer acceptance gap remains unbound; no prefeature guessed mapping; deterministic namespaces/Decimal invalid/boolean/NaN/no-session; stale captured S1 after S2 and simultaneous writers; receipt-before-projection crash recovered by admin detail; versioned wrapper rejected by pinned old mapper; actual pinned old sender rollback and capability off/on; actual pinned production receiver+store with new sender. Fake old-welcome test named explicitly as fake. No model calls.

Pinned provenance (git a1d6179 fixtures): gateway SHA25680d146884fb7a7c5a1bdab6fbcd74c67a84f22c50a6cff90961dd1699c7d1d2f; receiver d926e718784fc200c5773a44f3ad067a50a7ddaf34b6c688bf8eedb861abc4f2; store23a532a9662c05ad832ffa11d4b59d21476bc1185d3563cd5b94a7a8284d7a25. Each matched git show bytes. Mapper fixture contains the actual pre-Task6 standalone source (untracked module was not present at git HEAD), not current wrapper-aware code.

Independent reviewer: no confirmed findings, own27 offline passes; initially flagged FakeGateway evidence gap. Added pinned production receiver/store live WS test; reviewer verified fixture bytes/source loading and ran that one case successfully. Reviewer did not run PostgreSQL; PG evidence below belongs to implementer.

PostgreSQL16 isolated final groups: `PYTHONPATH=runner/src:runner TEST_POSTGRES_URL=<temporary> "$PY" -m pytest backend/tests/test_harness_projection.py -q --tb=short` → **5 passed**; separate process `... backend/tests/test_admin_operations.py backend/tests/test_runner_executor.py -q --tb=short` → **24 passed**. After each pytest session test_* DB count0. Owned tmpfs1.5GB/memory2GB container ID/label inspected then removed; final ownership listing empty. No existing volumes/resources touched.

Final combined command `PYTHONPATH=runner/src:runner /tmp/coreteams-admin-plan-venv/bin/python -m pytest backend/tests runner/tests -m 'not e2e' -q --tb=short` → **900 passed,18 skipped in77.18s**. This is one combined count, not added to overlapping focused/PG runs. Skips are pre-existing optional/dialect cases and PG-only tests under SQLite; PostgreSQL projection path was exercised separately. Runner scoped Ruff, compileall, git diff --check PASS. Runtime tests emit the pre-existing root pytest-asyncio fixture-loop deprecation warning; runner project-specific check remains lint-clean. Applicable actual-diff Python main/developer/idioms/errors-common/legacy-errors/testing/sqlalchemy/observability and PG deployment/migrations/squawk/locking references read/routed; SQLite bootstrap is additive, no PostgreSQL DDL/Alembic migration in Task6. security-review/reliability-review skills unavailable: their full checklist audits NOT run; concrete privacy, integrity, replay, concurrency and rollback tests are evidence, not substitute audit claims.
