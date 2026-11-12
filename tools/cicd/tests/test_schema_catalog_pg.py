"""Real PostgreSQL evidence, explicitly opted in; no production connections."""

import asyncio

import pytest

from tools.cicd.schema_probe import (
    CatalogConnection,
    ProbeError,
    canonical_signature,
    installed_catalog_profile,
    read_catalog,
)
from tools.cicd.tests.schema_pg_fixture import pg16 as pg16_fixture

pg16 = pg16_fixture


def test_actual_canonical_inventory(pg16):
    pg16.migrate()

    profile = installed_catalog_profile()
    mapping = CatalogConnection("127.0.0.1", pg16.port, "probe_test", "probe", profile)
    snapshot = asyncio.run(read_catalog(mapping))
    assert canonical_signature(snapshot, profile)
    assert len(snapshot["inventory"]["sequences"]) == 2
    assert not {k: v for k, v in snapshot["unsupported"].items() if v}
    inventory = snapshot["inventory"]
    sequences = {tuple(s["identity"]): s for s in inventory["sequences"]}
    for table in ("events", "artifacts"):
        sequence = sequences[("public", f"{table}_id_seq")]
        assert sequence["owned_by"] == ["public", table, "id"]
        assert sequence["ownership_dependency"] == "a"
        assert [
            sequence[k] for k in ("start", "increment", "min", "max", "cache", "cycle")
        ] == [1, 1, 1, 2147483647, 1, False]
        assert sequence["type"] == ["pg_catalog", "int4"]
    types = {c["formatted_type"] for c in inventory["columns"]}
    assert {"timestamp with time zone", "numeric(12,6)", "json"} <= types
    partial = next(
        i
        for i in inventory["indexes"]
        if i["identity"][-1] == "uq_runs_one_active_per_conversation"
    )
    assert partial["unique"] and partial["predicate"]
    pg16.execute("DROP TABLE artifacts CASCADE")
    assert signature(pg16) != canonical_signature(snapshot, profile)


def test_catalog_visibility_subscription_oracle(pg16):
    """Non-secret subscription columns must expose even another owner's objects."""
    pg16.execute(
        "CREATE SUBSCRIPTION hidden_subscription CONNECTION 'host=127.0.0.1' "
        "PUBLICATION nonexistent WITH (connect=false)"
    )

    async def inspect(role):
        connection = await pg16.connect(role)
        try:
            return await connection.fetchval(
                "SELECT count(subdbid) FROM pg_catalog.pg_subscription"
            )
        finally:
            await connection.close(timeout=1)

    assert asyncio.run(inspect("postgres")) == 1
    assert asyncio.run(inspect("probe")) == 1
    with pytest.raises(ProbeError):
        signature(pg16)


