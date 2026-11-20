"""Offline consumer composition tests; fakes are not live deployment evidence."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.cicd import rollout_provider as provider
from tools.cicd.deploy_server import _VerifiedHost
from tools.cicd.maintenance import Images, Readiness, Status, run_maintenance
from tools.cicd.maintenance_schema import (
    PROCEDURE,
    PROCEDURE_SHA256,
    SOURCE_REVISION,
    TARGET_REVISION,
    LiveSchema,
)
from tools.cicd.release import IMAGE_REPOSITORIES, Context, Image, Release
from tools.cicd.rollout_backup import BackupConfig, BackupReceipt
from tools.cicd.schema_probe import (
    CATALOG_CLASSES,
    INVENTORY,
    Baseline,
    Profile,
    Snapshot,
    TargetMapping,
    canonical_signature,
    target_signature,
    upgrade_paths,
)


def release(sha: str, digit: str) -> Release:
    return Release(
        Context(sha, 10, 1),
        **{
            name: Image(repository, "sha256:" + digit * 64, "sha256:" + "c" * 64)
            for name, repository in IMAGE_REPOSITORIES.items()
        },
    )


def catalog() -> dict:
    value = {
        "profile": Profile("a" * 64).to_data(),
        "facts": {
            "database": "coreteams",
            "current_user": "probe",
            "session_user": "probe",
            "server_version_num": 160015,
            "server_address": "127.0.0.1",
            "server_port": 5432,
        },
        "database_collation": {
            "encoding": "UTF8",
            "provider": "c",
            "collate": "C",
            "ctype": "C",
            "icu_locale": None,
            "icu_rules": None,
            "version": None,
            "actual_version": None,
        },
        "inventory": {name: [] for name in INVENTORY},
        "unsupported": dict.fromkeys(CATALOG_CLASSES, 0),
        "counts": dict.fromkeys(INVENTORY, 0),
    }
    value["inventory"]["schemas"] = [{"identity": ["public"]}]
    value["counts"]["schemas"] = 1
    return value


@pytest.fixture
def setup(tmp_path, monkeypatch):
    events = []
    old = catalog()
    new = deepcopy(old)
    new["inventory"]["tables"] = [
        {"identity": ["public", "new_table"], "access_method": "heap"}
    ]
    new["counts"]["tables"] = 1
    profile = Profile("a" * 64)
    mapping = TargetMapping(
        "db", 5432, "/etc/ca", "reviewed", "coreteams", "probe", ("public",), profile
    )
    target_release = release("a" * 40, "b")
    target_signature_value = canonical_signature(new, profile)
    baseline = Baseline(
        target_release.context.sha,
        profile,
        ("public",),
        target_signature_value,
        mapping.signature(),
        target_signature_value,
        tuple(
            (head, target_signature_value)
            for head in sorted(upgrade_paths(TARGET_REVISION))
        ),
        TARGET_REVISION,
    )
    raw = {
        "schema": 1,
        "source": release(provider.SOURCE_SHA, "d").to_dict(),
        "transition": {
            "source_revision": SOURCE_REVISION,
            "source_signature": canonical_signature(old, profile),
            "baseline": baseline.to_data(),
            "mapping": mapping.to_data(),
            "target_identity": target_signature(mapping, Snapshot.parse(old).facts),
            "procedure_sha256": PROCEDURE_SHA256,
        },
        "source_runtime": {
            name: "sha256:" + "e" * 64 for name in ("backend", "frontend")
        },
        "target_runtime": {
            name: "sha256:" + "f" * 64 for name in ("backend", "frontend")
        },
        "approval": "operator-approved-20261120",
        "recovery_scope": "same-node",
    }
    state = {
        "applied": False,
        "bad_live": False,
        "backup_fail": False,
        "smoke_fail": False,
        "drift_after_restart": False,
        "ddl_fail": False,
        "bad_receipt": False,
    }

    def inspect(*, timeout_seconds):
        events.append("inspect_target" if state["applied"] else "inspect_source")
        if state["bad_live"] or (
            state["drift_after_restart"] and "restart_db" in events
        ):
            return LiveSchema(new, (TARGET_REVISION,))
        return (
            LiveSchema(new, (TARGET_REVISION,))
            if state["applied"]
            else LiveSchema(old, (SOURCE_REVISION,))
        )

    class FakeHost:
        def preflight(self, expected, *, source_probe):
            events.append("preflight")
            assert source_probe() == expected.source_signature

        def gate_ingress(self):
            events.append("gate")

        def stop_applications(self):
            events.append("stop_apps")

        def stop_database_for_cold_copy(self):
            events.append("stop_db")

        def restart_database(self):
            events.append("restart_db")

        def run(self, procedure, *, timeout_seconds):
            assert procedure == PROCEDURE and timeout_seconds == 60
            assert events[-1] == "inspect_source"
            assert "restart_db" in events
            events.append("ddl")
            if state["ddl_fail"]:
                raise TimeoutError("fake ambiguous DDL outcome")
            state["applied"] = True

        def start_backend(self, image):
            assert image.reference == Images.from_release(target_release).backend
            events.append("start_backend")

        def start_frontend(self, image):
            assert "private_smoke" in events and "private_readiness" in events
            events.append("start_frontend")

    def evidence(*, timeout_seconds):
        assert events.index("stop_apps") < len(events) and "stop_db" not in events
        events.append("database_evidence")
        return "actual-db-evidence"

    # Real validated config, but backup I/O itself is replaced by the boundary fake.
    roots = {}
    for name in ("pgdata", "home", "data", "s3_pvc", "local_root", "scratch_root"):
        roots[name] = (tmp_path / name).resolve()
        roots[name].mkdir(mode=0o700)
    certificate = tmp_path / "recipient.crt"
    certificate.write_bytes(b"fake certificate: never passed to OpenSSL")
    private_key = tmp_path / "recipient.key"
    private_key.write_bytes(b"fake private key: never passed to OpenSSL")
    private_key.chmod(0o600)

    def config(observed):
        return BackupConfig(
            **roots,
            recipient_cert=certificate,
            private_key=private_key,
            expected_database_evidence=observed,
            migration_preserves_s3=True,
        )

    def backup(config, host, s3):
        events.append("backup_readback_restore")
        if state["backup_fail"]:
            raise ValueError("fake backup failure")
        return BackupReceipt(
            tmp_path / "local.enc",
            "pre-ddl/exact.enc",
            "9" * 64,
            "unrelated-evidence"
            if state["bad_receipt"]
            else config.expected_database_evidence,
        )

    def private_ready(*, timeout_seconds):
        assert "start_backend" in events and "start_frontend" not in events
        events.append("private_readiness")
        return True

    def private_smoke(*, timeout_seconds):
        assert "start_frontend" not in events
        events.append("private_smoke")
        return not state["smoke_fail"]

    def readiness(*, timeout_seconds):
        assert "start_frontend" in events
        events.append("readiness")
        return Readiness(Images.from_release(target_release), (1, 1), (1, 1))

    def public_smoke(*, timeout_seconds):
        events.append("public_smoke")
        return True

    monkeypatch.setattr(
        provider, "protected_read", lambda path, limit: json.dumps(raw).encode()
    )
    monkeypatch.setattr(provider, "create_backup", backup)
    installed = provider.RolloutProvider(
        Path("/etc/coreteams-deploy/approval.json"),
        host=FakeHost(),
        inspect=inspect,
        database_evidence=evidence,
        backup_config=config,
        backup_host=object(),
        s3=object(),
        private_readiness=private_ready,
        readiness=readiness,
        functional_smoke=private_smoke,
        public_smoke=public_smoke,
    )
    return installed, target_release, raw, state, events


def consume(setup):
    installed, release_value, _, _, _ = setup
    prepared = installed.prepare(release_value, timeout_seconds=60)
    persisted = []
    result = run_maintenance(
        prepared.plan,
        host=_VerifiedHost(prepared.host, lambda requested: requested),
        persist=persisted.append,
    )
    return result, persisted, prepared


def test_full_consumer_composes_real_boundaries_in_safety_order(setup):
    result, persisted, _ = consume(setup)
    events = setup[-1]
    assert result.status == Status.SUCCEEDED
    assert [e.kind for e in result.evidence] == [
        "backup",
        "validated_backup",
        "backup_readback",
        "restore_check",
    ]
    assert all(e.kind != "off_host" for e in result.evidence)
    ordered = [
        "gate",
        "stop_apps",
        "database_evidence",
        "stop_db",
        "backup_readback_restore",
        "restart_db",
        "ddl",
        "start_backend",
        "private_readiness",
        "private_smoke",
        "start_frontend",
        "readiness",
        "public_smoke",
    ]
    assert [event for event in events if event in ordered] == ordered
    assert events[events.index("ddl") - 1] == "inspect_source"
    assert any(item.ddl_possible for item in persisted)


@pytest.mark.parametrize(
    "field,value",
    [
        ("recovery_scope", "off-host"),
        ("approval", ""),
        ("schema", True),
    ],
)
def test_bad_approval_refused_before_effects(setup, field, value):
    installed, target, raw, _, events = setup
    raw[field] = value
    with pytest.raises(ValueError):
        installed.prepare(target, timeout_seconds=60)
    assert events == []


def test_old_approval_cannot_authorize_future_same_schema_release(setup):
    installed, _, _, _, events = setup
    with pytest.raises(ValueError):
        installed.prepare(release("b" * 40, "b"), timeout_seconds=60)
    assert events == []


@pytest.mark.parametrize("mutation", ["source", "procedure", "sql", "runtime"])
def test_arbitrary_source_sql_or_runtime_refused(setup, mutation):
    installed, target, raw, _, events = setup
    if mutation == "source":
        raw["source"] = release("b" * 40, "d").to_dict()
    elif mutation == "procedure":
        raw["transition"]["procedure_sha256"] = "0" * 64
    elif mutation == "sql":
        raw["sql"] = "SELECT arbitrary"
    else:
        raw["target_runtime"]["backend"] = "mutable:tag"
    with pytest.raises(ValueError):
        installed.prepare(target, timeout_seconds=60)
    assert events == []


def test_live_source_mismatch_before_host_effects(setup):
    installed, target, _, state, events = setup
    state["bad_live"] = True
    with pytest.raises(ValueError):
        installed.prepare(target, timeout_seconds=60)
    assert events == ["inspect_source"]


def test_failed_backup_keeps_database_stopped_and_forbids_ddl(setup):
    setup[3]["backup_fail"] = True
    result, _, _ = consume(setup)
    assert result.status == Status.FAILED
    assert "backup_readback_restore" in setup[-1]
    assert "restart_db" not in setup[-1] and "ddl" not in setup[-1]


def test_failed_private_smoke_never_reopens_ingress(setup):
    setup[3]["smoke_fail"] = True
    result, _, _ = consume(setup)
    assert result.status == Status.FAILED
    assert "ddl" in setup[-1] and "private_smoke" in setup[-1]
    assert "start_frontend" not in setup[-1]


def test_server_verifier_wrapper_is_required(setup):
    installed, target, _, _, events = setup
    prepared = installed.prepare(target, timeout_seconds=60)
    result = run_maintenance(
        prepared.plan, host=prepared.host, persist=lambda state: None
    )
    assert result.status == Status.FAILED
    assert "gate" not in events


def test_fresh_source_drift_after_backup_blocks_ddl(setup):
    setup[3]["drift_after_restart"] = True
    result, _, _ = consume(setup)
    assert result.status == Status.FAILED
    assert "restart_db" in setup[-1] and "ddl" not in setup[-1]


def test_unrelated_restore_receipt_never_restarts_database(setup):
    setup[3]["bad_receipt"] = True
    result, _, _ = consume(setup)
    assert result.status == Status.FAILED
    assert "backup_readback_restore" in setup[-1]
    assert "restart_db" not in setup[-1] and "ddl" not in setup[-1]


def test_ambiguous_ddl_is_not_retried_or_recovered(setup):
    setup[3]["ddl_fail"] = True
    result, _, prepared = consume(setup)
    assert result.status == Status.FAILED and result.ddl_possible
    with pytest.raises(ValueError):
        run_maintenance(
            prepared.plan,
            host=prepared.host,
            persist=lambda state: None,
            previous=result,
        )
    assert setup[-1].count("ddl") == 1
    assert "start_backend" not in setup[-1] and "start_frontend" not in setup[-1]


def test_durable_state_blocks_same_release_ddl_replay(setup):
    result, _, prepared = consume(setup)
    with pytest.raises(ValueError):
        run_maintenance(
            prepared.plan,
            host=prepared.host,
            persist=lambda state: None,
            previous=result,
        )
    assert setup[-1].count("ddl") == 1
