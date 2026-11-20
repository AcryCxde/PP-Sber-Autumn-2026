"""One reviewed PostgreSQL maintenance transition; NOT a production installer.

The host owns the deployment lock, verified writer quiescence, usable cold backup,
protected approval/configuration, read-only catalog connection, and a dedicated
least-privilege psql migration connection. None are acquired by this module.
Catalog inspections use the existing non-owner probe role, never an elevated role.
They surround (not share) the migration transaction, so quiescence must remain in
force throughout. A failed/ambiguous apply or post-commit mismatch requires the
outer durable maintenance journal to block rollout/retry, including after restart.
"""

import hashlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from tools.cicd.schema_probe import (
    Baseline,
    Facts,
    ProbeError,
    Snapshot,
    TargetMapping,
    canonical_signature,
    target_signature,
)

SOURCE_REVISION = "20261111_0005"
TARGET_REVISION = "20261112_0001"
PROCESS_SECONDS = 60

# Reviewed equivalent of 20261112_0001_admin_ai_access.upgrade(), rendered on
# PostgreSQL, with explicit schema qualification and the revision guard added.
# No SQL, path, command, or procedure identifier is accepted from release JSON.
PROCEDURE = b"""BEGIN;
SET LOCAL search_path = pg_catalog, public;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
SET LOCAL idle_in_transaction_session_timeout = '30s';
LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE;
DO $maintenance$
BEGIN
    IF (SELECT count(*) FROM public.alembic_version) <> 1
       OR (SELECT min(version_num) FROM public.alembic_version)
          IS DISTINCT FROM '20261111_0005' THEN
        RAISE EXCEPTION 'maintenance source revision refused';
    END IF;
END
$maintenance$;
ALTER TABLE public.users ADD COLUMN ai_frozen BOOLEAN DEFAULT false NOT NULL;
ALTER TABLE public.users ADD COLUMN ai_policy_version INTEGER DEFAULT '0' NOT NULL;
ALTER TABLE public.users ADD CONSTRAINT ck_users_ai_policy_version CHECK (ai_policy_version >= 0);
ALTER TABLE public.runs ADD COLUMN admitted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE public.runs ADD COLUMN admission_policy_version INTEGER;
ALTER TABLE public.runs ADD COLUMN admission_attempt_id VARCHAR(36);
ALTER TABLE public.runs ADD COLUMN harness_estimate JSON;
CREATE INDEX ix_runs_started_id ON public.runs (started_at, id);
CREATE TABLE public.ai_policy_audits (
    id VARCHAR(36) NOT NULL,
    actor_id VARCHAR(36) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    action VARCHAR(8) NOT NULL,
    reason TEXT,
    before_version INTEGER NOT NULL,
    after_version INTEGER NOT NULL,
    idempotency_key VARCHAR(64) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    response JSON NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_ai_policy_audits_actor_key UNIQUE (actor_id, idempotency_key),
    CONSTRAINT ck_ai_policy_audits_action CHECK (action IN ('freeze', 'unfreeze')),
    CONSTRAINT ck_ai_policy_audits_versions CHECK (before_version >= 0 AND after_version = before_version + 1),
    CONSTRAINT ck_ai_policy_audits_key CHECK (length(idempotency_key) BETWEEN 1 AND 64),
    CONSTRAINT ck_ai_policy_audits_reason CHECK ((reason IS NULL AND action = 'unfreeze') OR (reason IS NOT NULL AND length(reason) <= 1000 AND (action = 'unfreeze' OR length(trim(reason)) > 0))),
    FOREIGN KEY(actor_id) REFERENCES public.users (id) ON DELETE RESTRICT,
    FOREIGN KEY(target_id) REFERENCES public.users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_ai_policy_audits_target_created_id ON public.ai_policy_audits (target_id, created_at, id);
UPDATE public.alembic_version SET version_num = '20261112_0001' WHERE version_num = '20261111_0005';
COMMIT;
"""
# Deliberately literal: changes to installed bytes require separate review.
PROCEDURE_SHA256 = "3be29ddbb47695e55539ed23a1e74b0da8c919a6e70d663543955402e64a39b5"


