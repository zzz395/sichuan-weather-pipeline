"""Exact CSV, JSON, encoding, and rounding serialization regressions."""

import csv
from datetime import date
from decimal import Decimal
import io
import json
from pathlib import Path

import pytest

from sichuan_weather.analysis import (
    build_monthly_summary,
    build_overall_summary,
    build_weather_frequency,
)
from sichuan_weather.models import CanonicalRecord, Issue
from sichuan_weather.serialization import (
    CLEANED_FIELDS,
    MONTHLY_SUMMARY_FIELDS,
    SUMMARY_FIELDS,
    WEATHER_FREQUENCY_FIELDS,
    _decimal_text,
    quality_document,
    quality_json_text,
    write_cleaned_csv,
    write_monthly_summary_csv,
    write_quality_json,
    write_summary_csv,
    write_weather_frequency_csv,
)
from sichuan_weather.validation import validate_dataset

from tests.oracle import (
    SAMPLE_MANIFEST,
    SAMPLE_MANIFEST_SHA256,
    SAMPLE_RECORDS,
    SAMPLE_RECORDS_SHA256,
)


def _csv_matrix(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    text = raw.decode("utf-8", errors="strict")
    return list(csv.reader(io.StringIO(text, newline="")))


def test_frozen_sample_exact_csv_and_quality_schema(tmp_path: Path) -> None:
    result = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)
    assert result.is_valid and result.manifest is not None

    cleaned_path = tmp_path / "cleaned.csv"
    summary_path = tmp_path / "summary.csv"
    monthly_path = tmp_path / "monthly_summary.csv"
    weather_path = tmp_path / "weather_frequency.csv"
    quality_path = tmp_path / "quality.json"
    write_cleaned_csv(cleaned_path, result.canonical_records)
    write_summary_csv(
        summary_path,
        build_overall_summary(result.manifest, result.canonical_records),
    )
    write_monthly_summary_csv(
        monthly_path,
        build_monthly_summary(result.manifest, result.canonical_records),
    )
    write_weather_frequency_csv(
        weather_path,
        build_weather_frequency(result.manifest, result.canonical_records),
    )
    document = quality_document(
        result,
        analysis_generated=True,
        output_records=12,
    )
    write_quality_json(quality_path, document)

    assert _csv_matrix(cleaned_path) == [
        list(CLEANED_FIELDS),
        ["Chengdu", "sample-a", "2026-12-30", "2026-12-30", "-2.00", "4.00", "1.00", "Cloudy", "[1,9]"],
        ["Chengdu", "sample-a", "2026-12-30", "2026-12-31", "0.00", "6.00", "3.00", "Sunny", "[2]"],
        ["Chengdu", "sample-a", "2026-12-30", "2027-01-01", "-4.00", "-4.00", "-4.00", "Snow", "[3]"],
        ["Chengdu", "sample-a", "2026-12-30", "2027-01-02", "", "", "", "Cloudy", "[4]"],
        ["Mianyang", "sample-a", "2026-12-30", "2026-12-30", "-1.00", "5.00", "2.00", "Cloudy", "[5,14]"],
        ["Mianyang", "sample-a", "2026-12-30", "2026-12-31", "2.00", "8.00", "5.00", "Sunny", "[6]"],
        ["Mianyang", "sample-a", "2026-12-30", "2027-01-01", "1.50", "6.50", "4.00", "Rain", "[7]"],
        ["Mianyang", "sample-a", "2026-12-30", "2027-01-02", "", "", "", "", "[8]"],
        ["Chengdu", "sample-b", "2026-12-31", "2026-12-31", "1.00", "7.00", "4.00", "Sunny", "[10]"],
        ["Chengdu", "sample-b", "2026-12-31", "2027-01-01", "-3.00", "1.00", "-1.00", "Cloudy", "[11]"],
        ["Mianyang", "sample-b", "2026-12-31", "2027-01-01", "2.00", "8.00", "5.00", "Rain", "[12]"],
        ["Mianyang", "sample-b", "2026-12-31", "2027-01-02", "3.00", "9.00", "6.00", "Rain", "[13]"],
    ]
    assert _csv_matrix(summary_path) == [
        list(SUMMARY_FIELDS),
        ["sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "4", "3", "4", "2026-12-30", "2027-01-02", "-4.0000", "6.0000", "4.0000", "0.0000"],
        ["sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "4", "3", "3", "2026-12-30", "2027-01-02", "-1.0000", "8.0000", "5.6667", "3.6667"],
        ["sample-b", "2026-12-31", "Chengdu", "2026-12-31", "2027-01-02", "2", "2", "2", "2026-12-31", "2027-01-01", "-3.0000", "7.0000", "5.0000", "1.5000"],
        ["sample-b", "2026-12-31", "Mianyang", "2026-12-31", "2027-01-02", "2", "2", "2", "2027-01-01", "2027-01-02", "2.0000", "9.0000", "6.0000", "5.5000"],
    ]
    assert _csv_matrix(monthly_path) == [
        list(MONTHLY_SUMMARY_FIELDS),
        ["sample-a", "2026-12-30", "Chengdu", "2026-12", "2026-12-30", "2026-12-31", "2", "2", "2", "2026-12-30", "2026-12-31", "-2.0000", "6.0000", "6.0000", "2.0000"],
        ["sample-a", "2026-12-30", "Chengdu", "2027-01", "2027-01-01", "2027-01-02", "2", "1", "2", "2027-01-01", "2027-01-02", "-4.0000", "-4.0000", "0.0000", "-4.0000"],
        ["sample-a", "2026-12-30", "Mianyang", "2026-12", "2026-12-30", "2026-12-31", "2", "2", "2", "2026-12-30", "2026-12-31", "-1.0000", "8.0000", "6.0000", "3.5000"],
        ["sample-a", "2026-12-30", "Mianyang", "2027-01", "2027-01-01", "2027-01-02", "2", "1", "1", "2027-01-01", "2027-01-02", "1.5000", "6.5000", "5.0000", "4.0000"],
        ["sample-b", "2026-12-31", "Chengdu", "2026-12", "2026-12-31", "2026-12-31", "1", "1", "1", "2026-12-31", "2026-12-31", "1.0000", "7.0000", "6.0000", "4.0000"],
        ["sample-b", "2026-12-31", "Chengdu", "2027-01", "2027-01-01", "2027-01-02", "1", "1", "1", "2027-01-01", "2027-01-01", "-3.0000", "1.0000", "4.0000", "-1.0000"],
        ["sample-b", "2026-12-31", "Mianyang", "2026-12", "2026-12-31", "2026-12-31", "0", "0", "0", "", "", "", "", "", ""],
        ["sample-b", "2026-12-31", "Mianyang", "2027-01", "2027-01-01", "2027-01-02", "2", "2", "2", "2027-01-01", "2027-01-02", "2.0000", "9.0000", "6.0000", "5.5000"],
    ]
    assert _csv_matrix(weather_path) == [
        list(WEATHER_FREQUENCY_FIELDS),
        ["sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "Cloudy", "2", "4", "0.500000"],
        ["sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "Snow", "1", "4", "0.250000"],
        ["sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "Sunny", "1", "4", "0.250000"],
        ["sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "Cloudy", "1", "3", "0.333333"],
        ["sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "Rain", "1", "3", "0.333333"],
        ["sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "Sunny", "1", "3", "0.333333"],
        ["sample-b", "2026-12-31", "Chengdu", "2026-12-31", "2027-01-02", "Cloudy", "1", "2", "0.500000"],
        ["sample-b", "2026-12-31", "Chengdu", "2026-12-31", "2027-01-02", "Sunny", "1", "2", "0.500000"],
        ["sample-b", "2026-12-31", "Mianyang", "2026-12-31", "2027-01-02", "Rain", "2", "2", "1.000000"],
    ]

    raw_quality = quality_path.read_bytes()
    assert not raw_quality.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw_quality
    assert raw_quality.endswith(b"\n")
    assert b"NaN" not in raw_quality and b"Infinity" not in raw_quality
    parsed = json.loads(raw_quality.decode("utf-8", errors="strict"))
    assert list(parsed) == [
        "schema_version",
        "dataset_kind",
        "status",
        "validation_complete",
        "analysis_generated",
        "raw_input_sha256",
        "manifest_sha256",
        "counts",
        "missing_month_groups",
        "issues",
    ]
    assert list(parsed["counts"]) == [
        "input_records",
        "individually_valid_records",
        "invalid_records",
        "conflict_keys",
        "conflict_rows",
        "duplicate_rows_removed",
        "eligible_unique_records",
        "output_records",
        "post_dedup_missing_temperature_records",
        "post_dedup_missing_weather_records",
        "explicit_missing_month_groups",
    ]
    assert parsed == {
        "schema_version": 1,
        "dataset_kind": "synthetic",
        "status": "valid",
        "validation_complete": True,
        "analysis_generated": True,
        "raw_input_sha256": SAMPLE_RECORDS_SHA256,
        "manifest_sha256": SAMPLE_MANIFEST_SHA256,
        "counts": {
            "input_records": 14,
            "individually_valid_records": 14,
            "invalid_records": 0,
            "conflict_keys": 0,
            "conflict_rows": 0,
            "duplicate_rows_removed": 2,
            "eligible_unique_records": 12,
            "output_records": 12,
            "post_dedup_missing_temperature_records": 2,
            "post_dedup_missing_weather_records": 1,
            "explicit_missing_month_groups": 1,
        },
        "missing_month_groups": [{
            "snapshot_id": "sample-b",
            "location": "Mianyang",
            "year_month": "2026-12",
            "period_start": "2026-12-31",
            "period_end": "2026-12-31",
        }],
        "issues": [
            {"rule_id": "Q12", "severity": "WARNING", "source_lines": [], "field": "year_month", "message": "manifest-defined group sample-b/Mianyang/2026-12 has no records"},
            {"rule_id": "Q09", "severity": "INFO", "source_lines": [1, 9], "logical_key": {"snapshot_id": "sample-a", "location": "Chengdu", "valid_date": "2026-12-30"}, "message": "exact duplicate records were deterministically merged"},
            {"rule_id": "Q05", "severity": "WARNING", "source_lines": [4], "field": "temperature_raw", "logical_key": {"snapshot_id": "sample-a", "location": "Chengdu", "valid_date": "2027-01-02"}, "message": "temperature is missing"},
            {"rule_id": "Q09", "severity": "INFO", "source_lines": [5, 14], "logical_key": {"snapshot_id": "sample-a", "location": "Mianyang", "valid_date": "2026-12-30"}, "message": "exact duplicate records were deterministically merged"},
            {"rule_id": "Q05", "severity": "WARNING", "source_lines": [8], "field": "temperature_raw", "logical_key": {"snapshot_id": "sample-a", "location": "Mianyang", "valid_date": "2027-01-02"}, "message": "temperature is missing"},
            {"rule_id": "Q08", "severity": "WARNING", "source_lines": [8], "field": "weather_raw", "logical_key": {"snapshot_id": "sample-a", "location": "Mianyang", "valid_date": "2027-01-02"}, "message": "weather is missing"},
        ],
    }


def test_utf8_lf_fixed_precision_half_even_and_negative_zero(tmp_path: Path) -> None:
    record = CanonicalRecord(
        location="成都",
        snapshot_id="快照-a",
        snapshot_date=date(2026, 12, 30),
        valid_date=date(2026, 12, 30),
        temp_min_c=Decimal("-0.004"),
        temp_max_c=Decimal("1.005"),
        temp_midpoint_c=Decimal("0.5005"),
        weather="小雨",
        source_lines=(2, 7),
    )
    path = tmp_path / "unicode.csv"
    write_cleaned_csv(path, [record])

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw and raw.endswith(b"\n")
    assert "成都" in raw.decode("utf-8", errors="strict")
    assert _csv_matrix(path)[1] == [
        "成都", "快照-a", "2026-12-30", "2026-12-30",
        "0.00", "1.00", "0.50", "小雨", "[2,7]",
    ]
    assert _decimal_text(Decimal("1.005"), 2) == "1.00"
    assert _decimal_text(Decimal("1.015"), 2) == "1.02"
    assert _decimal_text(Decimal("2.34545"), 4) == "2.3454"
    assert _decimal_text(Decimal("2.34555"), 4) == "2.3456"
    assert _decimal_text(Decimal("0.1234565"), 6) == "0.123456"
    assert _decimal_text(Decimal("0.1234575"), 6) == "0.123458"
    assert _decimal_text(Decimal("-0.0000004"), 6) == "0.000000"


def test_serializers_refuse_non_standard_numeric_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        quality_json_text({"value": float("nan")})
    with pytest.raises(ValueError):
        quality_json_text({"value": float("inf")})
    with pytest.raises((ArithmeticError, ValueError)):
        _decimal_text(Decimal("NaN"), 4)
    with pytest.raises((ArithmeticError, ValueError)):
        _decimal_text(Decimal("Infinity"), 4)


def test_issue_sorting_uses_logical_key_components() -> None:
    short_location = Issue(
        "Q10", "ERROR", (1,), None,
        ("snapshot", "a", date(2026, 1, 1)), "conflict",
    )
    extended_location = Issue(
        "Q10", "ERROR", (1,), None,
        ("snapshot", "a\x00!", date(2026, 1, 1)), "conflict",
    )

    assert sorted([extended_location, short_location], key=Issue.sort_key) == [
        short_location,
        extended_location,
    ]
