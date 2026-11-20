"""Trusted transition checks, one-shot apply, and exact procedure evidence."""

import copy
import hashlib
import subprocess
from dataclasses import replace

import pytest

from tools.cicd import maintenance_schema as module
from tools.cicd.schema_probe import (
    Baseline,
    Facts,
    ProbeError,
    TargetMapping,
    canonical_signature,
    target_signature,
    upgrade_paths,
)
from tools.cicd.tests.test_schema_baseline import baseline_data, mapping_data
from tools.cicd.tests.test_schema_probe import snapshot as empty_snapshot

snapshot = empty_snapshot


def approval_for(source, target, mapping):
    signature = canonical_signature(target, mapping.profile)
    data = baseline_data()
    data.update(
        release_sha="76fb599ce0efc4550f654ff8dd64f18f041044a6",
        profile=mapping.profile.to_data(),
        schemas=list(mapping.schemas),
        expected_signature=signature,
        clean_signature=signature,
        target_mapping_hash=mapping.signature(),
        migration_head=module.TARGET_REVISION,
        upgrade_signatures=dict.fromkeys(
            upgrade_paths(module.TARGET_REVISION), signature
        ),
    )
    return module.ApprovedTransition(
        module.SOURCE_REVISION,
        canonical_signature(source, mapping.profile),
        Baseline.parse(data),
        mapping,
        target_signature(mapping, Facts.parse(source["facts"])),
        module.PROCEDURE_SHA256,
    )


@pytest.fixture
def scenario(snapshot):
    snapshot["inventory"]["schemas"] = [{"identity": ["public"]}]
    snapshot["counts"]["schemas"] = 1
    target = copy.deepcopy(snapshot)
    target["database_collation"]["ctype"] = "C.UTF-8"
    data = mapping_data()
    data["profile"] = snapshot["profile"]
    mapping = TargetMapping.parse(data)
    return approval_for(snapshot, target, mapping), snapshot, target


class Executor:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def run(self, procedure, *, timeout_seconds):
        self.calls.append((procedure, timeout_seconds))
        if self.error:
            raise self.error


def boundary(scenario, executor=None):
    approval, source, target = scenario
    observations = iter(
        [
            module.LiveSchema(source, (module.SOURCE_REVISION,)),
            module.LiveSchema(target, (module.TARGET_REVISION,)),
        ]
    )
    executor = executor or Executor()
    return module.MigrationBoundary(
        approval, inspect=lambda: next(observations), psql=executor
    ), executor


def test_installed_procedure_hash_is_pinned():
    assert hashlib.sha256(module.PROCEDURE).hexdigest() == module.PROCEDURE_SHA256
    assert module.PROCEDURE.startswith(b"BEGIN;")
    assert module.PROCEDURE.endswith(b"COMMIT;\n")
    assert b"IF NOT EXISTS" not in module.PROCEDURE


def test_no_request_sql_path_or_command_in_transition():
    assert set(module.ApprovedTransition.__dataclass_fields__) == {
        "source_revision",
        "source_signature",
        "baseline",
        "mapping",
        "target_identity",
        "procedure_sha256",
    }


def test_success_requires_both_exact_proofs(scenario):
    runner, executor = boundary(scenario)
    proof = runner.apply_once()
    assert proof == module.VerifiedTarget(
        scenario[0].baseline.release_sha,
        module.TARGET_REVISION,
        scenario[0].baseline.expected_signature,
        scenario[0].target_identity,
    )
    assert executor.calls == [(module.PROCEDURE, 60)]
    with pytest.raises(module.MigrationRefused):
        runner.apply_once()
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "20261030_0013"),
        ("source_signature", "bad"),
        ("target_identity", "pg_target_v1:sha256:bad"),
        ("procedure_sha256", "f" * 64),
    ],
)
def test_unknown_transition_refused(scenario, field, value):
    with pytest.raises(module.MigrationRefused):
        replace(scenario[0], **{field: value})


def test_new_exact_release_sha_allowed_only_with_exact_baseline_binding(scenario):
    approval = scenario[0]
    baseline = replace(approval.baseline, release_sha="b" * 40)
    assert replace(approval, baseline=baseline).baseline.release_sha == "b" * 40
    with pytest.raises(ProbeError):
        replace(approval, mapping=replace(approval.mapping, endpoint="wrong.test"))


@pytest.mark.parametrize("phase", ["source", "target"])
@pytest.mark.parametrize(
    "mutation",
    [
        "revision",
        "multiple_revisions",
        "schema",
        "profile",
        "target",
        "unsupported",
        "missing",
    ],
)
def test_exact_revision_does_not_hide_bad_proof(scenario, phase, mutation):
    approval, source, target = scenario
    raw = copy.deepcopy(source if phase == "source" else target)
    revisions = (
        module.SOURCE_REVISION if phase == "source" else module.TARGET_REVISION,
    )
    if mutation == "revision":
        revisions = ("wrong",)
    elif mutation == "multiple_revisions":
        revisions = (*revisions, *revisions)
    elif mutation == "schema":
        raw["database_collation"]["encoding"] = "LATIN1"
    elif mutation == "profile":
        raw["profile"]["query_sha256"] = "b" * 64
    elif mutation == "target":
        raw["facts"]["server_address"] = "127.0.0.2"
    elif mutation == "unsupported":
        raw["unsupported"]["pg_proc"] = 1
    else:
        del raw["inventory"]["tables"]
    verify = module.verify_source if phase == "source" else module.verify_target
    with pytest.raises(module.MigrationRefused):
        verify(approval, module.LiveSchema(raw, revisions))


