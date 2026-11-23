"""Offline provenance, archive and stale-release contracts."""

import hashlib
import io
import json
import urllib.error
import zipfile
from copy import deepcopy
from email.message import Message

import pytest

from tools.cicd import verify_release as verifier
from tools.cicd.release import CHECKS, IMAGE_REPOSITORIES, REPOSITORY
from tools.cicd.verify_release import (
    GitHubAPI,
    parse_archive,
    trusted_dispatch,
    verify_release,
)

SHA = "a" * 40
MASTER = "b" * 40
PREFIX = f"/repos/{REPOSITORY}"


def manifest():
    return {
        "schema": 1,
        "repository": REPOSITORY,
        "sha": SHA,
        "workflow_run_id": 123,
        "run_attempt": 2,
        "platform": "linux/amd64",
        "checks": dict.fromkeys(CHECKS, "success"),
        "images": {
            name: {
                "repository": repo,
                "digest": "sha256:" + "c" * 64,
                "image_id": "sha256:" + "d" * 64,
            }
            for name, repo in IMAGE_REPOSITORIES.items()
        },
    }


def archive(data, name="release.json", mode=0o100644):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        info = zipfile.ZipInfo(name)
        info.external_attr = mode << 16
        output.writestr(info, json.dumps(data))
    return stream.getvalue()


class FakeAPI:
    def __init__(self):
        self.payload = archive(manifest())
        self.run = {
            "id": 123,
            "run_attempt": 2,
            "workflow_id": 42,
            "path": ".github/workflows/ci.yml",
            "event": "push",
            "head_branch": "master",
            "head_sha": SHA,
            "status": "completed",
            "conclusion": "success",
            "pull_requests": [],
            "repository": {"full_name": REPOSITORY},
            "head_repository": {"full_name": REPOSITORY},
        }
        self.artifact = {
            "id": 456,
            "name": f"release-{SHA}-123-2",
            "expired": False,
            "expires_at": "2099-01-01T00:00:00Z",
            "size_in_bytes": len(self.payload),
            "digest": "sha256:" + hashlib.sha256(self.payload).hexdigest(),
            "workflow_run": {"id": 123, "head_sha": SHA, "head_branch": "master"},
        }
        self.master = SHA
        self.trees = {SHA: [], MASTER: []}
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        route = path.removeprefix(PREFIX)
        if route == "/actions/workflows/ci.yml":
            return {"id": 42, "path": ".github/workflows/ci.yml", "state": "active"}
        if route == "/actions/runs/123":
            return deepcopy(self.run)
        if route == "/actions/runs/123/artifacts?per_page=100":
            return {"total_count": 1, "artifacts": [deepcopy(self.artifact)]}
        if route == "/git/ref/heads/master":
            return {"object": {"type": "commit", "sha": self.master}}
        if route == f"/compare/{SHA}...{MASTER}":
            return {"status": "ahead", "merge_base_commit": {"sha": SHA}}
        if route.startswith("/git/commits/"):
            return {"tree": {"sha": route.rsplit("/", 1)[-1]}}
        if route.startswith("/git/trees/"):
            sha = route.split("/")[-1].split("?")[0]
            return {"truncated": False, "tree": deepcopy(self.trees[sha])}
        raise AssertionError(path)

    def download(self, artifact_id):
        assert artifact_id == 456
        return self.payload


DISCOVERY_PATH = (
    PREFIX + "/actions/workflows/ci.yml/runs"
    f"?branch=master&event=push&head_sha={SHA}&per_page=100"
)


class DiscoveryAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.listing = {"total_count": 1, "workflow_runs": [deepcopy(self.run)]}
        self.downloads = []

    def get(self, path):
        if path == DISCOVERY_PATH:
            self.calls.append(path)
            return deepcopy(self.listing)
        return super().get(path)

    def download(self, artifact_id):
        self.downloads.append(artifact_id)
        return super().download(artifact_id)


def test_dispatch_discovers_exact_master_push_release():
    api = DiscoveryAPI()
    assert verifier.discover_release(SHA, api=api).to_dict() == manifest()
    assert DISCOVERY_PATH in api.calls
    assert not any("/compare/" in path or "/attempts/" in path for path in api.calls)


