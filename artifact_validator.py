#!/usr/bin/env python3
"""Validate CSV/JSON artifacts, record evidence, and verify persistence."""

import argparse
import csv
import datetime
import hashlib
import json
from pathlib import Path


INVALID_TEXT_VALUES = {"nan"}
LOG_HEADER = (
    "| Step | Фаза | Timestamp | Run ID | File Path / URI в workspace | "
    "SHA-256 | Size (bytes) | Соответствие критериям | "
    "Комментарий / Зафиксированные остановки |\n"
    "|---:|---|---|---|---|---|---:|---|---|\n"
)


def utc_timestamp() -> str:
    """Return an ISO 8601 timestamp in UTC."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """Calculate SHA-256 without loading the whole file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_required_fields(value: str) -> list[str]:
    """Parse a comma-separated field list while preserving order."""
    fields = [item.strip() for item in value.split(",") if item.strip()]
    return list(dict.fromkeys(fields))


def invalid_value(value: object) -> bool:
    """Reject JSON null, IEEE NaN, and textual NaN values."""
    if value is None:
        return True
    if isinstance(value, float) and value != value:
        return True
    return isinstance(value, str) and value.strip().lower() in INVALID_TEXT_VALUES


def validate_csv(path: Path, required_fields: list[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source, strict=True)
            headers = reader.fieldnames
            if not headers:
                return False, ["CSV header is missing"]
            normalized = [header.strip() for header in headers]
            if any(not header for header in normalized):
                errors.append("CSV contains an empty column name")
            if len(set(normalized)) != len(normalized):
                errors.append("CSV contains duplicate column names")

            missing = [field for field in required_fields if field not in normalized]
            if missing:
                errors.append("missing required columns: " + ", ".join(missing))

            row_count = 0
            for row_number, row in enumerate(reader, start=2):
                row_count += 1
                if None in row:
                    errors.append(f"row {row_number} has extra fields")
                missing_cells = [name for name, value in row.items() if value is None]
                if missing_cells:
                    errors.append(
                        f"row {row_number} has fewer fields than the header"
                    )
                nan_cells = [
                    name
                    for name, value in row.items()
                    if name is not None and invalid_value(value)
                ]
                if nan_cells:
                    errors.append(
                        f"row {row_number} contains NaN in: {', '.join(nan_cells)}"
                    )
            if row_count == 0:
                errors.append("CSV contains no data rows")
    except UnicodeDecodeError:
        errors.append("file is not valid UTF-8")
    except csv.Error as error:
        errors.append(f"invalid CSV syntax: {error}")
    return not errors, errors


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def validate_json(path: Path, required_fields: list[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            payload = json.load(source, parse_constant=reject_json_constant)
    except UnicodeDecodeError:
        return False, ["file is not valid UTF-8"]
    except (json.JSONDecodeError, ValueError) as error:
        return False, [f"invalid JSON syntax: {error}"]

    if isinstance(payload, dict):
        records = [payload]
    elif isinstance(payload, list):
        if not payload:
            return False, ["JSON array is empty"]
        if not all(isinstance(item, dict) for item in payload):
            return False, ["JSON array items must all be objects"]
        records = payload
    else:
        return False, ["JSON root must be an object or an array of objects"]

    for index, record in enumerate(records, start=1):
        missing = [field for field in required_fields if field not in record]
        if missing:
            errors.append(f"record {index} is missing keys: {', '.join(missing)}")
        invalid = [key for key, value in record.items() if invalid_value(value)]
        if invalid:
            errors.append(
                f"record {index} contains null/NaN in: {', '.join(invalid)}"
            )
    return not errors, errors


def validate_artifact(path: Path, required_fields: list[str]) -> tuple[bool, list[str]]:
    """Validate file existence, encoding, syntax, and basic tabular schema."""
    if not path.exists():
        return False, ["file does not exist"]
    if not path.is_file():
        return False, ["path is not a regular file"]
    if path.stat().st_size == 0:
        return False, ["file is empty"]
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return validate_csv(path, required_fields)
    if suffix == ".json":
        return validate_json(path, required_fields)
    return False, [f"unsupported format: {suffix or '<no extension>'}; use CSV or JSON"]


def write_snapshot(snapshot_path: Path, artifact: Path, run_id: str, digest: str) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "run_id": run_id,
        "artifact_path": str(artifact.resolve()),
        "sha256": digest,
        "size_bytes": artifact.stat().st_size,
        "captured_at": utc_timestamp(),
    }
    snapshot_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def verify_snapshot(
    snapshot_path: Path, artifact: Path, run_id: str, digest: str
) -> tuple[bool, str]:
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"persistence snapshot not found: {snapshot_path}"
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return False, f"invalid persistence snapshot: {error}"

    mismatches = []
    if data.get("run_id") != run_id:
        mismatches.append("Run ID changed")
    if data.get("artifact_path") != str(artifact.resolve()):
        mismatches.append("artifact path changed")
    if data.get("sha256") != digest:
        mismatches.append("SHA-256 changed")
    if data.get("size_bytes") != artifact.stat().st_size:
        mismatches.append("file size changed")
    if mismatches:
        return False, "; ".join(mismatches)
    return True, "persistence verified: path, size, Run ID, and SHA-256 match"


def markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def append_log(
    log_path: Path,
    phase: str,
    run_id: str,
    artifact: Path,
    digest: str,
    size: int,
    passed: bool,
    comment: str,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists() or log_path.stat().st_size == 0:
        log_path.write_text("# Журнал наблюдений\n\n" + LOG_HEADER, encoding="utf-8")

    existing = log_path.read_text(encoding="utf-8")
    step = sum(1 for line in existing.splitlines() if line.startswith("| ") and line[2:3].isdigit()) + 1
    values = [
        step,
        phase,
        utc_timestamp(),
        run_id,
        str(artifact.resolve()),
        digest or "—",
        size,
        "Pass" if passed else "Fail",
        comment or "—",
    ]
    row = "| " + " | ".join(markdown_cell(value) for value in values) + " |\n"
    with log_path.open("a", encoding="utf-8", newline="") as target:
        target.write(row)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a CSV/JSON artifact and append evidence to a Markdown log."
    )
    parser.add_argument("artifact", type=Path, help="path to the CSV or JSON artifact")
    parser.add_argument("--run-id", required=True, help="run identifier")
    parser.add_argument(
        "--required-fields",
        default="",
        help="comma-separated required CSV columns or JSON keys",
    )
    parser.add_argument(
        "--phase",
        default="первичный прогон",
        choices=(
            "первичный прогон",
            "изменение требования",
            "повторное открытие после паузы",
        ),
    )
    parser.add_argument(
        "--log", type=Path, default=Path("observation_log.md"), help="Markdown log path"
    )
    parser.add_argument(
        "--persistence",
        choices=("none", "snapshot", "verify"),
        default="none",
        help="save or verify a pre-pause persistence snapshot",
    )
    parser.add_argument(
        "--snapshot-file",
        type=Path,
        default=Path("artifact_snapshot.json"),
        help="snapshot path used by --persistence",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    artifact = args.artifact
    required_fields = parse_required_fields(args.required_fields)
    valid, errors = validate_artifact(artifact, required_fields)

    digest = ""
    size = 0
    if artifact.is_file():
        digest = sha256_file(artifact)
        size = artifact.stat().st_size

    notes = list(errors)
    persistence_ok = True
    if valid and args.persistence == "snapshot":
        write_snapshot(args.snapshot_file, artifact, args.run_id, digest)
        notes.append(f"persistence snapshot saved: {args.snapshot_file}")
    elif valid and args.persistence == "verify":
        persistence_ok, message = verify_snapshot(
            args.snapshot_file, artifact, args.run_id, digest
        )
        notes.append(message)

    passed = valid and persistence_ok
    append_log(
        args.log,
        args.phase,
        args.run_id,
        artifact,
        digest,
        size,
        passed,
        "; ".join(notes) if notes else "validation passed",
    )
    print(
        json.dumps(
            {
                "status": "Pass" if passed else "Fail",
                "run_id": args.run_id,
                "path": str(artifact.resolve()),
                "sha256": digest or None,
                "size_bytes": size,
                "errors": errors,
                "persistence": args.persistence,
            },
            ensure_ascii=False,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
