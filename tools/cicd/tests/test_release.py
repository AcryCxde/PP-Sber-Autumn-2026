"""Release boundary tests: no daemon, credentials, registry or uploads."""

import json
import subprocess
from pathlib import Path

import pytest

from tools.cicd.release import (
    CHECKS,
    IMAGE_REPOSITORIES,
    REPOSITORY,
    Release,
    publish,
    trusted_context,
)

SHA = "a" * 40
IMAGE_ID = "sha256:" + "1" * 64
DIGEST = "sha256:" + "2" * 64


@pytest.fixture
def env():
    return {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/master",
        "GITHUB_SHA": SHA,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "CICD_PUBLISH_ENABLED": "true",
        "NEEDS_JSON": json.dumps(
            {
                name: {
                    "result": "success",
                    **(
                        {"outputs": {"docs_only": "false"}}
                        if name == "classify"
                        else {}
                    ),
                }
                for name in CHECKS
            }
        ),
    }


@pytest.fixture
def images(tmp_path):
    directory = tmp_path / "images"
    directory.mkdir()
    for name in IMAGE_REPOSITORIES:
        (directory / f"{name}.tar").write_bytes(b"fake docker archive")
        (directory / f"{name}.image-id").write_text(IMAGE_ID + "\n")
    return directory


class Docker:
    def __init__(self, fail=None, wrong=None):
        self.calls = []
        self.fail = fail
        self.wrong = wrong

    def __call__(self, args):
        self.calls.append(args)
        if self.fail and self.fail in " ".join(args):
            raise subprocess.CalledProcessError(7, args)
        if args[:2] == ["image", "load"]:
            name = Path(args[-1]).stem
            return "Loaded image: " + (
                "evil:tag" if self.wrong == "load" else f"pps-ci:{name}"
            )
        if args[:2] == ["image", "inspect"]:
            if "@" in args[-1]:
                return json.dumps(
                    [
                        {
                            "Id": "sha256:" + "3" * 64
                            if self.wrong == "id"
                            else IMAGE_ID,
                            "Os": "linux",
                            "Architecture": "arm64"
                            if self.wrong == "platform"
                            else "amd64",
                            "RepoDigests": [
                                args[-1]
                                if self.wrong != "repodigest"
                                else "evil@" + DIGEST
                            ],
                        }
                    ]
                )
            return "sha256:" + "3" * 64 if self.wrong == "archive-id" else IMAGE_ID
        if args[:3] == ["buildx", "imagetools", "inspect"]:
            return "bad digest" if self.wrong == "digest" else DIGEST
        return ""


def test_publish_only_after_three_verified_images(env, images, tmp_path):
    docker = Docker()
    output = tmp_path / "release.json"
    release = publish(trusted_context(env), images, output, docker)
    data = json.loads(output.read_text())
    assert Release.parse(output.read_text()) == release
    assert data == {
        "schema": 1,
        "repository": REPOSITORY,
        "sha": SHA,
        "workflow_run_id": 123,
        "run_attempt": 2,
        "platform": "linux/amd64",
        "images": {
            name: {"repository": repo, "digest": DIGEST, "image_id": IMAGE_ID}
            for name, repo in IMAGE_REPOSITORIES.items()
        },
        "checks": dict.fromkeys(CHECKS, "success"),
    }
    assert release.artifact_name == f"release-{SHA}-123-2"
    assert sum(call[0] == "push" for call in docker.calls) == 3
    assert sum(call[0] == "pull" for call in docker.calls) == 3
    assert not any("build" in call for call in docker.calls)
    for repo in IMAGE_REPOSITORIES.values():
        assert ["tag", IMAGE_ID, f"{repo}:{SHA}"] in docker.calls
        assert ["pull", "--platform", "linux/amd64", f"{repo}@{DIGEST}"] in docker.calls


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_EVENT_NAME", "workflow_dispatch"),
        ("GITHUB_REF", "refs/heads/topic"),
        ("GITHUB_REF", "refs/tags/master"),
        ("GITHUB_REPOSITORY", "attacker/repo"),
        ("GITHUB_WORKFLOW_REF", "wrong"),
        ("GITHUB_SHA", "A" * 40),
        ("GITHUB_SHA", "a" * 39),
        ("GITHUB_RUN_ID", "0"),
        ("GITHUB_RUN_ID", "True"),
        ("GITHUB_RUN_ATTEMPT", "-1"),
        ("GITHUB_RUN_ATTEMPT", "1.0"),
        ("CICD_PUBLISH_ENABLED", ""),
        ("CICD_PUBLISH_ENABLED", "false"),
        ("CICD_PUBLISH_ENABLED", "True"),
        ("NEEDS_JSON", "{}"),
    ],
)
def test_blocked_context(env, key, value):
    env[key] = value
    with pytest.raises(ValueError):
        trusted_context(env)


