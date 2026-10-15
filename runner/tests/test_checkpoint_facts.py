"""Факты о файлах checkpoint: размер и sha256 содержимого в коммите."""

import hashlib
from pathlib import Path

import pytest

from ctrunner import checkpoint
from ctrunner.checkpoint import FileFact, commit_turn, read_file_facts, run_git


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


def fact(data: bytes) -> FileFact:
    return FileFact(len(data), hashlib.sha256(data).hexdigest())


def test_facts_describe_committed_content(repo: Path) -> None:
    big = bytes(range(256)) * 2000  # больше одного блока чтения
    (repo / "a.md").write_bytes("Привет".encode())
    (repo / "empty.txt").write_bytes(b"")
    (repo / "dir").mkdir()
    (repo / "dir" / "программа.bin").write_bytes(big)
    sha = commit_turn(repo, "t1")
    assert read_file_facts(repo, sha) == {
        "a.md": fact("Привет".encode()),
        "empty.txt": fact(b""),
        "dir/программа.bin": fact(big),
    }


def test_facts_are_taken_from_commit_not_from_working_tree(repo: Path) -> None:
    (repo / "a.md").write_bytes(b"v1")
    sha = commit_turn(repo, "t1")
    (repo / "a.md").write_bytes(b"changed after commit")
    assert read_file_facts(repo, sha) == {"a.md": fact(b"v1")}


def test_only_files_changed_by_the_commit(repo: Path) -> None:
    (repo / "old.md").write_bytes(b"old")
    (repo / "gone.md").write_bytes(b"gone")
    commit_turn(repo, "t1")
    (repo / "new.md").write_bytes(b"new")
    (repo / "old.md").write_bytes(b"old, edited")
    (repo / "gone.md").unlink()
    sha = commit_turn(repo, "t2")
    assert read_file_facts(repo, sha) == {
        "new.md": fact(b"new"),
        "old.md": fact(b"old, edited"),
    }


def test_commit_without_changes_has_no_facts(repo: Path) -> None:
    assert read_file_facts(repo, commit_turn(repo, "t1")) == {}


def test_first_commit_without_parent_is_covered(tmp_path: Path) -> None:
    run_git(tmp_path, "init", "-q")
    (tmp_path / "a.md").write_bytes(b"x")
    run_git(tmp_path, "add", "-A")
    run_git(tmp_path, "commit", "-q", "-m", "first")
    sha = run_git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip()
    assert read_file_facts(tmp_path, sha) == {"a.md": fact(b"x")}


def test_path_with_line_break_is_skipped_others_kept(repo: Path) -> None:
    (repo / "bad\nname.md").write_bytes(b"x")
    (repo / "ok.md").write_bytes(b"y")
    sha = commit_turn(repo, "t1")
    assert read_file_facts(repo, sha) == {"ok.md": fact(b"y")}


def test_number_of_files_is_capped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkpoint, "MAX_FACT_FILES", 2)
    for name in ("a", "b", "c"):
        (repo / f"{name}.md").write_bytes(name.encode())
    sha = commit_turn(repo, "t1")
    assert list(read_file_facts(repo, sha)) == ["a.md", "b.md"]


def test_unknown_commit_raises_instead_of_returning_empty(repo: Path) -> None:
    with pytest.raises(Exception, match=r"."):
        read_file_facts(repo, "0" * 40)
