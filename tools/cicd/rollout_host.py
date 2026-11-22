"""Bounded, installed-host operations; never an Actions command interpreter.

The parent owns deployment locking, durable stages, approved schema verification,
backup/restore and smoke tests. Provision this module, interpreter, CLI binaries
and configuration root-owned before use. The existing restricted deploy identity
is NOT sufficient: exec/db scaling/CRI access need separate installation review.
There is deliberately no CLI, generic kubectl/exec method or credential writer.
"""

import hashlib
import ipaddress
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from tools.cicd.deploy_credentials import _validate_kubeconfig
from tools.cicd.maintenance import Images
from tools.cicd.release import _json, _object, _require
from tools.cicd.schema_probe import protected_credential_read, protected_read

NAMESPACE = "coreteams"
CONTAINERS = {
    "frontend": "frontend",
    "backend": "backend",
    "db": "postgres",
    "s3": "seaweedfs",
}
PROCEDURE_SHA256 = "3be29ddbb47695e55539ed23a1e74b0da8c919a6e70d663543955402e64a39b5"
OUTPUT_LIMIT = 1024 * 1024
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Unknown/null statuses count as active. Do not limit this to pending/running.
NO_ACTIVE_RUNS = b"""BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';
SELECT count(*) FROM public.runs
WHERE status IS NULL OR status NOT IN ('completed','failed','cancelled','outcome_unknown');
COMMIT;
"""


class Runner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, stdin: bytes, timeout: int, env: Mapping[str, str]
    ) -> bytes: ...


def bounded_run(
    argv: Sequence[str], *, stdin: bytes, timeout: int, env: Mapping[str, str]
) -> bytes:
    """Drain both pipes with a total byte cap; reap only our process group."""
    _require(type(timeout) is int and 1 <= timeout <= 200, "invalid command timeout")
    _require(len(stdin) <= OUTPUT_LIMIT, "oversized command input")
    output = bytearray()
    total = 0
    with subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        start_new_session=True,
    ) as child:
        try:
            assert (
                child.stdin is not None
                and child.stdout is not None
                and child.stderr is not None
            )
            with selectors.DefaultSelector() as selector:
                for stream in (child.stdout, child.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                if stdin:
                    os.set_blocking(child.stdin.fileno(), False)
                    selector.register(child.stdin, selectors.EVENT_WRITE)
                else:
                    child.stdin.close()
                remaining = memoryview(stdin)
                deadline = time.monotonic() + timeout
                while selector.get_map():
                    budget = deadline - time.monotonic()
                    if budget <= 0:
                        raise TimeoutError("host command deadline exceeded")
                    for key, _ in selector.select(budget):
                        if key.fileobj is child.stdin:
                            written = os.write(key.fd, remaining[:65536])
                            remaining = remaining[written:]
                            if not remaining:
                                selector.unregister(key.fileobj)
                                child.stdin.close()
                        else:
                            data = os.read(key.fd, 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(data)
                            _require(
                                total <= OUTPUT_LIMIT,
                                "host command output exceeds bound",
                            )
                            if key.fileobj is child.stdout:
                                output.extend(data)
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
            _require(child.returncode == 0, "host command failed")
            return bytes(output)
        except BaseException:
            # Also kill descendants retaining the pipes after their parent exits.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            raise


def _absolute(value: str) -> str:
    _require(
        isinstance(value, str)
        and value.startswith("/")
        and "\x00" not in value
        and ".." not in Path(value).parts,
        "explicit absolute host path required",
    )
    return value


def _trusted_executable(value: str) -> None:
    path = Path(value)
    for component in (path, *path.parents):
        info = component.lstat()
        _require(
            info.st_uid == 0
            and not info.st_mode & 0o022
            and not stat.S_ISLNK(info.st_mode),
            "untrusted host executable path",
        )
        _require(
            stat.S_ISREG(info.st_mode)
            if component == path
            else stat.S_ISDIR(info.st_mode),
            "invalid host executable path",
        )
    _require(os.access(path, os.X_OK), "host executable is not executable")


@dataclass(frozen=True)
class HostConfig:
    """Values come from a reviewed protected config, never a deploy request."""

    kubectl: str
    k3s: str
    kubeconfig: str
    cri_endpoint: str
    db_socket: str
    psql: str
    db_env: str

    def __post_init__(self) -> None:
        for value in (
            self.kubectl,
            self.k3s,
            self.kubeconfig,
            self.db_socket,
            self.psql,
            self.db_env,
        ):
            _absolute(value)
        _require(
            isinstance(self.cri_endpoint, str)
            and self.cri_endpoint.startswith("unix:///"),
            "explicit local CRI endpoint required",
        )
        _absolute(self.cri_endpoint.removeprefix("unix://"))

    @classmethod
    def load(cls, path: Path) -> "HostConfig":
        value = _object(
            _json(protected_read(path, 16384).decode("utf-8")),
            {
                "kubectl",
                "k3s",
                "kubeconfig",
                "cri_endpoint",
                "db_socket",
                "psql",
                "db_env",
            },
        )
        config = cls(**value)
        for executable in (config.kubectl, config.k3s):
            _trusted_executable(executable)
        _validate_kubeconfig(
            protected_credential_read(Path(config.kubeconfig), exact_mode=True)
        )
        return config


@dataclass(frozen=True)
class ImagePin:
    reference: str
    runtime_digest: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.reference, str)
            and len(self.reference) <= 512
            and re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", self.reference)
            is not None,
            "immutable image reference required",
        )
        _require(
            isinstance(self.runtime_digest, str)
            and DIGEST.fullmatch(self.runtime_digest),
            "verified runtime digest required",
        )


