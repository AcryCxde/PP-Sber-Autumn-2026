"""Installed provider contracts; no production files or network access."""

import asyncio
import json
import os
import ssl
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.cicd import schema_probe as probe
from tools.cicd.deploy_server import Clearance, SchemaEvidence


def test_trusted_probe_factory_is_lazy(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("factory performed I/O")

    monkeypatch.setattr(probe.Path, "read_bytes", forbidden)
    clearance = Clearance(
        "a" * 40, SchemaEvidence("signature", "target"), "p", "r", "a"
    )
    assert callable(probe.installed_probe("a" * 40, clearance))


def test_trusted_probe_expected_before_network(monkeypatch):
    from tools.cicd.schema_probe import UPGRADE_PATHS

    profile = probe.installed_catalog_profile()
    mapping = probe.TargetMapping(
        "db.example.test",
        6432,
        "/protected/ca.pem",
        "cluster-1",
        "coreteams",
        "probe",
        ("public",),
        profile,
    )
    signature = "pg_catalog_v1:sha256:" + "b" * 64
    baseline = probe.Baseline(
        "a" * 40,
        profile,
        ("public",),
        signature,
        mapping.signature(),
        signature,
        tuple((revision, signature) for revision in sorted(UPGRADE_PATHS)),
    )
    clearance = Clearance("a" * 40, SchemaEvidence(signature, "target"), "p", "r", "a")

    def forbidden(*args, **kwargs):
        pytest.fail("baseline mismatch reached network")

    monkeypatch.setattr(probe, "read_catalog", forbidden)
    with pytest.raises(probe.ProbeError):
        probe.probe(
            mapping,
            baseline,
            replace(clearance, evidence=SchemaEvidence("live-drift", "target")),
            "a" * 40,
        )


@pytest.fixture
def installed(monkeypatch):
    profile = probe.installed_catalog_profile()
    mapping = probe.TargetMapping(
        "db.example.test",
        6432,
        "/ca.pem",
        "cluster",
        "coreteams",
        "probe",
        ("public",),
        profile,
    )
    raw = {
        "profile": profile.to_data(),
        "facts": {
            "database": "coreteams",
            "current_user": "probe",
            "session_user": "probe",
            "server_version_num": 160015,
            "server_address": "10.20.30.40",
            "server_port": 5432,
        },
        "inventory": {section: [] for section in probe.INVENTORY},
        "unsupported": dict.fromkeys(probe.CATALOG_CLASSES, 0),
        "counts": dict.fromkeys(probe.INVENTORY, 0),
    }
    raw["inventory"]["schemas"] = [{"identity": ["public"]}]
    raw["counts"]["schemas"] = 1
    signature = probe.canonical_signature(raw, profile)
    baseline = probe.Baseline(
        "a" * 40,
        profile,
        mapping.schemas,
        signature,
        mapping.signature(),
        signature,
        tuple((r, signature) for r in sorted(probe.UPGRADE_PATHS)),
    )
    clearance = Clearance(
        "a" * 40,
        SchemaEvidence(
            signature, probe.target_signature(mapping, probe.Facts.parse(raw["facts"]))
        ),
        "p",
        "r",
        "a",
    )
    files = {
        probe.CONFIG_PATH: json.dumps(mapping.to_data()).encode(),
        probe.BASELINE_PATH: json.dumps(baseline.to_data()).encode(),
        probe.CREDENTIAL_PATH: b'{"username":"probe","password":"secret"}',
        Path(mapping.ca_file): b"test CA",
        Path(probe.__file__).with_name("schema_catalog.sql"): Path(probe.__file__)
        .with_name("schema_catalog.sql")
        .read_bytes(),
    }
    monkeypatch.setattr(probe, "protected_read", lambda path, limit: files[path])
    monkeypatch.setattr(probe, "protected_credential_read", lambda path: files[path])
    monkeypatch.setattr(
        ssl,
        "create_default_context",
        lambda **kwargs: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
    )
    calls = []

    async def read(mapping, **kwargs):
        calls.append((mapping, kwargs))
        return raw

    monkeypatch.setattr(probe, "read_catalog", read)
    return SimpleNamespace(
        mapping=mapping,
        baseline=baseline,
        clearance=clearance,
        raw=raw,
        files=files,
        calls=calls,
    )


def test_trusted_probe_success(installed):
    assert (
        probe.installed_probe("a" * 40, installed.clearance)()
        == installed.clearance.evidence
    )
    connection, options = installed.calls[0]
    assert connection.port == 6432
    assert options == {"password": "secret"}
    assert connection.tls_context.check_hostname
    assert connection.tls_context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "fact,value",
    [
        ("database", "wrong"),
        ("current_user", "owner"),
        ("session_user", "owner"),
        ("server_address", "10.20.30.41"),
        ("server_port", 5433),
        ("server_version_num", 160014),
    ],
)
def test_trusted_probe_actual_identity(installed, fact, value):
    installed.raw["facts"][fact] = value
    with pytest.raises(probe.ProbeError, match="installed catalog probe refused"):
        probe.installed_probe("a" * 40, installed.clearance)()


