"""Black-box tests for the standalone frozen-sample artifact checker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from sichuan_weather import cli

from tests.oracle import SAMPLE_MANIFEST, SAMPLE_RECORDS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER = PROJECT_ROOT / "scripts" / "check_artifacts.py"


@pytest.fixture(scope="module")
def cli_sample_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("artifact-checker-baseline")
    output = root / "actual CLI sample output"
    exit_code = cli.main(
        [
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output),
        ]
    )
    assert exit_code == 0
    return output


def _copy_output(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination


def _run_checker(output: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return subprocess.run(
        [sys.executable, str(CHECKER), "--output", str(output)],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_checker_accepts_cli_sample_and_reports_all_hashes(
    cli_sample_output: Path,
    tmp_path: Path,
) -> None:
    result = _run_checker(cli_sample_output, tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines[0] == "artifact-check: PASS"
    assert len(lines) == 8
    reported: dict[str, str] = {}
    for line in lines[1:]:
        match = re.fullmatch(r"SHA256 ([0-9a-f]{64}) (.+)", line)
        assert match is not None
        reported[match.group(2)] = match.group(1)
    assert set(reported) == {
        "cleaned.csv",
        "figures/temperature-ranges.png",
        "figures/weather-frequency.png",
        "monthly_summary.csv",
        "quality.json",
        "summary.csv",
        "weather_frequency.csv",
    }
    assert reported == {
        relative: hashlib.sha256((cli_sample_output / relative).read_bytes()).hexdigest()
        for relative in reported
    }


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        ("missing", 'missing artifact: "monthly_summary.csv"'),
        ("extra", 'unexpected artifact: "unexpected.txt"'),
    ],
)
def test_checker_rejects_missing_and_extra_artifacts(
    cli_sample_output: Path,
    tmp_path: Path,
    mutation: str,
    diagnostic: str,
) -> None:
    output = _copy_output(cli_sample_output, tmp_path / "mutated output")
    if mutation == "missing":
        (output / "monthly_summary.csv").unlink()
    else:
        (output / "unexpected.txt").write_text("extra", encoding="utf-8")

    result = _run_checker(output, tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert diagnostic in result.stderr
    assert "Traceback" not in result.stderr


def test_checker_rejects_byte_tampered_csv(
    cli_sample_output: Path,
    tmp_path: Path,
) -> None:
    output = _copy_output(cli_sample_output, tmp_path / "tampered CSV output")
    path = output / "summary.csv"
    original = path.read_bytes()
    assert original.count(b"5.6667") == 1
    path.write_bytes(original.replace(b"5.6667", b"5.6668"))

    result = _run_checker(output, tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        "summary.csv bytes differ from the frozen sample oracle" in result.stderr
    )
    assert "Traceback" not in result.stderr


def test_checker_rejects_semantically_wrong_quality_report(
    cli_sample_output: Path,
    tmp_path: Path,
) -> None:
    output = _copy_output(cli_sample_output, tmp_path / "wrong quality output")
    path = output / "quality.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["counts"]["output_records"] = 11
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    result = _run_checker(output, tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "quality.json report differs from the frozen sample oracle" in result.stderr
    assert "quality.json output count does not match eligible records" in result.stderr
    assert "Traceback" not in result.stderr


def test_checker_rejects_structurally_valid_but_blank_png(
    cli_sample_output: Path,
    tmp_path: Path,
) -> None:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    output = _copy_output(cli_sample_output, tmp_path / "blank PNG output")
    metadata = PngInfo()
    metadata.add_text("Software", "sichuan-weather-pipeline")
    Image.new("RGBA", (1800, 1200), "white").save(
        output / "figures" / "weather-frequency.png",
        format="PNG",
        dpi=(150, 150),
        pnginfo=metadata,
    )

    result = _run_checker(output, tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "weather-frequency.png: image is uniformly blank" in result.stderr
    assert "Traceback" not in result.stderr
