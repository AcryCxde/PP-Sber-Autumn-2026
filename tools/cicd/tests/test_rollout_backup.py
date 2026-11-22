"""Local fixture tests; no production endpoint, credentials or published services."""

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tools.cicd.rollout_backup import BackupConfig, create_backup, preflight_backup


def test_preflight_refuses_total_archive_before_cold_state(layout):
    from dataclasses import replace

    # Each file is below the cap; their combined tar (including headers) is not.
    (layout.pgdata / "postmaster.pid").write_text("running")
    for root in (layout.home, layout.data):
        (root / "large").write_bytes(b"x" * 20000)
    config = replace(layout, max_archive_bytes=32768)
    with pytest.raises(ValueError, match="archive.*bound"):
        preflight_backup(
            config.pgdata,
            config.home,
            config.data,
            available_memory_bytes=2 * 1024**3,
            max_archive_bytes=config.max_archive_bytes,
        )
    assert not list(layout.local_root.iterdir())
    assert not list(layout.scratch_root.iterdir())


def test_preflight_requires_available_memory_not_total(layout):
    with pytest.raises(ValueError, match="available memory"):
        preflight_backup(
            layout.pgdata,
            layout.home,
            layout.data,
            available_memory_bytes=2 * 1024**3 - 1,
        )


def test_preflight_accounts_for_pax_headers_padding_and_empty_dirs(layout):
    from tools.cicd.rollout_backup import _archive

    (layout.home / ("long-" + "x" * 120)).mkdir()
    (layout.data / "odd-sized").write_bytes(b"x" * 513)
    size = preflight_backup(
        layout.pgdata, layout.home, layout.data, available_memory_bytes=2 * 1024**3
    )
    archive = layout.scratch_root / "comparison.tar"
    _archive(layout, archive)
    assert size == archive.stat().st_size


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("MemTotal: 999999999 kB\nMemAvailable: 2097152 kB\n", 2 * 1024**3),
        ("MemTotal: 999999999 kB\nMemFree: 2097152 kB\n", None),
        ("MemAvailable: 2097152 MB\n", None),
        ("MemAvailable: -1 kB\n", None),
    ],
)
def test_available_memory_uses_only_valid_linux_memavailable(
    monkeypatch, content, expected
):
    from tools.cicd.rollout_backup import available_memory

    monkeypatch.setattr(Path, "read_text", lambda path: content)
    if expected is None:
        with pytest.raises(ValueError, match="MemAvailable"):
            available_memory()
    else:
        assert available_memory() == expected


def test_cold_growth_refuses_before_encryption_or_upload(layout):
    from dataclasses import replace

    config = replace(layout, max_archive_bytes=32768)
    preflight_backup(
        config.pgdata,
        config.home,
        config.data,
        available_memory_bytes=2 * 1024**3,
        max_archive_bytes=config.max_archive_bytes,
    )
    (layout.data / "grew").write_bytes(b"x" * 32768)
    objects = LocalObjects()
    with pytest.raises(ValueError, match="archive.*bound"):
        create_backup(config, FixtureHost(), objects)
    assert objects.objects == {}
    assert not list(layout.local_root.iterdir())


@pytest.mark.parametrize("bound", [True, 0, -1, 192 * 1024**2 + 1])
def test_archive_bound_cannot_bypass_verified_ceiling(layout, bound):
    from dataclasses import replace

    with pytest.raises(ValueError, match="archive.*bound"):
        replace(layout, max_archive_bytes=bound)


OPENSSL = os.environ.get(
    "BACKUP_TEST_OPENSSL",
    "/opt/homebrew/bin/openssl" if Path("/opt/homebrew/bin/openssl").exists() else "openssl",
)


class LocalObjects:
    """An in-memory transport fixture, NOT a SeaweedFS compatibility proof."""

    def __init__(self):
        self.objects = {}
        self.corrupt = False

    def put_object(self, *, Bucket, Key, Body):
        self.objects[Bucket, Key] = Body.read()

    def head_object(self, *, Bucket, Key):
        return {"ContentLength": len(self.objects[Bucket, Key])}

    def get_object(self, *, Bucket, Key):
        data = self.objects[Bucket, Key]
        if self.corrupt:
            data = bytes([data[0] ^ 1]) + data[1:]
        return {"Body": io.BytesIO(data)}


