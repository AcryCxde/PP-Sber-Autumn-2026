import subprocess
from pathlib import Path

import pytest

from ctrunner import checkpoint
from ctrunner.checkpoint import (
    CheckpointError,
    CheckpointFailure,
    Head,
    clear_stale_lock,
    commit_turn,
    read_head,
    run_git,
)


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@local")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    run_git(tmp_path, "init", "-q")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "init")
    return tmp_path


def test_initial_head_has_no_turn(repo: Path) -> None:
    head = read_head(repo)
    assert head.turn_id is None
    assert len(head.sha) == 40


def test_commit_turn_records_files_and_trailer(repo: Path) -> None:
    (repo / "a.txt").write_text("one")
    sha = commit_turn(repo, "t1")
    assert read_head(repo) == Head(sha, "t1")
    tracked = run_git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.decode()
    assert "a.txt" in tracked


def test_commit_turn_without_changes_still_marks_turn(repo: Path) -> None:
    before = read_head(repo).sha
    sha = commit_turn(repo, "t2")
    assert sha != before
    assert read_head(repo).turn_id == "t2"


def test_repo_hooks_do_not_block_checkpoint(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert commit_turn(repo, "t3")


def test_clear_stale_lock(repo: Path) -> None:
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    assert clear_stale_lock(repo) is True
    assert not lock.exists()
    assert clear_stale_lock(repo) is False


def _fail_with(monkeypatch: pytest.MonkeyPatch, stderr: bytes) -> None:
    def boom(project: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.CalledProcessError(128, ["git", *args], stderr=stderr)

    monkeypatch.setattr(checkpoint, "run_git", boom)


def test_disk_full_is_classified(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_with(monkeypatch, b"error: No space left on device")
    with pytest.raises(CheckpointError) as caught:
        commit_turn(repo, "t4")
    assert caught.value.failure is CheckpointFailure.DISK_FULL


def test_other_git_failure_is_classified(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_with(monkeypatch, b"fatal: something else")
    with pytest.raises(CheckpointError) as caught:
        commit_turn(repo, "t5")
    assert caught.value.failure is CheckpointFailure.FAILED
