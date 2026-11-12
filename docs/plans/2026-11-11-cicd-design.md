# CI/CD: согласованный дизайн

Дата: 2026-11-11. Статус: **дизайн согласован пользователем; текущий этап — planning, локальная реализация следует после handoff в executing-plans; активация требует отдельного approval**.
Исполнимый план: [2026-11-11-cicd.md](2026-11-11-cicd.md).

## Цель и границы

GitHub Actions проверяет PR, после squash в `master` собирает и проверяет release artifacts,
после отдельного разрешения публикует их в GHCR. Штатный **Actions → Deploy → Run workflow**
вручную развёртывает проверенный release в существующий k3s. Новых UI-кнопок в приложении нет.
Согласование дизайна не разрешает GitHub mutations, публикацию пакетов, SSH provisioning,
изменение данных или первый production deploy. В текущей planning-подзадаче изменяются только два документа;
локальная реализация допустима исходной задачей после handoff в `executing-plans`.

Это Boundary-изменение: Actions → registry → SSH → Kubernetes → stateful runtime.
Нельзя считать доступность HTTP `/` доказательством сохранения функций приложения.

## Исходное состояние и достоверность

Локально проверено: ветка `codex/coreteams-product-prototype-2026-11-11`, начальный status clean;
HEAD `68f7f0e4e900890d8a6a476533b43f9c543ad03e`, `origin/master`
`8a6b5eb91cc92c211843048d18ac53d4f76fd74b`. Между ними четыре docs/prototype-коммита.
Проверенные CI/runtime manifests не отличаются от master. `.github/workflows/ci.yml`
уже запускает backend/runner pytest, frontend test/build, product test/typecheck/build,
Compose config; Docker build и lint не включены.

Факты предыдущего read-only исследования, перед активацией перепроверить:
- Public repository `AcryCxde/PP-Sber-Autumn-2026`, live master совпадал с origin/master, CI green.
  Разрешён squash; master пока без protection/rulesets. Environments/secrets/variables пусты;
  admin-доступ не является разрешением на изменение настроек.
- VM: Debian 13, k3s 1.35.2/containerd, Docker отсутствует; namespace `coreteams`.
  backend `coreteams-backend:v5`, frontend `coreteams-frontend:v6`, `imagePullPolicy: Never`;
  Postgres 16-alpine, SeaweedFS 4.50. Backend/db/s3 — Recreate, frontend — RollingUpdate.
  Backend probes TCP; `/` возвращал 200. Ready node `core-team-node1-dcku`, есть старый NotReady node.
- Local RWO PVC с reclaim Delete; регулярный проверенный restore отсутствует.
  Старые SQL-файлы `/var/backups/coreteams` не доказывают восстановимость.
- `/opt/coreteams` — fork `demo-gateway` на `0acfc842cc5129b1a7a245fc292d855d2dea5618`,
  untracked `k8s-deploy.yaml`; `/opt/coreteams-v6` — manifests без `.git`.
  Provenance запущенных образов не установлена. Root BuildKit есть; GHCR TLS/outbound доступны.
- Дополнительная read-only проверка координатора: и Deployment, и Pod явно задают `EXECUTOR=cli`
  (выше приоритетом, чем envFrom); command/args не заданы. Mounts backend только
  `/app/backend/data`, `/home/node`. Docker socket отсутствует. Оба Dockerfile устанавливают CLI,
  используют USER node и CMD uvicorn. Полный compatibility rehearsal **не проводился**. Сохранить backend CLI pin `2.1.112`:
  Dockerfile связывает его с отсутствием SSE4.2 у CPU VM; не обновлять CLI в CI/CD scope.

Локальный lint-факт, не ревью прикладного кода: Ruff 0.17.4,
`(cd runner && uv tool run --offline ruff check src tests evals)` — PASS;
`ruff format --check` там же — FAIL, четыре файла. Backend `ruff check backend --select E4,E7,E9,F`
— FAIL: три F401. Нельзя объявлять будущий pinned baseline зелёным без повторения.
Frontend lint и новый product lint не запускались; полного suite в этой задаче не было.

## Выбор механизма deploy

| Вариант | Решение |
|---|---|
| Отдельный `workflow_dispatch` | **Выбран и согласован**: явное действие оператора, отдельный run, digest/SHA в summary |
| Автоматический workflow + environment approval | Не выбран: approval — дополнительная защита, но не замена ручному запуску |
| GitOps controller | Не выбран: новая control plane, ключи и reconciliation без необходимости для одного стенда |

`production` environment нужен для изоляции deploy credentials и ограничения ветки master.
Required reviewers — только дополнительная будущая настройка с согласованием, не обещанная функция.

## Pipeline и release contract

1. PR: без secrets/write permissions; build → tests/linters → стабильный required check `CI / gate`.
   Никаких publish/deploy, `pull_request_target` и запуска untrusted кода на VM.
