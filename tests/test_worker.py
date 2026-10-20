from __future__ import annotations

import csv
import json
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Thread

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from fluke8845a import __version__
from fluke8845a.modes import Mode, descriptor
from fluke8845a.mock import Mock8845A, MockScenario
from fluke8845a.scpi import InstrumentError, ModeSettings, Serial8845A, SerialSettings, UnsupportedRange
from fluke8845a.state import AcquisitionState
import fluke8845a.worker as worker_module
from fluke8845a.storage import ExperimentNameLock
from fluke8845a.worker import (
    CONNECTION_WATCH_INTERVAL_MS, MAX_PENDING_GUI_ROWS,
    MAX_PENDING_GUI_SCPI_LOG_LINES, MeasurementWorker,
)
from tests.fakes import FakeTransport


@pytest.fixture(scope="module", autouse=True)
def qt_core_application():
    return QCoreApplication.instance() or QCoreApplication([])


def test_scpi_log_mailbox_coalesces_notifications_and_bounds_backlog(qt_core_application):
    worker = MeasurementWorker()
    notifications = []
    worker.scpi_logs_ready.connect(lambda: notifications.append(True))

    total = MAX_PENDING_GUI_SCPI_LOG_LINES + 17
    for index in range(total):
        worker._write_log(f"> command {index}")

    assert len(notifications) == 1
    lines, dropped = worker.take_scpi_log_update()
    assert len(lines) == MAX_PENDING_GUI_SCPI_LOG_LINES
    assert dropped == 17
    assert lines[0][1] == "> command 17"
    assert lines[-1][1] == f"> command {total - 1}"
    assert lines[0][0]

    assert worker.take_scpi_log_update() == ([], 0)
    worker._write_log("> fresh command")
    assert len(notifications) == 2


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        (MockScenario(name="worker smoke", read_latency_s=0, step_at_s=1e9, recovery_at_s=None), "ok"),
        (MockScenario(name="timeout injection", timeout_at_sample=1, read_latency_s=0), "timeout"),
        (MockScenario(name="disconnect injection", disconnect_at_sample=1, read_latency_s=0), "communication_error"),
    ],
)
def test_worker_one_shot_persists_valid_or_failed_attempt_without_fabricating_value(
    tmp_path, scenario, expected_status
):
    worker = MeasurementWorker()
    rows_seen = []
    finished = []
    worker.sample_ready.connect(lambda: rows_seen.extend(worker.take_sample_update()[0]))
    worker.experiment_finished.connect(finished.append)

    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, scenario)
    assert worker.instrument is not None and worker.instrument.connected
    worker.start_experiment(settings, "one shot", str(tmp_path), 0, True)

    assert worker.state is AcquisitionState.STOPPED
    with ExperimentNameLock(tmp_path, "one shot"):
        pass
    assert len(finished) == 1
    assert len(rows_seen) == 1
    assert rows_seen[0]["status"] == expected_status
    assert (rows_seen[0]["value_si"] != "") is (expected_status == "ok")

    directory = Path(finished[0])
    with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["status"] == expected_status
    assert bool(rows[0]["value_si"]) is (expected_status == "ok")
    assert metadata["simulation"] is True
    assert metadata["completion_status"] in {"stopped", "disconnected"}


def test_invalid_read_details_use_sample_mailbox_without_per_failure_log_signal(tmp_path, monkeypatch):
    from fluke8845a.scpi import ReadingError

    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="invalid response", read_latency_s=0))
    monkeypatch.setattr(
        worker.instrument,
        "read",
        lambda: (_ for _ in ()).throw(ReadingError("invalid response", raw="not-a-number")),
    )
    rows_seen = []
    logs = []
    finished = []
    worker.sample_ready.connect(lambda: rows_seen.extend(worker.take_sample_update()[0]))
    worker.log_line.connect(logs.append)
    worker.experiment_finished.connect(finished.append)

    worker.start_experiment(settings, "invalid response", str(tmp_path), 0, True)

    assert len(rows_seen) == 1
    assert rows_seen[0]["status"] == "invalid"
    assert "invalid response" in rows_seen[0]["error"]
    assert not any(message.startswith("! invalid：") for message in logs)
    directory = Path(finished[0])
    with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        saved_rows = list(csv.DictReader(stream))
    assert saved_rows[0]["status"] == "invalid"
    assert "invalid response" in saved_rows[0]["error"]
    assert "invalid response" in (directory / "run.log").read_text(encoding="utf-8")
    worker.shutdown()


def test_run_log_excludes_per_read_range_queries_but_keeps_setup_and_faults(qt_core_application):
    worker = MeasurementWorker()

    class Recorder:
        def __init__(self):
            self.entries = []

        def log(self, level, message):
            self.entries.append((level, message))

    recorder = Recorder()
    worker._recorder = recorder
    worker._write_log("> CONF:VOLT:DC 10")
    worker._write_log("> SENS:VOLT:DC:RANG?")
    worker._write_log("> SENS:VOLT:DC:RANG?")
    worker._write_log("> READ?")
    worker._write_log("! response timeout")

    assert recorder.entries == [
        ("SCPI", "> CONF:VOLT:DC 10"),
        ("SCPI", "! response timeout"),
    ]
    worker._recorder = None
    worker.shutdown()


def test_worker_holds_experiment_name_lock_until_continuous_run_stops(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="lock lifetime", read_latency_s=0))

    worker.start_experiment(settings, "lock lifetime", str(tmp_path), 60, False)

    assert worker.state is AcquisitionState.RUNNING
    with pytest.raises(OSError, match="同名实验正在其他程序中运行"):
        ExperimentNameLock(tmp_path, "lock lifetime").acquire()

    worker.stop()
    assert worker.state is AcquisitionState.STOPPED
    with ExperimentNameLock(tmp_path, "lock lifetime"):
        pass
    worker.shutdown()
    worker.shutdown()


def test_worker_reserves_output_directory_before_instrument_configuration(tmp_path, monkeypatch):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="reserve before configure", read_latency_s=0))
    reserved_paths = []
    calls = []
    real_reserve = worker_module.reserve_experiment_directory
    real_configure = Mock8845A.configure

    def record_reservation(root, name):
        path = real_reserve(root, name)
        reserved_paths.append(path)
        calls.append("reserve")
        return path

    def verify_reservation_before_configure(instrument, selected_settings):
        assert reserved_paths and reserved_paths[-1].is_dir()
        assert list(reserved_paths[-1].iterdir()) == []
        calls.append("configure")
        return real_configure(instrument, selected_settings)

    monkeypatch.setattr(worker_module, "reserve_experiment_directory", record_reservation)
    monkeypatch.setattr(Mock8845A, "configure", verify_reservation_before_configure)

    worker.start_experiment(settings, "reserved first", str(tmp_path), 0, True)

    assert calls[:2] == ["reserve", "configure"]
    assert worker.state is AcquisitionState.STOPPED
    assert reserved_paths[0].joinpath("measurements.csv").is_file()
    worker.shutdown()


def test_worker_removes_empty_reservation_if_instrument_configuration_fails(tmp_path, monkeypatch):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="reserve cleanup", read_latency_s=0))

    def reject_configuration(_instrument, _selected_settings):
        raise InstrumentError("simulated configuration failure")

    monkeypatch.setattr(Mock8845A, "configure", reject_configuration)

    worker.start_experiment(settings, "reserved failure", str(tmp_path), 0, True)

    assert worker.state is AcquisitionState.STOPPED
    assert list(path.name for path in tmp_path.iterdir()) == [".8845a-locks"]
    assert list((tmp_path / ".8845a-locks").iterdir())
    worker.shutdown()


def test_mock_zero_reports_command_and_post_operation_configuration_readback():
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV, autorange=False, range_si=1.0, nplc=10, autozero=True)
    worker.connect_mock(settings, MockScenario(name="zero readback", read_latency_s=0))
    results = []
    active_settings = []
    worker.zero_result.connect(results.append)
    worker.active_settings_ready.connect(active_settings.append)

    worker.zero()

    assert results and "MOCK模拟：未向真实仪器发送ZERO命令" in results[-1]
    assert "模式 DCV" in results[-1] and "10 NPLC" in results[-1]
    assert "本软件没有零点值查询项" in results[-1]
    assert active_settings[-1]["range_si"] == 1.0
    assert active_settings[-1]["range_readback_si"] == 1.0
    worker.shutdown()


def test_mock_autorange_exposes_only_a_labeled_simulated_range_snapshot():
    worker = MeasurementWorker()
    active_settings = []
    worker.active_settings_ready.connect(active_settings.append)

    worker.connect_mock(ModeSettings(Mode.DCV), MockScenario(name="range snapshot", read_latency_s=0))

    assert active_settings[-1]["autorange"] is True
    assert active_settings[-1]["range_si"] is None
    assert active_settings[-1]["range_readback_si"] is not None
    assert active_settings[-1]["simulation"] is True
    worker.shutdown()


def test_zero_readback_failure_is_reported_as_an_error_after_command_was_sent(monkeypatch):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV, autorange=False, range_si=10)
    worker.connect_mock(settings, MockScenario(name="zero readback failure", read_latency_s=0))

    def fail_readback(_mode):
        raise InstrumentError("simulated readback failure")

    monkeypatch.setattr(worker.instrument, "read_settings", fail_readback)
    errors = []
    results = []
    worker.error.connect(errors.append)
    worker.zero_result.connect(results.append)

    worker.zero()

    assert results and "MOCK模拟操作已执行" in results[-1] and "配置回读失败" in results[-1]
    assert errors and "MOCK模拟配置回读失败" in errors[-1]
    assert worker.last_zero_readback_ok is False
    worker.shutdown()


def test_zero_command_failure_emits_a_fresh_failure_result(monkeypatch):
    worker = MeasurementWorker()
    worker.connect_mock(
        ModeSettings(Mode.DCV), MockScenario(name="zero command failure", read_latency_s=0)
    )

    def fail_zero():
        raise InstrumentError("simulated ZERO command failure")

    monkeypatch.setattr(worker.instrument, "zero", fail_zero)
    results = []
    errors = []
    worker.zero_result.connect(results.append)
    worker.error.connect(errors.append)

    worker.zero()

    assert results and "ZERO失败" in results[-1]
    assert "模拟" in results[-1] and "配置回读失败" not in results[-1]
    assert errors and "ZERO失败" in errors[-1]
    assert worker.last_zero_readback_ok is False
    worker.shutdown()


