"""Background-friendly parsing for historical measurement CSV files."""

from __future__ import annotations

import csv
import math
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .analysis import RunningStats
from .modes import Mode, descriptor
from .plot_history import downsample_plot_data
from .storage import complete_csv_prefix_size

_HISTORY_RANGE_INDEX_STRIDE = 256
_HISTORY_RANGE_INDEX_MAX_CHECKPOINTS = 8192


def _reject_duplicate_csv_columns(fieldnames: list[str] | tuple[str, ...] | None) -> None:
    if not fieldnames:
        return
    seen: set[str] = set()
    duplicates: set[str] = set()
    for fieldname in fieldnames:
        if fieldname in seen:
            duplicates.add(fieldname)
        seen.add(fieldname)
    if duplicates:
        names = "、".join(sorted(name or "<空列名>" for name in duplicates))
        raise ValueError(f"CSV表头包含重复列：{names}。请修正重复列后重试。")


@dataclass(frozen=True, slots=True)
class _HistoryCsvRangeIndex:
    path: Path
    signature: tuple[int, int, int, int]
    complete_prefix_bytes: int
    fieldnames: tuple[str, ...]
    checkpoints: tuple[tuple[float, int, bool], ...]
    monotonic_elapsed: bool


class _BinaryCsvLines:
    """Decode physical CSV lines while preserving exact seekable byte offsets."""

    def __init__(self, stream, end_offset: int, *, strip_bom: bool = False) -> None:
        self.stream = stream
        self.end_offset = end_offset
        self.strip_bom = strip_bom

    def __iter__(self):
        return self

    def __next__(self) -> str:
        position = self.stream.tell()
        if position >= self.end_offset:
            raise StopIteration
        line = self.stream.readline(self.end_offset - position)
        if not line:
            raise StopIteration
        encoding = "utf-8-sig" if self.strip_bom else "utf-8"
        self.strip_bom = False
        return line.decode(encoding)


def _history_file_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _has_final_line_terminator(path: Path, expected_size: int) -> bool:
    if expected_size <= 0:
        return True
    with path.open("rb") as stream:
        stream.seek(expected_size - 1)
        return stream.read(1) in (b"\r", b"\n")


