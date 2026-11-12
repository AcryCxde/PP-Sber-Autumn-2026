"""Offline release migration evidence on owned disposable PG16; never a deploy API.

The supplied SHA authorizes local migration execution only, not deployment approval.
Only exact Git-tree migration/bridge bytes execute, under Python -I in a child;
no extracted project directory enters sys.path and no application startup imports run.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tools.cicd.schema_probe import (
    CANONICAL_REVISIONS,
    MIGRATION_HEAD,
    UPGRADE_PATHS,
    Baseline,
    CatalogConnection,
    ProbeError,
    TargetMapping,
    _require,
    _sha,
    _unique_json,
    canonical_signature,
    installed_catalog_profile,
    read_catalog,
)

IMAGE = (
    "postgres@sha256:aa6cb33cf4c19c765cefcf48d96176258800870a9a1e930ad126200f9d825139"
)
ROOT = Path(__file__).resolve().parents[2]


def parse_json(raw: bytes) -> dict[str, object]:
    _require(type(raw) is bytes and len(raw) <= 65536)
    try:
        result = json.loads(
            raw,
            object_pairs_hook=_unique_json,
            parse_constant=lambda _: _require(False),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProbeError("invalid offline configuration") from error
    _require(type(result) is dict)
    return result


def _run(args: list[str], *, timeout: int = 30, cwd: Path | None = None) -> bytes:
    try:
        return subprocess.run(
            args, check=True, capture_output=True, timeout=timeout, cwd=cwd
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError("offline baseline operation refused") from error


def _git(repo: Path, *args: str) -> bytes:
    # Replace refs and host Git routing variables must not change SHA authority.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError("release source unavailable") from error


@contextmanager
def release_source(repo: Path, release_sha: str) -> Iterator[Path]:
    """Filter exact locally present commit to regular migration-authority files.

    Archive is data: no extractall, symlinks, project imports or dependencies.
    Verify each byte against ls-tree's blob id, then make files read-only.
    """
    _sha(release_sha)
    commit = (
        _git(repo, "rev-parse", "--verify", release_sha + "^{commit}").decode().strip()
    )
    _require(commit == release_sha)
    tree = _git(
        repo,
        "ls-tree",
        "-rz",
        release_sha,
        "--",
        "backend/migrations/versions",
        "backend/migration_bridge.py",
    )
    expected = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = name.decode("utf-8")
        _require(mode == "100644" and kind == "blob")
        _require(
            path == "backend/migration_bridge.py"
            or (
                Path(path).parent.as_posix() == "backend/migrations/versions"
                and path.endswith(".py")
            )
        )
        expected[path] = oid
    _require("backend/migration_bridge.py" in expected and len(expected) == 18)
    archive = _git(
        repo, "archive", "--format=tar", release_sha, "--", *sorted(expected)
    )
    with tempfile.TemporaryDirectory(prefix="schema-release-") as directory:
        root = Path(directory)
        seen = set()
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as source:
                for member in source:
                    if member.isdir():
                        continue
                    _require(
                        member.isfile()
                        and member.name in expected
                        and member.name not in seen
                    )
                    data = source.extractfile(member).read()
                    blob = hashlib.sha1(
                        b"blob " + str(len(data)).encode() + b"\0" + data
                    ).hexdigest()
                    _require(blob == expected[member.name])
                    path = root / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    path.chmod(0o400)
                    seen.add(member.name)
            _require(seen == set(expected))
        except (OSError, tarfile.TarError, UnicodeError, ValueError) as error:
            raise ProbeError("release source refused") from error
        yield root


# Trusted runner text lives outside the archive. Its Alembic env deliberately
# omits release env.py (which imports runtime models/config). Only revision DDL
# and the release bridge execute. -I excludes cwd, PYTHONPATH and user packages.
ENV = """from alembic import context
connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=None)
with context.begin_transaction():
    context.run_migrations()
