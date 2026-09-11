"""Manifest, raw-record, deduplication, and validation regressions."""

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from sichuan_weather.validation import validate_dataset

from tests.helpers import make_manifest, make_record, write_dataset


def _record_bytes(record: dict[str, object]) -> bytes:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_raw_records(
    root: Path,
    data: bytes,
    *,
    manifest: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    input_path, manifest_path = write_dataset(
        root,
        [],
        manifest=manifest,
    )
    input_path.write_bytes(data)
    return input_path, manifest_path


def _assert_count_identities(result: object) -> None:
    counts = result.counts
    assert counts.input_records is not None
    assert counts.individually_valid_records is not None
    assert counts.invalid_records is not None
    assert counts.conflict_rows is not None
    assert counts.duplicate_rows_removed is not None
    assert counts.eligible_unique_records is not None
    assert counts.input_records == (
        counts.individually_valid_records + counts.invalid_records
    )
    assert counts.eligible_unique_records == (
        counts.individually_valid_records
        - counts.conflict_rows
        - counts.duplicate_rows_removed
    )
    assert counts.eligible_unique_records == len(result.canonical_records)


def _contract_issue_order_key(issue: object) -> tuple[object, ...]:
    source_lines = issue.source_lines
    first_line = source_lines[0] if source_lines else -1
    logical_key = issue.logical_key
    logical_parts = (
        ("", "", "")
        if logical_key is None
        else (logical_key[0], logical_key[1], logical_key[2].isoformat())
    )
    return (
        0 if not source_lines else 1,
        first_line,
        issue.rule_id,
        issue.field or "",
        *logical_parts,
        issue.message,
    )


def test_a_a_b_is_one_whole_conflict_group(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", "0~1℃"),
            make_record("2026-12-30", "0 ～ 1 °C"),
            make_record("12-30", "0~2℃"),
        ],
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.individually_valid_records == 3
    assert result.counts.invalid_records == 0
    assert result.counts.conflict_keys == 1
    assert result.counts.conflict_rows == 3
    assert result.counts.duplicate_rows_removed == 0
    assert result.counts.eligible_unique_records == 0
    assert result.canonical_records == ()
    conflict = next(issue for issue in result.issues if issue.rule_id == "Q10")
    assert conflict.severity == "ERROR"
    assert conflict.source_lines == (1, 2, 3)


@pytest.mark.parametrize(
    ("second_temperature", "second_weather", "expected_fields"),
    [
        ("1~2℃", "Cloudy", ("temp_min_c",)),
        ("0~3℃", "Cloudy", ("temp_max_c",)),
        ("0~2℃", "Rain", ("weather",)),
        (
            "1~3℃",
            "Rain",
            ("temp_min_c", "temp_max_c", "weather"),
        ),
    ],
)
def test_q10_names_only_actual_differing_fields_in_frozen_order(
    tmp_path: Path,
    second_temperature: str,
    second_weather: str,
    expected_fields: tuple[str, ...],
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", "0~2℃", "Cloudy"),
            make_record("2026-12-30", second_temperature, second_weather),
        ],
    )

    result = validate_dataset(input_path, manifest_path)
    conflicts = [issue for issue in result.issues if issue.rule_id == "Q10"]

    assert not result.is_valid
    assert result.counts.conflict_keys == 1
    assert result.counts.conflict_rows == 2
    assert result.counts.duplicate_rows_removed == 0
    assert result.counts.eligible_unique_records == 0
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.logical_key == (
        "test-snapshot",
        "Chengdu",
        date(2026, 12, 30),
    )
    assert conflict.source_lines == (1, 2)
    assert conflict.field is None
    assert conflict.message == (
        "logical-key group contains conflicting normalized values; differing fields: "
        + ", ".join(expected_fields)
    )
    assert "0~2" not in conflict.message
    assert second_temperature not in conflict.message
    assert second_weather not in conflict.message


def test_non_object_json_value_is_an_invalid_record(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(tmp_path, [])
    input_path.write_text("null\n", encoding="utf-8", newline="\n")

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.input_records == 1
    assert result.counts.individually_valid_records == 0
    assert result.counts.invalid_records == 1
    assert any(
        issue.rule_id == "Q02"
        and issue.severity == "ERROR"
        and issue.source_lines == (1,)
        for issue in result.issues
    )


def test_manifest_collects_independent_field_errors(tmp_path: Path) -> None:
    manifest = make_manifest()
    manifest.pop("source_description")
    manifest["schema_version"] = 2
    snapshot = manifest["snapshots"][0]
    snapshot.pop("valid_end")
    snapshot["snapshot_id"] = 42
    snapshot["snapshot_date"] = "not-a-date"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "2~N/A")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)
    issue_pairs = {(issue.rule_id, issue.field) for issue in result.issues}

    assert not result.is_valid
    assert ("Q02", "source_description") in issue_pairs
    assert ("Q03", "schema_version") in issue_pairs
    assert ("Q02", "snapshots[0].valid_end") in issue_pairs
    assert ("Q02", "snapshots[0].snapshot_id") in issue_pairs
    assert ("Q03", "snapshots[0].snapshot_date") in issue_pairs
    assert any(
        issue.rule_id == "Q06"
        and issue.field == "temperature_raw"
        and issue.source_lines == (1,)
        for issue in result.issues
    )