@dataclass(frozen=True)
class CurrentRelease:
    frontend: ImagePin
    backend: ImagePin
    source_signature: str

    def __post_init__(self) -> None:
        _require(
            isinstance(self.frontend, ImagePin) and isinstance(self.backend, ImagePin),
            "image pins required",
        )
        _require(
            isinstance(self.source_signature, str)
            and 0 < len(self.source_signature) <= 512,
            "source signature required",
        )


@dataclass(frozen=True)
class DatabaseIdentity:
    system_identifier: str
    binding: str
    pod: str
    address: str
    port: int
    revisions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require(
            type(self.system_identifier) is str
            and re.fullmatch(r"[1-9][0-9]{0,19}", self.system_identifier) is not None
            and int(self.system_identifier) < 2**64,
            "invalid physical identity",
        )
        for value in (self.binding, self.pod):
            _require(
                re.fullmatch(r"[0-9a-f]{64}", value) is not None, "invalid binding"
            )
        ipaddress.ip_address(self.address)
        _require(type(self.port) is int and 1 <= self.port <= 65535, "invalid DB port")
        _require(
            type(self.revisions) is tuple and len(self.revisions) == 1, "revision drift"
        )
        _require(
            re.fullmatch(r"[0-9]{8}_[0-9]{4}", self.revisions[0]) is not None,
            "revision drift",
        )


def _identity_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _mounted_volume(
    spec: dict, container_name: str, mount_path: str
) -> tuple[dict, dict]:
    """Select one whole writable PVC; overlapping mounts cannot mask its bytes."""
    containers = spec["containers"]
    _require(
        len(containers) == 1
        and containers[0]["name"] == container_name
        and not spec.get("ephemeralContainers"),
        "storage container drift",
    )
    root = Path(mount_path)
    mounts = [
        mount
        for mount in containers[0].get("volumeMounts", [])
        if Path(mount["mountPath"]).is_relative_to(root)
        or root.is_relative_to(Path(mount["mountPath"]))
    ]
    _require(
        len(mounts) == 1
        and mounts[0]["mountPath"] == mount_path
        and not mounts[0].get("subPath")
        and not mounts[0].get("subPathExpr")
        and not mounts[0].get("readOnly"),
        "storage mount drift",
    )
    volumes = [v for v in spec["volumes"] if v["name"] == mounts[0]["name"]]
    _require(
        len(volumes) == 1
        and set(volumes[0]) == {"name", "persistentVolumeClaim"}
        and not volumes[0]["persistentVolumeClaim"].get("readOnly"),
        "storage volume drift",
    )
    return mounts[0], volumes[0]


