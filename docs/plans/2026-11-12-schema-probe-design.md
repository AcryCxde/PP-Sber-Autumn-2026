# Read-only PostgreSQL schema probe

Дата: 2026-11-12. **APPROVED пользователем 2026-11-12: local implementation + disposable tests only; no VM changes.**
Пользователь отдельно согласовал 2026-11-12 узкое уточнение SERIAL sequences ниже.
Текущий этап — только implementation plan; код и activation в этом задании не выполняются.

## Контекст и цель

Boundary: trusted server → PostgreSQL → `SchemaEvidence` → maintenance clearance → image patch.
`deploy_server.py` уже вызывает provider под deployment lock на risk-пути, но CLI его не подключает.
`Clearance` имеет ровно `sha/signature/target/preflight/rehearsal/approval`; формат не расширяем.
`migration_bridge._signature` теряет timezone, numeric precision/scale, index predicate;
`preflight_canonical` при canonical HEAD не сравнивает набор таблиц. Повторное использование недостаточно.
Это подтверждается чтением кода; переданное PG16 evidence здесь не воспроизводилось.
Миграции реально используют timestamptz, numeric(12,6), JSON, PK/FK/UNIQUE/CHECK и partial unique index
`uq_runs_one_active_per_conversation`. Нельзя ограничиться названиями таблиц и Alembic revision.

## Два минимальных варианта

1. **Рекомендуется: ограниченный pg_catalog fingerprint.** Один SQL statement возвращает весь
   поддерживаемый inventory + unsupported flags; Python только валидирует, сериализует и хеширует.
   Плюсы: один statement snapshot при READ COMMITTED, явные границы, нет парсинга dump DDL.
   Минус: полнота строго в объявленном профиле; новый объект требует review или блокируется.
2. **Pinned pg_dump --schema-only.** Точный binary/version/hash, no-owner/no-privileges/no-comments,
   одинаковые server/client versions и параметры; сравнивать консервативно, не сортировать SQL строки.
   Плюсы: гораздо шире поддержка DDL, меньше собственного catalog-кода. Минусы: служебные/version/random
   строки требуют узкой проверенной нормализации; сложные quoted identifiers/literals ломают regex;
   dump транзакция/snapshot и блокировки требуют отдельной проверки isolation-профиля.
   Нужен отдельный unsupported audit (например, invalid index status не заменяется CREATE INDEX DDL).
   Жизнеспособен, если нужна широкая поддержка объектов; для текущих миграций добавляет больше обвязки.

## Предлагаемый контракт v1

- Только PostgreSQL 16; точный протестированный minor/profile зафиксирован в baseline metadata.
  Иной профиль — отказ до review; не заявляем «полное описание любого PostgreSQL».
- Scope — все non-system schemas и их зависимости; не только видимые через information_schema объекты.
  Разрешённый набор schemas фиксируется baseline. Лишняя/пропавшая schema/table/column — mismatch.
- Поддержка: обычные permanent heap tables, built-in types, columns в логическом порядке,
  точный type identity + typmod + `format_type` (timezone/precision/scale/length), collation,
  nullability, default через pg_get_expr. Raw пользовательские OID не входят в переносимый hash:
  OID используются для joins, затем заменяются schema-qualified stable identities.
- PK/UNIQUE/FK/CHECK: имена, ordered columns, referenced schema/table/columns, match/update/delete,
  deferrability/deferred/validated/no-inherit; deparsed definitions без whitespace-нормализации literals.
- Btree indexes: имена, method, unique/nulls-not-distinct, key/include order, expressions/predicate,
  collations/opclasses, sort/null flags, options; invalid/not-ready/not-live — безусловный отказ.
  Неизвестный method/options — отказ, а не молчаливое исключение.
- Generated/identity columns и standalone/custom sequences v1 **запрещены**, обнаруживаются явно.
  Разрешены только sequences принадлежащие автоинкрементным SERIAL columns поддержанных tables:
  stable qualified sequence/table/column identities, точные type/start/increment/min/max/cache/cycle,
  ownership dependency `pg_depend` (`deptype=a`) и default/dependency `nextval` должны совпасть с baseline.
  Не читать и не хешировать current value/is_called; не выдавать sequence USAGE/SELECT probe role.
  Проверено на owned disposable PostgreSQL 16.15 (160015): actual migrations clean → `20261111_0005`
  создали ровно `public.events_id_seq → events.id` и `public.artifacts_id_seq → artifacts.id`,
  integer/start=1/increment=1/min=1/max=2147483647/cache=1/cycle=false, `deptype=a`;
  identity/generated пусты. Это inventory текущего checkout, не approved immutable release baseline.
  Views/materialized/foreign/partitioned/unlogged tables, inheritance, custom types/domains/collations,
  пользовательские functions/procedures, triggers/rules, RLS/policies, publications/subscriptions,
  event triggers, foreign servers и прочие неразрешённые user objects — отказ.
  Встроенные FK triggers/row types/TOAST разрешены только как проверенные производные поддержанных объектов.
  Extensions: только явно проверенный стандартный plpgsql профиль; иные extensions/зависимости — отказ.
  При реализации нужен закрытый список проверяемых catalog classes, не обещание общего unknown detector.
