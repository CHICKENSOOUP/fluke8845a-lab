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
RUNTIME_IMPORT_CHECK = "import numpy, PySide6, serial, pyqtgraph"
APP_MODULES = {
    "desktop": "fluke8845a",
    "web": "fluke8845a.webapp",
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


def _python_is_supported(python: Path, environment: dict[str, str]) -> bool:
    result = subprocess.run(
        [
            str(python), "-c",
            "import sys; raise SystemExit(sys.version_info < (3, 11) or sys.maxsize <= 2**32)",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def prepare_runtime() -> tuple[Path, dict[str, str]] | None:
    """Create a private venv and install app dependencies on first launch."""
    environment = _runtime_environment()
    if not VENV_PYTHON.is_file():
        if sys.version_info < (3, 11) or sys.maxsize <= 2**32:
            print("64-bit Python 3.11 or newer is required to create the app environment.")
            return None
        print("Creating the private Python environment (.venv)...")
        created = _run(
            [sys.executable, "-m", "venv", str(VENV)],
            environment,
        )
        if created.returncode != 0 or not VENV_PYTHON.is_file():
            print("Could not create .venv. Check that this Python installation includes venv support.")
            return None

    if not _python_is_supported(VENV_PYTHON, environment):
        print("The existing .venv uses an unsupported Python version. Rename .venv and retry.")
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
                "--disable-pip-version-check", "-r", str(ROOT / "requirements.txt"),
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
