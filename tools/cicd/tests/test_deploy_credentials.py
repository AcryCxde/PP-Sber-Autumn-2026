"""Production bootstrap contracts; fake API/process only, no network."""

import base64
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.cicd import deploy_credentials as credentials
from tools.cicd import deploy_server, schema_probe


def kubeconfig():
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "coreteams",
                "cluster": {
                    "server": "https://127.0.0.1:6443",
                    "certificate-authority-data": base64.b64encode(
                        b"reviewed CA"
                    ).decode(),
                },
            }
        ],
        "users": [{"name": "coreteams-deploy", "user": {"token": "scoped-token"}}],
        "contexts": [
            {
                "name": "coreteams-deploy",
                "context": {
                    "cluster": "coreteams",
                    "user": "coreteams-deploy",
                    "namespace": "coreteams",
                },
            }
        ],
        "current-context": "coreteams-deploy",
    }


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))


@pytest.fixture
def protected(monkeypatch):
    files = {
        credentials.TOKEN: b"server-token\n",
        credentials.KUBECONFIG: json.dumps(kubeconfig()).encode(),
    }
    reads = []

    def read(path, *, exact_mode=False):
        assert exact_mode
        reads.append(path)
        return files[path]

    monkeypatch.setattr(credentials, "protected_credential_read", read)
    return files, reads


def test_bootstrap_ignores_caller_credentials(monkeypatch, protected):
    monkeypatch.setenv("GH_TOKEN", "caller-secret")
    monkeypatch.setenv("KUBECONFIG", "/root/.kube/config")
    monkeypatch.setenv("HTTPS_PROXY", "https://caller.invalid")
    calls = []
    monkeypatch.setattr(
        credentials, "GitHubAPI", lambda *, token: calls.append(token) or "api"
    )

    def main(**kwargs):
        assert kwargs["api"] == "api"
        assert callable(kwargs["command"])
        return 0

    monkeypatch.setattr(deploy_server, "main", main)
    assert credentials.main() == 0
    assert calls == ["server-token"]
    assert protected[1] == [credentials.TOKEN, credentials.KUBECONFIG]


@pytest.mark.parametrize("bad", [b"", b"\xff", b"a b", b"a\nb", b"x" * 16385])
def test_bad_token_refuses_before_api(monkeypatch, protected, capsys, bad):
    protected[0][credentials.TOKEN] = bad
    monkeypatch.setattr(
        credentials, "GitHubAPI", lambda **kw: pytest.fail("API reached")
    )
    monkeypatch.setattr(
        deploy_server, "main", lambda **kw: pytest.fail("deploy reached")
    )
    assert credentials.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "deployment credential bootstrap refused\n"


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "malformed",
        "oversize",
        "exec",
        "auth-provider",
        "impersonate",
        "external-ca",
        "insecure",
        "server",
        "duplicate",
        "context",
    ],
)
def test_bad_kube_refuses_before_api(monkeypatch, protected, case, capsys):
    files = protected[0]
    value = kubeconfig()
    user = value["users"][0]["user"]
    cluster = value["clusters"][0]["cluster"]
    if case == "missing":
        del files[credentials.KUBECONFIG]
    elif case == "malformed":
        files[credentials.KUBECONFIG] = b"not json"
    elif case == "oversize":
        files[credentials.KUBECONFIG] = b" " * 16385
    elif case == "duplicate":
        files[credentials.KUBECONFIG] = b'{"kind":"Config","kind":"Config"}'
    else:
        if case in {"exec", "auth-provider", "impersonate"}:
            user[{"impersonate": "as"}.get(case, case)] = {"command": "/evil"}
        elif case == "external-ca":
            cluster["certificate-authority"] = "/root/.kube/ca"
        elif case == "insecure":
            cluster["insecure-skip-tls-verify"] = True
        elif case == "server":
            cluster["server"] = "https://evil.invalid"
        else:
            value["current-context"] = "root"
        files[credentials.KUBECONFIG] = json.dumps(value).encode()
    monkeypatch.setattr(
        credentials, "GitHubAPI", lambda **kw: pytest.fail("API reached")
    )
    monkeypatch.setattr(
        deploy_server, "main", lambda **kw: pytest.fail("deploy reached")
    )
    assert credentials.main() == 1
    assert capsys.readouterr().err == "deployment credential bootstrap refused\n"


