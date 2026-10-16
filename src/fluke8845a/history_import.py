"""Background-friendly parsing for historical measurement CSV files."""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Callable

from .analysis import RunningStats
from .modes import Mode, descriptor


def load_history_csv(
    path: str | Path, fallback_mode: Mode, max_plot_points: int,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Validate and downsample a historical CSV without changing its source files."""
    source = Path(path)
    cancelled = is_cancelled or (lambda: False)

    def check_cancelled() -> None:
        if cancelled():
            raise InterruptedError("历史 CSV 加载已取消")

    quality = {"row_count": 0, "statuses": {}, "invalid_numeric": 0}
    stats = RunningStats()
    valid_count = 0
    first_elapsed = last_elapsed = last_value = None
    mode_values: set[str] = set()
    unit_values: set[str] = set()
    measurement_experiment_id: str | None = None
    measurement_id_missing = False
    measurement_id_mixed = False
    required = {"elapsed_s", "value_si", "status"}

    def rows():
        with source.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError("CSV没有表头；可接受格式至少包含 elapsed_s、value_si、status 列")
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
    warnings: list[str] = []
    event_path = source.with_name("events.csv")
    if event_path.is_file():
        try:
            with event_path.open(newline="", encoding="utf-8-sig") as stream:
                event_reader = csv.DictReader(stream)
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
                        if event_text and math.isfinite(event_elapsed) and first_elapsed <= event_elapsed <= last_elapsed:
                            candidate_events.append((event_elapsed, event_text))

                    if missing_event_id:
                        warnings.append("events.csv 的 experiment_id 缺失或为空，未自动关联事件")
                    elif not event_ids_match:
                        warnings.append("events.csv 的 experiment_id 与测量 CSV 不一致，未自动关联事件")
                    elif not has_event_rows:
                        warnings.append("events.csv 没有事件记录，未自动关联事件")
                    else:
                        history_events.extend(candidate_events)
        except (OSError, UnicodeError, csv.Error) as exc:
            warnings.append(f"关联events.csv读取失败，继续加载测量CSV：{exc}")

    max_plot_points = max(2, int(max_plot_points))
    step = max(1, math.ceil((valid_count - 1) / (max_plot_points - 1)))
    xs: list[float] = []
    ys: list[float] = []
    plot_metadata: list[dict] = []
    plot_break_before: list[bool] = []
    break_pending = False
    valid_index = 0
    last_sample = None
    for row in rows():
        check_cancelled()
        status = str(row.get("status", "")).strip()
        if status != "ok":
            break_pending = True
            continue
        try:
            value = float(row["value_si"])
            elapsed = float(row["elapsed_s"])
        except (TypeError, ValueError, OverflowError):
            break_pending = True
            continue
        if not math.isfinite(value) or not math.isfinite(elapsed):
            break_pending = True
            continue
        metadata = {
            "sample_index": row.get("sample_index", ""),
            "raw_reading": row.get("raw_reading", ""),
            "timestamp_local": row.get("timestamp_local", ""),
            "timestamp_utc": row.get("timestamp_utc", ""),
            "unit_si": row.get("unit_si", ""),
            "mode": row.get("mode", ""),
            "range_si": row.get("range_si", ""),
        }
        if valid_index % step == 0:
            xs.append(elapsed)
            ys.append(value)
            plot_metadata.append(metadata)
            plot_break_before.append(break_pending)
            break_pending = False
        valid_index += 1
        last_sample = (elapsed, value, metadata, break_pending)
    if valid_index != valid_count:
        raise ValueError("CSV在加载过程中发生变化，请停止采集后重试")
    if last_sample is not None and (not xs or (xs[-1], ys[-1]) != last_sample[:2]):
        xs.append(last_sample[0])
        ys.append(last_sample[1])
        plot_metadata.append(last_sample[2])
        plot_break_before.append(last_sample[3])

    return {
        "stats": stats.as_dict(), "valid_count": valid_count,
        "first_elapsed": first_elapsed, "last_elapsed": last_elapsed, "last_value": last_value,
        "mode": mode_value, "events": history_events, "warnings": warnings,
        "x": xs, "y": ys, "metadata": plot_metadata, "break_before": plot_break_before,
        "row_count": quality["row_count"],
    }
