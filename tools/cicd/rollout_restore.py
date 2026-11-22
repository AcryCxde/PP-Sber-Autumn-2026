"""Installed, root-owned PG16 physical restore rehearsal; never activate from a release.

Only a newly extracted backup-*/restored/pgdata below the protected scratch root
is writable. No Kubernetes API, registry pull, host networking, credentials or
production PGDATA is used. The locally installed k3s/containerd must support the
ctr v1.7 create/info --spec/tasks start contract. Inspect the generated OCI spec
BEFORE starting any process; unsupported runtimes fail closed. Production Linux
x86_64/k3s execution requires an operator-owned rehearsal before activation.

Runtime flag/spec references (public containerd v1.7.28 source):
cmd/ctr/commands/{commands.go,containers/containers.go,run/run_unix.go,
tasks/start.go,tasks/delete.go}; oci/spec.go. A ctr-created private network has
no CNI, veth or routes; assert its namespace path is empty, never --net-host.
PGDATA is mounted at the production path; the pinned Alpine image supplies the
same PG16.15 executable, extensions, musl and locale filesystem. Postgres is
Unix-socket-only, nonroot, read-only queries; no refresh/stamp/migrations.
"""

import json
import os
import platform
import re
import signal
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

IMAGE = "docker.io/library/postgres@sha256:0cefaabd882e42bcb2a2f3dc56d3ce4770560ba62fd7f197f1a2e008fd5731ab"
SCRATCH_ROOT = Path("/var/lib/coreteams-deploy/scratch")
PGDATA = "/var/lib/postgresql/data/pgdata"
RUNTIME = "/restore-runtime"
COUNT_TABLES = ("users", "conversations", "runs", "messages", "events", "artifacts")
# Same physical cluster only: catalog OIDs/node trees are intentionally retained.
# Exclude pg_class storage/statistics changed by a clean restart, not schema flags.
# Hash complete critical rows inside PostgreSQL, never return row/column contents.
# Sorted fixed-length SHA256 row hashes preserve duplicates without order/PII leaks.
# Cap each section at 100000 rows (6.4 MB hash input); overflow returns NULL and
# fails closed. The installed 30s statement/5s lock deadlines also bound work.
# Literal relation list only: no dynamic SQL, psql interpolation or caller SQL.
EVIDENCE_SQL = """WITH catalog_rows AS (
 SELECT 'pg_namespace' AS relation, pg_catalog.to_jsonb(t) AS value FROM pg_catalog.pg_namespace t
 UNION ALL SELECT 'pg_class', pg_catalog.to_jsonb(t) - ARRAY['relfilenode','relpages','reltuples','relallvisible','relfrozenxid','relminmxid'] FROM pg_catalog.pg_class t
 UNION ALL SELECT 'pg_attribute', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_attribute t
 UNION ALL SELECT 'pg_attrdef', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_attrdef t
 UNION ALL SELECT 'pg_constraint', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_constraint t
 UNION ALL SELECT 'pg_index', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_index t
 UNION ALL SELECT 'pg_sequence', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_sequence t
 UNION ALL SELECT 'pg_type', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_type t
 UNION ALL SELECT 'pg_enum', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_enum t
 UNION ALL SELECT 'pg_range', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_range t
 UNION ALL SELECT 'pg_proc', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_proc t
 UNION ALL SELECT 'pg_language', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_language t
 UNION ALL SELECT 'pg_trigger', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_trigger t
 UNION ALL SELECT 'pg_event_trigger', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_event_trigger t
 UNION ALL SELECT 'pg_rewrite', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_rewrite t
 UNION ALL SELECT 'pg_policy', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_policy t
 UNION ALL SELECT 'pg_depend', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_depend t
 UNION ALL SELECT 'pg_extension', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_extension t
 UNION ALL SELECT 'pg_collation', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_collation t
 UNION ALL SELECT 'pg_conversion', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_conversion t
 UNION ALL SELECT 'pg_cast', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_cast t
 UNION ALL SELECT 'pg_operator', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_operator t
 UNION ALL SELECT 'pg_aggregate', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_aggregate t
 UNION ALL SELECT 'pg_statistic_ext', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_statistic_ext t
 UNION ALL SELECT 'pg_ts_config', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_ts_config t
 UNION ALL SELECT 'pg_ts_config_map', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_ts_config_map t
 UNION ALL SELECT 'pg_ts_dict', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_ts_dict t
 UNION ALL SELECT 'pg_ts_parser', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_ts_parser t
 UNION ALL SELECT 'pg_ts_template', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_ts_template t
 UNION ALL SELECT 'pg_opclass', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_opclass t
 UNION ALL SELECT 'pg_opfamily', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_opfamily t
 UNION ALL SELECT 'pg_am', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_am t
 UNION ALL SELECT 'pg_amop', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_amop t
 UNION ALL SELECT 'pg_amproc', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_amproc t
 UNION ALL SELECT 'pg_inherits', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_inherits t
 UNION ALL SELECT 'pg_partitioned_table', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_partitioned_table t
 UNION ALL SELECT 'pg_init_privs', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_init_privs t
 UNION ALL SELECT 'pg_default_acl', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_default_acl t
 UNION ALL SELECT 'pg_transform', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_transform t
 UNION ALL SELECT 'pg_foreign_data_wrapper', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_foreign_data_wrapper t
 UNION ALL SELECT 'pg_foreign_server', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_foreign_server t
 UNION ALL SELECT 'pg_foreign_table', pg_catalog.to_jsonb(t) FROM pg_catalog.pg_foreign_table t
), data_rows AS (
 SELECT 'users' AS relation, pg_catalog.to_jsonb(t) AS value FROM public.users t
 UNION ALL SELECT 'conversations', pg_catalog.to_jsonb(t) FROM public.conversations t
 UNION ALL SELECT 'runs', pg_catalog.to_jsonb(t) FROM public.runs t
 UNION ALL SELECT 'messages', pg_catalog.to_jsonb(t) FROM public.messages t
 UNION ALL SELECT 'events', pg_catalog.to_jsonb(t) FROM public.events t
 UNION ALL SELECT 'artifacts', pg_catalog.to_jsonb(t) FROM public.artifacts t
), normalized_data AS (
 -- jsonb orders object keys. Normalize timestamptz to epoch so session timezone
 -- (source vs scratch) cannot change the fingerprint of an identical row.
 SELECT d.relation, (SELECT pg_catalog.jsonb_object_agg(k.key,
   CASE WHEN a.atttypid='pg_catalog.timestamptz'::pg_catalog.regtype AND k.value<>'null'::jsonb
    THEN pg_catalog.to_jsonb(EXTRACT(EPOCH FROM (k.value #>> '{}')::timestamptz))
    ELSE k.value END)
  FROM pg_catalog.jsonb_each(d.value) k
  JOIN pg_catalog.pg_attribute a ON a.attrelid=pg_catalog.to_regclass('public.' || d.relation)
   AND a.attname=k.key AND a.attnum>0 AND NOT a.attisdropped) AS value
 FROM data_rows d
), sequence_rows AS (
 -- Direct state includes is_called, unlike pg_sequences.last_value (NULL before
 -- first use). These are the two serial sequences of the installed app schema.
 SELECT 'events_id_seq' AS relation, pg_catalog.jsonb_build_object('last_value',last_value,'is_called',is_called) AS value FROM public.events_id_seq
 UNION ALL SELECT 'artifacts_id_seq', pg_catalog.jsonb_build_object('last_value',last_value,'is_called',is_called) FROM public.artifacts_id_seq
), hashes AS (
 SELECT 'catalog' AS section, pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
  pg_catalog.jsonb_build_array(relation,value)::text,'UTF8')),'hex') AS hash FROM catalog_rows
 UNION ALL SELECT relation, pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(value::text,'UTF8')),'hex') FROM normalized_data
 UNION ALL SELECT 'sequences', pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
  pg_catalog.jsonb_build_array(relation,value)::text,'UTF8')),'hex') FROM sequence_rows
), ranked AS (
 SELECT section,hash,pg_catalog.row_number() OVER (PARTITION BY section ORDER BY hash COLLATE "C") AS position FROM hashes
), fingerprints AS (
 SELECT sections.section, CASE WHEN count(r.hash)<=100000 THEN pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(
  COALESCE(pg_catalog.string_agg(r.hash,'' ORDER BY r.hash COLLATE "C") FILTER (WHERE r.position<=100000),''),'UTF8')),'hex') END AS hash
 FROM (VALUES ('catalog'),('users'),('conversations'),('runs'),('messages'),('events'),('artifacts'),('sequences')) sections(section)
 LEFT JOIN ranked r ON r.section=sections.section GROUP BY sections.section
)
SELECT pg_catalog.jsonb_build_object(
 'system_identifier',(SELECT system_identifier::text FROM pg_catalog.pg_control_system()),
 'server_version_num',pg_catalog.current_setting('server_version_num')::integer,
 'revisions',(SELECT pg_catalog.jsonb_agg(version_num ORDER BY version_num) FROM public.alembic_version),
 'fingerprints',pg_catalog.jsonb_build_object(
 'catalog',(SELECT hash FROM fingerprints WHERE section='catalog'),
 'data',(SELECT pg_catalog.jsonb_object_agg(section,hash) FROM fingerprints WHERE section IN ('users','conversations','runs','messages','events','artifacts')),
 'sequences',(SELECT hash FROM fingerprints WHERE section='sequences')),
 'counts',pg_catalog.jsonb_build_object(
 'users',(SELECT count(*) FROM public.users),
 'conversations',(SELECT count(*) FROM public.conversations),
 'runs',(SELECT count(*) FROM public.runs),
 'messages',(SELECT count(*) FROM public.messages),
 'events',(SELECT count(*) FROM public.events),
 'artifacts',(SELECT count(*) FROM public.artifacts)),
 'collation',(SELECT pg_catalog.jsonb_build_object(
 'encoding',pg_catalog.pg_encoding_to_char(encoding),'provider',datlocprovider,
 'collate',datcollate,'ctype',datctype,'icu_locale',daticulocale,
 'icu_rules_md5',pg_catalog.md5(daticurules),'version',datcollversion,
 'actual_version',pg_catalog.pg_database_collation_actual_version(oid))
 FROM pg_catalog.pg_database WHERE datname=pg_catalog.current_database()))"""
PGOPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=5000 -c search_path=pg_catalog"


class RestoreError(ValueError):
    """Safe failure category; never embeds database rows, server logs or paths."""


def _require(condition: bool, message: str = "invalid restore evidence") -> None:
    if not condition:
        raise RestoreError(message)


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def sanitize_evidence(text: str) -> str:
    """Validate/canonicalize both source observation and scratch result, not a stamp."""
    _require(type(text) is str and 0 < len(text) <= 8192)
    try:
        value = json.loads(text, object_pairs_hook=_object)
    except (ValueError, TypeError, RecursionError) as error:
        raise RestoreError("invalid restore evidence") from error
    _require(
        type(value) is dict
        and set(value)
        == {
            "system_identifier",
            "server_version_num",
            "revisions",
            "counts",
            "fingerprints",
            "collation",
        }
    )
    identity = value["system_identifier"]
    _require(
        type(identity) is str
        and re.fullmatch(r"[1-9][0-9]{0,19}", identity) is not None
    )
    _require(int(identity) < 2**64)
    _require(
        type(value["server_version_num"]) is int
        and value["server_version_num"] == 160015
    )
    revisions = value["revisions"]
    _require(type(revisions) is list and 0 < len(revisions) <= 16)
    _require(
        all(
            type(item) is str and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", item) is not None
            for item in revisions
        )
    )
    _require(revisions == sorted(set(revisions)))
    counts = value["counts"]
    _require(type(counts) is dict and set(counts) == set(COUNT_TABLES))
    _require(all(type(item) is int and 0 <= item < 2**63 for item in counts.values()))
    fingerprints = value["fingerprints"]
    _require(
        type(fingerprints) is dict
        and set(fingerprints) == {"catalog", "data", "sequences"}
    )
    data = fingerprints["data"]
    _require(type(data) is dict and set(data) == set(COUNT_TABLES))
    _require(
        all(
            type(item) is str and re.fullmatch(r"[0-9a-f]{64}", item) is not None
            for item in (fingerprints["catalog"], fingerprints["sequences"], *data.values())
        )
    )
    collation = value["collation"]
    _require(
        type(collation) is dict
        and set(collation)
        == {
            "encoding",
            "provider",
            "collate",
            "ctype",
            "icu_locale",
            "icu_rules_md5",
            "version",
            "actual_version",
        }
    )
    for key in ("encoding", "provider", "collate", "ctype"):
        _require(
            type(collation[key]) is str
            and re.fullmatch(r"[a-zA-Z0-9_.@+-]{1,128}", collation[key]) is not None
        )
    for key in ("icu_locale", "version", "actual_version"):
        item = collation[key]
        _require(
            item is None
            or (
                type(item) is str
                and re.fullmatch(r"[a-zA-Z0-9_.@+,;= -]{1,256}", item) is not None
            )
        )
    rules = collation["icu_rules_md5"]
    _require(
        rules is None
        or (type(rules) is str and re.fullmatch(r"[0-9a-f]{32}", rules) is not None)
    )
    _require(collation["provider"] in ("c", "i"))
    _require((collation["provider"] == "i") == (collation["icu_locale"] is not None))
    _require(collation["provider"] == "i" or rules is None)
    _require(collation["version"] == collation["actual_version"])
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Command(Protocol):
    def __call__(self, args: Sequence[str], *, timeout: int) -> str: ...


