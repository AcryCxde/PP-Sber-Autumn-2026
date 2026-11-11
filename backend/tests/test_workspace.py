import pytest

from backend.config import Settings
from backend.workspace import (
    WorkspaceError,
    acquire_gateway_lock,
    gateway_lock_path,
    normalize_artifact_path,
    release_gateway_lock,
    validate_workspace_root,
)


def test_subscription_intervals_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Settings(subscription_poll_interval_seconds=0)
    with pytest.raises(ValueError):
        Settings(subscription_window_seconds=-1)
    with pytest.raises(ValueError):
        Settings(max_concurrent_runs=0)


def test_non_sqlite_database_requires_durable_lease(tmp_path) -> None:
    with pytest.raises(WorkspaceError, match="durable executor lease"):
        gateway_lock_path(
            "postgresql+asyncpg://localhost/example",
            tmp_path / "workspaces",
        )


def test_gateway_lock_rejects_second_process(tmp_path) -> None:
    lock_path = tmp_path / "history.db.gateway.lock"
    first = acquire_gateway_lock(lock_path)
    try:
        with pytest.raises(WorkspaceError, match="Another gateway process"):
            acquire_gateway_lock(lock_path)
    finally:
        release_gateway_lock(first)

    second = acquire_gateway_lock(lock_path)
    release_gateway_lock(second)


def test_workspace_root_must_not_overlap_gateway_or_database(tmp_path) -> None:
    project_root = tmp_path / "gateway"
    project_root.mkdir()
    database_path = tmp_path / "data" / "history.db"

    with pytest.raises(WorkspaceError, match="gateway repository"):
        validate_workspace_root(
            project_root / "workspaces",
            project_root=project_root,
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        )

    with pytest.raises(WorkspaceError, match="database directory"):
        validate_workspace_root(
            database_path.parent / "workspaces",
            project_root=project_root,
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        )


def test_artifact_path_is_relative_and_confined(tmp_path) -> None:
    workspace = tmp_path / "workspaces" / "conversation"
    workspace.mkdir(parents=True)

    assert normalize_artifact_path(workspace, "reports/result.md") == "reports/result.md"
    assert normalize_artifact_path(
        workspace,
        str(workspace / "reports" / "result.md"),
    ) == "reports/result.md"

    for path in (
        str(tmp_path / "secret.txt"),
        "../secret.txt",
        ".git/config",
        ".runner/state.json",
    ):
        with pytest.raises(WorkspaceError):
            normalize_artifact_path(workspace, path)


def test_artifact_path_rejects_symlink_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this system")

    with pytest.raises(WorkspaceError):
        normalize_artifact_path(workspace, str(link / "secret.txt"))


async def test_second_gateway_cannot_lease_same_database(tmp_path) -> None:
    from backend.tests.databases import database_url
    from backend.workspace import acquire_gateway_lease

    url = database_url(tmp_path / "data" / "history.db")
    release = await acquire_gateway_lease(url, tmp_path / "workspaces")
    try:
        with pytest.raises(WorkspaceError, match="Another gateway process"):
            await acquire_gateway_lease(url, tmp_path / "workspaces")
    finally:
        await release()

    release = await acquire_gateway_lease(url, tmp_path / "workspaces")
    await release()


def test_workspace_discovery_entry_budget_bounds_dependency_tree(tmp_path):
    from backend.workspace import workspace_files
    for index in range(200):
        folder = tmp_path / "node_modules" / f"package-{index:03}"
        folder.mkdir(parents=True)
        (folder / "bundle.js").write_bytes(b"x")
    result = workspace_files(tmp_path, skip_dirs=set(), max_entries=30)
    assert result.truncated
    assert result.entries_scanned == 30
    assert len(result) <= 30


def test_workspace_discovery_includes_generated_artifacts_when_within_budget(tmp_path):
    from backend.workspace import workspace_files
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "report.pdf").write_bytes(b"report")
    result = workspace_files(tmp_path, skip_dirs=set(), max_entries=30)
    assert not result.truncated
    assert "dist/report.pdf" in result


def test_managed_material_symlink_cleanup_does_not_delete_target(tmp_path):
    from backend.materials import write_workspace_materials
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.md").write_bytes(b"artifact")
    (workspace / "materials").symlink_to(outside, target_is_directory=True)
    write_workspace_materials(workspace, {})
    assert (outside / "keep.md").read_bytes() == b"artifact"
    assert not (workspace / "materials").exists()
