"""Offline stages, exact argv, fail-closed drift, and real local pipe bounds."""

import hashlib
import json
import sys
from dataclasses import replace

import pytest

from tools.cicd import rollout_host as host


@pytest.fixture
def config():
    # Test-only paths, not a claim about the production installation.
    return host.HostConfig(
        "/reviewed/kubectl",
        "/reviewed/k3s",
        "/reviewed/kubeconfig",
        "unix:///reviewed/containerd.sock",
        "/reviewed/socket",
        "/reviewed/psql",
        "/reviewed/env",
    )


@pytest.fixture
def release():
    return host.CurrentRelease(
        host.ImagePin("registry/frontend@sha256:" + "a" * 64, "sha256:" + "b" * 64),
        host.ImagePin("registry/backend@sha256:" + "c" * 64, "sha256:" + "d" * 64),
        "reviewed-source-signature",
    )


class Cluster:
    """Fake transport only; adapter state transitions and validation remain real."""

    def __init__(self, config, release):
        self.config = config
        self.calls = []
        self.replicas = dict.fromkeys(host.CONTAINERS, 1)
        self.images = {
            "frontend": release.frontend.reference,
            "backend": release.backend.reference,
            "db": "postgres:16",
            "s3": "seaweedfs:fixed",
        }
        self.digests = {
            "frontend": release.frontend.runtime_digest,
            "backend": release.backend.runtime_digest,
            "db": "sha256:" + "e" * 64,
            "s3": "sha256:" + "f" * 64,
        }
        self.active = b"0\n"
        self.ghost = None
        self.remote_cri = False
        self.unready = None
        self.unknown = False
        self.fail_ddl = False
        self.ddl = []

    def pod(self, name):
        return {
            "metadata": {
                "name": name + "-pod",
                "namespace": "coreteams",
                "labels": {"app": name},
            },
            "spec": {
                "nodeName": "reviewed-node",
                "containers": [{"image": self.images[name]}],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": host.CONTAINERS[name],
                        "ready": name != self.unready,
                        "state": {"running": {}},
                        "imageID": "containerd://" + self.digests[name],
                        "containerID": "containerd://" + name + "-id",
                    }
                ],
            },
        }

    def __call__(self, argv, *, stdin, timeout, env):
        argv = list(argv)
        self.calls.append((argv, stdin, timeout, env))
        assert 1 <= timeout <= 200
        assert "PGOPTIONS" not in env and "GH_TOKEN" not in env
        if argv[0] == self.config.k3s:
            assert argv == [
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
            ]
            names = [n for n in host.CONTAINERS if self.replicas[n] or self.ghost == n]
            return json.dumps(
                {
                    "containers": []
                    if self.remote_cri
                    else [
                        {
                            "id": n + "-id",
                            "metadata": {"name": host.CONTAINERS[n]},
                            "labels": {"io.kubernetes.pod.namespace": "coreteams"},
                        }
                        for n in names
                    ]
                }
            ).encode()
        assert argv[:6] == [
            self.config.kubectl,
            "--request-timeout=20s",
            "--kubeconfig=" + self.config.kubeconfig,
            "--context=coreteams-deploy",
            "-n",
            "coreteams",
        ]
        args = argv[6:]
        if args[:2] == ["get", "pods"]:
            pods = [self.pod(n) for n in host.CONTAINERS if self.replicas[n]]
            if self.unknown:
                pod = self.pod("db")
                pod["metadata"]["labels"]["app"] = "other-writer"
                pods.append(pod)
            return json.dumps({"items": pods}).encode()
        if args[:2] == ["get", "deployment"]:
            name = args[2]
            return json.dumps(
                {
                    "metadata": {
                        "name": name,
                        "namespace": "coreteams",
                        "resourceVersion": "1",
                    },
                    "spec": {
                        "replicas": self.replicas[name],
                        "selector": {"matchLabels": {"app": name}},
                        "template": {
                            "metadata": {"labels": {"app": name}},
                            "spec": {
                                "containers": [
                                    {
                                        "name": host.CONTAINERS[name],
                                        "image": self.images[name],
                                    }
                                ]
                            },
                        },
                    },
                }
            ).encode()
        if args[0] == "scale":
            name = args[1].split("/")[1]
            assert self.replicas[name] == int(args[2].split("=")[1])
            self.replicas[name] = int(args[3].split("=")[1])
            return b""
        if args[0] == "exec":
            assert args == [
                "exec",
                "-i",
                "db-pod",
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
            ]
            if stdin == host.NO_ACTIVE_RUNS:
                return self.active
            if b"SELECT 1" in stdin:
                return b"1\n"
            self.ddl.append(stdin)
            if self.fail_ddl:
                raise TimeoutError("ambiguous commit")
            return b""
        if args[:2] == ["patch", "deployment"]:
            patch = json.loads(args[-1])
            assert patch[0]["op"] == "test" and patch[1]["value"] == 0
            self.images[args[2]] = patch[-1]["value"]
            return b""
        if args[0] in {"wait", "rollout"}:
            return b""
        raise AssertionError(args)


