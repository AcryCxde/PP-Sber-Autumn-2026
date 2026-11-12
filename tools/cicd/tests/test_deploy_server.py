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