"""
RUNNER = """
import asyncio, importlib.util, pathlib, sys
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
source, authority, port, database, scenario = sys.argv[1:]
spec = importlib.util.spec_from_file_location("release_bridge", pathlib.Path(source)/"backend/migration_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
config = Config()
config.set_main_option("script_location", authority)
config.set_main_option("version_locations", str(pathlib.Path(source)/"backend/migrations/versions"))
script = ScriptDirectory.from_config(config)
expected = @VALUE@
revisions = tuple(r.revision for r in reversed(list(script.walk_revisions())))
if revisions != expected or script.get_heads() != [@VALUE@] or bridge.HEAD != @VALUE@:
    raise RuntimeError("unsupported release migration chain")
async def run():
    engine = create_async_engine("postgresql+asyncpg://migrator@127.0.0.1:"+port+"/"+database,
        connect_args={"timeout":5, "server_settings":{"lock_timeout":"1000", "statement_timeout":"5000"}})
    try:
        async with engine.begin() as connection:
            def migrate(conn):
                config.attributes["connection"] = conn
                if scenario == "deployed:20261030_0013":
                    bridge._apply(conn, bridge._paths("deployed", "20261030_0013"))
                    command.ensure_version(config)
                    conn.execute(sa.text("INSERT INTO alembic_version VALUES ('20261030_0013')"))
                    bridge.bridge(conn, apply=True, backup_verified=True)
                else:
                    if scenario != "clean":
                        if scenario not in expected:
                            raise RuntimeError("unsupported historical revision")
                        command.upgrade(config, scenario)
                        bridge.preflight_canonical(conn)
                    command.upgrade(config, @VALUE@)
                if list(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()) != [@VALUE@]:
                    raise RuntimeError("unexpected migration head")
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()
asyncio.run(run())
""".replace("@VALUE@", repr(CANONICAL_REVISIONS), 1).replace(
    "@VALUE@", repr(MIGRATION_HEAD)
)


def migrate_release(source: Path, port: int, database: str, scenario: str) -> None:
    _require(type(port) is int and 1 <= port <= 65535)
    _require(database.startswith("baseline_") and database[9:].isalnum())
    _require(scenario in ("clean", *UPGRADE_PATHS))
    with tempfile.TemporaryDirectory(prefix="schema-runner-") as directory:
        authority = Path(directory)
        (authority / "env.py").write_text(ENV)
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "SYSTEMROOT", "TMPDIR"}
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    RUNNER,
                    str(source),
                    str(authority),
                    str(port),
                    database,
                    scenario,
                ],
                cwd=authority,
                env=env,
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProbeError("release migration refused") from error


def _docker(*args: str, timeout: int = 30) -> str:
    return _run(["docker", *args], timeout=timeout).decode().strip()


@contextmanager
def owned_postgres() -> Iterator[tuple[str, int]]:
    """Pinned cached image, random loopback port, cleanup only this container."""
    container = None
    try:
        _docker("image", "inspect", IMAGE)
        container = "schema-baseline-" + uuid.uuid4().hex
        _docker(
            "run",
            "--rm",
            "--detach",
            "--pull=never",
            "--name",
            container,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        )
        _docker(
            "exec",
            container,
            "sh",
            "-c",
            "i=0; until pg_isready -h 127.0.0.1 -U postgres -t 1 >/dev/null; do "
            "i=$((i+1)); test $i -lt 40 || exit 1; sleep 0.2; done",
            timeout=15,
        )
        port = int(_docker("port", container, "5432/tcp").rsplit(":", 1)[1])
        _require(_psql(container, "SHOW server_version_num") == "160015")
        _psql(
            container, "CREATE ROLE migrator LOGIN; CREATE ROLE probe LOGIN NOINHERIT;"
        )
        yield container, port
    finally:
        if container is not None:
            primary_error = sys.exc_info()[1]
            try:
                _docker("stop", "--time", "2", container, timeout=15)
            except (ProbeError, OSError, subprocess.SubprocessError):
                if primary_error is None:
                    raise
                primary_error.add_note("owned container cleanup also failed")


def _psql(container: str, sql: str, database: str = "postgres") -> str:
    return _docker(
        "exec",
        container,
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "postgres",
        "-d",
        database,
        "-At",
        "-c",
        sql,
    )


def collect_baseline(
    source: Path, release_sha: str, mapping: TargetMapping, container: str, port: int
) -> Baseline:
    """No mapping endpoint/credential is ever passed to a connection."""
    _require(mapping.profile == installed_catalog_profile())
    signatures = {}
    for scenario in ("clean", *UPGRADE_PATHS):
        database = "baseline_" + uuid.uuid4().hex
        _psql(container, f"CREATE DATABASE {database} OWNER migrator")
        try:
            _psql(
                container,
                f"REVOKE CREATE, TEMP ON DATABASE {database} FROM PUBLIC; "
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
                "GRANT CREATE ON SCHEMA public TO migrator",
                database,
            )
            migrate_release(source, port, database, scenario)
            connection = CatalogConnection(
                "127.0.0.1", port, database, "probe", mapping.profile
            )
            snapshot = asyncio.run(read_catalog(connection))
            _require(
                tuple(
                    sorted(
                        row["identity"][0] for row in snapshot["inventory"]["schemas"]
                    )
                )
                == mapping.schemas
            )
            signatures[scenario] = canonical_signature(snapshot, mapping.profile)
            _require(signatures[scenario] == signatures["clean"])
        finally:
            _psql(container, f"DROP DATABASE {database} WITH (FORCE)")
    return Baseline(
        release_sha,
        mapping.profile,
        mapping.schemas,
        signatures["clean"],
        mapping.signature(),
        signatures["clean"],
        tuple(
            sorted((key, value) for key, value in signatures.items() if key != "clean")
        ),
    )


def generate(
    repo: Path, release_sha: str, mapping: TargetMapping, output: Path
) -> Baseline:
    _sha(release_sha)
    _require(not output.exists() and not output.is_symlink())
    _require(mapping.profile == installed_catalog_profile())
    with (
        release_source(repo, release_sha) as source,
        owned_postgres() as (container, port),
    ):
        baseline = collect_baseline(source, release_sha, mapping, container, port)
    payload = (
        json.dumps(
            baseline.to_data(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )
    _require(len(payload) <= 65536)
    _publish(output, payload)
    return baseline


def _publish(output: Path, payload: bytes) -> None:
    """Publish complete durable bytes, never replace any existing destination.

    The private inode is fsynced before linking. Directory fsync is required;
    failure after linking reports refusal but leaves a COMPLETE output (never
    deletes a possibly replaced racer). Operators may inspect it before retry.
    """
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".schema-baseline-", dir=output.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Same-filesystem link atomically creates the name or refuses EEXIST,
        # including dangling symlinks and racers. No check-then-rename window.
        os.link(temporary, output)
        temporary.unlink()
        temporary = None
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise ProbeError("baseline output refused") from error
    finally:
        if temporary is not None:
            primary_error = sys.exc_info()[1]
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                if primary_error is None:
                    raise
                primary_error.add_note("private output cleanup also failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--mapping", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        with args.mapping.open("rb") as stream:
            mapping = TargetMapping.parse(parse_json(stream.read(65537)))
        generate(ROOT, args.release_sha, mapping, args.output)
    except (ProbeError, OSError):
        print("offline baseline refused", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
