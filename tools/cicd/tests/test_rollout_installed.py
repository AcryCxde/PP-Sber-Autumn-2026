"""Offline launcher tests: fake system boundaries are not production evidence."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.cicd import rollout_installed as installed


def smoke_body(path, status):
    if status == 403:
        return b'{"detail":"Admin role required"}'
    if status != 200:
        return b""
    if path == "/api/scenarios":
        from backend.scenarios import catalog

        return json.dumps(catalog()).encode()
    period = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-20T00:00:00+00:00"}
    page = {"items": [], "total": 4, "page": installed._SMOKE_PAGE, "page_size": 1}
    return json.dumps(
        {
            "as_of": "2026-11-20T12:00:00+00:00",
            "period": period,
            "completeness": "complete",
            "totals": {"runs": 4, "failed": 1, "outcome_unknown": 0, "running": 3},
            "without_started_at": 0,
            "active": page,
            "attention": {**page, "period": period, "no_start": False},
        }
    ).encode()


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
            "sessions": "/etc/coreteams-deploy/smoke-sessions.json",
            "private_http_trusted": True,
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
        lambda d: d["smoke"].pop("sessions"),
        lambda d: d["smoke"].update(sessions="relative"),
        lambda d: d["smoke"].update(private_http_trusted=False),
        lambda d: d["smoke"].update(private_http_trusted=1),
        lambda d: d["smoke"].pop("private_http_trusted"),
        lambda d: d["smoke"].update(admin_session="must-not-inline"),
    ],
)
def test_config_rejects_unreviewed_shape_and_routes(alter):
    data = config_data()
    alter(data)
    with pytest.raises(ValueError):
        installed.InstalledConfig.parse(json.dumps(data).encode())


def test_private_smoke_requires_both_sessions_before_http(monkeypatch):
    config = installed.InstalledConfig.parse(json.dumps(config_data()).encode())
    monkeypatch.setattr(installed, "protected_credential_read", lambda *a, **kw: b"{}")
    monkeypatch.setattr(
        installed, "_get", lambda *a, **kw: pytest.fail("HTTP before auth validation")
    )
    with pytest.raises(ValueError):
        installed._http_smoke(config, public=False, timeout_seconds=30)


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
            else json.dumps(
                {"admin_session": "A" * 43, "user_session": "U" * 43}
            ).encode()
            if p == config.smoke_sessions
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

    monkeypatch.setattr(installed, "available_memory", lambda: 2 * 1024**3)
    monkeypatch.setattr(installed, "preflight_backup", lambda *args, **kwargs: 1)

    class Host:
        def __init__(self, host_config, *, backup_paths):
            assert host_config == config.host_config
            assert backup_paths == {
                name: dict(config.backup)[name] for name in ("home", "data", "s3_pvc")
            }

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

    def smoke_get(origin, path, expected, **kwargs):
        events.append((origin, path, expected))
        if state.get("smoke") and kwargs.get("session"):
            raise ValueError("smoke refused")
        return smoke_body(path, expected)

    monkeypatch.setattr(installed, "_get", smoke_get)
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
    assert events.index(
        (config.private_origin, installed._ADMIN_SMOKE_PATH, 200)
    ) < events.index("frontend")
    assert events.index((config.private_origin, "/api/scenarios", 200)) < events.index(
        "frontend"
    )
    assert (config.public_origin, "/product/", 200) in events


@pytest.mark.parametrize("failure", ["drift", "ready_fail", "smoke"])
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


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"null",
        b"not json",
        b"\xff",
        b'{"admin_session": "A", "user_session": "B"}',
        json.dumps({"admin_session": "A" * 43, "user_session": "A" * 43}).encode(),
        json.dumps(
            {"admin_session": "A" * 42 + "\n", "user_session": "U" * 43}
        ).encode(),
        json.dumps(
            {"admin_session": "A" * 43, "user_session": "U" * 43, "extra": True}
        ).encode(),
        json.dumps({"admin_session": True, "user_session": "U" * 43}).encode(),
        b'{"admin_session":"first","admin_session":"second","user_session":"third"}',
    ],
)
def test_sessions_fail_closed_without_values_in_error(monkeypatch, raw):
    monkeypatch.setattr(installed, "protected_credential_read", lambda *a, **kw: raw)
    with pytest.raises(ValueError) as caught:
        installed._SmokeSessions.load(Path("/etc/coreteams-deploy/smoke-sessions.json"))
    assert str(caught.value) in {
        "invalid smoke sessions",
        "invalid smoke session",
        "distinct smoke sessions required",
    }


def test_sessions_protected_read_and_redacted_repr(monkeypatch):
    calls = []

    def read(path, *, exact_mode):
        calls.append((path, exact_mode))
        return json.dumps(
            {"admin_session": "A" * 43, "user_session": "U" * 43}
        ).encode()

    monkeypatch.setattr(installed, "protected_credential_read", read)
    path = Path("/etc/coreteams-deploy/smoke-sessions.json")
    sessions = installed._SmokeSessions.load(path)
    assert calls == [(path, True)]
    assert "A" * 43 not in repr(sessions) and "U" * 43 not in repr(sessions)


@pytest.mark.parametrize("status", [b"200", b"302", b"401", b"500"])
def test_authenticated_cookie_only_on_pipe_no_response_leak(
    monkeypatch, capsys, status
):
    calls = []
    token = "A" * 43

    def run(argv, *, stdin, timeout, env):
        calls.append((argv, stdin, env))
        return b'{"secret":"must not print"}\n' + status

    monkeypatch.setattr(installed, "bounded_run", run)
    if status == b"200":
        assert (
            installed._get(
                "http://127.0.0.1:8000",
                "/api/scenarios",
                200,
                deadline=installed._deadline(30),
                session=token,
            )
            == b'{"secret":"must not print"}'
        )
    else:
        with pytest.raises(ValueError, match="^HTTP smoke refused$"):
            installed._get(
                "http://127.0.0.1:8000",
                "/api/scenarios",
                200,
                deadline=installed._deadline(30),
                session=token,
            )
    argv, stdin, env = calls[0]
    assert token not in repr(argv) and token not in repr(env)
    assert stdin == f'cookie = "coreteams_session={token}"\n'.encode()
    assert argv[-2:] == ["--config", "-"]
    assert argv[argv.index("--request") + 1] == "GET"
    assert argv[argv.index("--output") + 1] == "-"
    assert "--location" not in argv and "--insecure" not in argv
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/var/empty"}
    assert capsys.readouterr() == ("", "")


def test_authenticated_cookie_never_sent_to_public(monkeypatch):
    monkeypatch.setattr(
        installed, "bounded_run", lambda *a, **kw: pytest.fail("secret request started")
    )
    with pytest.raises(ValueError):
        installed._get(
            installed.PUBLIC_ORIGIN,
            "/api/scenarios",
            200,
            deadline=installed._deadline(30),
            session="A" * 43,
        )


def test_public_smoke_does_not_read_or_send_sessions(monkeypatch):
    config = installed.InstalledConfig.parse(json.dumps(config_data()).encode())
    monkeypatch.setattr(
        installed,
        "protected_credential_read",
        lambda *a, **kw: pytest.fail("public secret read"),
    )
    calls = []

    def get(origin, path, expected, **kwargs):
        assert "session" not in kwargs
        calls.append((path, expected))

    monkeypatch.setattr(installed, "_get", get)
    assert installed._http_smoke(config, public=True, timeout_seconds=30)
    assert calls == [
        ("/", 200),
        ("/product/", 200),
        ("/api/auth/me", 401),
        ("/api/admin/v1/users", 401),
    ]


@pytest.mark.parametrize(
    "alter",
    [
        lambda d: d.update(email="private@example.test"),
        lambda d: d["totals"].update(runs=True),
        lambda d: d["active"].update(items=[{"email": "private@example.test"}]),
        lambda d: d["attention"].update(items=[{"id": "private-id"}]),
        lambda d: d.update(completeness="partial"),
        lambda d: d.update(totals={}),
    ],
)
def test_admin_smoke_rejects_unsafe_or_malformed_shape(alter):
    body = json.loads(smoke_body(installed._ADMIN_SMOKE_PATH, 200))
    alter(body)
    with pytest.raises(ValueError):
        installed._admin_shape(json.dumps(body).encode())


def test_user_smoke_rejects_nested_unexpected_pii():
    body = json.loads(smoke_body("/api/scenarios", 200))
    body["scenarios"][0]["roles"][0]["email"] = "private@example.test"
    with pytest.raises(ValueError):
        installed._user_shape(json.dumps(body).encode())


@pytest.mark.parametrize("route_failure", ["user", "admin", "denial", "swapped"])
def test_private_smoke_failures_refuse_success(monkeypatch, route_failure):
    config = installed.InstalledConfig.parse(json.dumps(config_data()).encode())
    monkeypatch.setattr(
        installed,
        "protected_credential_read",
        lambda *a, **kw: json.dumps(
            {"admin_session": "A" * 43, "user_session": "U" * 43}
        ).encode(),
    )

    def get(origin, path, status, **kwargs):
        if (
            route_failure == "swapped"
            and status == 403
            or route_failure == "user"
            and path == "/api/scenarios"
            and status == 200
            or route_failure == "admin"
            and path == installed._ADMIN_SMOKE_PATH
            and status == 200
        ):
            raise ValueError("HTTP smoke refused")
        if route_failure == "denial" and status == 403:
            return b'{"detail":"wrong"}'
        return smoke_body(path, status)

    monkeypatch.setattr(installed, "_get", get)
    with pytest.raises(ValueError):
        installed._http_smoke(config, public=False, timeout_seconds=30)


@pytest.mark.filterwarnings(
    "ignore:Using `httpx` with `starlette.testclient` is deprecated"
)
def test_current_app_cookie_routes_and_shapes_without_pii(tmp_path, monkeypatch):
    pytest.importorskip(
        "fastapi", reason="application fixtures are not installed in the classify job"
    )
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient

    import backend.main as main_module
    from backend.config import Settings
    from backend.db import Database
    from backend.models import Conversation, Run, User
    from backend.tests.auth_helpers import sign_in

    app = main_module.create_app(
        Database(f"sqlite+aiosqlite:///{tmp_path / 'database' / 'smoke.db'}"),
        Settings(workspace_root=tmp_path / "workspaces"),
    )
    with TestClient(app) as client:
        ordinary = sign_in(client, "smoke-user@example.test")
        user_session = client.cookies.get("coreteams_session")
        client.cookies.clear()
        administrator = sign_in(client, "smoke-admin@example.test")
        admin_session = client.cookies.get("coreteams_session")

        async def grant_test_role():
            async with app.state.database.session() as session:
                admin = await session.get(User, administrator["id"])
                admin.role = "admin"
                conversation = Conversation(
                    owner_id=ordinary["id"], title="private fixture title"
                )
                session.add(conversation)
                await session.flush()
                session.add_all(
                    [
                        Run(
                            conversation_id=conversation.id,
                            status="running",
                            client_request_id="smoke-running",
                            started_at=datetime.now(UTC),
                        ),
                        Run(
                            conversation_id=conversation.id,
                            status="failed",
                            client_request_id="smoke-failed",
                            started_at=datetime.now(UTC),
                        ),
                    ]
                )
                await session.commit()

        client.portal.call(grant_test_role)
        client.cookies.clear()
        monkeypatch.setattr(
            installed,
            "protected_credential_read",
            lambda *a, **kw: json.dumps(
                {"admin_session": admin_session, "user_session": user_session}
            ).encode(),
        )
        calls = []

        # Only replace process/network transport: real router/auth/session/SQL
        # and smoke status/body parsers execute together against the current app.
        def command(argv, *, stdin, timeout, env):
            path = argv[argv.index("--url") + 1].removeprefix("http://127.0.0.1:8000")
            token = None
            if stdin:
                token = (
                    stdin.decode().split("coreteams_session=", 1)[1].split('"', 1)[0]
                )
            headers = {} if token is None else {"Cookie": "coreteams_session=" + token}
            response = client.get(path, headers=headers, follow_redirects=False)
            calls.append((path, response.status_code))
            if token == admin_session and path == installed._ADMIN_SMOKE_PATH:
                body = response.json()
                assert body["active"]["total"] == 1 and body["attention"]["total"] == 1
                assert body["active"]["items"] == body["attention"]["items"] == []
                assert b"smoke-user@example.test" not in response.content
                assert b"private fixture title" not in response.content
            status = str(response.status_code).encode()
            return response.content + b"\n" + status if stdin else status

        monkeypatch.setattr(installed, "bounded_run", command)
        config = installed.InstalledConfig.parse(json.dumps(config_data()).encode())
        assert installed._http_smoke(config, public=False, timeout_seconds=30)
        assert (installed._ADMIN_SMOKE_PATH, 403) in calls
        assert (installed._ADMIN_SMOKE_PATH, 200) in calls
        assert ("/api/scenarios", 200) in calls
        assert ordinary["role"] == "user"


def test_missing_smoke_sessions_refused_before_host_construction(monkeypatch):
    config = installed.InstalledConfig.parse(json.dumps(config_data()).encode())

    def missing(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(installed, "protected_credential_read", missing)
    monkeypatch.setattr(
        installed.HostConfig, "load", lambda *a: pytest.fail("host constructed")
    )
    with pytest.raises(FileNotFoundError):
        installed.build_provider(config)
