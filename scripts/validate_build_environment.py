"""Check that a Windows build environment matches its pinned requirements."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import re
import struct
import sys
from pathlib import Path
from typing import Mapping


_PINNED_REQUIREMENT = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)$"
)


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def parse_lock_contents(text: str) -> dict[str, str]:
    """Parse a pip lock containing exact name==version pins and comments."""
    locked: dict[str, str] = {}
    original_names: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        match = _PINNED_REQUIREMENT.fullmatch(line)
        if match is None:
            raise ValueError(
                f"requirements lock line {line_number} must use an exact name==version pin"
            )
        original_name, version = match.groups()
        name = _normalized_name(original_name)
        if name in locked:
            raise ValueError(
                f"duplicate requirement in lock: {original_names[name]} and {original_name}"
            )
        original_names[name] = original_name
        locked[name] = version
    if not locked:
        raise ValueError("requirements lock contains no pinned dependencies")
    return locked


def installed_distribution_versions() -> dict[str, str]:
    """Return installed Python distributions keyed by canonical package name."""
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not name or not version:
            continue
        normalized = _normalized_name(name)
        if normalized in installed and installed[normalized] != version:
            raise ValueError(f"multiple installed versions found for {name}")
        installed[normalized] = version
    return installed


def validate_lock_contents(
    text: str,
    *,
    installed: Mapping[str, str],
) -> dict[str, dict[str, str]]:
    """Require every lock pin to be installed exactly and report extra packages."""
    locked = parse_lock_contents(text)
    normalized_installed: dict[str, str] = {}
    for name, version in installed.items():
        normalized = _normalized_name(name)
        if normalized in normalized_installed and normalized_installed[normalized] != version:
            raise ValueError(f"multiple installed versions found for {name}")
        normalized_installed[normalized] = str(version)

    for name, expected_version in locked.items():
        actual_version = normalized_installed.get(name)
        if actual_version is None:
            raise ValueError(f"missing locked dependency: {name}")
        if actual_version != expected_version:
            raise ValueError(
                f"version mismatch for {name}: locked {expected_version}, installed {actual_version}"
            )

    return {
        "locked": {name: locked[name] for name in sorted(locked)},
        "extra": {
            name: normalized_installed[name]
            for name in sorted(normalized_installed.keys() - locked.keys())
        },
    }


def validate_lock_file(path: Path) -> dict[str, dict[str, str]]:
    return validate_lock_contents(
        Path(path).read_text(encoding="utf-8-sig"),
        installed=installed_distribution_versions(),
    )


def validate_runtime(
    *,
    implementation: str,
    version: tuple[int, ...],
    system: str,
    machine: str,
    bits: int,
) -> None:
    """Limit this lock to the tested CPython 3.12 Windows x64 build family."""
    if implementation != "CPython" or len(version) < 2 or version[:2] != (3, 12):
        raise ValueError("Windows build dependencies require CPython 3.12.x")
    if system != "Windows" or machine.casefold() not in {"amd64", "x86_64", "x64"}:
        raise ValueError("Windows build dependencies require Windows x64")
    if isinstance(bits, bool) or bits != 64:
        raise ValueError("Windows build dependencies require a 64-bit Python runtime")


def validate_current_runtime() -> None:
    validate_runtime(
        implementation=platform.python_implementation(),
        version=tuple(sys.version_info[:3]),
        system=platform.system(),
        machine=platform.machine(),
        bits=struct.calcsize("P") * 8,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True, help="exact-pinned requirements lock file")
    args = parser.parse_args(argv)
    try:
        validate_current_runtime()
        report = validate_lock_file(args.lock)
    except (OSError, ValueError) as exc:
        print(f"Build environment validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
