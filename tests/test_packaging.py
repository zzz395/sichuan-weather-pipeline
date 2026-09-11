"""Archive-level packaging checks using the locked, offline build toolchain."""

from __future__ import annotations

import base64
import configparser
import csv
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import zipfile

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name


PROJECT_ROOT = Path(__file__).resolve().parents[1]
W3_ROOT = PROJECT_ROOT / ".w3"
PACKAGE_NAME = "sichuan-weather-pipeline"
PACKAGE_VERSION = "0.1.0"

REQUIRED_FIXED_FILES = {
    ".github/workflows/ci.yml",
    "README.md",
    "pyproject.toml",
    "MANIFEST.in",
    "data/sample/records.jsonl",
    "data/sample/manifest.json",
    "requirements/baseline.in",
    "requirements/windows-x64-py312/runtime.txt",
    "requirements/windows-x64-py312/dev.txt",
    "requirements/windows-x64-py313/runtime.txt",
    "requirements/windows-x64-py313/dev.txt",
    "scripts/prepare_wheelhouse.py",
    "scripts/check_artifacts.py",
    "scripts/offline_acceptance.py",
    "reproducibility/candidate-files.txt",
    "docs/reproducibility.md",
    "docs/data-contract.md",
    "docs/assets/temperature-ranges.png",
    "docs/assets/weather-frequency.png",
}


def test_ci_workflow_is_a_required_distribution_input() -> None:
    assert ".github/workflows/ci.yml" in REQUIRED_FIXED_FILES
GENERATED_SDIST_FILES = {
    "PKG-INFO",
    "setup.cfg",
    "src/sichuan_weather_pipeline.egg-info/SOURCES.txt",
}
STANDARD_DIST_INFO_FILES = {
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
    "RECORD",
}
FORBIDDEN_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".pyright",
    ".ruff_cache",
    ".tox",
    ".nox",
    "__pycache__",
    "build",
    "dist",
    "wheelhouse",
}


@dataclass(frozen=True)
class BuiltArchives:
    work_root: Path
    clean_source: Path
    sdist: Path
    extracted_source: Path
    wheel: Path
    selected_source_files: frozenset[str]


def _selected_source_files() -> frozenset[str]:
    relative_paths = set(REQUIRED_FIXED_FILES)
    relative_paths.update(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "src" / "sichuan_weather").glob("*.py")
        if path.is_file()
    )
    relative_paths.update(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "tests").glob("*.py")
        if path.is_file()
    )
    missing = sorted(
        relative for relative in REQUIRED_FIXED_FILES if not (PROJECT_ROOT / relative).is_file()
    )
    assert not missing, f"required packaging inputs are missing: {missing}"
    assert "tests/test_packaging.py" in relative_paths
    return frozenset(relative_paths)


def _copy_clean_source(destination: Path, relative_paths: frozenset[str]) -> None:
    for relative in sorted(relative_paths):
        source = PROJECT_ROOT / Path(relative)
        assert not source.is_symlink(), f"candidate source is a symlink: {relative}"
        target = destination / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _offline_build_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "PIP_FIND_LINKS",
        "PIP_CACHE_DIR",
    ):
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    return environment


