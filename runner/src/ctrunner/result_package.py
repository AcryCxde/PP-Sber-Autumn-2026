"""Валидатор ZIP-пакета результата аналитического Run (PPS-138, `docs/2.4/result-package.md`).

Пакет — ровно четыре файла в корне: `result.md`, `sources.csv`, `metadata.json`, `manifest.json`;
имя — `coreteams_<project>_<task>_<run>_<phase>_<build>.zip`. Контракт — handoff pilot-package
(pokatyusha/project-harness) и `pilot-result-contract.md`; где он неоднозначен, взято самое строгое
прочтение (список — в документе).

Ядро чистое: `validate_bytes` не читает диск и сеть, ожидаемые нарушения возвращает значением
`Report`, а не исключением. Ввод-вывод — только `validate_file` и `cli`.

**Валидатор проверяет форму пакета, а не истину.** Он НЕ доказывает, что источники существуют
и говорят то, что о них написано, что утверждения в `result.md` верны и снабжены ссылками (он видит
только ссылки вида `[S1]`), что согласие участника реально получено, что эксперт действительно
проверял результат, что результат полезен и что в тексте нет персональных данных вне известных
шаблонов (e-mail, телефон, пути, токены). Эвристики (имена людей, заявления о проверке человеком,
фраза «недостаточно данных») ловят очевидное и обходятся. Весь этот список возвращается в
`Report.not_verified`: «ok» означает «структура допустима», а не «результату можно доверять».
"""

import csv
import hashlib
import io
import json
import re
import stat
import sys
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from pathlib import Path
from typing import Final, assert_never, final
from urllib.parse import urlsplit

from ctrunner.protocol import JsonValue

# --- Контракт --------------------------------------------------------------------------------

RESULT: Final = "result.md"
SOURCES: Final = "sources.csv"
METADATA: Final = "metadata.json"
MANIFEST: Final = "manifest.json"
PAYLOAD: Final = (RESULT, SOURCES, METADATA)
MEMBERS: Final = frozenset({*PAYLOAD, MANIFEST})
MEDIA_TYPES: Final = {RESULT: "text/markdown", SOURCES: "text/csv", METADATA: "application/json"}

SOURCE_COLUMNS: Final = (
    "source_id", "title", "publisher", "author", "published_at", "accessed_at", "url",
    "claim_ids", "source_type", "limitations",
)  # fmt: skip
REQUIRED_SOURCE_FIELDS: Final = (
    "source_id", "title", "accessed_at", "url", "claim_ids", "source_type", "limitations",
)  # fmt: skip
METADATA_REQUIRED: Final = (
    "project_id", "task_id", "run_id", "phase", "build_version", "environment", "created_at",
    "operator_id", "input_scope", "consent_status", "result_schema_version", "limitations",
)  # fmt: skip
METADATA_OPTIONAL: Final = ("participant_id", "consent_text_version")
ID_FIELDS: Final = ("project_id", "task_id", "run_id", "build_version")
PHASES: Final = ("baseline", "prototype")
MANIFEST_ENTRY_KEYS: Final = frozenset(
    {"filename", "media_type", "byte_size", "sha256", "schema_version"}
)

# Заголовки `##` в порядке контракта; сравниваются после нормализации (`_norm_heading`).
SECTIONS: Final = (
    "Decision requested",
    "Short answer",
    "What the evidence says",
    "Comparison or reasoning",
    "Unknowns and limitations",
    "Recommended next step",
    "Expert review/escalation",
    "Contributions",
    "Sources",
)
SEC_SHORT: Final = "short answer"
SEC_EVIDENCE: Final = "what the evidence says"
SEC_EXPERT: Final = "expert review/escalation"
SEC_CONTRIB: Final = "contributions"
SEC_SOURCES: Final = "sources"

# --- Лимиты (защита от zip-бомбы) ---------------------------------------------------------------

MAX_ZIP_BYTES: Final = 10 * 1024 * 1024
MAX_MEMBER_BYTES: Final = 5 * 1024 * 1024
MAX_TOTAL_BYTES: Final = 10 * 1024 * 1024
MAX_ENTRIES: Final = 16  # в корректном пакете четыре; запас для диагностики, а не для содержимого
MAX_RATIO: Final = 100  # распакованный / сжатый, для членов крупнее RATIO_FLOOR
RATIO_FLOOR: Final = 64 * 1024

