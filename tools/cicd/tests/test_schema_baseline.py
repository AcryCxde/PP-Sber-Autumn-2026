"""Release-bound baseline boundary cases; no target connections."""

import copy
import json

import pytest

from tools.cicd.schema_baseline import (
    Baseline,
    TargetMapping,
    parse_json,
    release_source,
)
from tools.cicd.schema_probe import UPGRADE_PATHS, ProbeError, installed_catalog_profile

SHA = "8a6b5eb91cc92c211843048d18ac53d4f76fd74b"


def mapping_data():
    return {
        "schema": 1,
        "endpoint": "db.example.test",
        "port": 5432,
        "ca_file": "/etc/coreteams/ca.pem",
        "cluster_identity": "reviewed-cluster",
        "database": "coreteams",
        "role": "probe",
        "schemas": ["public"],
        "profile": installed_catalog_profile().to_data(),
    }


def baseline_data():
    mapping = TargetMapping.parse(mapping_data())
    signature = "pg_catalog_v1:sha256:" + "a" * 64
    return {
        "schema": 1,
        "release_sha": SHA,
        "profile": mapping.profile.to_data(),
        "schemas": ["public"],
        "expected_signature": signature,
        "target_mapping_hash": mapping.signature(),
        "clean_signature": signature,
        "upgrade_signatures": dict.fromkeys(UPGRADE_PATHS, signature),
        "migration_head": "20261111_0005",
    }


def test_baseline_binding():
    mapping = TargetMapping.parse(mapping_data())
    baseline = Baseline.parse(baseline_data())
    baseline.bind(SHA, mapping)
    assert baseline.to_data() == baseline_data()
    changed = mapping_data()
    changed["endpoint"] = "different.example.test"
    with pytest.raises(ProbeError):
        baseline.bind(SHA, TargetMapping.parse(changed))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("release_sha", "HEAD"),
        ("release_sha", "0" * 40),
        ("release_sha", "f" * 39),
        ("migration_head", "future"),
        ("schemas", ["public", "public"]),
        ("schemas", []),
        ("clean_signature", "pg_catalog_v1:sha256:" + "b" * 64),
        ("upgrade_signatures", {}),
        ("upgrade_signatures", {"live": "anything"}),
        ("unexpected", 1),
        ("target_mapping_hash", True),
    ],
)
def test_baseline_binding_rejects(field, value):
    data = baseline_data()
    data[field] = value
    with pytest.raises(ProbeError):
        Baseline.parse(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("port", True),
        ("port", 0),
        ("ca_file", "relative"),
        ("schemas", ["public", "public"]),
        ("schemas", []),
        ("password", "secret"),
    ],
)
def test_mapping_rejects(field, value):
    data = mapping_data()
    data[field] = value
    with pytest.raises(ProbeError):
        TargetMapping.parse(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("server_version_num", 160016),
        ("query_sha256", "b" * 64),
        ("settings", {}),
        ("inventory_version", True),
    ],
)
def test_baseline_binding_profile(field, value):
    data = copy.deepcopy(baseline_data())
    data["profile"][field] = value
    with pytest.raises(ProbeError):
        Baseline.parse(data).bind(SHA, TargetMapping.parse(mapping_data()))


@pytest.mark.parametrize("raw", ['{"schema":1,"schema":1}', '{"x":NaN}', "[]"])
def test_json_rejects(raw):
    with pytest.raises(ProbeError):
        parse_json(raw.encode())


def test_json_bound():
    with pytest.raises(ProbeError):
        parse_json(b" " * 65537)
    assert parse_json(json.dumps(mapping_data()).encode()) == mapping_data()


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint", "different.test"),
        ("port", 6432),
        ("ca_file", "/other/ca.pem"),
        ("cluster_identity", "other-cluster"),
        ("database", "other"),
        ("role", "other"),
        ("schemas", ["other"]),
    ],
)
def test_mapping_hash_binds_every_field(field, value):
    data = mapping_data()
    data[field] = value
    with pytest.raises(ProbeError):
        Baseline.parse(baseline_data()).bind(SHA, TargetMapping.parse(data))