def _run_build(arguments: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", *arguments],
        cwd=cwd,
        env=_offline_build_environment(),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _safe_extract_sdist(sdist: Path, destination: Path) -> Path:
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        assert members
        roots: set[str] = set()
        for member in members:
            pure = PurePosixPath(member.name)
            assert not pure.is_absolute()
            assert ".." not in pure.parts
            assert "\\" not in member.name
            assert not (member.issym() or member.islnk() or member.isdev())
            roots.add(pure.parts[0])
        assert roots == {"sichuan_weather_pipeline-0.1.0"}
        archive.extractall(destination, filter="data")
    extracted = destination / next(iter(roots))
    assert extracted.is_dir()
    return extracted


@pytest.fixture(scope="module")
def built_archives() -> BuiltArchives:
    W3_ROOT.mkdir(parents=True, exist_ok=True)
    packaging_root = W3_ROOT / "packaging-tests"
    packaging_root.mkdir(parents=True, exist_ok=True)
    work_root = Path(
        tempfile.mkdtemp(prefix="archive-build-", dir=packaging_root)
    )
    clean_source = work_root / "source copy with spaces"
    clean_source.mkdir()
    selected = _selected_source_files()
    _copy_clean_source(clean_source, selected)

    sdist_output = work_root / "sdist output"
    sdist_output.mkdir()
    sdist_build = _run_build(
        ["--sdist", "--outdir", str(sdist_output), str(clean_source)],
        cwd=work_root,
    )
    assert sdist_build.returncode == 0, (
        "locked offline sdist build failed\n"
        f"stdout:\n{sdist_build.stdout}\n"
        f"stderr:\n{sdist_build.stderr}"
    )
    sdists = list(sdist_output.glob("*.tar.gz"))
    assert [path.name for path in sdists] == [
        "sichuan_weather_pipeline-0.1.0.tar.gz"
    ]

    extracted_root = work_root / "extracted sdist"
    extracted_root.mkdir()
    extracted_source = _safe_extract_sdist(sdists[0], extracted_root)
    wheel_output = work_root / "wheel output"
    wheel_output.mkdir()
    wheel_build = _run_build(
        ["--wheel", "--outdir", str(wheel_output), str(extracted_source)],
        cwd=work_root,
    )
    assert wheel_build.returncode == 0, (
        "locked offline wheel-from-sdist build failed\n"
        f"stdout:\n{wheel_build.stdout}\n"
        f"stderr:\n{wheel_build.stderr}"
    )
    wheels = list(wheel_output.glob("*.whl"))
    assert [path.name for path in wheels] == [
        "sichuan_weather_pipeline-0.1.0-py3-none-any.whl"
    ]
    return BuiltArchives(
        work_root=work_root,
        clean_source=clean_source,
        sdist=sdists[0],
        extracted_source=extracted_source,
        wheel=wheels[0],
        selected_source_files=selected,
    )


def _sdist_file_map(sdist: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    with tarfile.open(sdist, "r:gz") as archive:
        file_members = [member for member in archive.getmembers() if member.isfile()]
        assert len({member.name for member in file_members}) == len(file_members)
        for member in file_members:
            pure = PurePosixPath(member.name)
            relative = PurePosixPath(*pure.parts[1:]).as_posix()
            stream = archive.extractfile(member)
            assert stream is not None
            result[relative] = stream.read()
    return result


def _wheel_file_map(wheel: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        for name in names:
            pure = PurePosixPath(name)
            assert not pure.is_absolute()
            assert ".." not in pure.parts
            assert "\\" not in name
        return {name: archive.read(name) for name in names if not name.endswith("/")}


def _assert_no_forbidden_archive_paths(paths: set[str]) -> None:
    for relative in paths:
        pure = PurePosixPath(relative)
        lowered_parts = {part.casefold() for part in pure.parts}
        assert not (lowered_parts & FORBIDDEN_DIRECTORY_NAMES)
        assert not any(part.casefold().startswith(".w3") for part in pure.parts)
        assert not any(part.casefold().startswith("pytest-cache-files-") for part in pure.parts)
        assert pure.suffix.casefold() not in {".pyc", ".pyo"}
        filename = pure.name.casefold()
        assert not filename.startswith(("license", "licence", "copying"))


def test_sdist_has_exact_safe_public_inventory(built_archives: BuiltArchives) -> None:
    files = _sdist_file_map(built_archives.sdist)
    expected = set(built_archives.selected_source_files) | GENERATED_SDIST_FILES

    assert set(files) == expected
    _assert_no_forbidden_archive_paths(set(files))
    assert {
        relative
        for relative in files
        if relative.startswith("src/sichuan_weather/") and relative.endswith(".py")
    } == {
        relative
        for relative in built_archives.selected_source_files
        if relative.startswith("src/sichuan_weather/")
    }
    assert {
        "README.md",
        "docs/reproducibility.md",
        "docs/data-contract.md",
        "docs/assets/temperature-ranges.png",
        "docs/assets/weather-frequency.png",
    }.issubset(files)


def test_wheel_contains_only_application_and_standard_metadata(
    built_archives: BuiltArchives,
) -> None:
    files = _wheel_file_map(built_archives.wheel)
    package_files = {
        f"sichuan_weather/{Path(relative).name}"
        for relative in built_archives.selected_source_files
        if relative.startswith("src/sichuan_weather/")
    }
    dist_info_roots = {
        PurePosixPath(relative).parts[0]
        for relative in files
        if PurePosixPath(relative).parts[0].endswith(".dist-info")
    }
    assert dist_info_roots == {"sichuan_weather_pipeline-0.1.0.dist-info"}
    dist_info = next(iter(dist_info_roots))
    expected = package_files | {
        f"{dist_info}/{filename}" for filename in STANDARD_DIST_INFO_FILES
    }

    assert set(files) == expected
    _assert_no_forbidden_archive_paths(set(files))
    assert not any(
        relative.startswith(("tests/", "data/", "scripts/", "requirements/"))
        for relative in files
    )

    record_path = f"{dist_info}/RECORD"
    record_rows = list(
        csv.reader(io.StringIO(files[record_path].decode("utf-8"), newline=""))
    )
    assert {row[0] for row in record_rows} == set(files)
    for relative, digest, size in record_rows:
        if relative == record_path:
            assert digest == "" and size == ""
            continue
        expected_digest = base64.urlsafe_b64encode(
            hashlib.sha256(files[relative]).digest()
        ).rstrip(b"=").decode("ascii")
        assert digest == f"sha256={expected_digest}"
        assert size == str(len(files[relative]))


def _metadata(raw: bytes) -> Any:
    return BytesParser(policy=default).parsebytes(raw)


def _assert_distribution_metadata(metadata: Any) -> None:
    assert canonicalize_name(metadata["Name"]) == canonicalize_name(PACKAGE_NAME)
    assert metadata["Version"] == PACKAGE_VERSION
    assert metadata["Summary"] == (
        "A reproducible, offline-capable Python pipeline for cleaning, "
        "validating, summarizing, and visualizing weather-forecast snapshot data."
    )
    assert metadata["Description-Content-Type"] == "text/markdown"
    metadata_description = metadata.get_payload(decode=True).decode("utf-8").replace(
        "\r\n", "\n"
    )
    readme_description = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert metadata_description == readme_description
    assert metadata.get("License") is None
    assert metadata.get("Author") is None
    assert metadata.get_all("Project-URL", []) == []
    assert SpecifierSet(metadata["Requires-Python"]) == SpecifierSet(">=3.12,<3.14")
    requirements = [
        Requirement(value) for value in metadata.get_all("Requires-Dist", [])
    ]
    runtime = [requirement for requirement in requirements if requirement.marker is None]
    assert len(runtime) == 1
    assert canonicalize_name(runtime[0].name) == "matplotlib"
    assert runtime[0].specifier == SpecifierSet(">=3.9,<4")


def test_sdist_and_wheel_metadata_and_console_entry_point(
    built_archives: BuiltArchives,
) -> None:
    sdist_files = _sdist_file_map(built_archives.sdist)
    wheel_files = _wheel_file_map(built_archives.wheel)
    wheel_metadata_path = next(
        relative for relative in wheel_files if relative.endswith(".dist-info/METADATA")
    )
    wheel_entry_points_path = next(
        relative
        for relative in wheel_files
        if relative.endswith(".dist-info/entry_points.txt")
    )

    _assert_distribution_metadata(_metadata(sdist_files["PKG-INFO"]))
    _assert_distribution_metadata(_metadata(wheel_files[wheel_metadata_path]))
    entry_points = configparser.ConfigParser(interpolation=None)
    entry_points.read_string(wheel_files[wheel_entry_points_path].decode("utf-8"))
    assert entry_points.sections() == ["console_scripts"]
    assert dict(entry_points["console_scripts"]) == {
        "weather-pipeline": "sichuan_weather.cli:main"
    }
    wheel_description_path = next(
        relative for relative in wheel_files if relative.endswith(".dist-info/WHEEL")
    )
    wheel_description = _metadata(wheel_files[wheel_description_path])
    assert wheel_description["Root-Is-Purelib"].casefold() == "true"
    assert wheel_description.get_all("Tag") == ["py3-none-any"]


def test_wheel_is_built_from_sdist_and_all_shipped_python_compiles_in_memory(
    built_archives: BuiltArchives,
) -> None:
    sdist_files = _sdist_file_map(built_archives.sdist)
    wheel_files = _wheel_file_map(built_archives.wheel)
    sdist_python = {
        relative: raw for relative, raw in sdist_files.items() if relative.endswith(".py")
    }
    wheel_python = {
        relative: raw for relative, raw in wheel_files.items() if relative.endswith(".py")
    }

    assert sdist_python
    assert wheel_python
    for relative, raw in sdist_python.items():
        compile(raw, f"<sdist>/{relative}", "exec", dont_inherit=True)
    for relative, raw in wheel_python.items():
        compile(raw, f"<wheel>/{relative}", "exec", dont_inherit=True)
        source_relative = f"src/{relative}"
        assert raw == sdist_python[source_relative]


def test_distribution_outputs_and_hashes_stay_under_w3(
    built_archives: BuiltArchives,
) -> None:
    assert built_archives.sdist.is_relative_to(W3_ROOT)
    assert built_archives.wheel.is_relative_to(W3_ROOT)
    assert built_archives.clean_source.is_relative_to(W3_ROOT)
    assert built_archives.extracted_source.is_relative_to(W3_ROOT)
    for archive in (built_archives.sdist, built_archives.wheel):
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        assert len(digest) == 64
        assert int(digest, 16) > 0
