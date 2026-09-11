"""Formal, fail-closed W3 offline acceptance for one Windows Python target.

This program never changes network state.  It is intended to be launched only
after the operator has manually disconnected the acceptance machine.  A small
set of outbound probes is performed before any environment is created; if any
probe succeeds, formal acceptance is refused.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import subprocess
import sys
import tarfile
from typing import Any, Callable, Mapping, Sequence
import zipfile


SCHEMA_VERSION = 1
TARGET_RE = re.compile(r"windows-x64-py3(?P<minor>12|13)\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
REQUIREMENT_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"==(?P<version>[^\s;\\]+)"
    r"(?P<rest>(?:\s+--hash=sha256:[0-9a-f]{64})+)\Z"
)
HASH_OPTION_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s|\Z)")

CONNECTIVITY_PROBES = (
    ("1.1.1.1", 443),
    ("pypi.org", 443),
    ("files.pythonhosted.org", 443),
    ("github.com", 443),
)

SUCCESS_ARTIFACTS = (
    "cleaned.csv",
    "figures/temperature-ranges.png",
    "figures/weather-frequency.png",
    "monthly_summary.csv",
    "quality.json",
    "summary.csv",
    "weather_frequency.csv",
)

REQUIRED_SOURCE_FILES = (
    ".github/workflows/ci.yml",
    "MANIFEST.in",
    "data/sample/manifest.json",
    "data/sample/records.jsonl",
    "docs/reproducibility.md",
    "pyproject.toml",
    "reproducibility/candidate-files.txt",
    "scripts/check_artifacts.py",
    "scripts/offline_acceptance.py",
    "scripts/prepare_wheelhouse.py",
)

PROHIBITED_SOURCE_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".w3",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "wheelhouse",
    }
)

POISON_ENVIRONMENT_PREFIXES = (
    "PIP_",
    "PYTHONPATH",
    "PYTHONHOME",
    "UV_",
)
POISON_ENVIRONMENT_NAMES = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class AcceptanceFailure(RuntimeError):
    """One formal acceptance phase failed or could not be run."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase


class DuplicateJsonKey(ValueError):
    """Strict JSON rejected a duplicate object key."""


@dataclass(frozen=True, slots=True)
class LockEntry:
    name: str
    version: str
    hashes: tuple[str, ...]

    @property
    def requirement_line(self) -> str:
        hashes = " ".join(f"--hash=sha256:{digest}" for digest in self.hashes)
        return f"{self.name}=={self.version} {hashes}"


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    sha256: str


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-standard JSON constant: {value}")


def _load_json(path: Path, phase: str) -> Any:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AcceptanceFailure(phase, "cannot read JSON input") from error
    if data.startswith(b"\xef\xbb\xbf"):
        raise AcceptanceFailure(phase, "JSON input contains a UTF-8 BOM")
    try:
        text = data.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey, ValueError) as error:
        raise AcceptanceFailure(phase, "JSON input is not strict UTF-8 JSON") from error


