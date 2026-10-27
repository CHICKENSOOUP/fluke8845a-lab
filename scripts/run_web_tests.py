"""Run the browser-independent Web frontend tests with a supported Node.js."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
NODE_TEST_FILES = (
    "tests/iv_frontend.test.cjs",
    "tests/web_frontend.test.cjs",
    "tests/web_history_worker.test.cjs",
    "tests/web_history_import_ui.test.cjs",
    "tests/web_history_stream.test.cjs",
    "tests/web_plot_full_history.test.cjs",
)
WEB_TEST_TIMEOUT_SECONDS = 300
NODE_VERSION_TIMEOUT_SECONDS = 15
_NODE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:\b|$)")


def _parse_node_version(output: str) -> tuple[int, int, int] | None:
    match = _NODE_VERSION.match(output.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def run_web_tests(
    *,
    node_executable: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    executable = node_executable or shutil.which("node")
    if not executable:
        raise RuntimeError("Node.js 22 or newer is required to run the Web frontend tests.")

    version_command = [executable, "--version"]
    try:
        version_result = runner(
            version_command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=NODE_VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Node.js version probe exceeded {NODE_VERSION_TIMEOUT_SECONDS} seconds."
        ) from exc
    version_text = (version_result.stdout or "").strip()
    version = _parse_node_version(version_text)
    if version_result.returncode != 0 or version is None:
        detail = (version_result.stderr or version_text or "no version output").strip()
        raise RuntimeError(f"Unable to read the Node.js version: {detail}")
    if version[0] < 22:
        raise RuntimeError(
            f"Node.js 22 or newer is required to run the Web frontend tests; found {version_text}."
        )

    print(f"Running Web frontend tests with Node.js {version_text}.", flush=True)
    try:
        result = runner(
            [executable, "--test", *NODE_TEST_FILES],
            cwd=ROOT,
            check=False,
            timeout=WEB_TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Web frontend JavaScript tests exceeded {WEB_TEST_TIMEOUT_SECONDS} seconds."
        ) from exc
    return int(result.returncode)


def main() -> int:
    try:
        return run_web_tests()
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
