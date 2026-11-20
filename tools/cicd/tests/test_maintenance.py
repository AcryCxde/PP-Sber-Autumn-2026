"""Local rehearsal only: no host adapter, database, network, or cluster."""

from dataclasses import asdict, replace

import pytest

from tools.cicd import maintenance as m
from tools.cicd.release import IMAGE_REPOSITORIES, Context, Image, Release

# Scenarios: ordered complete release; every step fails/cancels closed; durable
# intent before effects; exact target/schema/backup binding; no zero-replica or
# failed-smoke success; explicit evidence-gated pre-DDL availability recovery.
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
TARGET = "pg_target_v1:sha256:" + "c" * 64
BEFORE = "pg_catalog_v1:sha256:" + "d" * 64
AFTER = "pg_catalog_v1:sha256:" + "e" * 64


def release():
    return Release(
        Context(SHA, 10, 1),
        **{
            name: Image(repo, DIGEST, DIGEST)
            for name, repo in IMAGE_REPOSITORIES.items()
        },
    )


def plan():
    return m.MaintenancePlan(release(), TARGET, BEFORE, AFTER, "admin-v1", DIGEST)


def old_snapshot():
    return m.Snapshot(
        TARGET,
        BEFORE,
        m.Images(
            *(
                IMAGE_REPOSITORIES[name] + "@sha256:" + "f" * 64
                for name in ("backend", "frontend")
            )
        ),
        (1, 1),
    )


class FakeHost:
    """Not a production implementation; assertions make prerequisites observable."""

    def __init__(self, states):
        self.states = states
        self.calls = []
        self.fail = None
        self.error = RuntimeError("secret-db-password")
        self.old = old_snapshot()
        self.backup = m.Backup(DIGEST, self.old, (DIGEST,))
        self.proofs = dict.fromkeys(
            ("validate_backup", "backup_readback", "restore_check"),
            m.BackupProof(DIGEST, DIGEST, True),
        )
        self.schema = m.Schema(TARGET, AFTER)
        self.old_schema = m.Schema(TARGET, BEFORE)
        self.ready = m.Readiness(m.Images.from_release(release()), (1, 1), (1, 1))
        self.smoke = True
        self.approval = True
        self.quiet = True
        self.public = True
        self.traffic_open = True
        self.unchanged = m.UnchangedEvidence(TARGET, BEFORE, self.old.images)
        self.drift_at_reverify = None

    def step(self, name, timeout_seconds):
        assert 0 < timeout_seconds <= 1800
        assert self.states[-1].phase.value == name
        assert self.states[-1].status == m.Status.RUNNING
        self.calls.append(name)
        if self.fail == name:
            raise self.error

    def preflight(self, plan, *, timeout_seconds):
        self.step("preflight", timeout_seconds)
        return self.old

    def validate_transition(self, plan, snapshot, *, timeout_seconds):
        self.step("validate_transition", timeout_seconds)
        return self.approval

    def reverify_current_release(self, release, *, timeout_seconds):
        name = (
            "reverify_before_ddl"
            if "cold_backup" in self.calls
            else "reverify_before_quiesce"
        )
        self.step(name, timeout_seconds)
        return self.drift_at_reverify != name

    def quiesce(self, snapshot, *, timeout_seconds):
        self.step("quiesce", timeout_seconds)
        assert self.states[-1].disruption_possible
        self.traffic_open = False
        return self.quiet

    def cold_backup(self, snapshot, *, timeout_seconds):
        self.step("cold_backup", timeout_seconds)
        assert "quiesce" in self.calls
        return self.backup

    def validate_backup(self, backup, *, timeout_seconds):
        self.step("validate_backup", timeout_seconds)
        return self.proofs["validate_backup"]

    def backup_readback(self, backup, *, timeout_seconds):
        self.step("backup_readback", timeout_seconds)
        return self.proofs["backup_readback"]

    def restore_check(self, backup, *, timeout_seconds):
        self.step("restore_check", timeout_seconds)
        return self.proofs["restore_check"]

    def schema_check(self, *, timeout_seconds):
        name = (
            "check_old_schema"
            if "migrate_exact" not in self.calls
            else "check_new_schema"
        )
        self.step(name, timeout_seconds)
        return self.old_schema if name == "check_old_schema" else self.schema

    def migrate_exact(self, plan, backup, *, timeout_seconds):
        self.step("migrate_exact", timeout_seconds)
        assert timeout_seconds == plan.ddl.process_seconds
        assert self.states[-1].ddl_possible
        assert all(
            step in self.calls
            for step in (
                "validate_backup",
                "backup_readback",
                "restore_check",
                "check_old_schema",
            )
        )
        assert {e.kind for e in self.states[-1].evidence} >= {
            "backup",
            "validated_backup",
            "backup_readback",
            "restore_check",
        }
        assert backup.source == self.old
        assert plan.target == self.old.target

    def rollout_images(self, images, *, timeout_seconds):
        self.step("rollout_images", timeout_seconds)
        assert images == m.Images.from_release(release())

    def start_workloads(self, replicas, *, timeout_seconds):
        self.step("start_workloads", timeout_seconds)
        assert all(n > 0 for n in replicas)

    def readiness(self, *, timeout_seconds):
        name = (
            "recovery_readiness"
            if "recover_availability" in self.calls
            else "readiness"
        )
        self.step(name, timeout_seconds)
        return self.ready

    def private_readiness(self, *, timeout_seconds):
        self.step("private_readiness", timeout_seconds)
        assert not self.traffic_open
        return True

    def functional_smoke(self, *, timeout_seconds):
        self.step("functional_smoke", timeout_seconds)
        assert not self.traffic_open
        return self.smoke

    def restore_traffic(self, *, timeout_seconds):
        self.step("restore_traffic", timeout_seconds)
        assert "functional_smoke" in self.calls
        assert not self.traffic_open
        self.traffic_open = True

    def public_smoke(self, *, timeout_seconds):
        self.step("public_smoke", timeout_seconds)
        assert self.traffic_open
        return self.public

    def verify_unchanged(self, snapshot, *, timeout_seconds):
        self.step("verify_unchanged", timeout_seconds)
        return self.unchanged

    def recover_availability(self, snapshot, *, timeout_seconds):
        self.step("recover_availability", timeout_seconds)


