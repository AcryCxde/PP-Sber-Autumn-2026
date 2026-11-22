"""No host daemon or production connection: test the trusted runtime boundary."""

import copy
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tools.cicd import rollout_restore as restore
from tools.cicd.tests.schema_pg_fixture import docker
from tools.cicd.tests.schema_pg_fixture import pg16 as pg16_fixture

pg16 = pg16_fixture


def evidence():
    return {
        "system_identifier": "7560000000000000000",
        "server_version_num": 160015,
        "revisions": ["20261111_0005"],
        "counts": {table: 4 for table in restore.COUNT_TABLES},
        "fingerprints": {
            "catalog": "a" * 64,
            "data": {table: "b" * 64 for table in restore.COUNT_TABLES},
            "sequences": "c" * 64,
        },
        "collation": {
            "encoding": "UTF8",
            "provider": "c",
            "collate": "en_US.utf8",
            "ctype": "en_US.utf8",
            "icu_locale": None,
            "icu_rules_md5": None,
            "version": None,
            "actual_version": None,
        },
    }


def test_evidence_is_canonical_and_rejects_raw_rows():
    raw = evidence()
    assert restore.sanitize_evidence(json.dumps(raw)) == json.dumps(
        raw, sort_keys=True, separators=(",", ":")
    )
    raw["email"] = "PRIVATE-ROW"
    with pytest.raises(
        restore.RestoreError, match="invalid restore evidence"
    ) as failure:
        restore.sanitize_evidence(json.dumps(raw))
    assert "PRIVATE-ROW" not in str(failure.value)


@pytest.mark.parametrize("section", ["catalog", "data", "sequences"])
def test_same_count_fingerprint_drift_is_not_canonicalized_away(section):
    source = evidence()
    scratch = copy.deepcopy(source)
    if section == "data":
        scratch["fingerprints"][section]["users"] = "d" * 64
    else:
        scratch["fingerprints"][section] = "d" * 64
    assert source["counts"] == scratch["counts"]
    assert restore.sanitize_evidence(json.dumps(source)) != restore.sanitize_evidence(
        json.dumps(scratch)
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(server_version_num=170009),
        lambda value: value.update(system_identifier="not a physical identity"),
        lambda value: value.update(revisions=[]),
        lambda value: value.update(revisions=["PRIVATE-ROW!"]),
        lambda value: value["counts"].update(users=True),
        lambda value: value["counts"].pop("runs"),
        lambda value: value["collation"].update(version="153", actual_version="154"),
        lambda value: value["collation"].update(collate="PRIVATE\nROW"),
        lambda value: value.pop("fingerprints"),
        lambda value: value["fingerprints"].update(catalog=None),
        lambda value: value["fingerprints"].update(sequences="A" * 64),
        lambda value: value["fingerprints"].update(sequences="PRIVATE-ROW"),
        lambda value: value["fingerprints"]["data"].pop("messages"),
        lambda value: value["fingerprints"]["data"].update(users="1" * 63),
        lambda value: value["fingerprints"]["data"].update(extra="a" * 64),
        lambda value: value["fingerprints"].update(rows=["PRIVATE-ROW"]),
    ],
)
def test_evidence_fails_closed(change):
    value = evidence()
    change(value)
    with pytest.raises(restore.RestoreError):
        restore.sanitize_evidence(json.dumps(value))


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        "null",
        '{"counts":{},"counts":{}}',
        "x" * 8193,
        "[" * 1500 + "]" * 1500,
    ],
)
def test_invalid_json_is_safe(text):
    with pytest.raises(restore.RestoreError):
        restore.sanitize_evidence(text)