@pytest.mark.parametrize("mode", [Mode.ACV, Mode.ACI])
def test_zero_feedback_marks_unsupported_analog_filter_not_applicable(mode):
    worker = MeasurementWorker()
    worker.connect_mock(ModeSettings(mode), MockScenario(name="zero no analog filter", read_latency_s=0))
    results = []
    worker.zero_result.connect(results.append)

    worker.zero()

    assert results and "模拟滤波 不适用" in results[-1]
    assert "模拟滤波 关" not in results[-1]
    worker.shutdown()


def test_zero_is_rejected_before_command_when_current_settings_were_not_read_back():
    class InstrumentWithoutReadback:
        connected = True

        def __init__(self):
            self.zero_calls = 0

        def zero(self):
            self.zero_calls += 1

        def close(self):
            self.connected = False

    worker = MeasurementWorker()
    instrument = InstrumentWithoutReadback()
    errors = []
    results = []
    worker.instrument = instrument
    worker.control_mode = "computer"
    worker.current_mode = Mode.DCV
    worker.error.connect(errors.append)
    worker.zero_result.connect(results.append)

    worker.zero()

    assert instrument.zero_calls == 0
    assert errors and "先读取或应用当前测量配置" in errors[-1]
    assert results and "ZERO未执行" in results[-1]
    worker.shutdown()


@pytest.mark.parametrize(
    ("firmware", "expected_state"),
    [
        ("", "unprobed_inactive"),
        ("1.9.0.0", "unsupported"),
        ("2.10.882.24", "unprobed_inactive"),
    ],
)
def test_active_settings_does_not_claim_filter_readback_without_evidence(firmware, expected_state):
    class CachedInstrument:
        range_firmware = (firmware, "test evidence")

        def close(self):
            pass

    worker = MeasurementWorker()
    worker.instrument = CachedInstrument()
    worker.simulation = False
    active = []
    worker.active_settings_ready.connect(active.append)

    worker._emit_active_settings(ModeSettings(Mode.DCV, nplc=1, digital_filter=False))

    expected_filter = None if expected_state == "unsupported" else False
    assert active[-1]["digital_filter"] is expected_filter
    assert active[-1]["digital_filter_state"] == expected_state
    worker.shutdown()


def test_invalid_experiment_name_is_rejected_before_instrument_configuration(tmp_path, monkeypatch):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="invalid path", read_latency_s=0))
    configured = []
    errors = []
    monkeypatch.setattr(worker.instrument, "configure", lambda selected: configured.append(selected))
    worker.error.connect(errors.append)

    worker.start_experiment(settings, "bad/name", str(tmp_path), 0, True)

    assert configured == []
    assert worker.state is AcquisitionState.STOPPED
    assert errors and "实验名称" in errors[-1]
    assert list(tmp_path.iterdir()) == []
    worker.shutdown()


def test_finalize_write_failure_is_not_reported_as_saved(tmp_path, monkeypatch):
    from fluke8845a.storage import ExperimentRecorder

    real_write_metadata = ExperimentRecorder._write_metadata
    metadata_writes = 0

    def fail_on_final_metadata_write(recorder):
        nonlocal metadata_writes
        metadata_writes += 1
        if metadata_writes == 3:
            raise OSError("disk full during final metadata write")
        real_write_metadata(recorder)

    monkeypatch.setattr(ExperimentRecorder, "_write_metadata", fail_on_final_metadata_write)
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="finalize failure", read_latency_s=0))
    finished = []
    errors = []
    logs = []
    statuses = []
    save_results = []
    worker.experiment_finished.connect(finished.append)
    worker.error.connect(errors.append)
    worker.log_line.connect(logs.append)
    worker.status_changed.connect(statuses.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )

    worker.start_experiment(settings, "finalize failure", str(tmp_path), 0, True)

    assert len(finished) == 1
    assert any("disk full during final metadata write" in message for message in errors)
    assert not any(message.startswith("实验保存完成：") for message in logs)
    assert "disk full" in statuses[-1]
    assert len(save_results) == 1
    assert save_results[0][1] is False
    assert "disk full" in save_results[0][2]
    worker.shutdown()


def test_non_oserror_finalize_failure_still_emits_save_failure_and_completion(tmp_path, monkeypatch):
    from fluke8845a.storage import ExperimentRecorder

    def fail_finish(_recorder, _status):
        raise ValueError("invalid metadata during finalization")

    monkeypatch.setattr(ExperimentRecorder, "finish", fail_finish)
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="invalid finalize", read_latency_s=0))
    finished = []
    errors = []
    logs = []
    statuses = []
    save_results = []
    worker.experiment_finished.connect(finished.append)
    worker.error.connect(errors.append)
    worker.log_line.connect(logs.append)
    worker.status_changed.connect(statuses.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )

    worker.start_experiment(settings, "invalid finalize", str(tmp_path), 0, True)

    assert len(finished) == 1
    assert len(save_results) == 1
    assert save_results[0][1] is False
    assert "invalid metadata" in save_results[0][2]
    assert any("invalid metadata" in message for message in errors)
    assert any("invalid metadata" in message for message in logs)
    assert "invalid metadata" in statuses[-1]
    worker.shutdown()


def test_startup_log_failure_stops_worker_and_marks_record_incomplete(tmp_path, monkeypatch):
    from fluke8845a.storage import ExperimentRecorder

    real_log = ExperimentRecorder.log
    log_calls = 0

    def fail_worker_start_log(recorder, level, message):
        nonlocal log_calls
        log_calls += 1
        if log_calls == 2:
            raise OSError("disk full while writing startup log")
        return real_log(recorder, level, message)

    monkeypatch.setattr(ExperimentRecorder, "log", fail_worker_start_log)
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="startup storage failure", read_latency_s=0))
    finished = []
    statuses = []
    save_results = []
    worker.experiment_finished.connect(finished.append)
    worker.status_changed.connect(statuses.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )

    worker.start_experiment(settings, "startup storage failure", str(tmp_path), 0, False)

    assert worker.state is AcquisitionState.STOPPED
    assert worker._recorder is None
    assert len(finished) == 1
    assert len(save_results) == 1
    assert save_results[0][1] is False
    assert "启动失败" in save_results[0][2]
    assert "启动失败" in statuses[-1]
    worker.shutdown()


def test_storage_fault_is_reported_as_incomplete_not_fully_saved(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="storage fault", read_latency_s=0))
    worker.start_experiment(settings, "storage fault", str(tmp_path), 1, False)
    logs = []
    statuses = []
    save_results = []
    worker.log_line.connect(logs.append)
    worker.status_changed.connect(statuses.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )

    worker._storage_fault(OSError("simulated disk write failure"))

    assert len(save_results) == 1
    assert save_results[0][1] is False
    assert "不完整" in save_results[0][2]
    assert not any(message.startswith("实验保存完成：") for message in logs)
    assert "不完整" in statuses[-1]
    worker.shutdown()


@pytest.mark.parametrize("run_log_write_fails", [False, True])
def test_midrun_low_disk_reserve_stops_before_next_mock_read_and_handles_run_log_failure(
    tmp_path, monkeypatch, run_log_write_fails
):
    from types import SimpleNamespace

    import fluke8845a.storage as storage_module

    free_bytes = [storage_module.MINIMUM_FREE_SPACE_BYTES + 1024 * 1024]
    monkeypatch.setattr(
        storage_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=100_000_000, used=100_000_000 - free_bytes[0], free=free_bytes[0]),
    )
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="mid-run low disk", read_latency_s=0))
    finished = []
    save_results = []
    errors = []
    worker.experiment_finished.connect(finished.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )
    worker.error.connect(errors.append)

    try:
        worker.start_experiment(settings, "mid-run low disk", str(tmp_path), 3600, False)
        worker._timer.stop()
        recorder = worker._recorder
        instrument = worker.instrument
        assert recorder is not None
        assert isinstance(instrument, Mock8845A)

        worker._take_sample()
        worker._timer.stop()
        assert recorder.sample_count == 1
        assert instrument._sample_number == 1

        if run_log_write_fails:
            real_log = recorder.log

            def fail_storage_diagnostic(level, message):
                if level == "ERROR" and "采集存储失败" in message:
                    raise OSError("simulated run.log disk-full failure")
                return real_log(level, message)

            monkeypatch.setattr(recorder, "log", fail_storage_diagnostic)

        free_bytes[0] = storage_module.MINIMUM_FREE_SPACE_BYTES - 1
        # The production check is throttled to once per five seconds. Force the
        # next check due instead of waiting in this deterministic test.
        recorder._last_space_check_ns = 0
        worker._take_sample()

        assert worker.state is AcquisitionState.STOPPED
        assert worker._recorder is None
        assert instrument._sample_number == 1  # reserve is checked before MOCK READ
        assert len(finished) == 1
        assert len(save_results) == 1
        assert save_results[0][1] is False
        assert "剩余空间低于安全余量" in errors[-1]

        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        run_log = (directory / "run.log").read_text(encoding="utf-8")
        assert len(rows) == 1
        assert rows[0]["sample_index"] == "1"
        assert rows[0]["status"] == "ok"
        assert metadata["sample_count"] == 1
        assert metadata["completion_status"] == "storage_error"
        if run_log_write_fails:
            assert "剩余空间低于安全余量" not in run_log
        else:
            assert "剩余空间低于安全余量" in run_log
        assert all(stream.closed for stream in (
            recorder._data_stream, recorder._event_stream, recorder._log_stream
        ))
    finally:
        worker.shutdown()


