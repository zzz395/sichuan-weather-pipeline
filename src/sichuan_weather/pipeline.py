"""Pipeline orchestration and atomic artifact delivery."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from .analysis import (
    build_monthly_summary,
    build_overall_summary,
    build_weather_frequency,
)
from .models import ValidationResult
from .serialization import (
    quality_document,
    write_cleaned_csv,
    write_monthly_summary_csv,
    write_quality_json,
    write_summary_csv,
    write_weather_frequency_csv,
)
from .visualization import render_figures


REQUIRED_SUCCESS_FILES = frozenset(
    {
        "cleaned.csv",
        "quality.json",
        "summary.csv",
        "monthly_summary.csv",
        "weather_frequency.csv",
        "figures/temperature-ranges.png",
        "figures/weather-frequency.png",
    }
)


class PathContractError(ValueError):
    """A command path violates the public CLI contract."""


class ArtifactDeliveryError(RuntimeError):
    """Artifact generation or atomic delivery failed."""

    def __init__(self, message: str, temporary_path: Path | None = None) -> None:
        super().__init__(message)
        self.temporary_path = temporary_path


def require_source_file(path: Path, label: str) -> None:
    """Reject missing and non-file source paths as CLI contract errors."""
    try:
        exists = path.exists()
        is_file = path.is_file()
    except OSError as error:
        raise ArtifactDeliveryError(f"cannot inspect {label} path") from error
    if not exists:
        raise PathContractError(f"{label} path does not exist")
    if not is_file:
        raise PathContractError(f"{label} path is not a file")


def require_available_output(path: Path) -> None:
    """Require a new path or an existing empty directory."""
    try:
        if path.exists():
            if not path.is_dir():
                raise PathContractError("output path is not a directory")
            if next(path.iterdir(), None) is not None:
                raise PathContractError("output directory is not empty")
        ancestor = path.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if ancestor.exists() and not ancestor.is_dir():
            raise PathContractError("output parent path crosses a non-directory")
    except PathContractError:
        raise
    except OSError as error:
        raise ArtifactDeliveryError("cannot inspect output path") from error


def deliver_run(result: ValidationResult, output_path: Path) -> None:
    """Create the appropriate run result privately, then atomically expose it."""
    require_available_output(output_path)
    stage: Path | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(
            tempfile.mkdtemp(
                prefix=f".{output_path.name}.staging-",
                dir=output_path.parent,
            )
        )
        if not result.is_valid:
            document = quality_document(
                result,
                analysis_generated=False,
                output_records=0,
            )
            write_quality_json(stage / "quality.json", document)
            _verify_files(stage, frozenset({"quality.json"}))
        else:
            _build_success_artifacts(stage, result)
            _verify_files(stage, REQUIRED_SUCCESS_FILES)
        _finalize(stage, output_path)
        stage = None
    except PathContractError:
        if stage is not None and stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError as cleanup_error:
                raise ArtifactDeliveryError(
                    "pipeline staging cleanup failed",
                    stage,
                ) from cleanup_error
        raise
    except Exception as error:
        cleanup_failed = False
        if stage is not None and stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError:
                cleanup_failed = True
        retained = stage if cleanup_failed else None
        raise ArtifactDeliveryError("pipeline artifact delivery failed", retained) from error


def _build_success_artifacts(stage: Path, result: ValidationResult) -> None:
    manifest = result.manifest
    if manifest is None:
        raise ArtifactDeliveryError("validated result has no manifest")

    records = result.canonical_records
    overall_rows = build_overall_summary(manifest, records)
    monthly_rows = build_monthly_summary(manifest, records)
    weather_rows = build_weather_frequency(manifest, records)

    write_cleaned_csv(stage / "cleaned.csv", records)
    write_summary_csv(stage / "summary.csv", overall_rows)
    write_monthly_summary_csv(stage / "monthly_summary.csv", monthly_rows)
    write_weather_frequency_csv(stage / "weather_frequency.csv", weather_rows)

    figures_dir = stage / "figures"
    figures_dir.mkdir()
    render_figures(manifest, records, figures_dir)

    document = quality_document(
        result,
        analysis_generated=True,
        output_records=len(records),
    )
    write_quality_json(stage / "quality.json", document)


def _verify_files(root: Path, expected: frozenset[str]) -> None:
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ArtifactDeliveryError("generated artifact set is incomplete")
    for relative in expected:
        path = root / Path(relative)
        if path.stat().st_size == 0:
            raise ArtifactDeliveryError("generated artifact is empty")
    for relative in (
        "figures/temperature-ranges.png",
        "figures/weather-frequency.png",
    ):
        if relative in expected:
            with (root / Path(relative)).open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise ArtifactDeliveryError("generated figure is not a PNG file")


def _finalize(stage: Path, target: Path) -> None:
    require_available_output(target)
    if target.exists():
        target.rmdir()
    os.replace(stage, target)
