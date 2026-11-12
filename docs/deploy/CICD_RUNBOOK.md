# CI/CD: preactivation runbook
Статус на 2026-11-12: **LOCAL evidence есть; ACTIVATION pending; FIRST DEPLOY blocked**.
Это Boundary-контракт Actions → GHCR → SSH → k3s → данные, не заявление production-ready.
Ни этот документ, ни согласование дизайна не разрешают настройки, provisioning или deploy.
Нормативный контекст: [дизайн](../plans/2026-11-11-cicd-design.md),
[Tasks 5/6](../plans/2026-11-11-cicd.md#task-5-githubvm-activation--только-после-отдельного-a).
Операционный контракт сверять с [CI](../../.github/workflows/ci.yml),
[Deploy](../../.github/workflows/deploy.yml), [release](../../tools/cicd/release.py),
[verifier](../../tools/cicd/verify_release.py), [server](../../tools/cicd/deploy_server.py).
## 1. Статус и разрешения
- [x] **L / LOCAL**: локальная реализация завершена в `ci/cd-k3s`; evidence ниже.
- [x] **A1 / REPO — commit/push/PR**: разрешены пользователем 2026-11-12 после синхронизации с master.
  Code-review по-русски публикуется после открытия PR; **merge не разрешён**.
  Разрешение на один шаг не разрешает остальные; перед merge review обязателен.
- [ ] **A2 / GITHUB**: protection/environment/private GHCR publish, scope §3; approval pending.
- [ ] **A3 / VM**: restricted SSH/RBAC/pull Secret/read-only DB role/mapping, без restart;
  approval pending. **A / ACTIVATION** требует ссылки на согласование каждого scope из §3.
  Task 5 remote **не выполнена**; имеющиеся GitHub/SSH полномочия — только read-only.
- [ ] **P / FIRST DEPLOY**: отдельное согласование maintenance, data changes, первого rollout (§5).
  A не разрешает P. Наличие секретов или admin-доступа не является approval.
- [ ] Ручные security/reliability gates: соответствующие skills недоступны, не запускались.
  Security: permissions/injection/secrets/supply chain/SSH/RBAC/Pod privileges.
  Reliability: lock/races/stale release/partial rollout/schema/restore/RPO.
**Перед запросом A перечислить чувствительные записи:** master protection и bypass policy,
правила squash/merge, environment `production`, secrets и две activation variables;
создание/публикация трёх private GHCR packages и package access; выдача SSH identity,
установка trusted wrapper/interpreter/imports/config/state/lock и read-only API credentials;
namespace pull Secret и RBAC. Подтвердить, что A не меняет application Pod templates/данные.
**Перед P отдельно перечислить:** остановку writers, backups/restore rehearsal, точную миграцию
(если нужна), clearance на точный SHA/schema, изменение pull policy/Secret references,
первый image rollout, downtime и согласованный план rollback/потери данных.
## 2. Что доказано локально, а что нет
Переданные результаты локальной проверки: три `linux/amd64` final images и archives в
`/tmp/pps-task2.4yteDY/images`; CI contracts — 244 PASS; backend с disposable PostgreSQL —
308 PASS; runner — 437 PASS; frontend — 28 PASS; product — 45 PASS.
Это исторический snapshot тестов, не подтверждение изменяющихся сейчас tools/config.
Финальный adapter принят по evidence координатора: full PG suite **564 PASS**, zero skips,
280.15s; Ruff/format 20 files PASS. Здесь suite не повторялся. Task 3 — **fake registry**,
Task 4 — **fake API/server**, не GHCR/SSH/k3s. Cold runner build ~36 минут, CI budget 90 минут;
GitHub cold run не проверен. Старые dirty-tree images — test artifacts, не release:
release images **заново собрать и проверить из actual approved merge SHA**, затем publish без rebuild.
Исторический snapshot перед PR sync/rebase (2026-11-12): origin/master `a1d6179` на один commit
впереди branch `8a6b5eb`; PR #24 изменил docs/prototypes, не backend migrations/CI.
Перед push ветка синхронизируется с актуальным origin/master; upstream prototype не включается
как собственное CI/CD изменение. Этот snapshot не описывает текущую степень синхронизации.
Историческое read-only наблюдение 2026-11-11, **всё перепроверить перед A/P**:
namespace `coreteams`, backend `coreteams-backend:v5`, frontend `coreteams-frontend:v6`,
`EXECUTOR=cli`, `imagePullPolicy: Never`, k3s/containerd, backend Recreate;
mounts `/app/backend/data`, `/home/node`, local RWO PVC/reclaim Delete.
Provenance/байты работающих images и actual DB schema неизвестны; fork migration IDs конфликтуют.
Старые `/var/backups/coreteams` не restore-verified. HTTP 200 не доказывает compatibility.
Read-only GitHub 2026-11-12: PR #21 показывает только `compose/frontend/python/web-product`,
не новый gate; GET master protection вернул `404 Branch not protected` (не проверка rulesets).
На момент этого исторического наблюдения CI/CD PR ещё не был открыт.
Будущий required context нельзя выдавать за установленный.
Координатор повторно проверил live read-only 2026-11-12: SSH port 2297 работает,
Deployment `backend`/`frontend` подтверждены (container names ещё не подтверждены), четыре
workload Ready 1/1; application CronJobs не обнаружены. Actual schema по-прежнему неизвестна:
нет согласованной read-only DB role/route, host psql отсутствует. Backup metadata только
2026-10-30, off-host copy/restore не доказаны; OCI revision label пуст, provenance не установлен.
Local ingress HTTPS 200 и TLS valid; external URL timeout с сервера не доказывает внешний outage.
Это переданное evidence, не самостоятельно повторённые здесь probes; перед P перепроверить.
## 3. ACTIVATION checklist — только после A, deploy остаётся off
- [ ] Перепроверить repository/master/protection/rulesets/merge methods/environments/flags.
  Согласовать PR-only master, squash-only merge, required checks и admin bypass policy.
- [ ] На **реальном PR нового CI** проверить success/failure/docs-only/cancelled пути gate.
  Предлагаемый UI label — `CI / gate`, YAML job — `gate`; protection context взять из
  фактического check-run `name` и GitHub App ID, а не скопировать UI label наугад.
  Затем проверить effective protection и merge blocking. До этого required gate UNVERIFIED.
- [ ] Environment `production`: selected deployment branch **только master**, не wildcard tags;
  optional required reviewers — отдельное согласование и проверка доступности функции.
- [ ] Secrets только environment `production`: `CICD_DEPLOY_HOST`, `CICD_DEPLOY_USER`,
  `CICD_DEPLOY_SSH_KEY`, `CICD_DEPLOY_KNOWN_HOSTS`. Host key сверить независимым каналом;
  не `ssh-keyscan` в workflow. Не читать/печатать существующие значения.
- [ ] `CICD_PUBLISH_ENABLED` и `CICD_DEPLOY_ENABLED` — **раздельные repository variables**.
  До A обе отсутствуют/не `true`; publish разрешить отдельно после A, deploy оставить off до P.
  Deploy verify job вне environment, поэтому environment-only flag ему недоступен.
- [ ] Private GHCR packages фактического release contract:
  `ghcr.io/acrycxde/pp-sber-autumn-2026-backend`, `...-frontend`, `...-runner`.
  Реализация использует tag `<40hex>`, не `sha-<40hex>`; истина — `@sha256:<64hex>`.
  Это расхождение с ранним дизайном сверить до A; не создавать packages по старым именам.
  Publish только master push после всех checks, не manual CI/PR/docs-only; no rebuild.
- [ ] Проверить private visibility/access для каждого package, в том числе ранее существовавшего.
  Пользователь предоставляет credential **только read:packages** с package access для pull;
  publish token не устанавливать на VM. Namespace Secret не хранить в git/logs.
- [ ] Отдельный SSH user/key: forced command `/usr/local/sbin/coreteams-deploy`, `restrict`,
  без shell/PTY/agent/X11/port forwarding/user rc. Запрет обхода forced command через sudo/env.
  Root-owned trusted wrapper, Python interpreter, imports и parent directories; sanitized env,
  фиксированный PATH, отсутствие writable import paths, arbitrary command/manifest arguments.
  Root-owned `/var/lib/coreteams-deploy` (state/lock) и `/etc/coreteams-deploy` (config);
  API credential read-only для нужных contents/actions, не в stdout и не из request JSON.
- [ ] Namespace-scoped identity: read rollout state, patch только двух фиксированных Deployments;
  secrets/PVC/RBAC/jobs/exec запрещены. GitHub не получает kubeconfig/cluster-admin.
  Server ожидает Deployment и container `backend`/`frontend`; Deployment names ранее совпадали,
  container names перепроверить. `coreteams-backend:v5` — image, не имя Deployment.
  Wrapper генерирует только image patches, RBAC сам по себе не ограничивает поля patch.
  Patch Deployment позволяет запуск кода с Pod privileges/секретами, при unsafe mounts —
  потенциально root-equivalent: forced command/RBAC не sandbox. Проверить Pod privileges.
- [ ] Проверить deny/allow RBAC, invalid input и disconnect/lock behaviour на изоляции.
  Dry-run API CLI не предполагается: использовать существующие fake tests, не production wrapper.
  Pull disposable Pod — только с отдельным разрешением workload; фиксировать digest/exit/runtime.
- [ ] Pull Secret можно provision после A, но **не patch live imagePullSecrets/policy**:
  любой Pod-template patch запускает rollout. `IfNotPresent`/Secret reference — reviewed inputs
  для P вместе с image change, без activation restart. Никаких cluster restart/registries.yaml.
- [ ] Schema adapter **реализован LOCAL**, production mapping/baseline/role не provisioned.
  [Probe](../../tools/cicd/schema_probe.py) подключён через server CLI, lazy под lock при schema-risk;
  обычный известный UI-only путь не читает DB config и не импортирует driver.
  Legacy bridge signature недостаточна (timezone/precision/partial indexes/missing tables):
  clearance опирается на новый catalog fingerprint, не bridge preflight или live hash как эталон.
  Fixed protected paths в `/etc/coreteams-deploy/`: `schema-probe.json` (mapping ниже),
  `schema-baseline.json`, `schema-probe-credential.json`, `clearance.json`.
  Root-owned files/parents, no symlinks/group/world write; credential строго root-only 0400/0600,
  JSON keys только `username/password`, username совпадает с role. Значения не печатать.
  CA — отдельный public-readable root-owned файл без group/world write; TLS verify hostname обязателен.
  Clearance keys: `sha/signature/target/preflight/rehearsal/approval`; target связывает mapping
  и observed DB/user/version/backend address/port, signature равна offline expected canonical.
  Profile строго PG `160015`, `pg_catalog_v1`, inventory 1, SHA256 installed `schema_catalog.sql`;
  это узкий SERIAL/heap/btree profile, не универсальный SQL proof. Standard builtin trust,
  unsupported/external DDL → отказ. Actual production compatibility всё ещё UNVERIFIED.
Mapping JSON shape (только placeholder-схема, не готовый production config):
```json
{"schema":1,"endpoint":"<TLS DNS>","port":5432,"ca_file":"<absolute CA path>",
 "cluster_identity":"<reviewed identity>","database":"<DB>","role":"<read-only role>",
 "schemas":["public"],"profile":{"name":"pg_catalog_v1","server_version_num":160015,
 "inventory_version":1,"query_sha256":"<64hex installed SQL SHA256>","settings":{
 "search_path":"pg_catalog","standard_conforming_strings":"on","TimeZone":"UTC",
 "DateStyle":"ISO, YMD","IntervalStyle":"postgres","extra_float_digits":"3"}}}
```
Baseline генерирует [offline CLI](../../tools/cicd/schema_baseline.py) из exact approved SHA,
не из dirty working tree/production DB. Он исполняет reviewed migration/bridge code этого SHA
на собственном disposable pinned PG16.15 Docker container (cached image, loopback trust),
сверяет clean и все поддержанные upgrade paths; это локальные DDL, не production maintenance.
Сейчас approved deployment SHA нет: сначала отдельный commit/PR/CI/review/merge, затем baseline.
После такого разрешения локально: `python3 -m tools.cicd.schema_baseline --release-sha "$APPROVED_SHA" --mapping "$REVIEWED_MAPPING" --output "$NEW_BASELINE"`.
Переменные — reviewed SHA, mapping path и новый output path; CLI не заменяет existing output.
## 4. Read-only команды проверки
Выполнять из корня CI/CD worktree. `gh` 2.100.0, синтаксис сверён с локальным `--help`.
`PR`, `CHECK_SHA`, `RUN_ID` заменить проверенными не-секретными значениями;
не оставлять placeholders. Команды ниже не меняют GitHub/кластер.
```sh
REPO=AcryCxde/PP-Sber-Autumn-2026
PR='<номер реального CI/CD PR>'
CHECK_SHA='<40hex SHA check suite этого PR>'
RUN_ID='<положительный ID master push CI run>'
gh pr checks "$PR" --repo "$REPO" --json name,workflow,state,link
gh api -H 'X-GitHub-Api-Version: 2022-11-28' \
  "repos/$REPO/commits/$CHECK_SHA/check-runs" \
  --jq '.check_runs[] | {name,app_id:.app.id,conclusion}'
gh api -H 'X-GitHub-Api-Version: 2022-11-28' "repos/$REPO/branches/master/protection"
gh run view "$RUN_ID" --repo "$REPO" --json event,headBranch,headSha,conclusion,attempt,url
gh api -H 'X-GitHub-Api-Version: 2022-11-28' \
  "repos/$REPO/actions/runs/$RUN_ID/artifacts" \
  --jq '.artifacts[] | {name,id,expired,expires_at}'
```
Для RBAC оператор использует **уже provisioned deploy identity**, не admin context;
`KUBECONFIG` передать защищённым способом, не выводить содержимое. После сверки имён:
```sh
kubectl -n coreteams auth can-i get deployments/backend
kubectl -n coreteams auth can-i patch deployments/backend
kubectl -n coreteams auth can-i patch deployments/frontend
kubectl -n coreteams auth can-i patch deployments/unrelated
kubectl -n coreteams auth can-i get secrets
kubectl -n coreteams auth can-i delete persistentvolumeclaims
kubectl -n coreteams auth can-i create roles.rbac.authorization.k8s.io
kubectl -n coreteams auth can-i create pods --subresource=exec
```
Ожидаются первые три `yes`, остальные `no`; отдельно сверить все verbs/resources с Role.
Не выдавать `no` из-за ошибки подключения за подтверждённый запрет.
REST reference: [checks](https://docs.github.com/en/rest/checks/runs),
[protection](https://docs.github.com/en/rest/branches/branch-protection),
[runs](https://docs.github.com/en/rest/actions/workflow-runs),
[artifacts](https://docs.github.com/en/rest/actions/artifacts).
Повторная загрузка официальных страниц здесь заблокирована сетью; поддержку API version
`2022-11-28` сверить перед будущими settings writes; новые settings endpoints здесь не предложены.
## 5. FIRST DEPLOY checklist — отдельный P
- [ ] Read-only preflight actual CPU/platform, CLI pin **2.1.112**, effective auth/network/config,
  schema structural signature и fork history. Не запускать новый backend: startup делает migrations.
- [ ] Сохранить `EXECUTOR=cli`, PVC/home/workspaces, Secret refs и стратегии; runner image на CLI
  стенд не доставлять, Docker/socket не добавлять. CI amd64 smoke не доказывает VM CPU compatibility.
- [ ] Остановить writers/schedulers в согласованное окно. Backup **DB + S3 + PVC/home/workspaces**,
  off-node копия, проверенный согласованный restore point и восстановление на изолированной копии.
  Зафиксировать RPO/downtime; не выводить credentials/персональные данные в evidence.
- [ ] На restored копии проверить actual schema и при необходимости отдельно согласовать миграцию
  по [canonical maintenance](../CANONICAL_MIGRATION.md). Не `stamp`, blind downgrade или reverse bridge.
  Copy/paste apply-команды здесь намеренно нет; ни CI, ни deploy не запускают maintenance автоматически.
- [ ] Rehearsal с реальным backup/S3: новый диалог/ответ/stream, restart/resume, файлы/artifacts,
  `/product/`, auth/ownership и persisted state. PG unit tests не заменяют этот rehearsal.
- [ ] Утвердить конкретный P и migration approval; сохранить текущие image bytes/config и возможный
  verified rollback digest. Старые локальные tags без provenance не считать доступным GHCR rollback.
- [ ] Frozen server clearance должен связать точный release SHA, **actual schema signature/target**,
  preflight/rehearsal/approval evidence; проверка live schema под lock, не dispatch boolean/version row.
  Не создавать фиктивные evidence/state для обхода первого unknown-baseline gate.
- [ ] Утвердить способ первого совместного изменения image + pull policy/Secret reference:
  image-only wrapper **этого не делает**. Без reviewed P transition sequence первый deploy blocked.
- [ ] После выполнения всех условий разрешить deploy flag; вручную выбрать master и verified release.
  Manifest `release-<SHA>-<run_id>-<attempt>` schema 1, срок хранения 7 дней; expired/missing → отказ,
  не rebuild. Verifier повторяет provenance/attempt/master freshness на сервере под lock.
Следующая команда — **ЗАПИСЬ / production dispatch, только после P**, здесь не выполнялась.
Вместо CLI можно Actions → Deploy → Run workflow, branch **master**; inputs те же.
```sh
RELEASE_RUN_ID='<проверенный ID успешного master push CI run>'
RELEASE_SHA='<точный 40hex SHA из этого run и manifest>'
gh workflow run deploy.yml --repo AcryCxde/PP-Sber-Autumn-2026 --ref master \
  -f "release_run_id=$RELEASE_RUN_ID" -f "release_sha=$RELEASE_SHA"
```
## 6. Каждый deploy, failure и rollback
Release SHA reachable из master; до master допустима только доказанная docs-only delta.
Docs-only SHA сам не release; старше deployed SHA обычный dispatch запрещён.
Все backend/unknown-code/schema-risk изменения требуют maintenance clearance, не SQL-proof.
GitHub `production-deploy` не гарантирует FIFO; server flock — окончательная serialization.
Сохранить run/attempt/SHA/три digests, предыдущие images, rollout и functional smoke evidence.
Успешный повтор — no-op только после проверки actual state; image drift блокирует повтор.
При timeout/SSH loss/частичном rollout оставить maintenance, проверить actual Deployments,
rollout и trusted persistent state **под тем же lock**. `failed`/`in_progress` требуют оператора;
не удалять lock/state, не слать retry и не отмечать success ради разблокировки.
Reconcile оба images, schema и writers; отдельное решение о продолжении/rollback и state repair.
Текущий wrapper не предоставляет операторскую reconciliation/rollback CLI: порядок согласовать.
Image rollback только на сохранённые **schema/runtime-compatible verified digests** под lock,
не blind `kubectl rollout undo` и не обычный stale dispatch. После data migration rollback images
не откатывает данные: отдельно согласовать coordinated DB/S3/PVC restore и потерю записей по RPO.
PVC не удалять. Writers разрешить только после rollout и функциональных проверок, затем наблюдать.
