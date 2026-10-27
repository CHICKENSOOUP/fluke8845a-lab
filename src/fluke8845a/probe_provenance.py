"""Capture the code or executable identity used by a real hardware probe."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


BUILD_PROVENANCE_NAME = "BUILD-PROVENANCE.json"
BUILD_IDENTITY_NAME = "BUILD-IDENTITY.json"
PROBE_SOURCE_FILES = (
    "src/fluke8845a/__init__.py",
    "src/fluke8845a/__main__.py",
    "src/fluke8845a/gui.py",
    "src/fluke8845a/app.py",
    "src/fluke8845a/worker.py",
    "src/fluke8845a/storage.py",
    "src/fluke8845a/scpi.py",
    "src/fluke8845a/modes.py",
    "src/fluke8845a/analysis.py",
    "src/fluke8845a/build_identity.py",
    "src/fluke8845a/filter_policy.py",
    "src/fluke8845a/history_import.py",
    "src/fluke8845a/mock.py",
    "src/fluke8845a/path_security.py",
    "src/fluke8845a/plot_history.py",
    "src/fluke8845a/share_export.py",
    "src/fluke8845a/spreadsheet_safety.py",
    "src/fluke8845a/versioning.py",
    "src/fluke8845a/state.py",
    "src/fluke8845a/webapp.py",
    "src/fluke8845a/webservice.py",
    "src/fluke8845a/probe_provenance.py",
)
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")
_GIT_COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def capture_runtime_source_identity(
    *,
    executable_path: Path | None = None,
    source_root: Path | None = None,
    package_file: Path | None = None,
) -> dict:
    """Snapshot source-file hashes or validate the packaged running executable.

    Source hashes are taken at probe start and checked again before its report
    is finalized. Frozen builds bind the report to the adjacent build
    provenance marker and the actual executable bytes.
    """
    entrypoint = Path(executable_path or sys.argv[0]).expanduser().resolve()
    marker_path = next(
        (
            entrypoint.parent / marker_name
            for marker_name in (BUILD_PROVENANCE_NAME, BUILD_IDENTITY_NAME)
            if (entrypoint.parent / marker_name).is_file()
        ),
        None,
    )
    if marker_path is not None:
        document = json.loads(marker_path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{marker_path.name}必须是 JSON 对象")
        source_digest = document.get("source_tree_sha256")
        head_commit = document.get("head_commit")
        dirty = document.get("worktree_dirty")
        executables = document.get("executables")
        desktop = executables.get("desktop") if isinstance(executables, dict) else None
        expected_executable_digest = desktop.get("sha256") if isinstance(desktop, dict) else None
        if (
            document.get("format_version") != 1
            or not isinstance(source_digest, str)
            or not _SHA256_PATTERN.fullmatch(source_digest)
            or not isinstance(head_commit, str)
            or not _GIT_COMMIT_PATTERN.fullmatch(head_commit)
            or not isinstance(dirty, bool)
            or not isinstance(expected_executable_digest, str)
            or not _SHA256_PATTERN.fullmatch(expected_executable_digest)
        ):
            raise ValueError(f"冻结版 {marker_path.name} 格式无效")
        actual_executable_digest = sha256_file(entrypoint)
        if actual_executable_digest.upper() != expected_executable_digest.upper():
            raise ValueError(f"运行中的EXE与{marker_path.name}摘要不一致")
        return {
            "kind": "frozen_executable",
            "head_commit": head_commit.lower(),
            "worktree_dirty": dirty,
            "source_tree_sha256": source_digest.upper(),
            "executable_sha256": actual_executable_digest,
        }

    module_path = Path(package_file or __file__).resolve()
    root = Path(source_root or module_path.parents[2]).resolve()
    hashes = {}
    for relative_path in PROBE_SOURCE_FILES:
        source_path = root / Path(relative_path)
        if not source_path.is_file():
            # Installed wheels place the same modules directly in
            # site-packages/fluke8845a rather than under a source/ directory.
            source_path = module_path.parent / Path(relative_path).name
        hashes[relative_path] = sha256_file(source_path)
    return {"kind": "source_tree_files", "source_file_sha256": hashes}
