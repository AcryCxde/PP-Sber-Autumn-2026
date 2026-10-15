"""Сборщик пакета: build → validate OK, сборка детерминирована, отказ = нарушения валидатора."""

import csv
import hashlib
import io
import json
import os
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ctrunner import result_package as rp
from ctrunner import result_package_build as rb
from ctrunner.result_package import Code

NAME = "coreteams_pilot.1_task-7_run-3_baseline_v0.4.2.zip"
CREATED = datetime(2026, 10, 15, 12, 0, tzinfo=timezone(timedelta(hours=3)))

META: dict[str, Any] = {
    "project_id": "pilot.1",
    "task_id": "task-7",
    "run_id": "run-3",
    "phase": "baseline",
    "build_version": "v0.4.2",
    "environment": "staging",
    "operator_id": "op-1",
    "participant_id": "P-01",
    "input_scope": "Описание задачи без конфиденциальных данных",
    "consent_status": "granted",
    "consent_text_version": "2026-10-01",
    "result_schema_version": "1",
    "limitations": ["Автоматический анализ, экспертной проверки не было"],
}

ROW = {
    "source_id": "S1",
    "title": "Guide",
    "publisher": "Example Org",
    "author": "",
    "published_at": "2025-03-01",
    "accessed_at": "2026-10-14",
    "url": "https://example.org/guide",
    "claim_ids": "C1",
    "source_type": "primary",
    "limitations": "Только для РФ",
}

RESULT = """# Brief

## Decision requested
Стоит ли выбирать вариант A.

## Short answer
Условно да.

## What the evidence says
Fact [S1]: вариант A дешевле.

## Comparison or reasoning
Критерии: цена, риск.

## Unknowns and limitations
Нет данных по региону.

## Recommended next step
Recommendation: проверить цену у поставщика.

## Expert review / escalation
Это не экспертная консультация; проверка специалистом не проводилась.

## Contributions
Роли ИИ-команды: аналитик (AI), редактор (AI). Верифицированной проверки человеком нет.

## Sources
[S1] Guide
"""

NO_SOURCES = RESULT.replace("## Sources\n[S1] Guide\n", "")


def sources_csv(*rows: Mapping[str, str]) -> str:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=rp.SOURCE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def build(
    result: str = RESULT, sources: str | None = None, meta: Mapping[str, Any] = META
) -> rb.BuildOutcome:
    csv_text = sources if sources is not None else sources_csv(ROW)
    return rb.build_package(result.encode(), csv_text.encode(), meta, created_at=CREATED)


def rejected_codes(outcome: rb.BuildOutcome) -> set[Code]:
    assert isinstance(outcome, rb.Rejected)
    return {v.code for v in outcome.report.violations}


def validator_codes(
    result: str = RESULT, sources: str | None = None, meta: Mapping[str, Any] = META
) -> set[Code]:
    """Что скажет валидатор о том же содержимом, упакованном вручную с верным manifest."""
    meta = {**meta, "created_at": CREATED.isoformat()}
    files = {
        rp.RESULT: result.encode(),
        rp.SOURCES: (sources if sources is not None else sources_csv(ROW)).encode(),
        rp.METADATA: json.dumps(meta, ensure_ascii=False).encode(),
    }
    entries = [
        {
            "filename": n,
            "media_type": rp.MEDIA_TYPES[n],
            "byte_size": len(files[n]),
            "sha256": hashlib.sha256(files[n]).hexdigest(),
            "schema_version": "1",
        }
        for n in rp.PAYLOAD
    ]
    files[rp.MANIFEST] = json.dumps({"files": entries}).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)
    return {
        v.code for v in rp.validate_bytes(buf.getvalue(), rp.expected_zip_name(meta)).violations
    }


# --- ядро ------------------------------------------------------------------------------------


def test_good_inputs_build_and_pass_validator() -> None:
    outcome = build()
    assert isinstance(outcome, rb.Built)
    assert outcome.filename == NAME
    assert outcome.report.ok
    again = rp.validate_bytes(outcome.data, outcome.filename)
    assert again.ok
    assert again.source_count == 1


