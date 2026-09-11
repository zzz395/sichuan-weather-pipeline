"""Synthetic, network-free tests for the W3 wheelhouse preparation tool."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from types import ModuleType
from typing import Callable

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "prepare_wheelhouse.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("w3_prepare_wheelhouse", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

WheelhouseError = tool.WheelhouseError

DEMO_WHEEL = "demo_pkg-1.0-py3-none-any.whl"
TOOLING_WHEEL = "tooling-2.0-py3-none-any.whl"
WHEEL_BYTES = {
    DEMO_WHEEL: b"synthetic demo wheel\n",
    TOOLING_WHEEL: b"synthetic tooling wheel\n",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lock_requirement(name: str, version: str, wheel: str) -> str:
    return f"{name}=={version} \\\n    --hash=sha256:{_digest(WHEEL_BYTES[wheel])}\n"


def _interpreter() -> object:
    system = platform.system()
    if system not in {"Windows", "Linux"}:
        system = "Windows"
    machine = "AMD64" if system == "Windows" else "x86_64"
    major, minor = sys.version_info[:2]
    return tool.InterpreterInfo(
        implementation="cpython",
        version=f"{major}.{minor}.0",
        system=system,
        machine=machine,
        bits=64,
        marker_environment={
            "implementation_name": "cpython",
            "implementation_version": f"{major}.{minor}.0",
            "os_name": "nt" if system == "Windows" else "posix",
            "platform_machine": machine,
            "platform_python_implementation": "CPython",
            "platform_release": "test",
            "platform_system": system,
            "platform_version": "test",
            "python_full_version": f"{major}.{minor}.0",
            "python_version": f"{major}.{minor}",
            "sys_platform": "win32" if system == "Windows" else "linux",
        },
        compatible_tags=frozenset({"py3-none-any"}),
    )


def _target(interpreter: object) -> str:
    prefix = "windows" if interpreter.system == "Windows" else "linux"
    version = interpreter.version.split(".")
    return f"{prefix}-x64-py{version[0]}{version[1]}"


def _write_locks(root: Path) -> tuple[Path, Path]:
    requirements = root / "requirements"
    requirements.mkdir()
    runtime = requirements / "runtime.txt"
    dev = requirements / "dev.txt"
    runtime.write_text(
        _lock_requirement("demo-pkg", "1.0", DEMO_WHEEL), encoding="utf-8"
    )
    dev.write_text(
        _lock_requirement("demo-pkg", "1.0", DEMO_WHEEL)
        + _lock_requirement("tooling", "2.0", TOOLING_WHEEL),
        encoding="utf-8",
    )
    return runtime, dev


def _fake_download(
    calls: list[Path | None],
) -> Callable[[Path, Path, Path], None]:
    def download(
        python: Path,
        lock: Path,
        destination: Path,
        *,
        offline_packages: Path | None,
    ) -> None:
        del python, lock
        calls.append(offline_packages)
        destination.mkdir(parents=True, exist_ok=True)
        if offline_packages is None:
            for filename, data in WHEEL_BYTES.items():
                (destination / filename).write_bytes(data)
        else:
            for path in offline_packages.iterdir():
                if path.is_file():
                    (destination / path.name).write_bytes(path.read_bytes())

    return download


def _prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, object, list[Path | None]]:
    runtime, dev = _write_locks(tmp_path)
    interpreter = _interpreter()
    calls: list[Path | None] = []
    monkeypatch.setattr(tool, "_probe_interpreter", lambda _python: interpreter)
    monkeypatch.setattr(tool, "_run_pip_download", _fake_download(calls))
    wheelhouse = tmp_path / ".w3" / "wheelhouse" / _target(interpreter)
    tool.prepare_wheelhouse(
        python=Path(sys.executable),
        target=_target(interpreter),
        project_root=tmp_path,
        runtime_lock=runtime,
        dev_lock=dev,
        wheelhouse=wheelhouse,
    )
    return wheelhouse, runtime, dev, interpreter, calls


def _verify(
    root: Path,
    wheelhouse: Path,
    runtime: Path,
    dev: Path,
    interpreter: object,
) -> dict[str, object]:
    return tool.verify_wheelhouse(
        python=Path(sys.executable),
        target=_target(interpreter),
        project_root=root,
        runtime_lock=runtime,
        dev_lock=dev,
        wheelhouse=wheelhouse,
    )


def test_prepare_emits_deterministic_relative_manifest_and_proves_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse, runtime, dev, interpreter, calls = _prepare(tmp_path, monkeypatch)

    document = json.loads((wheelhouse / "manifest.json").read_text(encoding="utf-8"))
    assert document == {
        "schema_version": 1,
        "target": _target(interpreter),
        "python": {
            "implementation": "cpython",
            "version": interpreter.version,
            "system": interpreter.system,
            "machine": interpreter.machine,
            "bits": 64,
        },
        "locks": {
            "runtime": {
                "path": "requirements/runtime.txt",
                "sha256": _digest(runtime.read_bytes()),
            },
            "dev": {
                "path": "requirements/dev.txt",
                "sha256": _digest(dev.read_bytes()),
            },
        },
        "packages": [
            {"name": "demo-pkg", "version": "1.0"},
            {"name": "tooling", "version": "2.0"},
        ],
        "wheels": [
            {
                "path": f"packages/{DEMO_WHEEL}",
                "name": "demo-pkg",
                "version": "1.0",
                "sha256": _digest(WHEEL_BYTES[DEMO_WHEEL]),
            },
            {
                "path": f"packages/{TOOLING_WHEEL}",
                "name": "tooling",
                "version": "2.0",
                "sha256": _digest(WHEEL_BYTES[TOOLING_WHEEL]),
            },
        ],
    }
    manifest_bytes = (wheelhouse / "manifest.json").read_bytes()
    assert manifest_bytes == tool._manifest_bytes(document)
    assert str(tmp_path).encode() not in manifest_bytes
    assert calls == [None, wheelhouse / "packages"]


def test_verify_is_network_free_and_reproves_local_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse, runtime, dev, interpreter, calls = _prepare(tmp_path, monkeypatch)
    calls.clear()

    verified = _verify(tmp_path, wheelhouse, runtime, dev, interpreter)

    assert verified["schema_version"] == 1
    assert calls == [wheelhouse / "packages"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda packages: (packages / DEMO_WHEEL).unlink(),
            "missing locked projects: demo-pkg",
        ),
        (
            lambda packages: (packages / DEMO_WHEEL).write_bytes(b"tampered"),
            "hash is not allowed",
        ),
        (
            lambda packages: (packages / "unknown-1.0-py3-none-any.whl").write_bytes(
                b"unknown"
            ),
            "not present in the active dev lock",
        ),
        (
            lambda packages: (
                (packages / DEMO_WHEEL).unlink(),
                (packages / "demo_pkg-1.0-cp312-cp312-win32.whl").write_bytes(
                    WHEEL_BYTES[DEMO_WHEEL]
                ),
            ),
            "incompatible with the target",
        ),
        (
            lambda packages: (packages / "demo-pkg-1.0.tar.gz").write_bytes(
                b"source distribution"
            ),
            "non-wheel distribution",
        ),
    ],
    ids=["missing", "tampered", "added", "incompatible", "sdist"],
)
def test_verify_rejects_invalid_wheel_inventory_before_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Callable[[Path], object],
    message: str,
) -> None:
    wheelhouse, runtime, dev, interpreter, calls = _prepare(tmp_path, monkeypatch)
    calls.clear()
    mutation(wheelhouse / "packages")

    with pytest.raises(WheelhouseError, match=message):
        _verify(tmp_path, wheelhouse, runtime, dev, interpreter)

    assert calls == []


def test_verify_rejects_manifest_content_or_format_drift_before_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse, runtime, dev, interpreter, calls = _prepare(tmp_path, monkeypatch)
    calls.clear()
    manifest = wheelhouse / "manifest.json"
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["target"] = "windows-x64-py399"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(WheelhouseError, match="manifest does not exactly match"):
        _verify(tmp_path, wheelhouse, runtime, dev, interpreter)

    assert calls == []


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("demo==1.0\n", "missing a SHA256 hash"),
        (
            "demo>=1.0 --hash=sha256:" + "0" * 64 + "\n",
            "not pinned with one exact == version",
        ),
        (
            "demo @ https://example.invalid/demo.whl --hash=sha256:"
            + "0" * 64
            + "\n",
            "direct URL or path",
        ),
        (
            "demo @ file:///tmp/demo.whl --hash=sha256:" + "0" * 64 + "\n",
            "direct URL or path",
        ),
        ("--index-url https://example.invalid/simple\n", "active option or include"),
        (
            "demo==1.0 --hash=sha512:" + "0" * 128 + "\n",
            "non-SHA256 hash",
        ),
    ],
    ids=["unhashed", "range", "url", "path", "option", "wrong-hash"],
)
def test_lock_validation_rejects_nonportable_or_unfrozen_entries(
    tmp_path: Path, line: str, message: str
) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text(line, encoding="utf-8")

    with pytest.raises(WheelhouseError, match=message):
        tool._parse_lock("dev", lock, tmp_path, _interpreter().marker_environment)


def test_runtime_lock_must_be_a_same_version_subset_of_dev_lock(tmp_path: Path) -> None:
    runtime, dev = _write_locks(tmp_path)
    dev.write_text(
        _lock_requirement("demo-pkg", "2.0", DEMO_WHEEL), encoding="utf-8"
    )

    with pytest.raises(WheelhouseError, match="every active runtime project"):
        tool._load_locks(runtime, dev, tmp_path, _interpreter())


def test_target_name_must_match_selected_interpreter_version() -> None:
    interpreter = _interpreter()
    wrong_minor = int(interpreter.version.split(".")[1]) + 1
    prefix = "windows" if interpreter.system == "Windows" else "linux"

    with pytest.raises(WheelhouseError, match="interpreter Python version"):
        tool._validate_target(
            tool._target_spec(f"{prefix}-x64-py3{wrong_minor:02d}"), interpreter
        )


def test_lock_lexical_symlink_is_rejected_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "lock.txt"
    lock.write_text(
        _lock_requirement("demo-pkg", "1.0", DEMO_WHEEL), encoding="utf-8"
    )
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == lock or original(path),
    )

    with pytest.raises(WheelhouseError, match="not a symlink"):
        tool._parse_lock("dev", lock, tmp_path, _interpreter().marker_environment)


def test_wheelhouse_lexical_symlink_is_rejected_before_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheelhouse, runtime, dev, interpreter, calls = _prepare(tmp_path, monkeypatch)
    calls.clear()
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == wheelhouse or original(path),
    )

    with pytest.raises(WheelhouseError, match="regular directory"):
        _verify(tmp_path, wheelhouse, runtime, dev, interpreter)

    assert calls == []


def test_prepare_does_not_publish_manifest_when_offline_closure_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, dev = _write_locks(tmp_path)
    interpreter = _interpreter()
    calls = 0

    def download(
        python: Path,
        lock: Path,
        destination: Path,
        *,
        offline_packages: Path | None,
    ) -> None:
        nonlocal calls
        del python, lock
        calls += 1
        if offline_packages is not None:
            raise WheelhouseError("synthetic closure failure")
        for filename, data in WHEEL_BYTES.items():
            (destination / filename).write_bytes(data)

    monkeypatch.setattr(tool, "_probe_interpreter", lambda _python: interpreter)
    monkeypatch.setattr(tool, "_run_pip_download", download)
    wheelhouse = tmp_path / "wheelhouse"

    with pytest.raises(WheelhouseError, match="synthetic closure failure"):
        tool.prepare_wheelhouse(
            python=Path(sys.executable),
            target=_target(interpreter),
            project_root=tmp_path,
            runtime_lock=runtime,
            dev_lock=dev,
            wheelhouse=wheelhouse,
        )

    assert calls == 2
    assert not (wheelhouse / "manifest.json").exists()


def test_prepare_refuses_nonempty_target_without_invoking_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, dev = _write_locks(tmp_path)
    interpreter = _interpreter()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "sentinel.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(tool, "_probe_interpreter", lambda _python: interpreter)
    monkeypatch.setattr(
        tool,
        "_run_pip_download",
        lambda *_args, **_kwargs: pytest.fail("pip must not run"),
    )

    with pytest.raises(WheelhouseError, match="initially be empty"):
        tool.prepare_wheelhouse(
            python=Path(sys.executable),
            target=_target(interpreter),
            project_root=tmp_path,
            runtime_lock=runtime,
            dev_lock=dev,
            wheelhouse=wheelhouse,
        )

    assert (wheelhouse / "sentinel.txt").read_text(encoding="utf-8") == "keep"


def test_pip_invocation_is_hashed_binary_only_sanitized_and_shell_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "lock.txt"
    destination = tmp_path / "destination"
    packages = tmp_path / "packages"
    captured: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setenv("PIP_INDEX_URL", "https://secret.invalid/simple")
    monkeypatch.setenv("PIP_TRUSTED_HOST", "secret.invalid")
    monkeypatch.setenv("PYTHONPATH", "secret-path")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-secret.invalid")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(tool.subprocess, "run", run)
    tool._run_pip_download(
        Path(sys.executable), lock, destination, offline_packages=None
    )
    tool._run_pip_download(
        Path(sys.executable), lock, destination, offline_packages=packages
    )

    assert len(captured) == 2
    for command, kwargs in captured:
        assert command[:4] == [str(Path(sys.executable)), "-m", "pip", "download"]
        assert "--require-hashes" in command
        assert "--only-binary=:all:" in command
        assert "--no-cache-dir" in command
        assert "--disable-pip-version-check" in command
        assert kwargs["shell"] is False
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert environment["PIP_CONFIG_FILE"] == os.devnull
        assert environment["PIP_REQUIRE_HASHES"] == "1"
        assert environment["PIP_ONLY_BINARY"] == ":all:"
        assert "PIP_INDEX_URL" not in environment
        assert "PIP_TRUSTED_HOST" not in environment
        assert "PYTHONPATH" not in environment
    online_command, online_kwargs = captured[0]
    offline_command, offline_kwargs = captured[1]
    assert "--no-index" not in online_command
    assert "PIP_NO_INDEX" not in online_kwargs["env"]
    assert online_kwargs["env"]["HTTPS_PROXY"] == "https://proxy-secret.invalid"
    assert offline_command[offline_command.index("--find-links") + 1] == str(packages)
    assert "--no-index" in offline_command
    assert offline_kwargs["env"]["PIP_NO_INDEX"] == "1"
    assert "HTTPS_PROXY" not in offline_kwargs["env"]


def test_pip_failure_does_not_echo_child_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "SECRET-TOKEN-IN-CHILD-OUTPUT"

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 1, secret, secret)

    monkeypatch.setattr(tool.subprocess, "run", run)

    with pytest.raises(WheelhouseError) as captured:
        tool._run_pip_download(
            Path(sys.executable),
            tmp_path / "lock.txt",
            tmp_path / "destination",
            offline_packages=tmp_path / "packages",
        )

    assert secret not in str(captured.value)


@pytest.mark.parametrize("mode", ["prepare", "verify"])
def test_cli_dispatches_both_target_interpreter_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    received: list[dict[str, object]] = []

    def operation(**kwargs: object) -> dict[str, object]:
        received.append(kwargs)
        return {}

    monkeypatch.setattr(tool, f"{mode}_wheelhouse", operation)
    target = "windows-x64-py313"
    arguments = [
        mode,
        "--python",
        str(Path(sys.executable)),
        "--target",
        target,
        "--project-root",
        str(tmp_path),
        "--runtime-lock",
        "requirements/runtime.txt",
        "--dev-lock",
        "requirements/dev.txt",
        "--wheelhouse",
        ".w3/wheelhouse/target",
    ]

    assert tool.main(arguments) == 0
    assert received == [
        {
            "python": Path(sys.executable),
            "target": target,
            "project_root": tmp_path,
            "runtime_lock": Path("requirements/runtime.txt"),
            "dev_lock": Path("requirements/dev.txt"),
            "wheelhouse": Path(".w3/wheelhouse/target"),
        }
    ]
    assert capsys.readouterr().out == f"wheelhouse {mode} verified for {target}\n"