@pytest.fixture
def environment():
    states = []
    host = FakeHost(states)
    return host, states


ORDER = [
    "preflight",
    "validate_transition",
    "reverify_before_quiesce",
    "quiesce",
    "cold_backup",
    "validate_backup",
    "backup_readback",
    "restore_check",
    "check_old_schema",
    "reverify_before_ddl",
    "migrate_exact",
    "check_new_schema",
    "rollout_images",
    "start_workloads",
    "private_readiness",
    "functional_smoke",
    "restore_traffic",
    "readiness",
    "public_smoke",
]


def run(environment):
    host, states = environment
    return m.run_maintenance(plan(), host=host, persist=states.append)


def test_complete_release_order_and_durable_success(environment):
    host, states = environment
    result = run(environment)
    assert host.calls == ORDER
    assert result == states[-1]
    assert result.status == m.Status.SUCCEEDED
    assert result.phase == m.Phase.COMPLETE
    assert result.old == old_snapshot()
    assert result.ddl_possible
    assert [s.phase.value for s in states[:-1]] == ORDER
    assert {e.kind for e in result.evidence} == {
        "backup",
        "validated_backup",
        "backup_readback",
        "restore_check",
    }
    assert all(s.updated_at.utcoffset().total_seconds() == 0 for s in states)
    assert result.plan == plan()
    assert host.traffic_open


def test_published_runner_is_verified_manifest_data_not_deployed_host_evidence():
    candidate = release()
    assert set(candidate.images) == {"backend", "frontend", "runner"}
    assert set(asdict(m.Images.from_release(candidate))) == {"backend", "frontend"}
    other_runner = replace(candidate.runner, digest="sha256:" + "9" * 64)
    assert m.Images.from_release(
        replace(candidate, runner=other_runner)
    ) == m.Images.from_release(candidate)
    with pytest.raises(ValueError):
        replace(candidate, runner=candidate.backend)


