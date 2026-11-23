"""Installed, release-independent k3s controller. Receives only verified release JSON."""

import copy
import fcntl
import hashlib
from http.cookies import SimpleCookie
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tools.cicd.cold_backup import ColdBackup, atomic_json
from tools.cicd.release import Release, _json
from tools.cicd.verify_release import (
    GitHubAPI,
    PREFIX,
    commit_sha,
    require,
    verify_release,
)

CONFIG = Path("/etc/coreteams-deploy/controller.json")
STATE = Path("/var/lib/coreteams-deploy/controller")
ACTIVE = {
    "preparing",
    "closing",
    "snapshot",
    "backup_ready",
    "rehearsing",
    "rehearsed",
    "migrating",
    "starting",
    "opening",
    "restoring",
    "reopening_source",
}


def run(args, *, data=None, timeout=60):
    result = subprocess.run(
        args,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("Command failed: " + Path(args[0]).name)
    return result.stdout


def ancestor(api, old, new):
    if old == new:
        return True
    result = api.get(f"{PREFIX}/compare/{commit_sha(old)}...{commit_sha(new)}")
    return (
        result.get("status") == "ahead"
        and result.get("merge_base_commit", {}).get("sha") == old
    )


class Host:
    def __init__(self, config, directory):
        self.config, self.directory = config, directory
        self.kube = [
            "kubectl",
            "--kubeconfig=" + config["kubeconfig"],
            "-n",
            config.get("namespace", "coreteams"),
            "--request-timeout=30s",
        ]

    def k(self, *args, data=None, timeout=60):
        return run([*self.kube, *args], data=data, timeout=timeout)

    def get(self, kind, name=None):
        return json.loads(self.k("get", kind, *([name] if name else []), "-o", "json"))

    def apply(self, resource):
        self.k("apply", "-f", "-", data=json.dumps(resource).encode())

    def patch(self, kind, name, patch):
        self.k("patch", kind, name, "--type=merge", "-p", json.dumps(patch))

    def gate(self, opened):
        self.patch(
            "service",
            "frontend",
            {
                "spec": {
                    "selector": {
                        "app": "frontend" if opened else "coreteams-release-closed"
                    }
                }
            },
        )
        # Observe the endpoint controller in both directions before proceeding.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            subsets = self.get("endpoints", "frontend").get("subsets", [])
            ready = any(subset.get("addresses") for subset in subsets)
            if ready == opened:
                return
            time.sleep(1)
        raise RuntimeError("Traffic gate endpoint timeout")

    def scale(self, name, replicas):
        self.k("scale", "deployment/" + name, "--replicas=" + str(replicas))
        deadline = time.monotonic() + self.config.get("readiness_seconds", 180)
        while time.monotonic() < deadline:
            pods = self.get("pods")["items"]
            matching = [
                p for p in pods if p["metadata"].get("labels", {}).get("app") == name
            ]
            deployment = self.get("deployment", name)
            observed = deployment.get("status", {})
            if (
                replicas == 0
                and not matching
                and observed.get("observedGeneration", 0)
                >= deployment["metadata"]["generation"]
                and observed.get("replicas", 0) == 0
            ):
                return
            ready = (
                observed.get("observedGeneration", 0)
                >= deployment["metadata"]["generation"]
                and observed.get("updatedReplicas") == replicas
                and observed.get("readyReplicas") == replicas
            )
            if (
                replicas
                and ready
                and len(matching) == replicas
                and all(
                    not p["metadata"].get("deletionTimestamp")
                    and any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in p.get("status", {}).get("conditions", [])
                    )
                    for p in matching
                )
            ):
                return
            time.sleep(2)
        raise RuntimeError("Deployment readiness timeout: " + name)

    def stop(self):
        for name in ("frontend", "backend", "s3", "db"):
            self.scale(name, 0)

    def start_storage(self):
        for name in ("db", "s3"):
            self.scale(name, 1)

    def source(self):
        result = {}
        for name in ("backend", "frontend", "db", "s3"):
            d = self.get("deployment", name)
            require(
                d["spec"]["replicas"] == 1, "Expected healthy single-replica source"
            )
            result[name] = {"spec": d["spec"]}
        backend = result["backend"]["spec"]["template"]["spec"]["containers"][0]
        require(
            any(
                e.get("name") == "EXECUTOR" and e.get("value") == "cli"
                for e in backend["env"]
            ),
            "Only CLI writer drain is supported",
        )
        # Refuse unrelated writer pods before accepting backup scope.
        for pod in self.get("pods")["items"]:
            if pod["metadata"].get("labels", {}).get("app") not in {
                "backend",
                "frontend",
                "db",
                "s3",
            }:
                require(
                    pod["status"]["phase"] in {"Succeeded", "Failed"},
                    "Unexpected writer pod",
                )
        return result

    def assert_release(self, current):
        release = Release.parse(json.dumps(current))
        for name in ("backend", "frontend"):
            expected = (
                release.images[name].repository + "@" + release.images[name].digest
            )
            actual = self.get("deployment", name)["spec"]["template"]["spec"][
                "containers"
            ][0]["image"]
            require(actual == expected, "Installed images differ from recorded release")

    def image(self, name):
        pods = [
            p
            for p in self.get("pods")["items"]
            if p["metadata"].get("labels", {}).get("app") == name
        ]
        require(len(pods) == 1, "Expected one image identity")
        image = pods[0]["status"]["containerStatuses"][0]["imageID"].removeprefix(
            "docker-pullable://"
        )
        require("@sha256:" in image, "Missing immutable image identity")
        return image

    def source_start(self, source):
        for name in ("backend", "frontend", "db", "s3"):
            spec = copy.deepcopy(source[name]["spec"])
            spec["replicas"] = 0
            self.k(
                "patch",
                "deployment",
                name,
                "--type=json",
                "-p",
                json.dumps([{"op": "replace", "path": "/spec", "value": spec}]),
            )
        self.start_storage()
        self.scale("backend", 1)
        self.scale("frontend", 1)
        self.smoke(public=False)

    def candidate_start(self, release):
        for name in ("backend", "frontend"):
            current = self.get("deployment", name)["spec"]["template"]["spec"][
                "containers"
            ][0]
            current["image"] = (
                release.images[name].repository + "@" + release.images[name].digest
            )
            if name == "backend":
                current["env"] = [
                    e
                    for e in current.get("env", [])
                    if e["name"] != "DATABASE_MIGRATIONS"
                ]
                current["env"].append(
                    {"name": "DATABASE_MIGRATIONS", "value": "verify"}
                )
            self.patch(
                "deployment",
                name,
                {
                    "spec": {
                        "template": {
                            "metadata": {
                                "annotations": {
                                    "coreteams/release-sha": release.context.sha
                                }
                            },
                            "spec": {"containers": [current]},
                        }
                    }
                },
            )
            self.scale(name, 1)

    def pod_ip(self, app):
        pods = self.get("pods")["items"]
        selected = [
            p
            for p in pods
            if p["metadata"].get("labels", {}).get("app") == app
            and p.get("status", {}).get("phase") == "Running"
            and not p["metadata"].get("deletionTimestamp")
        ]
        require(len(selected) == 1, "Expected one running pod")
        return selected[0]["status"]["podIP"]

    def smoke(self, public=False):
        backend = (
            self.config["public_origin"]
            if public
            else "http://" + self.pod_ip("backend") + ":8000"
        )
        frontend = backend if public else "http://" + self.pod_ip("frontend")
        # Existing dedicated accounts; routine sign-in avoids expiring fixed smoke cookies.
        identities = json.loads(Path(self.config["smoke_identities"]).read_text())
        sessions = {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for role in ("admin", "user"):
            identity = identities[role]
            request = urllib.request.Request(
                backend + "/api/auth/login",
                data=json.dumps(
                    {"email": identity["email"], "password": identity["password"]}
                ).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(request, timeout=15) as response:
                require(response.status == 200, "Smoke account sign-in failed")
                account = json.loads(response.read(65536))
                require(
                    account["id"] == identity["id"] and account["role"] == role,
                    "Smoke account role/identity changed",
                )
                cookies = SimpleCookie()
                for header in response.headers.get_all("Set-Cookie", []):
                    cookies.load(header)
                sessions[role + "_session"] = cookies["coreteams_session"].value
        overview = "/api/admin/v1/overview?active_page=2147483647&attention_page=2147483647&page_size=1"
        checks = [
            (frontend, "/", 200, None),
            (frontend, "/product/", 200, None),
            (backend, "/api/auth/me", 401, None),
            (backend, overview, 401, None),
            (backend, overview, 403, sessions["user_session"]),
            (backend, overview, 200, sessions["admin_session"]),
            (
                backend,
                "/api/admin/v1/users?page=2147483647&page_size=1",
                200,
                sessions["admin_session"],
            ),
            (backend, "/api/scenarios", 200, sessions["user_session"]),
        ]
        for origin, path, expected, cookie in checks:
            headers = {"Cookie": "coreteams_session=" + cookie} if cookie else {}
            request = urllib.request.Request(origin + path, headers=headers)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                response = opener.open(request, timeout=15)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                require(response.status == expected, "HTTP auth smoke failed")
                body = response.read(1024 * 1024 + 1)
                require(len(body) <= 1024 * 1024, "HTTP smoke response too large")
                if expected == 200 and path.startswith("/api/"):
                    require(
                        isinstance(json.loads(body), (dict, list)), "Invalid API JSON"
                    )

    def db_exec(self, args, *, clone=False):
        target = "pod/coreteams-release-db" if clone else "deployment/db"
        return self.k("exec", target, "--", *args, timeout=180)

    def schema(self, *, clone=False):
        body = self.db_exec(
            [
                "pg_dump",
                "-U",
                "coreteams",
                "-d",
                "coreteams",
                "--schema-only",
                "--no-owner",
                "--no-privileges",
            ],
            clone=clone,
        )
        lines = [
            line
            for line in body.splitlines()
            if not line.startswith((b"\\restrict ", b"\\unrestrict ", b"--"))
        ]
        return hashlib.sha256(b"\n".join(lines)).hexdigest()

    def rehearsal(self, state, backup):
        # Same physical server build and PGDATA, isolated from production network.
        self.cleanup()
        template = copy.deepcopy(state["source"]["db"]["spec"]["template"]["spec"])
        template["automountServiceAccountToken"] = False
        template["restartPolicy"] = "Never"
        template["volumes"] = [
            {
                "name": "data",
                "hostPath": {
                    "path": str(backup / Path(self.config["volumes"]["db"]).name),
                    "type": "Directory",
                },
            }
        ]
        template["containers"][0]["image"] = state["db_image"]
        template["containers"][0]["readinessProbe"]["exec"]["command"] = [
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "coreteams",
            "-d",
            "coreteams",
        ]
        self.apply(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "coreteams-release-database"},
                "spec": {
                    "podSelector": {"matchLabels": {"coreteams-release": "database"}},
                    "policyTypes": ["Ingress", "Egress"],
                    "egress": [],
                    "ingress": [
                        {
                            "from": [
                                {
                                    "podSelector": {
                                        "matchLabels": {
                                            "coreteams-release": "rehearsal"
                                        }
                                    }
                                }
                            ],
                            "ports": [{"protocol": "TCP", "port": 5432}],
                        }
                    ],
                },
            }
        )
        self.apply(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "coreteams-release-db",
                    "labels": {"coreteams-release": "database"},
                },
                "spec": template,
            }
        )
        self.k(
            "wait",
            "--for=condition=Ready",
            "pod/coreteams-release-db",
            "--timeout=180s",
            timeout=190,
        )
        ip = self.get("pod", "coreteams-release-db")["status"]["podIP"]
        # DATABASE_URL stays secretKeyRef; replace only host inside the container.
        self.job("plan", state, clone_ip=ip)
        self.job("upgrade", state, clone_ip=ip)
        self.job("check-models", state, clone_ip=ip)
        return self.schema(clone=True)

    def job(self, mode, state, clone_ip=None):
        name = "coreteams-release-" + ("rehearsal-" if clone_ip else "live-") + mode
        selector = {"coreteams-release": "database"} if clone_ip else {"app": "db"}
        label = "rehearsal" if clone_ip else "live"
        self.apply(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "coreteams-release-" + label},
                "spec": {
                    "podSelector": {"matchLabels": {"coreteams-release": label}},
                    "policyTypes": ["Ingress", "Egress"],
                    "ingress": [],
                    "egress": [
                        {
                            "to": [{"podSelector": {"matchLabels": selector}}],
                            "ports": [{"protocol": "TCP", "port": 5432}],
                        }
                    ],
                },
            }
        )
        # No retry of an existing Job whose COMMIT outcome might be unknown.
        listing = self.get("jobs")["items"]
        existing = next((j for j in listing if j["metadata"]["name"] == name), None)
        if existing:
            require(
                existing["metadata"]["annotations"]["coreteams/attempt"]
                == state["attempt"],
                "Job attempt mismatch",
            )
        else:
            release = Release.parse(json.dumps(state["release"]))
            command = ["python", "-m", "backend.migrate", mode]
            env = [
                {
                    "name": "RELEASE_MANIFEST",
                    "value": json.dumps(state["release"], separators=(",", ":")),
                },
                {
                    "name": "DATABASE_URL",
                    "valueFrom": {
                        "secretKeyRef": {"name": "postgres", "key": "DATABASE_URL"}
                    },
                },
            ]
            if clone_ip:
                command = [
                    "python",
                    "-c",
                    "import os,sys;from sqlalchemy.engine import make_url;"
                    'u=make_url(os.environ["DATABASE_URL"]);'
                    'os.environ["DATABASE_URL"]=u.set(host=sys.argv[1],port=5432).render_as_string(hide_password=False);'
                    'sys.argv=["backend.migrate",sys.argv[2]];'
                    "from backend.migrate import main;main()",
                    clone_ip,
                    mode,
                ]
            else:
                # DNS is deliberately unnecessary with default-deny egress.
                ip = self.get("service", "db")["spec"]["clusterIP"]
                command = [
                    "python",
                    "-c",
                    "import os,sys;from sqlalchemy.engine import make_url;"
                    'u=make_url(os.environ["DATABASE_URL"]);'
                    'os.environ["DATABASE_URL"]=u.set(host=sys.argv[1],port=5432).render_as_string(hide_password=False);'
                    'sys.argv=["backend.migrate",sys.argv[2]];'
                    "from backend.migrate import main;main()",
                    ip,
                    mode,
                ]
            self.apply(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {
                        "name": name,
                        "labels": {"coreteams-release": "job"},
                        "annotations": {"coreteams/attempt": state["attempt"]},
                    },
                    "spec": {
                        "backoffLimit": 0,
                        "activeDeadlineSeconds": 300,
                        "template": {
                            "metadata": {"labels": {"coreteams-release": label}},
                            "spec": {
                                "restartPolicy": "Never",
                                "automountServiceAccountToken": False,
                                "securityContext": {
                                    "runAsUser": 1000,
                                    "runAsNonRoot": True,
                                },
                                "imagePullSecrets": state["source"]["backend"]["spec"][
                                    "template"
                                ]["spec"].get("imagePullSecrets", []),
                                "containers": [
                                    {
                                        "name": "migration",
                                        "image": release.backend.repository
                                        + "@"
                                        + release.backend.digest,
                                        "command": command,
                                        "env": env,
                                        "resources": {
                                            "requests": {
                                                "cpu": "100m",
                                                "memory": "256Mi",
                                            },
                                            "limits": {"cpu": "1", "memory": "768Mi"},
                                        },
                                        "securityContext": {
                                            "allowPrivilegeEscalation": False,
                                            "capabilities": {"drop": ["ALL"]},
                                        },
                                    }
                                ],
                            },
                        },
                    },
                }
            )
        deadline = time.monotonic() + 360
        while time.monotonic() < deadline:
            job = self.get("job", name)
            conditions = job.get("status", {}).get("conditions", [])
            completed = any(
                c["type"] == "Complete" and c["status"] == "True" for c in conditions
            )
            failed = any(
                c["type"] == "Failed" and c["status"] == "True" for c in conditions
            )
            if completed or failed:
                log = self.k("logs", "job/" + name, "--all-containers=true")
                log_path = self.directory / (name + ".log")
                log_path.write_bytes(log)
                os.chmod(log_path, 0o600)
                require(completed, "Migration job failed: " + mode)
                return
            time.sleep(2)
        raise RuntimeError("Migration Job timeout")

    def cleanup(self):
        self.k(
            "delete",
            "jobs",
            "-l",
            "coreteams-release=job",
            "--ignore-not-found=true",
            "--wait=true",
            timeout=90,
        )
        self.k(
            "delete",
            "pod",
            "coreteams-release-db",
            "--ignore-not-found=true",
            "--wait=true",
            timeout=90,
        )


