"""Publish tested images only; schema-1 parsing is also the Task 4 boundary.

This does not attest that the active workflow has completed. A downstream verifier
must verify completed/success run metadata and artifact provenance through GitHub.
"""

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tools.cicd.gate import HEAVY_JOBS

REPOSITORY = "AcryCxde/PP-Sber-Autumn-2026"
IMAGE_REPOSITORIES = {
    name: f"ghcr.io/acrycxde/pp-sber-autumn-2026-{name}"
    for name in ("backend", "frontend", "runner")
}
CHECKS = tuple(sorted(HEAVY_JOBS | {"classify", "gate"}))
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _object(value: object, keys: set[str]) -> dict:
    _require(isinstance(value, dict) and set(value) == keys, "invalid object fields")
    return value


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _json(text: str) -> object:
    return json.loads(text, object_pairs_hook=_unique)


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _matches(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class Context:
    sha: str
    workflow_run_id: int
    run_attempt: int

    def __post_init__(self) -> None:
        _require(_matches(SHA, self.sha), "invalid SHA")
        _require(_positive(self.workflow_run_id), "invalid workflow run id")
        _require(_positive(self.run_attempt), "invalid run attempt")


def trusted_context(env: Mapping[str, str]) -> Context:
    expected = {
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": "refs/heads/master",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/ci.yml@refs/heads/master",
        "CICD_PUBLISH_ENABLED": "true",
    }
    _require(
        all(env.get(k) == v for k, v in expected.items()),
        "publication disabled or untrusted workflow",
    )
    needs = _object(_json(env.get("NEEDS_JSON", "")), set(CHECKS))
    _require(
        all(
            isinstance(v, dict) and v.get("result") == "success" for v in needs.values()
        ),
        "required check did not succeed",
    )
    outputs = needs["classify"].get("outputs")
    _require(
        isinstance(outputs, dict) and outputs.get("docs_only") == "false",
        "not a code release",
    )
    integers = []
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        text = env.get(key, "")
        _require(re.fullmatch(r"[1-9][0-9]*", text) is not None, "invalid run integer")
        integers.append(int(text))
    return Context(env.get("GITHUB_SHA", ""), *integers)


@dataclass(frozen=True)
class Image:
    repository: str
    digest: str
    image_id: str

    def __post_init__(self) -> None:
        _require(
            self.repository in IMAGE_REPOSITORIES.values(), "foreign image repository"
        )
        _require(_matches(DIGEST, self.digest), "invalid manifest digest")
        _require(_matches(DIGEST, self.image_id), "invalid image ID")

    def to_dict(self) -> dict:
        return {
            "repository": self.repository,
            "digest": self.digest,
            "image_id": self.image_id,
        }


@dataclass(frozen=True)
class Release:
    context: Context
    backend: Image
    frontend: Image
    runner: Image

    def __post_init__(self) -> None:
        for name, image in self.images.items():
            _require(
                image.repository == IMAGE_REPOSITORIES[name],
                "image repository mismatch",
            )

    @property
    def images(self) -> dict[str, Image]:
        return {
            "backend": self.backend,
            "frontend": self.frontend,
            "runner": self.runner,
        }

    @property
    def artifact_name(self) -> str:
        return f"release-{self.context.sha}-{self.context.workflow_run_id}-{self.context.run_attempt}"

    def bind(self, context: Context) -> None:
        _require(self.context == context, "release SHA/run/attempt mismatch")

    def to_dict(self) -> dict:
        return {
            "schema": 1,
            "repository": REPOSITORY,
            "sha": self.context.sha,
            "workflow_run_id": self.context.workflow_run_id,
            "run_attempt": self.context.run_attempt,
            "platform": "linux/amd64",
            "images": {name: image.to_dict() for name, image in self.images.items()},
            "checks": dict.fromkeys(CHECKS, "success"),
        }

    @classmethod
    def parse(cls, text: str) -> "Release":
        data = _object(
            _json(text),
            {
                "schema",
                "repository",
                "sha",
                "workflow_run_id",
                "run_attempt",
                "platform",
                "images",
                "checks",
            },
        )
        _require(type(data["schema"]) is int and data["schema"] == 1, "invalid schema")
        _require(data["repository"] == REPOSITORY, "foreign repository")
        _require(data["platform"] == "linux/amd64", "invalid platform")
        checks = _object(data["checks"], set(CHECKS))
        _require(all(v == "success" for v in checks.values()), "unsuccessful checks")
        context = Context(data["sha"], data["workflow_run_id"], data["run_attempt"])
        images = _object(data["images"], set(IMAGE_REPOSITORIES))
        parsed = {
            name: Image(**_object(value, {"repository", "digest", "image_id"}))
            for name, value in images.items()
        }
        return cls(context, **parsed)


class DockerRunner(Protocol):
    def __call__(self, args: Sequence[str]) -> str: ...


def docker(args: Sequence[str]) -> str:
    return subprocess.run(
        ["docker", *args], check=True, text=True, stdout=subprocess.PIPE, timeout=300
    ).stdout.strip()


def publish(
    context: Context, directory: Path, output: Path, run: DockerRunner = docker
) -> Release:
    # A pre-existing output must never survive a failed attempt as a fresh release.
    output.unlink(missing_ok=True)
    ids = {}
    for name in IMAGE_REPOSITORIES:
        archive = directory / f"{name}.tar"
        _require(
            archive.is_file() and archive.stat().st_size > 0, "missing/empty archive"
        )
        image_id = (directory / f"{name}.image-id").read_text().strip()
        _require(_matches(DIGEST, image_id), "invalid saved image ID")
        ids[name] = image_id
    # Validate all archives before the first push. Never accept names from load output.
    for name, image_id in ids.items():
        loaded = run(
            ["image", "load", "--input", str(directory / f"{name}.tar")]
        ).strip()
        _require(loaded == f"Loaded image: pps-ci:{name}", "unexpected archive image")
        _require(
            run(["image", "inspect", "--format", "{{.Id}}", f"pps-ci:{name}"]).strip()
            == image_id,
            "archive image ID mismatch",
        )
    images = {}
    for name, repository in IMAGE_REPOSITORIES.items():
        tag = f"{repository}:{context.sha}"
        run(["tag", ids[name], tag])
        run(["push", tag])
        digest = run(
            ["buildx", "imagetools", "inspect", "--format", "{{.Manifest.Digest}}", tag]
        ).strip()
        _require(_matches(DIGEST, digest), "invalid registry digest")
        reference = f"{repository}@{digest}"
        run(["pull", "--platform", "linux/amd64", reference])
        inspection = _json(run(["image", "inspect", reference]))
        _require(
            isinstance(inspection, list)
            and len(inspection) == 1
            and isinstance(inspection[0], dict),
            "invalid image inspection",
        )
        actual = inspection[0]
        _require(actual.get("Id") == ids[name], "registry image ID mismatch")
        _require(
            actual.get("Os") == "linux" and actual.get("Architecture") == "amd64",
            "registry platform mismatch",
        )
        _require(
            isinstance(actual.get("RepoDigests"), list)
            and reference in actual["RepoDigests"],
            "registry digest mismatch",
        )
        images[name] = Image(repository, digest, ids[name])
    release = Release(context, **images)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=output.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(release.to_dict(), stream, indent=2)
            stream.write("\n")
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return release


def main() -> int:
    try:
        context = trusted_context(os.environ)
        publish(context, Path("images"), Path("release.json"))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"release failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