@pytest.mark.parametrize("failed_read", [False, True])
def test_unexpected_csv_append_exception_stops_and_finalizes_experiment(tmp_path, failed_read):
    settings = ModeSettings(Mode.DCV)
    scenario = MockScenario(
        name="csv append exception",
        read_latency_s=0,
        timeout_at_sample=1 if failed_read else None,
    )
    worker = MeasurementWorker()
    worker.connect_mock(settings, scenario)
    finished = []
    save_results = []
    errors = []
    worker.experiment_finished.connect(finished.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )
    worker.error.connect(errors.append)
    worker.start_experiment(settings, "CSV append exception", str(tmp_path), 10, False)
    recorder = worker._recorder
    assert recorder is not None

    def reject_append(*_args, **_kwargs):
        raise RuntimeError("simulated csv serializer failure")

    recorder.append_reading = reject_append
    worker._take_sample()

    assert worker.state is AcquisitionState.STOPPED
    assert worker._recorder is None
    assert recorder._data_stream.closed
    assert len(finished) == 1
    assert len(save_results) == 1
    assert save_results[0][1] is False
    assert "不完整" in save_results[0][2]
    assert errors and "simulated csv serializer failure" in errors[0]
    metadata = json.loads((Path(finished[0]) / "config.json").read_text(encoding="utf-8"))
    assert metadata["completion_status"] == "storage_error"
    worker.shutdown()


@pytest.mark.parametrize("failing_sync", [1, 2])
def test_csv_fsync_failure_stops_worker_and_marks_experiment_incomplete(
    tmp_path, monkeypatch, failing_sync
):
    import fluke8845a.storage as storage_module

    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="fsync failure", read_latency_s=0))
    finished = []
    save_results = []
    statuses = []
    errors = []
    worker.experiment_finished.connect(finished.append)
    worker.experiment_save_result.connect(
        lambda directory, complete, detail: save_results.append((directory, complete, detail))
    )
    worker.status_changed.connect(statuses.append)
    worker.error.connect(errors.append)
    monkeypatch.setattr(storage_module, "CSV_FSYNC_INTERVAL_NS", 0)
    worker.start_experiment(settings, "CSV fsync failure", str(tmp_path), 3600, False)
    recorder = worker._recorder
    assert recorder is not None
    worker._timer.stop()

    real_fsync = storage_module.os.fsync
    calls = 0

    def fail_first_sync(fd):
        nonlocal calls
        calls += 1
        if calls == failing_sync:
            raise OSError("simulated CSV fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(storage_module.os, "fsync", fail_first_sync)
    worker._take_sample()

    assert worker.state is AcquisitionState.STOPPED
    assert worker._recorder is None
    assert not worker._timer.isActive()
    assert len(finished) == 1
    assert len(save_results) == 1
    assert save_results[0][1] is False
    assert "不完整" in save_results[0][2]
    assert statuses[-1].startswith("实验已停止；数据记录可能不完整")
    assert errors and "simulated CSV fsync failure" in errors[-1]
    assert all(stream.closed for stream in (recorder._data_stream, recorder._event_stream, recorder._log_stream))

    directory = Path(finished[0])
    with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["raw_reading"]
    assert metadata["completion_status"] == "storage_error"
    assert metadata["sample_count"] == 1
    worker.shutdown()


def test_current_overrange_stops_continuous_capture_without_another_read(tmp_path, monkeypatch):
    from fluke8845a.scpi import ReadingError

    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCI)
    worker.connect_mock(settings, MockScenario(name="current overrange", read_latency_s=0))
    read_attempts = 0

    def overrange_read():
        nonlocal read_attempts
        read_attempts += 1
        raise ReadingError("仪器返回无效读数：OL", "OL", status="overflow")

    monkeypatch.setattr(worker.instrument, "read", overrange_read)
    statuses = []
    worker.status_changed.connect(statuses.append)
    worker.start_experiment(settings, "current overrange", str(tmp_path), 1, False)

    worker._take_sample()

    assert read_attempts == 1
    assert worker.state == AcquisitionState.STOPPED
    assert worker._recorder is None
    assert any("过量程" in status and "已停止" in status for status in statuses)
    worker.shutdown()


def test_worker_experiment_config_distinguishes_front_panel_software_from_idn(tmp_path):
    worker = MeasurementWorker()
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54", terminals="FRONT")
    instrument = Serial8845A(
        "COM3", SerialSettings(), transport_factory=lambda *_: transport
    )
    worker.instrument = instrument
    worker.info = instrument.connect()
    worker.control_mode = "computer"
    worker.set_firmware_versions("2.10.882.24", "ad300210")

    settings = ModeSettings(Mode.DCI, autorange=False, range_si=0.1)
    worker.start_experiment(
        settings, "front panel metadata", str(tmp_path), 60, False,
        confirmed_settings=settings,
    )
    directory = worker._recorder.directory
    worker.shutdown()

    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    device = metadata["device"]
    assert device["idn_version_candidate"] is None
    assert device["idn_version_candidate_source"] is None
    assert device["idn_revision"] == "02/14/08-08:54"
    assert device["front_panel_software"] == {
        "outguard_sw": "2.10.882.24", "inguard_sw": "ad300210",
    }
    assert device["range_firmware"] == "2.10.882.24"
    assert device["range_firmware_source"] == "面板核对的 OutG SW"
    assert metadata["simulation"] is False
    assert metadata["software_version"] == __version__
    assert metadata["connection"]["port"] == "COM3"
    assert metadata["connection"]["serial_settings"] == {
        "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
        "flow_control": "none", "eol": "CRLF", "timeout_s": 2.0,
    }
    assert metadata["actual_device_configuration"]["function"] == "CURR:DC"
    assert metadata["actual_device_configuration"]["range_response"] == "0.1"
    assert metadata["mode"] == Mode.DCI.value
    assert metadata["mode_settings"]["range_si"] == 0.1


def test_worker_ignores_identical_firmware_profile_update_during_acquisition(monkeypatch):
    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    worker.set_firmware_versions("2.10.882.24", "ad300210")
    worker._machine.start()

    worker.set_firmware_versions("2.10.882.24", "ad300210")

    assert errors == []
    assert worker.instrument.outguard_version == "2.10.882.24"
    assert worker.instrument.inguard_version == "ad300210"
    worker._machine.stop()
    worker.shutdown()


def test_worker_still_rejects_firmware_profile_change_during_acquisition(monkeypatch):
    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    worker.set_firmware_versions("2.10.882.24", "ad300210")
    worker._machine.start()

    worker.set_firmware_versions("1.9.0.0", "legacy")

    assert errors and "采集中不能修改" in errors[-1]
    assert worker.instrument.outguard_version == "2.10.882.24"
    assert worker.instrument.inguard_version == "ad300210"
    worker._machine.stop()
    worker.shutdown()


@pytest.mark.parametrize("mode", list(Mode))
def test_worker_one_shot_records_each_mock_mode_with_correct_unit(tmp_path, mode):
    worker = MeasurementWorker()
    rows_seen = []
    finished = []
    settings = ModeSettings(mode)
    scenario = MockScenario(noise_fraction=0, drift_fraction_per_minute=0,
                            step_at_s=1e9, recovery_at_s=None, read_latency_s=0)
    worker.sample_ready.connect(lambda: rows_seen.extend(worker.take_sample_update()[0]))
    worker.experiment_finished.connect(finished.append)
    worker.connect_mock(settings, scenario)

    try:
        worker.start_experiment(settings, f"{mode.value} mock", str(tmp_path), 0, True)
        assert worker.state is AcquisitionState.STOPPED
        assert len(finished) == 1
        assert len(rows_seen) == 1
        row = rows_seen[0]
        assert row["status"] == "ok"
        assert row["mode"] == mode.value
        assert row["unit_si"] == descriptor(mode).unit_si
        assert row["quantity"] == descriptor(mode).quantity
        assert row["value_si"] != ""

        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            saved = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(saved) == 1
        assert saved[0]["status"] == "ok"
        assert saved[0]["raw_reading"]
        assert saved[0]["unit_si"] == descriptor(mode).unit_si
        assert metadata["simulation"] is True
        assert metadata["mode"] == mode.value
    finally:
        worker.shutdown()


@pytest.mark.parametrize("mode", list(Mode))
def test_worker_continuous_mock_acquisition_persists_all_six_modes(tmp_path, mode):
    worker = MeasurementWorker()
    settings = ModeSettings(mode)
    scenario = MockScenario(
        name=f"continuous {mode.value}",
        noise_fraction=1e-5,
        drift_fraction_per_minute=1e-4,
        step_at_s=0.02,
        recovery_at_s=0.06,
        read_latency_s=0.002,
    )
    finished = []
    rows_seen = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.sample_ready.connect(lambda: rows_seen.extend(worker.take_sample_update()[0]))
    worker.connect_mock(settings, scenario)

    try:
        worker.start_experiment(settings, f"continuous {mode.value}", str(tmp_path), 0.005, False)
        QTimer.singleShot(120, worker.stop)
        loop.exec()

        assert worker.state is AcquisitionState.STOPPED
        assert len(finished) == 1
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))

        assert len(rows) >= 3
        assert len(rows_seen) == len(rows)
        assert [int(row["sample_index"]) for row in rows] == list(range(1, len(rows) + 1))
        assert all(row["status"] == "ok" and row["raw_reading"] for row in rows)
        assert all(row["mode"] == mode.value for row in rows)
        assert all(row["quantity"] == descriptor(mode).quantity for row in rows)
        assert all(row["unit_si"] == descriptor(mode).unit_si for row in rows)
        assert all(row["value_si"] for row in rows)
        assert all(
            float(rows[index]["elapsed_s"]) >= float(rows[index - 1]["elapsed_s"])
            for index in range(1, len(rows))
        )
        assert metadata["simulation"] is True
        assert metadata["mock_scenario"]["name"] == scenario.name
        assert metadata["completion_status"] == "stopped"
        assert (directory / "events.csv").is_file()
        assert (directory / "run.log").is_file()
    finally:
        worker.shutdown()


def test_worker_coalesces_notifications_and_bounds_gui_mailbox():
    worker = MeasurementWorker()
    notifications = []
    worker.sample_ready.connect(lambda: notifications.append("ready"))
    worker._running_stats.add(42.0)

    for start in (0, 5_000, 10_000):
        worker._pending_rows.extend(
            {"sample_index": index} for index in range(start, start + 5_000)
        )
        worker._flush_ui_rows()

    assert notifications == ["ready"]
    assert len(worker._ui_pending_rows) == MAX_PENDING_GUI_ROWS
    assert worker._ui_notification_pending is True

    rows, rate, stats = worker.take_sample_update()
    assert [row["sample_index"] for row in rows] == list(range(5_000, 15_000))
    assert rate == 0.0
    assert stats["count"] == 1
    assert worker._ui_notification_pending is False

    worker._pending_rows.append({"sample_index": 15_000})
    worker._flush_ui_rows()
    assert notifications == ["ready", "ready"]
    worker.take_sample_update()
    worker.shutdown()


