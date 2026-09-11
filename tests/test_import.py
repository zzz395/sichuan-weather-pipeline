from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _installed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def test_package_and_cli_import() -> None:
    import sichuan_weather
    import sichuan_weather.cli

    assert sichuan_weather.__version__ == "0.1.0"
    assert sichuan_weather.__file__ is not None
    assert Path(sichuan_weather.__file__).resolve().is_relative_to(
        Path(sys.prefix).resolve()
    )


def test_imports_have_no_application_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    injected_path = tmp_path / "injected source"
    injected_package = injected_path / "sichuan_weather"
    injected_package.mkdir(parents=True)
    (injected_package / "__init__.py").write_text(
        "raise RuntimeError('source checkout masked installed package')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(injected_path))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "invalid python home"))
    process_cwd = tmp_path / "import probe cwd"
    process_cwd.mkdir()
    probe = r'''
import json
import os
from pathlib import Path
import sys

blocked_events = {
    "os.chdir",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.truncate",
    "shutil.copyfile",
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
}

def reject_side_effects(event, args):
    if event in blocked_events:
        raise RuntimeError(f"blocked audit event during import: {event}")
    if event == "open":
        mode = args[1]
        flags = args[2]
        if isinstance(mode, str) and any(character in mode for character in "wax+"):
            raise RuntimeError(f"blocked file write during import: {args[0]}")
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR):
            raise RuntimeError(f"blocked file write during import: {args[0]}")

sys.addaudithook(reject_side_effects)
before_cwd = os.getcwd()
before_entries = sorted(os.listdir("."))
import sichuan_weather
import sichuan_weather.cli
package_file = Path(sichuan_weather.__file__).resolve()
print(json.dumps({
    "cwd_unchanged": os.getcwd() == before_cwd,
    "entries_unchanged": sorted(os.listdir(".")) == before_entries,
    "package_inside_prefix": package_file.is_relative_to(Path(sys.prefix).resolve()),
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=process_cwd,
        env=_installed_environment(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "cwd_unchanged": True,
        "entries_unchanged": True,
        "package_inside_prefix": True,
    }
