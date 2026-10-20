"""Helpers for comparing explicit firmware version strings."""

from __future__ import annotations

import re


def compare_firmware(firmware: str, minimum: tuple[int, ...]) -> bool | None:
    """Return None for an unrecognized firmware version (never guess support)."""
    match = re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", firmware.strip())
    if not match:
        return None
    found = tuple(int(piece) for piece in match.group().split("."))
    # The 8845A's documented gates use either a two-part generation (2.0) or
    # a four-part build (1.0.700.18). A three-part value such as 1.0.0 is
    # ambiguous/date-like and must not be treated as a confirmed firmware.
    if len(found) not in (2, 4):
        return None
    size = max(len(found), len(minimum))
    return found + (0,) * (size - len(found)) >= minimum + (0,) * (size - len(minimum))