# --- Шаблоны ---------------------------------------------------------------------------------

_ID: Final = re.compile(r"[A-Za-z0-9.-]+")
_SOURCE_ID: Final = re.compile(r"S[0-9]+")
_ZIP_NAME: Final = re.compile(
    r"coreteams_(?P<project_id>[A-Za-z0-9.-]+)_(?P<task_id>[A-Za-z0-9.-]+)"
    r"_(?P<run_id>[A-Za-z0-9.-]+)_(?P<phase>baseline|prototype)_(?P<build_version>[A-Za-z0-9.-]+)\.zip"
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_CITE: Final = re.compile(r"\[(S[0-9]+)\]")
_BARE_ID: Final = re.compile(r"\bS[0-9]+\b")
_FENCE: Final = re.compile(r"^\s*(```|~~~)")
_H2: Final = re.compile(r"^##\s+(.*?)\s*#*\s*$")
_URL: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
_INSUFFICIENT: Final = re.compile(
    r"insufficient\s+evidence|evidence\s+is\s+(?:insufficient|limited)|not\s+enough\s+evidence"
    r"|недостаточно\s+(?:данных|доказательств|источников)|не\s+хватает\s+источников",
    re.IGNORECASE,
)
_NOT_APPLICABLE: Final = re.compile(r"not\s+applicable|не\s+применимо", re.IGNORECASE)
_AI_MARK: Final = re.compile(
    r"\b(?:AI|ИИ|automated|автоматическ\w*|искусственн\w+)\b|\bLLM\b", re.IGNORECASE
)
_NEGATION: Final = re.compile(
    r"\b(?:not|no|never|without|none|nobody)\b|\b(?:не|нет|ни|без|отсутству\w+)\b", re.IGNORECASE
)
_REVIEW_CLAIM: Final = re.compile(
    r"(?:reviewed|verified|checked|approved|validated|signed\s+off)\s+by\b"
    r"|(?:reviewed|verified|approved)\s+(?:and\s+\w+\s+)?by\s+(?:a|an|the)\b"
    r"|(?:проверил\w*|проверен\w*|утвердил\w*|подтвердил\w*|верифицирован\w*)\s+"
    r"(?:\w+\s+)?(?:юрист\w*|эксперт\w*|специалист\w*|аудитор\w*|врач\w*|консультант\w*)"
    r"|(?:юрист\w*|эксперт\w*|специалист\w*|аудитор\w*|врач\w*)\s+(?:проверил\w*|подтвердил\w*)",
    re.IGNORECASE,
)
# Люди, а не роли: инициалы, отчество, обращение. Эвристика, не доказательство отсутствия имён.
_PERSON: Final = re.compile(
    r"\b[A-ZА-ЯЁ]\.\s?[A-ZА-ЯЁ]\.\s?[A-ZА-ЯЁ][a-zа-яё]+"
    r"|\b[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ]\.\s?[A-ZА-ЯЁ]\."
    r"|\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:вич|вна|ична|ьич|кызы|оглы)\b"
    r"|\b(?:Dr|Prof|Mr|Mrs|Ms)\.?\s+[A-Z][a-z]+"
    r"|\b(?:д-р|проф\.|г-н|г-жа)\s+[А-ЯЁ][а-яё]+"
    r"|(?<!\w)@[A-Za-z0-9_]{3,}"
)

# (вид, шаблон, пропускать ли URL-ы). Совпадения в отчёт не попадают — только вид и строка.
_PRIVATE: Final = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), False),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), False),
    (
        "token",
        re.compile(
            r"\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
            r"|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{35}"
            r"|\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"
            r"|\bBearer\s+[A-Za-z0-9._~+/-]{16,}"
            r"|\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*\S{6,}",
            re.IGNORECASE,
        ),
        False,
    ),
    ("url_credentials", re.compile(r"://[^/\s:@]+:[^/\s@]+@"), False),
    (
        "url_secret_param",
        re.compile(
            r"[?&](?:access_|auth_|api_)?(?:token|key|secret|password|passwd|sig|signature)=[^&\s]{4,}",
            re.IGNORECASE,
        ),
        False,
    ),
    (
        "local_path",
        re.compile(
            r"(?<![\w.:/-])/(?:home|Users|root|tmp|var|etc|run|opt|mnt|proc|workspace)/"
            r"|(?<![\w/])~/|\b[A-Za-z]:\\(?:Users|Documents and Settings)\\",
            re.IGNORECASE,
        ),
        True,
    ),
    (
        "phone",
        re.compile(
            r"\+\d{1,3}[\s().-]*\d{2,4}[\s().-]*\d{2,4}[\s().-]*\d{2,4}(?!\d)"
            r"|(?<![\d-])[78][\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?![\d-])"
        ),
        True,
    ),
)
_PRIVATE_KEY_NAME: Final = re.compile(
    r"e-?mail|phone|contact|password|secret|api.?key|token|address", re.IGNORECASE
)


