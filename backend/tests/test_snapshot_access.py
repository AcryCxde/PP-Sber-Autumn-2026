"""T10 storage-level probe for selected immutable snapshot access.

This is a synthetic SQLite/filesystem reference probe, not a public-sharing API test.
It records the isolation contract needed by a future production implementation.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest


PROJECT_ID = "project-t10-synthetic-001"
RUN_ID = "run-t10-synthetic-001"
SNAPSHOT_ID = "snapshot-t10-synthetic-001"
OWNER_ID = "identity-t10-owner-001"
VIEWER_ID = "identity-t10-viewer-001"
OTHER_ID = "identity-t10-other-001"
OPERATION_ID = "op-t10-save-001"
SNAPSHOT_RELATIVE_PATH = Path("snapshots") / SNAPSHOT_ID / "selected.json"


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            live_content TEXT NOT NULL
        );
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            status TEXT NOT NULL,
            result_content TEXT NOT NULL
        );
        CREATE TABLE snapshots (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            run_id TEXT NOT NULL REFERENCES runs(id),
            relative_path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE snapshot_grants (
            snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            identity_id TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (snapshot_id, identity_id)
        );
        CREATE TABLE durable_receipts (
            operation_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
            sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            state TEXT NOT NULL CHECK (state = 'committed')
        );
        """
    )
    connection.commit()