def _command(args: Sequence[str], *, timeout: int) -> str:
    # No ambient credentials, PG*, kubeconfig, containerd address or user PATH.
    # A separate session lets timeout/interruption reap this CLI process group only.
    try:
        with subprocess.Popen(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        ) as child:
            try:
                output, _ = child.communicate(timeout=timeout)
            except BaseException:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise
            _require(
                child.returncode == 0 and len(output) <= 1024 * 1024,
                "scratch runtime command failed",
            )
            return output
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise RestoreError("scratch runtime command failed") from error


def _protected_pgdata(pgdata: Path, scratch_root: Path) -> None:
    _require(
        pgdata.is_absolute() and scratch_root.is_absolute(), "invalid scratch location"
    )
    _require(
        pgdata.resolve() == pgdata and scratch_root.resolve() == scratch_root,
        "invalid scratch location",
    )
    try:
        relative = pgdata.relative_to(scratch_root)
    except ValueError as error:
        raise RestoreError("invalid scratch location") from error
    _require(
        len(relative.parts) == 3
        and relative.parts[0].startswith("backup-")
        and relative.parts[1:] == ("restored", "pgdata"),
        "invalid scratch location",
    )
    for parent in (scratch_root, pgdata.parent.parent, pgdata.parent, pgdata):
        info = parent.lstat()
        _require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0,
            "unprotected scratch directory",
        )
    device = scratch_root.stat().st_dev
    for directory, dirs, files in os.walk(pgdata, followlinks=False):
        for path in [
            Path(directory),
            *(Path(directory) / name for name in dirs + files),
        ]:
            info = path.lstat()
            _require(
                (
                    stat.S_ISDIR(info.st_mode)
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
                )
                and info.st_uid == os.geteuid()
                and info.st_dev == device
                and not path.is_mount(),
                "unsafe scratch tree",
            )
    _require(
        (pgdata / "PG_VERSION").read_text().strip() == "16"
        and not (pgdata / "postmaster.pid").exists()
        and not (pgdata / "backup_label").exists()
        and not (pgdata / "standby.signal").exists()
        and not (pgdata / "recovery.signal").exists(),
        "scratch backup is not a cold PG16 cluster",
    )


