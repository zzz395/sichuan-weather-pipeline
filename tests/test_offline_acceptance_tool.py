from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "offline_acceptance.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("w3_offline_acceptance", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


HASH_A = "1" * 64
HASH_B = "2" * 64


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_lock(path: Path, packages: dict[str, str]) -> None:
    lines = [
        f"{name}=={version} --hash=sha256:{index:064x}"
        for index, (name, version) in enumerate(sorted(packages.items()), start=1)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _required_dev_packages() -> dict[str, str]:
    return {
        "build": "1.2.2",
        "matplotlib": "3.10.0",
        "pillow": "11.1.0",
        "pip": "25.0",
        "pip-tools": "7.4.1",
        "pytest": "8.3.4",
        "setuptools": "75.8.0",
        "wheel": "0.45.1",
    }


def _make_source_candidate(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "clean source candidate"
    for relative in tool.REQUIRED_SOURCE_FILES:
        path = source / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((relative + "\n").encode("utf-8"))
    manifest = tmp_path / "frozen-source-manifest.json"
    files = [
        {"path": relative, "sha256": _digest(source / Path(relative))}
        for relative in sorted(tool.REQUIRED_SOURCE_FILES)
    ]
    manifest.write_text(
        json.dumps({"schema_version": 1, "files": files}, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return source, manifest


def _make_snapshot_source(tmp_path: Path) -> tuple[Path, Path, tuple[str, ...]]:
    source = tmp_path / "working project"
    relative_paths = tuple(
        sorted(
            {
                *tool.REQUIRED_SOURCE_FILES,
                "src/sichuan_weather/__init__.py",
                "tests/test_smoke.py",
            }
        )
    )
    for relative in relative_paths:
        if relative == "reproducibility/candidate-files.txt":
            continue
        path = source / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".py"):
            path.write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
        else:
            path.write_text(relative + "\n", encoding="utf-8", newline="\n")
    allowlist = source / "reproducibility" / "candidate-files.txt"
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(
        "\n".join(relative_paths) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (source / "unlisted-private-note.txt").write_text("secret", encoding="utf-8")
    return source, allowlist, relative_paths


def _make_wheelhouse(
    tmp_path: Path,
) -> tuple[
    Path,
    Path,
    Path,
    dict[str, tool.LockEntry],
    dict[str, tool.LockEntry],
    dict[str, object],
]:
    project = tmp_path / "project"
    runtime_lock = project / "requirements" / "runtime.txt"
    dev_lock = project / "requirements" / "dev.txt"
    _write_lock(runtime_lock, {"matplotlib": "3.10.0"})
    _write_lock(dev_lock, _required_dev_packages())
    runtime = tool.parse_hashed_lock(runtime_lock)
    dev = tool.parse_hashed_lock(dev_lock)

    wheelhouse = tmp_path / "wheelhouse with spaces"
    package_dir = wheelhouse / "packages"
    package_dir.mkdir(parents=True)
    wheels: list[dict[str, str]] = []
    for name, entry in sorted(dev.items()):
        wheel_name = name.replace("-", "_") + f"-{entry.version}-py3-none-any.whl"
        path = package_dir / wheel_name
        path.write_bytes(f"wheel:{name}:{entry.version}".encode("ascii"))
        wheels.append(
            {
                "path": f"packages/{wheel_name}",
                "name": name,
                "version": entry.version,
                "sha256": _digest(path),
            }
        )

    identity: dict[str, object] = {
        "implementation": "CPython",
        "version": "3.13.5",
        "system": "Windows",
        "machine": "AMD64",
        "bits": 64,
    }
    manifest = {
        "schema_version": 1,
        "target": "windows-x64-py313",
        "python": identity,
        "locks": {
            "runtime": {
                "path": "requirements/runtime.txt",
                "sha256": _digest(runtime_lock),
            },
            "dev": {
                "path": "requirements/dev.txt",
                "sha256": _digest(dev_lock),
            },
        },
        "packages": [
            {"name": name, "version": entry.version}
            for name, entry in sorted(dev.items())
        ],
        "wheels": wheels,
    }
    (wheelhouse / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return wheelhouse, project, runtime_lock, runtime, dev, identity


def _recorder(tmp_path: Path) -> tuple[tool.CommandRecorder, dict[str, object]]:
    evidence: dict[str, object] = {"phases": []}
    return tool.CommandRecorder(evidence, {tmp_path: "<WORK>"}), evidence


def test_frozen_source_requires_ci_workflow() -> None:
    assert ".github/workflows/ci.yml" in tool.REQUIRED_SOURCE_FILES


def test_parser_exposes_explicit_formal_acceptance_inputs() -> None:
    parser = tool.build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert set(subparsers_action.choices) == {"snapshot", "accept"}
    accept = subparsers_action.choices["accept"]
    option_strings = {
        option
        for action in accept._actions
        for option in action.option_strings
    }
    assert {
        "--python",
        "--target",
        "--source",
        "--source-manifest",
        "--runtime-lock",
        "--dev-lock",
        "--wheelhouse",
        "--work-dir",
        "--connectivity-timeout",
    } <= option_strings
    snapshot_options = {
        option
        for action in subparsers_action.choices["snapshot"]._actions
        for option in action.option_strings
    }
    assert {
        "--source",
        "--candidate-files",
        "--destination",
        "--manifest",
    } <= snapshot_options


def test_parse_hashed_lock_accepts_pip_tools_continuations(tmp_path: Path) -> None:
    lock = tmp_path / "dev lock.txt"
    lock.write_text(
        "# generated\n"
        "Demo_Pkg==1.2.3 \\\n"
        f"    --hash=sha256:{HASH_B} \\\n"
        f"    --hash=sha256:{HASH_A}\n",
        encoding="utf-8",
        newline="\n",
    )

    entries = tool.parse_hashed_lock(lock)

    assert entries == {
        "demo-pkg": tool.LockEntry("demo-pkg", "1.2.3", (HASH_A, HASH_B))
    }
    assert entries["demo-pkg"].requirement_line == (
        f"demo-pkg==1.2.3 --hash=sha256:{HASH_A} --hash=sha256:{HASH_B}"
    )


@pytest.mark.parametrize(
    "line",
    [
        "demo==1.0",
        f"demo>=1.0 --hash=sha256:{HASH_A}",
        f"demo @ https://example.invalid/demo.whl --hash=sha256:{HASH_A}",
        "--index-url https://example.invalid/simple",
        f"demo==1.0; python_version > '3' --hash=sha256:{HASH_A}",
    ],
)
def test_parse_hashed_lock_rejects_nonfrozen_inputs(tmp_path: Path, line: str) -> None:
    lock = tmp_path / "bad.txt"
    lock.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(tool.AcceptanceFailure, match="exactly pinned|forbidden"):
        tool.parse_hashed_lock(lock)


def test_validate_lock_pair_requires_exact_runtime_superset(tmp_path: Path) -> None:
    runtime_lock = tmp_path / "runtime.txt"
    dev_lock = tmp_path / "dev.txt"
    _write_lock(runtime_lock, {"matplotlib": "3.10.0"})
    _write_lock(dev_lock, _required_dev_packages())

    runtime, dev = tool.validate_lock_pair(runtime_lock, dev_lock)

    assert runtime["matplotlib"].version == "3.10.0"
    assert set(runtime) < set(dev)

    _write_lock(dev_lock, {**_required_dev_packages(), "matplotlib": "3.9.0"})
    with pytest.raises(tool.AcceptanceFailure, match="exact runtime superset"):
        tool.validate_lock_pair(runtime_lock, dev_lock)


def test_source_manifest_copy_is_exact_and_supports_spaces(tmp_path: Path) -> None:
    source, manifest = _make_source_candidate(tmp_path)

    files = tool.load_source_manifest(source, manifest)
    destination = tmp_path / "formal work" / "source copy with spaces"
    tool.copy_clean_source(source, destination, files)

    assert tool._collect_files(destination, "test") == set(tool.REQUIRED_SOURCE_FILES)
    assert [item.path for item in files] == sorted(tool.REQUIRED_SOURCE_FILES)


def test_source_manifest_rejects_unlisted_extra_file(tmp_path: Path) -> None:
    source, manifest = _make_source_candidate(tmp_path)
    (source / "surprise.txt").write_text("not frozen", encoding="utf-8")

    with pytest.raises(tool.AcceptanceFailure, match="inventory differs"):
        tool.load_source_manifest(source, manifest)


def test_source_manifest_rejects_path_traversal(tmp_path: Path) -> None:
    source, manifest = _make_source_candidate(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["files"][0]["path"] = "../escape"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(tool.AcceptanceFailure, match="relative|canonical"):
        tool.load_source_manifest(source, manifest)


def test_snapshot_cli_creates_exact_deterministic_candidate_only_from_allowlist(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, allowlist, relative_paths = _make_snapshot_source(tmp_path)
    destination = tmp_path / "source candidate with spaces"
    manifest = tmp_path / "evidence" / "source-manifest.json"

    result = tool.main(
        [
            "snapshot",
            "--source",
            str(source),
            "--candidate-files",
            str(allowlist),
            "--destination",
            str(destination),
            "--manifest",
            str(manifest),
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == "source-snapshot: PASS\n"
    assert tool._collect_files(destination, "test") == set(relative_paths)
    assert not (destination / "unlisted-private-note.txt").exists()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    assert document == {
        "schema_version": 1,
        "files": [
            {"path": relative, "sha256": _digest(destination / Path(relative))}
            for relative in relative_paths
        ],
    }
    encoded = manifest.read_text(encoding="utf-8")
    assert str(source) not in encoded
    assert "timestamp" not in encoded
    assert tool.load_source_manifest(destination, manifest)

    second_destination = tmp_path / "second source candidate with spaces"
    second_manifest = tmp_path / "evidence" / "second-source-manifest.json"
    tool.freeze_source_snapshot(source, allowlist, second_destination, second_manifest)
    assert manifest.read_bytes() == second_manifest.read_bytes()


@pytest.mark.parametrize(
    "replacement",
    [
        ("tests/test_smoke.py", "src/sichuan_weather/__init__.py"),
        ("../escape.py",),
        ("build/generated.py",),
        ("Tests/test_smoke.py", "tests/test_smoke.py"),
    ],
)
def test_snapshot_rejects_unsorted_traversal_generated_and_duplicate_paths(
    tmp_path: Path,
    replacement: tuple[str, ...],
) -> None:
    source, allowlist, relative_paths = _make_snapshot_source(tmp_path)
    retained = [
        relative
        for relative in relative_paths
        if relative not in {"src/sichuan_weather/__init__.py", "tests/test_smoke.py"}
    ]
    lines = [*retained, *replacement]
    allowlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
    destination = tmp_path / "candidate"
    manifest = tmp_path / "manifest.json"

    with pytest.raises(tool.AcceptanceFailure):
        tool.freeze_source_snapshot(source, allowlist, destination, manifest)

    assert not destination.exists()
    assert not manifest.exists()


def test_snapshot_rejects_linked_allowlisted_file_before_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, allowlist, _ = _make_snapshot_source(tmp_path)
    linked = (source / "tests" / "test_smoke.py").absolute()
    original_is_link = tool._is_link

    def simulated_link(path: Path) -> bool:
        return path.absolute() == linked or original_is_link(path)

    monkeypatch.setattr(tool, "_is_link", simulated_link)
    destination = tmp_path / "candidate"
    manifest = tmp_path / "manifest.json"

    with pytest.raises(tool.AcceptanceFailure, match="symbolic link or junction"):
        tool.freeze_source_snapshot(source, allowlist, destination, manifest)

    assert not destination.exists()
    assert not manifest.exists()


def test_existing_path_rejects_lexical_link_before_resolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lexical_link = (tmp_path / "lexical-link").absolute()
    lexical_link.write_bytes(b"target")
    monkeypatch.setattr(tool, "_is_link", lambda path: path == lexical_link)

    def forbidden_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        del self, args, kwargs
        raise AssertionError("resolve must not run before lexical link rejection")

    monkeypatch.setattr(type(lexical_link), "resolve", forbidden_resolve)
    with pytest.raises(tool.AcceptanceFailure, match="symbolic link or junction"):
        tool._existing_path(
            lexical_link,
            phase="arguments",
            label="test input",
            kind="file",
        )


def test_compile_frozen_python_is_in_memory_and_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    good = source / "good.py"
    good.write_text("answer = 42\n", encoding="utf-8")
    files = (tool.SourceFile("good.py", _digest(good)),)

    assert tool.compile_frozen_python(source, files) == ("good.py",)
    assert not (source / "__pycache__").exists()

    good.write_text("def broken(:\n", encoding="utf-8")
    with pytest.raises(tool.AcceptanceFailure, match="does not compile"):
        tool.compile_frozen_python(source, files)


def test_sdist_extraction_rejects_windows_backslash_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    content = b"escape"
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo("package\\..\\escape.py")
        member.size = len(content)
        stream.addfile(member, io.BytesIO(content))

    with pytest.raises(tool.AcceptanceFailure, match="unsafe archive member"):
        tool._extract_sdist(archive, tmp_path / "extracted")

    assert not (tmp_path / "escape.py").exists()


def test_wheelhouse_manifest_accepts_exact_confirmed_schema(tmp_path: Path) -> None:
    wheelhouse, project, runtime_lock, runtime, dev, identity = _make_wheelhouse(tmp_path)

    document = tool.validate_wheelhouse_manifest(
        wheelhouse,
        target="windows-x64-py313",
        identity=identity,
        project_root=project,
        runtime_lock=runtime_lock,
        dev_lock=project / "requirements" / "dev.txt",
        runtime_entries=runtime,
        dev_entries=dev,
    )

    assert document["schema_version"] == 1
    assert [item["name"] for item in document["packages"]] == sorted(dev)
    assert len(document["wheels"]) == len(dev)


def test_wheelhouse_manifest_accepts_cpython_implementation_case_difference(
    tmp_path: Path,
) -> None:
    wheelhouse, project, runtime_lock, runtime, dev, identity = _make_wheelhouse(tmp_path)
    manifest_path = wheelhouse / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["python"]["implementation"] = "cpython"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    document = tool.validate_wheelhouse_manifest(
        wheelhouse,
        target="windows-x64-py313",
        identity=identity,
        project_root=project,
        runtime_lock=runtime_lock,
        dev_lock=project / "requirements" / "dev.txt",
        runtime_entries=runtime,
        dev_entries=dev,
    )

    assert document["python"]["implementation"] == "cpython"
    assert identity["implementation"] == "CPython"


def test_wheelhouse_manifest_rejects_different_python_implementation(
    tmp_path: Path,
) -> None:
    wheelhouse, project, runtime_lock, runtime, dev, identity = _make_wheelhouse(tmp_path)
    manifest_path = wheelhouse / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["python"]["implementation"] = "cpython"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    identity["implementation"] = "PyPy"

    with pytest.raises(
        tool.AcceptanceFailure,
        match="wheelhouse Python identity does not match interpreter",
    ):
        tool.validate_wheelhouse_manifest(
            wheelhouse,
            target="windows-x64-py313",
            identity=identity,
            project_root=project,
            runtime_lock=runtime_lock,
            dev_lock=project / "requirements" / "dev.txt",
            runtime_entries=runtime,
            dev_entries=dev,
        )


@pytest.mark.parametrize("mutation", ["wheel-tamper", "extra-file", "wrong-target"])
def test_wheelhouse_manifest_fails_closed_on_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    wheelhouse, project, runtime_lock, runtime, dev, identity = _make_wheelhouse(tmp_path)
    manifest_path = wheelhouse / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "wheel-tamper":
        (wheelhouse / Path(manifest["wheels"][0]["path"])).write_bytes(b"tampered")
    elif mutation == "extra-file":
        (wheelhouse / "extra.whl").write_bytes(b"extra")
    else:
        manifest["target"] = "windows-x64-py312"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(tool.AcceptanceFailure):
        tool.validate_wheelhouse_manifest(
            wheelhouse,
            target="windows-x64-py313",
            identity=identity,
            project_root=project,
            runtime_lock=runtime_lock,
            dev_lock=project / "requirements" / "dev.txt",
            runtime_entries=runtime,
            dev_entries=dev,
        )


def test_connectivity_precheck_records_all_unreachable_endpoints() -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    def disconnected(address: tuple[str, int], *, timeout: float) -> object:
        calls.append((address, timeout))
        raise TimeoutError("offline")

    outcomes = tool.probe_connectivity(timeout=0.25, connector=disconnected)

    assert len(calls) == len(tool.CONNECTIVITY_PROBES)
    assert {item["result"] for item in outcomes} == {"UNREACHABLE"}
    assert {item["error_type"] for item in outcomes} == {"TimeoutError"}


def test_connectivity_precheck_refuses_when_any_endpoint_is_reachable() -> None:
    class Connection:
        closed = False

        def close(self) -> None:
            self.closed = True

    connection = Connection()

    def partly_connected(address: tuple[str, int], *, timeout: float) -> object:
        del timeout
        if address == tool.CONNECTIVITY_PROBES[0]:
            return connection
        raise ConnectionRefusedError("offline")

    with pytest.raises(tool.AcceptanceFailure, match="manually disconnect") as caught:
        tool.probe_connectivity(timeout=0.1, connector=partly_connected)

    assert connection.closed
    outcomes = caught.value.probe_outcomes
    assert [item["result"] for item in outcomes].count("REACHABLE") == 1
    assert len(outcomes) == len(tool.CONNECTIVITY_PROBES)


def test_connectivity_probe_reports_the_requested_checkpoint_phase() -> None:
    def reachable(address: tuple[str, int], *, timeout: float) -> object:
        del address, timeout

        class Connection:
            def close(self) -> None:
                pass

        return Connection()

    with pytest.raises(tool.AcceptanceFailure) as caught:
        tool.probe_connectivity(
            timeout=0.1,
            connector=reachable,
            phase="connectivity-postcheck",
        )

    assert caught.value.phase == "connectivity-postcheck"


def test_installed_package_inventory_is_strict_and_deterministic() -> None:
    inventory = tool._parse_installed_package_inventory(
        json.dumps(
            [
                {"name": "Z-Package", "version": "2.0"},
                {"name": "alpha_package", "version": "1.0"},
            ]
        ),
        "package-inventory",
    )

    assert inventory == [
        {"name": "alpha-package", "version": "1.0"},
        {"name": "z-package", "version": "2.0"},
    ]
    with pytest.raises(tool.AcceptanceFailure, match="duplicate package"):
        tool._parse_installed_package_inventory(
            json.dumps(
                [
                    {"name": "alpha-package", "version": "1.0"},
                    {"name": "Alpha_Package", "version": "1.0"},
                ]
            ),
            "package-inventory",
        )


def test_interpreter_identity_records_distribution_provenance() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", tool.IDENTITY_CODE],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    identity = json.loads(result.stdout)

    assert set(identity) == {
        "bits",
        "build",
        "cache_tag",
        "compiler",
        "distribution",
        "implementation",
        "machine",
        "system",
        "version",
    }
    assert identity["distribution"] in {"conda", "unidentified"}
    assert identity["compiler"]
    assert identity["build"]


def test_clean_environment_removes_network_and_python_poison(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "poison")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.invalid")

    environment = tool.clean_subprocess_environment(
        matplotlib_cache=tmp_path / "matplotlib cache"
    )

    assert environment["PIP_NO_INDEX"] == "1"
    assert environment["PIP_NO_CACHE_DIR"] == "1"
    assert environment["NO_PROXY"] == "*"
    assert environment["MPLCONFIGDIR"].endswith("matplotlib cache")
    assert "PIP_INDEX_URL" not in environment
    assert "PYTHONPATH" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "UV_INDEX_URL" not in environment


def test_strict_pip_commands_keep_space_paths_atomic(tmp_path: Path) -> None:
    python = tmp_path / "target python" / "python.exe"
    lock = tmp_path / "locks with spaces" / "dev lock.txt"
    packages = tmp_path / "wheel house" / "packages"
    destination = tmp_path / "download closure"

    install = tool.strict_pip_install_command(python, lock, packages)
    download = tool.strict_pip_download_command(
        python,
        lock,
        packages,
        destination,
        no_deps=True,
    )

    for command in (install, download):
        assert "--no-index" in command
        assert "--require-hashes" in command
        assert "--only-binary=:all:" in command
        assert "--no-cache-dir" in command
        assert str(packages) in command
        assert str(lock) in command
    assert install[0] == str(python)
    assert str(packages) == install[install.index("--find-links") + 1]
    assert str(destination) == download[download.index("--dest") + 1]
    assert "--no-deps" in download


def test_command_recorder_uses_argv_no_shell_and_redacts_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder, evidence = _recorder(tmp_path)
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["argv"] = argv
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    spaced = tmp_path / "directory with spaces" / "input.txt"
    result = recorder.run(
        "fake-phase",
        [sys.executable, "--input", spaced],
        cwd=tmp_path,
        environment={"PIP_NO_INDEX": "1"},
    )

    assert result.stdout == "ok\n"
    assert observed["shell"] is False
    assert observed["check"] is False
    assert observed["argv"][2] == str(spaced)
    assert evidence["phases"] == [
        {
            "phase": "fake-phase",
            "argv": [sys.executable, "--input", "<WORK>/directory with spaces/input.txt"],
            "cwd": "<WORK>",
            "returncode": 0,
            "result": "PASS",
        }
    ]


def test_command_recorder_requires_negative_check_to_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder, evidence = _recorder(tmp_path)

    def unsuccessful(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 1, "", "expected")

    monkeypatch.setattr(tool.subprocess, "run", unsuccessful)
    recorder.run(
        "negative",
        ["pip", "download"],
        cwd=tmp_path,
        environment={},
        expect_failure=True,
    )
    assert evidence["phases"][0]["result"] == "EXPECTED_FAILURE"

    def successful(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tool.subprocess, "run", successful)
    with pytest.raises(tool.AcceptanceFailure, match="unexpectedly succeeded"):
        recorder.run(
            "negative-must-fail",
            ["pip", "download"],
            cwd=tmp_path,
            environment={},
            expect_failure=True,
        )


def test_create_venv_uses_target_interpreter_and_space_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder, evidence = _recorder(tmp_path)
    target = tmp_path / "Python 3.13" / "python.exe"
    target.parent.mkdir()
    target.write_bytes(b"fake")
    venv = tmp_path / "fresh env with spaces"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        Path(argv[-1], "Scripts").mkdir(parents=True)
        Path(argv[-1], "Scripts", "python.exe").write_bytes(b"fake")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    created = tool._create_venv(
        recorder,
        target,
        venv,
        cwd=tmp_path,
        environment={},
        phase="create-test-venv",
    )

    assert created == venv / "Scripts" / "python.exe"
    assert evidence["phases"][0]["argv"][-1] == "<WORK>/fresh env with spaces"
    assert evidence["phases"][0]["argv"][1:4] == ["-I", "-m", "venv"]


def test_run_sample_checks_cli_and_exact_two_level_artifact_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder, evidence = _recorder(tmp_path)
    venv = tmp_path / "runtime env"
    console = tool._console_script(venv)
    console.parent.mkdir(parents=True)
    console.write_bytes(b"fake")
    python = console.parent / "python.exe"
    python.write_bytes(b"fake")
    source = tmp_path / "source"
    for relative in (
        "data/sample/records.jsonl",
        "data/sample/manifest.json",
        "scripts/check_artifacts.py",
    ):
        path = source / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
    output = tmp_path / "output with spaces"
    checker_python = tmp_path / "locked build env" / "python.exe"

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        if len(argv) > 1 and argv[1] == "run":
            generated = Path(argv[argv.index("--output") + 1])
            for relative in tool.SUCCESS_ARTIFACTS:
                path = generated / Path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((relative + "\n").encode("utf-8"))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    hashes = tool._run_sample(
        recorder,
        python,
        venv,
        source,
        output,
        cwd=tmp_path,
        environment={},
        label="sample",
        include_help_and_validate=True,
        checker_python=checker_python,
    )

    assert set(hashes) == set(tool.SUCCESS_ARTIFACTS)
    assert [phase["phase"] for phase in evidence["phases"]] == [
        "sample-module-help",
        "sample-console-help",
        "sample-validate",
        "sample-run",
        "sample-artifact-check",
    ]
    assert evidence["phases"][-1]["argv"][0] == "<WORK>/locked build env/python.exe"


def test_formal_test_runner_rejects_skips_even_with_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    recorder, _ = _recorder(tmp_path)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 0, "10 passed, 1 skipped", "")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)
    with pytest.raises(tool.AcceptanceFailure, match="skip or xfail"):
        tool._run_tests(
            recorder,
            tmp_path / "python.exe",
            tmp_path / "source",
            cwd=tmp_path,
            basetemp=tmp_path / "pytest temp",
            environment={},
            phase="formal-tests",
        )


def test_reachable_network_main_fails_before_any_subprocess_and_records_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    wheelhouse = tmp_path / "wheelhouse"
    source.mkdir()
    wheelhouse.mkdir()
    target_python = tmp_path / "python.exe"
    source_manifest = tmp_path / "source-manifest.json"
    runtime_lock = source / "runtime.txt"
    dev_lock = source / "dev.txt"
    for path in (target_python, source_manifest, runtime_lock, dev_lock):
        path.write_bytes(b"input")
    (wheelhouse / "manifest.json").write_text("{}", encoding="utf-8")
    work = tmp_path / "formal acceptance work"

    monkeypatch.setattr(tool, "load_source_manifest", lambda *args: ())
    monkeypatch.setattr(tool, "validate_lock_pair", lambda *args: ({}, {}))

    failure = tool.AcceptanceFailure(
        "connectivity-precheck",
        "network appears reachable; manually disconnect it before formal acceptance",
    )
    failure.probe_outcomes = [
        {"endpoint": "1.1.1.1:443", "result": "REACHABLE"}
    ]

    def refuse_network(*, timeout: float) -> list[dict[str, object]]:
        assert timeout == 0.1
        raise failure

    monkeypatch.setattr(tool, "probe_connectivity", refuse_network)

    def forbidden_subprocess(*args: object, **kwargs: object) -> object:
        raise AssertionError("no subprocess is allowed while network is reachable")

    monkeypatch.setattr(tool.subprocess, "run", forbidden_subprocess)
    result = tool.main(
        [
            "accept",
            "--python",
            str(target_python),
            "--target",
            "windows-x64-py313",
            "--source",
            str(source),
            "--source-manifest",
            str(source_manifest),
            "--runtime-lock",
            str(runtime_lock),
            "--dev-lock",
            str(dev_lock),
            "--wheelhouse",
            str(wheelhouse),
            "--work-dir",
            str(work),
            "--connectivity-timeout",
            "0.1",
        ]
    )

    assert result == 1
    captured = capsys.readouterr()
    assert "ERROR [connectivity-precheck]" in captured.err
    assert "PASS" not in captured.out
    evidence = json.loads((work / "offline-acceptance.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "FAIL"
    assert evidence["failed_phase"] == "connectivity-precheck"
    assert evidence["network"]["result"] == "REACHABLE"
    assert evidence["phases"] == []


def test_failure_cleanup_never_removes_paths_outside_work(tmp_path: Path) -> None:
    work = tmp_path / "work"
    inside = work / "temporary env"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    (inside / "file").write_text("inside", encoding="utf-8")
    (outside / "file").write_text("outside", encoding="utf-8")

    retained = tool._cleanup_failure(work, [inside, outside])

    assert not inside.exists()
    assert outside.is_dir()
    assert retained == ("outside",)


def test_evidence_writer_is_byte_deterministic(tmp_path: Path) -> None:
    value = {
        "status": "PASS",
        "schema_version": 1,
        "artifacts": {"b": "2", "a": "1"},
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    tool._write_evidence(first, value)
    tool._write_evidence(second, value)

    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")
