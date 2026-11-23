"""Operator acceptance on a disposable namespace; never injects failures into production.

Run the reviewed package as root on the single-node server with --isolated.
Creates a logical read-only copy of production DB and independent file volumes.
Uses the installed application image with a read-only mount of candidate backend sources;
real immutable-image / workflow_run acceptance is performed separately after merge.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tools.cicd.cold_backup import atomic_json
from tools.cicd.controller import Controller, Host, run
from tools.cicd.release import Release

ROOT = Path("/var/lib/coreteams-deploy/acceptance")
NAMESPACE = "coreteams-deploy-test"
SOURCE = Path("/opt/coreteams-deploy/acceptance-source/backend")


def k(*args, data=None, namespace=NAMESPACE):
    return run(["kubectl", "-n", namespace, *args], data=data, timeout=180)


def get(kind, name, namespace=NAMESPACE):
    return json.loads(k("get", kind, name, "-o", "json", namespace=namespace))


def apply(item):
    k("apply", "-f", "-", data=json.dumps(item).encode())


def bootstrap():
    (SOURCE / "data").mkdir(exist_ok=True)
    if ROOT.exists():
        raise RuntimeError(
            "Acceptance directory already exists; retain evidence and choose explicit cleanup"
        )
    ROOT.mkdir(mode=0o700)
    apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}})
    for name in ("postgres", "s3", "coreteams-ghcr-pull-a3"):
        secret = get("secret", name, "coreteams")
        apply(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "type": secret["type"],
                "data": secret["data"],
            }
        )
    aliases = {"db": "db", "s3": "s3", "backend": "data"}
    for name in ("db", "s3", "backend", "frontend"):
        spec = copy.deepcopy(get("deployment", name, "coreteams")["spec"])
        spec["replicas"] = 0
        pod = spec["template"]["spec"]
        for volume in pod.get("volumes", []):
            alias = (
                "home"
                if name == "backend" and volume["name"] == "home"
                else aliases[name]
            )
            path = ROOT / "volumes" / alias
            path.mkdir(parents=True, exist_ok=True)
            volume.pop("persistentVolumeClaim", None)
            volume["hostPath"] = {"path": str(path), "type": "Directory"}
        if name == "backend":
            pod["containers"][0].pop("envFrom", None)
            pod["volumes"].append(
                {
                    "name": "backend-code",
                    "hostPath": {"path": str(SOURCE), "type": "Directory"},
                }
            )
            pod["containers"][0]["volumeMounts"].append(
                {"name": "backend-code", "mountPath": "/app/backend", "readOnly": True}
            )
        if name == "db":
            pod["containers"][0]["env"].append(
                {"name": "POSTGRES_INITDB_ARGS", "value": "--locale=en_US.utf8"}
            )
        apply(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": name},
                "spec": spec,
            }
        )
        service = get("service", name, "coreteams")
        apply(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name},
                "spec": {"selector": {"app": name}, "ports": service["spec"]["ports"]},
            }
        )
    initial = json.loads(
        Path("/var/lib/coreteams-deploy/maintenance-state.json").read_text()
    )["state"]["plan"]["release"]
    config = {
        "namespace": NAMESPACE,
        "kubeconfig": "/etc/rancher/k3s/k3s.yaml",
        "smoke_identities": "/etc/coreteams-deploy/smoke-identities.json",
        "backup_cert": "/etc/coreteams-deploy/rollout-backup.crt",
        "backup_key": "/etc/coreteams-deploy/rollout-backup.key",
        "volumes": {a: str(ROOT / "volumes" / a) for a in ("db", "data", "home", "s3")},
        "initial_release": initial,
        "retention": 12,
        "readiness_seconds": 45,
    }
    config["public_origin"] = (
        "http://" + get("service", "frontend")["spec"]["clusterIP"]
    )
    host = Host(config, ROOT)
    host.start_storage()
    # Whole database, all tables/sequences/data. Source is read-only during pg_dump.
    left = subprocess.Popen(
        [
            "kubectl",
            "-n",
            "coreteams",
            "exec",
            "deployment/db",
            "--",
            "pg_dump",
            "-U",
            "coreteams",
            "-d",
            "coreteams",
            "-Fc",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    right = subprocess.run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "exec",
            "-i",
            "deployment/db",
            "--",
            "pg_restore",
            "-U",
            "coreteams",
            "-d",
            "coreteams",
            "--no-owner",
            "--no-privileges",
            "--exit-on-error",
        ],
        stdin=left.stdout,
        capture_output=True,
        timeout=180,
    )
    left.stdout.close()
    if left.wait(timeout=180) or right.returncode:
        raise RuntimeError("Read-only logical clone failed")
    host.scale("backend", 1)
    host.scale("frontend", 1)
    host.smoke(public=True)
    atomic_json(ROOT / "config.json", config)
    return config


class AcceptanceHost(Host):
    fail = None
    crash = None

    def apply(self, resource):
        if resource.get("kind") == "Job":
            pod = resource["spec"]["template"]["spec"]
            pod["volumes"] = [
                {
                    "name": "code",
                    "hostPath": {
                        "path": self.config["candidate_source"],
                        "type": "Directory",
                    },
                }
            ]
            pod["containers"][0]["volumeMounts"] = [
                {"name": "code", "mountPath": "/app/backend", "readOnly": True}
            ]
        super().apply(resource)

    def candidate_start(self, release):
        self.patch(
            "deployment",
            "backend",
            {
                "spec": {
                    "template": {
                        "spec": {
                            "volumes": [
                                {
                                    "name": "data",
                                    "hostPath": {
                                        "path": self.config["volumes"]["data"],
                                        "type": "Directory",
                                    },
                                },
                                {
                                    "name": "home",
                                    "hostPath": {
                                        "path": self.config["volumes"]["home"],
                                        "type": "Directory",
                                    },
                                },
                                {
                                    "name": "backend-code",
                                    "hostPath": {
                                        "path": self.config["candidate_source"],
                                        "type": "Directory",
                                    },
                                },
                            ]
                        }
                    }
                }
            },
        )
        if self.fail == "startup":
            self.patch(
                "deployment",
                "backend",
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        **self.get("deployment", "backend")["spec"][
                                            "template"
                                        ]["spec"]["containers"][0],
                                        "command": ["sh", "-c", "exit 42"],
                                    }
                                ]
                            }
                        }
                    }
                },
            )
        super().candidate_start(release)

    def job(self, mode, state, clone_ip=None):
        super().job(mode, state, clone_ip)
        if mode == "upgrade" and not clone_ip and self.crash == "commit":
            self.crash = None
            raise KeyboardInterrupt("simulated lost controller after successful COMMIT")


class TestAPI:
    def get(self, path):
        old, new = path.rsplit("/", 1)[1].split("...")
        return {
            "status": "ahead" if int(old, 16) < int(new, 16) else "behind",
            "merge_base_commit": {"sha": min((old, new), key=lambda s: int(s, 16))},
        }


def candidate(config, name, parent, operation="ok"):
    path = ROOT / "sources" / name
    if path.exists():
        shutil.rmtree(path)
    shutil.copytree(SOURCE, path)
    versions = path / "migrations" / "versions"
    # Build on the previous successful dynamic graph, not a server-side revision list.
    if (ROOT / "current-source").exists():
        shutil.rmtree(path)
        shutil.copytree(Path((ROOT / "current-source").read_text()), path)
    revision = name
    if operation == "ok":
        models = path / "models.py"
        models.write_text(
            models.read_text()
            + """\n\nclass DeployAcceptanceEvidence(Base):
    __tablename__ = "deploy_acceptance_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
