"""Unactivated, same-node pre-DDL backup adapter; no rollout policy or credentials.

Binding contract (installed host shim, NOT release input): hold rollout lock;
stop admission/apps/runners and all home/data writers; record actual database
schema/collation AND application data evidence; cleanly stop PostgreSQL; call
create_backup; only its successful receipt permits DDL. Keep writers stopped
throughout. SeaweedFS remains running. assert_quiescent must check this actual
host state, including pg_controldata's clean shutdown, before and after copy.

Cold physical PGDATA preserves catalog/index/collation representation rather
than trusting a logical schema roundtrip. No tablespaces/external WAL/symlinks
are supported. Scratch PostgreSQL MUST match source binaries, OS, extensions,
locale libraries and run as an unprivileged database owner. The host owns that
binding; the fixture PG17 is not evidence for production PG16.

S3 app assets are intentionally excluded ONLY when maintenance/migration does
not mutate S3 and every app artifact writer is stopped. Never archive raw S3
PVCs: that would include this adapter's previous backups. Local protected copy
and coreteams-deploy-backups are SAME NODE, not independent/off-host disaster
recovery. No automatic retention, restore-to-production or bucket creation.

Requires OpenSSL CMS AuthEnvelopedData AES-256-GCM, RSA recipient certificate,
server-only unencrypted private key for scratch verification. Deploy credentials
stay in the installed server S3 client. Client must set bounded connect/read
and retry budgets (e.g. botocore Config(connect_timeout=5, read_timeout=30,
retries={'total_max_attempts': 2})); this adapter does not create that client.
Single PUT only, encrypted size <5 GiB. Provisioning/activation is separate.
"""

import hashlib
import json
import os
import stat
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

BUCKET = "coreteams-deploy-backups"
MAX_OBJECT = 5 * 1024**3 - 1
CHUNK = 1024**2


class Host(Protocol):
    def assert_quiescent(self) -> None:
        """Refuse unless apps/writers stopped and DB cleanly stopped, under lock."""
        ...

    def verify_restore(self, pgdata: Path) -> str:
        """Verify actual DB in separate scratch; return actual comparable evidence.

        Never accept a supplied signature as observed evidence. Include catalog,
        database locale/provider/version, critical rows and sequence values.
        """
        ...


class S3(Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: BinaryIO) -> object: ...
    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...
    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, object]: ...


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _private(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    _require(not path.is_symlink(), "protected path is a symlink")
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode), "invalid protected path")
    _require(info.st_uid == os.geteuid() and info.st_mode & 0o077 == 0, "path must be owner-only")


@dataclass(frozen=True)
class BackupConfig:
    pgdata: Path
    home: Path
    data: Path
    s3_pvc: Path
    local_root: Path
    scratch_root: Path
    recipient_cert: Path
    private_key: Path
    expected_database_evidence: str
    migration_preserves_s3: bool
    timeout: int = 600
    openssl: str = "openssl"

    def __post_init__(self) -> None:
        _require(self.migration_preserves_s3 is True, "S3 exclusion needs migration-preserves-S3 contract")
        _require(isinstance(self.expected_database_evidence, str) and bool(self.expected_database_evidence.strip()), "missing observed database evidence")
        _require(type(self.timeout) is int and self.timeout > 0, "invalid timeout")
        paths = (self.pgdata, self.home, self.data, self.s3_pvc, self.local_root, self.scratch_root)
        for path in paths:
            _require(path.is_absolute() and path.is_dir() and path.resolve() == path, "roots must be existing canonical absolute directories")
        for index, first in enumerate(paths):
            for second in paths[index + 1:]:
                _require(not (first.is_relative_to(second) or second.is_relative_to(first)), "backup roots overlap")
        for path in (self.local_root, self.scratch_root):
            _private(path, directory=True)
        _private(self.private_key, directory=False)
        _require(self.recipient_cert.is_file(), "missing recipient certificate")


@dataclass(frozen=True)
class BackupReceipt:
    local_path: Path
    object_key: str
    sha256: str
    database_evidence: str