@pytest.mark.parametrize("stage", ORDER)
@pytest.mark.parametrize("cancel", [False, True])
def test_every_failure_and_cancellation_is_durable_and_stops(
    environment, stage, cancel, capsys
):
    host, states = environment
    host.fail = stage
    if cancel:
        host.error = KeyboardInterrupt("secret-cancel-output")
        with pytest.raises(m.MaintenanceCancelled, match="maintenance cancelled"):
            run(environment)
    else:
        assert run(environment).status == m.Status.FAILED
    result = states[-1]
    assert result.phase.value == stage
    assert result.failure_phase.value == stage
    assert result.status == m.Status.FAILED
    assert result.reason == (m.Reason.CANCELLED if cancel else m.Reason.STEP_FAILED)
    assert result.ddl_possible == (ORDER.index(stage) >= ORDER.index("migrate_exact"))
    assert host.calls == ORDER[: ORDER.index(stage) + 1]
    assert "secret" not in repr(asdict(result))
    assert capsys.readouterr() == ("", "")
    before = list(host.calls)
    with pytest.raises(ValueError):
        m.run_maintenance(plan(), host=host, persist=states.append, previous=result)
    assert host.calls == before


@pytest.mark.parametrize("stage", ORDER + ["complete"])
def test_persist_intent_failure_prevents_host_effect(environment, stage):
    host, states = environment

    def persist(state):
        if state.phase.value == stage:
            raise OSError("secret-disk-detail")
        states.append(state)

    with pytest.raises(m.StatePersistenceError, match="state write failed") as error:
        m.run_maintenance(plan(), host=host, persist=persist)
    assert "secret" not in str(error.value)
    index = (ORDER + ["complete"]).index(stage)
    assert host.calls == ORDER[:index]
    if states:
        assert states[-1].status == m.Status.RUNNING
        with pytest.raises(ValueError):
            m.run_maintenance(
                plan(), host=host, persist=states.append, previous=states[-1]
            )


@pytest.mark.parametrize(
    "stage", ["validate_backup", "backup_readback", "restore_check"]
)
@pytest.mark.parametrize("fault", ["absent", "unverified", "foreign_manifest"])
def test_ddl_cannot_run_without_every_bound_backup_proof(environment, stage, fault):
    host, _states = environment
    host.proofs[stage] = {
        "absent": None,
        "unverified": m.BackupProof(DIGEST, DIGEST, False),
        "foreign_manifest": m.BackupProof("sha256:" + "9" * 64, DIGEST, True),
    }[fault]
    result = run(environment)
    assert result.status == m.Status.FAILED
    assert result.phase.value == stage
    assert not result.ddl_possible
    assert "migrate_exact" not in host.calls
    assert host.calls[-1] == stage


@pytest.mark.parametrize("which", ["preflight", "backup", "old_schema", "new_schema"])
@pytest.mark.parametrize("fault", ["target", "schema"])
def test_target_and_strict_fingerprint_are_checked_at_boundaries(
    environment, which, fault
):
    host, _states = environment
    target = TARGET if fault == "schema" else "pg_target_v1:sha256:" + "9" * 64
    schema = BEFORE if fault == "target" else "pg_catalog_v1:sha256:" + "9" * 64
    if which == "preflight":
        host.old = replace(host.old, target=target, schema=schema)
    elif which == "backup":
        host.backup = replace(
            host.backup, source=replace(host.old, target=target, schema=schema)
        )
    elif which == "old_schema":
        host.old_schema = m.Schema(target, schema)
    else:
        host.schema = m.Schema(target, AFTER if fault == "target" else schema)
    result = run(environment)
    expected = {
        "preflight": "preflight",
        "backup": "cold_backup",
        "old_schema": "check_old_schema",
        "new_schema": "check_new_schema",
    }[which]
    assert result.status == m.Status.FAILED
    assert result.phase.value == expected
    assert host.calls[-1] == expected
    assert ("migrate_exact" in host.calls) == (which == "new_schema")
    assert "rollout_images" not in host.calls