def test_manifest_matches_real_bytes() -> None:
    outcome = build()
    assert isinstance(outcome, rb.Built)
    with zipfile.ZipFile(io.BytesIO(outcome.data)) as z:
        manifest = json.loads(z.read(rp.MANIFEST))
        for entry in manifest["files"]:
            body = z.read(entry["filename"])
            assert entry["byte_size"] == len(body)
            assert entry["sha256"] == hashlib.sha256(body).hexdigest()
            assert entry["media_type"] == rp.MEDIA_TYPES[entry["filename"]]
            assert entry["schema_version"] == rb.MANIFEST_SCHEMA_VERSION
        assert [e["filename"] for e in manifest["files"]] == list(rp.PAYLOAD)
        meta = json.loads(z.read(rp.METADATA))
    assert meta["created_at"] == CREATED.isoformat()


def test_build_is_deterministic_and_zip_is_canonical() -> None:
    first, second = build(), build()
    assert isinstance(first, rb.Built)
    assert isinstance(second, rb.Built)
    assert first.data == second.data
    with zipfile.ZipFile(io.BytesIO(first.data)) as z:
        assert [i.filename for i in z.infolist()] == [*rp.PAYLOAD, rp.MANIFEST]
        for info in z.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.extra == b""
            assert info.comment == b""
            assert info.external_attr == 0o100644 << 16
            assert info.create_system == 3


def test_metadata_key_order_does_not_change_zip() -> None:
    reordered = dict(reversed(list(META.items())))
    a, b = build(), build(meta=reordered)
    assert isinstance(a, rb.Built)
    assert isinstance(b, rb.Built)
    assert a.data == b.data


def test_created_at_overrides_and_must_be_aware() -> None:
    meta = {**META, "created_at": "2000-01-01T00:00:00+00:00"}
    naive = rb.build_package(
        RESULT.encode(),
        sources_csv(ROW).encode(),
        meta,
        created_at=datetime(2026, 10, 15, 12, 0),
    )
    assert rejected_codes(naive) == {Code.METADATA_SCHEMA}
    utc = rb.build_package(
        RESULT.encode(),
        sources_csv(ROW).encode(),
        meta,
        created_at=datetime(2026, 10, 15, 9, 0, tzinfo=UTC),
    )
    assert isinstance(utc, rb.Built)


VIOLATING: dict[str, dict[str, Any]] = {
    "no_section": {"result": RESULT.replace("## Recommended next step\n", "")},
    "unresolved_source": {"result": RESULT.replace("[S1]: вариант", "[S1] и [S9]: вариант")},
    "orphan_source": {
        "sources": sources_csv(ROW, {**ROW, "source_id": "S2", "url": "https://example.org/2"})
    },
    "private_email": {"result": RESULT.replace("Нет данных", "Пишите a@b.com. Нет данных")},
    "private_token": {"result": RESULT + "\napi_key = abcdef123456\n"},
    "bad_phase": {"meta": {**META, "phase": "final"}},
    "missing_meta_field": {"meta": {k: v for k, v in META.items() if k != "operator_id"}},
    "unknown_meta_field": {"meta": {**META, "extra": "x"}},
    "private_meta_field": {"meta": {**META, "contact_email": "x"}},
    "bad_id": {"meta": {**META, "project_id": "pilot_1"}},
    "bad_url": {"sources": sources_csv({**ROW, "url": "http://localhost/x"})},
    "bad_header": {"sources": "a,b\n1,2\n"},
    "no_ai_mark": {"result": RESULT.replace("(AI)", "").replace("ИИ", "Команда")},
}


@pytest.mark.parametrize("case", sorted(VIOLATING))
def test_violation_gives_same_codes_as_validator(case: str) -> None:
    kwargs = VIOLATING[case]
    got = rejected_codes(build(**kwargs))
    assert got
    assert got == validator_codes(**kwargs)


