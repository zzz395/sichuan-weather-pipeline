"""Deterministic descriptive analysis over canonical forecast records.

The functions in this module deliberately return unformatted values.  In
particular, temperature metrics and weather proportions remain ``Decimal``
instances so the serialization layer can apply the contract's four- and
six-decimal output formats without introducing premature rounding.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any, TypeAlias


SUMMARY_FIELDS = (
    "snapshot_id",
    "snapshot_date",
    "location",
    "period_start",
    "period_end",
    "n_records",
    "n_temperature",
    "n_weather",
    "valid_date_start",
    "valid_date_end",
    "min_forecast_low_c",
    "max_forecast_high_c",
    "mean_forecast_range_width_c",
    "mean_forecast_midpoint_c",
)

# The overall file uses the base summary schema verbatim.  Keeping a named
# alias makes the intended output-file mapping explicit to callers.
OVERALL_SUMMARY_FIELDS = SUMMARY_FIELDS

MONTHLY_SUMMARY_FIELDS = (
    "snapshot_id",
    "snapshot_date",
    "location",
    "year_month",
    "period_start",
    "period_end",
    "n_records",
    "n_temperature",
    "n_weather",
    "valid_date_start",
    "valid_date_end",
    "min_forecast_low_c",
    "max_forecast_high_c",
    "mean_forecast_range_width_c",
    "mean_forecast_midpoint_c",
)

WEATHER_FREQUENCY_FIELDS = (
    "snapshot_id",
    "snapshot_date",
    "location",
    "period_start",
    "period_end",
    "weather",
    "count",
    "n_weather",
    "proportion",
)


AnalysisRow: TypeAlias = OrderedDict[str, object]
GroupKey: TypeAlias = tuple[str, str]


def _as_date(value: object) -> date:
    """Return a date from a canonical date or ISO-date string."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"expected a date or ISO date string, got {type(value).__name__}")


def _as_decimal(value: object) -> Decimal:
    """Return a canonical Decimal without accepting binary float input."""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)):
        return Decimal(value)
    raise TypeError(f"expected Decimal-compatible canonical data, got {type(value).__name__}")


def _snapshots_in_order(manifest: Any) -> list[Any]:
    return sorted(
        manifest.snapshots,
        key=lambda snapshot: (
            _as_date(snapshot.snapshot_date),
            snapshot.snapshot_id,
        ),
    )


def _locations_in_order(manifest: Any) -> list[str]:
    # Python's string ordering is Unicode code-point ordering, as required by
    # the canonical text-sorting contract.
    return sorted(manifest.locations)


def _records_by_group(records: Iterable[Any]) -> dict[GroupKey, list[Any]]:
    groups: dict[GroupKey, list[Any]] = defaultdict(list)
    for record in records:
        groups[(record.snapshot_id, record.location)].append(record)
    return groups


def _records_by_month(records: Iterable[Any]) -> dict[tuple[str, str, str], list[Any]]:
    groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for record in records:
        valid_date = _as_date(record.valid_date)
        groups[
            (
                record.snapshot_id,
                record.location,
                f"{valid_date.year:04d}-{valid_date.month:02d}",
            )
        ].append(record)
    return groups