def signature(pg):
    profile = installed_catalog_profile()
    mapping = CatalogConnection("127.0.0.1", pg.port, "probe_test", "probe", profile)
    return canonical_signature(asyncio.run(read_catalog(mapping)), profile)


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE sample ALTER COLUMN moment TYPE timestamp",
        "ALTER TABLE sample ALTER COLUMN amount TYPE numeric(12,5)",
        "ALTER TABLE sample ALTER COLUMN amount SET DEFAULT 2.5",
        "DROP TABLE sample",
        "CREATE TABLE extra(id integer)",
        "CREATE SCHEMA extra",
        "DROP INDEX partial_sample; CREATE INDEX partial_sample ON sample(id) WHERE id>1",
        "CREATE INDEX included ON sample(id) INCLUDE (amount)",
        "CREATE INDEX expression_sample ON sample((id+1))",
        "CREATE INDEX descending_sample ON sample(id DESC NULLS LAST)",
        "CREATE UNIQUE INDEX unique_sample ON sample(amount) NULLS NOT DISTINCT",
        "ALTER SEQUENCE sample_id_seq INCREMENT BY 2",
        "ALTER SEQUENCE sample_id_seq CACHE 2",
        "ALTER TABLE sample ADD CHECK (amount>0) NOT VALID",
        "ALTER TABLE sample ADD CONSTRAINT uq UNIQUE(amount) DEFERRABLE INITIALLY DEFERRED",
        "CREATE INDEX opts ON sample(id) WITH (fillfactor=70,deduplicate_items=off)",
        'ALTER TABLE sample ADD COLUMN label text; CREATE INDEX collated ON sample(label COLLATE "C" text_pattern_ops)',
        "CREATE TABLE parent(id integer PRIMARY KEY); ALTER TABLE sample ADD FOREIGN KEY(id) REFERENCES parent(id) ON UPDATE CASCADE ON DELETE SET NULL",
    ],
)
def test_supported_drift(pg16, ddl):
    pg16.execute(
        "CREATE TABLE sample(id serial PRIMARY KEY, moment timestamptz, amount numeric(12,6)); CREATE INDEX partial_sample ON sample(id) WHERE id>0"
    )
    before = signature(pg16)
    pg16.execute(ddl)
    assert signature(pg16) != before


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE SEQUENCE standalone",
        "CREATE TABLE unsupported(id integer GENERATED ALWAYS AS IDENTITY)",
        "CREATE TABLE unsupported(id integer, x integer GENERATED ALWAYS AS (id+1) STORED)",
        "CREATE VIEW unsupported AS SELECT 1",
        "CREATE MATERIALIZED VIEW unsupported AS SELECT 1",
        "CREATE UNLOGGED TABLE unsupported(id integer)",
        "CREATE TABLE unsupported(id integer) PARTITION BY RANGE(id)",
        "CREATE TYPE unsupported AS ENUM ('x')",
        "CREATE DOMAIN unsupported AS integer",
        "CREATE COLLATION unsupported (provider=libc,locale='C')",
        "CREATE FUNCTION unsupported() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "CREATE TABLE unsupported(id integer); ALTER TABLE unsupported ENABLE ROW LEVEL SECURITY",
        "CREATE EXTENSION hstore",
        "CREATE PUBLICATION unsupported",
        "CREATE SERVER unsupported FOREIGN DATA WRAPPER postgres_fdw",
        "CREATE STATISTICS unsupported ON id,amount FROM sample",
        "ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO probe",
        "CREATE INDEX unsupported ON sample USING hash(id)",
        "ALTER SEQUENCE sample_id_seq OWNED BY NONE",
        "ALTER SEQUENCE sample_id_seq OWNED BY sample.amount",
        "ALTER TABLE sample ALTER COLUMN id SET DEFAULT 1",
    ],
)
def test_unsupported_rejected(pg16, ddl):
    pg16.execute("CREATE TABLE sample(id serial PRIMARY KEY, amount numeric)")
    if "postgres_fdw" in ddl:
        pg16.execute("CREATE EXTENSION postgres_fdw")
    pg16.execute(ddl)
    with pytest.raises(ProbeError):
        signature(pg16)


def test_sequence_current_value_excluded(pg16):
    pg16.execute("CREATE TABLE sample(id serial PRIMARY KEY)")
    before = signature(pg16)
    pg16.execute("SELECT nextval('sample_id_seq') FROM generate_series(1,10)")
    assert signature(pg16) == before


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE parent(a integer,b integer, UNIQUE(a,b)); CREATE TABLE child(x integer, FOREIGN KEY(x,x) REFERENCES parent(a,b))",
        "CREATE TABLE selfref(a integer PRIMARY KEY,b integer REFERENCES selfref(a))",
    ],
)
def test_real_repeated_fk_and_multiple_dependency_types(pg16, ddl):
    pg16.execute(ddl)
    assert signature(pg16)


