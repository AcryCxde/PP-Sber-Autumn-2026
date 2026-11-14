import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


class StrictLoader(yaml.SafeLoader):
    pass


StrictLoader.yaml_implicit_resolvers = {
    key: [(tag, regex) for tag, regex in values if tag != "tag:yaml.org,2002:bool"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor("tag:yaml.org,2002:map", unique_mapping)
ROOT = Path(__file__).resolve().parents[3]
HEAVY = {"build", "python", "frontend", "web-product", "compose", "schema-postgres"}


def test_loader_contract():
    assert yaml.load("on: push", Loader=StrictLoader) == {"on": "push"}
    with pytest.raises(ValueError, match="duplicate"):
        yaml.load("a: 1\na: 2", Loader=StrictLoader)


def test_workflow_contract():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    workflow = yaml.load(text, Loader=StrictLoader)
    assert set(workflow["on"]) == {"pull_request", "push", "workflow_dispatch"}
    for trigger in workflow["on"].values():
        assert not trigger or not ({"paths", "paths-ignore"} & trigger.keys())
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in text
    assert text.count("secrets.") == 1  # Owner metadata secret: publish step only.
    jobs = workflow["jobs"]
    assert set(jobs) == HEAVY | {"classify", "gate", "publish"}
    assert "if" not in jobs["classify"]
    classifier = jobs["classify"]
    assert (
        classifier["outputs"]["docs_only"] == "${{ steps.classify.outputs.docs_only }}"
    )
    steps = classifier["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0
    assert any(
        step.get("run")
        == "python -m pytest -c tools/cicd/pytest.ini tools/cicd/tests -q"
        for step in steps
    )
    config = (ROOT / "tools/cicd/pytest.ini").read_text()
    assert "filterwarnings = error" in config
    assert "testpaths = tests" in config
    step = next(step for step in steps if step.get("id") == "classify")
    assert (
        step["env"]["BASE_SHA"]
        == "${{ github.event.pull_request.base.sha || github.event.before }}"
    )
    assert step["env"]["HEAD_SHA"] == "${{ github.sha }}"
    assert step["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    for job in HEAVY:
        assert jobs[job]["needs"] == (
            "classify" if job in {"build", "schema-postgres"} else ["classify", "build"]
        )
        assert jobs[job]["if"] == "needs.classify.outputs.docs_only != 'true'"
    gate = jobs["gate"]
    assert gate["if"] == "${{ always() }}"
    assert set(gate["needs"]) == HEAVY | {"classify"}
    assert gate.get("name", "gate") == "gate"
    gate_step = gate["steps"][-1]
    assert gate_step["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}"
    assert "tools.cicd.gate" in gate_step["run"]
    for name, job in jobs.items():
        expected_permissions = {"contents": "read"}
        if name == "publish":
            expected_permissions["packages"] = "write"
        assert job.get("permissions", {"contents": "read"}) == expected_permissions
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", step["uses"])
            assert "${{" not in step.get("run", "")
            assert not re.search(r"\b(docker push|ssh|kubectl)\b", step.get("run", ""))


def test_postgres_suite_is_required_and_opted_in():
    jobs = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )["jobs"]
    job = jobs["schema-postgres"]
    assert job["timeout-minutes"] == 15
    assert job["needs"] == "classify"
    assert job["env"]["SCHEMA_PG16_TESTS"] == "1"
    checkout = next(s for s in job["steps"] if "checkout@" in s.get("uses", ""))
    # Baseline tests archive exact historical release commits, not just HEAD.
    assert checkout["with"]["fetch-depth"] == 0
    setup = next(s for s in job["steps"] if "setup-python@" in s.get("uses", ""))
    assert setup["with"]["python-version"] == "3.12"
    commands = "\n".join(s.get("run", "") for s in job["steps"])
    assert "pip install -r tools/cicd/requirements.txt '.[test]'" in commands
    from tools.cicd.tests.schema_pg_fixture import IMAGE

    assert f"docker pull {IMAGE}" in commands
    assert "python -m pytest -c tools/cicd/pytest.ini tools/cicd/tests -q" in commands
    assert "-o asyncio_default_fixture_loop_scope=function" in commands
    assert "continue-on-error" not in str(job)
    assert "schema-postgres" in jobs["gate"]["needs"]
    assert "schema-postgres" in jobs["publish"]["needs"]
    assert "SCHEMA_PG16_TESTS" not in str(jobs["classify"])


def test_publish_is_downstream_disabled_and_trusted_only():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )
    jobs = workflow["jobs"]
    publish = jobs["publish"]
    assert set(publish["needs"]) == HEAVY | {"classify", "gate"}
    assert "publish" not in jobs["gate"]["needs"]
    condition = publish["if"]
    for clause in (
        "success()",
        "github.event_name == 'push'",
        "github.ref == 'refs/heads/master'",
        "needs.classify.outputs.docs_only == 'false'",
        "vars.CICD_PUBLISH_ENABLED == 'true'",
    ):
        assert clause in condition
    assert "||" not in condition
    steps = publish["steps"]
    download = next(
        step for step in steps if "download-artifact@" in step.get("uses", "")
    )
    assert download["with"] == {"name": "checked-images", "path": "images"}
    upload = steps[-1]
    assert "if" not in upload  # Default success(), never always().
    assert (
        upload["with"]["name"]
        == "release-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    assert upload["with"]["path"] == "release.json"
    assert upload["with"]["if-no-files-found"] == "error"
    command = next(step for step in steps if "run" in step)
    assert command["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}"
    assert "python -m tools.cicd.release" in command["run"]
    assert "--password-stdin" in command["run"]
    assert "trap 'rm -rf" in command["run"]
    assert "docker build" not in command["run"]
    for name, job in jobs.items():
        if name != "publish":
            assert "github.token" not in str(job)
            assert "secrets." not in str(job)


def test_deploy_is_manual_disabled_trusted_and_credential_isolated():
    text = (ROOT / ".github/workflows/deploy.yml").read_text()
    workflow = yaml.load(text, Loader=StrictLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {
        "release_run_id",
        "release_sha",
    }
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"] == {
        "group": "production-deploy",
        "cancel-in-progress": "false",
    }
    jobs = workflow["jobs"]
    assert set(jobs) == {"verify", "deploy"}
    verify = jobs["verify"]
    assert "environment" not in verify
    assert "secrets." not in str(verify)
    guard = verify["steps"][0]["run"]
    assert '"$DEPLOY_ENABLED" != true' in guard and "exit 1" in guard
    assert '"$GITHUB_REF" != refs/heads/master' in guard
    checkout = next(s for s in verify["steps"] if "checkout@" in s.get("uses", ""))
    assert checkout["with"] == {
        "repository": "AcryCxde/PP-Sber-Autumn-2026",
        "ref": "refs/heads/master",
        "persist-credentials": "false",
    }
    deploy = jobs["deploy"]
    assert deploy["needs"] == "verify" and deploy["environment"] == "production"
    assert "vars.CICD_DEPLOY_ENABLED == 'true'" in deploy["if"]
    assert all("uses" not in s for s in deploy["steps"])
    command = deploy["steps"][0]["run"]
    for required in (
        "StrictHostKeyChecking=yes",
        "UserKnownHostsFile=",
        "BatchMode=yes",
        "/usr/local/sbin/coreteams-deploy",
        "16383",
        "ssh -F /dev/null -T",
    ):
        assert required in command
    for forbidden in ("ssh-keyscan", "StrictHostKeyChecking=no", "docker", "kubectl"):
        assert forbidden not in text
    for job in jobs.values():
        for step in job["steps"]:
            assert "${{" not in step.get("run", "")
            if "uses" in step:
                assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", step["uses"])


@pytest.fixture
def deploy_shell(tmp_path):
    step = yaml.load(
        (ROOT / ".github/workflows/deploy.yml").read_text(), Loader=StrictLoader
    )["jobs"]["deploy"]["steps"][0]
    script = tmp_path / "deploy.sh"
    script.write_text(step["run"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "ssh.json"
    allocated = tmp_path / "allocated"
    # PATH has no real ssh or user tools; subprocess receives no inherited secrets.
    fake_ssh = bin_dir / "ssh"
    fake_ssh.write_text(
        f"#!{sys.executable} -I\n"
        "import json, os, pathlib, stat, sys\n"
        "args = sys.argv[1:]\n"
        "key = pathlib.Path(args[args.index('-i') + 1])\n"
        "hosts = key.parent / 'known_hosts'\n"
        "assert key.read_text() == 'dummy-key\\n'\n"
        "assert hosts.read_text() == 'dummy-host-key\\n'\n"
        "assert stat.S_IMODE(key.stat().st_mode) == 0o600\n"
        "assert stat.S_IMODE(hosts.stat().st_mode) == 0o600\n"
        "assert 'DEPLOY_KEY' not in os.environ\n"
        "assert 'DEPLOY_KNOWN_HOSTS' not in os.environ\n"
        "pathlib.Path(os.environ['RECORD']).write_text(\n"
        "    json.dumps({'argv': args, 'stdin': sys.stdin.read()}))\n"
    )
    fake_ssh.chmod(0o700)
    fake_mktemp = bin_dir / "mktemp"
    fake_mktemp.write_text(
        f"#!{sys.executable} -I\n"
        "import os, pathlib, tempfile\n"
        "pathlib.Path(os.environ['ALLOCATED']).touch()\n"
        "print(tempfile.mkdtemp(dir=os.environ['WORK']))\n"
    )
    fake_mktemp.chmod(0o700)
    (bin_dir / "rm").symlink_to("/bin/rm")
    payload = '{"schema":1,"sha":"' + "a" * 40 + '"}'

    def run(port, **overrides):
        env = {
            "PATH": str(bin_dir),
            "WORK": str(tmp_path),
            "RECORD": str(record),
            "ALLOCATED": str(allocated),
            "DEPLOY_HOST": "deploy.example.invalid",
            "DEPLOY_USER": "deploy_user",
            "DEPLOY_KEY": "dummy-key",
            "DEPLOY_KNOWN_HOSTS": "dummy-host-key",
            "RELEASE_JSON": payload,
        }
        if port is not None:
            env["DEPLOY_PORT"] = port
        env.update(overrides)
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-e", str(script)],
            check=False,
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result

    return run, record, allocated, payload, step


@pytest.mark.parametrize("port", ["1", "22", "2297", "65535"])
def test_deploy_port_valid(deploy_shell, port):
    run, record, allocated, payload, step = deploy_shell
    result = run(port)
    assert result.returncode == 0, result.stderr
    assert allocated.exists()
    captured = json.loads(record.read_text())
    key = next(record.parent.glob("tmp*/key"), None)
    assert key is None  # EXIT trap removed credential files.
    args = captured["argv"]
    directory = str(Path(args[args.index("-i") + 1]).parent)
    assert not Path(directory).exists()
    assert args == [
        "-F",
        "/dev/null",
        "-T",
        "-p",
        port,
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={directory}/known_hosts",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        f"{directory}/key",
        "deploy_user@deploy.example.invalid",
        "/usr/local/sbin/coreteams-deploy",
    ]
    assert captured["stdin"] == payload + "\n"
    assert step["env"]["DEPLOY_PORT"] == "${{ vars.CICD_DEPLOY_PORT }}"


@pytest.mark.parametrize(
    "port",
    [
        None,
        "",
        "0",
        "65536",
        "-1",
        "01",
        "00022",
        " 22",
        "22 ",
        "2 2",
        "22\n",
        "22\t",
        "+22",
        "２２",
        "٢٢",
        "22; exit 0",
        "$(touch injected)",
        "22`touch injected`",
        "22/1",
        "1+1",
        "9" * 10000,
        "18446744073709551617",
    ],
    ids=[
        "missing",
        "empty",
        "zero",
        "overflow",
        "negative",
        "leading-zero",
        "padded",
        "space-before",
        "space-after",
        "space-inside",
        "newline",
        "tab",
        "plus",
        "fullwidth",
        "arabic",
        "semicolon",
        "substitution",
        "backticks",
        "division",
        "arithmetic",
        "very-long",
        "integer-wrap",
    ],
)
def test_deploy_port_invalid_before_credentials(deploy_shell, port):
    run, record, allocated, _, _ = deploy_shell
    result = run(port)
    assert result.returncode != 0
    assert not record.exists()
    assert not allocated.exists()
    assert not (record.parent / "injected").exists()


@pytest.mark.parametrize(
    "overrides",
    [{"DEPLOY_HOST": "-oProxyCommand=bad"}, {"DEPLOY_USER": "root;bad"}],
)
def test_deploy_preserves_host_user_validation(deploy_shell, overrides):
    run, record, allocated, _, _ = deploy_shell
    result = run("2297", **overrides)
    assert result.returncode != 0
    assert not record.exists()
    assert not allocated.exists()


def test_compose_render_preserved():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )
    command = workflow["jobs"]["compose"]["steps"][-1]["run"]
    assert "cp .env.gateway.example .env.gateway" in command
    assert "trap 'rm -f .env.gateway' EXIT" in command
    assert (
        "POSTGRES_PASSWORD=ci-only S3_ACCESS_KEY=ci-only S3_SECRET_KEY=ci-only docker compose config"
        in command
    )


def test_package_preflight_is_isolated_read_only_static():
    workflow = yaml.load(
        (ROOT / ".github/workflows/package-metadata-preflight.yml").read_text(),
        Loader=StrictLoader,
    )
    assert workflow["on"] == {"workflow_dispatch": None}
    assert workflow["permissions"] == {}
    assert set(workflow["jobs"]) == {"metadata"}
    job = workflow["jobs"]["metadata"]
    assert "github.ref == 'refs/heads/master'" in job["if"]
    assert "github.repository == 'AcryCxde/PP-Sber-Autumn-2026'" in job["if"]
    assert job["timeout-minutes"] == 3
    assert len(job["steps"]) == 1
    step = job["steps"][0]
    assert "uses" not in step
    assert step["env"] == {
        "GHCR_METADATA_OWNER_TOKEN": "${{ secrets.CICD_GHCR_METADATA_OWNER_READ_TOKEN }}"
    }
    command = step["run"]
    assert "python3 -I" in command
    assert "${{" not in command
    assert "https://api.github.com/user/packages/container/" in command
    assert "timeout=20" in command
    assert "response.read(65537)" in command
    assert 'data.get("visibility") == "private"' in command
    for forbidden in (
        "checkout",
        "docker",
        "subprocess",
        "upload-artifact",
        "urlopen",
    ):
        assert forbidden not in str(step)
    # Verify embedded static Python syntax without executing network calls.
    compile(command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0], "preflight", "exec")


def test_publish_metadata_secret_is_step_scoped_and_never_docker_password():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )
    publish = workflow["jobs"]["publish"]
    assert "env" not in workflow and "env" not in publish
    step = next(s for s in publish["steps"] if "run" in s)
    assert step["env"]["GHCR_METADATA_OWNER_TOKEN"] == (
        "${{ secrets.CICD_GHCR_METADATA_OWNER_READ_TOKEN }}"
    )
    assert step["env"]["GHCR_TOKEN"] == "${{ github.token }}"
    assert (
        "printf '%s' \"$GHCR_TOKEN\" | env -u GHCR_METADATA_OWNER_TOKEN docker login"
        in step["run"]
    )
    assert step["run"].count("GHCR_METADATA_OWNER_TOKEN") == 1
    assert "docker build" not in step["run"]
    assert all(
        "uses" not in s or "checkout@" not in s["uses"] for s in publish["steps"][4:]
    )


@pytest.mark.parametrize(
    "failure", [None, "credential", "403", "404", "network", "malformed", "owner"]
)
def test_static_preflight_owner_contract_offline(monkeypatch, capsys, failure):
    import urllib.error
    import urllib.request

    step = yaml.load(
        (ROOT / ".github/workflows/package-metadata-preflight.yml").read_text(),
        Loader=StrictLoader,
    )["jobs"]["metadata"]["steps"][0]
    source = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    monkeypatch.setenv(
        "GHCR_METADATA_OWNER_TOKEN", "" if failure == "credential" else "owner-only"
    )
    monkeypatch.setenv("GHCR_TOKEN", "oci-not-metadata")
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 65537
            if failure == "malformed":
                return b"owner-only invalid-json"
            return json.dumps(
                {
                    "name": calls[-1],
                    "package_type": "container",
                    "visibility": "private",
                    "owner": {"login": "other" if failure == "owner" else "AcryCxde"},
                }
            ).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith(
                "https://api.github.com/user/packages/container/"
            )
            assert request.get_header("Authorization") == "Bearer owner-only"
            assert timeout == 20
            calls.append(request.full_url.rsplit("/", 1)[1])
            if failure in ("403", "404"):
                raise urllib.error.HTTPError(
                    request.full_url, int(failure), "owner-only", {}, None
                )
            if failure == "network":
                raise urllib.error.URLError("owner-only arbitrary exception")
            return Response()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    if failure is None:
        exec(compile(source, "preflight", "exec"), {})  # noqa: S102 — trusted repo YAML; network replaced by fake.
        assert calls == [
            "pp-sber-autumn-2026-" + name for name in ("backend", "frontend", "runner")
        ]
    else:
        with pytest.raises(SystemExit) as caught:
            exec(compile(source, "preflight", "exec"), {})  # noqa: S102 — trusted repo YAML; network replaced by fake.
        category = (
            "http"
            if failure in ("403", "404")
            else "metadata"
            if failure == "owner"
            else failure
        )
        code = (
            failure
            if failure in ("403", "404")
            else "200"
            if failure in ("malformed", "owner")
            else "none"
        )
        assert (
            str(caught.value)
            == f"package=pp-sber-autumn-2026-backend class={category} http={code}"
        )
        assert len(calls) == (0 if failure == "credential" else 1)
        assert capsys.readouterr() == ("", "")
