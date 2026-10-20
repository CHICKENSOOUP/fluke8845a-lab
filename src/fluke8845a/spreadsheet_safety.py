"""Spreadsheet-safe CSV cell handling for derived exports."""

from __future__ import annotations

import re


_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def is_numeric_literal(value: str) -> bool:
    """Return whether a CSV cell is a plain numeric literal, not a formula."""
    return bool(_NUMBER_RE.fullmatch(value.strip()))


def spreadsheet_safe_cell(value: str, *, preserve_numeric_literal: bool = False) -> str:
    """Prefix formula-like text with an apostrophe in a derived CSV cell.

    Numeric columns can opt to preserve signed numeric literals such as -1.25
    and +2.5; expressions such as -1+2 are still treated as formulas.
    """
    first_visible = next(
        (char for char in value if not (char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F)),
        "",
    )
    if first_visible not in {"=", "+", "-", "@"}:
        return value
    if (
        preserve_numeric_literal
        and first_visible in {"+", "-"}
        and is_numeric_literal(value)
    ):
        return value
    return "'" + value