def test_underivable_name_is_rejected_through_metadata() -> None:
    assert Code.METADATA_SCHEMA in rejected_codes(build(meta={**META, "phase": 7}))


def test_non_utf8_input_is_rejected() -> None:
    outcome = rb.build_package(b"\xff\xfe", sources_csv(ROW).encode(), META, created_at=CREATED)
    assert Code.NOT_UTF8 in rejected_codes(outcome)


def test_nan_in_metadata_is_rejected() -> None:
    outcome = build(meta={**META, "limitations": float("nan")})
    assert Code.INVALID_JSON in rejected_codes(outcome)


def test_oversize_input_is_rejected() -> None:
    big = RESULT + "x" * (rp.MAX_MEMBER_BYTES + 1)
    assert Code.MEMBER_TOO_LARGE in rejected_codes(build(result=big))


def test_rejection_does_not_leak_private_values() -> None:
    outcome = build(result=RESULT + "\nsecret@example.org\n")
    assert isinstance(outcome, rb.Rejected)
    assert "secret@example.org" not in json.dumps(outcome.report.to_json())


def test_empty_sources_with_statement_builds() -> None:
    result = (
        RESULT.replace("Условно да.", "Insufficient evidence для вывода.")
        .replace("Fact [S1]: вариант A дешевле.", "Источники не найдены.")
        .replace("[S1] Guide", "Источников нет.")
    )
    meta = {**META, "limitations": "Insufficient evidence: источники не собраны"}
    assert isinstance(build(result, sources_csv(), meta), rb.Built)


# --- оболочка --------------------------------------------------------------------------------


