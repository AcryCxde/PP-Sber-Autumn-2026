"""Failure boundaries and crash reconciliation of the installed controller."""

import copy
import json
from pathlib import Path

import pytest

from tools.cicd import controller as module
from tools.cicd.release import Release
from tools.cicd.tests.test_verify_release import manifest


class API:
    def get(self, path):
        old, new = path.rsplit("/", 1)[1].split("...")
        return {
            "status": "ahead" if old < new else "behind",
            "merge_base_commit": {"sha": min(old, new)},
        }


@pytest.fixture
def system(tmp_path, monkeypatch):
    source = manifest()
    source["sha"] = "0" * 39 + "1"
    target = Release.parse(json.dumps(manifest()))
    record = {"events": [], "fail": None}

    class Backup:
        def __init__(self, config):
            pass

        def preflight(self, path):
            record["events"].append("preflight")

        def create(self, path):
            record["events"].append("backup")
            return path

        def decrypt(self, path):
            return path

        def restore(self, path):
            record["events"].append("restore")

    class Host:
        def __init__(self, config, directory):
            pass

        def assert_release(self, current):
            pass

        def event(self, name):
            record["events"].append(name)
            if record["fail"] == name:
                raise RuntimeError("injected " + name)

        def source(self):
            return {
                n: {"spec": {"template": {"spec": {"containers": [{}]}}}}
                for n in ("db", "s3", "backend", "frontend")
            }

        def image(self, name):
            return name + "@sha256:" + "c" * 64

        def smoke(self, public=False):
            self.event("public" if public else "private")

        def gate(self, opened):
            self.event("open" if opened else "close")

        def stop(self):
            self.event("stop")

        def start_storage(self):
            self.event("storage")

        def rehearsal(self, state, readback):
            self.event("rehearsal")
            return "schema"

        def job(self, mode, state):
            self.event(mode)

        def schema(self):
            return "schema"

        def candidate_start(self, release):
            self.event("candidate")

        def cleanup(self):
            self.event("cleanup")

        def source_start(self, source):
            self.event("source")

    monkeypatch.setattr(module, "ColdBackup", Backup)
    # Portable unit tests; real resource checks run against Linux during acceptance.
    original_read = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *a, **kw: (
            "MemAvailable: 2097152 kB"
            if str(path) == "/proc/meminfo"
            else original_read(path, *a, **kw)
        ),
    )
    config = {"initial_release": source}

    def controller():
        return module.Controller(config, tmp_path, Host)

    return controller, target, record, tmp_path


def test_success_and_same_sha_noop(system):
    factory, release, record, _ = system
    factory().deploy(release, API())
    assert factory().state["current"] == release.to_dict()
    record["events"].clear()
    factory().deploy(release, API())
    assert record["events"] == ["public"]


@pytest.mark.parametrize(
    "failure,restores",
    [("rehearsal", True), ("upgrade", True), ("candidate", True), ("backup", False)],
)
def test_failure_before_open_restores_source(system, failure, restores):
    factory, release, record, _ = system
    if failure == "backup":
        # Inject before backup-ready: there must be no restore of an incomplete archive.
        original = module.ColdBackup.create

        def fail(self, path):
            raise RuntimeError("interrupted backup")

        module.ColdBackup.create = fail
    else:
        record["fail"] = failure
    with pytest.raises(RuntimeError):
        factory().deploy(release, API())
    assert factory().state["phase"] == "failed"
    assert ("restore" in record["events"]) == restores
    assert "source" in record["events"]
    if failure == "backup":
        module.ColdBackup.create = original


def test_failure_after_open_never_restores_old_user_data(system):
    factory, release, record, _ = system
    record["fail"] = "open"
    with pytest.raises(RuntimeError):
        factory().deploy(release, API())
    assert factory().state["phase"] == "opening"
    assert "restore" not in record["events"]
    record["fail"] = None
    record["events"].clear()
    factory().deploy(release, API())
    assert factory().state["phase"] == "succeeded"
    assert "upgrade" not in record["events"] and "restore" not in record["events"]


def test_interrupted_live_job_is_reconciled_without_repeating_backup(system):
    factory, release, record, root = system
    factory().deploy(release, API())
    state = factory().state
    state["phase"] = "migrating"
    (root / "state.json").write_text(json.dumps(state))
    record["events"].clear()
    factory().deploy(release, API())
    assert "upgrade" in record["events"]  # Host waits for the same named Job.
    assert "backup" not in record["events"] and "rehearsal" not in record["events"]
    assert factory().state["phase"] == "succeeded"


def test_late_release_cannot_downgrade_completed_descendant(system):
    factory, release, record, _ = system
    factory().deploy(release, API())
    stale = copy.deepcopy(release.to_dict())
    stale["sha"] = "1" * 40
    record["events"].clear()
    factory().deploy(Release.parse(json.dumps(stale)), API())
    assert record["events"] == []
    assert factory().state["current"] == release.to_dict()


def test_submission_waits_for_activating_service_before_replacing_pending_request(
    tmp_path, monkeypatch
):
    import io
    from types import SimpleNamespace
    from tools.cicd.cold_backup import atomic_json

    release = Release.parse(json.dumps(manifest()))
    token = tmp_path / "token"
    token.write_text("test-only")
    state = tmp_path / "state"
    state.mkdir()
    atomic_json(state / "state.json", {"phase": "succeeded"})
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.setattr(module, "GitHubAPI", lambda token: object())
    monkeypatch.setattr(module, "verify_release", lambda *args, **kw: release)
    monkeypatch.setattr(
        module.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                json.dumps({"schema": 2, "release": release.to_dict()}).encode()
            )
        ),
    )
    calls = []

    def command(args):
        calls.append(args[1])
        if args[1] == "show":
            assert not (state / "request.json").exists()
            return b"activating\n" if calls.count("show") == 1 else b"inactive\n"
        return b""

    monkeypatch.setattr(module, "run", command)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    class Waiter:
        returncode = 0

        def __init__(self, args, **kwargs):
            assert calls == ["show", "show", "reset-failed"]
            assert json.loads((state / "request.json").read_text()) == release.to_dict()
            assert args == ["systemctl", "start", "coreteams-deploy.service"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def poll(self):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", Waiter)
    module.submit({"github_token": str(token)})