@pytest.mark.parametrize(
    "sha", ["", "a" * 39, "0" * 40, "a" * 40 + "&event=pull_request"]
)
def test_discovery_invalid_dispatch_sha_has_no_api_effect(sha):
    api = DiscoveryAPI()
    with pytest.raises(ValueError):
        verifier.discover_release(sha, api=api)
    assert api.calls == []


def test_discovery_stale_dispatch_does_not_select_current_or_previous_release():
    api = DiscoveryAPI()
    api.master = MASTER
    # Even a harmless docs-only master advance does not permit an old dispatch.
    api.trees[MASTER] = [
        {"path": "README.md", "mode": "100644", "type": "blob", "sha": "c" * 40}
    ]
    with pytest.raises(ValueError, match="not current master"):
        verifier.discover_release(SHA, api=api)
    assert DISCOVERY_PATH not in api.calls
    assert api.downloads == []


@pytest.mark.parametrize(
    "listing",
    [
        {},
        {"total_count": 0, "workflow_runs": []},
        {"total_count": 1, "workflow_runs": []},
        {"total_count": 2, "workflow_runs": []},
        {"total_count": True, "workflow_runs": [{}]},
        {"total_count": "1", "workflow_runs": [{}]},
        {"total_count": 1, "workflow_runs": {}},
        {"total_count": 1, "workflow_runs": None},
        {"total_count": 1, "workflow_runs": [None]},
        {"total_count": 1, "workflow_runs": [{}]},
        {"total_count": 101, "workflow_runs": [{}]},
    ],
)
def test_discovery_missing_or_malformed_listing_fails_closed(listing):
    api = DiscoveryAPI()
    api.listing = listing
    with pytest.raises(ValueError):
        verifier.discover_release(SHA, api=api)
    assert api.downloads == []


@pytest.mark.parametrize("status", ["success", "failure", "in_progress"])
def test_discovery_ambiguous_run_is_not_resolved_by_status_or_order(status):
    api = DiscoveryAPI()
    other = deepcopy(api.run)
    other.update(
        id=124,
        status="in_progress" if status == "in_progress" else "completed",
        conclusion=None if status == "in_progress" else status,
    )
    api.listing = {"total_count": 2, "workflow_runs": [other, api.run]}
    with pytest.raises(ValueError, match="ambiguous"):
        verifier.discover_release(SHA, api=api)
    assert api.downloads == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "in_progress"),
        ("status", "queued"),
        ("conclusion", "failure"),
        ("conclusion", None),
        ("event", "workflow_dispatch"),
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("head_sha", MASTER),
        ("id", True),
        ("run_attempt", 0),
        ("run_attempt", True),
        ("workflow_id", 43),
        ("path", ".github/workflows/other.yml"),
        ("repository", {"full_name": "foreign/repo"}),
        ("head_repository", {"full_name": "foreign/repo"}),
        ("pull_requests", [{}]),
    ],
)
def test_discovery_revalidates_listing_provenance(field, value):
    api = DiscoveryAPI()
    api.listing["workflow_runs"][0][field] = value
    with pytest.raises(ValueError):
        verifier.discover_release(SHA, api=api)
    assert api.downloads == []


@pytest.mark.parametrize("published", ["missing", "previous-attempt", "expired"])
def test_discovery_requires_publish_for_selected_attempt(published):
    class UnpublishedAPI(DiscoveryAPI):
        def get(self, path):
            if published == "missing" and "/artifacts?" in path:
                self.calls.append(path)
                return {"total_count": 0, "artifacts": []}
            return super().get(path)

    api = UnpublishedAPI()
    if published == "previous-attempt":
        api.artifact["name"] = f"release-{SHA}-123-1"
    if published == "expired":
        api.artifact["expired"] = True
    with pytest.raises(ValueError):
        verifier.discover_release(SHA, api=api)
    assert api.downloads == []
    assert not any("/compare/" in path or "/attempts/" in path for path in api.calls)


