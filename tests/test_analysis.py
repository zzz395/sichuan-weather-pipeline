"""Independent analysis oracles for canonical and aggregate data."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from sichuan_weather.analysis import (
    MONTHLY_SUMMARY_FIELDS,
    OVERALL_SUMMARY_FIELDS,
    WEATHER_FREQUENCY_FIELDS,
    build_monthly_summary,
    build_overall_summary,
    build_weather_frequency,
)
from sichuan_weather.validation import validate_dataset

from tests.helpers import make_manifest, make_record, write_dataset
from tests.oracle import SAMPLE_MANIFEST, SAMPLE_RECORDS


def _values(rows: list[dict[str, object]], fields: tuple[str, ...]) -> list[tuple[object, ...]]:
    return [tuple(row[field] for field in fields) for row in rows]


def test_frozen_sample_complete_canonical_and_analysis_oracle() -> None:
    result = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)

    assert result.is_valid
    assert (
        result.counts.input_records,
        result.counts.individually_valid_records,
        result.counts.invalid_records,
        result.counts.conflict_keys,
        result.counts.conflict_rows,
        result.counts.duplicate_rows_removed,
        result.counts.eligible_unique_records,
        result.counts.missing_temperature,
        result.counts.missing_weather,
        result.counts.missing_month_groups,
    ) == (14, 14, 0, 0, 0, 2, 12, 2, 1, 1)

    canonical = [
        (
            record.location,
            record.snapshot_id,
            record.snapshot_date,
            record.valid_date,
            record.temp_min_c,
            record.temp_max_c,
            record.temp_midpoint_c,
            record.weather,
            record.source_lines,
        )
        for record in result.canonical_records
    ]
    assert canonical == [
        ("Chengdu", "sample-a", date(2026, 12, 30), date(2026, 12, 30), Decimal("-2"), Decimal("4"), Decimal("1"), "Cloudy", (1, 9)),
        ("Chengdu", "sample-a", date(2026, 12, 30), date(2026, 12, 31), Decimal("0"), Decimal("6"), Decimal("3"), "Sunny", (2,)),
        ("Chengdu", "sample-a", date(2026, 12, 30), date(2027, 1, 1), Decimal("-4"), Decimal("-4"), Decimal("-4"), "Snow", (3,)),
        ("Chengdu", "sample-a", date(2026, 12, 30), date(2027, 1, 2), None, None, None, "Cloudy", (4,)),
        ("Mianyang", "sample-a", date(2026, 12, 30), date(2026, 12, 30), Decimal("-1"), Decimal("5"), Decimal("2"), "Cloudy", (5, 14)),
        ("Mianyang", "sample-a", date(2026, 12, 30), date(2026, 12, 31), Decimal("2"), Decimal("8"), Decimal("5"), "Sunny", (6,)),
        ("Mianyang", "sample-a", date(2026, 12, 30), date(2027, 1, 1), Decimal("1.5"), Decimal("6.5"), Decimal("4.0"), "Rain", (7,)),
        ("Mianyang", "sample-a", date(2026, 12, 30), date(2027, 1, 2), None, None, None, None, (8,)),
        ("Chengdu", "sample-b", date(2026, 12, 31), date(2026, 12, 31), Decimal("1"), Decimal("7"), Decimal("4"), "Sunny", (10,)),
        ("Chengdu", "sample-b", date(2026, 12, 31), date(2027, 1, 1), Decimal("-3"), Decimal("1"), Decimal("-1"), "Cloudy", (11,)),
        ("Mianyang", "sample-b", date(2026, 12, 31), date(2027, 1, 1), Decimal("2"), Decimal("8"), Decimal("5"), "Rain", (12,)),
        ("Mianyang", "sample-b", date(2026, 12, 31), date(2027, 1, 2), Decimal("3"), Decimal("9"), Decimal("6"), "Rain", (13,)),
    ]

    assert result.manifest is not None
    overall = build_overall_summary(result.manifest, result.canonical_records)
    assert [tuple(row) for row in overall] == [OVERALL_SUMMARY_FIELDS] * 4
    assert _values(overall, OVERALL_SUMMARY_FIELDS) == [
        ("sample-a", date(2026, 12, 30), "Chengdu", date(2026, 12, 30), date(2027, 1, 2), 4, 3, 4, date(2026, 12, 30), date(2027, 1, 2), Decimal("-4"), Decimal("6"), Decimal("4"), Decimal("0")),
        ("sample-a", date(2026, 12, 30), "Mianyang", date(2026, 12, 30), date(2027, 1, 2), 4, 3, 3, date(2026, 12, 30), date(2027, 1, 2), Decimal("-1"), Decimal("8"), Decimal("5.666666666666666666666666667"), Decimal("3.666666666666666666666666667")),
        ("sample-b", date(2026, 12, 31), "Chengdu", date(2026, 12, 31), date(2027, 1, 2), 2, 2, 2, date(2026, 12, 31), date(2027, 1, 1), Decimal("-3"), Decimal("7"), Decimal("5"), Decimal("1.5")),
        ("sample-b", date(2026, 12, 31), "Mianyang", date(2026, 12, 31), date(2027, 1, 2), 2, 2, 2, date(2027, 1, 1), date(2027, 1, 2), Decimal("2"), Decimal("9"), Decimal("6"), Decimal("5.5")),
    ]

    monthly = build_monthly_summary(result.manifest, result.canonical_records)
    assert [tuple(row) for row in monthly] == [MONTHLY_SUMMARY_FIELDS] * 8
    assert _values(monthly, MONTHLY_SUMMARY_FIELDS) == [
        ("sample-a", date(2026, 12, 30), "Chengdu", "2026-12", date(2026, 12, 30), date(2026, 12, 31), 2, 2, 2, date(2026, 12, 30), date(2026, 12, 31), Decimal("-2"), Decimal("6"), Decimal("6"), Decimal("2")),
        ("sample-a", date(2026, 12, 30), "Chengdu", "2027-01", date(2027, 1, 1), date(2027, 1, 2), 2, 1, 2, date(2027, 1, 1), date(2027, 1, 2), Decimal("-4"), Decimal("-4"), Decimal("0"), Decimal("-4")),
        ("sample-a", date(2026, 12, 30), "Mianyang", "2026-12", date(2026, 12, 30), date(2026, 12, 31), 2, 2, 2, date(2026, 12, 30), date(2026, 12, 31), Decimal("-1"), Decimal("8"), Decimal("6"), Decimal("3.5")),
        ("sample-a", date(2026, 12, 30), "Mianyang", "2027-01", date(2027, 1, 1), date(2027, 1, 2), 2, 1, 1, date(2027, 1, 1), date(2027, 1, 2), Decimal("1.5"), Decimal("6.5"), Decimal("5"), Decimal("4")),
        ("sample-b", date(2026, 12, 31), "Chengdu", "2026-12", date(2026, 12, 31), date(2026, 12, 31), 1, 1, 1, date(2026, 12, 31), date(2026, 12, 31), Decimal("1"), Decimal("7"), Decimal("6"), Decimal("4")),
        ("sample-b", date(2026, 12, 31), "Chengdu", "2027-01", date(2027, 1, 1), date(2027, 1, 2), 1, 1, 1, date(2027, 1, 1), date(2027, 1, 1), Decimal("-3"), Decimal("1"), Decimal("4"), Decimal("-1")),
        ("sample-b", date(2026, 12, 31), "Mianyang", "2026-12", date(2026, 12, 31), date(2026, 12, 31), 0, 0, 0, None, None, None, None, None, None),
        ("sample-b", date(2026, 12, 31), "Mianyang", "2027-01", date(2027, 1, 1), date(2027, 1, 2), 2, 2, 2, date(2027, 1, 1), date(2027, 1, 2), Decimal("2"), Decimal("9"), Decimal("6"), Decimal("5.5")),
    ]

    weather = build_weather_frequency(result.manifest, result.canonical_records)
    assert [tuple(row) for row in weather] == [WEATHER_FREQUENCY_FIELDS] * 9
    third = Decimal("0.3333333333333333333333333333")
    assert _values(weather, WEATHER_FREQUENCY_FIELDS) == [
        ("sample-a", date(2026, 12, 30), "Chengdu", date(2026, 12, 30), date(2027, 1, 2), "Cloudy", 2, 4, Decimal("0.5")),
        ("sample-a", date(2026, 12, 30), "Chengdu", date(2026, 12, 30), date(2027, 1, 2), "Snow", 1, 4, Decimal("0.25")),
        ("sample-a", date(2026, 12, 30), "Chengdu", date(2026, 12, 30), date(2027, 1, 2), "Sunny", 1, 4, Decimal("0.25")),
        ("sample-a", date(2026, 12, 30), "Mianyang", date(2026, 12, 30), date(2027, 1, 2), "Cloudy", 1, 3, third),
        ("sample-a", date(2026, 12, 30), "Mianyang", date(2026, 12, 30), date(2027, 1, 2), "Rain", 1, 3, third),
        ("sample-a", date(2026, 12, 30), "Mianyang", date(2026, 12, 30), date(2027, 1, 2), "Sunny", 1, 3, third),
        ("sample-b", date(2026, 12, 31), "Chengdu", date(2026, 12, 31), date(2027, 1, 2), "Cloudy", 1, 2, Decimal("0.5")),
        ("sample-b", date(2026, 12, 31), "Chengdu", date(2026, 12, 31), date(2027, 1, 2), "Sunny", 1, 2, Decimal("0.5")),
        ("sample-b", date(2026, 12, 31), "Mianyang", date(2026, 12, 31), date(2027, 1, 2), "Rain", 2, 2, Decimal("1")),
    ]


def test_equal_snapshot_dates_remain_distinct_in_all_aggregate_rows(
    tmp_path: Path,
) -> None:
    manifest = make_manifest()
    manifest["snapshots"] = [
        {
            "snapshot_id": "snapshot-b",
            "snapshot_date": "2026-12-30",
            "valid_start": "2026-12-30",
            "valid_end": "2026-12-30",
        },
        {
            "snapshot_id": "snapshot-a",
            "snapshot_date": "2026-12-30",
            "valid_start": "2026-12-30",
            "valid_end": "2026-12-30",
        },
    ]
    snapshot_b = make_record("2026-12-30", "0~2℃", "Rain")
    snapshot_b["snapshot_id"] = "snapshot-b"
    snapshot_a = make_record("2026-12-30", "10~14℃", "Sunny")
    snapshot_a["snapshot_id"] = "snapshot-a"
    input_path, manifest_path = write_dataset(
        tmp_path,
        [snapshot_b, snapshot_a],
        manifest=manifest,
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid and result.manifest is not None
    assert _values(
        build_overall_summary(result.manifest, result.canonical_records),
        OVERALL_SUMMARY_FIELDS,
    ) == [
        (
            "snapshot-a",
            date(2026, 12, 30),
            "Chengdu",
            date(2026, 12, 30),
            date(2026, 12, 30),
            1,
            1,
            1,
            date(2026, 12, 30),
            date(2026, 12, 30),
            Decimal("10"),
            Decimal("14"),
            Decimal("4"),
            Decimal("12"),
        ),
        (
            "snapshot-b",
            date(2026, 12, 30),
            "Chengdu",
            date(2026, 12, 30),
            date(2026, 12, 30),
            1,
            1,
            1,
            date(2026, 12, 30),
            date(2026, 12, 30),
            Decimal("0"),
            Decimal("2"),
            Decimal("2"),
            Decimal("1"),
        ),
    ]
    assert _values(
        build_monthly_summary(result.manifest, result.canonical_records),
        MONTHLY_SUMMARY_FIELDS,
    ) == [
        (
            "snapshot-a",
            date(2026, 12, 30),
            "Chengdu",
            "2026-12",
            date(2026, 12, 30),
            date(2026, 12, 30),
            1,
            1,
            1,
            date(2026, 12, 30),
            date(2026, 12, 30),
            Decimal("10"),
            Decimal("14"),
            Decimal("4"),
            Decimal("12"),
        ),
        (
            "snapshot-b",
            date(2026, 12, 30),
            "Chengdu",
            "2026-12",
            date(2026, 12, 30),
            date(2026, 12, 30),
            1,
            1,
            1,
            date(2026, 12, 30),
            date(2026, 12, 30),
            Decimal("0"),
            Decimal("2"),
            Decimal("2"),
            Decimal("1"),
        ),
    ]
    assert _values(
        build_weather_frequency(result.manifest, result.canonical_records),
        WEATHER_FREQUENCY_FIELDS,
    ) == [
        (
            "snapshot-a",
            date(2026, 12, 30),
            "Chengdu",
            date(2026, 12, 30),
            date(2026, 12, 30),
            "Sunny",
            1,
            1,
            Decimal("1"),
        ),
        (
            "snapshot-b",
            date(2026, 12, 30),
            "Chengdu",
            date(2026, 12, 30),
            date(2026, 12, 30),
            "Rain",
            1,
            1,
            Decimal("1"),
        ),
    ]


def test_all_missing_temperature_and_weather_have_null_metrics(tmp_path: Path) -> None:
    records = [
        make_record("12-30", None, None),
        make_record("12-31", None, None),
        make_record("01-01", None, None),
        make_record("01-02", None, None),
    ]
    input_path, manifest_path = write_dataset(tmp_path, records, manifest=make_manifest())
    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    assert result.counts.missing_temperature == 4
    assert result.counts.missing_weather == 4
    assert result.manifest is not None
    overall = build_overall_summary(result.manifest, result.canonical_records)
    assert _values(overall, OVERALL_SUMMARY_FIELDS) == [
        ("test-snapshot", date(2026, 12, 30), "Chengdu", date(2026, 12, 30), date(2027, 1, 2), 4, 0, 0, date(2026, 12, 30), date(2027, 1, 2), None, None, None, None)
    ]
    monthly = build_monthly_summary(result.manifest, result.canonical_records)
    assert _values(monthly, MONTHLY_SUMMARY_FIELDS) == [
        ("test-snapshot", date(2026, 12, 30), "Chengdu", "2026-12", date(2026, 12, 30), date(2026, 12, 31), 2, 0, 0, date(2026, 12, 30), date(2026, 12, 31), None, None, None, None),
        ("test-snapshot", date(2026, 12, 30), "Chengdu", "2027-01", date(2027, 1, 1), date(2027, 1, 2), 2, 0, 0, date(2027, 1, 1), date(2027, 1, 2), None, None, None, None),
    ]
    assert build_weather_frequency(result.manifest, result.canonical_records) == []


def test_maximum_iso_year_month_window_is_supported(tmp_path: Path) -> None:
    manifest = make_manifest()
    manifest["snapshots"] = [
        {
            "snapshot_id": "last-day",
            "snapshot_date": "9999-12-31",
            "valid_start": "9999-12-31",
            "valid_end": "9999-12-31",
        }
    ]
    record = make_record("9999-12-31", "0~1℃")
    record["snapshot_id"] = "last-day"
    input_path, manifest_path = write_dataset(tmp_path, [record], manifest=manifest)

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    rows = build_monthly_summary(result.manifest, result.canonical_records)
    assert len(rows) == 1
    assert rows[0]["year_month"] == "9999-12"
    assert rows[0]["period_end"] == date.max