class K3sHost:
    def __init__(
        self,
        config: HostConfig,
        *,
        backup_paths: Mapping[str, Path],
        runner: Runner = bounded_run,
    ) -> None:
        _require(
            set(backup_paths) == {"home", "data", "s3_pvc"}, "backup roots required"
        )
        for path in backup_paths.values():
            _require(isinstance(path, Path), "backup root must be a path")
            _absolute(str(path))
        self._backup_paths = dict(backup_paths)
        self._storage_binding: str | None = None
        self.config = config
        self._runner = runner
        self._stage = "new"
        self._ddl_attempted = False
        self._node: str | None = None
        self._original: CurrentRelease | None = None
        self._original_images: dict[str, str] = {}
        self._started: dict[str, ImagePin] = {}
        self._observation_deadline: float | None = None

    def _at(self, stage: str) -> None:
        _require(self._stage == stage, "host stage boundary refused")

    def _kubectl(
        self, args: Sequence[str], *, stdin: bytes = b"", timeout: int = 30
    ) -> bytes:
        config = self.config
        if self._observation_deadline is not None:
            remaining = int(self._observation_deadline - time.monotonic())
            _require(remaining > 0, "identity deadline exceeded")
            timeout = min(timeout, remaining)
        return self._runner(
            [
                config.kubectl,
                "--request-timeout=20s",
                f"--kubeconfig={config.kubeconfig}",
                "--context=coreteams-deploy",
                "-n",
                NAMESPACE,
                *args,
            ],
            stdin=stdin,
            timeout=timeout,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/var/empty",
                "KUBECONFIG": config.kubeconfig,
            },
        )

    def _get(self, kind: str, name: str) -> dict:
        value = _json(self._kubectl(["get", kind, name, "-o", "json"]).decode("utf-8"))
        _require(isinstance(value, dict), "invalid Kubernetes response")
        return value

    def _deployment(self, name: str, replicas: int) -> dict:
        value = self._get("deployment", name)
        _require(
            value["metadata"]["name"] == name
            and value["metadata"]["namespace"] == NAMESPACE,
            "deployment identity mismatch",
        )
        spec = value["spec"]
        _require(
            spec["replicas"] == replicas and type(spec["replicas"]) is int,
            "deployment replica drift",
        )
        _require(
            spec["selector"] == {"matchLabels": {"app": name}}
            and spec["template"]["metadata"]["labels"].get("app") == name,
            "deployment selector drift",
        )
        containers = spec["template"]["spec"]["containers"]
        _require(
            len(containers) == 1 and containers[0]["name"] == CONTAINERS[name],
            "deployment container drift",
        )
        return value

    def _pods(self) -> list[dict]:
        value = _json(self._kubectl(["get", "pods", "-o", "json"]).decode("utf-8"))
        _require(
            isinstance(value, dict) and isinstance(value.get("items"), list),
            "invalid pod list",
        )
        for pod in value["items"]:
            _require(
                pod["metadata"]["namespace"] == NAMESPACE
                and pod["metadata"]["labels"].get("app") in CONTAINERS,
                "unknown namespace writer pod",
            )
        return value["items"]

    def _pod(self, name: str) -> dict:
        pods = [p for p in self._pods() if p["metadata"]["labels"]["app"] == name]
        _require(len(pods) == 1, "expected exactly one host pod")
        pod = pods[0]
        statuses = pod["status"]["containerStatuses"]
        _require(
            not pod["metadata"].get("deletionTimestamp")
            and pod["status"]["phase"] == "Running"
            and len(statuses) == 1
            and statuses[0]["name"] == CONTAINERS[name]
            and statuses[0]["ready"] is True
            and "running" in statuses[0]["state"],
            "host pod is not ready",
        )
        node = pod["spec"]["nodeName"]
        _require(isinstance(node, str) and bool(node), "missing host node")
        _require(
            self._node is None or self._node == node,
            "workloads must share the reviewed node",
        )
        self._node = node
        _require(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", pod["metadata"]["name"])
            is not None,
            "invalid pod identity",
        )
        return pod

    def _psql(self, sql: bytes, *, timeout: int = 20) -> bytes:
        pod = self._pod("db")
        return self._kubectl(
            [
                "exec",
                "-i",
                pod["metadata"]["name"],
                "-c",
                "postgres",
                "--",
                self.config.db_env,
                "-i",
                "HOME=/var/empty",
                self.config.psql,
                "-X",
                "-w",
                "--set=ON_ERROR_STOP=1",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--host",
                self.config.db_socket,
                "--username",
                "coreteams",
                "--dbname",
                "coreteams",
            ],
            stdin=sql,
            timeout=timeout,
        )

    def database_evidence(self, *, timeout_seconds: int) -> str:
        """Measure the cold-restore comparison while application writers are stopped."""
        from tools.cicd.rollout_restore import EVIDENCE_SQL, sanitize_evidence

        self._at("apps_stopped")
        self._assert_absent(["frontend", "backend"])
        _require(0 < timeout_seconds <= 60, "invalid evidence timeout")
        sql = (
            "BEGIN READ ONLY; SET LOCAL statement_timeout='30s'; "
            "SET LOCAL lock_timeout='5s'; SET LOCAL search_path=pg_catalog;\n"
            + EVIDENCE_SQL
            + ";\nCOMMIT;\n"
        )
        return sanitize_evidence(
            self._psql(sql.encode(), timeout=timeout_seconds).decode("utf-8")
        )

    def _local_volume(self, claim: str, path: Path) -> tuple[dict, dict]:
        """GET only the mounted claim and its named, same-node local PV."""
        pvc = self._get("pvc", claim)
        _require(
            pvc["metadata"]["namespace"] == NAMESPACE
            and pvc["metadata"]["name"] == claim
            and isinstance(pvc["metadata"]["uid"], str)
            and bool(pvc["metadata"]["uid"])
            and not pvc["metadata"].get("deletionTimestamp")
            and pvc["status"]["phase"] == "Bound"
            and pvc["spec"].get("volumeMode", "Filesystem") == "Filesystem",
            "PVC binding drift",
        )
        pv = self._get("pv", pvc["spec"]["volumeName"])
        spec = pv["spec"]
        ref = spec["claimRef"]
        _require(
            ref["namespace"] == NAMESPACE
            and ref["name"] == claim
            and ref["uid"] == pvc["metadata"]["uid"]
            and pv["metadata"]["name"] == pvc["spec"]["volumeName"]
            and isinstance(pv["metadata"]["uid"], str)
            and bool(pv["metadata"]["uid"])
            and pv["status"]["phase"] == "Bound"
            and not pv["metadata"].get("deletionTimestamp")
            and spec.get("volumeMode", "Filesystem") == "Filesystem",
            "PV binding drift",
        )
        sources = set(spec) - {
            "capacity",
            "accessModes",
            "claimRef",
            "nodeAffinity",
            "volumeMode",
            "persistentVolumeReclaimPolicy",
            "storageClassName",
            "mountOptions",
        }
        _require(sources in ({"hostPath"}, {"local"}), "local PV required")
        local = spec[next(iter(sources))]
        _require(
            type(local) is dict and local.get("path") == str(path),
            "backup path drift",
        )
        _require(
            spec["nodeAffinity"]["required"]["nodeSelectorTerms"]
            == [
                {
                    "matchExpressions": [
                        {
                            "key": "kubernetes.io/hostname",
                            "operator": "In",
                            "values": [self._node],
                        }
                    ]
                }
            ],
            "PV node drift",
        )
        return pvc, pv

    def _measure_storage_binding(self, *, backend_replicas: int) -> str:
        bindings = []
        for name, replicas, roots in (
            (
                "backend",
                backend_replicas,
                (
                    ("home", "/home/node", "coreteams-home"),
                    ("data", "/app/backend/data", "coreteams-data"),
                ),
            ),
            ("s3", 1, (("s3_pvc", "/data", "s3-data"),)),
        ):
            deployment = self._deployment(name, replicas)
            template = deployment["spec"]["template"]
            pod = self._pod(name) if replicas else None
            allowed_init = ["fix-perms", "wait-db"] if name == "backend" else []
            init = template["spec"].get("initContainers", [])
            names = [c["name"] for c in init]
            _require(
                names in ([], allowed_init)
                and all(not c.get("restartPolicy") for c in init),
                "unexpected storage init container",
            )
            if pod is not None:
                _require(
                    [c["name"] for c in pod["spec"].get("initContainers", [])] == names
                    and all(
                        not c.get("restartPolicy")
                        for c in pod["spec"].get("initContainers", [])
                    )
                    and [
                        s["name"]
                        for s in pod["status"].get("initContainerStatuses", [])
                    ]
                    == names
                    and all(
                        s["state"].get("terminated", {}).get("exitCode") == 0
                        for s in pod["status"].get("initContainerStatuses", [])
                    ),
                    "storage init container has not exited cleanly",
                )
            for key, mount_path, claim in roots:
                mount, volume = _mounted_volume(
                    template["spec"], CONTAINERS[name], mount_path
                )
                _require(
                    volume["persistentVolumeClaim"]["claimName"] == claim,
                    "storage claim drift",
                )
                if pod is not None:
                    _require(
                        _mounted_volume(pod["spec"], CONTAINERS[name], mount_path)
                        == (mount, volume),
                        "Pod storage differs from deployment",
                    )
                pvc, pv = self._local_volume(claim, self._backup_paths[key])
                bindings.append(
                    {
                        "root": key,
                        "deployment_uid": deployment["metadata"]["uid"],
                        "template": template,
                        "mount": mount,
                        "volume": volume,
                        "pvc_uid": pvc["metadata"]["uid"],
                        "pvc_spec": pvc["spec"],
                        "pv_uid": pv["metadata"]["uid"],
                        "pv_spec": pv["spec"],
                        "node": self._node,
                    }
                )
        return _identity_digest(bindings)

    def _assert_storage_binding(self, *, backend_replicas: int) -> None:
        _require(self._storage_binding is not None, "storage preflight required")
        _require(
            self._measure_storage_binding(backend_replicas=backend_replicas)
            == self._storage_binding,
            "backup storage binding changed",
        )

    def _database_binding(
        self, endpoint: str, port: int, pgdata: Path
    ) -> tuple[str, str, str]:
        """Only the existing numeric Service, exact ready Pod and local bound PVC.

        PV access is GET on the name obtained from that PVC, never list/discovery.
        The caller pins this complete stable binding before the cold restart.
        """
        ipaddress.ip_address(endpoint)
        service = self._get("service", "db")
        _require(
            service["metadata"]["namespace"] == NAMESPACE
            and service["spec"]["clusterIP"] == endpoint
            and service["spec"].get("type", "ClusterIP") == "ClusterIP"
            and service["spec"]["selector"] == {"app": "db"}
            and not service["spec"].get("externalIPs")
            and not service["spec"].get("externalName"),
            "DB Service mapping drift",
        )
        ports = service["spec"]["ports"]
        _require(
            len(ports) == 1
            and ports[0]["port"] == port
            and ports[0]["targetPort"] == port
            and ports[0].get("protocol", "TCP") == "TCP",
            "DB Service port drift",
        )
        deployment = self._deployment("db", 1)
        pod = self._pod("db")
        address = str(ipaddress.ip_address(pod["status"]["podIP"]))
        endpoints = self._get("endpoints", "db")
        subsets = endpoints["subsets"]
        _require(len(subsets) == 1, "DB endpoints drift")
        subset = subsets[0]
        peers = subset["addresses"]
        _require(
            not subset.get("notReadyAddresses")
            and len(peers) == 1
            and peers[0]["ip"] == address
            and peers[0]["targetRef"]["kind"] == "Pod"
            and peers[0]["targetRef"]["namespace"] == NAMESPACE
            and peers[0]["targetRef"]["name"] == pod["metadata"]["name"]
            and peers[0]["targetRef"]["uid"] == pod["metadata"]["uid"]
            and len(subset["ports"]) == 1
            and subset["ports"][0]["port"] == port
            and subset["ports"][0].get("protocol", "TCP") == "TCP",
            "DB endpoint peer drift",
        )
        container = pod["spec"]["containers"][0]
        _require(container["name"] == "postgres", "DB container drift")
        mounts = [
            m
            for m in container["volumeMounts"]
            if m["mountPath"] == "/var/lib/postgresql/data"
        ]
        _require(
            len(mounts) == 1
            and not mounts[0].get("subPath")
            and not mounts[0].get("subPathExpr")
            and not mounts[0].get("readOnly"),
            "DB mount drift",
        )
        volumes = [v for v in pod["spec"]["volumes"] if v["name"] == mounts[0]["name"]]
        _require(
            len(volumes) == 1
            and set(volumes[0]) == {"name", "persistentVolumeClaim"}
            and not volumes[0]["persistentVolumeClaim"].get("readOnly"),
            "DB volume drift",
        )
        claim = volumes[0]["persistentVolumeClaim"]["claimName"]
        _require(pgdata.name == "pgdata", "DB backup path drift")
        pvc, pv = self._local_volume(claim, pgdata.parent)
        runtime = pod["status"]["containerStatuses"][0]
        image = runtime["imageID"].split("://")[-1].split("@")[-1]
        _require(DIGEST.fullmatch(image) is not None, "DB runtime digest required")
        binding = _identity_digest(
            {
                "service_uid": service["metadata"]["uid"],
                "service": service["spec"],
                "deployment_uid": deployment["metadata"]["uid"],
                "template": deployment["spec"]["template"],
                "pvc_uid": pvc["metadata"]["uid"],
                "pvc_spec": pvc["spec"],
                "pv_uid": pv["metadata"]["uid"],
                "pv_spec": pv["spec"],
                "node": pod["spec"]["nodeName"],
                "mount": mounts[0],
                "volume": volumes[0],
                "image": image,
            }
        )
        current = _identity_digest(
            {
                "uid": pod["metadata"]["uid"],
                "name": pod["metadata"]["name"],
                "container": runtime["containerID"],
                "address": address,
            }
        )
        return binding, current, address

    def database_identity(
        self, *, endpoint: str, port: int, pgdata: Path, timeout_seconds: int
    ) -> DatabaseIdentity:
        """Read pg_control_system from the socket inside the currently bound Pod.

        Protected exec/binaries and the exact mounted data_directory bind this
        socket measurement to the same Pod serving the verified catalog peer.
        """
        _require(0 < timeout_seconds <= 60, "invalid identity timeout")
        _require(self._observation_deadline is None, "nested DB observation refused")
        self._observation_deadline = time.monotonic() + timeout_seconds
        try:
            return self._measure_database_identity(endpoint, port, pgdata)
        finally:
            self._observation_deadline = None

    def _measure_database_identity(
        self, endpoint: str, port: int, pgdata: Path
    ) -> DatabaseIdentity:
        deadline = self._observation_deadline
        assert deadline is not None
        before = self._database_binding(endpoint, port, pgdata)
        sql = b"""BEGIN READ ONLY;
SET LOCAL statement_timeout='5s'; SET LOCAL lock_timeout='2s';
SELECT pg_catalog.jsonb_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
'data_directory',pg_catalog.current_setting('data_directory'),
'port',pg_catalog.current_setting('port')::integer,
'revisions',(SELECT pg_catalog.jsonb_agg(version_num ORDER BY version_num) FROM public.alembic_version));
COMMIT;
"""
        budget = int(deadline - time.monotonic())
        _require(budget > 0, "identity deadline exceeded")
        value = _object(
            _json(self._psql(sql, timeout=budget).decode("ascii")),
            {"system_identifier", "data_directory", "port", "revisions"},
        )
        after = self._database_binding(endpoint, port, pgdata)
        _require(
            time.monotonic() < deadline and before == after,
            "DB binding changed during identity read",
        )
        _require(
            value["data_directory"] == "/var/lib/postgresql/data/pgdata"
            and value["port"] == port
            and type(value["revisions"]) is list,
            "DB physical endpoint drift",
        )
        return DatabaseIdentity(
            value["system_identifier"],
            before[0],
            before[1],
            before[2],
            value["port"],
            tuple(value["revisions"]),
        )

    def schema_revisions(self, *, timeout_seconds: int) -> tuple[str, ...]:
        _require(0 < timeout_seconds <= 60, "invalid evidence timeout")
        sql = b"BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; SELECT version_num FROM public.alembic_version ORDER BY version_num; COMMIT;\n"
        result = tuple(
            self._psql(sql, timeout=timeout_seconds).decode("ascii").splitlines()
        )
        _require(
            len(result) == 1
            and re.fullmatch(r"[0-9]{8}_[0-9]{4}", result[0]) is not None,
            "unexpected revision row",
        )
        return result

    def _no_active_runs(self) -> None:
        _require(
            self._psql(NO_ACTIVE_RUNS).strip() == b"0",
            "active or unknown Run prevents maintenance",
        )

    def preflight(
        self, expected: CurrentRelease, *, source_probe: Callable[[], str]
    ) -> None:
        self._at("new")
        _require(
            isinstance(expected, CurrentRelease) and callable(source_probe),
            "current release and actual source probe required",
        )
        original_images = {}
        for name in CONTAINERS:
            deployment = self._deployment(name, 1)
            original_images[name] = deployment["spec"]["template"]["spec"][
                "containers"
            ][0]["image"]
            pod = self._pod(name)
            if name in ("frontend", "backend"):
                pin = expected.frontend if name == "frontend" else expected.backend
                _require(
                    deployment["spec"]["template"]["spec"]["containers"][0]["image"]
                    == pin.reference
                    and pod["spec"]["containers"][0]["image"] == pin.reference,
                    "current image drift",
                )
                actual = pod["status"]["containerStatuses"][0]["imageID"]
                _require(
                    isinstance(actual, str)
                    and actual.split("://")[-1].split("@")[-1] == pin.runtime_digest,
                    "current runtime digest drift",
                )
        storage_binding = self._measure_storage_binding(backend_replicas=1)
        # Prove that CRI inspection is on the workload node, not an empty remote node.
        local_ids = {c["id"] for c in self._cri()}
        for name in CONTAINERS:
            identifier = self._pod(name)["status"]["containerStatuses"][0][
                "containerID"
            ]
            _require(
                isinstance(identifier, str)
                and identifier.startswith("containerd://")
                and identifier.removeprefix("containerd://") in local_ids,
                "CRI is not on the workload node",
            )
        _require(
            source_probe() == expected.source_signature, "actual source signature drift"
        )
        self._no_active_runs()
        _require(
            self._measure_storage_binding(backend_replicas=1) == storage_binding,
            "backup storage changed during preflight",
        )
        self._storage_binding = storage_binding
        self._original = expected
        self._original_images = original_images
        self._stage = "preflight"

    def _cri(self) -> list[dict]:
        raw = self._runner(
            [
                self.config.k3s,
                "crictl",
                "--runtime-endpoint",
                self.config.cri_endpoint,
                "--timeout",
                "10s",
                "ps",
                "--state",
                "Running",
                "-o",
                "json",
            ],
            stdin=b"",
            timeout=20,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/var/empty"},
        )
        value = _json(raw.decode("utf-8"))
        _require(
            isinstance(value, dict) and isinstance(value.get("containers"), list),
            "invalid CRI container list",
        )
        return value["containers"]

    def _assert_absent(self, names: Sequence[str]) -> None:
        _require(
            not any(p["metadata"]["labels"]["app"] in names for p in self._pods()),
            "application pods still present",
        )
        for container in self._cri():
            labels = container["labels"]
            namespace = labels["io.kubernetes.pod.namespace"]
            _require(
                isinstance(namespace, str) and bool(namespace), "missing CRI namespace"
            )
            if namespace == NAMESPACE:
                name = container["metadata"]["name"]
                _require(
                    name in CONTAINERS.values()
                    and name not in {CONTAINERS[n] for n in names},
                    "CRI writer still running",
                )

    def _scale(self, name: str, before: int, after: int) -> None:
        self._deployment(name, before)
        self._kubectl(
            [
                "scale",
                f"deployment/{name}",
                f"--current-replicas={before}",
                f"--replicas={after}",
            ]
        )
        if after == 0:
            self._kubectl(
                ["wait", "--for=delete", "pod", "-l", f"app={name}", "--timeout=120s"],
                timeout=130,
            )
            self._deployment(name, 0)
            self._assert_absent([name])
        else:
            self._kubectl(
                ["rollout", "status", f"deployment/{name}", "--timeout=120s"],
                timeout=130,
            )
            self._deployment(name, 1)
            self._pod(name)

    def gate_ingress(self) -> None:
        """Frontend zero is the ingress hold; no Service/Ingress rewiring."""
        self._at("preflight")
        self._assert_storage_binding(backend_replicas=1)
        self._no_active_runs()
        self._scale("frontend", 1, 0)
        self._stage = "gated"

    def stop_applications(self) -> None:
        self._at("gated")
        self._assert_storage_binding(backend_replicas=1)
        self._assert_absent(["frontend"])
        self._no_active_runs()
        self._scale("backend", 1, 0)
        self._assert_absent(["frontend", "backend"])
        self._no_active_runs()
        self._stage = "apps_stopped"

    def stop_database_for_cold_copy(self) -> None:
        self._at("apps_stopped")
        self._assert_storage_binding(backend_replicas=0)
        self._assert_absent(["frontend", "backend"])
        self._no_active_runs()
        self._pod("s3")  # S3 stays running to store the physical backup.
        self._scale("db", 1, 0)
        self._assert_absent(["frontend", "backend", "db"])
        self._stage = "db_stopped"

    def assert_quiescent(self) -> None:
        """Backup boundary: DB and applications cold; storage remains available."""
        self._at("db_stopped")
        self._assert_storage_binding(backend_replicas=0)
        for name in ("frontend", "backend", "db"):
            self._deployment(name, 0)
        self._assert_absent(["frontend", "backend", "db"])
        self._deployment("s3", 1)
        self._pod("s3")

    def restart_database(self) -> None:
        self.assert_quiescent()
        self._scale("db", 0, 1)
        _require(
            self._psql(
                b"BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; SELECT 1; COMMIT;"
            ).strip()
            == b"1",
            "database socket health failed",
        )
        self._stage = "db_ready"

    def _original_replicas(self) -> dict[str, int]:
        _require(
            not self._ddl_attempted and self._original is not None,
            "original recovery unavailable after possible DDL or without preflight",
        )
        replicas = {}
        for name in CONTAINERS:
            count = self._get("deployment", name)["spec"]["replicas"]
            _require(type(count) is int and count in (0, 1), "recovery replica drift")
            value = self._deployment(name, count)
            _require(
                value["spec"]["template"]["spec"]["containers"][0]["image"]
                == self._original_images[name],
                "original recovery image drift",
            )
            replicas[name] = count
        _require(replicas["s3"] == 1, "original storage unavailable")
        self._pod("s3")
        # Only prefixes of the fixed frontend/backend/db shutdown are recoverable.
        _require(
            tuple(replicas[n] for n in ("frontend", "backend", "db"))
            in {(1, 1, 1), (0, 1, 1), (0, 0, 1), (0, 0, 0)},
            "unexpected partial shutdown",
        )
        absent = [n for n in ("frontend", "backend", "db") if replicas[n] == 0]
        self._assert_absent(absent)
        return replicas

    def restore_database_only_if_original(self) -> None:
        """Recover a pre-DDL shutdown, never a restore/downgrade after DDL.

        Use the same live instance under the parent's durable lock/journal. The
        parent must independently rule out another writer or interrupted restore.
        Only DB scaling is permitted; application writes remain held if stopped.
        """
        replicas = self._original_replicas()
        if replicas["db"] == 0:
            self._scale("db", 0, 1)
        else:
            self._pod("db")
        _require(
            self._psql(
                b"BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; SELECT 1; COMMIT;"
            ).strip()
            == b"1",
            "original database health failed",
        )
        self._stage = "original_db_ready"

    def _original_pod(self, name: str, pin: ImagePin) -> None:
        pod = self._pod(name)
        _require(
            pod["spec"]["containers"][0]["image"] == pin.reference
            and pod["status"]["containerStatuses"][0]["imageID"]
            .split("://")[-1]
            .split("@")[-1]
            == pin.runtime_digest,
            "original runtime image drift",
        )

    def restore_original_applications(
        self, expected: CurrentRelease, *, source_probe: Callable[[], str]
    ) -> None:
        """Start only unchanged originals after fresh parent-owned schema proof.

        No image patches, retries of SQL, or automatic downgrade. A failed proof
        leaves stopped applications stopped. The source probe must be the strict
        target-bound verifier, not merely an Alembic revision query.
        """
        self._at("original_db_ready")
        _require(
            expected == self._original and callable(source_probe),
            "original recovery approval mismatch",
        )
        replicas = self._original_replicas()
        _require(replicas["db"] == 1, "original database is stopped")
        self._pod("db")
        for name, pin in (
            ("backend", expected.backend),
            ("frontend", expected.frontend),
        ):
            if replicas[name] == 1:
                self._original_pod(name, pin)
        _require(
            source_probe() == expected.source_signature, "original schema proof failed"
        )
        self._no_active_runs()
        for name, pin in (
            ("backend", expected.backend),
            ("frontend", expected.frontend),
        ):
            if replicas[name] == 0:
                # Repeat source proof before releasing each previously held writer.
                _require(
                    source_probe() == expected.source_signature,
                    "original schema proof failed",
                )
                current = self._deployment(name, 0)
                _require(
                    current["spec"]["template"]["spec"]["containers"][0]["image"]
                    == self._original_images[name],
                    "original recovery image drift",
                )
                self._scale(name, 0, 1)
            self._original_pod(name, pin)
        self._stage = "original_restored"

    def run(self, procedure: bytes, *, timeout_seconds: int) -> None:
        """PsqlProcess for the schema owner's fixed, transactional procedure.

        No wrapper: approved bytes already contain BEGIN/COMMIT, source revision
        assertion, lock and statement timeouts. Parent verifies schema afterwards.
        A failed/ambiguous attempt is never replayed on this host instance.
        """
        self._at("db_ready")
        _require(not self._ddl_attempted, "DDL already attempted; reconcile")
        _require(
            type(procedure) is bytes
            and hashlib.sha256(procedure).hexdigest() == PROCEDURE_SHA256,
            "unapproved maintenance procedure",
        )
        _require(
            type(timeout_seconds) is int and 1 <= timeout_seconds <= 180,
            "invalid DDL timeout",
        )
        self._assert_absent(["frontend", "backend"])
        self._assert_storage_binding(backend_replicas=0)
        self._ddl_attempted = True
        self._psql(procedure, timeout=timeout_seconds)
        self._stage = "ddl_applied"

    def start_backend(self, image: ImagePin) -> None:
        self._at("ddl_applied")
        self._start_image("backend", image)
        self._stage = "backend_ready"

    def start_frontend(self, image: ImagePin) -> None:
        self._at("backend_ready")
        self._start_image("frontend", image)
        self._stage = "frontend_ready"

    def confirm_ready(self, *, frontend: bool, timeout_seconds: int = 180) -> None:
        """Recheck what is running, rather than trusting the completed start stage."""
        _require(
            type(timeout_seconds) is int and 0 < timeout_seconds <= 180,
            "invalid readiness timeout",
        )
        self._at("frontend_ready" if frontend else "backend_ready")
        if not frontend:
            self._deployment("frontend", 0)
            self._assert_absent(["frontend"])
        for name in ("backend", "frontend") if frontend else ("backend",):
            pin = self._started[name]
            deployment = self._deployment(name, 1)
            status = deployment["status"]
            _require(
                status.get("observedGeneration") == deployment["metadata"]["generation"]
                and all(
                    status.get(key) == 1
                    for key in (
                        "replicas",
                        "updatedReplicas",
                        "readyReplicas",
                        "availableReplicas",
                    )
                )
                and deployment["spec"]["template"]["spec"]["containers"][0]["image"]
                == pin.reference,
                "deployment has not converged",
            )
            self._original_pod(name, pin)

    def ready_images(self, *, timeout_seconds: int = 180) -> Images:
        if self._stage == "original_restored":
            assert self._original is not None
            for name, pin in (
                ("backend", self._original.backend),
                ("frontend", self._original.frontend),
            ):
                self._deployment(name, 1)
                self._original_pod(name, pin)
            return Images(
                self._original.backend.reference, self._original.frontend.reference
            )
        self.confirm_ready(frontend=True, timeout_seconds=timeout_seconds)
        return Images(
            self._started["backend"].reference, self._started["frontend"].reference
        )

    def _start_image(
        self, name: Literal["frontend", "backend"], image: ImagePin
    ) -> None:
        _require(isinstance(image, ImagePin), "verified image pin required")
        self._assert_absent([name])
        current = self._deployment(name, 0)
        patch = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/spec/replicas", "value": 0},
            {
                "op": "test",
                "path": "/spec/template/spec/containers/0/name",
                "value": name,
            },
            {
                "op": "replace",
                "path": "/spec/template/spec/containers/0/image",
                "value": image.reference,
            },
        ]
        self._kubectl(
            ["patch", "deployment", name, "--type=json", "-p", json.dumps(patch)]
        )
        self._scale(name, 0, 1)
        pod = self._pod(name)
        _require(
            pod["spec"]["containers"][0]["image"] == image.reference
            and pod["status"]["containerStatuses"][0]["imageID"]
            .split("://")[-1]
            .split("@")[-1]
            == image.runtime_digest,
            "started image digest drift",
        )
        self._started[name] = image
