"""Thread-safe bridge from the localhost API to the production acquisition worker."""

from __future__ import annotations

import math
import csv
import re
import threading
import time
import uuid
from bisect import bisect_left
from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices

from .filter_policy import digital_filter_setting_error
from .mock import Mock8845A, MockScenario
from .modes import DESCRIPTORS, Mode, descriptor, range_label
from .path_security import resolve_safe_child_file
from .scpi import (
    CURRENT_RANGE_FIRMWARE_MINIMUM, FRONT_ONLY_CURRENT_RANGES,
    FlowControl, ModeSettings, SerialSettings,
    current_autorange_block_reason, current_firmware_gate_evidence_note,
    select_range_firmware, unsupported_current_ranges,
)
from .versioning import compare_firmware
from .storage import (
    estimate_recording_capacity_seconds, find_existing_experiment_directories,
    format_available_space, format_estimated_recording_capacity,
    get_available_space_bytes, prepare_experiment_storage,
    require_minimum_free_space, validate_experiment_name,
)
from .state import AcquisitionState
from .worker import MeasurementWorker

_SAMPLE_BUFFER_SIZE = 5000
_LOG_BUFFER_SIZE = 300
_EVENT_BUFFER_SIZE = 200
_CHART_OVERVIEW_MAX_BUCKETS = 4096
_CHART_INDEX_CHECKPOINT_LIMIT = 8192
_CHART_INDEX_CHECKPOINT_STRIDE = 256
_CHART_RANGE_JOB_LIMIT = 8
_CHART_RANGE_JOB_TTL_S = 120.0
_CHART_CANCEL_TOMBSTONE_LIMIT = 128


def _chart_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _chart_id(value) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


class _ChartBucket:
    """Keep bounded representatives for one time interval or display pixel."""

    def __init__(self) -> None:
        self._representatives: list[dict] = []

    def points(self) -> list[dict]:
        return list(self._representatives)

    def add(self, point: dict) -> None:
        candidates = self.points() + [point]
        candidates.sort(key=lambda item: (item["x"], item["id"]))
        if not candidates:
            return
        gap_indices = [
            index for index, item in enumerate(candidates)
            if item.get("break_before")
        ]
        selected_indices = {
            0,
            len(candidates) - 1,
            min(range(len(candidates)), key=lambda index: candidates[index]["y"]),
            max(range(len(candidates)), key=lambda index: candidates[index]["y"]),
        }
        if gap_indices:
            selected_indices.update((gap_indices[0], gap_indices[-1]))
        representatives: list[dict] = []
        previous = -1
        for index in sorted(selected_indices):
            representative = dict(candidates[index])
            representative["break_before"] = any(
                bool(item.get("break_before"))
                for item in candidates[previous + 1:index + 1]
            )
            representatives.append(representative)
            previous = index
        self._representatives = representatives

    def merge(self, other: "_ChartBucket") -> None:
        for point in other.points():
            self.add(point)


