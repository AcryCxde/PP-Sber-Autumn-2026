# CI/CD: эксплуатационный runbook
Статус на 2026-11-15: **ACTIVATION выполнена; FIRST DEPLOY success**.

## Deploy одной кнопкой: кандидат на активацию

После установки проверенного серверного обработчика: **Actions → Deploy → Run workflow → `master`**, без параметров. Merge не запускает выкладку. Deploy проверяет manifest успешного `push` CI для точного SHA запуска; чужой, устаревший или ещё не опубликованный релиз отклоняется. Новый протокол не обслуживается старым обработчиком.

Первый автоматизированный переход — `20261111_0005 → 20261112_0001` для согласованного снимка `master`, включающего `76fb599`. Новые схемы и последующие релизы этот обработчик без отдельно проверенного маршрута не принимает. Резервная копия для отката содержит БД, home и data; пользовательские S3-объекты при выкладке не изменяются. Зашифрованная копия хранится вне PVC и в отдельном бакете `coreteams-deploy-backups` того же S3. Успешный PUT не заменяет чтение назад и проверку изолированного восстановления. **Потеря узла не покрывается.**

До активации проверить production-подключения к k3s, PostgreSQL и S3, допуск на точный опубликованный SHA и готовность проверяемого пути восстановления. Настройки и секреты остаются на сервере. После допустимой ошибки до DDL проверяется прежняя схема и восстанавливается старый выпуск; после возможного DDL — никаких повторов, downgrade или старта прежнего backend вслепую. Подробности прежнего выпуска и legacy-команды ниже сохранены как история.

Серверный `rollout-installed.json` теперь требует в секции `smoke` поля `sessions`
(абсолютный путь к отдельному root:root 0600 JSON) и `private_http_trusted: true`.
Файл сессий содержит ровно `admin_session` и `user_session`: разные действующие
43-символьные cookies `coreteams_session` существующих администратора и обычного
пользователя. Оператор получает и ротирует их отдельно; не помещать значения в
Actions, конфигурацию runtime, argv, env или логи. Доверие private HTTP задавать
только после проверки серверного сетевого пути: частный IP сам по себе его не
подтверждает. До открытия frontend выполняются авторизованные GET сценариев и
обзора админки, проверяется форма ответов и отказ обычному пользователю в доступе
к админке. Публичные HTTPS-проверки cookies не отправляют. Отсутствие сессий
блокирует запуск до остановки сервисов; истёкшие сессии блокируют успешный smoke.

Перед остановкой writers обработчик связывает `home`, `data` и исключаемый
`s3_pvc` с фактическими Deployment/Pod mounts, PVC/PV и узлом. Привязка повторно
проверяется перед копированием и DDL. Проверка восстановленной БД сравнивает
каталог схемы/индексов, SHA256 содержимого шести критичных таблиц и состояние
последовательностей, включая `is_called`; содержимое строк наружу не выводится.
Более 100000 строк в одной секции проверки блокируют выпуск.

Резервная копия ограничена **192 MiB полного несжатого tar** БД/home/data и
требует **не менее 2 GiB MemAvailable**. Размер и доступная память проверяются
до остановки сервисов и перед копированием; память проверяется также перед
расшифровкой. CMS-шифрование потоковое, но декодер OpenSSL буферизует данные,
поэтому лимит архива сохраняется. Рост набора или нехватка памяти дают отказ;
увеличение лимита требует отдельной проверки декодера. Это не резервирование
памяти от других процессов узла.

Maintenance identity требует `get/list/watch` именованных Deployment
`backend`, `frontend`, `db` для ожидания готовности после масштабирования;
`list/watch` с ограничением `resourceNames` проверяются с field selector
`metadata.name`. Права изменения образов нужны только backend/frontend,
масштабирования — backend/frontend/db. Readiness не должна зависеть от
cluster-admin или расширения старой image-patch identity.

## Первый выпуск: историческая конфигурация и процедура