def test_equivalent_different_oids(pg16):
    ddl = "CREATE TABLE sample(id serial PRIMARY KEY, value text); CREATE INDEX expr ON sample(lower(value)) WHERE value IS NOT NULL"
    pg16.execute(ddl)
    oid = pg16.execute("SELECT 'sample'::regclass::oid")
    before = signature(pg16)
    pg16.execute("DROP TABLE sample; " + ddl)
    assert pg16.execute("SELECT 'sample'::regclass::oid") != oid
    assert signature(pg16) == before


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE parent(id integer); CREATE TABLE child() INHERITS(parent)",
        "CREATE TABLE unsupported(id integer); CREATE RULE nope AS ON INSERT TO unsupported DO INSTEAD NOTHING",
        "CREATE TABLE unsupported(id integer); CREATE POLICY nope ON unsupported USING(true)",
        "CREATE FUNCTION nope() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; CREATE TRIGGER nope BEFORE INSERT ON sample FOR EACH ROW EXECUTE FUNCTION nope()",
        "CREATE FUNCTION nope() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN END $$; CREATE EVENT TRIGGER nope ON ddl_command_start EXECUTE FUNCTION nope()",
        "CREATE TEXT SEARCH CONFIGURATION nope (COPY=pg_catalog.english)",
        "CREATE TEXT SEARCH DICTIONARY nope (TEMPLATE=pg_catalog.simple)",
        "CREATE OPERATOR === (LEFTARG=integer,RIGHTARG=integer,FUNCTION=int4eq)",
        "CREATE CONVERSION nope FOR 'UTF8' TO 'LATIN1' FROM utf8_to_iso8859_1",
        "CREATE CAST (integer AS text) WITH INOUT AS ASSIGNMENT",
        "CREATE LANGUAGE plpython3u",
        "CREATE TEXT SEARCH PARSER nope (START=prsd_start, GETTOKEN=prsd_nexttoken, END=prsd_end, LEXTYPES=prsd_lextype)",
        "CREATE TEXT SEARCH TEMPLATE nope (LEXIZE=dsimple_lexize)",
        "CREATE TRANSFORM FOR integer LANGUAGE sql (FROM SQL WITH FUNCTION int4send(integer))",
        "CREATE SEQUENCE extra; ALTER TABLE sample ALTER COLUMN id SET DEFAULT nextval('extra')",
        "CREATE FOREIGN DATA WRAPPER nope; CREATE SERVER no_server FOREIGN DATA WRAPPER nope; CREATE FOREIGN TABLE foreign_sample(id integer) SERVER no_server",
        "CREATE INDEX nope ON sample(id); UPDATE pg_class SET reloptions=ARRAY['unknown=1'] WHERE oid='nope'::regclass",
        "CREATE FOREIGN DATA WRAPPER nope",
        "CREATE FOREIGN DATA WRAPPER nope; CREATE SERVER no_server FOREIGN DATA WRAPPER nope; CREATE USER MAPPING FOR postgres SERVER no_server",
        "CREATE INDEX nope ON sample(id); UPDATE pg_index SET indisvalid=false WHERE indexrelid='nope'::regclass",
        "CREATE INDEX nope ON sample(id); UPDATE pg_index SET indisready=false WHERE indexrelid='nope'::regclass",
        "CREATE INDEX nope ON sample(id); UPDATE pg_index SET indislive=false WHERE indexrelid='nope'::regclass",
    ],
)
def test_more_unsupported_families(pg16, ddl):
    pg16.execute("CREATE TABLE sample(id integer, amount numeric)")
    if "plpython3u" in ddl:
        # The pinned image has no PL/Python binary: inject only its catalog
        # declaration on our disposable server, never execute the handler.
        ddl = "CREATE LANGUAGE custom_language HANDLER plpgsql_call_handler"
    if "CREATE TRANSFORM" in ddl:
        ddl = "INSERT INTO pg_transform(oid,trftype,trflang,trffromsql,trftosql) SELECT 999999, 'integer'::regtype, oid,0,0 FROM pg_language WHERE lanname='sql'"
    pg16.execute(ddl)
    with pytest.raises(ProbeError):
        signature(pg16)


