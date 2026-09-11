"""CLI-to-artifact delivery integration regressions."""

import json
import os
from pathlib import Path

import pytest

from sichuan_weather import cli, pipeline
from sichuan_weather.pipeline import REQUIRED_SUCCESS_FILES

from tests.helpers import artifact_files, csv_rows, make_record, write_dataset
from tests.oracle import PNG_SIGNATURE, SAMPLE_MANIFEST, SAMPLE_RECORDS


def _minimal_render_figures(
    manifest: object,
    records: object,
    figures_dir: str | Path,
) -> tuple[Path, Path]:
    destination = Path(figures_dir)
    temperature = destination / "temperature-ranges.png"
    weather = destination / "weather-frequency.png"
    temperature.write_bytes(PNG_SIGNATURE + b"temperature")
    weather.write_bytes(PNG_SIGNATURE + b"weather")
    return temperature, weather


def _staging_paths(output_path: Path) -> list[Path]:
    return list(output_path.parent.glob(f".{output_path.name}.staging-*"))


def _target_snapshot(path: Path) -> dict[str, bytes]:
    if path.is_file():
        return {".": path.read_bytes()}
    if not path.exists():
        return {}
    return {
        child.relative_to(path).as_posix(): child.read_bytes()
        for child in path.rglob("*")
        if child.is_file()
    }


def test_cli_success_and_exact_artifact_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    validate_exit = cli.main(
        [
            "validate",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
        ]
    )
    validate_output = capsys.readouterr()

    assert validate_exit == 0
    quality = json.loads(validate_output.out)
    assert quality["status"] == "valid"
    assert quality["analysis_generated"] is False
    assert quality["counts"]["output_records"] == 0

    output_path = tmp_path / "output"
    run_exit = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    run_output = capsys.readouterr()

    assert run_exit == 0, run_output.err
    assert artifact_files(output_path) == set(REQUIRED_SUCCESS_FILES)
    assert len(csv_rows(output_path / "cleaned.csv")) == 12
    assert len(csv_rows(output_path / "summary.csv")) == 4
    assert len(csv_rows(output_path / "monthly_summary.csv")) == 8
    assert len(csv_rows(output_path / "weather_frequency.csv")) == 9
    run_quality = json.loads((output_path / "quality.json").read_text("utf-8"))
    assert run_quality["status"] == "valid"
    assert run_quality["analysis_generated"] is True
    assert run_quality["counts"]["output_records"] == 12
    for relative_path in (
        "figures/temperature-ranges.png",
        "figures/weather-frequency.png",
    ):
        with (output_path / relative_path).open("rb") as stream:
            assert stream.read(8) == PNG_SIGNATURE


def test_validation_failure_delivers_only_quality_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path / "input",
        [make_record("12-30", "2~N/A")],
    )
    output_path = tmp_path / "failed-output"

    validate_exit = cli.main(
        [
            "validate",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
        ]
    )
    validate_output = capsys.readouterr()

    assert validate_exit == 1
    validate_quality = json.loads(validate_output.out)
    assert validate_quality["status"] == "invalid"
    assert validate_quality["analysis_generated"] is False
    assert validate_quality["counts"]["output_records"] == 0

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 1
    assert artifact_files(output_path) == {"quality.json"}
    quality = json.loads((output_path / "quality.json").read_text("utf-8"))
    assert quality["status"] == "invalid"
    assert quality["analysis_generated"] is False
    assert quality["counts"]["output_records"] == 0


def test_nonempty_output_is_refused_without_overwrite(tmp_path: Path) -> None:
    output_path = tmp_path / "occupied"
    output_path.mkdir()
    marker = output_path / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 2
    assert marker.read_text(encoding="utf-8") == "user data"
    assert artifact_files(output_path) == {"keep.txt"}


def test_runtime_io_error_uses_exit_three(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "PRIVATE-W2-RUNTIME-FAULT"

    def fail_validation(input_path: Path, manifest_path: Path) -> object:
        raise OSError(secret)

    monkeypatch.setattr(cli, "validate_dataset", fail_validation)

    exit_code = cli.main(
        [
            "validate",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
        ]
    )

    assert exit_code == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "runtime I/O error" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_existing_empty_output_directory_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "existing empty output"
    output_path.mkdir()
    monkeypatch.setattr(pipeline, "render_figures", _minimal_render_figures)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert artifact_files(output_path) == set(REQUIRED_SUCCESS_FILES)


@pytest.mark.parametrize("occupied_kind", ["file", "nonempty", "prior-quality"])
def test_occupied_output_targets_are_refused_unchanged(
    tmp_path: Path,
    occupied_kind: str,
) -> None:
    output_path = tmp_path / "occupied output"
    if occupied_kind == "file":
        output_path.write_bytes(b"USER-FILE-CONTENT")
    else:
        output_path.mkdir()
        marker_name = "quality.json" if occupied_kind == "prior-quality" else "keep.txt"
        (output_path / marker_name).write_bytes(
            f"USER-{occupied_kind}-CONTENT".encode("ascii")
        )
    before = _target_snapshot(output_path)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 2
    assert _target_snapshot(output_path) == before
    assert _staging_paths(output_path) == []


def test_output_parent_creation_failure_is_private_exit_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "blocked parent" / "output"
    blocked_parent = output_path.parent
    original_mkdir = Path.mkdir
    secret = "PRIVATE-OUTPUT-PARENT-CREATE"

    def fail_selected_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == blocked_parent:
            raise PermissionError(secret)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_selected_mkdir)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert "runtime error" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not blocked_parent.exists()