class _ChartOverview:
    """Streaming, bounded full-experiment summary for the live Web chart."""

    def __init__(self) -> None:
        self.bucket_width = 1.0
        self.buckets: dict[int, _ChartBucket] = {}
        self.attempts = 0
        self.valid = 0
        self.first_x: float | None = None
        self.last_x: float | None = None

    def add_attempt(self, row: dict, previous_id: int | None, pending_break: bool) -> tuple[int | None, bool, dict | None]:
        self.attempts += 1
        sample_id = _chart_id(row.get("sample_index"))
        elapsed = _chart_number(row.get("elapsed_s"))
        value = _chart_number(row.get("value_si"))
        status = str(row.get("status") or "")
        if sample_id is not None and previous_id is not None and sample_id != previous_id + 1:
            pending_break = True
        if status != "ok" or sample_id is None or elapsed is None or value is None:
            return sample_id if sample_id is not None else previous_id, True, None

        point = {
            "id": sample_id,
            "x": elapsed,
            "y": value,
            "status": "ok",
            "raw": str(row.get("raw_reading") or ""),
            "mode": str(row.get("mode") or ""),
            "unit": str(row.get("unit_si") or ""),
            "range": row.get("range_si"),
            "timestamp": str(row.get("timestamp_local") or ""),
            "error": "",
            "break_before": bool(pending_break),
            "summary": True,
        }
        self.valid += 1
        self.first_x = elapsed if self.first_x is None else min(self.first_x, elapsed)
        self.last_x = elapsed if self.last_x is None else max(self.last_x, elapsed)
        self._ensure_capacity(elapsed)
        bucket_index = max(0, int(elapsed // self.bucket_width))
        self.buckets.setdefault(bucket_index, _ChartBucket()).add(point)
        return sample_id, False, point

    def _ensure_capacity(self, elapsed: float) -> None:
        while elapsed >= self.bucket_width * _CHART_OVERVIEW_MAX_BUCKETS:
            merged: dict[int, _ChartBucket] = {}
            for index in sorted(self.buckets):
                target = index // 2
                merged.setdefault(target, _ChartBucket()).merge(self.buckets[index])
            self.buckets = merged
            self.bucket_width *= 2

    def points(self) -> list[dict]:
        result: list[dict] = []
        for index in sorted(self.buckets):
            result.extend(self.buckets[index].points())
        return result


class _BinaryCsvLines:
    """A bounded binary CSV line iterator with exact byte offsets."""

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


class _ChartIndexJob:
    def __init__(
        self,
        path: Path,
        active_check,
        source_signature: tuple[int, int, int, int, int] | None = None,
    ) -> None:
        self.job_id = uuid.uuid4().hex
        self.path = path
        self.active_check = active_check
        self.source_signature = source_signature
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.status = "scanning"
        self.error = ""
        self.header: list[str] = []
        self.offset = 0
        self.total_bytes = 0
        self.attempts = 0
        self.previous_id: int | None = None
        self.pending_break = False
        self.overview = _ChartOverview()
        self.checkpoints: list[tuple[float, int, int | None, bool]] = []
        self.checkpoint_stride = _CHART_INDEX_CHECKPOINT_STRIDE
        self.revision = 0
        self.max_elapsed = -math.inf
        self.thread: threading.Thread | None = None

    def snapshot(self, width: int) -> dict:
        with self.lock:
            rows = _downsample_chart_points(self.overview.points(), self.overview.first_x, self.overview.last_x, width)
            return {
                "job_id": self.job_id,
                "status": self.status,
                "error": self.error,
                "revision": self.revision,
                "total_attempts": self.overview.attempts,
                "total_valid": self.overview.valid,
                "duration_s": self.overview.last_x if self.overview.last_x is not None else 0,
                "scanned_bytes": self.offset,
                "total_bytes": self.total_bytes,
                "rows": rows,
            }


class _ChartRangeJob:
    def __init__(self, request_id: str, index_job: _ChartIndexJob, start_s: float, end_s: float, width: int) -> None:
        self.request_id = request_id
        self.index_job = index_job
        self.start_s = start_s
        self.end_s = end_s
        self.width = width
        self.created = time.monotonic()
        self.finished_at: float | None = None
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.status = "running"
        self.error = ""
        self.rows: list[dict] = []
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "request_id": self.request_id,
                "job_id": self.index_job.job_id,
                "status": self.status,
                "error": self.error,
                "start_s": self.start_s,
                "end_s": self.end_s,
                "rows": list(self.rows),
            }


def _downsample_chart_points(rows: list[dict], x_min: float | None, x_max: float | None, width: int) -> list[dict]:
    if not rows or x_min is None or x_max is None or x_max <= x_min:
        return list(rows)
    buckets: dict[int, _ChartBucket] = {}
    for point in rows:
        pixel = max(0, min(width - 1, int((point["x"] - x_min) / (x_max - x_min) * width)))
        buckets.setdefault(pixel, _ChartBucket()).add(point)
    result: list[dict] = []
    for pixel in sorted(buckets):
        result.extend(buckets[pixel].points())
    return result


class WebChartHistory:
    """Background CSV index and bounded chart-range reader for one recent experiment."""

    def __init__(self, store: "WebStateStore") -> None:
        self._store = store
        self._lock = threading.RLock()
        self._job: _ChartIndexJob | None = None
        self._range_jobs: dict[str, _ChartRangeJob] = {}
        self._cancelled_range_ids: dict[str, float] = {}
        self._closed = False

    @staticmethod
    def _safe_csv_path(state: dict) -> Path | None:
        directory_text = state.get("experiment_dir") or state.get("last_experiment_dir") or ""
        if not isinstance(directory_text, str) or not directory_text.strip():
            return None
        return resolve_safe_child_file(directory_text, "measurements.csv")

    def _job_is_current(self, path: Path) -> bool:
        state = self._store.snapshot()
        current = self._safe_csv_path(state)
        return current == path

    @staticmethod
    def _source_signature(path: Path) -> tuple[int, int, int, int, int] | None:
        try:
            info = path.stat()
        except OSError:
            return None
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )

    def _ensure_index_job(self) -> _ChartIndexJob | None:
        state = self._store.snapshot()
        path = self._safe_csv_path(state)
        if path is None:
            return None
        source_signature = self._source_signature(path)
        with self._lock:
            if self._closed:
                return None
            if self._job is not None and self._job.path == path:
                with self._job.lock:
                    if self._job.status in {"scanning", "ready"}:
                        return self._job
                    if (
                        self._job.status == "error"
                        and self._job.source_signature == source_signature
                    ):
                        return self._job
            if self._job is not None:
                self._job.cancel_event.set()
                with self._job.lock:
                    if self._job.status == "scanning":
                        self._job.status = "cancelled"
            job = _ChartIndexJob(
                path,
                lambda: self._job_is_current(path),
                source_signature=source_signature,
            )
            self._job = job
            job.thread = threading.Thread(target=self._scan_index, args=(job,), name="web-chart-index", daemon=True)
            job.thread.start()
            return job

    def overview(self, width: int = 1200) -> tuple[int, dict]:
        job = self._ensure_index_job()
        if job is None:
            return 404, {"error": "当前没有可读取的实验 measurements.csv"}
        try:
            width = int(width)
        except (TypeError, ValueError, OverflowError):
            return 400, {"error": "图表宽度必须是整数"}
        if not 1 <= width <= 4096:
            return 400, {"error": "图表宽度须为1至4096像素"}
        return 200, job.snapshot(width)

    def _add_checkpoint(self, job: _ChartIndexJob, elapsed: float, offset: int) -> None:
        """Update checkpoint state while the caller holds job.lock."""
        if job.attempts % job.checkpoint_stride:
            return
        checkpoint = (elapsed, offset, job.previous_id, job.pending_break)
        if not job.checkpoints or job.checkpoints[-1][1] != offset:
            job.checkpoints.append(checkpoint)
        if len(job.checkpoints) > _CHART_INDEX_CHECKPOINT_LIMIT:
            first, last = job.checkpoints[0], job.checkpoints[-1]
            job.checkpoints = [first, *job.checkpoints[1:-1:2], last]
            job.checkpoint_stride *= 2

    def _scan_index(self, job: _ChartIndexJob) -> None:
        try:
            while not job.cancel_event.is_set():
                if not job.active_check():
                    job.cancel_event.set()
                    break
                try:
                    total_size = job.path.stat().st_size
                except OSError:
                    if not job.active_check():
                        job.cancel_event.set()
                        break
                    time.sleep(0.1)
                    continue
                with job.lock:
                    job.total_bytes = total_size
                    offset = job.offset
                    header = list(job.header)
                if offset >= total_size:
                    if job.active_check() and self._store.snapshot().get("experiment_dir"):
                        job.cancel_event.wait(0.2)
                        continue
                    break
                with job.path.open("rb") as stream:
                    stream.seek(offset)
                    lines = _BinaryCsvLines(stream, total_size, strip_bom=not header)
                    reader = csv.reader(lines, strict=True)
                    if not header:
                        try:
                            parsed_header = next(reader)
                        except StopIteration:
                            break
                        if "elapsed_s" not in parsed_header or "sample_index" not in parsed_header or "value_si" not in parsed_header:
                            raise ValueError("measurements.csv 缺少图表所需列")
                        header = parsed_header
                        with job.lock:
                            job.header = list(header)
                            job.offset = stream.tell()
                            job.checkpoints = [(-math.inf, job.offset, None, False)]
                            job.revision += 1
                    while not job.cancel_event.is_set():
                        row_start = stream.tell()
                        try:
                            values = next(reader)
                        except StopIteration:
                            break
                        except csv.Error:
                            stream.seek(row_start)
                            break
                        row_end = stream.tell()
                        if len(values) != len(header) or row_end <= row_start:
                            stream.seek(row_start)
                            break
                        stream.seek(row_end - 1)
                        row_terminated = stream.read(1) == b"\n"
                        stream.seek(row_end)
                        if not row_terminated:
                            stream.seek(row_start)
                            break
                        row = dict(zip(header, values))
                        elapsed = _chart_number(row.get("elapsed_s"))
                        with job.lock:
                            previous_id, pending_break, point = job.overview.add_attempt(
                                row, job.previous_id, job.pending_break,
                            )
                            job.previous_id = previous_id
                            job.pending_break = pending_break
                            if elapsed is not None:
                                job.max_elapsed = max(job.max_elapsed, elapsed)
                            job.attempts = job.overview.attempts
                            job.offset = row_end
                            if elapsed is not None:
                                self._add_checkpoint(job, elapsed, row_end)
                            if point is not None:
                                job.revision += 1
                            attempts = job.attempts
                        if attempts % 256 == 0 and not job.active_check():
                            job.cancel_event.set()
                            break
                if job.cancel_event.is_set():
                    break
                if job.active_check() and self._store.snapshot().get("experiment_dir"):
                    job.cancel_event.wait(0.2)
                    continue
                break
            with job.lock:
                if job.cancel_event.is_set() and job.active_check():
                    job.status = "cancelled"
                elif job.cancel_event.is_set():
                    job.status = "cancelled"
                else:
                    job.status = "ready"
                job.revision += 1
        except Exception as exc:
            with job.lock:
                job.status = "error"
                job.error = str(exc)
                job.revision += 1

    def start_range(self, job_id: str, request_id: str, start_s: float, end_s: float, width: int) -> tuple[int, dict]:
        job = self._ensure_index_job()
        if job is None or job.job_id != job_id:
            return 404, {"error": "图表索引已过期，请重新读取概览"}
        if not request_id or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id):
            return 400, {"error": "范围请求编号无效"}
        if not math.isfinite(start_s) or not math.isfinite(end_s) or start_s < 0 or end_s <= start_s:
            return 400, {"error": "图表范围无效"}
        if not 1 <= width <= 4096:
            return 400, {"error": "图表宽度须为1至4096像素"}
        with self._lock:
            self._cleanup_range_jobs()
            if request_id in self._cancelled_range_ids:
                return 200, {
                    "request_id": request_id, "job_id": job_id,
                    "status": "cancelled", "rows": [],
                }
            existing = self._range_jobs.get(request_id)
            if existing is not None:
                return 202, existing.snapshot()
            with job.lock:
                ready_for_range = job.status == "ready" or job.max_elapsed >= end_s
                if not ready_for_range:
                    return 202, {"request_id": request_id, "job_id": job_id, "status": "indexing", "rows": []}
                active_count = 0
                for existing_job in self._range_jobs.values():
                    with existing_job.lock:
                        thread_alive = existing_job.thread is not None and existing_job.thread.is_alive()
                        active_count += existing_job.status in {"running", "cancelling"} or thread_alive
                if active_count >= _CHART_RANGE_JOB_LIMIT:
                    return 429, {"error": "图表范围请求过多；请取消旧视区请求后重试"}
                request = _ChartRangeJob(request_id, job, start_s, end_s, width)
                self._range_jobs[request_id] = request
                request.thread = threading.Thread(target=self._run_range_scan, args=(request,), name="web-chart-range", daemon=True)
                request.thread.start()
                return 202, request.snapshot()

    def _cleanup_range_jobs(self) -> None:
        terminal: list[tuple[str, _ChartRangeJob, float]] = []
        expired = []
        now = time.monotonic()
        for request_id, expires_at in list(self._cancelled_range_ids.items()):
            if expires_at <= now:
                self._cancelled_range_ids.pop(request_id, None)
        for key, job in self._range_jobs.items():
            with job.lock:
                status = job.status
                thread_alive = job.thread is not None and job.thread.is_alive()
                retention_start = job.finished_at if job.finished_at is not None else job.created
            if status in {"running", "cancelling"} or thread_alive:
                continue
            if now - retention_start > _CHART_RANGE_JOB_TTL_S:
                expired.append(key)
            else:
                terminal.append((key, job, retention_start))
        for key in expired:
            self._range_jobs.pop(key, None)
        # Retain a bounded set of recent results while freeing active capacity.
        terminal.sort(key=lambda item: item[2], reverse=True)
        for key, _job, _retention_start in terminal[_CHART_RANGE_JOB_LIMIT:]:
            self._range_jobs.pop(key, None)

    def range_status(self, request_id: str) -> tuple[int, dict]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id or ""):
            return 400, {"error": "范围请求编号无效"}
        with self._lock:
            self._cleanup_range_jobs()
            request = self._range_jobs.get(request_id)
            if request is None:
                return 404, {"error": "图表范围请求不存在或已过期"}
        return 200, request.snapshot()

    def cancel_range(self, request_id: str) -> tuple[int, dict]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", request_id or ""):
            return 400, {"error": "范围请求编号无效"}
        with self._lock:
            self._cleanup_range_jobs()
            request = self._range_jobs.get(request_id)
            if request is None:
                self._cancelled_range_ids[request_id] = time.monotonic() + _CHART_RANGE_JOB_TTL_S
                if len(self._cancelled_range_ids) > _CHART_CANCEL_TOMBSTONE_LIMIT:
                    oldest = sorted(self._cancelled_range_ids.items(), key=lambda item: item[1])
                    for expired_id, _expires_at in oldest[:-_CHART_CANCEL_TOMBSTONE_LIMIT]:
                        self._cancelled_range_ids.pop(expired_id, None)
                return 200, {"request_id": request_id, "status": "cancelled"}
            request.cancel_event.set()
            with request.lock:
                if request.status == "running":
                    thread_alive = request.thread is not None and request.thread.is_alive()
                    if thread_alive:
                        request.status = "cancelling"
                    else:
                        request.status = "cancelled"
                        request.finished_at = time.monotonic()
                status = request.status
        return 200, {"request_id": request_id, "status": status}

    def _run_range_scan(self, request: _ChartRangeJob) -> None:
        """Run a range scan and finalize retention only after its reader returns."""
        try:
            self._scan_range(request)
        except Exception as exc:
            with request.lock:
                request.status = "error"
                request.error = str(exc)
        finally:
            with request.lock:
                if request.status in {"running", "cancelling"}:
                    request.status = "cancelled" if request.cancel_event.is_set() else "ready"
                request.finished_at = time.monotonic()

    def _scan_range(self, request: _ChartRangeJob) -> None:
        index = request.index_job
        try:
            with index.lock:
                checkpoints = list(index.checkpoints)
                header = list(index.header)
            if not header or not checkpoints:
                raise ValueError("图表索引尚未就绪")
            checkpoint_times = [item[0] for item in checkpoints]
            checkpoint = checkpoints[max(0, bisect_left(checkpoint_times, request.start_s) - 1)]
            _elapsed_at_checkpoint, offset, previous_id, pending_break = checkpoint
            by_pixel: dict[int, _ChartBucket] = {}
            attempts = 0
            with index.path.open("rb") as stream:
                stream.seek(offset)
                reader = csv.reader(_BinaryCsvLines(stream, index.path.stat().st_size), strict=True)
                while not request.cancel_event.is_set():
                    row_start = stream.tell()
                    try:
                        values = next(reader)
                    except StopIteration:
                        break
                    if len(values) != len(header):
                        break
                    row_end = stream.tell()
                    row = dict(zip(header, values))
                    elapsed = _chart_number(row.get("elapsed_s"))
                    sample_id = _chart_id(row.get("sample_index"))
                    if elapsed is not None and elapsed > request.end_s:
                        break
                    if sample_id is not None and previous_id is not None and sample_id != previous_id + 1:
                        pending_break = True
                    if sample_id is not None:
                        previous_id = sample_id
                    attempts += 1
                    if elapsed is None:
                        pending_break = True
                    else:
                        value = _chart_number(row.get("value_si"))
                        if str(row.get("status") or "") != "ok" or value is None or sample_id is None:
                            pending_break = True
                        elif request.start_s <= elapsed <= request.end_s:
                            point = {
                                "id": sample_id,
                                "x": elapsed,
                                "y": value,
                                "status": "ok",
                                "raw": str(row.get("raw_reading") or ""),
                                "mode": str(row.get("mode") or ""),
                                "unit": str(row.get("unit_si") or ""),
                                "range": row.get("range_si"),
                                "timestamp": str(row.get("timestamp_local") or ""),
                                "error": "",
                                "break_before": bool(pending_break),
                                "summary": False,
                            }
                            pixel = max(0, min(request.width - 1, int((elapsed - request.start_s) / (request.end_s - request.start_s) * request.width)))
                            by_pixel.setdefault(pixel, _ChartBucket()).add(point)
                            pending_break = False
                    if attempts % 512 == 0 and request.cancel_event.is_set():
                        break
                    if row_end <= row_start:
                        break
            rows: list[dict] = []
            for pixel in sorted(by_pixel):
                rows.extend(by_pixel[pixel].points())
            with request.lock:
                request.rows = rows
                request.status = "cancelled" if request.cancel_event.is_set() else "ready"
        except Exception as exc:
            with request.lock:
                request.status = "error"
                request.error = str(exc)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            job = self._job
            requests = list(self._range_jobs.values())
            if job is not None:
                job.cancel_event.set()
            for request in requests:
                request.cancel_event.set()


