"""Bootstrap runtime dependencies and launch the Windows source package."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
VENV_PYTHON = VENV / "Scripts" / "python.exe"
RUNTIME_IMPORT_CHECK = (
    "import numpy, PySide6.QtCore, PySide6.QtGui, PySide6.QtWidgets, serial, pyqtgraph"
)
APP_MODULES = {
    "desktop": "fluke8845a",
    "web": "fluke8845a.webapp",
}
PYTHON_LOCKFILES = {
    "3.11": "requirements-lock-python311.txt",
    "3.12": "requirements-lock.txt",
}


def _runtime_environment() -> dict[str, str]:
    """Use this source tree without leaking unrelated Python/Qt installations."""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    for name in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QT_QPA_FONTDIR"):
        environment.pop(name, None)
    return environment


def _run(command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, env=environment, check=False)


def _python_lockfile(python: Path, environment: dict[str, str]) -> Path | None:
    """Return the exact dependency lock matching a supported 64-bit Python."""
    version_probe = (
        "import struct, sys; "
        "print('.'.join(map(str, sys.version_info[:2])) + ':' + "
        "str(struct.calcsize('P') * 8))"
    )
    result = subprocess.run(
        [str(python), "-c", version_probe],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        version, bits = (result.stdout or "").strip().split(":", maxsplit=1)
    except ValueError:
        return None
    lockfile = PYTHON_LOCKFILES.get(version)
    if lockfile is None or bits != "64":
        return None
    return ROOT / lockfile


def prepare_runtime() -> tuple[Path, dict[str, str]] | None:
    """Create a private venv and install app dependencies on first launch."""
    environment = _runtime_environment()
    host_lockfile = _python_lockfile(Path(sys.executable), environment)
    if not VENV_PYTHON.is_file():
        if host_lockfile is None:
            print("64-bit Python 3.11 or 3.12 is required to create the app environment.")
            return None
        print("Creating the private Python environment (.venv)...")
        created = _run(
            [sys.executable, "-m", "venv", str(VENV)],
            environment,
        )
        if created.returncode != 0 or not VENV_PYTHON.is_file():
            print("Could not create .venv. Check that this Python installation includes venv support.")
            return None

    lockfile = _python_lockfile(VENV_PYTHON, environment)
    if lockfile is None:
        print("The existing .venv must use 64-bit Python 3.11 or 3.12. Rename .venv and retry.")
        return None

    check = subprocess.run(
        [str(VENV_PYTHON), "-c", RUNTIME_IMPORT_CHECK],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if check.returncode != 0:
        print("Installing the app's Python dependencies. Internet access is needed on first launch...")
        installed = _run(
            [
                str(VENV_PYTHON), "-m", "pip", "install",
                "--disable-pip-version-check", "-r", str(lockfile),
            ],
            environment,
        )
        if installed.returncode != 0:
            print("Dependency installation failed. Check the internet connection and try again.")
            return None
        check = subprocess.run(
            [str(VENV_PYTHON), "-c", RUNTIME_IMPORT_CHECK],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if check.returncode != 0:
            print("The runtime dependencies are still unavailable after installation.")
            return None

    return VENV_PYTHON, environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", choices=tuple(APP_MODULES), help="desktop or localhost browser interface")
    parser.add_argument("app_args", nargs=argparse.REMAINDER, help="arguments passed to the application")
    args = parser.parse_args(argv)

    runtime = prepare_runtime()
    if runtime is None:
        return 1
    python, environment = runtime
    command = [str(python), "-m", APP_MODULES[args.app], *args.app_args]
    return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
