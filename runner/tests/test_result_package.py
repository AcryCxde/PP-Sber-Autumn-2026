"""Валидатор пакета результата: хороший пакет и по тесту на каждое нарушение. Без сети."""

import csv
import hashlib
import io
import json
import stat
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from ctrunner import result_package as rp
from ctrunner.result_package import Code, Report

NAME = "coreteams_pilot.1_task-7_run-3_baseline_v0.4.2.zip"

META: dict[str, object] = {
    "project_id": "pilot.1",
    "task_id": "task-7",
    "run_id": "run-3",
    "phase": "baseline",
    "build_version": "v0.4.2",
    "environment": "staging",
    "created_at": "2026-10-15T12:00:00+03:00",
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


def sources_csv(*rows: Mapping[str, str]) -> str:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=rp.SOURCE_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def manifest_for(files: Mapping[str, bytes]) -> bytes:
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
    return json.dumps({"files": entries}).encode()


def payload(
    result: str = RESULT, sources: str | None = None, meta: Mapping[str, object] = META
) -> dict[str, bytes]:
    files = {
        rp.RESULT: result.encode(),
        rp.SOURCES: (sources if sources is not None else sources_csv(ROW)).encode(),
        rp.METADATA: json.dumps(meta, ensure_ascii=False).encode(),
    }
    files[rp.MANIFEST] = manifest_for(files)
    return files


def build_zip(
    files: Mapping[str, bytes], infos: Mapping[str, zipfile.ZipInfo] | None = None
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in files.items():
            if infos and name in infos:
                z.writestr(infos[name], body)
            else:
                z.writestr(name, body)
    return buf.getvalue()


def codes(report: Report) -> set[Code]:
    return {v.code for v in report.violations}


def check(files: Mapping[str, bytes], name: str | None = NAME) -> Report:
    return rp.validate_bytes(build_zip(files), name)


def meta_with(**changes: object) -> dict[str, object]:
    return {**META, **changes}


def test_good_package_passes() -> None:
    report = check(payload())
    assert report.violations == ()
    assert report.ok
    assert report.source_count == 1
    assert report.not_verified  # честно: что не проверялось
    assert report.to_json()["structural_check"] == "PASS"


def test_good_package_from_file(tmp_path: Path) -> None:
    path = tmp_path / NAME
    path.write_bytes(build_zip(payload()))
    assert rp.validate(path).ok
    nameless = rp.validate(path.read_bytes())
    assert nameless.ok
    assert any("имя ZIP" in n for n in nameless.not_verified)


def test_unreadable_file(tmp_path: Path) -> None:
    assert codes(rp.validate_file(tmp_path / "nope.zip")) == {Code.UNREADABLE}


def test_empty_sources_with_explicit_statement_passes() -> None:
    result = (
        RESULT.replace("Условно да.", "Insufficient evidence для вывода.")
        .replace("Fact [S1]: вариант A дешевле.", "Источники не найдены.")
        .replace("[S1] Guide", "Источников нет.")
    )
    meta = meta_with(limitations="Insufficient evidence: источники не собраны")
    assert check(payload(result, sources_csv(), meta)).violations == ()


# --- контейнер -------------------------------------------------------------------------------


def test_not_a_zip() -> None:
    assert codes(rp.validate_bytes(b"hello", NAME)) == {Code.NOT_A_ZIP}


def test_zip_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "MAX_ZIP_BYTES", 10)
    assert codes(check(payload())) == {Code.ZIP_TOO_LARGE}


def test_too_many_entries() -> None:
    files = payload() | {f"x{i}.txt": b"x" for i in range(rp.MAX_ENTRIES)}
    assert Code.TOO_MANY_ENTRIES in codes(check(files))


@pytest.mark.parametrize("bad", ["../evil.md", "/abs.md", "a/../b.md", "dir\\x.md", "./x.md"])
def test_unsafe_path(bad: str) -> None:
    assert Code.UNSAFE_PATH in codes(check(payload() | {bad: b"x"}))


def test_nested_member_is_extra_file() -> None:
    assert Code.MEMBERS_MISMATCH in codes(check(payload() | {"sub/result.md": b"x"}))


def test_symlink_member() -> None:
    link = zipfile.ZipInfo(rp.RESULT)
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    report = rp.validate_bytes(build_zip(payload(), {rp.RESULT: link}), NAME)
    assert Code.SPECIAL_FILE in codes(report)


def test_duplicate_member() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in payload().items():
            z.writestr(name, body)
        with pytest.warns(UserWarning, match="Duplicate name"):
            z.writestr(rp.RESULT, b"second")
    assert Code.DUPLICATE_MEMBER in codes(rp.validate_bytes(buf.getvalue(), NAME))


def test_encrypted_member() -> None:
    raw = bytearray(build_zip(payload()))
    index = raw.index(b"PK\x01\x02")
    raw[index + 8] |= 1  # бит 0 general purpose flag в центральном каталоге
    assert Code.ENCRYPTED in codes(rp.validate_bytes(bytes(raw), NAME))


@pytest.mark.parametrize("missing", sorted(rp.MEMBERS))
def test_missing_member(missing: str) -> None:
    files = {k: v for k, v in payload().items() if k != missing}
    assert Code.MEMBERS_MISMATCH in codes(check(files))


def test_extra_member() -> None:
    assert Code.MEMBERS_MISMATCH in codes(check(payload() | {"notes.txt": b"x"}))


def test_member_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "MAX_MEMBER_BYTES", 100)
    assert Code.MEMBER_TOO_LARGE in codes(check(payload(RESULT + "x" * 500)))