class Controller:
    def __init__(self, config, root=STATE, host_class=Host):
        self.config, self.root, self.host_class = config, root, host_class
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.json"
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"phase": "idle", "current": config["initial_release"]}
        )

    def save(self, phase, **fields):
        self.state.update(fields)
        self.state.update(phase=phase, updated_at=time.time())
        atomic_json(self.path, self.state)
        print("release phase: " + phase, flush=True)

    def deploy(self, release, api):
        with (self.root / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self.locked(release, api)

    def locked(self, release, api):
        s = self.state
        # Finish/reconcile a pinned operation before admitting any newer target.
        if s["phase"] in ACTIVE:
            pinned = Release.parse(json.dumps(s["release"]))
            try:
                self.execute(pinned)
            except Exception:
                # A completed recovery has proven the old release healthy. Admit the
                # requested retry directly, without requiring a second button press.
                if self.state["phase"] != "failed":
                    raise
            s = self.state
        current = s["current"]
        host = self.host_class(self.config, self.root)
        if release.context.sha == current["sha"]:
            host.assert_release(current)
            host.smoke(public=True)
            print("release already installed and healthy", flush=True)
            return
        if ancestor(api, release.context.sha, current["sha"]):
            print("release superseded by installed descendant", flush=True)
            return
        require(
            ancestor(api, current["sha"], release.context.sha),
            "Refused unrelated release/downgrade",
        )
        attempt = str(time.time_ns())
        directory = self.root / attempt
        directory.mkdir(mode=0o700)
        host = self.host_class(self.config, directory)
        host.assert_release(current)
        source = host.source()
        ColdBackup(self.config).preflight(self.root)
        available = int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        require(available >= 1024 * 1024, "Insufficient free memory before drain")
        host.smoke(public=True)
        db_image = host.image("db")
        source["db"]["spec"]["template"]["spec"]["containers"][0]["image"] = db_image
        self.save(
            "preparing",
            release=release.to_dict(),
            attempt=attempt,
            source=source,
            db_image=db_image,
            directory=str(directory),
            previous=current,
            error=None,
            backup_verified=False,
            schema=None,
        )
        self.execute(release)

    def execute(self, release):
        s = self.state
        directory = Path(s["directory"])
        host = self.host_class(self.config, directory)
        backup = ColdBackup(self.config)
        try:
            phase = s["phase"]
            if phase in {"restoring", "reopening_source"}:
                self.rollback(host, backup, directory)
                raise RuntimeError("Interrupted release restored; retry deployment")
            if phase in {"preparing", "closing", "snapshot"}:
                self.save("closing")
                host.gate(False)
                host.stop()
                self.save("snapshot")
                readback = backup.create(directory)
                self.save("backup_ready", backup_verified=True)
                phase = "backup_ready"
            if phase in {"backup_ready", "rehearsing"}:
                self.save("rehearsing")
                # Re-extract before any repetition; never migrate an uncertain clone twice.
                host.cleanup()
                readback = backup.decrypt(directory)
                schema = host.rehearsal(s, readback)
                self.save("rehearsed", schema=schema)
                phase = "rehearsed"
            if phase in {"rehearsed", "migrating"}:
                host.start_storage()
                self.save("migrating")
                host.job("upgrade", s)
                host.job("verify", s)
                require(
                    host.schema() == s["schema"],
                    "Live schema differs from successful rehearsal",
                )
                self.save("starting")
                phase = "starting"
            if phase == "starting":
                host.candidate_start(release)
                host.smoke(public=False)
                host.job("record", s)
                host.cleanup()
                # Durable boundary: after this point user writes may have occurred.
                self.save("opening")
                phase = "opening"
            if phase == "opening":
                host.smoke(public=False)
                host.gate(True)
                host.smoke(public=True)
                self.save("succeeded", current=release.to_dict(), error=None)
                try:
                    self.prune_readback(directory)
                    self.retention()
                except OSError:
                    print(
                        "release succeeded; backup cleanup will be retried", flush=True
                    )
        except Exception as error:
            if s["phase"] == "opening":
                self.save("opening", error=type(error).__name__)
                # Never restore an old snapshot after traffic might have opened.
                raise
            if s["phase"] not in {
                "restoring",
                "reopening_source",
                "failed",
                "succeeded",
            }:
                self.save("restoring", error=type(error).__name__)
                self.rollback(host, backup, directory)
            raise

    def rollback(self, host, backup, directory):
        if self.state["phase"] != "reopening_source":
            host.gate(False)
            host.stop()
            host.cleanup()
            if self.state.get("backup_verified"):
                backup.restore(directory)
            host.source_start(self.state["source"])
            self.save("reopening_source")
        host.gate(True)
        host.smoke(public=True)
        self.save("failed", current=self.state["previous"])
        atomic_json(directory / "result.json", self.state)
        self.prune_readback(directory)

    @staticmethod
    def prune_readback(directory):
        if (directory / "readback").exists():
            shutil.rmtree(directory / "readback")

    def retention(self):
        attempts = sorted(
            (p for p in self.root.iterdir() if p.is_dir() and p.name.isdecimal()),
            key=lambda p: int(p.name),
        )
        for directory in attempts[: -self.config.get("retention", 5)]:
            shutil.rmtree(directory)


def submit(config):
    raw = sys.stdin.buffer.read(16385)
    require(len(raw) <= 16384, "Request too large")
    request = _json(raw.decode())
    require(
        isinstance(request, dict)
        and set(request) == {"schema", "release"}
        and request["schema"] == 2,
        "Invalid deploy envelope",
    )
    supplied = Release.parse(json.dumps(request["release"]))
    token = Path(config["github_token"]).read_text().strip()
    api = GitHubAPI(token)
    verified = verify_release(
        str(supplied.context.workflow_run_id),
        supplied.context.sha,
        api=api,
        allow_master_descendant=True,
    )
    require(
        verified == supplied, "Release differs from independently verified artifact"
    )
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "submit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        deadline = time.monotonic() + 3600
        while run(
            [
                "systemctl",
                "show",
                "coreteams-deploy.service",
                "-p",
                "ActiveState",
                "--value",
            ]
        ).decode().strip() in {"activating", "active", "reloading", "deactivating"}:
            require(time.monotonic() < deadline, "Controller busy; retry from GitHub")
            time.sleep(3)
        atomic_json(STATE / "request.json", supplied.to_dict())
        # A systemd service survives SSH disconnects. Candidate code never runs on the host.
        run(["systemctl", "reset-failed", "coreteams-deploy.service"])
        with subprocess.Popen(
            ["systemctl", "start", "coreteams-deploy.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as waiter:
            last_phase = None
            while waiter.poll() is None:
                if time.monotonic() >= deadline:
                    waiter.kill()  # Only the waiter; systemd keeps the controller running.
                    raise RuntimeError(
                        "Controller still running; retry checks existing operation"
                    )
                path = STATE / "state.json"
                phase = (
                    json.loads(path.read_text())["phase"] if path.exists() else "queued"
                )
                if phase != last_phase:
                    print("release phase: " + phase, flush=True)
                    last_phase = phase
                time.sleep(3)
            require(
                waiter.returncode == 0,
                "Server release failed; inspect controller journal",
            )
        print("release installed or superseded successfully")


def main():
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, "Installed controller requires root")
        config = json.loads(CONFIG.read_text())
        if sys.argv[1:] == ["--execute"]:
            release = Release.parse((STATE / "request.json").read_text())
            # Already authenticated and pinned by submit. No freshness recheck mid-operation.
            api = GitHubAPI(Path(config["github_token"]).read_text().strip())
            Controller(config).deploy(release, api)
        else:
            require(not sys.argv[1:], "No remote command arguments accepted")
            submit(config)
    except Exception as error:
        print("deployment failed: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
