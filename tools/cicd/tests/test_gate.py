import json
import os
import subprocess

import pytest

from tools.cicd.classify import classify
from tools.cicd.gate import gate_passes


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-qm", "base")
    return tmp_path, git, git("rev-parse", "HEAD")


def commit(repo, name):
    root, git, _ = repo
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content\n")
    git("add", "--", name)
    git("commit", "-qm", "change")
    return git("rev-parse", "HEAD")


@pytest.mark.parametrize(
    "name,docs",
    [
        ("README.md", True),
        ("LINKS.md", True),
        ("LICENSE", True),
        ("docs/a.md", True),
        ("docs/nested/a.md", True),
        ("runner/README.md", True),
        ("frontend/README.md", True),
        ("web-product/README.md", True),
        ("web-demo/README.md", True),
        ("backend/a.py", False),
        ("unknown.md", False),
        (".github/a.md", False),
        ("docs/a.sh", False),
        ("docs/prototypes/a.ts", False),
        ("docs/a b.md", True),
        ("docs/a\nb.md", True),
        ("docs/$(touch PWNED).md", True),
        ('docs/"quoted".md', True),
        ("-README.md", False),
        ("docs/../escape.md", False),
    ],
)
def test_paths(repo, name, docs):
    head = commit(repo, name)
    assert classify(repo[2], head)["docs_only"] is docs
    assert not (repo[0] / "PWNED").exists()


def test_rename_both_paths_and_delete(repo):
    head = commit(repo, "code.md")
    repo[1]("mv", "code.md", "README.md")
    repo[1]("commit", "-qm", "rename")
    renamed = repo[1]("rev-parse", "HEAD")
    assert not classify(head, renamed)["docs_only"]
    repo[1]("rm", "README.md")
    repo[1]("commit", "-qm", "delete")
    assert classify(renamed, repo[1]("rev-parse", "HEAD"))["docs_only"]


def test_push_full_range(repo):
    commit(repo, "backend/a.py")
    head = commit(repo, "README.md")
    assert not classify(repo[2], head)["docs_only"]


def test_pr_merge_base(repo):
    git = repo[1]
    git("checkout", "-qb", "feature")
    head = commit(repo, "README.md")
    git("checkout", "-qb", "base", repo[2])
    base = commit(repo, "backend/a.py")
    assert classify(base, head, triple_dot=True)["docs_only"]
    assert not classify(base, head)["docs_only"]


@pytest.mark.parametrize(
    "base", ["", "0" * 40, "not-a-ref", "--help", "$(touch PWNED)"]
)
def test_invalid_base(repo, base):
    head = commit(repo, "README.md")
    assert not classify(base, head)["docs_only"]


def test_empty_diff(repo):
    assert not classify(repo[2], repo[2])["docs_only"]


@pytest.mark.parametrize("change", ["executable_add", "executable_mode", "symlink"])
def test_nonregular_docs_are_heavy(repo, change):
    root, git, base = repo
    if change != "executable_add":
        base = commit(repo, "README.md")
    path = root / "README.md"
    if change == "symlink":
        path.unlink()
        path.symlink_to("target")
    else:
        path.write_text("content\n")
        path.chmod(0o755)
    git("add", "--", "README.md")
    git("commit", "-qm", "nonregular documentation")
    assert not classify(base, git("rev-parse", "HEAD"))["docs_only"]


HEAVY = ("build", "python", "frontend", "web-product", "compose")


def needs(docs="false", classification="success", result="success"):
    return {
        "classify": {"result": classification, "outputs": {"docs_only": docs}},
        **{job: {"result": result} for job in HEAVY},
    }


@pytest.mark.parametrize(
    "docs,result,expected",
    [
        ("true", "skipped", True),
        ("false", "success", True),
        ("false", "skipped", False),
        ("true", "failure", False),
        ("true", "cancelled", False),
        ("false", "cancelled", False),
        ("invalid", "success", False),
        ("", "success", False),
    ],
)
def test_gate(docs, result, expected):
    assert gate_passes(needs(docs=docs, result=result)) is expected


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped"])
def test_classification_failure(state):
    assert not gate_passes(needs(docs="true", classification=state, result="skipped"))


def test_missing_job():
    value = needs()
    del value["compose"]
    assert not gate_passes(value)


def test_gate_cli():
    env = {**os.environ, "NEEDS_JSON": json.dumps(needs())}
    result = subprocess.run(
        [os.sys.executable, "-m", "tools.cicd.gate"], env=env, check=False
    )
    assert result.returncode == 0
    env["NEEDS_JSON"] = "{}"
    assert (
        subprocess.run(
            [os.sys.executable, "-m", "tools.cicd.gate"], env=env, check=False
        ).returncode
        != 0
    )