На стенде установлен `bc10c7e05d6fc6e9965b0bc01f3d8ed30ca16874`:
- [CI 43583117140, attempt 2](https://github.com/AcryCxde/PP-Sber-Autumn-2026/actions/runs/43583117140) — все checks и publish **success**;
- [Deploy 43695918687](https://github.com/AcryCxde/PP-Sber-Autumn-2026/actions/runs/43695918687) — **success**, один ручной dispatch, server state `succeeded`;
- backend: `ghcr.io/acrycxde/pp-sber-autumn-2026-backend@sha256:f449aeb3a2de50bcde67fcd952cf6ad647be7052637a81418fba339229514a75`;
- frontend: `ghcr.io/acrycxde/pp-sber-autumn-2026-frontend@sha256:aeff030a53548827e658d2e22eb94403a60ae91c71fb54a0b254e85616646c3f`.

**Обычный выпуск:** PR → обязательный CI → merge в `master` → CI build/checks/publish →
**Actions → Deploy → Run workflow**, branch `master`, inputs `release_run_id` и полный
`release_sha` из успешного master-push CI. Deploy **необязательный и только ручной**;
merge сам стенд не обновляет. Обе repository variables `CICD_PUBLISH_ENABLED` и
`CICD_DEPLOY_ENABLED` сейчас `true`; это не добавляет автоматического trigger Deploy.

Перед кнопкой проверить release artifact, SHA/attempt, состояние сервера и необходимость
maintenance. Docs-only merge не создаёт release: прежний release допустим только при
проверенной docs-only разнице до master. Устаревший/истёкший artifact не заменять rebuild
во время deploy. Срок текущего artifact: **2026-11-21T12:59:13Z**, имя
`release-bc10c7e05d6fc6e9965b0bc01f3d8ed30ca16874-43583117140-2`.

**Следующий backend/unknown-code/schema-risk релиз не получает допуск автоматически.**
Baseline/clearance привязаны к точному SHA, profile и target; при необходимости подготовить
новые доказательства и maintenance. Кнопка Deploy не исполняет произвольные миграции.
Не копировать clearance первого выпуска на другой SHA. Failure/rollback — §6.

### Доказательства первого перехода

- Испытанный bridge: `20261030_0013` → `20261111_0005`; количество строк всех 15 исходных
  таблиц сохранено. Production read-only schema-probe после перехода — **PASS**.
- Полный baseline: clean + 17 canonical revisions + deployed fork, все **19** сценариев
  дали тот же fingerprint, что и production:
  `pg_catalog_v1:sha256:313c225564958f76bd2eb3fe8613b01703deea9805dfb7f598f2e8c4f36ec584`.
- Предварительные recovery tests: isolated PostgreSQL boot/schema/full dump; S3 4.50
  List/Get всех 10 объектов и контрольные суммы; home/data archives compare — **PASS**.
- Остановка до Ready frontend — около **5 мин 1 с**; полная приёмка — **376,8 с**.
  Все backend/frontend/db/s3 — Ready 1/1; `/` и `/product/` — HTTP 200.
- Synthetic register/login/me/history/logout — **PASS**, anonymous auth — 401.
  Новые модельные ответы и создание/скачивание артефактов через UI **не проверялись**.
  Synthetic smoke-account оставлен; это не доказательство полного модельного E2E.
- Image pull policy/Secret references подготовлены в согласованном первом окне;
  `EXECUTOR=cli` сохранён. Runner image на этот стенд не выкатывается.
  Страховочный recovery timer отключён, собственные тестовые workloads убраны.

### Установленная конфигурация и ротация

Trusted launcher `/usr/local/sbin/coreteams-deploy` использует `/usr/bin/python3.13 -I -S -B`,
`/opt/coreteams-deploy/runtime` и `/opt/coreteams-deploy/deps-asyncpg-0.30.0`.
Изолированный asyncpg **0.30.0**, cp313 x86_64 wheel SHA256
`46973045b567972128a27d40001124fbc821c87a6cade040cfcd4fa8a30bcdc4`;
system Python packages не менялись. Direct entrypoint — `deploy_credentials.main`.

Файлы `/etc/coreteams-deploy/{github-token,kubeconfig.json,schema-probe.json,
schema-probe-credential.json,schema-baseline.json,clearance.json}` — root-owned 0600.
Не выводить содержимое в отчёты. Namespace pull Secret — `coreteams-ghcr-pull-a3`;
SA/Role/RoleBinding — `coreteams-cicd-a3`, только get/watch/patch backend/frontend.
Реальным credential подтверждены запреты Secrets, exec, unrelated Deployment, PVC delete и RBAC.
Dedicated SSH user/key restricted; значения четырёх production secrets не читаются.

- Kubernetes token истекает **2027-02-12T19:35:29Z** — rotate заранее.
- TLS server certificate истекает **2027-02-12T21:08:32Z** — rotate заранее.
- Текущая исправленная CA истекает **2027-11-15T08:35:00Z**; CA keyUsage исправлен для
  строгого Python 3.13, проверки CA/hostname не отключались. Материалы —
  `/etc/coreteams-deploy/tls-a3`, server cert/key — PGDATA `tls-a3`.
- Сроки PAT не подтверждены: оператор сверяет expiration в GitHub UI и ротирует вручную.
  Metadata owner PAT, server verifier и GHCR pull — разные credentials; не заменять их широким token.
- Read-only probe не имеет TEMP; database owner приложения сохранил TEMP. Исходный ACL/undo
  сохранён в защищённом evidence. Не расширять права probe ради успешного результата.

### Проверенные точки восстановления

Старый rehearsal backup: `/var/backups/coreteams/maintenance-20261114T210433Z`.
Свежая **предпереходная** cold-копия четырёх PVC и конфигурации:
`/var/backups/coreteams/firstdeploy-20261115T084101Z` (0700), с `SHA256SUMS.json`,
исходными Deployment specs/volume map, `PRODUCTION_BRIDGE.private.json` и `SUCCESS`.
Это согласованная точка до migration/rollout, не непрерывный backup новых записей после выпуска.

Её внешняя копия на Mac проверена **2026-11-15**:
- `/Users/romanov/.ssh/coreteams-offhost-firstdeploy-20261115T090347Z/firstdeploy-backup.cms`;
- **106 795 324 bytes**, CMS AES-256-GCM, SHA256 ciphertext
  `1552d985aa2c5a4a1664e08b05a9ac93bd65056af13925a9dc3ca7521d121450`;
- decrypt-stream SHA256 совпал с исходным архивным потоком; локальный plaintext не сохранялся;
- recipient cert/key — защищённый каталог
  `/Users/romanov/.ssh/coreteams-offhost-20261114T212256Z/`, private key `backup-recipient.key` 0600.
  Private key не передавался на сервер. Ciphertext и key пока на одном Mac: отдельное хранение
  ключа, автоматические retention/rotation и резервирование самого Mac **не настроены**.

Ниже сохранён исходный checklist. Выполненные пункты отмечены; исторические результаты
и неиспытанные сценарии не выдаются за новые проверки.
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
- [x] **A2 / GITHUB**: protection/environment/private GHCR publish активированы после отдельных
  согласований пользователя 2026-11-14/15; успешный publish/master CI и Deploy приведены выше.
- [x] **A3 / VM**: restricted SSH/RBAC/pull Secret/read-only DB role/mapping установлены;
  permissions, TLS и actual probe проверены. Историческое ограничение только read-only снято
  отдельными разрешениями на provisioning и maintenance, не самим наличием доступа.
- [x] **P / FIRST DEPLOY**: пользователь отдельно согласовал финальный пакет; bridge,
  schema clearance и первый rollout выполнены 2026-11-15. Это не blanket approval следующих
  maintenance/data changes. Наличие credentials само по себе не является разрешением.
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
## 2. Историческое evidence 2026-11-11/12 (не текущий статус)

Следующие LOCAL/pending/unverified формулировки описывают состояние до активации.
Текущие live результаты и ограничения приведены в начале документа.
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
## 3. ACTIVATION checklist — для новой установки/reprovisioning

На текущем стенде A2/A3 завершены, оба flags включены. Требования и запреты этого
раздела сохраняются для новой установки; инструкции «оставить off» относятся к её
подготовке, не к фактическому состоянию уже активированного стенда.
- [ ] Перепроверить repository/master/protection/rulesets/merge methods/environments/flags.
  Согласовать PR-only master, squash-only merge, required checks и admin bypass policy.
- [ ] На **реальном PR нового CI** проверить success/failure/docs-only/cancelled пути gate.
  Предлагаемый UI label — `CI / gate`, YAML job — `gate`; protection context взять из
  фактического check-run `name` и GitHub App ID, а не скопировать UI label наугад.
  Затем проверить effective protection и merge blocking. До этого required gate UNVERIFIED.
- [x] Environment `production`: selected deployment branch **только master**, не wildcard tags;
  optional required reviewers — отдельное согласование и проверка доступности функции.
- [x] Secrets только environment `production`: `CICD_DEPLOY_HOST`, `CICD_DEPLOY_USER`,
  `CICD_DEPLOY_SSH_KEY`, `CICD_DEPLOY_KNOWN_HOSTS`. Host key сверить независимым каналом;
  не `ssh-keyscan` в workflow. Не читать/печатать существующие значения.
- [x] Обязательная **environment variable** `production`: `CICD_DEPLOY_PORT`.
  Только canonical ASCII decimal `1..65535`: без leading zeros, знака, whitespace или выражений.
  Missing/invalid → отказ до SSH и создания credential files; default `22` отсутствует.
  Например, `2297` — только пример конфигурации, не подтверждение live SSH route.
  Для этого примера `CICD_DEPLOY_KNOWN_HOSTS` должен pin-ить `[host]:2297`
  (вместо `host` — согласованный `CICD_DEPLOY_HOST`) с независимо проверенным host key.
  Настройка variable требует отдельного **A2**, настройка/изменение SSH route — **A3**;
  здесь ничего не настраивать и не активировать, deploy flag оставить off до P.
- [ ] `CICD_PUBLISH_ENABLED` и `CICD_DEPLOY_ENABLED` — **раздельные repository variables**.
  До A обе отсутствуют/не `true`; publish разрешить отдельно после A, deploy оставить off до P.
  Deploy verify job вне environment, поэтому environment-only flag ему недоступен.
- [ ] Private GHCR packages фактического release contract:
  `ghcr.io/acrycxde/pp-sber-autumn-2026-backend`, `...-frontend`, `...-runner`.
  Реализация использует tag `<40hex>`, не `sha-<40hex>`; истина — `@sha256:<64hex>`.
  Это расхождение с ранним дизайном сверить до A; не создавать packages по старым именам.
  Publish только master push после всех checks, не manual CI/PR/docs-only; no rebuild.
- [ ] До app publish отдельно bootstrap-ить все три exact private packages и настроить
  package access для репозитория. Guard не создаёт packages: missing/403/сетевая ошибка,
  public/internal или неверная metadata блокируют **все** push. После publish повторная
  проверка visibility блокирует release.json при drift, но не отменяет уже сделанные push.
- [ ] Разделённый auth contract: metadata использует **repository Actions secret**
  `CICD_GHCR_METADATA_OWNER_READ_TOKEN` → step env `GHCR_METADATA_OWNER_TOKEN`.
  Это отдельный classic PAT владельца **AcryCxde**, только `read:packages`, без
  `repo`, `write:packages`, `delete:packages`; не текущий широкий личный token.
  Через GitHub UI оператор создаёт ограниченный credential с expiration и добавляет
  secret, не передавая значение в чат, argv, manifests или logs. Secret доступен
  только trusted publish step и metadata-only preflight step; PR jobs его не получают.
  Owner login проверяется в каждой exact metadata; чужой owner → отказ.
  OCI login/push/pull сохраняют `github.token` (`GHCR_TOKEN`, publish `packages: write`);
  metadata credential исключён из Docker subprocess env и login, не используется для OCI.
- [ ] Metadata route: `GET /user/packages/container/<exact-name>`, не public user route,
  без fallback/listing/retries. До первого upload все три exact packages обязаны быть
  private; absent secret/403/404/network/malformed/owner mismatch → closed failure.
  Повторная проверка после push сохраняется; drift блокирует manifest, не откатывает uploads.
  Диагностика: только exact package, нормализованный class и HTTP status (`none`, если нет).
  Источник owner route/read scope: [GitHub REST Packages](https://docs.github.com/en/rest/packages/packages#get-a-package-for-the-authenticated-user),
  предоставленный snapshot `REST API endpoints for packages - GitHub Docs.mhtml`, API
  **2026-03-10**. Код сохраняет **2022-11-28**; её фактическая совместимость проверена
  [metadata preflight 43587379730](https://github.com/AcryCxde/PP-Sber-Autumn-2026/actions/runs/43587379730)
  и успешным publish CI attempt 2. Offline tests сами по себе этого не доказывают.
- [ ] `.github/workflows/package-metadata-preflight.yml`: manual metadata-only,
  `permissions: {}`, master only, без checkout/artifacts/build/login/upload;
  secret bearing job не исполняет код release artifacts. Dispatch возможен после
  separately approved review/merge в default branch. **API проверки и GitHub CI/dispatch
  требуют отдельного approval**, здесь не выполнялись. Оба activation flags остаются off.
### Private GHCR bootstrap — процедура для отсутствующих packages

**Выполнено 2026-11-14:** exact packages созданы private с inert marker и source label,
репозиторий связан, пользователь подтвердил Actions Admin для всех трёх. Фактический tag —
`bootstrap-private-20261114T103323Z`, не release SHA. Initial-private контракт подтверждён
предоставленным официальным MHTML «Working with the Container registry»: “When you first
publish a package, the default visibility is private.” Старый STOP ниже снят для этого
исполнения, не является текущим blocker. Следующую процедуру не повторять поверх packages.
Фактически использована существующая owner `gh` авторизация через защищённый pipe и
временный `DOCKER_CONFIG`; новый bootstrap PAT не создавался, credential не установлен в CI/VM.
Marker включал один несекретный текстовый файл, без application bytes. Рекомендации ниже
описывают первоначальный план, а не точный протокол уже выполненного bootstrap.

1. Получить отдельное разрешение на создание **ровно трёх** packages и package settings.
   `CICD_PUBLISH_ENABLED` и `CICD_DEPLOY_ENABLED` оставить отсутствующими/не `true`.
2. **STOP перед первым push:** в предоставленном REST MHTML не найдено доказательства
   default-private creation contract GHCR. До подтверждения официальным источником
   GitHub, что **первый push нового container package создаёт private package**, push
   запрещён: последующий перевод public → private уже не обеспечивает no-public-upload.
   Кандидат для независимой проверки:
   [Working with the Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#pushing-container-images).
   Это ссылка для проверки, не подтверждённый здесь source default-private утверждения.
   Если контракт не подтверждён — остановиться и запросить помощь, guard не обходить.
3. Только после source + approval оператор использует **отдельный** краткоживущий
   bootstrap credential с минимальным `write:packages` (и требуемым registry `read:packages`),
   не metadata secret, не существующий широкий личный token. Значение вводится по
   protected stdin в Docker login; не в чат, shell history, argv или git. Отдельный
   временный `DOCKER_CONFIG`, logout и удаление config после работ; не выдавать bootstrap
   credential CI или серверу. Создание credential/settings отдельно согласовать через UI.
4. В новом пустом временном каталоге подготовить inert marker Dockerfile — **только**:

   ```dockerfile
   FROM scratch
   LABEL org.opencontainers.image.description="Private package bootstrap marker; not a release"
   ```

   Без COPY/ADD/RUN/CMD/ENTRYPOINT/application bytes. После всех STOP gates собрать marker
   и push **только tag `bootstrap-private-v1`**, не release `<40hex>`/latest, по трём targets:
   - `ghcr.io/acrycxde/pp-sber-autumn-2026-backend:bootstrap-private-v1`
   - `ghcr.io/acrycxde/pp-sber-autumn-2026-frontend:bootstrap-private-v1`
   - `ghcr.io/acrycxde/pp-sber-autumn-2026-runner:bootstrap-private-v1`

   Bootstrap marker не запускать, не deploy-ить и не включать в release manifest.
5. После каждого push оператор в package UI подтверждает owner, exact name и **Private**;
   неожиданная visibility → STOP, не продолжать остальные push. В Package settings →
   Manage Actions access добавить `AcryCxde/PP-Sber-Autumn-2026` с **Write** для штатного
   `github.token` publish (включает pull). Одной repository linkage недостаточно;
   проверить effective Actions access. Официальная процедура UI:
   [Configuring access control and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#ensuring-workflow-access-to-your-package).
6. Через repo Settings → Secrets and variables → Actions установить вышеописанный
   **read-only owner** secret. После отдельного API/CI approval запустить master-only
   metadata preflight; все три private exact packages должны пройти до разрешения
   app publish. Это ещё не доказательство OCI upload. Flags не включать автоматически;
   publish activation и первый deploy требуют отдельных решений. Никаких server changes.

- [ ] Проверить private visibility/access для каждого package, в том числе ранее существовавшего.
  Пользователь предоставляет credential **только read:packages** с package access для pull;
  publish token не устанавливать на VM. Namespace Secret не хранить в git/logs.
- [ ] Отдельный SSH user/key: forced command `/usr/local/sbin/coreteams-deploy`, `restrict`,
  без shell/PTY/agent/X11/port forwarding/user rc. Запрет обхода forced command через sudo/env.
  Root-owned trusted wrapper, Python interpreter, imports и parent directories; sanitized env,
  фиксированный PATH, отсутствие writable import paths, arbitrary command/manifest arguments.
  Root-owned `/var/lib/coreteams-deploy` (state/lock) и `/etc/coreteams-deploy` (config);
  API credential read-only для нужных contents/actions, не в stdout и не из request JSON.
### Production credential bootstrap — активирован на текущем стенде

Установка credentials, isolated driver и restricted route выполнена; актуальный launcher
и paths приведены выше. Пример ниже — исторический шаблон, не команда для замены текущей
установки. Missing/unsafe credential по-прежнему приводит к закрытому отказу.

Отдельный entrypoint `tools.cicd.deploy_credentials`; обычный
`tools.cicd.deploy_server` сохраняет прежний default. Будущий root launcher после
отдельного review установки должен запускать **ровно этот entrypoint**, например
(не выполнять сейчас; absolute Python path сначала независимо сверить на сервере):

```sh
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/python3.13 -I -S -B -c 'import sys; sys.path.insert(0, "/opt/coreteams-deploy/runtime"); from tools.cicd.deploy_credentials import main; raise SystemExit(main())'
```

Interpreter, launcher, runtime/imports и все parents должны быть root-owned и
недоступны на запись другим; root-only state/lock и существующие schema gates
остаются обязательными. Это **не installer**, не sudo/key policy и не dry-run.
Уже установленный INERT launcher не переключается автоматически; credentials не
установлены, активация/RBAC/pull/rollout здесь не выполняются.

Только protected channel оператора, не CLI args/SSH env/request/logs:

- `/etc/coreteams-deploy/github-token`: ASCII token, опционально один завершающий
  LF, не более 16384 bytes. Root-owned regular file, **точно 0400 или 0600**.
  Endpoint фиксирован существующим API: `https://api.github.com`, только repo
  `AcryCxde/PP-Sber-Autumn-2026`, redirects с token запрещены. Read-only
  contents/actions permission и отсутствие write permissions — внешняя policy,
  адаптер не может вывести scope из token. Не использовать локальный широкий token.
- `/etc/coreteams-deploy/kubeconfig.json`: UTF-8 JSON (не YAML), до 16384 bytes,
  те же ownership/mode. Только следующая точная форма, без дополнительных ключей;
  CA/token заменить по protected channel. CA — base64 cluster CA, token — отдельная
  scoped service-account identity, не root kubeconfig; срок действия обслуживает оператор.

```json
{
  "apiVersion": "v1",
  "kind": "Config",
  "clusters": [{"name": "coreteams", "cluster": {
    "server": "https://127.0.0.1:6443",
    "certificate-authority-data": "BASE64_CLUSTER_CA"
  }}],
  "users": [{"name": "coreteams-deploy", "user": {"token": "SCOPED_TOKEN"}}],
  "contexts": [{"name": "coreteams-deploy", "context": {
    "cluster": "coreteams", "user": "coreteams-deploy", "namespace": "coreteams"
  }}],
  "current-context": "coreteams-deploy"
}
```

Оба файла читаются до API/deploy. Missing/unsafe/malformed credential → отказ,
никакого fallback к `/root/.kube/config`. Все parents root-owned, без group/world
write, symlinks запрещены на каждом компоненте (descriptor-based validator).
Запрещены exec/auth-provider, external CA/client-key paths, proxy, insecure TLS,
impersonation и иной endpoint/context. Существование локального k3s API и matching
CA/SAN нужно проверить отдельно до установки. kubectl получает фиксированные
`--kubeconfig=/etc/coreteams-deploy/kubeconfig.json --context=coreteams-deploy`,
namespace `coreteams`; env содержит только fixed PATH, HOME `/var/empty` и
KUBECONFIG. Caller GH_TOKEN/KUBECONFIG/proxy/CA env не используются; содержимое
credentials не печатается и не передаётся в argv. Между validation и kubectl файл
могут менять только доверенные root operators: не заменять его во время deploy.

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
- [x] Schema adapter установлен; production mapping/baseline/read-only role provisioned и
  проверены полным catalog probe после согласованного bridge первого выпуска.
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
  Profile строго PG `160015`, `pg_catalog_v1`, inventory 2 (default collation), SHA256 installed `schema_catalog.sql`;
  это узкий SERIAL/heap/btree profile, не универсальный SQL proof. Standard builtin trust,
  unsupported/external DDL → отказ. Production compatibility для `bc10c7e` подтверждена
  полным fingerprint выше; это не допуск иной будущей схемы.
Mapping JSON shape (только placeholder-схема, не готовый production config):
```json
{"schema":1,"endpoint":"<TLS DNS>","port":5432,"ca_file":"<absolute CA path>",
 "cluster_identity":"<reviewed identity>","database":"<DB>","role":"<read-only role>",
 "schemas":["public"],"profile":{"name":"pg_catalog_v1","server_version_num":160015,
 "inventory_version":2,"query_sha256":"<64hex installed SQL SHA256>","settings":{
 "search_path":"pg_catalog","standard_conforming_strings":"on","TimeZone":"UTC",
 "DateStyle":"ISO, YMD","IntervalStyle":"postgres","extra_float_digits":"3"}}}
```
Baseline генерирует [offline CLI](../../tools/cicd/schema_baseline.py) из exact approved SHA,
не из dirty working tree/production DB. Он исполняет reviewed migration/bridge code этого SHA
на собственном disposable pinned PG16.15 Docker container (cached image, loopback trust),
сверяет clean и все поддержанные upgrade paths; это локальные DDL, не production maintenance.
Approved SHA первого выпуска — `bc10c7e05d6fc6e9965b0bc01f3d8ed30ca16874`;
для другого релиза сначала отдельный commit/PR/CI/review/merge и baseline evidence.
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
## 5. FIRST DEPLOY checklist — выполненный P и требования следующего перехода

Первый P выполнен 2026-11-15, evidence/ограничения в начале документа. Checklist ниже
сохранён как процедура будущего перехода, не как сообщение о текущей блокировке.
Полный model/artifact UI E2E из rehearsal-пункта не заявлен выполненным: проверены recovery,
schema и auth/history smoke. Для будущего выпуска согласовать соответствующий scope заново.
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