@pytest.mark.parametrize(
    "path,raw",
    [
        (probe.CONFIG_PATH, b'{"schema":1,"schema":1}'),
        (probe.CONFIG_PATH, b'{"extra":true}'),
        (probe.BASELINE_PATH, b"{}"),
        (probe.CREDENTIAL_PATH, b'{"username":"owner","password":"secret"}'),
        (
            probe.CREDENTIAL_PATH,
            b'{"username":"probe","password":"secret","dsn":"evil"}',
        ),
    ],
)
def test_trusted_probe_protected_parsing(installed, path, raw):
    installed.files[path] = raw
    with pytest.raises(probe.ProbeError) as error:
        probe.installed_probe("a" * 40, installed.clearance)()
    assert str(error.value) == "installed catalog probe refused"
    assert not installed.calls


def test_trusted_probe_drift_cannot_bless(installed):
    clearance = replace(
        installed.clearance,
        evidence=SchemaEvidence(
            "pg_catalog_v1:sha256:" + "f" * 64, installed.clearance.evidence.target
        ),
    )
    with pytest.raises(probe.ProbeError):
        probe.installed_probe("a" * 40, clearance)()
    assert not installed.calls


@pytest.mark.parametrize(
    "change", ["port", "cluster", "sha", "query", "minor", "tls", "ca", "driver"]
)
def test_trusted_probe_fail_closed(installed, monkeypatch, change):
    if change in {"port", "cluster", "query", "minor"}:
        data = json.loads(installed.files[probe.CONFIG_PATH])
        if change == "port":
            data["port"] += 1
        elif change == "cluster":
            data["cluster_identity"] = "other"
        elif change == "query":
            data["profile"]["query_sha256"] = "f" * 64
        else:
            data["profile"]["server_version_num"] = 160014
        installed.files[probe.CONFIG_PATH] = json.dumps(data).encode()
    elif change == "tls":
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.check_hostname = False
        monkeypatch.setattr(ssl, "create_default_context", lambda **kw: tls)
    elif change == "ca":

        def invalid_ca(**kw):
            raise ssl.SSLError("secret certificate input")

        monkeypatch.setattr(ssl, "create_default_context", invalid_ca)
    elif change == "driver":

        async def unavailable(*a, **kw):
            raise probe.ProbeError("catalog driver unavailable") from ImportError(
                "secret"
            )

        monkeypatch.setattr(probe, "read_catalog", unavailable)
    with pytest.raises(probe.ProbeError) as error:
        probe.installed_probe(
            "b" * 40 if change == "sha" else "a" * 40, installed.clearance
        )()
    assert str(error.value) == "installed catalog probe refused"
    assert not installed.calls


def test_trusted_probe_active_loop_refusal(installed):
    async def run():
        with pytest.raises(probe.ProbeError):
            probe.installed_probe("a" * 40, installed.clearance)()

    asyncio.run(run())
    assert not installed.calls


@pytest.mark.parametrize(
    "change",
    [
        "uid",
        "writable",
        "symlink",
        "oversize",
        "empty",
        "parent_symlink",
        "parent_writable",
    ],
)
def test_trusted_probe_path_refusal(tmp_path, monkeypatch, change):
    # Real descriptors and no-follow traversal, with root ownership simulated.
    path = tmp_path / "config.json"
    path.write_bytes(b"{}")
    original = os.fstat

    def fake(fd):
        info = original(fd)
        mode, uid = info.st_mode, 0
        is_file = stat.S_ISREG(mode)
        if not is_file:
            mode = stat.S_IFDIR | 0o755
        if change == "uid" and is_file:
            uid = 1234
        if change == "writable" and is_file:
            mode |= 0o020
        if change == "parent_writable" and not is_file:
            mode = stat.S_IFDIR | 0o1777
        return SimpleNamespace(st_mode=mode, st_uid=uid, st_size=info.st_size)

    monkeypatch.setattr(os, "fstat", fake)
    if change == "symlink":
        link = tmp_path / "link"
        link.symlink_to(path)
        path = link
    elif change == "parent_symlink":
        link = tmp_path / "parent"
        link.symlink_to(tmp_path, target_is_directory=True)
        path = link / "config.json"
    elif change == "oversize":
        path.write_bytes(b"x" * 17)
    elif change == "empty":
        path.write_bytes(b"")
    with pytest.raises((probe.ProbeError, OSError)):
        probe.protected_read(path, 16)


@pytest.mark.parametrize("parent_mode", [0o755, 0o1777])
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o600, 0o400])
def test_trusted_probe_credential_confidentiality(
    tmp_path, monkeypatch, mode, parent_mode
):
    path = tmp_path / "credential.json"
    path.write_bytes(b'{"username":"probe","password":"secret"}')
    path.chmod(mode)
    original = os.fstat

    def root_owned(fd):
        info = original(fd)
        # Model the reviewed installation, not the host's temporary directories.
        file_mode = (
            stat.S_IFDIR | parent_mode if stat.S_ISDIR(info.st_mode) else info.st_mode
        )
        return SimpleNamespace(st_mode=file_mode, st_uid=0, st_size=info.st_size)

    monkeypatch.setattr(os, "fstat", root_owned)
    if mode & 0o077 or parent_mode & 0o022:
        with pytest.raises(probe.ProbeError) as error:
            probe.protected_credential_read(path)
        assert "secret" not in str(error.value)
    else:
        assert probe.protected_credential_read(path) == path.read_bytes()


def test_trusted_probe_path_success(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_bytes(b"{}")
    original = os.fstat
    monkeypatch.setattr(
        os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_mode=original(fd).st_mode & ~0o022,
            st_uid=0,
            st_size=original(fd).st_size,
        ),
    )
    assert probe.protected_read(path, 16) == b"{}"