# Trusted shell only, never read scripts/configs from the backup. No TCP even in
# the private network. Startup and shutdown output stay in owned scratch logs.
_SCRIPT = r"""test "$(postgres --version)" = 'postgres (PostgreSQL) 16.15'
pg_controldata "$PGDATA" > /restore-runtime/control
sed -n '/^Database cluster state:/p' /restore-runtime/control | grep -Eq ': *shut down$'
trap 'pg_ctl -D "$PGDATA" -m immediate -w -t 15 stop >/dev/null 2>&1 || true' EXIT
pg_ctl -D "$PGDATA" -l /restore-runtime/server.log -w -t 45 -o '-c config_file=/restore-runtime/postgresql.conf' start >/dev/null 2>&1
psql -X -A -t -v ON_ERROR_STOP=1 -h /restore-runtime -U "$1" -d "$2" -c "$3"
pg_ctl -D "$PGDATA" -m fast -w -t 15 stop >/dev/null 2>&1
trap - EXIT
"""
_CAPS = (
    "CHOWN",
    "DAC_OVERRIDE",
    "FSETID",
    "FOWNER",
    "MKNOD",
    "NET_RAW",
    "SETGID",
    "SETUID",
    "SETFCAP",
    "SETPCAP",
    "NET_BIND_SERVICE",
    "SYS_CHROOT",
    "KILL",
    "AUDIT_WRITE",
)