def snapshot_bytes(project_content: str, run_content: str) -> bytes:
    return json.dumps(
        {
            "project_id": PROJECT_ID,
            "run_id": RUN_ID,
            "project_content": project_content,
            "selected_run_result": run_content,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def read_snapshot(
    connection: sqlite3.Connection,
    storage_root: Path,
    identity_id: str,
) -> bytes:
    row = connection.execute(
        """
        SELECT s.relative_path, s.sha256
        FROM snapshots AS s
        JOIN snapshot_grants AS g ON g.snapshot_id = s.id
        WHERE s.id = ? AND s.deleted = 0
          AND g.identity_id = ? AND g.revoked = 0
        """,
        (SNAPSHOT_ID, identity_id),
    ).fetchone()
    if row is None:
        raise PermissionError("snapshot access denied")
    payload = (storage_root / row["relative_path"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != row["sha256"]:
        raise OSError("snapshot integrity check failed")
    return payload


def read_live_project(connection: sqlite3.Connection, identity_id: str) -> str:
    row = connection.execute(
        "SELECT live_content FROM projects WHERE id = ? AND owner_id = ?",
        (PROJECT_ID, identity_id),
    ).fetchone()
    if row is None:
        raise PermissionError("live project access denied")
    return str(row["live_content"])


def read_live_run(connection: sqlite3.Connection, identity_id: str) -> str:
    row = connection.execute(
        """
        SELECT r.result_content
        FROM runs AS r JOIN projects AS p ON p.id = r.project_id
        WHERE r.id = ? AND p.owner_id = ?
        """,
        (RUN_ID, identity_id),
    ).fetchone()
    if row is None:
        raise PermissionError("live run access denied")
    return str(row["result_content"])


def test_selected_snapshot_is_immutable_and_access_isolated(tmp_path: Path) -> None:
    database_path = tmp_path / "t10.sqlite3"
    storage_root = tmp_path / "storage"
    snapshot_path = storage_root / SNAPSHOT_RELATIVE_PATH
    snapshot_path.parent.mkdir(parents=True)

    connection = connect(database_path)
    create_schema(connection)
    connection.execute(
        "INSERT INTO projects VALUES (?, ?, ?)",
        (PROJECT_ID, OWNER_ID, "project:v1"),
    )
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?)",
        (RUN_ID, PROJECT_ID, "completed", "selected-result:v1"),
    )
    connection.commit()

    run_status = connection.execute(
        "SELECT status FROM runs WHERE id = ?", (RUN_ID,)
    ).fetchone()["status"]
    assert run_status == "completed", "terminal guard must reject non-terminal runs"

    payload = snapshot_bytes("project:v1", "selected-result:v1")
    digest = hashlib.sha256(payload).hexdigest()
    snapshot_path.write_bytes(payload)
    connection.execute(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, 0)",
        (
            SNAPSHOT_ID,
            PROJECT_ID,
            RUN_ID,
            SNAPSHOT_RELATIVE_PATH.as_posix(),
            digest,
            len(payload),
        ),
    )
    connection.execute(
        "INSERT INTO snapshot_grants VALUES (?, ?, 0)",
        (SNAPSHOT_ID, VIEWER_ID),
    )
    connection.execute(
        "INSERT INTO durable_receipts VALUES (?, ?, ?, ?, 'committed')",
        (OPERATION_ID, SNAPSHOT_ID, digest, len(payload)),
    )
    connection.commit()
    connection.close()

    # A new connection is the durable-write boundary for the receipt and ACL.
    connection = connect(database_path)
    receipt = connection.execute(
        "SELECT * FROM durable_receipts WHERE operation_id = ?", (OPERATION_ID,)
    ).fetchone()
    assert dict(receipt) == {
        "operation_id": OPERATION_ID,
        "snapshot_id": SNAPSHOT_ID,
        "sha256": digest,
        "size_bytes": len(payload),
        "state": "committed",
    }

    assert read_snapshot(connection, storage_root, VIEWER_ID) == payload
    with pytest.raises(PermissionError, match="snapshot access denied"):
        read_snapshot(connection, storage_root, OTHER_ID)
    with pytest.raises(PermissionError, match="live project access denied"):
        read_live_project(connection, VIEWER_ID)
    with pytest.raises(PermissionError, match="live run access denied"):
        read_live_run(connection, VIEWER_ID)

    connection.execute(
        "UPDATE projects SET live_content = 'project:v2-live' WHERE id = ?", (PROJECT_ID,)
    )
    connection.execute(
        "UPDATE runs SET result_content = 'selected-result:v2-live' WHERE id = ?", (RUN_ID,)
    )
    connection.commit()
    assert snapshot_path.read_bytes() == payload
    assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == digest

    backup_root = tmp_path / "backup"
    backup_storage = backup_root / "storage"
    backup_storage.mkdir(parents=True)
    shutil.copy2(snapshot_path, backup_storage / "selected.json")
    backup_database = backup_root / "policy.sqlite3"
    backup_connection = connect(backup_database)
    connection.backup(backup_connection)
    backup_connection.close()

    connection.execute(
        "UPDATE snapshot_grants SET revoked = 1 WHERE snapshot_id = ? AND identity_id = ?",
        (SNAPSHOT_ID, VIEWER_ID),
    )
    connection.execute("UPDATE snapshots SET deleted = 1 WHERE id = ?", (SNAPSHOT_ID,))
    connection.commit()
    snapshot_path.unlink()
    with pytest.raises(PermissionError, match="snapshot access denied"):
        read_snapshot(connection, storage_root, VIEWER_ID)
    connection.close()

    restored_database = tmp_path / "restored.sqlite3"
    restored_root = tmp_path / "restored-storage"
    restored_path = restored_root / SNAPSHOT_RELATIVE_PATH
    restored_path.parent.mkdir(parents=True)
    shutil.copy2(backup_database, restored_database)
    shutil.copy2(backup_storage / "selected.json", restored_path)

    restored = connect(restored_database)
    assert read_snapshot(restored, restored_root, VIEWER_ID) == payload
    with pytest.raises(PermissionError, match="snapshot access denied"):
        read_snapshot(restored, restored_root, OTHER_ID)
    with pytest.raises(PermissionError, match="live project access denied"):
        read_live_project(restored, VIEWER_ID)
    with pytest.raises(PermissionError, match="live run access denied"):
        read_live_run(restored, VIEWER_ID)
    restored.close()

    print(
        json.dumps(
            {
                "project_id": PROJECT_ID,
                "run_id": RUN_ID,
                "snapshot_id": SNAPSHOT_ID,
                "granted_identity_id": VIEWER_ID,
                "operation_id": OPERATION_ID,
                "path": SNAPSHOT_RELATIVE_PATH.as_posix(),
                "sha256": digest,
                "size_bytes": len(payload),
                "durable_receipt": "committed and reread through a new SQLite connection",
                "negative_access": {
                    "ungranted_snapshot": "DENY",
                    "viewer_live_project": "DENY",
                    "viewer_live_run": "DENY",
                    "revoked_or_deleted_snapshot": "DENY",
                    "restored_ungranted_snapshot": "DENY",
                    "restored_viewer_live_project": "DENY",
                    "restored_viewer_live_run": "DENY",
                },
                "backup_restore_acl": "PRESERVED",
            },
            sort_keys=True,
        )
    )