def _iter_month_windows(start: object, end: object) -> Iterator[tuple[str, date, date]]:
    """Yield each calendar-month intersection with the closed valid window."""

    valid_start = _as_date(start)
    valid_end = _as_date(end)
    cursor = date(valid_start.year, valid_start.month, 1)

    while cursor <= valid_end:
        if cursor == date(9999, 12, 1):
            next_month = None
            month_end = date.max
        elif cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
            month_end = next_month - timedelta(days=1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
            month_end = next_month - timedelta(days=1)
        yield (
            f"{cursor.year:04d}-{cursor.month:02d}",
            max(valid_start, cursor),
            min(valid_end, month_end),
        )
        if next_month is None:
            break
        cursor = next_month


def _summary_metrics(records: Sequence[Any]) -> dict[str, object]:
    valid_dates = [_as_date(record.valid_date) for record in records]
    temperature_intervals: list[tuple[Decimal, Decimal]] = []
    n_weather = 0

    for record in records:
        lower = record.temp_min_c
        upper = record.temp_max_c
        if lower is not None and upper is not None:
            temperature_intervals.append((_as_decimal(lower), _as_decimal(upper)))
        if record.weather is not None:
            n_weather += 1

    n_temperature = len(temperature_intervals)
    metrics: dict[str, object] = {
        "n_records": len(records),
        "n_temperature": n_temperature,
        "n_weather": n_weather,
        "valid_date_start": min(valid_dates) if valid_dates else None,
        "valid_date_end": max(valid_dates) if valid_dates else None,
        "min_forecast_low_c": None,
        "max_forecast_high_c": None,
        "mean_forecast_range_width_c": None,
        "mean_forecast_midpoint_c": None,
    }

    if not temperature_intervals:
        return metrics

    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        divisor = Decimal(n_temperature)
        total_width = sum(
            (upper - lower for lower, upper in temperature_intervals),
            start=Decimal(0),
        )
        total_midpoint = sum(
            (
                (lower + upper) / Decimal(2)
                for lower, upper in temperature_intervals
            ),
            start=Decimal(0),
        )
        metrics.update(
            {
                "min_forecast_low_c": min(
                    lower for lower, _ in temperature_intervals
                ),
                "max_forecast_high_c": max(
                    upper for _, upper in temperature_intervals
                ),
                "mean_forecast_range_width_c": total_width / divisor,
                "mean_forecast_midpoint_c": total_midpoint / divisor,
            }
        )

    return metrics


def _summary_row(
    snapshot: Any,
    location: str,
    period_start: date,
    period_end: date,
    records: Sequence[Any],
    *,
    year_month: str | None = None,
) -> AnalysisRow:
    metrics = _summary_metrics(records)
    values: dict[str, object] = {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_date": _as_date(snapshot.snapshot_date),
        "location": location,
        "period_start": period_start,
        "period_end": period_end,
        **metrics,
    }
    if year_month is not None:
        values["year_month"] = year_month
        fields = MONTHLY_SUMMARY_FIELDS
    else:
        fields = OVERALL_SUMMARY_FIELDS
    return OrderedDict((field, values[field]) for field in fields)


def build_overall_summary(manifest: Any, records: Iterable[Any]) -> list[AnalysisRow]:
    """Build expanded snapshot/location summary rows in canonical order."""

    groups = _records_by_group(records)
    rows: list[AnalysisRow] = []
    for snapshot in _snapshots_in_order(manifest):
        for location in _locations_in_order(manifest):
            rows.append(
                _summary_row(
                    snapshot,
                    location,
                    _as_date(snapshot.valid_start),
                    _as_date(snapshot.valid_end),
                    groups.get((snapshot.snapshot_id, location), ()),
                )
            )
    return rows


def build_monthly_summary(manifest: Any, records: Iterable[Any]) -> list[AnalysisRow]:
    """Build all manifest-defined monthly groups, including empty months."""

    groups = _records_by_month(records)
    rows: list[AnalysisRow] = []
    for snapshot in _snapshots_in_order(manifest):
        month_windows = list(
            _iter_month_windows(snapshot.valid_start, snapshot.valid_end)
        )
        for location in _locations_in_order(manifest):
            for year_month, period_start, period_end in month_windows:
                rows.append(
                    _summary_row(
                        snapshot,
                        location,
                        period_start,
                        period_end,
                        groups.get(
                            (snapshot.snapshot_id, location, year_month), ()
                        ),
                        year_month=year_month,
                    )
                )
    return rows


def build_weather_frequency(
    manifest: Any,
    records: Iterable[Any],
) -> list[AnalysisRow]:
    """Build overall non-missing weather frequencies in canonical order."""

    groups = _records_by_group(records)
    rows: list[AnalysisRow] = []

    for snapshot in _snapshots_in_order(manifest):
        for location in _locations_in_order(manifest):
            group = groups.get((snapshot.snapshot_id, location), ())
            counts: dict[str, int] = defaultdict(int)
            for record in group:
                if record.weather is not None:
                    counts[record.weather] += 1

            n_weather = sum(counts.values())
            if n_weather == 0:
                continue

            for weather in sorted(counts):
                count = counts[weather]
                with localcontext() as context:
                    context.prec = 28
                    context.rounding = ROUND_HALF_EVEN
                    proportion = Decimal(count) / Decimal(n_weather)
                values: dict[str, object] = {
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_date": _as_date(snapshot.snapshot_date),
                    "location": location,
                    "period_start": _as_date(snapshot.valid_start),
                    "period_end": _as_date(snapshot.valid_end),
                    "weather": weather,
                    "count": count,
                    "n_weather": n_weather,
                    "proportion": proportion,
                }
                rows.append(
                    OrderedDict(
                        (field, values[field])
                        for field in WEATHER_FREQUENCY_FIELDS
                    )
                )

    return rows
