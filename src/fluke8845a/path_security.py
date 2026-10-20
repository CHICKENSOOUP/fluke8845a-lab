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
