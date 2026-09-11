#!/usr/bin/env python3
"""Independently verify a frozen-sample pipeline output directory."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
from typing import Any, Iterable


EXPECTED_FILES = (
    "cleaned.csv",
    "figures/temperature-ranges.png",
    "figures/weather-frequency.png",
    "monthly_summary.csv",
    "quality.json",
    "summary.csv",
    "weather_frequency.csv",
)
EXPECTED_DIRECTORIES = frozenset({"figures"})
PNG_SIZE = (1800, 1200)
PNG_DPI = 150.0

SAMPLE_RECORDS_SHA256 = (
    "71e6fff3bd7d349a3415b93f1a7657df04767ecce5bb43e92abdb1552edb55bc"
)
SAMPLE_MANIFEST_SHA256 = (
    "30cc3661478e986cad1408fdbe1323f1dfc6d9e4049531919abfb0c6df78dcad"
)


def _csv_bytes(rows: Iterable[Iterable[str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


CLEANED_ROWS = (
    ("location", "snapshot_id", "snapshot_date", "valid_date", "temp_min_c", "temp_max_c", "temp_midpoint_c", "weather", "source_lines"),
    ("Chengdu", "sample-a", "2026-12-30", "2026-12-30", "-2.00", "4.00", "1.00", "Cloudy", "[1,9]"),
    ("Chengdu", "sample-a", "2026-12-30", "2026-12-31", "0.00", "6.00", "3.00", "Sunny", "[2]"),
    ("Chengdu", "sample-a", "2026-12-30", "2027-01-01", "-4.00", "-4.00", "-4.00", "Snow", "[3]"),
    ("Chengdu", "sample-a", "2026-12-30", "2027-01-02", "", "", "", "Cloudy", "[4]"),
    ("Mianyang", "sample-a", "2026-12-30", "2026-12-30", "-1.00", "5.00", "2.00", "Cloudy", "[5,14]"),
    ("Mianyang", "sample-a", "2026-12-30", "2026-12-31", "2.00", "8.00", "5.00", "Sunny", "[6]"),
    ("Mianyang", "sample-a", "2026-12-30", "2027-01-01", "1.50", "6.50", "4.00", "Rain", "[7]"),
    ("Mianyang", "sample-a", "2026-12-30", "2027-01-02", "", "", "", "", "[8]"),
    ("Chengdu", "sample-b", "2026-12-31", "2026-12-31", "1.00", "7.00", "4.00", "Sunny", "[10]"),
    ("Chengdu", "sample-b", "2026-12-31", "2027-01-01", "-3.00", "1.00", "-1.00", "Cloudy", "[11]"),
    ("Mianyang", "sample-b", "2026-12-31", "2027-01-01", "2.00", "8.00", "5.00", "Rain", "[12]"),
    ("Mianyang", "sample-b", "2026-12-31", "2027-01-02", "3.00", "9.00", "6.00", "Rain", "[13]"),
)

SUMMARY_ROWS = (
    ("snapshot_id", "snapshot_date", "location", "period_start", "period_end", "n_records", "n_temperature", "n_weather", "valid_date_start", "valid_date_end", "min_forecast_low_c", "max_forecast_high_c", "mean_forecast_range_width_c", "mean_forecast_midpoint_c"),
    ("sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "4", "3", "4", "2026-12-30", "2027-01-02", "-4.0000", "6.0000", "4.0000", "0.0000"),
    ("sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "4", "3", "3", "2026-12-30", "2027-01-02", "-1.0000", "8.0000", "5.6667", "3.6667"),
    ("sample-b", "2026-12-31", "Chengdu", "2026-12-31", "2027-01-02", "2", "2", "2", "2026-12-31", "2027-01-01", "-3.0000", "7.0000", "5.0000", "1.5000"),
    ("sample-b", "2026-12-31", "Mianyang", "2026-12-31", "2027-01-02", "2", "2", "2", "2027-01-01", "2027-01-02", "2.0000", "9.0000", "6.0000", "5.5000"),
)

MONTHLY_ROWS = (
    ("snapshot_id", "snapshot_date", "location", "year_month", "period_start", "period_end", "n_records", "n_temperature", "n_weather", "valid_date_start", "valid_date_end", "min_forecast_low_c", "max_forecast_high_c", "mean_forecast_range_width_c", "mean_forecast_midpoint_c"),
    ("sample-a", "2026-12-30", "Chengdu", "2026-12", "2026-12-30", "2026-12-31", "2", "2", "2", "2026-12-30", "2026-12-31", "-2.0000", "6.0000", "6.0000", "2.0000"),
    ("sample-a", "2026-12-30", "Chengdu", "2027-01", "2027-01-01", "2027-01-02", "2", "1", "2", "2027-01-01", "2027-01-02", "-4.0000", "-4.0000", "0.0000", "-4.0000"),
    ("sample-a", "2026-12-30", "Mianyang", "2026-12", "2026-12-30", "2026-12-31", "2", "2", "2", "2026-12-30", "2026-12-31", "-1.0000", "8.0000", "6.0000", "3.5000"),
    ("sample-a", "2026-12-30", "Mianyang", "2027-01", "2027-01-01", "2027-01-02", "2", "1", "1", "2027-01-01", "2027-01-02", "1.5000", "6.5000", "5.0000", "4.0000"),
    ("sample-b", "2026-12-31", "Chengdu", "2026-12", "2026-12-31", "2026-12-31", "1", "1", "1", "2026-12-31", "2026-12-31", "1.0000", "7.0000", "6.0000", "4.0000"),
    ("sample-b", "2026-12-31", "Chengdu", "2027-01", "2027-01-01", "2027-01-02", "1", "1", "1", "2027-01-01", "2027-01-01", "-3.0000", "1.0000", "4.0000", "-1.0000"),
    ("sample-b", "2026-12-31", "Mianyang", "2026-12", "2026-12-31", "2026-12-31", "0", "0", "0", "", "", "", "", "", ""),
    ("sample-b", "2026-12-31", "Mianyang", "2027-01", "2027-01-01", "2027-01-02", "2", "2", "2", "2027-01-01", "2027-01-02", "2.0000", "9.0000", "6.0000", "5.5000"),
)

WEATHER_ROWS = (
    ("snapshot_id", "snapshot_date", "location", "period_start", "period_end", "weather", "count", "n_weather", "proportion"),
    ("sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "Cloudy", "2", "4", "0.500000"),
    ("sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "Snow", "1", "4", "0.250000"),
    ("sample-a", "2026-12-30", "Chengdu", "2026-12-30", "2027-01-02", "Sunny", "1", "4", "0.250000"),
    ("sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "Cloudy", "1", "3", "0.333333"),
    ("sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "Rain", "1", "3", "0.333333"),
    ("sample-a", "2026-12-30", "Mianyang", "2026-12-30", "2027-01-02", "Sunny", "1", "3", "0.333333"),
    ("sample-b", "2026-12-31", "Chengdu", "2026-12-31", "2027-01-02", "Cloudy", "1", "2", "0.500000"),
    ("sample-b", "2026-12-31", "Chengdu", "2026-12-31", "2027-01-02", "Sunny", "1", "2", "0.500000"),
    ("sample-b", "2026-12-31", "Mianyang", "2026-12-31", "2027-01-02", "Rain", "2", "2", "1.000000"),
)

EXPECTED_QUALITY: dict[str, Any] = {
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
    "missing_month_groups": [
        {
            "snapshot_id": "sample-b",
            "location": "Mianyang",
            "year_month": "2026-12",
            "period_start": "2026-12-31",
            "period_end": "2026-12-31",
        }
    ],
    "issues": [
        {
            "rule_id": "Q12",
            "severity": "WARNING",
            "source_lines": [],
            "field": "year_month",
            "message": "manifest-defined group sample-b/Mianyang/2026-12 has no records",
        },
        {
            "rule_id": "Q09",
            "severity": "INFO",
            "source_lines": [1, 9],
            "logical_key": {
                "snapshot_id": "sample-a",
                "location": "Chengdu",
                "valid_date": "2026-12-30",
            },
            "message": "exact duplicate records were deterministically merged",
        },
        {
            "rule_id": "Q05",
            "severity": "WARNING",
            "source_lines": [4],
            "field": "temperature_raw",
            "logical_key": {
                "snapshot_id": "sample-a",
                "location": "Chengdu",
                "valid_date": "2027-01-02",
            },
            "message": "temperature is missing",
        },
        {
            "rule_id": "Q09",
            "severity": "INFO",
            "source_lines": [5, 14],
            "logical_key": {
                "snapshot_id": "sample-a",
                "location": "Mianyang",
                "valid_date": "2026-12-30",
            },
            "message": "exact duplicate records were deterministically merged",
        },
        {
            "rule_id": "Q05",
            "severity": "WARNING",
            "source_lines": [8],
            "field": "temperature_raw",
            "logical_key": {
                "snapshot_id": "sample-a",
                "location": "Mianyang",
                "valid_date": "2027-01-02",
            },
            "message": "temperature is missing",
        },
        {
            "rule_id": "Q08",
            "severity": "WARNING",
            "source_lines": [8],
            "field": "weather_raw",
            "logical_key": {
                "snapshot_id": "sample-a",
                "location": "Mianyang",
                "valid_date": "2027-01-02",
            },
            "message": "weather is missing",
        },
    ],
}

EXPECTED_TEXT_BYTES = {
    "cleaned.csv": _csv_bytes(CLEANED_ROWS),
    "summary.csv": _csv_bytes(SUMMARY_ROWS),
    "monthly_summary.csv": _csv_bytes(MONTHLY_ROWS),
    "weather_frequency.csv": _csv_bytes(WEATHER_ROWS),
    "quality.json": (
        json.dumps(
            EXPECTED_QUALITY,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8"),
}


def _display(relative: str) -> str:
    return json.dumps(relative, ensure_ascii=True)


def _inventory(root: Path) -> tuple[set[str], set[str], list[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    errors: list[str] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as error:
        return files, directories, [f"cannot enumerate output ({type(error).__name__})"]
    for path in entries:
        relative = path.relative_to(root).as_posix()
        try:
            if path.is_symlink():
                errors.append(f"symbolic link is not allowed: {_display(relative)}")
            elif path.is_dir():
                directories.add(relative)
            elif path.is_file():
                files.add(relative)
            else:
                errors.append(f"unsupported filesystem entry: {_display(relative)}")
        except OSError as error:
            errors.append(
                f"cannot inspect {_display(relative)} ({type(error).__name__})"
            )
    return files, directories, errors


def _parse_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _quality_errors(raw: bytes) -> list[str]:
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_parse_json_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        return ["quality.json is not strict UTF-8 JSON"]
    errors: list[str] = []
    if document != EXPECTED_QUALITY:
        errors.append("quality.json report differs from the frozen sample oracle")
    if not isinstance(document, dict):
        return errors
    counts = document.get("counts")
    if isinstance(counts, dict) and all(
        isinstance(counts.get(name), int) and not isinstance(counts.get(name), bool)
        for name in (
            "input_records",
            "individually_valid_records",
            "invalid_records",
            "conflict_rows",
            "duplicate_rows_removed",
            "eligible_unique_records",
            "output_records",
        )
    ):
        if counts["input_records"] != (
            counts["individually_valid_records"] + counts["invalid_records"]
        ):
            errors.append("quality.json input-count identity is false")
        if counts["individually_valid_records"] != (
            counts["conflict_rows"]
            + counts["duplicate_rows_removed"]
            + counts["eligible_unique_records"]
        ):
            errors.append("quality.json deduplication-count identity is false")
        if counts["output_records"] != counts["eligible_unique_records"]:
            errors.append("quality.json output count does not match eligible records")
    else:
        errors.append("quality.json counts schema is invalid")
    return errors


def _text_errors(relative: str, raw: bytes) -> list[str]:
    errors: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append(f"{relative} has a forbidden UTF-8 BOM")
    if b"\r" in raw or not raw.endswith(b"\n"):
        errors.append(f"{relative} does not use final-LF-only serialization")
    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        errors.append(f"{relative} is not valid UTF-8")
    if raw != EXPECTED_TEXT_BYTES[relative]:
        errors.append(f"{relative} bytes differ from the frozen sample oracle")
    if relative == "quality.json":
        errors.extend(_quality_errors(raw))
    return errors


def _near_color_count(
    image: Any,
    box: tuple[int, int, int, int],
    target: tuple[int, int, int],
    *,
    tolerance: int = 12,
) -> int:
    crop = image.crop(box)
    get_flattened_data = getattr(crop, "get_flattened_data", None)
    pixels = (
        get_flattened_data()
        if callable(get_flattened_data)
        else crop.getdata()
    )
    return sum(
        1
        for pixel in pixels
        if all(abs(int(pixel[index]) - target[index]) <= tolerance for index in range(3))
    )


def _render_semantic_errors(relative: str, image: Any) -> list[str]:
    width, height = image.size
    panels = (
        (0, 120, width // 2, height // 2),
        (width // 2, 120, width, height // 2),
        (0, height // 2, width // 2, height),
        (width // 2, height // 2, width, height),
    )
    errors: list[str] = []
    warning_count = _near_color_count(
        image,
        (0, 20, width, 125),
        (155, 28, 28),
    )
    if warning_count < 8:
        errors.append(f"{relative}: synthetic warning rendering not detected")
    if relative.endswith("temperature-ranges.png"):
        absent = [
            str(index)
            for index, box in enumerate(panels, start=1)
            if _near_color_count(image, box, (209, 73, 91)) < 3
            or _near_color_count(image, box, (23, 63, 95)) < 5
        ]
        if absent:
            errors.append(
                f"{relative}: temperature data not detected in panels {','.join(absent)}"
            )
    else:
        absent = [
            str(index)
            for index, box in enumerate(panels, start=1)
            if _near_color_count(image, box, (76, 149, 108)) < 25
        ]
        if absent:
            errors.append(
                f"{relative}: weather bars not detected in panels {','.join(absent)}"
            )
    return errors


def _png_errors(relative: str, path: Path, image_module: Any) -> list[str]:
    errors: list[str] = []
    try:
        with image_module.open(path) as image:
            if image.format != "PNG":
                errors.append(f"{relative}: file format is not PNG")
            if image.size != PNG_SIZE:
                errors.append(f"{relative}: dimensions are not 1800x1200")
            if image.mode not in {"RGB", "RGBA"}:
                errors.append(f"{relative}: color mode is not RGB/RGBA")
            dpi = image.info.get("dpi")
            if (
                not isinstance(dpi, tuple)
                or len(dpi) != 2
                or any(abs(float(value) - PNG_DPI) > 0.05 for value in dpi)
            ):
                errors.append(f"{relative}: DPI metadata is not 150")
            if image.info.get("Software") != "sichuan-weather-pipeline":
                errors.append(f"{relative}: Software metadata is invalid")
            if any(
                token in str(key).casefold()
                for key in image.info
                for token in ("time", "date", "creation")
            ):
                errors.append(f"{relative}: timestamp metadata is forbidden")
            image.verify()
        with image_module.open(path) as image:
            image.load()
            rgb = image.convert("RGB")
            if all(low == high for low, high in rgb.getextrema()):
                errors.append(f"{relative}: image is uniformly blank")
            else:
                errors.extend(_render_semantic_errors(relative, rgb))
    except Exception as error:  # Pillow exposes several format-specific exceptions.
        errors.append(f"{relative}: PNG decode failed ({type(error).__name__})")
    return errors


def check_artifacts(output: Path) -> tuple[list[str], dict[str, str]]:
    """Return deterministic errors and hashes for a frozen sample run."""
    if output.is_symlink():
        return ["output directory must not be a symbolic link"], {}
    if not output.exists() or not output.is_dir():
        return ["output directory is missing or is not a directory"], {}

    files, directories, errors = _inventory(output)
    expected = set(EXPECTED_FILES)
    for relative in sorted(expected - files):
        errors.append(f"missing artifact: {_display(relative)}")
    for relative in sorted(files - expected):
        errors.append(f"unexpected artifact: {_display(relative)}")
    for relative in sorted(directories - EXPECTED_DIRECTORIES):
        errors.append(f"unexpected directory: {_display(relative)}")

    try:
        from PIL import Image
    except ImportError:
        Image = None
        errors.append("Pillow is required for PNG validation")

    hashes: dict[str, str] = {}
    for relative in EXPECTED_FILES:
        if relative not in files:
            continue
        path = output / Path(relative)
        try:
            raw = path.read_bytes()
        except OSError as error:
            errors.append(f"cannot read {_display(relative)} ({type(error).__name__})")
            continue
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        if relative in EXPECTED_TEXT_BYTES:
            errors.extend(_text_errors(relative, raw))
        elif Image is not None:
            errors.extend(_png_errors(relative, path, Image))
    return sorted(set(errors)), hashes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the exact frozen-sample seven-artifact output set."
    )
    parser.add_argument("--output", required=True, type=Path, help="Sample run directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        errors, hashes = check_artifacts(args.output)
    except Exception as error:
        print(
            f"artifact-check: ERROR: checker failed ({type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    if errors:
        for error in errors:
            print(f"artifact-check: ERROR: {error}", file=sys.stderr)
        return 1
    print("artifact-check: PASS")
    for relative in EXPECTED_FILES:
        print(f"SHA256 {hashes[relative]} {relative}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