@pytest.fixture
def rig(config, release):
    cluster = Cluster(config, release)
    adapter = host.K3sHost(config, runner=cluster)
    return adapter, cluster, release


def preflight(rig):
    adapter, _, release = rig
    adapter.preflight(release, source_probe=lambda: release.source_signature)
    return adapter


def cold_restart(rig):
    adapter = preflight(rig)
    adapter.gate_ingress()
    adapter.stop_applications()
    adapter.stop_database_for_cold_copy()
    adapter.restart_database()
    return adapter


@pytest.mark.parametrize(
    "operation",
    [
        "gate_ingress",
        "stop_applications",
        "stop_database_for_cold_copy",
        "restart_database",
    ],
)
def test_unprepared_host_cannot_mutate(rig, operation):
    adapter, cluster, _ = rig
    with pytest.raises(ValueError, match="stage"):
        getattr(adapter, operation)()
    assert not cluster.calls


@pytest.mark.parametrize("drift", ["frontend", "backend", "db", "s3", "cri"])
def test_backup_quiescence_checks_detect_drift(rig, drift):
    adapter = preflight(rig)
    adapter.gate_ingress()
    adapter.stop_applications()
    adapter.stop_database_for_cold_copy()
    adapter.assert_quiescent()
    if drift == "cri":
        rig[1].ghost = "backend"
    elif drift == "s3":
        rig[1].replicas["s3"] = 0
    else:
        rig[1].replicas[drift] = 1
    with pytest.raises(ValueError):
        adapter.assert_quiescent()


@pytest.mark.parametrize(
    "stopped",
    [(), ("frontend",), ("frontend", "backend"), ("frontend", "backend", "db")],
)
def test_restore_original_after_observed_partial_shutdown(rig, stopped):
    adapter = preflight(rig)
    cluster = rig[1]
    # Simulate failure after scale accepted, before stage completion was recorded.
    for name in stopped:
        cluster.replicas[name] = 0
    adapter.restore_database_only_if_original()
    assert cluster.replicas["db"] == 1
    for name in ("frontend", "backend"):
        assert cluster.replicas[name] == (0 if name in stopped else 1)
    adapter.restore_original_applications(
        rig[2], source_probe=lambda: rig[2].source_signature
    )
    assert all(cluster.replicas.values())
    assert not any("patch" in argv for argv, _, _, _ in cluster.calls)
    assert not cluster.ddl
    with pytest.raises(ValueError, match="stage"):
        adapter.run(b"unapproved", timeout_seconds=60)


@pytest.mark.parametrize(
    "drift",
    ["db_image", "app_image", "replicas", "cri", "source", "approval", "readiness"],
)
def test_original_recovery_fails_closed(rig, drift):
    adapter = cold_restart(rig)
    cluster = rig[1]
    if drift == "db_image":
        cluster.images["db"] = "different-db:tag"
    elif drift == "app_image":
        cluster.images["backend"] = "different-backend:tag"
    elif drift == "replicas":
        cluster.replicas["frontend"] = 1
    elif drift == "cri":
        cluster.ghost = "backend"
    elif drift == "readiness":
        cluster.unready = "db"
    if drift not in {"source", "approval"}:
        with pytest.raises(ValueError):
            adapter.restore_database_only_if_original()
    else:
        adapter.restore_database_only_if_original()
        expected = (
            replace(rig[2], source_signature="other") if drift == "approval" else rig[2]
        )
        with pytest.raises(ValueError):
            adapter.restore_original_applications(
                expected, source_probe=lambda: "wrong-source"
            )
    assert cluster.replicas["backend"] == 0
    assert not cluster.ddl


def test_original_recovery_requires_database_step(rig):
    adapter = cold_restart(rig)
    with pytest.raises(ValueError, match="stage"):
        adapter.restore_original_applications(
            rig[2], source_probe=lambda: rig[2].source_signature
        )


def test_original_recovery_requires_successful_preflight(rig):
    with pytest.raises(ValueError, match="preflight"):
        rig[0].restore_database_only_if_original()
    assert not rig[1].calls


