"""Local deployment boundaries; never contact GitHub or a cluster."""

import fcntl
import io
import json
import multiprocessing
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.cicd import deploy_server as server
from tools.cicd.release import IMAGE_REPOSITORIES, Context, Image, Release
from tools.cicd.verify_release import verify_freshness


def release(sha: str = "a" * 40) -> Release:
    return Release(
        Context(sha, 10, 1),
        **{
            n: Image(r, "sha256:" + "b" * 64, "sha256:" + "c" * 64)
            for n, r in IMAGE_REPOSITORIES.items()
        },
    )


class FakeGitHub:
    def __init__(self) -> None:
        self.paths = ["README.md"]
        self.added = False
        self.mode = "100644"
        self.status = "ahead"
        self.truncated = False
        self.calls = []

    def get(self, path: str) -> dict:
        self.calls.append(path)
        if "/compare/" in path:
            return {"status": self.status, "merge_base_commit": {"sha": "a" * 40}}
        if "/git/commits/" in path:
            return {"tree": {"sha": path.rsplit("/", 1)[-1]}}
        baseline = "/" + "a" * 40 + "?" in path
        return {
            "truncated": self.truncated,
            "tree": [
                {
                    "path": p,
                    "mode": self.mode,
                    "type": "blob",
                    "sha": ("1" if baseline else "2") * 40,
                }
                for p in self.paths
                if not (baseline and self.added)
            ],
        }


class FakeKubectl:
    def __init__(self) -> None:
        self.images = dict.fromkeys(server.NAMES, "local:v1")
        self.calls = []
        self.fail = None

    def __call__(self, args) -> str:
        self.calls.append(list(args))
        if args[0] == "get":
            name = args[2]
            return json.dumps(
                {
                    "metadata": {
                        "name": name,
                        "namespace": "coreteams",
                        "resourceVersion": "42",
                    },
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {"name": name, "image": self.images[name]}
                                ]
                            }
                        }
                    },
                }
            )
        if args[0] == "patch":
            patch = json.loads(args[-1])
            assert [p["op"] for p in patch] == ["test", "test", "test", "replace"]
            assert patch[0]["value"] == "42"
            assert patch[2]["value"] == self.images[args[2]]
            self.images[args[2]] = patch[-1]["value"]
        if args[0] == "rollout" and args[2] == self.fail:
            raise TimeoutError("rollout failed")
        return ""


@pytest.fixture
def environment(tmp_path):
    api, command = FakeGitHub(), FakeKubectl()
    evidence = server.SchemaEvidence("actual-introspection-hash", "isolated-cluster/db")
    options = {
        "state_dir": tmp_path,
        "verifier": lambda r: r,
        "api": api,
        "command": command,
        "clearance": server.Clearance(
            "a" * 40, evidence, "preflight-record", "restore-rehearsal", "approval-P"
        ),
        "schema_probe": lambda: evidence,
    }
    return options, api, command


@pytest.mark.parametrize("with_clearance", [False, True])
def test_cli_ui_does_not_load_provider(environment, monkeypatch, with_clearance):
    from tools.cicd import schema_probe, verify_release

    options, api, command = environment
    server.deploy(release(), **options)
    before = (options["state_dir"] / "state.json").read_bytes()
    api.paths = ["web-product/src/App.tsx"]
    candidate = release("d" * 40)
    config = options["state_dir"] / "clearance.json"
    if with_clearance:
        data = asdict(options["clearance"])
        data.update(data.pop("evidence"))
        config.write_text(json.dumps(data))
    monkeypatch.setattr(server, "STATE", options["state_dir"])
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "trusted_path", lambda *a, **kw: None)
    monkeypatch.setattr(
        server,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(
                buffer=io.BytesIO(json.dumps(candidate.to_dict()).encode())
            )
        ),
    )
    monkeypatch.setattr(server, "kubectl", command)
    monkeypatch.setattr(verify_release, "GitHubAPI", lambda: api)
    monkeypatch.setattr(verify_release, "verify_release", lambda *a, **kw: candidate)

    def unavailable(*args, **kwargs):
        pytest.fail("UI path loaded unavailable DB dependency")

    def read_clearance(path, limit):
        if path == config:
            return config.read_bytes()
        return unavailable()

    monkeypatch.setattr(schema_probe, "protected_read", read_clearance)
    monkeypatch.setitem(__import__("sys").modules, "asyncpg", None)
    assert server.main() == 0
    assert (
        server.read_state(options["state_dir"])["deployed_sha"] == candidate.context.sha
    )
    assert (options["state_dir"] / "state.json").read_bytes() != before