@pytest.mark.parametrize("race", ["master-before-verify", "master-at-final", "attempt"])
def test_discovery_races_do_not_substitute_new_master_or_attempt(race):
    class RacingAPI(DiscoveryAPI):
        def get(self, path):
            result = super().get(path)
            if path == DISCOVERY_PATH:
                if race == "master-before-verify":
                    self.master = MASTER
                if race == "attempt":
                    self.listing["workflow_runs"][0]["run_attempt"] = 1
                    result["workflow_runs"][0]["run_attempt"] = 1
            if race == "master-at-final" and "/artifacts?" in path:
                self.master = MASTER
            return result

    with pytest.raises(ValueError):
        verifier.discover_release(SHA, api=RacingAPI())


def test_server_strict_master_option_rejects_even_docs_only_advance():
    api = FakeAPI()
    api.master = MASTER
    api.trees[MASTER] = [
        {"path": "README.md", "mode": "100644", "type": "blob", "sha": "c" * 40}
    ]
    assert verify_release("123", SHA, api=api).context.sha == SHA
    with pytest.raises(ValueError, match="not current master"):
        verify_release("123", SHA, api=api, require_current_master=True)


def dispatch_env(monkeypatch):
    for key, value in {
        "CICD_DEPLOY_ENABLED": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/deploy.yml@refs/heads/master",
        "GITHUB_SHA": SHA,
        # Legacy/manual inputs cannot override dispatch selection.
        "RELEASE_RUN_ID": "999",
        "RELEASE_SHA": MASTER,
    }.items():
        monkeypatch.setenv(key, value)


def test_dispatch_main_emits_only_existing_verified_wire(monkeypatch, capsys):
    dispatch_env(monkeypatch)
    monkeypatch.setattr(verifier, "GitHubAPI", DiscoveryAPI)
    assert verifier.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == manifest()
    assert len(captured.out.splitlines()) == 1
    assert captured.err == ""


def test_dispatch_main_sanitizes_external_failure(monkeypatch, capsys):
    class UnavailableAPI(DiscoveryAPI):
        def get(self, path):
            raise ValueError("secret-token signed-url untrusted-body")

    dispatch_env(monkeypatch)
    monkeypatch.setattr(verifier, "GitHubAPI", UnavailableAPI)
    assert verifier.main() == 1
    assert capsys.readouterr() == ("", "release verification refused: ValueError\n")


def test_verified_release_uses_exact_run_and_artifact():
    api = FakeAPI()
    assert verify_release("123", SHA, api=api).to_dict() == manifest()
    assert all(path.startswith(PREFIX + "/") for path in api.calls)


@pytest.mark.parametrize("run_id", ["", "0", "01", "-1", "1;id", True, 123, "9" * 30])
def test_invalid_run_input(run_id):
    with pytest.raises(ValueError):
        verify_release(run_id, SHA, api=FakeAPI())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("head_sha", MASTER),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("run_attempt", True),
        ("workflow_id", 43),
        ("id", True),
        ("path", ".github/workflows/evil.yml"),
        ("repository", {"full_name": "foreign/repo"}),
        ("head_repository", {"full_name": "foreign/repo"}),
        ("pull_requests", [{}]),
    ],
)
def test_untrusted_run_rejected(field, value):
    api = FakeAPI()
    api.run[field] = value
    with pytest.raises(ValueError):
        verify_release("123", SHA, api=api)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expired", True),
        ("expired", 0),
        ("expires_at", "2000-01-01T00:00:00Z"),
        ("size_in_bytes", 9999999),
        ("id", True),
        ("digest", "sha256:" + "0" * 64),
        ("name", f"release-{SHA}-123-1"),
        ("workflow_run", {"id": 999}),
    ],
)
def test_bad_artifact_rejected(field, value):
    api = FakeAPI()
    api.artifact[field] = value
    with pytest.raises(ValueError):
        verify_release("123", SHA, api=api)


@pytest.mark.parametrize(
    ("name", "mode"),
    [
        ("../release.json", 0o100644),
        ("/release.json", 0o100644),
        ("release.json", 0o120777),
        ("release.json", 0o100755),
        ("payload.py", 0o100644),
    ],
)
def test_unsafe_zip_rejected(name, mode):
    api = FakeAPI()
    api.payload = archive(manifest(), name, mode)
    api.artifact["digest"] = None
    with pytest.raises(ValueError):
        verify_release("123", SHA, api=api)


