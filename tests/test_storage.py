from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import threading
import gc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import fluke8845a.storage as storage_module
from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings, Reading
from fluke8845a.storage import ExperimentRecorder
from fluke8845a.storage import excel_safe_events_csv_bytes, write_excel_safe_events_csv


def make_reading(value=1.2345678901234567, raw="+1.2345678901234567E+00"):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="microseconds")
    return Reading(raw, value, 10, 1_000_000_000, 1_025_000_000, stamp, now.astimezone().isoformat(timespec="microseconds"))


def test_experiment_writes_csv_incrementally_with_full_value_and_time(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "long term", {"simulation": True, "device": {"model": "MOCK"}}, ModeSettings(Mode.DCV, input_impedance_auto=True))
    first = make_reading()
    row = recorder.append_reading(first, mode=Mode.DCV)
    assert row["value_si"] == repr(1.2345678901234567)
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1  # visible before experiment completion
    assert rows[0]["raw_reading"] == "+1.2345678901234567E+00"
    assert rows[0]["timestamp_utc"].endswith("+00:00")
    assert rows[0]["timestamp_local"] == first.timestamp_local
    assert rows[0]["elapsed_s"] == "0.0"
    assert rows[0]["actual_interval_s"] == ""
    assert rows[0]["read_duration_s"] == "0.025"
    assert rows[0]["range_si"] == "10"
    assert rows[0]["nplc"] == "1.0"
    assert rows[0]["voltage_v"] == repr(1.2345678901234567)
    assert rows[0]["input_impedance_auto"] == "True"

    second_stamp = datetime.now(timezone.utc)
    second = Reading(
        "+1.2345678901234568E+00", 1.2345678901234568, 10,
        1_050_000_000, 1_075_000_000,
        second_stamp.isoformat(timespec="microseconds"),
        second_stamp.astimezone().isoformat(timespec="microseconds"),
    )
    recorder.append_reading(second, mode=Mode.DCV)
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[1]["sample_index"] == "2"
    assert rows[1]["elapsed_s"] == "0.05"
    assert rows[1]["actual_interval_s"] == "0.05"
    assert rows[1]["raw_reading"] == "+1.2345678901234568E+00"
    recorder.finish("stopped")
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert config["simulation"] is True
    assert config["sample_count"] == 2
    assert config["completion_status"] == "stopped"
    assert config["mode_settings"]["input_impedance_auto"] is True