@pytest.fixture
def layout(tmp_path):
    roots = {}
    for name in ("pgdata", "home", "data", "s3_pvc", "local_root", "scratch_root"):
        roots[name] = tmp_path / name
        roots[name].mkdir(mode=0o700)
    (roots["pgdata"] / "PG_VERSION").write_text("16\n")
    (roots["pgdata"] / "pg_wal").mkdir()
    (roots["pgdata"] / "row-fixture").write_bytes(b"synthetic rows\x00")
    (roots["home"] / "session").write_text("synthetic transcript")
    (roots["data"] / "secret").write_text("synthetic application state")
    # Must never enter an archive, including previously uploaded backups.
    (roots["s3_pvc"] / "old-backup").write_text("excluded")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run([OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-subj", "/CN=local-fixture", "-days", "1"], check=True, capture_output=True, timeout=30)
    key.chmod(0o600)
    return BackupConfig(
        **roots,
        recipient_cert=cert,
        private_key=key,
        expected_database_evidence="fixture-evidence",
        migration_preserves_s3=True,
        openssl=OPENSSL,
        memory_available=lambda: 2 * 1024**3,
    )


class FixtureHost:
    """Checks archive contents only, never claims database restore readiness."""

    def __init__(self):
        self.checks = 0
        self.evidence = "fixture-evidence"

    def assert_quiescent(self):
        self.checks += 1

    def verify_restore(self, pgdata):
        assert (pgdata / "row-fixture").read_bytes() == b"synthetic rows\x00"
        assert (pgdata.parent / "home/session").read_text() == "synthetic transcript"
        assert (pgdata.parent / "data/secret").read_text() == "synthetic application state"
        assert not (pgdata.parent / "s3_pvc").exists()
        return self.evidence


def test_verified_encrypted_exact_bytes_retained_locally(layout):
    host, objects = FixtureHost(), LocalObjects()
    receipt = create_backup(layout, host, objects)
    stored = objects.objects["coreteams-deploy-backups", receipt.object_key]
    assert stored == receipt.local_path.read_bytes()
    assert b"synthetic transcript" not in stored
    # Indefinite-length BER is the actual -stream output despite -outform DER.
    assert stored[:2] == b"\x30\x80"
    cms = subprocess.run(
        [
            OPENSSL,
            "cms",
            "-cmsout",
            "-print",
            "-inform",
            "DER",
            "-in",
            str(receipt.local_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert "id-smime-ct-authEnvelopedData" in cms
    assert "aes-256-gcm" in cms
    assert receipt.sha256 == hashlib.sha256(stored).hexdigest()
    assert receipt.local_path.stat().st_mode & 0o777 == 0o600
    assert host.checks >= 2
    assert list(layout.scratch_root.iterdir()) == []


@pytest.mark.parametrize("damage", ["tag", "truncated"])
def test_authenticated_decryption_refuses_before_restore(layout, damage):
    # Corrupt BOTH local + transport after PUT so exact-byte readback still passes;
    # only the real OpenSSL decoder/authentication check can catch this damage.
    class DamagedObjects(LocalObjects):
        def put_object(self, *, Bucket, Key, Body):
            super().put_object(Bucket=Bucket, Key=Key, Body=Body)
            encoded = bytearray(self.objects[Bucket, Key])
            assert encoded[-24:-22] == b"\x04\x10"  # GCM MAC OCTET STRING
            if damage == "tag":
                encoded[-8] ^= 1
            else:
                del encoded[-12:]
            self.objects[Bucket, Key] = bytes(encoded)
            Path(Body.name).write_bytes(encoded)

    class NeverRestore(FixtureHost):
        def verify_restore(self, pgdata):
            pytest.fail("unauthenticated plaintext reached restore")

    with pytest.raises(subprocess.CalledProcessError):
        create_backup(layout, NeverRestore(), DamagedObjects())
    assert len(list(layout.local_root.glob("*.enc"))) == 1
    assert not list(layout.local_root.glob("*.verified.json"))
    assert not list(layout.scratch_root.iterdir())


def test_memory_drop_before_decoder_refuses_without_restore(layout):
    from dataclasses import replace

    readings = iter((2 * 1024**3, 2 * 1024**3 - 1))
    config = replace(layout, memory_available=lambda: next(readings))
    objects = LocalObjects()
    with pytest.raises(ValueError, match="available memory"):
        create_backup(config, FixtureHost(), objects)
    assert objects.objects  # PUT + exact readback are not permission to migrate.
    assert not list(layout.local_root.glob("*.verified.json"))
    assert not list(layout.scratch_root.iterdir())


def test_actual_tar_size_rechecked_before_crypto(layout, monkeypatch):
    from tools.cicd import rollout_backup

    archive = rollout_backup._archive

    def grown_archive(config, target):
        archive(config, target)
        with target.open("ab") as output:
            output.truncate(config.max_archive_bytes + 1)

    monkeypatch.setattr(rollout_backup, "_archive", grown_archive)
    objects = LocalObjects()
    with pytest.raises(ValueError, match="actual backup archive"):
        create_backup(layout, FixtureHost(), objects)
    assert not objects.objects
    assert not list(layout.local_root.iterdir())


def test_corrupt_readback_fails_and_preserves_local_copy(layout):
    objects = LocalObjects()
    objects.corrupt = True
    with pytest.raises(ValueError, match="readback"):
        create_backup(layout, FixtureHost(), objects)
    assert len(list(layout.local_root.glob("*.enc"))) == 1
    assert not list(layout.local_root.glob("*.verified.json"))


def test_wrong_actual_database_evidence_refuses(layout):
    host = FixtureHost()
    host.evidence = "different-collation-or-schema"
    with pytest.raises(ValueError, match="database evidence"):
        create_backup(layout, host, LocalObjects())
    assert not list(layout.local_root.glob("*.verified.json"))


def test_wrong_private_key_cannot_verify_backup(layout):
    from dataclasses import replace

    key = layout.private_key.parent / "wrong-key.pem"
    subprocess.run(
        [OPENSSL, "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
        check=True, capture_output=True, timeout=30,
    )
    key.chmod(0o600)
    with pytest.raises(subprocess.CalledProcessError):
        create_backup(replace(layout, private_key=key), FixtureHost(), LocalObjects())
    assert len(list(layout.local_root.glob("*.enc"))) == 1
    assert not list(layout.local_root.glob("*.verified.json"))
    assert not list(layout.scratch_root.iterdir())


def test_running_writers_refuse_before_upload(layout):
    class ActiveHost(FixtureHost):
        def assert_quiescent(self):
            raise ValueError("writers active")

    objects = LocalObjects()
    with pytest.raises(ValueError, match="writers active"):
        create_backup(layout, ActiveHost(), objects)
    assert objects.objects == {}
    assert not list(layout.local_root.iterdir())


def test_no_s3_exclusion_without_explicit_migration_contract(layout):
    from dataclasses import replace

    with pytest.raises(ValueError, match="S3"):
        replace(layout, migration_preserves_s3=False)


def test_local_backup_inside_s3_pvc_refused(layout):
    from dataclasses import replace

    local = layout.s3_pvc / "backup"
    local.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="overlap"):
        replace(layout, local_root=local)


def test_live_pgdata_refused(layout):
    (layout.pgdata / "postmaster.pid").write_text("1")
    with pytest.raises(ValueError, match="cold"):
        create_backup(layout, FixtureHost(), LocalObjects())


def test_external_tablespace_refused(layout):
    (layout.pgdata / "pg_tblspc").mkdir()
    (layout.pgdata / "pg_tblspc/123").symlink_to(layout.home, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        create_backup(layout, FixtureHost(), LocalObjects())


# Production PG16 is not claimed: this proves the physical recovery mechanism
# against an explicitly owned installed PG17 fixture without published services.
@pytest.fixture
def cold_postgres(layout):
    binaries = Path(os.environ.get("BACKUP_TEST_PG_BIN", "/opt/homebrew/opt/postgresql@17/bin"))
    if not (binaries / "initdb").is_file():
        pytest.skip("local PostgreSQL17 fixture binaries not installed")
    if os.geteuid() == 0:
        pytest.skip("fixture must run as an unprivileged database owner")
    shutil.rmtree(layout.pgdata)
    subprocess.run([str(binaries / "initdb"), "-D", str(layout.pgdata), "--no-locale", "-E", "UTF8", "-U", "fixture_owner", "--auth=trust"], check=True, capture_output=True, timeout=30)
    with tempfile.TemporaryDirectory(prefix="pg-source-") as directory:
        socket = Path(directory)
        try:
            subprocess.run([str(binaries / "pg_ctl"), "-D", str(layout.pgdata), "-l", str(socket / "log"), "-o", f"-c listen_addresses='' -c unix_socket_directories={socket}", "-w", "start"], check=True, capture_output=True, timeout=30)
            connection = [str(binaries / "psql"), "-X", "-At", "-v", "ON_ERROR_STOP=1", "-h", str(socket), "-U", "fixture_owner", "-d", "postgres", "-c"]
            subprocess.run([*connection, "CREATE TABLE recovery_rows (id bigint GENERATED ALWAYS AS IDENTITY, value text COLLATE \"C\"); INSERT INTO recovery_rows(value) VALUES ('real synthetic row'), ('second row'); CREATE INDEX recovery_partial ON recovery_rows(value COLLATE \"C\") WHERE value <> '';"], check=True, capture_output=True, timeout=30)
            sql = """
                SELECT json_build_object(
                    'database', (SELECT row_to_json(x) FROM (SELECT encoding, datlocprovider, datcollate, datctype, datcollversion, pg_database_collation_actual_version(oid) AS actual_version FROM pg_database WHERE datname=current_database()) x),
                    'columns', (SELECT json_agg(x ORDER BY attnum) FROM (SELECT attnum, attname, atttypid, atttypmod, attcollation FROM pg_attribute WHERE attrelid='recovery_rows'::regclass AND attnum>0) x),
                    'index', (SELECT pg_get_indexdef('recovery_partial'::regclass)),
                    'rows', (SELECT json_agg(x ORDER BY id) FROM recovery_rows x),
                    'sequence', (SELECT last_value FROM recovery_rows_id_seq),
                    'acl', (SELECT relacl FROM pg_class WHERE oid='recovery_rows'::regclass)
                );
            """
            evidence = subprocess.run([*connection, sql], check=True, capture_output=True, text=True, timeout=30).stdout
        finally:
            if (layout.pgdata / "postmaster.pid").exists():
                subprocess.run([str(binaries / "pg_ctl"), "-D", str(layout.pgdata), "-m", "fast", "-w", "stop"], check=True, capture_output=True, timeout=30)
    yield binaries, sql, evidence


def test_real_cold_postgres_restore_retains_rows_sequence_and_collation(layout, cold_postgres):
    from dataclasses import replace

    from tools.cicd.rollout_backup import verify_postgres_scratch

    binaries, sql, expected = cold_postgres
    config = replace(layout, expected_database_evidence=expected)

    class RealHost:
        def assert_quiescent(self):
            assert not (config.pgdata / "postmaster.pid").exists()
            control = subprocess.run([str(binaries / "pg_controldata"), str(config.pgdata)], check=True, capture_output=True, text=True, timeout=10).stdout
            assert "shut down" in control

        def verify_restore(self, pgdata):
            assert pgdata != config.pgdata
            assert pgdata.is_relative_to(config.scratch_root)
            return verify_postgres_scratch(pgdata, binaries=binaries, evidence_sql=sql, database="postgres", user="fixture_owner")

    original_control = (config.pgdata / "global/pg_control").read_bytes()
    receipt = create_backup(config, RealHost(), LocalObjects())
    assert receipt.database_evidence == expected
    assert (config.pgdata / "global/pg_control").read_bytes() == original_control
    assert not (config.pgdata / "postmaster.pid").exists()
    assert not list(config.scratch_root.iterdir())
