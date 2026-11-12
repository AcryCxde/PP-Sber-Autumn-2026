"""PG16 catalog contract and bounded read shell; driver imports remain lazy.

SQL produces a JSON object with EXACTLY these six keys (inventory_version=2):
* database_collation: exact PG16 pg_database encoding, provider, collate, ctype,
  ICU locale/rules, recorded version and actual provider version. This portable
  schema context IS hashed; stale recorded/actual version mismatches refuse.
  Runtime identity Facts remain excluded. Empty temporary namespaces recognized
  by PostgreSQL do not enter inventory; active temporary objects remain outside
  the supported persistent-schema grammar and fail the closed catalog audit.
* profile: Profile.to_data(); query_sha256 is supplied by the trusted caller.
  No SQL file is read here. Binding that digest to installed SQL belongs to Task 2.
* facts: database, current_user, session_user, server_version_num, server_address
  (numeric IPv4/IPv6), server_port. All are required; names/address are nonempty
  strings, port is 1..65535. Version must match profile. Facts are validated but
  EXCLUDED from the portable hash; endpoint/role verification belongs to the shell.
* inventory: exactly INVENTORY sections, arrays of objects described by _SHAPES.
  Names are JSON component arrays (schema=[schema], relation=[schema,name],
  column=[schema,table,column]); NEVER joined delimiter strings or catalog OIDs.
  Each record's `identity` is unique in its section, except dependency_edges:
  edge uniqueness and sorting use (identity, dependency), permitting different
  dependency types between the same endpoints; identical edges still reject.
  The wire edge identity remains the four-component endpoint array below.
  Columns additionally have
  positive `position`; table-local column order is their input order. Other
  object arrays are sorted by identity. Ordered key/include/reference arrays
  retain their order. Definitions/expressions/literals are preserved verbatim.
* unsupported: exactly CATALOG_CLASSES, each a nonnegative integer count of
  unsupported objects/dependencies found in that catalog class. Any nonzero
  count prevents a signature. Zero means the SQL audit succeeded, NOT that an
  unreadable catalog was treated as empty. Capability checks are Task 2/3 work.
* counts: exactly INVENTORY, nonnegative integer lengths of complete arrays.

_SHAPES is also the exact SQL handoff record contract. `name` below means a
nonempty string; `text?` permits null or any string (including empty SQL text).
`identityN` is an array of N names, `identityN[]` an ordered array thereof.
These arrays reject repeated identities except FK referencing `columns` (kind=f),
where PostgreSQL permits repeats. FK `referenced_columns` still reject repeats.
The contract rejects unknown/missing fields at every level, bool-as-int, floats,
raw OIDs, duplicate identities and incomplete counts. Raw duplicate JSON keys
must be rejected by the future transport parser BEFORE passing an object here.
No object API can recover duplicate keys already discarded by a JSON decoder.
No rows, sequence current values, owners/ACLs, storage or statistics are inputs.
This bounded format is not a universal PostgreSQL schema representation.
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import ssl
import stat
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.cicd.deploy_server import Clearance, SchemaEvidence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias


class ProbeError(ValueError):
    """Refusal with a fixed message, never input names, SQL or credentials."""


def _require(condition: bool) -> None:
    if not condition:
        raise ProbeError("invalid catalog snapshot or profile")


SETTINGS = (
    ("search_path", "pg_catalog"),
    ("standard_conforming_strings", "on"),
    ("TimeZone", "UTC"),
    ("DateStyle", "ISO, YMD"),
    ("IntervalStyle", "postgres"),
    ("extra_float_digits", "3"),
)
CATALOG_CLASSES = (
    "pg_namespace",
    "pg_class",
    "pg_attribute",
    "pg_attrdef",
    "pg_type",
    "pg_constraint",
    "pg_index",
    "pg_am",
    "pg_opclass",
    "pg_opfamily",
    "pg_collation",
    "pg_inherits",
    "pg_partitioned_table",
    "pg_sequence",
    "pg_depend",
    "pg_extension",
    "pg_proc",
    "pg_trigger",
    "pg_rewrite",
    "pg_policy",
    "pg_publication",
    "pg_publication_rel",
    "pg_publication_namespace",
    "pg_subscription",
    "pg_subscription_rel",
    "pg_event_trigger",
    "pg_foreign_table",
    "pg_foreign_server",
    "pg_foreign_data_wrapper",
    "pg_user_mapping",
    "pg_conversion",
    "pg_operator",
    "pg_cast",
    "pg_transform",
    "pg_language",
    "pg_ts_config",
    "pg_ts_config_map",
    "pg_ts_dict",
    "pg_ts_parser",
    "pg_ts_template",
    "pg_statistic_ext",
    "pg_default_acl",
    "pg_init_privs",
    "pg_shdepend",
    "pg_database",
    "pg_authid",
    "pg_auth_members",
)


@dataclass(frozen=True)
class Profile:
    query_sha256: str
    name: str = "pg_catalog_v1"
    server_version_num: int = 160015
    inventory_version: int = 2
    settings: tuple[tuple[str, str], ...] = SETTINGS

    def __post_init__(self) -> None:
        _require(
            type(self.query_sha256) is str
            and re.fullmatch(r"[0-9a-f]{64}", self.query_sha256) is not None
            and self.name == "pg_catalog_v1"
            and type(self.server_version_num) is int
            and self.server_version_num == 160015
            and type(self.inventory_version) is int
            and self.inventory_version == 2
            and type(self.settings) is tuple
            and all(type(pair) is tuple for pair in self.settings)
            and self.settings == SETTINGS
        )

    def to_data(self) -> dict[str, object]:
        return {
            "name": self.name,
            "server_version_num": self.server_version_num,
            "inventory_version": self.inventory_version,
            "query_sha256": self.query_sha256,
            "settings": dict(self.settings),
        }

    @classmethod
    def parse(cls, raw: object) -> "Profile":
        data = _object(raw, set(cls("0" * 64).to_data()))
        settings = _object(data["settings"], {key for key, _ in SETTINGS})
        _require(settings == dict(SETTINGS))
        _require(type(data["query_sha256"]) is str)
        _require(type(data["name"]) is str)
        _require(type(data["server_version_num"]) is int)
        _require(type(data["inventory_version"]) is int)
        return cls(
            query_sha256=data["query_sha256"],
            name=data["name"],
            server_version_num=data["server_version_num"],
            inventory_version=data["inventory_version"],
        )


MIGRATION_HEAD = "20261111_0005"
CANONICAL_REVISIONS = (
    "20260929_0001",
    "20260929_0002",
    "20260929_0003",
    "20260929_0004",
    "20260929_0005",
    "20261012_0006",
    "20261027_0007",
    "20261027_0008",
    "20261027_0009",
    "20261029_0010",
    "20261030_0011",
    "20261030_0012",
    "20261111_0001",
    "20261111_0002",
    "20261111_0003",
    "20261111_0004",
    MIGRATION_HEAD,
)
UPGRADE_PATHS = (*CANONICAL_REVISIONS, "deployed:20261030_0013")


def _sha(raw: object) -> str:
    _require(type(raw) is str and re.fullmatch(r"[0-9a-f]{40}", raw) is not None)
    _require(raw != "0" * 40)
    return raw


def _schemas(raw: object) -> tuple[str, ...]:
    _require(type(raw) is list and bool(raw))
    values = tuple(_name(value) for value in raw)
    _require(values == tuple(sorted(set(values))))
    return values


def _digest(data: object) -> str:
    return hashlib.sha256(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class TargetMapping:
    """Reviewed configuration identity only; no connection or observed-facts claim."""

    endpoint: str
    port: int
    ca_file: str
    cluster_identity: str
    database: str
    role: str
    schemas: tuple[str, ...]
    profile: Profile
    schema: int = 1

    def __post_init__(self) -> None:
        _require(type(self.schema) is int and self.schema == 1)
        for value in (
            self.endpoint,
            self.ca_file,
            self.cluster_identity,
            self.database,
            self.role,
        ):
            _name(value)
        _require(Path(self.ca_file).is_absolute())
        _require(type(self.port) is int and 1 <= self.port <= 65535)
        _require(type(self.schemas) is tuple)
        _require(_schemas(list(self.schemas)) == self.schemas)
        _require(type(self.profile) is Profile)

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "endpoint": self.endpoint,
            "port": self.port,
            "ca_file": self.ca_file,
            "cluster_identity": self.cluster_identity,
            "database": self.database,
            "role": self.role,
            "schemas": list(self.schemas),
            "profile": self.profile.to_data(),
        }

    def signature(self) -> str:
        return "sha256:" + _digest(self.to_data())

    @classmethod
    def parse(cls, raw: object) -> "TargetMapping":
        data = _object(
            raw,
            {
                "schema",
                "endpoint",
                "port",
                "ca_file",
                "cluster_identity",
                "database",
                "role",
                "schemas",
                "profile",
            },
        )
        return cls(
            **{
                **data,
                "schemas": _schemas(data["schemas"]),
                "profile": Profile.parse(data["profile"]),
            }
        )


@dataclass(frozen=True)
class Baseline:
    """Expected offline evidence; metadata is not operator release approval."""

    release_sha: str
    profile: Profile
    schemas: tuple[str, ...]
    expected_signature: str
    target_mapping_hash: str
    clean_signature: str
    upgrade_signatures: tuple[tuple[str, str], ...]
    migration_head: str = MIGRATION_HEAD
    schema: int = 1

    def __post_init__(self) -> None:
        _require(type(self.schema) is int and self.schema == 1)
        _sha(self.release_sha)
        _require(type(self.profile) is Profile and type(self.schemas) is tuple)
        _require(_schemas(list(self.schemas)) == self.schemas)
        _require(self.migration_head == MIGRATION_HEAD)
        _require(
            type(self.expected_signature) is str
            and re.fullmatch(
                r"pg_catalog_v1:sha256:[0-9a-f]{64}", self.expected_signature
            )
            is not None
        )
        _require(
            type(self.target_mapping_hash) is str
            and re.fullmatch(r"sha256:[0-9a-f]{64}", self.target_mapping_hash)
            is not None
        )
        _require(self.clean_signature == self.expected_signature)
        _require(type(self.upgrade_signatures) is tuple)
        _require(
            all(
                type(pair) is tuple and len(pair) == 2
                for pair in self.upgrade_signatures
            )
        )
        _require(
            tuple(pair[0] for pair in self.upgrade_signatures)
            == tuple(sorted(UPGRADE_PATHS))
        )
        _require(
            all(pair[1] == self.expected_signature for pair in self.upgrade_signatures)
        )

    def bind(self, release_sha: str, mapping: TargetMapping) -> None:
        _require(type(mapping) is TargetMapping)
        _require(_sha(release_sha) == self.release_sha)
        _require(self.profile == mapping.profile)
        _require(self.schemas == mapping.schemas)
        _require(self.target_mapping_hash == mapping.signature())

    def to_data(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "release_sha": self.release_sha,
            "profile": self.profile.to_data(),
            "schemas": list(self.schemas),
            "expected_signature": self.expected_signature,
            "target_mapping_hash": self.target_mapping_hash,
            "clean_signature": self.clean_signature,
            "upgrade_signatures": dict(self.upgrade_signatures),
            "migration_head": self.migration_head,
        }

    @classmethod
    def parse(cls, raw: object) -> "Baseline":
        data = _object(
            raw,
            {
                "schema",
                "release_sha",
                "profile",
                "schemas",
                "expected_signature",
                "target_mapping_hash",
                "clean_signature",
                "upgrade_signatures",
                "migration_head",
            },
        )
        upgrades = _object(data["upgrade_signatures"], set(UPGRADE_PATHS))
        return cls(
            **{
                **data,
                "profile": Profile.parse(data["profile"]),
                "schemas": _schemas(data["schemas"]),
                "upgrade_signatures": tuple(sorted(upgrades.items())),
            }
        )


# Small closed record grammar, not a generic catalog/validation framework.
_SHAPES = {
    "schemas": {"identity": "identity1"},
    "tables": {"identity": "identity2", "access_method": "name"},
    "columns": {
        "identity": "identity3",
        "position": "positive",
        "type": "identity2",
        "typmod": "int",
        "formatted_type": "name",
        "collation": "identity2?",
        "not_null": "bool",
        "default": "text?",
    },
    "constraints": {
        "identity": "identity3",
        "kind": "name",
        "columns": "identity3[]",
        "referenced_table": "identity2?",
        "referenced_columns": "identity3[]",
        "match": "text?",
        "on_update": "text?",
        "on_delete": "text?",
        "deferrable": "bool",
        "deferred": "bool",
        "validated": "bool",
        "no_inherit": "bool",
        "definition": "name",
    },
    "indexes": {
        "identity": "identity2",
        "table": "identity2",
        "method": "name",
        "unique": "bool",
        "nulls_not_distinct": "bool",
        "keys": "keys",
        "include": "identity3[]",
        "expressions": "text?",
        "predicate": "text?",
        "options": "options",
        "valid": "bool",
        "ready": "bool",
        "live": "bool",
    },
    "sequences": {
        "identity": "identity2",
        "type": "identity2",
        "start": "int",
        "increment": "int",
        "min": "int",
        "max": "int",
        "cache": "positive",
        "cycle": "bool",
        "owned_by": "identity3",
        "ownership_dependency": "name",
        "default": "name",
    },
    "dependency_edges": {
        "identity": "edge",
        "dependency": "name",
    },
}
# An edge identity is [source-kind, source-components, target-kind, target-components].
# Endpoints refer to inventory section names (singular forms are not accepted).
# Constraint identity is [schema, table, constraint]; index keys are ordered objects:
_KEY_SHAPE = {
    "definition": "name",
    "column": "identity3?",
    "collation": "identity2?",
    "opclass": "identity2",
    "descending": "bool",
    "nulls_first": "bool",
}
INVENTORY = tuple(_SHAPES)
JSONValue: TypeAlias = str | int | bool | None | tuple["JSONValue", ...]


def _object(raw: object, keys: set[str]) -> dict[str, object]:
    _require(type(raw) is dict)
    _require(set(raw) == keys)
    return raw


def _name(raw: object) -> str:
    _require(type(raw) is str and bool(raw) and "\x00" not in raw)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProbeError("invalid catalog snapshot or profile") from error
    return raw


def _identity(raw: object, size: int) -> tuple[str, ...]:
    _require(type(raw) is list and len(raw) == size)
    return tuple(_name(component) for component in raw)


def _value(raw: object, kind: str) -> JSONValue:
    if kind.endswith("?"):
        return None if raw is None else _value(raw, kind[:-1])
    if kind.endswith("[]"):
        _require(type(raw) is list)
        values = tuple(_value(value, kind[:-2]) for value in raw)
        _require(len(set(values)) == len(values))
        return values
    if kind.startswith("identity"):
        return _identity(raw, int(kind[-1]))
    if kind == "name":
        return _name(raw)
    if kind == "text":
        _require(type(raw) is str)
        if raw:
            _name(raw)
        return raw
    if kind in ("int", "positive"):
        _require(type(raw) is int and (kind == "int" or raw > 0))
        return raw
    if kind == "bool":
        _require(type(raw) is bool)
        return raw
    if kind == "options":
        # Closed reviewed btree reloptions; exact strings retained, unknown rejected.
        options = _object(raw, set(raw) if type(raw) is dict else set())
        _require(set(options) <= {"fillfactor", "deduplicate_items"})
        for key, value in options.items():
            if key == "fillfactor":
                _require(type(value) is int and 10 <= value <= 100)
            else:
                _require(type(value) is bool)
        return tuple(sorted(options.items()))
    if kind == "keys":
        _require(type(raw) is list and len(raw) > 0)
        return tuple(_record(value, _KEY_SHAPE) for value in raw)
    if kind == "edge":
        _require(type(raw) is list and len(raw) == 4)
        source, source_id, target, target_id = raw
        sizes = {
            "schemas": 1,
            "tables": 2,
            "columns": 3,
            "constraints": 3,
            "indexes": 2,
            "sequences": 2,
        }
        _require(type(source) is str and source in sizes)
        _require(type(target) is str and target in sizes)
        return (
            source,
            _identity(source_id, sizes[source]),
            target,
            _identity(target_id, sizes[target]),
        )
    raise AssertionError("unknown internal catalog field kind")


def _record(raw: object, shape: dict[str, str]) -> tuple[JSONValue, ...]:
    data = _object(raw, set(shape))
    values = []
    for key, kind in shape.items():
        if shape is _SHAPES["constraints"] and key == "columns" and data["kind"] == "f":
            _require(type(data[key]) is list)
            values.append(tuple(_value(value, "identity3") for value in data[key]))
        else:
            values.append(_value(data[key], kind))
    return tuple(values)


@dataclass(frozen=True)
class Facts:
    database: str
    current_user: str
    session_user: str
    server_version_num: int
    server_address: str
    server_port: int

    def __post_init__(self) -> None:
        for value in (self.database, self.current_user, self.session_user):
            _name(value)
        _require(
            type(self.server_version_num) is int and self.server_version_num == 160015
        )
        _require(type(self.server_port) is int and 1 <= self.server_port <= 65535)
        _name(self.server_address)
        try:
            ipaddress.ip_address(self.server_address)
        except ValueError as error:
            raise ProbeError("invalid catalog snapshot or profile") from error

    @classmethod
    def parse(cls, raw: object) -> "Facts":
        data = _object(
            raw,
            {
                "database",
                "current_user",
                "session_user",
                "server_version_num",
                "server_address",
                "server_port",
            },
        )
        return cls(**data)


@dataclass(frozen=True)
class DatabaseCollation:
    """Portable PG16 database semantics, separate from deployment identity facts."""

    encoding: str
    provider: str
    collate: str
    ctype: str
    icu_locale: str | None
    icu_rules: str | None
    version: str | None
    actual_version: str | None

    def __post_init__(self) -> None:
        for value in (self.encoding, self.provider, self.collate, self.ctype):
            _name(value)
        for value in (
            self.icu_locale,
            self.icu_rules,
            self.version,
            self.actual_version,
        ):
            _value(value, "text?")
        _require(self.provider in ("c", "i"))
        _require((self.provider == "i") == (self.icu_locale is not None))
        _require(self.provider == "i" or self.icu_rules is None)
        # A stale recorded version can hide indexes built with older semantics.
        _require(self.version == self.actual_version)

    def to_data(self) -> dict[str, object]:
        return {
            "encoding": self.encoding,
            "provider": self.provider,
            "collate": self.collate,
            "ctype": self.ctype,
            "icu_locale": self.icu_locale,
            "icu_rules": self.icu_rules,
            "version": self.version,
            "actual_version": self.actual_version,
        }

    @classmethod
    def parse(cls, raw: object) -> "DatabaseCollation":
        data = _object(
            raw,
            {
                "encoding",
                "provider",
                "collate",
                "ctype",
                "icu_locale",
                "icu_rules",
                "version",
                "actual_version",
            },
        )
        return cls(**data)


@dataclass(frozen=True)
class Snapshot:
    """Deeply immutable, validated catalog data; construct through parse or directly."""

    profile: Profile
    facts: Facts
    inventory: tuple[tuple[tuple[JSONValue, ...], ...], ...]
    unsupported: tuple[int, ...]
    database_collation: DatabaseCollation

    def __post_init__(self) -> None:
        _require(type(self.profile) is Profile and type(self.facts) is Facts)
        _require(type(self.database_collation) is DatabaseCollation)
        _require(
            type(self.inventory) is tuple and len(self.inventory) == len(INVENTORY)
        )
        _require(
            type(self.unsupported) is tuple
            and len(self.unsupported) == len(CATALOG_CLASSES)
        )
        _require(all(type(n) is int and n >= 0 for n in self.unsupported))
        # Revalidate direct construction as well as boundary parsing. This also
        # checks every nested tuple, rather than trusting a frozen outer wrapper.
        for section, records in zip(INVENTORY, self.inventory, strict=True):
            _require(type(records) is tuple)
            ids = set()
            for record in records:
                _require(type(record) is tuple and len(record) == len(_SHAPES[section]))
                try:
                    raw = _record_data(section, record)
                    _require(_record(raw, _SHAPES[section]) == record)
                except (TypeError, ValueError, IndexError) as error:
                    raise ProbeError("invalid catalog snapshot or profile") from error
                identity = record if section == "dependency_edges" else record[0]
                _require(identity not in ids)
                ids.add(identity)
        self._check_supported()

    def _check_supported(self) -> None:
        sections = dict(zip(INVENTORY, self.inventory, strict=True))
        for record in sections["tables"]:
            _require(record[1] == "heap")
        positions = set()
        for record in sections["columns"]:
            position = (record[0][:2], record[1])
            _require(position not in positions)
            positions.add(position)
        for record in sections["constraints"]:
            _require(record[1] in ("p", "u", "f", "c"))
        for record in sections["indexes"]:
            data = _record_data("indexes", record)
            _require(data["method"] == "btree")
            _require(all(data[key] is True for key in ("valid", "ready", "live")))
        for record in sections["sequences"]:
            _require(record[9] == "a")
        for record in sections["dependency_edges"]:
            _require(record[1] in ("a", "n", "i"))

    @classmethod
    def parse(cls, raw: object) -> "Snapshot":
        data = _object(
            raw,
            {
                "profile",
                "facts",
                "database_collation",
                "inventory",
                "unsupported",
                "counts",
            },
        )
        profile = Profile.parse(data["profile"])
        facts = Facts.parse(data["facts"])
        inventory = _object(data["inventory"], set(INVENTORY))
        counts = _object(data["counts"], set(INVENTORY))
        unsupported = _object(data["unsupported"], set(CATALOG_CLASSES))
        records = []
        for section, shape in _SHAPES.items():
            values = inventory[section]
            _require(type(values) is list)
            _require(type(counts[section]) is int and counts[section] == len(values))
            records.append(tuple(_record(value, shape) for value in values))
        return cls(
            profile,
            facts,
            tuple(records),
            tuple(unsupported[name] for name in CATALOG_CLASSES),
            DatabaseCollation.parse(data["database_collation"]),
        )


def _thaw(value: JSONValue) -> object:
    return [_thaw(item) for item in value] if type(value) is tuple else value


def _record_data(section: str, record: tuple[JSONValue, ...]) -> dict[str, object]:
    data = dict(zip(_SHAPES[section], map(_thaw, record), strict=True))
    if section == "indexes":
        data["options"] = dict(record[9])
        data["keys"] = [
            dict(zip(_KEY_SHAPE, map(_thaw, key), strict=True)) for key in record[5]
        ]
    return data


def canonical_signature(snapshot: object, profile: Profile) -> str:
    """Validate raw decoded JSON and hash only portable logical schema/profile."""
    _require(type(profile) is Profile)
    parsed = Snapshot.parse(snapshot)
    _require(parsed.profile == profile)
    if any(parsed.unsupported):
        raise ProbeError("unsupported catalog objects")
    inventory = {}
    for section, records in zip(INVENTORY, parsed.inventory, strict=True):
        # Columns: group tables deterministically, retain within-table input order.
        ordered = sorted(
            records,
            key=lambda record: (
                record[0][:2]
                if section == "columns"
                else record
                if section == "dependency_edges"
                else record[0]
            ),
        )
        inventory[section] = [_record_data(section, record) for record in ordered]
    payload = {
        "profile": profile.to_data(),
        "inventory": inventory,
        "database_collation": parsed.database_collation.to_data(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "pg_catalog_v1:sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CatalogConnection:
    """Explicit connection input; protected production config is a later task.

    Unencrypted connections are restricted to numeric loopback fixtures. This
    does not implement or bypass the production target authorization policy.
    """

    endpoint: str
    port: int
    database: str
    role: str
    profile: Profile
    ca_file: str | None = None
    tls_context: ssl.SSLContext | None = None

    def __post_init__(self) -> None:
        _name(self.endpoint)
        _name(self.database)
        _name(self.role)
        _require(type(self.port) is int and 1 <= self.port <= 65535)
        _require(type(self.profile) is Profile)
        if self.ca_file is None:
            try:
                _require(ipaddress.ip_address(self.endpoint).is_loopback)
            except ValueError as error:
                raise ProbeError("invalid catalog connection") from error
        else:
            _require(type(self.ca_file) is str and Path(self.ca_file).is_absolute())


def installed_catalog_profile() -> Profile:
    return Profile(
        hashlib.sha256(
            Path(__file__).with_name("schema_catalog.sql").read_bytes()
        ).hexdigest()
    )


def _unique_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


async def read_catalog(
    mapping: CatalogConnection, *, password: str | None = None
) -> object:
    """Read one bounded catalog snapshot, never application or sequence rows."""
    import ssl

    try:
        import asyncpg
    except ImportError as error:
        raise ProbeError("catalog driver unavailable") from error

    connection = None
    deadline = asyncio.get_running_loop().time() + 15
    try:
        async with asyncio.timeout_at(deadline):
            query = (
                Path(__file__)
                .with_name("schema_catalog.sql")
                .read_text(encoding="utf-8")
            )
            _require(
                mapping.profile
                == Profile(hashlib.sha256(query.encode("utf-8")).hexdigest())
            )
            tls = (
                mapping.tls_context
                if mapping.tls_context is not None
                else ssl.create_default_context(cafile=mapping.ca_file)
                if mapping.ca_file
                else False
            )
            connection = await asyncpg.connect(
                host=mapping.endpoint,
                port=mapping.port,
                database=mapping.database,
                user=mapping.role,
                password=password if password is not None else "",
                ssl=tls,
                timeout=5,
                statement_cache_size=0,
            )
            try:
                transaction = connection.transaction(
                    isolation="read_committed", readonly=True
                )
                await transaction.start()
                for setting, value in SETTINGS:
                    await connection.execute(f"SET LOCAL {setting} = '{value}'")
                await connection.execute("SET LOCAL lock_timeout = '1s'")
                await connection.execute("SET LOCAL statement_timeout = '5s'")
                raw = await connection.fetchval(
                    query, json.dumps(mapping.profile.to_data()), timeout=5
                )
                _require(type(raw) is str and len(raw.encode("utf-8")) <= 4194304)
                snapshot = json.loads(raw, object_pairs_hook=_unique_json)
                parsed = Snapshot.parse(snapshot)
                _require(parsed.profile == mapping.profile)
                _require(parsed.facts.database == mapping.database)
                _require(
                    parsed.facts.current_user
                    == mapping.role
                    == parsed.facts.session_user
                )
                await transaction.commit()
                return snapshot
            finally:
                remaining = deadline - asyncio.get_running_loop().time()
                # Cancellation must not start a second cleanup budget. Terminate
                # synchronously when the wall deadline or caller cancellation hit.
                if remaining <= 0 or asyncio.current_task().cancelling():
                    connection.terminate()
                else:
                    async with asyncio.timeout_at(deadline):
                        await connection.close(timeout=min(1, remaining))
    except (
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
        asyncpg.InternalClientError,
        OSError,
        TimeoutError,
        ValueError,
        RecursionError,
    ) as error:
        raise ProbeError("catalog read refused") from error
    finally:
        if connection is not None and not connection.is_closed():
            connection.terminate()


CONFIG_PATH = Path("/etc/coreteams-deploy/schema-probe.json")
BASELINE_PATH = Path("/etc/coreteams-deploy/schema-baseline.json")
CREDENTIAL_PATH = Path("/etc/coreteams-deploy/schema-probe-credential.json")


def _trusted_descriptor(fd: int, *, directory: bool) -> None:
    info = os.fstat(fd)
    _require(info.st_uid == 0 and not info.st_mode & 0o022)
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def protected_read(path: Path, limit: int) -> bytes:
    return _protected_read(path, limit, 0o022)


def protected_credential_read(path: Path) -> bytes:
    """Credentials must not grant any group/other access, unlike public CA data."""
    return _protected_read(path, 16384, 0o077)


def _protected_read(path: Path, limit: int, forbidden_mode: int) -> bytes:
    """Walk pinned directory descriptors, refusing symlinks at every component.

    Parent replacement cannot redirect an opened descriptor. Root-owned parents
    without group/world write prevent unprivileged replacement after inspection.
    """
    _require(path.is_absolute() and all(p not in {".", ".."} for p in path.parts))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _trusted_descriptor(fd, directory=True)
        for component in path.parts[1:-1]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            _trusted_descriptor(fd, directory=True)
        child = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
        )
        try:
            _trusted_descriptor(child, directory=False)
            info = os.fstat(child)
            _require(not info.st_mode & forbidden_mode)
            _require(info.st_size <= limit)
            with os.fdopen(child, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            _require(0 < len(raw) <= limit)
            return raw
        finally:
            os.close(child)
    finally:
        os.close(fd)


def _protected_json(path: Path, limit: int) -> object:
    return json.loads(protected_read(path, limit), object_pairs_hook=_unique_json)


def target_signature(mapping: TargetMapping, facts: Facts) -> str:
    """Bind the reviewed route AND its reviewed observed backend identity.

    A routed TLS endpoint may use a different port/address than the backend.
    Clearance's protected target must already pin those exact facts; neither
    DNS resolution nor a database OID is used as a cluster identity substitute.
    """
    _require(facts.database == mapping.database)
    _require(facts.current_user == facts.session_user == mapping.role)
    _require(facts.server_version_num == mapping.profile.server_version_num)
    return "pg_target_v1:sha256:" + _digest(
        {
            "mapping": mapping.to_data(),
            "facts": {
                "database": facts.database,
                "current_user": facts.current_user,
                "session_user": facts.session_user,
                "server_version_num": facts.server_version_num,
                "server_address": facts.server_address,
                "server_port": facts.server_port,
            },
        }
    )


def probe(
    mapping: TargetMapping,
    baseline: Baseline,
    clearance: "Clearance",
    release_sha: str,
    *,
    password: str = "",
    tls_context: ssl.SSLContext | None = None,
) -> "SchemaEvidence":
    """Synchronous installed CLI boundary; the read shell itself remains async."""
    baseline.bind(release_sha, mapping)
    _require(clearance.sha == baseline.release_sha)
    _require(clearance.evidence.signature == baseline.expected_signature)
    _require(mapping.profile == installed_catalog_profile())
    # Do not create a coroutine in an already running loop (or leak one).
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ProbeError("synchronous catalog probe requires CLI context")
    raw = asyncio.run(
        read_catalog(
            CatalogConnection(
                mapping.endpoint,
                mapping.port,
                mapping.database,
                mapping.role,
                mapping.profile,
                mapping.ca_file,
                tls_context,
            ),
            password=password,
        )
    )
    parsed = Snapshot.parse(raw)
    schemas = tuple(sorted(record[0][0] for record in parsed.inventory[0]))
    _require(schemas == mapping.schemas)
    signature = canonical_signature(raw, mapping.profile)
    _require(signature == baseline.expected_signature)
    target = target_signature(mapping, parsed.facts)
    _require(target == clearance.evidence.target)
    # Preserve the caller's evidence class when deploy_server runs via -m and
    # its dataclasses belong to __main__, rather than importing a second class.
    return type(clearance.evidence)(signature, target)


def installed_probe(
    release_sha: str, clearance: "Clearance"
) -> Callable[[], "SchemaEvidence"]:
    """No files, driver imports or network until called under deployment lock."""

    def run() -> "SchemaEvidence":
        try:
            mapping = TargetMapping.parse(_protected_json(CONFIG_PATH, 65536))
            baseline = Baseline.parse(_protected_json(BASELINE_PATH, 65536))
            baseline.bind(release_sha, mapping)
            _require(clearance.sha == baseline.release_sha)
            _require(clearance.evidence.signature == baseline.expected_signature)
            # Installed SQL, interpreter and imports are provisioned as trusted
            # code. Also verify the SQL path/parents before binding its digest.
            sql = protected_read(Path(__file__).with_name("schema_catalog.sql"), 262144)
            _require(mapping.profile == Profile(hashlib.sha256(sql).hexdigest()))
            credential = _object(
                json.loads(
                    protected_credential_read(CREDENTIAL_PATH),
                    object_pairs_hook=_unique_json,
                ),
                {"username", "password"},
            )
            _require(credential["username"] == mapping.role)
            password = _name(credential["password"])
            ca = protected_read(Path(mapping.ca_file), 65536).decode("ascii")
            tls = ssl.create_default_context(cadata=ca)
            _require(tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED)
            return probe(
                mapping,
                baseline,
                clearance,
                release_sha,
                password=password,
                tls_context=tls,
            )
        except (
            ValueError,
            OSError,
            TypeError,
            KeyError,
            RecursionError,
            ImportError,
        ) as error:
            raise ProbeError("installed catalog probe refused") from error

    return run
