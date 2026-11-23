"""Real streaming encryption/readback and complete restoration beyond old size limits."""

import json
import subprocess

import pytest

from tools.cicd.cold_backup import ColdBackup, tree_hash


@pytest.fixture
def snapshot(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    volumes = {}
    for alias in ("db", "data", "home", "s3"):
        path = storage / alias
        path.mkdir()
        (path / "unicode-данные").write_bytes((alias + " — сохранено").encode())
        (path / "link").symlink_to("unicode-данные")
        volumes[alias] = str(path)
    key, cert = tmp_path / "key", tmp_path / "cert"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=backup-test",
        ],
        check=True,
        capture_output=True,
    )
    config = {"volumes": volumes, "backup_key": str(key), "backup_cert": str(cert)}
    directory = tmp_path / "attempt"
    directory.mkdir()
    return ColdBackup(config), directory


def test_complete_encrypted_snapshot_and_restore_including_new_objects(snapshot):
    backup, directory = snapshot
    before = {name: tree_hash(path) for name, path in backup.volumes.items()}
    backup.create(directory)
    for path in backup.volumes.values():
        (path / "unicode-данные").write_text("changed by failed release")
        (path / "new-table-or-file").write_text("must disappear on restore")
    # Rehearsal may modify readback: restore must decrypt pristine data again.
    (directory / "readback" / "db" / "unicode-данные").write_text("rehearsed")
    backup.restore(directory)
    assert {name: tree_hash(path) for name, path in backup.volumes.items()} == before
    assert all(not (p / "new-table-or-file").exists() for p in backup.volumes.values())
    assert (directory / "snapshot.cms").stat().st_mode & 0o777 == 0o600


def test_ciphertext_tampering_is_refused_before_restoring(snapshot):
    backup, directory = snapshot
    backup.create(directory)
    (directory / "snapshot.cms").write_bytes(b"corrupt")
    before = tree_hash(backup.volumes["db"])
    with pytest.raises(ValueError, match="ciphertext"):
        backup.restore(directory)
    assert tree_hash(backup.volumes["db"]) == before


def test_file_larger_than_192mib_is_streamed(snapshot):
    backup, directory = snapshot
    with (backup.volumes["db"] / "large-table").open("wb") as stream:
        stream.truncate(193 * 1024**2)
    backup.create(directory)
    assert (
        directory / "readback" / "db" / "large-table"
    ).stat().st_size == 193 * 1024**2
    assert json.loads((directory / "backup.json").read_text())["trees"][
        "db"
    ] == tree_hash(backup.volumes["db"])


def test_budget_is_checked_before_backup(snapshot):
    backup, directory = snapshot
    backup.max_bytes = 1
    with pytest.raises(ValueError, match="resource budget"):
        backup.preflight(directory)
    assert not (directory / "snapshot.cms").exists()
