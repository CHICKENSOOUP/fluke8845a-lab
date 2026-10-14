from __future__ import annotations

import os
import csv
import json
import time
from threading import Event
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QFontInfo, QImage
from PySide6.QtWidgets import QApplication, QDialog, QFrame

from fluke8845a import gui as gui_module
from fluke8845a.gui import MainWindow
from fluke8845a.mock import MockScenario
from fluke8845a.modes import DESCRIPTORS, Mode
from fluke8845a.scpi import ModeSettings
from fluke8845a.worker import MAX_PENDING_GUI_ROWS, MAX_PENDING_GUI_SCPI_LOG_LINES


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def test_gui_uses_separate_setup_tabs_and_keeps_the_plot_workspace_clear(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert [window.setup_tabs.tabText(i) for i in range(window.setup_tabs.count())] == ["仪器", "实验"]
        assert window.log_dock.isHidden()
        assert window.port_combo.minimumContentsLength() == 14
        assert window.nplc_combo.isEnabled()
        assert not window.bandwidth_combo.isEnabled()
        assert window.findChild(QFrame, "readingCard")
        assert window.findChild(QFrame, "plotToolbar")
        assert window.brand_icon.pixmap() is not None
        assert window.latest_value_label.text() == "等待读数"
        assert "DCV" in window.reading_mode_label.text()
        assert not window.plot_empty_overlay.isHidden()

        window._on_status("连续采集中 · 目标间隔 0 s")
        assert window.experiment_status.text() == "连续采集中"
        assert window.experiment_status.toolTip() == "连续采集中 · 目标间隔 0 s"
    finally:
        window.close()
        qt_app.processEvents()


def test_minimum_window_size_does_not_clip_the_readout_or_analysis(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.resize(1040, 700)
        window.show()
        qt_app.processEvents()
        qt_app.processEvents()

        assert window._reading_layout_mode == "stacked"
        assert window.measurement_workspace.height() >= window.measurement_workspace.minimumSizeHint().height()
        assert window.latest_value_label.height() >= window.latest_value_label.minimumSizeHint().height()
        assert not window.reading_mode_label.geometry().intersects(window.latest_value_label.geometry())
        assert window.plot_widget.height() >= window.plot_widget.minimumHeight()
        assert window.analysis_tabs.height() >= window.analysis_tabs.minimumHeight()
        panel_items = [
            window._chart_panel_layout.itemAt(index).widget().geometry()
            for index in range(window._chart_panel_layout.count())
        ]
        assert all(
            current.y() + current.height() <= following.y()
            for current, following in zip(panel_items, panel_items[1:])
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_long_run_plot_cache_is_bounded_without_truncating_global_stats(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    total_samples = gui_module.MAX_PLOT_POINTS + MAX_PENDING_GUI_ROWS
    try:
        for start in range(0, total_samples, MAX_PENDING_GUI_ROWS):
            stop = min(total_samples, start + MAX_PENDING_GUI_ROWS)
            rows = [
                {
                    "sample_index": index + 1,
                    "status": "ok",
                    "value_si": 1.0 + index / 1_000_000,
                    "elapsed_s": float(index + 1),
                    "mode": Mode.DCV.value,
                    "error": "",
                }
                for index in range(start, stop)
            ]
            window._on_sample(rows, 0.0, {
                "count": stop,
                "minimum": 1.0,
                "maximum": 1.0 + (stop - 1) / 1_000_000,
                "mean": 1.0 + (stop - 1) / 2_000_000,
                "sample_stddev": ((stop * (stop + 1) / 12) ** 0.5) / 1_000_000,
            })

        assert len(window._plot_x) == gui_module.MAX_PLOT_POINTS
        assert len(window._plot_y) == gui_module.MAX_PLOT_POINTS
        assert window._plot_x[0] == total_samples - gui_module.MAX_PLOT_POINTS + 1
        assert window._plot_x[-1] == total_samples
        assert window._global_stats["count"] == total_samples
        assert f"全程有效点 n={total_samples}" in window.stats_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_help_menu_exposes_qt_license_notice(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        help_menu = next(
            action.menu()
            for action in window.menuBar().actions()
            if action.text() == "帮助"
        )
        assert any(action.text() == "Qt 与第三方许可…" for action in help_menu.actions())
        assert gui_module.MainWindow._find_document("qt-licensing-and-third-party-notices.md").is_file()
    finally:
        window.close()
        qt_app.processEvents()


def test_scpi_log_batch_shows_original_times_and_reports_dropped_backlog(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        with window._worker._ui_mailbox_lock:
            window._worker._ui_pending_scpi_logs.extend((
                ("12:00:00.001", "> READ?"),
                ("12:00:00.002", "< 1.000000E+00"),
            ))
            window._worker._ui_dropped_scpi_logs = MAX_PENDING_GUI_SCPI_LOG_LINES + 4
            window._worker._ui_scpi_log_notification_pending = True

        window._on_scpi_logs_ready()

        displayed = window.log_text.toPlainText()
        assert "12:00:00.001  > READ?" in displayed
        assert "12:00:00.002  < 1.000000E+00" in displayed
        assert "5004 行" in displayed
        assert window._worker.take_scpi_log_update() == ([], 0)
    finally:
        window.close()
        qt_app.processEvents()


def test_window_close_waits_for_worker_thread_to_finish_cleanup(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        assert not window.close()
        assert window.isVisible()
        assert window._shutdown_started
        assert not window.isEnabled()

        deadline = time.monotonic() + 2.0
        while window.isVisible() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.01)

        assert window._shutdown_finished
        assert not window._worker_thread.isRunning()
        assert not window.isVisible()
    finally:
        if window._worker_thread.isRunning():
            window._worker.request_stop()
            window._bridge.close_app.emit()
            deadline = time.monotonic() + 2.0
            while window._worker_thread.isRunning() and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.01)
        if window.isVisible():
            window._shutdown_finished = True
            window.close()
        qt_app.processEvents()


def test_window_close_waits_for_active_read_and_persists_its_result(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    read_started = Event()

    def pump_until(predicate, timeout_s=3.0):
        deadline = time.monotonic() + timeout_s
        while not predicate() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        qt_app.processEvents()
        return predicate()

    try:
        window.show()
        settings = ModeSettings(Mode.DCV, autorange=False, range_si=10, nplc=1)
        window._bridge.connect_mock.emit(settings, MockScenario(
            name="exit during active read", noise_fraction=0, drift_fraction_per_minute=0,
            read_latency_s=0.5, step_at_s=1e9, recovery_at_s=None,
        ))
        assert pump_until(lambda: window._connected)

        instrument = window._worker.instrument
        original_read = instrument.read

        def observed_read():
            read_started.set()
            return original_read()

        instrument.read = observed_read
        window._bridge.start_experiment.emit(
            settings, "exit during active read", str(tmp_path / "experiments"), 0, False
        )
        assert pump_until(read_started.is_set)
        assert window._experiment_csv_path is not None
        csv_path = window._experiment_csv_path

        assert not window.close()
        assert window.isVisible()

        assert pump_until(lambda: not window.isVisible())
        assert window._shutdown_finished
        assert not window._worker_thread.isRunning()
        with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["status"] == "ok"
        assert rows[0]["value_si"] != ""
    finally:
        if window._worker_thread.isRunning():
            window.close()
            pump_until(lambda: not window._worker_thread.isRunning())
        if window.isVisible():
            window._shutdown_finished = True
            window.close()
        qt_app.processEvents()


def test_real_reading_updates_gui_curve_and_stats_without_plotting_failed_value(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._load_mode_controls(Mode.RES2)
        value = 29_977_800.0
        row = {
            "sample_index": 1,
            "timestamp_local": "2026-10-12T19:06:31.663034+08:00",
            "timestamp_utc": "2026-10-12T11:06:31.663034+00:00",
            "elapsed_s": 0.0,
            "mode": Mode.RES2.value,
            "quantity": "resistance",
            "value_si": value,
            "unit_si": "Ohm",
            "raw_reading": "+2.99778000E+07",
            "status": "ok",
            "error": "",
        }
        stats = {
            "count": 1,
            "minimum": value,
            "maximum": value,
            "mean": value,
            "sample_stddev": None,
        }

        window._on_sample([row], 0.0, stats)
        qt_app.processEvents()

        assert window.plot_empty_overlay.isHidden()
        plot_x, plot_y = window.curve.getData()
        assert list(plot_x) == [0.0]
        assert list(plot_y) == [value]
        assert window._last_value == value
        assert "29.9778 MΩ" in window.latest_value_label.text()
        assert "n=1" in window.stats_label.text()

        failed_row = {**row, "sample_index": 2, "value_si": "", "status": "timeout", "error": "timeout"}
        window._on_sample([failed_row], 0.0, stats)
        qt_app.processEvents()

        plot_x, plot_y = window.curve.getData()
        assert list(plot_x) == [0.0]
        assert list(plot_y) == [value]
        assert window._last_value == value
        assert "数据无效：timeout" in window.log_text.toPlainText()

        window._clear_plot()
        assert not window.plot_empty_overlay.isHidden()
        assert window.latest_value_label.text() == "等待读数"
        assert window._global_stats["count"] == 0
    finally:
        window.close()
        qt_app.processEvents()


def test_live_plot_cache_stays_bounded_while_global_stats_keep_all_samples(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(gui_module, "MAX_PLOT_POINTS", 3)
    window = MainWindow()
    rows = [
        {
            "sample_index": index,
            "elapsed_s": float(index - 1),
            "mode": Mode.RES2.value,
            "quantity": "resistance",
            "value_si": float(100 + index),
            "unit_si": "Ohm",
            "raw_reading": f"+{100 + index}.0E+00",
            "status": "ok",
            "error": "",
        }
        for index in range(1, 6)
    ]
    full_stats = {
        "count": 5,
        "minimum": 101.0,
        "maximum": 105.0,
        "mean": 103.0,
        "sample_stddev": (2.5) ** 0.5,
    }
    try:
        window._current_mode = Mode.RES2
        window._load_mode_controls(Mode.RES2)
        window._on_sample(rows, 1.0, full_stats)
        qt_app.processEvents()

        plot_x, plot_y = window.curve.getData()
        assert list(plot_x) == [2.0, 3.0, 4.0]
        assert list(plot_y) == [103.0, 104.0, 105.0]
        assert list(window._plot_x) == [2.0, 3.0, 4.0]
        assert list(window._plot_y) == [103.0, 104.0, 105.0]
        assert window._global_stats["count"] == 5
        assert "n=5" in window.stats_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_local_region_statistics_use_raw_csv_then_track_new_samples(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    stream = csv_path.open("w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows((
        {"sample_index": 1, "elapsed_s": 0, "mode": "RES2", "value_si": 29e6, "status": "ok"},
        {"sample_index": 2, "elapsed_s": 1, "mode": "RES2", "value_si": 30e6, "status": "ok"},
        {"sample_index": 3, "elapsed_s": 2, "mode": "RES2", "value_si": 31e6, "status": "ok"},
        {"sample_index": 4, "elapsed_s": 3, "mode": "RES2", "value_si": 99e6, "status": "ok"},
        {"sample_index": 5, "elapsed_s": 1.5, "mode": "RES2", "value_si": "", "status": "timeout"},
    ))
    stream.flush()
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._experiment_csv_path = csv_path
        # Deliberately sparse plot points prove stats come from the source CSV.
        window._plot_x.extend((0.0, 3.0))
        window._plot_y.extend((29e6, 99e6))
        window.show()
        window._region.show()
        window._region.setRegion((0.0, 2.0))
        qt_app.processEvents()
        window._update_region_stats()
        deadline = time.monotonic() + 3.0
        while "完整原始 CSV" not in window._region_stats_label.text() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)

        text = window._region_stats_label.text()
        assert "完整原始 CSV" in text
        assert "n=3" in text
        assert "最大 31 MΩ" in text
        assert "最小 29 MΩ" in text
        assert "均值 30 MΩ" in text
        assert "样本标准差 s 1 MΩ" in text

        new_row = {
            "sample_index": 6, "elapsed_s": 1.5, "mode": "RES2",
            "value_si": 32e6, "status": "ok",
        }
        writer.writerow(new_row)
        stream.flush()
        window._on_region_samples([new_row])
        text = window._region_stats_label.text()
        assert "n=4" in text
        assert "最大 32 MΩ" in text
        assert "均值 30.5 MΩ" in text
        assert "完整原始 CSV · 实时更新" in text
    finally:
        window.close()
        qt_app.processEvents()
        stream.close()


@pytest.mark.parametrize("mode", list(Mode))
def test_mock_gui_continuous_acquisition_plots_and_persists_each_mode(
    qt_app, monkeypatch, tmp_path, mode
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(
        gui_module.WiringConfirmationDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    window = MainWindow()
    finished = []
    window._worker.experiment_finished.connect(finished.append)
    try:
        window._simulation = True
        window.mock_mode_check.setChecked(True)
        previous_signals = window.mode_combo.blockSignals(True)
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(mode.value))
        window.mode_combo.blockSignals(previous_signals)
        window._current_mode = mode
        window._load_mode_controls(mode)
        window.interval_spin.setValue(0.0)
        window.output_root.setText(str(tmp_path / "runs"))
        window.experiment_name.setText(f"{mode.value} mock GUI")

        window._connect_mock()
        deadline = time.monotonic() + 3.0
        while not window._connected and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        assert window._connected and window._simulation

        window._start_continuous()
        deadline = time.monotonic() + 3.0
        while int(window._global_stats.get("count", 0)) < 3 and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        assert int(window._global_stats["count"]) >= 3
        window._stop()

        deadline = time.monotonic() + 3.0
        while not finished and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        assert len(finished) == 1

        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            qt_app.processEvents()
            original_x, _original_y = window.curve.getOriginalDataset()
            original_count = 0 if original_x is None else len(original_x)
            if (
                not window._experiment_active
                and int(window._global_stats.get("count", 0)) == len(rows)
                and original_count == len(window._plot_x)
            ):
                break
            time.sleep(0.005)

        plot_x, plot_y = window.curve.getData()
        original_x, original_y = window.curve.getOriginalDataset()
        assert plot_x is not None and plot_y is not None and len(plot_x) > 0
        assert original_x is not None and original_y is not None
        assert len(original_x) == len(original_y) == len(window._plot_x) == len(window._plot_y)
        assert list(original_x) == list(window._plot_x)
        assert list(original_y) == list(window._plot_y)
        assert len(rows) == window._global_stats["count"]
        assert all(row["mode"] == mode.value for row in rows)
        assert DESCRIPTORS[mode].label in window.reading_mode_label.text()
        assert f"n={window._global_stats['count']}" in window.stats_label.text()

        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(rows) == window._global_stats["count"]
        assert metadata["simulation"] is True
        assert metadata["mode"] == mode.value
        assert all(row["status"] == "ok" and row["raw_reading"] for row in rows)
        assert all(row["unit_si"] == DESCRIPTORS[mode].unit_si for row in rows)
        assert (directory / "events.csv").is_file()
        assert (directory / "run.log").is_file()
    finally:
        window.close()
        qt_app.processEvents()


def test_baseline_clear_and_manual_event_leave_csv_events_and_curve_markers(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    emitted_events = []
    try:
        window._bridge.mark_event.disconnect(window._worker.mark_event)
        window._bridge.mark_event.connect(emitted_events.append)
        window._current_mode = Mode.RES2
        window._load_mode_controls(Mode.RES2)
        window.show()
        qt_app.processEvents()
        window._last_value = 29_977_800.0
        window._last_elapsed = 1.25
        window._experiment_active = True

        window._set_current_as_baseline()
        assert window._baseline == 29_977_800.0
        assert window.baseline_line.isVisible()
        assert "当前读数设为基准" in emitted_events[0]
        assert window._event_lines[-1].value() == 1.25

        window._clear_baseline()
        assert window._baseline is None
        assert not window.baseline_line.isVisible()
        assert "已清除基准值" in emitted_events[1]
        assert window._event_lines[-1].value() == 1.25

        window.event_edit.setText("环境温度调整")
        window._mark_event()
        assert emitted_events[2] == "环境温度调整"
        assert window.event_edit.text() == ""
        assert len(window._event_lines) == 3
        assert window._event_lines[-1].value() == 1.25
        qt_app.processEvents()
        label_rects = [label.mapRectToScene(label.boundingRect()) for label in window._event_labels]
        assert all(not left.intersects(right) for i, left in enumerate(label_rects) for right in label_rects[i + 1:])
    finally:
        window.close()
        qt_app.processEvents()


def test_sampling_interval_editor_emits_live_changes_and_maps_zero_to_fastest(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    emitted = []
    window._bridge.set_sampling_interval.connect(emitted.append)
    try:
        window._acquiring = True
        window.interval_spin.setValue(0.0)
        window.interval_spin.setValue(0.25)
        qt_app.processEvents()

        assert emitted == [None, 0.25]
    finally:
        window._acquiring = False
        window.close()
        qt_app.processEvents()


def test_sampling_interval_preference_persists_across_window_restarts(qt_app, monkeypatch, tmp_path):
    config_dir = tmp_path / "app-config"
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))
    first = MainWindow()
    first.interval_spin.setValue(0.375)
    first.close()
    qt_app.processEvents()

    second = MainWindow()
    try:
        assert second.interval_spin.value() == 0.375
    finally:
        second.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("firmware", "terminals", "stored_range", "unsupported_ranges"),
    [
        ("1.0.0", "FRON", 0.4, (0.4,)),
        ("2.0", "REAR", 1.0, (1.0, 3.0, 10.0)),
    ],
)
def test_unverified_current_autorange_and_ranges_are_disabled_in_gui(
    qt_app, monkeypatch, tmp_path, firmware, terminals, stored_range, unsupported_ranges
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = False
        window._firmware = firmware
        window._terminals = terminals
        window._current_mode = Mode.DCI
        window._profiles[Mode.DCI.value].update({"autorange": True, "range_si": stored_range})

        window._load_mode_controls(Mode.DCI)

        assert not window.autorange_check.isEnabled()
        assert not window.autorange_check.isChecked()
        assert window.range_combo.isEnabled()
        assert window.range_combo.currentData() == 100e-6
        for value in unsupported_ranges:
            index = window.range_combo.findData(value)
            assert index >= 0
            assert not window.range_combo.model().item(index).isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("mode", list(Mode))
def test_gui_exposes_only_parameters_supported_by_selected_mode(qt_app, monkeypatch, tmp_path, mode):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = True
        window._current_mode = mode
        window._load_mode_controls(mode)
        spec = DESCRIPTORS[mode]

        assert window.nplc_combo.isEnabled() is bool(spec.nplc_values)
        if spec.nplc_values:
            assert window.nplc_combo.count() == len(spec.nplc_values)
        else:
            assert window.nplc_combo.count() == 1
            assert window.nplc_combo.itemText(0) == "不适用"
        assert window.bandwidth_combo.isEnabled() is bool(spec.ac_bandwidths_hz)
        if spec.ac_bandwidths_hz:
            assert window.bandwidth_combo.count() == len(spec.ac_bandwidths_hz)
        else:
            assert window.bandwidth_combo.count() == 1
            assert window.bandwidth_combo.itemText(0) == "不适用"
        assert window.analog_filter_check.isHidden() is (not spec.analog_filter)
    finally:
        window.close()
        qt_app.processEvents()


def test_measurement_speed_and_sampling_interval_are_separate_controls(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = True
        window._current_mode = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        fast_index = window.nplc_combo.findData(0.02)
        slow_index = window.nplc_combo.findData(100)
        assert fast_index >= 0 and "快" in window.nplc_combo.itemText(fast_index)
        assert slow_index >= 0 and "慢" in window.nplc_combo.itemText(slow_index)

        window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(0.2))
        window.interval_spin.setValue(2.5)
        assert window._profiles[Mode.DCV.value]["nplc"] == 0.2
        assert window._profiles["sampling_interval_s"] == 2.5

        window._connected = True
        window._acquiring = True
        window._update_controls()
        assert window.nplc_combo.isEnabled()
        assert window.apply_speed_button.isEnabled()
        assert window.interval_spin.isEnabled()

        window._acquiring = False
        window._connected = False
        window._current_mode = Mode.ACV
        window._load_mode_controls(Mode.ACV)
        fast_band = window.bandwidth_combo.findData(200)
        slow_band = window.bandwidth_combo.findData(3)
        assert "快" in window.bandwidth_combo.itemText(fast_band)
        assert "慢" in window.bandwidth_combo.itemText(slow_band)
        assert window.nplc_combo.currentData() is None
    finally:
        window.close()
        qt_app.processEvents()


def test_readout_labels_applied_and_pending_measurement_speed(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._connected = True
        window._load_mode_controls(Mode.RES2)
        window._on_active_settings({
            "mode": Mode.RES2.value,
            "autorange": True,
            "range_si": None,
            "nplc": 1.0,
            "ac_bandwidth_hz": None,
            "analog_filter": False,
            "autozero": True,
        })
        assert "仪表已应用" in window.reading_mode_label.text()
        assert "1 NPLC" in window.reading_mode_label.text()

        window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(100))
        assert "仪表已应用" in window.reading_mode_label.text()
        assert "1 NPLC" in window.reading_mode_label.text()
        assert "待应用 100 NPLC" in window.reading_mode_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_configuration_mode_change_invalidates_old_readback(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._current_mode = Mode.RES2
        window._load_mode_controls(Mode.RES2)
        window._active_settings_by_mode[Mode.RES2.value] = {
            "mode": Mode.RES2.value,
            "autorange": True,
            "range_si": None,
            "nplc": 1.0,
            "ac_bandwidth_hz": None,
            "analog_filter": False,
            "autozero": True,
        }

        window._on_configuration({
            "function": '"VOLT:DC"',
            "input_terminals": "FRON",
            "idn": "FLUKE,8845A,1234567,1.0.0",
            "simulation": False,
        })

        assert window._current_mode == Mode.DCV
        assert window._active_settings_by_mode == {}
        assert "仪表设置待读回" in window.reading_mode_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_auto_follow_expands_during_startup_then_rolls_after_two_minutes(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.DCV
        stats = {"count": 1, "minimum": 1.0, "maximum": 1.0, "mean": 1.0, "sample_stddev": None}
        window._on_sample(
            [{"status": "ok", "value_si": 1.0, "elapsed_s": 30.0, "mode": Mode.DCV.value}],
            0.1,
            stats,
        )
        qt_app.processEvents()

        left, right = window.plot_widget.getPlotItem().vb.viewRange()[0]
        assert left == pytest.approx(0.0)
        assert right == pytest.approx(31.0)

        stats["count"] = 2
        window._on_sample(
            [{"status": "ok", "value_si": 1.1, "elapsed_s": 140.0, "mode": Mode.DCV.value}],
            0.1,
            stats,
        )
        qt_app.processEvents()

        left, right = window.plot_widget.getPlotItem().vb.viewRange()[0]
        assert left == pytest.approx(21.0)
        assert right == pytest.approx(141.0)
    finally:
        window.close()
        qt_app.processEvents()


def test_speed_and_interval_controls_remain_readable_at_minimum_window_size(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.resize(1120, 720)
        window.show()
        qt_app.processEvents()

        assert window.splitter.widget(0).objectName() == "setupScrollArea"
        for control in (window.nplc_combo, window.apply_speed_button, window.interval_spin):
            assert control.height() >= control.sizeHint().height()
            assert control.isVisible()
        assert window.plot_widget.height() >= 120
    finally:
        window.close()
        qt_app.processEvents()


def test_windows_ui_font_is_available_even_when_qt_font_database_starts_empty(
    qt_app, monkeypatch, tmp_path
):
    if os.name != "nt":
        pytest.skip("Windows YaHei font registration is platform-specific")
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert "Microsoft YaHei UI" in gui_module.QFontDatabase.families()
        assert QFontInfo(window.font()).family() == "Microsoft YaHei UI"
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("mode", "range_si", "expected_terminal"),
    [
        (Mode.DCI, 1.0, "10 A孔"),
        (Mode.RES2, 100e6, "HI/LO"),
    ],
)
def test_starting_in_current_or_resistance_mode_shows_wiring_schematic_and_cancel_blocks_run(
    qt_app, monkeypatch, tmp_path, mode, range_si, expected_terminal
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    dialogs = []

    def cancel_dialog(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui_module.WiringConfirmationDialog, "exec", cancel_dialog)
    try:
        window._connected = True
        window._instrument_mode_known = True
        window._terminals = "FRONT"
        window._current_mode = mode
        window._profiles[mode.value].update({"autorange": False, "range_si": range_si})
        window._load_mode_controls(mode)

        window._start_continuous()

        assert len(dialogs) == 1
        assert dialogs[0].schematic.mode is mode
        assert dialogs[0].schematic.current_range_si == range_si
        assert expected_terminal in dialogs[0].instructions
        assert "已关闭被测电路电源" in dialogs[0].instructions
        assert window._worker._recorder is None
    finally:
        window.close()
        qt_app.processEvents()


def test_mode_switch_requires_current_wiring_confirmation(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    dialogs = []

    def cancel_dialog(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui_module.WiringConfirmationDialog, "exec", cancel_dialog)
    try:
        window._current_mode = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        aci_index = window.mode_combo.findData(Mode.ACI.value)
        window.mode_combo.setCurrentIndex(aci_index)

        assert len(dialogs) == 1
        assert "关闭回路电源" in dialogs[0].instructions
        assert "串联" in dialogs[0].instructions
        assert "保险丝" in dialogs[0].instructions
        assert dialogs[0].schematic.mode is Mode.ACI
        assert dialogs[0].schematic.autorange is True
        assert "mA/400 mA孔" in dialogs[0].instructions
        assert "10 A孔" in dialogs[0].instructions
        assert window._current_mode is Mode.DCV
        assert window.mode_combo.currentData() == Mode.DCV.value
    finally:
        window.close()
        qt_app.processEvents()


def test_entering_high_current_mode_shows_the_10a_jack_for_saved_range(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    dialogs = []

    def cancel_dialog(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui_module.WiringConfirmationDialog, "exec", cancel_dialog)
    try:
        window._current_mode = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        window._profiles[Mode.DCI.value].update({"autorange": False, "range_si": 1.0})
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(Mode.DCI.value))

        assert len(dialogs) == 1
        assert dialogs[0].schematic.current_range_si == 1.0
        assert dialogs[0].schematic.autorange is False
        assert "当前固定量程 1 A" in dialogs[0].instructions
        assert "10 A孔" in dialogs[0].instructions
        assert window._current_mode is Mode.DCV
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("old_mode", "new_mode", "expected_prompt"),
    [
        (Mode.ACI, Mode.DCV, "移回V/Ω插孔"),
        (Mode.RES2, Mode.RES4, "SENSE"),
        (Mode.DCV, Mode.ACV, None),
    ],
)
def test_mode_switch_safety_checks_follow_terminal_changes(
    qt_app, monkeypatch, tmp_path, old_mode, new_mode, expected_prompt
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    dialogs = []

    def cancel_dialog(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(gui_module.WiringConfirmationDialog, "exec", cancel_dialog)
    try:
        window._current_mode = old_mode
        was_blocked = window.mode_combo.blockSignals(True)
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(old_mode.value))
        window.mode_combo.blockSignals(was_blocked)
        window._load_mode_controls(old_mode)
        new_index = window.mode_combo.findData(new_mode.value)
        window.mode_combo.setCurrentIndex(new_index)

        if expected_prompt is None:
            assert dialogs == []
            assert window._current_mode is new_mode
            assert window.mode_combo.currentData() == new_mode.value
        else:
            assert len(dialogs) == 1
            assert expected_prompt in dialogs[0].instructions
            assert dialogs[0].schematic.mode is new_mode
            assert window._current_mode is old_mode
            assert window.mode_combo.currentData() == old_mode.value
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("mode", list(Mode))
def test_mode_wiring_schematic_renders_for_every_measurement_mode(qt_app, mode):
    widget = gui_module.WiringSchematicWidget(mode)
    widget.resize(920, 280)
    image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    widget.render(image)

    assert widget.objectName() == "wiringSchematic"
    assert not image.isNull()
    assert image.pixelColor(20, 20).alpha() == 255
    widget.close()


@pytest.mark.parametrize(("current_range_si", "autorange", "expected_terminal"), [
    (0.4, False, "mA/400 mA"),
    (1.0, False, "10 A"),
    (None, True, "只能连接其中一个"),
])
def test_current_wiring_diagram_keeps_mA_and_10A_as_separate_inputs(
    qt_app, current_range_si, autorange, expected_terminal
):
    widget = gui_module.WiringSchematicWidget(
        Mode.DCI, current_range_si=current_range_si, autorange=autorange,
    )
    widget.resize(920, 280)
    image = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    widget.render(image)

    guidance = gui_module.current_input_guidance(current_range_si, autorange)
    assert not image.isNull()
    assert expected_terminal in guidance
    assert "不能检测表笔实际插孔" in guidance
    widget.close()


def test_current_range_safety_hint_tracks_the_selected_front_panel_jack(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = True
        window._current_mode = Mode.DCI
        window._load_mode_controls(Mode.DCI)
        window.autorange_check.setChecked(False)

        window.range_combo.setCurrentIndex(window.range_combo.findData(0.4))
        assert "当前固定量程 400 mA" in window.safety_text.text()
        assert "mA/400 mA孔" in window.safety_text.text()

        window.range_combo.setCurrentIndex(window.range_combo.findData(1.0))
        assert "当前固定量程 1 A" in window.safety_text.text()
        assert "10 A孔" in window.safety_text.text()

        window.autorange_check.setChecked(True)
        assert "自动量程时按最大预期电流选孔" in window.safety_text.text()
        assert "只能连接其中一个红色电流孔" in window.safety_text.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_mode_switch_is_refused_during_acquisition_without_reconfiguring(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    errors = []
    try:
        window._current_mode = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        window._connected = True
        window._acquiring = True
        window._show_error = errors.append
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(Mode.ACI.value))

        assert window._current_mode is Mode.DCV
        assert window.mode_combo.currentData() == Mode.DCV.value
        assert len(errors) == 1
        assert "不能切换测量模式" in errors[0]
    finally:
        window._acquiring = False
        window._connected = False
        window.close()
        qt_app.processEvents()


def test_acquisition_thread_keeps_saving_while_gui_event_loop_is_stalled(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    finished = []
    window._worker.experiment_finished.connect(finished.append)

    def pump_until(predicate, timeout_s=5.0):
        deadline = time.monotonic() + timeout_s
        while not predicate() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        qt_app.processEvents()
        return predicate()

    try:
        window.mock_mode_check.setChecked(True)
        window.output_root.setText(str(tmp_path / "experiments"))
        window.experiment_name.setText("GUI stall stress")
        window.interval_spin.setValue(0.0)
        window._connect_mock()
        assert pump_until(lambda: window._connected)

        window._start_continuous()
        assert pump_until(lambda: window._acquiring and window._worker._recorder is not None)
        before = window._worker._recorder.sample_count

        # Intentionally block the GUI thread; acquisition and CSV flushing run in QThread.
        time.sleep(0.4)

        recorder = window._worker._recorder
        assert recorder is not None
        after = recorder.sample_count
        assert after - before >= 5
        assert (recorder.directory / "measurements.csv").stat().st_size > 200
        assert window._worker._ui_notification_pending is True
        assert len(window._worker._ui_pending_rows) <= MAX_PENDING_GUI_ROWS

        window._stop()
        assert pump_until(lambda: bool(finished) and not window._acquiring)
        assert window._worker._ui_notification_pending is False
        directory = Path(finished[0])
        with (directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        metadata = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        assert len(rows) >= after
        assert all(row["status"] == "ok" for row in rows)
        assert metadata["simulation"] is True
        assert metadata["completion_status"] == "stopped"
    finally:
        window.close()
        qt_app.processEvents()


def test_history_load_shows_last_value_exact_stats_and_exports_curve_screenshot(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(gui_module, "MAX_PLOT_POINTS", 3)
    source = tmp_path / "history.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status", "error")
    original = [
        {"sample_index": index, "elapsed_s": elapsed, "mode": "RES2", "value_si": value, "status": "ok", "error": ""}
        for index, (elapsed, value) in enumerate(zip(range(5), (29_970_000, 29_980_000, 29_990_000, 30_000_000, 30_010_000)), 1)
    ]
    original.insert(2, {"sample_index": 3, "elapsed_s": 1.5, "mode": "RES2", "value_si": "", "status": "timeout", "error": "timeout"})
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(original)
    before = source.read_bytes()
    events_source = tmp_path / "events.csv"
    with events_source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("event_index", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerows([
            {"event_index": 1, "elapsed_s": "1.5", "event": "接通负载"},
            {"event_index": 2, "elapsed_s": "4", "event": "移除刺激"},
            {"event_index": 3, "elapsed_s": "6", "event": "不在本图范围"},
            {"event_index": 4, "elapsed_s": "nan", "event": "无效时间"},
        ])
    events_before = events_source.read_bytes()
    monkeypatch.setattr(gui_module.QFileDialog, "getOpenFileName", lambda *_args: (str(source), "CSV"))
    monkeypatch.setattr(gui_module.QMessageBox, "information", lambda *_args: gui_module.QMessageBox.StandardButton.Ok)
    window = MainWindow()
    screenshot = tmp_path / "history.png"
    try:
        window.show()
        window._last_elapsed = 100.0
        window._add_event_marker("上一个实验的旧标记")
        window._load_history()
        qt_app.processEvents()

        plot_x, plot_y = window.curve.getData()
        assert list(plot_x) == [0.0, 2.0, 4.0]
        assert list(plot_y) == [29_970_000.0, 29_990_000.0, 30_010_000.0]
        assert window._last_value == 30_010_000.0
        assert "30.01 MΩ" in window.latest_value_label.text()
        assert window.actual_rate_label.text() == "1 Hz"
        assert "n=5" in window.stats_label.text()
        assert source.read_bytes() == before
        assert events_source.read_bytes() == events_before
        assert [line.value() for line in window._event_lines] == [1.5, 4.0]
        assert [label.toPlainText() for label in window._event_labels] == ["接通负载", "移除刺激"]

        monkeypatch.setattr(gui_module.QFileDialog, "getSaveFileName", lambda *_args: (str(screenshot), "PNG"))
        window._save_screenshot()
        qt_app.processEvents()
        assert screenshot.exists() and screenshot.stat().st_size > 0
    finally:
        window.close()
        qt_app.processEvents()
