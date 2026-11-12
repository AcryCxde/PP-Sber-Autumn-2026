"""Exact committed migrations, all historical chains, and bridge on owned PG."""

import asyncio
import hashlib
from pathlib import Path

import pytest

from tools.cicd.schema_baseline import (
    ROOT,
    _psql,
    collect_baseline,
    migrate_release,
    release_source,
)
from tools.cicd.schema_probe import (
    UPGRADE_PATHS,
    CatalogConnection,
    ProbeError,
    TargetMapping,
    canonical_signature,
    read_catalog,
)
from tools.cicd.tests.schema_pg_fixture import pg16 as pg16_fixture
from tools.cicd.tests.test_schema_baseline import SHA, mapping_data

pg16 = pg16_fixture


def test_baseline_all_release_chains(pg16):
    """HEAD here is a test input, explicitly NOT an approved deployment SHA."""
    mapping = TargetMapping.parse(mapping_data())
    with release_source(ROOT, SHA) as source:
        baseline = collect_baseline(source, SHA, mapping, pg16.container, pg16.port)
        # Independently rerun canonical clean; do not use the collection's value.
        database = "baseline_independent"
        _psql(pg16.container, f"CREATE DATABASE {database} OWNER migrator")
        try:
            _psql(
                pg16.container,
                f"REVOKE CREATE, TEMP ON DATABASE {database} FROM PUBLIC; "
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
                "GRANT CREATE ON SCHEMA public TO migrator",
                database,
            )
            migrate_release(source, pg16.port, database, "clean")
            snapshot = asyncio.run(
                read_catalog(
                    CatalogConnection(
                        "127.0.0.1", pg16.port, database, "probe", mapping.profile
                    )
                )
            )
            assert (
                canonical_signature(snapshot, mapping.profile)
                == baseline.expected_signature
            )
        finally:
            _psql(pg16.container, f"DROP DATABASE {database} WITH (FORCE)")
    baseline.bind(SHA, mapping)
    assert set(dict(baseline.upgrade_signatures)) == set(UPGRADE_PATHS)
    assert set(dict(baseline.upgrade_signatures).values()) == {baseline.clean_signature}
    print("TEST-ONLY canonical signature", baseline.expected_signature)
    print("query SHA256", mapping.profile.query_sha256)


def test_baseline_unknown_fork_refuses(pg16):
    with release_source(ROOT, SHA) as source, pytest.raises(ProbeError):
        migrate_release(source, pg16.port, "baseline_unknown", "unknown:20261030_0013")


def test_source_exact_commit_ignores_dirty_checkout(tmp_path, pg16):
    """A minimal local clone has hostile dirty DDL; archive must ignore it."""
    from tools.cicd.schema_baseline import _git

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "fetch", "--quiet", "--no-tags", str(ROOT), SHA)
    _git(repo, "checkout", "--quiet", "--detach", SHA)
    file = repo / "backend/migrations/versions/20260929_0001_request_history.py"
    original = hashlib.sha256(file.read_bytes()).hexdigest()
    file.write_text("raise RuntimeError('dirty source must not execute')")
    (repo / "json.py").write_text("raise RuntimeError('import hijack')")
    with release_source(repo, SHA) as source:
        archived = source / file.relative_to(repo)
        assert hashlib.sha256(archived.read_bytes()).hexdigest() == original
        assert not archived.stat().st_mode & 0o222
        assert not (source / "json.py").exists()
        assert not (source / "backend/migrations/env.py").exists()
        assert all(
            path.suffix == ".py" for path in Path(source).rglob("*") if path.is_file()
        )
        database = "baseline_dirty"
        _psql(pg16.container, f"CREATE DATABASE {database} OWNER migrator")
        try:
            migrate_release(source, pg16.port, database, "clean")
        finally:
            _psql(pg16.container, f"DROP DATABASE {database} WITH (FORCE)")


def test_baseline_generate_local_output_and_cleanup(tmp_path):
    import os

    if os.environ.get("SCHEMA_PG16_TESTS") != "1":
        pytest.skip("set SCHEMA_PG16_TESTS=1 for owned PostgreSQL 16.15 tests")
    from tools.cicd.schema_baseline import generate, parse_json
    from tools.cicd.schema_probe import Baseline

    output = tmp_path / "test-only-baseline.json"
    mapping = TargetMapping.parse(mapping_data())
    baseline = generate(ROOT, SHA, mapping, output)
    assert Baseline.parse(parse_json(output.read_bytes())) == baseline
    assert output.stat().st_mode & 0o777 == 0o600
    assert baseline.profile == mapping.profile
