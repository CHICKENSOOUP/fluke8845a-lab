from __future__ import annotations

import os
import csv
import json
import math
import re
import subprocess
import sys
import textwrap
import time
from threading import Event
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFontInfo, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QLineEdit, QPushButton

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


def _wait_history_load(window, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while getattr(window, "_history_load_pending", False) and time.monotonic() < deadline:
        QTest.qWait(10)
    assert not getattr(window, "_history_load_pending", False), "后台历史 CSV 加载超时"


@pytest.mark.parametrize(
    ("rate_hz", "expected"),
    [
        (1.0, "1 s"),
        (2.0, "500 ms"),
        (0.5, "2 s"),
        (1000.0, "1 ms"),
        (0.0, "—"),
        (float("inf"), "—"),
    ],
)
def test_sampling_interval_display_uses_measured_rate(rate_hz, expected):
    assert gui_module._format_sampling_interval(rate_hz) == expected


@pytest.mark.parametrize(
    "profile_text",
    [
        "[]",
        '{"DCV":"bad"}',
        '{"DCV":{"baseline":"not-a-number"}}',
        '{"serial":{"timeout_s":"not-a-number"}}',
    ],
)
def test_structurally_invalid_saved_preferences_fall_back_to_defaults(
    qt_app, monkeypatch, tmp_path, profile_text
):
    config_dir = tmp_path / "app-config"
    config_dir.mkdir()
    (config_dir / "profiles.json").write_text(profile_text, encoding="utf-8")
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))

    window = MainWindow()
    try:
        assert window._profiles["DCV"]["autorange"] is True
        assert window._profiles["DCV"]["baseline"] is None
        assert window.interval_spin.value() == 1.0
    finally:
        window.close()
        qt_app.processEvents()


def test_extreme_integer_preferences_fall_back_without_crashing(qt_app, monkeypatch, tmp_path):
    config_dir = tmp_path / "app-config"
    config_dir.mkdir()
    huge_integer = 10 ** 400
    profile_text = json.dumps({
        "DCV": {"baseline": huge_integer, "range_si": huge_integer},
        "serial": {"timeout_s": huge_integer},
        "sampling_interval_s": huge_integer,
    })
    (config_dir / "profiles.json").write_text(profile_text, encoding="utf-8")
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))

    window = MainWindow()
    try:
        assert window._profiles["DCV"]["baseline"] is None
        assert window._profiles["DCV"]["range_si"] == DESCRIPTORS[Mode.DCV].ranges_si[0]
        assert window.timeout_spin.value() == 2.0
        assert window.interval_spin.value() == 1.0
    finally:
        window.close()
        qt_app.processEvents()


def test_main_window_is_a_single_plot_with_settings_in_a_separate_window(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert window.setup_section_stack.count() == 3
        assert [button.text() for button in window.setup_section_buttons] == ["通信", "测量", "实验"]
        assert window.log_dock.isHidden()
        assert window.configuration_dialog.isHidden()
        assert window.workspace_stack.count() == 2
        assert window.configuration_button.text() == "设置与分析…"
        assert window.configuration_button.parentWidget().objectName() == "appHeader"
        for object_name in (
            "communicationsNavigation", "acquisitionNavigation",
            "analysisNavigation", "experimentFilesNavigation", "diagnosticsNavigation",
        ):
            assert window.findChild(QPushButton, object_name) is not None
        assert window.setup_scroll_area.objectName() == "configurationScrollArea"
        assert window.setup_scroll_area.findChild(QPushButton, "diagnoseButton") is window.diagnose_button
        assert window.communication_workspace.isAncestorOf(window.port_combo)
        assert window.communication_workspace.isAncestorOf(window.baud_combo)
        assert window.communication_workspace.isAncestorOf(window.connection_stages_label)
        assert window.communication_workspace.isAncestorOf(window.communication_log_text)
        assert window.setup_section_stack.widget(1).isAncestorOf(window.mode_combo)
        assert window.measurement_workspace.isAncestorOf(window.interval_spin)
        assert not window.setup_section_stack.widget(2).isAncestorOf(window.interval_spin)
        experiment_page = window.setup_section_stack.widget(2)
        assert experiment_page.isAncestorOf(window.output_root)
        assert experiment_page.isAncestorOf(window.open_experiment_directory_button)
        assert experiment_page.isAncestorOf(window.export_snapshot_button)
        assert window.port_combo.minimumContentsLength() == 14
        assert window.nplc_combo.isEnabled()
        assert not window.bandwidth_combo.isEnabled()
        assert window.findChild(QFrame, "readingCard")
        assert window.findChild(QFrame, "plotToolbar")
        assert window.brand_icon.pixmap() is not None
        assert window.latest_value_label.text() == "等待读数"
        assert "DCV" in window.reading_mode_label.text()
        assert not window.plot_empty_overlay.isHidden()
        assert window.plot_widget.parent() is not None
        assert window.input_impedance_auto_check.isHidden() is False
        window.input_impedance_auto_check.setChecked(True)
        assert window._settings_for_mode(Mode.DCV).input_impedance_auto is True
        window._load_mode_controls(Mode.ACV)
        assert window.input_impedance_auto_check.isHidden() is True
        assert all(window._chart_panel_layout.itemAt(i).widget() is not window.analysis_tabs
                   for i in range(window._chart_panel_layout.count()))
        assert window.autoscale_button.text() == "全部数据"
        assert "纵轴缩放仍跟随" in window.acquisition_hint.text()
        assert "时间轴缩放或平移会停止跟随" in window.acquisition_hint.text()

        window._on_status("连续采集中 · 目标间隔 0 s")
        assert window.experiment_status.text() == "连续采集中"
        assert window.experiment_status.toolTip() == "连续采集中 · 目标间隔 0 s"
    finally:
        window._acquiring = False
        window.close()
        qt_app.processEvents()


def test_compact_window_reduces_plot_minimum_and_action_spacing(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "compact-layout-config"))
    window = MainWindow()
    try:
        window.measurement_workspace.resize(600, 550)
        window._update_readout_layout()

        assert window._short_chart_layout
        assert window.plot_widget.minimumHeight() <= 180
        assert window._action_layout.spacing() <= 4
        assert not window.configuration_button.isHidden()
    finally:
        window.close()
        qt_app.processEvents()


def _contrast_ratio(foreground: QColor, background: QColor) -> float:
    def luminance(color: QColor) -> float:
        channels = [color.redF(), color.greenF(), color.blueF()]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (first + 0.05) / (second + 0.05)


def _stylesheet_rule(window: MainWindow, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", window.styleSheet())
    assert match is not None, f"missing stylesheet rule for {selector}"
    return dict(re.findall(r"([a-z-]+)\s*:\s*([^;]+)", match.group(1)))


def test_auxiliary_text_is_readable_on_light_plot_and_dark_readout(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "readable-text-config"))
    window = MainWindow()
    try:
        light_text = window.plot_empty_hint
        light_style = _stylesheet_rule(window, "QLabel#plotEmptyHint")
        light_color = QColor(light_style["color"])
        assert int(light_style["font-size"].removesuffix("px")) >= 12
        assert _contrast_ratio(light_color, QColor("#f1efe6")) >= 4.5

        dark_text = window._reading_kicker
        dark_style = _stylesheet_rule(window, "QFrame#readingCard QLabel#eyebrow")
        dark_color = QColor(dark_style["color"])
        assert int(dark_style["font-size"].removesuffix("px")) >= 12
        assert _contrast_ratio(dark_color, QColor("#1d262a")) >= 4.5
    finally:
        window.close()
        qt_app.processEvents()


def test_sampling_interval_labels_have_readable_contrast_on_dark_action_bar(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "interval-contrast-config"))
    window = MainWindow()
    try:
        background = QColor("#30383c")
        for object_name in ("targetIntervalLabel", "fastestIntervalLabel"):
            label = window.findChild(QLabel, object_name)
            assert label is not None
            rule = _stylesheet_rule(window, f"QLabel#{object_name}")
            color = QColor(rule["color"])
            assert int(rule["font-size"].removesuffix("px")) >= 12
            assert _contrast_ratio(color, background) >= 4.5
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_toolbar_reflows_without_clipping_and_keeps_hints_readable(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "plot-toolbar-layout-config"))
    window = MainWindow()
    try:
        window.resize(700, 720)
        window.show()
        qt_app.processEvents()

        toolbar = window.findChild(QFrame, "plotToolbar")
        assert toolbar is not None
        controls = (
            window.follow_check,
            window.view_mode_label,
            window.acquisition_hint,
            window.chart_gesture_help_button,
            window.chart_source_label,
            window.autoscale_button,
            window.return_live_button,
            window.history_button,
            window.screenshot_button,
        )
        visible = [control for control in controls if not control.isHidden()]

        def assert_toolbar_geometry_is_safe():
            for index, control in enumerate(visible):
                assert toolbar.contentsRect().contains(control.geometry()), control.objectName() or control.text()
                for other in visible[index + 1 :]:
                    assert not control.geometry().intersects(other.geometry())

        def toolbar_uses_multiple_rows():
            centers = [control.geometry().center().y() for control in visible]
            return max(centers) - min(centers) > 8

        assert window._plot_toolbar_mode != "wide"
        assert toolbar_uses_multiple_rows()
        assert_toolbar_geometry_is_safe()

        assert window.acquisition_hint.wordWrap()
        for object_name in ("chartViewMode", "chartAcquisitionHint"):
            rule = _stylesheet_rule(window, f"QLabel#{object_name}")
            color = QColor(rule["color"])
            assert int(rule["font-size"].removesuffix("px")) >= 12
            assert _contrast_ratio(color, QColor("#252d31")) >= 4.5

        window.resize(1500, 720)
        qt_app.processEvents()
        assert window._plot_toolbar_mode == "wide"
        assert not window.acquisition_hint.wordWrap()
        assert not toolbar_uses_multiple_rows()
        assert_toolbar_geometry_is_safe()

        window.resize(700, 720)
        qt_app.processEvents()
        assert window._plot_toolbar_mode != "wide"
        assert toolbar_uses_multiple_rows()
        assert_toolbar_geometry_is_safe()
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_toolbar_follow_and_source_labels_have_readable_contrast(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "plot-toolbar-contrast-config"))
    window = MainWindow()
    try:
        background = QColor("#252d31")
        assert window.follow_check.objectName() == "chartFollowCheck"
        for selector in ("QCheckBox#chartFollowCheck", "QLabel#chartSourceLabel"):
            rule = _stylesheet_rule(window, selector)
            assert int(rule["font-size"].removesuffix("px")) >= 12
            assert _contrast_ratio(QColor(rule["color"]), background) >= 4.5
    finally:
        window.close()
        qt_app.processEvents()


def test_target_sampling_interval_is_in_the_main_acquisition_workspace(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "acquisition-interval-config"))
    window = MainWindow()
    try:
        assert window.measurement_workspace.isAncestorOf(window.interval_spin)
        assert not window.setup_section_stack.widget(2).isAncestorOf(window.interval_spin)
    finally:
        window.close()
        qt_app.processEvents()


def test_empty_chart_entry_opens_the_connection_workspace(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "empty-chart-config"))
    window = MainWindow()
    try:
        connect_button = window.findChild(QPushButton, "plotEmptyConnectButton")
        assert connect_button is not None
        assert not window.plot_empty_overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        connect_button.click()

        assert not window.configuration_dialog.isHidden()
        assert window.setup_section_stack.currentIndex() == 0
    finally:
        window.close()
        qt_app.processEvents()


def test_connected_mock_scenario_is_visible_on_the_main_screen(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "mock-scenario-header-config"))
    window = MainWindow()
    try:
        window.scenario_combo.setCurrentText("通信超时测试")
        window._on_connected(True, "MOCK 模拟仪器")
        window._navigate_workspace("acquisition")

        assert not window.mock_scenario_header.isHidden()
        assert window.mock_scenario_header.text() == "MOCK场景：通信超时测试"
    finally:
        window.close()
        qt_app.processEvents()


def test_input_impedance_profile_is_dc_voltage_only(qt_app, monkeypatch, tmp_path):
    config_dir = tmp_path / "app-config"
    config_dir.mkdir()
    (config_dir / "profiles.json").write_text(json.dumps({
        "DCV": {"input_impedance_auto": True},
        "ACV": {"input_impedance_auto": True},
    }), encoding="utf-8")
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))
    window = MainWindow()
    try:
        assert window.input_impedance_auto_check.isChecked()
        assert window._profiles["ACV"]["input_impedance_auto"] is False
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("ACV"))
        qt_app.processEvents()
        assert window.input_impedance_auto_check.isHidden()
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("DCV"))
        qt_app.processEvents()
        assert window.input_impedance_auto_check.isChecked()
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_top_navigation_opens_the_matching_settings_section(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.analysisNavigationButton.click()
        assert window.configuration_dialog.isVisible()
        assert window.workspace_stack.currentIndex() == 1
        assert window.setup_section_stack.currentIndex() == 0
        window.filesNavigationButton.click()
        assert window.configuration_dialog.isHidden()
        assert window.files_dialog.isVisible()
        window.acquisitionNavigationButton.click()
        assert window.files_dialog.isHidden()
        assert window.configuration_dialog.isHidden()
    finally:
        window.close()
        qt_app.processEvents()


def test_communication_workspace_shows_the_same_filtered_scpi_log_as_diagnostics(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "communication-log-config"))
    window = MainWindow()
    try:
        window._append_log("连接已建立")
        window._append_log("> READ?")
        window._append_log("< 1.000000E+00")
        assert window.communication_log_text.toPlainText() == window.log_text.toPlainText()

        window.communication_log_filter_combo.setCurrentIndex(
            window.communication_log_filter_combo.findData("scpi")
        )

        assert window.log_filter_combo.currentData() == "scpi"
        assert window.communication_log_text.toPlainText() == window.log_text.toPlainText()
        assert "READ?" in window.communication_log_text.toPlainText()
        assert "连接已建立" not in window.communication_log_text.toPlainText()
        assert window.communication_workspace.isAncestorOf(window.communication_log_filter_combo)
        assert window.communication_workspace.isAncestorOf(window.communication_export_log_button)

        exported = tmp_path / "filtered-scpi.txt"
        monkeypatch.setattr(
            gui_module.QFileDialog, "getSaveFileName",
            lambda *_args: (str(exported), "文本文件 (*.txt)"),
        )
        window.communication_export_log_button.click()
        content = exported.read_text(encoding="utf-8")
        assert "READ?" in content
        assert "连接已建立" not in content
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_files_navigation_opens_a_dedicated_artifact_workspace(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "files-workspace-config"))
    window = MainWindow()
    try:
        experiment_dir = tmp_path / "experiment"
        experiment_dir.mkdir()
        for name in ("measurements.csv", "events.csv", "config.json", "run.log", "curve.png"):
            (experiment_dir / name).write_text("sample", encoding="utf-8")
        window._experiment_csv_path = experiment_dir / "measurements.csv"
        window._update_experiment_directory_controls()

        window.filesNavigationButton.click()

        assert window.files_dialog.isVisible()
        assert window.configuration_dialog.isHidden()
        assert window.files_directory_label.text() == str(experiment_dir)
        listed = {window.experiment_file_list.item(i).text() for i in range(window.experiment_file_list.count())}
        assert all(any(text.startswith(name) for text in listed) for name in (
            "measurements.csv", "events.csv", "config.json", "run.log", "curve.png",
        ))
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("section", ["communications", "analysis"])
def test_desktop_settings_navigation_closes_files_workspace(
    qt_app, monkeypatch, tmp_path, section
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / f"files-to-{section}"))
    window = MainWindow()
    try:
        window._navigate_workspace("files")
        assert window.files_dialog.isVisible()

        window._navigate_workspace(section)

        assert window.files_dialog.isHidden()
        assert window.configuration_dialog.isVisible()
        assert window.workspace_stack.currentIndex() == (1 if section == "analysis" else 0)
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_top_readouts_follow_connection_status_reading_count_and_interval_order(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "readout-order-config"))
    window = MainWindow()
    try:
        layout = window._reading_layout
        assert layout.indexOf(window._status_tile) < layout.indexOf(window._reading_content)
        assert layout.indexOf(window._reading_content) < layout.indexOf(window._rate_tile)
        assert window._reading_content.layout().indexOf(window.latest_value_label) < window._reading_content.layout().indexOf(window.valid_sample_count_label)
        assert window.connection_badge.text() == "未连接"
    finally:
        window.close()