@pytest.mark.parametrize("fail", [False, True])
def test_original_recovery_forbidden_after_any_ddl_attempt(rig, monkeypatch, fail):
    adapter = cold_restart(rig)
    procedure = b"approved-test-procedure"
    monkeypatch.setattr(host, "PROCEDURE_SHA256", hashlib.sha256(procedure).hexdigest())
    rig[1].fail_ddl = fail
    if fail:
        with pytest.raises(TimeoutError):
            adapter.run(procedure, timeout_seconds=60)
    else:
        adapter.run(procedure, timeout_seconds=60)
    calls = len(rig[1].calls)
    with pytest.raises(ValueError, match="possible DDL"):
        adapter.restore_database_only_if_original()
    assert len(rig[1].calls) == calls
    assert rig[1].replicas["backend"] == rig[1].replicas["frontend"] == 0


def test_quiescence_order_and_s3_left_running(rig):
    adapter = cold_restart(rig)
    _, cluster, _ = rig
    scales = [
        argv[6:]
        for argv, _, _, _ in cluster.calls
        if argv[0] == cluster.config.kubectl and argv[6] == "scale"
    ]
    assert scales == [
        ["scale", "deployment/frontend", "--current-replicas=1", "--replicas=0"],
        ["scale", "deployment/backend", "--current-replicas=1", "--replicas=0"],
        ["scale", "deployment/db", "--current-replicas=1", "--replicas=0"],
        ["scale", "deployment/db", "--current-replicas=0", "--replicas=1"],
    ]
    assert cluster.replicas == {"frontend": 0, "backend": 0, "db": 1, "s3": 1}
    with pytest.raises(ValueError, match="stage"):
        adapter.start_backend(rig[2].backend)


@pytest.mark.parametrize(
    "drift", ["active", "digest", "image", "source", "unknown", "remote_cri", "unready"]
)
def test_preflight_drift_fails_before_mutations(rig, drift):
    adapter, cluster, release = rig
    signature = release.source_signature
    if drift == "active":
        cluster.active = b"1\n"
    elif drift == "digest":
        cluster.digests["backend"] = "sha256:" + "a" * 64
    elif drift == "image":
        cluster.images["backend"] = "unapproved:tag"
    elif drift == "source":
        signature = "wrong"
    elif drift == "unready":
        cluster.unready = "backend"
    else:
        setattr(cluster, drift, True)
    with pytest.raises(ValueError):
        adapter.preflight(release, source_probe=lambda: signature)
    assert not any("scale" in argv for argv, _, _, _ in cluster.calls)


def test_active_run_after_preflight_does_not_gate(rig):
    adapter = preflight(rig)
    rig[1].active = b"1\n"
    with pytest.raises(ValueError, match="Run"):
        adapter.gate_ingress()
    assert rig[1].replicas["frontend"] == 1


def test_cri_orphan_blocks_backend_stop(rig):
    adapter = preflight(rig)
    rig[1].ghost = "frontend"
    with pytest.raises(ValueError, match="CRI"):
        adapter.gate_ingress()
    assert rig[1].replicas["backend"] == 1
    with pytest.raises(ValueError, match="stage"):
        adapter.stop_applications()


def test_unapproved_ddl_never_executes(rig):
    adapter = cold_restart(rig)
    with pytest.raises(ValueError, match="unapproved"):
        adapter.run(b"DROP DATABASE coreteams;", timeout_seconds=60)
    assert not rig[1].ddl


@pytest.mark.parametrize("fail", [False, True])
def test_psql_exact_stdin_once_and_no_retry_after_ambiguity(rig, monkeypatch, fail):
    adapter = cold_restart(rig)
    # Only the transport fixture is substituted, not the schema verification core.
    procedure = b"BEGIN; SELECT 'approved-fixture'; COMMIT;\n"
    monkeypatch.setattr(host, "PROCEDURE_SHA256", hashlib.sha256(procedure).hexdigest())
    rig[1].fail_ddl = fail
    if fail:
        with pytest.raises(TimeoutError):
            adapter.run(procedure, timeout_seconds=60)
    else:
        adapter.run(procedure, timeout_seconds=60)
    with pytest.raises(ValueError):
        adapter.run(procedure, timeout_seconds=60)
    assert rig[1].ddl == [procedure]
    assert all(
        procedure.decode() not in " ".join(argv) for argv, _, _, _ in rig[1].calls
    )
    if not fail:
        adapter.start_backend(rig[2].backend)
        adapter.start_frontend(rig[2].frontend)
        assert all(rig[1].replicas.values())


@pytest.mark.parametrize(
    "change",
    [
        {"kubectl": "kubectl"},
        {"db_socket": ""},
        {"psql": "/a/../psql"},
        {"cri_endpoint": "tcp://remote"},
    ],
)
def test_config_requires_explicit_local_paths(config, change):
    with pytest.raises(ValueError):
        replace(config, **change)