def test_member_header_lies_about_size(monkeypatch: pytest.MonkeyPatch) -> None:
    # Заголовок занижает размер: срабатывает потолок чтения (или ошибка распаковки).
    monkeypatch.setattr(rp, "MAX_MEMBER_BYTES", 100)
    raw = bytearray(build_zip(payload(RESULT + "x" * 500)))
    index = raw.index(b"PK\x01\x02")
    raw[index + 24 : index + 28] = (1).to_bytes(4, "little")  # file_size в центральном каталоге
    found = codes(rp.validate_bytes(bytes(raw), NAME))
    assert found & {Code.MEMBER_TOO_LARGE, Code.ZIP_CORRUPT}


def test_total_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "MAX_TOTAL_BYTES", 300)
    assert Code.TOTAL_TOO_LARGE in codes(check(payload()))


def test_compression_bomb() -> None:
    files = payload(RESULT + "\n" + "a" * (2 * rp.RATIO_FLOOR) * 10)
    assert Code.COMPRESSION_RATIO in codes(check(files))


def test_corrupt_member() -> None:
    raw = bytearray(build_zip(payload()))
    index = raw.index(b"PK\x03\x04") + 30 + len(rp.RESULT) + 4
    raw[index] ^= 0xFF
    assert Code.ZIP_CORRUPT in codes(rp.validate_bytes(bytes(raw), NAME))


# --- имя, кодировка, JSON -------------------------------------------------------------------


def test_bad_zip_name() -> None:
    assert codes(check(payload(), "result.zip")) == {Code.BAD_ZIP_NAME}


def test_zip_name_disagrees_with_metadata() -> None:
    other = "coreteams_pilot.1_task-7_run-3_prototype_v0.4.2.zip"
    assert codes(check(payload(), other)) == {Code.ZIP_NAME_MISMATCH}


def test_not_utf8() -> None:
    files = payload()
    files[rp.RESULT] = b"\xff\xfe\x00bad"
    assert Code.NOT_UTF8 in codes(check(files))


def test_bom_rejected() -> None:
    files = payload()
    files[rp.SOURCES] = b"\xef\xbb\xbf" + files[rp.SOURCES]
    assert Code.BOM in codes(check(files))