"""
        )
        body = """    op.create_table("deploy_acceptance_evidence", sa.Column("id",sa.Integer(),primary_key=True), sa.Column("payload",sa.Text(),nullable=False))
    op.execute("INSERT INTO deploy_acceptance_evidence(payload) SELECT 'Проверка — ' || n FROM generate_series(1,120001) AS n")
"""
    elif operation == "next":
        models = path / "models.py"
        column = "startup_note" if "    note:" in models.read_text() else "note"
        models.write_text(
            models.read_text()
            + f"    {column}: Mapped[str | None] = mapped_column(String(40), nullable=True)\n"
        )
        body = f'    op.add_column("deploy_acceptance_evidence", sa.Column("{column}",sa.String(40),nullable=True))\n'
    elif operation == "before":
        body = '    raise RuntimeError("Injected pre-DDL failure")\n'
    else:
        body = '    op.execute("CREATE TABLE failed_release_object (id integer)")\n    op.get_bind().commit()\n    raise RuntimeError("Injected failure after committed DDL")\n'
    (versions / (name + "_acceptance.py")).write_text(
        "from alembic import op\nimport sqlalchemy as sa\nrevision="
        + repr(revision)
        + "\ndown_revision="
        + repr(parent)
        + "\nbranch_labels=None\ndepends_on=None\ndef upgrade():\n"
        + body
        + '\ndef downgrade():\n    raise RuntimeError("Restore snapshot instead")\n'
    )
    # The backend runs as uid 1000, and hostPath ancestors otherwise have root-only access.
    for folder, dirs, files in os.walk(path):
        os.chmod(folder, 0o755)
        for file in files:
            os.chmod(Path(folder) / file, 0o644)
    config["candidate_source"] = str(path)
    atomic_json(ROOT / "config.json", config)
    return path


def check():
    config = (
        json.loads((ROOT / "config.json").read_text())
        if (ROOT / "config.json").exists()
        else bootstrap()
    )
    os.chmod(ROOT, 0o755)
    (ROOT / "sources").mkdir(mode=0o755, exist_ok=True)
    sequence = int(config["initial_release"]["sha"], 16)
    evidence = (
        json.loads((ROOT / "evidence.json").read_text())["cases"]
        if (ROOT / "evidence.json").exists()
        else []
    )
    controller_root = ROOT / "controller"
    if (controller_root / "state.json").exists():
        state = json.loads((controller_root / "state.json").read_text())
        sequence = max(
            sequence,
            int(state["current"]["sha"], 16),
            int(state.get("release", state["current"])["sha"], 16),
        )

    def deploy(name, operation="ok", failure=None, crash=None):
        nonlocal sequence
        if any(case["case"] == name for case in evidence):
            return
        source = candidate(config, name, parent, operation)
        sequence += 1
        data = copy.deepcopy(config["initial_release"])
        data["sha"] = f"{sequence:040x}"
        # Test-only ancestry; image digests remain the currently installed tested images.
        AcceptanceHost.fail = failure
        AcceptanceHost.crash = crash
        release = Release.parse(json.dumps(data))
        try:
            Controller(config, controller_root, AcceptanceHost).deploy(
                release, TestAPI()
            )
        except KeyboardInterrupt:
            AcceptanceHost.crash = None
            Controller(config, controller_root, AcceptanceHost).deploy(
                release, TestAPI()
            )
        except Exception:
            if operation not in {"before", "after"} and failure is None:
                raise
            assert (
                json.loads((controller_root / "state.json").read_text())["phase"]
                == "failed"
            )
            evidence.append({"case": name, "result": "source-restored"})
            atomic_json(ROOT / "evidence.json", {"cases": evidence})
            return
        assert operation not in {"before", "after"} and failure is None
        (ROOT / "current-source").write_text(str(source))
        evidence.append({"case": name, "result": "deployed"})
        atomic_json(ROOT / "evidence.json", {"cases": evidence})

    parent = "20261112_0001"
    deploy("20990101_0001")
    parent = "20990101_0001"
    # Same head, code-only release and same SHA retry are exercised separately from revisions.
    current = Controller(config, controller_root, AcceptanceHost)
    current.deploy(Release.parse(json.dumps(current.state["current"])), TestAPI())
    rows = (
        run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "exec",
                "deployment/db",
                "--",
                "psql",
                "-U",
                "coreteams",
                "-d",
                "coreteams",
                "-At",
                "-c",
                "SELECT count(*),max(id) FROM deploy_acceptance_evidence",
            ]
        )
        .decode()
        .strip()
    )
    assert rows == "120001|120001"
    deploy("20990102_0001", "next", crash="commit")
    parent = "20990102_0001"
    deploy("20990103_0001", "before")
    deploy("20990104_0001", "after")
    # Candidate startup failure after live DDL, with the preceding successful schema as source.
    deploy("20990105_0001", "next", failure="startup")
    final = (
        run(
            [
                "kubectl",
                "-n",
                NAMESPACE,
                "exec",
                "deployment/db",
                "--",
                "psql",
                "-U",
                "coreteams",
                "-d",
                "coreteams",
                "-At",
                "-c",
                "SELECT count(*),max(id) FROM deploy_acceptance_evidence; SELECT version_num FROM alembic_version; SELECT to_regclass('failed_release_object')",
            ]
        )
        .decode()
        .strip()
    )
    assert final == "120001|120001\n20990102_0001"
    atomic_json(
        ROOT / "evidence.json",
        {
            "cases": evidence,
            "rows_and_revision": final,
            "same_sha_retry": "passed",
            "unknown_revisions": "passed",
            "after_commit_resume": "passed",
            "production_mutations": "none (read-only pg_dump and account smoke)",
        },
    )
    print("ISOLATED ACCEPTANCE PASSED")


if __name__ == "__main__":
    if sys.argv[1:] != ["--isolated"] or os.geteuid() != 0:
        raise SystemExit("Root --isolated is required")
    check()