def test_scoped_kubectl_exact_boundary(monkeypatch):
    calls = []

    class Child:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, *, timeout):
            assert timeout == 200
            return "ok", "secret stderr"

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return Child()

    monkeypatch.setattr(deploy_server.subprocess, "Popen", popen)
    monkeypatch.setenv("KUBECONFIG", "/root/.kube/config")
    monkeypatch.setenv("HTTPS_PROXY", "caller")
    assert (
        credentials.scoped_kubectl(["get", "deployment", "backend", "-o", "json"])
        == "ok"
    )
    args, kwargs = calls[0]
    assert args == [
        "/usr/local/bin/kubectl",
        "--request-timeout=20s",
        "-n",
        "coreteams",
        "--kubeconfig=/etc/coreteams-deploy/kubeconfig.json",
        "--context=coreteams-deploy",
        "get",
        "deployment",
        "backend",
        "-o",
        "json",
    ]
    assert kwargs["env"] == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/var/empty",
        "KUBECONFIG": str(credentials.KUBECONFIG),
    }


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "symlink",
        "parent-link",
        "writable-parent",
        "foreign-parent",
        "foreign-file",
        "directory",
        "fifo",
        "readable",
        "executable",
        "special",
        "empty",
        "oversize",
    ],
)
def test_protected_credential_files(monkeypatch, case):
    """Simulate descriptor metadata, retaining the real protected walk/read code."""
    content = b"secret"
    parent_mode = stat.S_IFDIR | 0o755
    file_mode = stat.S_IFREG | 0o600
    if case == "writable-parent":
        parent_mode |= 0o020
    if case == "readable":
        file_mode |= 0o040
    if case == "executable":
        file_mode |= 0o100
    if case == "special":
        file_mode |= stat.S_ISUID
    if case == "directory":
        file_mode = stat.S_IFDIR | 0o600
    if case == "fifo":
        file_mode = stat.S_IFIFO | 0o600
    if case == "empty":
        content = b""
    if case == "oversize":
        content = b"x" * 16385
    opened = []

    def open_file(path, flags, *, dir_fd=None):
        assert flags & os.O_NOFOLLOW
        opened.append(path)
        if case == "missing" and path == "github-token":
            raise FileNotFoundError
        if (case == "symlink" and path == "github-token") or (
            case == "parent-link" and path == "coreteams-deploy"
        ):
            raise OSError("symlink refused")
        return 4 if path == "github-token" else 3

    def metadata(fd):
        return SimpleNamespace(
            st_uid=int(
                (case == "foreign-file" and fd == 4)
                or (case == "foreign-parent" and fd == 3)
            ),
            st_mode=file_mode if fd == 4 else parent_mode,
            st_size=len(content),
        )

    import io

    monkeypatch.setattr(schema_probe.os, "open", open_file)
    monkeypatch.setattr(schema_probe.os, "fstat", metadata)
    monkeypatch.setattr(schema_probe.os, "close", lambda fd: None)
    monkeypatch.setattr(schema_probe.os, "fdopen", lambda *a, **kw: io.BytesIO(content))
    with pytest.raises((ValueError, OSError)):
        schema_probe.protected_credential_read(
            Path("/etc/coreteams-deploy/github-token"), exact_mode=True
        )
    assert opened


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_protected_credential_accepts_exact_modes(monkeypatch, mode):
    import io

    monkeypatch.setattr(
        schema_probe.os,
        "open",
        lambda path, *a, **kw: 4 if path == "github-token" else 3,
    )
    monkeypatch.setattr(schema_probe.os, "close", lambda fd: None)
    monkeypatch.setattr(
        schema_probe.os,
        "fstat",
        lambda fd: SimpleNamespace(
            st_uid=0,
            st_mode=(stat.S_IFREG | mode) if fd == 4 else (stat.S_IFDIR | 0o755),
            st_size=6,
        ),
    )
    monkeypatch.setattr(
        schema_probe.os, "fdopen", lambda *a, **kw: io.BytesIO(b"secret")
    )
    assert (
        schema_probe.protected_credential_read(credentials.TOKEN, exact_mode=True)
        == b"secret"
    )
