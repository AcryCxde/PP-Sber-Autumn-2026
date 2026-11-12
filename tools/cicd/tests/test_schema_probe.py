"""Pure catalog contract; no database, application imports or SQL file required."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from tools.cicd.schema_probe import (
    CATALOG_CLASSES,
    INVENTORY,
    Facts,
    ProbeError,
    Profile,
    Snapshot,
    canonical_signature,
)


@pytest.fixture
def snapshot():
    profile = Profile(query_sha256="a" * 64)
    snapshot = {
        "profile": profile.to_data(),
        "facts": {
            "database": "coreteams",
            "current_user": "probe",
            "session_user": "probe",
            "server_version_num": 160015,
            "server_address": "127.0.0.1",
            "server_port": 5432,
        },
        "database_collation": {
            "encoding": "UTF8",
            "provider": "c",
            "collate": "C",
            "ctype": "C",
            "icu_locale": None,
            "icu_rules": None,
            "version": None,
            "actual_version": None,
        },
        "inventory": {name: [] for name in INVENTORY},
        "unsupported": {name: 0 for name in CATALOG_CLASSES},
        "counts": dict.fromkeys(INVENTORY, 0),
    }
    return snapshot


@pytest.mark.parametrize(
    "field,value",
    [
        ("encoding", "LATIN1"),
        ("collate", "C.UTF-8"),
        ("ctype", "C.UTF-8"),
    ],
)
def test_database_collation_semantics_hashed(snapshot, field, value):
    changed = deepcopy(snapshot)
    changed["database_collation"][field] = value
    assert signature(changed) != signature(snapshot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "unknown"),
        ("provider", "i"),
        ("encoding", ""),
        ("collate", None),
        ("ctype", True),
        ("icu_locale", "und"),
        ("icu_rules", ""),
        ("version", "stale"),
        ("actual_version", "new"),
    ],
)
def test_database_collation_invalid_refused(snapshot, field, value):
    snapshot["database_collation"][field] = value
    with pytest.raises(ProbeError):
        signature(snapshot)


def test_database_collation_icu_context_hashed(snapshot):
    changed = deepcopy(snapshot)
    changed["database_collation"].update(
        provider="i", icu_locale="und", version="153.120", actual_version="153.120"
    )
    assert signature(changed) != signature(snapshot)
    other = deepcopy(changed)
    other["database_collation"]["icu_rules"] = "&a < b"
    assert signature(other) != signature(changed)
    other = deepcopy(changed)
    other["database_collation"].update(version="154", actual_version="154")
    assert signature(other) != signature(changed)


def test_canonical_contract_empty(snapshot):
    assert canonical_signature(snapshot, Profile("a" * 64)).startswith(
        "pg_catalog_v1:sha256:"
    )


def signature(snapshot):
    return canonical_signature(snapshot, Profile("a" * 64))


def add(snapshot, section, record):
    snapshot["inventory"][section].append(record)
    snapshot["counts"][section] += 1


@pytest.fixture
def populated(snapshot):
    add(snapshot, "schemas", {"identity": ["public"]})
    add(snapshot, "tables", {"identity": ["public", "items"], "access_method": "heap"})
    for position, name in enumerate(("id", "value"), 1):
        add(
            snapshot,
            "columns",
            {
                "identity": ["public", "items", name],
                "position": position,
                "type": ["pg_catalog", "int4"],
                "typmod": -1,
                "formatted_type": "integer",
                "collation": None,
                "not_null": True,
                "default": None,
            },
        )
    return snapshot


def test_canonical_object_order(populated):
    add(populated, "tables", {"identity": ["public", "other"], "access_method": "heap"})
    changed = deepcopy(populated)
    changed["inventory"]["tables"].reverse()
    assert signature(changed) == signature(populated)
    changed["inventory"]["columns"].reverse()
    assert signature(changed) != signature(populated)


@pytest.mark.parametrize("value", ["a.b", 'a"b', "a\nb", "имя", "a:b"])
def test_canonical_component_identity(snapshot, value):
    add(snapshot, "tables", {"identity": [value, "x"], "access_method": "heap"})
    changed = deepcopy(snapshot)
    changed["inventory"]["tables"][0]["identity"] = ["x", value]
    assert signature(changed) != signature(snapshot)


@pytest.mark.parametrize("literal", ["'a b'", "'a  b'", "'a\nb'", "'юникод'", "'a:b'"])
def test_canonical_literals_verbatim(populated, literal):
    changed = deepcopy(populated)
    changed["inventory"]["columns"][0]["default"] = literal
    assert signature(changed) != signature(populated)


@pytest.mark.parametrize(
    "field,value",
    [
        ("database", "other"),
        ("current_user", "other"),
        ("session_user", "other"),
        ("server_address", "::1"),
        ("server_port", 5433),
    ],
)
def test_canonical_runtime_facts_excluded(snapshot, field, value):
    changed = deepcopy(snapshot)
    changed["facts"][field] = value
    assert signature(changed) == signature(snapshot)


@pytest.mark.parametrize(
    "path,value",
    [
        (("oid",), 12),
        (("inventory", "oid"), 42),
        (("facts", "server_port"), True),
        (("facts", "server_port"), 0),
        (("facts", "server_port"), float("nan")),
        (("facts", "database"), ""),
        (("facts", "current_user"), []),
        (("facts", "server_address"), "hostname"),
        (("facts", "server_version_num"), 160016),
        (("profile", "server_version_num"), 160016),
        (("profile", "inventory_version"), True),
        (("profile", "name"), "other"),
        (("profile", "query_sha256"), "b" * 64),
        (("profile", "settings", "TimeZone"), "Europe/Moscow"),
        (("counts", "tables"), 1),
        (("counts", "schemas"), True),
        (("counts", "extra"), 0),
        (("unsupported", "extra"), 0),
        (("unsupported", "pg_class"), True),
        (("unsupported", "pg_class"), -1),
        (("unsupported", "pg_class"), 1),
        (("inventory", "tables"), {}),
    ],
)
def test_canonical_contract_rejects(snapshot, path, value):
    target = snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ProbeError):
        signature(snapshot)


@pytest.mark.parametrize("section", INVENTORY)
def test_canonical_missing_counts(snapshot, section):
    del snapshot["counts"][section]
    with pytest.raises(ProbeError):
        signature(snapshot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("oid", 123),
        ("position", True),
        ("position", 0),
        ("typmod", float("nan")),
        ("not_null", 1),
        ("identity", ["public.items", "id"]),
        ("identity", ["public", "items", ""]),
        ("identity", ["public", "items", "\ud800"]),
    ],
)
def test_canonical_column_shape_rejects(populated, field, value):
    populated["inventory"]["columns"][0][field] = value
    with pytest.raises(ProbeError):
        signature(populated)


def test_canonical_duplicate_identity(populated):
    add(populated, "columns", deepcopy(populated["inventory"]["columns"][0]))
    with pytest.raises(ProbeError):
        signature(populated)


@pytest.mark.parametrize(
    "changes",
    [
        {"server_version_num": True},
        {"server_version_num": 160016},
        {"inventory_version": True},
        {"inventory_version": 1},
        {"query_sha256": "xyz"},
        {"name": "other"},
        {"settings": list(Profile("a" * 64).settings)},
    ],
)
def test_profile_direct_construction_rejects(changes):
    with pytest.raises(ProbeError):
        replace(Profile("a" * 64), **changes)


@pytest.fixture
def complete(populated):
    add(
        populated,
        "constraints",
        {
            "identity": ["public", "items", "pk_items"],
            "kind": "p",
            "columns": [["public", "items", "id"], ["public", "items", "value"]],
            "referenced_table": None,
            "referenced_columns": [],
            "match": None,
            "on_update": None,
            "on_delete": None,
            "deferrable": False,
            "deferred": False,
            "validated": True,
            "no_inherit": False,
            "definition": "PRIMARY KEY (id, value)",
        },
    )
    add(
        populated,
        "indexes",
        {
            "identity": ["public", "pk_items"],
            "table": ["public", "items"],
            "method": "btree",
            "unique": True,
            "nulls_not_distinct": False,
            "keys": [
                {
                    "definition": name,
                    "column": ["public", "items", name],
                    "collation": None,
                    "opclass": ["pg_catalog", "int4_ops"],
                    "descending": False,
                    "nulls_first": False,
                }
                for name in ("id", "value")
            ],
            "include": [],
            "expressions": None,
            "predicate": "id > 0",
            "options": {"fillfactor": 90},
            "valid": True,
            "ready": True,
            "live": True,
        },
    )
    add(
        populated,
        "sequences",
        {
            "identity": ["public", "items_id_seq"],
            "type": ["pg_catalog", "int4"],
            "start": 1,
            "increment": 1,
            "min": 1,
            "max": 2147483647,
            "cache": 1,
            "cycle": False,
            "owned_by": ["public", "items", "id"],
            "ownership_dependency": "a",
            "default": "nextval('public.items_id_seq'::regclass)",
        },
    )
    add(
        populated,
        "dependency_edges",
        {
            "identity": [
                "sequences",
                ["public", "items_id_seq"],
                "columns",
                ["public", "items", "id"],
            ],
            "dependency": "a",
        },
    )
    return populated


@pytest.mark.parametrize(
    "section,field",
    [
        ("indexes", "keys"),
        ("constraints", "columns"),
    ],
)
def test_canonical_ordered_keys(complete, section, field):
    before = signature(complete)
    complete["inventory"][section][0][field].reverse()
    assert signature(complete) != before


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("indexes", "method", "hash"),
        ("indexes", "valid", False),
        ("indexes", "ready", False),
        ("indexes", "live", False),
        ("indexes", "options", {"unknown": 1}),
        ("indexes", "options", {"fillfactor": True}),
        ("sequences", "ownership_dependency", "i"),
        ("sequences", "current_value", 1),
        ("constraints", "kind", "unknown"),
        ("dependency_edges", "dependency", "unknown"),
    ],
)
def test_canonical_supported_shape(complete, section, field, value):
    complete["inventory"][section][0][field] = value
    with pytest.raises(ProbeError):
        signature(complete)


@pytest.mark.parametrize("section", INVENTORY)
def test_canonical_all_records_closed(complete, section):
    complete["inventory"][section][0]["unknown"] = 1
    with pytest.raises(ProbeError):
        signature(complete)


@pytest.mark.parametrize("section", INVENTORY)
def test_canonical_all_duplicates(complete, section):
    add(complete, section, deepcopy(complete["inventory"][section][0]))
    with pytest.raises(ProbeError):
        signature(complete)


def test_canonical_nested_unknown_key_rejects(complete):
    complete["inventory"]["indexes"][0]["keys"][0]["oid"] = 23
    with pytest.raises(ProbeError):
        signature(complete)


def test_canonical_malformed_frozen_rejects(snapshot):
    parsed = Snapshot.parse(snapshot)
    with pytest.raises(ProbeError):
        replace(parsed, inventory=(((["mutable"],),),) + parsed.inventory[1:])


def test_canonical_dependency_types_same_endpoints(complete):
    edge = deepcopy(complete["inventory"]["dependency_edges"][0])
    edge["dependency"] = "n"
    add(complete, "dependency_edges", edge)
    before = signature(complete)
    complete["inventory"]["dependency_edges"].reverse()
    assert signature(complete) == before
    complete["inventory"]["dependency_edges"].pop()
    complete["counts"]["dependency_edges"] -= 1
    assert signature(complete) != before


def test_canonical_fk_repeated_referencing_column(complete):
    constraint = complete["inventory"]["constraints"][0]
    constraint.update(
        kind="f",
        columns=[["public", "items", "id"], ["public", "items", "id"]],
        referenced_table=["public", "parent"],
        referenced_columns=[["public", "parent", "x"], ["public", "parent", "y"]],
        definition="FOREIGN KEY (id, id) REFERENCES parent(x, y)",
    )
    before = signature(complete)
    constraint["columns"][1] = ["public", "items", "value"]
    assert signature(complete) != before


@pytest.mark.parametrize(
    "kind,field", [("p", "columns"), ("u", "columns"), ("f", "referenced_columns")]
)
def test_canonical_other_constraint_duplicates_reject(complete, kind, field):
    constraint = complete["inventory"]["constraints"][0]
    constraint["kind"] = kind
    constraint[field] = [["public", "items", "id"]] * 2
    with pytest.raises(ProbeError):
        signature(complete)


def test_canonical_complete_roundtrip(complete):
    assert len(signature(complete)) == len("pg_catalog_v1:sha256:") + 64
    parsed = Snapshot.parse(complete)
    assert replace(parsed) == parsed


def test_frozen_values(snapshot):
    parsed = Snapshot.parse(snapshot)
    with pytest.raises(FrozenInstanceError):
        parsed.profile.name = "changed"
    with pytest.raises(ProbeError):
        replace(parsed, inventory=((),))
    with pytest.raises(ProbeError):
        replace(parsed.facts, server_port=True)
    assert isinstance(parsed.facts, Facts)
    snapshot["facts"]["database"] = "changed"
    assert parsed.facts.database == "coreteams"


@pytest.fixture
def catalog_driver(monkeypatch, snapshot):
    """Only the external driver boundary is faked; asyncio deadlines are real."""
    import json
    import sys
    from types import SimpleNamespace

    from tools.cicd.schema_probe import installed_catalog_profile

    class DriverError(Exception):
        pass

    class Connection:
        def __init__(self):
            self.calls = []
            self.closed = False
            self.terminated = False
            self.failure = None
            self.barrier = None
            self.entered = None
            snapshot["profile"] = installed_catalog_profile().to_data()
            snapshot["facts"]["database"] = "coreteams"
            self.raw = json.dumps(snapshot)

        async def step(self, name):
            self.calls.append(name)
            if self.failure == name:
                raise DriverError("secret password SQL literal")
            if self.barrier == name:
                self.entered.set()
                await __import__("asyncio").Future()

        def transaction(self, **kwargs):
            self.transaction_options = kwargs
            return self

        async def start(self):
            await self.step("begin")

        async def commit(self):
            await self.step("transaction_exit")

        async def __aenter__(self):
            await self.start()

        async def __aexit__(self, *args):
            await self.step("transaction_exit")
            if getattr(self, "stall_rollback", False):
                await __import__("asyncio").Future()

        async def execute(self, query):
            await self.step(query)

        async def fetchval(self, query, profile, **kwargs):
            self.query = query
            self.fetch_options = kwargs
            await self.step("fetch")
            return self.raw

        async def close(self, **kwargs):
            self.close_options = kwargs
            await self.step("close")
            self.closed = True

        def terminate(self):
            self.terminated = True
            self.closed = True

        def is_closed(self):
            return self.closed

    connection = Connection()

    async def connect(**kwargs):
        connection.connect_options = kwargs
        await connection.step("connect")
        return connection

    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=connect,
            PostgresError=DriverError,
            InterfaceError=DriverError,
            InternalClientError=DriverError,
        ),
    )
    return connection


def catalog_mapping():
    from tools.cicd.schema_probe import CatalogConnection, installed_catalog_profile

    return CatalogConnection(
        "127.0.0.1", 5432, "coreteams", "probe", installed_catalog_profile()
    )


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_probe_limits_exact_utf8_bound(catalog_driver, extra_bytes):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    # Valid JSON with legal trailing whitespace exercises the exact transport bound.
    raw = catalog_driver.raw
    catalog_driver.raw = raw + " " * (4194304 - len(raw.encode("utf-8")) + extra_bytes)
    if extra_bytes:
        with pytest.raises(ProbeError):
            asyncio.run(read_catalog(catalog_mapping()))
    else:
        assert asyncio.run(read_catalog(catalog_mapping()))


def test_probe_limits_settings(catalog_driver):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    assert asyncio.run(read_catalog(catalog_mapping()))
    assert catalog_driver.connect_options["timeout"] == 5
    assert catalog_driver.fetch_options["timeout"] == 5
    assert catalog_driver.transaction_options == {
        "isolation": "read_committed",
        "readonly": True,
    }
    assert "SET LOCAL lock_timeout = '1s'" in catalog_driver.calls
    assert "SET LOCAL statement_timeout = '5s'" in catalog_driver.calls
    assert catalog_driver.calls.count("fetch") == 1
    assert catalog_driver.closed


@pytest.mark.parametrize(
    "stage",
    [
        "connect",
        "begin",
        "SET LOCAL search_path = 'pg_catalog'",
        "fetch",
        "transaction_exit",
        "close",
    ],
)
def test_probe_limits_driver_errors(catalog_driver, stage):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    catalog_driver.failure = stage
    with pytest.raises(ProbeError, match="^catalog read refused$") as caught:
        asyncio.run(read_catalog(catalog_mapping()))
    assert caught.value.__cause__ is not None
    if stage != "connect":
        assert catalog_driver.closed


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "{",
        '{"overflow":true}',
        '{"profile":1,"profile":2}',
        "[" * 2000 + "]" * 2000,
        '"' + "ю" * 2097152 + '"',
    ],
)
def test_probe_limits_malformed_wire(catalog_driver, raw):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    catalog_driver.raw = raw
    with pytest.raises(ProbeError, match="^catalog read refused$"):
        asyncio.run(read_catalog(catalog_mapping()))
    assert catalog_driver.closed


@pytest.mark.parametrize(
    "stage",
    [
        "connect",
        "begin",
        "SET LOCAL search_path = 'pg_catalog'",
        "fetch",
        "transaction_exit",
        "close",
    ],
)
def test_probe_limits_external_cancel(catalog_driver, stage):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    async def run():
        catalog_driver.barrier = stage
        catalog_driver.entered = asyncio.Event()
        task = asyncio.create_task(read_catalog(catalog_mapping()))
        await asyncio.wait_for(catalog_driver.entered.wait(), timeout=1)
        task.cancel()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if stage != "connect":
            assert catalog_driver.closed
            assert catalog_driver.terminated

    asyncio.run(run())


@pytest.mark.parametrize("external_cancel", [False, True])
def test_probe_limits_stalled_rollback(catalog_driver, monkeypatch, external_cancel):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    original_timeout = asyncio.timeout_at

    async def run():
        catalog_driver.stall_rollback = True
        catalog_driver.barrier = "fetch"
        catalog_driver.entered = asyncio.Event()
        monkeypatch.setattr(
            asyncio,
            "timeout_at",
            lambda deadline: original_timeout(
                min(deadline, asyncio.get_running_loop().time() + 0.02)
            ),
        )
        task = asyncio.create_task(read_catalog(catalog_mapping()))
        await catalog_driver.entered.wait()
        if external_cancel:
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.15)
        if task not in done:
            task.cancel()
            # Cancel again releases the deliberately stalled fake rollback.
            await asyncio.gather(task, return_exceptions=True)
            pytest.fail("catalog cleanup outlived wall/caller cancellation")
        if external_cancel:
            with pytest.raises(asyncio.CancelledError):
                task.result()
        else:
            with pytest.raises(ProbeError):
                task.result()
        assert catalog_driver.terminated

    asyncio.run(run())


def test_probe_limits_tls_verifies_hostname(catalog_driver, monkeypatch):
    import asyncio
    import ssl

    from tools.cicd.schema_probe import (
        CatalogConnection,
        installed_catalog_profile,
        read_catalog,
    )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    seen = []
    monkeypatch.setattr(
        ssl, "create_default_context", lambda **kwargs: seen.append(kwargs) or context
    )
    mapping = CatalogConnection(
        "database.example.org",
        5432,
        "coreteams",
        "probe",
        installed_catalog_profile(),
        "/reviewed/ca.pem",
    )
    asyncio.run(read_catalog(mapping))
    assert seen == [{"cafile": "/reviewed/ca.pem"}]
    assert catalog_driver.connect_options["ssl"].check_hostname
    assert catalog_driver.connect_options["ssl"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("stage", ["begin", "fetch", "transaction_exit", "close"])
def test_probe_limits_wall_includes_close(catalog_driver, monkeypatch, stage):
    import asyncio

    from tools.cicd.schema_probe import read_catalog

    timeout_at = asyncio.timeout_at

    async def run():
        # Replace only the configured budget; use real loop scheduling/cancellation.
        monkeypatch.setattr(
            asyncio,
            "timeout_at",
            lambda deadline: timeout_at(
                min(deadline, asyncio.get_running_loop().time() + 0.02)
            ),
        )
        catalog_driver.barrier = stage
        catalog_driver.entered = asyncio.Event()
        start = asyncio.get_running_loop().time()
        with pytest.raises(ProbeError):
            await read_catalog(catalog_mapping())
        assert asyncio.get_running_loop().time() - start < 0.1
        if stage != "close":
            assert "close" not in catalog_driver.calls
        assert catalog_driver.terminated

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["example.org", "192.0.2.1", "0.0.0.0"])
def test_probe_limits_plaintext_off_loopback(endpoint):
    from tools.cicd.schema_probe import CatalogConnection, installed_catalog_profile

    with pytest.raises(ProbeError):
        CatalogConnection(endpoint, 5432, "db", "probe", installed_catalog_profile())
