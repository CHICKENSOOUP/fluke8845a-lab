"""Write archive candidates beside their destination and publish them atomically."""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def atomic_archive_path(destination: Path) -> Iterator[Path]:
    """Yield a temporary ZIP path and replace the destination only on success.

    Keeping the candidate in the destination directory makes ``os.replace`` an
    atomic same-volume operation. Any exception leaves a previously verified
    archive untouched and removes the uncommitted candidate.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.",
        suffix=".tmp.zip",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not remove temporary archive candidate %s: %s",
                temporary,
                exc,
            )
