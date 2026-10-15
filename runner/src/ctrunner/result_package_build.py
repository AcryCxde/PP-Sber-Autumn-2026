"""Сборщик ZIP-пакета результата (`docs/2.4/result-package.md`, раздел «Сборка»).

Из `result.md`, `sources.csv` и `metadata.json` строит `manifest.json` и детерминированный ZIP,
затем сразу проверяет его валидатором `result_package`. Принимается только прошедший архив,
иначе возвращается отказ с теми же нарушениями, что дал бы валидатор, и ничего не записывается.

Ядро `build_package` чистое: байты на входе, значение на выходе, ожидаемые отказы — значениями.
Ввод-вывод — только `build_from_dir` и `cli`. Содержимое файлов нигде не логируется и не выводится.

**Сборщик не доказывает истинность источников**: «собралось» значит «структура допустима»
(см. `Report.not_verified`), а не «источники существуют и подтверждают написанное».
"""

import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, assert_never, final

from ctrunner import result_package as rp
from ctrunner.fsio import _fsync_dir
from ctrunner.protocol import JsonValue
from ctrunner.result_package import Code, Report, Violation

MANIFEST_SCHEMA_VERSION: Final = "1"
_ZIP_DATE: Final = (1980, 1, 1, 0, 0, 0)  # минимум формата ZIP: время сборки в архив не попадает
_FILE_MODE: Final = (stat.S_IFREG | 0o644) << 16
_UNIX: Final = 3  # create_system: не зависит от ОС сборщика


@final
@dataclass(frozen=True, slots=True)
class Built:
    """Собранный и прошедший валидацию архив. `report.ok` истинно по построению."""

    data: bytes
    filename: str
    report: Report


@final
@dataclass(frozen=True, slots=True)
class Rejected:
    """Отказ сборки: `report.violations` непусто, архив не создан."""

    report: Report


type BuildOutcome = Built | Rejected


@final
@dataclass(frozen=True, slots=True)
class Written:
    path: Path
    report: Report


type DirOutcome = Written | Rejected


# --- Ядро ------------------------------------------------------------------------------------


def _reject(*violations: Violation) -> Rejected:
    return Rejected(Report(violations, 0, rp.NOT_VERIFIED))


def _json_bytes(value: JsonValue) -> bytes:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return (text + "\n").encode()


def _manifest(files: Mapping[str, bytes]) -> bytes:
    entries: list[JsonValue] = [
        {
            "filename": name,
            "media_type": rp.MEDIA_TYPES[name],
            "byte_size": len(files[name]),
            "sha256": hashlib.sha256(files[name]).hexdigest(),
            "schema_version": MANIFEST_SCHEMA_VERSION,
        }
        for name in rp.PAYLOAD
    ]
    return _json_bytes({"files": entries})