def make_src(root: Path, result: str = RESULT) -> Path:
    src = root / "src"
    src.mkdir()
    (src / "result.md").write_text(result, encoding="utf-8")
    (src / "sources.csv").write_text(sources_csv(ROW), encoding="utf-8")
    meta = {**META, "created_at": CREATED.isoformat()}
    (src / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return src


def make_out(root: Path, name: str = "out") -> Path:
    out = root / name
    out.mkdir()
    return out


def test_build_from_dir_writes_valid_zip(tmp_path: Path) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    result = rb.build_from_dir(src, out)
    assert isinstance(result, rb.Written)
    assert result.path == out / NAME
    assert rp.validate_file(result.path).ok
    assert [p.name for p in out.iterdir()] == [NAME]  # tmp не остаётся
    again = rb.build_from_dir(src, make_out(tmp_path, "out2"))
    assert isinstance(again, rb.Written)
    assert again.path.read_bytes() == result.path.read_bytes()


def test_no_overwrite_without_flag(tmp_path: Path) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    (out / NAME).write_bytes(b"foreign")
    with pytest.raises(FileExistsError):
        rb.build_from_dir(src, out)
    assert (out / NAME).read_bytes() == b"foreign"
    assert [p.name for p in out.iterdir()] == [NAME]
    result = rb.build_from_dir(src, out, overwrite=True)
    assert isinstance(result, rb.Written)
    assert rp.validate_file(out / NAME).ok


def test_rejection_writes_nothing(tmp_path: Path) -> None:
    src, out = make_src(tmp_path, NO_SOURCES), make_out(tmp_path)
    result = rb.build_from_dir(src, out)
    assert isinstance(result, rb.Rejected)
    assert Code.RESULT_SECTIONS in {v.code for v in result.report.violations}
    assert list(out.iterdir()) == []


def unreadable_only(result: rb.DirOutcome) -> bool:
    return isinstance(result, rb.Rejected) and {v.code for v in result.report.violations} == {
        Code.UNREADABLE
    }


def test_missing_file_is_unreadable_violation(tmp_path: Path) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    (src / "sources.csv").unlink()
    assert unreadable_only(rb.build_from_dir(src, out))
    assert list(out.iterdir()) == []


def test_symlinked_input_is_refused(tmp_path: Path) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text(RESULT, encoding="utf-8")
    (src / "result.md").unlink()
    (src / "result.md").symlink_to(outside)
    assert unreadable_only(rb.build_from_dir(src, out))


def test_symlinked_src_dir_is_refused(tmp_path: Path) -> None:
    real, out = make_src(tmp_path), make_out(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert unreadable_only(rb.build_from_dir(link, out))


def test_fifo_input_does_not_hang(tmp_path: Path) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    (src / "result.md").unlink()
    os.mkfifo(src / "result.md")
    assert unreadable_only(rb.build_from_dir(src, out))


def test_oversize_file_is_read_with_ceiling(tmp_path: Path) -> None:
    big = RESULT + "x" * (rp.MAX_MEMBER_BYTES + 10)
    src, out = make_src(tmp_path, big), make_out(tmp_path)
    result = rb.build_from_dir(src, out)
    assert isinstance(result, rb.Rejected)
    assert Code.MEMBER_TOO_LARGE in {v.code for v in result.report.violations}
    assert list(out.iterdir()) == []


@pytest.mark.parametrize("content", [b"{", b"[1]", b'{"a":1,"a":2}', b"\xff"])
def test_broken_metadata_json_is_rejected(tmp_path: Path, content: bytes) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    (src / "metadata.json").write_bytes(content)
    result = rb.build_from_dir(src, out)
    assert isinstance(result, rb.Rejected)
    assert result.report.violations
    assert list(out.iterdir()) == []


def test_metadata_without_created_at_uses_argument(tmp_path: Path) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    (src / "metadata.json").write_text(json.dumps(META), encoding="utf-8")
    assert isinstance(rb.build_from_dir(src, out, created_at=CREATED), rb.Written)


def test_output_dir_must_exist(tmp_path: Path) -> None:
    src = make_src(tmp_path)
    with pytest.raises(OSError):
        rb.build_from_dir(src, tmp_path / "nope")


def test_build_does_not_log_content(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    with caplog.at_level("DEBUG"):
        rb.build_from_dir(src, out)
    assert caplog.records == []


# --- CLI -------------------------------------------------------------------------------------


def test_cli_build_and_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    assert rb.cli(["build", str(src), str(out)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["structural_check"] == "PASS"
    assert report["filename"] == NAME
    zip_path = out / NAME
    assert rb.cli(["validate", str(zip_path)]) == 0
    assert rb.cli([str(zip_path)]) == 0  # прежний вызов без подкоманды
    capsys.readouterr()
    assert rb.cli(["build", str(src), str(out)]) == 1  # файл уже есть
    assert "FileExistsError" in capsys.readouterr().err
    assert rb.cli(["build", str(src), str(out), "--overwrite"]) == 0


def test_cli_build_rejection_exit_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, out = make_src(tmp_path, NO_SOURCES), make_out(tmp_path)
    assert rb.cli(["build", str(src), str(out)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["structural_check"] == "FAIL"
    assert "filename" not in report
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["build"],
        ["build", "a"],
        ["build", "a", "b", "c"],
        ["build", "a", "b", "--bogus"],
        ["build", "a", "b", "--created-at"],
        ["build", "a", "b", "--created-at", "not-a-date"],
        ["build", "a", "b", "--created-at", "2026-10-15T12:00:00"],
        ["validate"],
        ["validate", "a", "b"],
        ["a", "b"],
    ],
)
def test_cli_usage_errors_exit_64(argv: list[str]) -> None:
    assert rb.cli(argv) == 64


def test_cli_created_at_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src, out = make_src(tmp_path), make_out(tmp_path)
    assert rb.cli(["build", str(src), str(out), "--created-at", "2026-10-15T09:00:00+00:00"]) == 0
    capsys.readouterr()
    with zipfile.ZipFile(out / NAME) as z:
        assert json.loads(z.read("metadata.json"))["created_at"] == "2026-10-15T09:00:00+00:00"