def _required_bool(payload: dict, key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须是布尔值")
    return value


def _integer_value(payload: dict, key: str, default: int) -> int:
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        raise ValueError(f"{key} 必须是整数")
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} 必须是整数") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{key} 必须是整数")
    return int(number)


def _finite_number(raw, name: str) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{name} 必须是有限数值")
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值")
    return number


def mode_settings_from_payload(payload: dict) -> ModeSettings:
    """Validate web input against the app's 8845A mode capability table."""
    try:
        mode = Mode(str(payload.get("mode", "")))
    except ValueError as exc:
        raise ValueError("不支持的测量模式") from exc
    spec = descriptor(mode)
    if not spec.nplc_values and payload.get("nplc") not in (None, ""):
        raise ValueError(f"{spec.label}不支持NPLC设置")
    if not spec.ac_bandwidths_hz and payload.get("ac_bandwidth_hz") not in (None, ""):
        raise ValueError(f"{spec.label}不支持交流带宽设置")
    if not spec.analog_filter and payload.get("analog_filter", False) not in (False, None):
        raise ValueError(f"{spec.label}不支持直流模拟滤波器")
    digital_filter = _required_bool(payload, "digital_filter", False)
    if digital_filter and not spec.digital_filter:
        raise ValueError(f"{spec.label}不支持直流数字滤波")
    raw_input_impedance = payload.get("input_impedance_auto")
    if spec.input_impedance_auto:
        input_impedance_auto = _required_bool(payload, "input_impedance_auto", False)
    else:
        if raw_input_impedance not in (None, ""):
            raise ValueError(f"{spec.label}不支持DCV自动输入阻抗设置")
        input_impedance_auto = False
    autorange = _required_bool(payload, "autorange", True)
    raw_range = payload.get("range_si")
    range_si = None if autorange or raw_range in (None, "") else _finite_number(raw_range, "量程")
    nplc = payload.get("nplc") if spec.nplc_values else None
    nplc = None if nplc in (None, "") else _finite_number(nplc, "NPLC")
    filter_error = digital_filter_setting_error(mode, digital_filter, nplc=nplc)
    if filter_error:
        raise ValueError(filter_error)
    bandwidth = payload.get("ac_bandwidth_hz") if spec.ac_bandwidths_hz else None
    if bandwidth in (None, ""):
        bandwidth = None
    else:
        bandwidth = _integer_value(payload, "ac_bandwidth_hz", 20)
    settings = ModeSettings(
        mode=mode,
        autorange=autorange,
        range_si=range_si,
        nplc=nplc,
        ac_bandwidth_hz=bandwidth,
        analog_filter=_required_bool(payload, "analog_filter") if spec.analog_filter else False,
        autozero=_required_bool(payload, "autozero", True),
        digital_filter=digital_filter,
        input_impedance_auto=input_impedance_auto,
    )
    settings.validate()
    return settings


