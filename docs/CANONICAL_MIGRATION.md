# Canonical backend migration maintenance

Canonical history preserves master revisions `20261030_0011` (durable artifact confirmations) and `20261030_0012` (retry abandonment delivery). Product workspace, artifact storage and user activity are appended as `20261111_0001`, `20261111_0002`, `20261111_0003`; answer fingerprints follow in `20261111_0004` and selected materials in `20261111_0005`, the canonical target head.

The deployed fork reused `20261030_0011` for product workspace, `20261030_0012` for object storage and `20261030_0013` for user activity. **Never run `alembic stamp` to resolve this collision.** A revision identifier alone is not schema evidence.

## Preconditions

This procedure is a deliberate maintenance operation, not application startup behavior. Stop all backend/runner writers and schedulers. Take and verify a restorable database backup, and rehearse against a restored copy. Use a direct database connection with bounded operational lock/statement timeouts configured for the migration role. Keep object storage unchanged. Do not expose connection credentials in shell logs.

Canonical fresh/master databases use normal Alembic upgrade. Startup preflight checks the full expected master structural signature at either colliding ID; deployed or mixed schemas fail closed. Deployed revisions 0011/0012 must first be brought to deployed 0013 using the original deployed migration chain on a backed-up maintenance copy; the bridge intentionally refuses those incomplete sources.

## Deployed 0013 bridge

Set `DATABASE_URL` through the environment without printing it. Use the project's installed backend dependencies.

Read-only preflight:

```sh
python -m backend.migration_bridge --source deployed-20261030-0013
```

Only after verifying the backup and stopping writers:

```sh
python -m backend.migration_bridge --source deployed-20261030-0013 --apply --backup-verified
```

The tool verifies tables, columns, type affinities/lengths, nullability/defaults, primary/foreign keys, unique constraints, indexes and check constraints against reconstructed historical migrations. Unexpected, partial or mixed schemas abort before DDL. The bridge creates only the missing canonical confirmation schema using the original master revision operations, validates the complete canonical target, then updates the version row in the same transaction. It does not rewrite product rows or storage objects. Preserve its output as historical evidence: source fork, original revision `20261030_0013`, target `20261111_0005`, and preflight/applied status. This mapping does not falsely claim that deployed IDs meant canonical confirmations.

A repeated bridge invocation fails clearly with `Already migrated`; use normal canonical upgrades thereafter. Do not run bridge concurrently. On failure stop, inspect the transaction outcome and restore the verified backup before any retry. No automatic downgrade or reverse mapping is supplied.

## Verification performed locally

SQLite tests cover canonical/deployed 0011/0012 disambiguation, partial schema rejection, preflight, backup acknowledgement, apply and rerun refusal. A disposable local PostgreSQL 16 container additionally covers fresh/master/deployed DDL and rollback after an injected post-DDL failure. These are local test databases, not production writes. Test invocation:

```sh
PPS_TEST_POSTGRES_URL='<local disposable asyncpg URL>' python -m pytest backend/tests/test_migration_bridge.py -q
```

A real deployment rehearsal with the actual backup and S3 service remains an operator gate; no deployment was performed.