2. После squash: заново build **точного push SHA master**, затем проверки тех же artifacts.
   PR artifacts не становятся production release. Три Docker images: backend, frontend, runner.
   Frontend final image включает существующие `/` и `/product/`, не isolated prototype.
3. Build один раз на выбранную платформу VM; сохранить Docker archives и image IDs.
   Final backend устанавливает `pip install .` без test deps, final frontend — nginx:
   не обещать unit tests внутри production image. Unit tests/lint выполняются отдельно на том же SHA
   с теми же sources/lockfiles (frontend — в сохранённых build stages); final backend/runner/nginx
   проверяются runtime smoke. Для Python допускается временный дочерний test layer без изменения
   публикуемого image. После проверок только load/tag/push, без повторной сборки. Не копировать Docker-документированный multi-platform rebuild буквально.
4. Publish job только после успеха всех обязательных checks; `packages: write` только у него.
   До отдельного approval активации publish выключен, даже если package private.
   Имена: `ghcr.io/acrycxde/pp-sber-backend`, `pp-sber-frontend`, `pp-sber-runner`.
   Tag `sha-<40hex>` — удобный указатель, не гарантия immutability; истина — `@sha256:<64hex>`.
5. Release manifest — Actions artifact `release-<SHA>-<run_id>-<run_attempt>`, JSON schema 1:
   `repository`, `sha`, `workflow_run_id`, `run_attempt`, `platform`,
   `images.{backend,frontend,runner}.{repository,digest,image_id}`, `checks` (все required = success).
   Registry digest появляется после push; повторно pull по digest сверяет image ID с проверенным archive.
   Manifest публиковать только после всех push/verify. Частичный push не является release.
6. Deploy принимает только `release_run_id` и `release_sha`, не произвольные image/command/URL.
   Из GitHub REST проверяет repository, workflow path `.github/workflows/ci.yml`, event=push,
   head_branch=master, head_sha, completed/success, attempt, checks и unexpired artifact.
   SHA должен быть reachable из master. Archive извлекать без traversal/symlinks/выполнения файлов.
   Сверить schema/allowlisted repositories/digests и manifest с данными API. Нет manifest — отказ.
   Parser/checker берётся из доверенного текущего master; server wrapper provisioned отдельно,
   root-owned. Ни release artifact, ни selected old SHA не поставляют исполняемые deploy scripts.

## Docs-only и обязательный gate

Workflow не имеет верхнеуровневого `paths` filter: cheap classify + gate запускаются всегда.
Docs-only whitelist: только `docs/**/*.md`, `README.md`, `LINKS.md`, `LICENSE`,
`runner/README.md`, `frontend/README.md`, `web-product/README.md`, `web-demo/README.md`.
Все неизвестные пути, `.github/**`, Dockerfiles, manifests, lockfiles, prototypes и executable docs
считаются code. Rename проверяет оба имени; NUL-safe diff, удаления, пробелы и ошибка diff — fail closed.
Для PR diff base…merge SHA; для push полный before…after, не HEAD~1; missing base → code.
Gate принимает skipped heavy jobs только при доказанном docs-only; failure/cancelled всегда отказ.
Docs-only не build/publish/deploy. Deploy такого SHA не может незаметно взять старые образы:
отказ с объяснением; оператор может явно выбрать прежний verified release. Если с release SHA
до текущего master есть только разрешённые docs, это допустимо; если code/неизвестный путь — stale отказ.

## Доставка в k3s и security boundaries

Сохранить `EXECUTOR=cli`, PVC mounts `/app/backend/data` и `/home/node`, Secret refs,
service/ingress, resource strategies. Recreate backend: не допускать overlap writers;
startup migrations также требуют отдельной блокировки/maintenance gate.
Не применять Compose к k3s и не переносить `EXECUTOR=runner`/Docker socket из Compose.
Backend/frontend тянутся **нативным k3s containerd** из GHCR по digest;
`imagePullPolicy: IfNotPresent`, private `imagePullSecrets` в namespace `coreteams`.
Не нужны Docker, импорт в другой image store, cluster restart или `registries.yaml`.
Runner image проверяется/публикуется для отдельного runner runtime, но на CLI-стенд не доставляется.
Будущий переход на runner — отдельное решение с delivery в фактический executor runtime,
изоляцией, сетями и совместимостью; наличие image в containerd не создаёт Docker API.

GHCR first publish private по умолчанию; public repo не разрешает public package.
Private pull credential (PAT classic `read:packages`, с нужным package access) предоставляет пользователь
при активации; значение не читать/не писать в docs/logs. Не использовать publish token на VM.

Простой deploy transport: отдельный SSH user с forced command, `restrict`, без shell/forwarding/PTY;
GitHub передаёт bounded JSON по stdin root-owned wrapper `/usr/local/sbin/coreteams-deploy`.
Strict host key pinning, никаких `ssh-keyscan` в run. В GitHub **нет cluster-admin kubeconfig**.
Wrapper локально использует namespace-scoped Kubernetes identity: read rollout state и patch
только двух именованных Deployments; не secrets/PVC/roles/jobs/exec. Root provisioning отдельно.
Wrapper сам формирует image-only patches из allowlisted names/digests, не принимает manifests.
Patch Deployment может позволить запуск произвольного кода с доступами Pod; на небезопасном
hostPath/privileged Pod это потенциально root-equivalent. Forced command/RBAC не sandbox;
ручная проверка pod privileges, mounts и identity обязательна. Секреты контейнера всё ещё доступны коду.