def test_catalog_visibility_hidden_schema(pg16):
    pg16.execute(
        "CREATE SCHEMA hidden; REVOKE ALL ON SCHEMA hidden FROM PUBLIC; CREATE TABLE hidden.invisible(id integer)"
    )
    before = signature(pg16)
    pg16.execute("ALTER TABLE hidden.invisible ADD COLUMN extra text")
    assert signature(pg16) != before


def test_catalog_visibility_capability_denied(pg16):
    pg16.execute("REVOKE SELECT ON pg_catalog.pg_constraint FROM PUBLIC")
    with pytest.raises(ProbeError):
        signature(pg16)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sample",
        "INSERT INTO sample VALUES (1)",
        "UPDATE sample SET id=2",
        "DELETE FROM sample",
        "DROP TABLE sample",
        "CREATE TABLE public.forbidden(id integer)",
        "CREATE TEMP TABLE forbidden(id integer)",
        "SELECT nextval('sample_id_seq')",
        "SELECT last_value FROM sample_id_seq",
        "SELECT subconninfo FROM pg_catalog.pg_subscription",
        "SELECT rolpassword FROM pg_catalog.pg_authid",
    ],
)
def test_catalog_visibility_role_denials(pg16, sql):
    pg16.execute("CREATE TABLE sample(id serial PRIMARY KEY)")

    async def denied():
        import asyncpg

        connection = await pg16.connect("probe")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await connection.execute(sql)
        finally:
            await connection.close(timeout=1)

    asyncio.run(denied())


def test_catalog_visibility_user_mapping_oracle(pg16):
    pg16.execute(
        "CREATE FOREIGN DATA WRAPPER nope; CREATE SERVER no_server FOREIGN DATA WRAPPER nope; CREATE USER MAPPING FOR postgres SERVER no_server"
    )

    async def inspect(role):
        connection = await pg16.connect(role)
        try:
            return await connection.fetchval("SELECT count(umid) FROM pg_user_mappings")
        finally:
            await connection.close(timeout=1)

    assert asyncio.run(inspect("probe")) == asyncio.run(inspect("postgres")) == 1


def test_catalog_visibility_full_class_oracle(pg16):
    import json
    from pathlib import Path

    pg16.migrate()
    query = (Path(__file__).parents[1] / "schema_catalog.sql").read_text()
    profile = installed_catalog_profile()

    async def inspect(role):
        connection = await pg16.connect(role)
        try:
            async with connection.transaction(readonly=True):
                await connection.execute("SET LOCAL search_path=pg_catalog")
                return json.loads(
                    await connection.fetchval(query, json.dumps(profile.to_data()))
                )
        finally:
            await connection.close(timeout=1)

    admin = asyncio.run(inspect("postgres"))
    probe = asyncio.run(inspect("probe"))
    assert admin["inventory"] == probe["inventory"]
    # These are role-safety refusals rather than object coverage counters.
    for name in ("pg_authid", "pg_database", "pg_auth_members"):
        admin["unsupported"].pop(name)
        probe["unsupported"].pop(name)
    assert admin["unsupported"] == probe["unsupported"]


def test_hostile_deparse_settings(pg16):
    pg16.execute(
        "CREATE TABLE sample(id serial PRIMARY KEY, moment timestamptz DEFAULT now())"
    )
    before = signature(pg16)
    pg16.execute(
        "ALTER ROLE probe SET search_path='public'; ALTER ROLE probe SET TimeZone='Asia/Tokyo'; ALTER ROLE probe SET standard_conforming_strings=off"
    )
    assert signature(pg16) == before


def test_snapshot_lock_refusal(pg16):
    pg16.execute("CREATE TABLE sample(id integer)")

    async def run():
        admin = await pg16.connect()
        try:
            async with admin.transaction():
                await admin.execute("LOCK TABLE sample IN ACCESS EXCLUSIVE MODE")
                profile = installed_catalog_profile()
                mapping = CatalogConnection(
                    "127.0.0.1", pg16.port, "probe_test", "probe", profile
                )
                # Catalog deparse may not require a table lock: a coherent old
                # snapshot is valid; a bounded refusal is valid, mixed is not.
                try:
                    snapshot = await read_catalog(mapping)
                    assert canonical_signature(snapshot, profile)
                except ProbeError:
                    pass
        finally:
            await admin.close(timeout=1)

    asyncio.run(run())


