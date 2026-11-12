"""Installed server boundary, not uploaded release tooling.

Provision this module and its imports root-owned, with a root-only state directory.
Deployment patch rights can run code with Pod privileges; they are not a sandbox.
The installed catalog provider is lazy and runs only on the locked risk path.
Production activation still requires protected configuration and operator review.
"""

import fcntl
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from tools.cicd.classify import _is_doc
from tools.cicd.release import REPOSITORY, SHA, Release, _json, _object, _require
from tools.cicd.verify_release import tree

LIMIT = 16384
NAMES = ("backend", "frontend")
PREFIX = f"/repos/{REPOSITORY}"
STATE = Path("/var/lib/coreteams-deploy")
CONFIG = Path("/etc/coreteams-deploy/clearance.json")


class API(Protocol):
    def get(self, path: str) -> dict[str, object]: ...


class Verifier(Protocol):
    def __call__(self, release: Release) -> Release: ...


class Command(Protocol):
    def __call__(self, args: Sequence[str]) -> str: ...


@dataclass(frozen=True)
class SchemaEvidence:
    """Actual live introspection identity, not an Alembic revision.

    A separately reviewed trusted provider must query the actual database under
    the deployment lock, bound its I/O, and derive signature and target identity.
    A static file, version table, or caller assertion does not implement this API.
    """

    signature: str
    target: str

    def __post_init__(self) -> None:
        _require(
            all(
                isinstance(v, str) and 0 < len(v) <= 512
                for v in (self.signature, self.target)
            ),
            "invalid actual schema evidence",
        )


@dataclass(frozen=True)
class Clearance:
    sha: str
    evidence: SchemaEvidence
    preflight: str
    rehearsal: str
    approval: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.sha, str) and SHA.fullmatch(self.sha),
            "invalid clearance SHA",
        )
        _require(
            all(
                isinstance(v, str) and 0 < len(v) <= 512
                for v in (self.preflight, self.rehearsal, self.approval)
            ),
            "missing maintenance evidence",
        )

    @classmethod
    def parse(cls, text: str) -> "Clearance":
        data = _object(
            _json(text),
            {"sha", "signature", "target", "preflight", "rehearsal", "approval"},
        )
        return cls(
            data["sha"],
            SchemaEvidence(data["signature"], data["target"]),
            data["preflight"],
            data["rehearsal"],
            data["approval"],
        )


def parse_request(raw: bytes) -> Release:
    _require(0 < len(raw) <= LIMIT, "request exceeds bound or is empty")
    return Release.parse(raw.decode("utf-8"))