def test_empty_result() -> None:
    files = payload()
    files[rp.RESULT] = b"  \n"
    assert Code.EMPTY_MEMBER in codes(check(files))


@pytest.mark.parametrize("raw", [b"{", b"[1]", b'{"a": 1, "a": 2}', b'{"a": NaN}'])
def test_invalid_json(raw: bytes) -> None:
    files = payload()
    files[rp.METADATA] = raw
    assert Code.INVALID_JSON in codes(check(files))


# --- metadata ---------------------------------------------------------------------------------


def test_metadata_missing_field() -> None:
    meta = {k: v for k, v in META.items() if k != "environment"}
    assert Code.METADATA_SCHEMA in codes(check(payload(meta=meta)))


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "pilot"},
        {"created_at": "2026-10-15T12:00:00"},  # без часового пояса
        {"created_at": "вчера"},
        {"run_id": "run_3"},  # подчёркивание зарезервировано разделителем имени
        {"environment": ""},
        {"limitations": []},
        {"limitations": 5},
        {"extra": "x"},
        {"consent_text_version": ""},
    ],
)
def test_metadata_schema_violations(changes: dict[str, object]) -> None:
    assert Code.METADATA_SCHEMA in codes(check(payload(meta=meta_with(**changes))))


def test_participant_without_consent_text_version() -> None:
    meta = {k: v for k, v in META.items() if k != "consent_text_version"}
    assert Code.METADATA_SCHEMA in codes(check(payload(meta=meta)))


def test_contact_field_in_metadata_is_private() -> None:
    assert Code.PRIVATE_DATA in codes(check(payload(meta=meta_with(participant_email="x"))))


# --- manifest ---------------------------------------------------------------------------------


def with_manifest(change: Callable[[dict[str, object]], None]) -> dict[str, bytes]:
    files = payload()
    manifest = json.loads(files[rp.MANIFEST])
    change(manifest)
    files[rp.MANIFEST] = json.dumps(manifest).encode()
    return files


def entries(manifest: dict[str, object]) -> list[dict[str, object]]:
    value = manifest["files"]
    assert isinstance(value, list)
    return value


def test_manifest_hash_and_size_mismatch() -> None:
    files = payload()
    files[rp.RESULT] = files[rp.RESULT] + b" "
    found = codes(check(files))
    assert {Code.MANIFEST_HASH, Code.MANIFEST_SIZE} <= found


def test_manifest_hash_mismatch_same_size() -> None:
    files = payload()
    files[rp.RESULT] = files[rp.RESULT].replace(b"A", b"B", 1)
    assert codes(check(files)) == {Code.MANIFEST_HASH}


def test_manifest_self_hash_forbidden() -> None:
    def add(m: dict[str, object]) -> None:
        entries(m).append({**entries(m)[0], "filename": "manifest.json"})

    assert Code.MANIFEST_SCHEMA in codes(check(with_manifest(add)))


def test_manifest_missing_payload_entry() -> None:
    def drop(m: dict[str, object]) -> None:
        entries(m).pop()

    assert Code.MANIFEST_SCHEMA in codes(check(with_manifest(drop)))


@pytest.mark.parametrize(
    ("field", "value"),
    [("media_type", "text/plain"), ("sha256", "ABC"), ("byte_size", "12"), ("schema_version", "")],
)
def test_manifest_entry_fields(field: str, value: object) -> None:
    def change(m: dict[str, object]) -> None:
        entries(m)[0][field] = value

    assert Code.MANIFEST_SCHEMA in codes(check(with_manifest(change)))


def test_manifest_extra_key() -> None:
    assert Code.MANIFEST_SCHEMA in codes(check(with_manifest(lambda m: m.update(extra=1))))


# --- sources.csv ---------------------------------------------------------------------------------


def test_sources_wrong_header() -> None:
    bad = sources_csv(ROW).replace("source_id", "id", 1)
    assert Code.SOURCES_HEADER in codes(check(payload(sources=bad)))