class Runtime:
    """Fake the process boundary, not the PostgreSQL result's validation."""

    def __init__(self):
        self.calls = []
        self.mutate = lambda spec: None
        self.output = json.dumps(evidence())
        self.fail = None
        self.pgdata = None
        self.runtime = None
        self.script = None

    def __call__(self, args: Sequence[str], *, timeout: int) -> str:
        args = list(args)
        assert timeout > 0
        self.calls.append(args)
        assert args[:7] == [
            "/usr/local/bin/k3s",
            "ctr",
            "--address",
            "/run/k3s/containerd/containerd.sock",
            "--namespace",
            "k8s.io",
            args[6],
        ]
        operation = " ".join(args[6:8])
        if operation == self.fail:
            raise restore.RestoreError("scratch runtime command failed")
        if operation == "containers create":
            assert restore.IMAGE in args
            assert "--net-host" not in args and "--cni" not in args
            assert "--privileged" not in args and "--with-ns" not in args
            # ctr containers create selects the checked native x86_64 platform;
            # unlike ctr run it has no --platform option in v1.7.
            assert "--platform" not in args
            assert args[args.index("--snapshotter") + 1] == "overlayfs"
            process_args = args[args.index(restore.IMAGE) + 2 :]
            mounts = []
            for index, arg in enumerate(args):
                if arg != "--mount":
                    continue
                fields = dict(item.split("=", 1) for item in args[index + 1].split(","))
                mounts.append(
                    {
                        "type": fields["type"],
                        "source": fields["src"],
                        "destination": fields["dst"],
                        "options": fields["options"].split(":"),
                    }
                )
            self.pgdata = Path(mounts[0]["source"])
            self.runtime = Path(mounts[1]["source"])
            self.script = process_args[process_args.index("-c") + 1]
            config = (self.runtime / "postgresql.conf").read_text()
            assert "listen_addresses = ''" in config
            assert "shared_preload_libraries = ''" in config
            assert "default_transaction_read_only = on" in config
            assert "include" not in config
            assert (self.pgdata / "postgresql.auto.conf").read_text() == ""
            assert process_args[-1] == restore.EVIDENCE_SQL
            assert "PGOPTIONS=" + restore.PGOPTIONS in process_args
            self.spec = {
                "root": {"readonly": True},
                "linux": {
                    "namespaces": [
                        {"type": kind}
                        for kind in ("network", "pid", "mount", "uts", "ipc")
                    ]
                },
                "process": {
                    "user": {"uid": 70, "gid": 70},
                    "args": process_args,
                    "noNewPrivileges": True,
                    "capabilities": {"bounding": [], "permitted": [], "effective": []},
                },
                "mounts": mounts,
            }
        elif operation == "containers info":
            spec = copy.deepcopy(self.spec)
            self.mutate(spec)
            return json.dumps(spec)
        elif operation == "tasks start":
            return self.output
        return ""


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    # Simulated Linux/root only at the OS boundary; no real daemon/chown/network.
    monkeypatch.setattr(restore.platform, "system", lambda: "Linux")
    monkeypatch.setattr(restore.platform, "machine", lambda: "x86_64")
    actual_uid = os.geteuid()
    monkeypatch.setattr(restore.os, "geteuid", lambda: actual_uid)
    monkeypatch.setattr(restore.os, "chown", lambda *args: None)
    root = tmp_path / "scratch"
    pgdata = root / "backup-owned" / "restored" / "pgdata"
    pgdata.mkdir(parents=True)
    for path in (root, pgdata.parent.parent, pgdata.parent, pgdata):
        path.chmod(0o700)
    (pgdata / "PG_VERSION").write_text("16\n")
    (pgdata / "postgresql.auto.conf").write_text(
        "shared_preload_libraries = 'unsafe'\n"
    )
    # Preserve real owner validation; pretend root only for initial platform gate.
    original = restore._protected_pgdata

    def protected(*args):
        monkeypatch.setattr(restore.os, "geteuid", lambda: actual_uid)
        original(*args)
        monkeypatch.setattr(restore.os, "geteuid", lambda: 0)

    monkeypatch.setattr(restore, "_protected_pgdata", protected)
    monkeypatch.setattr(restore.os, "geteuid", lambda: 0)
    return root, pgdata


