"""Explicit production credential bootstrap; installation remains inert until reviewed."""

import base64
import binascii
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from tools.cicd import deploy_server
from tools.cicd.release import _json, _require
from tools.cicd.schema_probe import protected_credential_read
from tools.cicd.verify_release import GitHubAPI

TOKEN = Path("/etc/coreteams-deploy/github-token")
KUBECONFIG = Path("/etc/coreteams-deploy/kubeconfig.json")
CONTEXT = "coreteams-deploy"


def _token(raw: bytes) -> str:
    _require(0 < len(raw) <= 16384, "invalid credential")
    value = raw.decode("ascii").removesuffix("\n")
    _require(re.fullmatch(r"[A-Za-z0-9_.-]+", value) is not None, "invalid credential")
    return value


def _validate_kubeconfig(raw: bytes) -> None:
    """One static local k3s identity; no plugins, paths, proxies or impersonation."""
    _require(0 < len(raw) <= 16384, "invalid kube credential")
    value = _json(raw.decode("utf-8"))
    # Extract only the two credential-bearing fields, then compare the whole shape.
    ca = value["clusters"][0]["cluster"]["certificate-authority-data"]
    token = value["users"][0]["user"]["token"]
    _require(isinstance(ca, str) and bool(ca), "invalid kube CA")
    _require(bool(base64.b64decode(ca, validate=True)), "invalid kube CA")
    _require(isinstance(token, str), "invalid kube token")
    _token(token.encode("ascii"))
    _require("\n" not in token, "invalid kube token")
    expected = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "coreteams",
                "cluster": {
                    "server": "https://127.0.0.1:6443",
                    "certificate-authority-data": ca,
                },
            }
        ],
        "users": [{"name": CONTEXT, "user": {"token": token}}],
        "contexts": [
            {
                "name": CONTEXT,
                "context": {
                    "cluster": "coreteams",
                    "user": CONTEXT,
                    "namespace": "coreteams",
                },
            }
        ],
        "current-context": CONTEXT,
    }
    _require(value == expected, "unsupported kube credential")


def scoped_kubectl(args: Sequence[str]) -> str:
    return deploy_server.kubectl(
        [f"--kubeconfig={KUBECONFIG}", f"--context={CONTEXT}", *args],
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/var/empty",
            "KUBECONFIG": str(KUBECONFIG),
        },
    )


def main() -> int:
    try:
        token = _token(protected_credential_read(TOKEN, exact_mode=True))
        _validate_kubeconfig(protected_credential_read(KUBECONFIG, exact_mode=True))
        # The supported launcher is already env -i. Also discard inherited proxy,
        # CA and credential variables if this entrypoint is invoked directly.
        os.environ.clear()
        return deploy_server.main(api=GitHubAPI(token=token), command=scoped_kubectl)
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        IndexError,
        UnicodeError,
        binascii.Error,
    ):
        print("deployment credential bootstrap refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
