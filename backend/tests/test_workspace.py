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