def serial_settings_from_payload(payload: dict) -> SerialSettings:
    try:
        timeout_s = _finite_number(payload.get("timeout_s", 2.0), "响应超时")
        if not math.isfinite(timeout_s) or not 0.1 <= timeout_s <= 5.0:
            raise ValueError("浏览器版响应超时须为0.1至5秒")
        return SerialSettings(
            baudrate=_integer_value(payload, "baudrate", 9600),
            bytesize=_integer_value(payload, "bytesize", 8),
            parity=str(payload.get("parity", "N")),
            stopbits=_integer_value(payload, "stopbits", 1),
            flow_control=FlowControl(str(payload.get("flow_control", "none"))),
            eol=str(payload.get("eol", "CRLF")),
            timeout_s=timeout_s,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"串口参数无效：{exc}") from exc


class WebStateStore:
    """Bounded, lock-protected UI view; the recorder remains the full data source."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._samples: deque[dict] = deque(maxlen=_SAMPLE_BUFFER_SIZE)
        self._logs: deque[dict] = deque(maxlen=_LOG_BUFFER_SIZE)
        self._events: deque[dict] = deque(maxlen=_EVENT_BUFFER_SIZE)
        self._log_seq = 0
        self._generation = 0
        self._state: dict = {
            "connected": False,
            "simulation": False,
            "mock_scenario": None,
            "connection_progress": "",
            "connection_stages": [],
            "control_mode": "disconnected",
            "connection_text": "未连接",
            "status": "未连接",
            "device": {},
            "configuration": {},
            "active_settings": {},
            "actual_hz": 0.0,
            "stats": {},
            "latest": None,
            "experiment_dir": "",
            "last_experiment_dir": "",
            "save_result": None,
            "last_error": "",
            "iv_voltage_boundary": None,
            "diagnostic": "",
            "speed_result": None,
            "speed_seq": 0,
            "zero_result": "",
            "error_count": 0,
        }

    def update(self, **values) -> None:
        with self._lock:
            self._state.update(values)

    def append_log(self, line: str) -> None:
        with self._lock:
            self._log_seq += 1
            self._logs.append({"seq": self._log_seq, "time": datetime.now().astimezone().isoformat(timespec="milliseconds"), "text": str(line)})

    def append_samples(self, rows: list[dict], actual_hz: float, stats: dict) -> None:
        with self._lock:
            for source in rows:
                row = dict(source)
                self._samples.append(row)
                if row.get("status") == "ok":
                    self._state["latest"] = row
                if row.get("status") != "ok":
                    self._state["last_error"] = str(row.get("error") or row.get("status"))
            self._state["actual_hz"] = float(actual_hz)
            self._state["stats"] = dict(stats)

    def begin_experiment(self, directory: str) -> None:
        with self._lock:
            self._generation += 1
            self._samples.clear()
            self._events.clear()
            self._state.update(
                experiment_dir=directory,
                last_experiment_dir=directory,
                latest=None,
                actual_hz=0.0,
                stats={},
                last_error="",
                iv_voltage_boundary=None,
                save_result=None,
            )

    def finish_experiment(self, directory: str) -> None:
        with self._lock:
            if directory:
                self._state["last_experiment_dir"] = directory
            self._state["experiment_dir"] = ""

    def append_event(self, text: str, event_record: dict | None = None) -> dict:
        with self._lock:
            latest = self._state.get("latest") or {}
            record = event_record or {}
            value = record.get("value_si", latest.get("value_si"))
            try:
                value = None if value in (None, "") else float(value)
            except (TypeError, ValueError, OverflowError):
                value = None
            event_time = record.get("timestamp_local") or datetime.now().astimezone().isoformat(timespec="microseconds")
            event = {
                "text": text,
                "elapsed_s": record.get("elapsed_s", latest.get("elapsed_s", "0")),
                "sample_index": record.get("sample_index", latest.get("sample_index", 0)),
                "value_si": value,
                "unit_si": record.get("unit_si", latest.get("unit_si", "")),
                "time": event_time,
                "timestamp_local": event_time,
            }
            self._events.append(event)
            return dict(event)

    def record_error(self, message: str) -> None:
        with self._lock:
            self._state["last_error"] = str(message)
            self._state["error_count"] += 1

    def snapshot(self, *, since_sample: int = 0, since_log: int = 0, generation: int | None = None) -> dict:
        with self._lock:
            reset = generation is not None and generation != self._generation
            samples = [
                row for row in self._samples
                if reset or int(row.get("sample_index", 0) or 0) > since_sample
            ]
            logs = [line for line in self._logs if int(line["seq"]) > since_log]
            state = deepcopy(self._state)
            state.update({
                "generation": self._generation,
                "sample_buffer_start": int(self._samples[0].get("sample_index", 0)) if self._samples else 0,
                "samples": samples,
                "logs": logs,
                "log_seq": self._log_seq,
                "events": list(self._events),
                "sample_buffer_limit": _SAMPLE_BUFFER_SIZE,
            })
            return state


class CommandBroker(QObject):
    """Bridge HTTP commands to Qt without executing timed-out queued operations."""

    dispatch = Signal(str, str, object)

    _OPERATION_TTL_S = 300.0
    _MAX_IN_FLIGHT = 128

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._operations: dict[str, dict] = {}

    def bind(self, router: "WebCommandRouter") -> None:
        router._command_broker = self
        self.dispatch.connect(router.execute, Qt.ConnectionType.QueuedConnection)
        router.command_completed.connect(self._complete, Qt.ConnectionType.QueuedConnection)

    def _enqueue(self, action: str, payload: dict, *, enforce_limit: bool = True) -> tuple[str, threading.Event] | None:
        request_id = uuid.uuid4().hex
        ready = threading.Event()
        with self._lock:
            self._purge_completed_locked(time.monotonic())
            in_flight = sum(operation["state"] in {"queued", "running"} for operation in self._operations.values())
            if enforce_limit and in_flight >= self._MAX_IN_FLIGHT:
                return None
            self._operations[request_id] = {
                "ready": ready,
                "state": "queued",
                "result": None,
                "updated_at": time.monotonic(),
            }
        self.dispatch.emit(request_id, action, payload)
        return request_id, ready

    def enqueue_internal(self, action: str, payload: dict) -> str:
        """Queue an app-lifecycle command that must bypass the HTTP request limit."""
        queued = self._enqueue(action, payload, enforce_limit=False)
        assert queued is not None
        return queued[0]

    def request(self, action: str, payload: dict, timeout_s: float = 20.0) -> dict:
        queued = self._enqueue(action, payload)
        if queued is None:
            return {
                "success": False,
                "message": "本地服务中未完成命令过多，请等待已有操作结束后重试",
            }
        request_id, ready = queued
        ready.wait(timeout_s)
        with self._lock:
            operation = self._operations.get(request_id)
            if operation is None:
                return {"success": False, "message": "命令状态已过期；请检查仪器状态后再操作"}
            if operation["state"] == "completed":
                self._operations.pop(request_id, None)
                return operation["result"] or {"success": False, "message": "命令没有返回结果"}
            if operation["state"] == "queued":
                # The Qt signal cannot be withdrawn. Removing this record makes
                # begin_operation reject the queued callback before any side effect.
                self._operations.pop(request_id, None)
                return {
                    "success": False,
                    "cancelled": True,
                    "pending": False,
                    "message": "命令等待超时，已在执行前取消；确认当前状态后再操作",
                }
            return {
                "success": False,
                "pending": True,
                "operation_id": request_id,
                "message": "命令已开始且仍在处理；页面会继续查询结果，请勿重复提交",
            }

    def _purge_completed_locked(self, now: float) -> None:
        expired = [
            request_id for request_id, operation in self._operations.items()
            if operation["state"] == "completed"
            and now - operation["updated_at"] >= self._OPERATION_TTL_S
        ]
        for request_id in expired:
            self._operations.pop(request_id, None)

    def begin_operation(self, request_id: str) -> bool:
        """Atomically claim a queued operation before the router can cause effects."""
        with self._lock:
            operation = self._operations.get(request_id)
            if operation is None or operation["state"] != "queued":
                return False
            operation["state"] = "running"
            operation["updated_at"] = time.monotonic()
            return True

    def operation_status(self, request_id: str) -> dict | None:
        with self._lock:
            self._purge_completed_locked(time.monotonic())
            operation = self._operations.get(request_id)
            if operation is None:
                return None
            if operation["state"] == "completed":
                return {"pending": False, "result": operation["result"]}
            return {"pending": True}

    @Slot(str, object)
    def _complete(self, request_id: str, result: dict) -> None:
        with self._lock:
            operation = self._operations.get(request_id)
            if operation is None or operation["state"] != "running":
                return
            operation["state"] = "completed"
            operation["result"] = result
            operation["updated_at"] = time.monotonic()
            operation["ready"].set()


class _KnownConfigurationConflict(ValueError):
    def __init__(self, conflicts: list[str], readback_basis: dict) -> None:
        self.conflicts = list(conflicts)
        self.readback_basis = dict(readback_basis)
        super().__init__("配置前已知冲突")


class WebCommandRouter(QObject):
    """Runs every instrument operation on the same thread as MeasurementWorker."""

    command_completed = Signal(str, object)
    shutdown_requested = Signal()

    def __init__(self, worker: MeasurementWorker, store: WebStateStore) -> None:
        super().__init__()
        self.worker = worker
        self.store = store
        worker.connected_changed.connect(self._on_connected)
        worker.connection_progress.connect(self._on_connection_progress)
        worker.connection_stage.connect(self._on_connection_stage)
        worker.control_mode_changed.connect(self._on_control_mode)
        worker.status_changed.connect(self._on_status)
        worker.log_line.connect(self._on_log)
        worker.configuration_ready.connect(self._on_configuration)
        worker.active_settings_ready.connect(self._on_active_settings)
        worker.sample_ready.connect(self._on_samples)
        worker.experiment_started.connect(self._on_experiment_started)
        worker.experiment_finished.connect(self._on_experiment_finished)
        worker.experiment_save_result.connect(self._on_experiment_save_result)
        worker.diagnostic_ready.connect(self._on_diagnostic)
        worker.error.connect(self._on_error)
        worker.measurement_speed_result.connect(self._on_speed_result)
        worker.zero_result.connect(self._on_zero_result)

    @Slot(bool, str)
    def _on_connected(self, connected: bool, message: str) -> None:
        mock_scenario = (
            self.store.snapshot().get("mock_scenario")
            if connected and self.worker.simulation
            else None
        )
        self.store.update(
            connected=bool(connected),
            connection_text=str(message),
            connection_progress=(self.store.snapshot().get("connection_progress", "") if connected else ""),
            simulation=bool(self.worker.simulation) if connected else False,
            mock_scenario=mock_scenario,
            control_mode=str(self.worker.control_mode),
            device=self._current_device_state() if connected else {},
        )

    @Slot(str)
    def _on_connection_progress(self, message: str) -> None:
        self.store.update(connection_progress=str(message))
        self.store.append_log(f"连接阶段：{message}")

    @Slot(str, str, str)
    def _on_connection_stage(self, stage: str, status: str, message: str) -> None:
        stages = self.store.snapshot().get("connection_stages", [])
        if stage == "device" and status == "running":
            stages = []
        by_stage = {item.get("stage"): dict(item) for item in stages if isinstance(item, dict)}
        by_stage[stage] = {"stage": stage, "status": status, "message": str(message)}
        order = ("device", "port", "identify", "configuration", "ready")
        ordered = [by_stage[key] for key in order if key in by_stage]
        self.store.update(connection_stages=ordered)

    @Slot(str)
    def _on_control_mode(self, mode: str) -> None:
        self.store.update(control_mode=str(mode))

    @Slot(str)
    def _on_status(self, status: str) -> None:
        self.store.update(status=str(status))

    @Slot(str)
    def _on_log(self, line: str) -> None:
        self.store.append_log(line)

    @Slot(str)
    def _on_experiment_started(self, directory: str) -> None:
        self.store.begin_experiment(directory)

    @Slot(str)
    def _on_experiment_finished(self, directory: str) -> None:
        self.store.finish_experiment(directory)

    @Slot(str, bool, str)
    def _on_experiment_save_result(self, directory: str, saved_ok: bool, message: str) -> None:
        self.store.update(save_result={
            "directory": str(directory),
            "saved": bool(saved_ok),
            "message": str(message),
        })

    @Slot(str)
    def _on_diagnostic(self, text: str) -> None:
        self.store.update(diagnostic=str(text))

    @Slot(str)
    def _on_error(self, message: str) -> None:
        self.store.record_error(message)

    @Slot(object)
    def _on_configuration(self, configuration: dict) -> None:
        self.store.update(configuration=dict(configuration))

    @Slot(object)
    def _on_active_settings(self, settings: dict) -> None:
        self.store.update(active_settings=dict(settings))

    @Slot()
    def _on_samples(self) -> None:
        rows, rate, stats = self.worker.take_sample_update()
        self.store.append_samples(rows, rate, stats)

    @Slot(bool, str)
    def _on_speed_result(self, success: bool, message: str) -> None:
        previous = self.store.snapshot()["speed_seq"]
        self.store.update(speed_result={"success": success, "message": message}, speed_seq=previous + 1)

    @Slot(str)
    def _on_zero_result(self, message: str) -> None:
        self.store.update(zero_result=str(message))

    def _current_device_state(self) -> dict:
        info = self.worker.info
        if info is None:
            return {}
        instrument = self.worker.instrument
        outguard = str(getattr(instrument, "outguard_version", "") or "")
        inguard = str(getattr(instrument, "inguard_version", "") or "")
        if hasattr(instrument, "range_firmware"):
            range_firmware, range_source = instrument.range_firmware
        else:
            range_firmware, range_source = select_range_firmware(outguard)
        return {
            "idn": info.idn_raw,
            "serial_number": info.serial_number,
            "idn_version_candidate": info.idn_version_candidate or None,
            "idn_revision": info.idn_revision,
            "front_panel_software": {
                "outguard_sw": outguard or None,
                "inguard_sw": inguard or None,
            },
            "range_firmware": range_firmware or None,
            "range_firmware_source": range_source,
        }

    @staticmethod
    def _version_text(value, label: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label}必须是文本")
        text = value.strip()
        if len(text) > 64:
            raise ValueError(f"{label}最多64个字符")
        return text

    @classmethod
    def _software_profiles_by_serial(cls, value: object) -> dict[str, dict[str, str]]:
        """Keep only bounded, plain-text operator records from browser storage."""
        if not isinstance(value, dict):
            return {}
        profiles: dict[str, dict[str, str]] = {}
        for serial, profile in value.items():
            if not isinstance(serial, str) or not serial.strip() or len(serial) > 64:
                continue
            if not isinstance(profile, dict):
                continue
            try:
                outguard = cls._version_text(profile.get("outguard_sw", ""), "OutG SW")
                inguard = cls._version_text(profile.get("inguard_sw", ""), "InG SW")
            except ValueError:
                continue
            if any(ord(char) < 32 or ord(char) == 127 for char in outguard + inguard):
                continue
            if outguard or inguard:
                profiles[serial] = {"outguard_sw": outguard, "inguard_sw": inguard}
        return profiles

    def _configuration_readback_basis(self, worker: MeasurementWorker) -> dict:
        state = self.store.snapshot()
        device = state.get("device") or {}
        configuration = state.get("configuration") or {}
        return {
            "description": "当前缓存的最近一次仪器回读；本次预检未再次查询仪器",
            "simulation": bool(worker.simulation),
            "function": configuration.get("function") or None,
            "input_terminals": configuration.get("input_terminals") or None,
            "range_firmware": device.get("range_firmware"),
            "range_firmware_source": device.get("range_firmware_source") or "未知",
        }

    @staticmethod
    def _configuration_conflict_response(conflicts: list[str], readback_basis: dict) -> dict:
        return {
            "success": False,
            "message": (
                "配置前预检未通过，未发送配置 SCPI 命令。已知冲突如下：\n• "
                + "\n• ".join(conflicts)
                + f"\n依据：{readback_basis['description']}。"
            ),
            "conflicts": list(conflicts),
            "readback_basis": dict(readback_basis),
        }

    def _known_configuration_conflicts(
        self, settings: ModeSettings, worker: MeasurementWorker,
    ) -> tuple[list[str], dict]:
        """Report known real-instrument conflicts using cached readback, without querying."""
        basis = self._configuration_readback_basis(worker)
        try:
            settings.validate()
        except (TypeError, ValueError) as exc:
            return [f"模式/参数：{exc}"], basis

        instrument = worker.instrument
        if (
            worker.simulation or instrument is None or not instrument.connected
            or worker.control_mode != "computer"
            or descriptor(settings.mode).quantity != "current"
        ):
            return [], basis

        state = self.store.snapshot()
        device = state.get("device") or {}
        configuration = state.get("configuration") or {}
        firmware = str(device.get("range_firmware") or "")
        firmware_source = str(device.get("range_firmware_source") or "未知")
        terminals = str(configuration.get("input_terminals") or "")
        spec = descriptor(settings.mode)
        unsupported = unsupported_current_ranges(settings.mode, firmware, terminals)
        conflicts: list[str] = []

        if settings.autorange:
            reason = current_autorange_block_reason(settings.mode, firmware, terminals)
            if reason:
                conflicts.append(f"量程：自动量程可能进入不兼容档位（{reason}）")

        minimum = CURRENT_RANGE_FIRMWARE_MINIMUM.get(settings.mode)
        for value in unsupported:
            if not settings.autorange and settings.range_si != value:
                continue
            reasons: list[str] = []
            if value == 0.4 and minimum and compare_firmware(firmware, minimum) is False:
                required = ".".join(str(part) for part in minimum)
                reasons.append(
                    f"{firmware_source}版本 {firmware} 低于 400 mA 档要求 {required}"
                )
                evidence_note = current_firmware_gate_evidence_note(settings.mode)
                if evidence_note:
                    reasons.append(evidence_note)
            if value in FRONT_ONLY_CURRENT_RANGES and not terminals.strip().upper().startswith("FRON"):
                reasons.append(f"端子 {terminals or '未确认'} 不支持该前面板档位")
            if reasons:
                conflict_dimensions = []
                if value == 0.4 and minimum and compare_firmware(firmware, minimum) is False:
                    conflict_dimensions.append("固件")
                if value in FRONT_ONLY_CURRENT_RANGES and not terminals.strip().upper().startswith("FRON"):
                    conflict_dimensions.append("端子")
                dimension_label = "/".join(conflict_dimensions) or "仪器"
                conflicts.append(
                    f"量程：{range_label(value, spec.unit_si)} 与最近回读的{dimension_label}状态冲突（{'；'.join(reasons)}）"
                )
        return conflicts, basis

    @Slot(str, str, object)
    def execute(self, request_id: str, action: str, payload: dict) -> None:
        broker = getattr(self, "_command_broker", None)
        if broker is not None and not broker.begin_operation(request_id):
            return
        try:
            result = self._execute(action, payload if isinstance(payload, dict) else {})
        except _KnownConfigurationConflict as exc:
            result = self._configuration_conflict_response(exc.conflicts, exc.readback_basis)
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            result = {"success": False, "message": str(exc)}
        except Exception as exc:  # Preserve API availability after an unexpected backend exception.
            result = {"success": False, "message": f"操作失败：{exc}"}
        zero_feedback = result.get("zero_result") if isinstance(result, dict) else None
        if action == "zero" and (
            not isinstance(zero_feedback, str) or not zero_feedback.strip()
        ):
            detail = str(
                (result.get("message") if isinstance(result, dict) else None)
                or "ZERO 路由未返回执行结果"
            )
            feedback = f"ZERO执行状态未确认：{detail}；请核对设备状态后再操作。"
            self.store.update(zero_result=feedback)
            result = {
                **(result if isinstance(result, dict) else {}),
                "success": False,
                "uncertain": True,
                "message": feedback,
                "zero_result": feedback,
            }
        self.command_completed.emit(request_id, result)
        if action == "shutdown" and result.get("success"):
            self.shutdown_requested.emit()

    def _execute(self, action: str, payload: dict) -> dict:
        worker = self.worker
        error_count = self.store.snapshot()["error_count"]

        def check_worker_error(default: str) -> dict | None:
            state = self.store.snapshot()
            if state["error_count"] > error_count:
                return {"success": False, "message": state["last_error"] or default}
            return None

        if action == "iv_voltage":
            if worker.state != AcquisitionState.RUNNING:
                raise ValueError("请先开始电流采集")
            voltage = _finite_number(payload.get("voltage_v"), "电压")
            event = worker.mark_event(f"IV 电压确认: {voltage:.17g} V")
            error = check_worker_error("电压确认记录失败")
            if error:
                return error
            if not isinstance(event, dict):
                raise ValueError("电压确认记录失败")
            boundary = {"voltage_v": voltage, "sample_index": int(event["sample_index"]),
                        "elapsed_s": float(event["elapsed_s"]),
                        "generation": self.store.snapshot()["generation"]}
            self.store.update(iv_voltage_boundary=boundary)
            return {"success": True, **boundary, "boundary": boundary}

        if action == "connect_real":
            if worker.instrument is not None and worker.instrument.connected:
                raise ValueError("已连接，请先断开当前仪器")
            port = str(payload.get("port", "")).strip().upper()
            if not re.fullmatch(r"COM[1-9][0-9]*", port):
                raise ValueError("请输入有效的 Windows 串口名，例如 COM3")
            settings = serial_settings_from_payload(payload.get("serial", {}))
            mode_settings = mode_settings_from_payload(payload.get("settings", {"mode": "DCV"}))
            profiles = self._software_profiles_by_serial(
                payload.get("front_panel_software_by_serial", {})
            )
            worker.connect_real_with_profiles(port, settings, mode_settings, False, profiles)
            error = check_worker_error("仪器连接失败")
            if error:
                return error
            self.store.update(device=self._current_device_state())
            return {"success": True, "message": "仪器已连接；连接过程只读取现有模式，没有改变仪器设置"}

        if action == "connect_mock":
            if worker.instrument is not None and worker.instrument.connected:
                raise ValueError("已连接，请先断开当前仪器")
            settings = mode_settings_from_payload(payload.get("settings", {"mode": "DCV"}))
            scenario = self._mock_scenario(str(payload.get("scenario", "阶跃响应与恢复")))
            worker.connect_mock(settings, scenario)
            error = check_worker_error("MOCK连接失败")
            if error:
                return error
            self.store.update(mock_scenario=scenario.name)
            return {"success": True, "message": "MOCK模拟仪器已连接；数据为模拟数据"}

        if action == "disconnect":
            if worker.state != AcquisitionState.STOPPED:
                raise ValueError("请先停止采集，再断开仪器")
            worker.shutdown()
            return {"success": True, "message": "仪器连接已安全关闭"}

        if action == "preflight":
            checks, duplicate_name, duplicate_path = self._preflight(payload, worker)
            return {
                "success": True, "message": "采集前检查通过", "checks": checks,
                "duplicate_name": duplicate_name, "duplicate_path": duplicate_path,
            }

        if action == "open_experiment_directory":
            state = self.store.snapshot()
            directory_text = state.get("experiment_dir") or state.get("last_experiment_dir", "")
            directory = Path(directory_text).expanduser() if directory_text else None
            if directory is None or not directory.is_dir():
                raise ValueError("当前没有可打开的实验目录；请先开始或完成一次实验")
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve()))):
                raise ValueError(f"无法打开实验目录：{directory}")
            return {"success": True, "message": f"已打开实验目录：{directory}"}

        if action == "list_experiment_files":
            state = self.store.snapshot()
            directory_text = state.get("experiment_dir") or state.get("last_experiment_dir", "")
            directory = Path(directory_text).expanduser() if directory_text else None
            if directory is None or not directory.is_dir():
                return {"success": True, "directory": "", "files": []}
            files = []
            for entry in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
                try:
                    if entry.is_symlink() or not entry.is_file():
                        continue
                    stat = entry.stat()
                    files.append({"name": entry.name, "size_bytes": stat.st_size})
                except OSError:
                    continue
                if len(files) >= 1000:
                    break
            return {"success": True, "directory": str(directory.resolve()), "files": files}

        if action == "control_mode":
            mode = str(payload.get("mode", "")).strip().casefold()
            if mode not in {"computer", "local"}:
                raise ValueError("控制方式只能选择 Computer 或 Local")
            if worker.simulation:
                raise ValueError("MOCK模拟器没有仪器面板控制方式")
            if worker.state != AcquisitionState.STOPPED:
                raise ValueError("请先停止采集，再切换仪器控制方式")
            worker.set_control_mode(mode)
            error = check_worker_error("切换仪器控制方式失败")
            if error:
                return error
            return {"success": True, "message": self.store.snapshot()["status"]}

        if action == "firmware_versions":
            if worker.state != AcquisitionState.STOPPED:
                raise ValueError("采集中不能修改面板软件版本记录")
            if worker.simulation or worker.instrument is None or not worker.instrument.connected:
                raise ValueError("请先连接真实的 Fluke 8845A，再记录面板软件版本")
            outguard = self._version_text(payload.get("outguard_sw", ""), "OutG SW")
            inguard = self._version_text(payload.get("inguard_sw", ""), "InG SW")
            worker.set_firmware_versions(outguard, inguard)
            error = check_worker_error("记录面板软件版本失败")
            if error:
                return error
            self.store.update(device=self._current_device_state())
            return {"success": True, "message": "已记录面板核对的软件版本；这些版本不是通过SCPI读取"}

        if action == "shutdown":
            worker.shutdown()
            QThread.currentThread().quit()
            return {"success": True, "message": "测量服务已安全退出"}

        if action == "configure":
            self._require_safety_confirmation(payload, worker)
            if worker.state != AcquisitionState.STOPPED:
                raise ValueError("请停止采集后再应用仪器参数")
            try:
                settings = mode_settings_from_payload(payload.get("settings", {}))
            except (TypeError, ValueError) as exc:
                return self._configuration_conflict_response(
                    [f"模式/参数：{exc}"], self._configuration_readback_basis(worker),
                )
            conflicts, readback_basis = self._known_configuration_conflicts(settings, worker)
            if conflicts:
                return self._configuration_conflict_response(conflicts, readback_basis)
            worker.configure_mode(settings, settings)
            error = check_worker_error("配置仪器参数失败")
            return error or {"success": True, "message": f"已应用并核对 {descriptor(settings.mode).label} 参数"}

        if action in ("start", "single"):
            _checks, duplicate_name, _duplicate_path = self._preflight(payload, worker)
            duplicate_action = str(payload.get("duplicate_name_action", ""))
            if duplicate_name and duplicate_action not in {"create-copy", "overwrite"}:
                raise ValueError(
                    "采集预检失败：发现同名实验；请明确选择覆盖（先备份）、新建独立目录，或修改实验名称后重试"
                )
            if not duplicate_name and duplicate_action == "overwrite":
                raise ValueError("采集预检失败：同名实验目录已变化，请重新预检后选择操作")
            expected_duplicate_path = str(payload.get("expected_duplicate_path", ""))
            if duplicate_action == "overwrite" and not expected_duplicate_path:
                raise ValueError("采集预检失败：缺少已确认的实验目录，请重新预检后再选择覆盖")
            settings = mode_settings_from_payload(payload.get("settings", {}))
            iv_mode = payload.get("iv_mode") is True
            if iv_mode and (action != "start" or settings.mode != Mode.DCI):
                raise ValueError("IV 测量需要连续直流电流采集")
            name = validate_experiment_name(str(payload.get("name", "")))
            root = Path(str(payload.get("root", "")).strip()).expanduser()
            interval = self._interval(payload.get("interval_s", 1.0))
            worker._iv_mode = iv_mode
            worker.start_experiment(
                settings, name, str(root), interval, action == "single", duplicate_action,
                expected_duplicate_path, settings,
            )
            error = check_worker_error("实验启动失败")
            if error:
                return error
            if worker.state == AcquisitionState.STOPPED and action == "start":
                return {"success": False, "message": "实验没有进入采集状态"}
            message = "单次测量已启动" if action == "single" else "连续采集已启动"
            if worker.last_experiment_backup_path is not None:
                message += f"；旧实验目录已备份至：{worker.last_experiment_backup_path}"
            return {"success": True, "message": message}

        if action == "interval":
            if worker.state != AcquisitionState.STOPPED and getattr(worker, "_iv_mode", False):
                raise ValueError("IV 采集中不能更改采样参数")
            interval = self._interval(payload.get("interval_s"))
            result = worker.set_target_interval(interval)
            error = check_worker_error("采样间隔更新失败")
            return error or result

        if action == "speed":
            if worker.state != AcquisitionState.STOPPED and getattr(worker, "_iv_mode", False):
                raise ValueError("IV 采集中不能更改采样参数")
            settings = mode_settings_from_payload(payload.get("settings", {}))
            if settings.mode != worker.current_mode:
                raise ValueError("采集期间不能切换测量模式；请停止本次实验后再配置")
            speed_seq = self.store.snapshot()["speed_seq"]
            worker.set_measurement_speed(settings)
            state = self.store.snapshot()
            result = state.get("speed_result")
            if state["speed_seq"] == speed_seq or result is None:
                return {"success": False, "message": "未收到仪器调速确认"}
            return dict(result)

        if action == "pause":
            worker.pause()
            error = check_worker_error("暂停失败")
            return error or {"success": True, "message": "采集已暂停"}

        if action == "resume":
            worker.resume()
            error = check_worker_error("继续失败")
            return error or {"success": True, "message": "采集已继续"}

        if action == "stop":
            if worker.state == AcquisitionState.STOPPED:
                raise ValueError("当前没有进行中的采集")
            worker.stop()
            state = self.store.snapshot()
            save_result = state.get("save_result")
            if not isinstance(save_result, dict):
                return {"success": False, "message": "停止命令已执行，但未收到实验文件最终保存回执；请检查运行日志和实验目录"}
            if not save_result.get("saved"):
                return {
                    "success": False,
                    "message": str(save_result.get("message") or "实验文件最终写入失败；记录可能不完整"),
                    "directory": str(save_result.get("directory") or ""),
                }
            stats = state.get("stats") or {}
            directory = state.get("last_experiment_dir") or "实验目录不可用"
            message = (
                f"采集已停止并完成文件写入 · 尝试 {int(stats.get('attempt_count', 0) or 0)}"
                f" · 有效 {int(stats.get('valid_count', 0) or 0)}"
                f" · 异常 {int(stats.get('abnormal_count', 0) or 0)}"
                f" · 原始 CSV：{Path(directory) / 'measurements.csv'}"
            )
            return {"success": True, "message": message, "stats": stats, "directory": directory}

        if action == "event":
            text = str(payload.get("text", "")).strip()
            if not text or len(text) > 256:
                raise ValueError("事件内容须为 1–256 个字符")
            event_record = worker.mark_event(text)
            error = check_worker_error("实验事件保存失败")
            if error:
                return error
            if not isinstance(event_record, dict):
                return {"success": False, "message": "实验事件没有返回已保存记录，请查看运行日志"}
            event = self.store.append_event(text, event_record)
            reading = "读数不可用" if event["value_si"] is None else f"{event['value_si']} {event['unit_si']}"
            message = (
                f"已记录 {event['time']} · 样本 #{event['sample_index']} · "
                f"{reading} · {text}"
            )
            return {"success": True, "message": message, "event": event}

        if action == "zero":
            self.store.update(zero_result="ZERO执行中：正在核对执行条件。")

            def reject_zero(message: str) -> dict:
                feedback = f"ZERO未执行：{message}"
                self.store.update(zero_result=feedback)
                return {"success": False, "message": message, "zero_result": feedback}

            if not payload.get("zero_confirmed") is True:
                return reject_zero("请先在页面确认已核对 ZERO 条件")
            if worker.state != AcquisitionState.STOPPED:
                return reject_zero("请停止采集后再执行 ZERO")
            try:
                worker.zero()
            except Exception as exc:
                feedback = f"ZERO执行状态未确认：{exc}；请核对设备状态后再操作。"
                self.store.update(zero_result=feedback)
                return {
                    "success": False,
                    "uncertain": True,
                    "message": feedback,
                    "zero_result": feedback,
                }

            zero_feedback = str(
                getattr(worker, "last_zero_message", "")
                or self.store.snapshot().get("zero_result")
                or "未收到 ZERO 执行反馈"
            )
            error = check_worker_error("ZERO执行失败")
            if error:
                return {**error, "zero_result": zero_feedback}
            if worker.last_zero_readback_ok is not True:
                return {
                    "success": False,
                    "message": zero_feedback or "未收到ZERO配置回读确认",
                    "zero_result": zero_feedback,
                }
            return {
                "success": True,
                "message": zero_feedback,
                "zero_result": zero_feedback,
            }

        if action == "diagnose":
            worker.diagnose()
            error = check_worker_error("仪器诊断失败")
            return error or {"success": True, "message": "诊断查询已完成"}

        raise ValueError("未知的仪器操作")

    def _preflight(self, payload: dict, worker: MeasurementWorker) -> tuple[list[str], bool, str | None]:
        """Check acquisition readiness before the worker configures the instrument."""
        if worker.instrument is None or not worker.instrument.connected:
            raise ValueError("采集预检失败：请先连接仪器或明确标记的 MOCK 模拟器")
        if worker.state != AcquisitionState.STOPPED:
            raise ValueError("采集预检失败：请先停止当前采集")
        if not worker.simulation and worker.control_mode != "computer":
            raise ValueError("采集预检失败：仪表处于 LOCAL；请切回 Computer 并重新读取配置")
        self._require_safety_confirmation(payload, worker)

        try:
            settings = mode_settings_from_payload(payload.get("settings", {}))
        except (TypeError, ValueError) as exc:
            raise _KnownConfigurationConflict(
                [f"模式/参数：{exc}"], self._configuration_readback_basis(worker),
            ) from exc
        conflicts, readback_basis = self._known_configuration_conflicts(settings, worker)
        if conflicts:
            raise _KnownConfigurationConflict(conflicts, readback_basis)
        active = self.store.snapshot().get("active_settings") or {}
        configuration = self.store.snapshot().get("configuration") or {}
        if worker.current_mode != settings.mode or active.get("mode") != settings.mode.value or not configuration:
            raise ValueError(
                f"采集预检失败：{descriptor(settings.mode).label}尚未成为当前回读配置；"
                "请先应用仪器参数并确认回读结果"
            )
        name = validate_experiment_name(str(payload.get("name", "")))
        root_text = str(payload.get("root", "")).strip()
        if not root_text:
            raise ValueError("采集预检失败：请填写实验保存目录")
        target_interval_s = self._interval(payload.get("interval_s", 1.0))
        root = prepare_experiment_storage(Path(root_text).expanduser())
        free_bytes = require_minimum_free_space(get_available_space_bytes(root))
        available_space = format_available_space(free_bytes)
        capacity_seconds = estimate_recording_capacity_seconds(free_bytes, target_interval_s)
        if capacity_seconds is None:
            capacity_forecast = format_estimated_recording_capacity(None)
        else:
            capacity_forecast = (
                f"按 4 KiB/样本粗估，目标间隔 {target_interval_s:g} s 时可记录"
                f"{format_estimated_recording_capacity(capacity_seconds)}；实际采样间隔、原始响应及错误文本长度会影响结果"
            )
        duplicates = find_existing_experiment_directories(root, name)

        device = self.store.snapshot().get("device") or {}
        identity = str(device.get("model") or ("MOCK模拟器" if worker.simulation else "Fluke 8845A"))
        wiring = "MOCK无需实体接线确认" if worker.simulation else "接线已确认；SCPI无法检查端子、保险丝和外部回路"
        checks = [
            f"仪器已连接：{identity}",
            f"当前模式已回读：{descriptor(settings.mode).label}；启动时将再次写入并回读所选参数",
            f"保存目录可写：{root}；当前可用 {available_space}，启动预留 10 MiB；{capacity_forecast}；"
            "此估算不限制实验时长。采集中每 5 秒复查空间，低于预留值时停止采集并尝试收尾文件，"
            "若收尾失败会提示记录可能不完整",
            wiring,
            f"实验名称有效：{name}；已有数据不会被覆盖",
        ]
        if duplicates:
            checks.append(
                f"发现 {len(duplicates)} 个同名实验目录；最近一次为 {duplicates[-1].name}，"
                "覆盖时会先移入可恢复备份目录"
            )
        duplicate_path = str(duplicates[-1].resolve()) if duplicates else None
        return checks, bool(duplicates), duplicate_path

    @staticmethod
    def _require_safety_confirmation(payload: dict, worker: MeasurementWorker) -> None:
        instrument = worker.instrument
        if (
            worker.simulation
            and isinstance(instrument, Mock8845A)
            and instrument.connected
        ):
            return
        if payload.get("safety_confirmed") is not True:
            raise ValueError("请先阅读接线提示，并勾选确认框")

    @staticmethod
    def _interval(value) -> float | None:
        interval = _finite_number(value, "采样间隔")
        if not 0 <= interval <= 86400:
            raise ValueError("采样间隔须为 0 至 86400 秒")
        return None if interval == 0 else interval

    @staticmethod
    def _mock_scenario(name: str) -> MockScenario:
        if name == "长期漂移":
            return MockScenario(name=name, noise_fraction=1e-4, drift_fraction_per_minute=3e-4,
                                step_at_s=1e9, recovery_at_s=None, read_latency_s=0.005)
        if name == "通信超时测试":
            return MockScenario(name=name, timeout_at_sample=8, read_latency_s=0.03)
        if name == "断线恢复测试":
            return MockScenario(name=name, disconnect_at_sample=8, read_latency_s=0.03)
        if name == "阶跃响应与恢复":
            return MockScenario(name="阶跃响应与恢复")
        raise ValueError("未知的 MOCK 场景")


class WebRuntime:
    """Owns exactly one worker/serial session for this localhost web process."""

    def __init__(self) -> None:
        self.store = WebStateStore()
        self.chart_history = WebChartHistory(self.store)
        self.worker = MeasurementWorker()
        self.router = WebCommandRouter(self.worker, self.store)
        self.broker = CommandBroker()
        self.broker.bind(self.router)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.router.moveToThread(self.thread)
        self._stop_requested = False

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout_ms: int = 15000) -> bool:
        self.chart_history.close()
        if self.thread.isRunning():
            if not self._stop_requested:
                self._stop_requested = True
                self.broker.enqueue_internal("shutdown", {})
            deadline = time.monotonic() + timeout_ms / 1000
            while self.thread.isRunning() and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                self.thread.wait(20)
            return not self.thread.isRunning()
        return True
