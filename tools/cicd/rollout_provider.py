"""Composition-only installed provider; no launcher, credentials or live defaults.

The installer supplies fixed host, backup/restore and bounded live observations.
The server owns the outer lock, GitHub verification and durable once-only journal.
Only the reviewed 0005 -> 0001 transition from the clean bc10c7e release is
supported. A future same-schema release needs its own reviewed policy: it must
NOT reuse this source approval. Same-node backup is not off-host recovery.

Requires the maintenance consumer's backup_readback/private_readiness contract:
backend-only readiness/smoke precede frontend start; full readiness follows it.
"""

import hashlib
import json
import re
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar

from tools.cicd.deploy_server import PreparedMaintenance
from tools.cicd.maintenance import (
    Backup,
    BackupProof,
    DDLBounds,
    Images,
    MaintenancePlan,
    Readiness,
    Schema,
    Snapshot,
    UnchangedEvidence,
)
from tools.cicd.maintenance_schema import (
    PROCEDURE_SHA256,
    PROCESS_SECONDS,
    ApprovedTransition,
    LiveSchema,
    MigrationBoundary,
    verify_source,
    verify_target,
)
from tools.cicd.release import Release, _json, _object, _require
from tools.cicd.rollout_backup import (
    S3,
    BackupConfig,
    BackupReceipt,
    create_backup,
)
from tools.cicd.rollout_backup import (
    Host as BackupHost,
)
from tools.cicd.rollout_host import CurrentRelease, ImagePin, K3sHost
from tools.cicd.schema_probe import Baseline, TargetMapping, protected_read

SOURCE_SHA = "bc10c7e05d6fc6e9965b0bc01f3d8ed30ca16874"
PROCEDURE_ID = "admin-ai-access-0005-0001"
APPROVAL_LIMIT = 65536
T_co = TypeVar("T_co", covariant=True)


