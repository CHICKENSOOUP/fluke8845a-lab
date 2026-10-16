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
from fluke8845a.scpi import ModeSettings, Serial8845A, SerialSettings, UnsupportedRange
from fluke8845a.state import AcquisitionState
from fluke8845a.worker import (
    MAX_PENDING_GUI_ROWS, MAX_PENDING_GUI_SCPI_LOG_LINES, MeasurementWorker,
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

    assert results and "命令已发送" in results[-1]
    assert "模式 DCV" in results[-1] and "10 NPLC" in results[-1]
    assert "本软件没有零点值查询项" in results[-1]
    assert active_settings[-1]["range_si"] == 1.0
    worker.shutdown()


@pytest.mark.parametrize(
    ("firmware", "expected_state"),
    [
        ("", "unprobed_inactive"),
        ("1.9.0.0", "unprobed_inactive"),
        ("2.10.882.24", "readback_off"),
    ],
)
def test_active_settings_preserve_digital_filter_readback_state(firmware, expected_state):
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

    assert active[-1]["digital_filter"] is False
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
    transport = FakeTransport("FLUKE,8845A,9383035,02/14/08-08:54", terminals="FRONT")
    instrument = Serial8845A(
        "COM3", SerialSettings(), transport_factory=lambda *_: transport
    )
    worker.instrument = instrument
    worker.info = instrument.connect()
    worker.control_mode = "computer"
    worker.set_firmware_versions("2.10.882.24", "ad300210")

    settings = ModeSettings(Mode.DCI, autorange=False, range_si=0.1)
    worker.start_experiment(settings, "front panel metadata", str(tmp_path), 60, False)
    directory = worker._recorder.directory
    worker.shutdown()

    metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    device = metadata["device"]
    assert device["firmware"] is None
    assert device["firmware_source"] is None
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
    base_ns = time.monotonic_ns() + 2_000_000_000

    def read_with_one_timeout():
        nonlocal attempt, valid_index
        attempt += 1
        if attempt == 2:
            raise TimeoutError("injected Mock timeout")
        reading = original_read()
        received_ns = base_ns + valid_offsets_ns[valid_index]
        valid_index += 1
        return replace(
            reading,
            started_monotonic_ns=received_ns - 1_000_000,
            received_monotonic_ns=received_ns,
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
    worker.error.connect(errors.append)
    worker.status_changed.connect(statuses.append)
    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))

    worker.set_control_mode("local")
    assert worker.control_mode == "local"
    assert "LOCAL" in statuses[-1] and "已停用" in statuses[-1]
    assert "暂停" not in statuses[-1]
    assert transport.commands[-1] == "SYST:LOC"
    before = list(transport.commands)
    worker.configure_mode(ModeSettings(Mode.DCI))
    assert transport.commands == before
    assert "LOCAL" in errors[-1]

    transport.function = "CURR:DC"
    worker.set_control_mode("computer")
    assert worker.control_mode == "computer"
    assert worker.current_mode is Mode.DCI
    assert "SYST:REM" in transport.commands
    assert "SENS:FUNC?" in transport.commands
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
    worker.start_experiment(ModeSettings(Mode.DCV), "local interruption", str(tmp_path), 0.01)
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
    worker.start_experiment(settings, "SCPI timeout safety", str(tmp_path), 0.01)
    loop.exec()

    try:
        assert worker.state is AcquisitionState.STOPPED
        assert [command for command in transport.commands if command == "READ?"] == ["READ?"]
        assert transport.commands[-1] == "SYST:LOC"
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
        assert metadata["completion_status"] == "scpi_timeout"
        assert worker.instrument is not None and not worker.instrument.connected
    finally:
        worker.shutdown()


def test_rs232_command_echo_closes_session_without_sending_more_scpi(tmp_path, monkeypatch):
    transport = FakeTransport()
    worker = MeasurementWorker()
    worker.instrument = Serial8845A(
        "COM3", SerialSettings(), transport_factory=lambda *_: transport
    )
    worker.info = worker.instrument.connect()
    worker.control_mode = "computer"
    connection_changes = []
    statuses = []
    worker.connected_changed.connect(lambda connected, label: connection_changes.append((connected, label)))
    worker.status_changed.connect(statuses.append)

    read_until = transport.read_until

    def echo_read_command(terminator, timeout_s=None):
        if transport.last_command == "READ?":
            return b"READ?\r\n"
        return read_until(terminator, timeout_s)

    monkeypatch.setattr(transport, "read_until", echo_read_command)
    settings = ModeSettings(Mode.DCV)
    worker.start_experiment(settings, "RS-232 terminal mode", str(tmp_path), 0, True)

    assert worker.instrument is not None and not worker.instrument.connected
    assert transport.is_open is False
    assert connection_changes[-1][0] is False
    assert "Computer" in connection_changes[-1][1]
    assert statuses[-1].startswith("RS-232")
    assert "Computer" in statuses[-1]
    assert transport.commands[-1] == "READ?"
    assert "SYST:LOC" not in transport.commands

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