def test_config_load_uses_protected_reads_and_validates_executables(
    config, monkeypatch
):
    reads = []
    executables = []
    data = {field: getattr(config, field) for field in config.__dataclass_fields__}

    def read(path, limit):
        reads.append((str(path), limit))
        return json.dumps(data).encode() if len(reads) == 1 else b"kubeconfig-fixture"

    monkeypatch.setattr(host, "protected_read", read)
    monkeypatch.setattr(host, "_trusted_executable", executables.append)

    def credential_read(path, *, exact_mode):
        assert exact_mode is True
        return read(path, 16384)

    monkeypatch.setattr(host, "protected_credential_read", credential_read)
    monkeypatch.setattr(
        host,
        "_validate_kubeconfig",
        lambda raw: raw == b"kubeconfig-fixture" or pytest.fail(),
    )
    assert host.HostConfig.load(host.Path("/reviewed/config")) == config
    assert executables == [config.kubectl, config.k3s]
    assert reads == [("/reviewed/config", 16384), (config.kubeconfig, 16384)]


def test_actual_subprocess_sanitizes_and_returns_stdout():
    result = host.bounded_run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
        stdin=b"safe-input",
        timeout=2,
        env={},
    )
    assert result == b"safe-input"


def test_actual_subprocess_nonzero_does_not_leak_stderr():
    with pytest.raises(ValueError, match="host command failed") as error:
        host.bounded_run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.stderr.write('secret'); sys.exit(1)",
            ],
            stdin=b"",
            timeout=2,
            env={},
        )
    assert "secret" not in str(error.value)


def test_actual_subprocess_bounds_output():
    with pytest.raises(ValueError, match="output exceeds"):
        host.bounded_run(
            [sys.executable, "-I", "-c", "print('x' * 1100000)"],
            stdin=b"",
            timeout=2,
            env={},
        )


def test_actual_subprocess_deadline_kills_and_reaps_child():
    with pytest.raises(TimeoutError):
        host.bounded_run(
            [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
            stdin=b"",
            timeout=1,
            env={},
        )


@pytest.fixture
def identity_rig(config, release):
    from pathlib import Path

    cluster = Cluster(config, release)
    adapter = host.K3sHost(config, runner=cluster)
    original_pod = cluster.pod
    objects = {
        ("service", "db"): {
            "metadata": {"uid": "svc-uid", "namespace": host.NAMESPACE},
            "spec": {
                "clusterIP": "10.43.0.5",
                "selector": {"app": "db"},
                "ports": [{"port": 5432, "targetPort": 5432}],
            },
        },
        ("endpoints", "db"): {
            "subsets": [
                {
                    "addresses": [
                        {
                            "ip": "10.42.0.5",
                            "targetRef": {
                                "kind": "Pod",
                                "namespace": host.NAMESPACE,
                                "name": "db-pod",
                                "uid": "pod-uid",
                            },
                        }
                    ],
                    "ports": [{"port": 5432}],
                }
            ]
        },
        ("pvc", "db-data"): {
            "metadata": {
                "name": "db-data",
                "namespace": host.NAMESPACE,
                "uid": "pvc-uid",
            },
            "spec": {"volumeName": "pv-db"},
            "status": {"phase": "Bound"},
        },
        ("pv", "pv-db"): {
            "metadata": {"name": "pv-db", "uid": "pv-uid"},
            "spec": {
                "claimRef": {
                    "namespace": host.NAMESPACE,
                    "name": "db-data",
                    "uid": "pvc-uid",
                },
                "hostPath": {"path": "/reviewed/db"},
                "nodeAffinity": {
                    "required": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "kubernetes.io/hostname",
                                        "operator": "In",
                                        "values": ["reviewed-node"],
                                    }
                                ]
                            }
                        ]
                    }
                },
            },
            "status": {"phase": "Bound"},
        },
    }
    state = {
        "ip": "10.42.0.5",
        "uid": "pod-uid",
        "image": "e" * 64,
        "identity": {
            "system_identifier": "123456789",
            "data_directory": "/var/lib/postgresql/data/pgdata",
            "port": 5432,
            "revisions": ["20261111_0005"],
        },
    }

    def pod(name):
        value = original_pod(name)
        if name == "db":
            value["metadata"]["uid"] = state["uid"]
            value["status"]["podIP"] = state["ip"]
            value["status"]["containerStatuses"][0]["imageID"] = (
                "containerd://sha256:" + state["image"]
            )
            value["spec"]["containers"][0].update(
                name="postgres",
                volumeMounts=[
                    {"name": "data", "mountPath": "/var/lib/postgresql/data"}
                ],
            )
            value["spec"]["volumes"] = [
                {"name": "data", "persistentVolumeClaim": {"claimName": "db-data"}}
            ]
        return value

    cluster.pod = pod

    def runner(argv, **kwargs):
        args = list(argv)[6:]
        if args and args[0] == "get" and tuple(args[1:3]) in objects:
            return json.dumps(objects[tuple(args[1:3])]).encode()
        if args[:2] == ["get", "deployment"]:
            value = json.loads(cluster(argv, **kwargs))
            value["metadata"]["uid"] = "deployment-uid"
            return json.dumps(value).encode()
        if args and args[0] == "exec" and b"pg_control_system" in kwargs["stdin"]:
            assert "--host" in args and config.db_socket in args
            return json.dumps(state["identity"]).encode()
        return cluster(argv, **kwargs)

    adapter._runner = runner

    def measure():
        return adapter.database_identity(
            endpoint="10.43.0.5",
            port=5432,
            pgdata=Path("/reviewed/db/pgdata"),
            timeout_seconds=60,
        )

    return measure, objects, state