@pytest.mark.parametrize("failure", [False, True])
def test_cli_installed_provider_under_lock(environment, monkeypatch, capsys, failure):
    from tools.cicd import schema_probe, verify_release

    options, api, command = environment
    config = options["state_dir"] / "clearance.json"
    clearance = options["clearance"]
    data = asdict(clearance)
    data.update(data.pop("evidence"))
    config.write_text(json.dumps(data))
    monkeypatch.setattr(server, "STATE", options["state_dir"])
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "trusted_path", lambda *a, **kw: None)
    monkeypatch.setattr(
        server,
        "sys",
        SimpleNamespace(
            stdin=SimpleNamespace(
                buffer=io.BytesIO(json.dumps(release().to_dict()).encode())
            ),
            stderr=__import__("sys").stderr,
        ),
    )
    monkeypatch.setattr(server, "kubectl", command)
    monkeypatch.setattr(verify_release, "GitHubAPI", lambda: api)
    monkeypatch.setattr(verify_release, "verify_release", lambda *a, **kw: release())
    called = []

    def factory(sha, provided):
        assert sha == release().context.sha and provided == clearance

        def run():
            with (
                (options["state_dir"] / "lock").open("r+") as stream,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            called.append(True)
            if failure:
                raise schema_probe.ProbeError(
                    "installed catalog probe refused"
                ) from RuntimeError("password secret SQL")
            return clearance.evidence

        return run

    monkeypatch.setattr(
        schema_probe, "protected_read", lambda path, limit: config.read_bytes()
    )
    monkeypatch.setattr(schema_probe, "installed_probe", factory)
    assert server.main() == int(failure)
    assert called == [True]
    if failure:
        assert not command.calls
        assert not (options["state_dir"] / "state.json").exists()
        assert "secret" not in capsys.readouterr().err
    else:
        assert server.read_state(options["state_dir"])["status"] == "succeeded"


@pytest.mark.parametrize(
    "raw",
    [
        b" " * 16385,
        b"",
        b"{}",
        b'{"schema":1,"schema":1}',
        b"$(touch /tmp/no)",
        b"\xff",
    ],
)
def test_bad_request(raw) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        server.parse_request(raw)


def test_schema_two_handoff_wraps_unchanged_release() -> None:
    request = server.parse_request(
        json.dumps({"schema": 2, "release": release().to_dict()}).encode()
    )
    assert isinstance(request, server.MaintenanceRequest)
    assert request.release == release()
    # The old Release parser must reject this wire format, not image-patch it.
    with pytest.raises(ValueError):
        Release.parse(json.dumps({"schema": 2, "release": release().to_dict()}))


@pytest.mark.parametrize("schema", [True, 1, 2.0, "2", None])
def test_handoff_schema_is_exact(schema) -> None:
    with pytest.raises(ValueError):
        server.parse_request(
            json.dumps({"schema": schema, "release": release().to_dict()}).encode()
        )


def test_raw_manifest_only() -> None:
    assert server.parse_request(json.dumps(release().to_dict()).encode()) == release()
    with pytest.raises(ValueError):
        server.parse_request(
            json.dumps({"manifest": release().to_dict(), "approved": True}).encode()
        )


@pytest.mark.parametrize(
    "alter",
    [
        lambda d: d.update(extra=True),
        lambda d: d.update(sha="$(id)"),
        lambda d: d.update(preflight=""),
        lambda d: d.update(signature=""),
    ],
)
def test_clearance_strict(alter) -> None:
    data = {
        "sha": "a" * 40,
        "signature": "actual",
        "target": "database",
        "preflight": "record",
        "rehearsal": "record",
        "approval": "P",
    }
    alter(data)
    with pytest.raises(ValueError):
        server.Clearance.parse(json.dumps(data))


def test_initial_without_provider_blocked(environment) -> None:
    options, _, command = environment
    options["schema_probe"] = None
    with pytest.raises(ValueError, match="clearance"):
        server.deploy(release(), **options)
    assert not command.calls
    assert not (options["state_dir"] / "state.json").exists()


def test_unknown_schema_blocked(environment) -> None:
    options, _, command = environment
    options["schema_probe"] = lambda: server.SchemaEvidence("different", "database")
    with pytest.raises(ValueError, match="schema mismatch"):
        server.deploy(release(), **options)
    assert not command.calls


def test_success_and_verified_noop(environment) -> None:
    options, _, command = environment
    assert server.deploy(release(), **options) == "succeeded"
    state = server.read_state(options["state_dir"])
    assert state["previous"] == dict.fromkeys(server.NAMES, "local:v1")
    assert state["deployed_sha"] == "a" * 40
    before = len([c for c in command.calls if c[0] == "patch"])
    options["clearance"] = options["schema_probe"] = None
    assert server.deploy(release(), **options) == "already deployed"
    assert len([c for c in command.calls if c[0] == "patch"]) == before
    command.images["backend"] = "drift"
    with pytest.raises(ValueError, match="drift"):
        server.deploy(release(), **options)


@pytest.mark.parametrize("stage", [1, 2])
def test_reverify_mismatch_no_patch(environment, stage) -> None:
    options, _, command = environment
    count = 0

    def verify(request):
        nonlocal count
        count += 1
        return release("d" * 40) if count == stage else request

    options["verifier"] = verify
    with pytest.raises(ValueError, match="mismatch"):
        server.deploy(release(), **options)
    assert not any(c[0] == "patch" for c in command.calls)


def test_partial_rollout_failed_and_reconciliation(environment) -> None:
    options, _, command = environment
    command.fail = "deployment/frontend"
    with pytest.raises(TimeoutError):
        server.deploy(release(), **options)
    state = server.read_state(options["state_dir"])
    assert state["status"] == "failed"
    assert state["deployed_sha"] is None
    assert state["previous"]["backend"] == "local:v1"
    calls = len(command.calls)
    with pytest.raises(ValueError, match="reconciliation"):
        server.deploy(release(), **options)
    assert len(command.calls) == calls
    assert not any("undo" in c for c in command.calls)


@pytest.mark.parametrize(
    "path",
    [
        "backend/migrations/x.py",
        "backend/alembic.ini",
        "backend/db.py",
        "backend/main.py",
        "backend/migration_bridge.py",
        "backend/models.py",
        "backend/schemas.py",
        "backend/repository.py",
        "backend/runner_transport.py",
        "backend/containers.py",
        "pyproject.toml",
        "unknown/bootstrap.py",
    ],
)
def test_code_risk_needs_exact_clearance(environment, path) -> None:
    options, api, command = environment
    server.deploy(release(), **options)
    api.paths = [path]
    before = len(command.calls)
    with pytest.raises(ValueError, match="clearance"):
        server.deploy(release("d" * 40), **options)
    assert len(command.calls) == before


@pytest.mark.parametrize("mode", ["100755", "120000"])
def test_executable_or_symlink_docs_risk(environment, mode) -> None:
    options, api, _ = environment
    server.deploy(release(), **options)
    api.mode = mode
    with pytest.raises(ValueError, match="clearance"):
        server.deploy(release("d" * 40), **options)


def test_docs_delta_known_baseline(environment) -> None:
    options, _, _ = environment
    server.deploy(release(), **options)
    options["clearance"] = options["schema_probe"] = None
    assert server.deploy(release("d" * 40), **options) == "succeeded"


@pytest.mark.parametrize("status", ["behind", "diverged", "identical"])
def test_stale_nonancestor(environment, status) -> None:
    options, api, _ = environment
    server.deploy(release(), **options)
    api.status = status
    with pytest.raises(ValueError, match="older or non-ancestor"):
        server.deploy(release("d" * 40), **options)


def test_truncated_tree_fails(environment) -> None:
    options, api, _ = environment
    server.deploy(release(), **options)
    api.truncated = True
    with pytest.raises(ValueError, match="truncated"):
        server.deploy(release("d" * 40), **options)


@pytest.mark.parametrize("gate", [server.migration_risk, verify_freshness])
@pytest.mark.parametrize(
    "entries",
    [
        [("docs", "040000", "tree"), ("docs", "040000", "tree")],
        [("docs", "100644", "tree")],
        [("docs/guide.md", "040000", "blob")],
        [("docs/guide.md", "100644", "commit")],
        [("docs/guide.md", "100644", "tag")],
        [("README.md", "100644", "blob")],
        [("../README.md", "100644", "blob")],
        [("/README.md", "100644", "blob")],
        [("docs/./README.md", "100644", "blob")],
        [("docs//README.md", "100644", "blob")],
        [("docs/README\0.md", "100644", "blob")],
    ],
    ids=[
        "duplicate-directory",
        "malformed-directory-mode",
        "malformed-blob-mode",
        "malformed-submodule-mode",
        "unsupported-entry-type",
        "duplicate-blob",
        "parent-traversal",
        "absolute-path",
        "dot-component",
        "empty-component",
        "nul-path",
    ],
)
def test_malformed_git_tree_fails_closed_at_both_gates(gate, entries) -> None:
    class MalformedAPI(FakeGitHub):
        def get(self, path: str) -> dict:
            result = super().get(path)
            if "/git/trees/" in path:
                result["tree"].extend(
                    {"path": p, "mode": mode, "type": kind, "sha": "3" * 40}
                    for p, mode, kind in entries
                )
            return result

    with pytest.raises(ValueError):
        gate(MalformedAPI(), "a" * 40, "d" * 40)


def hold_lock(directory, ready, finish):
    with server.deployment_lock(Path(directory)):
        ready.set()
        assert finish.wait(10)


def crash_after_state(directory):
    with server.deployment_lock(Path(directory)):
        data = server.read_state(Path(directory))
        data["status"] = "in_progress"
        server.atomic_state(Path(directory), data)
        os._exit(9)


def test_real_flock_serializes_processes(tmp_path) -> None:
    context = multiprocessing.get_context("spawn")
    ready, finish = context.Event(), context.Event()
    child = context.Process(target=hold_lock, args=(str(tmp_path), ready, finish))
    child.start()
    try:
        assert ready.wait(10)
        with (
            pytest.raises(ValueError, match="already running"),
            server.deployment_lock(tmp_path),
        ):
            pytest.fail("acquired competing lock")
    finally:
        finish.set()
        child.join(10)
        if child.is_alive():
            child.kill()
            child.join()
    assert child.exitcode == 0
    with server.deployment_lock(tmp_path):
        pass


def test_crash_inprogress_survives_and_blocks(environment) -> None:
    options, _, _ = environment
    server.deploy(release(), **options)
    child = multiprocessing.get_context("spawn").Process(
        target=crash_after_state, args=(str(options["state_dir"]),)
    )
    child.start()
    child.join(10)
    assert child.exitcode == 9
    with pytest.raises(ValueError, match="reconciliation"):
        server.deploy(release(), **options)


def test_atomic_replace_failure_preserves_old(environment, monkeypatch) -> None:
    options, _, _ = environment
    server.deploy(release(), **options)
    old = (options["state_dir"] / "state.json").read_bytes()

    def failure(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(server.os, "replace", failure)
    with pytest.raises(OSError):
        server.atomic_state(options["state_dir"], {"invalid": True})
    assert (options["state_dir"] / "state.json").read_bytes() == old
    assert {p.name for p in options["state_dir"].iterdir()} == {"state.json", "lock"}


@pytest.mark.parametrize(
    "path",
    ["frontend/src/App.tsx", "web-product/src/domain.ts", "frontend/src/main.css"],
)
def test_known_ui_release_without_clearance(environment, path) -> None:
    options, api, _ = environment
    server.deploy(release(), **options)
    api.paths = [path]
    options["clearance"] = options["schema_probe"] = None
    assert server.deploy(release("d" * 40), **options) == "succeeded"


@pytest.mark.parametrize(
    "path",
    [
        "frontend/package.json",
        "web-product/pnpm-lock.yaml",
        "frontend/Dockerfile",
        "frontend/nginx.conf",
        "web-product/eslint.config.js",
        "backend/new_bootstrap.py",
        "frontend/src/new.ts",
    ],
)
def test_unknown_and_ui_runtime_paths_risky(environment, path) -> None:
    options, api, _ = environment
    server.deploy(release(), **options)
    api.paths = [path]
    api.added = path == "frontend/src/new.ts"
    options["clearance"] = options["schema_probe"] = None
    with pytest.raises(ValueError, match="clearance"):
        server.deploy(release("d" * 40), **options)


# New-route scenarios: absent provider/config, legacy activation/downgrade,
# one shared lock, interruption/retry gates, GitHub races, durable phase failures,
# readiness and private/public smoke determine the actual CLI outcome.
class MaintenanceHost:
    """Local deterministic evidence only; never an installed adapter."""

    def __init__(self, directory, plan):
        from tools.cicd import maintenance as m

        self.directory, self.plan = directory, plan
        self.calls = []
        self.fail = None
        self.old = m.Snapshot(
            plan.target, plan.old_schema, m.Images.from_release(release()), (1, 1)
        )
        self.backup = m.Backup("sha256:" + "f" * 64, self.old, ("sha256:" + "e" * 64,))

    def __getattr__(self, name):
        from tools.cicd import maintenance as m

        def step(*args, timeout_seconds):
            assert 0 < timeout_seconds <= 1800
            with (
                (self.directory / "lock").open("r+") as stream,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = server.read_maintenance_state(self.directory)
            assert state.status == m.Status.RUNNING
            self.calls.append(name)
            if name == self.fail:
                raise RuntimeError("private DB secret")
            if name == "preflight":
                return self.old
            if name in {
                "validate_transition",
                "quiesce",
                "private_readiness",
                "functional_smoke",
                "public_smoke",
            }:
                return True
            if name == "reverify_current_release":
                pytest.fail("provider must not own the GitHub verifier")
            if name == "cold_backup":
                return self.backup
            if name in {"validate_backup", "backup_readback", "restore_check"}:
                return m.BackupProof(self.backup.manifest, "sha256:" + "e" * 64, True)
            if name == "schema_check":
                return m.Schema(
                    self.plan.target,
                    self.plan.new_schema
                    if "migrate_exact" in self.calls
                    else self.plan.old_schema,
                )
            if name == "readiness":
                return m.Readiness(
                    m.Images.from_release(self.plan.release), (1, 1), (1, 1)
                )
            return None

        return step


@pytest.fixture
def maintenance(tmp_path, monkeypatch):
    from tools.cicd import maintenance as m

    monkeypatch.setattr(server, "MAINTENANCE_CONFIG", tmp_path / "activation.json")
    plan = m.MaintenancePlan(
        release(),
        "pg_target_v1:sha256:" + "a" * 64,
        "pg_catalog_v1:sha256:" + "b" * 64,
        "pg_catalog_v1:sha256:" + "c" * 64,
        "reviewed-transition",
        "sha256:" + "d" * 64,
    )
    host = MaintenanceHost(tmp_path, plan)
    preparations = []

    class Provider:
        provider_id = "reviewed-host"

        def prepare(self, candidate, *, timeout_seconds):
            assert candidate == plan.release and timeout_seconds == 60
            preparations.append(candidate)
            return server.PreparedMaintenance(plan, host)

    return (
        {
            "state_dir": tmp_path,
            "verifier": lambda candidate: candidate,
            "configuration": server.MaintenanceConfiguration("reviewed-host"),
            "provider": Provider(),
        },
        host,
        preparations,
    )


@pytest.mark.parametrize("absent", ["provider", "configuration"])
def test_maintenance_missing_installation_has_no_effects(maintenance, absent):
    options, host, preparations = maintenance
    options[absent] = None
    with pytest.raises(ValueError, match="provider required"):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    assert not preparations and not host.calls
    assert not list(options["state_dir"].iterdir())


def test_maintenance_success_uses_single_shared_lock_and_durable_state(maintenance):
    from tools.cicd import maintenance as m

    options, host, _ = maintenance
    assert (
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
        == "succeeded"
    )
    state = server.read_maintenance_state(options["state_dir"])
    assert state.status == m.Status.SUCCEEDED and state.phase == m.Phase.COMPLETE
    assert state.plan.release == release()
    assert "readiness" in host.calls and "functional_smoke" in host.calls
    assert "public_smoke" in host.calls
    calls = list(host.calls)
    with pytest.raises(ValueError, match="reconciliation"):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    assert host.calls == calls


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
def test_maintenance_github_race_never_advances_to_next_effect(maintenance, stage):
    options, host, _ = maintenance
    count = 0

    def verifier(candidate):
        nonlocal count
        count += 1
        return release("d" * 40) if count == stage else candidate

    options["verifier"] = verifier
    with pytest.raises(ValueError):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    assert "migrate_exact" not in host.calls
    if stage <= 3:
        assert "quiesce" not in host.calls
    if stage <= 2:
        assert not host.calls
    else:
        assert (
            server.read_maintenance_state(options["state_dir"]).status.value == "failed"
        )


@pytest.mark.parametrize(
    "failure", ["readiness", "functional_smoke", "restore_traffic", "public_smoke"]
)
def test_maintenance_stage_failure_is_not_success_and_blocks_retry(
    maintenance, failure
):
    options, host, preparations = maintenance
    host.fail = failure
    with pytest.raises(ValueError, match="did not complete"):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    state = server.read_maintenance_state(options["state_dir"])
    assert state.status.value == "failed" and state.failure_phase.value == failure
    assert (
        "private DB secret"
        not in (options["state_dir"] / server.MAINTENANCE_JOURNAL).read_text()
    )
    calls, prepared = list(host.calls), list(preparations)
    with pytest.raises(ValueError, match="reconciliation"):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    assert host.calls == calls and preparations == prepared


@pytest.mark.parametrize("journal", ["running", "malformed", "symlink"])
def test_maintenance_interrupted_or_bad_journal_blocks_before_provider(
    maintenance, journal
):
    from tools.cicd import maintenance as m

    options, host, preparations = maintenance
    path = options["state_dir"] / server.MAINTENANCE_JOURNAL
    if journal == "running":
        plan = options["provider"].prepare(release(), timeout_seconds=60).plan
        server.atomic_state(
            options["state_dir"],
            server.maintenance_state_data(
                m.MaintenanceState(
                    plan,
                    m.Phase.MIGRATE_EXACT,
                    disruption_possible=True,
                    ddl_possible=True,
                )
            ),
            filename=server.MAINTENANCE_JOURNAL,
        )
        preparations.clear()
    elif journal == "symlink":
        path.symlink_to(options["state_dir"] / "missing")
    else:
        path.write_text("not JSON")
    with pytest.raises(ValueError):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    assert not host.calls and not preparations


def test_maintenance_persist_failure_stops_before_host(maintenance, monkeypatch):
    from tools.cicd import maintenance as m

    options, host, _ = maintenance
    monkeypatch.setattr(
        server.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(m.StatePersistenceError):
        server.deploy_maintenance(server.MaintenanceRequest(release()), **options)
    assert not host.calls


@pytest.fixture
def maintenance_cli(maintenance, monkeypatch):
    from tools.cicd import schema_probe, verify_release

    options, host, _ = maintenance
    server.MAINTENANCE_CONFIG.write_text('{"schema":1,"provider":"reviewed-host"}')
    monkeypatch.setattr(
        schema_probe, "protected_read", lambda path, limit: path.read_bytes()
    )
    monkeypatch.setattr(server, "trusted_path", lambda *a, **kw: None)
    monkeypatch.setattr(server, "STATE", options["state_dir"])
    monkeypatch.setattr(
        server.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                json.dumps({"schema": 2, "release": release().to_dict()}).encode()
            )
        ),
    )
    monkeypatch.setattr(
        server, "deploy", lambda *a, **kw: pytest.fail("legacy path reached")
    )
    monkeypatch.setattr(
        server, "kubectl", lambda *a, **kw: pytest.fail("legacy authority reached")
    )
    checks = []

    def verify(run, sha, *, api, require_current_master):
        assert run == "10" and sha == release().context.sha
        assert require_current_master is True
        checks.append(sha)
        return release()

    monkeypatch.setattr(verify_release, "verify_release", verify)
    return options, host, checks


@pytest.mark.parametrize("installed", [False, True])
def test_cli_schema_two_never_falls_back_without_config_or_provider(
    maintenance_cli, installed, capsys
):
    options, host, checks = maintenance_cli
    if installed:
        provider = None
    else:
        server.MAINTENANCE_CONFIG.unlink()
        provider = options["provider"]
    assert server.main(api=FakeGitHub(), maintenance_provider=provider) == 1
    assert not host.calls and not checks
    assert not (options["state_dir"] / server.MAINTENANCE_JOURNAL).exists()
    assert "deployment refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", [None, "readiness", "functional_smoke", "public_smoke"]
)
def test_cli_strict_exact_verification_and_actual_stage_exit(
    maintenance_cli, failure, capsys
):
    options, host, checks = maintenance_cli
    host.fail = failure
    assert server.main(
        api=FakeGitHub(), maintenance_provider=options["provider"]
    ) == int(failure is not None)
    assert len(checks) == 4
    assert "private DB secret" not in capsys.readouterr().err


def test_cli_active_installation_rejects_legacy_request(maintenance_cli, monkeypatch):
    options, host, checks = maintenance_cli
    monkeypatch.setattr(
        server.sys,
        "stdin",
        SimpleNamespace(buffer=io.BytesIO(json.dumps(release().to_dict()).encode())),
    )
    assert server.main(api=FakeGitHub(), maintenance_provider=options["provider"]) == 1
    assert not host.calls and not checks


@pytest.mark.parametrize("activation", ["config", "journal"])
def test_legacy_cannot_bypass_maintenance_activation(
    environment, monkeypatch, activation
):
    options, _, command = environment
    path = options["state_dir"] / (
        "activation.json" if activation == "config" else server.MAINTENANCE_JOURNAL
    )
    monkeypatch.setattr(
        server, "MAINTENANCE_CONFIG", options["state_dir"] / "activation.json"
    )
    path.write_text("even invalid activation must fail closed")
    with pytest.raises(ValueError, match="legacy deployment disabled"):
        server.deploy(release(), **options)
    assert not command.calls
