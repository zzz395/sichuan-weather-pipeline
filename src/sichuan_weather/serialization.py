"""Deterministic serialization for pipeline artifacts."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import CanonicalRecord, ValidationResult


CLEANED_FIELDS = (
    "location",
    "snapshot_id",
    "snapshot_date",
    "valid_date",
    "temp_min_c",
    "temp_max_c",
    "temp_midpoint_c",
    "weather",
    "source_lines",
)

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

_SUMMARY_DECIMAL_FIELDS = {
    "min_forecast_low_c",
    "max_forecast_high_c",
    "mean_forecast_range_width_c",
    "mean_forecast_midpoint_c",
}


def _decimal_text(value: Decimal | None, places: int) -> str:
    if value is None:
        return ""
    if not value.is_finite():
        raise ValueError("decimal output values must be finite")
    quantum = Decimal(1).scaleb(-places)
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        rounded = value.quantize(quantum)
    if rounded == 0:
        rounded = abs(rounded)
    return format(rounded, f".{places}f")


def _plain_text(value: Any) -> str | int:
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | str):
        return value
    return str(value)


def write_cleaned_csv(path: Path, records: Sequence[CanonicalRecord]) -> None:
    """Write canonical records using the frozen nine-column schema."""
    ordered = sorted(
        records,
        key=lambda record: (
            record.snapshot_date,
            record.snapshot_id,
            record.location,
            record.valid_date,
        ),
    )
    rows: list[dict[str, str | int]] = []
    for record in ordered:
        rows.append(
            {
                "location": record.location,
                "snapshot_id": record.snapshot_id,
                "snapshot_date": record.snapshot_date.isoformat(),
                "valid_date": record.valid_date.isoformat(),
                "temp_min_c": _decimal_text(record.temp_min_c, 2),
                "temp_max_c": _decimal_text(record.temp_max_c, 2),
                "temp_midpoint_c": _decimal_text(record.temp_midpoint_c, 2),
                "weather": record.weather or "",
                "source_lines": json.dumps(
                    list(record.source_lines),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
    _write_dict_rows(path, CLEANED_FIELDS, rows)


def write_summary_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    _write_analysis_rows(path, SUMMARY_FIELDS, rows)


def write_monthly_summary_csv(
    path: Path, rows: Iterable[Mapping[str, Any]]
) -> None:
    _write_analysis_rows(path, MONTHLY_SUMMARY_FIELDS, rows)


def write_weather_frequency_csv(
    path: Path, rows: Iterable[Mapping[str, Any]]
) -> None:
    serialized: list[dict[str, str | int]] = []
    for row in rows:
        serialized.append(
            {
                field: (
                    _decimal_text(row[field], 6)
                    if field == "proportion"
                    else _plain_text(row[field])
                )
                for field in WEATHER_FREQUENCY_FIELDS
            }
        )
    _write_dict_rows(path, WEATHER_FREQUENCY_FIELDS, serialized)


def _write_analysis_rows(
    path: Path,
    fields: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> None:
    serialized: list[dict[str, str | int]] = []
    for row in rows:
        serialized.append(
            {
                field: (
                    _decimal_text(row[field], 4)
                    if field in _SUMMARY_DECIMAL_FIELDS
                    else _plain_text(row[field])
                )
                for field in fields
            }
        )
    _write_dict_rows(path, fields, serialized)


def _write_dict_rows(
    path: Path,
    fields: Sequence[str],
    rows: Iterable[Mapping[str, str | int]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=list(fields),
            extrasaction="raise",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def quality_document(
    result: ValidationResult,
    *,
    analysis_generated: bool,
    output_records: int,
) -> dict[str, Any]:
    """Build the stable JSON-compatible quality report."""
    manifest = result.manifest
    counts = result.counts
    return {
        "schema_version": manifest.schema_version if manifest else None,
        "dataset_kind": manifest.dataset_kind if manifest else None,
        "status": result.status,
        "validation_complete": result.validation_complete,
        "analysis_generated": analysis_generated,
        "raw_input_sha256": result.input_sha256,
        "manifest_sha256": result.manifest_sha256,
        "counts": {
            "input_records": counts.input_records,
            "individually_valid_records": counts.individually_valid_records,
            "invalid_records": counts.invalid_records,
            "conflict_keys": counts.conflict_keys,
            "conflict_rows": counts.conflict_rows,
            "duplicate_rows_removed": counts.duplicate_rows_removed,
            "eligible_unique_records": counts.eligible_unique_records,
            "output_records": output_records,
            "post_dedup_missing_temperature_records": counts.missing_temperature,
            "post_dedup_missing_weather_records": counts.missing_weather,
            "explicit_missing_month_groups": counts.missing_month_groups,
        },
        "missing_month_groups": [
            {
                "snapshot_id": group.snapshot_id,
                "location": group.location,
                "year_month": group.year_month,
                "period_start": group.period_start.isoformat(),
                "period_end": group.period_end.isoformat(),
            }
            for group in result.missing_month_groups
        ],
        "issues": [issue.to_dict() for issue in result.issues],
    }


def quality_json_text(document: Mapping[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        separators=(",", ": "),
    ) + "\n"


def write_quality_json(path: Path, document: Mapping[str, Any]) -> None:
    path.write_text(quality_json_text(document), encoding="utf-8", newline="\n")
