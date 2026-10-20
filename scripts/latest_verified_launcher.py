"""Create a stable local launcher for the last verified Windows build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


LAUNCHER_NAME = "Start-Latest-Verified-Fluke8845A.bat"
BUILD_IDENTITY_NAME = "BUILD-IDENTITY.json"
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_SAFE_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


def _validated_relative_executable(dist_root: Path, executable: Path) -> tuple[Path, str, str]:
    root = Path(dist_root).resolve(strict=True)
    exe = Path(executable).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("distribution root must be a directory")
    if not exe.is_file() or exe.name.casefold() != "fluke8845a.exe":
        raise ValueError("executable must be the packaged Fluke8845A.exe")

    try:
        relative = exe.relative_to(root)
    except ValueError as exc:
        raise ValueError("executable must be inside the distribution directory") from exc
    if exe.parent.name.casefold() != "fluke8845a.dist":
        raise ValueError("executable must be inside Fluke8845A.dist")
    if len(relative.parts) not in (2, 3):
        raise ValueError("executable must use the standard or one-level side-by-side layout")
    if any(not _SAFE_COMPONENT_PATTERN.fullmatch(part) for part in relative.parts):
        raise ValueError("executable path contains an unsafe batch-file path component")

    identity_path = exe.parent / BUILD_IDENTITY_NAME
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("packaged executable has no readable build identity") from exc

    source_digest = identity.get("source_tree_sha256") if isinstance(identity, dict) else None
    executables = identity.get("executables") if isinstance(identity, dict) else None
    desktop = executables.get("desktop") if isinstance(executables, dict) else None
    expected_digest = desktop.get("sha256") if isinstance(desktop, dict) else None
    if not isinstance(source_digest, str) or not _SHA256_PATTERN.fullmatch(source_digest):
        raise ValueError("build identity has no valid source-tree SHA-256")
    if not isinstance(expected_digest, str) or not _SHA256_PATTERN.fullmatch(expected_digest):
        raise ValueError("build identity has no valid desktop executable SHA-256")

    actual_digest = hashlib.sha256()
    with exe.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            actual_digest.update(chunk)
    if actual_digest.hexdigest().casefold() != expected_digest.casefold():
        raise ValueError("desktop executable SHA-256 does not match its build identity")

    relative_exe = "\\".join(relative.parts)
    relative_dir = "\\".join(relative.parts[:-1])
    return root, relative_exe, relative_dir


def _launcher_text(relative_exe: str, relative_dir: str) -> bytes:
    lines = (
        "@echo off",
        "setlocal DisableDelayedExpansion",
        f'set "APP_EXE=%~dp0{relative_exe}"',
        f'set "APP_DIR=%~dp0{relative_dir}"',
        'if not exist "%APP_EXE%" (',
        '  echo The latest verified Fluke 8845A build is missing:',
        '  echo "%APP_EXE%"',
        "  pause",
        "  exit /b 2",
        ")",
        'start "" /D "%APP_DIR%" "%APP_EXE%"',
        "exit /b 0",
        "",
    )
    return "\r\n".join(lines).encode("ascii")


def create_latest_launcher(dist_root: Path, executable: Path) -> Path:
    """Atomically point the stable launcher at a hash-verified packaged EXE."""
    root, relative_exe, relative_dir = _validated_relative_executable(dist_root, executable)
    launcher = root / LAUNCHER_NAME
    payload = _launcher_text(relative_exe, relative_dir)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{LAUNCHER_NAME}.",
            suffix=".tmp",
            dir=root,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, launcher)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return launcher


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-root", required=True, type=Path)
    parser.add_argument("--executable", required=True, type=Path)
    args = parser.parse_args()
    try:
        launcher = create_latest_launcher(args.dist_root, args.executable)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Could not update latest verified launcher: {exc}\n")
    print(f"Updated {launcher}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