@contextmanager
def _backup_deadline(seconds: int) -> Iterator[None]:
    _require(type(seconds) is int and 0 < seconds <= 1800, "invalid backup budget")
    _require(
        signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "conflicting backup timer"
    )

    def expire(_signum: int, _frame: object) -> None:
        raise TimeoutError("backup deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expire)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Observation(Protocol[T_co]):
    """Installed observations must enforce this budget, never return assertions."""

    def __call__(self, *, timeout_seconds: int) -> T_co: ...


@dataclass(frozen=True)
class Approval:
    source: Release
    transition: ApprovedTransition
    source_runtime: tuple[str, str]
    target_runtime: tuple[str, str]
    approval: str
    recovery_scope: str = "same-node"

    def __post_init__(self) -> None:
        _require(isinstance(self.source, Release), "invalid approved source")
        _require(self.source.context.sha == SOURCE_SHA, "unapproved source release")
        _require(type(self.transition) is ApprovedTransition, "invalid transition")
        _require(self.recovery_scope == "same-node", "unapproved recovery scope")
        _require(
            isinstance(self.approval, str)
            and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", self.approval),
            "missing operator approval",
        )
        for pins in (self.source_runtime, self.target_runtime):
            _require(type(pins) is tuple and len(pins) == 2, "invalid runtime pins")
            for digest in pins:
                _require(
                    isinstance(digest, str)
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
                    "invalid runtime digest",
                )

    @classmethod
    def parse(cls, raw: bytes) -> "Approval":
        _require(type(raw) is bytes and 0 < len(raw) <= APPROVAL_LIMIT, "approval size")
        data = _object(
            _json(raw.decode("utf-8")),
            {
                "schema",
                "source",
                "transition",
                "source_runtime",
                "target_runtime",
                "approval",
                "recovery_scope",
            },
        )
        _require(type(data["schema"]) is int and data["schema"] == 1, "approval schema")
        transition = _object(
            data["transition"],
            {
                "source_revision",
                "source_signature",
                "baseline",
                "mapping",
                "target_identity",
                "procedure_sha256",
            },
        )
        runtimes = []
        for field in ("source_runtime", "target_runtime"):
            pins = _object(data[field], {"backend", "frontend"})
            runtimes.append((pins["backend"], pins["frontend"]))
        return cls(
            Release.parse(json.dumps(data["source"])),
            ApprovedTransition(
                transition["source_revision"],
                transition["source_signature"],
                Baseline.parse(transition["baseline"]),
                TargetMapping.parse(transition["mapping"]),
                transition["target_identity"],
                transition["procedure_sha256"],
            ),
            runtimes[0],
            runtimes[1],
            data["approval"],
            data["recovery_scope"],
        )

    def plan(self, release: Release) -> MaintenancePlan:
        self.transition.baseline.bind(release.context.sha, self.transition.mapping)
        return MaintenancePlan(
            release,
            self.transition.target_identity,
            self.transition.source_signature,
            self.transition.baseline.expected_signature,
            PROCEDURE_ID,
            "sha256:" + PROCEDURE_SHA256,
            DDLBounds(5, 30, PROCESS_SECONDS),
        )


def _pins(images: Images, runtime: tuple[str, str]) -> tuple[ImagePin, ImagePin]:
    return ImagePin(images.backend, runtime[0]), ImagePin(images.frontend, runtime[1])


class RolloutProvider:
    provider_id = "same-node-rollout-v1"

    def __init__(
        self,
        approval_path: Path,
        *,
        host: K3sHost,
        inspect: Observation[LiveSchema],
        database_evidence: Observation[str],
        backup_config: Callable[[str], BackupConfig],
        backup_host: BackupHost,
        s3: S3,
        private_readiness: Observation[bool],
        readiness: Observation[Readiness],
        functional_smoke: Observation[bool],
        public_smoke: Observation[bool],
    ) -> None:
        _require(
            approval_path.is_absolute(), "absolute installed approval path required"
        )
        self._path, self._host, self._inspect = approval_path, host, inspect
        self._database_evidence, self._backup_config = database_evidence, backup_config
        self._backup_host, self._s3 = backup_host, s3
        self._private_readiness, self._readiness = private_readiness, readiness
        self._functional_smoke, self._public_smoke = functional_smoke, public_smoke

    def prepare(self, release: Release, *, timeout_seconds: int) -> PreparedMaintenance:
        _require(
            type(timeout_seconds) is int and 0 < timeout_seconds <= 60, "prepare budget"
        )
        approval = Approval.parse(protected_read(self._path, APPROVAL_LIMIT))
        plan = approval.plan(release)
        verify_source(
            approval.transition, self._inspect(timeout_seconds=timeout_seconds)
        )
        return PreparedMaintenance(plan, _HostSteps(self, approval, plan))


class _HostSteps:
    """Map existing fixed capabilities; never own journal, retry or SQL bytes."""

    def __init__(
        self, provider: RolloutProvider, approval: Approval, plan: MaintenancePlan
    ) -> None:
        self._provider, self._approval, self._plan = provider, approval, plan
        self._snapshot: Snapshot | None = None
        self._source: CurrentRelease | None = None
        self._backup: Backup | None = None
        self._receipt: BackupReceipt | None = None
        self._migration = MigrationBoundary(
            approval.transition,
            inspect=lambda: provider._inspect(timeout_seconds=60),
            psql=provider._host,
        )
        self._backend, self._frontend = _pins(
            Images.from_release(plan.release), approval.target_runtime
        )
        self._target_proven = False

    def preflight(self, plan: MaintenancePlan, *, timeout_seconds: int) -> Snapshot:
        _require(plan == self._plan, "unrelated plan")
        old = Images.from_release(self._approval.source)
        backend, frontend = _pins(old, self._approval.source_runtime)
        self._source = CurrentRelease(frontend, backend, plan.old_schema)
        self._provider._host.preflight(
            self._source,
            source_probe=lambda: (
                verify_source(
                    self._approval.transition,
                    self._provider._inspect(timeout_seconds=timeout_seconds),
                ).signature
            ),
        )
        snapshot = Snapshot(plan.target, plan.old_schema, old, (1, 1))
        self._snapshot = snapshot
        return snapshot

    def validate_transition(
        self, plan: MaintenancePlan, snapshot: Snapshot, *, timeout_seconds: int
    ) -> bool:
        _require(
            plan == self._plan and snapshot == self._snapshot, "unrelated transition"
        )
        return self._approval.plan(plan.release) == plan

    def reverify_current_release(
        self, release: Release, *, timeout_seconds: int
    ) -> bool:
        # deploy_server._VerifiedHost MUST replace this method with its GitHub gate.
        raise ValueError("server-owned GitHub verification wrapper required")

    def quiesce(self, snapshot: Snapshot, *, timeout_seconds: int) -> bool:
        _require(snapshot == self._snapshot, "unrelated source")
        self._provider._host.gate_ingress()
        self._provider._host.stop_applications()
        return True  # Fixed host methods raise until actual writer absence is proven.

    def cold_backup(self, snapshot: Snapshot, *, timeout_seconds: int) -> Backup:
        with _backup_deadline(timeout_seconds):
            return self._capture_backup(snapshot)

    def _capture_backup(self, snapshot: Snapshot) -> Backup:
        _require(
            snapshot == self._snapshot and self._receipt is None, "unrelated backup"
        )
        provider = self._provider
        verify_source(self._approval.transition, provider._inspect(timeout_seconds=60))
        observed = provider._database_evidence(timeout_seconds=60)
        _require(
            isinstance(observed, str) and bool(observed.strip()),
            "missing live DB evidence",
        )
        config = provider._backup_config(observed)
        _require(
            type(config) is BackupConfig
            and config.expected_database_evidence == observed,
            "backup config changed actual DB evidence",
        )
        provider._host.stop_database_for_cold_copy()
        receipt = create_backup(config, provider._backup_host, provider._s3)
        _require(
            type(receipt) is BackupReceipt and receipt.database_evidence == observed,
            "backup receipt mismatch",
        )
        digest = "sha256:" + receipt.sha256
        backup = Backup(digest, snapshot, (digest,))
        self._receipt, self._backup = receipt, backup
        return backup

    def _proof(self, backup: Backup) -> BackupProof:
        _require(
            self._receipt is not None
            and self._backup is not None
            and backup == self._backup,
            "verified backup receipt required",
        )
        # This digest describes a completed readback/restore receipt, not fictitious
        # DB/S3/PVC component digests or an independent/off-host retention claim.
        report = hashlib.sha256(
            json.dumps(
                {
                    "manifest": backup.manifest,
                    "object_key": self._receipt.object_key,
                    "database_evidence": self._receipt.database_evidence,
                    "recovery_scope": "same-node",
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return BackupProof(backup.manifest, "sha256:" + report, True)

    def validate_backup(self, backup: Backup, *, timeout_seconds: int) -> BackupProof:
        return self._proof(backup)

    def backup_readback(self, backup: Backup, *, timeout_seconds: int) -> BackupProof:
        return self._proof(backup)

    def restore_check(self, backup: Backup, *, timeout_seconds: int) -> BackupProof:
        proof = self._proof(backup)
        self._provider._host.restart_database()
        return proof

    def schema_check(self, *, timeout_seconds: int) -> Schema:
        live = self._provider._inspect(timeout_seconds=timeout_seconds)
        verify = verify_target if self._target_proven else verify_source
        proof = verify(self._approval.transition, live)
        return Schema(proof.target, proof.signature)

    def migrate_exact(
        self, plan: MaintenancePlan, backup: Backup, *, timeout_seconds: int
    ) -> None:
        _require(
            plan == self._plan and timeout_seconds == PROCESS_SECONDS,
            "unrelated migration",
        )
        self._proof(backup)
        self._migration.apply_once()  # Owns fixed bytes and fresh pre/post catalog proofs.
        self._target_proven = True

    def rollout_images(self, images: Images, *, timeout_seconds: int) -> None:
        _require(
            self._target_proven and images == Images.from_release(self._plan.release),
            "target schema/image proof required",
        )
        # Fixed K3sHost atomically selects each image immediately before its start.

    def start_workloads(
        self, replicas: tuple[int, int], *, timeout_seconds: int
    ) -> None:
        _require(
            self._target_proven and replicas == (1, 1), "unsupported start profile"
        )
        self._provider._host.start_backend(self._backend)

    def private_readiness(self, *, timeout_seconds: int) -> bool:
        return self._provider._private_readiness(timeout_seconds=timeout_seconds)

    def readiness(self, *, timeout_seconds: int) -> Readiness:
        actual = self._provider._readiness(timeout_seconds=timeout_seconds)
        expected = Images.from_release(
            self._plan.release if self._target_proven else self._approval.source
        )
        _require(
            isinstance(actual, Readiness)
            and actual.images == expected
            and actual.desired == actual.ready == (1, 1),
            "actual readiness mismatch",
        )
        return actual

    def functional_smoke(self, *, timeout_seconds: int) -> bool:
        return self._provider._functional_smoke(timeout_seconds=timeout_seconds)

    def restore_traffic(self, *, timeout_seconds: int) -> None:
        self._provider._host.start_frontend(self._frontend)

    def public_smoke(self, *, timeout_seconds: int) -> bool:
        return self._provider._public_smoke(timeout_seconds=timeout_seconds)

    def verify_unchanged(
        self, snapshot: Snapshot, *, timeout_seconds: int
    ) -> UnchangedEvidence:
        _require(
            snapshot == self._snapshot and not self._target_proven,
            "original recovery unavailable",
        )
        self._provider._host.restore_database_only_if_original()
        proof = verify_source(
            self._approval.transition,
            self._provider._inspect(timeout_seconds=timeout_seconds),
        )
        return UnchangedEvidence(proof.target, proof.signature, snapshot.images)

    def recover_availability(self, snapshot: Snapshot, *, timeout_seconds: int) -> None:
        _require(
            snapshot == self._snapshot
            and self._source is not None
            and not self._target_proven,
            "original recovery unavailable",
        )
        self._provider._host.restore_original_applications(
            self._source,
            source_probe=lambda: (
                verify_source(
                    self._approval.transition,
                    self._provider._inspect(timeout_seconds=timeout_seconds),
                ).signature
            ),
        )