def _validate_spec(raw: str, *, pgdata: Path, runtime: Path, args: list[str]) -> None:
    try:
        spec = json.loads(raw, object_pairs_hook=_object)
        linux, process = spec["linux"], spec["process"]
        namespaces = linux["namespaces"]
        _require(
            all(
                sum(item.get("type") == kind for item in namespaces) == 1
                for kind in ("network", "pid", "mount", "ipc", "uts")
            )
            and all(not item.get("path") for item in namespaces),
            "unsafe scratch OCI namespaces",
        )
        _require(
            spec["root"]["readonly"] is True
            and process["user"]["uid"] == 70
            and process["user"]["gid"] == 70
            and process["args"] == args
            and not process.get("terminal", False)
            and process.get("noNewPrivileges") is True,
            "unsafe scratch OCI process",
        )
        _require(
            all(not values for values in process.get("capabilities", {}).values()),
            "unsafe scratch capabilities",
        )
        binds = {
            item["destination"]: item
            for item in spec["mounts"]
            if item["type"] == "bind"
        }
        _require(set(binds) == {PGDATA, RUNTIME}, "unsafe scratch OCI mounts")
        for destination, source in ((PGDATA, pgdata), (RUNTIME, runtime)):
            mount = binds[destination]
            _require(
                mount["source"] == str(source)
                and "rbind" not in mount["options"]
                and "rw" in mount["options"]
                and "bind" in mount["options"],
                "unsafe scratch OCI mounts",
            )
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        raise RestoreError("unsafe scratch OCI specification") from error