class MigrationRefused(ValueError):
    """No successful target proof; callers must not start/roll back workloads."""


def _require(condition: bool) -> None:
    if not condition:
        raise MigrationRefused("maintenance schema transition refused")


@dataclass(frozen=True)
class ApprovedTransition:
    """Trusted installed configuration, never parsed from the Actions request.

    Source fingerprint is independently reviewed evidence, not the first live
    snapshot. The target Baseline must be bound to the exact approved release
    SHA (which may follow 76fb599 while preserving this reviewed transition).
    """

    source_revision: str
    source_signature: str
    baseline: Baseline
    mapping: TargetMapping
    target_identity: str
    procedure_sha256: str

    def __post_init__(self) -> None:
        _require(
            type(self.baseline) is Baseline and type(self.mapping) is TargetMapping
        )
        self.baseline.bind(self.baseline.release_sha, self.mapping)
        _require(self.source_revision == SOURCE_REVISION)
        _require(self.baseline.migration_head == TARGET_REVISION)
        _require(self.mapping.schemas == ("public",))
        _require(
            type(self.source_signature) is str
            and re.fullmatch(
                r"pg_catalog_v1:sha256:[0-9a-f]{64}", self.source_signature
            )
            is not None
        )
        _require(self.source_signature != self.baseline.expected_signature)
        _require(
            type(self.target_identity) is str
            and re.fullmatch(r"pg_target_v1:sha256:[0-9a-f]{64}", self.target_identity)
            is not None
        )
        _require(self.procedure_sha256 == PROCEDURE_SHA256)


@dataclass(frozen=True)
class PhysicalContinuity:
    """Installed observer's fresh Pod/PVC and physical-cluster continuity proof.

    Never accepted from release/approval JSON. The observer must sandwich each
    catalog read with equal live host measurements, including the current Pod IP.
    """

    approved_facts: Facts
    initial_identity: tuple[str, str]
    current_identity: tuple[str, str]
    current_address: str

    def __post_init__(self) -> None:
        _require(type(self.approved_facts) is Facts)
        for identity in (self.initial_identity, self.current_identity):
            _require(type(identity) is tuple and len(identity) == 2)
            _require(re.fullmatch(r"[1-9][0-9]{0,19}", identity[0]) is not None)
            _require(int(identity[0]) < 2**64)
            _require(re.fullmatch(r"[0-9a-f]{64}", identity[1]) is not None)
        _require(self.initial_identity == self.current_identity)
        replace(self.approved_facts, server_address=self.current_address)


@dataclass(frozen=True)
class LiveSchema:
    """Fresh read-only observation from the exact protected target connection.

    The host reads all alembic_version rows (no LIMIT/filter) and the complete
    catalog within one bounded read-only transaction. No names/secrets in errors.
    """

    catalog: object
    revisions: tuple[str, ...]
    continuity: PhysicalContinuity | None = None

    def __post_init__(self) -> None:
        _require(type(self.revisions) is tuple)
        _require(all(type(value) is str for value in self.revisions))


@dataclass(frozen=True)
class VerifiedTarget:
    release_sha: str
    revision: str
    signature: str
    target: str


class PsqlProcess(Protocol):
    """Host-owned, non-retrying, dedicated psql process capability.

    MUST start the installed fixed psql executable with -X --no-password
    --set=ON_ERROR_STOP=1 --quiet, pipe only these bytes to stdin, discard output
    (which can contain data/credentials), enforce the passed wall deadline and
    terminate/reap on timeout. Never inherit PGOPTIONS/psqlrc/service overrides
    from an untrusted caller. Target/credentials and TLS come from protected host
    configuration, not release JSON. This capability does not provision them.
    Return only after exit zero; any other/unknown outcome raises. No retries.
    """

    def run(self, procedure: bytes, *, timeout_seconds: int) -> None: ...


