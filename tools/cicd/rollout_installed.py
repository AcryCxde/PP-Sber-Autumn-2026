"""Explicit trusted-host launcher for the reviewed same-node migration only.

Install this module, its imported code/SQL, Python, CLI binaries and dependencies
(root-owned immutable reviewed package; asyncpg AND boto3/botocore are required)
before activation. The live deploy Python does not currently provide boto3.
Provision the extra /etc/coreteams-deploy/rollout-installed.json protected path,
HostConfig, Approval, schema-probe mapping/baseline/read-only credential and CA,
root-only backup certificate/key, protected scratch/local directories, and the
existing same-node S3 bucket/least-privilege credentials separately. This launcher
creates no buckets, credentials, RBAC, directories or other infrastructure.

Use an env -i installed launcher; no argv paths, commands or URLs are accepted.
The maintenance activation config must exist: this entrypoint NEVER falls back
to legacy image patching. Source/target proof is real catalog+target comparison;
revision reads bracket the catalog read on the fixed host (not one transaction).
The operator must hold external schema writers as well as application writers.
HTTP GET checks prove route availability/auth denial only; authenticated user
and admin authorization smoke remains explicitly deferred, not fabricated.
Do not activate without an operator-owned restore rehearsal and config review.
"""

import asyncio
import hashlib
import ipaddress
import math
import os
import ssl
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from tools.cicd import deploy_credentials, deploy_server, schema_probe
from tools.cicd.maintenance import Readiness
from tools.cicd.maintenance_schema import LiveSchema, PhysicalContinuity, verify_source
from tools.cicd.release import _json, _object, _require
from tools.cicd.rollout_backup import BackupConfig
from tools.cicd.rollout_host import HostConfig, K3sHost, bounded_run
from tools.cicd.rollout_provider import APPROVAL_LIMIT, Approval, RolloutProvider
from tools.cicd.rollout_restore import RestoreVerifier, sanitize_evidence
from tools.cicd.schema_probe import protected_credential_read, protected_read

CONFIG_PATH = Path("/etc/coreteams-deploy/rollout-installed.json")
PUBLIC_ORIGIN = "https://coreteams.2026.tochka-urfu.tech"
BACKUP_PATHS = {
    "pgdata",
    "home",
    "data",
    "s3_pvc",
    "local_root",
    "scratch_root",
    "recipient_cert",
    "private_key",
}
HTTP_LIMIT = 1024 * 1024


def _path(value: object) -> Path:
    _require(
        type(value) is str
        and value.startswith("/")
        and "\x00" not in value
        and ".." not in value.split("/")
        and str(Path(value)) == value,
        "invalid installed path",
    )
    return Path(value)


def _local_origin(value: object) -> str:
    _require(type(value) is str, "invalid installed origin")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid installed origin") from error
    _require(
        parsed.scheme == "http"
        and not parsed.username
        and not parsed.password
        and parsed.path == parsed.query == parsed.fragment == ""
        and port is not None
        and 1 <= port <= 65535
        and (address.is_private or address.is_loopback)
        and not address.is_unspecified
        and not address.is_multicast
        and value
        == f"http://{'[' + str(address) + ']' if address.version == 6 else address}:{port}",
        "invalid installed origin",
    )
    return value


@dataclass(frozen=True)
class InstalledConfig:
    host_config: Path
    approval: Path
    backup: tuple[tuple[str, Path], ...]
    s3_endpoint: str
    s3_credential: Path
    private_origin: str
    public_origin: str

    def __post_init__(self) -> None:
        for path in (self.host_config, self.approval, self.s3_credential):
            _require(type(path) is type(Path()), "invalid installed path")
            _path(str(path))
        _require(type(self.backup) is tuple, "invalid backup paths")
        _require(
            len(self.backup) == len(BACKUP_PATHS)
            and {pair[0] for pair in self.backup} == BACKUP_PATHS,
            "invalid backup paths",
        )
        for name, path in self.backup:
            _require(type(path) is type(Path()), "invalid backup path")
            _path(str(path))
        _local_origin(self.s3_endpoint)
        _local_origin(self.private_origin)
        _require(self.public_origin == PUBLIC_ORIGIN, "unapproved public origin")

    @classmethod
    def parse(cls, raw: bytes) -> "InstalledConfig":
        _require(type(raw) is bytes and 0 < len(raw) <= 65536, "config size")
        data = _object(
            _json(raw.decode("utf-8")),
            {
                "schema",
                "host_config",
                "approval",
                "backup",
                "s3",
                "smoke",
            },
        )
        _require(type(data["schema"]) is int and data["schema"] == 1, "config schema")
        backup = _object(data["backup"], BACKUP_PATHS)
        s3 = _object(data["s3"], {"endpoint", "credential"})
        smoke = _object(data["smoke"], {"private_origin", "public_origin"})
        return cls(
            _path(data["host_config"]),
            _path(data["approval"]),
            tuple((name, _path(backup[name])) for name in sorted(BACKUP_PATHS)),
            s3["endpoint"],
            _path(s3["credential"]),
            smoke["private_origin"],
            smoke["public_origin"],
        )

    @classmethod
    def load(cls) -> "InstalledConfig":
        return cls.parse(protected_read(CONFIG_PATH, 65536))


