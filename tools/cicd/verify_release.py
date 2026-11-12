"""Fail-closed GitHub provenance verification using current, trusted tooling.

Only JSON is read from the artifact; nothing is extracted or executed. The server
calls this same boundary independently under its own deployment lock.
"""

import hashlib
import io
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from tools.cicd.classify import _docs_diff
from tools.cicd.release import REPOSITORY, SHA, Context, Release, _json

PREFIX = f"/repos/{REPOSITORY}"
MAX_MANIFEST = 16384
MAX_ARCHIVE = 65536
MAX_API = 8 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def obj(value: object) -> dict[str, object]:
    require(isinstance(value, dict), "expected API object")
    return value


def positive(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def commit_sha(value: object) -> str:
    require(
        isinstance(value, str)
        and SHA.fullmatch(value) is not None
        and value != "0" * 40,
        "invalid commit SHA",
    )
    return value


class API(Protocol):
    def get(self, path: str) -> dict[str, object]: ...
    def download(self, artifact_id: int) -> bytes: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI:
    """HTTPS only; API redirects rejected, signed archive URL fetched WITHOUT token."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        require(
            bool(self.token) and "\n" not in self.token and "\r" not in self.token,
            "missing/invalid GitHub credential",
        )
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, url: str, *, authenticated: bool, limit: int) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if authenticated:
            require(
                url.startswith("https://api.github.com" + PREFIX + "/"),
                "foreign API URL",
            )
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        with self.opener.open(request, timeout=20) as response:
            require(response.status == 200, "unexpected HTTP status")
            data = response.read(limit + 1)
            require(len(data) <= limit, "response exceeds size bound")
            return data

    def get(self, path: str) -> dict[str, object]:
        require(path.startswith(PREFIX + "/") and "#" not in path, "foreign API path")
        try:
            return obj(
                _json(
                    self._request(
                        "https://api.github.com" + path,
                        authenticated=True,
                        limit=MAX_API,
                    ).decode("utf-8")
                )
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise ValueError("GitHub API request failed") from error

    def download(self, artifact_id: int) -> bytes:
        require(positive(artifact_id), "invalid artifact ID")
        try:
            # The documented endpoint returns 302. Never let urllib forward its token.
            self._request(
                f"https://api.github.com{PREFIX}/actions/artifacts/{artifact_id}/zip",
                authenticated=True,
                limit=MAX_ARCHIVE,
            )
        except urllib.error.HTTPError as redirect:
            if redirect.code != 302:
                raise ValueError("artifact download API failed") from redirect
            location = redirect.headers.get("Location", "")
            redirect.close()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ValueError("artifact download API failed") from error
        else:
            raise ValueError("artifact endpoint did not redirect")
        parsed = urllib.parse.urlsplit(location)
        host = parsed.hostname or ""
        require(
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and host.endswith(
                (".blob.core.windows.net", ".actions.githubusercontent.com")
            ),
            "untrusted artifact archive host",
        )
        try:
            # No Authorization on the signed URL; further redirects also fail closed.
            return self._request(location, authenticated=False, limit=MAX_ARCHIVE)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ValueError("artifact archive request failed") from error


def verify_run(data: dict[str, object], context: Context, workflow_id: int) -> None:
    require(
        type(data.get("id")) is int and data["id"] == context.workflow_run_id,
        "run ID mismatch",
    )
    require(
        type(data.get("workflow_id")) is int
        and data["workflow_id"] == workflow_id
        and data.get("path") == ".github/workflows/ci.yml",
        "foreign workflow",
    )
    for field in ("repository", "head_repository"):
        require(
            obj(data.get(field)).get("full_name") == REPOSITORY,
            "foreign run repository",
        )
    require(
        data.get("event") == "push"
        and data.get("head_branch") == "master"
        and data.get("head_sha") == context.sha
        and data.get("pull_requests") == []
        and data.get("status") == "completed"
        and data.get("conclusion") == "success"
        and type(data.get("run_attempt")) is int
        and data["run_attempt"] == context.run_attempt,
        "untrusted/incomplete run",
    )


def master_sha(api: API) -> str:
    reference = obj(api.get(PREFIX + "/git/ref/heads/master").get("object"))
    require(reference.get("type") == "commit", "master is not a commit")
    return commit_sha(reference.get("sha"))


def tree(api: API, sha: str) -> dict[str, tuple[str, str, str]]:
    commit = api.get(f"{PREFIX}/git/commits/{commit_sha(sha)}")
    tree_sha = commit_sha(obj(commit.get("tree")).get("sha"))
    response = api.get(f"{PREFIX}/git/trees/{tree_sha}?recursive=1")
    require(
        response.get("truncated") is False and isinstance(response.get("tree"), list),
        "missing/truncated git tree",
    )
    result = {}
    seen: set[str] = set()
    for raw in response["tree"]:
        item = obj(raw)
        path, mode, kind = item.get("path"), item.get("mode"), item.get("type")
        require(
            isinstance(path, str)
            and path
            and not path.startswith("/")
            and all(part not in {"", ".", ".."} for part in path.split("/"))
            and "\0" not in path
            and path not in seen,
            "invalid git tree path",
        )
        seen.add(path)
        require(
            isinstance(mode, str) and isinstance(kind, str), "invalid git tree entry"
        )
        object_sha = commit_sha(item.get("sha"))
        if kind == "tree":
            require(mode == "040000", "invalid directory mode")
            continue
        require(
            (kind == "blob" and mode in {"100644", "100755", "120000"})
            or (kind == "commit" and mode == "160000"),
            "unknown git object kind",
        )
        result[path] = (mode, kind, object_sha)
    return result


def verify_freshness(api: API, sha: str, master: str) -> None:
    if sha == master:
        return
    comparison = api.get(f"{PREFIX}/compare/{commit_sha(sha)}...{commit_sha(master)}")
    require(
        comparison.get("status") == "ahead"
        and obj(comparison.get("merge_base_commit")).get("sha") == sha,
        "release is not an ancestor of current master",
    )
    old, new = tree(api, sha), tree(api, master)
    raw = bytearray()
    for path in sorted(old.keys() | new.keys()):
        before, after = old.get(path), new.get(path)
        if before == after:
            continue
        old_mode = before[0] if before else "000000"
        new_mode = after[0] if after else "000000"
        status = "M" if before and after else "D" if before else "A"
        raw.extend(f":{old_mode} {new_mode} {'0' * 40} {'0' * 40} {status}\0".encode())
        raw.extend(path.encode("utf-8") + b"\0")
    # Exactly the existing classifier semantics, including modes, deletes and both
    # rename endpoints (represented conservatively as delete/add).
    require(_docs_diff(bytes(raw)), "release stale: non-documentation or empty delta")


def parse_archive(payload: bytes) -> Release:
    require(0 < len(payload) <= MAX_ARCHIVE, "artifact archive too large/empty")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = archive.infolist()
            require(len(members) == 1, "expected singleton release manifest")
            member = members[0]
            mode = member.external_attr >> 16
            require(
                member.filename == "release.json"
                and not member.is_dir()
                and not member.flag_bits & 1
                and (stat.S_IFMT(mode) in (0, stat.S_IFREG))
                and not mode & 0o111
                and 0 < member.file_size <= MAX_MANIFEST,
                "unsafe release archive member",
            )
            with archive.open(member) as stream:
                content = stream.read(MAX_MANIFEST + 1)
            require(len(content) <= MAX_MANIFEST, "manifest too large")
            return Release.parse(content.decode("utf-8"))
    except (
        zipfile.BadZipFile,
        UnicodeError,
        RuntimeError,
        NotImplementedError,
    ) as error:
        raise ValueError("invalid release archive") from error


def verify_release(run_id: str, sha: str, *, api: API | None = None) -> Release:
    require(
        isinstance(run_id, str)
        and re.fullmatch(r"[1-9][0-9]{0,18}", run_id) is not None,
        "invalid release run ID",
    )
    commit_sha(sha)
    require(positive(int(run_id)), "invalid release run ID")
    api = api if api is not None else GitHubAPI()
    workflow = api.get(PREFIX + "/actions/workflows/ci.yml")
    workflow_id = workflow.get("id")
    require(
        positive(workflow_id)
        and workflow.get("path") == ".github/workflows/ci.yml"
        and workflow.get("state") == "active",
        "foreign/inactive CI workflow",
    )
    run_path = f"{PREFIX}/actions/runs/{run_id}"
    run = api.get(run_path)
    context = Context(sha, int(run_id), run.get("run_attempt"))
    verify_run(run, context, workflow_id)
    master = master_sha(api)
    verify_freshness(api, sha, master)
    listing = api.get(run_path + "/artifacts?per_page=100")
    artifacts = listing.get("artifacts")
    count = listing.get("total_count")
    require(
        type(count) is int
        and isinstance(artifacts, list)
        and count == len(artifacts)
        and 0 < count <= 100,
        "incomplete artifact listing",
    )
    name = f"release-{sha}-{run_id}-{context.run_attempt}"
    matches = [obj(item) for item in artifacts if obj(item).get("name") == name]
    require(len(matches) == 1, "missing/ambiguous release artifact")
    artifact = matches[0]
    require(
        positive(artifact.get("id"))
        and artifact.get("expired") is False
        and positive(artifact.get("size_in_bytes"))
        and artifact["size_in_bytes"] <= MAX_ARCHIVE,
        "invalid/expired artifact",
    )
    expires = artifact.get("expires_at")
    require(isinstance(expires, str), "missing artifact expiry")
    try:
        expiry = datetime.fromisoformat(expires)
        require(
            expiry.tzinfo is not None and expiry > datetime.now(UTC),
            "artifact expired",
        )
    except (ValueError, OverflowError) as error:
        raise ValueError("invalid/expired artifact timestamp") from error
    provenance = obj(artifact.get("workflow_run"))
    require(
        type(provenance.get("id")) is int
        and provenance["id"] == context.workflow_run_id
        and provenance.get("head_sha") == sha
        and provenance.get("head_branch") == "master",
        "artifact run mismatch",
    )
    payload = api.download(artifact["id"])
    digest = artifact.get("digest")
    if digest is not None:
        require(
            digest == "sha256:" + hashlib.sha256(payload).hexdigest(),
            "artifact digest mismatch",
        )
    release = parse_archive(payload)
    release.bind(context)
    # Fail rather than approve a mixed view if a rerun/master race occurred.
    verify_run(api.get(run_path), context, workflow_id)
    require(master_sha(api) == master, "master changed during verification; retry")
    return release


def trusted_dispatch(env: Mapping[str, str]) -> None:
    require(
        env.get("CICD_DEPLOY_ENABLED") == "true",
        "deployment disabled: CICD_DEPLOY_ENABLED must be true",
    )
    require(
        env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_REF") == "refs/heads/master"
        and env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_WORKFLOW_REF")
        == f"{REPOSITORY}/.github/workflows/deploy.yml@refs/heads/master",
        "untrusted deployment dispatch/ref",
    )


def main() -> int:
    try:
        trusted_dispatch(os.environ)
        release = verify_release(
            os.environ.get("RELEASE_RUN_ID", ""), os.environ.get("RELEASE_SHA", "")
        )
        message = json.dumps(release.to_dict(), separators=(",", ":")) + "\n"
        require(len(message.encode()) <= MAX_MANIFEST, "request too large")
        sys.stdout.write(message)
    except (ValueError, OSError, TypeError) as error:
        # Do not echo external HTTP bodies, signed URLs, tokens or supplied values.
        print(f"release verification refused: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