def test_take_sample_update_reports_attempt_counts_and_recent_valid_intervals(tmp_path, monkeypatch):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="telemetry", noise_fraction=0, drift_fraction_per_minute=0,
                     step_at_s=1e9, recovery_at_s=None, read_latency_s=0),
    )
    original_read = worker.instrument.read
    attempt = 0
    valid_index = 0
    valid_offsets_ns = (0, 100_000_000, 400_000_000, 450_000_000)
    request_offsets_ns = (-100_000_000, -50_000_000, -10_000_000, 440_000_000)
    base_ns = time.monotonic_ns() + 2_000_000_000

    def read_with_one_timeout():
        nonlocal attempt, valid_index
        attempt += 1
        if attempt == 2:
            raise TimeoutError("injected Mock timeout")
        reading = original_read()
        received_ns = base_ns + valid_offsets_ns[valid_index]
        started_ns = base_ns + request_offsets_ns[valid_index]
        valid_index += 1
        return replace(
            reading,
            started_monotonic_ns=started_ns,
            received_monotonic_ns=received_ns,
            read_response_end_local="",
            read_response_end_utc="",
        )

    monkeypatch.setattr(worker.instrument, "read", read_with_one_timeout)
    worker.start_experiment(settings, "telemetry counts", str(tmp_path), 60, False)

    try:
        worker._timer.stop()
        for _ in range(5):
            worker._take_sample()
            worker._timer.stop()
        worker.stop()
        rows, actual_hz, stats = worker.take_sample_update()

        assert [row["status"] for row in rows] == ["ok", "timeout", "ok", "ok", "ok"]
        assert stats["attempt_count"] == 5
        assert stats["valid_count"] == 4
        assert stats["abnormal_count"] == 1
        assert stats["interval_median_s"] == pytest.approx(0.1)
        assert stats["interval_min_s"] == pytest.approx(0.05)
        assert stats["interval_max_s"] == pytest.approx(0.3)
        assert stats["interval_range_jitter_s"] == pytest.approx(0.25)
        assert actual_hz == pytest.approx(3 / 0.45)
        valid_rows = [row for row in rows if row["status"] == "ok"]
        assert [row["actual_interval_s"] for row in valid_rows] == [
            "", "0.1", "0.3", "0.05",
        ]
        assert all(row["host_read_response_end_monotonic_ns"] for row in valid_rows)
        assert rows[1]["actual_interval_s"] == ""
    finally:
        worker.shutdown()


def test_take_sample_update_has_no_interval_metrics_for_one_valid_mock_reading(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="one valid point", noise_fraction=0, drift_fraction_per_minute=0,
                     step_at_s=1e9, recovery_at_s=None, read_latency_s=0),
    )
    updates = []
    worker.sample_ready.connect(lambda: updates.append(worker.take_sample_update()))

    try:
        worker.start_experiment(settings, "one valid point", str(tmp_path), 0, True)

        assert len(updates) == 1
        rows, actual_hz, stats = updates[0]
        assert len(rows) == 1 and rows[0]["status"] == "ok"
        assert stats["attempt_count"] == 1
        assert stats["valid_count"] == 1
        assert stats["abnormal_count"] == 0
        assert stats["interval_median_s"] is None
        assert stats["interval_min_s"] is None
        assert stats["interval_max_s"] is None
        assert stats["interval_range_jitter_s"] is None
        assert actual_hz == 0.0
    finally:
        worker.shutdown()


def test_real_connection_reads_current_mode_without_reconfiguring_instrument(monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    transport.force_function = "RES"
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    connection_info = []
    statuses = []
    worker.configuration_ready.connect(connection_info.append)
    worker.status_changed.connect(statuses.append)

    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))

    assert worker.instrument is not None and worker.instrument.connected
    assert "REMOTE" in statuses[-1]
    assert "除LOCAL外" in statuses[-1]
    assert worker.current_mode is Mode.RES2
    assert connection_info[0]["function"] == "RES"
    assert connection_info[0]["input_terminals"] == "FRONT"
    assert "SYST:REM" in transport.commands
    assert not any(command.startswith("CONF:") for command in transport.commands)
    assert "READ?" not in transport.commands
    worker.shutdown()
    assert "SYST:LOC" in transport.commands


def test_saved_pre_2_firmware_profile_is_loaded_before_initial_settings_readback(monkeypatch):
    import fluke8845a.worker as worker_module

    class LegacyFirmwareTransport(FakeTransport):
        def read_until(self, terminator, timeout_s=None):
            if self.last_command.upper().endswith(":FILT:DIG?"):
                raise AssertionError("known unsupported filter query must be skipped")
            return super().read_until(terminator, timeout_s)

    transport = LegacyFirmwareTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    active = []
    worker.active_settings_ready.connect(active.append)

    worker.connect_real_with_profiles(
        "COM3", SerialSettings(), ModeSettings(Mode.DCV), False,
        {"TEST8845A001": {"outguard_sw": "1.9.0.0", "inguard_sw": "legacy"}},
    )

    assert worker.instrument is not None and worker.instrument.outguard_version == "1.9.0.0"
    assert not any(command.endswith(":FILT:DIG?") or command.endswith(":FILT:DIG OFF") for command in transport.commands)
    assert active[-1]["digital_filter_state"] == "unsupported"
    assert worker.instrument.connected
    worker.shutdown()