def _deadline(timeout_seconds: int) -> float:
    _require(
        type(timeout_seconds) is int and 1 <= timeout_seconds <= 600,
        "observation budget",
    )
    return time.monotonic() + timeout_seconds


def _remaining(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    _require(remaining >= 1, "observation deadline exceeded")
    return math.floor(remaining)


def _get(origin: str, path: str, expected: int, *, deadline: float) -> None:
    """Fixed GET only; bounded child also covers DNS stalls and oversized output."""
    budget = min(200, _remaining(deadline))
    scheme = urlsplit(origin).scheme
    output = bounded_run(
        [
            "/usr/bin/curl",
            "--disable",
            "--silent",
            "--show-error",
            "--noproxy",
            "*",
            "--proxy",
            "",
            "--request",
            "GET",
            "--proto",
            "=" + scheme,
            "--max-redirs",
            "0",
            "--connect-timeout",
            str(min(5, budget)),
            "--max-time",
            str(budget),
            "--max-filesize",
            str(HTTP_LIMIT),
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--url",
            origin + path,
        ],
        stdin=b"",
        timeout=budget,
        env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty"},
    )
    _require(output == str(expected).encode("ascii"), "HTTP smoke refused")
    _remaining(deadline)


def _http_smoke(config: InstalledConfig, *, public: bool, timeout_seconds: int) -> bool:
    deadline = _deadline(timeout_seconds)
    origin = config.public_origin if public else config.private_origin
    paths = (("/", 200), ("/product/", 200)) if public else ()
    for path, status in (*paths, ("/api/auth/me", 401), ("/api/admin/v1/users", 401)):
        _get(origin, path, status, deadline=deadline)
    return True


def _s3(config: InstalledConfig):
    """Only fixed existing endpoint/bucket; no ambient credential discovery."""
    import boto3
    from botocore.config import Config

    credential = _object(
        _json(
            protected_credential_read(config.s3_credential, exact_mode=True).decode(
                "utf-8"
            )
        ),
        {"access_key_id", "secret_access_key"},
    )
    for value in credential.values():
        _require(
            type(value) is str
            and 0 < len(value) <= 4096
            and not any(ord(char) < 33 or ord(char) > 126 for char in value),
            "invalid S3 credential",
        )
    return boto3.client(
        "s3",
        endpoint_url=config.s3_endpoint,
        aws_access_key_id=credential["access_key_id"],
        aws_secret_access_key=credential["secret_access_key"],
        aws_session_token="",
        region_name="us-east-1",
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 2},
            proxies={},
            s3={"addressing_style": "path"},
            signature_version="s3v4",
        ),
    )


@dataclass(frozen=True)
class _BackupHost:
    host: K3sHost
    restore: RestoreVerifier

    def assert_quiescent(self) -> None:
        self.host.assert_quiescent()

    def verify_restore(self, pgdata: Path) -> str:
        return self.restore.verify_restore(pgdata)


