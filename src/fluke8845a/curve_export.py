"""Stream simple and detailed curve CSVs from one raw acquisition snapshot.

The detailed export preserves raw fields verbatim; text columns may contain
spreadsheet formulas. Call from a worker thread, never from the GUI thread.
"""
from __future__ import annotations

import csv
import io
import math
import os
import tempfile
from collections import deque
from pathlib import Path

from .storage import _LimitedBinaryReader, complete_csv_prefix_size

AVERAGE_FIELD = "moving_average_value_si"
AVERAGE_WINDOW_FIELD = "moving_average_window_points"
MAX_AVERAGE_WINDOW = 100_000
SIMPLE_FIELDS = ("timestamp_local", "elapsed_s", "value_si", AVERAGE_FIELD, "unit_si")


def detailed_curve_path(destination_path: Path | str) -> Path:
    """Return the companion file name for the full-detail export."""
    path = Path(destination_path)
    return path.with_name(f"{path.stem}_detailed{path.suffix}")


def _publish_without_overwrite(staged: Path, destination: Path) -> None:
    try:
        if os.name == "nt":
            os.rename(staged, destination)
        else:
            os.link(staged, destination)
    except FileExistsError as exc:
        raise FileExistsError("导出目标已存在，请另选文件名") from exc


def write_curve_csv(source_path: Path | str, destination_path: Path | str, window: int) -> int:
    """Export a simple CSV and a full-detail companion from one snapshot.

    Return the number of source rows.

    The moving average uses up to ``window`` consecutive valid samples; an
    invalid sample or nonconsecutive sample index starts a new segment. Source
    records and SI values are preserved; early segment points use the number
    of available samples. Existing outputs are never replaced.
    """
    if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= MAX_AVERAGE_WINDOW:
        raise ValueError("平均点数必须在 1 到 100000 之间")
    source_path, destination_path = Path(source_path), Path(destination_path)
    detailed_path = detailed_curve_path(destination_path)
    for target in (destination_path, detailed_path):
        if source_path.resolve() == target.resolve():
            raise ValueError("导出目标不能覆盖原始测量 CSV")
        if target.exists():
            if os.path.samefile(source_path, target):
                raise ValueError("导出目标不能覆盖原始测量 CSV")
            raise FileExistsError("导出目标已存在，请另选文件名")
    source_info = source_path.stat()
    prefix_size = complete_csv_prefix_size(source_path)
    if prefix_size == 0:
        raise ValueError("原始测量 CSV 没有完整表头")
    current_info = source_path.stat()
    if (current_info.st_dev, current_info.st_ino) != (source_info.st_dev, source_info.st_ino) or current_info.st_size < prefix_size:
        raise OSError("原始测量 CSV 在导出时被替换或截断")
    staged_path = None
    staged_detailed_path = None
    published_detail_info = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".curve-export-", suffix=".tmp", dir=destination_path.parent, delete=False) as staged:
            staged_path = Path(staged.name)
        with tempfile.NamedTemporaryFile(prefix=".curve-export-", suffix=".tmp", dir=destination_path.parent, delete=False) as staged:
            staged_detailed_path = Path(staged.name)
        with source_path.open("rb") as raw:
            opened_info = os.fstat(raw.fileno())
            if (opened_info.st_dev, opened_info.st_ino) != (source_info.st_dev, source_info.st_ino) or opened_info.st_size < prefix_size:
                raise OSError("原始测量 CSV 在导出时被替换或截断")
            with io.TextIOWrapper(io.BufferedReader(_LimitedBinaryReader(raw, prefix_size)), encoding="utf-8-sig", newline="") as source, staged_path.open("w", encoding="utf-8-sig", newline="") as destination, staged_detailed_path.open("w", encoding="utf-8-sig", newline="") as detailed:
                reader = csv.DictReader(source, strict=True)
                fields = reader.fieldnames
                required = {"sample_index", "elapsed_s", "value_si", "status"}
                if not fields or len(set(fields)) != len(fields) or not required.issubset(fields) or AVERAGE_FIELD in fields or AVERAGE_WINDOW_FIELD in fields:
                    raise ValueError("原始测量 CSV 表头无效或缺少必需字段")
                writer = csv.DictWriter(destination, fieldnames=SIMPLE_FIELDS, extrasaction="ignore", lineterminator="\r\n")
                detail_writer = csv.DictWriter(detailed, fieldnames=[*fields, AVERAGE_FIELD, AVERAGE_WINDOW_FIELD], lineterminator="\r\n")
                writer.writeheader()
                detail_writer.writeheader()
                recent = deque()
                total = 0.0
                previous_index = None
                count = 0
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("原始测量 CSV 行的列数与表头不一致")
                    try:
                        sample_index = int(row["sample_index"])
                        value = float(row["value_si"])
                        elapsed = float(row["elapsed_s"])
                        valid = row["status"] == "ok" and math.isfinite(value) and math.isfinite(elapsed)
                    except (TypeError, ValueError, OverflowError):
                        sample_index = None
                        valid = False
                    if not valid or (previous_index is not None and sample_index != previous_index + 1):
                        recent.clear()
                        total = 0.0
                    average = ""
                    if valid:
                        recent.append(value)
                        total += value
                        if len(recent) > window:
                            total -= recent.popleft()
                        mean = total / len(recent)
                        if not math.isfinite(mean):
                            raise ValueError("移动平均超出数值范围")
                        average = repr(mean)
                    row[AVERAGE_FIELD] = average
                    row[AVERAGE_WINDOW_FIELD] = str(window)
                    simple_row = dict(row)
                    if not valid:
                        simple_row["value_si"] = ""
                    writer.writerow(simple_row)
                    detail_writer.writerow(row)
                    previous_index = sample_index
                    count += 1
                if os.fstat(raw.fileno()).st_size < prefix_size:
                    raise OSError("原始测量 CSV 在导出时被截断")
                destination.flush()
                os.fsync(destination.fileno())
                detailed.flush()
                os.fsync(detailed.fileno())
        _publish_without_overwrite(staged_detailed_path, detailed_path)
        published_detail_info = detailed_path.stat()
        try:
            _publish_without_overwrite(staged_path, destination_path)
        except Exception:
            # Remove only the file we published; another process may have replaced it.
            try:
                current = detailed_path.stat()
                if (current.st_dev, current.st_ino) == (published_detail_info.st_dev, published_detail_info.st_ino):
                    detailed_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return count
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        if staged_detailed_path is not None:
            staged_detailed_path.unlink(missing_ok=True)