def test_reapplying_saved_firmware_profile_preserves_filter_readback(monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    active = []
    worker.active_settings_ready.connect(active.append)
    firmware_profiles = {
        "TEST8845A001": {"outguard_sw": "2.10.882.24", "inguard_sw": "ad300210"},
    }

    worker.connect_real_with_profiles(
        "COM3", SerialSettings(), ModeSettings(Mode.DCV), False, firmware_profiles,
    )
    assert active[-1]["digital_filter_state"] == "readback_off"
    original_command_count = len(transport.commands)

    worker.set_firmware_versions("2.10.882.24", "ad300210")

    assert active[-1]["digital_filter_state"] == "readback_off"
    assert len(transport.commands) == original_command_count
    worker.shutdown()


def test_unsupported_firmware_transition_clears_stale_filter_readback(monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    transport.digital_filters["SENS:VOLT:DC"] = "1"
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    active = []
    worker.active_settings_ready.connect(active.append)
    firmware_profiles = {
        "TEST8845A001": {"outguard_sw": "2.10.882.24", "inguard_sw": "ad300210"},
    }
    worker.connect_real_with_profiles(
        "COM3", SerialSettings(), ModeSettings(Mode.DCV), False, firmware_profiles,
    )
    assert active[-1]["digital_filter_state"] == "readback_on"
    assert active[-1]["digital_filter"] is True
    original_command_count = len(transport.commands)

    worker.set_firmware_versions("1.9.0.0", "legacy")

    assert active[-1]["digital_filter_state"] == "unsupported"
    assert active[-1]["digital_filter"] is None
    assert len(transport.commands) == original_command_count
    worker.shutdown()


def test_manual_firmware_record_change_refreshes_filter_status_without_claiming_readback(monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    transport.digital_filters["SENS:VOLT:DC"] = "1"
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    active = []
    worker.active_settings_ready.connect(active.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    original_command_count = len(transport.commands)
    assert "SENS:VOLT:DC:FILT:DIG?" in transport.commands
    assert active[-1]["digital_filter"] is True
    assert active[-1]["digital_filter_state"] == "readback_on"

    worker.set_firmware_versions("1.9.0.0", "legacy")
    assert active[-1]["digital_filter_state"] == "unsupported"
    worker.set_firmware_versions("2.10.882.24", "ad300210")
    assert active[-1]["digital_filter_state"] == "unprobed_inactive"
    assert len(transport.commands) == original_command_count
    worker.shutdown()


@pytest.mark.parametrize("outguard_version", ["2.10.882.24", ""])
def test_confirmed_filter_readback_survives_recording_firmware_versions(
    monkeypatch, outguard_version,
):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    transport.digital_filters["SENS:VOLT:DC"] = "1"
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    active = []
    worker.active_settings_ready.connect(active.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    original_command_count = len(transport.commands)
    assert active[-1]["digital_filter_state"] == "readback_on"

    worker.set_firmware_versions(outguard_version, "ad300210")

    assert active[-1]["digital_filter_state"] == "readback_on"
    assert active[-1]["digital_filter"] is True
    assert len(transport.commands) == original_command_count
    worker.shutdown()


def test_idle_real_connection_watch_detects_serial_timeout_without_sampling(monkeypatch, qt_core_application):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    disconnects = []
    worker.connected_changed.connect(
        lambda connected, message: disconnects.append(message) if not connected else None
    )
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    assert worker.instrument is not None and worker.instrument.connected

    transport.timeouts.add("*IDN?")
    worker._connection_watch_timer.start(1)
    loop = QEventLoop()
    worker.connected_changed.connect(
        lambda connected, _message: loop.quit() if not connected else None
    )
    QTimer.singleShot(1000, loop.quit)
    loop.exec()

    assert disconnects
    assert "超时" in disconnects[-1] or "关闭" in disconnects[-1]
    assert worker.instrument is not None and not worker.instrument.connected
    assert not any(command == "READ?" for command in transport.commands)
    assert worker.control_mode == "disconnected"
    worker.shutdown()
    assert not worker._connection_watch_timer.isActive()


def test_connection_watch_interval_is_ten_seconds_and_mock_never_starts_it():
    worker = MeasurementWorker()
    assert worker._connection_watch_timer.interval() == CONNECTION_WATCH_INTERVAL_MS == 10_000

    worker.connect_mock(
        ModeSettings(Mode.DCV),
        MockScenario(name="watch disabled", read_latency_s=0),
    )
    assert worker.instrument is not None and worker.instrument.connected
    assert not worker._connection_watch_timer.isActive()

    worker._check_idle_connection()
    assert worker.instrument is not None and worker.instrument.connected
    assert not worker._connection_watch_timer.isActive()
    worker.shutdown()


def test_paused_experiment_finishes_once_when_idle_probe_times_out(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    finished = []
    worker.experiment_finished.connect(finished.append)
    settings = ModeSettings(Mode.DCV)
    worker.connect_real("COM3", SerialSettings(timeout_s=0.002), settings)
    worker.start_experiment(
        settings, "paused idle timeout", str(tmp_path), 30.0,
        confirmed_settings=settings,
    )

    # Invoke one sample deterministically without waiting on the GUI event loop.
    worker._take_sample()
    worker.pause()
    assert worker.state is AcquisitionState.PAUSED
    assert worker._recorder is not None
    read_count_before_probe = transport.commands.count("READ?")
    assert read_count_before_probe == 1

    transport.timeouts.add("*IDN?")
    worker._check_idle_connection()

    assert worker.state is AcquisitionState.STOPPED
    assert worker._recorder is None
    assert len(finished) == 1
    assert not worker._connection_watch_timer.isActive()
    assert worker.instrument is not None and not worker.instrument.connected
    assert transport.is_open is False
    assert transport.commands.count("READ?") == read_count_before_probe

    directory = Path(finished[0])
    with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["value_si"]
    assert metadata["completion_status"] == "scpi_timeout"
    worker.shutdown()


def test_local_computer_switch_blocks_writes_and_refreshes_front_panel_changes(monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    statuses = []
    control_results = []
    worker.error.connect(errors.append)
    worker.status_changed.connect(statuses.append)
    worker.control_mode_result.connect(lambda success, message: control_results.append((success, message)))
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    assert worker._connection_watch_timer.isActive()

    worker.set_control_mode("local")
    assert not worker._connection_watch_timer.isActive()
    assert worker.control_mode == "local"
    assert "LOCAL" in statuses[-1] and "已停用" in statuses[-1]
    assert "暂停" not in statuses[-1]
    assert transport.commands[-1] == "SYST:LOC"
    assert control_results[-1][0] is True
    assert "已发送 SYST:LOC" in control_results[-1][1]
    assert "请在仪表面板确认 LOCAL" in control_results[-1][1]
    assert "无法通过 SCPI 回读" in control_results[-1][1]
    before = list(transport.commands)
    worker.set_control_mode("local")
    assert transport.commands == before
    assert "软件仍记录为已发送 Local 请求" in control_results[-1][1]
    assert "仪表状态无法回读" in control_results[-1][1]
    assert "请核对面板" in control_results[-1][1]
    worker._check_idle_connection()
    assert transport.commands == before
    target = ModeSettings(Mode.DCI)
    worker.configure_mode(target, confirmed_settings=target)
    assert transport.commands == before
    assert "LOCAL" in errors[-1]

    transport.function = "CURR:DC"
    worker.set_control_mode("computer")
    assert worker.control_mode == "computer"
    assert worker.current_mode is Mode.DCI
    assert "SYST:REM" in transport.commands
    assert "SENS:FUNC?" in transport.commands
    worker.shutdown()


def test_computer_request_reasserts_remote_even_when_worker_cache_already_says_computer(monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    results = []
    worker.control_mode_result.connect(lambda success, message: results.append((success, message)))
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    transport.commands.clear()

    worker.set_control_mode("computer")

    assert transport.commands[:2] == ["SYST:REM", "*IDN?"]
    assert results[-1][0] is True
    assert worker.control_mode == "computer"
    worker.shutdown()


@pytest.mark.parametrize("reject_query_number", [1, 2])
def test_local_rejection_during_connection_readback_is_not_reported_as_connected(
    monkeypatch, reject_query_number
):
    import fluke8845a.worker as worker_module

    class LocalErrorTransport(FakeTransport):
        sens_func_queries = 0

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command == "SENS:FUNC?":
                self.sens_func_queries += 1
            if self.sens_func_queries == reject_query_number and self.last_command == "SENS:FUNC?":
                self.read_timeouts.append((self.last_command, timeout_s))
                return b'+550,"Command not allowed in local"' + terminator
            return super().read_until(terminator, timeout_s)

    transport = LocalErrorTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    connected = []
    statuses = []
    errors = []
    worker.connected_changed.connect(lambda value, label: connected.append((value, label)))
    worker.status_changed.connect(statuses.append)
    worker.error.connect(errors.append)

    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))

    assert connected and connected[-1][0] is False
    assert "LOCAL" in connected[-1][1]
    assert "LOCAL" in statuses[-1]
    assert worker.control_mode == "disconnected"
    assert not transport.is_open
    assert "SYST:LOC" not in transport.commands
    assert errors and "LOCAL本地控制" in errors[-1]
    worker.shutdown()


def test_local_rejection_during_computer_mode_settings_readback_closes_session(monkeypatch):
    import fluke8845a.worker as worker_module

    class LocalErrorTransport(FakeTransport):
        reject_second_function_query = False
        function_queries = 0

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command == "SENS:FUNC?":
                self.function_queries += 1
                if self.reject_second_function_query and self.function_queries == 2:
                    self.read_timeouts.append((self.last_command, timeout_s))
                    return b'+550,"Command not allowed in local"' + terminator
            return super().read_until(terminator, timeout_s)

    transport = LocalErrorTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    connected = []
    statuses = []
    worker.connected_changed.connect(lambda value, label: connected.append((value, label)))
    worker.status_changed.connect(statuses.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    assert connected[-1][0] is True
    transport.commands.clear()
    transport.function_queries = 0
    transport.reject_second_function_query = True

    worker.set_control_mode("computer")

    assert connected[-1][0] is False
    assert "LOCAL" in connected[-1][1]
    assert "LOCAL" in statuses[-1]
    assert worker.control_mode == "disconnected"
    assert not transport.is_open
    assert "SYST:LOC" not in transport.commands
    worker.shutdown()


def test_configure_mode_serial_write_failure_closes_port_and_reports_disconnected(monkeypatch):
    import fluke8845a.worker as worker_module

    class FailingWriteTransport(FakeTransport):
        fail_prefix = None

        def write(self, payload: bytes):
            command = payload.decode("ascii").rstrip("\r\n")
            if self.fail_prefix and command.startswith(self.fail_prefix):
                raise OSError("simulated serial write failure")
            return super().write(payload)

    transport = FailingWriteTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    connected = []
    errors = []
    worker.connected_changed.connect(lambda value, label: connected.append((value, label)))
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    assert connected[-1][0] is True

    transport.fail_prefix = "CONF:"
    target = ModeSettings(Mode.DCI)
    worker.configure_mode(target, confirmed_settings=target)

    assert not transport.is_open
    assert connected[-1][0] is False
    assert "重新连接" in connected[-1][1]
    assert worker.control_mode == "disconnected"
    assert errors and "simulated serial write failure" in errors[-1]
    worker.shutdown()


def test_live_mode_configuration_requires_mode_bound_wiring_confirmation(monkeypatch):
    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    transport.commands.clear()

    worker.configure_mode(ModeSettings(Mode.DCI))

    assert errors and "接线" in errors[-1]
    assert transport.commands == []
    assert worker.current_mode is Mode.DCV
    worker.shutdown()


def test_live_experiment_requires_matching_wiring_confirmation_before_io_or_storage(
    monkeypatch, tmp_path
):
    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    transport.commands.clear()
    settings = ModeSettings(Mode.DCI)

    worker.start_experiment(settings, "unconfirmed current run", str(tmp_path), 0.1)

    assert errors and "接线" in errors[-1]
    assert transport.commands == []
    assert worker.state is AcquisitionState.STOPPED
    assert not (tmp_path / "unconfirmed current run").exists()
    worker.shutdown()


@pytest.mark.parametrize("operation", ["configure", "start"])
def test_live_worker_rejects_wiring_confirmation_for_different_range(
    monkeypatch, tmp_path, operation,
):
    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    transport.commands.clear()
    target = ModeSettings(Mode.DCI, autorange=False, range_si=1e-3)
    confirmation = ModeSettings(Mode.DCI, autorange=False, range_si=100e-3)

    if operation == "configure":
        worker.configure_mode(target, confirmed_settings=confirmation)
    else:
        worker.start_experiment(
            target, "wrong range confirmation", str(tmp_path), 0.1,
            confirmed_settings=confirmation,
        )

    assert errors and "接线" in errors[-1]
    assert transport.commands == []
    assert worker.current_mode is Mode.DCV
    assert worker.state is AcquisitionState.STOPPED
    assert not (tmp_path / "wrong range confirmation").exists()
    worker.shutdown()


@pytest.mark.parametrize(
    "operation", ["configure", "start", "speed", "zero", "zero_readback", "diagnose", "computer"]
)
def test_local_mode_rejection_from_non_read_operation_marks_session_disconnected(
    tmp_path, monkeypatch, operation
):
    import fluke8845a.worker as worker_module

    class LocalErrorTransport(FakeTransport):
        local_error_commands = set()

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command in self.local_error_commands:
                self.local_error_commands.remove(self.last_command)
                self.read_timeouts.append((self.last_command, timeout_s))
                return b'+550,"Command not allowed in local"' + terminator
            return super().read_until(terminator, timeout_s)

    transport = LocalErrorTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    connected = []
    statuses = []
    errors = []
    zero_results = []
    worker.connected_changed.connect(lambda value, label: connected.append((value, label)))
    worker.status_changed.connect(statuses.append)
    worker.error.connect(errors.append)
    worker.zero_result.connect(zero_results.append)
    settings = ModeSettings(Mode.DCV)
    worker.connect_real("COM3", SerialSettings(), settings)
    assert connected[-1][0] is True
    commands_before = len(transport.commands)

    if operation == "diagnose":
        transport.local_error_commands.add("*IDN?")
        worker.diagnose()
    elif operation == "computer":
        transport.local_error_commands.add("*IDN?")
        worker.set_control_mode("computer")
    elif operation == "speed":
        worker.start_experiment(
            settings, "local speed rejection", str(tmp_path), 0.1,
            confirmed_settings=settings,
        )
        transport.local_error_commands.add("SYST:ERR?")
        worker.set_measurement_speed(replace(settings, nplc=0.2))
    else:
        transport.local_error_commands.add("SYST:ERR?")
        if operation == "configure":
            target = ModeSettings(Mode.DCI)
            worker.configure_mode(target, confirmed_settings=target)
        elif operation == "start":
            worker.start_experiment(
                settings, "local rejection", str(tmp_path), 0.1,
                confirmed_settings=settings,
            )
        elif operation in {"zero", "zero_readback"}:
            if operation == "zero_readback":
                transport.local_error_commands = {"SENS:FUNC?"}
            worker.zero()

    assert connected[-1][0] is False
    assert "LOCAL" in connected[-1][1]
    assert worker.control_mode == "disconnected"
    assert not transport.is_open
    assert "LOCAL" in statuses[-1]
    if operation == "zero_readback":
        assert zero_results and "回读失败" in zero_results[-1]
    else:
        assert errors and "LOCAL本地控制" in errors[-1]
    assert "SYST:LOC" not in transport.commands[commands_before:]
    worker.shutdown()


@pytest.mark.parametrize(("close_failures", "replacement_allowed"), [(2, True), (3, False)])
def test_reconnect_retries_old_transport_close_before_replacing_instrument(
    monkeypatch, close_failures, replacement_allowed
):
    import fluke8845a.worker as worker_module

    class FailingWriteTransport(FakeTransport):
        fail_prefix = None
        close_failures = 0

        def write(self, payload: bytes):
            command = payload.decode("ascii").rstrip("\r\n")
            if self.fail_prefix and command.startswith(self.fail_prefix):
                raise OSError("simulated serial write failure")
            return super().write(payload)

        def close(self):
            if self.close_failures:
                self.close_failures -= 1
                raise OSError("simulated serial close failure")
            return super().close()

    created_transports = []
    real_instrument_type = Serial8845A

    def make_instrument(port, settings, callback):
        if not created_transports:
            transport = FailingWriteTransport()
            transport.close_failures = close_failures
        else:
            transport = FakeTransport()
        created_transports.append(transport)
        return real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        )

    monkeypatch.setattr(worker_module, "Serial8845A", make_instrument)
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    old_instrument = worker.instrument
    old_transport = created_transports[0]
    old_transport.fail_prefix = "CONF:"

    target = ModeSettings(Mode.DCI)
    worker.configure_mode(target, confirmed_settings=target)
    assert old_transport.is_open
    assert worker.instrument is old_instrument

    worker.connect_real("COM4", SerialSettings(), ModeSettings(Mode.DCV))

    if replacement_allowed:
        assert len(created_transports) == 2
        assert not old_transport.is_open
        assert worker.instrument is not old_instrument
        assert worker.instrument is not None and worker.instrument.connected
    else:
        assert len(created_transports) == 1
        assert worker.instrument is old_instrument
        assert old_transport.is_open
        assert errors and "句柄未能释放" in errors[-1]
    worker.shutdown()


def test_front_panel_local_rejection_stops_without_fake_reading(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    transport.local_error_on_read = True
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    finished = []
    connected = []
    statuses = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.connected_changed.connect(lambda connected_now, label: connected.append((connected_now, label)))
    worker.status_changed.connect(statuses.append)

    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    settings = ModeSettings(Mode.DCV)
    worker.start_experiment(
        settings, "local interruption", str(tmp_path), 0.01,
        confirmed_settings=settings,
    )
    loop.exec()

    try:
        assert worker.state is AcquisitionState.STOPPED
        assert connected[-1][0] is False
        assert "LOCAL" in connected[-1][1]
        assert any("\u8bf7\u91cd\u65b0\u8fde\u63a5" in status for status in statuses)
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))

        assert len(rows) == 1
        assert rows[0]["status"] == "instrument_local"
        assert rows[0]["value_si"] == ""
        assert rows[0]["raw_reading"] == '+550,"Command not allowed in local"'
        assert metadata["completion_status"] == "instrument_local"
        assert worker.instrument is not None and not worker.instrument.connected
        assert not worker._connection_watch_timer.isActive()
        assert "SYST:LOC" not in transport.commands
    finally:
        worker.shutdown()


def test_scpi_read_timeout_is_saved_once_then_session_closes(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    finished = []
    connected = []
    statuses = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.connected_changed.connect(lambda connected_now, label: connected.append((connected_now, label)))
    worker.status_changed.connect(statuses.append)

    settings = ModeSettings(Mode.DCV)
    worker.connect_real("COM3", SerialSettings(), settings)
    transport.timeouts.add("READ?")
    worker.start_experiment(
        settings, "SCPI timeout safety", str(tmp_path), 0.01,
        confirmed_settings=settings,
    )
    loop.exec()

    try:
        assert worker.state is AcquisitionState.STOPPED
        assert [command for command in transport.commands if command == "READ?"] == ["READ?"]
        assert transport.commands[-1] == "READ?"
        assert "SYST:LOC" not in transport.commands
        assert transport.is_open is False
        assert connected[-1][0] is False
        assert statuses[-1].startswith("SCPI")
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["status"] == "timeout"
        assert rows[0]["value_si"] == ""
        assert rows[0]["raw_reading"] == "partial"
        assert rows[0]["timestamp_local"] == rows[0]["host_read_request_start_local"]
        assert rows[0]["timestamp_utc"] == rows[0]["host_read_request_start_utc"]
        assert int(rows[0]["host_read_response_end_monotonic_ns"]) >= int(
            rows[0]["host_read_request_start_monotonic_ns"]
        )
        assert rows[0]["host_read_response_end_local"]
        assert float(rows[0]["read_duration_s"]) >= 0
        assert rows[0]["actual_interval_s"] == ""
        assert metadata["completion_status"] == "scpi_timeout"
        assert worker.instrument is not None and not worker.instrument.connected
    finally:
        worker.shutdown()


def test_plain_read_timeout_stops_real_acquisition_after_one_failed_row(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    finished = []
    statuses = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.status_changed.connect(statuses.append)
    settings = ModeSettings(Mode.DCV)
    worker.connect_real("COM3", SerialSettings(), settings)
    calls = []

    def plain_timeout():
        calls.append(True)
        raise TimeoutError("plain read timeout")

    monkeypatch.setattr(worker.instrument, "read", plain_timeout)
    worker.start_experiment(
        settings, "plain read timeout", str(tmp_path), 0.01,
        confirmed_settings=settings,
    )
    QTimer.singleShot(200, loop.quit)
    loop.exec()

    try:
        assert len(calls) == 1
        assert worker.state is AcquisitionState.STOPPED
        assert worker.instrument is not None and not worker.instrument.connected
        assert transport.is_open is False
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["status"] == "timeout"
        assert rows[0]["value_si"] == ""
        assert "plain read timeout" in rows[0]["error"]
        assert metadata["completion_status"] == "read_timeout"
        assert any("重新连接" in status and "新实验" in status for status in statuses)
    finally:
        worker.shutdown()


def test_communication_disconnect_stops_and_saves_failure_without_retry(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    finished = []
    statuses = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.status_changed.connect(statuses.append)
    settings = ModeSettings(Mode.DCV)
    worker.connect_real("COM3", SerialSettings(), settings)
    calls = []

    def disconnected_read():
        calls.append(True)
        raise OSError("serial device disconnected")

    monkeypatch.setattr(worker.instrument, "read", disconnected_read)
    worker.start_experiment(
        settings, "read disconnect", str(tmp_path), 0.01,
        confirmed_settings=settings,
    )
    QTimer.singleShot(200, loop.quit)
    loop.exec()

    try:
        assert len(calls) == 1
        assert worker.state is AcquisitionState.STOPPED
        assert worker.instrument is not None and not worker.instrument.connected
        assert transport.is_open is False
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["status"] == "communication_error"
        assert rows[0]["value_si"] == ""
        assert "serial device disconnected" in rows[0]["error"]
        assert metadata["completion_status"] == "communication_error"
        assert any("重新连接" in status and "新实验" in status for status in statuses)
    finally:
        worker.shutdown()


def test_closed_transport_during_invalid_read_stops_connection_watch(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module
    from fluke8845a.scpi import ReadingError

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    finished = []
    worker.experiment_finished.connect(finished.append)
    settings = ModeSettings(Mode.DCV)
    worker.connect_real("COM3", SerialSettings(), settings)
    assert worker._connection_watch_timer.isActive()

    def invalid_read_after_transport_close():
        worker.instrument._client.close()
        raise ReadingError("malformed meter reading", raw="not-a-number")

    monkeypatch.setattr(worker.instrument, "read", invalid_read_after_transport_close)
    worker.start_experiment(
        settings, "closed transport", str(tmp_path), 0, True,
        confirmed_settings=settings,
    )

    assert worker.state is AcquisitionState.STOPPED
    assert len(finished) == 1
    assert not worker._connection_watch_timer.isActive()
    assert worker.instrument is not None and not worker.instrument.connected
    assert worker.control_mode == "disconnected"
    directory = Path(finished[0])
    with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["status"] == "invalid"
    assert rows[0]["value_si"] == ""
    assert metadata["completion_status"] == "disconnected"
    worker.shutdown()


def test_rs232_command_echo_closes_session_without_sending_more_scpi(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    connection_changes = []
    statuses = []
    worker.connected_changed.connect(lambda connected, label: connection_changes.append((connected, label)))
    worker.status_changed.connect(statuses.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))
    assert worker._connection_watch_timer.isActive()

    read_until = transport.read_until

    def echo_read_command(terminator, timeout_s=None):
        if transport.last_command == "READ?":
            return b"READ?\r\n"
        return read_until(terminator, timeout_s)

    monkeypatch.setattr(transport, "read_until", echo_read_command)
    settings = ModeSettings(Mode.DCV)
    worker.start_experiment(
        settings, "RS-232 terminal mode", str(tmp_path), 0, True,
        confirmed_settings=settings,
    )

    assert worker.instrument is not None and not worker.instrument.connected
    assert transport.is_open is False
    assert connection_changes[-1][0] is False
    assert "Computer" in connection_changes[-1][1]
    assert statuses[-1].startswith("RS-232")
    assert "Computer" in statuses[-1]
    assert transport.commands[-1] == "READ?"
    assert "SYST:LOC" not in transport.commands
    assert not worker._connection_watch_timer.isActive()

    worker.shutdown()


def test_live_sampling_interval_changes_apply_and_are_recorded(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="live interval", read_latency_s=0, step_at_s=1e9, recovery_at_s=None),
    )
    finished = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))

    QTimer.singleShot(40, lambda: worker.set_target_interval(None))
    QTimer.singleShot(100, lambda: worker.set_target_interval(0.05))
    QTimer.singleShot(350, worker.stop)
    worker.start_experiment(settings, "live interval", str(tmp_path), 10.0, False)
    loop.exec()

    try:
        assert worker.state is AcquisitionState.STOPPED
        assert len(finished) == 1
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        with (directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            events = list(csv.DictReader(stream))
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))

        assert metadata["completion_status"] == "stopped"
        assert metadata["initial_target_interval_s"] == 10.0
        assert metadata["target_interval_s"] == 0.05
        assert [change["target_interval_s"] for change in metadata["target_interval_changes"]] == [None, 0.05]
        assert all(change["timestamp_local"] and change["timestamp_utc"] for change in metadata["target_interval_changes"])
        assert all(change["effective_sample_index"] >= 1 for change in metadata["target_interval_changes"])
        assert len(events) == 2
        assert "最快可用" in events[0]["event"]
        assert "0.05 秒" in events[1]["event"]
        assert len(rows) >= 2
        assert all(row["status"] == "ok" for row in rows)
        assert int(rows[-1]["sample_index"]) == len(rows)
    finally:
        worker.shutdown()


def test_live_sampling_interval_change_returns_effective_sample_and_host_time(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="interval response", read_latency_s=0, step_at_s=1e9, recovery_at_s=None),
    )
    results = []
    finished = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))

    def change_interval():
        results.append(worker.set_target_interval(0.05))
        QTimer.singleShot(180, worker.stop)

    QTimer.singleShot(40, change_interval)
    worker.start_experiment(settings, "interval response", str(tmp_path), 10.0, False)
    loop.exec()

    try:
        result = results[0]
        assert result["success"] is True
        assert result["changed"] is True
        assert result["no_op"] is False
        assert result["effective_sample_index"] == 2
        assert result["host_timestamp_local"]
        assert result["host_timestamp_utc"]
        assert datetime.fromisoformat(result["host_timestamp_local"]).tzinfo is not None
        assert datetime.fromisoformat(result["host_timestamp_utc"]).utcoffset().total_seconds() == 0
        assert result["elapsed_s"] >= 0
        assert f"从样本 {result['effective_sample_index']} 起生效" in result["message"]
        assert "主机时间" in result["message"] and "实验历时" in result["message"]
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        change = metadata["target_interval_changes"][0]
        assert result["effective_sample_index"] == change["effective_sample_index"]
        assert result["host_timestamp_local"] == change["timestamp_local"]
        assert result["elapsed_s"] == change["elapsed_s"]
        assert worker.set_target_interval(0.05)["no_op"] is True
    finally:
        worker.shutdown()


def test_target_sampling_interval_reports_noop_when_experiment_is_not_running():
    worker = MeasurementWorker()
    try:
        result = worker.set_target_interval(0.5)
        assert result["success"] is True
        assert result["changed"] is False
        assert result["no_op"] is True
        assert "未运行" in result["message"]
    finally:
        worker.shutdown()


def test_paused_interval_change_reports_first_resumed_measurement_attempt(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="paused interval", read_latency_s=0, step_at_s=1e9, recovery_at_s=None),
    )
    receipt = {}
    paused_count = {}
    finished = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))

    def pause_and_change_interval():
        worker.pause()
        paused_count["value"] = worker._recorder.sample_count
        receipt["value"] = worker.set_target_interval(0.5)
        QTimer.singleShot(80, worker.resume)
        QTimer.singleShot(750, worker.stop)

    QTimer.singleShot(60, pause_and_change_interval)
    worker.start_experiment(settings, "paused interval", str(tmp_path), 10.0, False)
    loop.exec()

    try:
        result = receipt["value"]
        expected_sample = paused_count["value"] + 1
        assert result["changed"] is True
        assert result["effective_sample_index"] == expected_sample
        assert f"从样本 {expected_sample} 起生效" in result["message"]
        assert "样本编号对应测量尝试，包含无效读数" in result["message"]
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        change = metadata["target_interval_changes"][0]
        assert change["effective_sample_index"] == expected_sample
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        assert int(rows[paused_count["value"]]["sample_index"]) == expected_sample
        assert rows[paused_count["value"]]["status"] == "ok"
        # The first resumed attempt is the first one governed by the new
        # interval; its interval is measured from the preceding attempt.
        assert float(rows[paused_count["value"]]["actual_interval_s"]) >= 0.45
    finally:
        worker.shutdown()