def test_owned_runtime_checks_spec_then_queries_then_cleans(scratch):
    root, pgdata = scratch
    runtime = Runtime()
    result = restore.RestoreVerifier(root, command=runtime).verify_restore(pgdata)
    assert result == restore.sanitize_evidence(json.dumps(evidence()))
    assert [args[6:8] for args in runtime.calls] == [
        ["containers", "create"],
        ["containers", "info"],
        ["tasks", "start"],
        ["tasks", "delete"],
        ["containers", "delete"],
    ]
    ids = {args[-1] for args in runtime.calls[1:]}
    assert len(ids) == 1 and next(iter(ids)).startswith("restore-check-")
    assert runtime.runtime is not None and not runtime.runtime.exists()
    assert (
        subprocess.run(
            ["/bin/sh", "-n", "-c", runtime.script], capture_output=True, check=False
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda spec: spec["linux"]["namespaces"][0].update(path="/proc/1/ns/net"),
        lambda spec: spec["linux"]["namespaces"].append(
            {"type": "network", "path": "/proc/1/ns/net"}
        ),
        lambda spec: spec["linux"]["namespaces"].pop(0),
        lambda spec: spec["root"].update(readonly=False),
        lambda spec: spec["process"]["user"].update(uid=0),
        lambda spec: spec["process"]["args"].append("untrusted"),
        lambda spec: spec["process"]["capabilities"].update(bounding=["CAP_NET_ADMIN"]),
        lambda spec: spec["mounts"][0].update(source="/production"),
        lambda spec: spec["mounts"].append(
            {
                "type": "bind",
                "source": "/",
                "destination": "/host",
                "options": ["bind", "rw"],
            }
        ),
    ],
)
def test_unsafe_oci_never_starts_and_is_cleaned(scratch, mutate):
    root, pgdata = scratch
    runtime = Runtime()
    runtime.mutate = mutate
    with pytest.raises(restore.RestoreError):
        restore.RestoreVerifier(root, command=runtime).verify_restore(pgdata)
    assert not any(args[6:8] == ["tasks", "start"] for args in runtime.calls)
    assert runtime.calls[-1][6:8] == ["containers", "delete"]


@pytest.mark.parametrize(
    "failure", ["tasks start", "containers info", "containers create"]
)
def test_runtime_failure_is_not_success(scratch, failure):
    root, pgdata = scratch
    runtime = Runtime()
    runtime.fail = failure
    with pytest.raises(restore.RestoreError):
        restore.RestoreVerifier(root, command=runtime).verify_restore(pgdata)
    assert runtime.calls[-1][6:8] == ["containers", "delete"]


def test_bad_query_output_is_not_returned(scratch):
    root, pgdata = scratch
    runtime = Runtime()
    runtime.output = "PRIVATE-DATABASE-CONTENT"
    with pytest.raises(restore.RestoreError) as failure:
        restore.RestoreVerifier(root, command=runtime).verify_restore(pgdata)
    assert "PRIVATE" not in str(failure.value)
    assert runtime.calls[-1][6:8] == ["containers", "delete"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "outside",
        "symlink",
        "postmaster.pid",
        "backup_label",
        "standby.signal",
        "recovery.signal",
        "version",
        "public",
        "fifo",
    ],
)
def test_unsafe_pgdata_rejected_before_runtime(scratch, tmp_path, unsafe):
    root, pgdata = scratch
    if unsafe == "outside":
        pgdata = tmp_path
    elif unsafe == "symlink":
        (pgdata / "external").symlink_to(tmp_path)
    elif unsafe == "public":
        pgdata.chmod(0o755)
    elif unsafe == "fifo":
        os.mkfifo(pgdata / "fifo")
    elif unsafe == "version":
        (pgdata / "PG_VERSION").write_text("17")
    else:
        (pgdata / unsafe).write_text("")
    runtime = Runtime()
    with pytest.raises(restore.RestoreError):
        restore.RestoreVerifier(root, command=runtime).verify_restore(pgdata)
    assert runtime.calls == []


@pytest.fixture
def evidence_pg(pg16):
    """Only the opt-in, owned PG16.15 container; never host database settings."""
    pg16.execute(
        "CREATE TABLE public.alembic_version(version_num text PRIMARY KEY); "
        "INSERT INTO public.alembic_version VALUES ('20261111_0005'); "
        + " ".join(
            f"CREATE TABLE public.{table} (id integer PRIMARY KEY, payload jsonb, "
            "created_at timestamptz);"
            for table in restore.COUNT_TABLES
            if table not in ("events", "artifacts")
        )
        + "CREATE TABLE public.events (id serial PRIMARY KEY, payload jsonb, created_at timestamptz); "
        "CREATE TABLE public.artifacts (id serial PRIMARY KEY, payload jsonb, created_at timestamptz); "
        "INSERT INTO public.users VALUES (1, '{\"email\":\"PRIVATE-ROW@example.invalid\",\"nested\":{\"b\":2,\"a\":1}}', '2026-01-01T00:00:00Z'); "
        "INSERT INTO public.events (payload) VALUES ('{\"content\":\"PRIVATE-EVENT\"}');"
    )
    return pg16