def test_sources_short_row() -> None:
    bad = sources_csv(ROW) + "S2,only\n"
    assert Code.SOURCES_ROW in codes(check(payload(sources=bad)))


@pytest.mark.parametrize(
    "changes",
    [
        {"url": ""},
        {"url": "ftp://example.org/x"},
        {"url": "https://example.org/a b"},
        {"url": "https://user:pw@example.org/x"},
        {"url": "http://localhost:8000/x"},
        {"url": "http://10.0.0.1/x"},
        {"source_id": ""},
        {"source_id": "src-1"},
        {"title": ""},
        {"accessed_at": ""},
        {"accessed_at": "вчера"},
        {"claim_ids": ""},
        {"limitations": ""},
        {"source_type": ""},
    ],
)
def test_sources_row_violations(changes: dict[str, str]) -> None:
    assert Code.SOURCES_ROW in codes(check(payload(sources=sources_csv({**ROW, **changes}))))


def test_sources_duplicate_id() -> None:
    assert Code.SOURCES_DUPLICATE_ID in codes(check(payload(sources=sources_csv(ROW, ROW))))


def test_sources_empty_without_statement() -> None:
    result = RESULT.replace("Fact [S1]: вариант A дешевле.", "Факты.").replace("[S1] Guide", "нет")
    assert Code.SOURCES_EMPTY_UNSTATED in codes(check(payload(result, sources_csv())))


def test_sources_empty_statement_must_be_in_metadata_too() -> None:
    result = (
        RESULT.replace("Условно да.", "Insufficient evidence.")
        .replace("Fact [S1]: вариант A дешевле.", "Факты.")
        .replace("[S1] Guide", "нет")
    )
    assert Code.SOURCES_EMPTY_UNSTATED in codes(check(payload(result, sources_csv())))


# --- result.md: разделы и ссылки ----------------------------------------------------------------


def test_missing_section() -> None:
    result = RESULT.replace("## Unknowns and limitations\nНет данных по региону.\n\n", "")
    assert Code.RESULT_SECTIONS in codes(check(payload(result)))


def test_empty_section() -> None:
    assert Code.RESULT_SECTIONS in codes(
        check(payload(RESULT.replace("Нет данных по региону.", "")))
    )


def test_bare_not_applicable() -> None:
    result = RESULT.replace("Критерии: цена, риск.", "Not applicable.")
    assert Code.RESULT_SECTIONS in codes(check(payload(result)))


def test_not_applicable_with_reason_ok() -> None:
    result = RESULT.replace("Критерии: цена, риск.", "Not applicable: один вариант.")
    assert check(payload(result)).ok


def test_sections_out_of_order() -> None:
    result = (
        RESULT.replace("## Short answer", "## TMP")
        .replace("## Decision requested", "## Short answer")
        .replace("## TMP", "## Decision requested")
    )
    assert Code.RESULT_SECTIONS in codes(check(payload(result)))


def test_duplicate_section() -> None:
    assert Code.RESULT_SECTIONS in codes(check(payload(RESULT + "\n## Sources\nещё\n")))


def test_heading_in_code_fence_does_not_count() -> None:
    result = RESULT.replace("## Contributions", "```\n## Contributions\n```\n## Other")
    assert Code.RESULT_SECTIONS in codes(check(payload(result)))


def test_numbered_bold_headings_accepted() -> None:
    result = RESULT.replace("## Short answer", "## 2. **Short answer**:")
    assert check(payload(result)).ok


def test_unresolved_citation() -> None:
    result = RESULT.replace("Fact [S1]", "Fact [S1][S9]")
    assert Code.SOURCE_UNRESOLVED in codes(check(payload(result)))


def test_orphan_source() -> None:
    report = check(payload(sources=sources_csv(ROW, {**ROW, "source_id": "S2"})))
    assert Code.SOURCE_ORPHAN in codes(report)
    assert Code.SOURCE_NOT_LISTED in codes(report)


