"""Durable per-experiment CSV/JSON/log output with one append per acquisition."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .modes import Mode, descriptor
from .scpi import ModeSettings, Reading

log = logging.getLogger(__name__)

SAMPLE_FIELDS = (
    "sample_index", "timestamp_local", "timestamp_utc", "elapsed_s", "read_duration_s",
    "actual_interval_s", "mode", "quantity", "value_si", "unit_si", "raw_reading",
    "voltage_v", "current_a", "resistance_ohm", "range_si", "nplc", "ac_bandwidth_hz",
    "analog_filter", "autozero", "measurement_basis", "status", "error",
)
EVENT_FIELDS = ("event_index", "timestamp_local", "timestamp_utc", "elapsed_s", "sample_index", "mode", "value_si", "unit_si", "event")


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Mode):
        return obj.value
    raise TypeError(f"无法序列化类型：{type(obj).__name__}")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^\w\-一-鿿]+", "_", value.strip(), flags=re.UNICODE).strip("_-")
    return cleaned[:64] or "experiment"


class ExperimentRecorder:
    """Create a fresh run folder and flush each row after writing it."""

    def __init__(self, directory: Path, metadata: dict[str, Any], settings: ModeSettings) -> None:
        self.directory = directory
        self.metadata = dict(metadata)
        self.settings = settings
        self.mode = settings.mode
        self.spec = descriptor(self.mode)
        self.started_monotonic_ns: int | None = None
        self.last_attempt_ns: int | None = None
        self.sample_count = 0
        self.event_count = 0
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
            self.metadata.update({
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
                    "autozero": self.settings.autozero,
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
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        base = root / f"{stamp}_{_slug(name)}"
        directory = base
        suffix = 1
        while directory.exists():
            directory = root / f"{base.name}_{suffix}"
            suffix += 1
        directory.mkdir()
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
            "measurement_basis": "true RMS" if self.spec.rms else "direct reading",
            "status": status,
            "error": error,
        }
        self._writer.writerow(row)
        self._data_stream.flush()
        self._log_stream.flush()
        if self.sample_count % 100 == 0:
            self.metadata["sample_count"] = self.sample_count
            self._write_metadata()
        return row

    def append_event(self, event: str, *, value_si: float | None = None) -> dict[str, Any]:
        if not event.strip():
            raise ValueError("事件内容不能为空")
        import time
        from datetime import datetime, timezone

        now_ns = time.monotonic_ns()
        if self.started_monotonic_ns is None:
            self.started_monotonic_ns = now_ns
        event_time_utc = datetime.now(timezone.utc)
        event_time_local = event_time_utc.astimezone()
        row = {
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
        self.settings = ModeSettings(
            mode=self.settings.mode,
            autorange=self.settings.autorange,
            range_si=self.settings.range_si,
            nplc=nplc,
            ac_bandwidth_hz=ac_bandwidth_hz,
            analog_filter=self.settings.analog_filter,
            autozero=self.settings.autozero,
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