def test_snapshot_readonly_and_connection_loss(pg16):
    async def run():
        import asyncpg

        connection = await pg16.connect()
        killer = await pg16.connect()
        try:
            async with connection.transaction(readonly=True):
                with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                    await connection.execute("CREATE TABLE forbidden(id integer)")
            pid = connection.get_server_pid()
            await killer.execute("SELECT pg_terminate_backend($1)", pid)
            with pytest.raises(
                (
                    asyncpg.PostgresError,
                    asyncpg.InterfaceError,
                    asyncpg.InternalClientError,
                )
            ):
                await connection.fetchval("SELECT 1")
        finally:
            await connection.close(timeout=1)
            await killer.close(timeout=1)

    asyncio.run(run())


@pytest.mark.parametrize("delayed_connect", [False, True])
def test_snapshot_midfetch_connection_loss(pg16, monkeypatch, delayed_connect):
    """Start observation at actual fetch, not at variable connection startup."""
    import asyncpg

    real_connect = asyncpg.connect

    async def run():
        fetch_entered = asyncio.Event()
        release_fetch = asyncio.Event()
        probe_pid = None

        class ObservedConnection:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            async def fetchval(self, *args, **kwargs):
                nonlocal probe_pid
                probe_pid = self.connection.get_server_pid()
                fetch_entered.set()
                await release_fetch.wait()
                return await self.connection.fetchval(*args, **kwargs)

        async def observed_connect(**kwargs):
            if delayed_connect:
                # Controlled startup delay longer than the old 0.8s observation
                # window; does not alter the real server's 1s lock timeout.
                connected = asyncio.Event()
                handle = asyncio.get_running_loop().call_later(0.9, connected.set)
                try:
                    await connected.wait()
                finally:
                    handle.cancel()
            return ObservedConnection(await real_connect(**kwargs))

        admin = await pg16.connect()
        task = None
        try:
            async with admin.transaction():
                await admin.execute(
                    "LOCK TABLE pg_catalog.pg_constraint IN ACCESS EXCLUSIVE MODE"
                )
                profile = installed_catalog_profile()
                mapping = CatalogConnection(
                    "127.0.0.1", pg16.port, "probe_test", "probe", profile
                )
                monkeypatch.setattr(asyncpg, "connect", observed_connect)
                task = asyncio.create_task(read_catalog(mapping))
                # Startup is bounded separately, before the SQL lock wait starts.
                await asyncio.wait_for(fetch_entered.wait(), timeout=5)
                release_fetch.set()
                async with asyncio.timeout(0.8):
                    while True:
                        pid = await admin.fetchval(
                            "SELECT a.pid FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid "
                            "WHERE a.pid=$1 AND a.usename='probe' AND a.datname='probe_test' "
                            "AND l.relation='pg_catalog.pg_constraint'::regclass AND NOT l.granted",
                            probe_pid,
                        )
                        if pid is not None:
                            break
                        # Each awaited server query yields to the probe; no sleeps
                        # or timing assumption decides when termination happens.
                    await admin.execute("SELECT pg_terminate_backend($1)", pid)
                    with pytest.raises(
                        ProbeError, match="^catalog read refused$"
                    ) as caught:
                        await task
                    assert caught.value.__cause__ is not None
        finally:
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, ProbeError):
                    pass
            await admin.close(timeout=1)

    asyncio.run(run())