# --- Типы отчёта -----------------------------------------------------------------------------


class Code(StrEnum):
    """Закрытый перечень нарушений; значение — стабильный идентификатор для машин."""

    UNREADABLE = "unreadable"
    NOT_A_ZIP = "not_a_zip"
    ZIP_TOO_LARGE = "zip_too_large"
    TOO_MANY_ENTRIES = "too_many_entries"
    ZIP_CORRUPT = "zip_corrupt"
    DUPLICATE_MEMBER = "duplicate_member"
    UNSAFE_PATH = "unsafe_path"
    SPECIAL_FILE = "special_file"  # симлинк, устройство, каталог
    ENCRYPTED = "encrypted"
    MEMBERS_MISMATCH = "members_mismatch"
    MEMBER_TOO_LARGE = "member_too_large"
    TOTAL_TOO_LARGE = "total_too_large"
    COMPRESSION_RATIO = "compression_ratio"
    BAD_ZIP_NAME = "bad_zip_name"
    ZIP_NAME_MISMATCH = "zip_name_mismatch"
    NOT_UTF8 = "not_utf8"
    BOM = "bom"
    EMPTY_MEMBER = "empty_member"
    INVALID_JSON = "invalid_json"
    METADATA_SCHEMA = "metadata_schema"
    MANIFEST_SCHEMA = "manifest_schema"
    MANIFEST_HASH = "manifest_hash"
    MANIFEST_SIZE = "manifest_size"
    SOURCES_HEADER = "sources_header"
    SOURCES_ROW = "sources_row"
    SOURCES_DUPLICATE_ID = "sources_duplicate_id"
    SOURCES_EMPTY_UNSTATED = "sources_empty_unstated"
    RESULT_SECTIONS = "result_sections"
    SOURCE_UNRESOLVED = "source_unresolved"
    SOURCE_ORPHAN = "source_orphan"
    SOURCE_NOT_LISTED = "source_not_listed"
    NO_CITATIONS = "no_citations"
    PRIVATE_DATA = "private_data"
    AI_NOT_MARKED = "ai_not_marked"
    PERSON_AS_ROLE = "person_as_role"
    HUMAN_REVIEW_CLAIM = "human_review_claim"


@final
@dataclass(frozen=True, slots=True)
class Violation:
    code: Code
    member: str  # файл пакета или "" для пакета в целом
    detail: str  # без значений из пакета, которые могут быть приватными


NOT_VERIFIED: Final = (
    "существование источников и соответствие им утверждений",
    "истинность и полнота выводов; наличие ссылок у каждого фактического утверждения",
    "фактическое согласие участника (проверяется только поле consent_status)",
    "факт проверки результата квалифицированным специалистом",
    "полезность результата и пригодность для решения",
    "персональные данные и секреты вне известных шаблонов; имена людей — только эвристика",
)


@final
@dataclass(frozen=True, slots=True)
class Report:
    violations: tuple[Violation, ...]
    source_count: int
    not_verified: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "structural_check": "PASS" if self.ok else "FAIL",
            "source_count": self.source_count,
            "violations": [
                {"code": v.code.value, "member": v.member, "detail": v.detail}
                for v in self.violations
            ],
            "not_verified": list(self.not_verified),
        }


type Found = list[Violation]


def _v(code: Code, member: str, detail: str) -> Violation:
    return Violation(code, member, detail)


# --- Контейнер ---------------------------------------------------------------------------------


