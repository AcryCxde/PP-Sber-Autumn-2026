from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from sqlalchemy.engine import make_url


PROTECTED_WORKSPACE_PARTS = {".git", ".runner"}


class WorkspaceError(ValueError):
    pass


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_workspace_root(
    workspace_root: Path,
    *,
    project_root: Path,
    database_url: str,
) -> Path:
    resolved_root = workspace_root.expanduser().resolve()
    resolved_project = project_root.resolve()

    if _contains(resolved_project, resolved_root) or _contains(
        resolved_root, resolved_project
    ):
        raise WorkspaceError(
            "Workspace root must not overlap the gateway repository"
        )

    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() == "sqlite" and parsed_url.database:
        database_path = Path(parsed_url.database).expanduser().resolve()
        if _contains(resolved_root, database_path) or _contains(
            database_path.parent, resolved_root
        ):
            raise WorkspaceError(
                "Workspace root must not overlap the database directory"
            )

    resolved_root.mkdir(parents=True, exist_ok=True)
    return resolved_root


def gateway_lock_path(database_url: str, workspace_root: Path) -> Path:
    parsed_url = make_url(database_url)
    if parsed_url.get_backend_name() != "sqlite":
        raise WorkspaceError(
            "Non-SQLite deployments require a durable executor lease"
        )
    if parsed_url.database and parsed_url.database != ":memory:":
        database_path = Path(parsed_url.database).expanduser().resolve()
        return database_path.with_name(f"{database_path.name}.gateway.lock")
    return workspace_root.resolve().parent / "gateway.lock"


def acquire_gateway_lock(lock_path: Path) -> BinaryIO:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise WorkspaceError(
            "Another gateway process already owns this database"
        ) from error
    return handle


def release_gateway_lock(handle: BinaryIO) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def conversation_workspace(
    workspace_root: Path,
    conversation_id: str,
    *,
    create: bool,
) -> Path:
    if not conversation_id or any(
        character in conversation_id for character in ("/", "\\", "\0")
    ):
        raise WorkspaceError("Invalid conversation ID for workspace")

    workspace = (workspace_root / conversation_id).resolve()
    if not _contains(workspace_root.resolve(), workspace):
        raise WorkspaceError("Conversation workspace escaped workspace root")
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def normalize_artifact_path(workspace: Path, raw_path: str) -> str:
    if not raw_path or "\0" in raw_path:
        raise WorkspaceError("Artifact path is empty or invalid")

    raw = Path(raw_path)
    target = raw if raw.is_absolute() else workspace / raw
    resolved_workspace = workspace.resolve()
    resolved_target = target.resolve(strict=False)

    try:
        relative = resolved_target.relative_to(resolved_workspace)
    except ValueError as error:
        raise WorkspaceError("Artifact path is outside the workspace") from error

    if not relative.parts:
        raise WorkspaceError("Artifact path points to the workspace root")
    if any(part.lower() in PROTECTED_WORKSPACE_PARTS for part in relative.parts):
        raise WorkspaceError("Artifact path points to a protected directory")

    return relative.as_posix()