def test_database_default_collation_context(pg16):
    # The pinned Alpine image has no locale(1); inventory actual catalog providers.
    assert (
        pg16.execute(
            "SELECT count(*)>0 FROM pg_collation WHERE collprovider='i' AND colliculocale='und'"
        )
        == "t"
    )
    signatures = []
    contexts = []
    orderings = []
    for database, provider in (
        ("locale_c", "libc"),
        ("locale_icu", "icu"),
        ("locale_c_copy", "libc"),
    ):
        options = (
            "LOCALE_PROVIDER libc LOCALE 'C'"
            if provider == "libc"
            else "LOCALE_PROVIDER icu LOCALE 'C' ICU_LOCALE 'und'"
        )
        pg16.execute(
            f"CREATE DATABASE {database} TEMPLATE template0 OWNER migrator {options}",
            "postgres",
        )
        pg16.execute(
            "REVOKE CREATE, TEMP ON DATABASE " + database + " FROM PUBLIC; "
            "CREATE TABLE sample(value text UNIQUE)",
            database,
        )
        profile = installed_catalog_profile()
        raw = asyncio.run(
            read_catalog(
                CatalogConnection("127.0.0.1", pg16.port, database, "probe", profile)
            )
        )
        signatures.append(canonical_signature(raw, profile))
        contexts.append(
            pg16.execute(
                "SELECT json_build_array(datlocprovider, datcollate, datctype, "
                "daticulocale, daticurules, datcollversion, "
                "pg_database_collation_actual_version(oid)) FROM pg_database "
                "WHERE datname=current_database()",
                database,
            )
        )
        orderings.append(
            pg16.execute(
                "SELECT string_agg(value,',' ORDER BY value) "
                "FROM (VALUES ('a'::text),('Z'::text)) v(value)",
                database,
            )
        )
    assert orderings[0] != orderings[1]
    assert contexts[0] != contexts[1]
    assert signatures[0] != signatures[1]
    assert signatures[0] == signatures[2]


def test_stale_temporary_namespaces_excluded(pg16):
    before = signature(pg16)
    pg16.execute("CREATE TEMP TABLE transient(value text)")
    assert (
        int(
            pg16.execute(
                "SELECT count(*) FROM pg_namespace WHERE pg_is_other_temp_schema(oid)"
            )
        )
        >= 2
    )
    assert signature(pg16) == before


@pytest.mark.parametrize(
    "schema", ["pg_business", "pg_temp_imitation", "pg_toast_temp_imitation"]
)
def test_persistent_pg_prefix_objects_not_exempt(pg16, schema):
    # Only a superuser can make reserved-prefix imitators; still fail closed.
    pg16.execute(
        "CREATE SCHEMA original; CREATE TABLE original.unknown(value text); "
        f"UPDATE pg_namespace SET nspname='{schema}' WHERE nspname='original'"
    )
    if schema == "pg_business":
        before = signature(pg16)
        pg16.execute(f"ALTER TABLE {schema}.unknown ADD COLUMN extra integer")
        assert signature(pg16) != before
    else:
        with pytest.raises(ProbeError):
            signature(pg16)


def test_database_collation_stale_version_refused(pg16):
    pg16.execute(
        "UPDATE pg_database SET datcollversion='stale' WHERE datname=current_database()"
    )
    with pytest.raises(ProbeError):
        signature(pg16)


def test_active_temporary_objects_still_refused(pg16):
    async def run():
        admin = await pg16.connect()
        try:
            await admin.execute("CREATE TEMP TABLE transient(value text)")
            profile = installed_catalog_profile()
            raw = await read_catalog(
                CatalogConnection(
                    "127.0.0.1", pg16.port, "probe_test", "probe", profile
                )
            )
            with pytest.raises(ProbeError, match="unsupported catalog objects"):
                canonical_signature(raw, profile)
        finally:
            await admin.close(timeout=1)

    asyncio.run(run())


def test_quoted_serial_identity(pg16):
    pg16.execute(
        'CREATE SCHEMA "a\'b"; CREATE TABLE "a\'b"."имя" ("x.y" serial PRIMARY KEY)'
    )
    assert signature(pg16)