def _zip(files: Mapping[str, bytes]) -> bytes:
    """Канонический ZIP: порядок и даты фиксированы, без extra/comment, без сжатия.

    Без сжатия, потому что байты DEFLATE зависят от версии zlib, а детерминизм важнее размера.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as archive:
        for name in (*rp.PAYLOAD, rp.MANIFEST):
            info = zipfile.ZipInfo(name, _ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = _UNIX
            info.external_attr = _FILE_MODE
            archive.writestr(info, files[name])
    return buf.getvalue()


def build_package(
    result_md: bytes, sources_csv: bytes, metadata: Mapping[str, JsonValue], *, created_at: datetime
) -> BuildOutcome:
    """Собирает пакет. `created_at` (с поясом) записывается в metadata и перекрывает присланное."""
    if created_at.tzinfo is None:
        return _reject(
            Violation(Code.METADATA_SCHEMA, rp.METADATA, "created_at: нужен часовой пояс")
        )
    meta: dict[str, JsonValue] = {**metadata, "created_at": created_at.isoformat()}
    try:
        meta_bytes = _json_bytes(meta)
    except (ValueError, TypeError, RecursionError):
        return _reject(
            Violation(Code.INVALID_JSON, rp.METADATA, "metadata не сериализуется в строгий JSON")
        )
    files = {rp.RESULT: result_md, rp.SOURCES: sources_csv, rp.METADATA: meta_bytes}
    files[rp.MANIFEST] = _manifest(files)
    data = _zip(files)
    filename = rp.expected_zip_name(meta)
    # Имени нет только при плохих полях имени в metadata; их же отметит проверка содержимого.
    report = rp.validate_bytes(data, filename)
    if not report.ok:
        return Rejected(report)
    if filename is None:  # недостижимо: имя берётся из тех же полей, что проверяет валидатор
        return _reject(Violation(Code.BAD_ZIP_NAME, "", "имя архива не определено"))
    return Built(data, filename, report)


# --- Оболочка --------------------------------------------------------------------------------

_OPEN_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _unreadable(member: str, error: OSError) -> Violation:
    return Violation(Code.UNREADABLE, member, type(error).__name__)


def _read_member(dir_fd: int, name: str) -> bytes | Violation:
    """Читает обычный файл без следования симлинкам, не больше лимита+1 байт."""
    try:
        fd = os.open(name, _OPEN_FLAGS, dir_fd=dir_fd)
    except OSError as e:
        return _unreadable(name, e)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return Violation(Code.UNREADABLE, name, "не обычный файл")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(rp.MAX_MEMBER_BYTES + 1)
    except OSError as e:
        return _unreadable(name, e)
    finally:
        os.close(fd)


def _read_sources(src_dir: Path) -> dict[str, bytes] | list[Violation]:
    try:
        dir_fd = os.open(src_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        return [_unreadable("", e)]
    try:
        read = {name: _read_member(dir_fd, name) for name in rp.PAYLOAD}
    finally:
        os.close(dir_fd)
    problems = [v for v in read.values() if isinstance(v, Violation)]
    if problems:
        return problems
    return {name: body for name, body in read.items() if isinstance(body, bytes)}


def _parse_metadata(raw: bytes) -> dict[str, JsonValue] | list[Violation]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [Violation(Code.NOT_UTF8, rp.METADATA, "не UTF-8")]
    if text.startswith("﻿"):
        return [Violation(Code.BOM, rp.METADATA, "BOM в начале файла не допускается")]
    meta, found = rp.load_json_object(rp.METADATA, text)
    return found if meta is None else meta


def _created_at(meta: Mapping[str, JsonValue]) -> datetime:
    """Время из metadata, если оно корректно и с поясом, иначе текущее UTC."""
    raw = meta.get("created_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return datetime.now(UTC)
        if parsed.tzinfo is not None:
            return parsed
    return datetime.now(UTC)


def _write_new(out_dir: Path, filename: str, data: bytes, *, overwrite: bool) -> Path:
    """tmp в том же каталоге → fsync → no-clobber link (или replace при overwrite)."""
    final = out_dir / filename
    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{filename}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(tmp, final)
        else:
            os.link(tmp, final)  # FileExistsError, если файл (или симлинк) уже есть
    finally:
        Path(tmp).unlink(missing_ok=True)
    _fsync_dir(out_dir)
    return final


def build_from_dir(
    src_dir: Path, out_dir: Path, *, overwrite: bool = False, created_at: datetime | None = None
) -> DirOutcome:
    """Читает три файла из `src_dir`, собирает пакет и атомарно пишет его в `out_dir`.

    Нарушения входа — значение `Rejected`, и в `out_dir` ничего не появляется. Ошибки записи и
    существующий файл без `overwrite` — `OSError` (`FileExistsError`). `created_at` по умолчанию
    берётся из metadata.json, а при его отсутствии — текущее время UTC.
    """
    read = _read_sources(src_dir)
    if isinstance(read, list):
        return _reject(*read)
    meta = _parse_metadata(read[rp.METADATA])
    if isinstance(meta, list):
        return _reject(*meta)
    outcome = build_package(
        read[rp.RESULT],
        read[rp.SOURCES],
        meta,
        created_at=created_at or _created_at(meta),
    )
    match outcome:
        case Rejected():
            return outcome
        case Built(data=data, filename=filename, report=report):
            return Written(_write_new(out_dir, filename, data, overwrite=overwrite), report)
        case _:
            assert_never(outcome)


# --- CLI -------------------------------------------------------------------------------------

USAGE: Final = (
    "ctrunner-result-package [validate] <архив.zip>\n"
    "ctrunner-result-package build <каталог-источников> <каталог-вывода> "
    "[--overwrite] [--created-at ISO8601-с-поясом]"
)


@final
@dataclass(frozen=True, slots=True)
class _BuildArgs:
    src: Path
    out: Path
    overwrite: bool
    created_at: datetime | None


def _parse_build(args: Sequence[str]) -> _BuildArgs | None:
    positional: list[str] = []
    overwrite = False
    created_at: datetime | None = None
    it = iter(args)
    for arg in it:
        if arg == "--overwrite":
            overwrite = True
        elif arg == "--created-at":
            raw = next(it, None)
            try:
                created_at = None if raw is None else datetime.fromisoformat(raw)
            except ValueError:
                return None
            if created_at is None or created_at.tzinfo is None:
                return None
        elif arg.startswith("--"):
            return None
        else:
            positional.append(arg)
    if len(positional) != 2:
        return None
    return _BuildArgs(Path(positional[0]), Path(positional[1]), overwrite, created_at)


def _print(report: Report, **extra: str) -> None:
    print(json.dumps({**report.to_json(), **extra}, ensure_ascii=False, indent=2))


def _cli_build(args: _BuildArgs) -> int:
    try:
        outcome = build_from_dir(
            args.src, args.out, overwrite=args.overwrite, created_at=args.created_at
        )
    except OSError as e:
        print(f"{type(e).__name__}: {e.strerror or ''}", file=sys.stderr)
        return 1
    match outcome:
        case Written(path=path, report=report):
            _print(report, filename=path.name, path=str(path))
            return 0
        case Rejected(report=report):
            _print(report)
            return 1
        case _:
            assert_never(outcome)


def cli(argv: Sequence[str] | None = None) -> int:
    """`validate` (по умолчанию) и `build`; код 0 — допустимо, 1 — нарушения или I/O, 64 — вызов."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["build"]:
        parsed = _parse_build(args[1:])
        if parsed is not None:
            return _cli_build(parsed)
    elif (args[:1] == ["validate"] and len(args) == 2) or (
        args[:1] not in (["validate"], ["build"]) and len(args) == 1
    ):
        report = rp.validate_file(Path(args[-1]))
        _print(report)
        return 0 if report.ok else 1
    print(USAGE, file=sys.stderr)
    return 64
