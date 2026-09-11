"""Date and temperature parsing regressions."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sichuan_weather.models import SnapshotDefinition
from sichuan_weather.validation import (
    ContractValueError,
    normalize_location,
    normalize_snapshot_id,
    normalize_weather,
    parse_forecast_date,
    parse_temperature,
    validate_dataset,
)

from tests.helpers import make_record, write_dataset


def _snapshot(
    valid_start: date,
    valid_end: date,
    *,
    snapshot_date: date | None = None,
) -> SnapshotDefinition:
    return SnapshotDefinition(
        snapshot_id="unit-snapshot",
        snapshot_date=snapshot_date or valid_start,
        valid_start=valid_start,
        valid_end=valid_end,
    )


def _assert_rule(error: pytest.ExceptionInfo[ContractValueError], rule_id: str) -> None:
    assert error.value.rule_id == rule_id
    assert str(error.value)


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        ("星期一", date(2027, 1, 4)),
        ("周一", date(2027, 1, 4)),
        ("星期二", date(2027, 1, 5)),
        ("周二", date(2027, 1, 5)),
        ("星期三", date(2027, 1, 6)),
        ("周三", date(2027, 1, 6)),
        ("星期四", date(2027, 1, 7)),
        ("周四", date(2027, 1, 7)),
        ("星期五", date(2027, 1, 8)),
        ("周五", date(2027, 1, 8)),
        ("星期六", date(2027, 1, 9)),
        ("周六", date(2027, 1, 9)),
        ("星期日", date(2027, 1, 10)),
        ("星期天", date(2027, 1, 10)),
        ("周日", date(2027, 1, 10)),
        ("周天", date(2027, 1, 10)),
    ],
)
@pytest.mark.parametrize("line_break", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_all_weekday_suffixes_and_line_endings_are_accepted(
    suffix: str,
    expected: date,
    line_break: str,
) -> None:
    snapshot = _snapshot(date(2027, 1, 4), date(2027, 1, 10))

    assert (
        parse_forecast_date(expected.strftime("%m-%d") + line_break + suffix, snapshot)
        == expected
    )


def test_forecast_window_endpoints_are_closed_and_outside_dates_are_rejected() -> None:
    snapshot = _snapshot(date(2026, 12, 30), date(2027, 1, 2))

    assert parse_forecast_date("2026-12-30", snapshot) == date(2026, 12, 30)
    assert parse_forecast_date("2027-01-02", snapshot) == date(2027, 1, 2)
    for raw in ("2026-12-29", "2027-01-03"):
        with pytest.raises(ContractValueError) as caught:
            parse_forecast_date(raw, snapshot)
        _assert_rule(caught, "Q04")


@pytest.mark.parametrize(
    "raw",
    [
        "2026/12/30",
        "2026-2-03",
        "26-12-30",
        "2026-02-30",
        "02-30",
        "12-30 星期三",
        "12-30\nMonday",
    ],
)
def test_invalid_date_syntax_and_impossible_dates_are_q04(raw: str) -> None:
    snapshot = _snapshot(date(2026, 1, 1), date(2027, 12, 31))

    with pytest.raises(ContractValueError) as caught:
        parse_forecast_date(raw, snapshot)

    _assert_rule(caught, "Q04")


def test_month_day_with_multiple_candidate_years_is_rejected_as_ambiguous() -> None:
    snapshot = _snapshot(date(2024, 1, 1), date(2025, 12, 31))

    with pytest.raises(ContractValueError) as caught:
        parse_forecast_date("01-02", snapshot)

    _assert_rule(caught, "Q04")
    assert "more than one year" in str(caught.value)


def test_month_day_without_a_candidate_in_the_window_is_rejected() -> None:
    snapshot = _snapshot(date(2026, 12, 30), date(2027, 1, 2))

    with pytest.raises(ContractValueError) as caught:
        parse_forecast_date("02-01", snapshot)

    _assert_rule(caught, "Q04")
    assert "does not occur" in str(caught.value)


def test_leap_day_is_resolved_only_in_a_leap_year() -> None:
    leap_snapshot = _snapshot(date(2028, 2, 28), date(2028, 3, 1))
    ordinary_snapshot = _snapshot(date(2027, 2, 28), date(2027, 3, 1))

    assert parse_forecast_date("02-29", leap_snapshot) == date(2028, 2, 29)
    for raw, snapshot in (
        ("2027-02-29", ordinary_snapshot),
        ("02-29", ordinary_snapshot),
    ):
        with pytest.raises(ContractValueError) as caught:
            parse_forecast_date(raw, snapshot)
        _assert_rule(caught, "Q04")


def test_month_day_resolution_depends_on_manifest_year_not_system_year() -> None:
    snapshot = _snapshot(
        date(2099, 12, 31),
        date(2100, 1, 1),
        snapshot_date=date(2099, 12, 30),
    )

    assert parse_forecast_date("12-31", snapshot) == date(2099, 12, 31)
    assert parse_forecast_date("01-01", snapshot) == date(2100, 1, 1)


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "—", "N/A", "n/a"],
    ids=["json-null", "empty", "whitespace", "em-dash", "upper-na", "lower-na"],
)
def test_all_temperature_missing_markers_are_null_not_zero(raw: str | None) -> None:
    assert parse_temperature(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "1~2",
        "1～2",
        "1℃~2℃",
        "1 °C ～ 2 °C",
        "1C~2C",
        "1℃ ～ 2 C",
    ],
)
def test_supported_celsius_units_and_range_separators_normalize_equally(
    raw: str,
) -> None:
    parsed = parse_temperature(raw)

    assert parsed is not None
    assert parsed.temp_min_c == Decimal("1")
    assert parsed.temp_max_c == Decimal("2")
    assert parsed.temp_midpoint_c == Decimal("1.5")


@pytest.mark.parametrize(
    "raw",
    [
        "1~2°F",
        "1e0~2℃",
        "1.00~2℃",
        "1~2.00℃",
        "~2℃",
        "1~℃",
        "N/A~2℃",
        "1~N/A",
        "NaN~2℃",
        "1~Infinity℃",
        "1-2℃",
    ],
)
def test_malformed_temperature_forms_are_q06(raw: str) -> None:
    with pytest.raises(ContractValueError) as caught:
        parse_temperature(raw)

    _assert_rule(caught, "Q06")


def test_negative_cross_zero_and_equal_temperature_ranges_are_exact() -> None:
    negative = parse_temperature("-5~-1℃")
    crossing = parse_temperature("-2~4℃")
    equal = parse_temperature("-4~-4℃")

    assert negative is not None
    assert (
        negative.temp_min_c,
        negative.temp_max_c,
        negative.temp_midpoint_c,
    ) == (Decimal("-5"), Decimal("-1"), Decimal("-3"))
    assert crossing is not None
    assert (
        crossing.temp_min_c,
        crossing.temp_max_c,
        crossing.temp_midpoint_c,
    ) == (Decimal("-2"), Decimal("4"), Decimal("1"))
    assert equal is not None
    assert (
        equal.temp_min_c,
        equal.temp_max_c,
        equal.temp_midpoint_c,
    ) == (Decimal("-4"), Decimal("-4"), Decimal("-4"))


def test_one_decimal_endpoints_preserve_two_decimal_midpoint_exactly() -> None:
    parsed = parse_temperature("1.2~1.3℃")

    assert parsed is not None
    assert parsed.temp_min_c == Decimal("1.2")
    assert parsed.temp_max_c == Decimal("1.3")
    assert parsed.temp_midpoint_c == Decimal("1.25")


def test_negative_zero_is_removed_from_all_temperature_values() -> None:
    parsed = parse_temperature("-0.0~+0℃")

    assert parsed is not None
    for value in (
        parsed.temp_min_c,
        parsed.temp_max_c,
        parsed.temp_midpoint_c,
    ):
        assert value == Decimal("0")
        assert not value.is_signed()


def test_inverted_temperature_is_q07_and_is_not_swapped() -> None:
    with pytest.raises(ContractValueError) as caught:
        parse_temperature("7~2℃")

    _assert_rule(caught, "Q07")


def test_text_normalization_is_nfc_trimmed_case_preserving_and_non_splitting() -> None:
    assert normalize_location("  Cafe\u0301  ") == "Café"
    assert normalize_location("  CHENGdu  ") == "CHENGdu"
    assert normalize_weather("  Rain / SNOW + Fog  ") == "Rain / SNOW + Fog"
    assert normalize_weather("  Cafe\u0301  ") == "Café"


@pytest.mark.parametrize("raw", ["", "   ", "snow day", "雪", "line\nbreak"])
def test_snapshot_identifiers_must_be_nonempty_printable_ascii_without_space(
    raw: str,
) -> None:
    with pytest.raises(ContractValueError) as caught:
        normalize_snapshot_id(raw)

    _assert_rule(caught, "Q02")


def test_cross_year_resolution_and_weekday_mismatch(tmp_path: Path) -> None:
    valid_input, valid_manifest = write_dataset(
        tmp_path / "valid",
        [
            make_record("12-30\n星期三", "-2~4℃"),
            make_record("01-01", "-4~-4℃"),
        ],
    )
    valid_result = validate_dataset(valid_input, valid_manifest)

    assert valid_result.is_valid
    assert [record.valid_date for record in valid_result.canonical_records] == [
        date(2026, 12, 30),
        date(2027, 1, 1),
    ]

    invalid_input, invalid_manifest = write_dataset(
        tmp_path / "invalid",
        [make_record("12-30\n星期四", "-2~4℃")],
    )
    invalid_result = validate_dataset(invalid_input, invalid_manifest)

    assert not invalid_result.is_valid
    assert any(
        issue.rule_id == "Q04"
        and issue.field == "forecast_date_raw"
        and issue.source_lines == (1,)
        for issue in invalid_result.issues
    )


def test_missing_temperature_and_decimal_midpoint(tmp_path: Path) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [
            make_record("12-30", None),
            make_record("12-31", "1.5 ~ 6.5 °C", "Rain"),
        ],
    )

    result = validate_dataset(input_path, manifest_path)

    assert result.is_valid
    records = {record.valid_date: record for record in result.canonical_records}
    missing = records[date(2026, 12, 30)]
    assert (
        missing.temp_min_c,
        missing.temp_max_c,
        missing.temp_midpoint_c,
    ) == (None, None, None)
    missing_issue = next(issue for issue in result.issues if issue.rule_id == "Q05")
    assert missing_issue.severity == "WARNING"
    assert missing_issue.source_lines == (1,)
    assert missing_issue.logical_key == (
        "test-snapshot",
        "Chengdu",
        date(2026, 12, 30),
    )
    midpoint = records[date(2026, 12, 31)].temp_midpoint_c
    assert isinstance(midpoint, Decimal)
    assert midpoint == Decimal("4.0")


@pytest.mark.parametrize(
    ("temperature_raw", "expected_rule"),
    [("2~N/A", "Q06"), ("7~2℃", "Q07")],
)
def test_invalid_temperature_is_rejected(
    tmp_path: Path,
    temperature_raw: str,
    expected_rule: str,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path,
        [make_record("12-30", temperature_raw)],
    )

    result = validate_dataset(input_path, manifest_path)

    assert not result.is_valid
    assert result.counts.input_records == 1
    assert result.counts.individually_valid_records == 0
    assert result.counts.invalid_records == 1
    assert result.canonical_records == ()
    assert any(
        issue.rule_id == expected_rule
        and issue.severity == "ERROR"
        and issue.source_lines == (1,)
        for issue in result.issues
    )