def test_fast_sampling_honors_thread_safe_stop_request_after_current_read(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.ACV)
    worker.connect_mock(
        settings,
        MockScenario(
            name="fast stop request",
            noise_fraction=0,
            drift_fraction_per_minute=0,
            step_at_s=1e9,
            recovery_at_s=None,
            read_latency_s=0.08,
        ),
    )
    finished = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.sample_ready.connect(lambda: worker.take_sample_update())

    try:
        worker.start_experiment(settings, "thread-safe stop", str(tmp_path), 0, False)
        stopper = Thread(target=lambda: (time.sleep(0.02), worker.request_stop()), daemon=True)
        stopper.start()
        QTimer.singleShot(1500, loop.quit)
        loop.exec()
        stopper.join(timeout=0.5)

        assert len(finished) == 1
        assert worker.state is AcquisitionState.STOPPED
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["raw_reading"]
        assert metadata["completion_status"] == "stopped"
    finally:
        worker.request_stop()
        if worker.state != AcquisitionState.STOPPED:
            worker.stop()
        worker.shutdown()


def test_manual_experiment_event_is_written_incrementally_with_clock_and_latest_reading(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="manual event", noise_fraction=0, drift_fraction_per_minute=0,
                     step_at_s=1e9, recovery_at_s=None, read_latency_s=0),
    )
    finished = []
    event_receipts = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.event_recorded.connect(event_receipts.append)

    QTimer.singleShot(80, lambda: worker.mark_event("接通负载"))
    QTimer.singleShot(240, worker.stop)
    worker.start_experiment(settings, "manual event", str(tmp_path), 0.01, False)
    loop.exec()

    try:
        assert len(finished) == 1
        directory = Path(finished[0])
        with (directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            events = list(csv.DictReader(stream))
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))

        assert len(events) == 1
        assert events[0]["event"] == "接通负载"
        assert events[0]["timestamp_local"] and events[0]["timestamp_utc"]
        assert float(events[0]["elapsed_s"]) >= 0
        assert float(events[0]["value_si"]) == float(rows[-1]["value_si"])
        assert len(event_receipts) == 1
        assert event_receipts[0]["event"] == "接通负载"
        assert event_receipts[0]["timestamp_local"] == events[0]["timestamp_local"]
        assert event_receipts[0]["sample_index"] == int(events[0]["sample_index"])
        assert float(event_receipts[0]["value_si"]) == float(events[0]["value_si"])
    finally:
        worker.shutdown()


