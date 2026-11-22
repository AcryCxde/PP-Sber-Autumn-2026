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
Private smoke requires operator-provisioned, existing admin and ordinary-user
sessions: smoke.sessions names a root:root 0600 JSON file with exactly
{"admin_session":"<token>","user_session":"<token>"}. Obtain/rotate existing
sessions out of band; never place them in Actions, argv, env, logs or this config.
Tokens are 43-character URL-safe coreteams_session cookies, not password/API keys.
No account/session creation or login is performed here. All smoke requests are GET;
normal auth activity bookkeeping (or expiry cleanup) can still update metadata.
smoke.private_http_trusted must be true ONLY after the operator reviews the fixed
private HTTP backend path/network as trusted for bearer cookies; private IP alone
is NOT transport authentication. Never use this on an untrusted ClusterIP network.
Public HTTPS smoke sends no cookies; private smoke never bypasses TLS to public.
The v1 overview uses distant fixed pages to read aggregates without user/run rows;
nonempty rows fail closed. No response bodies/headers are logged or persisted.
Do not activate without an operator-owned restore rehearsal and config review.
"""

import asyncio
import hashlib
import ipaddress
import math
import os
import re
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from tools.cicd import deploy_credentials, deploy_server, schema_probe
from tools.cicd.maintenance import Readiness
from tools.cicd.maintenance_schema import LiveSchema, PhysicalContinuity, verify_source
from tools.cicd.release import _json, _object, _require
from tools.cicd.rollout_backup import BackupConfig, available_memory, preflight_backup
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
    smoke_sessions: Path
    private_http_trusted: bool

    def __post_init__(self) -> None:
        for path in (
            self.host_config,
            self.approval,
            self.s3_credential,
            self.smoke_sessions,
        ):
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
        _require(self.private_http_trusted is True, "private HTTP smoke trust required")

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
        smoke = _object(
            data["smoke"],
            {"private_origin", "public_origin", "sessions", "private_http_trusted"},
        )
        return cls(
            _path(data["host_config"]),
            _path(data["approval"]),
            tuple((name, _path(backup[name])) for name in sorted(BACKUP_PATHS)),
            s3["endpoint"],
            _path(s3["credential"]),
            smoke["private_origin"],
            smoke["public_origin"],
            _path(smoke["sessions"]),
            smoke["private_http_trusted"],
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


@dataclass(frozen=True, repr=False)
class _SmokeSessions:
    admin_session: str
    user_session: str

    def __post_init__(self) -> None:
        for token in (self.admin_session, self.user_session):
            _require(
                type(token) is str
                and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None,
                "invalid smoke session",
            )
        _require(
            self.admin_session != self.user_session, "distinct smoke sessions required"
        )

    @classmethod
    def load(cls, path: Path) -> "_SmokeSessions":
        raw = protected_credential_read(path, exact_mode=True)
        try:
            data = _object(
                _json(raw.decode("utf-8")), {"admin_session", "user_session"}
            )
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("invalid smoke sessions") from None
        return cls(data["admin_session"], data["user_session"])


# Keep OFFSET within PostgreSQL bigint while making real user/run rows unreachable
# for the reviewed installation. Unexpected nonempty pages are rejected, never logged.
_SMOKE_PAGE = 2147483647
_ADMIN_SMOKE_PATH = (
    f"/api/admin/v1/overview?active_page={_SMOKE_PAGE}"
    f"&attention_page={_SMOKE_PAGE}&page_size=1"
)


def _get(
    origin: str,
    path: str,
    expected: int,
    *,
    deadline: float,
    session: str | None = None,
) -> bytes:
    """Fixed GET only; bounded child also covers DNS stalls and oversized output."""
    budget = min(200, _remaining(deadline))
    scheme = urlsplit(origin).scheme
    secret = b""
    if session is not None:
        _local_origin(origin)
        _require(
            re.fullmatch(r"[A-Za-z0-9_-]{43}", session) is not None,
            "invalid smoke session",
        )
        # A pipe, not argv/env or a temporary cookie/header file. The strict token
        # alphabet cannot escape curl's config quoting or inject another option.
        secret = f'cookie = "coreteams_session={session}"\n'.encode("ascii")
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
            "-" if session is not None else "/dev/null",
            "--write-out",
            "\\n%{http_code}" if session is not None else "%{http_code}",
            "--url",
            origin + path,
        ]
        + (["--config", "-"] if session is not None else []),
        stdin=secret,
        timeout=budget,
        env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty"},
    )
    body, status = (
        output.rsplit(b"\n", 1)
        if session is not None and b"\n" in output
        else (b"", output)
    )
    _require(status == str(expected).encode("ascii"), "HTTP smoke refused")
    _require(len(body) <= HTTP_LIMIT, "HTTP smoke response bound")
    _remaining(deadline)
    return body


def _smoke_json(raw: bytes) -> object:
    try:
        return _json(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid smoke response") from None


def _admin_shape(raw: bytes) -> None:
    body = _object(
        _smoke_json(raw),
        {
            "as_of",
            "period",
            "completeness",
            "totals",
            "without_started_at",
            "active",
            "attention",
        },
    )

    def timestamp(value: object) -> datetime:
        _require(type(value) is str and len(value) <= 40, "admin timestamp shape")
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("admin timestamp shape") from None
        _require(moment.tzinfo is not None, "admin timestamp timezone")
        return moment

    timestamp(body["as_of"])
    _require(body["completeness"] == "complete", "admin smoke shape")

    def period(value: object) -> None:
        data = _object(value, {"start", "end"})
        _require(
            timestamp(data["start"]) < timestamp(data["end"]), "admin period shape"
        )

    period(body["period"])
    totals = _object(body["totals"], {"runs", "failed", "outcome_unknown", "running"})
    _require(
        all(
            type(v) is int and v >= 0
            for v in (*totals.values(), body["without_started_at"])
        ),
        "admin totals shape",
    )
    for name in ("active", "attention"):
        page = _object(
            body[name],
            {"items", "total", "page", "page_size"}
            | ({"period", "no_start"} if name == "attention" else set()),
        )
        _require(
            page["items"] == []
            and type(page["total"]) is int
            and page["total"] >= 0
            and type(page["page"]) is int
            and page["page"] == _SMOKE_PAGE
            and type(page["page_size"]) is int
            and page["page_size"] == 1,
            "admin smoke must contain no user/run rows",
        )
        if name == "attention":
            period(page["period"])
            _require(page["no_start"] is False, "admin attention shape")


def _user_shape(raw: bytes) -> None:
    body = _object(
        _smoke_json(raw),
        {"team_suggestion", "scenarios", "marketer", "promotion_answer"},
    )
    _require(
        all(
            type(body[name]) is str and bool(body[name])
            for name in ("team_suggestion", "promotion_answer")
        ),
        "user smoke shape",
    )

    def text_object(value: object, fields: set[str]) -> None:
        data = _object(value, fields)
        _require(all(type(v) is str for v in data.values()), "scenario text shape")

    text_object(body["marketer"], {"name", "why", "result", "origin"})
    scenarios = body["scenarios"]
    _require(type(scenarios) is list and len(scenarios) == 4, "scenario smoke shape")
    for item, key in zip(
        scenarios, ("course", "book", "business", "other"), strict=True
    ):
        row = _object(
            item, {"key", "label", "roles", "questions", "stages", "suggestions"}
        )
        _require(
            row["key"] == key and type(row["label"]) is str, "scenario smoke shape"
        )
        for name in ("roles", "questions", "stages", "suggestions"):
            _require(
                type(row[name]) is list and len(row[name]) == 3, "scenario smoke shape"
            )
        _require(
            all(type(v) is str for v in row["suggestions"]),
            "scenario suggestions shape",
        )
        for role in row["roles"]:
            text_object(role, {"name", "why", "result", "origin"})
        for stage in row["stages"]:
            text_object(stage, {"short", "title", "result", "next", "time"})
        for question in row["questions"]:
            data = _object(question, {"title", "help", "options", "guide", "copy"})
            _require(
                type(data["options"]) is list
                and all(type(v) is str for v in data["options"]),
                "scenario options shape",
            )
            _require(
                all(type(data[k]) is str for k in ("title", "help", "guide", "copy")),
                "scenario question shape",
            )


def _http_smoke(config: InstalledConfig, *, public: bool, timeout_seconds: int) -> bool:
    deadline = _deadline(timeout_seconds)
    # Re-read protected sessions to allow out-of-band rotation and fail closed.
    sessions = None if public else _SmokeSessions.load(config.smoke_sessions)
    origin = config.public_origin if public else config.private_origin
    paths = (("/", 200), ("/product/", 200)) if public else ()
    for path, status in (*paths, ("/api/auth/me", 401), ("/api/admin/v1/users", 401)):
        _get(origin, path, status, deadline=deadline)
    if sessions is not None:
        _get(origin, "/api/scenarios", 401, deadline=deadline)
        _get(origin, _ADMIN_SMOKE_PATH, 401, deadline=deadline)
        denied = _get(
            origin,
            _ADMIN_SMOKE_PATH,
            403,
            deadline=deadline,
            session=sessions.user_session,
        )
        _require(
            _smoke_json(denied) == {"detail": "Admin role required"},
            "admin denial shape",
        )
        _user_shape(
            _get(
                origin,
                "/api/scenarios",
                200,
                deadline=deadline,
                session=sessions.user_session,
            )
        )
        _admin_shape(
            _get(
                origin,
                _ADMIN_SMOKE_PATH,
                200,
                deadline=deadline,
                session=sessions.admin_session,
            )
        )
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
    # Provisioning errors must fail before ingress gating or application disruption.
    _SmokeSessions.load(config.smoke_sessions)
    paths = dict(config.backup)
    host = K3sHost(
        HostConfig.load(config.host_config),
        backup_paths={name: paths[name] for name in ("home", "data", "s3_pvc")},
    )
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
            timeout_seconds=min(60, _remaining(deadline)),
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

    def backup_preflight() -> None:
        preflight_backup(
            paths["pgdata"],
            paths["home"],
            paths["data"],
            available_memory_bytes=available_memory(),
        )

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
        backup_preflight=backup_preflight,
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