def load_history_csv(
    path: str | Path, fallback_mode: Mode, max_plot_points: int,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Validate and downsample a historical CSV without changing its source files."""
    source = Path(path)
    source_signature = _history_file_signature(source)
    cancelled = is_cancelled or (lambda: False)

    def check_cancelled() -> None:
        if cancelled():
            raise InterruptedError("历史 CSV 加载已取消")

    complete_prefix_bytes = complete_csv_prefix_size(
        source, allow_complete_eof_record=True, check_cancelled=check_cancelled,
    )
    has_final_line_terminator = _has_final_line_terminator(source, source_signature[2])
    if _history_file_signature(source) != source_signature:
        raise ValueError("CSV在检查完整记录边界时发生变化，请停止采集后重试")

    quality = {"row_count": 0, "statuses": {}, "invalid_numeric": 0}
    stats = RunningStats()
    valid_count = 0
    first_elapsed = last_elapsed = minimum_elapsed = maximum_elapsed = last_value = None
    mode_values: set[str] = set()
    unit_values: set[str] = set()
    warnings: list[str] = []
    if complete_prefix_bytes < source_signature[2]:
        warnings.append(
            f"{source.name} 尾部有 {source_signature[2] - complete_prefix_bytes} 字节未组成完整记录；"
            f"仅加载前 {complete_prefix_bytes} 字节的完整记录，源文件未修改。"
        )
    elif source_signature[2] and not has_final_line_terminator:
        warnings.append(
            f"{source.name} 末行没有 CR/LF 行结束符，已按 CSV 语法加载；"
            "无法判断该行是否曾被截断，源文件未修改。"
        )
    measurement_experiment_id: str | None = None
    measurement_id_missing = False
    measurement_id_mixed = False
    required = {"elapsed_s", "value_si", "status"}

    def rows():
        with source.open("rb") as stream:
            reader = csv.DictReader(
                _BinaryCsvLines(stream, complete_prefix_bytes, strip_bom=True),
            )
            if not reader.fieldnames:
                raise ValueError("CSV没有表头；可接受格式至少包含 elapsed_s、value_si、status 列")
            _reject_duplicate_csv_columns(reader.fieldnames)
            missing = required - set(reader.fieldnames)
            if missing:
                missing_text = "、".join(sorted(missing))
                raise ValueError(
                    f"CSV缺少列：{missing_text}。可接受格式至少包含 elapsed_s、value_si、status 列；"
                    "请使用实验原始 measurements.csv，或补齐这些列后重试。"
                )
            yield from reader

    for row in rows():
        check_cancelled()
        quality["row_count"] += 1
        row_experiment_id = str(row.get("experiment_id", "")).strip()
        if not row_experiment_id:
            measurement_id_missing = True
        elif measurement_experiment_id is None:
            measurement_experiment_id = row_experiment_id
        elif row_experiment_id != measurement_experiment_id:
            measurement_id_mixed = True
        status = str(row.get("status", "")).strip()
        if status != "ok":
            quality["statuses"][status or "空状态"] = quality["statuses"].get(status or "空状态", 0) + 1
            continue
        try:
            value = float(row["value_si"])
            elapsed = float(row["elapsed_s"])
        except (TypeError, ValueError, OverflowError):
            quality["invalid_numeric"] += 1
            continue
        if not math.isfinite(value) or not math.isfinite(elapsed):
            quality["invalid_numeric"] += 1
            continue
        first_elapsed = elapsed if first_elapsed is None else first_elapsed
        last_elapsed, last_value = elapsed, value
        minimum_elapsed = elapsed if minimum_elapsed is None else min(minimum_elapsed, elapsed)
        maximum_elapsed = elapsed if maximum_elapsed is None else max(maximum_elapsed, elapsed)
        stats.add(value)
        valid_count += 1
        mode = str(row.get("mode", "")).strip()
        unit = str(row.get("unit_si", "")).strip()
        if mode:
            mode_values.add(mode)
        if unit:
            unit_values.add(unit)

    if not valid_count:
        status_summary = "、".join(
            f"{status} {count} 行" for status, count in sorted(quality["statuses"].items())
        ) or "没有 status=ok 的行"
        numeric_summary = f"；数值列无效 {quality['invalid_numeric']} 行" if quality["invalid_numeric"] else ""
        raise ValueError(
            f"CSV没有有效读数（共 {quality['row_count']} 行，{status_summary}{numeric_summary}）。"
            "检查 status 是否为 ok、elapsed_s/value_si 是否为有限数字，并确认文件包含表头。"
        )
    if len(mode_values) > 1:
        raise ValueError(f"CSV混合测量模式：{'、'.join(sorted(mode_values))}；请按单一模式拆分文件后重试。")
    if len(unit_values) > 1:
        raise ValueError(f"CSV混合单位：{'、'.join(sorted(unit_values))}；请按单一单位拆分文件后重试。")
    mode_value = next(iter(mode_values), fallback_mode.value)
    if mode_value not in {item.value for item in Mode}:
        supported = "、".join(item.value for item in Mode)
        raise ValueError(f"CSV测量模式 {mode_value} 不受支持；当前可识别模式：{supported}。")
    spec = descriptor(Mode(mode_value))
    if unit_values and next(iter(unit_values)) != spec.unit_si:
        raise ValueError(
            f"CSV单位 {next(iter(unit_values))} 与测量模式 {mode_value} 的单位 {spec.unit_si} 不匹配；"
            "请核对原始 CSV 表头和值。"
        )
    history_events: list[tuple[float, str]] = []
    event_path = source.with_name("events.csv")
    if event_path.is_file():
        try:
            event_signature = _history_file_signature(event_path)
            event_complete_prefix_bytes = complete_csv_prefix_size(
                event_path, allow_complete_eof_record=True,
                check_cancelled=check_cancelled,
            )
            event_has_final_line_terminator = _has_final_line_terminator(
                event_path, event_signature[2],
            )
            if _history_file_signature(event_path) != event_signature:
                raise ValueError("events.csv 在检查完整记录边界时发生变化")
            if event_complete_prefix_bytes < event_signature[2]:
                warnings.append(
                    f"events.csv 尾部有 {event_signature[2] - event_complete_prefix_bytes} 字节未组成完整记录；"
                    "未完成部分不会关联，源文件未修改。"
                )
            elif event_signature[2] and not event_has_final_line_terminator:
                warnings.append(
                    "events.csv 末行没有 CR/LF 行结束符，已按 CSV 语法加载；"
                    "无法判断该行是否曾被截断，源文件未修改。"
                )
            with event_path.open("rb") as stream:
                event_reader = csv.DictReader(
                    _BinaryCsvLines(stream, event_complete_prefix_bytes, strip_bom=True),
                )
                event_fields = set(event_reader.fieldnames or ())
                if not {"elapsed_s", "event", "experiment_id"}.issubset(event_fields):
                    warnings.append("events.csv 缺少 experiment_id/elapsed_s/event 列，未自动关联事件")
                elif measurement_id_missing or measurement_id_mixed or not measurement_experiment_id:
                    warnings.append(
                        "测量 CSV 的 experiment_id 缺失或不一致，无法验证配套事件；未自动关联 events.csv"
                    )
                else:
                    has_event_rows = False
                    missing_event_id = False
                    event_ids_match = True
                    candidate_events: list[tuple[float, str]] = []
                    for row in event_reader:
                        check_cancelled()
                        has_event_rows = True
                        candidate = str(row.get("experiment_id", "")).strip()
                        if not candidate:
                            missing_event_id = True
                        elif candidate != measurement_experiment_id:
                            event_ids_match = False
                        event_text = str(row.get("event", "")).strip()
                        try:
                            event_elapsed = float(row.get("elapsed_s", ""))
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if (
                            event_text
                            and math.isfinite(event_elapsed)
                            and minimum_elapsed <= event_elapsed <= maximum_elapsed
                        ):
                            candidate_events.append((event_elapsed, event_text))

                    event_source_changed = _history_file_signature(event_path) != event_signature
                    if event_source_changed:
                        warnings.append("events.csv 在读取期间发生变化，未关联事件")
                    if missing_event_id:
                        warnings.append("events.csv 的 experiment_id 缺失或为空，未自动关联事件")
                    elif not event_ids_match:
                        warnings.append("events.csv 的 experiment_id 与测量 CSV 不一致，未自动关联事件")
                    elif not has_event_rows:
                        warnings.append("events.csv 没有事件记录，未自动关联事件")
                    else:
                        if not event_source_changed:
                            history_events.extend(candidate_events)
        except InterruptedError:
            raise
        except (OSError, UnicodeError, csv.Error) as exc:
            warnings.append(f"关联events.csv读取失败，继续加载测量CSV：{exc}")
        except ValueError as exc:
            warnings.append(f"关联events.csv读取失败，继续加载测量CSV：{exc}")

    max_plot_points = max(5, int(max_plot_points))
    bucket_count = max(1, max_plot_points // 5)
    bucket_size = max(1, math.ceil(valid_count / bucket_count))
    xs: list[float] = []
    ys: list[float] = []
    plot_metadata: list[dict] = []
    plot_break_before: list[bool] = []
    break_pending = False
    valid_index = 0
    bucket: list[tuple[float, float, dict, bool]] = []

    def append_bucket() -> None:
        if not bucket:
            return
        values = [item[1] for item in bucket]
        candidates = {
            0,
            len(bucket) - 1,
            min(range(len(bucket)), key=values.__getitem__),
            max(range(len(bucket)), key=values.__getitem__),
        }
        gap = next((index for index, item in enumerate(bucket) if item[3]), None)
        if gap is not None:
            candidates.add(gap)
        previous_index = -1
        for index in sorted(candidates):
            elapsed, value, metadata, _has_gap = bucket[index]
            xs.append(elapsed)
            ys.append(value)
            plot_metadata.append(metadata)
            plot_break_before.append(
                any(item[3] for item in bucket[previous_index + 1:index + 1])
            )
            previous_index = index
        bucket.clear()

    range_checkpoints: list[tuple[float, int, bool]] = []
    range_index_stride = _HISTORY_RANGE_INDEX_STRIDE
    range_rows_seen = 0
    range_previous_elapsed: float | None = None
    range_monotonic_elapsed = True
    range_break_pending = False
    if _history_file_signature(source) != source_signature:
        raise ValueError("CSV在加载过程中发生变化，请停止采集后重试")
    with source.open("rb") as stream:
        reader = csv.DictReader(
            _BinaryCsvLines(stream, complete_prefix_bytes, strip_bom=True),
        )
        fieldnames = tuple(reader.fieldnames or ())
        if not fieldnames:
            raise ValueError("CSV没有表头；无法建立历史图表索引")
        _reject_duplicate_csv_columns(fieldnames)
        range_checkpoints.append((-math.inf, stream.tell(), False))
        for row in reader:
            check_cancelled()
            range_rows_seen += 1
            status = str(row.get("status", "")).strip()
            try:
                value = float(row.get("value_si", ""))
            except (TypeError, ValueError, OverflowError):
                value = math.nan
            try:
                elapsed = float(row.get("elapsed_s", ""))
            except (TypeError, ValueError, OverflowError):
                elapsed = math.nan

            elapsed_is_finite = math.isfinite(elapsed)
            if not elapsed_is_finite:
                range_monotonic_elapsed = False
            elif range_previous_elapsed is not None and elapsed < range_previous_elapsed:
                range_monotonic_elapsed = False
            if elapsed_is_finite:
                range_previous_elapsed = elapsed

            if status != "ok" or not math.isfinite(value) or not elapsed_is_finite:
                break_pending = True
                range_break_pending = True
            else:
                metadata = {
                    "sample_index": row.get("sample_index", ""),
                    "raw_reading": row.get("raw_reading", ""),
                    "timestamp_local": row.get("timestamp_local", ""),
                    "timestamp_utc": row.get("timestamp_utc", ""),
                    "unit_si": row.get("unit_si", ""),
                    "mode": row.get("mode", ""),
                    "range_si": row.get("range_si", ""),
                }
                bucket.append((elapsed, value, metadata, break_pending))
                break_pending = False
                range_break_pending = False
                valid_index += 1
                if len(bucket) >= bucket_size:
                    append_bucket()

            if range_rows_seen % range_index_stride == 0 and elapsed_is_finite:
                offset = stream.tell()
                if range_checkpoints[-1][1] != offset:
                    range_checkpoints.append((elapsed, offset, range_break_pending))
                if len(range_checkpoints) > _HISTORY_RANGE_INDEX_MAX_CHECKPOINTS:
                    first, last = range_checkpoints[0], range_checkpoints[-1]
                    range_checkpoints = [first, *range_checkpoints[1:-1:2], last]
                    range_index_stride *= 2
    if valid_index != valid_count:
        raise ValueError("CSV在加载过程中发生变化，请停止采集后重试")
    if _history_file_signature(source) != source_signature:
        raise ValueError("CSV在加载过程中发生变化，请停止采集后重试")
    append_bucket()
    range_index = _HistoryCsvRangeIndex(
        path=source.resolve(),
        signature=source_signature,
        complete_prefix_bytes=complete_prefix_bytes,
        fieldnames=fieldnames,
        checkpoints=tuple(range_checkpoints),
        monotonic_elapsed=range_monotonic_elapsed,
    )

    return {
        "stats": stats.as_dict(), "valid_count": valid_count,
        "first_elapsed": first_elapsed, "last_elapsed": last_elapsed, "last_value": last_value,
        "mode": mode_value, "events": history_events, "warnings": warnings,
        "x": xs, "y": ys, "metadata": plot_metadata, "break_before": plot_break_before,
        "row_count": quality["row_count"],
        "_range_index": range_index,
    }


def load_history_csv_range(
    path: str | Path,
    start_elapsed: float,
    end_elapsed: float,
    max_plot_points: int,
    is_cancelled: Callable[[], bool] | None = None,
    *,
    range_index: _HistoryCsvRangeIndex | None = None,
) -> dict:
    """Stream one elapsed-time window from a measurement CSV for zoom detail."""
    source = Path(path)
    cancelled = is_cancelled or (lambda: False)

    def check_cancelled() -> None:
        if cancelled():
            raise InterruptedError("图表细节加载已取消")

    source_signature = _history_file_signature(source)
    complete_prefix_bytes: int | None = None
    if range_index is not None and range_index.monotonic_elapsed:
        try:
            same_source = source.resolve() == range_index.path
            same_contents = source_signature == range_index.signature
        except OSError:
            same_source = same_contents = False
        if same_source and same_contents:
            complete_prefix_bytes = range_index.complete_prefix_bytes
    if complete_prefix_bytes is None:
        complete_prefix_bytes = complete_csv_prefix_size(
            source, allow_complete_eof_record=True, check_cancelled=check_cancelled,
        )
        if _history_file_signature(source) != source_signature:
            raise ValueError("CSV在检查完整记录边界时发生变化，请停止采集后重试")
    start = float(start_elapsed)
    end = float(end_elapsed)
    if not (math.isfinite(start) and math.isfinite(end)) or end < start:
        raise ValueError("图表时间范围无效")
    limit = max(5, int(max_plot_points))
    xs: list[float] = []
    ys: list[float] = []
    breaks: list[bool] = []
    metadata: list[dict] = []
    break_pending = False
    rows_seen = 0
    valid_count = 0

    indexed_offset: int | None = None
    if range_index is not None and range_index.monotonic_elapsed:
        try:
            same_source = source.resolve() == range_index.path
            same_contents = source_signature == range_index.signature
        except OSError:
            same_source = same_contents = False
        if same_source and same_contents:
            checkpoint_times = [item[0] for item in range_index.checkpoints]
            checkpoint_index = max(0, bisect_left(checkpoint_times, start) - 1)
            _elapsed, indexed_offset, break_pending = range_index.checkpoints[checkpoint_index]

    try:
        if indexed_offset is None:
            stream_context = source.open("rb")
            reader = None
        else:
            binary_stream = source.open("rb")
            binary_stream.seek(indexed_offset)
            stream_context = binary_stream
            reader = csv.DictReader(
                _BinaryCsvLines(binary_stream, range_index.complete_prefix_bytes),
            )
            reader.fieldnames = list(range_index.fieldnames)
        with stream_context as stream:
            if reader is None:
                reader = csv.DictReader(
                    _BinaryCsvLines(stream, complete_prefix_bytes, strip_bom=True),
                )
            required = {"elapsed_s", "value_si", "status"}
            if not reader.fieldnames:
                raise ValueError("CSV没有表头；无法读取图表时间范围")
            _reject_duplicate_csv_columns(reader.fieldnames)
            missing = required - set(reader.fieldnames)
            if missing:
                raise ValueError(f"CSV缺少列：{'、'.join(sorted(missing))}；无法读取图表时间范围")
            for row in reader:
                check_cancelled()
                rows_seen += 1
                status = str(row.get("status", "")).strip()
                try:
                    elapsed = float(row.get("elapsed_s", ""))
                except (TypeError, ValueError, OverflowError):
                    break_pending = True
                    continue
                if indexed_offset is not None and elapsed > end:
                    break
                if not math.isfinite(elapsed) or status != "ok":
                    break_pending = True
                    continue
                try:
                    value = float(row.get("value_si", ""))
                except (TypeError, ValueError, OverflowError):
                    break_pending = True
                    continue
                if not math.isfinite(value):
                    break_pending = True
                    continue
                if not start <= elapsed <= end:
                    break_pending = False
                    continue
                item_metadata = {
                    "sample_index": row.get("sample_index", ""),
                    "raw_reading": row.get("raw_reading", ""),
                    "timestamp_local": row.get("timestamp_local", ""),
                    "timestamp_utc": row.get("timestamp_utc", ""),
                    "unit_si": row.get("unit_si", ""),
                    "mode": row.get("mode", ""),
                    "range_si": row.get("range_si", ""),
                }
                xs.append(elapsed)
                ys.append(value)
                breaks.append(break_pending)
                metadata.append(item_metadata)
                break_pending = False
                valid_count += 1
                if len(xs) > limit:
                    xs, ys, breaks, metadata = downsample_plot_data(
                        xs, ys, breaks, metadata, max_points=max(5, int(limit * 0.75))
                    )
        if _history_file_signature(source) != source_signature:
            raise ValueError("CSV在读取图表范围时发生变化，请停止采集后重试")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV不是有效的UTF-8文件") from exc
    except csv.Error as exc:
        raise ValueError(f"CSV格式错误：{exc}") from exc

    if not valid_count:
        return {
            "x": [], "y": [], "break_before": [], "metadata": [],
            "valid_count": 0, "rows_seen": rows_seen,
        }
    xs, ys, breaks, metadata = downsample_plot_data(
        xs, ys, breaks, metadata, max_points=limit
    )
    return {
        "x": xs,
        "y": ys,
        "break_before": breaks,
        "metadata": metadata,
        "valid_count": valid_count,
        "rows_seen": rows_seen,
    }