def _unsafe_name(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return (
        name.startswith(("/", "\\"))
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or any(p in ("", "..", ".") for p in parts)
    )


def _entry_violations(infos: Sequence[zipfile.ZipInfo]) -> Found:
    found: Found = []
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if _unsafe_name(name):
            found.append(_v(Code.UNSAFE_PATH, "", f"недопустимый путь {name!r}"))
            continue
        if name in seen:
            found.append(_v(Code.DUPLICATE_MEMBER, name, "имя встречается в архиве повторно"))
        seen.add(name)
        kind = stat.S_IFMT(info.external_attr >> 16)  # 0 — архиватор тип не записал
        if info.is_dir() or kind not in (0, stat.S_IFREG):
            found.append(_v(Code.SPECIAL_FILE, name, "симлинк, каталог или не обычный файл"))
        if info.flag_bits & 0x1:
            found.append(_v(Code.ENCRYPTED, name, "член зашифрован"))
        if info.file_size > MAX_MEMBER_BYTES:
            found.append(_v(Code.MEMBER_TOO_LARGE, name, f"заявлено {info.file_size} байт"))
        if info.file_size > RATIO_FLOOR and info.file_size > MAX_RATIO * max(info.compress_size, 1):
            found.append(_v(Code.COMPRESSION_RATIO, name, f"степень сжатия выше {MAX_RATIO}:1"))
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        found.append(_v(Code.TOTAL_TOO_LARGE, "", f"заявлено {total} байт суммарно"))
    names = {i.filename for i in infos if not _unsafe_name(i.filename)}
    for missing in sorted(MEMBERS - names):
        found.append(_v(Code.MEMBERS_MISMATCH, missing, "обязательный файл отсутствует"))
    for extra in sorted(names - MEMBERS):
        found.append(_v(Code.MEMBERS_MISMATCH, extra, "лишний файл: допустимы ровно четыре"))
    return found


def _read_members(archive: zipfile.ZipFile) -> tuple[dict[str, bytes], Found]:
    """Читает члены с жёстким потолком: заголовок архива может врать о размере."""
    data: dict[str, bytes] = {}
    found: Found = []
    total = 0
    for info in archive.infolist():
        try:
            with archive.open(info) as handle:
                body = handle.read(MAX_MEMBER_BYTES + 1)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error, EOFError) as e:
            found.append(_v(Code.ZIP_CORRUPT, info.filename, type(e).__name__))
            continue
        total += len(body)
        if len(body) > MAX_MEMBER_BYTES:
            found.append(_v(Code.MEMBER_TOO_LARGE, info.filename, "распаковано больше лимита"))
        elif total > MAX_TOTAL_BYTES:
            found.append(_v(Code.TOTAL_TOO_LARGE, "", "распаковано больше лимита"))
            break
        else:
            data[info.filename] = body
    return data, found


def _open_container(raw: bytes) -> tuple[dict[str, bytes], Found]:
    if len(raw) > MAX_ZIP_BYTES:
        return {}, [_v(Code.ZIP_TOO_LARGE, "", f"{len(raw)} байт, лимит {MAX_ZIP_BYTES}")]
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, NotImplementedError, OSError, ValueError):
        return {}, [_v(Code.NOT_A_ZIP, "", "не ZIP-архив")]
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            return {}, [_v(Code.TOO_MANY_ENTRIES, "", f"{len(infos)} записей, лимит {MAX_ENTRIES}")]
        found = _entry_violations(infos)
        if found:
            return {}, found
        return _read_members(archive)


# --- Текст и JSON --------------------------------------------------------------------------------


def _decode(data: Mapping[str, bytes]) -> tuple[dict[str, str], Found]:
    texts: dict[str, str] = {}
    found: Found = []
    for name, body in data.items():
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            found.append(_v(Code.NOT_UTF8, name, "не UTF-8"))
            continue
        if text.startswith("﻿"):
            found.append(_v(Code.BOM, name, "BOM в начале файла не допускается"))
        elif not text.strip() and name != SOURCES:
            found.append(_v(Code.EMPTY_MEMBER, name, "файл пуст"))
        else:
            texts[name] = text
    return texts, found


def _reject_constant(name: str) -> JsonValue:
    raise ValueError(f"недопустимая константа {name}")


