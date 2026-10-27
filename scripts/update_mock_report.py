"""Add the packaged executable hash to a frozen Mock GUI report safely."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def update_mock_report_hash(report_path: Path, executable_path: Path) -> None:
    """Set the EXE hash without changing the UTF-8 title or adding a BOM."""
    report_path = Path(report_path)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"unable to read Mock report as UTF-8 JSON: {report_path}") from exc
    if not isinstance(report, dict):
        raise ValueError("Mock report root must be a JSON object")

    report["executable_sha256"] = file_sha256(Path(executable_path))
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        update_mock_report_hash(args.report, args.executable)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"unable to update Mock report: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