def test_bad_snapshot_reference_does_not_hide_independent_date_error(
    tmp_path: Path,
) -> None:
    record = make_record("not-a-date", "0~1℃")
    record["snapshot_id"] = "not-declared"
    input_path, manifest_path = write_dataset(tmp_path, [record])

    result = validate_dataset(input_path, manifest_path)
    issue_pairs = {(issue.rule_id, issue.field) for issue in result.issues}

    assert ("Q03", "snapshot_id") in issue_pairs
    assert ("Q04", "forecast_date_raw") in issue_pairs


def test_valid_manifest_dimensions_survive_an_unrelated_bad_snapshot(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["snapshots"].append(
        {
            "snapshot_id": "broken-snapshot",
            "snapshot_date": "not-a-date",
            "valid_start": "2027-02-01",
            "valid_end": "2027-02-02",
        }
    )
    record = make_record("2027-02-01", "0~1℃")
    record["location"] = "NotDeclared"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [record],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)
    issue_pairs = {(issue.rule_id, issue.field) for issue in result.issues}

    assert ("Q03", "snapshots[1].snapshot_date") in issue_pairs
    assert ("Q03", "location") in issue_pairs
    assert ("Q04", "forecast_date_raw") in issue_pairs


def test_invalid_utf8_line_does_not_hide_parseable_later_records(
    tmp_path: Path,
) -> None:
    input_path, manifest_path = write_dataset(tmp_path, [])
    first = json.dumps(make_record("12-30", "0~1℃"), ensure_ascii=False).encode()
    third = json.dumps(make_record("12-31", "1~2℃"), ensure_ascii=False).encode()
    input_path.write_bytes(first + b"\n\xff\n" + third + b"\n")

    result = validate_dataset(input_path, manifest_path)

    assert not result.validation_complete
    assert not result.is_valid
    assert result.counts.input_records == 3
    assert result.counts.individually_valid_records == 2
    assert result.counts.invalid_records == 1
    assert len(result.canonical_records) == 2
    assert any(
        issue.rule_id == "Q01" and issue.source_lines == (2,)
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "description",
    [
        "Synthetic test data based on 2025 historical weather observations.",
        "Not fabricated historical observations used as synthetic test data.",
        "Synthetic test data derived from actual weather observations.",
        "Not synthetic test data.",
        "This is not synthetic; it is real collection data for a test.",
        "Synthetic test data gathered from live weather stations.",
        "Synthetic test data，来自真实历史天气观测。",
        "No claim this is synthetic test data.",
        "Synthetic contest records.",
        "Synthetic demolition records.",
        "Synthetic test data copied from production sensor telemetry.",
        "Synthetic data, not for testing or demonstration.",
        "合成数据，不用于测试或演示。",
        "Synthetic test data fetched from an official weather API.",
        "Synthetic test data imported from a government weather archive.",
        "Synthetic test data replayed from station telemetry.",
    ],
)
def test_manifest_rejects_a_historical_observation_claim(
    tmp_path: Path,
    description: str,
) -> None:
    manifest = make_manifest()
    manifest["source_description"] = description
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert any(
        issue.rule_id == "Q03" and issue.field == "source_description"
        for issue in result.issues
    )


@pytest.mark.parametrize(
    "description",
    [
        "Synthetic test data; not historical weather observations.",
        "No external sources were used for this synthetic test data.",
        "Hand-authored synthetic test data for historical date parser testing.",
        "Synthetic test data; never based on real historical weather observations.",
        "用于演示的合成测试数据，并非真实历史天气观测。",
        "Synthetic test data for historical fake-weather format parsing.",
    ],
)
def test_manifest_accepts_an_explicit_historical_data_disclaimer(
    tmp_path: Path,
    description: str,
) -> None:
    manifest = make_manifest()
    manifest["source_description"] = description
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid


def test_known_manifest_identity_survives_a_bad_description(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["source_description"] = "Synthetic test data gathered from live stations."
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.manifest is not None
    assert result.manifest.schema_version == 1
    assert result.manifest.dataset_kind == "synthetic"


def test_lone_surrogate_is_a_validation_error(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(tmp_path, [])
    record = make_record("12-30", "0~1℃", "\ud800")
    input_path.write_text(
        json.dumps(record, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert any(issue.rule_id == "Q01" for issue in result.issues)


def test_guarded_large_json_integer_is_a_validation_error(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(tmp_path, [])
    input_path.write_text(
        '{"location":"Chengdu","snapshot_id":"test-snapshot",'
        '"forecast_date_raw":"12-30","temperature_raw":'
        + "1" * 5000
        + ',"weather_raw":"Cloudy"}\n',
        encoding="utf-8",
        newline="\n",
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.invalid_records == 1
    assert any(issue.rule_id in {"Q01", "Q02"} for issue in result.issues)


def test_extreme_json_exponent_is_a_validation_error(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(tmp_path, [])
    input_path.write_text(
        '{"location":"Chengdu","snapshot_id":"test-snapshot",'
        '"forecast_date_raw":"12-30","temperature_raw":1e999999999999999999999,'
        '"weather_raw":"Cloudy"}\n',
        encoding="utf-8",
        newline="\n",
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.invalid_records == 1
    assert any(
        issue.rule_id == "Q02" and issue.field == "temperature_raw"
        for issue in result.issues
    )


def test_format_equivalent_exact_duplicates_merge_once_with_q09(
    tmp_path: Path,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", "0~1℃", " Cloudy "),
            make_record("2026-12-30", "0 ～ 1 °C", "Cloudy"),
        ],
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.input_records == 2
    assert result.counts.individually_valid_records == 2
    assert result.counts.invalid_records == 0
    assert result.counts.conflict_keys == 0
    assert result.counts.conflict_rows == 0
    assert result.counts.duplicate_rows_removed == 1
    assert result.counts.eligible_unique_records == 1
    assert len(result.canonical_records) == 1
    retained = result.canonical_records[0]
    assert retained.source_lines == (1, 2)
    assert retained.temp_min_c == Decimal("0")
    assert retained.temp_max_c == Decimal("1")
    assert retained.temp_midpoint_c == Decimal("0.5")
    assert retained.weather == "Cloudy"
    duplicate = [issue for issue in result.issues if issue.rule_id == "Q09"]
    assert len(duplicate) == 1
    assert duplicate[0].severity == "INFO"
    assert duplicate[0].source_lines == (1, 2)
    assert duplicate[0].logical_key == (
        "test-snapshot",
        "Chengdu",
        date(2026, 12, 30),
    )
    _assert_count_identities(result)


def test_null_equals_null_for_duplicate_business_values(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", None, None),
            make_record("2026-12-30", None, None),
        ],
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.duplicate_rows_removed == 1
    assert result.counts.conflict_keys == 0
    assert result.counts.conflict_rows == 0
    assert result.counts.eligible_unique_records == 1
    assert result.counts.missing_temperature == 1
    assert result.counts.missing_weather == 1
    assert len(result.canonical_records) == 1
    retained = result.canonical_records[0]
    assert (
        retained.temp_min_c,
        retained.temp_max_c,
        retained.temp_midpoint_c,
        retained.weather,
    ) == (None, None, None, None)
    assert retained.source_lines == (1, 2)
    assert sum(issue.rule_id == "Q09" for issue in result.issues) == 1
    _assert_count_identities(result)


@pytest.mark.parametrize(
    ("first_temperature", "second_temperature", "first_weather", "second_weather", "fields"),
    [
        (None, "0~2℃", "Cloudy", "Cloudy", ("temp_min_c", "temp_max_c")),
        ("0~2℃", "0~2℃", None, "Cloudy", ("weather",)),
    ],
    ids=["temperature-null-vs-value", "weather-null-vs-value"],
)
def test_null_vs_non_null_is_a_conflict_not_imputation(
    tmp_path: Path,
    first_temperature: str | None,
    second_temperature: str | None,
    first_weather: str | None,
    second_weather: str | None,
    fields: tuple[str, ...],
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", first_temperature, first_weather),
            make_record("2026-12-30", second_temperature, second_weather),
        ],
    )

    result = validate_dataset(input_path, manifest_path)
    conflicts = [issue for issue in result.issues if issue.rule_id == "Q10"]

    assert not result.is_valid
    assert result.counts.individually_valid_records == 2
    assert result.counts.conflict_keys == 1
    assert result.counts.conflict_rows == 2
    assert result.counts.duplicate_rows_removed == 0
    assert result.counts.eligible_unique_records == 0
    assert result.canonical_records == ()
    assert len(conflicts) == 1
    assert conflicts[0].message.endswith(", ".join(fields))
    assert conflicts[0].source_lines == (1, 2)
    _assert_count_identities(result)


def test_same_snapshot_date_with_distinct_snapshot_ids_remains_two_records(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    first_snapshot = manifest["snapshots"][0]
    first_snapshot["snapshot_id"] = "snapshot-b"
    second_snapshot = dict(first_snapshot)
    second_snapshot["snapshot_id"] = "snapshot-a"
    manifest["snapshots"].append(second_snapshot)
    first = make_record("12-30", "0~1℃")
    first["snapshot_id"] = "snapshot-b"
    second = make_record("12-30", "5~6℃")
    second["snapshot_id"] = "snapshot-a"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [first, second],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.conflict_keys == 0
    assert result.counts.duplicate_rows_removed == 0
    assert result.counts.eligible_unique_records == 2
    assert [record.snapshot_id for record in result.canonical_records] == [
        "snapshot-a",
        "snapshot-b",
    ]
    assert [record.temp_min_c for record in result.canonical_records] == [
        Decimal("5"),
        Decimal("0"),
    ]
    _assert_count_identities(result)


def test_blank_lines_occupy_physical_numbers_and_final_newline_is_optional(
    tmp_path: Path,
) -> None:
    first = _record_bytes(make_record("12-30", "0~1℃"))
    equivalent = _record_bytes(make_record("2026-12-30", "0 ～ 1 °C"))
    input_path, manifest_path = _write_raw_records(
        tmp_path,
        b"\n" + first + b"\n \t\r\n" + equivalent,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.input_records == 2
    assert result.counts.duplicate_rows_removed == 1
    assert result.canonical_records[0].source_lines == (2, 4)
    duplicate = next(issue for issue in result.issues if issue.rule_id == "Q09")
    assert duplicate.source_lines == (2, 4)
    assert list(duplicate.source_lines) == sorted(set(duplicate.source_lines))
    assert all(line > 0 for line in duplicate.source_lines)


def test_malformed_json_does_not_hide_a_later_no_newline_record(
    tmp_path: Path,
) -> None:
    later = _record_bytes(make_record("12-31", "1~2℃"))
    input_path, manifest_path = _write_raw_records(
        tmp_path,
        b"\n{not-json}\n\n" + later,
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.validation_complete
    assert result.counts.input_records == 2
    assert result.counts.individually_valid_records == 1
    assert result.counts.invalid_records == 1
    assert len(result.canonical_records) == 1
    assert result.canonical_records[0].source_lines == (4,)
    malformed = next(issue for issue in result.issues if issue.rule_id == "Q01")
    assert malformed.severity == "ERROR"
    assert malformed.source_lines == (2,)


def test_both_count_identities_hold_with_invalid_duplicate_conflict_and_valid_rows(
    tmp_path: Path,
) -> None:
    rows = [
        b"{not-json}",
        _record_bytes(make_record("12-30", "0~1℃")),
        _record_bytes(make_record("2026-12-30", "0 ～ 1 °C")),
        _record_bytes(make_record("12-31", "0~1℃")),
        _record_bytes(make_record("2026-12-31", "0~2℃")),
        _record_bytes(make_record("01-01", "-1~1℃")),
    ]
    input_path, manifest_path = _write_raw_records(
        tmp_path,
        b"\n".join(rows) + b"\n",
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.input_records == 6
    assert result.counts.individually_valid_records == 5
    assert result.counts.invalid_records == 1
    assert result.counts.conflict_keys == 1
    assert result.counts.conflict_rows == 2
    assert result.counts.duplicate_rows_removed == 1
    assert result.counts.eligible_unique_records == 2
    assert len(result.canonical_records) == 2
    _assert_count_identities(result)


@pytest.mark.parametrize(
    ("payload", "expected_rule", "expected_field"),
    [
        (b"{", "Q01", None),
        (b"null", "Q02", None),
        (b'{"schema_version":1,"schema_version":1}', "Q01", "schema_version"),
        (b'{"schema_version":NaN}', "Q01", None),
        (b'{"schema_version":Infinity}', "Q01", None),
        (b'{"schema_version":-Infinity}', "Q01", None),
        (b"\xff", "Q01", None),
    ],
    ids=[
        "syntax",
        "non-object",
        "duplicate-key",
        "nan",
        "infinity",
        "negative-infinity",
        "invalid-utf8",
    ],
)
def test_manifest_json_and_encoding_failures_have_frozen_issue_shape(
    tmp_path: Path,
    payload: bytes,
    expected_rule: str,
    expected_field: str | None,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
    )
    manifest_path.write_bytes(payload)

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert not result.validation_complete
    assert result.manifest is None
    issue = next(issue for issue in result.issues if issue.rule_id == expected_rule)
    assert issue.severity == "ERROR"
    assert issue.source_lines == ()
    assert issue.field == expected_field
    assert issue.logical_key is None
    assert issue.message
    assert result.counts.input_records == 1
    assert result.counts.individually_valid_records is None
    assert result.counts.invalid_records is None
    assert result.counts.conflict_keys is None
    assert result.counts.conflict_rows is None
    assert result.counts.duplicate_rows_removed is None
    assert result.counts.eligible_unique_records is None
    assert result.counts.output_records == 0
    assert result.counts.missing_temperature is None
    assert result.counts.missing_weather is None
    assert result.counts.missing_month_groups is None


def test_manifest_utf8_bom_is_reported_but_remaining_manifest_is_inspected(
    tmp_path: Path,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
    )
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(b"\xef\xbb\xbf" + original)

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.validation_complete
    assert result.manifest is not None
    assert result.counts.individually_valid_records == 1
    issues = [issue for issue in result.issues if issue.rule_id == "Q01"]
    assert len(issues) == 1
    assert issues[0].severity == "ERROR"
    assert issues[0].source_lines == ()
    assert issues[0].field is None
    assert "BOM" in issues[0].message


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_are_q01_and_later_lines_continue(
    tmp_path: Path,
    constant: str,
) -> None:
    malformed = (
        '{"location":"Chengdu","snapshot_id":"test-snapshot",'
        '"forecast_date_raw":"12-30","temperature_raw":'
        f'{constant},"weather_raw":"Cloudy"}}'
    ).encode("utf-8")
    valid = _record_bytes(make_record("12-31", "1~2℃"))
    input_path, manifest_path = _write_raw_records(
        tmp_path,
        malformed + b"\n" + valid + b"\n",
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.validation_complete
    assert result.counts.input_records == 2
    assert result.counts.individually_valid_records == 1
    assert result.counts.invalid_records == 1
    assert result.canonical_records[0].source_lines == (2,)
    q01 = [issue for issue in result.issues if issue.rule_id == "Q01"]
    assert len(q01) == 1
    assert q01[0].source_lines == (1,)


def test_input_utf8_bom_is_q01_without_hiding_the_first_record(tmp_path: Path) -> None:
    record = _record_bytes(make_record("12-30", "0~1℃"))
    input_path, manifest_path = _write_raw_records(
        tmp_path,
        b"\xef\xbb\xbf" + record + b"\n",
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.validation_complete
    assert result.counts.input_records == 1
    assert result.counts.individually_valid_records == 1
    assert result.counts.invalid_records == 0
    assert len(result.canonical_records) == 1
    issue = next(issue for issue in result.issues if issue.rule_id == "Q01")
    assert issue.severity == "ERROR"
    assert issue.source_lines == (1,)
    assert issue.field is None
    assert "BOM" in issue.message


@pytest.mark.parametrize(
    ("case", "expected_rule", "expected_field", "expected_logical_key"),
    [
        ("duplicate-key", "Q01", "weather_raw", None),
        (
            "extra-key",
            "Q01",
            "unexpected",
            ("test-snapshot", "Chengdu", date(2026, 12, 30)),
        ),
        (
            "missing-key",
            "Q02",
            "weather_raw",
            ("test-snapshot", "Chengdu", date(2026, 12, 30)),
        ),
        ("wrong-type", "Q02", "location", None),
    ],
)
def test_raw_record_key_and_type_contracts(
    tmp_path: Path,
    case: str,
    expected_rule: str,
    expected_field: str,
    expected_logical_key: tuple[str, str, date] | None,
) -> None:
    if case == "duplicate-key":
        payload = (
            b'{"location":"Chengdu","snapshot_id":"test-snapshot",'
            b'"forecast_date_raw":"12-30","temperature_raw":"0~1\\u2103",'
            b'"weather_raw":"Cloudy","weather_raw":"Rain"}'
        )
    else:
        record = make_record("12-30", "0~1℃")
        if case == "extra-key":
            record["unexpected"] = "value"
        elif case == "missing-key":
            record.pop("weather_raw")
        else:
            record["location"] = 42
        payload = _record_bytes(record)
    input_path, manifest_path = _write_raw_records(tmp_path, payload + b"\n")

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.input_records == 1
    assert result.counts.individually_valid_records == 0
    assert result.counts.invalid_records == 1
    issue = next(
        issue
        for issue in result.issues
        if issue.rule_id == expected_rule and issue.field == expected_field
    )
    assert issue.severity == "ERROR"
    assert issue.source_lines == (1,)
    assert issue.logical_key == expected_logical_key


def test_one_bad_record_collects_independent_errors_without_cascades(
    tmp_path: Path,
) -> None:
    record: dict[str, object] = {
        "location": 42,
        "snapshot_id": "bad id",
        "forecast_date_raw": "02-30",
        "temperature_raw": "1~N/A",
        "unexpected": "do not echo",
    }
    input_path, manifest_path = write_dataset(tmp_path, [record])

    result = validate_dataset(input_path, manifest_path)
    pairs = {(issue.rule_id, issue.field) for issue in result.issues}

    assert not result.is_valid
    assert {
        ("Q01", "unexpected"),
        ("Q02", "weather_raw"),
        ("Q02", "location"),
        ("Q02", "snapshot_id"),
        ("Q04", "forecast_date_raw"),
        ("Q06", "temperature_raw"),
    }.issubset(pairs)
    assert result.counts.input_records == 1
    assert result.counts.individually_valid_records == 0
    assert result.counts.invalid_records == 1


def test_missing_snapshot_context_does_not_invent_window_or_weekday_errors(
    tmp_path: Path,
) -> None:
    record = make_record("12-30", "0~1℃")
    record["snapshot_id"] = "not-declared"
    input_path, manifest_path = write_dataset(tmp_path, [record])

    result = validate_dataset(input_path, manifest_path)
    record_issues = [issue for issue in result.issues if issue.source_lines == (1,)]

    assert [(issue.rule_id, issue.field) for issue in record_issues] == [
        ("Q03", "snapshot_id")
    ]


def test_manifest_extra_missing_and_wrong_typed_fields_are_all_reported(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["unexpected"] = True
    manifest.pop("source_description")
    manifest["schema_version"] = True
    manifest["dataset_kind"] = 1
    manifest["locations"] = "Chengdu"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)
    pairs = {(issue.rule_id, issue.field) for issue in result.issues}

    assert not result.is_valid
    assert ("Q01", "unexpected") in pairs
    assert ("Q02", "source_description") in pairs
    assert ("Q02", "schema_version") in pairs
    assert ("Q02", "dataset_kind") in pairs
    assert ("Q02", "locations") in pairs
    assert result.manifest is None
    assert result.counts.individually_valid_records is None


def test_normalized_manifest_locations_must_be_unique(tmp_path: Path) -> None:
    manifest = make_manifest()
    manifest["locations"] = ["Café", "  Cafe\u0301  "]
    input_path, manifest_path = write_dataset(
        tmp_path,
        [],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    duplicate = next(
        issue
        for issue in result.issues
        if issue.rule_id == "Q03" and issue.field == "locations[1]"
    )
    assert duplicate.severity == "ERROR"
    assert duplicate.source_lines == ()
    assert "duplicated" in duplicate.message


@pytest.mark.parametrize(
    "snapshot_id",
    ["", "   ", "bad id", "snow\nday", "雪"],
    ids=["empty", "trimmed-empty", "space", "newline", "non-ascii"],
)
def test_manifest_snapshot_identifier_contract(
    tmp_path: Path,
    snapshot_id: str,
) -> None:
    manifest = make_manifest()
    manifest["snapshots"][0]["snapshot_id"] = snapshot_id
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    issue = next(
        issue
        for issue in result.issues
        if issue.field == "snapshots[0].snapshot_id"
    )
    assert issue.rule_id == "Q02"
    assert issue.severity == "ERROR"
    assert issue.source_lines == ()


def test_duplicate_manifest_snapshot_identifiers_are_rejected(tmp_path: Path) -> None:
    manifest = make_manifest()
    manifest["snapshots"].append(dict(manifest["snapshots"][0]))
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    issue = next(
        issue
        for issue in result.issues
        if issue.rule_id == "Q03"
        and issue.field == "snapshots[1].snapshot_id"
    )
    assert issue.severity == "ERROR"
    assert "duplicated" in issue.message
    assert result.manifest is None
    assert not result.validation_complete
    assert result.counts.conflict_keys is None


@pytest.mark.parametrize(
    ("snapshot_date", "valid_start", "valid_end"),
    [
        ("2026-12-31", "2026-12-30", "2027-01-02"),
        ("2026-12-30", "2027-01-02", "2027-01-01"),
    ],
    ids=["snapshot-after-start", "start-after-end"],
)
def test_manifest_requires_snapshot_start_end_order(
    tmp_path: Path,
    snapshot_date: str,
    valid_start: str,
    valid_end: str,
) -> None:
    manifest = make_manifest()
    snapshot = manifest["snapshots"][0]
    snapshot["snapshot_date"] = snapshot_date
    snapshot["valid_start"] = valid_start
    snapshot["valid_end"] = valid_end
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    issue = next(
        issue
        for issue in result.issues
        if issue.rule_id == "Q03" and issue.field == "snapshots[0]"
    )
    assert issue.severity == "ERROR"
    assert issue.source_lines == ()
    assert "ordering" in issue.message


def test_manifest_declared_location_and_snapshot_references_are_enforced(
    tmp_path: Path,
) -> None:
    unknown_location = make_record("12-30", "0~1℃")
    unknown_location["location"] = "Elsewhere"
    unknown_snapshot = make_record("12-31", "1~2℃")
    unknown_snapshot["snapshot_id"] = "elsewhere"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [unknown_location, unknown_snapshot],
    )

    result = validate_dataset(input_path, manifest_path)
    pairs = [
        (issue.rule_id, issue.field, issue.source_lines)
        for issue in result.issues
        if issue.rule_id == "Q03"
    ]

    assert pairs == [
        ("Q03", "location", (1,)),
        ("Q03", "snapshot_id", (2,)),
    ]


def test_manifest_non_object_snapshot_and_snapshot_extra_key_are_collected(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["snapshots"] = [None, {**manifest["snapshots"][0], "extra": 1}]
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)
    pairs = {(issue.rule_id, issue.field) for issue in result.issues}

    assert ("Q02", "snapshots[0]") in pairs
    assert ("Q01", "snapshots[1].extra") in pairs


@pytest.mark.parametrize(
    "payload",
    [b"", b" \t\r\n\n  \r\n"],
    ids=["zero-byte", "whitespace-only"],
)
def test_empty_input_is_q11_with_determinate_zero_counts(
    tmp_path: Path,
    payload: bytes,
) -> None:
    input_path, manifest_path = _write_raw_records(tmp_path, payload)

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.validation_complete
    assert result.counts.input_records == 0
    assert result.counts.individually_valid_records == 0
    assert result.counts.invalid_records == 0
    assert result.counts.conflict_keys == 0
    assert result.counts.conflict_rows == 0
    assert result.counts.duplicate_rows_removed == 0
    assert result.counts.eligible_unique_records == 0
    assert result.counts.output_records == 0
    assert result.canonical_records == ()
    q11 = [issue for issue in result.issues if issue.rule_id == "Q11"]
    assert len(q11) == 1
    assert q11[0].severity == "ERROR"
    assert q11[0].source_lines == ()
    assert q11[0].field is None
    assert q11[0].logical_key is None
    _assert_count_identities(result)


def test_all_missing_temperature_markers_are_counted_but_not_zero_imputed(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["snapshots"][0]["valid_end"] = "2027-01-05"
    missing_markers: list[str | None] = [None, "", "   ", "—", "N/A", "n/a"]
    dates = [
        "2026-12-30",
        "2026-12-31",
        "2027-01-01",
        "2027-01-02",
        "2027-01-03",
        "2027-01-04",
    ]
    records = [
        make_record(raw_date, marker)
        for raw_date, marker in zip(dates, missing_markers, strict=True)
    ]
    records.append(make_record("2027-01-05", "0~0℃"))
    input_path, manifest_path = write_dataset(
        tmp_path,
        records,
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.input_records == 7
    assert result.counts.eligible_unique_records == 7
    assert result.counts.missing_temperature == 6
    q05 = [issue for issue in result.issues if issue.rule_id == "Q05"]
    assert len(q05) == 6
    assert [issue.source_lines for issue in q05] == [
        (1,),
        (2,),
        (3,),
        (4,),
        (5,),
        (6,),
    ]
    zero = result.canonical_records[-1]
    assert zero.valid_date == date(2027, 1, 5)
    assert zero.temp_min_c == Decimal("0")
    assert zero.temp_max_c == Decimal("0")
    assert zero.temp_midpoint_c == Decimal("0")


def test_all_temperatures_missing_emits_q13_for_each_nonempty_group(
    tmp_path: Path,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", None, "Cloudy"),
            make_record("01-01", "—", "Rain"),
        ],
    )

    result = validate_dataset(input_path, manifest_path)
    q13_temperature = [
        issue
        for issue in result.issues
        if issue.rule_id == "Q13" and issue.field == "temperature"
    ]

    assert result.is_valid
    assert result.counts.missing_temperature == 2
    assert result.counts.missing_weather == 0
    assert len(q13_temperature) == 3
    assert [issue.severity for issue in q13_temperature] == [
        "WARNING",
        "WARNING",
        "WARNING",
    ]
    assert {issue.source_lines for issue in q13_temperature} == {
        (1,),
        (2,),
        (1, 2),
    }


def test_missing_weather_is_retained_excluded_from_denominator_and_not_split(
    tmp_path: Path,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", "0~1℃", None),
            make_record("12-31", "1~2℃", "   "),
            make_record("01-01", "2~3℃", " Rain / Snow + FOG "),
        ],
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.eligible_unique_records == 3
    assert result.counts.missing_weather == 2
    assert len(result.canonical_records) == 3
    assert [record.weather for record in result.canonical_records] == [
        None,
        None,
        "Rain / Snow + FOG",
    ]
    q08 = [issue for issue in result.issues if issue.rule_id == "Q08"]
    assert len(q08) == 2
    assert [issue.source_lines for issue in q08] == [(1,), (2,)]
    assert all(issue.severity == "WARNING" for issue in q08)


def test_all_weather_missing_emits_q13_for_each_nonempty_group(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", "0~1℃", None),
            make_record("01-01", "1~2℃", ""),
        ],
    )

    result = validate_dataset(input_path, manifest_path)
    q13_weather = [
        issue
        for issue in result.issues
        if issue.rule_id == "Q13" and issue.field == "weather"
    ]

    assert result.is_valid
    assert result.counts.missing_weather == 2
    assert len(q13_weather) == 3
    assert {issue.source_lines for issue in q13_weather} == {
        (1,),
        (2,),
        (1, 2),
    }


def test_manifest_month_without_records_is_explicit_q12(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", "0~1℃")],
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.missing_month_groups == 1
    assert len(result.missing_month_groups) == 1
    missing = result.missing_month_groups[0]
    assert (
        missing.snapshot_id,
        missing.location,
        missing.year_month,
        missing.period_start,
        missing.period_end,
    ) == (
        "test-snapshot",
        "Chengdu",
        "2027-01",
        date(2027, 1, 1),
        date(2027, 1, 2),
    )
    q12 = [issue for issue in result.issues if issue.rule_id == "Q12"]
    assert len(q12) == 1
    assert q12[0].severity == "WARNING"
    assert q12[0].source_lines == ()
    assert q12[0].field == "year_month"
    assert q12[0].logical_key is None
    assert q12[0].message == (
        "manifest-defined group test-snapshot/Chengdu/2027-01 has no records"
    )


def test_year_one_record_is_not_misclassified_as_a_missing_month(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    snapshot = manifest["snapshots"][0]
    snapshot["snapshot_date"] = "0001-01-01"
    snapshot["valid_start"] = "0001-01-01"
    snapshot["valid_end"] = "0001-01-01"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("0001-01-01", "0~1℃")],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.missing_month_groups == 0
    assert result.missing_month_groups == ()
    assert not any(issue.rule_id == "Q12" for issue in result.issues)


def test_canonical_text_and_record_order_use_nfc_and_unicode_code_points(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["locations"] = ["éclair", "Zulu", " Alpha "]
    decomposed = make_record("12-30", "0~1℃", " Cafe\u0301 / RAIN ")
    decomposed["location"] = " e\u0301clair "
    zulu = make_record("12-30", "1~2℃", "Cloudy")
    zulu["location"] = "Zulu"
    alpha = make_record("12-30", "2~3℃", "Sunny")
    alpha["location"] = "Alpha"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [decomposed, zulu, alpha],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.manifest is not None
    assert result.manifest.locations == ("éclair", "Zulu", "Alpha")
    assert [record.location for record in result.canonical_records] == [
        "Alpha",
        "Zulu",
        "éclair",
    ]
    assert result.canonical_records[-1].weather == "Café / RAIN"


def test_q01_through_q13_have_frozen_severity_shape_and_order(
    tmp_path: Path,
) -> None:
    results: list[Any] = []

    malformed_record: dict[str, object] = {
        "location": 42,
        "snapshot_id": "bad id",
        "forecast_date_raw": "02-30",
        "temperature_raw": "1~N/A",
        "unexpected": "SENSITIVE_RAW_VALUE",
    }
    input_path, manifest_path = write_dataset(
        tmp_path / "record-errors",
        [malformed_record],
    )
    results.append(validate_dataset(input_path, manifest_path))

    unknown = make_record("12-30", "0~1℃")
    unknown["location"] = "Unknown"
    input_path, manifest_path = write_dataset(
        tmp_path / "manifest-reference",
        [unknown],
    )
    results.append(validate_dataset(input_path, manifest_path))

    input_path, manifest_path = write_dataset(
        tmp_path / "missing-duplicate",
        [
            make_record("12-30", None, None),
            make_record("2026-12-30", None, None),
        ],
    )
    results.append(validate_dataset(input_path, manifest_path))

    input_path, manifest_path = write_dataset(
        tmp_path / "inverted",
        [make_record("12-31", "7~2℃")],
    )
    results.append(validate_dataset(input_path, manifest_path))

    input_path, manifest_path = write_dataset(
        tmp_path / "conflict",
        [
            make_record("01-01", "0~1℃", "Cloudy"),
            make_record("2027-01-01", "0~2℃", "Rain"),
        ],
    )
    results.append(validate_dataset(input_path, manifest_path))

    input_path, manifest_path = _write_raw_records(tmp_path / "empty", b"")
    results.append(validate_dataset(input_path, manifest_path))

    severity_by_rule = {
        "Q01": "ERROR",
        "Q02": "ERROR",
        "Q03": "ERROR",
        "Q04": "ERROR",
        "Q05": "WARNING",
        "Q06": "ERROR",
        "Q07": "ERROR",
        "Q08": "WARNING",
        "Q09": "INFO",
        "Q10": "ERROR",
        "Q11": "ERROR",
        "Q12": "WARNING",
        "Q13": "WARNING",
    }
    issues_by_rule = {
        rule_id: [
            issue
            for result in results
            for issue in result.issues
            if issue.rule_id == rule_id
        ]
        for rule_id in severity_by_rule
    }

    assert all(issues_by_rule.values())
    for rule_id, expected_severity in severity_by_rule.items():
        for issue in issues_by_rule[rule_id]:
            assert issue.severity == expected_severity
            assert issue.message
            assert "SENSITIVE_RAW_VALUE" not in issue.message
            assert issue.source_lines == tuple(
                sorted(set(issue.source_lines))
            )
            assert all(line > 0 for line in issue.source_lines)
            serialized = issue.to_dict()
            expected_keys = ["rule_id", "severity", "source_lines"]
            if issue.field is not None:
                expected_keys.append("field")
            if issue.logical_key is not None:
                expected_keys.append("logical_key")
            expected_keys.append("message")
            assert list(serialized) == expected_keys
            assert serialized["rule_id"] == rule_id
            assert serialized["severity"] == expected_severity
            assert serialized["source_lines"] == list(issue.source_lines)
            if issue.logical_key is not None:
                assert list(serialized["logical_key"]) == [
                    "snapshot_id",
                    "location",
                    "valid_date",
                ]

    for result in results:
        assert result.issues == tuple(
            sorted(result.issues, key=_contract_issue_order_key)
        )