def test_baseline_rejects_unequal_upgrade_and_direct_mutability():
    data = baseline_data()
    data["upgrade_signatures"][UPGRADE_PATHS[0]] = "pg_catalog_v1:sha256:" + "b" * 64
    with pytest.raises(ProbeError):
        Baseline.parse(data)
    mapping = TargetMapping.parse(mapping_data())
    from dataclasses import replace

    with pytest.raises(ProbeError):
        replace(mapping, schemas=["public"])
    with pytest.raises(ProbeError):
        Baseline.parse(baseline_data()).bind("b" * 40, mapping)


def test_generate_refuses_existing_output_before_any_effect(tmp_path, monkeypatch):
    from tools.cicd import schema_baseline as module

    output = tmp_path / "existing"
    output.write_text("preserve")

    def forbidden(*args):
        pytest.fail("must not create a container")

    monkeypatch.setattr(module, "owned_postgres", forbidden)
    with pytest.raises(ProbeError):
        module.generate(tmp_path, SHA, TargetMapping.parse(mapping_data()), output)
    assert output.read_text() == "preserve"


def test_launch_timeout_cleans_recorded_name(monkeypatch):
    import subprocess

    from tools.cicd import schema_baseline as module

    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "run":
            raise subprocess.TimeoutExpired("docker", 30)
        if args[0] == "stop":
            raise ProbeError("cleanup failed")
        return ""

    monkeypatch.setattr(module, "_docker", docker)
    with pytest.raises(subprocess.TimeoutExpired), module.owned_postgres():
        pytest.fail("launch should fail")
    launch = next(args for args in calls if args[0] == "run")
    name = launch[launch.index("--name") + 1]
    assert name.startswith("schema-baseline-")
    assert ("stop", "--time", "2", name) in calls


def test_partial_output_failure_is_retryable(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from tools.cicd import schema_baseline as module

    @contextmanager
    def source(*args):
        yield tmp_path

    @contextmanager
    def postgres():
        yield "owned", 1234

    monkeypatch.setattr(module, "release_source", source)
    monkeypatch.setattr(module, "owned_postgres", postgres)
    monkeypatch.setattr(
        module, "collect_baseline", lambda *args: Baseline.parse(baseline_data())
    )
    original = module.os.fdopen

    @contextmanager
    def broken(fd, mode):
        with original(fd, mode) as stream:

            class Writer:
                def write(self, payload):
                    stream.write(payload[:12])
                    raise OSError("disk full")

            yield Writer()

    output = tmp_path / "baseline.json"
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fdopen", broken)
        with pytest.raises(ProbeError):
            module.generate(tmp_path, SHA, TargetMapping.parse(mapping_data()), output)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []
    module.generate(tmp_path, SHA, TargetMapping.parse(mapping_data()), output)
    assert Baseline.parse(parse_json(output.read_bytes())) == Baseline.parse(
        baseline_data()
    )


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_atomic_output_refuses_publication_racer(tmp_path, monkeypatch, kind):
    from tools.cicd import schema_baseline as module

    output = tmp_path / "baseline.json"
    original = module.os.link

    def race(source, destination):
        if kind == "file":
            destination.write_bytes(b"racer")
        else:
            destination.symlink_to(tmp_path / "missing")
        original(source, destination)

    monkeypatch.setattr(module.os, "link", race)
    with pytest.raises(ProbeError):
        module._publish(output, b"complete baseline")
    if kind == "file":
        assert output.read_bytes() == b"racer"
    else:
        assert output.is_symlink()
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("stage", ["file", "directory"])
def test_atomic_output_fsync_failure_policy(tmp_path, monkeypatch, stage):
    from tools.cicd import schema_baseline as module

    output = tmp_path / "baseline.json"
    calls = 0

    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if stage == "file" else 2):
            raise OSError("fsync failure")

    monkeypatch.setattr(module.os, "fsync", fsync)
    with pytest.raises(ProbeError):
        module._publish(output, b"complete baseline")
    if stage == "file":
        assert not output.exists()
        assert list(tmp_path.iterdir()) == []
    else:
        assert output.read_bytes() == b"complete baseline"
        assert list(tmp_path.iterdir()) == [output]


def test_source_unknown_and_alias(tmp_path):
    for sha in ("HEAD", "f" * 40):
        with pytest.raises(ProbeError), release_source(tmp_path, sha):
            pytest.fail("invalid source accepted")