def test_source_failure_never_reaches_executor(scenario):
    approval, source, _ = scenario
    executor = Executor()
    runner = module.MigrationBoundary(
        approval,
        inspect=lambda: module.LiveSchema(source, ("wrong",)),
        psql=executor,
    )
    with pytest.raises(module.MigrationRefused):
        runner.apply_once()
    assert executor.calls == []


def test_modified_installed_bytes_refused_before_executor(scenario, monkeypatch):
    runner, executor = boundary(scenario)
    monkeypatch.setattr(module, "PROCEDURE", module.PROCEDURE + b"SELECT 1;\n")
    with pytest.raises(module.MigrationRefused):
        runner.apply_once()
    assert executor.calls == []


def test_ambiguous_execution_consumed_without_retry(scenario):
    runner, executor = boundary(scenario, Executor(TimeoutError("unknown")))
    with pytest.raises(TimeoutError):
        runner.apply_once()
    with pytest.raises(module.MigrationRefused):
        runner.apply_once()
    assert len(executor.calls) == 1


def test_post_commit_mismatch_never_retries(scenario):
    _, _, target = scenario
    target["facts"]["server_address"] = "127.0.0.2"
    runner, executor = boundary(scenario)
    with pytest.raises(module.MigrationRefused):
        runner.apply_once()
    with pytest.raises(module.MigrationRefused):
        runner.apply_once()
    assert len(executor.calls) == 1


@pytest.mark.parametrize("failure", ["nonzero", "oserror", "interrupt"])
def test_bounded_psql_other_failures_refuse_and_cleanup(failure):
    class Process:
        returncode = None
        reaped = False

        def communicate(self, *, input, timeout):
            if failure == "oserror":
                raise OSError("pipe failed")
            if failure == "interrupt":
                raise KeyboardInterrupt()
            self.returncode = 3

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, *, timeout):
            self.reaped = True

    process = Process()
    expected = KeyboardInterrupt if failure == "interrupt" else module.MigrationRefused
    with pytest.raises(expected):
        module.BoundedPsql(lambda: process).run(module.PROCEDURE, timeout_seconds=60)
    assert process.reaped == (failure != "nonzero")


def test_bounded_psql_kills_and_reaps_on_timeout():
    class Process:
        returncode = None
        killed = False
        reaped = False

        def communicate(self, *, input, timeout):
            assert input == module.PROCEDURE and timeout == 60
            raise subprocess.TimeoutExpired("fixed", timeout)

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True

        def wait(self, *, timeout):
            assert timeout == 5
            self.returncode = -9
            self.reaped = True

    process = Process()
    with pytest.raises(module.MigrationRefused):
        module.BoundedPsql(lambda: process).run(module.PROCEDURE, timeout_seconds=60)
    assert process.killed and process.reaped


@pytest.mark.parametrize("source_phase", [True, False])
def test_explicit_physical_continuity_preserves_exact_schema_contract(
    scenario, source_phase
):
    approval, source, target = scenario
    raw = copy.deepcopy(source if source_phase else target)
    facts = Facts.parse(raw["facts"])
    identity = ("123456789", "1" * 64)
    raw["facts"]["server_address"] = "10.42.0.9"
    revision = module.SOURCE_REVISION if source_phase else module.TARGET_REVISION
    verify = module.verify_source if source_phase else module.verify_target
    with pytest.raises(module.MigrationRefused):
        verify(approval, module.LiveSchema(raw, (revision,)))
    continuity = module.PhysicalContinuity(facts, identity, identity, "10.42.0.9")
    proof = verify(approval, module.LiveSchema(raw, (revision,), continuity))
    assert proof.target == approval.target_identity
    raw["database_collation"]["ctype"] = "unexpected"
    with pytest.raises(module.MigrationRefused):
        verify(approval, module.LiveSchema(raw, (revision,), continuity))


@pytest.mark.parametrize(
    "field,value",
    [
        ("server_port", 5433),
        ("server_address", "10.42.0.99"),
        ("current_user", "wrong"),
    ],
)
def test_continuity_never_authorizes_other_catalog_facts(scenario, field, value):
    approval, source, _ = scenario
    raw = copy.deepcopy(source)
    facts = Facts.parse(raw["facts"])
    raw["facts"]["server_address"] = "10.42.0.9"
    raw["facts"][field] = value
    identity = ("123456789", "1" * 64)
    continuity = module.PhysicalContinuity(facts, identity, identity, "10.42.0.9")
    with pytest.raises(module.MigrationRefused):
        module.verify_source(
            approval, module.LiveSchema(raw, (module.SOURCE_REVISION,), continuity)
        )
