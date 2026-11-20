"""Offline launcher tests: fake system boundaries are not production evidence."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.cicd import rollout_installed as installed


def config_data():
    return {
        "schema": 1,
        "host_config": "/etc/coreteams-deploy/rollout-host.json",
        "approval": "/etc/coreteams-deploy/rollout-approval.json",
        "backup": {
            name: "/var/lib/coreteams-deploy/" + name
            for name in (
                "pgdata",
                "home",
                "data",
                "s3_pvc",
                "local_root",
                "scratch_root",
            )
        }
        | {
            "recipient_cert": "/etc/coreteams-deploy/backup.crt",
            "private_key": "/etc/coreteams-deploy/backup.key",
        },
        "s3": {
            "endpoint": "http://127.0.0.1:8333",
            "credential": "/etc/coreteams-deploy/backup-s3.json",
        },
        "smoke": {
            "private_origin": "http://127.0.0.1:8000",
            "public_origin": "https://coreteams.2026.tochka-urfu.tech",
        },
    }


@pytest.mark.parametrize(
    "alter",
    [
        lambda d: d.update(schema=True),
        lambda d: d.update(command="sh"),
        lambda d: d["backup"].update(pgdata="relative"),
        lambda d: d["backup"].update(private_key="/etc/../secret"),
        lambda d: d["s3"].update(endpoint="http://untrusted.test:8333"),
        lambda d: d["s3"].update(endpoint="http://127.0.0.1:8333/bucket"),
        lambda d: d["smoke"].update(private_origin="http://remote.test:8000"),
        lambda d: d["smoke"].update(public_origin="https://u:p@host"),
        lambda d: d["smoke"].update(public_origin="https://host/path"),
        lambda d: d["smoke"].update(public_origin="http://host"),
    ],
)
def test_config_rejects_unreviewed_shape_and_routes(alter):
    data = config_data()
    alter(data)
    with pytest.raises(ValueError):
        installed.InstalledConfig.parse(json.dumps(data).encode())


def test_duplicate_config_keys_refused():
    with pytest.raises(ValueError):
        installed.InstalledConfig.parse(b'{"schema":1,"schema":1}')


def test_explicit_protected_config_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        installed,
        "protected_read",
        lambda p, n: calls.append(p) or json.dumps(config_data()).encode(),
    )
    loaded = installed.InstalledConfig.load()
    assert calls == [Path("/etc/coreteams-deploy/rollout-installed.json")]
    assert loaded.s3_endpoint == "http://127.0.0.1:8333"


def test_main_refuses_missing_activation_without_legacy_fallback(monkeypatch, capsys):
    monkeypatch.setattr(installed.os.path, "lexists", lambda p: False)
    monkeypatch.setattr(
        installed.deploy_credentials,
        "main",
        lambda **kw: pytest.fail("legacy launcher invoked"),
    )
    assert installed.main() == 1
    assert capsys.readouterr().err == "installed rollout launcher refused\n"


@pytest.fixture
def composed(tmp_path, monkeypatch):
    from tools.cicd import rollout_provider
    from tools.cicd.deploy_server import _VerifiedHost
    from tools.cicd.maintenance import Images, run_maintenance
    from tools.cicd.maintenance_schema import (
        PROCEDURE,
        SOURCE_REVISION,
        TARGET_REVISION,
    )
    from tools.cicd.rollout_backup import BackupReceipt
    from tools.cicd.schema_probe import (
        Baseline,
        Profile,
        Snapshot,
        TargetMapping,
        canonical_signature,
        target_signature,
        upgrade_paths,
    )
    from tools.cicd.tests.test_rollout_provider import catalog, release
    from tools.cicd.tests.test_rollout_restore import evidence

    events, state = (
        [],
        {
            "applied": False,
            "drift": False,
            "ready_fail": False,
            "restarted": False,
            "failure": None,
        },
    )
    data = config_data()
    for name in installed.BACKUP_PATHS:
        path = (tmp_path / name).resolve()
        if name in {"recipient_cert", "private_key"}:
            path.write_text("boundary fake certificate/key")
            path.chmod(0o600)
        else:
            path.mkdir(mode=0o700)
        data["backup"][name] = str(path)
    config = installed.InstalledConfig.parse(json.dumps(data).encode())
    old, sql = catalog(), b"installed catalog SQL boundary"
    profile = Profile(installed.hashlib.sha256(sql).hexdigest())
    old["profile"] = profile.to_data()
    new = deepcopy(old)
    new["inventory"]["tables"] = [
        {"identity": ["public", "new"], "access_method": "heap"}
    ]
    new["counts"]["tables"] = 1
    mapping = TargetMapping(
        "db", 5432, "/etc/ca", "reviewed", "coreteams", "probe", ("public",), profile
    )
    target = release("a" * 40, "b")
    signature = canonical_signature(new, profile)
    baseline = Baseline(
        target.context.sha,
        profile,
        ("public",),
        signature,
        mapping.signature(),
        signature,
        tuple((head, signature) for head in sorted(upgrade_paths(TARGET_REVISION))),
        TARGET_REVISION,
    )
    approval = {
        "schema": 1,
        "source": release(rollout_provider.SOURCE_SHA, "d").to_dict(),
        "transition": {
            "source_revision": SOURCE_REVISION,
            "source_signature": canonical_signature(old, profile),
            "baseline": baseline.to_data(),
            "mapping": mapping.to_data(),
            "target_identity": target_signature(mapping, Snapshot.parse(old).facts),
            "procedure_sha256": rollout_provider.PROCEDURE_SHA256,
        },
        "source_runtime": dict.fromkeys(("backend", "frontend"), "sha256:" + "e" * 64),
        "target_runtime": dict.fromkeys(("backend", "frontend"), "sha256:" + "f" * 64),
        "approval": "reviewed-test",
        "recovery_scope": "same-node",
    }
    files = {
        config.approval: json.dumps(approval).encode(),
        installed.schema_probe.CONFIG_PATH: json.dumps(mapping.to_data()).encode(),
        installed.schema_probe.BASELINE_PATH: json.dumps(baseline.to_data()).encode(),
        Path(installed.schema_probe.__file__).with_name("schema_catalog.sql"): sql,
        Path(mapping.ca_file): b"fake CA at TLS boundary",
    }
    monkeypatch.setattr(installed, "protected_read", lambda p, n: files[p])
    monkeypatch.setattr(rollout_provider, "protected_read", lambda p, n: files[p])
    monkeypatch.setattr(
        installed,
        "protected_credential_read",
        lambda p, **kw: (
            json.dumps({"username": "probe", "password": "never print"}).encode()
            if p == installed.schema_probe.CREDENTIAL_PATH
            else b"protected key"
        ),
    )
    monkeypatch.setattr(
        installed.ssl,
        "create_default_context",
        lambda **kw: SimpleNamespace(
            check_hostname=True, verify_mode=installed.ssl.CERT_REQUIRED
        ),
    )
    monkeypatch.setattr(installed.HostConfig, "load", lambda p: p)

    class Host:
        def __init__(self, config):
            pass

        def database_identity(self, *, endpoint, port, pgdata, timeout_seconds):
            from tools.cicd.rollout_host import DatabaseIdentity

            events.append("identity")
            drift = state["restarted"] and state["failure"]
            return DatabaseIdentity(
                "999" if drift == "cluster" else evidence()["system_identifier"],
                "2" * 64 if drift == "binding" else "1" * 64,
                "4" * 64 if state["restarted"] else "3" * 64,
                "10.42.0.9" if state["restarted"] else old["facts"]["server_address"],
                old["facts"]["server_port"],
                (TARGET_REVISION if state["applied"] else SOURCE_REVISION,),
            )

        def database_evidence(self, *, timeout_seconds):
            events.append("evidence")
            value = evidence()
            if state["failure"] == "backup_cluster":
                value["system_identifier"] = "999"
            return json.dumps(value)

        def preflight(self, expected, *, source_probe):
            assert source_probe() == expected.source_signature

        def gate_ingress(self):
            events.append("gate")

        def stop_applications(self):
            events.append("stop_apps")

        def stop_database_for_cold_copy(self):
            events.append("stop_db")

        def assert_quiescent(self):
            events.append("quiescent")

        def restart_database(self):
            events.append("restart_db")
            state["restarted"] = True

        def run(self, procedure, *, timeout_seconds):
            assert procedure == PROCEDURE and timeout_seconds == 60
            events.append("ddl")
            state["applied"] = True

        def start_backend(self, pin):
            events.append("backend")

        def start_frontend(self, pin):
            events.append("frontend")

        def confirm_ready(self, *, frontend, timeout_seconds):
            if state["ready_fail"]:
                raise ValueError("not ready")
            events.append("ready_all" if frontend else "ready_backend")

        def ready_images(self, *, timeout_seconds):
            events.append("observed_images")
            return Images.from_release(target)

    monkeypatch.setattr(installed, "K3sHost", Host)

    async def read_catalog(connection, *, password):
        assert password == "never print" and connection.role == "probe"
        events.append("catalog_target" if state["applied"] else "catalog_source")
        value = deepcopy(old if state["drift"] else new if state["applied"] else old)
        if state["restarted"]:
            value["facts"]["server_address"] = (
                "10.42.0.99" if state["failure"] == "peer" else "10.42.0.9"
            )
        if state["failure"] == "initial_peer":
            value["facts"]["server_address"] = "10.42.0.99"
        return value

    monkeypatch.setattr(installed.schema_probe, "read_catalog", read_catalog)
    s3 = object()
    monkeypatch.setattr(installed, "_s3", lambda config: s3)
    monkeypatch.setattr(
        installed.RestoreVerifier,
        "verify_restore",
        lambda self, p: (
            events.append("scratch_restore")
            or installed.sanitize_evidence(json.dumps(evidence()))
        ),
    )

    def backup(config, host, client):
        assert client is s3
        host.assert_quiescent()
        actual = host.verify_restore(config.scratch_root / "isolated-fake-pgdata")
        assert actual == config.expected_database_evidence
        return BackupReceipt(
            tmp_path / "ciphertext", "pre-ddl/backup", "9" * 64, actual
        )

    monkeypatch.setattr(rollout_provider, "create_backup", backup)
    monkeypatch.setattr(
        installed,
        "_get",
        lambda origin, path, expected, **kw: events.append((origin, path, expected)),
    )
    provider = installed.build_provider(config)

    def run():
        prepared = provider.prepare(target, timeout_seconds=60)
        return run_maintenance(
            prepared.plan,
            host=_VerifiedHost(prepared.host, lambda requested: requested),
            persist=lambda value: None,
        )

    return run, events, state, config


def test_actual_closures_compose_existing_provider_and_migration(composed):
    from tools.cicd.maintenance import Status

    run, events, _, config = composed
    result = run()
    assert result.status == Status.SUCCEEDED
    assert (
        events.index("evidence")
        < events.index("stop_db")
        < events.index("scratch_restore")
        < events.index("ddl")
    )
    assert (
        events.index("ddl") < events.index("catalog_target") < events.index("backend")
    )
    assert (
        events.index("ready_backend")
        < events.index("frontend")
        < events.index("observed_images")
    )
    assert (config.private_origin, "/api/admin/v1/users", 401) in events
    assert (config.public_origin, "/product/", 200) in events


@pytest.mark.parametrize("failure", ["drift", "ready_fail"])
def test_actual_observation_failure_never_releases_frontend(composed, failure):
    from tools.cicd.maintenance import Status

    run, events, state, _ = composed
    state[failure] = True
    result = run()
    assert result.status != Status.SUCCEEDED
    assert "frontend" not in events


def test_main_injects_provider_and_sanitizes_failure(monkeypatch, capsys):
    monkeypatch.setattr(installed.sys, "argv", ["launcher"])
    monkeypatch.setattr(installed.os.path, "lexists", lambda p: True)
    monkeypatch.setattr(installed.os.environ, "clear", lambda: None)
    config, provider = object(), object()
    monkeypatch.setattr(installed.InstalledConfig, "load", lambda: config)
    monkeypatch.setattr(installed, "build_provider", lambda c: provider)
    calls = []
    monkeypatch.setattr(
        installed.deploy_credentials, "main", lambda **kw: calls.append(kw) or 0
    )
    assert installed.main() == 0
    assert calls == [{"maintenance_provider": provider}]

    def refused(c):
        raise RuntimeError("secret must not escape")

    monkeypatch.setattr(installed, "build_provider", refused)
    assert installed.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "installed rollout launcher refused\n"


@pytest.mark.parametrize("status", [b"200", b"301", b"404", b"200\nsecret"])
def test_fixed_get_no_redirect_or_credential_inheritance(monkeypatch, status):
    calls = []

    def command(argv, *, stdin, timeout, env):
        calls.append((argv, stdin, timeout, env))
        return status

    monkeypatch.setattr(installed, "bounded_run", command)
    if status == b"200":
        installed._get(
            installed.PUBLIC_ORIGIN, "/", 200, deadline=installed._deadline(30)
        )
    else:
        with pytest.raises(ValueError):
            installed._get(
                installed.PUBLIC_ORIGIN, "/", 200, deadline=installed._deadline(30)
            )
    argv, stdin, timeout, env = calls[0]
    assert argv[0:2] == ["/usr/bin/curl", "--disable"]
    assert argv[argv.index("--request") + 1] == "GET"
    assert "--location" not in argv and "-L" not in argv
    assert argv[argv.index("--max-redirs") + 1] == "0"
    assert argv[argv.index("--url") + 1] == installed.PUBLIC_ORIGIN + "/"
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/var/empty"}
    assert stdin == b"" and timeout <= 30


def test_s3_explicit_protected_credentials_and_bounded_client(monkeypatch):
    import sys

    config = installed.InstalledConfig.parse(json.dumps(config_data()).encode())
    calls = []
    monkeypatch.setattr(
        installed,
        "protected_credential_read",
        lambda p, **kw: json.dumps(
            {"access_key_id": "fixed-access", "secret_access_key": "fixed-secret"}
        ).encode(),
    )
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda *a, **kw: calls.append((a, kw)) or "client"),
    )
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "botocore.config", SimpleNamespace(Config=lambda **kw: kw)
    )
    assert installed._s3(config) == "client"
    args, kwargs = calls[0]
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == config.s3_endpoint
    assert kwargs["aws_access_key_id"] == "fixed-access"
    assert kwargs["aws_secret_access_key"] == "fixed-secret"
    assert kwargs["aws_session_token"] == ""
    assert kwargs["config"] == {
        "connect_timeout": 5,
        "read_timeout": 30,
        "retries": {"total_max_attempts": 2},
        "proxies": {},
        "s3": {"addressing_style": "path"},
        "signature_version": "s3v4",
    }


@pytest.mark.parametrize("failure", ["cluster", "binding", "peer"])
def test_restarted_endpoint_drift_never_reaches_ddl(composed, failure):
    from tools.cicd.maintenance import Status

    run, events, state, _ = composed
    state["failure"] = failure
    result = run()
    assert result.status != Status.SUCCEEDED
    assert "restart_db" in events
    assert "ddl" not in events and "backend" not in events and "frontend" not in events


def test_initial_endpoint_must_match_approved_old_probe_target(composed):
    run, events, state, _ = composed
    state["failure"] = "initial_peer"
    with pytest.raises(ValueError):
        run()
    assert "gate" not in events


def test_backup_identity_must_match_actually_verified_db(composed):
    from tools.cicd.maintenance import Status

    run, events, state, _ = composed
    state["failure"] = "backup_cluster"
    result = run()
    assert result.status != Status.SUCCEEDED
    assert "stop_db" not in events and "ddl" not in events