def _run(args: list[str], timeout: int, *, stdout: BinaryIO | int = subprocess.PIPE) -> bytes:
    result = subprocess.run(
        args, check=True, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout
    )
    return result.stdout or b""


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_tree(root: Path) -> None:
    # Do not silently omit external WAL/tablespaces or follow an application link
    # into S3/local backups. Refusal is safer than a partial recovery point.
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            _require(not stat.S_ISLNK(mode), "symlink not supported in backup source")
            _require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "special file not supported in backup source")
            _require(not path.is_mount(), "nested mounts require separate backup binding")


def _archive(config: BackupConfig, target: Path) -> None:
    _require((config.pgdata / "PG_VERSION").is_file() and not (config.pgdata / "postmaster.pid").exists(), "PGDATA must be cold")
    for root in (config.pgdata, config.home, config.data):
        _check_tree(root)
    # dereference=True writes independent bytes for hardlinked files. Symlinks
    # have already refused; host quiescence must prevent changes during this pass.
    with tarfile.open(target, "w", dereference=True) as archive:
        for name in ("pgdata", "home", "data"):
            archive.add(getattr(config, name), arcname=name)


def _extract(archive_path: Path, target: Path) -> None:
    with tarfile.open(archive_path) as archive:
        for member in archive:
            parts = Path(member.name).parts
            _require(bool(parts) and parts[0] in {"pgdata", "home", "data"} and not Path(member.name).is_absolute() and ".." not in parts, "invalid backup member")
            _require(member.isfile() or member.isdir(), "backup link/special member refused")
        archive.extractall(target, filter="data")
    # Python's data filter retains owner permissions but clears setuid etc.
    (target / "pgdata").chmod(0o700)


def _readback(s3: S3, key: str, local: Path, target: Path) -> str:
    size = local.stat().st_size
    _require(s3.head_object(Bucket=BUCKET, Key=key).get("ContentLength") == size, "S3 readback size mismatch")
    body = s3.get_object(Bucket=BUCKET, Key=key)["Body"]
    digest = hashlib.sha256()
    try:
        with local.open("rb") as original, target.open("xb") as output:
            while chunk := body.read(CHUNK):
                _require(chunk == original.read(len(chunk)), "S3 readback bytes mismatch")
                output.write(chunk)
                digest.update(chunk)
            _require(original.read(1) == b"", "S3 readback truncated")
    finally:
        body.close()
    return digest.hexdigest()


def create_backup(config: BackupConfig, host: Host, s3: S3) -> BackupReceipt:
    """Fail closed. No receipt/verified marker until exact readback + real restore.

    An encrypted local copy is retained even on upload/verification failure;
    it is NOT verified until the .verified.json marker exists. Caller keeps
    maintenance active on every exception and never treats mere file existence
    or successful PUT as permission to migrate.
    """
    # Recheck filesystem protection at use, not merely config construction.
    config.__post_init__()
    host.assert_quiescent()
    identifier = uuid.uuid4().hex
    key = f"pre-ddl/{identifier}.cms.enc"
    local = config.local_root / f"{identifier}.cms.enc"
    with tempfile.TemporaryDirectory(prefix="backup-", dir=config.scratch_root) as temporary:
        scratch = Path(temporary)
        archive = scratch / "recovery.tar"
        _archive(config, archive)
        host.assert_quiescent()
        # AuthEnvelopedData is authenticated GCM, unlike legacy CMS CBC.
        with local.open("xb") as output:
            local.chmod(0o600)
            _run([config.openssl, "cms", "-encrypt", "-binary", "-aes-256-gcm", "-outform", "DER", "-in", str(archive), str(config.recipient_cert)], config.timeout, stdout=output)
            output.flush()
            os.fsync(output.fileno())
        _fsync(config.local_root)
        _require(0 < local.stat().st_size <= MAX_OBJECT, "backup exceeds single-PUT limit")
        with local.open("rb") as stream:
            s3.put_object(Bucket=BUCKET, Key=key, Body=stream)
        readback = scratch / "readback.cms"
        digest = _readback(s3, key, local, readback)
        decrypted = scratch / "restored.tar"
        _run([config.openssl, "cms", "-decrypt", "-binary", "-inform", "DER", "-in", str(readback), "-inkey", str(config.private_key), "-out", str(decrypted)], config.timeout)
        restored = scratch / "restored"
        restored.mkdir(mode=0o700)
        _extract(decrypted, restored)
        actual = host.verify_restore(restored / "pgdata")
        _require(actual == config.expected_database_evidence, "restored database evidence mismatch")
        host.assert_quiescent()
        receipt = BackupReceipt(local, key, digest, actual)
        # Protected marker binds exact ciphertext to evidence. No source hashes or
        # observer supplied by release code can substitute for host verification.
        marker = local.with_suffix(local.suffix + ".verified.json")
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"bucket": BUCKET, "object_key": key, "sha256": digest, "database_evidence": actual, "s3_assets": "excluded: maintenance must not mutate S3", "recovery_scope": "same-node only"}, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        _fsync(config.local_root)
        return receipt