def test_live_measurement_speed_change_is_logged_and_each_row_keeps_active_nplc(tmp_path):
    worker = MeasurementWorker()
    initial = ModeSettings(Mode.DCV)
    worker.connect_mock(
        initial,
        MockScenario(name="live speed", read_latency_s=0.005, step_at_s=1e9, recovery_at_s=None),
    )
    finished = []
    results = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.measurement_speed_result.connect(lambda success, message: results.append((success, message)))

    QTimer.singleShot(80, lambda: worker.set_measurement_speed(replace(initial, nplc=0.2)))
    QTimer.singleShot(300, worker.stop)
    worker.start_experiment(initial, "live speed", str(tmp_path), 0.01, False)
    loop.exec()

    try:
        assert worker.state is AcquisitionState.STOPPED
        assert len(finished) == 1
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        with (directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            events = list(csv.DictReader(stream))
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))

        assert results and results[0][0] is True
        assert metadata["mode_settings"]["nplc"] == 0.2
        assert len(metadata["measurement_speed_changes"]) == 1
        assert metadata["measurement_speed_changes"][0]["effective_sample_index"] >= 2
        assert metadata["measurement_speed_changes"][0]["timestamp_local"]
        assert metadata["measurement_speed_changes"][0]["timestamp_utc"]
        assert len(rows) >= metadata["measurement_speed_changes"][0]["effective_sample_index"]
        assert rows[0]["nplc"] == "1.0"
        assert any(row["nplc"] == "0.2" for row in rows)
        assert any("NPLC 1→0.2" in event["event"] for event in events)
    finally:
        worker.shutdown()


def test_rejected_live_speed_validation_keeps_acquisition_running(tmp_path):
    worker = MeasurementWorker()
    initial = ModeSettings(Mode.DCV, nplc=1)
    worker.connect_mock(
        initial,
        MockScenario(name="rejected live speed", read_latency_s=0.005, step_at_s=1e9, recovery_at_s=None),
    )

    def reject_slow_speed(_settings):
        raise UnsupportedRange("无法确认8845A的OutG SW版本")

    worker.instrument.set_measurement_speed = reject_slow_speed
    finished = []
    results = []
    states_after_rejection = []
    samples_at_rejection = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.measurement_speed_result.connect(lambda success, message: results.append((success, message)))

    def request_slow_speed():
        worker.set_measurement_speed(replace(initial, nplc=10))
        states_after_rejection.append(worker.state)
        samples_at_rejection.append(worker._recorder.sample_count)

    QTimer.singleShot(80, request_slow_speed)
    QTimer.singleShot(300, worker.stop)
    worker.start_experiment(initial, "rejected live speed", str(tmp_path), 0.01, False)
    loop.exec()

    try:
        assert states_after_rejection == [AcquisitionState.RUNNING]
        assert results and results[0][0] is False
        assert "OutG SW" in results[0][1]
        assert len(finished) == 1
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        assert metadata["completion_status"] == "stopped"
        assert len(rows) > samples_at_rejection[0]
    finally:
        worker.shutdown()