def test_connection_stage_display_keeps_each_phase_result_and_failure_visible(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path))
    window = MainWindow()
    try:
        for stage, status, message in (
            ("device", "success", "设备 COM3 已选定"),
            ("port", "success", "串口 COM3 已打开"),
            ("identify", "failure", "8845A 身份识别失败：型号不符"),
        ):
            window._on_connection_stage(stage, status, message)

        text = window.connection_stages_label.text()
        assert "设备 成功" in text
        assert "串口 成功" in text
        assert "8845A 失败" in text
        assert "配置 待开始" in text and "就绪 待开始" in text
        assert "型号不符" in window.connection_stages_label.toolTip()
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_auxiliary_labels_do_not_use_tiny_font_sizes(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert not re.search(r"font-size:\s*(?:8|9|10)px", window.styleSheet())
    finally:
        window.close()
        qt_app.processEvents()


def test_zero_confirmation_includes_active_configuration_and_result_is_visible(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    dialogs = []
    monkeypatch.setattr(
        gui_module.QMessageBox,
        "question",
        lambda *args: dialogs.append(args) or gui_module.QMessageBox.StandardButton.No,
    )
    try:
        window._active_settings_by_mode[Mode.DCV.value] = {
            "mode": Mode.DCV.value,
            "autorange": False,
            "range_si": 10.0,
            "nplc": 1.0,
            "ac_bandwidth_hz": None,
            "analog_filter": False,
            "autozero": True,
        }
        window._request_zero()
        assert dialogs
        message = str(dialogs[0][2])
        assert "直流电压" in message
        assert "固定量程 10 V" in message
        assert "NPLC=1" in message
        assert "模拟滤波：关" in message

        window._on_zero_result("ZERO配置已回读；此回读不代表ZERO测量结果")
        assert window.zero_result_label.text() == "ZERO配置已回读；此回读不代表ZERO测量结果"
        assert not window.zero_result_label.isHidden()
    finally:
        window.close()
        qt_app.processEvents()


def test_diagnostics_action_is_available_in_instrument_workspace_only_when_computer_control_is_ready(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert window.diagnose_button.parentWidget().title() == "仪器连接"
        assert not window.diagnose_button.isEnabled()

        window._connected = True
        window._control_mode = "computer"
        window._update_controls()
        assert window.diagnose_button.isEnabled()

        window._control_mode = "local"
        window._update_controls()
        assert not window.diagnose_button.isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_event_recording_is_disabled_until_an_experiment_is_active(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert not window.event_button.isEnabled()
        event_action = window.findChild(gui_module.QAction, "eventShortcutAction")
        assert event_action is not None and not event_action.isEnabled()
        assert "先开始一次实验" in window.event_button.toolTip()

        window._experiment_active = True
        window._update_controls()
        assert window.event_button.isEnabled()
        assert event_action.isEnabled()

        window._experiment_active = False
        window._update_controls()
        assert not window.event_button.isEnabled()
        assert not event_action.isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_event_input_enter_submits_the_event(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    emitted_events = []
    try:
        window._bridge.mark_event.disconnect(window._worker.mark_event)
        window._bridge.mark_event.connect(emitted_events.append)
        window._experiment_active = True
        window._update_controls()
        window.event_edit.setText("接通负载")

        QTest.keyClick(window.event_edit, Qt.Key.Key_Return)

        assert emitted_events == ["接通负载"]
        assert window.event_edit.text() == "接通负载"
        assert not window._event_lines

        window._worker.event_recorded.emit({
            "event": "接通负载", "timestamp_local": "2026-10-16T10:15:00+08:00",
            "elapsed_s": "1.25", "sample_index": 7, "value_si": "2.5", "unit_si": "V",
        })

        assert window.event_edit.text() == ""
    finally:
        window.close()
        qt_app.processEvents()


def test_capture_shortcuts_are_visible_and_bound_to_the_expected_actions(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        expected = {
            "startShortcutAction": "Ctrl+R",
            "pauseShortcutAction": "Ctrl+P",
            "stopShortcutAction": "Ctrl+Shift+S",
            "settingsShortcutAction": "Ctrl+,",
            "eventShortcutAction": "Ctrl+Return",
        }
        for object_name, sequence in expected.items():
            action = window.findChild(gui_module.QAction, object_name)
            assert action is not None
            assert action.shortcut().toString() == sequence

        assert "Ctrl+R" in window.start_button.toolTip()
        assert "Ctrl+P" in window.pause_button.toolTip()
        assert "Ctrl+Shift+S" in window.stop_button.toolTip()
        assert "Ctrl+," in window.configuration_button.toolTip()
        assert "Ctrl+Enter" in window.event_button.toolTip()
    finally:
        window.close()
        qt_app.processEvents()


def test_settings_shortcut_opens_the_settings_dialog(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window.activateWindow()
        window.setFocus()
        qt_app.processEvents()
        QTest.keyClick(window, Qt.Key.Key_Comma, Qt.KeyboardModifier.ControlModifier)
        qt_app.processEvents()

        assert window.configuration_dialog.isVisible()
    finally:
        window.close()
        qt_app.processEvents()


def test_initial_window_dimensions_respect_small_available_screen(
    qt_app,
):
    initial_size, minimum_size = gui_module._window_dimensions_for_available_screen(1200, 800)

    assert initial_size == (1176, 728)
    assert minimum_size == (1040, 700)


def test_main_window_initial_size_follows_logical_screen_work_area(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "startup-screen-config"))
    screen = QApplication.primaryScreen()
    assert screen is not None
    area = screen.availableGeometry()
    window = MainWindow()
    try:
        assert window.width() <= max(1, area.width() - 24)
        assert window.height() <= max(1, area.height() - 72)
        assert window.minimumWidth() <= window.width()
        assert window.minimumHeight() <= window.height()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(("width", "height"), [(1024, 768), (1280, 600), (900, 520)])
def test_settings_dialog_is_clamped_to_available_work_area(qt_app, monkeypatch, tmp_path, width, height):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        available = QRect(20, 30, width, height)
        window._fit_configuration_dialog(available, initial=True)
        window.configuration_dialog.show()
        qt_app.processEvents()

        frame = window.configuration_dialog.frameGeometry()
        assert available.contains(frame), (available, frame)
        assert window.configuration_dialog.width() <= width - 24
        assert window.configuration_dialog.height() <= height - 72

        window.configuration_dialog.resize(width * 2, height * 2)
        window._fit_configuration_dialog(available)
        qt_app.processEvents()
        frame = window.configuration_dialog.frameGeometry()
        assert available.contains(frame), (available, frame)
        assert window.configuration_dialog.width() <= width - 24
        assert window.configuration_dialog.height() <= height - 72
    finally:
        window.configuration_dialog.close()
        window._worker_thread.quit()
        assert window._worker_thread.wait(2000), "GUI测试清理时工作线程未能退出"
        window._shutdown_finished = True
        window.close()
        qt_app.processEvents()


def test_port_dropdown_shows_description_and_connected_badge_keeps_selected_port(
    qt_app, monkeypatch, tmp_path
):
    from serial.tools import list_ports

    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [type("Port", (), {"device": "COM3", "description": "USB Serial Device"})()],
    )
    window = MainWindow()
    try:
        window._refresh_ports()
        assert window.port_combo.currentData() == "COM3"
        assert "USB Serial Device" in window.port_combo.currentText()

        window._on_connected(True, "已连接 · FLUKE,8845A,TEST,2.0")
        assert "COM3" in window.connection_badge.text()
    finally:
        window._on_connected(False, "未连接")
        window.close()
        qt_app.processEvents()


def test_disconnect_leaves_serial_parameters_and_offers_reconnect(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.port_combo.setEditText("COM3")
        window.baud_combo.setCurrentText("115200")
        window._on_connected(True, "已连接 · FLUKE,8845A,TEST,2.0")
        window._on_connected(False, "连接中断")

        assert window.port_combo.currentText() == "COM3"
        assert window.baud_combo.currentText() == "115200"
        assert window.connect_button.text() == "重新连接"
    finally:
        window.close()
        qt_app.processEvents()


def test_disabled_measurement_controls_explain_acquisition_lock(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._acquiring = True
        window._update_controls()

        assert not window.range_combo.isEnabled()
        assert "采集中" in window.range_combo.toolTip()
        assert not window.single_button.isEnabled()
        assert "采集中" in window.single_button.toolTip()
    finally:
        window._connected = False
        window._acquiring = False
        window.close()
        qt_app.processEvents()


def test_diagnostic_busy_state_prevents_duplicate_commands(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    sent = []
    monkeypatch.setattr(gui_module.QMessageBox, "information", lambda *_args, **_kwargs: None)
    try:
        window._connected = True
        window._control_mode = "computer"
        window._bridge.diagnose.disconnect(window._worker.diagnose)
        window._bridge.diagnose.connect(lambda *args: sent.append(args))

        window._diagnose()
        window._diagnose()

        assert len(sent) == 1
        assert not window.diagnose_button.isEnabled()
        assert "正在读取仪器诊断" in window.statusBar().currentMessage()
        window._show_diagnostic("通信正常")
        assert window.diagnose_button.isEnabled()
    finally:
        window._connected = False
        window.close()
        qt_app.processEvents()


def test_single_measurement_has_distinct_saved_result_feedback(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    sent = []
    directory = tmp_path / "single-run"
    try:
        window._connected = True
        window._simulation = True
        window._instrument_mode_known = True
        window._active_settings_by_mode[Mode.DCV.value] = {"mode": Mode.DCV.value}
        window.output_root.setText(str(tmp_path))
        window.experiment_name.setText("single")
        window._bridge.start_experiment.disconnect(window._worker.start_experiment)
        window._bridge.start_experiment.connect(lambda *args: sent.append(args))

        window._start_single()
        window._start_single()

        assert len(sent) == 1
        assert sent[0][-1] is True
        window._on_experiment_started(str(directory))
        window._on_sample([{
            "sample_index": 1, "elapsed_s": 0.25, "mode": Mode.DCV.value,
            "value_si": 1.25, "status": "ok", "unit_si": "V",
        }], 0.0, {"count": 1, "attempt_count": 1, "valid_count": 1, "abnormal_count": 0})
        window._on_experiment_finished(str(directory))
        window._on_experiment_save_result(str(directory), True, f"实验数据已保存：{directory}")

        feedback = window.statusBar().currentMessage()
        assert "单次测量完成" in feedback
        assert "1.25 V" in feedback
        assert str(directory / "measurements.csv") in feedback
    finally:
        window._connected = False
        window.close()
        qt_app.processEvents()


def test_stop_save_feedback_includes_valid_and_abnormal_sample_counts(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    directory = tmp_path / "continuous-run"
    try:
        window._global_stats.update({"attempt_count": 5, "valid_count": 4, "abnormal_count": 1})
        window._user_stop_pending = True
        window._on_experiment_finished(str(directory))
        window._on_experiment_save_result(str(directory), True, f"实验数据已保存：{directory}")

        feedback = window.statusBar().currentMessage()
        assert "停止并保存完成" in feedback
        assert "5 次" in feedback and "有效 4" in feedback and "异常 1" in feedback
        assert str(directory / "measurements.csv") in feedback
    finally:
        window.close()
        qt_app.processEvents()


def test_pause_button_stays_in_place_and_changes_to_resume(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._on_status("连续采集中")
        pause_geometry = window.pause_button.geometry()
        assert window.pause_button.text() == "暂停"
        window._on_status("采集已暂停")
        assert window.pause_button.text() == "继续"
        assert window.pause_button.geometry() == pause_geometry
    finally:
        window._acquiring = False
        window._paused = False
        window.close()
        qt_app.processEvents()


def test_event_saved_feedback_includes_time_sample_and_current_reading(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    sent = []
    try:
        window._on_sample([{
            "sample_index": 7, "elapsed_s": 1.25, "mode": Mode.DCV.value,
            "value_si": 2.5, "status": "ok", "unit_si": "V",
        }], 1.0, {"count": 1})
        window._experiment_active = True
        window._update_controls()
        window._bridge.mark_event.disconnect(window._worker.mark_event)
        window._bridge.mark_event.connect(lambda event: sent.append(event))
        window.event_edit.setText("接通负载")

        window._mark_event()

        assert len(sent) == 1
        assert sent[0] == "接通负载"
        assert window.event_edit.text() == "接通负载"
        assert not window._event_lines
        assert "事件已记录" not in window.statusBar().currentMessage()
        window._append_log("事件已记录：接通负载")
        assert "事件已记录" not in window.statusBar().currentMessage()

        window._worker.event_recorded.emit({
            "event": "接通负载", "timestamp_local": "2026-10-16T10:15:00+08:00",
            "elapsed_s": "1.25", "sample_index": 7, "value_si": "2.5", "unit_si": "V",
        })

        feedback = window.statusBar().currentMessage()
        assert "t=1.25 s" in feedback
        assert "样本 7" in feedback
        assert "2.5 V" in feedback
        assert "已保存" in feedback
        assert window.event_edit.text() == ""
        assert len(window._event_lines) == 1
    finally:
        window._experiment_active = False
        window.close()
        qt_app.processEvents()


def test_failed_event_write_keeps_text_without_showing_saved_feedback(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(gui_module.QMessageBox, "warning", lambda *args, **kwargs: None)
    window = MainWindow()
    sent = []
    try:
        window._experiment_active = True
        window._update_controls()
        window._bridge.mark_event.disconnect(window._worker.mark_event)
        window._bridge.mark_event.connect(sent.append)
        window.event_edit.setText("负载切换")

        window._mark_event()
        assert sent == ["负载切换"]
        assert window._event_save_pending
        assert window.event_edit.text() == "负载切换"
        assert "已保存" not in window.statusBar().currentMessage()

        window._show_error("事件保存失败：磁盘不可写")
        assert not window._event_save_pending
        assert window.event_edit.text() == "负载切换"
        assert not window._event_lines
        assert "事件保存失败" in window.statusBar().currentMessage()
        assert "已保存" not in window.statusBar().currentMessage()
    finally:
        window._experiment_active = False
        window.close()
        qt_app.processEvents()


def test_window_close_confirmation_explains_active_acquisition_will_stop(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    prompts = []
    answers = iter((gui_module.QMessageBox.StandardButton.No, gui_module.QMessageBox.StandardButton.Yes))

    def question(*args, **kwargs):
        prompts.append(args)
        return next(answers)

    monkeypatch.setattr(gui_module.QMessageBox, "question", question)
    window = MainWindow()
    history_cancellations = []
    window._history_load_task = SimpleNamespace(cancel=lambda: history_cancellations.append(True))
    try:
        window._connected = True
        window._acquiring = True
        window.show()
        qt_app.processEvents()

        assert not window.close()
        assert window.isVisible() and not window._shutdown_started
        assert history_cancellations == []
        assert "关闭桌面软件窗口将停止当前采集" in prompts[0][2]
        assert "单独运行的 localhost 网页服务不会因此关闭" in prompts[0][2]

        assert not window.close()
        assert history_cancellations == [True]
        deadline = time.monotonic() + 3.0
        while window._worker_thread.isRunning() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        assert not window._worker_thread.isRunning()
    finally:
        if window._worker_thread.isRunning():
            window._shutdown_finished = True
            window._worker.request_stop()
            window._bridge.close_app.emit()
            deadline = time.monotonic() + 2.0
            while window._worker_thread.isRunning() and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
        window._shutdown_finished = True
        window.close()
        qt_app.processEvents()


def test_cancelled_history_load_completes_without_showing_error_dialog(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    window._history_load_pending = True
    window._history_load_task = object()
    window.history_button.setEnabled(False)
    try:
        window._on_history_loaded("large.csv", None, "历史 CSV 加载已取消")
        assert not window._history_load_pending
        assert window.history_button.isEnabled()
        assert errors == []
    finally:
        window._history_load_task = None
        window.close()
        qt_app.processEvents()


def test_completed_history_load_result_is_discarded_after_shutdown_starts(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    information_calls = []
    monkeypatch.setattr(
        gui_module.QMessageBox, "information",
        lambda *args: information_calls.append(args),
    )
    window = MainWindow()
    history_task = object()
    window._history_load_pending = True
    window._history_load_task = history_task
    window.history_button.setEnabled(False)
    window._shutdown_started = True
    window.setEnabled(False)
    window.shutdown_status_label.setText("正在等待仪器操作结束并关闭连接…")
    window.shutdown_status_label.show()
    window.statusBar().showMessage("正在安全关闭…")
    original_plot = (list(window._plot_x), list(window._plot_y), window._history_mode)
    original_source = window._experiment_csv_path
    original_log = window.log_text.toPlainText()
    result = {
        "warnings": ["不应写入待关闭窗口的日志"],
        "x": [0.0, 1.0], "y": [1.0, 2.0], "metadata": [{}, {}],
        "break_before": [False, False], "events": [], "valid_count": 2,
        "first_elapsed": 0.0, "last_elapsed": 1.0, "last_value": 2.0,
        "mode": "DCV",
        "stats": {
            "count": 2, "minimum": 1.0, "maximum": 2.0, "mean": 1.5,
            "sample_stddev": 0.7071067811865476, "attempt_count": 2,
            "valid_count": 2, "abnormal_count": 0,
        },
    }
    try:
        window._on_history_loaded("history.csv", result, None)

        assert (list(window._plot_x), list(window._plot_y), window._history_mode) == original_plot
        assert window._experiment_csv_path == original_source
        assert window.log_text.toPlainText() == original_log
        assert information_calls == []
        assert window._shutdown_started and not window.isEnabled()
        assert window.shutdown_status_label.text() == "正在等待仪器操作结束并关闭连接…"
        assert window.statusBar().currentMessage() == "正在安全关闭…"
        assert window._history_load_pending is False
        assert window._history_load_task is None
    finally:
        if window._worker_thread.isRunning():
            window._shutdown_finished = True
            window._worker.request_stop()
            window._bridge.close_app.emit()
            deadline = time.monotonic() + 2.0
            while window._worker_thread.isRunning() and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
        window._shutdown_finished = True
        window.close()
        qt_app.processEvents()


def test_close_after_worker_stops_marks_shutdown_and_discards_queued_history_result(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    information_calls = []
    monkeypatch.setattr(
        gui_module.QMessageBox, "information",
        lambda *args: information_calls.append(args),
    )
    window = MainWindow()
    history_cancellations = []
    region_cancellations = []
    window._history_load_pending = True
    window._history_load_task = SimpleNamespace(
        cancel=lambda: history_cancellations.append(True),
    )
    window._region_stats_request_id = 7
    window._region_stats_pending_rows.append({"value_si": 1.0})
    window._region_stats_tasks[7] = SimpleNamespace(
        cancel=lambda: region_cancellations.append(True),
    )
    window.history_button.setEnabled(False)
    original_plot = (list(window._plot_x), list(window._plot_y), window._history_mode)
    original_log = window.log_text.toPlainText()
    result = {
        "warnings": ["不应写入已关闭窗口的日志"],
        "x": [0.0, 1.0], "y": [1.0, 2.0], "metadata": [{}, {}],
        "break_before": [False, False], "events": [], "valid_count": 2,
        "first_elapsed": 0.0, "last_elapsed": 1.0, "last_value": 2.0,
        "mode": "DCV",
        "stats": {
            "count": 2, "minimum": 1.0, "maximum": 2.0, "mean": 1.5,
            "sample_stddev": 0.7071067811865476, "attempt_count": 2,
            "valid_count": 2, "abnormal_count": 0,
        },
    }
    try:
        window.show()
        qt_app.processEvents()
        window._worker_thread.quit()
        assert window._worker_thread.wait(2000)
        assert not window._shutdown_finished  # finished() is still queued to the GUI thread

        assert window.close()
        assert not window.isVisible()
        assert history_cancellations == [True]
        assert region_cancellations == [True]
        assert window._region_stats_request_id == 8
        assert window._region_stats_pending_rows == []
        assert not window._clock_timer.isActive()
        assert window._shutdown_started

        # Model a history task completion that was already queued when close was accepted.
        window._on_history_loaded("late-history.csv", result, None)

        assert (list(window._plot_x), list(window._plot_y), window._history_mode) == original_plot
        assert information_calls == []
        assert window.log_text.toPlainText() == original_log
        assert not window.history_button.isEnabled()
        assert window._history_load_pending is False
        assert window._history_load_task is None

        status_before_late_exports = window.statusBar().currentMessage()
        window.export_events_safe_button.setEnabled(False)
        window._on_snapshot_exported("snapshot.csv", "snapshot.json", "now", 10, None)
        window._on_safe_events_exported("events-safe.csv", None)
        assert window.statusBar().currentMessage() == status_before_late_exports
        assert not window.export_events_safe_button.isEnabled()
    finally:
        if window._worker_thread.isRunning():
            window._worker_thread.quit()
            window._worker_thread.wait(2000)
        window._shutdown_finished = True
        window.close()
        qt_app.processEvents()


def test_shutdown_wait_state_is_visible_in_status_area(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        assert not window.close()
        assert window.shutdown_status_label.isVisible()
        assert "正在等待当前仪器操作结束" in window.shutdown_status_label.text()
        window._on_experiment_finished(str(tmp_path / "saved-run"))
        assert "采集数据已保存" in window.shutdown_status_label.text()
        assert "正在关闭仪器连接" in window.shutdown_status_label.text()
    finally:
        if window._worker_thread.isRunning():
            window._worker.request_stop()
            window._bridge.close_app.emit()
            deadline = time.monotonic() + 2.0
            while window._worker_thread.isRunning() and time.monotonic() < deadline:
                qt_app.processEvents()
                time.sleep(0.005)
        window._shutdown_finished = True
        window.close()
        qt_app.processEvents()


def test_chart_gesture_help_can_be_expanded_and_collapsed(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        assert window.chart_gesture_help.isHidden()
        window.chart_gesture_help_button.click()
        assert window.chart_gesture_help.isVisible()
        assert "Shift" in window.chart_gesture_help.text()
        assert "Ctrl" in window.chart_gesture_help.text()
        window.chart_gesture_help_button.click()
        assert window.chart_gesture_help.isHidden()
    finally:
        window.close()
        qt_app.processEvents()


def test_real_connection_displays_remote_front_panel_lock(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._on_connected(True, "已连接 · FLUKE,8845A,TEST,2.0")
        assert "REMOTE" in window.connection_badge.text()
        assert "除LOCAL键外" in window.connection_badge.toolTip()
        assert "RS-232接口协议" in window.control_mode_combo.toolTip()
        assert window.control_mode_combo.itemData(window.control_mode_combo.findText("Local · 面板操作")) == "local"
        window._on_control_mode_changed("local")
        assert "LOCAL" in window.connection_badge.text()
        assert not window.start_button.isEnabled()
        window._on_configuration({
            "idn": "FLUKE,8845A,TEST,2.0", "function": "", "input_terminals": "",
            "simulation": False,
        })
        assert "仪表设置待读回" in window.reading_mode_label.text()
        window._on_control_mode_changed("computer")
        window._on_configuration({
            "idn": "FLUKE,8845A,TEST,2.0", "function": "VOLT:DC", "input_terminals": "FRON",
            "simulation": False,
        })
        window._on_active_settings({
            "mode": "DCV", "autorange": False, "range_si": 10.0,
            "nplc": 10.0, "ac_bandwidth_hz": None,
            "analog_filter": False, "digital_filter": False, "autozero": True,
        })
        assert "REMOTE" in window.connection_badge.text()
        assert "直流电压 DCV" in window.reading_mode_label.text()
        assert "仪表已应用" in window.reading_mode_label.text()
        assert "10 NPLC" in window.reading_mode_label.text()

        window._on_status("就绪（REMOTE远程控制；除LOCAL外面板按键已禁用）")
        assert "REMOTE" in window.statusBar().currentMessage()
        assert "LOCAL" in window.experiment_status.toolTip()
    finally:
        window._on_connected(False, "未连接")
        window.close()
        qt_app.processEvents()


def test_minimum_window_size_does_not_clip_the_readout_or_analysis(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.resize(1040, 700)
        window.show()
        window._show_configuration()
        window.workspace_stack.setCurrentIndex(1)
        qt_app.processEvents()
        qt_app.processEvents()

        assert window.configuration_dialog.isVisible()
        assert window.measurement_workspace.height() >= window.measurement_workspace.minimumSizeHint().height()
        assert window.latest_value_label.height() >= window.latest_value_label.minimumSizeHint().height()
        assert not window.reading_mode_label.geometry().intersects(window.latest_value_label.geometry())
        assert window.plot_widget.height() >= window.plot_widget.minimumHeight()
        assert window.analysis_tabs.height() >= window.analysis_tabs.minimumHeight()
        panel_items = [
            window._chart_panel_layout.itemAt(index).widget().geometry()
            for index in range(window._chart_panel_layout.count())
            if window._chart_panel_layout.itemAt(index).widget() is not None
            and not window._chart_panel_layout.itemAt(index).widget().isHidden()
        ]
        assert all(
            current.y() + current.height() <= following.y()
            for current, following in zip(panel_items, panel_items[1:])
        )
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("scale_factor", [1.25, 1.5])
def test_high_dpi_layout_keeps_readout_chart_and_controls_separate(tmp_path, scale_factor):
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "QT_SCALE_FACTOR": str(scale_factor),
        "FLUKE8845A_CONFIG_DIR": str(tmp_path / "high-dpi-config"),
    })
    probe = textwrap.dedent(
        """
        import time
        import json
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from fluke8845a.gui import MainWindow

        app = QApplication([])
        window = MainWindow()
        window.resize(1280, 720)
        window.show()
        window._show_configuration()
        app.processEvents()
        app.processEvents()

        controls = (window.nplc_combo, window.apply_speed_button, window.interval_spin)
        window.workspace_stack.setCurrentIndex(1)
        app.processEvents()
        analysis_height = window.analysis_tabs.height()
        analysis_minimum_height = window.analysis_tabs.minimumHeight()
        window.workspace_stack.setCurrentIndex(0)
        app.processEvents()
        panel_items = []
        for index in range(window._chart_panel_layout.count()):
            widget = window._chart_panel_layout.itemAt(index).widget()
            if widget is not None and widget.isHidden():
                continue
            panel_items.append({
                "name": widget.objectName() if widget is not None else "",
                "class": widget.metaObject().className() if widget is not None else "spacer",
                "geometry": (
                    [widget.geometry().x(), widget.geometry().y(),
                     widget.geometry().width(), widget.geometry().height()]
                    if widget is not None else []
                ),
            })
        window._show_setup_section(2)
        app.processEvents()
        interval_visible = controls[2].isVisible()
        interval_fits = controls[2].height() >= controls[2].sizeHint().height()
        window._show_setup_section(1)
        app.processEvents()
        snapshot = {
            "device_pixel_ratio": window.devicePixelRatioF(),
            "reading_labels_overlap": window.reading_mode_label.geometry().intersects(
                window.latest_value_label.geometry()
            ),
            "plot_height": window.plot_widget.height(),
            "plot_minimum_height": window.plot_widget.minimumHeight(),
            "analysis_height": analysis_height,
            "analysis_minimum_height": analysis_minimum_height,
            "setup_scroll_area": window.setup_scroll_area.objectName(),
            "configuration_visible": window.configuration_dialog.isVisible(),
            "instrument_controls_visible": [control.isVisible() for control in controls[:2]],
            "instrument_controls_fit": [control.height() >= control.sizeHint().height() for control in controls[:2]],
            "interval_visible": interval_visible,
            "interval_fits": interval_fits,
            "panel_items": panel_items,
            "panel_items_ordered": all(
                current["geometry"][1] + current["geometry"][3] <= following["geometry"][1]
                for current, following in zip(panel_items, panel_items[1:])
            ),
        }
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(5000, app.quit)
        app.exec()
        snapshot["worker_shutdown"] = window._shutdown_finished
        print(json.dumps(snapshot, ensure_ascii=False))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, f"high-DPI probe failed:\n{result.stdout}\n{result.stderr}"
    snapshot = json.loads(result.stdout)
    assert snapshot["device_pixel_ratio"] == scale_factor
    assert not snapshot["reading_labels_overlap"]
    assert snapshot["plot_height"] >= snapshot["plot_minimum_height"]
    assert snapshot["analysis_height"] >= snapshot["analysis_minimum_height"]
    assert snapshot["setup_scroll_area"] == "configurationScrollArea"
    assert snapshot["configuration_visible"]
    assert all(snapshot["instrument_controls_visible"])
    assert all(snapshot["instrument_controls_fit"])
    assert snapshot["interval_visible"]
    assert snapshot["interval_fits"]
    assert snapshot["panel_items_ordered"], snapshot["panel_items"]
    assert snapshot["worker_shutdown"]


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
        assert f"全程统计 · 有效点 n={total_samples}" in window.stats_label.text()
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


def test_experiment_finished_clears_acquisition_after_unrecognized_failure_status(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = True
        window._control_mode = "computer"
        window._acquiring = True
        window._paused = False
        window._experiment_active = True
        window._update_controls()
        assert window.stop_button.isEnabled()
        assert not window.start_button.isEnabled()

        window._on_experiment_finished(str(tmp_path / "failed-experiment"))
        window._on_status("实验文件关闭或元数据写入失败：disk full")

        assert not window._acquiring
        assert not window._paused
        assert not window._experiment_active
        assert window.start_button.isEnabled()
        assert not window.stop_button.isEnabled()
    finally:
        window._connected = False
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
    monkeypatch.setattr(
        gui_module.QMessageBox, "question",
        lambda *_args, **_kwargs: gui_module.QMessageBox.StandardButton.Yes,
    )
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


def test_local_statistics_interval_accepts_typed_bounds_and_syncs_chart(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window.region_start_spin.setValue(2.5)
        window.region_end_spin.setValue(8.0)
        window.region_apply_button.click()
        qt_app.processEvents()

        assert tuple(window._region.getRegion()) == pytest.approx((2.5, 8.0))
        assert window.region_check.isChecked()
        assert window.region_start_spin.value() == pytest.approx(2.5)
        assert window.region_end_spin.value() == pytest.approx(8.0)

        window._region.setRegion((10.0, 20.0))
        qt_app.processEvents()
        assert window.region_start_spin.value() == pytest.approx(10.0)
        assert window.region_end_spin.value() == pytest.approx(20.0)

        window.region_start_spin.setValue(20.0)
        window.region_end_spin.setValue(20.0)
        previous_region = tuple(window._region.getRegion())
        window.region_apply_button.click()
        assert tuple(window._region.getRegion()) == pytest.approx(previous_region)
        assert "起点必须早于终点" in window.statusBar().currentMessage()
    finally:
        window.close()
        qt_app.processEvents()


def test_locked_curve_point_stays_fixed_when_mouse_preview_moves(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((10.0, 20.0, 30.0))
        window.curve.setData(list(window._plot_x), list(window._plot_y))
        window.plot_widget.setRange(xRange=(0.0, 2.0), yRange=(0.0, 30.0), padding=0)
        qt_app.processEvents()

        vb = window.plot_widget.getPlotItem().vb
        scene_pos = vb.mapViewToScene(QPointF(1.0, 20.0))
        QTest.mouseClick(
            window.plot_widget.viewport(), Qt.MouseButton.LeftButton,
            pos=window.plot_widget.mapFromScene(scene_pos),
        )
        qt_app.processEvents()
        locked_point = window._selected_plot_point
        assert locked_point == (1.0, 20.0, 2)
        window._on_mouse_moved(vb.mapViewToScene(QPointF(2.0, 30.0)))
        assert window._selected_plot_point == locked_point
        assert "预览" in window.cursor_label.text()
        assert "已锁定" in window.cursor_label.text()

        window._last_value = 999.0
        window._set_selected_point_as_baseline()
        assert window._baseline == 20.0
        assert window._locked_cursor_marker.isVisible()
    finally:
        window.close()
        qt_app.processEvents()


def test_zero_controls_explain_one_shot_and_automatic_zero(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert "不会把被测输入的读数强制改成 0" in window.autozero_check.toolTip()
        assert "不会把被测输入的读数强制改成 0" in window.autozero_label.toolTip()
        assert "ZERO:AUTO ONCE" in window.zero_button.toolTip()
        assert "不会把当前显示值清零" in window.zero_button.toolTip()
        assert "不是校准调整" in window.zero_button.toolTip()
        assert not hasattr(window, "zero_explanation")
    finally:
        window.close()
        qt_app.processEvents()


def test_control_mode_has_hover_help_and_local_disables_instrument_controls(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert "Computer" in window.control_mode_combo.toolTip()
        assert "Local" in window.control_mode_combo.toolTip()
        assert "RS-232" in window.control_mode_combo.toolTip()
        assert "RS-232" in window.control_mode_label.toolTip()

        window._connected = True
        window._control_mode = "local"
        local_help = window._control_mode_tooltip()
        assert "无法回读" in local_help
        assert "切回Computer" in local_help
        window._update_controls()
        assert window.control_mode_combo.isEnabled()
        assert window.set_control_mode_button.isEnabled()
        for widget in (
            window.mode_combo, window.apply_button, window.initialize_parameters_button,
            window.zero_button, window.start_button, window.single_button,
        ):
            assert not widget.isEnabled()

        window._control_mode = "computer"
        window._update_controls()
        assert window.mode_combo.isEnabled()
        assert window.apply_button.isEnabled()
        assert window.zero_button.isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_control_mode_switch_clears_local_readback_then_reports_computer_readback(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "control-mode-readback-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = False
        window._current_mode = Mode.RES2
        window._active_settings_by_mode[Mode.RES2.value] = {
            "mode": Mode.RES2.value, "autorange": True, "range_si": None, "nplc": 1.0,
        }
        window._on_configuration({"idn": "FLUKE,8845A,9383035,1.0.0", "function": "", "simulation": False})
        window._on_control_mode_changed("local")

        assert window._active_settings_by_mode == {}
        assert "Local" in window.instrument_readback_status.text()
        assert "无法回读" in window.instrument_readback_status.toolTip()

        window._on_configuration({
            "idn": "FLUKE,8845A,9383035,1.0.0", "function": "RES", "simulation": False,
        })
        window._on_control_mode_changed("computer")
        assert "等待" in window.instrument_readback_status.text()
        window._on_active_settings({
            "mode": Mode.RES2.value, "autorange": True, "range_si": None, "nplc": 1.0,
        })
        assert "已回读" in window.instrument_readback_status.text()
    finally:
        window.close()
        qt_app.processEvents()


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
        assert window.event_edit.text() == "环境温度调整"
        assert len(window._event_lines) == 2
        window._on_event_saved({
            "event": "环境温度调整", "timestamp_local": "2026-10-16T10:15:00+08:00",
            "elapsed_s": "1.25", "sample_index": 0, "value_si": "29977800",
            "unit_si": "Ω",
        })
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


def test_initialize_parameters_restores_mode_defaults_without_resetting_instrument(
    qt_app, monkeypatch, tmp_path
):
    config_dir = tmp_path / "app-config"
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(
        gui_module.QMessageBox,
        "question",
        lambda *_args, **_kwargs: gui_module.QMessageBox.StandardButton.Yes,
    )
    window = MainWindow()
    configure_requests = []
    window._bridge.configure.connect(configure_requests.append)
    output_directory = tmp_path / "experiment-output"
    window.output_root.setText(str(output_directory))
    window.baud_combo.setCurrentText("115200")
    window.interval_spin.setValue(2.75)
    window._simulation = True
    for mode in Mode:
        profile = window._profiles[mode.value]
        spec = DESCRIPTORS[mode]
        profile.update({
            "autorange": False,
            "range_si": spec.ranges_si[-1],
            "nplc": spec.nplc_values[-1] if spec.nplc_values else None,
            "ac_bandwidth_hz": spec.ac_bandwidths_hz[-1] if spec.ac_bandwidths_hz else None,
            "analog_filter": bool(spec.analog_filter),
            "autozero": False,
            "baseline": 12.5,
        })
    window._current_mode = Mode.DCV
    window._load_mode_controls(Mode.DCV)
    window._baseline = 12.5
    window.baseline_line.show()
    try:
        assert window.initialize_parameters_button.isEnabled()
        window.initialize_parameters_button.click()

        for mode in Mode:
            assert window._profiles[mode.value] == window._default_mode_profile(mode)
        assert window.interval_spin.value() == 1.0
        assert window.baud_combo.currentText() == "115200"
        assert window.output_root.text() == str(output_directory)
        assert window._baseline is None
        assert window.baseline_edit.text() == ""
        assert not window.baseline_line.isVisible()
        assert configure_requests == []

        saved = json.loads(window._profile_path.read_text(encoding="utf-8"))
        assert saved["serial"]["baudrate"] == "115200"
        assert saved["sampling_interval_s"] == 1.0
        assert all(saved[mode.value] == window._default_mode_profile(mode) for mode in Mode)
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("firmware", "terminals", "stored_range", "unsupported_ranges"),
    [
        ("1.0", "FRON", 0.4, (0.4,)),
        ("2.0", "REAR", 1.0, (1.0, 3.0, 10.0)),
    ],
)
def test_known_unsupported_current_autorange_and_ranges_are_disabled_in_gui(
    qt_app, monkeypatch, tmp_path, firmware, terminals, stored_range, unsupported_ranges
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = False
        window._firmware = firmware
        window._terminals = terminals
        window._connected = True
        window._control_mode = "computer"
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
            tooltip = window.range_combo.model().item(index).toolTip()
            assert "原因：" in tooltip
            assert "下一步：" in tooltip
        assert "原因：" in window.autorange_check.toolTip()
        assert "下一步：" in window.autorange_check.toolTip()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("firmware", "terminals", "cause", "next_step"),
    [
        ("1.9.0.0", "FRONT", "400 mA", "OutG SW"),
        ("2.1.0.0", "REAR", "REAR", "前面板电流端子"),
    ],
)
def test_current_range_limit_is_visible_with_cause_and_next_step(
    qt_app, monkeypatch, tmp_path, firmware, terminals, cause, next_step
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / f"range-limit-{terminals}"))
    window = MainWindow()
    try:
        window._simulation = False
        window._firmware = firmware
        window._terminals = terminals
        window._current_mode = Mode.DCI
        window._load_mode_controls(Mode.DCI)

        assert not window.range_limit_hint.isHidden()
        assert cause in window.range_limit_hint.text()
        assert next_step in window.range_limit_hint.text()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("firmware", "terminals", "disabled_ranges", "enabled_ranges", "cause", "next_step"),
    [
        (
            "1.0.700.17", "FRONT", (0.4,), (0.1, 1.0),
            "1.0.700.17", "OutG SW",
        ),
        (
            "1.0.700.18", "REAR", (1.0, 3.0, 10.0), (0.4,),
            "REAR", "前面板电流插孔",
        ),
    ],
)
def test_aci_current_range_limit_shows_cause_and_next_step(
    qt_app, monkeypatch, tmp_path, firmware, terminals, disabled_ranges, enabled_ranges,
    cause, next_step,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / f"aci-range-limit-{terminals}"))
    window = MainWindow()
    try:
        window._simulation = False
        window._connected = False
        window._device_serial = "ACI-RANGE-LIMIT-TEST"
        window._idn_firmware = ""
        window._terminals = terminals
        window._set_firmware_fields(firmware, "")
        window._firmware_version_changed()
        window._current_mode = Mode.ACI
        mode_combo_signals_blocked = window.mode_combo.blockSignals(True)
        window._set_combo_data(window.mode_combo, Mode.ACI.value)
        window.mode_combo.blockSignals(mode_combo_signals_blocked)

        window._load_mode_controls(Mode.ACI)

        assert window.mode_combo.currentData() == Mode.ACI.value
        assert not window.range_limit_hint.isHidden()
        assert cause in window.range_limit_hint.text()
        assert next_step in window.range_limit_hint.text()
        if firmware == "1.0.700.17":
            assert "ACI 要求 1.0.700.18" in window.range_limit_hint.text()
            assert "不超过100 mA" in window.range_limit_hint.text()
        for value in disabled_ranges:
            index = window.range_combo.findData(value)
            assert index >= 0
            item = window.range_combo.model().item(index)
            assert not item.isEnabled()
            assert "原因：" in item.toolTip()
            assert "下一步：" in item.toolTip()
        for value in enabled_ranges:
            index = window.range_combo.findData(value)
            assert index >= 0
            assert window.range_combo.model().item(index).isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_date_coded_idn_firmware_keeps_400ma_and_autorange_available_in_gui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = False
        window._firmware = "02/14/08-08:54"
        window._terminals = "FRONT"
        window._current_mode = Mode.DCI
        window._profiles[Mode.DCI.value].update({"autorange": True, "range_si": 0.4})

        window._load_mode_controls(Mode.DCI)

        assert window.autorange_check.isEnabled()
        assert window.autorange_check.isChecked()
        index = window.range_combo.findData(0.4)
        assert index >= 0
        assert window.range_combo.model().item(index).isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_front_panel_firmware_profile_is_restored_by_serial_and_controls_only_400ma(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._firmware_profiles["9383035"] = {
            "outguard_sw": "2.10.882.24", "inguard_sw": "ad300210",
        }
        window._on_configuration({
            "idn": "FLUKE,8845A,9383035,02/14/08-08:54",
            "function": "CURR:DC", "input_terminals": "FRONT", "simulation": False,
        })
        assert window._firmware == "2.10.882.24"
        assert window.outguard_sw_edit.text() == "2.10.882.24"
        assert window.inguard_sw_edit.text() == "ad300210"

        window._connected = True
        window._load_mode_controls(Mode.DCI)
        assert window.range_combo.model().item(window.range_combo.findData(0.4)).isEnabled()
        assert window.outguard_sw_edit.isEnabled()

        window.outguard_sw_edit.setText("1.9.0.0")
        window._firmware_version_changed()
        saved = json.loads(window._profile_path.read_text(encoding="utf-8"))
        saved_firmware = saved["instrument_firmware_by_serial"]["9383035"]
        assert saved_firmware["outguard_sw"] == "1.9.0.0"
        assert saved_firmware["inguard_sw"] == "ad300210"
        assert saved_firmware["source"] == "前面板人工核对"
        assert saved_firmware["updated_at"]
        assert not window.range_combo.model().item(window.range_combo.findData(0.4)).isEnabled()
        assert window.range_combo.model().item(window.range_combo.findData(0.1)).isEnabled()
        assert window.range_combo.model().item(window.range_combo.findData(10.0)).isEnabled()
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

        assert window.nplc_combo.isHidden() is (not bool(spec.nplc_values))
        assert window.nplc_combo.isEnabled() is bool(spec.nplc_values)
        if spec.nplc_values:
            assert window.nplc_combo.count() == len(spec.nplc_values)
            assert "NPLC" in window.measurement_speed_help.text()
        assert window.bandwidth_combo.isHidden() is (not bool(spec.ac_bandwidths_hz))
        assert window.bandwidth_combo.isEnabled() is bool(spec.ac_bandwidths_hz)
        if spec.ac_bandwidths_hz:
            assert window.bandwidth_combo.count() == len(spec.ac_bandwidths_hz)
            assert "带宽" in window.measurement_speed_help.text()
        assert window.analog_filter_check.isHidden() is (not spec.analog_filter)
        assert window.digital_filter_check.isHidden() is (not spec.digital_filter)
        if spec.digital_filter:
            assert not window.digital_filter_check.isEnabled()
            window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(10))
            assert not window.digital_filter_check.isEnabled()
            assert window._settings_for_mode(mode).digital_filter is False
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("firmware", ["1.9.0.0", ""])
def test_digital_filter_gui_stays_disabled_until_read_timeout_is_validated(
    qt_app, monkeypatch, tmp_path, firmware
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = False
        window._firmware = firmware
        window._current_mode = Mode.DCV
        window._profiles[Mode.DCV.value].update({"nplc": 10, "digital_filter": False})

        window._load_mode_controls(Mode.DCV)

        assert not window.digital_filter_check.isEnabled()
        if firmware:
            assert "2.0" in window.digital_filter_check.toolTip()
        else:
            assert "OutG SW" in window.digital_filter_check.toolTip()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("firmware", ["", "1.9.0.0"])
def test_unverified_real_outguard_requires_verified_version_for_slow_nplc(
    qt_app, monkeypatch, tmp_path, firmware
):
    from fluke8845a.scpi import digital_filter_slow_read_block_reason

    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = False
        window._firmware = firmware
        window._current_mode = Mode.DCV
        window._profiles[Mode.DCV.value]["nplc"] = 10

        window._load_mode_controls(Mode.DCV)
        slow_index = window.nplc_combo.findData(10)

        assert slow_index >= 0
        assert window.nplc_combo.currentData() == 10
        assert not window.nplc_combo.model().item(slow_index).isEnabled()
        assert window.digital_filter_check.toolTip() == digital_filter_slow_read_block_reason(
            Mode.DCV, 10, firmware
        )

        window._firmware = "2.10.882.24"
        window._update_digital_filter_availability()

        assert window.nplc_combo.model().item(slow_index).isEnabled()
    finally:
        window._connected = False
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


def test_measurement_settings_have_separate_effective_and_pending_columns(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "settings-columns-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._current_mode = Mode.RES2
        window._load_mode_controls(Mode.RES2)
        window._on_active_settings({
            "mode": Mode.RES2.value, "autorange": False, "range_si": 100.0,
            "nplc": 1.0, "analog_filter": False, "autozero": True,
        })

        assert "两线电阻" in window.settings_active_summary.text()
        assert "100 Ω" in window.settings_active_summary.text()
        assert "1 NPLC" in window.settings_active_summary.text()
        assert "当前选择" in window.settings_pending_summary.text() or "待应用" in window.settings_pending_summary.text()

        window.autorange_check.setChecked(False)
        window.range_combo.setCurrentIndex(window.range_combo.findData(100.0))
        window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(10))

        assert "100 Ω" in window.settings_active_summary.text()
        assert "10 NPLC" in window.settings_pending_summary.text()
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
        window._show_configuration()
        qt_app.processEvents()

        assert window.setup_scroll_area.objectName() == "configurationScrollArea"
        window._show_setup_section(1)
        qt_app.processEvents()
        for control in (window.nplc_combo, window.apply_speed_button):
            assert control.height() >= control.sizeHint().height()
            assert control.isVisible()
        window._show_setup_section(2)
        qt_app.processEvents()
        assert window.interval_spin.height() >= window.interval_spin.sizeHint().height()
        assert window.interval_spin.isVisible()
        assert window.plot_widget.height() >= 120
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("modifiers", "expected"),
    [
        (gui_module.Qt.KeyboardModifier.NoModifier, (False, True)),
        (gui_module.Qt.KeyboardModifier.ShiftModifier, (True, False)),
        (gui_module.Qt.KeyboardModifier.ControlModifier, (True, True)),
        (gui_module.Qt.KeyboardModifier.AltModifier, (False, True)),
        (gui_module.Qt.KeyboardModifier.AltModifier | gui_module.Qt.KeyboardModifier.ShiftModifier, (False, True)),
    ],
)
def test_wheel_zoom_modifier_mapping(modifiers, expected):
    assert gui_module._wheel_zoom_axes(modifiers) == expected


def test_manual_vertical_zoom_keeps_live_follow_but_time_zoom_stops_it(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.follow_check.setChecked(True)
        window._on_plot_wheel_zoom(False, True)
        assert window.follow_check.isChecked()
        window._on_plot_wheel_zoom(True, False)
        assert not window.follow_check.isChecked()
    finally:
        window.close()
        qt_app.processEvents()


def test_live_follow_guidance_explains_axis_behavior_and_highlights_return(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        guidance = f"{window.acquisition_hint.text()} {window.chart_gesture_help.text()}"
        assert "仅缩放读数轴不会停止自动跟随" in guidance
        assert "缩放或平移时间轴会停止自动跟随" in guidance
        assert "回到实时窗口" in guidance
        assert window.return_live_button.objectName() == "primaryButton"
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
        (Mode.DCV, 10.0, "HI/LO"),
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
        assert DESCRIPTORS[mode].safety in dialogs[0].instructions
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


def test_switching_between_current_modes_does_not_instruct_moving_leads_to_voltage_jacks(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    dialogs = []

    def accept_dialog(dialog):
        dialogs.append(dialog)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gui_module.WiringConfirmationDialog, "exec", accept_dialog)
    try:
        window._current_mode = Mode.DCI
        old_block = window.mode_combo.blockSignals(True)
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(Mode.DCI.value))
        window.mode_combo.blockSignals(old_block)
        window._load_mode_controls(Mode.DCI)

        window.mode_combo.setCurrentIndex(window.mode_combo.findData(Mode.ACI.value))

        assert len(dialogs) == 1
        instructions = dialogs[0].instructions
        assert "关闭回路电源" in instructions
        assert "串联" in instructions
        assert "移回V/Ω插孔" not in instructions
        assert "mA/400 mA孔" in instructions
        assert "10 A孔" in instructions
        assert window._current_mode is Mode.ACI
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
    fields = ("experiment_id", "sample_index", "elapsed_s", "mode", "unit_si", "value_si", "status", "error")
    original = [
        {"experiment_id": "exp-1", "sample_index": index, "elapsed_s": elapsed, "mode": "RES2", "unit_si": "Ohm", "value_si": value, "status": "ok", "error": ""}
        for index, (elapsed, value) in enumerate(zip(range(5), (29_970_000, 29_980_000, 29_990_000, 30_000_000, 30_010_000)), 1)
    ]
    original.insert(2, {"experiment_id": "exp-1", "sample_index": 3, "elapsed_s": 1.5, "mode": "RES2", "unit_si": "Ohm", "value_si": "", "status": "timeout", "error": "timeout"})
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(original)
    before = source.read_bytes()
    events_source = tmp_path / "events.csv"
    with events_source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("experiment_id", "event_index", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerows([
            {"experiment_id": "exp-1", "event_index": 1, "elapsed_s": "1.5", "event": "接通负载"},
            {"experiment_id": "exp-1", "event_index": 2, "elapsed_s": "4", "event": "移除刺激"},
            {"experiment_id": "exp-1", "event_index": 3, "elapsed_s": "6", "event": "不在本图范围"},
            {"experiment_id": "exp-1", "event_index": 4, "elapsed_s": "nan", "event": "无效时间"},
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
        assert window._history_load_pending
        _wait_history_load(window)

        plot_x, plot_y = window.curve.getData()
        assert all(math.isfinite(float(value)) for value in plot_x)
        assert all(left <= right for left, right in zip(plot_x, plot_x[1:]))
        assert list(plot_x[[0, 2, 3]]) == [0.0, 2.0, 4.0]
        assert list(plot_y[[0, 2, 3]]) == [29_970_000.0, 29_990_000.0, 30_010_000.0]
        assert plot_x[1] == plot_x[2] == 2.0
        assert math.isnan(plot_y[1])
        assert window._last_value == 30_010_000.0
        assert "30.01 MΩ" in window.latest_value_label.text()
        assert window.actual_rate_label.text() == "1 s"
        assert "n=5" in window.stats_label.text()
        assert source.read_bytes() == before
        assert events_source.read_bytes() == events_before
        assert [line.value() for line in window._event_lines] == [1.5, 4.0]
        assert [label.toPlainText() for label in window._event_labels] == ["接通负载", "移除刺激"]
        window._baseline = 30_000_000.0
        window.baseline_edit.setText("30000000")
        screenshot_context = " ".join(window._screenshot_context_lines())
        assert "RES2" in screenshot_context
        assert "Ω" in screenshot_context
        assert "history" in screenshot_context
        assert "基准线" in screenshot_context
        assert "时间范围" in screenshot_context

        monkeypatch.setattr(gui_module.QFileDialog, "getSaveFileName", lambda *_args: (str(screenshot), "PNG"))
        window._save_screenshot()
        qt_app.processEvents()
        assert screenshot.exists() and screenshot.stat().st_size > 0
        saved_image = gui_module.QPixmap(str(screenshot))
        assert saved_image.height() > window.plot_widget.height()

        with events_source.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=("experiment_id", "event_index", "elapsed_s", "event"))
            writer.writeheader()
            writer.writerow({"experiment_id": "another-experiment", "event_index": 1, "elapsed_s": "1.5", "event": "不应关联"})
        window._load_history()
        _wait_history_load(window)
        assert not window._event_lines
        assert "experiment_id 与测量 CSV 不一致" in window.log_text.toPlainText()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("fieldnames", "rows", "message"),
    [
        (("sample_index", "value_si", "status"), ({"sample_index": "1", "value_si": "2", "status": "ok"},), "缺少列.*elapsed_s"),
        (("elapsed_s", "value_si", "status", "mode", "unit_si"), (
            {"elapsed_s": "0", "value_si": "2", "status": "ok", "mode": "DCV", "unit_si": "V"},
            {"elapsed_s": "1", "value_si": "3", "status": "ok", "mode": "RES2", "unit_si": "Ohm"},
        ), "混合测量模式"),
        (("elapsed_s", "value_si", "status", "mode", "unit_si"), (
            {"elapsed_s": "0", "value_si": "2", "status": "ok", "mode": "DCV", "unit_si": "V"},
            {"elapsed_s": "1", "value_si": "3", "status": "ok", "mode": "DCV", "unit_si": "mV"},
        ), "混合单位"),
        (("elapsed_s", "value_si", "status", "error"), (
            {"elapsed_s": "0", "value_si": "", "status": "timeout", "error": "串口超时"},
        ), "没有有效读数.*timeout.*检查"),
    ],
)
def test_history_import_explains_missing_columns_mixed_measurements_and_empty_data(
    qt_app, monkeypatch, tmp_path, fieldnames, rows, message
):
    source = tmp_path / "invalid-history.csv"
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(gui_module.QFileDialog, "getOpenFileName", lambda *_args: (str(source), "CSV"))
    window = MainWindow()
    errors = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    try:
        window._load_history()
        _wait_history_load(window)
        assert len(errors) == 1
        assert re.search(message, errors[0], re.IGNORECASE)
    finally:
        window.close()
        qt_app.processEvents()


def test_invalid_live_readings_split_plot_segments_but_not_valid_cache(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.DCV
        stats = {
            "count": 2, "valid_count": 2, "attempt_count": 3, "abnormal_count": 1,
            "minimum": 10.0, "maximum": 30.0, "mean": 20.0, "sample_stddev": 14.0,
        }
        window._on_sample([
                {"status": "ok", "sample_index": 1, "value_si": 10.0, "elapsed_s": 0.0, "raw_reading": "10", "mode": Mode.DCV.value},
            {"status": "timeout", "sample_index": 2, "value_si": "", "elapsed_s": 1.0, "error": "串口超时"},
                {"status": "ok", "sample_index": 3, "value_si": 30.0, "elapsed_s": 2.0, "raw_reading": "30", "mode": Mode.DCV.value},
        ], 0.0, stats)

        assert list(window._plot_x) == [0.0, 2.0]
        assert list(window._plot_y) == [10.0, 30.0]
        plot_x, plot_y = window.curve.getOriginalDataset()
        assert list(plot_x)[0] == 0.0 and list(plot_x)[-1] == 2.0
        assert all(math.isfinite(float(value)) for value in plot_x)
        assert all(left <= right for left, right in zip(plot_x, plot_x[1:]))
        assert any(math.isnan(float(value)) for value in plot_y)
        assert window.curve.opts["downsampleMethod"] == "peak"
    finally:
        window.close()
        qt_app.processEvents()


def test_live_plot_marks_a_gap_when_gui_mailbox_skipped_sample_indices(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    stats = {
        "count": 2, "valid_count": 2, "attempt_count": 4, "abnormal_count": 0,
        "minimum": 1.0, "maximum": 4.0, "mean": 2.5, "sample_stddev": 2.12,
    }
    try:
        window._current_mode = Mode.DCV
        window._on_sample([{
            "status": "ok", "sample_index": 1, "value_si": 1.0,
            "elapsed_s": 0.0, "raw_reading": "1", "mode": Mode.DCV.value,
        }], 0.0, stats)
        window._on_sample([{
            "status": "ok", "sample_index": 4, "value_si": 4.0,
            "elapsed_s": 3.0, "raw_reading": "4", "mode": Mode.DCV.value,
        }], 0.0, stats)

        _plot_x, plot_y = window.curve.getOriginalDataset()
        assert any(math.isnan(float(value)) for value in plot_y)
        assert list(window._plot_break_before) == [False, True]
    finally:
        window.close()
        qt_app.processEvents()


def test_gui_key_status_text_colors_meet_wcag_normal_text_contrast(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        stylesheet = window.styleSheet()
        selectors = {
            "QLabel#latestInvalidLabel": "#303b3e",
            "QLabel#recoveryHintLabel": "#303b3e",
        }

        def luminance(color: str) -> float:
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        for selector, background in selectors.items():
            rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", stylesheet)
            assert rule, f"stylesheet rule missing for {selector}"
            foreground = re.search(r"color:\s*(#[0-9a-fA-F]{6})", rule.group(1))
            assert foreground, f"foreground color missing for {selector}"
            l1, l2 = sorted((luminance(foreground.group(1)), luminance(background)), reverse=True)
            assert (l1 + 0.05) / (l2 + 0.05) >= 4.5, selector
    finally:
        window.close()
        qt_app.processEvents()


def test_apply_settings_status_reports_command_and_readback_phases(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    emitted = []
    original_bridge = window._bridge
    try:
        window._connected = True
        window._instrument_mode_known = True
        window._bridge = SimpleNamespace(configure=SimpleNamespace(emit=emitted.append))

        window._apply_settings()

        assert emitted and "正在发送仪器配置命令" in window.statusBar().currentMessage()
        window._on_active_settings({"mode": Mode.DCV.value, "autorange": True})
        assert "配置回读完成" in window.statusBar().currentMessage()
        assert "预计" not in window.statusBar().currentMessage()
    finally:
        window._bridge = original_bridge
        window.close()
        qt_app.processEvents()


def test_long_operation_status_persists_progress_and_expected_result(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(gui_module.QMessageBox, "question", lambda *args: gui_module.QMessageBox.StandardButton.Yes)
    window = MainWindow()
    original_bridge = window._bridge
    configure_requests = []
    zero_requests = []
    stop_requests = []
    directory = tmp_path / "continuous-run"
    try:
        assert window.operation_status_label.text() == "操作：就绪"
        window._connected = True
        window._instrument_mode_known = True
        window._bridge = SimpleNamespace(
            configure=SimpleNamespace(emit=configure_requests.append),
            zero=SimpleNamespace(emit=lambda: zero_requests.append(True)),
            stop=SimpleNamespace(emit=lambda: stop_requests.append(True)),
        )

        window._apply_settings()
        assert configure_requests
        assert "正在应用" in window.operation_status_label.text()
        assert "配置回读" in window.operation_status_label.toolTip()
        window._on_active_settings({"mode": Mode.DCV.value, "autorange": True})
        assert "配置已回读" in window.operation_status_label.text()

        window._request_zero()
        assert zero_requests
        assert "ZERO执行中" in window.operation_status_label.text()
        assert "预期" in window.operation_status_label.toolTip()
        window._on_zero_result("ZERO配置已回读；此回读不代表ZERO测量结果")
        assert "ZERO结果已返回" in window.operation_status_label.text()
        assert "ZERO配置已回读" in window.operation_status_label.toolTip()

        window._acquiring = True
        monkeypatch.setattr(window._worker, "request_stop", lambda: None)
        window._stop()
        assert stop_requests
        assert "正在停止并保存" in window.operation_status_label.text()
        assert "measurements.csv" in window.operation_status_label.toolTip()
        window._global_stats.update({"attempt_count": 5, "valid_count": 4, "abnormal_count": 1})
        window._on_experiment_save_result(str(directory), True, f"实验数据已保存：{directory}")
        assert "停止并保存完成" in window.operation_status_label.text()
        assert "有效 4" in window.operation_status_label.text()
        assert str(directory / "measurements.csv") in window.operation_status_label.toolTip()
    finally:
        window._bridge = original_bridge
        window._connected = False
        window.close()
        qt_app.processEvents()


def test_live_plot_peak_downsampling_preserves_spike_and_clips_to_view(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.resize(1100, 760)
        window.show()
        count = 50_000
        peak_index = 25_001
        window._plot_x.extend(float(index) for index in range(count))
        window._plot_y.extend(100.0 if index == peak_index else 0.0 for index in range(count))
        window._refresh_curve()
        window.plot_widget.setRange(xRange=(0, count - 1), yRange=(-1, 110), padding=0)
        qt_app.processEvents()

        all_x, all_y = window.curve.getData()
        assert len(all_x) < count
        assert max(all_y) == 100.0
        assert window.curve.opts["downsampleMethod"] == "peak"

        window.plot_widget.setRange(xRange=(10_000, 10_100), yRange=(-1, 1), padding=0)
        qt_app.processEvents()
        visible_x, visible_y = window.curve.getData()
        assert len(visible_x) < len(all_x)
        assert all(9_990 <= value <= 10_110 for value in visible_x)
        assert max(visible_y) == 0.0
        assert len(window._plot_x) == count
        assert len(window._plot_y) == count
    finally:
        window.close()
        qt_app.processEvents()


def test_clicking_event_list_item_centers_plot_on_that_event(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 10.0, 20.0, 30.0))
        window._plot_y.extend((1.0, 2.0, 3.0, 4.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.plot_widget.setRange(xRange=(0.0, 30.0), yRange=(0.0, 5.0), padding=0)
        window._add_event_marker("恢复过程", elapsed_s=20.0)
        item = window.event_list_widget.item(0)

        window.event_list_widget.itemClicked.emit(item)

        left, right = window.plot_widget.getPlotItem().vb.viewRange()[0]
        assert left <= 20.0 <= right
        assert not window.follow_check.isChecked()
        assert "恢复过程" in item.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_keyboard_selects_points_and_sets_selected_baseline(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((10.0, 20.0, 30.0))
        window._refresh_curve()
        window.plot_widget.setFocus()
        qt_app.processEvents()

        QTest.keyClick(window.plot_widget, Qt.Key.Key_Right)
        assert window._selected_plot_point == (0.0, 10.0, 1)
        QTest.keyClick(window.plot_widget, Qt.Key.Key_Right)
        assert window._selected_plot_point == (1.0, 20.0, 2)
        QTest.keyClick(window.plot_widget, Qt.Key.Key_Return)

        assert window._baseline == 20.0
        assert window.baseline_line.isVisible()
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_keyboard_pans_and_zoom_axes_and_focus_style_is_visible(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 100.0, 200.0))
        window._plot_y.extend((1.0, 10.0, 100.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.plot_widget.setRange(xRange=(20.0, 180.0), yRange=(5.0, 80.0), padding=0)
        window.plot_widget.setFocus()
        qt_app.processEvents()
        initial_x, initial_y = window.plot_widget.getPlotItem().vb.viewRange()

        QTest.keyClick(window.plot_widget, Qt.Key.Key_PageUp)
        zoomed_x, zoomed_y = window.plot_widget.getPlotItem().vb.viewRange()
        assert zoomed_x[1] - zoomed_x[0] < initial_x[1] - initial_x[0]
        assert zoomed_y[1] - zoomed_y[0] < initial_y[1] - initial_y[0]

        center_before = sum(zoomed_x) / 2
        QTest.keyClick(window.plot_widget, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        panned_x = window.plot_widget.getPlotItem().vb.viewRange()[0]
        assert sum(panned_x) / 2 > center_before
        assert window.plot_widget.focusPolicy() & Qt.FocusPolicy.StrongFocus
        assert "QGraphicsView#trendPlot:focus" in window.styleSheet()
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_view_limits_prevent_panning_far_from_data(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 10.0))
        window._plot_y.extend((5.0, 15.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.plot_widget.setRange(xRange=(-1000.0, -900.0), yRange=(-500.0, -400.0), padding=0)

        x_range, y_range = window.plot_widget.getPlotItem().vb.viewRange()
        assert x_range[1] >= -1.0
        assert y_range[1] >= 4.0
    finally:
        window.close()
        qt_app.processEvents()


def test_full_data_autoscale_and_return_to_live_window_are_distinct(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 3600.0, 7200.0))
        window._plot_y.extend((1.0, 2.0, 3.0))
        window._refresh_curve()
        window._update_plot_data_limits()

        window._autoscale_plot()
        full_x_range = window.view_box.viewRange()[0]
        assert full_x_range[0] <= 0.0
        assert full_x_range[1] >= 7200.0
        assert not window.follow_check.isChecked()

        window._return_to_live_view()
        live_x_range = window.view_box.viewRange()[0]
        assert live_x_range[1] == pytest.approx(7201.0)
        assert live_x_range[0] > full_x_range[0]
        assert window.follow_check.isChecked()
        assert window.view_mode_label.text() == "自动跟随最新数据"
    finally:
        window.close()
        qt_app.processEvents()


def test_history_source_label_and_return_live_restore_cached_curve(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    source = tmp_path / "published_history.csv"
    fields = ("experiment_id", "sample_index", "elapsed_s", "mode", "unit_si", "value_si", "status")
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows([
            {"experiment_id": "history", "sample_index": 1, "elapsed_s": 2, "mode": "DCV", "unit_si": "V", "value_si": 8, "status": "ok"},
            {"experiment_id": "history", "sample_index": 2, "elapsed_s": 3, "mode": "DCV", "unit_si": "V", "value_si": 9, "status": "ok"},
        ])
    monkeypatch.setattr(gui_module.QFileDialog, "getOpenFileName", lambda *_args: (str(source), "CSV"))
    monkeypatch.setattr(gui_module.QMessageBox, "information", lambda *_args: gui_module.QMessageBox.StandardButton.Ok)
    window = MainWindow()
    try:
        window.show()
        window._current_mode = Mode.DCV
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend((1.0, 2.0))
        window._refresh_curve()
        window._load_history()
        _wait_history_load(window)

        assert window.chart_source_label.text() == "历史：published_history.csv"
        assert window.return_live_button.isVisible() and window.return_live_button.isEnabled()
        history_x, history_y = window.curve.getOriginalDataset()
        assert list(history_x) == [2.0, 3.0]
        assert list(history_y) == [8.0, 9.0]

        window.return_live_button.click()
        live_x, live_y = window.curve.getData()
        assert window.chart_source_label.text() == "实时数据"
        assert window.view_mode_label.text() != "历史文件视图"
        assert list(live_x) == [0.0, 1.0]
        assert list(live_y) == [1.0, 2.0]
    finally:
        window.close()
        qt_app.processEvents()


def test_history_plot_retains_gap_for_invalid_rows_without_editing_csv(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(gui_module, "MAX_PLOT_POINTS", 10)
    source = tmp_path / "gapped.csv"
    fields = ("sample_index", "elapsed_s", "mode", "unit_si", "value_si", "status")
    rows = [
        {"sample_index": 1, "elapsed_s": 0, "mode": "DCV", "unit_si": "V", "value_si": 1, "status": "ok"},
        {"sample_index": 2, "elapsed_s": 1, "mode": "DCV", "unit_si": "V", "value_si": "", "status": "timeout"},
        {"sample_index": 3, "elapsed_s": 2, "mode": "DCV", "unit_si": "V", "value_si": 3, "status": "ok"},
        {"sample_index": 4, "elapsed_s": 3, "mode": "DCV", "unit_si": "V", "value_si": "nan", "status": "ok"},
        {"sample_index": 5, "elapsed_s": 4, "mode": "DCV", "unit_si": "V", "value_si": 5, "status": "ok"},
    ]
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    original = source.read_bytes()
    monkeypatch.setattr(gui_module.QFileDialog, "getOpenFileName", lambda *_args: (str(source), "CSV"))
    monkeypatch.setattr(gui_module.QMessageBox, "information", lambda *_args: gui_module.QMessageBox.StandardButton.Ok)
    window = MainWindow()
    try:
        window._load_history()
        _wait_history_load(window)
        plot_x, plot_y = window.curve.getOriginalDataset()
        assert list(window._plot_x) == [0.0, 2.0, 4.0]
        assert all(math.isfinite(float(value)) for value in plot_x)
        assert all(left <= right for left, right in zip(plot_x, plot_x[1:]))
        assert sum(math.isnan(float(value)) for value in plot_y) == 2
        assert source.read_bytes() == original
    finally:
        window.close()
        qt_app.processEvents()


def test_latest_invalid_reading_is_visible_and_log_button_selects_its_reason(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._on_sample([
            {
                "status": "timeout", "sample_index": 7, "value_si": "",
                "elapsed_s": 6.0, "error": "响应读取超时",
            },
        ], 0.0, {"attempt_count": 7, "valid_count": 6, "abnormal_count": 1, "count": 6})

        assert "#7" in window.latest_invalid_label.text()
        assert "响应读取超时" in window.latest_invalid_label.text()
        assert window.latest_invalid_log_button.isEnabled()
        assert "下一采样周期" in window.recovery_hint_label.text()
        assert "连续 3 次超时" in window.recovery_hint_label.text()
        assert "已保存 7 次尝试" in window.recovery_hint_label.text()

        window.latest_invalid_log_button.click()

        assert not window.log_dock.isHidden()
        assert "响应读取超时" in window.log_text.textCursor().selectedText()
    finally:
        window.close()
        qt_app.processEvents()


def test_repeated_timeout_save_feedback_includes_sample_counts_and_recovery_path(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    directory = tmp_path / "timeout-run"
    directory.mkdir()
    try:
        window._global_stats = {
            "attempt_count": 3, "valid_count": 2, "abnormal_count": 1, "count": 2,
        }
        window._on_status("连续超时，采集已停止；请检查连接后重试")
        assert "连续 3 次超时" in window.recovery_hint_label.text()

        window._on_experiment_save_result(str(directory), True, "已保存")

        status = window.statusBar().currentMessage()
        assert "尝试 3" in status
        assert "有效 2" in status
        assert "异常 1" in status
        assert str(directory / "measurements.csv") in status
        assert "重新连接后开始新实验" in window.recovery_hint_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_recent_experiment_directory_can_be_opened_and_copied(qt_app, monkeypatch, tmp_path):
    directory = tmp_path / "experiment-run"
    directory.mkdir()
    window = MainWindow()
    opened_urls = []
    monkeypatch.setattr(
        gui_module.QDesktopServices, "openUrl",
        lambda url: opened_urls.append(url.toLocalFile()) or True,
    )
    try:
        window._experiment_csv_path = directory / "measurements.csv"
        window._update_controls()
        assert "measurements.csv" in window.recent_experiment_files_label.text()
        assert "events.csv" in window.recent_experiment_files_label.text()
        assert "config.json" in window.recent_experiment_files_label.text()
        assert "run.log" in window.recent_experiment_files_label.text()
        assert window.open_experiment_directory_button.isEnabled()
        assert window.copy_experiment_path_button.isEnabled()
        window._open_experiment_directory()
        window._copy_experiment_path()
        assert Path(opened_urls[0]).resolve() == directory.resolve()
        assert QApplication.clipboard().text() == str(directory)
    finally:
        window.close()
        qt_app.processEvents()


def test_safe_events_export_is_available_only_for_a_saved_run(qt_app, tmp_path):
    directory = tmp_path / "experiment-run"
    directory.mkdir()
    window = MainWindow()
    try:
        window._experiment_csv_path = directory / "measurements.csv"
        window._experiment_active = False
        window._update_controls()
        assert window.export_events_safe_button.isEnabled()
        assert "原始 events.csv 保持不变" in window.export_events_safe_button.toolTip()
        window._experiment_active = True
        window._update_experiment_directory_controls()
        assert not window.export_events_safe_button.isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_safe_events_export_saves_safe_copy_and_preserves_raw_csv(
    qt_app, monkeypatch, tmp_path
):
    directory = tmp_path / "experiment-run"
    directory.mkdir()
    source = directory / "events.csv"
    original = (
        b"event_index,event\r\n"
        b"1,=1+1\r\n"
        b"2,+SUM(A1)\r\n"
        b"3,-1+2\r\n"
        b"4,@HYPERLINK\r\n"
    )
    source.write_bytes(original)
    destination = tmp_path / "events_excel_safe.csv"
    monkeypatch.setattr(
        gui_module.QFileDialog, "getSaveFileName",
        lambda *_args: (str(destination), "CSV 文件 (*.csv)"),
    )
    window = MainWindow()
    try:
        window._experiment_csv_path = directory / "measurements.csv"
        window._experiment_active = False
        window._export_safe_events_csv()
        deadline = time.monotonic() + 3.0
        while window._safe_events_export_task is not None and time.monotonic() < deadline:
            QTest.qWait(10)

        assert window._safe_events_export_task is None, "后台安全副本导出超时"
        assert source.read_bytes() == original
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert [row["event"] for row in rows] == [
            "'=1+1", "'+SUM(A1)", "'-1+2", "'@HYPERLINK",
        ]
        assert "原始 events.csv 未修改" in window.statusBar().currentMessage()
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_log_filter_exports_only_the_selected_category(qt_app, monkeypatch, tmp_path):
    window = MainWindow()
    export_path = tmp_path / "filtered-log.txt"
    monkeypatch.setattr(gui_module.QFileDialog, "getSaveFileName", lambda *_args: (str(export_path), "Text"))
    try:
        window._log_entries.clear()
        window.log_text.clear()
        window._append_log("COM3 设备已连接")
        window._append_log("> SENS:FUNC?")
        window._append_log("! READ?响应超时")
        window._append_log("开始实验采集")

        window.log_filter_combo.setCurrentIndex(window.log_filter_combo.findData("error"))
        assert "READ?响应超时" in window.log_text.toPlainText()
        assert "SENS:FUNC?" not in window.log_text.toPlainText()
        window._export_filtered_logs()

        exported = export_path.read_text(encoding="utf-8")
        assert "READ?响应超时" in exported
        assert "SENS:FUNC?" not in exported
        assert "COM3" not in exported
        assert "开始实验采集" not in exported
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_cursor_description_includes_raw_value_unit_sample_and_local_time(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "cursor-config"))
    window = MainWindow()
    try:
        description = window._describe_plot_point(
            1.25, 1.234, 17,
            {"raw_reading": "+1.23400000E+00", "timestamp_local": "2026-10-15T12:34:56+08:00"},
        )
        assert "样本 #17" in description
        assert "原始 +1.23400000E+00" in description
        assert "1.234 V" in description
        assert "2026-10-15T12:34:56+08:00" in description
        assert "t=1.25 s" in description
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_experiment_name_is_validated_before_start(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "name-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._control_mode = "computer"
        window._update_controls()
        window.experiment_name.setText("bad/name")
        qt_app.processEvents()
        assert "非法字符" in window.experiment_name_validation.text()
        assert not window.start_button.isEnabled()
        assert not window.single_button.isEnabled()
        window.experiment_name.setText("合法实验名称")
        qt_app.processEvents()
        assert "有效" in window.experiment_name_validation.text()
        assert window.start_button.isEnabled()
        assert window.single_button.isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("choice", "expected_start_count"),
    [
        ("新建独立目录", 1),
        ("返回改名", 0),
        ("取消", 0),
    ],
)
def test_duplicate_experiment_dialog_choices_preserve_existing_data_and_gate_start(
    qt_app, tmp_path, monkeypatch, choice, expected_start_count
):
    """Only an explicit create-copy choice may submit a run with a duplicate name."""
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "duplicate-config"))
    root = tmp_path / "runs"
    old_run = root / "20261016_120000_000001_repeatable_run"
    old_run.mkdir(parents=True)
    old_measurements = old_run / "measurements.csv"
    original_data = b"sample_index,value_si\r\n1,123.456\r\n"
    old_measurements.write_bytes(original_data)

    window = MainWindow()
    dialog_choices = []
    start_requests = []

    # Do not let the real worker open a device or start acquisition. Keep the
    # GUI signal boundary real and observe only the request emitted by the UI.
    window._bridge.start_experiment.disconnect(window._worker.start_experiment)
    window._bridge.start_experiment.connect(lambda *args: start_requests.append(args))

    def choose_duplicate_action():
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            return
        button = next((item for item in dialog.buttons() if item.text() == choice), None)
        if button is not None:
            dialog_choices.append(button.text())
            button.click()

    try:
        window._connected = True
        window._simulation = True
        window._current_mode = Mode.DCV
        window._active_settings_by_mode[Mode.DCV.value] = {"mode": Mode.DCV.value}
        window.output_root.setText(str(root))
        window.experiment_name.setText("repeatable run")
        QTimer.singleShot(0, choose_duplicate_action)

        window._start_continuous()

        assert dialog_choices == [choice]
        assert len(start_requests) == expected_start_count
        assert old_measurements.read_bytes() == original_data
        assert window._worker._recorder is None
        if choice == "新建独立目录":
            _settings, submitted_name, submitted_root, _interval, one_shot = start_requests[0]
            assert submitted_name == "repeatable run"
            assert submitted_root == str(root)
            assert one_shot is False
        else:
            assert not window._start_pending
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_preflight_lists_connection_readback_storage_and_mock_wiring(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "preflight-config"))
    monkeypatch.setattr(gui_module, "prepare_experiment_storage", lambda root: Path(root))
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = True
        window._active_settings_by_mode[Mode.DCV.value] = {"mode": Mode.DCV.value}
        window._prepare_start_preflight(
            ModeSettings(Mode.DCV, autorange=True, nplc=1),
            tmp_path / "runs",
            wiring_confirmed=False,
        )
        text = window.preflight_status.text()
        assert "仪器已连接" in text
        assert "当前功能已回读" in text
        assert "保存目录可写" in text
        assert "模拟器无需实体接线" in text
    finally:
        window.close()
        qt_app.processEvents()


def test_preflight_fails_closed_when_free_space_cannot_be_verified(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "space-unknown-config"))

    def unknown_space(_root):
        raise OSError("无法检查剩余空间")

    monkeypatch.setattr(gui_module, "prepare_experiment_storage", unknown_space)
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = True
        window._active_settings_by_mode[Mode.DCV.value] = {"mode": Mode.DCV.value}
        reported_errors = []
        starts = []
        window._show_error = reported_errors.append
        window._bridge.start_experiment.connect(lambda *_args: starts.append(True))
        with pytest.raises(OSError, match="无法检查剩余空间"):
            window._prepare_start_preflight(
                ModeSettings(Mode.DCV, autorange=True, nplc=1),
                tmp_path / "runs",
                wiring_confirmed=False,
            )
        assert not window.preflight_status.text().startswith("采集预检通过")
        window.output_root.setText(str(tmp_path / "runs"))
        window.experiment_name.setText("disk check")
        window._start_continuous()
        assert reported_errors and "无法检查剩余空间" in reported_errors[-1]
        assert starts == []
    finally:
        window.close()
        qt_app.processEvents()


def test_apply_settings_summarizes_known_range_firmware_and_terminal_conflicts(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "settings-conflict-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.DCI
        window._firmware = "1.0"
        window._terminals = "REAR"
        settings = ModeSettings(Mode.DCI, autorange=True, nplc=1)

        conflicts = window._settings_conflicts(settings)

        assert any("400 mA" in item and "固件" in item for item in conflicts)
        assert any("1 A" in item and "前面板" in item and "端子" in item for item in conflicts)

        warnings = []
        configure_calls = []
        monkeypatch.setattr(gui_module.QMessageBox, "warning", lambda *args: warnings.append(args))
        window._persist_current_settings = lambda: None
        window._settings_for_mode = lambda _mode: settings
        window._bridge.configure.connect(lambda _settings: configure_calls.append(True))
        window._apply_settings()
        assert warnings and "400 mA" in warnings[0][2] and "1 A" in warnings[0][2]
        assert configure_calls == []
    finally:
        window.close()
        qt_app.processEvents()


def test_running_csv_snapshot_stops_at_the_last_complete_csv_record(tmp_path):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "snapshot.csv"
    source.write_bytes(b'id,value\r\n1,"first line\nsecond line"\r\n2,"partial')

    copied_bytes = gui_module._copy_complete_csv_prefix(source, destination)

    assert destination.read_bytes() == b'id,value\r\n1,"first line\nsecond line"\r\n'
    assert copied_bytes == destination.stat().st_size


def test_running_csv_snapshot_writes_explicit_state_and_snapshot_time_sidecar(tmp_path):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "run_进行中快照.csv"
    source.write_bytes(b"sample_index,value\r\n1,1.25\r\n2,partial")
    task = gui_module._CsvSnapshotTask(
        source, destination, "2026-10-16T12:34:56+08:00", allow_overwrite=False,
    )

    task.run()

    assert destination.read_bytes() == b"sample_index,value\r\n1,1.25\r\n"
    metadata = json.loads(Path(str(destination) + ".snapshot.json").read_text(encoding="utf-8"))
    assert metadata["export_state"] == "in-progress-snapshot"
    assert metadata["snapshot_time_local"] == "2026-10-16T12:34:56+08:00"
    assert metadata["last_sample_index_at_export"] == 1


def test_running_csv_snapshot_task_refuses_to_replace_the_raw_source(tmp_path):
    source = tmp_path / "measurements.csv"
    original = b"sample_index,value\r\n1,1.25\r\n2,partial"
    source.write_bytes(original)
    task = gui_module._CsvSnapshotTask(
        source, source, "2026-10-16T12:34:56+08:00", allow_overwrite=True,
    )
    completed = []
    task.signals.completed.connect(lambda *args: completed.append(args))

    task.run()

    assert len(completed) == 1
    assert completed[0][-1] and "原始" in completed[0][-1]
    assert source.read_bytes() == original
    assert not Path(str(source) + ".snapshot.json").exists()
    assert not list(tmp_path.glob(".8845a-snapshot-*.tmp"))
    assert not list(tmp_path.glob(".8845a-snapshot-meta-*.tmp"))
    assert not list(tmp_path.glob(".8845a-snapshot-backup-*.tmp"))


@pytest.mark.parametrize("alias_target", ["destination", "sidecar"])
def test_running_csv_snapshot_task_refuses_hardlink_aliases_of_the_raw_source(
    tmp_path, alias_target
):
    source = tmp_path / "measurements.csv"
    original = b"sample_index,value\r\n1,1.25\r\n2,partial"
    source.write_bytes(original)
    destination = tmp_path / "snapshot.csv"
    sidecar = Path(str(destination) + ".snapshot.json")
    alias = destination if alias_target == "destination" else sidecar
    alias.hardlink_to(source)
    task = gui_module._CsvSnapshotTask(
        source, destination, "2026-10-16T12:34:56+08:00", allow_overwrite=True,
    )
    completed = []
    task.signals.completed.connect(lambda *args: completed.append(args))

    task.run()

    assert len(completed) == 1
    assert completed[0][-1] and "原始" in completed[0][-1]
    assert source.read_bytes() == original
    assert alias.read_bytes() == original
    assert gui_module.os.path.samefile(source, alias)
    assert not destination.exists() if alias_target == "sidecar" else True
    assert not list(tmp_path.glob(".8845a-snapshot-*.tmp"))
    assert not list(tmp_path.glob(".8845a-snapshot-meta-*.tmp"))
    assert not list(tmp_path.glob(".8845a-snapshot-backup-*.tmp"))


def test_running_csv_snapshot_sidecar_uses_copied_prefix_not_stale_gui_sample_index(tmp_path):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "race_snapshot.csv"
    source.write_bytes(b"sample_index,value\r\n1,1.25\r\n2,2.5\r\n3,partial")
    task = gui_module._CsvSnapshotTask(
        source, destination, "2026-10-16T12:34:56+08:00", allow_overwrite=False,
    )

    task.run()

    metadata = json.loads(Path(str(destination) + ".snapshot.json").read_text(encoding="utf-8"))
    assert metadata["last_sample_index_at_export"] == 2


@pytest.mark.parametrize("alias_target", ["destination", "sidecar"])
def test_snapshot_export_ui_rejects_raw_source_alias_before_overwrite_confirmation(
    qt_app, monkeypatch, tmp_path, alias_target
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    source = tmp_path / "measurements.csv"
    original = b"sample_index,value\r\n1,1.25\r\n"
    source.write_bytes(original)
    destination = source if alias_target == "destination" else tmp_path / "snapshot.csv"
    sidecar = Path(str(destination) + ".snapshot.json")
    if alias_target == "sidecar":
        sidecar.hardlink_to(source)
    monkeypatch.setattr(
        gui_module.QFileDialog, "getSaveFileName",
        lambda *_args: (str(destination), "CSV 文件 (*.csv)"),
    )
    overwrite_questions = []
    shown_errors = []
    started_tasks = []
    monkeypatch.setattr(
        gui_module.QMessageBox, "question",
        lambda *args, **_kwargs: overwrite_questions.append(args)
        or gui_module.QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(gui_module.QThreadPool, "start", lambda _self, task: started_tasks.append(task))
    window = MainWindow()
    monkeypatch.setattr(window, "_show_error", shown_errors.append)
    try:
        window._experiment_active = True
        window._experiment_csv_path = source

        window._export_running_snapshot()

        assert source.read_bytes() == original
        assert overwrite_questions == []
        assert shown_errors and "原始" in shown_errors[0]
        assert started_tasks == []
        assert not window._snapshot_pending
        assert window._snapshot_task is None
        if alias_target == "sidecar":
            assert sidecar.read_bytes() == original
            assert gui_module.os.path.samefile(source, sidecar)
    finally:
        window._experiment_active = False
        window.close()
        qt_app.processEvents()


def test_running_csv_snapshot_rolls_back_existing_pair_when_metadata_publish_fails(tmp_path, monkeypatch):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "run_snapshot.csv"
    sidecar = Path(str(destination) + ".snapshot.json")
    source.write_bytes(b"sample_index,value\r\n2,9.5\r\n")
    destination.write_bytes(b"previous complete snapshot\r\n")
    sidecar.write_text('{"export_state":"previous"}\n', encoding="utf-8")
    original_replace = gui_module.os.replace

    def fail_new_metadata_once(source_path, destination_path):
        if ".8845a-snapshot-meta-" in Path(source_path).name and Path(destination_path) == sidecar:
            raise OSError("injected metadata publish failure")
        return original_replace(source_path, destination_path)

    monkeypatch.setattr(gui_module.os, "replace", fail_new_metadata_once)
    task = gui_module._CsvSnapshotTask(
        source, destination, "2026-10-16T12:34:56+08:00", allow_overwrite=True,
    )
    completed = []
    task.signals.completed.connect(lambda *args: completed.append(args))

    task.run()

    assert destination.read_bytes() == b"previous complete snapshot\r\n"
    assert sidecar.read_text(encoding="utf-8") == '{"export_state":"previous"}\n'
    assert "injected metadata publish failure" in completed[0][-1]


def test_running_csv_snapshot_reports_staging_cleanup_failure_and_emits_completion(tmp_path, monkeypatch):
    source = tmp_path / "measurements.csv"
    destination = tmp_path / "snapshot.csv"
    source.write_text("sample_index,value\r\n1,1.25\r\n", encoding="utf-8")
    def fail_copy_after_partial_write(_source_path, destination_path, **_kwargs):
        Path(destination_path).write_bytes(b"incomplete snapshot")
        raise OSError("injected copy failure")

    monkeypatch.setattr(gui_module, "_copy_complete_csv_prefix", fail_copy_after_partial_write)
    original_unlink = Path.unlink
    unlinked = set()
    failed_paths = []

    def fail_staging_cleanup_once(path, *args, **kwargs):
        if path.name.startswith(".8845a-snapshot-") and not path.name.startswith(".8845a-snapshot-meta-") and path not in unlinked:
            unlinked.add(path)
            return original_unlink(path, *args, **kwargs)
        if path.name.startswith(".8845a-snapshot-") and not path.name.startswith(".8845a-snapshot-meta-") and path in unlinked and path not in failed_paths:
            failed_paths.append(path)
            raise OSError("injected staging cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_staging_cleanup_once)
    task = gui_module._CsvSnapshotTask(
        source, destination, "2026-10-16T12:34:56+08:00", allow_overwrite=False,
    )
    completed = []
    task.signals.completed.connect(lambda *args: completed.append(args))

    task.run()

    assert len(completed) == 1
    assert "injected copy failure" in completed[0][-1]
    assert "injected staging cleanup failure" in completed[0][-1]
    assert len(failed_paths) == 1 and failed_paths[0].exists()


def test_autozero_speed_effect_is_visible_beside_setting(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "autozero-note-config"))
    window = MainWindow()
    try:
        assert "增加测量时间" in window.autozero_speed_hint.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_top_readout_shows_valid_sample_count(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "sample-count-config"))
    window = MainWindow()
    try:
        stats = {"count": 2, "valid_count": 2, "minimum": 1.0, "maximum": 2.0,
                 "mean": 1.5, "sample_stddev": 0.707}
        window._on_sample([
            {"status": "ok", "value_si": 1.0, "elapsed_s": 0.0, "mode": Mode.DCV.value,
             "sample_index": 1},
            {"status": "ok", "value_si": 2.0, "elapsed_s": 1.0, "mode": Mode.DCV.value,
             "sample_index": 2},
        ], 1.0, stats)

        assert window.valid_sample_count_label.text() == "有效样本 2"
    finally:
        window.close()
        qt_app.processEvents()


def test_interval_change_log_displays_effective_sample_in_status_bar(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "interval-effective-config"))
    window = MainWindow()
    try:
        message = "目标采样间隔由1秒改为0.5秒；从样本 18 起生效"
        window._append_log(message)
        assert window.statusBar().currentMessage() == message
    finally:
        window.close()
        qt_app.processEvents()


def test_mock_connection_locks_scenario_and_keeps_selected_scenario_visible(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "mock-scenario-config"))
    window = MainWindow()
    try:
        window.scenario_combo.setCurrentText("通信超时测试")
        window._on_connected(True, "MOCK 模拟仪器")

        assert not window.scenario_combo.isEnabled()
        assert window.mock_scenario_status.text() == "MOCK场景：通信超时测试"
        window.scenario_combo.setCurrentText("长期漂移")
        assert window.mock_scenario_status.text() == "MOCK场景：通信超时测试"
    finally:
        window.close()
        qt_app.processEvents()


def test_serial_settings_are_saved_and_restored_per_com_port(qt_app, monkeypatch, tmp_path):
    config_dir = tmp_path / "port-profile-config"
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))
    window = MainWindow()
    try:
        window.port_combo.setEditText("COM3")
        window.baud_combo.setCurrentText("115200")
        window._save_profiles()
        window.port_combo.setEditText("COM4")
        window.baud_combo.setCurrentText("9600")
        window._save_profiles()

        window.port_combo.setEditText("COM3")

        assert window.baud_combo.currentText() == "115200"
        window.port_combo.setEditText("COM4")
        assert window.baud_combo.currentText() == "9600"
        saved = json.loads(window._profile_path.read_text(encoding="utf-8"))
        assert saved["serial_by_port"]["COM3"]["baudrate"] == "115200"
    finally:
        window.close()
        qt_app.processEvents()


def test_recommended_serial_settings_restore_only_the_selected_port_profile(
    qt_app, monkeypatch, tmp_path
):
    config_dir = tmp_path / "recommended-port-profile-config"
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))
    window = MainWindow()
    try:
        window.port_combo.setEditText("COM3")
        window.baud_combo.setCurrentText("115200")
        window.data_bits_combo.setCurrentText("7")
        window.parity_combo.setCurrentIndex(window.parity_combo.findData("E"))
        window.stop_bits_combo.setCurrentText("2")
        window.flow_combo.setCurrentIndex(window.flow_combo.findData("rtscts"))
        window.eol_combo.setCurrentIndex(window.eol_combo.findData("LF"))
        window.timeout_spin.setValue(5.0)
        window._save_profiles()

        window.port_combo.setEditText("COM4")
        assert window.baud_combo.currentText() == "9600"
        assert window.parity_combo.currentData() == "N"
        window.baud_combo.setCurrentText("19200")
        window._save_profiles()

        window.port_combo.setEditText("COM3")
        window.restore_serial_defaults_button.click()

        assert window.baud_combo.currentText() == "9600"
        assert window.data_bits_combo.currentText() == "8"
        assert window.parity_combo.currentData() == "N"
        assert window.stop_bits_combo.currentText() == "1"
        assert window.flow_combo.currentData() == "none"
        assert window.eol_combo.currentData() == "CRLF"
        assert window.timeout_spin.value() == 2.0

        window.port_combo.setEditText("COM4")
        assert window.baud_combo.currentText() == "19200"
        saved = json.loads(window._profile_path.read_text(encoding="utf-8"))
        assert saved["serial_by_port"]["COM3"] == {
            "baudrate": "9600", "bytesize": "8", "parity": "N", "stopbits": "1",
            "flow_control": "none", "eol": "CRLF", "timeout_s": 2.0,
        }
        assert saved["serial_by_port"]["COM4"]["baudrate"] == "19200"
    finally:
        window.close()
        qt_app.processEvents()


def test_sampling_interval_tooltips_distinguish_target_from_monotonic_measurement_time(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "interval-help-config"))
    window = MainWindow()
    try:
        assert "软件等待目标" in window.interval_spin.toolTip()
        assert "转换时间" in window.interval_spin.toolTip()
        assert "PC 单调时钟时间戳" in window._rate_caption.toolTip()
        assert "相邻有效读数" in window._rate_caption.toolTip()
    finally:
        window.close()
        qt_app.processEvents()


def test_full_run_and_region_statistics_label_sample_count_and_units_per_metric(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "stats-context-config"))
    window = MainWindow()
    try:
        stats = {"count": 3, "minimum": 1e-3, "maximum": 3e-3, "mean": 2e-3, "sample_stddev": 1e-3}
        window._update_stats(stats)
        full_text = window.stats_label.text()
        assert full_text.count("n=3") == 5
        assert all(name in full_text for name in ("最大", "最小", "平均", "标准差"))
        assert full_text.count("mV") >= 4

        from fluke8845a.analysis import RunningStats

        window._region_stats_bounds = (0.0, 2.0)
        region = RunningStats()
        for value in (1e-3, 2e-3, 3e-3):
            region.add(value)
        window._show_region_stats(region, "完整原始 CSV")
        region_text = window._region_stats_label.text()
        assert region_text.count("n=3") == 5
        assert all(name in region_text for name in ("最大", "最小", "均值", "标准差"))
        assert region_text.count("mV") >= 4
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_firmware_profile_shows_source_time_and_has_explicit_clear(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "firmware-source-config"))
    window = MainWindow()
    try:
        window._on_connected(True, "FLUKE,8845A,9383035,02/14/08-08:54")
        window._on_configuration({
            "idn": "FLUKE,8845A,9383035,02/14/08-08:54",
            "function": "VOLT:DC", "input_terminals": "FRONT", "simulation": False,
        })
        window.outguard_sw_edit.setText("2.10.882.24")
        window.inguard_sw_edit.setText("ad300210")
        window._firmware_version_changed()

        assert "前面板人工核对" in window.firmware_profile_status.text()
        assert "更新时间" in window.firmware_profile_status.text()
        saved = json.loads(window._profile_path.read_text(encoding="utf-8"))
        record = saved["instrument_firmware_by_serial"]["9383035"]
        assert record["source"] == "前面板人工核对"
        assert record["updated_at"]
        monkeypatch.setattr(
            gui_module.QMessageBox, "question",
            lambda *_args, **_kwargs: gui_module.QMessageBox.StandardButton.Yes,
        )
        window.clear_firmware_profile_button.click()
        assert "9383035" not in window._firmware_profiles
        assert window.outguard_sw_edit.text() == ""
        assert window.inguard_sw_edit.text() == ""
    finally:
        window.close()
        qt_app.processEvents()


def test_autorange_readout_shows_each_readings_actual_range(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "actual-range-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.DCV
        window._active_settings_by_mode[Mode.DCV.value] = {
            "mode": Mode.DCV.value, "autorange": True, "range_si": None,
        }
        window._on_sample([{
            "status": "ok", "value_si": 1.2, "elapsed_s": 1.0, "mode": Mode.DCV.value,
            "sample_index": 1, "range_si": 10.0,
        }], 0.0, {"count": 1, "valid_count": 1, "minimum": 1.2, "maximum": 1.2,
                 "mean": 1.2, "sample_stddev": None})

        assert "实际量程 10 V" in window.reading_mode_label.text()
        assert window._plot_metadata[-1]["range_si"] == 10.0
        assert "实际量程 10 V" in window._describe_plot_point(
            1.0, 1.2, 1, window._plot_metadata[-1]
        )
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("scale_factor", ["1.25", "1.5"])
def test_configuration_dialog_form_layout_is_checkable_at_common_windows_dpi_scales(
    tmp_path, scale_factor
):
    script = textwrap.dedent(r"""
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QRect
        from fluke8845a.gui import MainWindow
        app = QApplication([])
        window = MainWindow()
        try:
            available = QRect(0, 0, 1920, 1080)
            window._fit_configuration_dialog(available, initial=True)
            window.show()
            window._show_configuration()
            window.serial_advanced_button.setChecked(True)
            app.processEvents()
            for section_index, required in enumerate(((window.baud_combo, window.data_bits_combo,
                                                       window.outguard_sw_edit, window.inguard_sw_edit),
                                                      (window.mode_combo,),
                                                      (window.scenario_combo,))):
                window._show_setup_section(section_index)
                app.processEvents()
                state = [(widget.objectName(), widget.isVisible(), widget.geometry().getRect())
                         for widget in required]
                assert all(item[1] for item in state), f"section {section_index} visibility: {state}"
                assert all(item[2][2] > 0 and item[2][3] > 0 for item in state), f"section {section_index} geometry: {state}"
                for left_index, left in enumerate(required):
                    for right in required[left_index + 1:]:
                        if left.parentWidget() is right.parentWidget():
                            left_rect = QRect(left.mapToGlobal(left.rect().topLeft()), left.size())
                            right_rect = QRect(right.mapToGlobal(right.rect().topLeft()), right.size())
                            assert not left_rect.intersects(right_rect), (
                                left.objectName(), right.objectName(), left_rect, right_rect,
                            )
            dialog_frame = window.configuration_dialog.frameGeometry()
            assert available.contains(dialog_frame), dialog_frame
            assert window.configuration_dialog.width() <= available.width() - 24
            assert window.configuration_dialog.height() <= available.height() - 72
        finally:
            window._shutdown_finished = True
            window._worker_thread.quit()
            window._worker_thread.wait(2000)
            window.close()
            app.processEvents()
    """)
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["QT_SCALE_FACTOR"] = scale_factor
    env["FLUKE8845A_CONFIG_DIR"] = str(tmp_path / f"dpi-{scale_factor}")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1] / "src"), env.get("PYTHONPATH", "")]))
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