def verify_postgres_scratch(pgdata: Path, *, binaries: Path, evidence_sql: str, database: str, user: str, timeout: int = 120) -> str:
    """Start only the supplied restored PGDATA with no TCP listener.

    Host must call as unprivileged DB owner, with the exact production PG build,
    environment and extension/locale libraries. evidence_sql is INSTALLED trusted
    read-only SQL, identical to the pre-stop observation, not uploaded SQL. Return
    deterministic psql output (including collation and application data probes).
    Helper never connects to production or uses ambient PGHOST/PGSERVICE/PGPASS.
    PGDATA is modified by recovery only inside caller's separate scratch root.
    """
    _require(os.geteuid() != 0, "scratch PostgreSQL must not run as root")
    _require(pgdata.is_absolute() and pgdata.is_dir() and not (pgdata / "postmaster.pid").exists(), "invalid cold scratch PGDATA")
    # The caller must pass a copy: this function deliberately has no source path.
    # No production config/includes, preload hooks, replication or archive writes.
    with tempfile.TemporaryDirectory(prefix="pg-scratch-") as temporary:
        runtime = Path(temporary)
        runtime.chmod(0o700)
        config = runtime / "postgresql.conf"
        hba = runtime / "pg_hba.conf"
        hba.write_text("local all all trust\n")
        (pgdata / "postgresql.auto.conf").write_text("")
        for recovery in ("standby.signal", "recovery.signal"):
            (pgdata / recovery).unlink(missing_ok=True)
        # runtime is generated locally; quote once for PostgreSQL string syntax.
        socket = str(runtime).replace("'", "''")
        config.write_text(f"listen_addresses = ''\nunix_socket_directories = '{socket}'\nunix_socket_permissions = 0700\nhba_file = '{socket}/pg_hba.conf'\nssl = off\narchive_mode = off\nshared_preload_libraries = ''\n")
        started = False
        try:
            _run([str(binaries / "pg_ctl"), "-D", str(pgdata), "-l", str(runtime / "server.log"), "-o", f"-c config_file={config}", "-w", "-t", str(timeout), "start"], timeout + 5)
            started = True
            # All connection coordinates explicit; clean env removes service and
            # connection-option inheritance. Read-only transactions bound SQL.
            env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
            env["PGOPTIONS"] = "-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=5000"
            return subprocess.run([str(binaries / "psql"), "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", str(runtime), "-U", user, "-d", database, "-c", evidence_sql], check=True, capture_output=True, text=True, timeout=timeout, env=env).stdout
        finally:
            # Even a readiness timeout can leave a live postmaster; pg_ctl stop
            # uses only this restored PGDATA, never production.
            if started or (pgdata / "postmaster.pid").exists():
                _run([str(binaries / "pg_ctl"), "-D", str(pgdata), "-w", "-t", str(timeout), "-m", "immediate", "stop"], timeout + 5)