class RestoreVerifier:
    """Bind only from protected installed host configuration, never request JSON."""

    def __init__(
        self,
        scratch_root: Path = SCRATCH_ROOT,
        *,
        database: str = "coreteams",
        user: str = "coreteams",
        command: Command = _command,
    ) -> None:
        _require(
            all(
                re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", item) is not None
                for item in (database, user)
            ),
            "invalid scratch database identity",
        )
        self.scratch_root, self.database, self.user, self.command = (
            scratch_root,
            database,
            user,
            command,
        )

    def verify_restore(self, pgdata: Path) -> str:
        _require(
            platform.system() == "Linux"
            and platform.machine() == "x86_64"
            and os.geteuid() == 0,
            "scratch runtime requires Linux x86_64 root",
        )
        _protected_pgdata(pgdata, self.scratch_root)
        name = "restore-check-" + uuid.uuid4().hex
        prefix = [
            "/usr/local/bin/k3s",
            "ctr",
            "--address",
            "/run/k3s/containerd/containerd.sock",
            "--namespace",
            "k8s.io",
        ]
        with tempfile.TemporaryDirectory(
            prefix="runtime-", dir=pgdata.parent.parent
        ) as directory:
            runtime = Path(directory)
            try:
                (pgdata / "postgresql.auto.conf").write_text("")
                (runtime / "pg_hba.conf").write_text("local all all trust\n")
                (runtime / "pg_ident.conf").write_text("")
                (runtime / "postgresql.conf").write_text(
                    "data_directory = '" + PGDATA + "'\n"
                    "listen_addresses = ''\nunix_socket_directories = '/restore-runtime'\n"
                    "unix_socket_permissions = 0700\nhba_file = '/restore-runtime/pg_hba.conf'\n"
                    "ident_file = '/restore-runtime/pg_ident.conf'\nssl = off\n"
                    "archive_mode = off\narchive_command = ''\nrestore_command = ''\n"
                    "shared_preload_libraries = ''\nsession_preload_libraries = ''\n"
                    "local_preload_libraries = ''\nmax_worker_processes = 0\n"
                    "default_transaction_read_only = on\n"
                )
                for root in (pgdata, runtime):
                    for current, dirs, files in os.walk(root):
                        os.chown(current, 70, 70)
                        for file in files:
                            os.chown(Path(current) / file, 70, 70)
                args = [
                    "/usr/bin/env",
                    "-i",
                    "PATH=/usr/local/bin:/usr/bin:/bin",
                    "LC_ALL=C",
                    "PGDATA=" + PGDATA,
                    "PGOPTIONS=" + PGOPTIONS,
                    "/bin/sh",
                    "-eu",
                    "-c",
                    _SCRIPT,
                    "restore-check",
                    self.user,
                    self.database,
                    EVIDENCE_SQL,
                ]
                create = [
                    *prefix,
                    "containers",
                    "create",
                    "--snapshotter",
                    "overlayfs",
                    "--read-only",
                    "--user",
                    "70:70",
                ]
                for cap in _CAPS:
                    create.extend(["--cap-drop", "CAP_" + cap])
                for source, destination in ((pgdata, PGDATA), (runtime, RUNTIME)):
                    _require(
                        not any(char in str(source) for char in ",\n\r"),
                        "invalid scratch mount path",
                    )
                    create.extend(
                        [
                            "--mount",
                            f"type=bind,src={source},dst={destination},options=bind:rw",
                        ]
                    )
                self.command([*create, IMAGE, name, *args], timeout=30)
                spec = self.command(
                    [*prefix, "containers", "info", "--spec", name], timeout=15
                )
                _validate_spec(spec, pgdata=pgdata, runtime=runtime, args=args)
                output = self.command([*prefix, "tasks", "start", name], timeout=110)
                return sanitize_evidence(output)
            finally:
                # IDs are fresh and owned by this call. Never kill by process name,
                # list production tasks, restart services or restore the source.
                try:
                    self.command(
                        [*prefix, "tasks", "delete", "--force", name], timeout=20
                    )
                except RestoreError:
                    # Normal synchronous ctr start already reaps its exited task.
                    pass
                self.command([*prefix, "containers", "delete", name], timeout=20)
                # TemporaryDirectory must remain removable by the orchestrator.
                os.chown(runtime, 0, 0)


def verify_restore(pgdata: Path) -> str:
    """Default host shim API for rollout_backup.Host.verify_restore."""
    return RestoreVerifier().verify_restore(pgdata)
