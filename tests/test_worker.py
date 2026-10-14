from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from fluke8845a.modes import Mode, descriptor
from fluke8845a.mock import MockScenario
from fluke8845a.scpi import ModeSettings, Serial8845A, SerialSettings
from fluke8845a.state import AcquisitionState
from fluke8845a.worker import MAX_PENDING_GUI_ROWS, MeasurementWorker
from tests.fakes import FakeTransport


@pytest.fixture(scope="module", autouse=True)
def qt_core_application():
    return QCoreApplication.instance() or QCoreApplication([])


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
    worker.configuration_ready.connect(connection_info.append)

    worker.connect_real("COM3", SerialSettings(), ModeSettings(Mode.DCV))

    assert worker.instrument is not None and worker.instrument.connected
    assert worker.current_mode is Mode.RES2
    assert connection_info[0]["function"] == "RES"
    assert connection_info[0]["input_terminals"] == "FRONT"
    assert not any(command.startswith("CONF:") for command in transport.commands)
    assert "READ?" not in transport.commands
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
        assert any("重新连接" in status for status in statuses)
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
        assert len(events) == 2
        assert "最快可用" in events[0]["event"]
        assert "0.05 秒" in events[1]["event"]
        assert len(rows) >= 2
        assert all(row["status"] == "ok" for row in rows)
        assert int(rows[-1]["sample_index"]) == len(rows)
    finally:
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
    loop = QEventLoop()
    worker.experiment_finished.connect(lambda directory: (finished.append(directory), loop.quit()))

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
        assert len(rows) >= metadata["measurement_speed_changes"][0]["effective_sample_index"]
        assert rows[0]["nplc"] == "1.0"
        assert any(row["nplc"] == "0.2" for row in rows)
        assert any("NPLC 1→0.2" in event["event"] for event in events)
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

    def resume_and_compare():
        snapshots["still_paused_count"] = worker._recorder.sample_count
        worker.resume()

    QTimer.singleShot(75, pause_and_count)
    QTimer.singleShot(150, resume_and_compare)
    QTimer.singleShot(300, worker.stop)
    try:
        worker.start_experiment(settings, "pause resume", str(tmp_path), 0.01, False)
        loop.exec()

        assert len(finished) == 1
        assert snapshots["paused_count"] >= 2
        assert snapshots["still_paused_count"] == snapshots["paused_count"]
        directory = Path(finished[0])
        with (directory / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            events = [row["event"] for row in csv.DictReader(stream)]
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        assert any("暂停采集" in event for event in events)
        assert any("继续采集" in event for event in events)
        assert len(rows) > snapshots["paused_count"]
        assert all(row["status"] == "ok" for row in rows)
    finally:
        worker.shutdown()