def test_staging_creation_failure_is_private_exit_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "output"
    secret = "PRIVATE-STAGING-CREATE"

    def fail_mkdtemp(*args: object, **kwargs: object) -> str:
        raise PermissionError(secret)

    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", fail_mkdtemp)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert "runtime error" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()
    assert _staging_paths(output_path) == []


@pytest.mark.parametrize(
    "writer_name",
    [
        "write_cleaned_csv",
        "write_summary_csv",
        "write_monthly_summary_csv",
        "write_weather_frequency_csv",
        "write_quality_json",
    ],
)
def test_each_artifact_writer_failure_delivers_no_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    writer_name: str,
) -> None:
    output_path = tmp_path / f"failed {writer_name}"
    secret = f"PRIVATE-{writer_name}-CONTENT"

    def fail_writer(*args: object, **kwargs: object) -> None:
        raise OSError(secret)

    monkeypatch.setattr(pipeline, "render_figures", _minimal_render_figures)
    monkeypatch.setattr(pipeline, writer_name, fail_writer)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert "runtime error" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()
    assert _staging_paths(output_path) == []


def test_invalid_quality_writer_failure_delivers_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path / "invalid input",
        [make_record("12-30", "2~N/A")],
    )
    output_path = tmp_path / "invalid quality write failure"
    secret = "PRIVATE-INVALID-QUALITY"

    def fail_quality_writer(*args: object, **kwargs: object) -> None:
        raise OSError(secret)

    monkeypatch.setattr(pipeline, "write_quality_json", fail_quality_writer)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()
    assert _staging_paths(output_path) == []


@pytest.mark.parametrize("render_failure", ["first", "second"])
def test_each_render_failure_delivers_no_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    render_failure: str,
) -> None:
    output_path = tmp_path / f"{render_failure} render failure"
    secret = f"PRIVATE-{render_failure}-FIGURE-CONTENT"

    def fail_render(
        manifest: object,
        records: object,
        figures_dir: str | Path,
    ) -> tuple[Path, Path]:
        destination = Path(figures_dir)
        if render_failure == "second":
            (destination / "temperature-ranges.png").write_bytes(
                PNG_SIGNATURE + b"partial-first-figure"
            )
        raise OSError(secret)

    monkeypatch.setattr(pipeline, "render_figures", fail_render)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()
    assert _staging_paths(output_path) == []


def test_integrity_failure_delivers_no_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "integrity failure"

    def omit_quality(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(pipeline, "render_figures", _minimal_render_figures)
    monkeypatch.setattr(pipeline, "write_quality_json", omit_quality)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert "runtime error" in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()
    assert _staging_paths(output_path) == []


def test_finalize_replace_failure_delivers_no_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "replace failure"
    secret = "PRIVATE-REPLACE-FAILURE"

    def fail_replace(source: object, target: object) -> None:
        raise OSError(secret)

    monkeypatch.setattr(pipeline, "render_figures", _minimal_render_figures)
    monkeypatch.setattr(pipeline.os, "replace", fail_replace)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()
    assert _staging_paths(output_path) == []


def test_final_output_race_is_exit_two_and_preserves_racer_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "raced output"
    marker_content = b"RACER-OWNED-CONTENT"
    original_finalize = pipeline._finalize

    def occupy_then_finalize(stage: Path, target: Path) -> None:
        target.mkdir()
        (target / "racer.txt").write_bytes(marker_content)
        original_finalize(stage, target)

    monkeypatch.setattr(pipeline, "render_figures", _minimal_render_figures)
    monkeypatch.setattr(pipeline, "_finalize", occupy_then_finalize)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert "path error" in captured.err
    assert "Traceback" not in captured.err
    assert _target_snapshot(output_path) == {"racer.txt": marker_content}
    assert _staging_paths(output_path) == []


def test_cleanup_failure_reports_only_retained_staging_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "cleanup failure"
    operation_secret = "PRIVATE-WRITER-DATA"
    cleanup_secret = "PRIVATE-CLEANUP-DATA"

    def fail_writer(*args: object, **kwargs: object) -> None:
        raise OSError(operation_secret)

    def fail_cleanup(path: object) -> None:
        raise OSError(cleanup_secret)

    monkeypatch.setattr(pipeline, "write_cleaned_csv", fail_writer)
    monkeypatch.setattr(pipeline.shutil, "rmtree", fail_cleanup)

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()
    retained = _staging_paths(output_path)

    assert exit_code == 3
    assert captured.out == ""
    assert "temporary artifacts could not be removed" in captured.err
    assert len(retained) == 1
    assert str(retained[0]) in captured.err
    assert operation_secret not in captured.err
    assert cleanup_secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()


def test_success_artifacts_exclude_runtime_paths_identity_and_timestamps(
    tmp_path: Path,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path / "PRIVATE SOURCE DIRECTORY",
        [make_record("12-30", "0~1℃")],
    )
    output_path = tmp_path / "PRIVATE OUTPUT DIRECTORY"

    exit_code = cli.main(
        [
            "run",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    forbidden = (
        str(input_path).encode("utf-8"),
        str(manifest_path).encode("utf-8"),
        str(output_path).encode("utf-8"),
        os.environ.get("USERNAME", "Administrator").encode("utf-8"),
        b"generated_at",
        b"created_at",
        b"timestamp",
    )
    for path in output_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert all(value not in content for value in forbidden)
