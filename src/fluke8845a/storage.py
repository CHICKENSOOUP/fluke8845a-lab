"""Durable per-experiment CSV/JSON/log output with one append per acquisition."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .modes import Mode, descriptor
from .scpi import ModeSettings, Reading

log = logging.getLogger(__name__)

# Reserve enough room to create the run metadata, CSV headers, and log before
# touching the instrument. This is only a startup floor; it cannot predict the
# storage needed by a multi-hour or multi-day experiment.
MINIMUM_FREE_SPACE_BYTES = 10 * 1024 * 1024
STORAGE_SPACE_CHECK_INTERVAL_NS = 5_000_000_000
CSV_FSYNC_INTERVAL_NS = 5_000_000_000
_INVALID_EXPERIMENT_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

SAMPLE_FIELDS = (
    "experiment_id", "sample_index", "timestamp_local", "timestamp_utc", "elapsed_s", "read_duration_s",
    "actual_interval_s", "mode", "quantity", "value_si", "unit_si", "raw_reading",
    "voltage_v", "current_a", "resistance_ohm", "range_si", "nplc", "ac_bandwidth_hz",
    "analog_filter", "autozero", "input_impedance_auto", "measurement_basis", "status", "error",
)
EVENT_FIELDS = ("experiment_id", "event_index", "timestamp_local", "timestamp_utc", "elapsed_s", "sample_index", "mode", "value_si", "unit_si", "event")


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


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Mode):
        return obj.value
    raise TypeError(f"无法序列化类型：{type(obj).__name__}")


def _escape_excel_text_fields(
    row: dict[str | None, str | None], fields: tuple[str, ...]
) -> dict[str | None, str | None]:
    if None in row or any(value is None for value in row.values()):
        raise ValueError("CSV 存在列数不一致的记录，已拒绝导出以免丢失字段")
    for field in fields:
        value = row.get(field)
        if value is None:
            continue
        first_visible = next((char for char in value if not (char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F)), "")
        if first_visible in {"=", "+", "-", "@"}:
            row[field] = "'" + value
    return row


def _escape_excel_event(row: dict[str | None, str | None]) -> dict[str | None, str | None]:
    return _escape_excel_text_fields(row, ("event",))


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

    Only the free-text ``event`` column is escaped. Measurement values and other
    metadata remain byte-for-value equivalent after CSV parsing.
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
    protected_text_columns: tuple[str, ...],
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
                writer.writerow(_escape_excel_text_fields(row, protected_text_columns))
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
        protected_text_columns=("event",),
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
        protected_text_columns=("raw_reading", "error"),
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


def find_existing_experiment_directories(root: Path | str, name: str) -> list[Path]:
    """Find prior app-created run folders with the same visible experiment name."""
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


def complete_csv_prefix_size(path: Path | str, chunk_size: int = 64 * 1024) -> int:
    """Return bytes through the last complete CSV record, ignoring quoted newlines."""
    if chunk_size < 1:
        raise ValueError("CSV扫描块大小必须大于0")
    csv_path = Path(path)
    size = csv_path.stat().st_size
    if size == 0:
        return 0
    in_quotes = False
    pending_quote = False
    position = 0
    complete_end = 0
    with csv_path.open("rb") as stream:
        while position < size:
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
    return complete_end


class ExperimentRecorder:
    """Create a fresh run folder and periodically sync appended CSV records."""

    def __init__(self, directory: Path, metadata: dict[str, Any], settings: ModeSettings) -> None:
        self.directory = directory
        self.metadata = dict(metadata)
        self.experiment_id = uuid.uuid4().hex
        self.settings = settings
        self.mode = settings.mode
        self.spec = descriptor(self.mode)
        self.started_monotonic_ns: int | None = None
        self.last_attempt_ns: int | None = None
        self.sample_count = 0
        self.event_count = 0
        self._last_space_check_ns = 0
        self._last_csv_fsync_ns = time.monotonic_ns()
        self._data_stream = None
        self._event_stream = None
        self._log_stream = None
        self._opened_streams = []
        try:
            self._data_stream = self._open_stream(directory / "measurements.csv", "w", newline="", encoding="utf-8-sig")
            self._event_stream = self._open_stream(directory / "events.csv", "w", newline="", encoding="utf-8-sig")
            self._log_stream = self._open_stream(directory / "run.log", "w", encoding="utf-8")
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
                "completion_status": "running",
            })
            self._write_metadata()
            self.log("INFO", f"实验开始；数据仿真={self.metadata['simulation']}；模式={self.spec.label}")
        except BaseException:
            self._close_open_streams(suppress_errors=True)
            raise

    def _open_stream(self, path: Path, *args: Any, **kwargs: Any):
        stream = path.open(*args, **kwargs)
        self._opened_streams.append(stream)
        return stream

    def check_storage_reserve(self) -> None:
        now_ns = time.monotonic_ns()
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

    @classmethod
    def create(
        cls,
        root: Path,
        name: str,
        metadata: dict[str, Any],
        settings: ModeSettings,
    ) -> "ExperimentRecorder":
        name = validate_experiment_name(name)
        root = prepare_experiment_storage(root)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        base = root / f"{stamp}_{_slug(name)}"
        directory = base
        suffix = 1
        while directory.exists():
            directory = root / f"{base.name}_{suffix}"
            suffix += 1
        directory.mkdir()
        metadata = dict(metadata)
        metadata["experiment_name"] = name
        return cls(directory, metadata, settings)

    def log(self, level: str, message: str) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
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
        read_duration_s: float | None = None,
        status: str = "ok",
        error: str = "",
        raw_reading: str = "",
    ) -> dict[str, Any]:
        if mode != self.mode:
            raise ValueError("一个实验数据段中不能混合不同测量模式")
        self.check_storage_reserve()
        if reading is not None:
            started_monotonic_ns = reading.started_monotonic_ns
            timestamp_local = reading.timestamp_local
            timestamp_utc = reading.timestamp_utc
            read_duration_s = reading.read_duration_s
            status = reading.status
        if started_monotonic_ns is None:
            import time
            started_monotonic_ns = time.monotonic_ns()
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
        if self.started_monotonic_ns is None:
            self.started_monotonic_ns = started_monotonic_ns
        elapsed = max(0.0, (started_monotonic_ns - self.started_monotonic_ns) / 1e9)
        interval = None if self.last_attempt_ns is None else max(0.0, (started_monotonic_ns - self.last_attempt_ns) / 1e9)
        self.last_attempt_ns = started_monotonic_ns
        self.sample_count += 1
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
            "sample_index": self.sample_count,
            "timestamp_local": timestamp_local,
            "timestamp_utc": timestamp_utc,
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
            "autozero": self.settings.autozero,
            "input_impedance_auto": (
                self.settings.input_impedance_auto if self.spec.input_impedance_auto else ""
            ),
            "measurement_basis": "true RMS" if self.spec.rms else "direct reading",
            "status": status,
            "error": error,
        }
        self._writer.writerow(row)
        self._data_stream.flush()
        self._log_stream.flush()
        self._sync_csv_streams()
        if self.sample_count % 100 == 0:
            self.metadata["sample_count"] = self.sample_count
            self._write_metadata()
        return row

    def append_event(self, event: str, *, value_si: float | None = None) -> dict[str, Any]:
        if not event.strip():
            raise ValueError("事件内容不能为空")
        self.check_storage_reserve()
        import time
        from datetime import datetime, timezone

        now_ns = time.monotonic_ns()
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
        self._events.writerow(row)
        self.event_count += 1
        self._event_stream.flush()
        self._sync_csv_streams(force=True)
        self.metadata["event_count"] = self.event_count
        self._write_metadata()
        self.log("EVENT", event.strip())
        return row

    def record_measurement_speed_change(
        self,
        *,
        nplc: float | None,
        ac_bandwidth_hz: int | None,
        device_configuration: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an acknowledged speed change before any subsequent sample."""
        previous_nplc = self.settings.nplc
        previous_band = self.settings.ac_bandwidth_hz
        self.settings = replace(
            self.settings, nplc=nplc, ac_bandwidth_hz=ac_bandwidth_hz
        )
        active_settings = self.metadata.setdefault("mode_settings", {})
        active_settings["nplc"] = nplc
        active_settings["ac_bandwidth_hz"] = ac_bandwidth_hz
        self.metadata["actual_device_configuration"] = dict(device_configuration)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone()
        now_ns = time.monotonic_ns()
        change = {
            "timestamp_local": now_local.isoformat(timespec="microseconds"),
            "timestamp_utc": now_utc.isoformat(timespec="microseconds"),
            "elapsed_s": max(0.0, (now_ns - (self.started_monotonic_ns or now_ns)) / 1e9),
            "effective_sample_index": self.sample_count + 1,
            "previous_nplc": previous_nplc,
            "nplc": nplc,
            "previous_ac_bandwidth_hz": previous_band,
            "ac_bandwidth_hz": ac_bandwidth_hz,
        }
        self.metadata.setdefault("measurement_speed_changes", []).append(change)
        self._write_metadata()
        pieces = []
        if nplc is not None:
            pieces.append(f"NPLC {previous_nplc:g}→{nplc:g}")
        if ac_bandwidth_hz is not None:
            pieces.append(f"交流带宽 {previous_band}→{ac_bandwidth_hz} Hz")
        description = "、".join(pieces) or "测量速度参数无变化"
        message = f"测量速度已应用：{description}；从样本 {change['effective_sample_index']} 起生效"
        return self.append_event(message)

    def finish(self, status: str = "stopped") -> None:
        streams = (self._data_stream, self._event_stream, self._log_stream)
        if all(stream is None or stream.closed for stream in streams):
            return
        self.metadata.update({"sample_count": self.sample_count, "event_count": self.event_count, "completion_status": status,
                              "finished_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds")})
        first_error: Exception | None = None
        for action in (
            lambda: self.log("INFO", f"实验结束；状态={status}；测量尝试={self.sample_count}；事件={self.event_count}"),
            self._write_metadata,
            lambda: self._sync_csv_streams(force=True),
        ):
            try:
                action()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        for stream in streams:
            if stream is None or stream.closed:
                continue
            try:
                stream.flush()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            try:
                stream.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _write_metadata(self) -> None:
        temp = self.directory / "config.json.tmp"
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(self.metadata, stream, ensure_ascii=False, indent=2, default=_json_default, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.directory / "config.json")

    def _flush_streams(self) -> None:
        for stream in (getattr(self, "_data_stream", None), getattr(self, "_event_stream", None), getattr(self, "_log_stream", None)):
            if stream is not None and not stream.closed:
                stream.flush()

    def _sync_csv_streams(self, *, force: bool = False) -> None:
        now_ns = time.monotonic_ns()
        if not force and now_ns - self._last_csv_fsync_ns < CSV_FSYNC_INTERVAL_NS:
            return
        for stream in (self._data_stream, self._event_stream):
            if stream is not None and not stream.closed:
                stream.flush()
                os.fsync(stream.fileno())
        self._last_csv_fsync_ns = now_ns