class BoundedPsql:
    """Bounded process I/O; host injects the fixed protected psql launcher.

    Launcher must satisfy PsqlProcess's fixed flags/environment/target contract,
    return a fresh Popen with stdin=PIPE, stdout=DEVNULL, stderr=DEVNULL, and
    start no shell or child process tree. No command or pathname argument exists.
    This is not production credential provisioning or launcher installation.
    """

    def __init__(self, spawn: Callable[[], subprocess.Popen[bytes]]) -> None:
        self._spawn = spawn

    def run(self, procedure: bytes, *, timeout_seconds: int) -> None:
        _require(type(procedure) is bytes)
        _require(hashlib.sha256(procedure).hexdigest() == PROCEDURE_SHA256)
        _require(type(timeout_seconds) is int and timeout_seconds == PROCESS_SECONDS)
        process = self._spawn()
        try:
            process.communicate(input=procedure, timeout=timeout_seconds)
            _require(process.returncode == 0)
        except (subprocess.TimeoutExpired, OSError) as error:
            raise MigrationRefused(
                "maintenance psql outcome unknown; do not retry"
            ) from error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _verify(
    approval: ApprovedTransition, live: LiveSchema, *, source: bool
) -> VerifiedTarget:
    try:
        _require(type(live) is LiveSchema)
        parsed = Snapshot.parse(live.catalog)
        _require(parsed.profile == approval.mapping.profile)
        _require(
            tuple(sorted(record[0][0] for record in parsed.inventory[0]))
            == approval.mapping.schemas
        )
        signature = canonical_signature(live.catalog, approval.mapping.profile)
        facts = parsed.facts
        if live.continuity is not None:
            continuity = live.continuity
            _require(type(continuity) is PhysicalContinuity)
            _require(continuity.initial_identity == continuity.current_identity)
            _require(facts.server_address == continuity.current_address)
            _require(
                facts
                == replace(
                    continuity.approved_facts,
                    server_address=continuity.current_address,
                )
            )
            # Recompute from the actually approved initial facts, never stamp the
            # old hash onto an unverified redirected catalog.
            facts = continuity.approved_facts
        target = target_signature(approval.mapping, facts)
        _require(target == approval.target_identity)
        revision = SOURCE_REVISION if source else TARGET_REVISION
        _require(live.revisions == (revision,))
        expected = (
            approval.source_signature
            if source
            else approval.baseline.expected_signature
        )
        _require(signature == expected)
        return VerifiedTarget(
            approval.baseline.release_sha, revision, signature, target
        )
    except ProbeError as error:
        raise MigrationRefused("maintenance catalog proof refused") from error


def verify_source(approval: ApprovedTransition, live: LiveSchema) -> VerifiedTarget:
    """Pure preflight; safe to use before stopping writes as well as afterward."""
    _require(type(approval) is ApprovedTransition)
    return _verify(approval, live, source=True)


def verify_target(approval: ApprovedTransition, live: LiveSchema) -> VerifiedTarget:
    """Pure strict target comparison, not merely an Alembic revision check."""
    _require(type(approval) is ApprovedTransition)
    return _verify(approval, live, source=False)


class MigrationBoundary:
    """One attempt per instance, additionally guarded by the host's durable journal.

    Call only while the host retains its lock, verified quiescence and backup.
    No automatic recovery/downgrade, and no retry even if executor reports error.
    """

    def __init__(
        self,
        approval: ApprovedTransition,
        *,
        inspect: Callable[[], LiveSchema],
        psql: PsqlProcess,
    ) -> None:
        _require(type(approval) is ApprovedTransition)
        self._approval = approval
        self._inspect = inspect
        self._psql = psql
        self._consumed = False

    def apply_once(self) -> VerifiedTarget:
        _require(not self._consumed)
        self._consumed = True
        # Hash BEFORE any executor call, not after reading a mutable pathname.
        _require(type(PROCEDURE) is bytes)
        _require(
            hashlib.sha256(PROCEDURE).hexdigest()
            == self._approval.procedure_sha256
            == PROCEDURE_SHA256
        )
        verify_source(self._approval, self._inspect())
        self._psql.run(PROCEDURE, timeout_seconds=PROCESS_SECONDS)
        # A mismatch here may be AFTER COMMIT. Never roll back/start old replicas.
        return verify_target(self._approval, self._inspect())
