import subprocess
from pathlib import Path

import pytest

from ctrunner.main import seed_project


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@local")


def head(project: Path) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(project), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_seeds_data_ctf_and_git(tmp_path: Path) -> None:
    seed, ctf, project = tmp_path / "seed", tmp_path / "ctf", tmp_path / "w" / "project"
    seed.mkdir()
    (seed / "a.md").write_text("data")
    ctf.mkdir()
    (ctf / "settings.json").write_text("{}")
    seed_project(project, seed=seed, ctf=ctf)
    assert (project / "a.md").read_text() == "data"
    assert (project / ".claude" / "settings.json").exists()
    first = head(project)
    seed_project(project, seed=seed, ctf=ctf)  # повторный старт ничего не меняет
    assert head(project) == first


def test_leftover_partial_copy_is_replaced(tmp_path: Path) -> None:
    seed, project = tmp_path / "seed", tmp_path / "w" / "project"
    seed.mkdir()
    (seed / "a.md").write_text("data")
    partial = project.with_name(".project.partial")
    partial.mkdir(parents=True)
    (partial / "junk").write_text("x")
    seed_project(project, seed=seed, ctf=tmp_path / "no-ctf")
    assert sorted(p.name for p in project.iterdir()) == [".git", "a.md"]


def test_without_seed_creates_empty_repo(tmp_path: Path) -> None:
    project = tmp_path / "w" / "project"
    seed_project(project, seed=tmp_path / "none", ctf=tmp_path / "none")
    assert head(project)