def test_live_identity_allows_only_pod_lifetime_to_change(identity_rig):
    measure, objects, state = identity_rig
    before = measure()
    state.update(ip="10.42.0.9", uid="new-pod-uid")
    peer = objects[("endpoints", "db")]["subsets"][0]["addresses"][0]
    peer["ip"] = state["ip"]
    peer["targetRef"]["uid"] = state["uid"]
    after = measure()
    assert before.binding == after.binding
    assert before.system_identifier == after.system_identifier
    assert before.pod != after.pod and before.address != after.address


@pytest.mark.parametrize(
    "drift",
    [
        "service",
        "peer_ip",
        "peer_uid",
        "pvc",
        "path",
        "data_directory",
        "port",
        "revisions",
    ],
)
def test_live_identity_rejects_broken_physical_binding(identity_rig, drift):
    measure, objects, state = identity_rig
    measure()
    if drift == "service":
        objects[("service", "db")]["spec"]["clusterIP"] = "10.43.0.99"
    elif drift in {"peer_ip", "peer_uid"}:
        peer = objects[("endpoints", "db")]["subsets"][0]["addresses"][0]
        if drift == "peer_ip":
            peer["ip"] = "10.42.0.99"
        else:
            peer["targetRef"]["uid"] = "wrong-pod"
    elif drift == "pvc":
        objects[("pvc", "db-data")]["metadata"]["uid"] = "wrong-pvc"
    elif drift == "path":
        objects[("pv", "pv-db")]["spec"]["hostPath"]["path"] = "/other/db"
    elif drift == "data_directory":
        state["identity"]["data_directory"] = "/other/pgdata"
    elif drift == "port":
        state["identity"]["port"] = 5433
    else:
        state["identity"]["revisions"].append("20261112_0001")
    with pytest.raises(ValueError):
        measure()


@pytest.mark.parametrize("drift", ["pv_uid", "pvc_uid", "image", "system_identifier"])
def test_live_identity_exposes_stable_identity_drift(identity_rig, drift):
    measure, objects, state = identity_rig
    before = measure()
    if drift == "pv_uid":
        objects[("pv", "pv-db")]["metadata"]["uid"] = "new-pv"
    elif drift == "pvc_uid":
        objects[("pvc", "db-data")]["metadata"]["uid"] = "new-pvc"
        objects[("pv", "pv-db")]["spec"]["claimRef"]["uid"] = "new-pvc"
    elif drift == "image":
        state["image"] = "f" * 64
    else:
        state["identity"]["system_identifier"] = "987654321"
    after = measure()
    assert (before.system_identifier, before.binding) != (
        after.system_identifier,
        after.binding,
    )


def test_identity_rejects_pod_replaced_during_socket_read(identity_rig):
    measure, objects, state = identity_rig
    # Select the existing adapter from the closure rather than bypass its guards.
    adapter = next(
        c.cell_contents
        for c in measure.__closure__
        if isinstance(c.cell_contents, host.K3sHost)
    )
    runner = adapter._runner

    def replaced(argv, **kwargs):
        result = runner(argv, **kwargs)
        if b"pg_control_system" in kwargs["stdin"]:
            state["uid"] = "replacement"
            objects[("endpoints", "db")]["subsets"][0]["addresses"][0]["targetRef"][
                "uid"
            ] = "replacement"
        return result

    adapter._runner = replaced
    with pytest.raises(ValueError, match="binding changed"):
        measure()
