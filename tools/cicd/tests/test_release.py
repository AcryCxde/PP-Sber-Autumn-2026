"""Release boundary tests: no daemon, credentials, registry or uploads."""

import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from tools.cicd.release import (
    CHECKS,
    IMAGE_REPOSITORIES,
    REPOSITORY,
    PackageAPI,
    Release,
    publish,
    require_private_packages,
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
    release = publish(trusted_context(env), images, output, docker, metadata=Metadata())
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
    release = publish(
        trusted_context(env),
        images,
        tmp_path / "release.json",
        Docker(),
        metadata=Metadata(),
    )
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
        publish(
            trusted_context(env),
            images,
            output,
            Docker(wrong=wrong),
            metadata=Metadata(),
        )
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
        publish(
            trusted_context(env),
            images,
            output,
            Docker(fail=failure),
            metadata=Metadata(),
        )
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("file", ["runner.tar", "runner.image-id"])
def test_missing_image_blocks_before_push(env, images, tmp_path, file):
    (images / file).unlink()
    docker = Docker()
    with pytest.raises((ValueError, OSError)):
        publish(
            trusted_context(env),
            images,
            tmp_path / "release.json",
            docker,
            metadata=Metadata(),
        )
    assert not any(call[0] == "push" for call in docker.calls)


def test_parser_rejects_mutations(env, images, tmp_path):
    output = tmp_path / "release.json"
    publish(trusted_context(env), images, output, Docker(), metadata=Metadata())
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
        publish(trusted_context(env), images, output, docker, metadata=Metadata())
    assert not output.exists()
    if kind == "partial-push":
        assert sum(call[0] == "push" for call in docker.calls) == 3
    else:
        assert not docker.calls


def test_context_binding_is_required_when_consuming(env, images, tmp_path):
    output = tmp_path / "release.json"
    release = publish(
        trusted_context(env), images, output, Docker(), metadata=Metadata()
    )
    for key in ["GITHUB_SHA", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"]:
        changed = dict(env)
        changed[key] = "b" * 40 if key == "GITHUB_SHA" else "999"
        with pytest.raises(ValueError):
            release.bind(trusted_context(changed))


class Metadata:
    def __init__(self, docker=None, bad=None):
        self.docker = docker
        self.bad = bad
        self.calls = []

    def get(self, name):
        self.calls.append(name)
        return {
            "name": name,
            "package_type": "container",
            "owner": {"login": "AcryCxde"},
            "visibility": "public"
            if name.endswith("runner") and self.bad
            else "private",
        }


def test_all_private_metadata_before_first_push(env, images, tmp_path):
    docker = Docker()
    metadata = Metadata(docker, bad=True)
    with pytest.raises(ValueError):
        publish(
            trusted_context(env), images, tmp_path / "release.json", docker, metadata
        )
    assert metadata.calls == [
        "pp-sber-autumn-2026-" + name for name in IMAGE_REPOSITORIES
    ]
    assert not any(call[0] == "push" for call in docker.calls)


def test_all_private_checked_before_push_and_again_after(env, images, tmp_path):
    docker = Docker()
    metadata = Metadata()
    events = []
    original_get = metadata.get

    def get(name):
        events.append(("metadata", name))
        return original_get(name)

    metadata.get = get

    def run(args):
        events.append((args[0], args[-1]))
        return docker(args)

    publish(trusted_context(env), images, tmp_path / "release.json", run, metadata)
    names = ["pp-sber-autumn-2026-" + name for name in IMAGE_REPOSITORIES]
    assert metadata.calls == names * 2
    first_push = next(i for i, event in enumerate(events) if event[0] == "push")
    assert [value for kind, value in events[:first_push] if kind == "metadata"] == names
    assert events[-3:] == [("metadata", name) for name in names]


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        "private",
        {},
        {"owner": []},
        {"visibility": "internal"},
        {"visibility": "public"},
        {"name": "wrong"},
        {"package_type": "npm"},
        {"owner": {"login": "other"}},
    ],
)
def test_metadata_shape_and_identity_fail_before_push(env, images, tmp_path, bad):
    class InvalidMetadata:
        def get(self, name):
            if isinstance(bad, dict):
                return {**Metadata().get(name), **bad} if bad else {}
            return bad

    docker = Docker()
    output = tmp_path / "release.json"
    output.write_text("stale")
    with pytest.raises(ValueError):
        publish(trusted_context(env), images, output, docker, InvalidMetadata())
    assert not output.exists()
    assert not any(call[0] == "push" for call in docker.calls)


@pytest.mark.parametrize("failure", ["403", "404", "network"])
def test_metadata_error_never_pushes(env, images, tmp_path, failure):
    class FailedMetadata:
        def get(self, name):
            raise ValueError("metadata " + failure)

    docker = Docker()
    with pytest.raises(ValueError):
        publish(
            trusted_context(env),
            images,
            tmp_path / "release.json",
            docker,
            FailedMetadata(),
        )
    assert not any(call[0] == "push" for call in docker.calls)


def test_post_publish_visibility_drift_leaves_no_manifest(env, images, tmp_path):
    class Drift(Metadata):
        def get(self, name):
            data = super().get(name)
            if len(self.calls) > 3:
                data["visibility"] = "public"
            return data

    output = tmp_path / "release.json"
    docker = Docker()
    with pytest.raises(ValueError):
        publish(trusted_context(env), images, output, docker, Drift())
    assert sum(call[0] == "push" for call in docker.calls) == 3
    assert not output.exists()


@pytest.mark.parametrize(
    "payload",
    [b"[]", b"null", b'{"visibility":', b'{"x":1,"x":2}', b"\xff", b"x" * 65537],
)
def test_package_http_malformed_is_redacted(payload):
    api = PackageAPI("secret-token")

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            assert size == 65537
            return payload

    class Opener:
        def open(self, request, timeout):
            assert (
                request.full_url
                == "https://api.github.com/users/AcryCxde/packages/container/pp-sber-autumn-2026-backend"
            )
            assert timeout == 20
            return Response()

    api.opener = Opener()
    with pytest.raises(ValueError):
        require_private_packages(api)


def test_package_api_cannot_target_caller_url():
    api = PackageAPI("secret-token")
    for target in [
        "https://evil.test",
        "../runner",
        "other",
        "pp-sber-autumn-2026-backend?x=1",
    ]:
        with pytest.raises(ValueError, match="foreign package target"):
            api.get(target)


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            "https://secret-token.invalid", status, "secret-token", {}, None
        )
        for status in (301, 302, 403, 404, 429, 500)
    ]
    + [urllib.error.URLError("secret-token"), TimeoutError("secret-token")],
)
def test_package_http_failure_is_redacted_before_push(
    env, images, tmp_path, failure, capsys
):
    api = PackageAPI("secret-token")

    class Opener:
        def open(self, request, timeout):
            raise failure

    api.opener = Opener()
    docker = Docker()
    with pytest.raises(ValueError, match="^package metadata request failed$") as caught:
        publish(trusted_context(env), images, tmp_path / "release.json", docker, api)
    assert caught.value.__suppress_context__
    assert "secret-token" not in str(caught.value)
    assert not any(call[0] == "push" for call in docker.calls)
    assert capsys.readouterr() == ("", "")