def _no_duplicates(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("повторяющийся ключ JSON")
        result[key] = value
    return result


def _load_object(name: str, text: str) -> tuple[dict[str, JsonValue] | None, Found]:
    try:
        value: JsonValue = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as e:
        return None, [_v(Code.INVALID_JSON, name, str(e)[:120])]
    if not isinstance(value, dict):
        return None, [_v(Code.INVALID_JSON, name, "корень JSON должен быть объектом")]
    return value, []


def load_json_object(name: str, text: str) -> tuple[dict[str, JsonValue] | None, list[Violation]]:
    """Строгий разбор JSON-объекта (без дублей ключей и NaN) — тем же кодом, что у валидатора."""
    return _load_object(name, text)


def _nonempty_str(value: JsonValue) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _aware_timestamp(value: str) -> bool:
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _iso(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


# --- metadata.json -------------------------------------------------------------------------------


def _check_metadata_keys(meta: Mapping[str, JsonValue]) -> Found:
    found: Found = []
    for key in sorted(meta.keys() - {*METADATA_REQUIRED, *METADATA_OPTIONAL}):
        if _PRIVATE_KEY_NAME.search(key):
            found.append(_v(Code.PRIVATE_DATA, METADATA, "поле с приватным именем"))
        else:
            found.append(_v(Code.METADATA_SCHEMA, METADATA, f"неизвестное поле {key!r}"))
    found.extend(
        _v(Code.METADATA_SCHEMA, METADATA, f"нет обязательного поля {key}")
        for key in METADATA_REQUIRED
        if key not in meta
    )
    return found


def _check_metadata(meta: Mapping[str, JsonValue]) -> Found:
    found = _check_metadata_keys(meta)

    def bad(detail: str) -> None:
        found.append(_v(Code.METADATA_SCHEMA, METADATA, detail))

    for key in (*METADATA_REQUIRED, *METADATA_OPTIONAL):
        if key not in meta or key == "limitations":
            continue
        if not _nonempty_str(meta[key]):
            bad(f"{key}: ожидается непустая строка")
    for key in (*ID_FIELDS, "participant_id", "operator_id"):
        value = meta.get(key)
        if isinstance(value, str) and not _ID.fullmatch(value):
            bad(f"{key}: допустимы только A-Za-z0-9.- (для людей — псевдоним)")
    if meta.get("phase") not in PHASES:
        bad("phase: baseline или prototype")
    created = meta.get("created_at")
    if isinstance(created, str) and not _aware_timestamp(created):
        bad("created_at: ISO 8601 с часовым поясом")
    if "participant_id" in meta and not _nonempty_str(meta.get("consent_text_version")):
        bad("при participant_id нужен consent_text_version")
    found.extend(_check_limitations(meta.get("limitations")))
    return found


def _check_limitations(value: JsonValue) -> Found:
    ok = _nonempty_str(value) or (
        isinstance(value, list) and bool(value) and all(_nonempty_str(i) for i in value)
    )
    if ok:
        return []
    return [_v(Code.METADATA_SCHEMA, METADATA, "limitations: непустая строка или список строк")]


def _limitations_text(value: JsonValue) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(i for i in value if isinstance(i, str))
    return ""


def expected_zip_name(meta: Mapping[str, JsonValue]) -> str | None:
    """Имя ZIP по metadata или None, если поля имени не строки допустимого вида."""
    parts = [meta.get(k) for k in ("project_id", "task_id", "run_id", "phase", "build_version")]
    if not all(isinstance(p, str) and _ID.fullmatch(p) for p in parts) or parts[3] not in PHASES:
        return None
    return "coreteams_" + "_".join(str(p) for p in parts) + ".zip"


def _check_zip_name(name: str, meta: Mapping[str, JsonValue]) -> Found:
    match = _ZIP_NAME.fullmatch(name)
    if match is None:
        return [
            _v(Code.BAD_ZIP_NAME, "", "имя не coreteams_<project>_<task>_<run>_<phase>_<build>.zip")
        ]
    wrong = [k for k, v in match.groupdict().items() if meta.get(k) != v]
    if wrong:
        return [
            _v(Code.ZIP_NAME_MISMATCH, "", f"имя архива расходится с metadata: {', '.join(wrong)}")
        ]
    return []


# --- manifest.json -------------------------------------------------------------------------------


def _check_manifest(manifest: Mapping[str, JsonValue], data: Mapping[str, bytes]) -> Found:
    found: Found = []
    if manifest.keys() != {"files"}:
        found.append(_v(Code.MANIFEST_SCHEMA, MANIFEST, "допустим единственный ключ files"))
    files = manifest.get("files")
    if not isinstance(files, list):
        return [*found, _v(Code.MANIFEST_SCHEMA, MANIFEST, "files должен быть списком")]
    seen: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or entry.keys() != MANIFEST_ENTRY_KEYS:
            found.append(_v(Code.MANIFEST_SCHEMA, MANIFEST, "запись files: ровно 5 ключей"))
            continue
        name = entry["filename"]
        if not isinstance(name, str) or name not in PAYLOAD:
            found.append(
                _v(Code.MANIFEST_SCHEMA, MANIFEST, "filename вне payload (manifest не хэшируется)")
            )
            continue
        seen.append(name)
        found.extend(_check_entry(name, entry, data[name]))
    if sorted(seen) != sorted(PAYLOAD):
        found.append(
            _v(Code.MANIFEST_SCHEMA, MANIFEST, "files: ровно три payload-файла по одному разу")
        )
    return found


def _check_entry(name: str, entry: Mapping[str, JsonValue], body: bytes) -> Found:
    found: Found = []
    media = entry["media_type"]
    if media not in (MEDIA_TYPES[name], f"{MEDIA_TYPES[name]}; charset=utf-8"):
        found.append(_v(Code.MANIFEST_SCHEMA, name, f"media_type должен быть {MEDIA_TYPES[name]}"))
    if not _nonempty_str(entry["schema_version"]):
        found.append(_v(Code.MANIFEST_SCHEMA, name, "schema_version: непустая строка"))
    size = entry["byte_size"]
    if isinstance(size, bool) or not isinstance(size, int):
        found.append(_v(Code.MANIFEST_SCHEMA, name, "byte_size: целое число"))
    elif size != len(body):
        found.append(_v(Code.MANIFEST_SIZE, name, f"в manifest {size}, в архиве {len(body)}"))
    digest = entry["sha256"]
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        found.append(_v(Code.MANIFEST_SCHEMA, name, "sha256: 64 hex-символа в нижнем регистре"))
    elif digest != hashlib.sha256(body).hexdigest():
        found.append(_v(Code.MANIFEST_HASH, name, "sha256 не совпадает с байтами файла"))
    return found


# --- sources.csv ---------------------------------------------------------------------------------


@final
@dataclass(frozen=True, slots=True)
class _Sources:
    ids: frozenset[str]
    violations: tuple[Violation, ...]


def _host_problem(host: str) -> str | None:
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return "не публичный хост"
    try:
        address = ip_address(host)
    except ValueError:
        return None
    return None if address.is_global else "не публичный IP"


def _url_problem(url: str) -> str | None:
    if re.search(r"\s", url):
        return "пробелы в URL"
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "URL не разбирается"
    if parts.scheme not in ("http", "https") or not host:
        return "нужен прямой http(s)-URL"
    if parts.username or parts.password:
        return "учётные данные в URL"
    return _host_problem(host)


def _check_row(row: Sequence[str], seen: set[str]) -> tuple[str | None, Found]:
    cell = dict(zip(SOURCE_COLUMNS, row, strict=True))
    sid = cell["source_id"]
    found: Found = []

    def bad(detail: str) -> None:
        found.append(_v(Code.SOURCES_ROW, SOURCES, f"{sid or '?'}: {detail}"))

    for column in REQUIRED_SOURCE_FIELDS:
        if not cell[column].strip():
            bad(f"пустое поле {column}")
    if sid and not _SOURCE_ID.fullmatch(sid):
        bad("source_id вида S<число>")
    if sid in seen:
        found.append(_v(Code.SOURCES_DUPLICATE_ID, SOURCES, f"повтор {sid}"))
    if cell["url"].strip() and (problem := _url_problem(cell["url"])):
        bad(problem)
    if cell["accessed_at"].strip() and not _iso(cell["accessed_at"]):
        bad("accessed_at: ISO 8601")
    if cell["published_at"].strip() and not _iso(cell["published_at"]):
        bad("published_at: ISO 8601 или пусто")
    return (sid or None), found


def _check_sources(text: str) -> _Sources:
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as e:
        return _Sources(frozenset(), (_v(Code.SOURCES_ROW, SOURCES, f"CSV: {e}"[:120]),))
    if not rows or tuple(rows[0]) != SOURCE_COLUMNS:
        return _Sources(
            frozenset(),
            (_v(Code.SOURCES_HEADER, SOURCES, f"заголовок: {','.join(SOURCE_COLUMNS)}"),),
        )
    found: Found = []
    ids: set[str] = set()
    for number, row in enumerate(rows[1:], start=2):
        if not row:
            continue  # пустая строка в конце
        if len(row) != len(SOURCE_COLUMNS):
            found.append(_v(Code.SOURCES_ROW, SOURCES, f"строка {number}: {len(row)} столбцов"))
            continue
        sid, row_found = _check_row(row, ids)
        found.extend(row_found)
        if sid:
            ids.add(sid)
    return _Sources(frozenset(ids), tuple(found))


# --- result.md -----------------------------------------------------------------------------------


def _norm_heading(raw: str) -> str:
    text = re.sub(r"[*_`]", "", raw)
    text = re.sub(r"^\s*\d+[.)]\s*", "", text)
    text = re.sub(r"\s*/\s*", "/", text)
    return re.sub(r"\s+", " ", text).strip(" :").casefold()


@final
@dataclass(frozen=True, slots=True)
class _Doc:
    preamble: str
    parts: tuple[tuple[str, str], ...]  # (нормализованный заголовок, тело) в порядке документа

    @property
    def order(self) -> list[str]:
        return [title for title, _ in self.parts]

    def body(self, title: str) -> str:
        """Тело первого раздела с таким заголовком; пусто, если раздела нет."""
        return next((body for t, body in self.parts if t == title), "")

    def outside(self, title: str) -> str:
        return self.preamble + "".join(body for t, body in self.parts if t != title)


def _split_sections(text: str) -> _Doc:
    """Делит по заголовкам `##` вне ограждённых блоков кода."""
    preamble: list[str] = []
    titles: list[str] = []
    bodies: list[list[str]] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _H2.match(line)
        if heading is not None:
            titles.append(_norm_heading(heading.group(1)))
            bodies.append([])
        elif bodies:
            bodies[-1].append(line)
        else:
            preamble.append(line)
    parts = tuple((t, "\n".join(b) + "\n") for t, b in zip(titles, bodies, strict=True))
    return _Doc("\n".join(preamble) + "\n", parts)


def _check_sections(doc: _Doc) -> Found:
    found: Found = []
    order = doc.order
    required = [_norm_heading(s) for s in SECTIONS]
    for title, name in zip(required, SECTIONS, strict=True):
        count = order.count(title)
        if count == 0:
            found.append(_v(Code.RESULT_SECTIONS, RESULT, f"нет раздела «{name}»"))
        elif count > 1:
            found.append(_v(Code.RESULT_SECTIONS, RESULT, f"раздел «{name}» повторён"))
    present = [t for t in order if t in required]
    first_seen = list(dict.fromkeys(present))
    if first_seen != [t for t in required if t in first_seen]:
        found.append(_v(Code.RESULT_SECTIONS, RESULT, "разделы не в порядке контракта"))
    for title, name in zip(required, SECTIONS, strict=True):
        body = doc.body(title)
        if title not in order:
            continue
        if not body.strip():
            found.append(_v(Code.RESULT_SECTIONS, RESULT, f"раздел «{name}» пуст"))
        elif _NOT_APPLICABLE.fullmatch(re.sub(r"[\W_]+$", "", body.strip())):
            found.append(_v(Code.RESULT_SECTIONS, RESULT, f"«{name}»: Not applicable без причины"))
    return found


def _check_citations(doc: _Doc, source_ids: frozenset[str]) -> Found:
    found: Found = []
    listing = doc.body(SEC_SOURCES)
    cited = set(_CITE.findall(doc.outside(SEC_SOURCES)))
    listed = set(_BARE_ID.findall(listing))
    for sid in sorted((cited | listed) - source_ids):
        found.append(_v(Code.SOURCE_UNRESOLVED, RESULT, f"ссылка {sid} не найдена в sources.csv"))
    for sid in sorted(source_ids - cited):
        found.append(_v(Code.SOURCE_ORPHAN, SOURCES, f"{sid} нигде не цитируется"))
    for sid in sorted(source_ids - listed):
        found.append(_v(Code.SOURCE_NOT_LISTED, RESULT, f"{sid} нет в разделе Sources"))
    if source_ids and not _CITE.search(doc.body(SEC_EVIDENCE)):
        found.append(_v(Code.NO_CITATIONS, RESULT, "в «What the evidence says» нет ссылок [S<n>]"))
    return found


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _check_roles(doc: _Doc) -> Found:
    found: Found = []
    contrib = doc.body(SEC_CONTRIB)
    if contrib.strip() and not _AI_MARK.search(contrib):
        found.append(
            _v(
                Code.AI_NOT_MARKED,
                RESULT,
                "Contributions не указывает, что роли — ИИ/автоматический анализ",
            )
        )
    if _PERSON.search(contrib):
        found.append(_v(Code.PERSON_AS_ROLE, RESULT, "в Contributions похоже на имя человека"))
    for title in (SEC_CONTRIB, SEC_EXPERT):
        for sentence in _sentences(doc.body(title)):
            if _REVIEW_CLAIM.search(sentence) and not _NEGATION.search(sentence):
                found.append(
                    _v(
                        Code.HUMAN_REVIEW_CLAIM,
                        RESULT,
                        "заявлена проверка человеком без подтверждения",
                    )
                )
                break
    return found


def _check_insufficient(sources: _Sources, doc: _Doc, meta: Mapping[str, JsonValue]) -> Found:
    if sources.ids or sources.violations:
        return []
    stated = _INSUFFICIENT.search(doc.body(SEC_SHORT)) and _INSUFFICIENT.search(
        _limitations_text(meta.get("limitations"))
    )
    if stated:
        return []
    return [_v(Code.SOURCES_EMPTY_UNSTATED, SOURCES,
               "пустая таблица допустима только при явном «insufficient evidence» "
               "в Short answer и в metadata.limitations")]  # fmt: skip


# --- Приватные данные ----------------------------------------------------------------------------


def _scan_private(name: str, text: str) -> Found:
    found: Found = []
    stripped = _URL.sub(" ", text)
    for kind, pattern, skip_urls in _PRIVATE:
        haystack = stripped if skip_urls else text
        for match in pattern.finditer(haystack):
            line = haystack.count("\n", 0, match.start()) + 1
            found.append(_v(Code.PRIVATE_DATA, name, f"{kind}, строка {line}"))
            break
    return found


# --- Оркестрация -----------------------------------------------------------------------------


def _report(found: Found, source_count: int, filename_checked: bool) -> Report:
    note = () if filename_checked else ("имя ZIP не передано: соответствие metadata не проверено",)
    return Report(tuple(found), source_count, (*NOT_VERIFIED, *note))


def _content(texts: Mapping[str, str], data: Mapping[str, bytes], filename: str | None) -> Report:
    found: Found = []
    meta, meta_found = _load_object(METADATA, texts[METADATA])
    manifest, manifest_found = _load_object(MANIFEST, texts[MANIFEST])
    found += meta_found + manifest_found
    if meta is not None:
        found += _check_metadata(meta)
        if filename is not None:
            found += _check_zip_name(filename, meta)
    if manifest is not None:
        found += _check_manifest(manifest, data)
    sources = _check_sources(texts[SOURCES])
    found += sources.violations
    doc = _split_sections(texts[RESULT])
    found += _check_sections(doc)
    found += _check_citations(doc, sources.ids)
    found += _check_roles(doc)
    if meta is not None:
        found += _check_insufficient(sources, doc, meta)
    for name, text in texts.items():
        found += _scan_private(name, text)
    return _report(found, len(sources.ids), filename is not None)


def validate_bytes(raw: bytes, filename: str | None = None) -> Report:
    """Проверяет ZIP целиком. `filename` — имя архива; без него имя не сверяется с metadata.

    Нарушения контейнера (небезопасные пути, лимиты, набор файлов) останавливают проверку
    содержимого: распаковывать то, что не прошло защиту, нельзя.
    """
    data, found = _open_container(raw)
    if found:
        return _report(found, 0, filename is not None)
    texts, decode_found = _decode(data)
    if decode_found:
        return _report(decode_found, 0, filename is not None)
    return _content(texts, data, filename)


type Source = bytes | Path


def validate(source: Source) -> Report:
    match source:
        case bytes():
            return validate_bytes(source)
        case Path():
            return validate_file(source)
        case _:
            assert_never(source)


def validate_file(path: Path) -> Report:
    """Оболочка ввода-вывода: читает не больше лимита+1 байт и передаёт имя файла в ядро."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_ZIP_BYTES + 1)
    except OSError as e:
        return _report([_v(Code.UNREADABLE, "", type(e).__name__)], 0, filename_checked=True)
    return validate_bytes(raw, path.name)


def cli(argv: Sequence[str] | None = None) -> int:
    """`ctrunner-result-package <архив.zip>` → JSON-отчёт на stdout; код 0 — структура допустима."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(cli.__doc__, file=sys.stderr)
        return 64
    report = validate_file(Path(args[0]))
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1
