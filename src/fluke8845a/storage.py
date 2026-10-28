"""Durable per-experiment CSV/JSON/log output with one append per acquisition."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import weakref
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, RLock, Thread, current_thread
from typing import Any, Callable

from . import __version__
from .modes import Mode, descriptor
from .path_security import is_symlink_or_junction as _is_symlink_or_junction
from .scpi import ModeSettings, Reading
from .spreadsheet_safety import is_numeric_literal, spreadsheet_safe_cell

log = logging.getLogger(__name__)

# Reserve enough room to create the run metadata, CSV headers, and log before
# touching the instrument. This is only a startup floor; it cannot predict the
# storage needed by a multi-hour or multi-day experiment.
MINIMUM_FREE_SPACE_BYTES = 10 * 1024 * 1024
# Forecast only: real rows vary with the instrument response and error text.
ESTIMATED_SAMPLE_BYTES = 4 * 1024
STORAGE_SPACE_CHECK_INTERVAL_NS = 5_000_000_000
CSV_FSYNC_INTERVAL_NS = 1_000_000_000
LOG_FSYNC_INTERVAL_NS = 1_000_000_000
EXPERIMENT_NAME_LOCK_PROTOCOL = 1
_INVALID_EXPERIMENT_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FINAL_EXPERIMENT_STATUSES = frozenset({
    "application_exit", "communication_error", "current_overrange", "disconnected",
    "instrument_error", "instrument_local", "interrupted", "read_timeout", "repeated_timeouts",
    "rs232_terminal_mode", "scpi_timeout", "speed_update_error", "start_error",
    "stopped", "storage_error",
})
_RAW_CSV_SPREADSHEET_NOTICE = (
    "实验 CSV 打开说明\n\n"
    "measurements.csv 和 events.csv 保留原始记录，不会为电子表格软件改写。\n"
    "不要直接用 Excel 等电子表格软件打开原始 events.csv；measurements.csv 中的原始读数和错误文本也可能以 =、+、- 或 @ 开头，"
    "表格软件可能将其解释为公式。\n"
    "需要用表格软件查看时，请在软件中导出并打开 events_excel_safe.csv 或 measurements_excel_safe.csv 安全副本；"
    "原始 CSV 不会被修改。\n"
    "科研计算请优先使用原始 CSV；若直接导入原始 CSV，请将事件、原始读数和错误列指定为文本。\n"
)

SAMPLE_FIELDS = (
    "experiment_id", "sample_index", "timestamp_local", "timestamp_utc",
    "host_read_request_start_local", "host_read_request_start_utc", "host_read_request_start_monotonic_ns",
    "host_read_response_end_local", "host_read_response_end_utc", "host_read_response_end_monotonic_ns",
    "elapsed_s", "read_duration_s",
    "actual_interval_s", "mode", "quantity", "value_si", "unit_si", "raw_reading",
    "voltage_v", "current_a", "resistance_ohm", "range_si", "nplc", "ac_bandwidth_hz",
    "analog_filter", "digital_filter", "autozero", "input_impedance_auto", "measurement_basis", "status", "error",
)
EVENT_FIELDS = ("experiment_id", "event_index", "timestamp_local", "timestamp_utc", "elapsed_s", "sample_index", "mode", "value_si", "unit_si", "event")
EVENT_NUMERIC_FIELDS = frozenset({"event_index", "elapsed_s", "sample_index", "value_si"})
SAMPLE_NUMERIC_FIELDS = frozenset({
    "sample_index", "host_read_request_start_monotonic_ns",
    "host_read_response_end_monotonic_ns", "elapsed_s", "read_duration_s",
    "actual_interval_s", "value_si", "raw_reading", "voltage_v", "current_a",
    "resistance_ohm", "range_si", "nplc", "ac_bandwidth_hz",
})


def get_available_space_bytes(directory: Path | str) -> int:
    """Return the currently available bytes on the filesystem containing directory."""
    path = Path(directory)
    try:
        return shutil.disk_usage(path).free
    except OSError as exc:
        raise OSError(f"无法检查实验保存位置剩余空间：{path}：{exc}") from exc


def require_minimum_free_space(free_bytes: int) -> int:
    """Reject a preflight snapshot that is below the startup reserve."""
    if free_bytes < MINIMUM_FREE_SPACE_BYTES:
        raise OSError(
            f"实验保存位置可用空间不足：当前 {free_bytes / (1024 * 1024):.1f} MiB，"
            f"启动至少需要 {MINIMUM_FREE_SPACE_BYTES // (1024 * 1024)} MiB。"
            "此检查只保证可创建启动文件，不能预测长期实验的总空间。"
        )
    return free_bytes


def format_available_space(free_bytes: int) -> str:
    """Format free space using binary units for human-readable preflight messages."""
    gibibyte = 1024**3
    mebibyte = 1024**2
    if free_bytes >= gibibyte:
        return f"{free_bytes / gibibyte:.1f} GiB"
    return f"{free_bytes / mebibyte:.0f} MiB"


def estimate_recording_capacity_seconds(
    free_bytes: int,
    target_interval_s: float | None,
) -> float | None:
    """Estimate the time before the startup reserve is reached.

    A zero/unspecified interval means fastest mode, whose actual sample cadence
    cannot be known before the instrument responds. This estimate is not a hard
    duration limit and uses a deliberately rounded 4 KiB per CSV sample.
    """
    if target_interval_s is None:
        return None
    interval = float(target_interval_s)
    if not math.isfinite(interval) or interval < 0:
        raise ValueError("目标采样间隔必须为非负有限数")
    if interval == 0:
        return None
    free = max(0, int(free_bytes))
    writable_bytes = max(0, free - MINIMUM_FREE_SPACE_BYTES)
    estimated_samples = writable_bytes // ESTIMATED_SAMPLE_BYTES
    return estimated_samples * interval


def format_estimated_recording_capacity(seconds: float | None) -> str:
    """Format a rough capacity forecast without implying a run-time limit."""
    if seconds is None:
        return "最快采集的实际采样速率未知，启动前无法预估可记录时长"
    if not math.isfinite(seconds):
        return "估算时长超出显示范围"
    if seconds < 60:
        return f"约 {seconds:.1f} 秒"
    if seconds < 3600:
        return f"约 {seconds / 60:.1f} 分钟"
    if seconds < 86400:
        return f"约 {seconds / 3600:.1f} 小时"
    return f"约 {seconds / 86400:.1f} 天"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Mode):
        return obj.value
    raise TypeError(f"无法序列化类型：{type(obj).__name__}")


def _escape_excel_row(
    row: dict[str | None, str | None], numeric_fields: frozenset[str]
) -> dict[str | None, str | None]:
    if None in row or any(value is None for value in row.values()):
        raise ValueError("CSV 存在列数不一致的记录，已拒绝导出以免丢失字段")
    for field, value in row.items():
        if value is None:
            continue
        if field in numeric_fields and is_numeric_literal(value):
            continue
        row[field] = spreadsheet_safe_cell(value)
    return row


def _escape_excel_event(row: dict[str | None, str | None]) -> dict[str | None, str | None]:
    return _escape_excel_row(row, EVENT_NUMERIC_FIELDS)


def _validate_excel_safe_header(
    fieldnames: list[str] | None,
    *,
    csv_label: str,
    required_column: str,
    allowed_columns: tuple[str, ...],
) -> list[str]:
    if not fieldnames or required_column not in fieldnames:
        raise ValueError(f"{csv_label} CSV 缺少 {required_column} 列")
    if len(fieldnames) != len(set(fieldnames)) or any(
        field is None or field not in allowed_columns for field in fieldnames
    ):
        raise ValueError(f"{csv_label} CSV 含未知列或重复列名，已拒绝生成安全副本")
    return fieldnames


def excel_safe_events_csv_bytes(source: bytes) -> bytes:
    """Return a spreadsheet-safe copy without changing the source event CSV.

    Every allowed column is checked. Valid numeric cells remain numeric, while
    formula-like text is escaped regardless of its column.
    """
    try:
        text = source.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("事件 CSV 不是有效的 UTF-8 文件") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = _validate_excel_safe_header(
        reader.fieldnames,
        csv_label="事件",
        required_column="event",
        allowed_columns=EVENT_FIELDS,
    )
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in reader:
        writer.writerow(_escape_excel_event(row))
    return output.getvalue().encode("utf-8-sig")


def _write_excel_safe_csv(
    source_path: Path,
    destination_path: Path,
    *,
    csv_label: str,
    required_column: str,
    allowed_columns: tuple[str, ...],
    numeric_fields: frozenset[str],
    staging_prefix: str,
) -> None:
    """Stream a formula-safe text-field copy and atomically publish a new file."""
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError(f"导出目标不能覆盖原始{csv_label} CSV")
    try:
        if destination_path.exists() and os.path.samefile(source_path, destination_path):
            raise ValueError(f"导出目标不能覆盖原始{csv_label} CSV")
    except OSError as exc:
        raise OSError(f"无法核对{csv_label}安全副本导出目标：{exc}") from exc
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=staging_prefix, suffix=".tmp", dir=destination_path.parent, delete=False
        ) as staged:
            staged_path = Path(staged.name)
        with source_path.open("r", encoding="utf-8-sig", newline="") as source, staged_path.open(
            "w", encoding="utf-8-sig", newline=""
        ) as destination:
            reader = csv.DictReader(source)
            fieldnames = _validate_excel_safe_header(
                reader.fieldnames,
                csv_label=csv_label,
                required_column=required_column,
                allowed_columns=allowed_columns,
            )
            writer = csv.DictWriter(destination, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\r\n")
            writer.writeheader()
            for row in reader:
                writer.writerow(_escape_excel_row(row, numeric_fields))
            destination.flush()
            os.fsync(destination.fileno())
        try:
            if os.name == "nt":
                # Windows rename is same-volume atomic and refuses to replace an
                # existing path, including a file created after the dialog check.
                os.rename(staged_path, destination_path)
            else:
                # POSIX rename replaces existing files, so publish with an
                # exclusive hard link instead; staging is in the same directory.
                os.link(staged_path, destination_path)
        except FileExistsError as exc:
            raise FileExistsError("导出目标已存在，请另选文件名") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{csv_label} CSV 不是有效的 UTF-8 文件") from exc
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)


def write_excel_safe_events_csv(source_path: Path, destination_path: Path) -> None:
    """Stream a spreadsheet-safe event copy while leaving the original unchanged."""
    _write_excel_safe_csv(
        source_path,
        destination_path,
        csv_label="事件",
        required_column="event",
        allowed_columns=EVENT_FIELDS,
        numeric_fields=EVENT_NUMERIC_FIELDS,
        staging_prefix=".events-safe-",
    )


def write_excel_safe_measurements_csv(source_path: Path, destination_path: Path) -> None:
    """Stream a spreadsheet-safe measurement copy without changing numeric fields or source."""
    _write_excel_safe_csv(
        source_path,
        destination_path,
        csv_label="测量",
        required_column="raw_reading",
        allowed_columns=SAMPLE_FIELDS,
        numeric_fields=SAMPLE_NUMERIC_FIELDS,
        staging_prefix=".measurements-safe-",
    )


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^\w\-一-鿿]+", "_", value.strip(), flags=re.UNICODE).strip("_-")
    return cleaned[:64] or "experiment"


def validate_experiment_name(name: str) -> str:
    """Validate a user-visible name before creating a run directory."""
    if not isinstance(name, str):
        raise ValueError("实验名称必须是文本")
    value = name.strip()
    if not value:
        raise ValueError("请填写实验名称")
    if len(value) > 100:
        raise ValueError("实验名称最多100个字符")
    if _INVALID_EXPERIMENT_NAME.search(value):
        raise ValueError("实验名称不能包含 Windows 路径非法字符或控制字符")
    return value


def _find_experiment_slug_candidates(root: Path | str, name: str) -> list[Path]:
    valid_name = validate_experiment_name(name)
    directory = Path(root).expanduser()
    if not directory.is_dir():
        return []
    slug = re.escape(_slug(valid_name))
    pattern = re.compile(rf"^\d{{8}}_\d{{6}}_\d+_{slug}(?:_\d+)?$", re.IGNORECASE)
    try:
        return sorted(
            (child for child in directory.iterdir() if child.is_dir() and pattern.fullmatch(child.name)),
            key=lambda child: child.name,
        )
    except OSError as exc:
        raise OSError(f"无法检查保存目录中的同名实验：{directory}：{exc}") from exc


def find_existing_experiment_directories(root: Path | str, name: str) -> list[Path]:
    """Find prior run folders whose saved visible name matches exactly."""
    valid_name = validate_experiment_name(name)
    exact_matches = []
    for candidate in _find_experiment_slug_candidates(root, valid_name):
        if _is_symlink_or_junction(candidate) or not candidate.is_dir():
            continue
        try:
            config_path = candidate / "config.json"
            if _is_symlink_or_junction(config_path):
                continue
            metadata = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(metadata, dict)
            and isinstance(metadata.get("experiment_name"), str)
            and metadata["experiment_name"].strip().casefold() == valid_name.casefold()
        ):
            exact_matches.append(candidate)
    return exact_matches


class ExperimentNameLock:
    """Hold an OS-released, cross-process lock for one Windows-style experiment name."""

    def __init__(self, root: Path | str, name: str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.name = validate_experiment_name(name)
        self._fd: int | None = None

    def acquire(self) -> "ExperimentNameLock":
        if self._fd is not None:
            return self
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            lock_directory = self.root / ".8845a-locks"
            if _is_symlink_or_junction(lock_directory):
                raise OSError(f"实验锁目录不能是符号链接或 junction：{lock_directory}")
            lock_directory.mkdir(exist_ok=True)
            if (
                not lock_directory.is_dir()
                or _is_symlink_or_junction(lock_directory)
                or lock_directory.resolve(strict=True).parent != self.root.resolve(strict=True)
            ):
                raise OSError(f"实验锁路径不是文件夹：{lock_directory}")
            key = hashlib.sha256(self.name.casefold().encode("utf-8")).hexdigest()
            lock_path = lock_directory / f"{key}.lock"
            if _is_symlink_or_junction(lock_path):
                raise OSError(f"实验锁文件不能是符号链接或 junction：{lock_path}")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise OSError(f"无法准备实验名称锁：{self.root}：{exc}") from exc

        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError) as exc:
            os.close(fd)
            raise OSError(
                f"同名实验正在其他程序中运行，或无法锁定：{self.name}；请停止该实验后重试：{exc}"
            ) from exc
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "ExperimentNameLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class _LimitedBinaryReader(io.RawIOBase):
    """Read at most a known complete prefix without loading a long CSV into memory."""

    def __init__(self, stream, limit: int) -> None:
        super().__init__()
        self._stream = stream
        self._remaining = limit

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._remaining <= 0:
            return 0
        chunk = self._stream.read(min(len(buffer), self._remaining))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        self._remaining -= len(chunk)
        return len(chunk)

    def close(self) -> None:
        if not self.closed:
            try:
                self._stream.close()
            finally:
                super().close()


def _count_complete_csv_records(path: Path, prefix_bytes: int, index_field: str) -> int:
    """Count data records in a previously validated complete CSV byte prefix."""
    if prefix_bytes <= 0:
        raise ValueError("CSV 没有完整表头，无法确认记录数量")
    limited = _LimitedBinaryReader(path.open("rb"), prefix_bytes)
    text = io.TextIOWrapper(
        io.BufferedReader(limited), encoding="utf-8-sig", newline=""
    )
    try:
        rows = csv.reader(text, strict=True)
        header = next(rows, None)
        if header is None or header.count(index_field) != 1:
            raise ValueError(f"CSV 表头缺少唯一的 {index_field} 列")
        index_position = header.index(index_field)
        count = 0
        for row in rows:
            if len(row) != len(header):
                raise ValueError("CSV 记录的列数与表头不一致")
            try:
                record_index = int(row[index_position])
            except ValueError as exc:
                raise ValueError(f"{index_field} 列索引不是整数") from exc
            if record_index != count + 1:
                raise ValueError(f"{index_field} 列索引不连续")
            count += 1
        return count
    finally:
        text.close()


def _recovery_csv_snapshot(
    directory: Path, filename: str, *, index_field: str
) -> dict[str, Any]:
    path = directory / filename
    if _is_symlink_or_junction(path):
        raise OSError(f"异常退出实验的 {filename} 不能是符号链接或 junction：{path}")
    if not path.exists():
        return {
            "present": False,
            "size_bytes": None,
            "complete_prefix_bytes": None,
            "partial_tail_bytes": None,
            "complete_record_count": None,
        }
    if not path.is_file():
        raise OSError(f"异常退出实验的 {filename} 不是普通文件：{path}")
    before = path.stat()
    prefix_bytes = complete_csv_prefix_size(path)
    count_error = None
    try:
        record_count = _count_complete_csv_records(path, prefix_bytes, index_field)
    except (csv.Error, UnicodeError, ValueError) as exc:
        record_count = None
        count_error = f"{type(exc).__name__}: {exc}"
    after = path.stat()
    before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_signature != after_signature:
        raise OSError(f"恢复检查期间 {filename} 发生变化；未修改实验元数据：{path}")
    snapshot = {
        "present": True,
        "size_bytes": after.st_size,
        "complete_prefix_bytes": prefix_bytes,
        "partial_tail_bytes": after.st_size - prefix_bytes,
        "complete_record_count": record_count,
    }
    if count_error is not None:
        snapshot["record_count_error"] = count_error
    return snapshot


def mark_stale_experiment_interrupted(
    root: Path | str,
    name: str,
    *,
    expected_directory: Path | str,
    lock: ExperimentNameLock,
) -> dict[str, Any] | None:
    """Mark a lock-free stale run interrupted while preserving both raw CSV files.

    The caller must hold the matching cross-process name lock. A running status on
    its own is not proof of a crash; the lock is what distinguishes a stale run
    from another live acquisition process.
    """
    valid_name = validate_experiment_name(name)
    root_path = Path(root).expanduser().resolve(strict=True)
    if not isinstance(lock, ExperimentNameLock) or lock._fd is None:
        raise OSError("恢复异常退出实验前必须取得同名实验锁")
    if lock.root != root_path or lock.name.casefold() != valid_name.casefold():
        raise OSError("恢复异常退出实验时取得的锁与保存目录或实验名称不匹配")

    candidate = Path(expected_directory).expanduser()
    if _is_symlink_or_junction(candidate):
        raise OSError(f"恢复目标不能是符号链接或 junction：{candidate}")
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise OSError(f"预检确认的异常退出目录已变化；未修改文件：{exc}") from exc
    if candidate.parent != root_path or not candidate.is_dir():
        raise OSError(f"异常退出目录不在指定保存目录的直接子级中：{candidate}")
    exact_matches = find_existing_experiment_directories(root_path, valid_name)
    if not exact_matches or exact_matches[-1].resolve(strict=True) != candidate:
        raise OSError("最近一次同名实验目录已变化；未修改任何实验文件，请重新预检")

    config_path = candidate / "config.json"
    if _is_symlink_or_junction(config_path) or not config_path.is_file():
        raise OSError(f"异常退出目录缺少可安全读取的 config.json：{config_path}")
    try:
        original_config = config_path.read_bytes()
        metadata = json.loads(original_config.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OSError(f"异常退出目录的配置文件无法安全读取；未修改文件：{exc}") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("experiment_name"), str):
        raise OSError(f"异常退出目录缺少有效实验名称元数据；未修改文件：{config_path}")
    if metadata["experiment_name"].strip().casefold() != valid_name.casefold():
        raise OSError(f"异常退出目录名称与配置不匹配；未修改文件：{config_path}")

    status = metadata.get("completion_status")
    if status in _FINAL_EXPERIMENT_STATUSES:
        return None
    if status != "running":
        raise OSError(f"实验状态无法确认，拒绝恢复：{status!r}；未修改文件：{config_path}")
    if metadata.get("experiment_name_lock_protocol") != EXPERIMENT_NAME_LOCK_PROTOCOL:
        raise OSError(
            "running 实验缺少已知的同名锁协议版本或锁协议标记，无法安全判断是否仍有旧版进程写入；"
            f"未修改任何文件，请先确认没有进程正在采集后手动处理：{config_path}"
        )

    csv_snapshots = {
        "measurements_csv": _recovery_csv_snapshot(
            candidate, "measurements.csv", index_field="sample_index"
        ),
        "events_csv": _recovery_csv_snapshot(
            candidate, "events.csv", index_field="event_index"
        ),
    }
    if _is_symlink_or_junction(config_path) or config_path.read_bytes() != original_config:
        raise OSError(f"恢复检查期间 config.json 发生变化；未修改实验元数据：{config_path}")

    backup_name = f".8845a-config-before-recovery-{uuid.uuid4().hex}.json"
    backup_path = candidate / backup_name
    try:
        with backup_path.open("xb") as stream:
            stream.write(original_config)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise OSError(f"无法保留恢复前配置；未修改实验元数据：{backup_path}：{exc}") from exc

    recovery = {
        "recovered_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "reason": "running metadata with released experiment-name lock",
        "original_config_backup": backup_name,
        "sample_count_checkpoint": metadata.get("sample_count"),
        "event_count_checkpoint": metadata.get("event_count"),
        "recovered_sample_count": csv_snapshots["measurements_csv"]["complete_record_count"],
        "recovered_event_count": csv_snapshots["events_csv"]["complete_record_count"],
        **csv_snapshots,
    }
    updated_metadata = dict(metadata)
    if recovery["recovered_sample_count"] is not None:
        updated_metadata["sample_count"] = recovery["recovered_sample_count"]
    if recovery["recovered_event_count"] is not None:
        updated_metadata["event_count"] = recovery["recovered_event_count"]
    updated_metadata["completion_status"] = "interrupted"
    updated_metadata["recovery"] = recovery
    encoded_metadata = (
        json.dumps(updated_metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    temp_path = candidate / f".8845a-config-recovery-{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("xb") as stream:
            stream.write(encoded_metadata)
            stream.flush()
            os.fsync(stream.fileno())
        if _is_symlink_or_junction(config_path) or config_path.read_bytes() != original_config:
            raise OSError(f"恢复期间 config.json 发生变化；已保留原文件：{config_path}")
        os.replace(temp_path, config_path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise OSError(f"无法原子更新异常退出实验状态；原始 CSV 未修改：{exc}") from exc
    return recovery


def archive_latest_existing_experiment_directory(
    root: Path | str, name: str, *, expected_directory: Path | str | None = None,
    reserved_directory: Path | str | None = None,
) -> Path:
    """Archive the newest exact-name, finalized run; refuse ambiguous and active folders."""
    directory = Path(root).expanduser()
    matches = _find_experiment_slug_candidates(directory, name)
    if reserved_directory is not None:
        reserved = Path(reserved_directory).expanduser().resolve(strict=True)
        matches = [candidate for candidate in matches if candidate.resolve(strict=True) != reserved]
    if not matches:
        raise FileNotFoundError("预检发现的同名实验目录已不存在；请重新检查后再开始")

    valid_name = validate_experiment_name(name)
    exact_matches: list[tuple[Path, dict[str, Any]]] = []
    for candidate in matches:
        if (
            _is_symlink_or_junction(candidate)
            or not candidate.is_dir()
            or candidate.resolve(strict=True).parent != directory.resolve(strict=True)
        ):
            raise OSError(f"同名候选目录已变化或不是普通文件夹，未覆盖：{candidate}")
        config_path = candidate / "config.json"
        if _is_symlink_or_junction(config_path):
            raise OSError(f"同名候选目录的配置文件不能是符号链接或 junction；旧数据未移动：{config_path}")
        try:
            metadata = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OSError(
                f"同名候选目录缺少可读取的配置元数据，无法确认覆盖目标；旧数据未移动：{candidate}：{exc}"
            ) from exc
        if not isinstance(metadata, dict) or not isinstance(metadata.get("experiment_name"), str):
            raise OSError(
                f"同名候选目录缺少有效实验名称元数据，无法确认覆盖目标；旧数据未移动：{candidate}"
            )
        if metadata["experiment_name"].strip().casefold() == valid_name.casefold():
            exact_matches.append((candidate, metadata))

    if not exact_matches:
        raise OSError(
            f"存在名称相似但元数据不匹配的目录，无法安全确认“{valid_name}”的覆盖目标；旧数据未移动"
        )
    existing, metadata = exact_matches[-1]
    if expected_directory is not None:
        try:
            expected = Path(expected_directory).expanduser().resolve(strict=True)
            actual = existing.resolve(strict=True)
        except OSError as exc:
            raise OSError(f"预检确认的实验目录已变化，未移动任何旧数据；请重新预检：{exc}") from exc
        if expected != actual:
            raise OSError("最近一次同名实验目录已变化，未移动任何旧数据；请重新预检并确认")
    completion_status = metadata.get("completion_status")
    if completion_status == "running":
        raise OSError(f"实验仍在运行，拒绝移动其目录；请先停止后再覆盖：{existing}")
    if completion_status not in _FINAL_EXPERIMENT_STATUSES:
        raise OSError(f"实验状态无法确认，拒绝移动目录；旧数据未移动：{existing}")

    archive_root = directory / ".8845a-backups"
    try:
        if _is_symlink_or_junction(archive_root):
            raise OSError(f"备份目录不能是符号链接或 junction：{archive_root}")
        archive_root.mkdir(parents=True, exist_ok=True)
        if (
            not archive_root.is_dir()
            or _is_symlink_or_junction(archive_root)
            or archive_root.resolve(strict=True).parent != directory.resolve(strict=True)
        ):
            raise OSError(f"备份路径不是文件夹：{archive_root}")
        while True:
            backup = archive_root / f"{existing.name}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}"
            if backup.exists() or backup.is_symlink():
                continue
            existing.rename(backup)
            return backup
    except OSError as exc:
        raise OSError(f"无法安全备份同名实验目录，旧数据未覆盖：{existing}：{exc}") from exc


def prepare_experiment_storage(root: Path | str) -> Path:
    """Create a writable experiment root and check a conservative startup reserve."""
    if not str(root).strip():
        raise ValueError("请指定实验保存目录")
    directory = Path(root).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"实验保存目录无法创建：{directory}：{exc}") from exc
    if not directory.is_dir():
        raise OSError(f"实验保存路径不是文件夹：{directory}")

    require_minimum_free_space(get_available_space_bytes(directory))

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".8845a-write-check-", dir=directory, delete=True
        ) as probe:
            probe.flush()
    except OSError as exc:
        raise OSError(f"实验保存目录不可写：{directory}：{exc}") from exc
    return directory


def reserve_experiment_directory(root: Path | str, name: str) -> Path:
    """Atomically reserve a unique output directory without overwriting data."""
    name = validate_experiment_name(name)
    root = prepare_experiment_storage(root)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    base = root / f"{stamp}_{_slug(name)}"
    suffix = 0
    while True:
        directory = base if suffix == 0 else root / f"{base.name}_{suffix}"
        try:
            directory.mkdir()
        except FileExistsError:
            suffix += 1
            continue
        except OSError as exc:
            raise OSError(f"无法预留实验目录：{directory}：{exc}") from exc
        return directory


def complete_csv_prefix_size(
    path: Path | str,
    chunk_size: int = 64 * 1024,
    *,
    allow_complete_eof_record: bool = False,
    check_cancelled: Callable[[], None] | None = None,
) -> int:
    """Return bytes through complete CSV records, optionally including a valid EOF record.

    The default requires a line terminator so acquisition recovery treats a durable
    newline as the row-completion checkpoint. History import may opt in to CSV's
    valid final record without CR/LF, while still excluding an unclosed quoted tail.
    An optional callback is invoked before each scan chunk and may raise to cancel.
    """
    if chunk_size < 1:
        raise ValueError("CSV扫描块大小必须大于0")
    csv_path = Path(path)
    size = csv_path.stat().st_size
    if size == 0:
        if check_cancelled is not None:
            check_cancelled()
        return 0
    in_quotes = False
    pending_quote = False
    position = 0
    complete_end = 0
    with csv_path.open("rb") as stream:
        while position < size:
            if check_cancelled is not None:
                check_cancelled()
            chunk = stream.read(min(chunk_size, size - position))
            if not chunk:
                break
            index = 0
            if pending_quote:
                if chunk[0] == ord('"'):
                    index = 1
                else:
                    in_quotes = False
                pending_quote = False
            while index < len(chunk):
                byte = chunk[index]
                if byte == ord('"'):
                    if in_quotes:
                        if index + 1 < len(chunk):
                            if chunk[index + 1] == ord('"'):
                                index += 2
                                continue
                            in_quotes = False
                        else:
                            pending_quote = True
                    else:
                        in_quotes = True
                elif byte == 10 and not in_quotes:
                    complete_end = position + index + 1
                index += 1
            position += len(chunk)
    if allow_complete_eof_record and position == size and (not in_quotes or pending_quote):
        return size
    return complete_end


def _close_windows_handle_for_gc(kernel32, handle) -> None:
    """Best-effort fallback for a directory handle whose owner was abandoned."""
    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass


class _WindowsDirectoryLease:
    """Prevent a Windows experiment directory from being renamed while it is in use."""

    def __init__(self, directory: Path) -> None:
        self._handle = None
        self._kernel32 = None
        if os.name != "nt":
            return

        import ctypes
        from ctypes import wintypes

        class _FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("file_attributes", wintypes.DWORD),
                ("creation_time", _FileTime),
                ("last_access_time", _FileTime),
                ("last_write_time", _FileTime),
                ("volume_serial_number", wintypes.DWORD),
                ("file_size_high", wintypes.DWORD),
                ("file_size_low", wintypes.DWORD),
                ("number_of_links", wintypes.DWORD),
                ("file_index_high", wintypes.DWORD),
                ("file_index_low", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        file_read_attributes = 0x0080
        delete_access = 0x00010000
        share_read = 0x00000001
        share_write = 0x00000002
        open_existing = 3
        file_flag_open_reparse_point = 0x00200000
        file_flag_backup_semantics = 0x02000000
        invalid_handle = ctypes.c_void_p(-1).value
        handle = kernel32.CreateFileW(
            str(directory),
            file_read_attributes | delete_access,
            share_read | share_write,
            None,
            open_existing,
            file_flag_backup_semantics | file_flag_open_reparse_point,
            None,
        )
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(
                f"无法锁定实验目录，拒绝在可能被替换的路径中保存数据：{directory}："
                f"{ctypes.WinError(error)}"
            )

        try:
            information = _ByHandleFileInformation()
            if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
                error = ctypes.get_last_error()
                raise OSError(
                    f"无法核对实验目录句柄：{directory}：{ctypes.WinError(error)}"
                )
            file_attribute_directory = 0x00000010
            file_attribute_reparse_point = 0x00000400
            if information.file_attributes & file_attribute_reparse_point:
                raise OSError(f"实验目录不能是符号链接或 junction：{directory}")
            if not information.file_attributes & file_attribute_directory:
                raise OSError(f"实验路径不是文件夹：{directory}")

            path_stat = directory.stat(follow_symlinks=False)
            handle_file_id = (
                information.file_index_high << 32
            ) | information.file_index_low
            # CPython exposes the Windows file index as st_ino and the volume
            # serial in the low 32 bits of st_dev. Compare both to detect a path
            # replacement that races the handle acquisition.
            if (
                path_stat.st_ino != handle_file_id
                or (path_stat.st_dev & 0xFFFFFFFF)
                != information.volume_serial_number
            ):
                raise OSError(f"实验目录在安全锁定期间发生变化：{directory}")
        except BaseException:
            kernel32.CloseHandle(handle)
            raise

        self._kernel32 = kernel32
        self._handle = handle
        self._finalizer = weakref.finalize(
            self, _close_windows_handle_for_gc, kernel32, handle
        )

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        if not self._kernel32.CloseHandle(handle):
            import ctypes

            error = ctypes.get_last_error()
            raise OSError(f"无法释放实验目录锁：{ctypes.WinError(error)}")
        self._handle = None
        self._finalizer.detach()


class ExperimentRecorder:
    """Create a fresh run folder and periodically sync appended CSV records."""

    def __init__(self, directory: Path, metadata: dict[str, Any], settings: ModeSettings) -> None:
        requested_directory = Path(directory).expanduser()
        if _is_symlink_or_junction(requested_directory):
            raise OSError(f"实验目录不能是符号链接或 junction：{requested_directory}")
        try:
            resolved_directory = requested_directory.resolve(strict=True)
        except OSError as exc:
            raise OSError(f"实验目录不存在或不可访问：{requested_directory}：{exc}") from exc
        if not resolved_directory.is_dir():
            raise OSError(f"实验路径不是文件夹：{requested_directory}")
        self.directory = resolved_directory
        self.metadata = dict(metadata)
        self.experiment_id = uuid.uuid4().hex
        self.settings = settings
        self.mode = settings.mode
        self.spec = descriptor(self.mode)
        self.started_monotonic_ns: int | None = None
        self.last_attempt_ns: int | None = None
        self.last_valid_response_end_ns: int | None = None
        self.sample_count = 0
        self._pending_sample_index: int | None = None
        self.event_count = 0
        self._pending_event_index: int | None = None
        self._last_space_check_ns = 0
        self._last_csv_fsync_ns = time.perf_counter_ns()
        self._last_log_fsync_ns = time.perf_counter_ns()
        self._csv_sync_lock = RLock()
        self._log_sync_lock = RLock()
        self._csv_sync_error: Exception | None = None
        self._log_sync_error: Exception | None = None
        self._csv_sync_stop = Event()
        self._csv_sync_thread: Thread | None = None
        self._config_file_identity: tuple[int, int] | None = None
        self._directory_lease: _WindowsDirectoryLease | None = None
        self._data_stream = None
        self._event_stream = None
        self._log_stream = None
        self._opened_streams = []
        try:
            self._directory_lease = _WindowsDirectoryLease(self.directory)
            self._assert_output_directory()
            for filename in ("measurements.csv", "events.csv", "run.log", "config.json"):
                self._assert_output_path(self.directory / filename, must_not_exist=True)
            self._data_stream = self._open_stream(self.directory / "measurements.csv", "x", newline="", encoding="utf-8-sig")
            self._event_stream = self._open_stream(self.directory / "events.csv", "x", newline="", encoding="utf-8-sig")
            self._log_stream = self._open_stream(self.directory / "run.log", "x", encoding="utf-8")
            self._writer = csv.DictWriter(self._data_stream, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
            self._events = csv.DictWriter(self._event_stream, fieldnames=EVENT_FIELDS, extrasaction="ignore")
            self._writer.writeheader()
            self._events.writeheader()
            self._flush_streams()
            self._sync_csv_streams(force=True)
            self.metadata.update({
                "experiment_id": self.experiment_id,
                "software_version": __version__,
                "simulation": bool(self.metadata.get("simulation", False)),
                "mode": self.mode.value,
                "mode_label": self.spec.label,
                "quantity": self.spec.quantity,
                "unit_si": self.spec.unit_si,
                "measurement_basis": "true RMS" if self.spec.rms else "direct reading",
                "mode_settings": {
                    "autorange": self.settings.autorange,
                    "range_si": self.settings.range_si,
                    "nplc": self.settings.nplc,
                    "ac_bandwidth_hz": self.settings.ac_bandwidth_hz,
                    "analog_filter": self.settings.analog_filter,
                    "digital_filter": (
                        self.settings.digital_filter if self.spec.digital_filter else None
                    ),
                    "autozero": self.settings.autozero,
                    "input_impedance_auto": (
                        self.settings.input_impedance_auto
                        if self.spec.input_impedance_auto else None
                    ),
                },
                "created_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "sample_count": 0,
                "event_count": 0,
                "experiment_name_lock_protocol": EXPERIMENT_NAME_LOCK_PROTOCOL,
                "completion_status": "running",
            })
            self._write_metadata()
            self.log("INFO", f"实验开始；数据仿真={self.metadata['simulation']}；模式={self.spec.label}")
            self._write_csv_spreadsheet_notice()
        except BaseException:
            self._close_open_streams(suppress_errors=True)
            self._close_directory_lease(suppress_errors=True)
            raise

    def start_periodic_sync(self, on_error: Callable[[Exception], None]) -> None:
        """Run due CSV and run-log fsyncs independently of acquisition."""
        thread = self._csv_sync_thread
        if thread is not None and thread.is_alive():
            raise RuntimeError("实验文件持久化线程已在运行")
        self._csv_sync_stop = Event()
        interval_ns = min(CSV_FSYNC_INTERVAL_NS, LOG_FSYNC_INTERVAL_NS)
        interval_s = max(0.01, interval_ns / 1_000_000_000)

        def sync_loop() -> None:
            while not self._csv_sync_stop.wait(interval_s):
                try:
                    self.sync_csv_if_due()
                    self.sync_log_if_due()
                except Exception as exc:
                    try:
                        on_error(exc)
                    except Exception:
                        log.exception("could not report background storage sync failure")
                    return

        thread = Thread(
            target=sync_loop,
            name=f"Fluke8845A-storage-sync-{self.experiment_id[:8]}",
            daemon=True,
        )
        self._csv_sync_thread = thread
        try:
            thread.start()
        except BaseException:
            self._csv_sync_stop.set()
            if getattr(thread, "ident", None) is None:
                self._csv_sync_thread = None
            else:
                try:
                    if thread is not current_thread():
                        thread.join()
                except Exception:
                    # Keep the reference so finish() can retry the join before
                    # it closes any stream that the thread may still be syncing.
                    log.exception("could not join CSV sync thread after start failure")
                if not thread.is_alive():
                    self._csv_sync_thread = None
            raise

    def stop_periodic_sync(self) -> None:
        """Stop and join the independent CSV sync thread before closing streams."""
        thread = self._csv_sync_thread
        if thread is None:
            return
        self._csv_sync_stop.set()
        try:
            if thread is not current_thread() and getattr(thread, "ident", None) is not None:
                thread.join()
        finally:
            if not thread.is_alive():
                self._csv_sync_thread = None

    def _write_csv_spreadsheet_notice(self) -> None:
        """Place a visible warning next to raw CSV files without changing them."""
        path = self.directory / "CSV_打开说明.txt"
        temporary = self.directory / f"{path.name}.{uuid.uuid4().hex}.partial"
        created = False
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                created = True
                stream.write(_RAW_CSV_SPREADSHEET_NOTICE)
                stream.flush()
            if path.exists():
                raise FileExistsError(f"实验目录中已存在 {path.name}")
            temporary.rename(path)
        except FileExistsError:
            self._remove_partial_csv_spreadsheet_notice(temporary, created)
            self.log("WARNING", "未覆盖实验目录中已存在的 CSV_打开说明.txt")
        except OSError as exc:
            self._remove_partial_csv_spreadsheet_notice(temporary, created)
            self.log("WARNING", f"无法创建 CSV_打开说明.txt；原始 CSV 保持不变：{exc}")

    @staticmethod
    def _remove_partial_csv_spreadsheet_notice(path: Path, created: bool) -> None:
        if not created:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.exception("failed to remove incomplete CSV spreadsheet notice")

    def _open_stream(self, path: Path, *args: Any, **kwargs: Any):
        self._assert_output_path(path, must_not_exist=True)
        stream = path.open(*args, **kwargs)
        try:
            self._assert_output_directory()
            self._assert_output_path(path, must_not_exist=False)
            opened = os.fstat(stream.fileno())
            named = path.stat()
            if self._file_identity(opened) != self._file_identity(named):
                raise OSError(f"输出文件在打开期间被替换：{path}")
        except BaseException:
            stream.close()
            raise
        self._opened_streams.append(stream)
        return stream

    def _assert_output_directory(self) -> None:
        directory = self.directory
        if _is_symlink_or_junction(directory):
            raise OSError(f"实验目录不能是符号链接或 junction：{directory}")
        try:
            if not directory.is_dir() or directory.resolve(strict=True) != directory:
                raise OSError(f"实验目录已变化或不可访问：{directory}")
        except OSError as exc:
            raise OSError(f"实验目录已变化或不可访问：{directory}：{exc}") from exc

    def _assert_output_path(
        self,
        path: Path,
        *,
        must_not_exist: bool,
        expected_identity: tuple[int, int] | None = None,
    ) -> tuple[int, int] | None:
        self._assert_output_directory()
        if path.parent != self.directory:
            raise OSError(f"输出文件不在实验目录中：{path}")
        if _is_symlink_or_junction(path):
            raise OSError(f"实验输出不能是符号链接或 junction：{path}")
        try:
            details = path.lstat()
        except FileNotFoundError:
            if expected_identity is not None:
                raise OSError(f"实验输出在写入期间被移除：{path}")
            return None
        if not stat.S_ISREG(details.st_mode):
            raise OSError(f"实验输出不是普通文件：{path}")
        identity = self._file_identity(details)
        if must_not_exist:
            raise FileExistsError(f"实验目录中已存在输出文件，拒绝覆盖：{path}")
        if expected_identity is not None and identity != expected_identity:
            raise OSError(f"实验输出在写入期间被替换：{path}")
        return identity

    @staticmethod
    def _file_identity(details: os.stat_result) -> tuple[int, int]:
        return details.st_dev, details.st_ino

    def check_storage_reserve(self) -> None:
        now_ns = time.perf_counter_ns()
        if self._last_space_check_ns and now_ns - self._last_space_check_ns < STORAGE_SPACE_CHECK_INTERVAL_NS:
            return
        try:
            free_bytes = shutil.disk_usage(self.directory).free
        except OSError as exc:
            raise OSError(f"无法检查实验目录剩余空间，已暂停写入以保护数据：{exc}") from exc
        self._last_space_check_ns = now_ns
        if free_bytes < MINIMUM_FREE_SPACE_BYTES:
            raise OSError(
                f"实验目录剩余空间低于安全余量 {MINIMUM_FREE_SPACE_BYTES // (1024 * 1024)} MiB，"
                "已停止继续写入；请先释放磁盘空间并另开新实验。"
            )

    def _ensure_no_pending_csv_record(self) -> None:
        if self._pending_sample_index is not None:
            raise OSError("前一条测量记录的CSV写入状态未确认，必须结束当前实验后恢复")
        if self._pending_event_index is not None:
            raise OSError("前一条事件记录的CSV写入状态未确认，必须结束当前实验后恢复")

    def _close_open_streams(self, *, suppress_errors: bool) -> None:
        for stream in reversed(self._opened_streams):
            if stream.closed:
                continue
            try:
                stream.close()
            except Exception:
                if not suppress_errors:
                    raise
                log.exception("failed to close partially initialized experiment file")

    def _close_directory_lease(self, *, suppress_errors: bool) -> None:
        lease = self._directory_lease
        if lease is None:
            return
        try:
            lease.close()
        except Exception:
            if not suppress_errors:
                raise
            log.exception("failed to release experiment directory lease")
        else:
            self._directory_lease = None

    @classmethod
    def create(
        cls,
        root: Path,
        name: str,
        metadata: dict[str, Any],
        settings: ModeSettings,
        *,
        reserved_directory: Path | None = None,
    ) -> "ExperimentRecorder":
        name = validate_experiment_name(name)
        if reserved_directory is None:
            root = prepare_experiment_storage(root)
            directory = reserve_experiment_directory(root, name)
        else:
            root = Path(root).expanduser()
            directory = Path(reserved_directory).expanduser()
            if _is_symlink_or_junction(directory):
                raise OSError(f"预留的实验目录不能是符号链接或 junction：{directory}")
            try:
                resolved_root = root.resolve(strict=True)
                resolved_directory = directory.resolve(strict=True)
                if resolved_directory.parent != resolved_root or not resolved_directory.is_dir():
                    raise ValueError("预留的实验目录不在指定保存根目录中")
                if any(resolved_directory.iterdir()):
                    raise FileExistsError("预留的实验目录已包含文件，拒绝覆盖")
            except OSError as exc:
                raise OSError(f"预留的实验目录不可用：{directory}：{exc}") from exc
        metadata = dict(metadata)
        metadata["experiment_name"] = name
        return cls(directory, metadata, settings)

    def log(self, level: str, message: str) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._log_sync_lock:
            self._log_stream.write(f"{timestamp} [{level.upper()}] {message}\n")
            self._log_stream.flush()

    def append_reading(
        self,
        reading: Reading | None,
        *,
        mode: Mode,
        started_monotonic_ns: int | None = None,
        timestamp_local: str | None = None,
        timestamp_utc: str | None = None,
        host_read_request_start_local: str | None = None,
        host_read_request_start_utc: str | None = None,
        host_read_request_start_monotonic_ns: int | None = None,
        host_read_response_end_local: str | None = None,
        host_read_response_end_utc: str | None = None,
        host_read_response_end_monotonic_ns: int | None = None,
        read_duration_s: float | None = None,
        status: str = "ok",
        error: str = "",
        raw_reading: str = "",
    ) -> dict[str, Any]:
        if mode != self.mode:
            raise ValueError("一个实验数据段中不能混合不同测量模式")
        self._ensure_no_pending_csv_record()
        self.check_storage_reserve()
        if reading is not None:
            started_monotonic_ns = reading.read_request_start_monotonic_ns
            timestamp_local = reading.read_request_start_local
            timestamp_utc = reading.read_request_start_utc
            host_read_request_start_local = reading.read_request_start_local
            host_read_request_start_utc = reading.read_request_start_utc
            host_read_request_start_monotonic_ns = reading.read_request_start_monotonic_ns
            host_read_response_end_local = reading.read_response_end_local or None
            host_read_response_end_utc = reading.read_response_end_utc or None
            host_read_response_end_monotonic_ns = reading.read_response_end_monotonic_ns
            read_duration_s = reading.read_duration_s
            status = reading.status
        if started_monotonic_ns is None:
            import time
            started_monotonic_ns = time.perf_counter_ns()
        if not timestamp_local or not timestamp_utc:
            if timestamp_utc:
                sample_time = datetime.fromisoformat(timestamp_utc)
                if sample_time.tzinfo is None:
                    sample_time = sample_time.replace(tzinfo=timezone.utc)
            elif timestamp_local:
                sample_time = datetime.fromisoformat(timestamp_local)
                if sample_time.tzinfo is None:
                    sample_time = sample_time.astimezone()
            else:
                sample_time = datetime.now(timezone.utc)
            timestamp_local = timestamp_local or sample_time.astimezone().isoformat(timespec="microseconds")
            timestamp_utc = timestamp_utc or sample_time.astimezone(timezone.utc).isoformat(timespec="microseconds")
        host_read_request_start_local = host_read_request_start_local or timestamp_local
        host_read_request_start_utc = host_read_request_start_utc or timestamp_utc
        if host_read_request_start_monotonic_ns is None:
            host_read_request_start_monotonic_ns = started_monotonic_ns
        if host_read_response_end_monotonic_ns is None:
            duration_ns = int((read_duration_s or 0.0) * 1e9)
            host_read_response_end_monotonic_ns = started_monotonic_ns + duration_ns
        if not host_read_response_end_utc or not host_read_response_end_local:
            start_time = datetime.fromisoformat(host_read_request_start_utc)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            end_time = start_time + timedelta(seconds=read_duration_s or 0.0)
            host_read_response_end_utc = host_read_response_end_utc or end_time.astimezone(timezone.utc).isoformat(timespec="microseconds")
            host_read_response_end_local = host_read_response_end_local or end_time.astimezone().isoformat(timespec="microseconds")
        experiment_start_ns = self.started_monotonic_ns or started_monotonic_ns
        elapsed = max(0.0, (started_monotonic_ns - experiment_start_ns) / 1e9)
        interval = None
        response_end_ns = host_read_response_end_monotonic_ns
        if reading is not None and reading.status == "ok":
            if self.last_valid_response_end_ns is not None:
                interval = max(0.0, (response_end_ns - self.last_valid_response_end_ns) / 1e9)
        sample_index = self.sample_count + 1
        voltage = current = resistance = ""
        value = ""
        raw = raw_reading
        range_si: float | str = ""
        if reading is not None:
            value = repr(reading.value_si)
            raw = reading.raw
            range_si = "" if reading.range_si is None else repr(reading.range_si)
            if self.spec.quantity == "voltage":
                voltage = value
            elif self.spec.quantity == "current":
                current = value
            else:
                resistance = value
        row = {
            "experiment_id": self.experiment_id,
            "sample_index": sample_index,
            "timestamp_local": timestamp_local,
            "timestamp_utc": timestamp_utc,
            "host_read_request_start_local": host_read_request_start_local,
            "host_read_request_start_utc": host_read_request_start_utc,
            "host_read_request_start_monotonic_ns": str(host_read_request_start_monotonic_ns),
            "host_read_response_end_local": host_read_response_end_local,
            "host_read_response_end_utc": host_read_response_end_utc,
            "host_read_response_end_monotonic_ns": str(host_read_response_end_monotonic_ns),
            "elapsed_s": repr(elapsed),
            "read_duration_s": "" if read_duration_s is None else repr(read_duration_s),
            "actual_interval_s": "" if interval is None else repr(interval),
            "mode": mode.value,
            "quantity": self.spec.quantity,
            "value_si": value,
            "unit_si": self.spec.unit_si,
            "raw_reading": raw,
            "voltage_v": voltage,
            "current_a": current,
            "resistance_ohm": resistance,
            "range_si": range_si,
            "nplc": "" if self.settings.nplc is None else repr(float(self.settings.nplc)),
            "ac_bandwidth_hz": "" if self.settings.ac_bandwidth_hz is None else self.settings.ac_bandwidth_hz,
            "analog_filter": self.settings.analog_filter if self.spec.analog_filter else "",
            "digital_filter": self.settings.digital_filter if self.spec.digital_filter else "",
            "autozero": self.settings.autozero,
            "input_impedance_auto": (
                self.settings.input_impedance_auto if self.spec.input_impedance_auto else ""
            ),
            "measurement_basis": "true RMS" if self.spec.rms else "direct reading",
            "status": status,
            "error": error,
        }
        self._pending_sample_index = sample_index
        try:
            with self._csv_sync_lock:
                self._writer.writerow(row)
                self._data_stream.flush()
                self.sync_csv_if_due()
        except Exception:
            # A failed write, flush, log flush, or fsync leaves persistence ambiguous. The
            # worker stops; finish() reconciles against complete CSV records.
            raise
        # Count only a complete CSV record accepted by the stream. A failed or
        # partial writerow must not inflate the persisted-row checkpoint.
        if self.started_monotonic_ns is None:
            self.started_monotonic_ns = started_monotonic_ns
        self.sample_count = sample_index
        self._pending_sample_index = None
        self.last_attempt_ns = started_monotonic_ns
        if reading is not None and reading.status == "ok":
            self.last_valid_response_end_ns = response_end_ns
        if self.sample_count % 100 == 0:
            self.metadata["sample_count"] = self.sample_count
            self._write_metadata()
        return row

    def append_event(self, event: str, *, value_si: float | None = None) -> dict[str, Any]:
        if not event.strip():
            raise ValueError("事件内容不能为空")
        self._ensure_no_pending_csv_record()
        self.check_storage_reserve()
        import time
        from datetime import datetime, timezone

        now_ns = time.perf_counter_ns()
        if self.started_monotonic_ns is None:
            self.started_monotonic_ns = now_ns
        event_time_utc = datetime.now(timezone.utc)
        event_time_local = event_time_utc.astimezone()
        row = {
            "experiment_id": self.experiment_id,
            "event_index": self.event_count + 1,
            "timestamp_local": event_time_local.isoformat(timespec="microseconds"),
            "timestamp_utc": event_time_utc.isoformat(timespec="microseconds"),
            "elapsed_s": repr(max(0.0, (now_ns - self.started_monotonic_ns) / 1e9)),
            "sample_index": self.sample_count,
            "mode": self.mode.value,
            "value_si": "" if value_si is None else repr(value_si),
            "unit_si": self.spec.unit_si,
            "event": event.strip(),
        }
        self._pending_event_index = row["event_index"]
        with self._csv_sync_lock:
            self._events.writerow(row)
            self._event_stream.flush()
            self.sync_csv_if_due(force=True)
        self.event_count = row["event_index"]
        self._pending_event_index = None
        self.metadata["event_count"] = self.event_count
        self._write_metadata()
        self.log("EVENT", event.strip())
        return row

    def record_measurement_speed_change(
        self,
        *,
        nplc: float | None,
        ac_bandwidth_hz: int | None,
        digital_filter: bool,
        device_configuration: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist acknowledged integration, bandwidth, or digital-filter changes."""
        self._ensure_no_pending_csv_record()
        previous_nplc = self.settings.nplc
        previous_band = self.settings.ac_bandwidth_hz
        previous_digital_filter = self.settings.digital_filter if self.spec.digital_filter else None
        self.settings = replace(
            self.settings,
            nplc=nplc,
            ac_bandwidth_hz=ac_bandwidth_hz,
            digital_filter=digital_filter if self.spec.digital_filter else False,
        )
        active_settings = self.metadata.setdefault("mode_settings", {})
        active_settings["nplc"] = nplc
        active_settings["ac_bandwidth_hz"] = ac_bandwidth_hz
        active_settings["digital_filter"] = digital_filter if self.spec.digital_filter else None
        self.metadata["actual_device_configuration"] = dict(device_configuration)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone()
        now_ns = time.perf_counter_ns()
        change = {
            "timestamp_local": now_local.isoformat(timespec="microseconds"),
            "timestamp_utc": now_utc.isoformat(timespec="microseconds"),
            "elapsed_s": max(0.0, (now_ns - (self.started_monotonic_ns or now_ns)) / 1e9),
            "effective_sample_index": self.sample_count + 1,
            "previous_nplc": previous_nplc,
            "nplc": nplc,
            "previous_ac_bandwidth_hz": previous_band,
            "ac_bandwidth_hz": ac_bandwidth_hz,
            "previous_digital_filter": previous_digital_filter,
            "digital_filter": digital_filter if self.spec.digital_filter else None,
        }
        self.metadata.setdefault("measurement_speed_changes", []).append(change)
        self._write_metadata()
        pieces = []
        if nplc is not None:
            pieces.append(f"NPLC {previous_nplc:g}→{nplc:g}")
        if ac_bandwidth_hz is not None:
            pieces.append(f"交流带宽 {previous_band}→{ac_bandwidth_hz} Hz")
        if self.spec.digital_filter and previous_digital_filter != digital_filter:
            pieces.append(
                "直流数字滤波 "
                f"{'开' if previous_digital_filter else '关'}→{'开' if digital_filter else '关'}"
            )
        description = "、".join(pieces) or "测量速度参数无变化"
        message = f"测量速度已应用：{description}；从样本 {change['effective_sample_index']} 起生效"
        return self.append_event(message)

    def finish(self, status: str = "stopped") -> None:
        try:
            self._finish_experiment(status)
        finally:
            streams = (self._data_stream, self._event_stream, self._log_stream)
            sync_thread = self._csv_sync_thread
            sync_stopped = sync_thread is None or not sync_thread.is_alive()
            if sync_stopped and not all(stream is None or stream.closed for stream in streams):
                # A transient close failure must not strand the directory lease
                # after the worker drops its recorder reference.
                self._close_open_streams(suppress_errors=True)
                streams = (self._data_stream, self._event_stream, self._log_stream)
            if all(stream is None or stream.closed for stream in streams):
                self._close_directory_lease(suppress_errors=True)

    def _finish_experiment(self, status: str) -> None:
        self.stop_periodic_sync()
        streams = (self._data_stream, self._event_stream, self._log_stream)
        if all(stream is None or stream.closed for stream in streams):
            return
        # Preserve asynchronous sync failures even if their queued Qt notification
        # has not reached the worker before a user stop/finalization.
        first_error: Exception | None = self.csv_sync_error or self.log_sync_error
        log_succeeded = False

        csv_sync_succeeded = False
        try:
            self.sync_csv_if_due(force=True)
            if self._pending_sample_index is not None:
                self.sample_count = self._reconcile_pending_csv_record(
                    "measurements.csv",
                    current_count=self.sample_count,
                    pending_index=self._pending_sample_index,
                    index_field="sample_index",
                )
                self.metadata["sample_count"] = self.sample_count
                self._pending_sample_index = None
            if self._pending_event_index is not None:
                self.event_count = self._reconcile_pending_csv_record(
                    "events.csv",
                    current_count=self.event_count,
                    pending_index=self._pending_event_index,
                    index_field="event_index",
                )
                self.metadata["event_count"] = self.event_count
                self._pending_event_index = None
            # A later successful final sync cannot erase a prior interval sync
            # failure: the promised periodic persistence was already interrupted.
            csv_sync_succeeded = self.csv_sync_error is None
        except Exception as exc:
            if first_error is None:
                first_error = exc

        unresolved = []
        if self._pending_sample_index is not None:
            unresolved.append("测量记录")
        if self._pending_event_index is not None:
            unresolved.append("事件记录")
        csv_sync_error = self.csv_sync_error
        log_sync_error = self.log_sync_error
        ending_status = (
            "storage_error"
            if csv_sync_error is not None or log_sync_error is not None
            else status
        )
        ending = f"实验结束；状态={ending_status}；测量尝试={self.sample_count}；事件={self.event_count}"
        if csv_sync_error is not None:
            ending += f"；CSV同步失败={csv_sync_error}"
        if log_sync_error is not None:
            ending += f"；run.log同步失败={log_sync_error}"
        if unresolved:
            ending += f"；以下记录写入状态未确认：{'、'.join(unresolved)}"
        try:
            self.log("INFO", ending)
            log_succeeded = True
        except Exception as exc:
            if first_error is None:
                first_error = exc

        log_sync_succeeded = False
        try:
            self._sync_log_stream()
            log_sync_succeeded = self.log_sync_error is None
        except Exception as exc:
            if first_error is None:
                first_error = exc

        streams_closed = True
        for stream in streams:
            if stream is None or stream.closed:
                continue
            try:
                stream.flush()
            except Exception as exc:
                streams_closed = False
                if first_error is None:
                    first_error = exc
            try:
                stream.close()
            except Exception as exc:
                streams_closed = False
                if first_error is None:
                    first_error = exc
            if not stream.closed:
                streams_closed = False

        # Persist a clean completion marker only after the measurement CSV,
        # event CSV, and run log have been flushed/fsynced and all streams have
        # closed successfully. Otherwise stale-run recovery still sees "running".
        if log_succeeded and csv_sync_succeeded and log_sync_succeeded and streams_closed:
            completion_fields = {
                "sample_count": self.sample_count,
                "event_count": self.event_count,
                "completion_status": status,
                "finished_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            }
            previous_values = {key: self.metadata.get(key) for key in completion_fields}
            missing = object()
            for key in completion_fields:
                if key not in self.metadata:
                    previous_values[key] = missing
            self.metadata.update(completion_fields)
            try:
                self._write_metadata()
            except Exception as exc:
                for key, previous in previous_values.items():
                    if previous is missing:
                        self.metadata.pop(key, None)
                    else:
                        self.metadata[key] = previous
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _reconcile_pending_csv_record(
        self,
        filename: str,
        *,
        current_count: int,
        pending_index: int,
        index_field: str,
    ) -> int:
        """Reconcile an ambiguous last write using only complete CSV records."""
        if pending_index != current_count + 1:
            raise ValueError(f"{filename} 的待确认记录序号不连续；拒绝猜测持久化数量")
        path = self.directory / filename
        if _is_symlink_or_junction(path):
            raise OSError(f"待确认的 {filename} 不能是符号链接或 junction：{path}")
        before = path.stat()
        prefix_bytes = complete_csv_prefix_size(path)
        persisted_count = _count_complete_csv_records(path, prefix_bytes, index_field)
        after = path.stat()
        before_signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_signature = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_signature != after_signature:
            raise OSError(f"核对待确认记录期间 {filename} 发生变化；拒绝猜测持久化数量：{path}")
        if persisted_count not in (current_count, pending_index):
            raise ValueError(
                f"{filename} 中完整记录数 {persisted_count} 与待确认序号不符；拒绝猜测持久化数量"
            )
        return persisted_count

    def _write_metadata(self) -> None:
        target = self.directory / "config.json"
        temp = self.directory / f"config.json.{uuid.uuid4().hex}.tmp"
        created_identity: tuple[int, int] | None = None
        expected_identity = self._config_file_identity
        try:
            self._assert_output_path(
                target,
                must_not_exist=expected_identity is None,
                expected_identity=expected_identity,
            )
            self._assert_output_path(temp, must_not_exist=True)
            with temp.open("x", encoding="utf-8") as stream:
                created_identity = self._file_identity(os.fstat(stream.fileno()))
                json.dump(
                    self.metadata,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                    allow_nan=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())

            # Windows can briefly deny a rename while another process holds a
            # handle without delete sharing. Recheck both identities on every
            # attempt and preserve the atomic replace and eventual error.
            for attempt in range(4):
                self._assert_output_path(
                    temp, must_not_exist=False, expected_identity=created_identity
                )
                self._assert_output_path(
                    target,
                    must_not_exist=expected_identity is None,
                    expected_identity=expected_identity,
                )
                try:
                    os.replace(temp, target)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) != 32 or attempt == 3:
                        raise
                    Event().wait(0.05 * (2 ** attempt))
            self._assert_output_path(
                target, must_not_exist=False, expected_identity=created_identity
            )
            self._config_file_identity = created_identity
        except BaseException:
            try:
                if created_identity is not None and temp.exists():
                    self._assert_output_path(
                        temp, must_not_exist=False, expected_identity=created_identity
                    )
                    temp.unlink()
            except OSError:
                log.exception("failed to remove incomplete experiment metadata temp file")
            raise

    def _flush_streams(self) -> None:
        for stream in (getattr(self, "_data_stream", None), getattr(self, "_event_stream", None), getattr(self, "_log_stream", None)):
            if stream is not None and not stream.closed:
                stream.flush()

    def sync_csv_if_due(self, *, force: bool = False) -> None:
        """Synchronize open CSV streams when due, including during idle periods."""
        self._sync_csv_streams(force=force)

    @property
    def csv_sync_error(self) -> Exception | None:
        """Return the first CSV sync failure, retained through finalization."""
        with self._csv_sync_lock:
            return self._csv_sync_error

    @property
    def log_sync_error(self) -> Exception | None:
        """Return the first run.log sync failure, retained through finalization."""
        with self._log_sync_lock:
            return self._log_sync_error

    def _sync_csv_streams(self, *, force: bool = False) -> None:
        with self._csv_sync_lock:
            now_ns = time.perf_counter_ns()
            if not force and now_ns - self._last_csv_fsync_ns < CSV_FSYNC_INTERVAL_NS:
                return
            try:
                for stream in (self._data_stream, self._event_stream):
                    if stream is not None and not stream.closed:
                        stream.flush()
                        os.fsync(stream.fileno())
            except Exception as exc:
                if self._csv_sync_error is None:
                    self._csv_sync_error = exc
                raise
            self._last_csv_fsync_ns = now_ns

    def _sync_log_stream(self) -> None:
        with self._log_sync_lock:
            stream = self._log_stream
            if stream is not None and not stream.closed:
                try:
                    stream.flush()
                    os.fsync(stream.fileno())
                except Exception as exc:
                    if self._log_sync_error is None:
                        self._log_sync_error = exc
                    raise

    def sync_log_if_due(self, *, force: bool = False) -> None:
        """Synchronize run.log at a bounded interval, retaining the first failure."""
        with self._log_sync_lock:
            now_ns = time.perf_counter_ns()
            if not force and now_ns - self._last_log_fsync_ns < LOG_FSYNC_INTERVAL_NS:
                return
            self._sync_log_stream()
            self._last_log_fsync_ns = now_ns