@pytest.mark.parametrize("stage", ["reverify_before_quiesce", "reverify_before_ddl"])
def test_release_drift_never_switches_release_or_continues(environment, stage):
    host, _states = environment
    host.drift_at_reverify = stage
    result = run(environment)
    assert result.status == m.Status.FAILED
    assert result.phase.value == stage
    assert host.calls[-1] == stage
    assert "migrate_exact" not in host.calls
    assert result.plan.release == release()


@pytest.mark.parametrize(
    "attr,stage",
    [
        ("approval", "validate_transition"),
        ("quiet", "quiesce"),
        ("smoke", "functional_smoke"),
        ("public", "public_smoke"),
    ],
)
@pytest.mark.parametrize("invalid", [False, None, 1, "success"])
def test_only_explicit_positive_step_result_proceeds(environment, attr, stage, invalid):
    host, _states = environment
    setattr(host, attr, invalid)
    result = run(environment)
    assert result.status == m.Status.FAILED
    assert result.phase.value == stage
    assert host.calls[-1] == stage


@pytest.mark.parametrize(
    "fault", ["desired_zero", "ready_zero", "not_all_ready", "wrong_images"]
)
def test_readiness_cannot_claim_zero_or_wrong_images_success(environment, fault):
    host, _states = environment
    if fault == "desired_zero":
        host.ready = replace(host.ready, desired=(0, 0))
    elif fault == "ready_zero":
        host.ready = replace(host.ready, ready=(0, 0))
    elif fault == "not_all_ready":
        host.ready = replace(host.ready, ready=(1, 0))
    else:
        host.ready = replace(host.ready, images=host.old.images)
    result = run(environment)
    assert result.status == m.Status.FAILED
    assert result.phase == m.Phase.READINESS
    assert "public_smoke" not in host.calls


def preddl_failed(environment):
    host, _states = environment
    host.fail = "cold_backup"
    result = run(environment)
    host.fail = None
    return result


def test_old_availability_only_explicit_after_verified_unchanged_identity(environment):
    host, states = environment
    failed = preddl_failed(environment)
    assert "verify_unchanged" not in host.calls
    host.ready = m.Readiness(host.old.images, host.old.replicas, host.old.replicas)
    result = m.recover_old_availability(failed, host=host, persist=states.append)
    assert host.calls[-3:] == [
        "verify_unchanged",
        "recover_availability",
        "recovery_readiness",
    ]
    assert result.status == m.Status.FAILED
    assert result.failure_phase == m.Phase.COLD_BACKUP
    assert result.availability_recovered
    assert not result.ddl_possible
    before = list(host.calls)
    with pytest.raises(ValueError):
        m.recover_old_availability(result, host=host, persist=states.append)
    assert host.calls == before


@pytest.mark.parametrize("fault", ["missing", "target", "schema", "images"])
def test_no_old_start_without_explicit_identity_evidence(environment, fault):
    host, states = environment
    failed = preddl_failed(environment)
    if fault == "missing":
        host.unchanged = None
    elif fault == "target":
        host.unchanged = replace(
            host.unchanged, target="pg_target_v1:sha256:" + "9" * 64
        )
    elif fault == "schema":
        host.unchanged = replace(host.unchanged, schema=AFTER)
    else:
        host.unchanged = replace(
            host.unchanged, images=m.Images.from_release(release())
        )
    result = m.recover_old_availability(failed, host=host, persist=states.append)
    assert result.status == m.Status.FAILED
    assert not result.availability_recovered
    assert host.calls[-1] == "verify_unchanged"
    assert "recover_availability" not in host.calls


