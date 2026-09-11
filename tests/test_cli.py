from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

from sichuan_weather import cli
from sichuan_weather.cli import main

from tests.helpers import artifact_files, make_record, write_dataset
from tests.oracle import SAMPLE_MANIFEST, SAMPLE_RECORDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _installed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _run_module(
    *arguments: str, cwd: Path = PROJECT_ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sichuan_weather.cli", *arguments],
        cwd=cwd,
        env=_installed_environment(),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


def _run_module_help(
    *arguments: str, cwd: Path = PROJECT_ROOT
) -> subprocess.CompletedProcess[str]:
    return _run_module(*arguments, "--help", cwd=cwd)


def _console_entry_point() -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return Path(sysconfig.get_path("scripts")) / f"weather-pipeline{suffix}"


def _run_console(
    *arguments: str, cwd: Path = PROJECT_ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_console_entry_point()), *arguments],
        cwd=cwd,
        env=_installed_environment(),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )


@pytest.mark.parametrize(
    ("arguments", "expected_options"),
    [
        ((), ("validate", "run")),
        (("validate",), ("--input", "--manifest")),
        (("run",), ("--input", "--manifest", "--output")),
    ],
)
def test_module_help(
    tmp_path: Path,
    arguments: tuple[str, ...],
    expected_options: tuple[str, ...],
) -> None:
    process_cwd = tmp_path / "module help cwd"
    process_cwd.mkdir()
    result = _run_module_help(*arguments, cwd=process_cwd)

    assert result.returncode == 0, result.stderr
    for option in expected_options:
        assert option in result.stdout


def test_installed_console_entry_point_help(tmp_path: Path) -> None:
    process_cwd = tmp_path / "console help cwd"
    process_cwd.mkdir()

    result = _run_console("--help", cwd=process_cwd)

    assert _console_entry_point().is_file()
    assert _console_entry_point().resolve().is_relative_to(Path(sys.prefix).resolve())
    assert result.returncode == 0, result.stderr
    assert "validate" in result.stdout
    assert "run" in result.stdout


def test_missing_source_is_a_path_contract_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "validate",
            "--input",
            str(tmp_path / "missing-records.jsonl"),
            "--manifest",
            str(tmp_path / "missing-manifest.json"),
        ]
    )

    assert exit_code == 2
    assert "path error" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("input", "missing"),
        ("manifest", "missing"),
        ("input", "directory"),
        ("manifest", "directory"),
    ],
)
def test_each_source_missing_or_non_file_is_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    source: str,
    kind: str,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path / "valid sources",
        [make_record("12-30", "0~1℃")],
    )
    selected = tmp_path / f"{source} {kind}"
    if kind == "directory":
        selected.mkdir()
    if source == "input":
        input_path = selected
    else:
        manifest_path = selected

    exit_code = main(
        [
            "validate",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert "path error" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("source", ["input", "manifest"])
def test_source_read_failure_after_preflight_is_private_exit_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: str,
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path / "existing sources",
        [make_record("12-30", "0~1℃")],
    )
    output_path = tmp_path / "must not exist"
    failing_path = input_path if source == "input" else manifest_path
    original_read_bytes = Path.read_bytes
    secret = f"PRIVATE-{source}-READ-CONTENT"

    def fail_selected_read(path: Path) -> bytes:
        if path == failing_path:
            raise PermissionError(secret)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_selected_read)

    exit_code = main(
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
    assert "runtime" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not output_path.exists()


def test_validate_only_valid_writes_stdout_and_no_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = artifact_files(tmp_path)

    exit_code = main(
        [
            "validate",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    quality = json.loads(captured.out)
    assert quality["status"] == "valid"
    assert quality["analysis_generated"] is False
    assert quality["counts"]["output_records"] == 0
    assert artifact_files(tmp_path) == before == set()


def test_validate_only_invalid_writes_stdout_and_no_new_files(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    input_path, manifest_path = write_dataset(
        tmp_path / "invalid sources",
        [make_record("12-30", "2~N/A")],
    )
    before = artifact_files(tmp_path)

    exit_code = main(
        [
            "validate",
            "--input",
            str(input_path),
            "--manifest",
            str(manifest_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err == ""
    quality = json.loads(captured.out)
    assert quality["status"] == "invalid"
    assert quality["analysis_generated"] is False
    assert quality["counts"]["output_records"] == 0
    assert artifact_files(tmp_path) == before


def test_paths_with_spaces_work_in_a_shell_free_console_subprocess(
    tmp_path: Path,
) -> None:
    seed_input, seed_manifest = write_dataset(
        tmp_path / "seed",
        [make_record("12-30", "0~1℃")],
    )
    input_dir = tmp_path / "input records"
    manifest_dir = tmp_path / "manifest files"
    input_dir.mkdir()
    manifest_dir.mkdir()
    input_path = input_dir / "forecast records.jsonl"
    manifest_path = manifest_dir / "sample manifest.json"
    input_path.write_bytes(seed_input.read_bytes())
    manifest_path.write_bytes(seed_manifest.read_bytes())
    output_path = tmp_path / "output results" / "run one"
    process_cwd = tmp_path / "console process cwd"
    process_cwd.mkdir()

    result = _run_console(
        "run",
        "--input",
        str(input_path),
        "--manifest",
        str(manifest_path),
        "--output",
        str(output_path),
        cwd=process_cwd,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert artifact_files(output_path) == {
        "cleaned.csv",
        "figures/temperature-ranges.png",
        "figures/weather-frequency.png",
        "monthly_summary.csv",
        "quality.json",
        "summary.csv",
        "weather_frequency.csv",
    }


def test_unexpected_runtime_error_does_not_echo_secret_or_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "PRIVATE-RAW-RECORD-CONTENT"

    def fail_validation(input_path: Path, manifest_path: Path) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(cli, "validate_dataset", fail_validation)

    exit_code = main(
        [
            "validate",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.out == ""
    assert "unexpected runtime error" in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