Deploy concurrency: одна группа `production-deploy`, `cancel-in-progress: false`.
Не обещать FIFO: default queue может заменить pending run, active не отменяется.
На сервере `flock` на root-owned lock плюс persistent state `in_progress/succeeded/failed`,
release SHA/digests и предыдущий release. Потеря SSH не должна отпускать lock посреди apply:
wrapper ведёт операцию до terminal state; новый run при незавершённом state требует reconciliation.
Повтор одного release — проверяемый no-op. Ancestor старее уже deployed SHA — отказ обычного deploy.
После lock повторная GitHub проверка master/staleness; если master двинулся после проверки,
закончить уже начатый transaction, записать SHA, не объявлять его «последним master».

## Production preflight, миграция, rollback

CI улучшать можно независимо; deploy fail-closed до runtime и data evidence.
Обязательны actual VM CPU/platform smoke, CLI auth/network, новый диалог, ответ/stream,
resume после restart, материалы/artifacts, `/product/`, права и сохранность persisted state
на восстановленной копии. Отдельно сравнить CLI и runner необходимые функции; не менять executor
для обхода провала тестов. Не записывать токены и пользовательские данные в evidence.

**Каждый release, не только первый:** под server lock до image patch сравнить deployed SHA и candidate
через NUL-safe diff для `backend/migrations/**`, `backend/alembic.ini`, `backend/db.py`,
`backend/main.py`, `backend/migration_bridge.py` и schema contracts `backend/models.py`,
`backend/schemas.py`, `backend/repository.py`, а также dependencies в `pyproject.toml`.
При execution уточнить exact-path inventory startup/migration tooling; неизвестные пути считать schema-risk,
не делать application review в planning-подзадаче.
Изменение/rename/delete любого такого пути, неизвестный baseline или ошибка diff блокируют normal deploy.
Это консервативный gate, не автоматическое доказательство SQL compatibility. Для исключения требуется
отдельно provisioned server-managed maintenance clearance на **точный release SHA и фактическую schema
signature/target**, DB preflight и maintenance rehearsal; boolean из dispatch не является approval.
Сверить clearance и actual schema под lock до запуска backend; unknown initial baseline — блок до P.

Live DB schema не исследована; `docs/CANONICAL_MIGRATION.md` описывает collision fork revisions.
Перед первым переходом: остановка writers/schedulers, согласованный maintenance window,
backup DB + S3 + PVC/home/workspaces, off-node копия и проверенный restore/rehearsal.
Bridge/stamp не является deploy step; никаких automatic reverse bridge/stamp.
Миграция — отдельное approval на production data changes. До неё нельзя запускать новый backend,
если его startup может выполнить DDL. Незнакомая схема — stop, не принудительный upgrade.

Обычный rollback допустим только на сохранённый verified digest с совместимой схемой и runtime;
выполняется оператором под тем же lock, не обычным dispatch старого SHA.
После data migration image rollback не гарантирует rollback данных: stop writers, ручное решение
о restore DB/S3/PVC совместимого снимка, возможная потеря новых записей; никаких PVC delete.
До первого изменения сохранить текущие image bytes/config: старые локальные tags без provenance
не считать доступным GHCR rollback. При timeout/частичном rollout сохранять failure state и evidence,
не пытаться автоматически отменить миграцию; backend Recreate означает допустимый согласованный downtime.

## Ограничения проверки и источники

Навыки `security-review` и `reliability-review` недоступны: эти gates не запускались.
Перед активацией обязательны ручные gates security (permissions, injection, secrets, SSH/RBAC,
supply chain) и reliability (lock, stale/race, partial rollout, restore и schema compatibility).
При будущих Python изменениях вызвать `skill-issue-python:python-rules`; TypeScript plugin отсутствует,
его нельзя считать применённым. Прикладной Python/TS здесь не ревьюился и не изменялся.

Официальные интерфейсы проверены 2026-11-11; перед реализацией перепроверить версии и pin action SHA:
- [Dispatch/default branch](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#workflow_dispatch)
- [Concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
- [Runs REST](https://docs.github.com/en/rest/actions/workflow-runs), [Artifacts REST](https://docs.github.com/en/rest/actions/artifacts)
- [GHCR permissions, visibility, digests](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [Docker test-before-push](https://docs.docker.com/build/ci/github-actions/test-before-push/), [push](https://docs.docker.com/reference/cli/docker/image/push/)
- [Kubernetes imagePullSecrets](https://kubernetes.io/docs/tasks/configure-pod-container/pull-image-private-registry/)