- Canonical JSON: versioned profile, stable qualified names, deterministic sorting/encoding,
  порядок columns/keys сохраняется; SHA-256 → `signature` с profile prefix, длина ≤512.
  Никаких OID/storage/statistics/sequence-current-values, application rows, owners/ACL/comments/secrets.
  Поэтому grants/ownership/security compatibility проверяются отдельно; DDL literals тоже не логировать.
- `target` — hash проверенной server-owned target mapping (TLS endpoint/cluster identity/database/schema
  scope/profile) и полученных DB/server identity facts. Проверить database/user/server version;
  database OID/name сами по себе не глобальная identity. Смена маршрута требует новой сверки оператора.

## Источник истины и исполнение

- Baseline получают **не из actual target**: на disposable PG16 прогнать reviewed canonical migrations
  точного immutable release SHA до `20261111_0005`, отдельно clean install и supported upgrade chain.
  Их fingerprint обязан совпасть; расхождение блокирует baseline. SQLite/create_all не эталон.
  Текущий checkout HEAD `8a6b5eb91cc92c211843048d18ac53d4f76fd74b` содержит uncommitted CI/CD work:
  он не считается автоматически утверждённым release/baseline SHA.
- Server-owned reviewed baseline связывает точный release SHA, profile, expected fingerprint и target;
  clearance signature обязана равняться **expected canonical**, затем live обязана равняться ей.
  Оператор не может узаконить missing table копированием live hash в clearance.
  Неизвестный fork требует отдельной maintenance/rehearsal, а не исключения probe.
- Trusted installed provider подключается к существующему CLI, не загружается из workflow/artifact.
  Config/imports/interpreter/baseline/root parents защищены как wrapper; credentials локальные, не в Actions.
  Настоящая отдельная non-owner/non-superuser role без DDL/DML/CREATE/TEMP/опасных memberships;
  только CONNECT/необходимое catalog чтение. Catalog visibility и deny DML/DDL проверяются тестами.
- Прямая защищённая DB route; BEGIN READ ONLY при READ COMMITTED, SET LOCAL search_path=pg_catalog,
  фиксированные deparse settings; один catalog SELECT, без пагинации и per-table запросов.
  Connect ≤5s, lock ≤1s, statement ≤5s, общий wall deadline ≤15s, output ≤4MiB; превышение — отказ.
- Окно без внешнего DDL от probe до rollout — обязательное operational условие.
  Deployment flock/cooperative maintenance lock не останавливает SQL другого администратора;
  snapshot даёт согласованное чтение, но не защиту от DDL после него. Без соблюдения окна deploy запрещён.
- Missing provider/config/baseline, unknown objects, mismatch, incomplete/oversized output, privilege/network
  error/timeout → отказ до Kubernetes patch. Не менять state на success; никаких retries с более слабыми правилами.
  Known UI-only/no-risk путь сохраняет существующее поведение без новой обязательной DB зависимости;
  здесь нет обещания проверки live drift на каждом UI deploy и нет расширения allowlist.

## Проверки и границы разрешения

Disposable реальный PG16: clean/upgrade equality; missing/extra tables при HEAD; timezone, numeric scale,
partial predicate, default, FK action/schema, CHECK validation/deferrability, UNIQUE и index include/expression/
collation/opclass/nulls/order; invalid index, unsupported extension/function/trigger/RLS/partition → отказ.
Quoted/newline/unicode names и literals не коллидируют; разные OID дают равный logical hash,
same-OID ALTER даёт drift; hostile search_path не влияет; extra schema/dependency не теряется.
Проверить отсутствие пагинации/truncation, readonly privilege denial, concurrent DDL/error policy,
timeouts/cancel/connection cleanup; fake server tests: exact SHA/target/baseline, zero patches при ошибке,
missing provider остаётся blocked, UI-only path и существующие 244 CI contracts не регрессируют.
Schema equality не доказывает корректность данных/миграции, backup, restore, RPO или runtime compatibility.
`security-review`/`reliability-review` skills отсутствуют; gates не запускались. Existing-code/Postgres/Python
skills загружены; изменений кода нет. Полный probe/drift suite здесь не запускался; прежние PASS — переданное
evidence. Ниже отдельно отмечено реально выполненное disposable clean migration/sequence inventory исследование.

**Оба approval получены 2026-11-12**, следующий документ — [implementation plan](2026-11-12-schema-probe.md).
A/P, live DB/SSH/GitHub writes, commit/push и production maintenance по-прежнему требуют отдельных разрешений.
Disposable inventory выше воспроизведён отдельно: read-only bind checkout в existing tooling image,
миграции меняли только новый owned PG container; catalog SELECT выполнен BEGIN READ ONLY, container удалён.
Остальные drift/security/reliability тесты ещё не запускались. Skills security-review/reliability-review
недоступны: до acceptance обязательны независимое ревью и явно записанные manual gates.
Источники проверены через curl: PostgreSQL 16 `catalog-pg-attribute`, `catalog-pg-index`, `app-pgdump`.
