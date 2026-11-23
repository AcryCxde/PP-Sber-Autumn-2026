"""One-time root bootstrap. New application releases never execute this installer.

Install the reviewed tools package in /opt/coreteams-deploy/controller, then run:
python3 -m tools.cicd.install_controller
Uses the existing server-only GitHub token, backup encryption keys and smoke identities.
"""

import json
import os
import subprocess
from pathlib import Path

from tools.cicd.cold_backup import atomic_json
from tools.cicd.controller import STATE


def k(*args, data=None):
    return subprocess.check_output(["kubectl", "-n", "coreteams", *args], input=data)


def apply(value):
    k("apply", "-f", "-", data=json.dumps(value).encode())


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Root bootstrap required")
    os.umask(0o077)
    config_path = Path("/etc/coreteams-deploy/controller.json")
    if config_path.exists():
        raise RuntimeError(
            "Already installed: review configuration instead of overwriting"
        )
    old = json.loads(
        Path("/var/lib/coreteams-deploy/maintenance-state.json").read_text()
    )["state"]
    if old["status"] != "succeeded":
        raise RuntimeError("Legacy operation must be complete before bootstrap")
    volumes = {}
    for alias, claim in {
        "db": "postgres-data",
        "data": "coreteams-data",
        "home": "coreteams-home",
        "s3": "s3-data",
    }.items():
        pvc = json.loads(k("get", "pvc", claim, "-o", "json"))
        pv = json.loads(k("get", "pv", pvc["spec"]["volumeName"], "-o", "json"))
        volumes[alias] = (pv["spec"].get("hostPath") or pv["spec"].get("local"))["path"]
    apply(
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "coreteams-controller"},
        }
    )
    apply(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": "coreteams-controller"},
            "rules": [
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments", "deployments/scale"],
                    "resourceNames": ["backend", "frontend", "db", "s3"],
                    "verbs": ["get", "patch", "update"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["services"],
                    "resourceNames": ["frontend"],
                    "verbs": ["patch"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["services", "endpoints"],
                    "verbs": ["get"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods"],
                    "verbs": ["get", "list", "watch", "create", "patch", "delete"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["pods/exec"],
                    "verbs": ["create", "get"],
                },
                {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                {
                    "apiGroups": ["batch"],
                    "resources": ["jobs"],
                    "verbs": ["get", "list", "watch", "create", "patch", "delete"],
                },
                {
                    "apiGroups": ["networking.k8s.io"],
                    "resources": ["networkpolicies"],
                    "verbs": ["get", "list", "create", "patch", "delete"],
                },
            ],
        }
    )
    apply(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": "coreteams-controller"},
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": "coreteams-controller",
                    "namespace": "coreteams",
                }
            ],
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": "coreteams-controller",
            },
        }
    )
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "coreteams-controller-token",
                "annotations": {
                    "kubernetes.io/service-account.name": "coreteams-controller"
                },
            },
            "type": "kubernetes.io/service-account-token",
        }
    )
    import base64
    import time

    for _ in range(20):
        secret = json.loads(
            k("get", "secret", "coreteams-controller-token", "-o", "json")
        )
        if secret.get("data", {}).get("token"):
            break
        time.sleep(1)
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "coreteams",
                "cluster": {
                    "server": "https://127.0.0.1:6443",
                    "certificate-authority-data": secret["data"]["ca.crt"],
                },
            }
        ],
        "users": [
            {
                "name": "controller",
                "user": {"token": base64.b64decode(secret["data"]["token"]).decode()},
            }
        ],
        "contexts": [
            {
                "name": "controller",
                "context": {
                    "cluster": "coreteams",
                    "user": "controller",
                    "namespace": "coreteams",
                },
            }
        ],
        "current-context": "controller",
    }
    atomic_json(Path("/etc/coreteams-deploy/controller-kubeconfig.json"), kubeconfig)
    atomic_json(
        config_path,
        {
            "kubeconfig": "/etc/coreteams-deploy/controller-kubeconfig.json",
            "github_token": "/etc/coreteams-deploy/github-token",
            "backup_cert": "/etc/coreteams-deploy/rollout-backup.crt",
            "backup_key": "/etc/coreteams-deploy/rollout-backup.key",
            "smoke_identities": "/etc/coreteams-deploy/smoke-identities.json",
            "public_origin": "https://coreteams.2026.tochka-urfu.tech",
            "volumes": volumes,
            "initial_release": old["plan"]["release"],
            "max_backup_bytes": 20 * 1024**3,
            "retention": 5,
        },
    )
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(STATE / "request.json", old["plan"]["release"])
    launcher = Path("/usr/local/sbin/coreteams-deploy")
    Path("/opt/coreteams-deploy/previous-launcher").write_bytes(launcher.read_bytes())
    launcher.write_text("""#!/bin/sh
set -eu
[ "$#" -eq 0 ] || exit 64
umask 077
cd /
exec /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/root LANG=C.UTF-8 /usr/bin/python3 -I -S -B -c 'import sys;sys.path.insert(0,"/opt/coreteams-deploy/controller");from tools.cicd.controller import main;raise SystemExit(main())'
""")
    launcher.chmod(0o755)
    execute = Path("/usr/local/sbin/coreteams-deploy-execute")
    execute.write_text(
        launcher.read_text()
        .replace('[ "$#" -eq 0 ] || exit 64', "set -- --execute")
        .replace(
            "from tools.cicd.controller import main;",
            'sys.argv=["controller","--execute"];from tools.cicd.controller import main;',
        )
    )
    execute.chmod(0o700)
    Path("/etc/systemd/system/coreteams-deploy.service").write_text("""[Unit]
Description=CoreTeams verified release controller
After=k3s.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/coreteams-deploy-execute
TimeoutStartSec=3600
UMask=0077
WorkingDirectory=/
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
""")
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "coreteams-deploy.service"], check=True)
    print("Installed persistent controller; no workload or data changes")


if __name__ == "__main__":
    main()
