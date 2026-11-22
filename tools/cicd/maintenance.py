"""Local-testable maintenance orchestration; NOT an installed production adapter.

The caller owns the ONE outer deployment lock from state read through final write.
There is no lock, command runner, SQL, network, or production default in this module.
HostSteps must be separately reviewed/provisioned: exact target introspection,
complete writer drain (including CLI executor), cold DB/S3/PVC snapshot consistency,
validated off-host retention and isolated restore, installed procedure allowlist,
DDL deadlines, image CAS, positive readiness and functional backend/frontend/CLI
smoke, followed by public traffic handback and public smoke. A boolean from a fake
does not implement any of these host capabilities.

persist must atomically replace and fsync the state AND parent directory before
returning, under that same outer lock. Load previous state under that lock; never
omit an existing journal. Interrupted RUNNING and all FAILED records block retry.
A persistence error stops effects; the last durable intent is deliberately
ambiguous. Production signal handling must raise a cancellation, not resume work.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from tools.cicd.release import IMAGE_REPOSITORIES, Release


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError("invalid maintenance evidence")


def _match(pattern: str, value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _digest(value: object) -> bool:
    return _match(r"sha256:[0-9a-f]{64}", value)


def _target(value: object) -> bool:
    return _match(r"pg_target_v1:sha256:[0-9a-f]{64}", value)


def _schema(value: object) -> bool:
    return _match(r"pg_catalog_v1:sha256:[0-9a-f]{64}", value)


def _replicas(value: object, *, positive: bool) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(type(n) is int and (n > 0 if positive else n >= 0) for n in value)
    )


@dataclass(frozen=True)
class Images:
    """Deployed workloads only; EXECUTOR=cli does not deploy the runner image.

    Release still validates all three published images, including runner.
    """

    backend: str
    frontend: str

    def __post_init__(self) -> None:
        for name in ("backend", "frontend"):
            value = getattr(self, name)
            prefix = IMAGE_REPOSITORIES[name] + "@"
            _require(
                isinstance(value, str)
                and value.startswith(prefix)
                and _digest(value[len(prefix) :])
            )

    @classmethod
    def from_release(cls, release: Release) -> "Images":
        return cls(
            *(
                image.repository + "@" + image.digest
                for image in (release.backend, release.frontend)
            )
        )


@dataclass(frozen=True)
class Snapshot:
    target: str
    schema: str
    images: Images
    replicas: tuple[int, int]

    def __post_init__(self) -> None:
        _require(_target(self.target) and _schema(self.schema))
        _require(
            isinstance(self.images, Images) and _replicas(self.replicas, positive=True)
        )


@dataclass(frozen=True)
class Schema:
    target: str
    signature: str

    def __post_init__(self) -> None:
        _require(_target(self.target) and _schema(self.signature))


@dataclass(frozen=True)
class UnchangedEvidence:
    """Fresh actual identity observation; stopped replica counts are irrelevant."""

    target: str
    schema: str
    images: Images

    def __post_init__(self) -> None:
        _require(
            _target(self.target)
            and _schema(self.schema)
            and isinstance(self.images, Images)
        )


@dataclass(frozen=True)
class DDLBounds:
    """Installed adapter enforces DB lock/statement AND process deadlines; no retry."""

    lock_seconds: int = 5
    statement_seconds: int = 300
    process_seconds: int = 600

    def __post_init__(self) -> None:
        _require(
            all(
                type(n) is int
                for n in (
                    self.lock_seconds,
                    self.statement_seconds,
                    self.process_seconds,
                )
            )
        )
        _require(
            0 < self.lock_seconds <= 5
            and self.lock_seconds <= self.statement_seconds <= 300
            and self.statement_seconds < self.process_seconds <= 600
        )


@dataclass(frozen=True)
class MaintenancePlan:
    """Trusted installed policy, NOT release JSON fields or user-supplied commands.

    validate_transition must resolve procedure+digest in a protected installed
    registry for this exact release SHA, old/new fingerprints and target BEFORE
    quiescence. migrate_exact must recheck that binding and execute those bytes
    only, without startup migrations, custom arguments, retries or downgrade.
    """

    release: Release
    target: str
    old_schema: str
    new_schema: str
    procedure: str
    procedure_digest: str
    ddl: DDLBounds = DDLBounds()

    def __post_init__(self) -> None:
        _require(isinstance(self.release, Release) and _target(self.target))
        _require(_schema(self.old_schema) and _schema(self.new_schema))
        _require(_match(r"[a-z][a-z0-9-]{0,63}", self.procedure))
        _require(_digest(self.procedure_digest) and isinstance(self.ddl, DDLBounds))


@dataclass(frozen=True)
class Backup:
    """Recovery bundle covers the database and application volumes, not unchanged S3 assets."""

    manifest: str
    source: Snapshot
    components: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(_digest(self.manifest) and isinstance(self.source, Snapshot))
        _require(
            isinstance(self.components, tuple)
            and len(self.components) == 1
            and all(_digest(v) for v in self.components)
        )


@dataclass(frozen=True)
class BackupProof:
    """Safe report digest, bound to exact manifest; never a path or credentials."""

    manifest: str
    report: str
    verified: bool

    def __post_init__(self) -> None:
        _require(
            _digest(self.manifest)
            and _digest(self.report)
            and type(self.verified) is bool
        )


@dataclass(frozen=True)
class Readiness:
    images: Images
    desired: tuple[int, int]
    ready: tuple[int, int]

    def __post_init__(self) -> None:
        _require(isinstance(self.images, Images))
        _require(
            _replicas(self.desired, positive=False)
            and _replicas(self.ready, positive=False)
        )


class Phase(str, Enum):
    PREFLIGHT = "preflight"
    VALIDATE_TRANSITION = "validate_transition"
    REVERIFY_BEFORE_QUIESCE = "reverify_before_quiesce"
    QUIESCE = "quiesce"
    COLD_BACKUP = "cold_backup"
    VALIDATE_BACKUP = "validate_backup"
    BACKUP_READBACK = "backup_readback"
    RESTORE_CHECK = "restore_check"
    CHECK_OLD_SCHEMA = "check_old_schema"
    REVERIFY_BEFORE_DDL = "reverify_before_ddl"
    MIGRATE_EXACT = "migrate_exact"
    CHECK_NEW_SCHEMA = "check_new_schema"
    ROLLOUT_IMAGES = "rollout_images"
    START_WORKLOADS = "start_workloads"
    PRIVATE_READINESS = "private_readiness"
    FUNCTIONAL_SMOKE = "functional_smoke"
    RESTORE_TRAFFIC = "restore_traffic"
    READINESS = "readiness"
    PUBLIC_SMOKE = "public_smoke"
    COMPLETE = "complete"
    VERIFY_UNCHANGED = "verify_unchanged"
    RECOVER_AVAILABILITY = "recover_availability"
    RECOVERY_READINESS = "recovery_readiness"


class Status(str, Enum):
    RUNNING = "running"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class Reason(str, Enum):
    NONE = "none"
    STEP_FAILED = "step_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Evidence:
    kind: str
    manifest: str
    report: str

    def __post_init__(self) -> None:
        _require(
            self.kind
            in {"backup", "validated_backup", "backup_readback", "restore_check"}
        )
        _require(_digest(self.manifest) and _digest(self.report))


@dataclass(frozen=True)
class MaintenanceState:
    plan: MaintenancePlan
    phase: Phase
    status: Status = Status.RUNNING
    old: Snapshot | None = None
    evidence: tuple[Evidence, ...] = ()
    disruption_possible: bool = False
    ddl_possible: bool = False
    reason: Reason = Reason.NONE
    failure_phase: Phase | None = None
    availability_recovered: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _require(
            isinstance(self.plan, MaintenancePlan) and isinstance(self.phase, Phase)
        )
        _require(isinstance(self.status, Status) and isinstance(self.reason, Reason))
        _require(self.old is None or isinstance(self.old, Snapshot))
        _require(
            isinstance(self.evidence, tuple)
            and all(isinstance(e, Evidence) for e in self.evidence)
        )
        _require(
            all(
                type(v) is bool
                for v in (
                    self.disruption_possible,
                    self.ddl_possible,
                    self.availability_recovered,
                )
            )
        )
        _require(not self.ddl_possible or self.disruption_possible)
        _require(self.failure_phase is None or isinstance(self.failure_phase, Phase))
        _require(
            self.updated_at.tzinfo is not None
            and self.updated_at.utcoffset() is not None
        )


class HostSteps(Protocol):
    """Consumer-owned capability; every method bounded and called under outer lock.

    reverify_current_release is read-only, rechecks exact SHA/CI/manifest, never
    selects another release. quiesce returns True only after all writers stop.
    backup_readback verifies the retained ciphertext in the approved same-node
    bucket; restore_check checks the database from that exact bundle in isolation.
    Traffic remains closed through backend readiness and private functional smoke.
    After restore_traffic starts the frontend, readiness verifies both deployed
    image identities and positive replicas. public_smoke verifies the public route.
    The backup is not disaster recovery for the loss of the node.
    No method may take another deployment lock or accept untrusted commands.
    """

    def preflight(self, plan: MaintenancePlan, *, timeout_seconds: int) -> Snapshot: ...
    def validate_transition(
        self, plan: MaintenancePlan, snapshot: Snapshot, *, timeout_seconds: int
    ) -> bool: ...
    def reverify_current_release(
        self, release: Release, *, timeout_seconds: int
    ) -> bool: ...
    def quiesce(self, snapshot: Snapshot, *, timeout_seconds: int) -> bool: ...
    def cold_backup(self, snapshot: Snapshot, *, timeout_seconds: int) -> Backup: ...
    def validate_backup(
        self, backup: Backup, *, timeout_seconds: int
    ) -> BackupProof: ...
    def backup_readback(
        self, backup: Backup, *, timeout_seconds: int
    ) -> BackupProof: ...
    def restore_check(self, backup: Backup, *, timeout_seconds: int) -> BackupProof: ...
    def schema_check(self, *, timeout_seconds: int) -> Schema: ...
    def migrate_exact(
        self, plan: MaintenancePlan, backup: Backup, *, timeout_seconds: int
    ) -> None: ...
    def rollout_images(self, images: Images, *, timeout_seconds: int) -> None: ...
    def start_workloads(
        self, replicas: tuple[int, int], *, timeout_seconds: int
    ) -> None: ...
    def private_readiness(self, *, timeout_seconds: int) -> bool: ...
    def readiness(self, *, timeout_seconds: int) -> Readiness: ...
    def functional_smoke(self, *, timeout_seconds: int) -> bool: ...
    def restore_traffic(self, *, timeout_seconds: int) -> None: ...
    def public_smoke(self, *, timeout_seconds: int) -> bool: ...
    def verify_unchanged(
        self, snapshot: Snapshot, *, timeout_seconds: int
    ) -> UnchangedEvidence: ...
    def recover_availability(
        self, snapshot: Snapshot, *, timeout_seconds: int
    ) -> None: ...


class StatePersistenceError(RuntimeError):
    """No further host operation is allowed; reconcile last durable intent."""


class MaintenanceCancelled(KeyboardInterrupt):
    """Cancellation recorded; safe message deliberately omits adapter output."""


class _Journal:
    def __init__(
        self, state: MaintenanceState, persist: Callable[[MaintenanceState], None]
    ) -> None:
        self.state = state
        self.persist = persist

    def write(self, **changes: object) -> None:
        candidate = replace(self.state, **changes, updated_at=datetime.now(UTC))
        try:
            self.persist(candidate)
        except BaseException:  # noqa: BLE001 - any interrupted write forbids further effects
            raise StatePersistenceError(
                "maintenance state write failed; reconcile durable intent"
            ) from None
        self.state = candidate

    def enter(self, phase: Phase, **changes: object) -> None:
        self.write(phase=phase, **changes)

    def proof(self, kind: str, backup: Backup, proof: BackupProof) -> None:
        _require(
            isinstance(proof, BackupProof)
            and proof.verified
            and proof.manifest == backup.manifest
        )
        # Attached to the next durable intent; cannot reach DDL without it persisted.
        self.state = replace(
            self.state,
            evidence=(
                *self.state.evidence,
                Evidence(kind, proof.manifest, proof.report),
            ),
        )

    def failed(self, error: BaseException) -> MaintenanceState:
        self.write(
            status=Status.FAILED,
            reason=(
                Reason.CANCELLED
                if not isinstance(error, Exception)
                else Reason.STEP_FAILED
            ),
            failure_phase=self.state.failure_phase or self.state.phase,
        )
        if not isinstance(error, Exception):
            raise MaintenanceCancelled(
                "maintenance cancelled; inspect durable state"
            ) from None
        return self.state


def _ready(actual: Readiness, images: Images, replicas: tuple[int, int]) -> None:
    _require(
        isinstance(actual, Readiness)
        and actual.images == images
        and actual.desired == replicas
        and actual.ready == replicas
        and all(n > 0 for n in actual.ready)
    )


def run_maintenance(
    plan: MaintenancePlan,
    *,
    host: HostSteps,
    persist: Callable[[MaintenanceState], None],
    previous: MaintenanceState | None = None,
) -> MaintenanceState:
    """Run once; failure/cancellation never advances or automatically recovers.

    Failed/running previous state is a hard stop, including possible partial DDL.
    Explicit recover_old_availability is separate and never converts failure to
    success. An uncatchable process death leaves persisted RUNNING intent blocked.
    """
    _require(
        previous is None
        or (
            previous.status == Status.SUCCEEDED
            and previous.plan.release.context.sha != plan.release.context.sha
        )
    )
    journal = _Journal(MaintenanceState(plan, Phase.PREFLIGHT), persist)
    try:
        journal.enter(Phase.PREFLIGHT)
        old = host.preflight(plan, timeout_seconds=60)
        _require(
            isinstance(old, Snapshot)
            and old.target == plan.target
            and old.schema == plan.old_schema
        )
        journal.enter(Phase.VALIDATE_TRANSITION, old=old)
        _require(host.validate_transition(plan, old, timeout_seconds=60) is True)
        journal.enter(Phase.REVERIFY_BEFORE_QUIESCE)
        _require(
            host.reverify_current_release(plan.release, timeout_seconds=60) is True
        )
        journal.enter(Phase.QUIESCE, disruption_possible=True)
        _require(host.quiesce(old, timeout_seconds=180) is True)
        journal.enter(Phase.COLD_BACKUP)
        backup = host.cold_backup(old, timeout_seconds=1800)
        _require(isinstance(backup, Backup) and backup.source == old)
        journal.state = replace(
            journal.state,
            evidence=(Evidence("backup", backup.manifest, backup.manifest),),
        )
        journal.enter(Phase.VALIDATE_BACKUP)
        journal.proof(
            "validated_backup",
            backup,
            host.validate_backup(backup, timeout_seconds=300),
        )
        journal.enter(Phase.BACKUP_READBACK)
        journal.proof(
            "backup_readback",
            backup,
            host.backup_readback(backup, timeout_seconds=1800),
        )
        journal.enter(Phase.RESTORE_CHECK)
        journal.proof(
            "restore_check", backup, host.restore_check(backup, timeout_seconds=1800)
        )
        journal.enter(Phase.CHECK_OLD_SCHEMA)
        _require(
            host.schema_check(timeout_seconds=60)
            == Schema(plan.target, plan.old_schema)
        )
        journal.enter(Phase.REVERIFY_BEFORE_DDL)
        _require(
            host.reverify_current_release(plan.release, timeout_seconds=60) is True
        )
        journal.enter(Phase.MIGRATE_EXACT, ddl_possible=True)
        host.migrate_exact(plan, backup, timeout_seconds=plan.ddl.process_seconds)
        journal.enter(Phase.CHECK_NEW_SCHEMA)
        _require(
            host.schema_check(timeout_seconds=60)
            == Schema(plan.target, plan.new_schema)
        )
        journal.enter(Phase.ROLLOUT_IMAGES)
        images = Images.from_release(plan.release)
        host.rollout_images(images, timeout_seconds=180)
        journal.enter(Phase.START_WORKLOADS)
        host.start_workloads(old.replicas, timeout_seconds=180)
        journal.enter(Phase.PRIVATE_READINESS)
        _require(host.private_readiness(timeout_seconds=180) is True)
        journal.enter(Phase.FUNCTIONAL_SMOKE)
        _require(host.functional_smoke(timeout_seconds=180) is True)
        journal.enter(Phase.RESTORE_TRAFFIC)
        host.restore_traffic(timeout_seconds=180)
        journal.enter(Phase.READINESS)
        _ready(host.readiness(timeout_seconds=180), images, old.replicas)
        journal.enter(Phase.PUBLIC_SMOKE)
        _require(host.public_smoke(timeout_seconds=180) is True)
        journal.enter(Phase.COMPLETE, status=Status.SUCCEEDED)
    except StatePersistenceError:
        raise
    except BaseException as error:  # noqa: BLE001 - orchestration boundary records cancellation/failure
        return journal.failed(error)
    return journal.state


def recover_old_availability(
    failed: MaintenanceState,
    *,
    host: HostSteps,
    persist: Callable[[MaintenanceState], None],
) -> MaintenanceState:
    """Explicit pre-DDL recovery only, under the same caller-owned lock.

    Re-query actual target/schema/images before starting ONLY old replicas; never
    patch images, restore a backup, retry migration, or clear the failed release.
    Refuse an interrupted recovery: reconciliation, not blind retry, owns it.
    """
    _require(
        failed.status == Status.FAILED
        and failed.disruption_possible
        and not failed.ddl_possible
        and failed.old is not None
        and not failed.availability_recovered
        and failed.phase
        not in {
            Phase.VERIFY_UNCHANGED,
            Phase.RECOVER_AVAILABILITY,
            Phase.RECOVERY_READINESS,
        }
    )
    journal = _Journal(failed, persist)
    try:
        journal.enter(Phase.VERIFY_UNCHANGED, status=Status.RUNNING)
        _require(
            host.verify_unchanged(failed.old, timeout_seconds=60)
            == UnchangedEvidence(
                failed.old.target, failed.old.schema, failed.old.images
            )
        )
        journal.enter(Phase.RECOVER_AVAILABILITY)
        host.recover_availability(failed.old, timeout_seconds=180)
        journal.enter(Phase.RECOVERY_READINESS)
        _ready(
            host.readiness(timeout_seconds=180), failed.old.images, failed.old.replicas
        )
        journal.write(status=Status.FAILED, availability_recovered=True)
    except StatePersistenceError:
        raise
    except BaseException as error:  # noqa: BLE001 - orchestration boundary records cancellation/failure
        return journal.failed(error)
    return journal.state
