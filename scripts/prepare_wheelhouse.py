"""Prepare and verify a hash-locked, wheel-only dependency wheelhouse.

The ``prepare`` command is the only online phase.  Both it and ``verify``
finish by asking pip to resolve the complete lock from the local wheelhouse
with ``--no-index``.  Verification binds every file to the supplied lock
files and to the selected target interpreter without persisting absolute
host paths.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Mapping, Sequence
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier
from packaging.utils import (
    InvalidWheelFilename,
    canonicalize_name,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version


_TARGET_RE = re.compile(r"(?P<system>windows|linux)-x64-py(?P<major>3)(?P<minor>[0-9]{2})\Z")
_HASH_RE = re.compile(
    r"(?<!\S)--hash=sha256:(?P<digest>[0-9A-Fa-f]{64})(?=\s|\Z)"
)
_MANIFEST_NAME = "manifest.json"
_PACKAGES_NAME = "packages"
_SCHEMA_VERSION = 1


class WheelhouseError(RuntimeError):
    """The lock, target, wheel inventory, or closure proof is invalid."""


@dataclass(frozen=True, slots=True)
class TargetSpec:
    name: str
    system: str
    python_version: tuple[int, int]


@dataclass(frozen=True, slots=True)
class InterpreterInfo:
    implementation: str
    version: str
    system: str
    machine: str
    bits: int
    marker_environment: Mapping[str, str]
    compatible_tags: frozenset[str]

    def manifest_identity(self) -> dict[str, object]:
        return {
            "implementation": self.implementation,
            "version": self.version,
            "system": self.system,
            "machine": self.machine,
            "bits": self.bits,
        }


@dataclass(frozen=True, slots=True)
class LockEntry:
    name: str
    version: str
    hashes: frozenset[str]
    active: bool


@dataclass(frozen=True, slots=True)
class LockFile:
    role: str
    path: Path
    relative_path: str
    sha256: str
    entries: Mapping[str, LockEntry]

    @property
    def active_entries(self) -> dict[str, LockEntry]:
        return {name: entry for name, entry in self.entries.items() if entry.active}

    def manifest_identity(self) -> dict[str, str]:
        return {"path": self.relative_path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class WheelFile:
    path: str
    name: str
    version: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_spec(target: str) -> TargetSpec:
    match = _TARGET_RE.fullmatch(target)
    if match is None:
        raise WheelhouseError(
            "target must match windows-x64-py3NN or linux-x64-py3NN"
        )
    system = "Windows" if match.group("system") == "windows" else "Linux"
    return TargetSpec(
        name=target,
        system=system,
        python_version=(int(match.group("major")), int(match.group("minor"))),
    )


def _sanitized_environment(*, offline: bool) -> dict[str, str]:
    offline_network_overrides = {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PIP_")
        and key.upper() not in {"PYTHONHOME", "PYTHONPATH"}
        and (not offline or key.upper() not in offline_network_overrides)
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_REQUIRE_HASHES": "1",
            "PIP_ONLY_BINARY": ":all:",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
    )
    if offline:
        environment["PIP_NO_INDEX"] = "1"
    return environment


_INTERPRETER_PROBE = textwrap.dedent(
    """
    import json
    import platform
    import struct
    import sys
    from packaging.markers import default_environment
    from packaging.tags import sys_tags

    print(json.dumps({
        "implementation": sys.implementation.name,
        "version": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "bits": struct.calcsize("P") * 8,
        "marker_environment": default_environment(),
        "compatible_tags": sorted(str(tag) for tag in sys_tags()),
    }, sort_keys=True))
    """
).strip()


def _probe_interpreter(python: Path) -> InterpreterInfo:
    if not python.exists() or not python.is_file():
        raise WheelhouseError("target Python executable does not exist or is not a file")
    try:
        completed = subprocess.run(
            [str(python), "-c", _INTERPRETER_PROBE],
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env=_sanitized_environment(offline=True),
        )
    except OSError as error:
        raise WheelhouseError("target Python interpreter could not be executed") from error
    if completed.returncode != 0:
        raise WheelhouseError(
            "target Python must provide the locked packaging APIs for tag inspection"
        )
    try:
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise TypeError
        marker_environment = value["marker_environment"]
        tags = value["compatible_tags"]
        if not isinstance(marker_environment, dict) or not isinstance(tags, list):
            raise TypeError
        return InterpreterInfo(
            implementation=str(value["implementation"]),
            version=str(value["version"]),
            system=str(value["system"]),
            machine=str(value["machine"]),
            bits=int(value["bits"]),
            marker_environment={
                str(key): str(item) for key, item in marker_environment.items()
            },
            compatible_tags=frozenset(str(tag) for tag in tags),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise WheelhouseError("target Python returned invalid identity data") from error


def _validate_target(spec: TargetSpec, interpreter: InterpreterInfo) -> None:
    try:
        version = Version(interpreter.version)
    except InvalidVersion as error:
        raise WheelhouseError("target Python reported an invalid version") from error
    machine = interpreter.machine.casefold().replace("-", "_")
    if interpreter.implementation.casefold() != "cpython":
        raise WheelhouseError("target interpreter must be CPython")
    if (version.major, version.minor) != spec.python_version:
        raise WheelhouseError("target name does not match the interpreter Python version")
    if interpreter.system.casefold() != spec.system.casefold():
        raise WheelhouseError("target name does not match the interpreter operating system")
    if interpreter.bits != 64 or machine not in {"amd64", "x86_64"}:
        raise WheelhouseError("target interpreter must be x86-64")
    if not interpreter.compatible_tags:
        raise WheelhouseError("target interpreter returned no compatible wheel tags")


def _relative_file(path: Path, project_root: Path, label: str) -> tuple[Path, str]:
    if path.is_symlink():
        raise WheelhouseError(f"{label} must be a regular file, not a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise WheelhouseError(f"{label} does not exist") from error
    if not resolved.is_file():
        raise WheelhouseError(f"{label} must be a regular file")
    try:
        relative = resolved.relative_to(project_root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise WheelhouseError(f"{label} must be inside project root") from error
    return resolved, relative.as_posix()


def _logical_requirement_lines(text: str) -> list[tuple[int, str]]:
    logical: list[tuple[int, str]] = []
    pending: list[str] = []
    first_line = 0
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            if pending:
                raise WheelhouseError(
                    f"lock continuation is interrupted at line {line_number}"
                )
            continue
        stripped = re.split(r"\s+#", stripped, maxsplit=1)[0].rstrip()
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        if not pending:
            first_line = line_number
        pending.append(fragment)
        if not continued:
            logical.append((first_line, " ".join(pending)))
            pending = []
    if pending:
        raise WheelhouseError(f"lock has an unfinished continuation at line {first_line}")
    return logical


def _parse_lock(
    role: str,
    path: Path,
    project_root: Path,
    marker_environment: Mapping[str, str],
) -> LockFile:
    resolved, relative = _relative_file(path, project_root, f"{role} lock")
    data = resolved.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise WheelhouseError(f"{role} lock must not contain a UTF-8 BOM")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise WheelhouseError(f"{role} lock is not valid UTF-8") from error

    entries: dict[str, LockEntry] = {}
    for line_number, logical in _logical_requirement_lines(text):
        if logical.startswith("-"):
            raise WheelhouseError(
                f"{role} lock line {line_number} contains an active option or include"
            )
        hashes = frozenset(
            match.group("digest").lower() for match in _HASH_RE.finditer(logical)
        )
        requirement_text = _HASH_RE.sub("", logical).strip()
        if "--hash=" in requirement_text:
            raise WheelhouseError(
                f"{role} lock line {line_number} contains a non-SHA256 hash"
            )
        if not hashes:
            raise WheelhouseError(
                f"{role} lock line {line_number} is missing a SHA256 hash"
            )
        try:
            requirement = Requirement(requirement_text)
        except InvalidRequirement as error:
            raise WheelhouseError(
                f"{role} lock line {line_number} is not a valid pinned requirement"
            ) from error
        if requirement.url is not None:
            raise WheelhouseError(
                f"{role} lock line {line_number} contains a direct URL or path"
            )
        if requirement.extras:
            raise WheelhouseError(
                f"{role} lock line {line_number} must be stripped of extras"
            )
        specifiers: list[Specifier] = list(requirement.specifier)
        if (
            len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise WheelhouseError(
                f"{role} lock line {line_number} is not pinned with one exact == version"
            )
        try:
            version = str(Version(specifiers[0].version))
        except InvalidVersion as error:
            raise WheelhouseError(
                f"{role} lock line {line_number} has an invalid exact version"
            ) from error
        name = str(canonicalize_name(requirement.name))
        if name in entries:
            raise WheelhouseError(f"{role} lock repeats project {name}")
        active = requirement.marker is None or requirement.marker.evaluate(
            environment=dict(marker_environment)
        )
        entries[name] = LockEntry(
            name=name,
            version=version,
            hashes=hashes,
            active=active,
        )
    if not entries:
        raise WheelhouseError(f"{role} lock contains no requirements")
    return LockFile(
        role=role,
        path=resolved,
        relative_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        entries=entries,
    )


def _load_locks(
    runtime_lock: Path,
    dev_lock: Path,
    project_root: Path,
    interpreter: InterpreterInfo,
) -> tuple[LockFile, LockFile]:
    runtime = _parse_lock(
        "runtime", runtime_lock, project_root, interpreter.marker_environment
    )
    dev = _parse_lock("dev", dev_lock, project_root, interpreter.marker_environment)
    runtime_active = runtime.active_entries
    dev_active = dev.active_entries
    if not dev_active:
        raise WheelhouseError("dev lock has no requirements active for the target")
    for name, runtime_entry in runtime_active.items():
        dev_entry = dev_active.get(name)
        if dev_entry is None or dev_entry.version != runtime_entry.version:
            raise WheelhouseError(
                "dev lock must contain every active runtime project at the same version"
            )
    return runtime, dev


def _validate_packages_directory(packages_dir: Path) -> None:
    if packages_dir.is_symlink() or not packages_dir.is_dir():
        raise WheelhouseError("wheelhouse packages path must be a regular directory")


def _scan_wheels(
    packages_dir: Path,
    runtime: LockFile,
    dev: LockFile,
    interpreter: InterpreterInfo,
) -> tuple[WheelFile, ...]:
    _validate_packages_directory(packages_dir)
    expected = dev.active_entries
    runtime_expected = runtime.active_entries
    wheels: dict[str, WheelFile] = {}
    for path in sorted(packages_dir.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            raise WheelhouseError("packages contains a symlink or non-file entry")
        if path.suffix != ".whl":
            raise WheelhouseError(
                f"packages contains a non-wheel distribution: {path.name}"
            )
        try:
            parsed_name, parsed_version, _build, tags = parse_wheel_filename(path.name)
        except InvalidWheelFilename as error:
            raise WheelhouseError(f"invalid wheel filename: {path.name}") from error
        name = str(canonicalize_name(parsed_name))
        entry = expected.get(name)
        if entry is None:
            raise WheelhouseError(f"wheel is not present in the active dev lock: {path.name}")
        version = str(parsed_version)
        if version != entry.version:
            raise WheelhouseError(f"wheel version is not allowed by the lock: {path.name}")
        if not any(str(tag) in interpreter.compatible_tags for tag in tags):
            raise WheelhouseError(f"wheel is incompatible with the target: {path.name}")
        digest = _sha256(path)
        if digest not in entry.hashes:
            raise WheelhouseError(f"wheel hash is not allowed by the dev lock: {path.name}")
        runtime_entry = runtime_expected.get(name)
        if runtime_entry is not None and digest not in runtime_entry.hashes:
            raise WheelhouseError(
                f"wheel hash is not allowed by the runtime lock: {path.name}"
            )
        if name in wheels:
            raise WheelhouseError(f"packages contains multiple wheels for project {name}")
        wheels[name] = WheelFile(
            path=f"{_PACKAGES_NAME}/{path.name}",
            name=name,
            version=version,
            sha256=digest,
        )
    missing = sorted(set(expected) - set(wheels))
    if missing:
        raise WheelhouseError("wheelhouse is missing locked projects: " + ", ".join(missing))
    return tuple(wheels[name] for name in sorted(wheels))


def _manifest_document(
    spec: TargetSpec,
    interpreter: InterpreterInfo,
    runtime: LockFile,
    dev: LockFile,
    wheels: tuple[WheelFile, ...],
) -> dict[str, object]:
    packages = [
        {"name": name, "version": entry.version}
        for name, entry in sorted(dev.active_entries.items())
    ]
    return {
        "schema_version": _SCHEMA_VERSION,
        "target": spec.name,
        "python": interpreter.manifest_identity(),
        "locks": {
            "runtime": runtime.manifest_identity(),
            "dev": dev.manifest_identity(),
        },
        "packages": packages,
        "wheels": [wheel.to_dict() for wheel in wheels],
    }


def _manifest_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WheelhouseError(f"manifest repeats JSON key {key}")
        result[key] = value
    return result


def _read_manifest(path: Path) -> tuple[object, bytes]:
    try:
        data = path.read_bytes()
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                WheelhouseError(f"manifest contains non-standard number {token}")
            ),
        )
    except WheelhouseError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WheelhouseError("manifest is missing or invalid") from error
    return value, data


def _write_manifest(path: Path, document: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_manifest_bytes(document))
    os.replace(temporary, path)


def _validate_wheelhouse_root(wheelhouse: Path, *, require_manifest: bool) -> Path:
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise WheelhouseError("wheelhouse must be a regular directory")
    expected = {_PACKAGES_NAME}
    if require_manifest:
        expected.add(_MANIFEST_NAME)
    actual = {path.name for path in wheelhouse.iterdir()}
    if actual != expected:
        raise WheelhouseError("wheelhouse root inventory is not exact")
    packages_dir = wheelhouse / _PACKAGES_NAME
    _validate_packages_directory(packages_dir)
    manifest = wheelhouse / _MANIFEST_NAME
    if require_manifest and (manifest.is_symlink() or not manifest.is_file()):
        raise WheelhouseError("wheelhouse manifest must be a regular file")
    return packages_dir


def _run_pip_download(
    python: Path,
    lock: Path,
    destination: Path,
    *,
    offline_packages: Path | None,
) -> None:
    command = [
        str(python),
        "-m",
        "pip",
        "download",
        "--require-hashes",
        "--only-binary=:all:",
        "--no-cache-dir",
        "--disable-pip-version-check",
    ]
    offline = offline_packages is not None
    if offline_packages is not None:
        command.extend(["--no-index", "--find-links", str(offline_packages)])
    command.extend(["--dest", str(destination), "-r", str(lock)])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            env=_sanitized_environment(offline=offline),
        )
    except OSError as error:
        raise WheelhouseError("target pip could not be executed") from error
    if completed.returncode != 0:
        phase = "offline closure" if offline else "online preparation"
        raise WheelhouseError(
            f"target pip {phase} download failed with exit code {completed.returncode}"
        )


def _prove_closure(
    python: Path,
    wheelhouse: Path,
    runtime: LockFile,
    dev: LockFile,
    interpreter: InterpreterInfo,
    expected_wheels: tuple[WheelFile, ...],
) -> None:
    with tempfile.TemporaryDirectory(
        prefix=".wheelhouse-closure-", dir=wheelhouse.parent
    ) as temporary:
        destination = Path(temporary)
        _run_pip_download(
            python,
            dev.path,
            destination,
            offline_packages=wheelhouse / _PACKAGES_NAME,
        )
        closure_wheels = _scan_wheels(destination, runtime, dev, interpreter)
        if closure_wheels != expected_wheels:
            raise WheelhouseError("offline closure inventory differs from wheelhouse")


def _resolve_inputs(
    *,
    python: Path,
    target: str,
    project_root: Path,
    runtime_lock: Path,
    dev_lock: Path,
) -> tuple[Path, TargetSpec, InterpreterInfo, LockFile, LockFile]:
    root = project_root.resolve(strict=True)
    python_path = python if python.is_absolute() else root / python
    runtime_path = runtime_lock if runtime_lock.is_absolute() else root / runtime_lock
    dev_path = dev_lock if dev_lock.is_absolute() else root / dev_lock
    try:
        resolved_python = python_path.resolve(strict=True)
    except OSError as error:
        raise WheelhouseError("target Python executable does not exist") from error
    spec = _target_spec(target)
    interpreter = _probe_interpreter(resolved_python)
    _validate_target(spec, interpreter)
    runtime, dev = _load_locks(runtime_path, dev_path, root, interpreter)
    return resolved_python, spec, interpreter, runtime, dev


def prepare_wheelhouse(
    *,
    python: Path,
    target: str,
    project_root: Path,
    runtime_lock: Path,
    dev_lock: Path,
    wheelhouse: Path,
) -> dict[str, object]:
    resolved_python, spec, interpreter, runtime, dev = _resolve_inputs(
        python=python,
        target=target,
        project_root=project_root,
        runtime_lock=runtime_lock,
        dev_lock=dev_lock,
    )
    root = project_root.resolve(strict=True)
    wheelhouse_path = wheelhouse if wheelhouse.is_absolute() else root / wheelhouse
    if wheelhouse_path.exists():
        if wheelhouse_path.is_symlink() or not wheelhouse_path.is_dir():
            raise WheelhouseError("target wheelhouse must be absent or an empty directory")
        if next(wheelhouse_path.iterdir(), None) is not None:
            raise WheelhouseError("target wheelhouse must initially be empty")
    else:
        wheelhouse_path.mkdir(parents=True)
    packages_dir = wheelhouse_path / _PACKAGES_NAME
    packages_dir.mkdir()

    _run_pip_download(
        resolved_python,
        dev.path,
        packages_dir,
        offline_packages=None,
    )
    _validate_wheelhouse_root(wheelhouse_path, require_manifest=False)
    wheels = _scan_wheels(packages_dir, runtime, dev, interpreter)
    document = _manifest_document(spec, interpreter, runtime, dev, wheels)
    _prove_closure(
        resolved_python,
        wheelhouse_path,
        runtime,
        dev,
        interpreter,
        wheels,
    )
    _write_manifest(wheelhouse_path / _MANIFEST_NAME, document)
    return document


def verify_wheelhouse(
    *,
    python: Path,
    target: str,
    project_root: Path,
    runtime_lock: Path,
    dev_lock: Path,
    wheelhouse: Path,
) -> dict[str, object]:
    resolved_python, spec, interpreter, runtime, dev = _resolve_inputs(
        python=python,
        target=target,
        project_root=project_root,
        runtime_lock=runtime_lock,
        dev_lock=dev_lock,
    )
    root = project_root.resolve(strict=True)
    wheelhouse_path = wheelhouse if wheelhouse.is_absolute() else root / wheelhouse
    packages_dir = _validate_wheelhouse_root(wheelhouse_path, require_manifest=True)
    wheels = _scan_wheels(packages_dir, runtime, dev, interpreter)
    expected = _manifest_document(spec, interpreter, runtime, dev, wheels)
    actual, manifest_data = _read_manifest(wheelhouse_path / _MANIFEST_NAME)
    if actual != expected or manifest_data != _manifest_bytes(expected):
        raise WheelhouseError("manifest does not exactly match locks and wheel inventory")
    _prove_closure(
        resolved_python,
        wheelhouse_path,
        runtime,
        dev,
        interpreter,
        wheels,
    )
    return expected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or verify a deterministic, hash-locked wheelhouse."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("prepare", "verify"):
        command = subparsers.add_parser(mode)
        command.add_argument("--python", required=True, type=Path)
        command.add_argument("--target", required=True)
        command.add_argument("--project-root", type=Path, default=Path.cwd())
        command.add_argument("--runtime-lock", required=True, type=Path)
        command.add_argument("--dev-lock", required=True, type=Path)
        command.add_argument("--wheelhouse", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    operation = prepare_wheelhouse if args.mode == "prepare" else verify_wheelhouse
    try:
        operation(
            python=args.python,
            target=args.target,
            project_root=args.project_root,
            runtime_lock=args.runtime_lock,
            dev_lock=args.dev_lock,
            wheelhouse=args.wheelhouse,
        )
    except (WheelhouseError, OSError) as error:
        message = str(error) if isinstance(error, WheelhouseError) else type(error).__name__
        print(f"prepare-wheelhouse: error: {message}", file=sys.stderr)
        return 1
    print(f"wheelhouse {args.mode} verified for {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
