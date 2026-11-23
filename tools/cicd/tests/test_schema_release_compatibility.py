"""Finite release contracts, including the exact PR candidate rather than old DDL."""

import shutil

import pytest

from tools.cicd.schema_baseline import ROOT, _git, release_source, source_chain
from tools.cicd.schema_probe import Baseline, ProbeError, TargetMapping
from tools.cicd.tests.test_schema_baseline import SHA, baseline_data, mapping_data

# Historical acceptance of the retired per-release installer; future candidate
# graphs and metadata drift are checked in test_automatic_migrations_pg.py.
CANDIDATE_SHA = "00e9646d033bb36ca72a58b8ff5613f54886bd69"
CURRENT_HEAD = "20261112_0001"
LEGACY_HEAD = "20261111_0005"


def test_exact_candidate_archive_accepts_new_migration():
    with release_source(ROOT, CANDIDATE_SHA) as source:
        assert (
            source / "backend/migrations/versions/20261112_0001_admin_ai_access.py"
        ).is_file()


@pytest.mark.parametrize("head", [LEGACY_HEAD, CURRENT_HEAD])
def test_installed_baseline_accepts_only_complete_evidence_for_supported_head(head):
    data = baseline_data()
    data["migration_head"] = head
    if head == CURRENT_HEAD:
        data["upgrade_signatures"][head] = data["expected_signature"]
    parsed = Baseline.parse(data)
    parsed.bind(SHA, TargetMapping.parse(mapping_data()))
    assert parsed.to_data() == data


@pytest.mark.parametrize("head", [LEGACY_HEAD, CURRENT_HEAD, "20261113_0001"])
@pytest.mark.parametrize(
    "mutation", ["missing", "extra", "wrong_signature", "wrong_sha"]
)
def test_installed_baseline_refuses_incomplete_or_misbound_evidence(head, mutation):
    data = baseline_data()
    data["migration_head"] = head
    if head != LEGACY_HEAD:
        data["upgrade_signatures"][head] = data["expected_signature"]
    if mutation == "missing":
        data["upgrade_signatures"].pop(LEGACY_HEAD)
    elif mutation == "extra":
        data["upgrade_signatures"]["unsupported"] = data["expected_signature"]
    elif mutation == "wrong_signature":
        data["upgrade_signatures"][LEGACY_HEAD] = "pg_catalog_v1:sha256:" + "b" * 64
    else:
        data["release_sha"] = "b" * 40
    with pytest.raises(ProbeError):
        Baseline.parse(data).bind(SHA, TargetMapping.parse(mapping_data()))


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "wrong_parent", "branch", "dependency", "duplicate"],
)
def test_candidate_refuses_unreviewed_chain_before_execution(tmp_path, mutation):
    with release_source(ROOT, CANDIDATE_SHA) as source:
        shutil.copytree(source, tmp_path / "source")
    root = tmp_path / "source"
    versions = root / "backend/migrations/versions"
    latest = versions / "20261112_0001_admin_ai_access.py"
    latest.chmod(0o600)
    if mutation == "missing":
        latest.unlink()
        # Deleting a historical link is not a supported old release either.
        (versions / "20261111_0004_answer_fingerprint.py").unlink()
    elif mutation == "extra":
        (versions / "20261113_0001_extra.py").write_text(
            "revision='20261113_0001'\ndown_revision='20261112_0001'\nbranch_labels=None\ndepends_on=None\n"
        )
    elif mutation == "wrong_parent":
        latest.write_text(
            latest.read_text().replace(
                'down_revision = "20261111_0005"', 'down_revision = "20261111_0004"'
            )
        )
    elif mutation == "branch":
        latest.write_text(
            latest.read_text().replace(
                "branch_labels = None", "branch_labels = ('fork',)"
            )
        )
    elif mutation == "dependency":
        latest.write_text(
            latest.read_text().replace(
                "depends_on = None", "depends_on = '20261111_0004'"
            )
        )
    else:
        (versions / "20261112_0001_duplicate.py").write_bytes(latest.read_bytes())
    with pytest.raises(ProbeError):
        source_chain(root)


@pytest.mark.parametrize("head", [LEGACY_HEAD, CURRENT_HEAD])
def test_baseline_cannot_substitute_different_release_sha(head):
    data = baseline_data()
    data["migration_head"] = head
    if head == CURRENT_HEAD:
        data["upgrade_signatures"][head] = data["expected_signature"]
    with pytest.raises(ProbeError):
        Baseline.parse(data).bind(CANDIDATE_SHA, TargetMapping.parse(mapping_data()))


def test_oversized_release_blob_refuses_before_archive(monkeypatch):
    from tools.cicd import schema_baseline as module

    calls = []

    def git(repo, *args):
        calls.append(args[0])
        if args[0] == "rev-parse":
            return CANDIDATE_SHA.encode() + b"\n"
        if args[0] == "ls-tree":
            return (
                b"100644 blob " + b"a" * 40 + b" 262145\tbackend/migration_bridge.py\0"
            )
        pytest.fail("oversized blob reached archive")

    monkeypatch.setattr(module, "_git", git)
    with pytest.raises(ProbeError), release_source(ROOT, CANDIDATE_SHA):
        pytest.fail("oversized blob accepted")
    assert calls == ["rev-parse", "ls-tree"]


def test_ci_uses_immutable_candidate_for_pr_merge_and_master():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    job = workflow["jobs"]["schema-postgres"]
    assert job["env"]["SCHEMA_CANDIDATE_SHA"] == "${{ github.sha }}"
    assert job["env"]["SCHEMA_PG16_TESTS"] == "1"
    checkout = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    assert (
        _git(ROOT, "rev-parse", "--verify", CANDIDATE_SHA + "^{commit}")
        .decode()
        .strip()
        == CANDIDATE_SHA
    )