def test_source_only_in_sources_section_is_orphan() -> None:
    result = RESULT.replace("Fact [S1]: вариант A дешевле.", "Факты без ссылок.")
    report = check(payload(result))
    assert Code.SOURCE_ORPHAN in codes(report)
    assert Code.NO_CITATIONS in codes(report)


def test_listed_source_missing_in_csv() -> None:
    result = RESULT.replace("[S1] Guide", "[S1] Guide\n[S4] Ghost")
    assert Code.SOURCE_UNRESOLVED in codes(check(payload(result)))


# --- приватные данные и роли ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "leak",
    [
        "пишите ivan.petrov@example.com",
        "файл лежит в /home/ivan/work/notes.md",
        "см. /Users/ivan/notes",
        "ключ sk-abcdefghijklmnopqrstuvwx",
        "token = abcdef123456",
        "Authorization: Bearer abcdefghijklmnopqrstuv",
        "-----BEGIN RSA PRIVATE KEY-----",
        "звоните +7 (916) 123-45-67",
        "звоните 8 916 123 45 67",
    ],
)
def test_private_data_in_result(leak: str) -> None:
    report = check(payload(RESULT.replace("Критерии: цена, риск.", leak)))
    assert Code.PRIVATE_DATA in codes(report)
    assert all(leak not in v.detail for v in report.violations)  # утечку не тиражируем


def test_private_data_in_sources_and_metadata() -> None:
    row = {**ROW, "limitations": "автор a@b.io"}
    assert Code.PRIVATE_DATA in codes(check(payload(sources=sources_csv(row))))
    assert Code.PRIVATE_DATA in codes(check(payload(meta=meta_with(input_scope="от a@b.io"))))
    row = {**ROW, "url": "https://example.org/x?access_token=abcd1234"}
    assert Code.PRIVATE_DATA in codes(check(payload(sources=sources_csv(row))))


def test_path_like_segment_inside_url_is_not_a_leak() -> None:
    row = {**ROW, "url": "https://example.org/home/guide"}
    assert check(payload(sources=sources_csv(row))).ok


def test_contributions_must_say_ai() -> None:
    result = RESULT.replace(
        "Роли ИИ-команды: аналитик (AI), редактор (AI).", "Аналитик и редактор."
    )
    assert Code.AI_NOT_MARKED in codes(check(payload(result)))


@pytest.mark.parametrize(
    "name",
    ["Иванов И.И.", "И. И. Иванов", "Иван Иванович Петров", "Dr. Smith", "@ivan_p"],
)
def test_contributions_person_named_as_role(name: str) -> None:
    result = RESULT.replace("аналитик (AI)", f"аналитик (AI) - {name}")
    assert Code.PERSON_AS_ROLE in codes(check(payload(result)))


@pytest.mark.parametrize(
    "claim",
    [
        "Результат проверен юристом.",
        "Reviewed by a qualified lawyer.",
        "Проверил эксперт по налогам.",
    ],
)
def test_human_review_claim_without_evidence(claim: str) -> None:
    result = RESULT.replace(
        "Это не экспертная консультация; проверка специалистом не проводилась.", claim
    )
    assert Code.HUMAN_REVIEW_CLAIM in codes(check(payload(result)))


def test_negated_review_statement_is_fine() -> None:
    result = RESULT.replace(
        "Это не экспертная консультация; проверка специалистом не проводилась.",
        "Not reviewed by a lawyer.",
    )
    assert check(payload(result)).ok


# --- CLI -------------------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / NAME
    good.write_bytes(build_zip(payload()))
    assert rp.cli([str(good)]) == 0
    assert json.loads(capsys.readouterr().out)["structural_check"] == "PASS"
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    assert rp.cli([str(bad)]) == 1
    assert json.loads(capsys.readouterr().out)["violations"][0]["code"] == "not_a_zip"
    assert rp.cli([]) == 64
