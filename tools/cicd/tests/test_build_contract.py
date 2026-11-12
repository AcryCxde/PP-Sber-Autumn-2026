"""Cheap build orchestration contracts; never invoke the Docker daemon."""

import subprocess
from pathlib import Path

import pytest
import yaml
from test_workflows import StrictLoader

from tools.cicd.gate import gate_passes

ROOT = Path(__file__).resolve().parents[3]


def test_build_precedes_checks_and_is_required():
    jobs = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )["jobs"]
    assert "build" in jobs
    assert jobs["build"]["timeout-minutes"] == 90
    for name in ("python", "frontend", "web-product", "compose"):
        assert set(jobs[name]["needs"]) == {"classify", "build"}
    assert "build" in jobs["gate"]["needs"]
    results = {name: {"result": "success"} for name in jobs["gate"]["needs"]}
    results["classify"]["outputs"] = {"docs_only": "false"}
    assert gate_passes(results)
    results["build"]["result"] = "failure"
    assert not gate_passes(results)


def test_archives_ids_and_smoke_without_rebuild(tmp_path):
    # Fake only the Docker process boundary, not orchestration or filesystem writes.
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [ "$1 $2" = "image inspect" ]; then\n'
        '  printf "sha256:%064d\\n" 1\n'
        'elif [ "$1 $2" = "image save" ]; then\n'
        '  touch "$4"\nfi\n'
    )
    docker.chmod(0o755)
    log = tmp_path / "docker.log"
    output = tmp_path / "images"
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "DOCKER_LOG": str(log),
    }
    script = ROOT / "tools/cicd/check-images.sh"
    subprocess.run(["bash", str(script), "build", str(output)], env=env, check=True)
    for name in ("backend", "frontend", "runner", "frontend-checks", "product-checks"):
        assert (output / f"{name}.tar").is_file()
        assert (output / f"{name}.image-id").read_text().startswith("sha256:")
    calls = log.read_text().splitlines()
    finals = [
        line for line in calls if line.startswith("build ") and "--target" not in line
    ]
    assert len(finals) == 3
    assert all("--platform linux/amd64" in line for line in finals)
    log.write_text("")
    subprocess.run(["bash", str(script), "smoke", str(output)], env=env, check=True)
    smoke_calls = log.read_text()
    assert "build " not in smoke_calls
    assert smoke_calls.count("run --rm --network none") == 3
    assert (
        "backend.main:app --help" not in smoke_calls
    )  # Never start application lifespan.
    assert "--lifespan" not in smoke_calls


@pytest.mark.parametrize("failure", ["build", "image save"])
def test_build_or_archive_failure_stops_pipeline(tmp_path, failure):
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "{failure}" ] || [ "$1 $2" = "{failure}" ]; then exit 7; fi\n'
        'if [ "$1 $2" = "image inspect" ]; then printf "sha256:%064d\\n" 1; fi\n'
    )
    docker.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "tools/cicd/check-images.sh"),
            "build",
            str(tmp_path / "images"),
        ],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 7
    assert not (tmp_path / "images/frontend.tar").exists()


@pytest.mark.parametrize("exit_code", [0, 7, 143])
def test_smoke_cleans_only_owned_container(tmp_path, exit_code):
    docker = tmp_path / "docker"
    docker.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        "if [[ $1 == run ]]; then\n"
        "  while [[ $# -gt 0 && $1 != --cidfile ]]; do shift; done\n"
        "  [[ $# -gt 1 ]] || exit 9\n"
        '  printf "%064d\\n" 2 > "$2"\n'
        + ('  kill -TERM "$PPID"\n' if exit_code == 143 else "")
        + f"  exit {exit_code}\n"
        + 'elif [[ "$1 $2" == "image inspect" ]]; then\n'
        '  printf "sha256:%064d\\n" 1\nfi\n'
    )
    docker.chmod(0o755)
    images = tmp_path / "images"
    images.mkdir()
    (images / "backend.image-id").write_text(f"sha256:{1:064d}\n")
    log = tmp_path / "docker.log"
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "tools/cicd/check-images.sh"),
            "smoke",
            str(images),
            "backend",
        ],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin", "DOCKER_LOG": str(log)},
        check=False,
    )
    assert result.returncode == exit_code
    assert f"rm -f {2:064d}" in log.read_text().splitlines()


def test_product_test_sources_are_in_build_context():
    # Dockerfile-specific ignore takes precedence over root .dockerignore.
    ignored = (ROOT / "frontend/Dockerfile.dockerignore").read_text().splitlines()
    assert "!web-product" in ignored
    assert "web-product/test" not in ignored
    assert list((ROOT / "web-product/test").glob("*.test.*"))


def test_artifacts_partition_final_images_and_each_ui_stage():
    jobs = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )["jobs"]
    uploads = {
        s["with"]["name"]: s["with"]
        for s in jobs["build"]["steps"]
        if "upload-artifact@" in s.get("uses", "")
    }
    groups = {
        "checked-images": ("backend", "frontend", "runner"),
        "frontend-checks": ("frontend-checks",),
        "product-checks": ("product-checks",),
    }
    assert set(uploads) == set(groups)
    for artifact, images in groups.items():
        assert set(uploads[artifact]["path"].splitlines()) == {
            f"images/{name}.{suffix}"
            for name in images
            for suffix in ("tar", "image-id")
        }
        assert uploads[artifact]["if-no-files-found"] == "error"
        assert uploads[artifact]["retention-days"] == 7
    publish_download = next(
        s for s in jobs["publish"]["steps"] if "download-artifact@" in s.get("uses", "")
    )
    assert publish_download["with"] == {"name": "checked-images", "path": "images"}


def test_frontend_checks_reuse_build_artifacts():
    jobs = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=StrictLoader
    )["jobs"]
    for name, tag in (
        ("frontend", "frontend-checks"),
        ("web-product", "product-checks"),
    ):
        commands = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert f"docker image load --input images/{tag}.tar" in commands
        assert f"check-images.sh checks images {tag}" in commands
        download = next(
            s for s in jobs[name]["steps"] if "download-artifact@" in s.get("uses", "")
        )
        assert download["with"] == {"name": tag, "path": "images"}
        assert jobs[name]["timeout-minutes"] == 15
        assert "docker build" not in commands
        assert " install" not in commands
    dockerfile = (ROOT / "frontend/Dockerfile").read_text()
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in dockerfile
    assert "COPY --from=product /app/dist /usr/share/nginx/html/product" in dockerfile
    assert "CLAUDE_CODE_VERSION=2.1.112" in (ROOT / "backend/Dockerfile").read_text()