@pytest.mark.parametrize("stage", ORDER[ORDER.index("migrate_exact") :])
def test_no_recovery_or_retry_after_possible_ddl(environment, stage):
    host, states = environment
    host.fail = stage
    failed = run(environment)
    before = list(host.calls)
    with pytest.raises(ValueError):
        m.recover_old_availability(failed, host=host, persist=states.append)
    with pytest.raises(ValueError):
        m.run_maintenance(plan(), host=host, persist=states.append, previous=failed)
    assert before == host.calls


@pytest.mark.parametrize(
    "stage", ["verify_unchanged", "recover_availability", "recovery_readiness"]
)
@pytest.mark.parametrize("cancel", [False, True])
def test_recovery_failure_or_cancellation_remains_blocked(environment, stage, cancel):
    host, states = environment
    failed = preddl_failed(environment)
    host.fail = stage
    host.error = (
        KeyboardInterrupt("secret-cancel") if cancel else OSError("secret-failure")
    )
    host.ready = m.Readiness(host.old.images, (1, 1), (1, 1))
    if cancel:
        with pytest.raises(m.MaintenanceCancelled):
            m.recover_old_availability(failed, host=host, persist=states.append)
    else:
        m.recover_old_availability(failed, host=host, persist=states.append)
    result = states[-1]
    assert result.status == m.Status.FAILED
    assert not result.availability_recovered
    assert host.calls[-1] == stage
    before = list(host.calls)
    with pytest.raises(ValueError):
        m.recover_old_availability(result, host=host, persist=states.append)
    assert host.calls == before


@pytest.mark.parametrize("write_index", range(4))
def test_recovery_persist_failure_stops_effects_and_blocks_repeat(
    environment, write_index
):
    host, states = environment
    failed = preddl_failed(environment)
    host.ready = m.Readiness(host.old.images, (1, 1), (1, 1))
    base_calls = len(host.calls)
    writes = 0

    def persist(state):
        nonlocal writes
        current = writes
        writes += 1
        if current == write_index:
            raise OSError("secret-recovery-state")
        states.append(state)

    with pytest.raises(m.StatePersistenceError):
        m.recover_old_availability(failed, host=host, persist=persist)
    assert (
        host.calls[base_calls:]
        == ["verify_unchanged", "recover_availability", "recovery_readiness"][
            :write_index
        ]
    )
    assert not states[-1].availability_recovered
    if write_index > 0:
        assert states[-1].status == m.Status.RUNNING
        with pytest.raises(ValueError):
            m.recover_old_availability(states[-1], host=host, persist=states.append)


def test_failure_journal_write_failure_leaves_blocking_intent(environment):
    host, states = environment
    host.fail = "migrate_exact"

    def persist(state):
        if state.status == m.Status.FAILED:
            raise OSError("secret")
        states.append(state)

    with pytest.raises(m.StatePersistenceError):
        m.run_maintenance(plan(), host=host, persist=persist)
    assert states[-1].phase == m.Phase.MIGRATE_EXACT
    assert states[-1].status == m.Status.RUNNING
    assert states[-1].ddl_possible
    assert host.calls[-1] == "migrate_exact"


@pytest.mark.parametrize(
    "construct",
    [
        lambda: m.DDLBounds(lock_seconds=0),
        lambda: m.DDLBounds(lock_seconds=6),
        lambda: m.DDLBounds(statement_seconds=301),
        lambda: m.DDLBounds(process_seconds=300),
        lambda: m.DDLBounds(process_seconds=601),
        lambda: m.DDLBounds(lock_seconds=True),
        lambda: replace(plan(), procedure="sh -c command"),
        lambda: replace(plan(), procedure_digest="file:///tmp/migration"),
        lambda: replace(plan(), target="caller-db"),
        lambda: replace(plan(), new_schema="alembic-head"),
        lambda: replace(old_snapshot(), replicas=(0, 0)),
        lambda: replace(old_snapshot(), replicas=(True, 1)),
    ],
)
def test_construction_rejects_custom_commands_unbounded_ddl_or_invalid_evidence(
    construct,
):
    with pytest.raises(ValueError):
        construct()