def build_provider(config: InstalledConfig) -> RolloutProvider:
    """Compose existing owners; no second maintenance policy or incoming inputs."""
    host = K3sHost(HostConfig.load(config.host_config))
    approval = Approval.parse(protected_read(config.approval, APPROVAL_LIMIT))
    mapping = schema_probe.TargetMapping.parse(
        _json(protected_read(schema_probe.CONFIG_PATH, 65536).decode("utf-8"))
    )
    baseline = schema_probe.Baseline.parse(
        _json(protected_read(schema_probe.BASELINE_PATH, 65536).decode("utf-8"))
    )
    _require(mapping == approval.transition.mapping, "probe mapping approval mismatch")
    _require(
        baseline == approval.transition.baseline, "probe baseline approval mismatch"
    )
    sql = protected_read(
        Path(schema_probe.__file__).with_name("schema_catalog.sql"), 262144
    )
    _require(
        mapping.profile == schema_probe.Profile(hashlib.sha256(sql).hexdigest()),
        "installed SQL mismatch",
    )
    credential = _object(
        _json(
            protected_credential_read(
                schema_probe.CREDENTIAL_PATH, exact_mode=True
            ).decode("utf-8")
        ),
        {"username", "password"},
    )
    _require(credential["username"] == mapping.role, "read-only probe role mismatch")
    password = credential["password"]
    _require(
        type(password) is str and bool(password) and "\x00" not in password,
        "invalid probe credential",
    )
    tls = ssl.create_default_context(
        cadata=protected_read(Path(mapping.ca_file), 65536).decode("ascii")
    )
    _require(
        tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED,
        "probe TLS required",
    )
    connection = schema_probe.CatalogConnection(
        mapping.endpoint,
        mapping.port,
        mapping.database,
        mapping.role,
        mapping.profile,
        mapping.ca_file,
        tls,
    )
    paths = dict(config.backup)
    for name in ("recipient_cert", "private_key"):
        protected_credential_read(paths[name], exact_mode=True)

    async def catalog_read(timeout: int) -> object:
        async with asyncio.timeout(timeout):
            return await schema_probe.read_catalog(connection, password=password)

    initial: tuple[schema_probe.Facts, tuple[str, str]] | None = None

    def identity(deadline: float):
        return host.database_identity(
            endpoint=mapping.endpoint,
            port=mapping.port,
            pgdata=paths["pgdata"],
            timeout_seconds=_remaining(deadline),
        )

    def inspect(*, timeout_seconds: int) -> LiveSchema:
        nonlocal initial
        deadline = _deadline(timeout_seconds)
        before = identity(deadline)
        catalog = asyncio.run(catalog_read(_remaining(deadline)))
        after = identity(deadline)
        _require(before == after, "physical DB changed during observation")
        facts = schema_probe.Snapshot.parse(catalog).facts
        _require(
            facts.server_address == after.address and facts.server_port == after.port,
            "catalog peer is not the verified DB Pod",
        )
        current = (after.system_identifier, after.binding)
        if initial is None:
            # Do not learn trust from the first arbitrary endpoint: it must pass
            # the historical approved target AND exact approved source schema.
            live = LiveSchema(catalog, after.revisions)
            verify_source(approval.transition, live)
            initial = (facts, current)
            return live
        continuity = PhysicalContinuity(initial[0], initial[1], current, after.address)
        return LiveSchema(catalog, after.revisions, continuity)

    def evidence(*, timeout_seconds: int) -> str:
        deadline = _deadline(timeout_seconds)
        _require(initial is not None, "approved initial observation required")
        before = identity(deadline)
        observed = sanitize_evidence(
            host.database_evidence(timeout_seconds=_remaining(deadline))
        )
        after = identity(deadline)
        measured = _json(observed)
        _require(
            before == after
            and (after.system_identifier, after.binding) == initial[1]
            and measured["system_identifier"] == after.system_identifier
            and tuple(measured["revisions"]) == after.revisions,
            "backup evidence is not from the verified physical DB",
        )
        return observed

    def backup_config(observed: str) -> BackupConfig:
        # Called while quiescent; never replace actual evidence with approval data.
        for name in ("recipient_cert", "private_key"):
            protected_credential_read(paths[name], exact_mode=True)
        return BackupConfig(
            **paths,
            expected_database_evidence=observed,
            migration_preserves_s3=True,
            openssl="/usr/bin/openssl",
        )

    def private_readiness(*, timeout_seconds: int) -> bool:
        host.confirm_ready(frontend=False, timeout_seconds=timeout_seconds)
        return True

    def readiness(*, timeout_seconds: int) -> Readiness:
        images = host.ready_images(timeout_seconds=timeout_seconds)
        # Host wrappers re-check actual replicas, pod readiness and selected pins.
        return Readiness(images, (1, 1), (1, 1))

    return RolloutProvider(
        config.approval,
        host=host,
        inspect=inspect,
        database_evidence=evidence,
        backup_config=backup_config,
        backup_host=_BackupHost(host, RestoreVerifier(paths["scratch_root"])),
        s3=_s3(config),
        private_readiness=private_readiness,
        readiness=readiness,
        functional_smoke=lambda *, timeout_seconds: _http_smoke(
            config, public=False, timeout_seconds=timeout_seconds
        ),
        public_smoke=lambda *, timeout_seconds: _http_smoke(
            config, public=True, timeout_seconds=timeout_seconds
        ),
    )


def main() -> int:
    try:
        _require(not sys.argv[1:], "installed launcher accepts no arguments")
        _require(
            os.path.lexists(deploy_server.MAINTENANCE_CONFIG),
            "maintenance activation required",
        )
        # Clear proxy/cloud/SDK profile overrides before constructing any clients.
        os.environ.clear()
        provider = build_provider(InstalledConfig.load())
        return deploy_credentials.main(maintenance_provider=provider)
    except Exception:  # noqa: BLE001 -- sanitizing terminal process boundary
        # Process boundary: paths, bodies, credentials, DB rows and SDK errors must
        # never appear on stdout/stderr. No fallback, retry or automatic recovery.
        print("installed rollout launcher refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