def test_live_speed_serial_failure_closes_connection_and_finalizes_run(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    transport = FakeTransport()
    original_write = transport.write

    def failing_write(payload):
        command = payload.decode("ascii").strip()
        if command == "SENS:VOLT:DC:NPLC 10":
            raise InstrumentError("串口写入失败：设备已拔出")
        original_write(payload)

    transport.write = failing_write
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, log_callback: Serial8845A(
            port, settings, log_callback, transport_factory=lambda *_: transport
        ),
    )

    worker = MeasurementWorker()
    initial = ModeSettings(Mode.DCV, nplc=1)
    worker.connect_real("COM3", SerialSettings(), initial)
    finished = []
    results = []
    connection_changes = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.measurement_speed_result.connect(lambda success, message: results.append((success, message)))
    worker.connected_changed.connect(lambda connected, message: connection_changes.append((connected, message)))

    QTimer.singleShot(80, lambda: worker.set_measurement_speed(replace(initial, nplc=10)))
    QTimer.singleShot(3000, loop.quit)
    worker.start_experiment(
        initial, "调速串口故障", str(tmp_path), 0.01, False,
        confirmed_settings=initial,
    )
    loop.exec()

    try:
        assert len(finished) == 1
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert metadata["completion_status"] == "speed_update_error"
        assert worker.instrument is not None and not worker.instrument.connected
        assert not transport.is_open
        assert results and results[0][0] is False
        assert any(not connected and "重新连接" in message for connected, message in connection_changes)
    finally:
        worker.shutdown()


def test_live_speed_rejects_digital_filter_without_stopping_acquisition(tmp_path):
    worker = MeasurementWorker()
    initial = ModeSettings(Mode.DCV, nplc=1)
    worker.connect_mock(
        initial,
        MockScenario(name="filter speed rejection", read_latency_s=0.005, step_at_s=1e9, recovery_at_s=None),
    )
    finished = []
    results = []
    states_after_rejection = []
    samples_at_rejection = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.measurement_speed_result.connect(lambda success, message: results.append((success, message)))

    def request_filter_speed():
        worker.set_measurement_speed(replace(initial, nplc=10, digital_filter=True))
        states_after_rejection.append(worker.state)
        samples_at_rejection.append(
            worker._recorder.sample_count if worker._recorder is not None else -1
        )

    QTimer.singleShot(80, request_filter_speed)
    QTimer.singleShot(300, worker.stop)
    QTimer.singleShot(3000, loop.quit)
    worker.start_experiment(initial, "filter speed rejection", str(tmp_path), 0.01, False)
    loop.exec()

    try:
        assert states_after_rejection == [AcquisitionState.RUNNING]
        assert results and results[0][0] is False
        assert "数字滤波" in results[0][1]
        assert len(finished) == 1
        directory = Path(finished[0])
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        assert metadata["completion_status"] == "stopped"
        assert len(rows) > samples_at_rejection[0]
    finally:
        worker.shutdown()


def test_overwrite_recovers_stale_run_under_name_lock_before_archiving(tmp_path):
    stale_directory = tmp_path / "20261019_120000_000001_recover_me"
    stale_directory.mkdir()
    measurement_bytes = b"sample_index,raw_reading,status\r\n1,1.2,ok\r\n2,partial"
    (stale_directory / "measurements.csv").write_bytes(measurement_bytes)
    (stale_directory / "config.json").write_text(
        json.dumps({
            "experiment_name": "recover me",
            "completion_status": "running",
            "experiment_name_lock_protocol": 1,
            "sample_count": 0,
            "simulation": True,
        }),
        encoding="utf-8",
    )

    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(settings, MockScenario(name="overwrite recovery", read_latency_s=0.005))
    finished = []
    errors = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))
    worker.error.connect(errors.append)
    QTimer.singleShot(80, worker.stop)
    QTimer.singleShot(3000, loop.quit)
    worker.start_experiment(
        settings,
        "recover me",
        str(tmp_path),
        0.01,
        False,
        "overwrite",
        str(stale_directory.resolve()),
    )
    loop.exec()

    try:
        assert not errors
        assert len(finished) == 1
        assert worker.last_experiment_backup_path is not None
        archived = worker.last_experiment_backup_path
        archived_metadata = json.loads((archived / "config.json").read_text(encoding="utf-8"))
        assert archived_metadata["completion_status"] == "interrupted"
        assert archived_metadata["recovery"]["sample_count_checkpoint"] == 0
        assert (archived / "measurements.csv").read_bytes() == measurement_bytes
        assert finished[0] != str(archived)
    finally:
        worker.shutdown()


def test_range_query_timeout_preserves_successful_read_text_in_failure_csv(tmp_path, monkeypatch):
    import fluke8845a.worker as worker_module

    class TimeoutAfterReadTransport(FakeTransport):
        saw_read = False

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command == "SENS:VOLT:DC:RANG?" and self.saw_read:
                self.read_timeouts.append((self.last_command, timeout_s))
                return b"range-partial"
            payload = super().read_until(terminator, timeout_s)
            if self.last_command == "READ?":
                self.saw_read = True
            return payload

    transport = TimeoutAfterReadTransport()
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, log_callback: Serial8845A(
            port, settings, log_callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV, nplc=1)
    worker.connect_real("COM3", SerialSettings(), settings)
    finished = []
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))

    QTimer.singleShot(3000, loop.quit)
    worker.start_experiment(
        settings, "量程回读超时", str(tmp_path), 0.01, False,
        confirmed_settings=settings,
    )
    loop.exec()

    try:
        assert len(finished) == 1
        with (Path(finished[0]) / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        assert rows and rows[0]["status"] == "timeout"
        assert rows[0]["raw_reading"] == "+1.2345678901234567E+00"
        assert rows[0]["value_si"] == ""
    finally:
        worker.shutdown()


def test_pause_resume_halts_scheduled_samples_and_records_both_transitions(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCV)
    worker.connect_mock(
        settings,
        MockScenario(name="pause resume", read_latency_s=0.001, step_at_s=1e9, recovery_at_s=None),
    )
    finished = []
    snapshots = {}
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))

    def pause_and_count():
        worker.pause()
        snapshots["paused_count"] = worker._recorder.sample_count
        snapshots["pause_monotonic_ns"] = time.monotonic_ns()

    def resume_and_compare():
        snapshots["still_paused_count"] = worker._recorder.sample_count
        worker.resume()
        snapshots["resume_monotonic_ns"] = time.monotonic_ns()

    QTimer.singleShot(75, pause_and_count)
    QTimer.singleShot(275, resume_and_compare)
    QTimer.singleShot(425, worker.stop)
    try:
        worker.start_experiment(settings, "pause resume", str(tmp_path), 0.01, False)
        loop.exec()

        assert len(finished) == 1
        assert snapshots["paused_count"] >= 2
        assert snapshots["still_paused_count"] == snapshots["paused_count"]
        directory = Path(finished[0])
        with (directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            event_rows = list(csv.DictReader(stream))
        events = [row["event"] for row in event_rows]
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        pause_event = next(row for row in event_rows if "暂停采集" in row["event"])
        resume_event = next(row for row in event_rows if "继续采集" in row["event"])
        assert any("暂停采集" in event for event in events)
        assert any("继续采集" in event for event in events)
        # Experiment elapsed time is monotonic wall time from experiment start.
        # The display freezes while paused, but elapsed time catches up on resume.
        paused_for_s = (snapshots["resume_monotonic_ns"] - snapshots["pause_monotonic_ns"]) / 1e9
        recorded_pause_s = float(resume_event["elapsed_s"]) - float(pause_event["elapsed_s"])
        assert recorded_pause_s >= paused_for_s - 0.03
        assert len(rows) > snapshots["paused_count"]
        pre_pause_rows = [row for row in rows if int(row["sample_index"]) <= snapshots["paused_count"]]
        resumed_rows = [row for row in rows if int(row["sample_index"]) > snapshots["still_paused_count"]]
        assert pre_pause_rows and resumed_rows
        sample_gap_s = float(resumed_rows[0]["elapsed_s"]) - float(pre_pause_rows[-1]["elapsed_s"])
        assert sample_gap_s >= paused_for_s - 0.05
        assert all(row["status"] == "ok" for row in rows)
    finally:
        worker.shutdown()


def test_mock_connection_reports_each_real_stage_without_claiming_hardware_identity():
    worker = MeasurementWorker()
    stages = []
    worker.connection_progress.connect(stages.append)
    scenario = MockScenario(name="长期漂移")
    try:
        worker.connect_mock(ModeSettings(Mode.DCV), scenario)
        assert stages[0] == "正在启动模拟器…"
        assert "长期漂移" in stages[1] and "模拟" in stages[1]
        assert stages[-1] == "模拟配置已准备，模拟器已就绪。"
        assert all("Fluke 8845A" not in stage for stage in stages)
    finally:
        worker.shutdown()


def test_mock_connection_exposes_named_progress_stages_with_truthful_skipped_hardware_steps():
    worker = MeasurementWorker()
    stages = []
    worker.connection_stage.connect(lambda stage, status, message: stages.append((stage, status, message)))
    try:
        worker.connect_mock(ModeSettings(Mode.DCV), MockScenario(name="阶段验证"))

        assert [(stage, status) for stage, status, _message in stages] == [
            ("device", "running"),
            ("device", "success"),
            ("port", "skipped"),
            ("identify", "skipped"),
            ("configuration", "running"),
            ("configuration", "success"),
            ("ready", "running"),
            ("ready", "success"),
        ]
        assert "真实仪器" in stages[3][2]
        assert "Fluke 8845A" not in " ".join(message for _stage, _status, message in stages)
    finally:
        worker.shutdown()


def test_mock_configuration_failure_is_reported_at_configuration_stage(monkeypatch):
    worker = MeasurementWorker()
    stages = []
    worker.connection_stage.connect(lambda stage, status, message: stages.append((stage, status, message)))
    monkeypatch.setattr(Mock8845A, "configure", lambda *_args: (_ for _ in ()).throw(RuntimeError("mock config failure")))
    try:
        worker.connect_mock(ModeSettings(Mode.DCV), MockScenario(name="配置失败"))

        assert ("configuration", "running") in [(stage, status) for stage, status, _message in stages]
        assert stages[-1][0:2] == ("configuration", "failure")
        assert not any(stage == "ready" and status == "success" for stage, status, _message in stages)
        assert worker.instrument is None
    finally:
        worker.shutdown()
