"""Fresh-data factories and small, implementation-independent test helpers."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def make_manifest() -> dict[str, object]:
    """Return a new valid manifest object on every call."""
    return {
        "schema_version": 1,
        "dataset_kind": "synthetic",
        "source_description": (
            "Hand-authored synthetic forecast records for pipeline "
            "demonstration and testing."
        ),
        "locations": ["Chengdu"],
        "snapshots": [
            {
                "snapshot_id": "test-snapshot",
                "snapshot_date": "2026-12-30",
                "valid_start": "2026-12-30",
                "valid_end": "2027-01-02",
            }
        ],
    }


def make_record(
    forecast_date_raw: str,
    temperature_raw: str | None,
    weather_raw: str | None = "Cloudy",
) -> dict[str, object]:
    """Return a new raw-record object on every call."""
    return {
        "location": "Chengdu",
        "snapshot_id": "test-snapshot",
        "forecast_date_raw": forecast_date_raw,
        "temperature_raw": temperature_raw,
        "weather_raw": weather_raw,
    }


def write_dataset(
    root: Path,
    records: list[dict[str, object]],
    *,
    manifest: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    input_path = root / "records.jsonl"
    manifest_path = root / "manifest.json"
    input_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
        newline="\n",
    )
    manifest_path.write_text(
        json.dumps(manifest or make_manifest(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return input_path, manifest_path


def business_rows(result: object) -> list[tuple[object, ...]]:
    return [
        (
            record.location,
            record.snapshot_id,
            record.snapshot_date,
            record.valid_date,
            record.temp_min_c,
            record.temp_max_c,
            record.temp_midpoint_c,
            record.weather,
        )
        for record in result.canonical_records
    ]


def artifact_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))