def read_request(stream: BinaryIO) -> Release:
    # Bound reading time as well as bytes; slow/no-EOF senders cannot hold forever.
    def expired(signum: int, frame: object) -> None:
        raise TimeoutError("stdin deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(15)
    try:
        return parse_request(stream.read(LIMIT + 1))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def deployment_lock(directory: Path) -> Iterator[None]:
    fd = os.open(directory / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("deployment already running") from error
        yield
    finally:
        os.close(fd)


def atomic_state(directory: Path, data: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / "state.json")
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_state(directory: Path) -> dict | None:
    path = directory / "state.json"
    if not path.exists():
        return None
    _require(path.stat().st_size <= 65536, "oversized state")
    data = _object(
        _json(path.read_text()),
        {
            "status",
            "requested",
            "deployed_sha",
            "previous",
            "previous_release",
            "schema_signature",
            "schema_target",
            "updated_at",
            "reason",
        },
    )
    _require(data["status"] in {"in_progress", "succeeded", "failed"}, "invalid state")
    Release.parse(json.dumps(data["requested"]))
    if data["previous_release"] is not None:
        Release.parse(json.dumps(data["previous_release"]))
    _require(
        data["deployed_sha"] is None
        or (
            isinstance(data["deployed_sha"], str)
            and SHA.fullmatch(data["deployed_sha"])
        ),
        "invalid deployed SHA",
    )
    previous = _object(data["previous"], set(NAMES))
    _require(
        all(isinstance(v, str) and 0 < len(v) <= 1024 for v in previous.values()),
        "invalid previous images",
    )
    SchemaEvidence(data["schema_signature"], data["schema_target"])
    _require(
        isinstance(data["updated_at"], str) and isinstance(data["reason"], str),
        "invalid state metadata",
    )
    if data["status"] == "succeeded":
        _require(data["deployed_sha"] == data["requested"]["sha"], "state SHA mismatch")
    return data


def migration_risk(api: API, baseline: str, candidate: str) -> bool:
    if baseline == candidate:
        return False
    comparison = api.get(f"{PREFIX}/compare/{baseline}...{candidate}")
    _require(comparison.get("status") == "ahead", "candidate is older or non-ancestor")
    _require(
        isinstance(comparison.get("merge_base_commit"), dict)
        and comparison["merge_base_commit"].get("sha") == baseline,
        "unproven ancestry",
    )
    before, after = tree(api, baseline), tree(api, candidate)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    # Observed startup/DDL inventory: main/db/migrations/bridge/models/schemas/
    # repository, runner_transport/containers. Treat ALL backend and unknown code
    # (including dependencies) as risk; existing regular UI source edits cannot
    # change backend startup/schema. New/moved UI paths remain review-required.
    for path in changed:
        regular = all(
            entry is None or entry[:2] == ("100644", "blob")
            for entry in (before.get(path), after.get(path))
        )
        known_ui = (
            path in before
            and path.startswith(("frontend/src/", "web-product/src/"))
            and path.endswith((".ts", ".tsx", ".js", ".jsx", ".css"))
        )
        if not regular or not (_is_doc(path.encode("utf-8")) or known_ui):
            return True
    return False


def kubectl(args: Sequence[str], *, env: Mapping[str, str] | None = None) -> str:
    # Only this process's child is terminated/reaped on timeout or signal.
    with subprocess.Popen(
        ["/usr/local/bin/kubectl", "--request-timeout=20s", "-n", "coreteams", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **({"env": env} if env is not None else {}),
    ) as child:
        try:
            stdout, _ = child.communicate(timeout=200)
        except BaseException:
            child.kill()
            child.wait()
            raise
        _require(child.returncode == 0, "kubectl operation failed")
        return stdout


def snapshot(command: Command, name: str) -> tuple[str, str, int]:
    value = _json(command(["get", "deployment", name, "-o", "json"]))
    _require(isinstance(value, dict), "invalid deployment")
    metadata = value["metadata"]
    _require(
        metadata["name"] == name and metadata["namespace"] == "coreteams",
        "deployment identity mismatch",
    )
    version = metadata["resourceVersion"]
    _require(isinstance(version, str) and bool(version), "missing resourceVersion")
    containers = value["spec"]["template"]["spec"]["containers"]
    _require(
        isinstance(containers, list)
        and len(containers) == 1
        and containers[0]["name"] == name,
        "container contract mismatch",
    )
    image = containers[0]["image"]
    _require(isinstance(image, str) and bool(image), "missing current image")
    return version, image, 0


def deploy(
    release: Release,
    *,
    state_dir: Path,
    verifier: Verifier,
    api: API,
    command: Command,
    clearance: Clearance | None = None,
    schema_probe: Callable[[], SchemaEvidence] | None = None,
) -> str:
    with deployment_lock(state_dir):
        old = read_state(state_dir)
        _require(
            old is None or old["status"] == "succeeded",
            "manual reconciliation required",
        )
        _require(verifier(release) == release, "GitHub release mismatch")
        baseline = old["deployed_sha"] if old else None
        risk = baseline is None or migration_risk(api, baseline, release.context.sha)
        evidence = None
        if risk:
            _require(
                clearance is not None
                and clearance.sha == release.context.sha
                and schema_probe is not None,
                "maintenance clearance/live schema provider required",
            )
            evidence = schema_probe()
            _require(
                isinstance(evidence, SchemaEvidence) and evidence == clearance.evidence,
                "actual schema mismatch",
            )
        else:
            evidence = SchemaEvidence(old["schema_signature"], old["schema_target"])
        current = {name: snapshot(command, name) for name in NAMES}
        if old is not None:
            previous_release = Release.parse(json.dumps(old["requested"]))
            _require(
                all(
                    current[n][1]
                    == f"{previous_release.images[n].repository}@{previous_release.images[n].digest}"
                    for n in NAMES
                ),
                "cluster drift requires reconciliation",
            )
        targets = {
            name: f"{release.images[name].repository}@{release.images[name].digest}"
            for name in NAMES
        }
        # Reverify current master, provenance, checks and attempt immediately before
        # mutations, under the lock. A later master update cannot be made atomic
        # with the cluster; the recorded SHA is exact, never asserted latest.
        _require(verifier(release) == release, "GitHub release mismatch")
        if baseline == release.context.sha:
            _require(
                old["requested"] == release.to_dict(), "same SHA different release"
            )
            _require(
                all(current[n][1] == targets[n] for n in NAMES),
                "cluster drift requires reconciliation",
            )
            for name in NAMES:
                command(["rollout", "status", f"deployment/{name}", "--timeout=180s"])
            return "already deployed"
        data = {
            "status": "in_progress",
            "requested": release.to_dict(),
            "deployed_sha": baseline,
            "previous": {n: current[n][1] for n in NAMES},
            "previous_release": old["requested"] if old else None,
            "schema_signature": evidence.signature,
            "schema_target": evidence.target,
            "updated_at": datetime.now(UTC).isoformat(),
            "reason": "",
        }
        atomic_state(state_dir, data)
        try:
            for name in NAMES:
                version, image, index = current[name]
                path = f"/spec/template/spec/containers/{index}/image"
                patch = [
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": version,
                    },
                    {
                        "op": "test",
                        "path": f"/spec/template/spec/containers/{index}/name",
                        "value": name,
                    },
                    {"op": "test", "path": path, "value": image},
                    {"op": "replace", "path": path, "value": targets[name]},
                ]
                command(
                    [
                        "patch",
                        "deployment",
                        name,
                        "--type=json",
                        "-p",
                        json.dumps(patch),
                    ]
                )
                command(["rollout", "status", f"deployment/{name}", "--timeout=180s"])
            _require(
                all(snapshot(command, n)[1] == targets[n] for n in NAMES),
                "post-rollout image drift",
            )
            data["status"], data["deployed_sha"] = "succeeded", release.context.sha
        except BaseException as error:
            # A signal/crash after a patch is ambiguous: never retry/undo blindly.
            data["status"], data["reason"] = "failed", type(error).__name__
            raise
        finally:
            data["updated_at"] = datetime.now(UTC).isoformat()
            atomic_state(state_dir, data)
        return "succeeded"


def trusted_path(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    _require(
        info.st_uid == 0
        and not info.st_mode & 0o022
        and not stat.S_ISLNK(info.st_mode),
        "untrusted installed path",
    )
    _require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "wrong installed path type",
    )


def main(*, api: API | None = None, command: Command | None = None) -> int:
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    def terminate(signum: int, frame: object) -> None:
        raise InterruptedError("deployment terminated")

    signal.signal(signal.SIGTERM, terminate)
    try:
        # Parent paths/imports/interpreter and forced command must also be secured
        # by provisioning; this is not a substitute for installation review.
        trusted_path(Path(__file__))
        trusted_path(STATE.parent, directory=True)
        trusted_path(STATE, directory=True)
        release = read_request(sys.stdin.buffer)
        clearance = None
        if CONFIG.exists():
            from tools.cicd.schema_probe import protected_read

            clearance = Clearance.parse(protected_read(CONFIG, LIMIT).decode("utf-8"))
        from tools.cicd.verify_release import GitHubAPI, verify_release

        api = api if api is not None else GitHubAPI()

        def verify(request: Release) -> Release:
            return verify_release(
                str(request.context.workflow_run_id), request.context.sha, api=api
            )

        schema_probe = None
        if clearance is not None:
            from tools.cicd.schema_probe import installed_probe

            schema_probe = installed_probe(release.context.sha, clearance)

        deploy(
            release,
            state_dir=STATE,
            verifier=verify,
            api=api,
            command=command if command is not None else kubectl,
            clearance=clearance,
            schema_probe=schema_probe,
        )
    except (ValueError, OSError, KeyError, TypeError, UnicodeError):
        # Never echo tokens, kubectl stderr or caller-supplied payloads.
        print(
            "deployment refused; inspect trusted state and reconcile with operator",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