def _require_exact_keys(
    value: object,
    expected: set[str],
    phase: str,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise AcceptanceFailure(phase, f"{label} has an invalid object shape")
    return value


def _relative_posix(value: object, phase: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AcceptanceFailure(phase, f"{label} must be a relative POSIX path")
    if "\\" in value or "\x00" in value:
        raise AcceptanceFailure(phase, f"{label} must be a relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part or part.endswith((" ", ".")) for part in path.parts)
    ):
        raise AcceptanceFailure(phase, f"{label} must be a canonical relative path")
    return value


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _lexical_absolute(value: str | os.PathLike[str]) -> Path:
    """Return an absolute path without resolving away a link component."""

    return Path(value).absolute()


def _reject_link_chain(path: Path, phase: str, label: str) -> None:
    """Reject final or ancestor links before any call to ``resolve``."""

    try:
        for candidate in (path, *path.parents):
            if _is_link(candidate):
                raise AcceptanceFailure(
                    phase,
                    f"{label} must not use a symbolic link or junction",
                )
    except OSError as error:
        raise AcceptanceFailure(phase, f"cannot inspect {label}") from error


def _existing_path(
    value: str | os.PathLike[str],
    *,
    phase: str,
    label: str,
    kind: str,
) -> Path:
    path = _lexical_absolute(value)
    _reject_link_chain(path, phase, label)
    if kind == "directory":
        valid = path.is_dir()
    elif kind == "file":
        valid = path.is_file()
    else:  # pragma: no cover - internal programming guard
        raise ValueError(f"unsupported path kind: {kind}")
    if not valid:
        raise AcceptanceFailure(phase, f"{label} must be a real {kind}")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise AcceptanceFailure(phase, f"cannot resolve {label}") from error


def _new_path(
    value: str | os.PathLike[str],
    *,
    phase: str,
    label: str,
) -> Path:
    path = _lexical_absolute(value)
    _reject_link_chain(path, phase, label)
    if os.path.lexists(path):
        raise AcceptanceFailure(phase, f"{label} must not already exist")
    return path


def _collect_files(root: Path, phase: str) -> set[str]:
    if not root.is_dir() or _is_link(root):
        raise AcceptanceFailure(phase, "root must be a real directory")
    files: set[str] = set()
    try:
        def raise_walk_error(error: OSError) -> None:
            raise error

        for current, directory_names, file_names in os.walk(
            root,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            current_path = Path(current)
            for name in directory_names:
                child = current_path / name
                if _is_link(child):
                    raise AcceptanceFailure(phase, "symbolic links and junctions are forbidden")
            for name in file_names:
                child = current_path / name
                if _is_link(child) or not child.is_file():
                    raise AcceptanceFailure(phase, "non-regular source file is forbidden")
                files.add(child.relative_to(root).as_posix())
    except OSError as error:
        raise AcceptanceFailure(phase, "cannot enumerate file inventory") from error
    return files


def parse_hashed_lock(path: Path, *, phase: str = "lock-validation") -> dict[str, LockEntry]:
    """Parse the deliberately narrow, target-specific pip-tools lock format."""

    try:
        data = path.read_bytes()
    except OSError as error:
        raise AcceptanceFailure(phase, "cannot read dependency lock") from error
    if not data or data.startswith(b"\xef\xbb\xbf"):
        raise AcceptanceFailure(phase, "dependency lock is empty or contains a BOM")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AcceptanceFailure(phase, "dependency lock is not UTF-8") from error

    logical_lines: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        piece = stripped[:-1].rstrip() if continued else stripped
        pending = f"{pending} {piece}".strip()
        if not continued:
            logical_lines.append(" ".join(pending.split()))
            pending = ""
    if pending:
        raise AcceptanceFailure(phase, "dependency lock has an unfinished continuation")
    if not logical_lines:
        raise AcceptanceFailure(phase, "dependency lock has no requirements")

    entries: dict[str, LockEntry] = {}
    forbidden_fragments = (
        " --index-url",
        " --extra-index-url",
        " --trusted-host",
        " --find-links",
        " @ ",
        "file:",
        "http:",
        "https:",
    )
    for logical in logical_lines:
        lowered = f" {logical.lower()}"
        if logical.startswith(("-", ".", "/", "\\")) or any(
            fragment in lowered for fragment in forbidden_fragments
        ):
            raise AcceptanceFailure(phase, "dependency lock contains a forbidden directive")
        match = REQUIREMENT_RE.fullmatch(logical)
        if match is None:
            raise AcceptanceFailure(
                phase,
                "every dependency must be exactly pinned with SHA-256 hashes",
            )
        name = _canonical_name(match.group("name"))
        hashes = tuple(sorted(set(HASH_OPTION_RE.findall(match.group("rest")))))
        if not hashes or name in entries:
            raise AcceptanceFailure(phase, "dependency lock contains a duplicate or unhashed entry")
        entries[name] = LockEntry(name, match.group("version"), hashes)
    return entries


def validate_lock_pair(
    runtime_path: Path,
    dev_path: Path,
) -> tuple[dict[str, LockEntry], dict[str, LockEntry]]:
    runtime = parse_hashed_lock(runtime_path)
    dev = parse_hashed_lock(dev_path)
    for name, entry in runtime.items():
        dev_entry = dev.get(name)
        if dev_entry is None or dev_entry.version != entry.version:
            raise AcceptanceFailure(
                "lock-validation",
                "development lock is not an exact runtime superset",
            )
    if "matplotlib" not in runtime:
        raise AcceptanceFailure("lock-validation", "runtime lock omits matplotlib")
    for required in ("build", "pillow", "pip", "pip-tools", "pytest", "setuptools", "wheel"):
        if required not in dev:
            raise AcceptanceFailure(
                "lock-validation",
                "development lock omits a required reproducibility package",
            )
    return runtime, dev


def load_source_manifest(source: Path, manifest_path: Path) -> tuple[SourceFile, ...]:
    phase = "source-manifest"
    source_resolved = source.resolve()
    manifest_resolved = manifest_path.resolve()
    try:
        manifest_resolved.relative_to(source_resolved)
    except ValueError:
        pass
    else:
        raise AcceptanceFailure(phase, "source manifest must be stored outside the candidate")

    document = _require_exact_keys(
        _load_json(manifest_path, phase),
        {"schema_version", "files"},
        phase,
        "source manifest",
    )
    if document["schema_version"] != SCHEMA_VERSION or type(document["schema_version"]) is not int:
        raise AcceptanceFailure(phase, "source manifest schema version is unsupported")
    raw_files = document["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise AcceptanceFailure(phase, "source manifest files must be a nonempty array")

    files: list[SourceFile] = []
    seen_casefolded: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        item = _require_exact_keys(
            raw_file,
            {"path", "sha256"},
            phase,
            f"source manifest files[{index}]",
        )
        relative = _relative_posix(item["path"], phase, "source file path")
        digest = item["sha256"]
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise AcceptanceFailure(phase, "source file SHA-256 is invalid")
        folded = relative.casefold()
        if folded in seen_casefolded:
            raise AcceptanceFailure(phase, "source manifest contains duplicate Windows paths")
        seen_casefolded.add(folded)
        parts = {part.casefold() for part in PurePosixPath(relative).parts}
        if parts & PROHIBITED_SOURCE_PARTS or relative.lower().endswith((".pyc", ".pyo")):
            raise AcceptanceFailure(phase, "source manifest includes a forbidden generated path")
        files.append(SourceFile(relative, digest))

    if [item.path for item in files] != sorted(item.path for item in files):
        raise AcceptanceFailure(phase, "source manifest file inventory is not sorted")
    expected = {item.path for item in files}
    missing_required = set(REQUIRED_SOURCE_FILES) - expected
    if missing_required:
        raise AcceptanceFailure(phase, "source manifest omits formal-acceptance files")
    verify_source(source, tuple(files))
    return tuple(files)


def verify_source(source: Path, files: tuple[SourceFile, ...]) -> None:
    phase = "source-manifest"
    actual = _collect_files(source, phase)
    expected = {item.path for item in files}
    if actual != expected:
        raise AcceptanceFailure(phase, "candidate source inventory differs from its manifest")
    for item in files:
        path = source / Path(item.path)
        if _sha256(path) != item.sha256:
            raise AcceptanceFailure(phase, "candidate source hash differs from its manifest")


def copy_clean_source(
    source: Path,
    destination: Path,
    files: tuple[SourceFile, ...],
) -> None:
    phase = "source-copy"
    if destination.exists():
        raise AcceptanceFailure(phase, "clean source destination already exists")
    verify_source(source, files)
    try:
        destination.mkdir(parents=True)
        for item in files:
            target = destination / Path(item.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / Path(item.path), target)
    except OSError as error:
        raise AcceptanceFailure(phase, "cannot create the clean source copy") from error
    verify_source(source, files)
    verify_source(destination, files)


def _parse_candidate_file_list(path: Path, source: Path) -> tuple[str, ...]:
    phase = "source-snapshot"
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AcceptanceFailure(phase, "cannot read candidate file allowlist") from error
    if not data or data.startswith(b"\xef\xbb\xbf"):
        raise AcceptanceFailure(phase, "candidate file allowlist is empty or contains a BOM")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AcceptanceFailure(phase, "candidate file allowlist is not UTF-8") from error

    raw_lines = text.splitlines()
    if not raw_lines or any(not line for line in raw_lines):
        raise AcceptanceFailure(phase, "candidate file allowlist contains an empty line")
    paths: list[str] = []
    seen_casefolded: set[str] = set()
    for line in raw_lines:
        if line != line.strip():
            raise AcceptanceFailure(phase, "candidate file allowlist has surrounding whitespace")
        relative = _relative_posix(line, phase, "candidate file path")
        folded = relative.casefold()
        if folded in seen_casefolded:
            raise AcceptanceFailure(phase, "candidate file allowlist has duplicate Windows paths")
        seen_casefolded.add(folded)
        parts = {part.casefold() for part in PurePosixPath(relative).parts}
        if parts & PROHIBITED_SOURCE_PARTS or relative.lower().endswith((".pyc", ".pyo")):
            raise AcceptanceFailure(phase, "candidate file allowlist includes a generated path")
        paths.append(relative)
    if paths != sorted(paths):
        raise AcceptanceFailure(phase, "candidate file allowlist must be sorted")
    if set(REQUIRED_SOURCE_FILES) - set(paths):
        raise AcceptanceFailure(phase, "candidate file allowlist omits formal-acceptance files")

    allowlist_relative = _path_inside(
        source,
        path,
        phase,
        "candidate file allowlist",
    )
    if allowlist_relative != "reproducibility/candidate-files.txt":
        raise AcceptanceFailure(
            phase,
            "candidate file allowlist must be reproducibility/candidate-files.txt",
        )
    if allowlist_relative not in paths:
        raise AcceptanceFailure(phase, "candidate file allowlist must include itself")
    return tuple(paths)


def _remove_snapshot_destination(path: Path) -> None:
    """Best-effort cleanup which never follows a replacement link."""

    try:
        if _is_link(path):
            try:
                path.unlink()
            except OSError:
                os.rmdir(path)
        elif path.is_dir():
            try:
                _collect_files(path, "source-snapshot-cleanup")
            except AcceptanceFailure:
                return
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def freeze_source_snapshot(
    source_value: str | os.PathLike[str],
    candidate_files_value: str | os.PathLike[str],
    destination_value: str | os.PathLike[str],
    manifest_value: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create one manifest-bound clean candidate from an explicit allowlist."""

    phase = "source-snapshot"
    source = _existing_path(
        source_value,
        phase=phase,
        label="source root",
        kind="directory",
    )
    candidate_files = _existing_path(
        candidate_files_value,
        phase=phase,
        label="candidate file allowlist",
        kind="file",
    )
    destination = _new_path(
        destination_value,
        phase=phase,
        label="source candidate destination",
    )
    manifest = _new_path(
        manifest_value,
        phase=phase,
        label="source manifest",
    )
    destination_resolved = destination.resolve(strict=False)
    manifest_resolved = manifest.resolve(strict=False)
    try:
        source.relative_to(destination_resolved)
    except ValueError:
        pass
    else:
        raise AcceptanceFailure(phase, "source candidate destination cannot contain source root")
    try:
        manifest_resolved.relative_to(destination_resolved)
    except ValueError:
        pass
    else:
        raise AcceptanceFailure(phase, "source manifest must be outside the source candidate")

    paths = _parse_candidate_file_list(candidate_files, source)
    source_hashes: dict[str, str] = {}
    for relative in paths:
        path = source / Path(relative)
        _reject_link_chain(path, phase, "allowlisted source file")
        if not path.is_file():
            raise AcceptanceFailure(phase, "candidate file allowlist names a missing file")
        try:
            resolved = path.resolve(strict=True)
            if resolved.relative_to(source).as_posix() != relative:
                raise AcceptanceFailure(phase, "allowlisted source file escapes its source root")
            source_hashes[relative] = _sha256(path)
        except ValueError as error:
            raise AcceptanceFailure(phase, "allowlisted source file escapes its source root") from error
        except OSError as error:
            raise AcceptanceFailure(phase, "cannot hash an allowlisted source file") from error

    temporary_manifest = manifest.with_name(f".{manifest.name}.tmp")
    created_destination = False
    created_manifest = False
    try:
        _new_path(
            temporary_manifest,
            phase=phase,
            label="temporary source manifest",
        )
        destination.mkdir(parents=True)
        created_destination = True
        for relative in paths:
            target = destination / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / Path(relative), target)

        files = tuple(SourceFile(relative, source_hashes[relative]) for relative in paths)
        for relative, digest in source_hashes.items():
            if _sha256(source / Path(relative)) != digest:
                raise AcceptanceFailure(phase, "allowlisted source changed during snapshot")
        verify_source(destination, files)
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "files": [
                {"path": item.path, "sha256": item.sha256}
                for item in files
            ],
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest_text = json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        ) + "\n"
        temporary_manifest.write_text(
            manifest_text,
            encoding="utf-8",
            newline="\n",
        )
        if os.path.lexists(manifest):
            raise AcceptanceFailure(phase, "source manifest appeared during snapshot")
        os.replace(temporary_manifest, manifest)
        created_manifest = True
        load_source_manifest(destination, manifest)
        return document
    except AcceptanceFailure:
        _remove_snapshot_destination(temporary_manifest)
        if created_manifest:
            _remove_snapshot_destination(manifest)
        if created_destination:
            _remove_snapshot_destination(destination)
        raise
    except OSError as error:
        _remove_snapshot_destination(temporary_manifest)
        if created_manifest:
            _remove_snapshot_destination(manifest)
        if created_destination:
            _remove_snapshot_destination(destination)
        raise AcceptanceFailure(phase, "could not create source snapshot") from error


def compile_frozen_python(
    source: Path,
    files: tuple[SourceFile, ...],
) -> tuple[str, ...]:
    """Compile candidate Python bytes in memory without producing caches."""

    phase = "source-compile"
    compiled: list[str] = []
    for item in files:
        if not item.path.endswith(".py"):
            continue
        try:
            content = (source / Path(item.path)).read_bytes()
            compile(content, item.path, "exec", dont_inherit=True, optimize=0)
        except (OSError, SyntaxError, ValueError) as error:
            raise AcceptanceFailure(phase, "candidate Python source does not compile") from error
        compiled.append(item.path)
    if not compiled:
        raise AcceptanceFailure(phase, "candidate contains no Python source")
    return tuple(compiled)


def _path_inside(root: Path, child: Path, phase: str, label: str) -> str:
    try:
        return child.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise AcceptanceFailure(phase, f"{label} must be inside the clean source") from error


def validate_wheelhouse_manifest(
    wheelhouse: Path,
    *,
    target: str,
    identity: Mapping[str, object],
    project_root: Path,
    runtime_lock: Path,
    dev_lock: Path,
    runtime_entries: Mapping[str, LockEntry],
    dev_entries: Mapping[str, LockEntry],
) -> dict[str, Any]:
    phase = "wheelhouse-manifest"
    document = _require_exact_keys(
        _load_json(wheelhouse / "manifest.json", phase),
        {"schema_version", "target", "python", "locks", "packages", "wheels"},
        phase,
        "wheelhouse manifest",
    )
    if document["schema_version"] != SCHEMA_VERSION or type(document["schema_version"]) is not int:
        raise AcceptanceFailure(phase, "wheelhouse manifest schema version is unsupported")
    if document["target"] != target:
        raise AcceptanceFailure(phase, "wheelhouse target does not match requested target")

    manifest_python = _require_exact_keys(
        document["python"],
        {"implementation", "version", "system", "machine", "bits"},
        phase,
        "wheelhouse python identity",
    )
    manifest_implementation = manifest_python["implementation"]
    runtime_implementation = identity["implementation"]
    if (
        not isinstance(manifest_implementation, str)
        or not isinstance(runtime_implementation, str)
        or manifest_implementation.casefold() != runtime_implementation.casefold()
    ):
        raise AcceptanceFailure(phase, "wheelhouse Python identity does not match interpreter")
    for key in ("version", "system", "machine", "bits"):
        if manifest_python[key] != identity[key]:
            raise AcceptanceFailure(phase, "wheelhouse Python identity does not match interpreter")

    locks = _require_exact_keys(
        document["locks"],
        {"runtime", "dev"},
        phase,
        "wheelhouse locks",
    )
    for label, lock_path in (("runtime", runtime_lock), ("dev", dev_lock)):
        entry = _require_exact_keys(
            locks[label],
            {"path", "sha256"},
            phase,
            f"wheelhouse {label} lock",
        )
        relative = _path_inside(project_root, lock_path, phase, f"{label} lock")
        if entry["path"] != relative or entry["sha256"] != _sha256(lock_path):
            raise AcceptanceFailure(phase, "wheelhouse lock identity does not match exact lock")

    packages_value = document["packages"]
    if not isinstance(packages_value, list):
        raise AcceptanceFailure(phase, "wheelhouse packages must be an array")
    packages: list[tuple[str, str]] = []
    for index, raw_package in enumerate(packages_value):
        package = _require_exact_keys(
            raw_package,
            {"name", "version"},
            phase,
            f"wheelhouse packages[{index}]",
        )
        name = package["name"]
        version = package["version"]
        if (
            not isinstance(name, str)
            or name != _canonical_name(name)
            or not isinstance(version, str)
            or not version
        ):
            raise AcceptanceFailure(phase, "wheelhouse package identity is invalid")
        packages.append((name, version))
    if packages != sorted(set(packages)):
        raise AcceptanceFailure(phase, "wheelhouse packages must be unique and sorted")
    locked_packages = sorted((name, item.version) for name, item in dev_entries.items())
    if packages != locked_packages:
        raise AcceptanceFailure(phase, "wheelhouse packages do not match the development lock")

    wheels_value = document["wheels"]
    if not isinstance(wheels_value, list):
        raise AcceptanceFailure(phase, "wheelhouse wheels must be an array")
    wheel_paths: list[str] = []
    wheel_packages: list[tuple[str, str]] = []
    wheel_hashes: list[tuple[str, str]] = []
    for index, raw_wheel in enumerate(wheels_value):
        wheel = _require_exact_keys(
            raw_wheel,
            {"path", "name", "version", "sha256"},
            phase,
            f"wheelhouse wheels[{index}]",
        )
        relative = _relative_posix(wheel["path"], phase, "wheel path")
        if not relative.startswith("packages/") or not relative.endswith(".whl"):
            raise AcceptanceFailure(phase, "wheel path must be packages/<file>.whl")
        name = wheel["name"]
        version = wheel["version"]
        digest = wheel["sha256"]
        if (
            not isinstance(name, str)
            or name != _canonical_name(name)
            or not isinstance(version, str)
            or not isinstance(digest, str)
            or SHA256_RE.fullmatch(digest) is None
        ):
            raise AcceptanceFailure(phase, "wheel manifest entry is invalid")
        wheel_paths.append(relative)
        wheel_packages.append((name, version))
        wheel_hashes.append((relative, digest))
    if wheel_paths != sorted(set(wheel_paths)):
        raise AcceptanceFailure(phase, "wheel entries must be unique and path-sorted")
    if sorted(wheel_packages) != packages or len(wheel_packages) != len(packages):
        raise AcceptanceFailure(phase, "wheelhouse must contain exactly one wheel per package")
    expected_inventory = {"manifest.json", *wheel_paths}
    if _collect_files(wheelhouse, phase) != expected_inventory:
        raise AcceptanceFailure(phase, "wheelhouse contains missing or unlisted files")
    try:
        for relative, digest in wheel_hashes:
            if _sha256(wheelhouse / Path(relative)) != digest:
                raise AcceptanceFailure(phase, "wheel hash does not match manifest")
    except OSError as error:
        raise AcceptanceFailure(phase, "cannot hash a wheelhouse file") from error

    for name, runtime_entry in runtime_entries.items():
        dev_entry = dev_entries.get(name)
        if dev_entry is None or dev_entry.version != runtime_entry.version:
            raise AcceptanceFailure(phase, "runtime lock is not represented in wheelhouse closure")
    return document


def probe_connectivity(
    *,
    timeout: float,
    connector: Callable[..., Any] = socket.create_connection,
    phase: str = "connectivity-precheck",
) -> list[dict[str, object]]:
    """Fail when any public endpoint appears reachable; never mutate adapters."""

    outcomes: list[dict[str, object]] = []
    reachable = False
    for host, port in CONNECTIVITY_PROBES:
        try:
            connection = connector((host, port), timeout=timeout)
        except OSError as error:
            outcomes.append(
                {
                    "endpoint": f"{host}:{port}",
                    "result": "UNREACHABLE",
                    "error_type": type(error).__name__,
                }
            )
        else:
            reachable = True
            outcomes.append({"endpoint": f"{host}:{port}", "result": "REACHABLE"})
            try:
                connection.close()
            except OSError:
                pass
    if reachable:
        message = (
            "network appears reachable; manually disconnect it before formal acceptance"
            if phase == "connectivity-precheck"
            else "network became reachable before formal acceptance completed"
        )
        failure = AcceptanceFailure(phase, message)
        setattr(failure, "probe_outcomes", outcomes)
        raise failure
    return outcomes


def clean_subprocess_environment(*, matplotlib_cache: Path | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in POISON_ENVIRONMENT_NAMES
        and not any(key.upper().startswith(prefix) for prefix in POISON_ENVIRONMENT_PREFIXES)
    }
    environment.update(
        {
            "NO_PROXY": "*",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "SOURCE_DATE_EPOCH": "315532800",
            "TZ": "UTC",
        }
    )
    if matplotlib_cache is not None:
        environment["MPLCONFIGDIR"] = str(matplotlib_cache)
    return environment


class CommandRecorder:
    def __init__(self, evidence: dict[str, Any], path_tokens: Mapping[Path, str]) -> None:
        self.evidence = evidence
        self.path_tokens = sorted(
            ((path.resolve(), token) for path, token in path_tokens.items()),
            key=lambda item: len(str(item[0])),
            reverse=True,
        )

    def _display(self, value: object) -> str:
        text = str(value)
        lowered = text.casefold()
        for path, token in self.path_tokens:
            prefix = str(path)
            if lowered == prefix.casefold():
                return token
            prefix_with_separator = prefix.rstrip("\\/") + os.sep
            if lowered.startswith(prefix_with_separator.casefold()):
                suffix = text[len(prefix_with_separator) :].replace("\\", "/")
                return f"{token}/{suffix}"
        return text

    def run(
        self,
        phase: str,
        command: Sequence[object],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        expect_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(item) for item in command]
        record: dict[str, object] = {
            "phase": phase,
            "argv": [self._display(item) for item in argv],
            "cwd": self._display(cwd),
        }
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=dict(environment),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as error:
            record.update({"result": "FAIL", "returncode": None})
            self.evidence["phases"].append(record)
            raise AcceptanceFailure(phase, "could not start required subprocess") from error

        record["returncode"] = result.returncode
        if expect_failure:
            if result.returncode == 0:
                record["result"] = "FAIL"
                self.evidence["phases"].append(record)
                raise AcceptanceFailure(phase, "negative offline check unexpectedly succeeded")
            record["result"] = "EXPECTED_FAILURE"
        elif result.returncode != 0:
            record["result"] = "FAIL"
            self.evidence["phases"].append(record)
            raise AcceptanceFailure(phase, "required subprocess returned nonzero")
        else:
            record["result"] = "PASS"
        self.evidence["phases"].append(record)
        return result


IDENTITY_CODE = (
    "import json,pathlib,platform,struct,sys;"
    "distribution='conda' if (pathlib.Path(sys.prefix)/'conda-meta').is_dir() "
    "else 'unidentified';"
    "print(json.dumps({'implementation':platform.python_implementation(),"
    "'version':platform.python_version(),'system':platform.system(),"
    "'machine':platform.machine(),'bits':struct.calcsize('P')*8,"
    "'distribution':distribution,'compiler':platform.python_compiler(),"
    "'build':list(platform.python_build()),"
    "'cache_tag':sys.implementation.cache_tag},sort_keys=True))"
)


def probe_interpreter(
    python: Path,
    target: str,
    recorder: CommandRecorder,
    cwd: Path,
    environment: Mapping[str, str],
) -> dict[str, object]:
    if not python.is_file() or _is_link(python):
        raise AcceptanceFailure("target-identity", "target interpreter is not a regular file")
    result = recorder.run(
        "target-identity",
        [python, "-I", "-c", IDENTITY_CODE],
        cwd=cwd,
        environment=environment,
    )
    try:
        identity = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise AcceptanceFailure("target-identity", "interpreter identity output is invalid") from error
    identity = _require_exact_keys(
        identity,
        {
            "implementation",
            "version",
            "system",
            "machine",
            "bits",
            "distribution",
            "compiler",
            "build",
            "cache_tag",
        },
        "target-identity",
        "interpreter identity",
    )
    match = TARGET_RE.fullmatch(target)
    if match is None:
        raise AcceptanceFailure("target-identity", "only frozen Windows x64 targets are accepted")
    version = identity["version"]
    if (
        identity["implementation"] != "CPython"
        or identity["system"] != "Windows"
        or identity["bits"] != 64
        or not isinstance(version, str)
        or not version.startswith(f"3.{match.group('minor')}.")
        or str(identity["machine"]).casefold() not in {"amd64", "x86_64"}
    ):
        raise AcceptanceFailure("target-identity", "interpreter does not match target identity")
    return identity


def strict_pip_install_command(
    python: Path,
    lock: Path,
    packages: Path,
) -> list[str]:
    return [
        str(python),
        "-m",
        "pip",
        "install",
        "--no-index",
        "--find-links",
        str(packages),
        "--require-hashes",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "-r",
        str(lock),
    ]


def strict_pip_download_command(
    python: Path,
    lock: Path,
    packages: Path,
    destination: Path,
    *,
    no_deps: bool = False,
) -> list[str]:
    command = [
        str(python),
        "-m",
        "pip",
        "download",
        "--no-index",
        "--find-links",
        str(packages),
        "--require-hashes",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
        "--dest",
        str(destination),
    ]
    if no_deps:
        command.append("--no-deps")
    command.extend(["-r", str(lock)])
    return command


def _write_requirement(path: Path, entry: LockEntry) -> None:
    path.write_text(entry.requirement_line + "\n", encoding="utf-8", newline="\n")


def _venv_python(root: Path) -> Path:
    return root / "Scripts" / "python.exe"


def _console_script(root: Path) -> Path:
    return root / "Scripts" / "weather-pipeline.exe"


def _create_venv(
    recorder: CommandRecorder,
    target_python: Path,
    venv: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> Path:
    if venv.exists():
        raise AcceptanceFailure(phase, "fresh virtual environment path already exists")
    recorder.run(
        phase,
        [target_python, "-I", "-m", "venv", str(venv)],
        cwd=cwd,
        environment=environment,
    )
    python = _venv_python(venv)
    if not python.is_file():
        raise AcceptanceFailure(phase, "virtual environment did not create its interpreter")
    return python


def _install_lock(
    recorder: CommandRecorder,
    python: Path,
    lock: Path,
    packages: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> None:
    recorder.run(
        phase,
        strict_pip_install_command(python, lock, packages),
        cwd=cwd,
        environment=environment,
    )


def _pip_check(
    recorder: CommandRecorder,
    python: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> None:
    recorder.run(
        phase,
        [python, "-I", "-m", "pip", "check"],
        cwd=cwd,
        environment=environment,
    )


def _parse_installed_package_inventory(
    text: str,
    phase: str,
) -> list[dict[str, str]]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise AcceptanceFailure(phase, "pip package inventory is not valid JSON") from error
    if not isinstance(value, list):
        raise AcceptanceFailure(phase, "pip package inventory is not a list")

    packages: dict[str, str] = {}
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise AcceptanceFailure(phase, "pip package inventory entry is invalid")
        name = item["name"]
        version = item["version"]
        if not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version:
            raise AcceptanceFailure(phase, "pip package inventory entry is invalid")
        canonical_name = re.sub(r"[-_.]+", "-", name).casefold()
        if canonical_name in packages:
            raise AcceptanceFailure(phase, "pip package inventory contains a duplicate package")
        packages[canonical_name] = version
    return [
        {"name": name, "version": packages[name]}
        for name in sorted(packages)
    ]


def _installed_package_inventory(
    recorder: CommandRecorder,
    python: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> list[dict[str, str]]:
    result = recorder.run(
        phase,
        [python, "-I", "-m", "pip", "list", "--format=json"],
        cwd=cwd,
        environment=environment,
    )
    return _parse_installed_package_inventory(result.stdout, phase)


def _extract_sdist(archive: Path, destination: Path) -> Path:
    phase = "extract-sdist"
    try:
        destination.mkdir()
        with tarfile.open(archive, "r:gz") as stream:
            members = stream.getmembers()
            for member in members:
                relative = PurePosixPath(member.name)
                if (
                    relative.is_absolute()
                    or "\\" in member.name
                    or (relative.parts and ":" in relative.parts[0])
                    or any(part in {"", ".", ".."} for part in relative.parts)
                    or not (member.isdir() or member.isfile())
                ):
                    raise AcceptanceFailure(phase, "sdist contains an unsafe archive member")
            stream.extractall(destination, members=members, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise AcceptanceFailure(phase, "cannot safely extract the sdist") from error
    top_levels = sorted(path for path in destination.iterdir() if path.is_dir())
    if len(top_levels) != 1 or any(path.is_file() for path in destination.iterdir()):
        raise AcceptanceFailure(phase, "sdist must contain exactly one top-level directory")
    return top_levels[0]


def _project_identity_from_wheel(path: Path) -> tuple[str, str]:
    phase = "wheel-metadata"
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(metadata_names) != 1:
                raise AcceptanceFailure(phase, "wheel has invalid metadata inventory")
            metadata = BytesParser(policy=compat32).parsebytes(
                archive.read(metadata_names[0])
            )
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise AcceptanceFailure(phase, "cannot inspect built wheel") from error
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise AcceptanceFailure(phase, "built wheel omits name or version metadata")
    return _canonical_name(name), version


def artifact_hashes(output: Path) -> dict[str, str]:
    phase = "artifact-reproducibility"
    inventory = _collect_files(output, phase)
    if inventory != set(SUCCESS_ARTIFACTS):
        raise AcceptanceFailure(phase, "sample run did not produce the exact seven artifacts")
    return {relative: _sha256(output / Path(relative)) for relative in SUCCESS_ARTIFACTS}


IMPORT_PROBE = (
    "import json,pathlib,sichuan_weather,sys;"
    "p=pathlib.Path(sichuan_weather.__file__).resolve();"
    "v=pathlib.Path(sys.prefix).resolve();"
    "p.relative_to(v);"
    "print(json.dumps({'version':sichuan_weather.__version__},sort_keys=True))"
)


def _verify_installed_import(
    recorder: CommandRecorder,
    python: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> dict[str, object]:
    result = recorder.run(
        phase,
        [python, "-I", "-c", IMPORT_PROBE],
        cwd=cwd,
        environment=environment,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AcceptanceFailure(phase, "installed import probe returned invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {"version"}:
        raise AcceptanceFailure(phase, "installed import probe returned invalid identity")
    return value


def _run_sample(
    recorder: CommandRecorder,
    python: Path,
    venv: Path,
    source: Path,
    output: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    label: str,
    include_help_and_validate: bool,
    checker_python: Path | None = None,
) -> dict[str, str]:
    console = _console_script(venv)
    if not console.is_file():
        raise AcceptanceFailure(f"{label}-console", "console entry point is missing")
    sample_records = source / "data" / "sample" / "records.jsonl"
    sample_manifest = source / "data" / "sample" / "manifest.json"
    checker = source / "scripts" / "check_artifacts.py"
    if include_help_and_validate:
        recorder.run(
            f"{label}-module-help",
            [python, "-I", "-m", "sichuan_weather.cli", "--help"],
            cwd=cwd,
            environment=environment,
        )
        recorder.run(
            f"{label}-console-help",
            [console, "--help"],
            cwd=cwd,
            environment=environment,
        )
        recorder.run(
            f"{label}-validate",
            [
                console,
                "validate",
                "--input",
                sample_records,
                "--manifest",
                sample_manifest,
            ],
            cwd=cwd,
            environment=environment,
        )
    recorder.run(
        f"{label}-run",
        [
            console,
            "run",
            "--input",
            sample_records,
            "--manifest",
            sample_manifest,
            "--output",
            output,
        ],
        cwd=cwd,
        environment=environment,
    )
    recorder.run(
        f"{label}-artifact-check",
        [checker_python or python, "-I", checker, "--output", output],
        cwd=cwd,
        environment=environment,
    )
    return artifact_hashes(output)


def _run_tests(
    recorder: CommandRecorder,
    python: Path,
    source: Path,
    *,
    cwd: Path,
    basetemp: Path,
    environment: Mapping[str, str],
    phase: str,
) -> None:
    result = recorder.run(
        phase,
        [
            python,
            "-I",
            "-m",
            "pytest",
            "--import-mode=importlib",
            "-p",
            "no:cacheprovider",
            "-ra",
            "--basetemp",
            basetemp,
            source / "tests",
        ],
        cwd=cwd,
        environment=environment,
    )
    summary = f"{result.stdout}\n{result.stderr}".lower()
    if re.search(r"\b[1-9][0-9]*\s+(?:skipped|xfailed|xpassed)\b", summary):
        raise AcceptanceFailure(phase, "formal test run contains skip or xfail outcomes")


def _build_distribution(
    recorder: CommandRecorder,
    build_python: Path,
    source: Path,
    output: Path,
    kind: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> Path:
    output.mkdir(parents=True)
    recorder.run(
        phase,
        [
            build_python,
            "-I",
            "-m",
            "build",
            "--no-isolation",
            f"--{kind}",
            "--outdir",
            output,
            source,
        ],
        cwd=cwd,
        environment=environment,
    )
    suffix = ".tar.gz" if kind == "sdist" else ".whl"
    files = sorted(path for path in output.iterdir() if path.is_file() and path.name.endswith(suffix))
    if len(files) != 1 or len(list(output.iterdir())) != 1:
        raise AcceptanceFailure(phase, f"build did not produce exactly one {kind}")
    return files[0]


def _install_project_wheel(
    recorder: CommandRecorder,
    python: Path,
    requirement: Path,
    wheel_dir: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> None:
    recorder.run(
        phase,
        [
            python,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            wheel_dir,
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "-r",
            requirement,
        ],
        cwd=cwd,
        environment=environment,
    )


def _install_project_sdist(
    recorder: CommandRecorder,
    python: Path,
    requirement: Path,
    sdist_dir: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    phase: str,
) -> None:
    recorder.run(
        phase,
        [
            python,
            "-I",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            sdist_dir,
            "--require-hashes",
            "--no-build-isolation",
            "--no-deps",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "-r",
            requirement,
        ],
        cwd=cwd,
        environment=environment,
    )


def _negative_offline_checks(
    recorder: CommandRecorder,
    python: Path,
    runtime_lock: Path,
    packages: Path,
    wheel_manifest: Mapping[str, Any],
    project_wheel: Path,
    project_entry: LockEntry,
    root: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    missing_root = root / "missing wheel"
    missing_packages = missing_root / "packages"
    missing_download = missing_root / "download"
    missing_packages.mkdir(parents=True)
    missing_download.mkdir()
    recorder.run(
        "negative-missing-wheel",
        strict_pip_download_command(
            python,
            runtime_lock,
            missing_packages,
            missing_download,
        ),
        cwd=cwd,
        environment=environment,
        expect_failure=True,
    )

    first_wheel = wheel_manifest["wheels"][0]
    bad_hash_root = root / "bad hash"
    bad_hash_download = bad_hash_root / "download"
    bad_hash_root.mkdir(parents=True)
    bad_hash_download.mkdir()
    bad_hash_lock = bad_hash_root / "bad-hash.txt"
    bad_hash_lock.write_text(
        f"{first_wheel['name']}=={first_wheel['version']} "
        f"--hash=sha256:{'0' * 64}\n",
        encoding="utf-8",
        newline="\n",
    )
    recorder.run(
        "negative-bad-hash",
        strict_pip_download_command(
            python,
            bad_hash_lock,
            packages,
            bad_hash_download,
            no_deps=True,
        ),
        cwd=cwd,
        environment=environment,
        expect_failure=True,
    )

    incompatible_root = root / "incompatible wheel"
    incompatible_packages = incompatible_root / "packages"
    incompatible_download = incompatible_root / "download"
    incompatible_packages.mkdir(parents=True)
    incompatible_download.mkdir()
    safe_name = project_entry.name.replace("-", "_")
    incompatible = (
        incompatible_packages
        / f"{safe_name}-{project_entry.version}-cp39-cp39-win32.whl"
    )
    shutil.copyfile(project_wheel, incompatible)
    incompatible_lock = incompatible_root / "incompatible.txt"
    _write_requirement(incompatible_lock, project_entry)
    recorder.run(
        "negative-incompatible-wheel",
        strict_pip_download_command(
            python,
            incompatible_lock,
            incompatible_packages,
            incompatible_download,
            no_deps=True,
        ),
        cwd=cwd,
        environment=environment,
        expect_failure=True,
    )


def _write_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    text = json.dumps(
        evidence,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _cleanup_failure(work: Path, tracked: Sequence[Path]) -> tuple[str, ...]:
    retained: list[str] = []
    work_resolved = work.resolve()
    for path in reversed(tracked):
        try:
            resolved = path.resolve()
            resolved.relative_to(work_resolved)
        except ValueError:
            retained.append(path.name)
            continue
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved)
            elif resolved.exists():
                resolved.unlink()
        except OSError:
            try:
                retained.append(resolved.relative_to(work_resolved).as_posix())
            except ValueError:
                retained.append(resolved.name)
    return tuple(sorted(set(retained)))


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    target = args.target
    if TARGET_RE.fullmatch(target) is None:
        raise AcceptanceFailure("arguments", "target must be windows-x64-py312 or windows-x64-py313")
    target_python = _existing_path(
        args.python,
        phase="arguments",
        label="target interpreter",
        kind="file",
    )
    source = _existing_path(
        args.source,
        phase="arguments",
        label="source",
        kind="directory",
    )
    source_manifest = _existing_path(
        args.source_manifest,
        phase="arguments",
        label="source manifest",
        kind="file",
    )
    runtime_lock_input = _existing_path(
        args.runtime_lock,
        phase="arguments",
        label="runtime lock",
        kind="file",
    )
    dev_lock_input = _existing_path(
        args.dev_lock,
        phase="arguments",
        label="development lock",
        kind="file",
    )
    wheelhouse = _existing_path(
        args.wheelhouse,
        phase="arguments",
        label="wheelhouse",
        kind="directory",
    )
    wheelhouse_manifest_path = _existing_path(
        wheelhouse / "manifest.json",
        phase="arguments",
        label="wheelhouse manifest",
        kind="file",
    )
    work = _new_path(
        args.work_dir,
        phase="arguments",
        label="work directory",
    ).resolve(strict=False)
    evidence_path = work / "offline-acceptance.json"

    for outer, inner in ((source, wheelhouse), (wheelhouse, source)):
        try:
            inner.relative_to(outer)
        except ValueError:
            pass
        else:
            raise AcceptanceFailure("arguments", "source and wheelhouse must not overlap")
    for protected in (source, wheelhouse):
        try:
            work.relative_to(protected)
        except ValueError:
            pass
        else:
            raise AcceptanceFailure("arguments", "work directory must be outside source and wheelhouse")

    source_files = load_source_manifest(source, source_manifest)
    runtime_entries, dev_entries = validate_lock_pair(
        runtime_lock_input,
        dev_lock_input,
    )
    runtime_relative = _path_inside(
        source,
        runtime_lock_input,
        "arguments",
        "runtime lock",
    )
    dev_relative = _path_inside(source, dev_lock_input, "arguments", "development lock")

    try:
        frozen_hashes = {
            "runtime_sha256": _sha256(runtime_lock_input),
            "dev_sha256": _sha256(dev_lock_input),
            "source_manifest_sha256": _sha256(source_manifest),
            "wheelhouse_manifest_sha256": _sha256(wheelhouse_manifest_path),
        }
        work.mkdir(parents=True)
    except OSError as error:
        raise AcceptanceFailure("arguments", "cannot hash inputs or create work directory") from error
    tracked: list[Path] = []
    evidence: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": "W3_WINDOWS_AUTOMATED_OFFLINE_ACCEPTANCE",
        "status": "RUNNING",
        "target": target,
        "phases": [],
        "network": {"result": "NOT_RUN", "probes": []},
        "network_postcheck": {"result": "NOT_RUN", "probes": []},
        "locks": {
            "runtime_sha256": frozen_hashes["runtime_sha256"],
            "dev_sha256": frozen_hashes["dev_sha256"],
        },
        "source_manifest_sha256": frozen_hashes["source_manifest_sha256"],
        "wheelhouse_manifest_sha256": frozen_hashes["wheelhouse_manifest_sha256"],
    }
    path_tokens = {
        target_python: "<TARGET_PYTHON>",
        source: "<SOURCE>",
        source_manifest: "<SOURCE_MANIFEST>",
        wheelhouse: "<WHEELHOUSE>",
        work: "<WORK>",
    }
    recorder = CommandRecorder(evidence, path_tokens)
    base_environment = clean_subprocess_environment()

    try:
        try:
            probes = probe_connectivity(timeout=args.connectivity_timeout)
        except AcceptanceFailure as error:
            probes = getattr(error, "probe_outcomes", [])
            evidence["network"] = {"result": "REACHABLE", "probes": probes}
            raise
        evidence["network"] = {"result": "UNREACHABLE", "probes": probes}

        identity = probe_interpreter(
            target_python,
            target,
            recorder,
            work,
            base_environment,
        )
        evidence["python"] = {
            **identity,
            "executable_sha256": _sha256(target_python),
        }
        wheel_manifest = validate_wheelhouse_manifest(
            wheelhouse,
            target=target,
            identity=identity,
            project_root=source,
            runtime_lock=runtime_lock_input,
            dev_lock=dev_lock_input,
            runtime_entries=runtime_entries,
            dev_entries=dev_entries,
        )

        clean_source = work / "source copy with spaces"
        tracked.append(clean_source)
        copy_clean_source(source, clean_source, source_files)
        compiled = compile_frozen_python(clean_source, source_files)
        evidence["source_compile"] = {
            "result": "PASS",
            "files": list(compiled),
        }
        runtime_lock = clean_source / Path(runtime_relative)
        dev_lock = clean_source / Path(dev_relative)
        packages = wheelhouse / "packages"

        execution_cwd = work / "execution cwd outside source"
        execution_cwd.mkdir()
        tracked.append(execution_cwd)

        build_venv = work / "build env with spaces"
        tracked.append(build_venv)
        build_python = _create_venv(
            recorder,
            target_python,
            build_venv,
            cwd=execution_cwd,
            environment=base_environment,
            phase="create-build-venv",
        )
        _install_lock(
            recorder,
            build_python,
            dev_lock,
            packages,
            cwd=execution_cwd,
            environment=base_environment,
            phase="install-build-dev-lock",
        )
        _pip_check(
            recorder,
            build_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="check-build-environment",
        )
        evidence["installed_packages"] = {
            "build": _installed_package_inventory(
                recorder,
                build_python,
                cwd=execution_cwd,
                environment=base_environment,
                phase="inventory-build-environment",
            )
        }

        prepare_wheelhouse = clean_source / "scripts" / "prepare_wheelhouse.py"
        recorder.run(
            "verify-wheelhouse-tool",
            [
                build_python,
                "-I",
                prepare_wheelhouse,
                "verify",
                "--python",
                build_python,
                "--target",
                target,
                "--project-root",
                clean_source,
                "--runtime-lock",
                runtime_lock,
                "--dev-lock",
                dev_lock,
                "--wheelhouse",
                wheelhouse,
            ],
            cwd=execution_cwd,
            environment=base_environment,
        )
        closure = work / "wheel closure verification"
        closure.mkdir()
        tracked.append(closure)
        recorder.run(
            "verify-wheelhouse-pip-closure",
            strict_pip_download_command(
                build_python,
                dev_lock,
                packages,
                closure,
            ),
            cwd=execution_cwd,
            environment=base_environment,
        )

        distributions = work / "distributions"
        tracked.append(distributions)
        sdist_dir = distributions / "sdist"
        sdist = _build_distribution(
            recorder,
            build_python,
            clean_source,
            sdist_dir,
            "sdist",
            cwd=execution_cwd,
            environment=base_environment,
            phase="build-sdist",
        )
        extracted = distributions / "extracted sdist with spaces"
        extracted_source = _extract_sdist(sdist, extracted)
        wheel_dir = distributions / "wheel"
        wheel = _build_distribution(
            recorder,
            build_python,
            extracted_source,
            wheel_dir,
            "wheel",
            cwd=execution_cwd,
            environment=base_environment,
            phase="build-wheel-from-sdist",
        )
        project_name, project_version = _project_identity_from_wheel(wheel)
        project_entry = LockEntry(project_name, project_version, (_sha256(wheel),))
        project_wheel_lock = distributions / "project-wheel.txt"
        _write_requirement(project_wheel_lock, project_entry)
        project_sdist_entry = LockEntry(project_name, project_version, (_sha256(sdist),))
        project_sdist_lock = distributions / "project-sdist.txt"
        _write_requirement(project_sdist_lock, project_sdist_entry)
        evidence["distributions"] = {
            "sdist": {"filename": sdist.name, "sha256": _sha256(sdist)},
            "wheel": {"filename": wheel.name, "sha256": _sha256(wheel)},
        }

        pip_only_lock = distributions / "pip-only.txt"
        _write_requirement(pip_only_lock, dev_entries["pip"])
        runtime_venv = work / "runtime env with spaces"
        tracked.append(runtime_venv)
        runtime_python = _create_venv(
            recorder,
            target_python,
            runtime_venv,
            cwd=execution_cwd,
            environment=base_environment,
            phase="create-runtime-venv",
        )
        _install_lock(
            recorder,
            runtime_python,
            pip_only_lock,
            packages,
            cwd=execution_cwd,
            environment=base_environment,
            phase="pin-runtime-pip",
        )
        _install_lock(
            recorder,
            runtime_python,
            runtime_lock,
            packages,
            cwd=execution_cwd,
            environment=base_environment,
            phase="install-runtime-lock",
        )
        _install_project_wheel(
            recorder,
            runtime_python,
            project_wheel_lock,
            wheel_dir,
            cwd=execution_cwd,
            environment=base_environment,
            phase="install-project-wheel",
        )
        _pip_check(
            recorder,
            runtime_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="check-runtime-environment",
        )
        evidence["installed_project"] = _verify_installed_import(
            recorder,
            runtime_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="runtime-import",
        )
        evidence["installed_packages"]["runtime_only"] = _installed_package_inventory(
            recorder,
            runtime_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="inventory-runtime-environment",
        )

        run_root = work / "run outputs"
        run_root.mkdir()
        tracked.append(run_root)
        cache_root = work / "matplotlib caches"
        cache_root.mkdir()
        tracked.append(cache_root)
        first_environment = clean_subprocess_environment(
            matplotlib_cache=cache_root / "first independent cache"
        )
        second_environment = clean_subprocess_environment(
            matplotlib_cache=cache_root / "second independent cache"
        )
        first_hashes = _run_sample(
            recorder,
            runtime_python,
            runtime_venv,
            clean_source,
            run_root / "first run",
            cwd=execution_cwd,
            environment=first_environment,
            label="runtime-first",
            include_help_and_validate=True,
            checker_python=build_python,
        )
        second_hashes = _run_sample(
            recorder,
            runtime_python,
            runtime_venv,
            clean_source,
            run_root / "second run",
            cwd=execution_cwd,
            environment=second_environment,
            label="runtime-second",
            include_help_and_validate=False,
            checker_python=build_python,
        )
        if first_hashes != second_hashes:
            raise AcceptanceFailure(
                "artifact-reproducibility",
                "two independent sample runs are not byte-identical",
            )
        evidence["artifacts"] = {
            "byte_identical": True,
            "sha256": first_hashes,
        }

        _install_lock(
            recorder,
            runtime_python,
            dev_lock,
            packages,
            cwd=execution_cwd,
            environment=base_environment,
            phase="extend-runtime-with-dev-lock",
        )
        _pip_check(
            recorder,
            runtime_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="check-runtime-dev-environment",
        )
        evidence["installed_packages"]["wheel_test"] = _installed_package_inventory(
            recorder,
            runtime_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="inventory-wheel-test-environment",
        )
        test_temp_root = work / "test temporary roots"
        test_temp_root.mkdir()
        tracked.append(test_temp_root)
        _run_tests(
            recorder,
            runtime_python,
            clean_source,
            cwd=execution_cwd,
            basetemp=test_temp_root / "wheel install tests",
            environment=base_environment,
            phase="test-wheel-install",
        )

        sdist_venv = work / "sdist env with spaces"
        tracked.append(sdist_venv)
        sdist_python = _create_venv(
            recorder,
            target_python,
            sdist_venv,
            cwd=execution_cwd,
            environment=base_environment,
            phase="create-sdist-venv",
        )
        _install_lock(
            recorder,
            sdist_python,
            dev_lock,
            packages,
            cwd=execution_cwd,
            environment=base_environment,
            phase="install-sdist-dev-lock",
        )
        _install_project_sdist(
            recorder,
            sdist_python,
            project_sdist_lock,
            sdist_dir,
            cwd=execution_cwd,
            environment=base_environment,
            phase="install-project-sdist",
        )
        _pip_check(
            recorder,
            sdist_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="check-sdist-environment",
        )
        _verify_installed_import(
            recorder,
            sdist_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="sdist-import",
        )
        evidence["installed_packages"]["sdist_test"] = _installed_package_inventory(
            recorder,
            sdist_python,
            cwd=execution_cwd,
            environment=base_environment,
            phase="inventory-sdist-test-environment",
        )
        _run_tests(
            recorder,
            sdist_python,
            extracted_source,
            cwd=execution_cwd,
            basetemp=test_temp_root / "sdist install tests",
            environment=base_environment,
            phase="test-sdist-install",
        )
        sdist_environment = clean_subprocess_environment(
            matplotlib_cache=cache_root / "sdist independent cache"
        )
        _run_sample(
            recorder,
            sdist_python,
            sdist_venv,
            extracted_source,
            run_root / "sdist run",
            cwd=execution_cwd,
            environment=sdist_environment,
            label="sdist",
            include_help_and_validate=True,
        )

        negative_root = work / "negative offline checks"
        negative_root.mkdir()
        tracked.append(negative_root)
        _negative_offline_checks(
            recorder,
            build_python,
            runtime_lock,
            packages,
            wheel_manifest,
            wheel,
            project_entry,
            negative_root,
            cwd=execution_cwd,
            environment=base_environment,
        )

        try:
            postcheck_probes = probe_connectivity(
                timeout=args.connectivity_timeout,
                phase="connectivity-postcheck",
            )
        except AcceptanceFailure as error:
            evidence["network_postcheck"] = {
                "result": "REACHABLE",
                "probes": getattr(error, "probe_outcomes", []),
            }
            raise
        evidence["network_postcheck"] = {
            "result": "UNREACHABLE",
            "probes": postcheck_probes,
        }
        evidence["status"] = "PASS"
        evidence["manual_disconnect"] = (
            "performed externally; corroborated by precheck and postcheck connectivity probes"
        )
        _write_evidence(evidence_path, evidence)
        return evidence
    except Exception as caught:
        if isinstance(caught, AcceptanceFailure):
            error = caught
        elif isinstance(caught, OSError):
            error = AcceptanceFailure(
                "filesystem",
                f"filesystem operation failed ({type(caught).__name__})",
            )
        else:
            error = AcceptanceFailure(
                "internal",
                f"acceptance driver failed ({type(caught).__name__})",
            )
        evidence["status"] = "FAIL"
        evidence["failed_phase"] = error.phase
        retained = _cleanup_failure(work, tracked)
        evidence["cleanup"] = {
            "result": "PASS" if not retained else "FAIL",
            "retained": list(retained),
        }
        try:
            _write_evidence(evidence_path, evidence)
        except OSError:
            pass
        if error is caught:
            raise
        raise error from caught


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze a clean W3 source snapshot or run one formal Windows target "
            "after the operator has manually disconnected networking. This tool "
            "never changes network state."
        )
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    snapshot = subparsers.add_parser(
        "snapshot",
        help="Freeze an exact allowlisted clean-source candidate and external manifest",
    )
    snapshot.add_argument("--source", required=True, help="Working project source root")
    snapshot.add_argument(
        "--candidate-files",
        required=True,
        help="Sorted project-relative candidate-file allowlist (which includes itself)",
    )
    snapshot.add_argument(
        "--destination",
        required=True,
        help="New clean source-candidate directory",
    )
    snapshot.add_argument(
        "--manifest",
        required=True,
        help="New external schema-v1 source manifest",
    )

    accept = subparsers.add_parser(
        "accept",
        help="Run formal acceptance for one disconnected Windows Python target",
    )
    accept.add_argument("--python", required=True, help="Exact target CPython executable")
    accept.add_argument(
        "--target",
        required=True,
        choices=("windows-x64-py312", "windows-x64-py313"),
    )
    accept.add_argument("--source", required=True, help="Frozen clean candidate source root")
    accept.add_argument(
        "--source-manifest",
        required=True,
        help="External schema-v1 exact source-file SHA-256 manifest",
    )
    accept.add_argument("--runtime-lock", required=True, help="Exact target runtime hash lock")
    accept.add_argument("--dev-lock", required=True, help="Exact target dev hash lock")
    accept.add_argument("--wheelhouse", required=True, help="Prepared target wheelhouse root")
    accept.add_argument(
        "--work-dir",
        required=True,
        help="New formal acceptance directory; it must not already exist",
    )
    accept.add_argument(
        "--connectivity-timeout",
        type=float,
        default=1.0,
        help="Per-endpoint connectivity timeout in seconds (default: 1.0)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.operation == "snapshot":
        try:
            freeze_source_snapshot(
                args.source,
                args.candidate_files,
                args.destination,
                args.manifest,
            )
        except AcceptanceFailure as error:
            print(
                f"source-snapshot: ERROR [{error.phase}]: {error}",
                file=sys.stderr,
            )
            return 1
        except OSError as error:
            print(
                f"source-snapshot: ERROR [filesystem]: {type(error).__name__}",
                file=sys.stderr,
            )
            return 1
        except Exception as error:
            print(
                f"source-snapshot: ERROR [internal]: {type(error).__name__}",
                file=sys.stderr,
            )
            return 1
        print("source-snapshot: PASS")
        return 0

    if not 0.05 <= args.connectivity_timeout <= 10.0:
        parser.error("--connectivity-timeout must be between 0.05 and 10 seconds")
    try:
        run_acceptance(args)
    except AcceptanceFailure as error:
        print(
            f"offline-acceptance: ERROR [{error.phase}]: {error}",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(
            f"offline-acceptance: ERROR [filesystem]: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        print(
            f"offline-acceptance: ERROR [internal]: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    print("offline-acceptance: PASS (automated Windows target scope)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
