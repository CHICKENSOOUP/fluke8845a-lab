"""Path validation helpers for files copied into public release archives."""

from __future__ import annotations

from pathlib import Path


def ensure_path_within(path: Path, root: Path) -> Path:
    """Reject symlinks and paths resolving outside the intended archive root."""
    root = Path(root).resolve(strict=True)
    path = Path(path)
    try:
        lexical_relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"release file is outside its archive root: {path}") from exc

    current = root
    for part in lexical_relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"release archives cannot include symbolic links: {path}")

    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"release file resolves outside its archive root: {path}") from exc
    return resolved


def ensure_regular_file_within(path: Path, root: Path) -> Path:
    """Validate a regular file destined for a public archive."""
    resolved = ensure_path_within(path, root)
    if not resolved.is_file():
        raise ValueError(f"release archive entry is not a regular file: {path}")
    return resolved