@pytest.mark.parametrize(
    "key", ["CICD_PUBLISH_ENABLED", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF"]
)
def test_missing_context(env, key):
    del env[key]
    with pytest.raises(ValueError):
        trusted_context(env)


@pytest.mark.parametrize("check", CHECKS)
@pytest.mark.parametrize("result", ["failure", "skipped", "cancelled"])
def test_required_check_must_succeed(env, check, result):
    needs = json.loads(env["NEEDS_JSON"])
    needs[check]["result"] = result
    env["NEEDS_JSON"] = json.dumps(needs)
    with pytest.raises(ValueError):
        trusted_context(env)


def test_release_requires_postgres_evidence_in_context_and_manifest(
    env, images, tmp_path
):
    assert "schema-postgres" in CHECKS
    release = publish(trusted_context(env), images, tmp_path / "release.json", Docker())
    manifest = release.to_dict()
    del manifest["checks"]["schema-postgres"]
    with pytest.raises(ValueError):
        Release.parse(json.dumps(manifest))
    needs = json.loads(env["NEEDS_JSON"])
    del needs["schema-postgres"]
    env["NEEDS_JSON"] = json.dumps(needs)
    with pytest.raises(ValueError):
        trusted_context(env)


def test_docs_only_cannot_publish(env):
    needs = json.loads(env["NEEDS_JSON"])
    needs["classify"]["outputs"]["docs_only"] = "true"
    env["NEEDS_JSON"] = json.dumps(needs)
    with pytest.raises(ValueError):
        trusted_context(env)


@pytest.mark.parametrize(
    "wrong", ["load", "archive-id", "id", "platform", "repodigest", "digest"]
)
def test_inconsistent_images_never_produce_manifest(env, images, tmp_path, wrong):
    output = tmp_path / "release.json"
    with pytest.raises(ValueError):
        publish(trusted_context(env), images, output, Docker(wrong=wrong))
    assert not output.exists()


@pytest.mark.parametrize(
    "failure",
    ["image load", "image inspect", "tag", "push", "buildx imagetools", "pull"],
)
def test_command_failure_leaves_no_manifest_or_temporary_file(
    env, images, tmp_path, failure
):
    output = tmp_path / "release.json"
    output.write_text("stale manifest")
    with pytest.raises(subprocess.CalledProcessError):
        publish(trusted_context(env), images, output, Docker(fail=failure))
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("file", ["runner.tar", "runner.image-id"])
def test_missing_image_blocks_before_push(env, images, tmp_path, file):
    (images / file).unlink()
    docker = Docker()
    with pytest.raises((ValueError, OSError)):
        publish(trusted_context(env), images, tmp_path / "release.json", docker)
    assert not any(call[0] == "push" for call in docker.calls)


def test_parser_rejects_mutations(env, images, tmp_path):
    output = tmp_path / "release.json"
    publish(trusted_context(env), images, output, Docker())
    original = output.read_text()
    mutations = [
        ("schema", True),
        ("schema", 2),
        ("extra", 1),
        ("repository", "attacker/repo"),
        ("sha", "not-a-sha"),
        ("workflow_run_id", True),
        ("workflow_run_id", 0),
        ("run_attempt", False),
        ("run_attempt", "2"),
        ("run_attempt", -1),
        ("platform", "linux/arm64"),
        ("images", {}),
        ("checks", {}),
    ]
    for key, value in mutations:
        data = json.loads(original)
        data[key] = value
        with pytest.raises(ValueError):
            Release.parse(json.dumps(data))
    for key, value in [
        ("repository", "ghcr.io/evil/backend"),
        ("digest", "sha256:bad"),
        ("image_id", "a" * 64),
        ("extra", 1),
    ]:
        data = json.loads(original)
        data["images"]["backend"][key] = value
        with pytest.raises(ValueError):
            Release.parse(json.dumps(data))
    for bad in [
        original.rstrip()[:-1],
        original.replace('"schema": 1', '"schema": 1, "schema": 1'),
        original.replace('"digest":', '"digest": "bad", "digest":', 1),
    ]:
        with pytest.raises(ValueError):
            Release.parse(bad)


@pytest.mark.parametrize(
    "kind", ["empty-archive", "bad-id", "partial-push", "duplicate-needs"]
)
def test_other_fail_closed_boundaries(env, images, tmp_path, kind):
    docker = Docker()
    if kind == "empty-archive":
        (images / "runner.tar").write_bytes(b"")
    elif kind == "bad-id":
        (images / "runner.image-id").write_text("$(touch injected)")
    elif kind == "partial-push":
        docker.fail = "push " + IMAGE_REPOSITORIES["runner"]
    else:
        env["NEEDS_JSON"] = env["NEEDS_JSON"].replace('"gate":', '"gate": {}, "gate":')
    output = tmp_path / "release.json"
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        publish(trusted_context(env), images, output, docker)
    assert not output.exists()
    if kind == "partial-push":
        assert sum(call[0] == "push" for call in docker.calls) == 3
    else:
        assert not docker.calls


def test_context_binding_is_required_when_consuming(env, images, tmp_path):
    output = tmp_path / "release.json"
    release = publish(trusted_context(env), images, output, Docker())
    for key in ["GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"]:
        changed = dict(env)
        changed[key] = "b" * 40 if key == "GITHUB_SHA" else "999"
        with pytest.raises(ValueError):
            release.bind(trusted_context(changed))
