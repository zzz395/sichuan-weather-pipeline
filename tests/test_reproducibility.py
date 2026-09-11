"""Frozen-input, order-independence, and byte-reproducibility regressions."""

from collections import Counter
import hashlib
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest

from sichuan_weather.analysis import (
    build_monthly_summary,
    build_overall_summary,
    build_weather_frequency,
)
from sichuan_weather.pipeline import REQUIRED_SUCCESS_FILES
from sichuan_weather.validation import validate_dataset

from tests.helpers import business_rows
from tests.oracle import (
    SAMPLE_MANIFEST,
    SAMPLE_MANIFEST_SHA256,
    SAMPLE_RECORDS,
    SAMPLE_RECORDS_SHA256,
)


ORIGINAL_SOURCE_LINES = {
    ("sample-a", "Chengdu", "2026-12-30"): (1, 9),
    ("sample-a", "Chengdu", "2026-12-31"): (2,),
    ("sample-a", "Chengdu", "2027-01-01"): (3,),
    ("sample-a", "Chengdu", "2027-01-02"): (4,),
    ("sample-a", "Mianyang", "2026-12-30"): (5, 14),
    ("sample-a", "Mianyang", "2026-12-31"): (6,),
    ("sample-a", "Mianyang", "2027-01-01"): (7,),
    ("sample-a", "Mianyang", "2027-01-02"): (8,),
    ("sample-b", "Chengdu", "2026-12-31"): (10,),
    ("sample-b", "Chengdu", "2027-01-01"): (11,),
    ("sample-b", "Mianyang", "2027-01-01"): (12,),
    ("sample-b", "Mianyang", "2027-01-02"): (13,),
}


def _issue_semantics(result: object) -> Counter[tuple[object, ...]]:
    return Counter(
        (
            issue.rule_id,
            issue.severity,
            issue.field,
            issue.logical_key,
            issue.message,
        )
        for issue in result.issues
    )


def _artifact_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _run_pipeline_process(
    *,
    output: Path,
    mpl_config: Path,
    process_cwd: Path,
    hash_seed: str,
) -> subprocess.CompletedProcess[str]:
    process_cwd.mkdir(parents=True)
    mpl_config.mkdir(parents=True)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONHASHSEED"] = hash_seed
    environment["MPLCONFIGDIR"] = str(mpl_config)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sichuan_weather.cli",
            "run",
            "--input",
            str(SAMPLE_RECORDS),
            "--manifest",
            str(SAMPLE_MANIFEST),
            "--output",
            str(output),
        ],
        cwd=process_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_frozen_sample_files_are_byte_stable() -> None:
    assert hashlib.sha256(SAMPLE_RECORDS.read_bytes()).hexdigest() == (
        SAMPLE_RECORDS_SHA256
    )
    assert hashlib.sha256(SAMPLE_MANIFEST.read_bytes()).hexdigest() == (
        SAMPLE_MANIFEST_SHA256
    )


@pytest.mark.parametrize("permutation", ["reversed", "seeded-shuffle"])
def test_input_reordering_preserves_business_semantics_and_remaps_source_lines(
    tmp_path: Path,
    permutation: str,
) -> None:
    original = validate_dataset(SAMPLE_RECORDS, SAMPLE_MANIFEST)
    assert original.is_valid and original.manifest is not None
    original_lines = SAMPLE_RECORDS.read_text(encoding="utf-8").splitlines()
    indexed_lines = list(enumerate(original_lines, start=1))
    if permutation == "reversed":
        indexed_lines.reverse()
    else:
        random.Random(20260911).shuffle(indexed_lines)

    reordered_path = tmp_path / f"{permutation}.jsonl"
    reordered_bytes = ("\n".join(line for _, line in indexed_lines) + "\n").encode(
        "utf-8"
    )
    reordered_path.write_bytes(reordered_bytes)
    old_to_new = {
        original_line: new_line
        for new_line, (original_line, _) in enumerate(indexed_lines, start=1)
    }
    reordered = validate_dataset(reordered_path, SAMPLE_MANIFEST)

    assert reordered.is_valid and reordered.manifest is not None
    assert reordered.input_sha256 == hashlib.sha256(reordered_bytes).hexdigest()
    assert reordered.input_sha256 != original.input_sha256
    assert reordered.manifest_sha256 == original.manifest_sha256
    assert reordered.counts == original.counts
    assert reordered.missing_month_groups == original.missing_month_groups
    assert business_rows(reordered) == business_rows(original)
    assert build_overall_summary(
        reordered.manifest, reordered.canonical_records
    ) == build_overall_summary(original.manifest, original.canonical_records)
    assert build_monthly_summary(
        reordered.manifest, reordered.canonical_records
    ) == build_monthly_summary(original.manifest, original.canonical_records)
    assert build_weather_frequency(
        reordered.manifest, reordered.canonical_records
    ) == build_weather_frequency(original.manifest, original.canonical_records)
    assert _issue_semantics(reordered) == _issue_semantics(original)
    assert list(reordered.issues) == sorted(reordered.issues, key=lambda issue: issue.sort_key())

    actual_source_lines = {
        (
            record.snapshot_id,
            record.location,
            record.valid_date.isoformat(),
        ): record.source_lines
        for record in reordered.canonical_records
    }
    expected_source_lines = {
        key: tuple(sorted(old_to_new[line] for line in old_lines))
        for key, old_lines in ORIGINAL_SOURCE_LINES.items()
    }
    assert actual_source_lines == expected_source_lines


def test_two_independent_processes_produce_identical_seven_artifacts(
    tmp_path: Path,
) -> None:
    first_output = tmp_path / "first process output"
    second_output = tmp_path / "second process output"
    first_cache = tmp_path / "matplotlib cache one"
    second_cache = tmp_path / "matplotlib cache two"

    first = _run_pipeline_process(
        output=first_output,
        mpl_config=first_cache,
        process_cwd=tmp_path / "first process cwd",
        hash_seed="11",
    )
    second = _run_pipeline_process(
        output=second_output,
        mpl_config=second_cache,
        process_cwd=tmp_path / "second process cwd",
        hash_seed="97",
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == "" and second.stdout == ""
    first_hashes = _artifact_hashes(first_output)
    second_hashes = _artifact_hashes(second_output)
    assert set(first_hashes) == set(REQUIRED_SUCCESS_FILES)
    assert set(second_hashes) == set(REQUIRED_SUCCESS_FILES)
    assert first_hashes == second_hashes
    assert first_cache != second_cache