@pytest.mark.parametrize("mutation", ["unknown", "bool", "attempt", "checks", "image"])
def test_manifest_bound_and_strict(mutation):
    data = manifest()
    if mutation == "unknown":
        data["command"] = "id"
    elif mutation == "bool":
        data["workflow_run_id"] = True
    elif mutation == "attempt":
        data["run_attempt"] = 1
    elif mutation == "checks":
        data["checks"]["gate"] = "skipped"
    else:
        data["images"]["backend"]["repository"] = "foreign/image"
    api = FakeAPI()
    api.payload = archive(data)
    api.artifact["digest"] = None
    with pytest.raises(ValueError):
        verify_release("123", SHA, api=api)


@pytest.mark.parametrize(
    ("path", "mode", "allowed"),
    [
        ("docs/guide.md", "100644", True),
        ("README.md", "100644", True),
        ("docs/guide.md", "100755", False),
        ("docs/guide.md", "120000", False),
        ("backend/main.py", "100644", False),
        ("docs/payload.py", "100644", False),
    ],
)
def test_only_strict_docs_delta_allowed(path, mode, allowed):
    api = FakeAPI()
    api.master = MASTER
    api.trees[MASTER] = [{"path": path, "mode": mode, "type": "blob", "sha": "c" * 40}]
    if allowed:
        assert verify_release("123", SHA, api=api).context.sha == SHA
    else:
        with pytest.raises(ValueError):
            verify_release("123", SHA, api=api)


def test_master_race_fails_closed():
    class MovingAPI(FakeAPI):
        def get(self, path):
            result = super().get(path)
            if path.endswith("/git/ref/heads/master"):
                self.master = MASTER
            return result

    with pytest.raises(ValueError):
        verify_release("123", SHA, api=MovingAPI())


def test_api_failure_propagates():
    class UnavailableAPI(FakeAPI):
        def get(self, path):
            raise ValueError("API unavailable")

    with pytest.raises(ValueError, match="unavailable"):
        verify_release("123", SHA, api=UnavailableAPI())


@pytest.mark.parametrize("kind", ["truncated", "diverged", "rerun", "pagination"])
def test_incomplete_or_racing_api_evidence_rejected(kind):
    class BrokenAPI(FakeAPI):
        def get(self, path):
            result = super().get(path)
            if kind == "truncated" and "/git/trees/" in path:
                result["truncated"] = True
            if kind == "diverged" and "/compare/" in path:
                result["status"] = "diverged"
            if kind == "pagination" and "/artifacts?" in path:
                result["total_count"] = 101
            if kind == "rerun" and path.endswith("/actions/runs/123"):
                self.run["run_attempt"] = 3
            return result

    api = BrokenAPI()
    if kind in {"truncated", "diverged"}:
        api.master = MASTER
    with pytest.raises(ValueError):
        verify_release("123", SHA, api=api)


def test_zip_multiple_members_and_bomb_metadata_rejected():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        output.writestr("release.json", json.dumps(manifest()))
        output.writestr("payload.py", "raise RuntimeError('never execute')")
    with pytest.raises(ValueError):
        parse_archive(stream.getvalue())
    with pytest.raises(ValueError):
        parse_archive(archive("x" * 20000))
    with pytest.raises(ValueError):
        parse_archive(b"not a zip")


@pytest.mark.parametrize(
    "change", ["missing", "ref", "event", "repository", "workflow"]
)
def test_dispatch_guard_fails_closed(change):
    env = {
        "CICD_DEPLOY_ENABLED": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/deploy.yml@refs/heads/master",
    }
    trusted_dispatch(env)
    key = {
        "missing": "CICD_DEPLOY_ENABLED",
        "ref": "GITHUB_REF",
        "event": "GITHUB_EVENT_NAME",
        "repository": "GITHUB_REPOSITORY",
        "workflow": "GITHUB_WORKFLOW_REF",
    }[change]
    env.pop(key)
    with pytest.raises(ValueError):
        trusted_dispatch(env)


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.payload[:size]


