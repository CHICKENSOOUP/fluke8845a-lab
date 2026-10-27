"""Filesystem path checks shared by experiment storage and export code."""

from __future__ import annotations

import stat
from pathlib import Path


def is_symlink_or_junction(path: Path) -> bool:
    """Return whether *path* is a symlink or a Windows reparse point.

    Windows directory junctions are reparse points but are not always reported
    as symbolic links by ``stat.S_ISLNK``. Checking the file attributes catches
    them before a caller resolves or opens the path.
    """
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(details, "st_file_attributes", 0) & reparse_point)


def resolve_safe_child_file(parent: str | Path, filename: str) -> Path | None:
    """Resolve an existing regular file directly inside *parent*.

    Reject a linked parent or file, and reject paths whose resolved parent is
    outside the requested experiment directory. This is used by local Web
    exports and chart history so both readers share the same path boundary.
    """
    if not isinstance(parent, (str, Path)) or not isinstance(filename, str):
        return None
    if not filename or filename in {".", ".."} or Path(filename).name != filename:
        return None
    try:
        requested_parent = Path(parent).expanduser()
        if is_symlink_or_junction(requested_parent):
            return None
        resolved_parent = requested_parent.resolve(strict=True)
        if not resolved_parent.is_dir():
            return None
        candidate = resolved_parent / filename
        if is_symlink_or_junction(candidate):
            return None
        resolved = candidate.resolve(strict=True)
        if (
            resolved.parent != resolved_parent
            or resolved.name != filename
            or not resolved.is_file()
        ):
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None