def test_new_experiment_includes_a_spreadsheet_safety_notice_for_raw_csv(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "csv safety notice", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    try:
        notice = recorder.directory / "CSV_打开说明.txt"
        assert notice.is_file()
        text = notice.read_text(encoding="utf-8")
        assert "events.csv" in text
        assert "measurements.csv" in text
        assert "不要直接用 Excel" in text
        assert "公式" in text
        assert "events_excel_safe.csv" in text
        assert "measurements_excel_safe.csv" in text
        assert "原始读数和错误文本" in text
        assert "原始 CSV 不会被修改" in text
    finally:
        recorder.finish("test")


def test_csv_safety_notice_open_failure_does_not_block_raw_recording(tmp_path, monkeypatch):
    original_open = Path.open

    def fail_notice_open(path, *args, **kwargs):
        if path.name.startswith("CSV_打开说明.txt"):
            raise OSError("simulated notice open failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_notice_open)
    recorder = ExperimentRecorder.create(
        tmp_path, "csv notice open failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    try:
        recorder.append_reading(make_reading(), mode=Mode.DCV)
        recorder.append_event("=1+1")
        assert not (recorder.directory / "CSV_打开说明.txt").exists()
        assert "无法创建 CSV_打开说明.txt" in (recorder.directory / "run.log").read_text(encoding="utf-8")
        with (recorder.directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            events = list(csv.DictReader(stream))
        assert events[0]["event"] == "=1+1"
    finally:
        recorder.finish("test")


def test_csv_safety_notice_partial_write_is_removed_without_changing_raw_csv(
    tmp_path, monkeypatch,
):
    original_open = Path.open

    class PartialFailingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.stream.__exit__(exc_type, exc_value, traceback)

        def write(self, text):
            self.stream.write(text[:12])
            self.stream.flush()
            raise OSError("simulated notice write failure")

        def flush(self):
            self.stream.flush()

    def partially_fail_notice_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name.startswith("CSV_打开说明.txt"):
            return PartialFailingStream(stream)
        return stream

    monkeypatch.setattr(Path, "open", partially_fail_notice_open)
    recorder = ExperimentRecorder.create(
        tmp_path, "csv notice write failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    try:
        event = recorder.append_event("=1+1")
        assert not (recorder.directory / "CSV_打开说明.txt").exists()
        assert "无法创建 CSV_打开说明.txt" in (recorder.directory / "run.log").read_text(encoding="utf-8")
        with (recorder.directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            events = list(csv.DictReader(stream))
        assert events[0]["event"] == "=1+1"
        assert event["event"] == "=1+1"
    finally:
        recorder.finish("test")


def test_failed_cleanup_of_partial_notice_never_leaves_a_complete_notice_path(
    tmp_path, monkeypatch,
):
    original_open = Path.open
    original_unlink = Path.unlink

    class PartialFailingStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return self.stream.__exit__(exc_type, exc_value, traceback)

        def write(self, text):
            self.stream.write(text[:12])
            self.stream.flush()
            raise OSError("simulated notice write failure")

        def flush(self):
            self.stream.flush()

    def partially_fail_notice_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name.startswith("CSV_打开说明.txt"):
            return PartialFailingStream(stream)
        return stream

    def fail_partial_notice_cleanup(path, *args, **kwargs):
        if path.name.startswith("CSV_打开说明.txt"):
            raise PermissionError("simulated notice cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", partially_fail_notice_open)
    monkeypatch.setattr(Path, "unlink", fail_partial_notice_cleanup)
    recorder = ExperimentRecorder.create(
        tmp_path, "csv notice cleanup failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    try:
        assert not (recorder.directory / "CSV_打开说明.txt").exists()
        recorder.append_event("=1+1")
        assert "无法创建 CSV_打开说明.txt" in (recorder.directory / "run.log").read_text(encoding="utf-8")
    finally:
        recorder.finish("test")


def test_existing_csv_notice_is_preserved_and_not_replaced(tmp_path):
    directory = tmp_path / "existing experiment"
    directory.mkdir()
    notice = directory / "CSV_打开说明.txt"
    original_notice = "用户已有说明\n"
    notice.write_text(original_notice, encoding="utf-8")
    recorder = storage_module.ExperimentRecorder(
        directory, {"simulation": True}, ModeSettings(Mode.DCV)
    )
    try:
        assert notice.read_text(encoding="utf-8") == original_notice
        assert not list(directory.glob("CSV_打开说明.txt.*.partial"))
        assert "未覆盖实验目录中已存在的 CSV_打开说明.txt" in (
            directory / "run.log"
        ).read_text(encoding="utf-8")
    finally:
        recorder.finish("test")


def test_large_mock_run_keeps_every_attempt_and_periodic_progress_metadata(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path,
        "high volume mock",
        {"simulation": True, "device": {"model": "MOCK"}},
        ModeSettings(Mode.DCV),
    )
    start_utc = datetime(2026, 10, 17, tzinfo=timezone.utc)
    total_attempts = 5_000

    for index in range(total_attempts):
        start_ns = 1_000_000_000 + index * 10_000_000
        sample_utc = start_utc + timedelta(milliseconds=index * 10)
        timestamp_utc = sample_utc.isoformat(timespec="microseconds")
        timestamp_local = sample_utc.astimezone().isoformat(timespec="microseconds")
        if index == 2_500:
            recorder.append_reading(
                None,
                mode=Mode.DCV,
                started_monotonic_ns=start_ns,
                timestamp_utc=timestamp_utc,
                timestamp_local=timestamp_local,
                status="timeout",
                error="simulated timeout",
                raw_reading="NO RESPONSE",
            )
        else:
            value = 1.000000123456789 + index * 1e-9
            recorder.append_reading(
                Reading(
                    f"{value:+.17E}", value, 10.0, start_ns, start_ns + 100_000,
                    timestamp_utc, timestamp_local,
                ),
                mode=Mode.DCV,
            )

        if (index + 1) % 1_000 == 0:
            with (recorder.directory / "measurements.csv").open(
                newline="", encoding="utf-8-sig"
            ) as stream:
                visible_rows = list(csv.DictReader(stream))
            config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
            assert len(visible_rows) == index + 1
            assert config["sample_count"] == index + 1

    recorder.finish("stopped")

    with (recorder.directory / "measurements.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))

    assert len(rows) == total_attempts
    assert [int(row["sample_index"]) for row in rows] == list(range(1, total_attempts + 1))
    assert rows[2_500]["status"] == "timeout"
    assert rows[2_500]["value_si"] == ""
    assert rows[2_500]["raw_reading"] == "NO RESPONSE"
    assert rows[2_501]["value_si"] == repr(1.000000123456789 + 2_501 * 1e-9)
    assert rows[2_501]["raw_reading"] == f"{1.000000123456789 + 2_501 * 1e-9:+.17E}"
    assert config["sample_count"] == total_attempts
    assert config["completion_status"] == "stopped"


def test_csv_interval_uses_valid_read_response_end_times(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "response-end interval", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    start_utc = "2026-10-16T01:00:00.000000+00:00"
    start_local = "2026-10-16T09:00:00.000000+08:00"
    first = Reading(
        "+1E+00", 1.0, 10, 1_000_000_000, 1_100_000_000,
        start_utc, start_local,
        read_response_end_utc="2026-10-16T01:00:00.100000+00:00",
        read_response_end_local="2026-10-16T09:00:00.100000+08:00",
    )
    second = Reading(
        "+2E+00", 2.0, 10, 1_200_000_000, 1_600_000_000,
        "2026-10-16T01:00:00.200000+00:00",
        "2026-10-16T09:00:00.200000+08:00",
        read_response_end_utc="2026-10-16T01:00:00.600000+00:00",
        read_response_end_local="2026-10-16T09:00:00.600000+08:00",
    )

    first_row = recorder.append_reading(first, mode=Mode.DCV)
    second_row = recorder.append_reading(second, mode=Mode.DCV)

    assert first_row["actual_interval_s"] == ""
    assert second_row["actual_interval_s"] == "0.5"
    assert second_row["read_duration_s"] == "0.4"
    assert second_row["host_read_request_start_monotonic_ns"] == "1200000000"
    assert second_row["host_read_response_end_monotonic_ns"] == "1600000000"
    assert second_row["host_read_response_end_utc"] == "2026-10-16T01:00:00.600000+00:00"
    recorder.finish("test")


def test_speed_change_preserves_all_measurement_mode_settings(tmp_path):
    settings = ModeSettings(
        Mode.DCV, nplc=10, digital_filter=True, input_impedance_auto=True,
        analog_filter=True, autozero=False,
    )
    recorder = ExperimentRecorder.create(
        tmp_path, "speed change settings", {"simulation": True}, settings
    )

    recorder.record_measurement_speed_change(
        nplc=100, ac_bandwidth_hz=None, digital_filter=False,
        device_configuration={"nplc": 100, "digital_filter_response": "0"},
    )

    assert recorder.settings.nplc == 100
    assert recorder.settings.digital_filter is False
    assert recorder.settings.input_impedance_auto is True
    assert recorder.settings.analog_filter is True
    assert recorder.settings.autozero is False
    recorder.finish()
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert config["mode_settings"]["digital_filter"] is False
    assert config["mode_settings"]["input_impedance_auto"] is True
    assert config["measurement_speed_changes"][0]["previous_digital_filter"] is True
    assert config["measurement_speed_changes"][0]["digital_filter"] is False


def test_measurement_csv_records_digital_filter_state_for_each_attempt(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "digital filter csv", {"simulation": True},
        ModeSettings(Mode.DCV, nplc=10, digital_filter=True),
    )

    row = recorder.append_reading(make_reading(), mode=Mode.DCV)
    recorder.finish("stopped")

    assert row["digital_filter"] is True
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        saved = next(csv.DictReader(stream))
    assert saved["digital_filter"] == "True"


def test_experiment_periodically_syncs_csv_rows_and_syncs_events_and_finish(
    tmp_path, monkeypatch
):
    sync_calls = []
    monkeypatch.setattr(storage_module, "CSV_FSYNC_INTERVAL_NS", 0, raising=False)
    monkeypatch.setattr(storage_module.os, "fsync", lambda fd: sync_calls.append(fd))
    recorder = ExperimentRecorder.create(
        tmp_path, "durability", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    sync_calls.clear()  # Ignore the initial config.json fsync.

    recorder.append_reading(make_reading(), mode=Mode.DCV)
    assert len(sync_calls) == 2  # measurements.csv and events.csv

    sync_calls.clear()
    recorder.append_event("durable event")
    assert len(sync_calls) >= 2  # Both CSVs are synced; config.json is synced too.

    sync_calls.clear()
    recorder.finish("stopped")
    assert len(sync_calls) >= 2


def test_csv_fsync_uses_the_one_second_monotonic_interval_without_sleep(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "fsync throttle", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    assert storage_module.CSV_FSYNC_INTERVAL_NS == 1_000_000_000
    baseline_ns = recorder._last_csv_fsync_ns
    recorder._last_log_fsync_ns = baseline_ns
    now_ns = [baseline_ns + storage_module.CSV_FSYNC_INTERVAL_NS - 1]
    real_fsync = storage_module.os.fsync
    synced_fds = []

    def capture_fsync(fd):
        synced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(storage_module.time, "perf_counter_ns", lambda: now_ns[0])
    monkeypatch.setattr(storage_module.os, "fsync", capture_fsync)

    try:
        recorder.append_reading(make_reading(), mode=Mode.DCV)
        assert synced_fds == []

        now_ns[0] = baseline_ns + storage_module.CSV_FSYNC_INTERVAL_NS
        recorder.append_reading(make_reading(), mode=Mode.DCV)
        recorder.sync_log_if_due()
        assert len(synced_fds) == 3  # measurements.csv, events.csv, and run.log
        assert recorder._log_stream.fileno() in synced_fds
    finally:
        recorder.finish("stopped")


def test_background_run_log_fsync_failure_is_reported_and_keeps_run_incomplete(
    tmp_path, monkeypatch
):
    recorder = ExperimentRecorder.create(
        tmp_path, "periodic run log sync failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    errors = []
    failure_reported = threading.Event()
    log_fd = recorder._log_stream.fileno()
    real_fsync = storage_module.os.fsync
    failed_once = threading.Event()

    def fail_first_run_log_sync(fd):
        if fd == log_fd and not failed_once.is_set():
            failed_once.set()
            raise OSError("injected periodic run.log fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(storage_module, "LOG_FSYNC_INTERVAL_NS", 1, raising=False)
    monkeypatch.setattr(storage_module.os, "fsync", fail_first_run_log_sync)
    recorder.start_periodic_sync(
        lambda exc: (errors.append(exc), failure_reported.set())
    )

    try:
        assert failure_reported.wait(1), "background run.log sync failure was not reported"
        assert isinstance(recorder.log_sync_error, OSError)
        assert "periodic run.log fsync failure" in str(recorder.log_sync_error)
        assert errors == [recorder.log_sync_error]

        with pytest.raises(OSError, match="periodic run.log fsync failure"):
            recorder.finish("stopped")

        config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
        assert config["completion_status"] == "running"
    finally:
        if not recorder._log_stream.closed:
            try:
                recorder.finish("test_cleanup")
            except OSError:
                pass


def test_slow_run_log_fsync_does_not_block_measurement_append(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "slow log sync", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    monkeypatch.setattr(storage_module, "CSV_FSYNC_INTERVAL_NS", 60_000_000_000)
    log_fd = recorder._log_stream.fileno()
    real_fsync = storage_module.os.fsync
    log_fsync_started = threading.Event()
    release_log_fsync = threading.Event()
    append_finished = threading.Event()
    append_errors = []

    def hold_run_log_sync(fd):
        if fd == log_fd:
            log_fsync_started.set()
            if not release_log_fsync.wait(2):
                raise TimeoutError("test did not release run.log fsync")
        return real_fsync(fd)

    def append_one_reading():
        try:
            recorder.append_reading(make_reading(), mode=Mode.DCV)
        except Exception as exc:
            append_errors.append(exc)
        finally:
            append_finished.set()

    monkeypatch.setattr(storage_module.os, "fsync", hold_run_log_sync)
    sync_thread = threading.Thread(target=lambda: recorder.sync_log_if_due(force=True))
    append_thread = threading.Thread(target=append_one_reading)
    sync_thread.start()

    try:
        assert log_fsync_started.wait(1), "run.log fsync did not start"
        append_thread.start()
        assert append_finished.wait(0.5), "run.log fsync blocked measurement append"
        assert not append_errors
    finally:
        release_log_fsync.set()
        sync_thread.join(2)
        if append_thread.ident is not None:
            append_thread.join(2)
        recorder.finish("stopped")

    assert not sync_thread.is_alive()
    assert not append_thread.is_alive()


def test_failed_attempt_stays_blank_and_event_is_saved(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "fault test", {"simulation": False}, ModeSettings(Mode.ACI, nplc=None, ac_bandwidth_hz=20))
    failed = recorder.append_reading(None, mode=Mode.ACI, status="timeout", error="response timeout", raw_reading="partial")
    recorder.append_event("load disconnected")
    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    with (recorder.directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
        events = list(csv.DictReader(stream))
    assert failed["value_si"] == ""
    assert rows[0]["status"] == "timeout"
    assert rows[0]["raw_reading"] == "partial"
    assert events[0]["event"] == "load disconnected"
    recorder.finish("test")


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", " \t=", "\u00a0+", "\x01@"])
def test_excel_safe_event_export_escapes_formula_like_text_only(prefix):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["experiment_id", "event_index", "value_si", "event"])
    writer.writerow(["id", 1, "-1.25", prefix + "cmd"])
    writer.writerow(["id", 2, "3.5", "ordinary, text"])
    source = buffer.getvalue().encode("utf-8-sig")
    exported = excel_safe_events_csv_bytes(source)
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
    assert rows[0]["event"] == "'" + prefix + "cmd"
    assert rows[0]["value_si"] == "-1.25"
    assert rows[1]["event"] == "ordinary, text"
    assert rows[1]["value_si"] == "3.5"
    assert source.startswith(b"\xef\xbb\xbf")  # source object is untouched


def test_excel_safe_event_export_preserves_quoted_newlines_and_rejects_wrong_schema():
    source = b'event,elapsed_s\r\n"line one\nline two",0.5\r\n'
    exported = excel_safe_events_csv_bytes(source)
    rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline="")))
    assert rows == [{"event": "line one\nline two", "elapsed_s": "0.5"}]
    with pytest.raises(ValueError, match="event"):
        excel_safe_events_csv_bytes(b"name,value\na,b\n")
    with pytest.raises(ValueError, match="列数不一致"):
        excel_safe_events_csv_bytes(b"event,elapsed_s\nordinary,0.5,unexpected\n")


def test_excel_safe_event_export_sanitizes_every_column_and_preserves_valid_numbers():
    fields = (
        "experiment_id", "event_index", "timestamp_local", "timestamp_utc",
        "elapsed_s", "sample_index", "mode", "value_si", "unit_si", "event",
    )
    rows = [
        {
            "experiment_id": "=1+1", "event_index": "=2+2",
            "timestamp_local": "+COMMAND", "timestamp_utc": "-COMMAND",
            "elapsed_s": "=3+3", "sample_index": "@SUM(1,2)",
            "mode": "=CMD", "value_si": "=4+4", "unit_si": "@CMD",
            "event": " \t=HYPERLINK(\"https://example.invalid\")",
        },
        {
            "experiment_id": "run-1", "event_index": "2",
            "timestamp_local": "2026-10-19T10:00:00", "timestamp_utc": "2026-10-19T02:00:00Z",
            "elapsed_s": "0.5", "sample_index": "3", "mode": "RES2",
            "value_si": "-1.2345678901234567", "unit_si": "Ohm", "event": "接通负载",
        },
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    source = buffer.getvalue().encode("utf-8-sig")

    exported = excel_safe_events_csv_bytes(source)
    result = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"), newline="")))

    assert result[0] == {
        "experiment_id": "'=1+1", "event_index": "'=2+2",
        "timestamp_local": "'+COMMAND", "timestamp_utc": "'-COMMAND",
        "elapsed_s": "'=3+3", "sample_index": "'@SUM(1,2)",
        "mode": "'=CMD", "value_si": "'=4+4", "unit_si": "'@CMD",
        "event": "' \t=HYPERLINK(\"https://example.invalid\")",
    }
    assert result[1] == rows[1]
    assert source == buffer.getvalue().encode("utf-8-sig")


def test_safe_csv_exports_reject_unrecognized_formula_like_headers(tmp_path):
    with pytest.raises(ValueError, match="未知列"):
        excel_safe_events_csv_bytes(b"=1+1,event\n=HYPERLINK,ordinary\n")

    source = tmp_path / "measurements.csv"
    destination = tmp_path / "measurements_safe.csv"
    source.write_bytes(b"=1+1,raw_reading,error\n=HYPERLINK,1.0,\n")
    with pytest.raises(ValueError, match="未知列"):
        storage_module.write_excel_safe_measurements_csv(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".measurements-safe-*.tmp"))


def test_streaming_excel_safe_export_preserves_raw_source_and_replaces_only_destination(tmp_path):
    source = tmp_path / "events.csv"
    destination = tmp_path / "events_excel_safe.csv"
    original = (
        b"value_si,event\r\n"
        b"-2.5,=1+1\r\n"
        b"-3.5,+SUM(A1)\r\n"
        b"-4.5,-1+2\r\n"
        b"-5.5,@HYPERLINK\r\n"
    )
    source.write_bytes(original)
    write_excel_safe_events_csv(source, destination)
    assert source.read_bytes() == original
    with destination.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        {"value_si": "-2.5", "event": "'=1+1"},
        {"value_si": "-3.5", "event": "'+SUM(A1)"},
        {"value_si": "-4.5", "event": "'-1+2"},
        {"value_si": "-5.5", "event": "'@HYPERLINK"},
    ]
    assert not list(tmp_path.glob(".events-safe-*.tmp"))


def test_streaming_excel_safe_export_never_overwrites_an_existing_destination(tmp_path):
    source = tmp_path / "events.csv"
    destination = tmp_path / "user-notes.csv"
    source.write_text("event,value_si\n=1+1,2\n", encoding="utf-8")
    destination.write_bytes(b"user data that must remain unchanged")

    with pytest.raises(FileExistsError, match="目标已存在"):
        write_excel_safe_events_csv(source, destination)

    assert destination.read_bytes() == b"user data that must remain unchanged"
    assert not list(tmp_path.glob(".events-safe-*.tmp"))


def test_streaming_excel_safe_export_does_not_replace_destination_on_malformed_csv(tmp_path):
    source = tmp_path / "events.csv"
    destination = tmp_path / "events_excel_safe.csv"
    source.write_text("event,value_si\ntext,1,extra\n", encoding="utf-8")
    destination.write_bytes(b"prior export")
    with pytest.raises(ValueError, match="列数不一致"):
        write_excel_safe_events_csv(source, destination)
    assert destination.read_bytes() == b"prior export"
    assert not list(tmp_path.glob(".events-safe-*.tmp"))


def test_streaming_excel_safe_export_refuses_to_overwrite_the_raw_events_file(tmp_path):
    source = tmp_path / "events.csv"
    original = b"event,value_si\n=1+1,3\n"
    source.write_bytes(original)
    with pytest.raises(ValueError, match="不能覆盖原始事件 CSV"):
        write_excel_safe_events_csv(source, source)
    assert source.read_bytes() == original
    alias = tmp_path / "events-alias.csv"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="不能覆盖原始事件 CSV"):
        write_excel_safe_events_csv(source, alias)
    assert source.read_bytes() == original


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", " \t=", "\u00a0+", "\x01@"])
def test_measurement_safe_copy_escapes_untrusted_text_and_preserves_raw_file(tmp_path, prefix):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "measurements_excel_safe.csv"
    original = (
        "experiment_id,value_si,voltage_v,raw_reading,error,elapsed_s\r\n"
        f"run-1,-1.2345678901234567,-1.2345678901234567,{prefix}READ,{prefix}DETAIL,0.25\r\n"
        "run-1,2.5,2.5,+2.50000000E+00,,0.50\r\n"
    ).encode("utf-8-sig")
    source.write_bytes(original)

    storage_module.write_excel_safe_measurements_csv(source, destination)

    assert source.read_bytes() == original
    with destination.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["raw_reading"] == "'" + prefix + "READ"
    assert rows[0]["error"] == "'" + prefix + "DETAIL"
    assert rows[0]["value_si"] == "-1.2345678901234567"
    assert rows[0]["voltage_v"] == "-1.2345678901234567"
    assert rows[0]["elapsed_s"] == "0.25"
    assert rows[1]["raw_reading"] == "+2.50000000E+00"
    assert rows[1]["value_si"] == "2.5"
    assert rows[1]["error"] == ""
    assert not list(tmp_path.glob(".measurements-safe-*.tmp"))


def test_measurement_safe_copy_sanitizes_formula_cells_in_numeric_columns(tmp_path):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "measurements_excel_safe.csv"
    original = (
        "experiment_id,sample_index,mode,value_si,unit_si,raw_reading,error,elapsed_s\r\n"
        "run-1,=1+1,DCV,=2+2,V,-1.2345678901234567E+00,@COMMAND,=3+3\r\n"
    ).encode("utf-8-sig")
    source.write_bytes(original)

    storage_module.write_excel_safe_measurements_csv(source, destination)

    assert source.read_bytes() == original
    with destination.open("r", encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row == {
        "experiment_id": "run-1", "sample_index": "'=1+1", "mode": "DCV",
        "value_si": "'=2+2", "unit_si": "V",
        "raw_reading": "-1.2345678901234567E+00", "error": "'@COMMAND",
        "elapsed_s": "'=3+3",
    }


def test_measurement_safe_export_rejects_wrong_schema_and_malformed_rows(tmp_path):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "safe.csv"
    source.write_text("value_si,error\n1,=1+1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="raw_reading"):
        storage_module.write_excel_safe_measurements_csv(source, destination)
    assert not destination.exists()

    source.write_text("raw_reading,error\ntext,detail,extra\n", encoding="utf-8")
    with pytest.raises(ValueError, match="列数不一致"):
        storage_module.write_excel_safe_measurements_csv(source, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".measurements-safe-*.tmp"))


def test_measurement_safe_export_refuses_to_overwrite_source_or_existing_destination(tmp_path):
    source = tmp_path / "measurements.csv"
    original = b"raw_reading,error\n=1+1,details\n"
    source.write_bytes(original)
    with pytest.raises(ValueError, match="不能覆盖原始测量 CSV"):
        storage_module.write_excel_safe_measurements_csv(source, source)
    assert source.read_bytes() == original

    destination = tmp_path / "user-notes.csv"
    destination.write_bytes(b"preserve user file")
    with pytest.raises(FileExistsError, match="目标已存在"):
        storage_module.write_excel_safe_measurements_csv(source, destination)
    assert destination.read_bytes() == b"preserve user file"
    assert not list(tmp_path.glob(".measurements-safe-*.tmp"))


def test_measurement_and_event_csv_share_a_persisted_experiment_identifier(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "linked files", {"simulation": True}, ModeSettings(Mode.DCV))
    recorder.append_reading(make_reading(), mode=Mode.DCV)
    recorder.append_event("paired event")

    with (recorder.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        measurement = next(csv.DictReader(stream))
    with (recorder.directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
        event = next(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))

    assert recorder.metadata["experiment_id"]
    assert measurement["experiment_id"] == recorder.metadata["experiment_id"]
    assert event["experiment_id"] == recorder.metadata["experiment_id"]
    assert config["experiment_id"] == recorder.metadata["experiment_id"]
    recorder.finish("test")


def test_generated_local_and_utc_timestamps_share_the_same_instant(tmp_path):
    recorder = ExperimentRecorder.create(tmp_path, "timestamp pair", {"simulation": True}, ModeSettings(Mode.DCV))
    sample = recorder.append_reading(None, mode=Mode.DCV, status="invalid", error="no reading")
    event = recorder.append_event("timestamp check")

    sample_local = datetime.fromisoformat(sample["timestamp_local"])
    sample_utc = datetime.fromisoformat(sample["timestamp_utc"])
    event_local = datetime.fromisoformat(event["timestamp_local"])
    event_utc = datetime.fromisoformat(event["timestamp_utc"])
    assert sample_local.astimezone(timezone.utc) == sample_utc
    assert event_local.astimezone(timezone.utc) == event_utc
    recorder.finish("stopped")


def test_constructor_closes_earlier_streams_if_a_later_open_fails(tmp_path, monkeypatch):
    opened_streams = []
    original_open = Path.open

    def fail_open_events(path, *args, **kwargs):
        if path.name == "events.csv":
            raise OSError("injected open failure")
        stream = original_open(path, *args, **kwargs)
        opened_streams.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", fail_open_events)
    with pytest.raises(OSError, match="injected open failure"):
        ExperimentRecorder.create(tmp_path, "open failure", {"simulation": True}, ModeSettings(Mode.DCV))

    assert len(opened_streams) == 1
    assert opened_streams[0].closed


def test_finish_closes_all_files_even_if_metadata_write_fails(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(tmp_path, "finish failure", {"simulation": True}, ModeSettings(Mode.DCV))
    streams = (recorder._data_stream, recorder._event_stream, recorder._log_stream)

    def fail_metadata_write():
        raise OSError("injected metadata failure")

    monkeypatch.setattr(recorder, "_write_metadata", fail_metadata_write)
    with pytest.raises(OSError, match="injected metadata failure"):
        recorder.finish("stopped")

    assert all(stream.closed for stream in streams)
    recorder.finish("stopped")  # a second close remains safe and does not repeat failed I/O


def test_failed_final_csv_sync_keeps_run_recoverable(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "final sync failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    recorder.append_reading(make_reading(), mode=Mode.DCV)

    def fail_final_sync(*, force=False):
        raise OSError("injected final CSV fsync failure")

    monkeypatch.setattr(recorder, "_sync_csv_streams", fail_final_sync)
    with pytest.raises(OSError, match="injected final CSV fsync failure"):
        recorder.finish("stopped")

    config_path = recorder.directory / "config.json"
    after_failed_finish = json.loads(config_path.read_text(encoding="utf-8"))
    assert after_failed_finish["completion_status"] == "running"

    with storage_module.ExperimentNameLock(tmp_path, "final sync failure").acquire() as lock:
        recovery = storage_module.mark_stale_experiment_interrupted(
            tmp_path,
            "final sync failure",
            expected_directory=recorder.directory,
            lock=lock,
        )

    recovered = json.loads(config_path.read_text(encoding="utf-8"))
    assert recovery is not None
    assert recovered["completion_status"] == "interrupted"
    assert recovered["recovery"]["measurements_csv"]["complete_prefix_bytes"] > 0


def test_failed_final_log_sync_keeps_run_recoverable(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "final log sync failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    recorder.append_reading(make_reading(), mode=Mode.DCV)

    def fail_final_log_sync():
        raise OSError("injected final run.log fsync failure")

    monkeypatch.setattr(recorder, "_sync_log_stream", fail_final_log_sync, raising=False)
    with pytest.raises(OSError, match="injected final run.log fsync failure"):
        recorder.finish("stopped")

    config_path = recorder.directory / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["completion_status"] == "running"


def test_failed_stream_close_keeps_run_recoverable(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "final close failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    recorder.append_reading(make_reading(), mode=Mode.DCV)
    log_stream = recorder._log_stream

    class FailingCloseStream:
        def __init__(self, stream):
            self.stream = stream

        @property
        def closed(self):
            return self.stream.closed

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def close(self):
            raise OSError("injected run.log close failure")

    recorder._log_stream = FailingCloseStream(log_stream)
    try:
        with pytest.raises(OSError, match="injected run.log close failure"):
            recorder.finish("stopped")
        assert log_stream.closed
        assert recorder._directory_lease is None
    finally:
        if not log_stream.closed:
            log_stream.close()

    config_path = recorder.directory / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["completion_status"] == "running"

    with storage_module.ExperimentNameLock(tmp_path, "final close failure").acquire() as lock:
        recovery = storage_module.mark_stale_experiment_interrupted(
            tmp_path,
            "final close failure",
            expected_directory=recorder.directory,
            lock=lock,
        )
    assert recovery is not None
    assert json.loads(config_path.read_text(encoding="utf-8"))["completion_status"] == "interrupted"


@pytest.mark.parametrize("name", ["bad/name", "bad\\name", "bad:name", "bad\x00name"])
def test_validate_experiment_name_rejects_windows_path_and_control_characters(name):
    with pytest.raises(ValueError, match="实验名称"):
        storage_module.validate_experiment_name(name)


def test_validate_experiment_name_preserves_valid_name():
    assert storage_module.validate_experiment_name("  阶跃响应 测试  ") == "阶跃响应 测试"


def test_get_available_space_bytes_reports_free_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(total=100, used=25, free=75),
    )

    assert storage_module.get_available_space_bytes(tmp_path) == 75


@pytest.mark.parametrize(
    ("free_bytes", "expected"),
    [(int(4.5 * 1024**3), "4.5 GiB"), (512 * 1024**2, "512 MiB")],
)
def test_format_available_space_uses_readable_binary_units(free_bytes, expected):
    assert storage_module.format_available_space(free_bytes) == expected


def test_require_minimum_free_space_accepts_exact_startup_reserve():
    assert storage_module.require_minimum_free_space(
        storage_module.MINIMUM_FREE_SPACE_BYTES
    ) == storage_module.MINIMUM_FREE_SPACE_BYTES


def test_require_minimum_free_space_rejects_one_byte_below_startup_reserve():
    with pytest.raises(OSError, match="可用空间不足"):
        storage_module.require_minimum_free_space(storage_module.MINIMUM_FREE_SPACE_BYTES - 1)


def test_estimate_recording_capacity_uses_target_interval_and_keeps_startup_reserve():
    free_bytes = storage_module.MINIMUM_FREE_SPACE_BYTES + 8 * storage_module.ESTIMATED_SAMPLE_BYTES

    seconds = storage_module.estimate_recording_capacity_seconds(free_bytes, 0.25)

    assert seconds == 2.0


@pytest.mark.parametrize("target_interval_s", [None, 0])
def test_estimate_recording_capacity_is_unknown_for_fastest_mode(target_interval_s):
    assert storage_module.estimate_recording_capacity_seconds(
        4 * 1024**3, target_interval_s
    ) is None


def test_reserve_experiment_directory_retries_after_atomic_name_collision(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(storage_module, "datetime", FrozenDateTime)
    base = tmp_path / "20261015_120000_000000_shared"
    original_mkdir = Path.mkdir
    collision_injected = False

    def mkdir_with_competing_creator(path, *args, **kwargs):
        nonlocal collision_injected
        if path == base and not collision_injected:
            collision_injected = True
            original_mkdir(path, *args, **kwargs)
            raise FileExistsError(path)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir_with_competing_creator)

    reserved = storage_module.reserve_experiment_directory(tmp_path, "shared")

    assert collision_injected
    assert base.is_dir()
    assert reserved == tmp_path / "20261015_120000_000000_shared_1"
    assert reserved.is_dir()


def test_prepare_experiment_storage_probes_and_cleans_writable_directory(tmp_path, monkeypatch):
    root = tmp_path / "new experiments"
    low_water = storage_module.MINIMUM_FREE_SPACE_BYTES
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=low_water * 2, used=0, free=low_water),
    )

    prepared = storage_module.prepare_experiment_storage(root)

    assert prepared == root
    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_prepare_experiment_storage_rejects_low_free_space(tmp_path, monkeypatch):
    root = tmp_path / "low disk"
    low_water = storage_module.MINIMUM_FREE_SPACE_BYTES
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=low_water * 2, used=low_water + 1, free=low_water - 1),
    )

    with pytest.raises(OSError, match="可用空间不足"):
        storage_module.prepare_experiment_storage(root)

    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_recorder_stops_appending_when_free_space_drops_below_reserve(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(tmp_path, "low-space-run", {"simulation": True}, ModeSettings(Mode.DCV))
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100, used=100, free=storage_module.MINIMUM_FREE_SPACE_BYTES - 1),
    )
    try:
        with pytest.raises(OSError, match="已停止继续写入"):
            recorder.append_reading(make_reading(1.0, "+1E+00"), mode=Mode.DCV)
        assert recorder.sample_count == 0
        assert recorder.metadata["sample_count"] == 0
    finally:
        recorder.finish("stopped")


def test_partial_csv_write_does_not_increment_persisted_sample_count(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "partial csv write", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    original_stream = recorder._data_stream

    class PartialFailureStream:
        def write(self, text):
            original_stream.write(text[: max(1, len(text) // 2)])
            raise OSError("injected partial CSV write failure")

        def __getattr__(self, name):
            return getattr(original_stream, name)

    failing_stream = PartialFailureStream()
    recorder._data_stream = failing_stream
    recorder._writer = csv.DictWriter(
        failing_stream, fieldnames=storage_module.SAMPLE_FIELDS, extrasaction="ignore"
    )
    try:
        with pytest.raises(OSError, match="injected partial CSV write failure"):
            recorder.append_reading(make_reading(), mode=Mode.DCV)

        assert recorder.sample_count == 0
        assert recorder.metadata["sample_count"] == 0
        recorder.finish("storage_error")

        config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
        assert config["sample_count"] == 0
        assert config["completion_status"] == "storage_error"
        csv_path = recorder.directory / "measurements.csv"
        complete_end = storage_module.complete_csv_prefix_size(csv_path)
        raw = csv_path.read_bytes()
        assert complete_end < len(raw)
        complete_rows = list(
            csv.DictReader(io.StringIO(raw[:complete_end].decode("utf-8-sig")))
        )
        assert complete_rows == []
    finally:
        for stream in (recorder._data_stream, recorder._event_stream, recorder._log_stream):
            if stream is not None and not stream.closed:
                stream.close()


def test_csv_flush_failure_reconciles_count_if_final_flush_succeeds(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "csv flush retry", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    original_stream = recorder._data_stream

    class FlushFailsOnceStream:
        def __init__(self):
            self.failed = False

        def flush(self):
            original_stream.flush()
            if not self.failed:
                self.failed = True
                raise OSError("injected CSV flush failure after write")

        def __getattr__(self, name):
            return getattr(original_stream, name)

    failing_stream = FlushFailsOnceStream()
    recorder._data_stream = failing_stream
    recorder._writer = csv.DictWriter(
        failing_stream, fieldnames=storage_module.SAMPLE_FIELDS, extrasaction="ignore"
    )

    with pytest.raises(OSError, match="injected CSV flush failure after write"):
        recorder.append_reading(make_reading(), mode=Mode.DCV)
    assert recorder.sample_count == 0
    with pytest.raises(OSError, match="前一条测量记录的CSV写入状态未确认"):
        recorder.append_reading(make_reading(2.0, "+2E+00"), mode=Mode.DCV)
    assert recorder.sample_count == 0

    recorder.finish("storage_error")

    with (recorder.directory / "measurements.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["sample_index"] == "1"
    assert config["sample_count"] == 1
    assert config["completion_status"] == "storage_error"


@pytest.mark.parametrize(
    "csv_bytes",
    [
        b"sample_index,raw_reading\r\n2,1.25\r\n",
        b"sample_index,raw_reading\r\nnot-an-index,1.25\r\n",
    ],
)
def test_complete_csv_record_count_rejects_nonsequential_indices(tmp_path, csv_bytes):
    path = tmp_path / "measurements.csv"
    path.write_bytes(csv_bytes)
    prefix_bytes = storage_module.complete_csv_prefix_size(path)

    with pytest.raises(ValueError, match="sample_index.*索引"):
        storage_module._count_complete_csv_records(path, prefix_bytes, "sample_index")


def test_event_flush_failure_counts_only_complete_csv_records_on_finish(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "event flush reconciliation", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    original_stream = recorder._event_stream
    header_size = (recorder.directory / "events.csv").stat().st_size

    class FlushTruncatesEventOnceStream:
        def __init__(self):
            self.failed = False

        def flush(self):
            original_stream.flush()
            if not self.failed:
                self.failed = True
                os.ftruncate(original_stream.fileno(), header_size)
                original_stream.seek(header_size)
                raise OSError("injected event CSV flush failure after partial persistence")

        def __getattr__(self, name):
            return getattr(original_stream, name)

    failing_stream = FlushTruncatesEventOnceStream()
    recorder._event_stream = failing_stream
    recorder._events = csv.DictWriter(
        failing_stream, fieldnames=storage_module.EVENT_FIELDS, extrasaction="ignore"
    )

    with pytest.raises(OSError, match="injected event CSV flush failure"):
        recorder.append_event("partially persisted event")

    recorder.finish("storage_error")

    with (recorder.directory / "events.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert rows == []
    assert recorder.event_count == 0
    assert config["event_count"] == 0
    assert config["completion_status"] == "storage_error"


def test_event_flush_failure_preserves_complete_event_on_finish(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "complete event flush failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    original_stream = recorder._event_stream

    class FlushFailsOnceStream:
        def __init__(self):
            self.failed = False

        def flush(self):
            original_stream.flush()
            if not self.failed:
                self.failed = True
                raise OSError("injected event CSV flush failure after write")

        def __getattr__(self, name):
            return getattr(original_stream, name)

    failing_stream = FlushFailsOnceStream()
    recorder._event_stream = failing_stream
    recorder._events = csv.DictWriter(
        failing_stream, fieldnames=storage_module.EVENT_FIELDS, extrasaction="ignore"
    )

    with pytest.raises(OSError, match="injected event CSV flush failure"):
        recorder.append_event("complete despite flush error")
    assert recorder.event_count == 0
    with pytest.raises(OSError, match="前一条事件记录的CSV写入状态未确认"):
        recorder.append_reading(make_reading(), mode=Mode.DCV)
    with pytest.raises(OSError, match="前一条事件记录的CSV写入状态未确认"):
        recorder.append_event("must not append after ambiguous write")

    recorder.finish("storage_error")

    with (recorder.directory / "events.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["event_index"] == "1"
    assert recorder.event_count == 1
    assert config["event_count"] == 1
    assert config["completion_status"] == "storage_error"


def test_sample_flush_failure_reconciles_truncated_record_on_finish(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "truncated sample flush", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    original_stream = recorder._data_stream
    header_size = (recorder.directory / "measurements.csv").stat().st_size

    class FlushTruncatesSampleOnceStream:
        def __init__(self):
            self.failed = False

        def flush(self):
            original_stream.flush()
            if not self.failed:
                self.failed = True
                os.ftruncate(original_stream.fileno(), header_size)
                original_stream.seek(header_size)
                raise OSError("injected measurement CSV flush failure after partial persistence")

        def __getattr__(self, name):
            return getattr(original_stream, name)

    failing_stream = FlushTruncatesSampleOnceStream()
    recorder._data_stream = failing_stream
    recorder._writer = csv.DictWriter(
        failing_stream, fieldnames=storage_module.SAMPLE_FIELDS, extrasaction="ignore"
    )

    with pytest.raises(OSError, match="injected measurement CSV flush failure"):
        recorder.append_reading(make_reading(), mode=Mode.DCV)
    with pytest.raises(OSError, match="前一条测量记录的CSV写入状态未确认"):
        recorder.append_event("must not append while sample write is unresolved")
    recorder.finish("storage_error")

    with (recorder.directory / "measurements.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert rows == []
    assert recorder.sample_count == 0
    assert config["sample_count"] == 0
    assert config["completion_status"] == "storage_error"


def test_sample_fsync_failure_keeps_pending_and_blocks_event_append(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "sample fsync failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    monkeypatch.setattr(storage_module, "CSV_FSYNC_INTERVAL_NS", 0)
    original_fsync = os.fsync
    failed = False

    def fail_first_fsync(fd):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected CSV fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(storage_module.os, "fsync", fail_first_fsync)

    with pytest.raises(OSError, match="injected CSV fsync failure"):
        recorder.append_reading(make_reading(), mode=Mode.DCV)
    assert recorder.sample_count == 0
    with pytest.raises(OSError, match="前一条测量记录的CSV写入状态未确认"):
        recorder.append_event("must not append while sample fsync is unresolved")

    with pytest.raises(OSError, match="injected CSV fsync failure"):
        recorder.finish("storage_error")
    assert json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))[
        "completion_status"
    ] == "running"
    assert all(stream.closed for stream in (recorder._data_stream, recorder._event_stream))

    with (recorder.directory / "measurements.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    with (recorder.directory / "events.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        events = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert events == []
    assert recorder.sample_count == 1
    assert config["sample_count"] == 0
    assert config["event_count"] == 0
    assert config["completion_status"] == "running"


def test_event_csv_fsync_failure_keeps_pending_sample_and_blocks_append(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "event CSV fsync failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    monkeypatch.setattr(storage_module, "CSV_FSYNC_INTERVAL_NS", 0)
    event_fd = recorder._event_stream.fileno()
    original_fsync = os.fsync
    failed = False

    def fail_event_csv_once(fd):
        nonlocal failed
        if fd == event_fd and not failed:
            failed = True
            raise OSError("injected event CSV fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(storage_module.os, "fsync", fail_event_csv_once)

    with pytest.raises(OSError, match="injected event CSV fsync failure"):
        recorder.append_reading(make_reading(), mode=Mode.DCV)
    assert failed
    assert recorder.sample_count == 0
    with pytest.raises(OSError, match="前一条测量记录的CSV写入状态未确认"):
        recorder.append_event("must not append while sample sync is unresolved")

    with pytest.raises(OSError, match="injected event CSV fsync failure"):
        recorder.finish("storage_error")
    assert json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))[
        "completion_status"
    ] == "running"
    assert all(stream.closed for stream in (recorder._data_stream, recorder._event_stream))

    with (recorder.directory / "measurements.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    with (recorder.directory / "events.csv").open(
        newline="", encoding="utf-8-sig"
    ) as stream:
        events = list(csv.DictReader(stream))
    config = json.loads((recorder.directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert events == []
    assert recorder.sample_count == 1
    assert config["sample_count"] == 0
    assert config["event_count"] == 0
    assert config["completion_status"] == "running"


def test_prepare_experiment_storage_reports_write_probe_failure(tmp_path, monkeypatch):
    root = tmp_path / "unwritable"
    low_water = storage_module.MINIMUM_FREE_SPACE_BYTES
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=low_water * 2, used=0, free=low_water),
    )

    def fail_probe(**_kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(storage_module.tempfile, "NamedTemporaryFile", fail_probe)
    with pytest.raises(OSError, match="不可写"):
        storage_module.prepare_experiment_storage(root)


def test_same_experiment_name_creates_distinct_directories_without_overwriting(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(storage_module, "datetime", FrozenDateTime)
    settings = ModeSettings(Mode.DCV)
    first = ExperimentRecorder.create(tmp_path, "same name", {"simulation": True}, settings)
    first.append_reading(make_reading(1.0, "+1E+00"), mode=Mode.DCV)
    second = ExperimentRecorder.create(tmp_path, "same name", {"simulation": True}, settings)

    assert first.directory != second.directory
    assert first.directory.name.endswith("_same_name")
    assert second.directory.name.endswith("_same_name_1")
    assert storage_module.find_existing_experiment_directories(tmp_path, "same name") == [
        first.directory, second.directory,
    ]
    with (first.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        assert len(list(csv.DictReader(stream))) == 1
    with (second.directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        assert list(csv.DictReader(stream)) == []
    first.finish("stopped")
    second.finish("stopped")


def test_same_experiment_name_detection_matches_windows_case_insensitive_folders(tmp_path, monkeypatch):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(storage_module, "datetime", FrozenDateTime)
    recorder = ExperimentRecorder.create(tmp_path, "Test Run", {"simulation": True}, ModeSettings(Mode.DCV))
    try:
        assert storage_module.find_existing_experiment_directories(tmp_path, "test run") == [recorder.directory]
    finally:
        recorder.finish("stopped")


def test_archive_latest_same_slug_run_requires_exact_name_metadata(tmp_path):
    different_name = tmp_path / "20261017_120000_000001_dose_1"
    exact_name = tmp_path / "20261016_120000_000001_dose_1"
    different_name.mkdir()
    exact_name.mkdir()
    (different_name / "measurements.csv").write_bytes(b"keep dose_1\r\n")
    (different_name / "config.json").write_text(
        json.dumps({"experiment_name": "dose_1", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    (exact_name / "measurements.csv").write_bytes(b"archive dose 1\r\n")
    (exact_name / "config.json").write_text(
        json.dumps({"experiment_name": "dose 1", "completion_status": "stopped"}),
        encoding="utf-8",
    )

    backup = storage_module.archive_latest_existing_experiment_directory(tmp_path, "dose 1")

    assert different_name.is_dir()
    assert (different_name / "measurements.csv").read_bytes() == b"keep dose_1\r\n"
    assert not exact_name.exists()
    assert (backup / "measurements.csv").read_bytes() == b"archive dose 1\r\n"


def test_archive_refuses_run_without_matching_metadata_or_finished_status(tmp_path):
    active = tmp_path / "20261017_120000_000001_dose_1"
    active.mkdir()
    (active / "measurements.csv").write_bytes(b"active run\r\n")
    (active / "config.json").write_text(
        json.dumps({"experiment_name": "dose 1", "completion_status": "running"}),
        encoding="utf-8",
    )

    with pytest.raises(OSError):
        storage_module.archive_latest_existing_experiment_directory(tmp_path, "dose 1")

    assert active.is_dir()
    assert (active / "measurements.csv").read_bytes() == b"active run\r\n"
    assert not (tmp_path / ".8845a-backups").exists()


def test_stale_run_recovery_marks_interrupted_and_preserves_raw_files(tmp_path):
    directory = tmp_path / "20261019_120000_000001_recover_me"
    directory.mkdir()
    measurement_bytes = (
        b"\xef\xbb\xbf"
        b"sample_index,raw_reading,status\r\n"
        b'1,"first line\r\nsecond, line",ok\r\n'
        b"2,partial"
    )
    event_bytes = b"event_index,event\r\n1,started\r\n2,partial"
    (directory / "measurements.csv").write_bytes(measurement_bytes)
    (directory / "events.csv").write_bytes(event_bytes)
    original_config = json.dumps(
        {
            "experiment_name": "recover me",
            "completion_status": "running",
            "experiment_name_lock_protocol": 1,
            "sample_count": 0,
            "event_count": 0,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    (directory / "config.json").write_bytes(original_config)

    with storage_module.ExperimentNameLock(tmp_path, "recover me").acquire() as lock:
        recovery = storage_module.mark_stale_experiment_interrupted(
            tmp_path, "recover me", expected_directory=directory, lock=lock,
        )
        assert lock._fd is not None

    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert metadata["completion_status"] == "interrupted"
    assert metadata["sample_count"] == 1
    assert metadata["event_count"] == 1
    assert metadata["recovery"]["sample_count_checkpoint"] == 0
    assert metadata["recovery"]["recovered_sample_count"] == 1
    assert metadata["recovery"]["recovered_event_count"] == 1
    assert recovery["measurements_csv"]["complete_record_count"] == 1
    assert recovery["events_csv"]["complete_record_count"] == 1
    assert recovery["measurements_csv"]["complete_prefix_bytes"] == measurement_bytes.rfind(b"\n") + 1
    assert recovery["measurements_csv"]["partial_tail_bytes"] == len(b"2,partial")
    assert recovery["events_csv"]["complete_prefix_bytes"] == event_bytes.rfind(b"\n") + 1
    assert recovery["events_csv"]["partial_tail_bytes"] == len(b"2,partial")
    assert (directory / "measurements.csv").read_bytes() == measurement_bytes
    assert (directory / "events.csv").read_bytes() == event_bytes
    backup_config_name = metadata["recovery"]["original_config_backup"]
    assert (directory / backup_config_name).read_bytes() == original_config

    archived = storage_module.archive_latest_existing_experiment_directory(
        tmp_path, "recover me", expected_directory=directory,
    )

    assert not directory.exists()
    assert (archived / "measurements.csv").read_bytes() == measurement_bytes
    assert (archived / "events.csv").read_bytes() == event_bytes


def test_abrupt_subprocess_exit_releases_lock_and_recovers_flushed_measurements(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    experiment_name = "abrupt process exit"
    child_code = """
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, sys.argv[3])
from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings, Reading
from fluke8845a.storage import ExperimentNameLock, ExperimentRecorder

root = Path(sys.argv[1])
name = sys.argv[2]
stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
with ExperimentNameLock(root, name):
    recorder = ExperimentRecorder.create(
        root, name, {"simulation": True}, ModeSettings(Mode.DCV)
    )
    for index, value in enumerate((1.25, 1.5)):
        start_ns = 1_000_000_000 + index * 100_000_000
        recorder.append_reading(
            Reading(
                f"{value:+.8E}", value, 10.0, start_ns, start_ns + 10_000_000,
                stamp, stamp,
            ),
            mode=Mode.DCV,
        )
    os._exit(23)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            str(tmp_path),
            experiment_name,
            str(repository_root / "src"),
        ],
        cwd=repository_root,
        capture_output=True,
        text=False,
        timeout=10,
        check=False,
    )

    assert child.returncode == 23, child.stderr.decode("utf-8", errors="replace")

    with storage_module.ExperimentNameLock(tmp_path, experiment_name).acquire() as lock:
        matches = storage_module.find_existing_experiment_directories(tmp_path, experiment_name)
        assert len(matches) == 1
        directory = matches[0]
        measurements_path = directory / "measurements.csv"
        raw_measurements = measurements_path.read_bytes()
        original_config = (directory / "config.json").read_bytes()
        recovery = storage_module.mark_stale_experiment_interrupted(
            tmp_path,
            experiment_name,
            expected_directory=directory,
            lock=lock,
        )

    assert recovery is not None
    assert measurements_path.read_bytes() == raw_measurements
    with measurements_path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["sample_index"] for row in rows] == ["1", "2"]
    assert [row["value_si"] for row in rows] == ["1.25", "1.5"]
    assert recovery["measurements_csv"]["complete_prefix_bytes"] == len(raw_measurements)
    assert recovery["measurements_csv"]["partial_tail_bytes"] == 0

    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert metadata["completion_status"] == "interrupted"
    assert metadata["recovery"]["sample_count_checkpoint"] == 0
    assert metadata["sample_count"] == 2
    assert metadata["recovery"]["recovered_sample_count"] == 2
    assert recovery["measurements_csv"]["complete_record_count"] == 2
    backup = directory / metadata["recovery"]["original_config_backup"]
    assert backup.read_bytes() == original_config


def test_stale_recovery_keeps_checkpoint_when_csv_record_count_is_ambiguous(tmp_path):
    directory = tmp_path / "20261019_120000_000001_ambiguous_run"
    directory.mkdir()
    (directory / "measurements.csv").write_bytes(
        b"sample_index,raw_reading,status\r\n1,1.25\r\n"
    )
    (directory / "events.csv").write_bytes(b"event_index,event\r\n")
    config = {
        "experiment_name": "ambiguous run",
        "completion_status": "running",
        "experiment_name_lock_protocol": 1,
        "sample_count": 100,
        "event_count": 0,
    }
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")

    with storage_module.ExperimentNameLock(tmp_path, "ambiguous run").acquire() as lock:
        recovery = storage_module.mark_stale_experiment_interrupted(
            tmp_path, "ambiguous run", expected_directory=directory, lock=lock,
        )

    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert recovery is not None
    assert metadata["sample_count"] == 100
    assert metadata["recovery"]["sample_count_checkpoint"] == 100
    assert metadata["recovery"]["recovered_sample_count"] is None
    assert recovery["measurements_csv"]["complete_record_count"] is None
    assert "列数与表头不一致" in recovery["measurements_csv"]["record_count_error"]


@pytest.mark.parametrize("measurement_bytes", [b"", b"sample_index,raw_reading"])
def test_stale_recovery_keeps_checkpoint_without_a_complete_csv_header(
    tmp_path, measurement_bytes,
):
    directory = tmp_path / "20261019_120000_000001_incomplete_header"
    directory.mkdir()
    measurements_path = directory / "measurements.csv"
    measurements_path.write_bytes(measurement_bytes)
    (directory / "events.csv").write_bytes(b"event_index,event\r\n")
    config = {
        "experiment_name": "incomplete header",
        "completion_status": "running",
        "experiment_name_lock_protocol": 1,
        "sample_count": 12,
        "event_count": 0,
    }
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")

    with storage_module.ExperimentNameLock(tmp_path, "incomplete header").acquire() as lock:
        recovery = storage_module.mark_stale_experiment_interrupted(
            tmp_path, "incomplete header", expected_directory=directory, lock=lock,
        )

    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert recovery is not None
    assert metadata["sample_count"] == 12
    assert metadata["recovery"]["sample_count_checkpoint"] == 12
    assert metadata["recovery"]["recovered_sample_count"] is None
    assert recovery["measurements_csv"]["complete_record_count"] is None
    assert "完整表头" in recovery["measurements_csv"]["record_count_error"]
    assert measurements_path.read_bytes() == measurement_bytes


def test_stale_run_recovery_requires_matching_acquired_lock(tmp_path):
    directory = tmp_path / "20261019_120000_000001_recover_me"
    directory.mkdir()
    original_config = json.dumps(
        {"experiment_name": "recover me", "completion_status": "running"},
    )
    (directory / "config.json").write_text(original_config, encoding="utf-8")
    (directory / "measurements.csv").write_bytes(b"header\r\n")
    lock = storage_module.ExperimentNameLock(tmp_path, "recover me")

    with pytest.raises(OSError, match="锁"):
        storage_module.mark_stale_experiment_interrupted(
            tmp_path, "recover me", expected_directory=directory, lock=lock,
        )

    assert json.loads((directory / "config.json").read_text(encoding="utf-8"))["completion_status"] == "running"
    assert not list(directory.glob("*recovery*"))


def test_csv_prefix_can_include_closed_eof_quote_but_stops_at_unclosed_quote(tmp_path):
    source = tmp_path / "quoted-eof.csv"
    header = b"experiment_id,raw_reading\r\n"
    complete_row = b'run-1,"quoted, reading"'
    source.write_bytes(header + complete_row)

    assert storage_module.complete_csv_prefix_size(source, chunk_size=1) == len(header)
    assert storage_module.complete_csv_prefix_size(
        source, chunk_size=1, allow_complete_eof_record=True,
    ) == len(header + complete_row)

    incomplete_row = b'run-1,"unterminated'
    source.write_bytes(header + complete_row + b"\r\n" + incomplete_row)
    assert storage_module.complete_csv_prefix_size(
        source, chunk_size=1, allow_complete_eof_record=True,
    ) == len(header + complete_row + b"\r\n")


def test_stale_run_recovery_refuses_unmarked_legacy_writer_without_changes(tmp_path):
    directory = tmp_path / "20261019_120000_000001_recover_me"
    directory.mkdir()
    original_config = json.dumps(
        {"experiment_name": "recover me", "completion_status": "running"},
    ).encode("utf-8")
    original_csv = b"sample_index,raw_reading,status\r\n1,1.0,ok\r\n"
    (directory / "config.json").write_bytes(original_config)
    (directory / "measurements.csv").write_bytes(original_csv)

    with storage_module.ExperimentNameLock(tmp_path, "recover me").acquire() as lock:
        with pytest.raises(OSError, match="版本或锁协议"):
            storage_module.mark_stale_experiment_interrupted(
                tmp_path, "recover me", expected_directory=directory, lock=lock,
            )

    assert (directory / "config.json").read_bytes() == original_config
    assert (directory / "measurements.csv").read_bytes() == original_csv
    assert not list(directory.glob("*recovery*"))


def test_archive_refuses_if_latest_directory_changes_after_confirmation(tmp_path):
    confirmed = tmp_path / "20261016_120000_000001_repeat"
    confirmed.mkdir()
    (confirmed / "config.json").write_text(
        json.dumps({"experiment_name": "repeat", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    latest = tmp_path / "20261017_120000_000001_repeat"
    latest.mkdir()
    (latest / "config.json").write_text(
        json.dumps({"experiment_name": "repeat", "completion_status": "stopped"}),
        encoding="utf-8",
    )

    with pytest.raises(OSError):
        storage_module.archive_latest_existing_experiment_directory(
            tmp_path, "repeat", expected_directory=confirmed,
        )

    assert confirmed.is_dir()
    assert latest.is_dir()
    assert not (tmp_path / ".8845a-backups").exists()


def test_experiment_name_lock_blocks_same_name_and_releases_on_close(tmp_path):
    first = storage_module.ExperimentNameLock(tmp_path, "Same Name").acquire()
    try:
        with pytest.raises(OSError, match="同名实验正在其他程序中运行"):
            storage_module.ExperimentNameLock(tmp_path, "same name").acquire()

        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        child_code = (
            "import sys; from fluke8845a.storage import ExperimentNameLock; "
            "lock = ExperimentNameLock(sys.argv[1], sys.argv[2]); "
            "\ntry: lock.acquire()\nexcept OSError: print('blocked')\n"
            "else: lock.release(); print('acquired')"
        )
        child = subprocess.run(
            [sys.executable, "-c", child_code, str(tmp_path), "same name"],
            capture_output=True, text=True, env=environment, timeout=5, check=False,
        )
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "blocked"
    finally:
        first.release()

    with storage_module.ExperimentNameLock(tmp_path, "same name").acquire():
        pass


def test_experiment_name_lock_refuses_reparse_point_directory(tmp_path, monkeypatch):
    lock_directory = tmp_path / ".8845a-locks"
    real_check = storage_module._is_symlink_or_junction
    monkeypatch.setattr(
        storage_module,
        "_is_symlink_or_junction",
        lambda path: Path(path) == lock_directory or real_check(Path(path)),
    )

    with pytest.raises(OSError):
        storage_module.ExperimentNameLock(tmp_path, "junction check").acquire()


def test_archive_refuses_reparse_point_backup_directory(tmp_path, monkeypatch):
    existing = tmp_path / "20261017_120000_000001_repeat"
    existing.mkdir()
    (existing / "config.json").write_text(
        json.dumps({"experiment_name": "repeat", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    backup_root = tmp_path / ".8845a-backups"
    real_check = storage_module._is_symlink_or_junction
    monkeypatch.setattr(
        storage_module,
        "_is_symlink_or_junction",
        lambda path: Path(path) == backup_root or real_check(Path(path)),
    )

    with pytest.raises(OSError):
        storage_module.archive_latest_existing_experiment_directory(tmp_path, "repeat")

    assert existing.is_dir()
    assert not backup_root.exists()


def test_archive_refuses_reparse_point_config_metadata(tmp_path, monkeypatch):
    existing = tmp_path / "20261017_120000_000001_repeat"
    existing.mkdir()
    config_path = existing / "config.json"
    config_path.write_text(
        json.dumps({"experiment_name": "repeat", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    real_check = storage_module._is_symlink_or_junction
    monkeypatch.setattr(
        storage_module,
        "_is_symlink_or_junction",
        lambda path: Path(path) == config_path or real_check(Path(path)),
    )

    with pytest.raises(OSError):
        storage_module.archive_latest_existing_experiment_directory(tmp_path, "repeat")

    assert existing.is_dir()
    assert not (tmp_path / ".8845a-backups").exists()


def test_recorder_refuses_reserved_reparse_directory_before_writing(tmp_path, monkeypatch):
    root = tmp_path / "experiments"
    root.mkdir()
    reserved = storage_module.reserve_experiment_directory(root, "junction guard")
    real_check = storage_module._is_symlink_or_junction
    monkeypatch.setattr(
        storage_module,
        "_is_symlink_or_junction",
        lambda path: Path(path) == reserved or real_check(Path(path)),
    )

    with pytest.raises(OSError):
        ExperimentRecorder.create(
            root,
            "junction guard",
            {"simulation": True},
            ModeSettings(Mode.DCV),
            reserved_directory=reserved,
        )

    assert list(reserved.iterdir()) == []


def test_recorder_refuses_a_real_reserved_directory_symlink(tmp_path):
    root = tmp_path / "experiments"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    reserved = storage_module.reserve_experiment_directory(root, "symlink guard")
    reserved.rmdir()
    try:
        reserved.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"This Windows environment cannot create symlinks: {exc}")

    try:
        with pytest.raises(OSError):
            ExperimentRecorder.create(
                root,
                "symlink guard",
                {"simulation": True},
                ModeSettings(Mode.DCV),
                reserved_directory=reserved,
            )

        assert list(outside.iterdir()) == []
    finally:
        reserved.unlink(missing_ok=True)


def test_recorder_refuses_a_real_windows_reserved_directory_junction(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows directory junction integration test")
    root = tmp_path / "experiments"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    reserved = storage_module.reserve_experiment_directory(root, "junction integration")
    reserved.rmdir()
    command = (
        "New-Item -ItemType Junction "
        f"-Path '{str(reserved).replace(chr(39), chr(39) * 2)}' "
        f"-Target '{str(outside).replace(chr(39), chr(39) * 2)}' | Out-Null"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"could not create a directory junction: {result.stderr or result.stdout}")

    try:
        with pytest.raises(OSError):
            ExperimentRecorder.create(
                root,
                "junction integration",
                {"simulation": True},
                ModeSettings(Mode.DCV),
                reserved_directory=reserved,
            )

        assert list(outside.iterdir()) == []
    finally:
        reserved.rmdir()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory swap race test")
def test_recorder_metadata_write_cannot_swap_run_directory_for_junction(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_config = outside / "config.json"
    original_outside = b"external config sentinel; preserve exactly\r\n"
    outside_config.write_bytes(original_outside)
    recorder = ExperimentRecorder.create(
        tmp_path, "directory swap race", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    directory = recorder.directory
    moved = directory.with_name(f"{directory.name}-moved")
    replace = os.replace
    attack = {"rename_blocked": False, "junction_created": False}

    def create_junction() -> None:
        command = (
            "New-Item -ItemType Junction "
            f"-Path '{str(directory).replace(chr(39), chr(39) * 2)}' "
            f"-Target '{str(outside).replace(chr(39), chr(39) * 2)}' | Out-Null"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise OSError(f"could not create race-test junction: {result.stderr or result.stdout}")
        attack["junction_created"] = True

    def replace_after_directory_swap(source, target):
        try:
            os.rename(directory, moved)
        except PermissionError:
            attack["rename_blocked"] = True
        else:
            create_junction()
        return replace(source, target)

    monkeypatch.setattr(storage_module.os, "replace", replace_after_directory_swap)
    try:
        # finish() closes every output stream before atomically replacing config.json.
        # That is the narrow window where stream handles alone cannot pin the folder.
        recorder.finish("test")
        assert attack["rename_blocked"] is True
        assert outside_config.read_bytes() == original_outside
        os.rename(directory, moved)
        os.rename(moved, directory)
    finally:
        monkeypatch.setattr(storage_module.os, "replace", replace)
        if attack["junction_created"]:
            directory.rmdir()
        if moved.exists():
            os.rename(moved, directory)
        if not all(
            stream is None or stream.closed
            for stream in (recorder._data_stream, recorder._event_stream, recorder._log_stream)
        ):
            recorder.finish("test")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory lease lifecycle test")
def test_recorder_directory_lease_is_released_when_recorder_is_collected(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "directory lease gc", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    directory = recorder.directory
    moved = directory.with_name(f"{directory.name}-moved")
    del recorder
    gc.collect()

    os.rename(directory, moved)
    assert moved.is_dir()


def test_recorder_periodic_sync_start_failure_does_not_prevent_cleanup(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "sync thread start failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )

    class _ThreadThatCannotStart:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("simulated thread start failure")

        def is_alive(self):
            return False

        def join(self):
            raise RuntimeError("cannot join thread before it is started")

    monkeypatch.setattr(storage_module, "Thread", _ThreadThatCannotStart)
    with pytest.raises(RuntimeError, match="simulated thread start failure"):
        recorder.start_periodic_sync(lambda exc: None)

    assert recorder._csv_sync_thread is None
    recorder.finish("start_error")
    assert all(
        stream is None or stream.closed
        for stream in (recorder._data_stream, recorder._event_stream, recorder._log_stream)
    )
    assert recorder._directory_lease is None


def test_recorder_periodic_sync_start_error_joins_a_thread_that_already_started(
    tmp_path, monkeypatch
):
    recorder = ExperimentRecorder.create(
        tmp_path, "sync thread post-start failure", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    created_threads = []

    class _ThreadStartsThenFails:
        def __init__(self, *, target, name, daemon):
            self._thread = threading.Thread(target=target, name=name, daemon=daemon)
            self.join_calls = 0
            created_threads.append(self)

        @property
        def ident(self):
            return self._thread.ident

        def start(self):
            self._thread.start()
            raise RuntimeError("simulated post-start failure")

        def join(self):
            self.join_calls += 1
            self._thread.join()

        def is_alive(self):
            return self._thread.is_alive()

    monkeypatch.setattr(storage_module, "Thread", _ThreadStartsThenFails)
    with pytest.raises(RuntimeError, match="simulated post-start failure"):
        recorder.start_periodic_sync(lambda exc: None)

    sync_thread = created_threads[0]
    assert sync_thread.join_calls == 1
    assert not sync_thread.is_alive()
    assert recorder._csv_sync_thread is None
    recorder.finish("start_error")
    assert all(
        stream is None or stream.closed
        for stream in (recorder._data_stream, recorder._event_stream, recorder._log_stream)
    )
    assert recorder._directory_lease is None


@pytest.mark.parametrize(
    "filename", ["measurements.csv", "events.csv", "run.log", "config.json"]
)
def test_recorder_never_overwrites_existing_output_file(tmp_path, filename):
    directory = tmp_path / "existing output"
    directory.mkdir()
    output = directory / filename
    original = b"external sentinel; do not overwrite\r\n"
    output.write_bytes(original)

    with pytest.raises(OSError):
        ExperimentRecorder(directory, {"simulation": True}, ModeSettings(Mode.DCV))

    assert output.read_bytes() == original
    assert list(directory.iterdir()) == [output]


@pytest.mark.parametrize(
    "filename", ["measurements.csv", "events.csv", "run.log", "config.json"]
)
def test_recorder_refuses_output_file_symlink_without_touching_target(
    tmp_path, filename,
):
    directory = tmp_path / "symlinked output"
    directory.mkdir()
    output = directory / filename
    outside = tmp_path / f"outside-{filename}"
    original = b"external symlink sentinel\r\n"
    outside.write_bytes(original)
    try:
        output.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"This Windows environment cannot create symlinks: {exc}")

    with pytest.raises(OSError):
        ExperimentRecorder(directory, {"simulation": True}, ModeSettings(Mode.DCV))

    assert output.is_symlink()
    assert outside.read_bytes() == original
    assert list(directory.iterdir()) == [output]


def test_recorder_metadata_does_not_touch_legacy_fixed_temp_path(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "metadata temp", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    fixed_temp = recorder.directory / "config.json.tmp"
    original = b"external metadata sentinel\r\n"
    fixed_temp.write_bytes(original)
    try:
        recorder._write_metadata()
        assert fixed_temp.read_bytes() == original
        assert json.loads(
            (recorder.directory / "config.json").read_text(encoding="utf-8")
        )["completion_status"] == "running"
    finally:
        recorder.finish("test")


def test_recorder_metadata_does_not_follow_legacy_fixed_temp_symlink(tmp_path):
    recorder = ExperimentRecorder.create(
        tmp_path, "metadata temp symlink", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    fixed_temp = recorder.directory / "config.json.tmp"
    outside = tmp_path / "outside-config-temp.json"
    original = b"external metadata temp sentinel\r\n"
    outside.write_bytes(original)
    try:
        fixed_temp.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        recorder.finish("test")
        pytest.skip(f"This Windows environment cannot create symlinks: {exc}")

    try:
        recorder._write_metadata()
        assert fixed_temp.is_symlink()
        assert outside.read_bytes() == original
        assert json.loads(
            (recorder.directory / "config.json").read_text(encoding="utf-8")
        )["completion_status"] == "running"
    finally:
        fixed_temp.unlink(missing_ok=True)
        recorder.finish("test")


def test_recorder_metadata_refuses_reparse_config_target(tmp_path, monkeypatch):
    recorder = ExperimentRecorder.create(
        tmp_path, "metadata target", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    config_path = recorder.directory / "config.json"
    original_config = config_path.read_bytes()
    outside = tmp_path / "outside-config.json"
    original_outside = b"outside metadata sentinel\r\n"
    outside.write_bytes(original_outside)
    real_check = storage_module._is_symlink_or_junction
    monkeypatch.setattr(
        storage_module,
        "_is_symlink_or_junction",
        lambda path: Path(path) == config_path or real_check(Path(path)),
    )

    with pytest.raises(OSError):
        recorder._write_metadata()

    assert outside.read_bytes() == original_outside
    assert config_path.read_bytes() == original_config
    monkeypatch.setattr(storage_module, "_is_symlink_or_junction", real_check)
    recorder.finish("test")