def read_pg_evidence(pg, timezone="UTC"):
    # Execute exactly the installed statement and scratch read-only PGOPTIONS.
    # The session timezone intentionally varies to test source/scratch equivalence.
    raw = docker(
        "exec", "-e", "PGOPTIONS=" + restore.PGOPTIONS + " -c timezone=" + timezone,
        pg.container, "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", "probe_test", "-c", restore.EVIDENCE_SQL,
    )
    return restore.sanitize_evidence(raw)


def test_pg16_fixed_read_only_evidence_is_deterministic_and_minimized(evidence_pg):
    first = read_pg_evidence(evidence_pg)
    assert first == read_pg_evidence(evidence_pg, "America/New_York")
    value = json.loads(first)
    assert len(first) < 8192
    assert "PRIVATE" not in first and "email" not in first and "created_at" not in first
    assert len(value["fingerprints"]["catalog"]) == 64
    assert value["counts"]["conversations"] == 0
    assert value["fingerprints"]["data"]["conversations"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    # Heap/insertion order and jsonb key order must not change the evidence.
    evidence_pg.execute(
        "INSERT INTO public.messages VALUES (2,'{\"b\":2,\"a\":1}',NULL), (1,NULL,NULL)"
    )
    ordered = read_pg_evidence(evidence_pg)
    evidence_pg.execute(
        "DELETE FROM public.messages; "
        "INSERT INTO public.messages VALUES (1,NULL,NULL),(2,'{\"a\":1,\"b\":2}',NULL)"
    )
    assert read_pg_evidence(evidence_pg) == ordered


@pytest.mark.parametrize(
    ("sql", "section"),
    [
        ("UPDATE public.users SET payload='{\"email\":\"OTHER-PRIVATE\"}' WHERE id=1", "data"),
        ("CREATE INDEX fixture_payload_idx ON public.users ((payload->>'email'))", "catalog"),
        ("ALTER TABLE public.users ALTER COLUMN payload SET DEFAULT '{}'::jsonb", "catalog"),
        ("ALTER TABLE public.users ADD CONSTRAINT fixture_check CHECK (id>0)", "catalog"),
        ("ALTER SEQUENCE public.events_id_seq INCREMENT BY 2", "catalog"),
        ("SELECT pg_catalog.setval('public.events_id_seq',91,true)", "sequences"),
        ("SELECT pg_catalog.setval('public.events_id_seq',1,false)", "sequences"),
        ("SELECT pg_catalog.setval('public.artifacts_id_seq',91,false)", "sequences"),
    ],
)
def test_pg16_same_count_drift_changes_evidence(evidence_pg, sql, section):
    before = json.loads(read_pg_evidence(evidence_pg))
    evidence_pg.execute(sql)
    after = json.loads(read_pg_evidence(evidence_pg))
    assert before["counts"] == after["counts"]
    assert before["system_identifier"] == after["system_identifier"]
    assert before["revisions"] == after["revisions"]
    assert before["fingerprints"][section] != after["fingerprints"][section]
    assert before != after


def test_pg16_fingerprint_limit_fails_closed(evidence_pg):
    evidence_pg.execute(
        "INSERT INTO public.messages (id) SELECT pg_catalog.generate_series(1,100001)"
    )
    with pytest.raises(restore.RestoreError, match="invalid restore evidence"):
        read_pg_evidence(evidence_pg)


def test_pg16_missing_sequence_fails_closed(evidence_pg):
    evidence_pg.execute("DROP SEQUENCE public.artifacts_id_seq CASCADE")
    with pytest.raises(subprocess.CalledProcessError):
        read_pg_evidence(evidence_pg)


def test_wrong_platform_rejected_without_commands(tmp_path):
    runtime = Runtime()
    with pytest.raises(restore.RestoreError, match="Linux x86_64 root"):
        restore.RestoreVerifier(tmp_path, command=runtime).verify_restore(tmp_path)
    assert runtime.calls == []
