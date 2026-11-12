import re
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
    assert "secrets." not in text  # github.token is scoped to the trusted publish job.
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