class FakeOpener:
    def __init__(self, location, *, status=302, archive_status=200):
        self.location = location
        self.status = status
        self.archive_status = archive_status
        self.requests = []

    def open(self, request, timeout):
        assert timeout == 20
        self.requests.append(request)
        headers = Message()
        headers["Location"] = self.location
        if len(self.requests) == 1:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "response", headers, None
            )
        if self.archive_status != 200:
            raise urllib.error.HTTPError(
                request.full_url, self.archive_status, "response", headers, None
            )
        return FakeResponse(b"archive")


def test_authenticated_download_never_forwards_authorization():
    api = GitHubAPI(token="secret-token")
    api.opener = FakeOpener("https://example.blob.core.windows.net/archive?sig=signed")
    assert api.download(456) == b"archive"
    first, second = api.opener.requests
    assert first.get_header("Authorization") == "Bearer secret-token"
    assert second.get_header("Authorization") is None


@pytest.mark.parametrize(
    "location",
    [
        "http://example.blob.core.windows.net/archive",
        "https://evil.example/archive",
        "https://example.blob.core.windows.net.evil.example/archive",
        "https://user:pass@example.blob.core.windows.net/archive",
        "https://example.blob.core.windows.net:444/archive",
        "file:///etc/passwd",
        "https://127.0.0.1/archive",
    ],
)
def test_untrusted_download_redirect_rejected_without_second_request(location):
    api = GitHubAPI(token="secret-token")
    api.opener = FakeOpener(location)
    with pytest.raises(ValueError):
        api.download(456)
    assert len(api.opener.requests) == 1


@pytest.mark.parametrize("status", [301, 401, 403, 404, 410, 429, 500])
def test_api_download_errors_fail_closed(status):
    api = GitHubAPI(token="secret-token")
    api.opener = FakeOpener(
        "https://example.blob.core.windows.net/archive", status=status
    )
    with pytest.raises(ValueError):
        api.download(456)
    assert len(api.opener.requests) == 1


def test_archive_redirect_is_not_followed():
    api = GitHubAPI(token="secret-token")
    api.opener = FakeOpener(
        "https://example.blob.core.windows.net/archive", archive_status=302
    )
    with pytest.raises(ValueError):
        api.download(456)
    assert api.opener.requests[1].get_header("Authorization") is None


def test_api_get_redirect_and_rate_limit_fail_closed():
    for status in (302, 403, 429):
        api = GitHubAPI(token="secret-token")
        api.opener = FakeOpener("https://evil.example", status=status)
        with pytest.raises(ValueError):
            api.get(PREFIX + "/actions/runs/123")
        assert len(api.opener.requests) == 1


def test_automatic_release_pins_upstream_sha_when_master_advances(tmp_path):
    api = FakeAPI()
    api.master = MASTER
    event = {
        "action": "completed",
        "repository": {"full_name": REPOSITORY},
        "workflow_run": api.run,
    }
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    env = {
        "CICD_DEPLOY_ENABLED": "true",
        "GITHUB_EVENT_NAME": "workflow_run",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/deploy.yml@refs/heads/master",
        "GITHUB_SHA": MASTER,
        "GITHUB_EVENT_PATH": str(path),
    }
    release = verifier.workflow_run_release(env, api=api)
    assert release.context.sha == SHA


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "pull_request"),
        ("conclusion", "failure"),
        ("head_repository", {"full_name": "attacker/fork"}),
        ("path", ".github/workflows/other.yml"),
    ],
)
def test_automatic_release_refuses_untrusted_upstream(tmp_path, field, value):
    api = FakeAPI()
    upstream = deepcopy(api.run)
    upstream[field] = value
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {
                "action": "completed",
                "repository": {"full_name": REPOSITORY},
                "workflow_run": upstream,
            }
        )
    )
    env = {
        "CICD_DEPLOY_ENABLED": "true",
        "GITHUB_EVENT_NAME": "workflow_run",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/deploy.yml@refs/heads/master",
        "GITHUB_EVENT_PATH": str(path),
    }
    with pytest.raises(ValueError):
        verifier.workflow_run_release(env, api=api)
