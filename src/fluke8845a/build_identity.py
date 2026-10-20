"""Read a build marker so desktop windows can identify their exact source build."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


BUILD_IDENTITY_NAME = "BUILD-IDENTITY.json"
BUILD_PROVENANCE_NAME = "BUILD-PROVENANCE.json"
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


def desktop_build_label(executable_path: Path, version: str, *, frozen: bool) -> str:
    """Return a concise, truthful version/build label for the desktop title bar."""
    version_label = f"v{str(version).strip() or 'unknown'}"
    if not frozen:
        return f"{version_label} · 源码运行"

    executable = Path(executable_path).resolve()
    identity = None
    for filename in (BUILD_IDENTITY_NAME, BUILD_PROVENANCE_NAME):
        marker = executable.parent / filename
        if not marker.is_file():
            continue
        try:
            identity = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return f"{version_label} · 构建标识缺失"
        break
    if not isinstance(identity, dict):
        return f"{version_label} · 构建标识缺失"

    source_digest = identity.get("source_tree_sha256")
    executables = identity.get("executables")
    desktop = executables.get("desktop") if isinstance(executables, dict) else None
    expected_exe_digest = desktop.get("sha256") if isinstance(desktop, dict) else None
    if (
        not isinstance(source_digest, str)
        or not _SHA256_PATTERN.fullmatch(source_digest)
        or not isinstance(expected_exe_digest, str)
        or not _SHA256_PATTERN.fullmatch(expected_exe_digest)
    ):
        return f"{version_label} · 构建标识缺失"

    actual_digest = hashlib.sha256()
    try:
        with executable.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                actual_digest.update(chunk)
    except OSError:
        return f"{version_label} · 构建标识缺失"
    if actual_digest.hexdigest().upper() != expected_exe_digest.upper():
        return f"{version_label} · 构建标识不匹配"
    return f"{version_label} · 构建 {source_digest[:8].upper()}"
