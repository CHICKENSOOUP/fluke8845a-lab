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
from PySide6.QtCore import QPoint, QPointF, QRect, QThread, Qt, QTimer
from PySide6.QtGui import QColor, QFontInfo, QImage, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QLabel, QLineEdit, QPushButton

from fluke8845a import gui as gui_module
from fluke8845a import storage as storage_module
from fluke8845a.filter_policy import digital_filter_enable_error
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


def test_main_window_title_identifies_the_running_application_build(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        assert "v1.1.0 · 源码运行" in window.windowTitle()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    "profile_text",
    [
        "[]",
        '{"DCV":"bad"}',
        '{"DCV":{"baseline":"not-a-number"}}',
        '{"serial":{"timeout_s":"not-a-number"}}',
        '{"output_root":{"unexpected":"object"}}',
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
        assert window._profiles["output_root"] == ""
        assert window.output_root.text()
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
        assert "手动缩放任一坐标轴或平移时间轴会停止自动跟随" in window.acquisition_hint.text()
        assert "纵轴缩放仍跟随" not in window.acquisition_hint.text()

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


def test_light_secondary_text_and_unselected_tabs_meet_contrast_target(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "secondary-contrast-config"))
    window = MainWindow()
    try:
        light_background = QColor("#f1efe6")
        for selector in (
            "QLabel#subtitle, QLabel#secondaryText",
            "QTabWidget#setupTabs QTabBar::tab, QTabWidget#analysisTabs QTabBar::tab",
        ):
            style = _stylesheet_rule(window, selector)
            assert _contrast_ratio(QColor(style["color"]), light_background) >= 4.5

        dark_style = _stylesheet_rule(window, "QWidget#measurementWorkspace QLabel#secondaryText")
        assert _contrast_ratio(QColor(dark_style["color"]), QColor("#252d31")) >= 4.5
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


def test_y_axis_span_label_uses_readable_dark_workspace_text(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "y-span-label-contrast-config"))
    window = MainWindow()
    try:
        label = next(
            child for child in window.y_axis_span_control.findChildren(QLabel)
            if child.text() == "纵轴跨度"
        )
        assert label.objectName() == "secondaryText"
        style = _stylesheet_rule(window, "QWidget#measurementWorkspace QLabel#secondaryText")
        assert _contrast_ratio(QColor(style["color"]), QColor("#252d31")) >= 4.5
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
        window._instrument_mode_known = True
        window._instrument_mode_readback = Mode.DCV
        window._active_settings_by_mode[Mode.DCV.value] = {
            "mode": Mode.DCV.value,
            "autorange": True,
            "range_si": None,
            "range_readback_si": 10.0,
            "nplc": 1.0,
            "ac_bandwidth_hz": None,
            "analog_filter": False,
            "autozero": True,
        }
        window._update_readout_context()
        assert "当前量程回读 10 V（快照）" in window.reading_mode_label.text()
        window._request_zero()
        assert dialogs
        message = str(dialogs[0][2])
        assert "直流电压" in message
        assert "自动量程" in message
        assert "当前量程回读 10 V" in message
        assert "可能随输入变化" in message
        assert "NPLC=1" in message
        assert "模拟滤波：关" in message

        window._on_zero_result("ZERO配置回读已返回；此回读不代表ZERO测量结果")
        assert window.zero_result_label.text() == "ZERO配置回读已返回；此回读不代表ZERO测量结果"
        assert "ZERO结果" not in window.zero_result_label.text()
        assert not window.zero_result_label.isHidden()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("mode", [Mode.ACV, Mode.ACI])
def test_zero_confirmation_marks_unsupported_analog_filter_not_applicable(
    qt_app, monkeypatch, tmp_path, mode
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    dialogs = []
    monkeypatch.setattr(
        gui_module.QMessageBox,
        "question",
        lambda *args: dialogs.append(args) or gui_module.QMessageBox.StandardButton.No,
    )
    window = MainWindow()
    try:
        window._current_mode = mode
        window._instrument_mode_known = True
        window._instrument_mode_readback = mode
        window._active_settings_by_mode[mode.value] = {
            "mode": mode.value, "autorange": True, "range_si": None,
            "range_readback_si": None, "nplc": None, "ac_bandwidth_hz": 20,
            "analog_filter": False, "autozero": True,
        }

        window._request_zero()

        confirmation = str(dialogs[-1][2])
        assert "模拟滤波：不适用" in confirmation
        assert "模拟滤波：关" not in confirmation
    finally:
        window.close()
        qt_app.processEvents()


def test_zero_command_failure_result_sets_failure_operation_status(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._on_zero_result("MOCK模拟 ZERO失败：模拟命令错误")
        assert "失败" in window.operation_status_label.text()
        assert window.operation_status_label.property("state") == "failure"
    finally:
        window.close()
        qt_app.processEvents()


def test_zero_request_replaces_stale_feedback_when_preconditions_block_execution(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        monkeypatch.setattr(window, "_show_error", lambda *_args: None)
        window.zero_result_label.setText("previous successful ZERO feedback")
        window.zero_result_label.show()
        window._acquiring = True

        window._request_zero()

        assert "ZERO未执行" in window.zero_result_label.text()
        assert not window.zero_result_label.isHidden()
        assert window.operation_status_label.property("state") == "failure"
    finally:
        window._acquiring = False
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


@pytest.mark.parametrize(
    ("scale_factor", "physical_width", "physical_height"),
    [(1.25, 1920, 1080), (1.5, 1366, 768)],
)
def test_secondary_dialogs_fit_scaled_logical_work_area(
    qt_app, monkeypatch, tmp_path, scale_factor, physical_width, physical_height,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / f"dialog-dpi-{scale_factor}"))
    available = QRect(
        0, 0,
        round(physical_width / scale_factor),
        round(physical_height / scale_factor),
    )
    monkeypatch.setattr(
        gui_module, "_available_geometry_for_widget", lambda _widget: available, raising=False,
    )
    window = MainWindow()
    wiring_dialog = None
    try:
        window.show()
        window._navigate_workspace("files")
        qt_app.processEvents()
        dialogs = [("files", window.files_dialog)]

        window.files_dialog.hide()
        window._fit_configuration_dialog(available, initial=True)
        window.configuration_dialog.show()
        qt_app.processEvents()
        dialogs.append(("settings", window.configuration_dialog))

        wiring_dialog = gui_module.WiringConfirmationDialog(
            Mode.DCV, "核对直流电压接线", window,
        )
        wiring_dialog.show()
        qt_app.processEvents()
        dialogs.append(("wiring", wiring_dialog))

        for name, dialog in dialogs:
            frame = dialog.frameGeometry()
            assert available.contains(frame), (name, scale_factor, available, frame)
            assert frame.width() <= available.width() - 24, (name, scale_factor, frame)
            assert frame.height() <= available.height() - 72, (name, scale_factor, frame)
            if name == "files":
                for control in (
                    window.refresh_files_button, window.files_open_directory_button,
                    window.files_copy_path_button, window.files_load_history_button,
                    window.files_snapshot_button, window.files_safe_events_button,
                    window.files_safe_measurements_button, window.files_share_package_button,
                    window.files_close_button,
                ):
                    control_rect = QRect(
                        control.mapTo(dialog, control.rect().topLeft()), control.size(),
                    )
                    assert dialog.rect().contains(control_rect), (
                        control.objectName(), scale_factor, dialog.rect(), control_rect,
                    )
            elif name == "settings":
                for control in (window.setup_scroll_area, window.workspace_stack.currentWidget()):
                    control_rect = QRect(
                        control.mapTo(dialog, control.rect().topLeft()), control.size(),
                    )
                    assert dialog.rect().contains(control_rect), (
                        control.objectName(), scale_factor, dialog.rect(), control_rect,
                    )
            else:
                for control in (
                    wiring_dialog.instructions_label, wiring_dialog.schematic,
                    wiring_dialog.note_label, wiring_dialog.button_box,
                ):
                    control_rect = QRect(
                        control.mapTo(dialog, control.rect().topLeft()), control.size(),
                    )
                    assert dialog.rect().contains(control_rect), (
                        control.objectName(), scale_factor, dialog.rect(), control_rect,
                    )
    finally:
        if wiring_dialog is not None:
            wiring_dialog.close()
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


def test_connection_button_distinguishes_real_instrument_and_mock_before_connecting(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "connection-target-config"))
    window = MainWindow()
    try:
        assert window.connect_button.text() == "连接真实仪器"
        assert window.connect_button.property("connectionTarget") == "instrument"

        window.mock_mode_check.setChecked(True)

        assert window.connect_button.text() == "启动MOCK模拟仪器"
        assert window.connect_button.property("connectionTarget") == "mock"
        assert 'QPushButton#primaryButton[connectionTarget="mock"]' in window.styleSheet()
        assert not window._connected

        window.show()
        qt_app.processEvents()
        enabled_image = QImage(window.connect_button.size(), QImage.Format.Format_ARGB32_Premultiplied)
        window.connect_button.render(enabled_image)
        sample_x = max(1, enabled_image.width() - 8)
        sample_y = window.connect_button.height() // 2
        assert enabled_image.pixelColor(sample_x, sample_y) == QColor("#8a5d13")

        window.connect_button.setEnabled(False)
        qt_app.processEvents()
        disabled_image = QImage(window.connect_button.size(), QImage.Format.Format_ARGB32_Premultiplied)
        window.connect_button.render(disabled_image)
        disabled_sample_x = max(1, disabled_image.width() - 8)
        assert disabled_image.pixelColor(disabled_sample_x, sample_y) == QColor("#5c4a2e")
    finally:
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


def test_history_csv_loading_disables_measurement_start_controls(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = True
        window._history_load_pending = True
        window._update_controls()

        assert not window.start_button.isEnabled()
        assert not window.single_button.isEnabled()
    finally:
        window._history_load_pending = False
        window.close()
        qt_app.processEvents()


def test_start_entrypoint_cannot_clear_live_state_during_history_csv_loading(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    start_requests = []
    preflight_calls = []
    live_csv = tmp_path / "live" / "measurements.csv"
    original_plot = ([0.0, 1.0], [2.0, 2.1])
    try:
        window._connected = True
        window._simulation = True
        window._history_load_pending = True
        window._history_load_task = object()
        window._experiment_csv_path = live_csv
        window._plot_x.extend(original_plot[0])
        window._plot_y.extend(original_plot[1])
        window.output_root.setText(str(tmp_path))
        window.experiment_name.setText("race-check")
        window._bridge.start_experiment.disconnect(window._worker.start_experiment)
        window._bridge.start_experiment.connect(lambda *args: start_requests.append(args))
        monkeypatch.setattr(
            window, "_prepare_start_preflight",
            lambda *_args, **_kwargs: preflight_calls.append(True) or False,
        )

        window._start_continuous()

        assert not preflight_calls
        assert not start_requests
        assert not window._start_pending
        assert window._experiment_csv_path == live_csv
        assert (list(window._plot_x), list(window._plot_y)) == original_plot
    finally:
        window._history_load_pending = False
        window._history_load_task = None
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


def test_apply_settings_prevents_duplicate_commands_until_readback(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    sent = []
    try:
        window._connected = True
        window._simulation = True
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._persist_current_settings = lambda: None
        window._bridge.configure.disconnect(window._worker.configure_mode)
        window._bridge.configure.connect(
            lambda settings, confirmed_settings: sent.append((settings, confirmed_settings))
        )

        window._apply_settings()
        window._apply_settings()

        assert len(sent) == 1
        assert window._configure_pending
        assert not window.apply_button.isEnabled()
        assert "正在应用仪器参数" in window.apply_button.toolTip()

        window._on_configuration({
            "idn": "FLUKE,8845A,MOCK,2.0", "function": "VOLT:DC",
            "input_terminals": "FRONT", "simulation": True,
        })

        assert not window._configure_pending
        assert window.apply_button.isEnabled()
    finally:
        window._connected = False
        window.close()
        qt_app.processEvents()


def test_zero_prevents_duplicate_commands_until_result_and_unlocks_on_error(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(
        gui_module.QMessageBox, "question",
        lambda *_args, **_kwargs: gui_module.QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(gui_module.QMessageBox, "warning", lambda *_args, **_kwargs: None)
    window = MainWindow()
    sent = []
    try:
        window._connected = True
        window._simulation = True
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._instrument_mode_readback = Mode.DCV
        window._active_settings_by_mode[Mode.DCV.value] = {
            "mode": Mode.DCV.value, "autorange": True, "range_si": None, "nplc": 1,
        }
        window._bridge.zero.disconnect(window._worker.zero)
        window._bridge.zero.connect(lambda: sent.append(True))

        window._request_zero()
        window._request_zero()

        assert sent == [True]
        assert window._zero_pending
        assert not window.zero_button.isEnabled()
        assert "ZERO正在执行" in window.zero_button.toolTip()

        window._on_zero_result("ZERO完成")
        assert not window._zero_pending
        assert window.zero_button.isEnabled()

        window._request_zero()
        assert window._zero_pending
        window._show_error("ZERO通信失败")
        assert not window._zero_pending
        assert window.zero_button.isEnabled()
    finally:
        window._connected = False
        window.close()
        qt_app.processEvents()


def test_zero_requires_current_mode_configuration_readback(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(
        gui_module.QMessageBox, "question",
        lambda *_args, **_kwargs: gui_module.QMessageBox.StandardButton.Yes,
    )
    window = MainWindow()
    sent = []
    try:
        window._connected = True
        window._simulation = True
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._instrument_mode_readback = Mode.DCV
        window._active_settings_by_mode.clear()
        window._bridge.zero.disconnect(window._worker.zero)
        window._bridge.zero.connect(lambda: sent.append(True))

        window._update_controls()
        window._request_zero()

        assert not window.zero_button.isEnabled()
        assert "先读取或应用当前模式配置" in window.zero_button.toolTip()
        assert sent == []
        assert not window._zero_pending
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
        assert sent[0][4] is True
        assert sent[0][5] == ""
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
    window._connected = True
    window._simulation = True
    window._history_load_pending = True
    window._history_load_task = object()
    window.history_button.setEnabled(False)
    window._update_controls()
    try:
        assert not window.start_button.isEnabled()
        assert not window.single_button.isEnabled()
        window._on_history_loaded("large.csv", None, "历史 CSV 加载已取消")
        assert not window._history_load_pending
        assert window.history_button.isEnabled()
        assert window.start_button.isEnabled()
        assert window.single_button.isEnabled()
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


def test_close_waits_for_region_stats_and_discards_queued_history_result(
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

        assert not window.close()
        assert window.isVisible()
        assert not window.isEnabled()
        assert history_cancellations == [True]
        assert region_cancellations == [True]
        assert window._region_stats_request_id == 8
        assert window._region_stats_pending_rows == []
        assert not window._clock_timer.isActive()
        assert window._shutdown_started

        # Model a history task completion that was already queued during shutdown.
        window._on_history_loaded("late-history.csv", result, None)

        assert (list(window._plot_x), list(window._plot_y), window._history_mode) == original_plot
        assert information_calls == []
        assert window.log_text.toPlainText() == original_log
        assert not window.history_button.isEnabled()
        assert window._history_load_pending is False
        assert window._history_load_task is None
        assert window.isVisible(), "shutdown must wait until the region statistics task finishes"
        window._on_region_stats_finished(7, None, "区间统计已取消")
        assert not window.isVisible()

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


@pytest.mark.parametrize(
    ("task_attribute", "completion_method", "completion_args", "task_label"),
    [
        ("_safe_events_export_task", "_on_safe_events_exported", ("events-safe.csv", "disk full"), "事件安全副本"),
        ("_safe_measurements_export_task", "_on_safe_measurements_exported", ("measurements-safe.csv", "disk full"), "测量安全副本"),
        ("_share_export_task", "_on_share_package_exported", ("share.zip", "disk full"), "实验分享包"),
        ("_snapshot_task", "_on_snapshot_exported", ("snapshot.csv", "snapshot.json", "now", 10, "disk full"), "实验快照"),
    ],
)
def test_close_waits_for_background_file_writes_and_reports_failures(
    qt_app, monkeypatch, tmp_path, task_attribute, completion_method, completion_args, task_label
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    window._shutdown_started = True
    window._shutdown_finished = True
    setattr(window, task_attribute, object())
    warnings = []
    close_calls = []
    monkeypatch.setattr(
        gui_module.QMessageBox, "warning",
        lambda *args: warnings.append(args),
    )
    original_close = window.close
    monkeypatch.setattr(window, "close", lambda: close_calls.append(True))

    class CloseEvent:
        accepted = False
        ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    try:
        close_event = CloseEvent()
        window.closeEvent(close_event)

        assert close_event.ignored
        assert not close_event.accepted
        assert not close_calls
        assert "后台任务" in window.shutdown_status_label.text()

        getattr(window, completion_method)(*completion_args)

        assert warnings
        assert task_label in warnings[0][2]
        assert "disk full" in warnings[0][2]
        assert close_calls == [True]
    finally:
        monkeypatch.setattr(window, "close", original_close)
        if window._worker_thread.isRunning():
            window._worker_thread.quit()
            window._worker_thread.wait(2000)
        window._shutdown_started = False
        window._shutdown_finished = True
        window.close()
        qt_app.processEvents()


def test_close_waits_until_cancelled_history_and_analysis_reads_finish(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    cancelled = []
    close_calls = []
    original_close = window.close
    history_task = SimpleNamespace(cancel=lambda: cancelled.append("history"))
    range_task = SimpleNamespace(request_id=39, cancel=lambda: cancelled.append("range"))
    stats_task = SimpleNamespace(cancel=lambda: cancelled.append("stats"))
    window._history_load_pending = True
    window._history_load_task = history_task
    window._history_range_sequence = 40
    window._history_range_pending = True
    window._history_range_task = range_task
    window._active_history_range_tasks = {39: range_task}
    window._region_stats_request_id = 7
    window._region_stats_tasks[7] = stats_task
    window._shutdown_started = True
    window._shutdown_finished = True
    monkeypatch.setattr(window, "close", lambda: close_calls.append(True))

    class CloseEvent:
        ignored = False

        def ignore(self):
            self.ignored = True

        def accept(self):
            raise AssertionError("window must stay open until background reads finish")

    try:
        close_event = CloseEvent()
        window.closeEvent(close_event)

        assert close_event.ignored
        assert set(cancelled) == {"history", "range", "stats"}
        assert not close_calls
        assert "历史 CSV" in window.shutdown_status_label.text()
        assert "区间统计" in window.shutdown_status_label.text()

        window._on_history_loaded("history.csv", None, "历史 CSV 加载已取消")
        assert not close_calls
        window._on_history_detail_loaded(39, "history.csv", None, "图表细节加载已取消")
        assert not close_calls
        window._on_region_stats_finished(7, None, "区间统计已取消")

        assert close_calls == [True]
        assert window._history_load_task is None
        assert window._history_range_task is None
        assert not window._active_history_range_tasks
        assert not window._region_stats_tasks
    finally:
        monkeypatch.setattr(window, "close", original_close)
        window._shutdown_started = False
        window._shutdown_finished = True
        if window._worker_thread.isRunning():
            window._worker_thread.quit()
            window._worker_thread.wait(2000)
        window.close()
        qt_app.processEvents()


def test_shutdown_wait_state_is_visible_in_status_area(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._navigate_workspace("analysis")
        window.analysis_tabs.setCurrentIndex(2)
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


def test_local_control_badge_explicitly_requires_panel_confirmation(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._on_connected(True, "已连接 · FLUKE,8845A,TEST,2.0")
        window._on_control_mode_changed("local")

        assert "待面板确认" in window.connection_badge.text()
        assert "已发送 SYST:LOC" in window.connection_badge.toolTip()
        assert "没有 SCPI 回读" in window.connection_badge.toolTip()
        assert "核对仪表面板" in window.connection_badge.toolTip()
        assert "Local请求" in window.instrument_readback_status.text()
        assert "仪表不提供状态回读" in window.instrument_readback_status.toolTip()
    finally:
        window._on_connected(False, "未连接")
        window.close()
        qt_app.processEvents()


def test_local_error_guidance_explains_remote_command_rejection(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._show_recovery_guidance("仪器已切回LOCAL；采集已停止")

        assert "LOCAL 状态拒绝远程命令" in window.recovery_hint_label.toolTip()
        assert "当前实验记录已保留" in window.recovery_hint_label.toolTip()
        assert "切回 Computer" in window.recovery_hint_label.toolTip()
    finally:
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
        timeout=300,
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


def test_150_percent_dpi_short_window_keeps_chart_panels_inside_and_readable(tmp_path):
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "QT_SCALE_FACTOR": "1.5",
        "FLUKE8845A_CONFIG_DIR": str(tmp_path / "short-high-dpi-config"),
    })
    probe = textwrap.dedent(
        """
        import json
        from PySide6.QtWidgets import QApplication
        from fluke8845a.gui import MainWindow

        app = QApplication([])
        window = MainWindow()
        window.resize(887, 440)
        window.show()
        app.processEvents()
        app.processEvents()
        bounds = window.measurement_workspace.contentsRect()
        panel_items = []
        for index in range(window._chart_panel_layout.count()):
            widget = window._chart_panel_layout.itemAt(index).widget()
            if widget is None or widget.isHidden():
                continue
            rect = widget.geometry()
            panel_items.append({
                "name": widget.objectName(),
                "geometry": [rect.x(), rect.y(), rect.width(), rect.height()],
            })
        labels = (window.reading_mode_label, window.latest_value_label, window.valid_sample_count_label)
        snapshot = {
            "workspace": [window.measurement_workspace.width(), window.measurement_workspace.height()],
            "short_viewport_property": window._reading_card.property("short"),
            "workspace_minimum_hint": [
                window.measurement_workspace.minimumSizeHint().width(),
                window.measurement_workspace.minimumSizeHint().height(),
            ],
            "reading_card": [
                window._reading_card.height(), window._reading_card.minimumSizeHint().height(),
            ],
            "latest_value_font": [
                window.latest_value_label.font().pixelSize(),
                window.latest_value_label.font().pointSizeF(),
            ],
            "latest_invalid_button_visible": window.latest_invalid_log_button.isVisible(),
            "panel_items": panel_items,
            "panel_items_inside": all(
                rect[0] >= bounds.x() and rect[1] >= bounds.y()
                and rect[0] + rect[2] <= bounds.x() + bounds.width()
                and rect[1] + rect[3] <= bounds.y() + bounds.height()
                for rect in (item["geometry"] for item in panel_items)
            ),
            "reading_labels_fit": all(label.height() >= label.minimumSizeHint().height() for label in labels),
            "reading_label_heights": [
                [label.height(), label.minimumSizeHint().height()] for label in labels
            ],
            "plot_toolbar_fit": all(
                widget.height() >= widget.minimumSizeHint().height()
                for widget in window._plot_toolbar_widgets
            ),
            "plot_toolbar_heights": [
                [widget.objectName(), widget.height(), widget.minimumSizeHint().height()]
                for widget in window._plot_toolbar_widgets
            ],
        }
        window._on_sample([{
            "status": "timeout", "sample_index": 1, "value_si": "",
            "elapsed_s": 0.0, "error": "串口响应超时",
        }], 0.0, {"attempt_count": 1, "valid_count": 0, "abnormal_count": 1, "count": 0})
        window._save_live_plot_snapshot()
        window._restore_live_plot_snapshot(window._live_plot_snapshot)
        app.processEvents()
        error_bounds = window.measurement_workspace.contentsRect()
        error_panels = [
            widget.geometry().getRect()
            for index in range(window._chart_panel_layout.count())
            if (widget := window._chart_panel_layout.itemAt(index).widget()) is not None
            and not widget.isHidden()
        ]
        error_snapshot = {
            "workspace": [window.measurement_workspace.width(), window.measurement_workspace.height()],
            "workspace_minimum_hint": window.measurement_workspace.minimumSizeHint().height(),
            "panels_inside": all(
                rect[0] >= error_bounds.x() and rect[1] >= error_bounds.y()
                and rect[0] + rect[2] <= error_bounds.x() + error_bounds.width()
                and rect[1] + rect[3] <= error_bounds.y() + error_bounds.height()
                for rect in error_panels
            ),
            "panel_items": error_panels,
            "recovery_visible": window.recovery_hint_label.isVisible(),
            "recovery_text": window.recovery_hint_label.text(),
            "recovery_tooltip": window.recovery_hint_label.toolTip(),
            "latest_invalid_text": window.latest_invalid_label.text(),
            "latest_invalid_tooltip": window.latest_invalid_label.toolTip(),
            "invalid_log_button_visible": window.latest_invalid_log_button.isVisible(),
            "invalid_log_button_text": window.latest_invalid_log_button.text(),
            "action_buttons_visible": all(
                button.isVisible() for button in (
                    window.start_button, window.single_button, window.pause_button, window.stop_button,
                )
            ),
            "compact_labels_fit": all(
                label.height() >= label.minimumSizeHint().height()
                for label in (
                    window.reading_mode_label,
                    window.latest_value_label,
                    window.valid_sample_count_label,
                    window.latest_invalid_label,
                    window.recovery_hint_label,
                ) if label.isVisible()
            ),
            "compact_labels_no_horizontal_clip": all(
                label.fontMetrics().horizontalAdvance(label.text()) <= label.contentsRect().width()
                for label in (window.latest_invalid_label, window.recovery_hint_label)
                if label.isVisible()
            ),
        }
        window.resize(1280, 720)
        app.processEvents()
        app.processEvents()
        wide_bounds = window.measurement_workspace.contentsRect()
        wide_panels = [
            widget.geometry().getRect()
            for index in range(window._chart_panel_layout.count())
            if (widget := window._chart_panel_layout.itemAt(index).widget()) is not None
            and not widget.isHidden()
        ]
        wide_snapshot = {
            "short_viewport_property": window._reading_card.property("short"),
            "recovery_text": window.recovery_hint_label.text(),
            "latest_invalid_text": window.latest_invalid_label.text(),
            "panels_inside": all(
                rect[0] >= wide_bounds.x() and rect[1] >= wide_bounds.y()
                and rect[0] + rect[2] <= wide_bounds.x() + wide_bounds.width()
                and rect[1] + rect[3] <= wide_bounds.y() + wide_bounds.height()
                for rect in wide_panels
            ),
        }
        window.close()
        app.processEvents()
        print(json.dumps({"idle": snapshot, "error": error_snapshot, "wide": wide_snapshot}, ensure_ascii=False))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"short high-DPI probe failed:\n{result.stdout}\n{result.stderr}"
    snapshots = json.loads(result.stdout)
    snapshot = snapshots["idle"]
    assert snapshot["reading_labels_fit"], {
        "heights": snapshot["reading_label_heights"],
        "font": snapshot["latest_value_font"],
        "property": snapshot["short_viewport_property"],
    }
    assert snapshot["workspace"][1] >= snapshot["workspace_minimum_hint"][1], snapshot
    assert snapshot["panel_items_inside"], snapshot["panel_items"]
    assert snapshot["reading_labels_fit"], snapshot["reading_label_heights"]
    assert snapshot["plot_toolbar_fit"], snapshot["plot_toolbar_heights"]
    error = snapshots["error"]
    assert error["workspace"][1] >= error["workspace_minimum_hint"], error
    assert error["panels_inside"], error["panel_items"]
    assert error["recovery_visible"] and "重新连接" in error["recovery_tooltip"], error
    assert "悬停查看" in error["recovery_text"], error
    assert "超时" in error["latest_invalid_text"], error
    assert "串口响应超时" in error["latest_invalid_tooltip"], error
    assert error["compact_labels_fit"], error
    assert error["compact_labels_no_horizontal_clip"], error
    assert error["invalid_log_button_visible"]
    assert error["invalid_log_button_text"] == "日志"
    assert error["action_buttons_visible"]
    wide = snapshots["wide"]
    assert wide["short_viewport_property"] is False
    assert wide["panels_inside"]
    assert "不自动重试" in wide["recovery_text"]
    assert "串口响应超时" in wide["latest_invalid_text"]


def test_150_percent_dpi_1366x768_work_area_keeps_plot_controls_readable(tmp_path):
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "QT_SCALE_FACTOR": "1.5",
        "FLUKE8845A_CONFIG_DIR": str(tmp_path / "mid-size-high-dpi-config"),
    })
    probe = textwrap.dedent(
        """
        import json
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from fluke8845a.gui import MainWindow

        app = QApplication([])
        window = MainWindow()
        window.resize(911, 512)  # 1366 x 768 physical pixels at 150% scaling.
        window.show()
        for _ in range(3):
            app.processEvents()
        QTest.qWait(120)
        app.processEvents()
        labels = (window.reading_mode_label, window.latest_value_label, window.valid_sample_count_label)
        snapshot = {
            "workspace": [window.measurement_workspace.width(), window.measurement_workspace.height()],
            "workspace_minimum_height": window.measurement_workspace.minimumSizeHint().height(),
            "short_layout": window._reading_card.property("short"),
            "reading_labels_fit": all(label.height() >= label.minimumSizeHint().height() for label in labels),
            "reading_heights": [[label.height(), label.minimumSizeHint().height()] for label in labels],
            "span_control_fit": window.y_axis_span_control.height() >= window.y_axis_span_control.minimumSizeHint().height(),
            "span_control_heights": [window.y_axis_span_control.height(), window.y_axis_span_control.minimumSizeHint().height()],
            "span_input_fits": window.y_axis_span_edit.height() <= window.y_axis_span_control.height(),
            "toolbar_fits": window.plot_toolbar.height() >= window.plot_toolbar.minimumSizeHint().height(),
            "toolbar_heights": [window.plot_toolbar.height(), window.plot_toolbar.minimumSizeHint().height()],
        }
        window.close()
        app.processEvents()
        print(json.dumps(snapshot, ensure_ascii=False))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"150% DPI work-area probe failed:\n{result.stdout}\n{result.stderr}"
    snapshot = json.loads(result.stdout)
    assert snapshot["short_layout"], snapshot
    assert snapshot["workspace"][1] >= snapshot["workspace_minimum_height"], snapshot
    assert snapshot["reading_labels_fit"], snapshot
    assert snapshot["span_control_fit"], snapshot
    assert snapshot["span_input_fits"], snapshot
    assert snapshot["toolbar_fits"], snapshot


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

        assert len(window._plot_x) <= gui_module.MAX_PLOT_POINTS
        assert len(window._plot_y) <= gui_module.MAX_PLOT_POINTS
        assert window._plot_x[0] == 1
        assert window._plot_x[-1] == total_samples
        assert window._global_stats["count"] == total_samples
        assert f"统计范围：全程 · 有效点 n={total_samples}" in window.stats_label.text()
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


def test_scpi_log_batch_renders_once_and_preserves_selected_filter(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.log_filter_combo.setCurrentIndex(window.log_filter_combo.findData("scpi"))
        calls = []
        original_render = window._render_filtered_log

        def counted_render(*args):
            calls.append(args)
            return original_render(*args)

        monkeypatch.setattr(window, "_render_filtered_log", counted_render)
        with window._worker._ui_mailbox_lock:
            window._worker._ui_pending_scpi_logs.extend((
                ("12:00:00.001", "> READ?"),
                ("12:00:00.002", "< 1.000000E+00"),
            ))
            window._worker._ui_dropped_scpi_logs = 3
            window._worker._ui_scpi_log_notification_pending = True

        window._on_scpi_logs_ready()

        assert len(calls) == 1
        assert window.log_filter_combo.currentData() == "scpi"
        assert window.communication_log_filter_combo.currentData() == "scpi"
        assert window.log_text.toPlainText() == window.communication_log_text.toPlainText()
        assert "12:00:00.001  > READ?" in window.log_text.toPlainText()
        assert "12:00:00.002  < 1.000000E+00" in window.log_text.toPlainText()
        assert "丢弃较早的 3 行" not in window.log_text.toPlainText()
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
            settings, "exit during active read", str(tmp_path / "experiments"),
            0, False, "", "", settings,
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
        assert list(plot_y) == [0.0]
        assert list(window._plot_y) == [value]
        assert window._last_value == value
        assert "29.9778 MΩ" in window.latest_value_label.text()
        assert "n=1" in window.stats_label.text()

        failed_row = {**row, "sample_index": 2, "value_si": "", "status": "timeout", "error": "timeout"}
        window._on_sample([failed_row], 0.0, stats)
        qt_app.processEvents()

        plot_x, plot_y = window.curve.getData()
        assert list(plot_x) == [0.0]
        assert list(plot_y) == [0.0]
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
    monkeypatch.setattr(gui_module, "MAX_PLOT_POINTS", 10)
    window = MainWindow()
    rows = [
        {
            "sample_index": index,
            "elapsed_s": float(index - 1),
            "mode": Mode.RES2.value,
            "quantity": "resistance",
            "value_si": 999.0 if index == 6 else -50.0 if index == 31 else float(100 + index),
            "unit_si": "Ohm",
            "raw_reading": f"+{100 + index}.0E+00",
            "status": "timeout" if index == 16 else "ok",
            "error": "模拟超时" if index == 16 else "",
        }
        for index in range(1, 41)
    ]
    full_stats = {
        "count": 39,
        "minimum": -50.0,
        "maximum": 999.0,
        "mean": 120.0,
        "sample_stddev": 180.0,
    }
    try:
        window._current_mode = Mode.RES2
        window._load_mode_controls(Mode.RES2)
        window._on_sample(rows, 1.0, full_stats)
        qt_app.processEvents()

        assert len(window._plot_x) <= gui_module.MAX_PLOT_POINTS
        assert window._plot_x[0] == 0.0
        assert window._plot_x[-1] == 39.0
        assert 999.0 in window._plot_y
        assert -50.0 in window._plot_y
        assert any(window._plot_break_before)
        assert window._global_stats["count"] == 39
        assert "n=39" in window.stats_label.text()
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
        assert "选中区间 0–2 s" in text
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
        assert "选中区间 0–2 s" in text
        assert "完整原始 CSV · 实时更新" in text
    finally:
        window.close()
        qt_app.processEvents()
        stream.close()


def test_local_region_statistics_rescan_csv_when_incremental_rows_have_a_gap(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")

    def row(index):
        return {
            "sample_index": index,
            "elapsed_s": index,
            "mode": "RES2",
            "value_si": index * 1e6,
            "status": "ok",
        }

    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row(index) for index in range(1, 4))

    started_tasks = []
    monkeypatch.setattr(
        gui_module.QThreadPool, "start",
        lambda _self, task: started_tasks.append(task),
    )
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._experiment_csv_path = csv_path
        window.show()
        window._region.show()
        window._region.setRegion((0.0, 10.0))
        qt_app.processEvents()
        started_tasks.clear()  # Ignore scans triggered by initial widget visibility.
        window._update_region_stats()
        assert len(started_tasks) == 1

        # Complete the initial source CSV scan through sample 3.
        started_tasks[0].run()
        qt_app.processEvents()
        assert "完整原始 CSV" in window._region_stats_label.text()
        assert "n=3" in window._region_stats_label.text()

        # Samples 4 and 5 were written, but only sample 6 survived in the GUI
        # mailbox after it overflowed. The source CSV remains complete.
        with csv_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writerows(row(index) for index in range(4, 7))
            stream.flush()
        window._on_region_samples([row(6)])

        assert len(started_tasks) == 2, "a missing display row must trigger CSV rescan"
        assert "正在后台" in window._region_stats_label.text()
        assert "完整原始 CSV" not in window._region_stats_label.text()

        started_tasks[1].run()
        qt_app.processEvents()

        text = window._region_stats_label.text()
        assert "完整原始 CSV" in text
        assert "n=6" in text
        assert "均值 3.5 MΩ" in text
        assert "最大 6 MΩ" in text
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("pending_indexes", "pending_limit"),
    [((4, 6), 10), ((4, 5, 6), 2)],
    ids=("pending-gap", "pending-overflow"),
)
def test_region_statistics_rescan_raw_csv_when_scan_mailbox_has_gap_or_overflow(
    qt_app, monkeypatch, tmp_path, pending_indexes, pending_limit
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    monkeypatch.setattr(gui_module, "MAX_PENDING_GUI_ROWS", pending_limit)
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")

    def row(index):
        return {
            "sample_index": index,
            "elapsed_s": index,
            "mode": "RES2",
            "value_si": index * 1e6,
            "status": "ok",
        }

    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(row(index) for index in range(1, 7))

    started_tasks = []
    monkeypatch.setattr(
        gui_module.QThreadPool, "start",
        lambda _self, task: started_tasks.append(task),
    )
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._experiment_csv_path = csv_path
        window.show()
        window._region.show()
        window._region.setRegion((0.0, 10.0))
        qt_app.processEvents()
        started_tasks.clear()

        window._region_stats_request_id = 17
        window._region_stats_scanning = True
        window._region_stats_bounds = (0.0, 10.0)
        for index in pending_indexes:
            window._on_region_samples([row(index)])
        assert len(window._region_stats_pending_rows) <= pending_limit

        scanned_stats = gui_module.RunningStats()
        for index in range(1, 4):
            scanned_stats.add(index * 1e6)
        window._on_region_stats_finished(
            17,
            {"stats": scanned_stats, "last_sample_index": 3},
            None,
        )

        assert len(started_tasks) == 1
        assert "正在后台" in window._region_stats_label.text()
        assert "完整原始 CSV" not in window._region_stats_label.text()

        started_tasks[0].run()
        qt_app.processEvents()
        text = window._region_stats_label.text()
        assert "完整原始 CSV" in text
        assert "n=6" in text
        assert "均值 3.5 MΩ" in text
    finally:
        window.close()
        qt_app.processEvents()


def test_region_statistics_reject_raw_csv_with_missing_sample_index(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows((
            {"sample_index": 1, "elapsed_s": 0, "mode": "RES2", "value_si": 1e6, "status": "ok"},
            {"sample_index": 3, "elapsed_s": 2, "mode": "RES2", "value_si": 3e6, "status": "ok"},
        ))

    completed = []
    task = gui_module._RegionStatisticsTask(1, csv_path, 0.0, 10.0, Mode.RES2)
    task.signals.completed.connect(lambda *args: completed.append(args))
    task.run()

    assert len(completed) == 1
    _request_id, result, error = completed[0]
    assert result is None
    assert "样本序号不连续" in error


@pytest.mark.parametrize("invalid_index", [4.9, True, "4.9", "not-an-index", 0])
def test_region_incremental_continuity_rejects_noninteger_sample_ids(invalid_index):
    assert not gui_module._region_sample_rows_are_contiguous(
        [{"sample_index": invalid_index}], last_sample_index=3
    )


def test_region_statistics_rejects_noninteger_sample_id_in_raw_csv(tmp_path):
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "sample_index": "1.5", "elapsed_s": 1, "mode": "RES2",
            "value_si": 1e6, "status": "ok",
        })

    completed = []
    task = gui_module._RegionStatisticsTask(1, csv_path, 0.0, 10.0, Mode.RES2)
    task.signals.completed.connect(lambda *args: completed.append(args))
    task.run()

    assert len(completed) == 1
    _request_id, result, error = completed[0]
    assert result is None
    assert "无效样本序号" in error


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("elapsed_s", ""),
        ("elapsed_s", "NaN"),
        ("value_si", ""),
        ("value_si", "not-a-number"),
        ("value_si", "inf"),
    ],
)
def test_region_statistics_reject_successful_csv_rows_with_invalid_numeric_values(
    qt_app, monkeypatch, tmp_path, field, invalid_value
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    row = {
        "sample_index": 1,
        "elapsed_s": 1.0,
        "mode": "RES2",
        "value_si": 1e6,
        "status": "ok",
    }
    row[field] = invalid_value
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow(row)

    completed = []
    task = gui_module._RegionStatisticsTask(1, csv_path, 0.0, 10.0, Mode.RES2)
    task.signals.completed.connect(lambda *args: completed.append(args))
    task.run()

    assert len(completed) == 1
    _request_id, result, error = completed[0]
    assert result is None
    assert "成功样本 #1" in error
    assert field in error


def test_region_statistics_rescan_when_mode_changes_during_csv_scan(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    rows = [
        {"sample_index": index, "elapsed_s": index, "mode": "RES2", "value_si": index * 1e6, "status": "ok"}
        for index in range(1, 4)
    ] + [
        {"sample_index": index, "elapsed_s": index, "mode": "DCV", "value_si": float(index), "status": "ok"}
        for index in range(4, 7)
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    started_tasks = []
    monkeypatch.setattr(
        gui_module.QThreadPool, "start",
        lambda _self, task: started_tasks.append(task),
    )
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._experiment_csv_path = csv_path
        window.show()
        window._region.show()
        window._region.setRegion((0.0, 10.0))
        qt_app.processEvents()
        started_tasks.clear()
        window._update_region_stats()
        assert len(started_tasks) == 1

        # Simulate a mode readback changing while the old-mode file scan runs.
        window._current_mode = Mode.DCV
        started_tasks[0].run()
        qt_app.processEvents()

        assert len(started_tasks) == 2
        assert "正在后台" in window._region_stats_label.text()
        assert "完整原始 CSV" not in window._region_stats_label.text()

        started_tasks[1].run()
        qt_app.processEvents()
        text = window._region_stats_label.text()
        assert "n=3" in text
        assert "均值 5 V" in text
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("during_scan", [False, True], ids=("live-increment", "pending-increment"))
def test_region_statistics_invalid_incremental_success_is_not_left_marked_complete(
    qt_app, monkeypatch, tmp_path, during_scan
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()

    started_tasks = []
    monkeypatch.setattr(
        gui_module.QThreadPool, "start",
        lambda _self, task: started_tasks.append(task),
    )
    window = MainWindow()
    try:
        window.show()
        window._region.show()
        qt_app.processEvents()
        window._experiment_csv_path = csv_path
        window._current_mode = Mode.RES2
        window._region_stats_bounds = (0.0, 10.0)
        window._region_stats_mode = Mode.RES2
        window._region.setRegion((0.0, 10.0))
        qt_app.processEvents()
        started_tasks.clear()
        bad_row = {
            "sample_index": 1, "elapsed_s": 1.0, "mode": "RES2",
            "value_si": "not-a-number", "status": "ok",
        }
        with csv_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writerow(bad_row)
            stream.flush()

        if during_scan:
            window._region_stats_request_id = 17
            window._region_stats_scanning = True
            window._region_stats_values = None
            window._on_region_samples([bad_row])
            window._on_region_stats_finished(
                17, {"stats": gui_module.RunningStats(), "last_sample_index": 0}, None,
            )
        else:
            window._region_stats_scanning = False
            window._region_stats_values = gui_module.RunningStats()
            window._region_stats_values.add(1e6)
            window._region_stats_last_sample_index = 0
            window._show_region_stats(window._region_stats_values, "完整原始 CSV")
            window._on_region_samples([bad_row])

        assert len(started_tasks) == 1
        assert "正在后台" in window._region_stats_label.text()
        assert "完整原始 CSV" not in window._region_stats_label.text()

        started_tasks[0].run()
        qt_app.processEvents()
        assert "原始 CSV 区间统计失败" in window._region_stats_label.text()
        assert "成功样本 #1" in window._region_stats_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_configuration_readback_restarts_region_statistics_for_new_mode(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows((
            {"sample_index": 1, "elapsed_s": 1, "mode": "RES2", "value_si": 1e6, "status": "ok"},
            {"sample_index": 2, "elapsed_s": 2, "mode": "DCV", "value_si": 5.0, "status": "ok"},
        ))

    started_tasks = []
    monkeypatch.setattr(
        gui_module.QThreadPool, "start",
        lambda _self, task: started_tasks.append(task),
    )
    window = MainWindow()
    try:
        window.show()
        window._region.show()
        qt_app.processEvents()
        window._current_mode = Mode.RES2
        window._experiment_csv_path = csv_path
        window._region.setRegion((0.0, 10.0))
        started_tasks.clear()
        window._update_region_stats()
        assert len(started_tasks) == 1
        first_request = window._region_stats_request_id

        window._on_configuration({
            "idn": "FLUKE,8845A,TEST,02/14/08-08:54",
            "function": '"VOLT:DC"', "input_terminals": "FRONT", "simulation": True,
        })

        assert window._current_mode is Mode.DCV
        assert window._region_stats_request_id > first_request
        assert len(started_tasks) == 2
        assert window._region_stats_mode is Mode.DCV
        started_tasks[0].run()  # A cancelled old-mode scan cannot overwrite the new request.
        qt_app.processEvents()
        assert window._region_stats_scanning
        started_tasks[1].run()
        qt_app.processEvents()

        text = window._region_stats_label.text()
        assert "完整原始 CSV" in text
        assert "n=1" in text
        assert "均值 5 V" in text
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    "deliver_sample_first", [True, False],
    ids=("sample-signal-before-scan-result", "sample-signal-after-scan-result"),
)
def test_region_statistics_real_worker_thread_counts_snapshot_race_exactly_once(
    qt_app, monkeypatch, tmp_path, deliver_sample_first
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    csv_path = tmp_path / "measurements.csv"
    fields = ("sample_index", "elapsed_s", "mode", "value_si", "status")

    def row(index):
        return {
            "sample_index": index, "elapsed_s": index, "mode": "RES2",
            "value_si": index * 1e6, "status": "ok",
        }

    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows((row(1), row(2)))

    snapshot_entered = Event()
    release_snapshot = Event()
    original_snapshot_lines = gui_module._RegionStatisticsTask._snapshot_lines

    def gated_snapshot_lines(stream, byte_count):
        lines = iter(original_snapshot_lines(stream, byte_count))
        header = next(lines, None)
        if header is not None:
            yield header
        snapshot_entered.set()
        if not release_snapshot.wait(timeout=5):
            raise TimeoutError("测试未及时释放区间统计CSV快照")
        yield from lines

    monkeypatch.setattr(
        gui_module._RegionStatisticsTask, "_snapshot_lines", staticmethod(gated_snapshot_lines)
    )
    window = MainWindow()
    try:
        window.show()
        window._region.show()
        qt_app.processEvents()
        window._current_mode = Mode.RES2
        window._experiment_csv_path = csv_path
        window._region.setRegion((0.0, 10.0))
        qt_app.processEvents()
        window._update_region_stats()
        assert snapshot_entered.wait(timeout=3), "后台统计任务没有进入快照同步点"

        new_row = row(3)
        with csv_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writerow(new_row)
            stream.flush()
        if deliver_sample_first:
            window._on_region_samples([new_row])

        release_snapshot.set()
        deadline = time.monotonic() + 3
        while window._region_stats_scanning and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        assert not window._region_stats_scanning, "区间统计线程未在限定时间内完成"

        if not deliver_sample_first:
            window._on_region_samples([new_row])
            qt_app.processEvents()
        text = window._region_stats_label.text()
        assert "完整原始 CSV" in text
        assert "n=3" in text
        assert "均值 2 MΩ" in text
        assert "最大 3 MΩ" in text
    finally:
        release_snapshot.set()
        window.close()
        qt_app.processEvents()
        gui_module.QThreadPool.globalInstance().waitForDone(3000)


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
        # ZERO is enabled only after a matching instrument mode and active
        # settings have been read back; model that confirmed Computer state.
        window._instrument_mode_known = True
        window._instrument_mode_readback = window._current_mode
        window._active_settings_by_mode[window._current_mode.value] = {
            "mode": window._current_mode.value,
            "autorange": True,
            "range_si": None,
            "nplc": 1.0,
        }
        window._control_mode = "local"
        local_help = window._control_mode_tooltip()
        assert "没有 SCPI 回读命令" in local_help
        assert "核对仪表面板" in local_help
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
        window._on_configuration({"idn": "FLUKE,8845A,TEST8845A001,1.0.0", "function": "", "simulation": False})
        window._on_control_mode_changed("local")

        assert window._active_settings_by_mode == {}
        assert "Local" in window.instrument_readback_status.text()
        assert "仪表不提供状态回读" in window.instrument_readback_status.toolTip()

        window._on_configuration({
            "idn": "FLUKE,8845A,TEST8845A001,1.0.0", "function": "RES", "simulation": False,
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
        assert list(original_y) == [value - (window._plot_y_origin or 0.0) for value in window._plot_y]
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


def test_bulk_history_event_markers_layout_once(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    position_calls = []
    position_labels = window._position_event_labels

    def count_position_calls(*args):
        position_calls.append(args)
        position_labels(*args)

    monkeypatch.setattr(window, "_position_event_labels", count_position_calls)
    try:
        window.show()
        qt_app.processEvents()
        events = [(float(index), f"实验事件 {index}") for index in range(120)]

        window._add_event_markers(events)

        assert len(position_calls) == 1
        assert [line.value() for line in window._event_lines] == [float(index) for index in range(120)]
        assert [label.toPlainText() for label in window._event_labels] == [text for _, text in events]
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


def test_output_directory_preference_persists_across_window_restarts(qt_app, monkeypatch, tmp_path):
    config_dir = tmp_path / "app-config"
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(config_dir))
    output_directory = tmp_path / "long-term-experiments"

    first = MainWindow()
    first.output_root.setText(str(output_directory))
    first.close()
    qt_app.processEvents()

    second = MainWindow()
    try:
        assert second.output_root.text() == str(output_directory)
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
    window._bridge.configure.connect(
        lambda settings, _confirmed_settings: configure_requests.append(settings)
    )
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
        window._idn_version_candidate = ""
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


def test_parseable_idn_candidate_does_not_gate_400ma_in_gui(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._simulation = False
        window._idn_version_candidate = "1.0.0.0"
        window._firmware = ""
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
        window._firmware_profiles["TEST8845A001"] = {
            "outguard_sw": "2.10.882.24", "inguard_sw": "ad300210",
        }
        window._on_configuration({
            "idn": "FLUKE,8845A,TEST8845A001,02/14/08-08:54",
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
        saved_firmware = saved["instrument_firmware_by_serial"]["TEST8845A001"]
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

        speed_help = window.measurement_speed_help.text()
        assert "实测采样间隔" in speed_help
        assert "自动零点" in speed_help
        assert "软件目标间隔" in speed_help
        assert ("NPLC" in speed_help) is bool(spec.nplc_values)
        assert ("模拟滤波" in speed_help) is bool(spec.analog_filter)
        assert ("交流响应带宽" in speed_help) is bool(spec.ac_bandwidths_hz)
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
            assert digital_filter_enable_error(mode) in window.digital_filter_check.toolTip()
            window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(10))
            assert not window.digital_filter_check.isEnabled()
            assert window._settings_for_mode(mode).digital_filter is False
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("firmware", ["", "1.9.0.0"])
def test_unverified_or_old_firmware_does_not_block_slow_nplc(
    qt_app, monkeypatch, tmp_path, firmware
):
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
        assert window.nplc_combo.model().item(slow_index).isEnabled()
        assert not window.digital_filter_check.isEnabled()
        assert "OutG SW 早于 2.0 不支持该功能" in window.digital_filter_check.toolTip()
        assert "跳过数字滤波命令和回读" in window.digital_filter_check.toolTip()
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
            "idn": "FLUKE,8845A,TEST8845A001,1.0.0",
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


def test_plain_wheel_forwarded_from_time_axis_still_zooms_vertical_axis(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_x = tuple(window.view_box.viewRange()[0])
        initial_y = tuple(window.view_box.viewRange()[1])
        scene_position = window.view_box.mapViewToScene(QPointF(1.0, 0.0))
        wheel_event = SimpleNamespace(
            delta=lambda: 120,
            modifiers=lambda: Qt.KeyboardModifier.NoModifier,
            scenePos=lambda: scene_position,
            accept=lambda: None,
            ignore=lambda: None,
        )

        # PyQtGraph forwards events over the bottom axis with axis=0. The
        # unmodified wheel is the user's default Y-axis zoom everywhere in the chart.
        window.view_box.wheelEvent(wheel_event, axis=0)

        actual_x = tuple(window.view_box.viewRange()[0])
        actual_y = tuple(window.view_box.viewRange()[1])
        assert actual_x == pytest.approx(initial_x)
        assert 0 < actual_y[1] - actual_y[0] < initial_y[1] - initial_y[0]
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_zoom_on_either_axis_stops_live_follow(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.follow_check.setChecked(True)
        window._on_plot_wheel_zoom(False, True)
        assert not window.follow_check.isChecked()
        window._on_plot_wheel_zoom(True, False)
        assert not window.follow_check.isChecked()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("axes", "expected_follow"),
    [([True, False], False), ([False, True], True)],
)
def test_manual_drag_stops_live_follow_only_when_time_axis_moves(
    qt_app, monkeypatch, tmp_path, axes, expected_follow
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.follow_check.setChecked(True)
        window.view_box.sigRangeChangedManually.emit(axes)
        assert window.follow_check.isChecked() is expected_follow
    finally:
        window.close()
        qt_app.processEvents()


def test_live_follow_guidance_explains_axis_behavior_and_highlights_return(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        guidance = f"{window.acquisition_hint.text()} {window.chart_gesture_help.text()}"
        assert "手动缩放任一坐标轴或平移时间轴会停止自动跟随" in window.acquisition_hint.text()
        assert "回到实时窗口" in guidance
        assert window.return_live_button.objectName() == "primaryButton"
    finally:
        window.close()
        qt_app.processEvents()


def test_disabled_pause_and_stop_buttons_explain_the_required_state(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._connected = True
        window._acquiring = False
        window._paused = False
        window._update_controls()
        assert not window.pause_button.isEnabled()
        assert "开始连续采集后" in window.pause_button.toolTip()
        assert not window.stop_button.isEnabled()
        assert "没有进行中的采集" in window.stop_button.toolTip()

        window._acquiring = True
        window._update_controls()
        assert window.pause_button.isEnabled()
        assert "暂停当前连续采集" in window.pause_button.toolTip()
        assert window.stop_button.isEnabled()
        assert "停止当前采集" in window.stop_button.toolTip()

        window._on_status("单次测量中")
        assert not window.pause_button.isEnabled()
        assert "单次测量不支持暂停" in window.pause_button.toolTip()
        assert window.stop_button.isEnabled()
    finally:
        window._acquiring = False
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
        if DESCRIPTORS[mode].safety_limits:
            assert DESCRIPTORS[mode].safety_limits in dialogs[0].instructions
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


def test_history_csv_scan_runs_off_gui_thread_while_event_loop_stays_responsive(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    source = tmp_path / "threaded-history.csv"
    source.write_text(
        "experiment_id,sample_index,elapsed_s,mode,unit_si,value_si,status,error\n"
        "exp-1,1,0,DCV,V,1.25,ok,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gui_module.QFileDialog, "getOpenFileName", lambda *_args: (str(source), "CSV"))
    original_loader = gui_module.load_history_csv
    parser_started = Event()
    release_parser = Event()
    parser_threads = []
    main_thread = QThread.currentThread()

    def delayed_loader(*args, **kwargs):
        parser_threads.append(QThread.currentThread())
        parser_started.set()
        if not release_parser.wait(5):
            raise TimeoutError("测试未及时释放历史 CSV 解析任务")
        return original_loader(*args, **kwargs)

    monkeypatch.setattr(gui_module, "load_history_csv", delayed_loader)
    window = MainWindow()
    error_messages = []
    monkeypatch.setattr(window, "_show_error", error_messages.append)
    monkeypatch.setattr(
        gui_module.QMessageBox, "information",
        lambda *_args: gui_module.QMessageBox.StandardButton.Ok,
    )
    try:
        window.show()
        window._connected = True
        window._simulation = True
        window._update_controls()
        assert window.start_button.isEnabled()
        assert window.single_button.isEnabled()
        window._load_history()
        assert not window.start_button.isEnabled()
        assert not window.single_button.isEnabled()

        deadline = time.monotonic() + 2
        while not parser_started.is_set() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        assert parser_started.is_set(), "历史 CSV 解析任务没有启动"
        assert parser_threads[0] != main_thread

        timer_fired = Event()
        QTimer.singleShot(0, timer_fired.set)
        QTest.qWait(10)
        assert timer_fired.is_set(), "解析期间 GUI 事件循环没有响应"

        release_parser.set()
        _wait_history_load(window)
        assert not error_messages, error_messages
        assert window.start_button.isEnabled()
        assert window.single_button.isEnabled()
        assert window._history_mode
        assert window._last_value == 1.25
    finally:
        release_parser.set()
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
        assert list(plot_y[[0, 2, 3]]) == [0.0, 20_000.0, 40_000.0]
        assert list(window._plot_y) == [29_970_000.0, 29_990_000.0, 30_010_000.0]
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
    warnings = []
    monkeypatch.setattr(window, "_show_error", errors.append)
    monkeypatch.setattr(gui_module.QMessageBox, "warning", lambda *_args: warnings.append(_args[-1]))
    try:
        window._load_history()
        _wait_history_load(window)
        if "混合测量模式" in message or "混合单位" in message:
            assert errors == []
            assert len(warnings) == 1
            assert warnings[0].startswith("警告：")
            assert "未加载" in warnings[0]
            assert re.search(message, warnings[0], re.IGNORECASE)
        else:
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


def test_invalid_sample_batch_renders_log_view_once(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    rendered_batches = []
    monkeypatch.setattr(
        window,
        "_store_log_entries",
        lambda entries: rendered_batches.append(list(entries)),
    )
    rows = [
        {
            "status": "invalid",
            "sample_index": index,
            "value_si": "",
            "elapsed_s": float(index),
            "error": "malformed response",
            "mode": Mode.DCV.value,
        }
        for index in range(1, 51)
    ]
    stats = {
        "count": 0, "valid_count": 0, "attempt_count": len(rows), "abnormal_count": len(rows),
        "minimum": None, "maximum": None, "mean": None, "sample_stddev": None,
    }
    try:
        window._current_mode = Mode.DCV

        window._on_sample(rows, 0.0, stats)

        assert len(rendered_batches) == 1
        assert len(rendered_batches[0]) == len(rows)
        assert all("数据无效" in message for message, _formatted in rendered_batches[0])
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
        window._instrument_mode_readback = Mode.DCV
        window._bridge = SimpleNamespace(
            configure=SimpleNamespace(emit=lambda *args: emitted.append(args))
        )

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
        window._instrument_mode_readback = Mode.DCV
        window._bridge = SimpleNamespace(
            configure=SimpleNamespace(emit=lambda *args: configure_requests.append(args)),
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
        window._on_zero_result("ZERO配置回读已返回；此回读不代表ZERO测量结果")
        assert "ZERO配置回读已返回" in window.operation_status_label.text()
        assert "ZERO配置回读已返回" in window.operation_status_label.toolTip()
        window._on_zero_result("ZERO命令已发送，但执行后配置回读失败：模拟回读失败")
        assert "失败" in window.operation_status_label.text()

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


def test_keyboard_focus_feedback_covers_buttons_fields_tabs_and_log_editors(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "focus-style-config"))
    window = MainWindow()
    try:
        stylesheet = window.styleSheet()
        assert re.search(
            r"QLineEdit:focus\s*,\s*QComboBox:focus\s*,\s*QSpinBox:focus\s*,\s*"
            r"QDoubleSpinBox:focus\s*,\s*QPlainTextEdit:focus\s*\{\s*"
            r"border:\s*2px solid #075e63;",
            stylesheet,
        ), "text inputs and both read-only log editors need an equally clear focus border"
        assert "QPushButton:focus, QToolButton:focus, QCheckBox:focus { border: 2px solid #075e63; }" in stylesheet
        assert "QTabWidget QTabBar::tab:focus { border: 2px solid #075e63; border-radius: 4px; }" in stylesheet
        assert window.connect_button.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert window.mode_combo.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert window.analysis_tabs.tabBar().focusPolicy() & Qt.FocusPolicy.TabFocus
        assert window.communication_log_text.focusPolicy() & Qt.FocusPolicy.TabFocus
        assert window.log_text.focusPolicy() & Qt.FocusPolicy.TabFocus
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_keyboard_pan_preserves_tiny_vertical_scale(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0, 30_000_000.0, 30_000_000.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(xRange=(0.0, 2.0), yRange=(-5e-101, 5e-101), padding=0)
        window.plot_widget.setFocus()
        qt_app.processEvents()
        before = window.view_box.viewRange()[1]
        before_center = sum(before) / 2
        before_span = before[1] - before[0]

        QTest.keyClick(window.plot_widget, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)
        after = window.view_box.viewRange()[1]
        after_center = sum(after) / 2

        assert after[1] - after[0] == before_span
        assert abs(after_center - (before_center + before_span * 0.1)) <= before_span * 1e-10
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_wheel_zoom_can_narrow_high_magnitude_y_range_below_old_floor(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0, 30_000_000.0, 30_000_000.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        assert list(window._plot_y) == [30_000_000.0] * 3
        assert list(window.curve.getData()[1]) == [0.0] * 3
        fine_ticks = window.y_axis.tickStrings((-5e-13, 0.0, 5e-13), 1.0, 1e-13)
        assert fine_ticks == ["-5e-13", "0", "+5e-13"]
        window.view_box.setRange(
            xRange=(0.0, 2.0),
            yRange=(-5e-91, 5e-91),
            padding=0,
        )
        qt_app.processEvents()
        scene_position = window.view_box.mapViewToScene(QPointF(1.0, 0.0))
        wheel_event = SimpleNamespace(
            delta=lambda: 120,
            modifiers=lambda: Qt.KeyboardModifier.NoModifier,
            scenePos=lambda: scene_position,
            accept=lambda: None,
            ignore=lambda: None,
        )

        for _ in range(200):
            window.view_box.wheelEvent(wheel_event)

        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        assert 0 < span < 1e-100, f"expected wheel zoom to pass the former 1e-100 Ω floor, got {span} Ω"
    finally:
        window.close()
        qt_app.processEvents()


def test_wheel_can_zoom_in_from_normal_autoscale_to_a_very_small_y_span(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0, 30_000_000.0, 30_000_000.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_range = window.view_box.viewRange()[1]
        initial_span = initial_range[1] - initial_range[0]
        assert initial_span > 0
        assert list(window._plot_y) == [30_000_000.0] * 3

        scene_position = window.view_box.mapViewToScene(QPointF(1.0, 0.0))
        wheel_event = SimpleNamespace(
            delta=lambda: 120,
            modifiers=lambda: Qt.KeyboardModifier.NoModifier,
            scenePos=lambda: scene_position,
            accept=lambda: None,
            ignore=lambda: None,
        )
        for _ in range(180):
            window.view_box.wheelEvent(wheel_event)

        zoomed_range = window.view_box.viewRange()[1]
        zoomed_span = zoomed_range[1] - zoomed_range[0]
        assert 0 < zoomed_span < initial_span * 1e-12, (
            f"wheel zoom from normal autoscale should reach a much narrower span; "
            f"initial={initial_span:g}, zoomed={zoomed_span:g} Ω"
        )
        assert zoomed_range[0] <= 0.0 <= zoomed_range[1]
        assert list(window._plot_y) == [30_000_000.0] * 3
        assert list(window.curve.getData()[1]) == [0.0] * 3

        window._autoscale_plot()
        autoscaled_span = window.view_box.viewRange()[1][1] - window.view_box.viewRange()[1][0]
        assert autoscaled_span > zoomed_span * 1e12
    finally:
        window.close()
        qt_app.processEvents()


def test_actual_qt_wheel_event_sent_to_plot_viewport_shrinks_y_axis(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        before = window.view_box.viewRange()[1]
        initial_span = before[1] - before[0]

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        after = window.view_box.viewRange()[1]
        assert 0 < after[1] - after[0] < initial_span
        assert event.isAccepted()
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("modifiers", "zoom_x", "zoom_y", "y_anchor"),
    [
            (Qt.KeyboardModifier.NoModifier, False, True, "sample"),
        (Qt.KeyboardModifier.ShiftModifier, True, False, None),
            (Qt.KeyboardModifier.ControlModifier, True, True, "sample"),
        (Qt.KeyboardModifier.AltModifier, False, True, "zero"),
    ],
)
def test_qt_wheel_modifiers_change_expected_axes_and_preserve_y_anchor(
    qt_app, monkeypatch, tmp_path, modifiers, zoom_x, zoom_y, y_anchor,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((2.0, 4.0, 6.0))
        window._plot_y_origin = 2.0
        window._refresh_curve()
        window.view_box.setRange(xRange=(0.0, 2.0), yRange=(-4.0, 4.0), padding=0)
        qt_app.processEvents()
        initial_x = tuple(window.view_box.viewRange()[0])
        initial_y = tuple(window.view_box.viewRange()[1])
        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        anchor_value = None
        if y_anchor == "sample":
            scene_position = window.plot_widget.mapToScene(position)
            pointer = window.view_box.mapSceneToView(QPointF(scene_position))
            anchor = window._plot_y_zoom_anchor_at_x(float(pointer.x()))
            anchor_value = anchor[0] if anchor is not None else None
        elif y_anchor == "zero":
            anchor_value = window.view_box.zero_y
        initial_y_fraction = None
        if anchor_value is not None:
            initial_y_fraction = (anchor_value - initial_y[0]) / (initial_y[1] - initial_y[0])
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            modifiers,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        after_x = tuple(window.view_box.viewRange()[0])
        after_y = tuple(window.view_box.viewRange()[1])
        assert event.isAccepted()
        if zoom_x:
            assert after_x[1] - after_x[0] < initial_x[1] - initial_x[0]
        else:
            assert after_x == pytest.approx(initial_x)
        if zoom_y:
            assert after_y[1] - after_y[0] < initial_y[1] - initial_y[0]
            after_y_fraction = (anchor_value - after_y[0]) / (after_y[1] - after_y[0])
            assert after_y_fraction == pytest.approx(initial_y_fraction)
        else:
            assert after_y == pytest.approx(initial_y)
        assert list(window._plot_y) == [2.0, 4.0, 6.0]
        assert list(window.curve.getData()[1]) == [0.0, 2.0, 4.0]
    finally:
        window.close()
        qt_app.processEvents()


def test_live_wheel_zoom_stays_fixed_after_next_sample(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        qt_app.processEvents()
        assert window.follow_check.isChecked()

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        for _ in range(16):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        zoomed_range = window.view_box.viewRange()[1]
        zoomed_span = zoomed_range[1] - zoomed_range[0]
        assert zoomed_span > 0
        assert not window.follow_check.isChecked()

        window._on_sample(
            [{"status": "ok", "value_si": 1.2, "elapsed_s": 3.0, "mode": Mode.DCV.value}],
            1.0,
            {"valid_count": 4},
        )
        qt_app.processEvents()

        updated_range = window.view_box.viewRange()[1]
        assert tuple(updated_range) == pytest.approx(tuple(zoomed_range), rel=1e-9, abs=1e-12), (
            f"a live sample must not reset or shift manual wheel zoom: {zoomed_range!r} → {updated_range!r} V"
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_pending_live_y_autorange_is_resolved_before_first_wheel_zoom(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._on_sample(
            [
                {"status": "ok", "value_si": 1.0, "elapsed_s": 0.0, "sample_index": 1, "mode": Mode.DCV.value},
                {"status": "ok", "value_si": 1.0, "elapsed_s": 1.0, "sample_index": 2, "mode": Mode.DCV.value},
            ],
            1.0,
            {"count": 2, "minimum": 1.0, "maximum": 1.0, "mean": 1.0, "sample_stddev": 0.0, "valid_count": 2},
        )
        qt_app.processEvents()
        window._on_sample(
            [{"status": "ok", "value_si": 1.2, "elapsed_s": 2.0, "sample_index": 3, "mode": Mode.DCV.value}],
            1.0,
            {"count": 3, "minimum": 1.0, "maximum": 1.2, "mean": 1.0, "sample_stddev": 0.0, "valid_count": 3},
        )
        assert window.view_box.state["autoRange"][1]
        assert window.view_box._autoRangeNeedsUpdate

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        assert not window.view_box.state["autoRange"][1]
        assert 0 < span < 0.2, f"first live wheel notch must narrow the pending auto-range, got {span:g} V"
    finally:
        window.close()
        qt_app.processEvents()


def test_x_only_wheel_keeps_live_y_autoscale_enabled(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        qt_app.processEvents()

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ShiftModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()
        assert not window.follow_check.isChecked()
        assert window.view_box.state["autoRange"][1]

        window._on_sample(
            [{"status": "ok", "value_si": 1.2, "elapsed_s": 3.0, "mode": Mode.DCV.value}],
            1.0,
            {"valid_count": 4},
        )
        qt_app.processEvents()

        updated_range = window.view_box.viewRange()[1]
        assert updated_range[0] <= 0.2 <= updated_range[1]
    finally:
        window.close()
        qt_app.processEvents()


def test_actual_qt_wheel_over_y_axis_ticks_shrinks_y_axis(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        before = window.view_box.viewRange()[1]
        initial_span = before[1] - before[0]

        viewport = window.plot_widget.viewport()
        scene_position = QPointF(window.y_axis.geometry().center())
        assert not window.view_box.sceneBoundingRect().contains(scene_position)
        position = window.plot_widget.mapFromScene(scene_position)
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        after = window.view_box.viewRange()[1]
        assert 0 < after[1] - after[0] < initial_span
        assert event.isAccepted()
    finally:
        window.close()
        qt_app.processEvents()


def test_actual_qt_wheel_over_x_axis_ticks_keeps_vertical_default(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_x = tuple(window.view_box.viewRange()[0])
        initial_y = window.view_box.viewRange()[1]
        initial_y_span = initial_y[1] - initial_y[0]

        viewport = window.plot_widget.viewport()
        scene_position = QPointF(window.plot_widget.getPlotItem().getAxis("bottom").geometry().center())
        assert not window.view_box.sceneBoundingRect().contains(scene_position)
        position = window.plot_widget.mapFromScene(scene_position)
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )
        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        after_x = window.view_box.viewRange()[0]
        after_y = window.view_box.viewRange()[1]
        assert after_x == pytest.approx(initial_x)
        assert 0 < after_y[1] - after_y[0] < initial_y_span
        assert after_y[0] <= 0.0 <= after_y[1]
        assert event.isAccepted()
    finally:
        window.close()
        qt_app.processEvents()


def test_actual_qt_wheel_zoom_continues_after_scene_to_view_anchor_overflows(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0,) * 3)
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_range = window.view_box.viewRange()[1]
        initial_span = initial_range[1] - initial_range[0]

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        for _ in range(1400):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)

        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        assert all(math.isfinite(value) for value in y_range)
        assert 0 < span <= 1e-323, (
            f"Qt wheel zoom should keep shrinking down to the representable range near zero; "
            f"initial={initial_span:g}, actual={span:g} Ω"
        )
        assert "浮点精度下限" in window.statusBar().currentMessage()
    finally:
        window.close()
        qt_app.processEvents()


def test_actual_qt_wheel_zoom_uses_recorded_sample_anchor_below_absolute_origin_ulp(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        original_values = (1.2, 1.200000000001)
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend(original_values)
        window._plot_y_origin = original_values[0]
        window._refresh_curve()
        window.view_box.setRange(
            xRange=(0.25, 1.25), yRange=(-1e-12, 2e-12), padding=0,
        )
        qt_app.processEvents()

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        for _ in range(1400):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)

        y_range = window.view_box.viewRange()[1]
        span = float(y_range[1] - y_range[0])
        assert all(math.isfinite(value) for value in y_range)
        assert 0 < span <= 1e-323, (
            "a sample near a large absolute reading must not stop at "
            f"the absolute-origin ULP; actual span={span:.17g} V"
        )
        assert tuple(window._plot_y) == original_values
        assert window._plot_y_origin == original_values[1]
    finally:
        window.close()
        qt_app.processEvents()


def test_ctrl_wheel_keeps_sample_anchor_when_deep_zoom_rebases_y_axis(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        readings = (1.2, 1.200000000001)
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend(readings)
        window._plot_y_origin = readings[0]
        window._refresh_curve()
        window.view_box.setRange(
            xRange=(0.25, 1.25), yRange=(-1e-12, 2e-12), padding=0,
        )
        qt_app.processEvents()

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        rebased = False
        for _ in range(100):
            old_origin = window._plot_y_origin
            before_range = tuple(window.view_box.viewRange()[1])
            before_fraction = (
                (readings[1] - old_origin - before_range[0])
                / (before_range[1] - before_range[0])
            )
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.ControlModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)
            qt_app.processEvents()

            assert event.isAccepted()
            new_origin = window._plot_y_origin
            if new_origin != old_origin:
                after_range = tuple(window.view_box.viewRange()[1])
                after_fraction = (
                    (readings[1] - new_origin - after_range[0])
                    / (after_range[1] - after_range[0])
                )
                assert after_fraction == pytest.approx(before_fraction, abs=1e-6), (
                    "the Ctrl-wheel event that rebases the Y origin must keep the "
                    f"recorded sample at the same screen fraction: "
                    f"{before_fraction:.6f} → {after_fraction:.6f}"
                )
                rebased = True
                break

        assert rebased, "deep Ctrl-wheel zoom should exercise Y-origin rebasing"
        assert tuple(window._plot_y) == readings
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_axis_tick_spacing_stays_positive_for_subnormal_ranges(qt_app):
    axis = gui_module._OffsetValueAxis("left")

    tick_levels = axis.tickValues(-5e-324, 5e-324, 300)

    assert tick_levels
    assert all(math.isfinite(spacing) and spacing > 0 for spacing, _values in tick_levels)


def test_plot_y_axis_unit_tracks_visible_span_and_keeps_absolute_reference(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "axis-prefix-config"))
    window = MainWindow()
    try:
        window._current_mode = Mode.RES2
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0,) * 3)
        window._refresh_curve()
        window.view_box.setRange(xRange=(0.0, 2.0), yRange=(-0.05, 0.05), padding=0)
        window._sync_plot_y_axis()

        assert window.y_axis.display_unit == "mΩ"
        assert "mΩ" in window.y_axis.label.toPlainText()
        assert "30 MΩ" in window.y_axis.labelText
        labels = window.y_axis.tickStrings((-0.05, 0.0, 0.05), 1.0, 0.025)
        assert labels == ["-50", "0", "+50"]
        assert list(window._plot_y) == [30_000_000.0] * 3

        window.view_box.setRange(yRange=(-500_000.0, 500_000.0), padding=0)
        window._sync_plot_y_axis()
        assert window.y_axis.display_unit == "MΩ"
        assert "MΩ" in window.y_axis.label.toPlainText()
    finally:
        window.close()
        qt_app.processEvents()


def test_zoom_rebase_tracks_actual_float_origin_delta(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        old_origin = 30_000_000.0
        requested_delta = 2e-9
        window._plot_y_origin = old_origin

        window._rebase_plot_for_zoom(requested_delta)

        actual_delta = window._plot_y_origin - old_origin
        assert actual_delta != requested_delta
        assert window.view_box._last_zoom_rebase_delta == actual_delta
    finally:
        window.close()
        qt_app.processEvents()


def test_short_wheel_run_can_shrink_desktop_y_span_by_six_orders_of_magnitude(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0,) * 3)
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_range = window.view_box.viewRange()[1]
        initial_span = initial_range[1] - initial_range[0]
        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()

        for _ in range(16):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        zoomed_range = window.view_box.viewRange()[1]
        zoomed_span = zoomed_range[1] - zoomed_range[0]
        assert 0 < zoomed_span < initial_span * 1e-6, (
            f"16 upward wheel notches should shrink the view at least 1,000,000×; "
            f"{initial_span} → {zoomed_span} Ω"
        )
        assert list(window._plot_y) == [30_000_000.0] * 3
    finally:
        window.close()
        qt_app.processEvents()


def test_actual_qt_wheel_zoom_reaches_tiny_span_for_small_changes_at_30_megohm_baseline(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        origin = 30_000_000.0
        one_ulp = math.ulp(origin)
        readings = (origin, origin + one_ulp, origin + 2 * one_ulp)
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend(readings)
        window._plot_y_origin = origin
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_range = tuple(window.view_box.viewRange()[1])
        initial_span = initial_range[1] - initial_range[0]
        assert initial_span > 48 * one_ulp, "start above the former high-baseline zoom floor"

        viewport = window.plot_widget.viewport()
        position = viewport.rect().center()
        for _ in range(32):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        final_range = tuple(window.view_box.viewRange()[1])
        final_span = final_range[1] - final_range[0]
        assert 0 < final_span < one_ulp, (
            "plain Qt wheel events at a 30 MΩ baseline must cross the former precision floor; "
            f"{initial_span:.17g} → {final_span:.17g} Ω"
        )
        assert final_range[0] <= one_ulp <= final_range[1], (
            "the measured sample under the pointer must remain visible after deep zoom"
        )
        assert tuple(window._plot_y) == readings
        assert list(window.curve.getData()[1]) == [0.0, one_ulp, 2 * one_ulp]
    finally:
        window.close()
        qt_app.processEvents()


def test_plain_wheel_keeps_curve_visible_when_pointer_is_in_blank_plot_area(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_range = window.view_box.viewRange()[1]
        initial_span = initial_range[1] - initial_range[0]
        plotted_values = list(window.curve.getData()[1])

        viewport = window.plot_widget.viewport()
        position = QPoint(viewport.rect().center().x(), viewport.rect().top() + 3)
        for _ in range(16):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        zoomed_range = window.view_box.viewRange()[1]
        zoomed_span = zoomed_range[1] - zoomed_range[0]
        assert 0 < zoomed_span < initial_span * 1e-6
        assert all(zoomed_range[0] <= value <= zoomed_range[1] for value in plotted_values), (
            f"plain wheel zoom should keep the curve visible when the pointer is in blank plot space; "
            f"range={zoomed_range!r}, curve={plotted_values!r}"
        )
        assert list(window._plot_y) == [1.0, 1.0, 1.0]
    finally:
        window.close()
        qt_app.processEvents()


def test_plain_wheel_keeps_curve_at_pointer_time_visible_when_view_center_is_blank(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0, 3.0, 4.0))
        window._plot_y.extend((1.0, 1.1, 1.2, 1.3, 1.4))
        window._plot_y_origin = 0.0
        window._refresh_curve()
        window.view_box.setRange(xRange=(0.0, 4.0), yRange=(0.0, 5.0), padding=0)
        qt_app.processEvents()
        original_curve_y = list(window.curve.getData()[1])

        viewport = window.plot_widget.viewport()
        scene_position = window.view_box.mapViewToScene(QPointF(1.5, 4.5))
        position = window.plot_widget.mapFromScene(scene_position)
        event = QWheelEvent(
            QPointF(position),
            QPointF(viewport.mapToGlobal(position)),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.ScrollUpdate,
            False,
        )

        QApplication.sendEvent(viewport, event)
        qt_app.processEvents()

        y_range = window.view_box.viewRange()[1]
        assert y_range[0] <= 1.15 <= y_range[1], (
            "zooming at t=1.5 s should retain the interpolated curve value under the pointer; "
            f"range={y_range!r}, curve={original_curve_y!r}"
        )
        assert list(window.curve.getData()[1]) == original_curve_y
        assert list(window._plot_y) == [1.0, 1.1, 1.2, 1.3, 1.4]
    finally:
        window.close()
        qt_app.processEvents()


def test_wheel_keeps_one_ulp_curve_anchor_at_large_reading(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        origin = 30_000_000.0
        adjacent_reading = math.nextafter(origin, math.inf)
        relative_sample = adjacent_reading - origin
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend((origin, adjacent_reading))
        window._plot_y_origin = origin
        window._refresh_curve()
        window.view_box.setRange(
            xRange=(0.0, 1.0),
            yRange=(-2 * relative_sample, 4 * relative_sample),
            padding=0,
        )
        qt_app.processEvents()
        initial_y_range = tuple(window.view_box.viewRange()[1])
        initial_span = initial_y_range[1] - initial_y_range[0]
        initial_anchor_fraction = (
            relative_sample - initial_y_range[0]
        ) / initial_span
        scene_position = window.view_box.mapViewToScene(QPointF(1.0, 0.0))
        wheel_event = SimpleNamespace(
            delta=lambda: 120,
            modifiers=lambda: Qt.KeyboardModifier.NoModifier,
            scenePos=lambda: scene_position,
            accept=lambda: None,
            ignore=lambda: None,
        )

        for _ in range(12):
            window.view_box.wheelEvent(wheel_event)

        y_min, y_max = window.view_box.viewRange()[1]
        final_span = y_max - y_min
        final_anchor_fraction = (relative_sample - y_min) / final_span
        assert final_span < initial_span
        assert final_anchor_fraction == pytest.approx(initial_anchor_fraction)
        assert y_min <= relative_sample <= y_max
        assert list(window.curve.getData()[1]) == [0.0, relative_sample]
        assert list(window._plot_y) == [origin, adjacent_reading]
    finally:
        window.close()
        qt_app.processEvents()


def test_curve_y_anchor_uses_nearest_sample_without_reordering_nonmonotonic_history(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 2.0, 1.0))
        window._plot_y.extend((1.0, 2.0, 3.0))
        window._plot_y_origin = 0.0
        window._refresh_curve()

        anchor = window._plot_y_anchor_at_x(1.1)

        assert anchor == pytest.approx(3.0)
        assert list(window.curve.getData()[0]) == [0.0, 2.0, 1.0]
        assert list(window._plot_y) == [1.0, 2.0, 3.0]
    finally:
        window.close()
        qt_app.processEvents()


def test_nonmonotonic_history_plot_keeps_samples_when_view_is_narrow(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 2.0, 1.0))
        window._plot_y.extend((10.0, 20.0, 30.0))
        window._plot_y_origin = 10.0
        window._refresh_curve()
        window.view_box.setRange(xRange=(0.9, 1.1), yRange=(-1.0, 25.0), padding=0)
        qt_app.processEvents()

        plotted_x, plotted_y = window.curve.getData()
        assert list(plotted_x) == [0.0, 2.0, 1.0]
        assert list(plotted_y) == [0.0, 10.0, 20.0]
        assert list(window._plot_x) == [0.0, 2.0, 1.0]
        assert list(window._plot_y) == [10.0, 20.0, 30.0]
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize(
    ("x_values", "y_values"),
    [
        ((0.0, 1.0, 1.0, 2.0), (0.0, 1.0, 20.0, 2.0)),
        ((0.0, 1.0, 2.1, 3.1), (0.0, 20.0, 1.0, 2.0)),
    ],
)
def test_duplicate_or_irregular_history_times_disable_uniform_downsampling_and_keep_spike(
    qt_app, monkeypatch, tmp_path, x_values, y_values,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend(x_values)
        window._plot_y.extend(y_values)
        window._plot_y_origin = 0.0
        window._refresh_curve()
        window.view_box.setRange(xRange=(0.9, 1.1), yRange=(0.0, 21.0), padding=0)
        qt_app.processEvents()

        plotted_x, plotted_y = window.curve.getData()
        assert window._plot_x_nondecreasing
        assert not window._plot_x_uniformly_spaced
        assert window.curve.opts["clipToView"] is True
        assert window.curve.opts["autoDownsample"] is False
        assert 20.0 in plotted_y
        assert any(x == 1.0 and y == 20.0 for x, y in zip(plotted_x, plotted_y))
        assert list(window._plot_x) == list(x_values)
        assert list(window._plot_y) == list(y_values)
    finally:
        window.close()
        qt_app.processEvents()


def test_curve_y_anchor_does_not_interpolate_across_history_gaps(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 1.0, 2.0, 3.0))
        window._plot_y.extend((0.0, 1.0, 2.0, 3.0))
        window._plot_break_before.extend((False, False, True, False))
        window._plot_y_origin = 0.0
        window._refresh_curve()

        assert window._plot_y_anchor_at_x(1.4) == pytest.approx(1.0)
        assert window._plot_y_anchor_at_x(1.6) == pytest.approx(2.0)
        assert window._plot_y_anchor_at_x(2.0) == pytest.approx(2.0)
    finally:
        window.close()
        qt_app.processEvents()


def test_plain_wheel_rebase_keeps_center_sample_visible_with_blank_pointer(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.000001, 1.000002))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        center_sample = float(window.curve.getData()[1][1])
        window.view_box.setRange(
            yRange=(center_sample - 1e-6, center_sample + 1e-6), padding=0,
        )
        qt_app.processEvents()
        original_origin = window._plot_y_origin
        viewport = window.plot_widget.viewport()
        position = QPoint(viewport.rect().center().x(), viewport.rect().top() + 3)

        for _ in range(1400):
            event = QWheelEvent(
                QPointF(position),
                QPointF(viewport.mapToGlobal(position)),
                QPoint(0, 0),
                QPoint(0, 120),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.ScrollUpdate,
                False,
            )
            QApplication.sendEvent(viewport, event)

        qt_app.processEvents()
        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        center_sample = float(window.curve.getData()[1][1])
        assert all(math.isfinite(value) for value in y_range)
        assert 0 < span <= 1e-323
        assert window._plot_y_origin != original_origin, "long wheel zoom should exercise Y-origin rebasing"
        assert y_range[0] <= center_sample <= y_range[1], (
            f"rebase during plain wheel zoom should keep the center sample visible; "
            f"range={y_range!r}, sample={center_sample!r}"
        )
        assert list(window._plot_y) == [1.0, 1.000001, 1.000002]
    finally:
        window.close()
        qt_app.processEvents()


def test_plot_hover_tooltip_explains_wheel_zoom_direction(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        tooltip = window.plot_widget.viewport().toolTip()

        assert "图表内滚轮向上缩小纵轴跨度" in tooltip
        assert "鼠标横坐标附近的实测点为锚" in tooltip
        assert "Shift+滚轮缩放时间轴" in tooltip
        assert "向下放大" in tooltip
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_plot_y_span_applies_a_microvolt_range_around_current_center(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        assert "1e-6" in window.y_axis_span_edit.toolTip()
        assert "最小跨度受计算机浮点精度限制" in window.y_axis_span_edit.toolTip()
        assert "不会提升仪表分辨率" in window.y_axis_span_edit.toolTip()
        assert "不修改原始读数" in window.y_axis_span_edit.toolTip()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window.view_box.setRange(
            xRange=(0.0, 2.0), yRange=(-0.1, 0.1), padding=0,
        )
        qt_app.processEvents()
        center_before = sum(window.view_box.viewRange()[1]) / 2
        window.y_axis_span_edit.setText("1e-6")

        QTest.mouseClick(window.apply_y_axis_span_button, Qt.MouseButton.LeftButton)
        qt_app.processEvents()

        y_range = window.view_box.viewRange()[1]
        assert abs((y_range[1] - y_range[0]) - 1e-6) < 1e-8
        assert abs((y_range[0] + y_range[1]) / 2 - center_before) < 1e-7
        assert list(window._plot_y) == [1.0, 1.0, 1.0]
        assert not window.follow_check.isChecked()
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_plot_y_span_near_zero_is_not_limited_by_distant_data(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend((-1e12, 1e12))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 1.0), yRange=(0.0, 2e12), padding=0,
        )
        qt_app.processEvents()
        window.y_axis_span_edit.setText("1e-12")

        QTest.mouseClick(window.apply_y_axis_span_button, Qt.MouseButton.LeftButton)
        qt_app.processEvents()

        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        assert abs(span - 1e-12) < 1e-15, f"expected a 1 pV span around zero, got {span} V"
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_plot_y_span_bypasses_pyqtgraph_quantization_below_old_floor(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0, 30_000_000.0, 30_000_000.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 2.0),
            yRange=(-0.001, 0.001),
            padding=0,
        )
        qt_app.processEvents()
        window.y_axis_span_edit.setText("1e-200")

        QTest.mouseClick(window.apply_y_axis_span_button, Qt.MouseButton.LeftButton)
        qt_app.processEvents()

        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        assert abs(span / 1e-200 - 1.0) < 1e-10, f"expected a 1e-200 Ω range around the offset origin, got {span} Ω"
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_plot_y_span_reports_actual_range_when_finite_view_limits_clip_request(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 2.0), yRange=(-1e-6, 1e-6), padding=0,
        )
        qt_app.processEvents()
        window.y_axis_span_edit.setText("1e308")

        QTest.mouseClick(window.apply_y_axis_span_button, Qt.MouseButton.LeftButton)
        qt_app.processEvents()

        actual_span = window.view_box.viewRange()[1][1] - window.view_box.viewRange()[1][0]
        message = window.statusBar().currentMessage()
        assert math.isfinite(actual_span) and actual_span > 0, (
            f"range={window.view_box.viewRange()[1]!r}, "
            f"target={window.view_box.state['targetRange'][1]!r}, "
            f"limits={window.view_box.state['limits']!r}, "
            f"effective={window.view_box._effectiveLimits()!r}, status={message!r}"
        )
        assert "1e+308" not in message, f"status must not report the unclipped requested span: {message}"
        assert f"{actual_span:.8g}" in message, f"status must report the actual plotted span {actual_span}: {message}"
    finally:
        window.close()
        qt_app.processEvents()


def test_manual_plot_y_span_failure_restores_follow_state_and_previous_range(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 2.0), yRange=(-1e-6, 1e-6), padding=0,
        )
        qt_app.processEvents()
        previous_range = tuple(window.view_box.viewRange()[1])
        assert window.follow_check.isChecked()

        original_update = window.view_box.updateViewRange
        calls = 0

        def collapse_first_update(*args, **kwargs):
            nonlocal calls
            original_update(*args, **kwargs)
            calls += 1
            if calls == 1:
                window.view_box.state["viewRange"][1] = [1.0, 1.0]

        monkeypatch.setattr(window.view_box, "updateViewRange", collapse_first_update)
        window.y_axis_span_edit.setText("1e-6")
        QTest.mouseClick(window.apply_y_axis_span_button, Qt.MouseButton.LeftButton)
        qt_app.processEvents()

        assert calls >= 2, "the invalid range is detected and the previous range is restored"
        assert window.follow_check.isChecked(), "failed zoom must not disable auto-follow"
        restored_range = window.view_box.viewRange()[1]
        assert abs(restored_range[0] - previous_range[0]) < 1e-12
        assert abs(restored_range[1] - previous_range[1]) < 1e-12
    finally:
        window.close()
        qt_app.processEvents()


def test_time_axis_limits_prevent_panning_far_from_data(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 10.0))
        window._plot_y.extend((5.0, 15.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.plot_widget.setRange(xRange=(-1000.0, -900.0), padding=0)

        x_range, _y_range = window.plot_widget.getPlotItem().vb.viewRange()
        assert x_range[1] >= -1.0
    finally:
        window.close()
        qt_app.processEvents()


def test_vertical_pan_keeps_curve_in_view_without_changing_zoom_span(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "vertical-pan-config"))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0,) * 3)
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 2.0), yRange=(-5e-101, 5e-101), padding=0,
        )
        before = window.view_box.viewRange()[1]
        before_span = before[1] - before[0]

        window.view_box.translateBy(y=1.0)

        after = window.view_box.viewRange()[1]
        assert after[0] <= 0.0 <= after[1], "vertical panning should not lose all plotted data in empty space"
        assert after[1] - after[0] == pytest.approx(before_span, rel=1e-12), (
            "limiting pan must preserve the user's zoom level"
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_repeated_keyboard_vertical_pan_cannot_leave_tiny_data_view(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "keyboard-vertical-pan-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0,) * 3)
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 2.0), yRange=(-5e-101, 5e-101), padding=0,
        )
        window.plot_widget.setFocus()
        qt_app.processEvents()
        before = window.view_box.viewRange()[1]
        before_span = before[1] - before[0]

        for _ in range(20):
            QTest.keyClick(window.plot_widget, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)

        after = window.view_box.viewRange()[1]
        assert after[0] <= 0.0 <= after[1], "keyboard panning should not move the curve into empty space"
        assert after[1] - after[0] == pytest.approx(before_span, rel=1e-12), (
            "limiting pan must preserve the user's zoom level"
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_vertical_mouse_drag_keeps_curve_in_view_without_changing_zoom_span(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "mouse-vertical-pan-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((30_000_000.0,) * 3)
        window._refresh_curve()
        window._update_plot_data_limits()
        window.view_box.setRange(
            xRange=(0.0, 2.0), yRange=(-5e-7, 5e-7), padding=0,
        )
        qt_app.processEvents()

        viewport = window.plot_widget.viewport()
        view_box = window.plot_widget.getPlotItem().vb
        start = window.plot_widget.mapFromScene(
            view_box.mapViewToScene(QPointF(1.0, 0.0))
        )
        end = start + QPoint(0, -80)
        assert viewport.rect().contains(start)
        assert viewport.rect().contains(end)
        before_x, before = view_box.viewRange()
        before_span = before[1] - before[0]

        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(viewport, end, 30)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
        qt_app.processEvents()

        after_x, after = view_box.viewRange()
        assert after != before, "QTest drag should exercise the real ViewBox mouse-pan path"
        assert after[0] <= 0.0 <= after[1], "vertical panning should keep plotted data in the viewport"
        assert after[1] - after[0] == pytest.approx(before_span, rel=1e-12), (
            "limiting mouse pan must preserve the user's zoom level"
        )
        assert after_x == pytest.approx(before_x, rel=1e-12), (
            "vertical-only mouse drag must not shift the horizontal axis"
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_vertical_zoom_out_can_shrink_curve_beyond_data_margin(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        window.plot_widget.getPlotItem().enableAutoRange(x=False, y=False)
        qt_app.processEvents()
        window.view_box.setRange(yRange=(1.0 - 0.5e-6, 1.0 + 0.5e-6), padding=0)
        qt_app.processEvents()
        initial_span = window.view_box.viewRange()[1][1] - window.view_box.viewRange()[1][0]
        assert initial_span == pytest.approx(1e-6, rel=1e-6)

        for _ in range(60):
            window._zoom_plot(1.25, zoom_x=False, zoom_y=True)

        actual_span = window.view_box.viewRange()[1][1] - window.view_box.viewRange()[1][0]
        assert actual_span > initial_span * 100, (
            f"vertical zoom-out should continue far beyond the initial data margin of {initial_span:g}, "
            f"got {actual_span:g}"
        )

        window._autoscale_plot()
        autoscaled_span = window.view_box.viewRange()[1][1] - window.view_box.viewRange()[1][0]
        assert autoscaled_span < actual_span, "automatic scaling should restore a useful view of the measured data"
    finally:
        window.close()
        qt_app.processEvents()


def test_vertical_zoom_out_is_not_capped_at_data_scale_factor(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0, 2.0))
        window._plot_y.extend((1.0, 1.0, 1.0))
        window._refresh_curve()
        window._autoscale_plot()
        qt_app.processEvents()
        initial_y = window.view_box.viewRange()[1]
        initial_span = initial_y[1] - initial_y[0]
        assert initial_span > 0

        for _ in range(250):
            window._zoom_plot(1.25, zoom_x=False, zoom_y=True)

        y_range = window.view_box.viewRange()[1]
        span = y_range[1] - y_range[0]
        assert all(math.isfinite(value) for value in y_range)
        assert span > initial_span * 1e20, (
            f"vertical zoom-out should continue far beyond the old 1e12 cap, got {span / initial_span:g}x"
        )
    finally:
        window.close()
        qt_app.processEvents()


def test_vertical_zoom_out_at_finite_limits_keeps_range_finite(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.show()
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend((1.0, 1.0))
        window._refresh_curve()
        window._update_plot_data_limits()
        lower, upper = window.view_box._effectiveLimits()[1]

        for anchor in (lower, upper):
            window.view_box.setRange(yRange=(lower, upper), padding=0)
            qt_app.processEvents()
            window.view_box._scale_y_by(2.1, anchor)
            actual = window.view_box.viewRange()[1]
            target = window.view_box.state["targetRange"][1]

            assert all(math.isfinite(value) for value in (*actual, *target))
            assert actual[0] >= lower and actual[1] <= upper
            assert target[0] >= lower and target[1] <= upper
    finally:
        window.close()
        qt_app.processEvents()


def test_extreme_vertical_axis_labels_do_not_overflow_absolute_offset(qt_app):
    axis = gui_module._OffsetValueAxis("left")
    axis.value_offset = 1.7e308

    labels = axis.tickStrings((sys.float_info.max / 4,), 1.0, 1e307)

    assert len(labels) == 1
    assert labels[0].lower() not in {"inf", "nan"}
    assert "e+308" in labels[0]


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


def test_history_zoom_loads_csv_detail_and_autoscale_restores_full_overview(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    source = tmp_path / "long-history.csv"
    fields = ("sample_index", "elapsed_s", "mode", "unit_si", "value_si", "status")
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({
            "sample_index": index + 1, "elapsed_s": index, "mode": "DCV",
            "unit_si": "V", "value_si": index % 7, "status": "ok",
        } for index in range(1000))

    window = MainWindow()
    try:
        overview = gui_module.load_history_csv(source, Mode.DCV, max_plot_points=10)
        assert overview["_range_index"].monotonic_elapsed
        range_index = overview["_range_index"]

        # Exercise the same completion path used by the background history loader.
        # It must retain the sparse index so later zooms can seek into the CSV.
        monkeypatch.setattr(
            gui_module.QMessageBox, "information",
            lambda *_args: gui_module.QMessageBox.StandardButton.Ok,
        )
        range_reads = []
        load_range = gui_module.load_history_csv_range

        def capture_range_read(*args, **kwargs):
            result = load_range(*args, **kwargs)
            range_reads.append((kwargs.get("range_index"), result))
            return result

        monkeypatch.setattr(gui_module, "load_history_csv_range", capture_range_read)
        window._on_history_loaded(str(source), overview, None)
        assert window._history_overview_data["_range_index"] is range_index

        window.view_box.setRange(xRange=(100.0, 140.0), yRange=(0.0, 6.0), padding=0)

        window._load_history_detail_for_view()
        deadline = time.monotonic() + 3.0
        while window._history_range_pending and time.monotonic() < deadline:
            QTest.qWait(10)
        assert not window._history_range_pending
        assert window._plot_x[0] <= 100.0
        assert window._plot_x[-1] >= 140.0
        assert window._plot_x[0] >= 98.0
        assert window._plot_x[-1] <= 142.0
        assert len(window._plot_x) > len(overview["x"])
        assert window._history_overview_data["_range_index"] is range_index
        assert len(range_reads) == 1
        assert range_reads[0][0] is range_index
        assert range_reads[0][1]["rows_seen"] < 512

        window._autoscale_plot()
        assert list(window._plot_x) == overview["x"]
        full_x = window.view_box.viewRange()[0]
        assert full_x[0] <= 0.0 and full_x[1] >= 999.0
    finally:
        window.close()
        qt_app.processEvents()


def test_event_editor_prominently_warns_about_spreadsheet_formula_text(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._navigate_workspace("analysis")
        window.analysis_tabs.setCurrentIndex(2)
        qt_app.processEvents()
        assert window.event_csv_safety_warning.isVisible()
        warning = window.event_csv_safety_warning.text()
        assert "=、+、-、@" in warning
        assert "公式" in warning
        assert "安全副本" in warning
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
        live_events = [(float(index), f"实时事件 {index}") for index in range(120)]
        window._add_event_markers(live_events)
        window._load_history()
        _wait_history_load(window)

        assert window.chart_source_label.text() == "历史：published_history.csv"
        assert window.return_live_button.isVisible() and window.return_live_button.isEnabled()
        history_x, history_y = window.curve.getOriginalDataset()
        assert list(history_x) == [2.0, 3.0]
        assert list(history_y) == [0.0, 1.0]
        assert list(window._plot_y) == [8.0, 9.0]

        position_calls = []
        position_labels = window._position_event_labels

        def count_position_calls(*args):
            position_calls.append(args)
            position_labels(*args)

        monkeypatch.setattr(window, "_position_event_labels", count_position_calls)
        window.return_live_button.click()
        live_x, live_y = window.curve.getData()
        assert window.chart_source_label.text() == "实时数据"
        assert window.view_mode_label.text() != "历史文件视图"
        assert list(live_x) == [0.0, 1.0]
        assert list(live_y) == [0.0, 1.0]
        assert list(window._plot_y) == [1.0, 2.0]
        assert 1 <= len(position_calls) < 5
        assert [line.value() for line in window._event_lines] == [elapsed for elapsed, _ in live_events]
        assert [label.toPlainText() for label in window._event_labels] == [text for _, text in live_events]
        assert window.event_list_widget.count() == len(live_events)
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
        assert "\u4e0d\u81ea\u52a8\u91cd\u8bd5" in window.recovery_hint_label.text()
        assert "\u91cd\u65b0\u8fde\u63a5" in window.recovery_hint_label.text()
        assert "已保存 7 次尝试" in window.recovery_hint_label.text()

        window.latest_invalid_log_button.click()

        assert not window.log_dock.isHidden()
        assert "响应读取超时" in window.log_text.textCursor().selectedText()
    finally:
        window.close()
        qt_app.processEvents()


def test_latest_invalid_log_button_reveals_error_when_scpi_filter_is_active(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window.log_filter_combo.setCurrentIndex(window.log_filter_combo.findData("scpi"))
        window._on_sample([
            {
                "status": "timeout", "sample_index": 8, "value_si": "",
                "elapsed_s": 7.0, "error": "响应读取超时",
            },
        ], 0.0, {"attempt_count": 8, "valid_count": 7, "abnormal_count": 1, "count": 7})

        assert "响应读取超时" not in window.log_text.toPlainText()
        window.latest_invalid_log_button.click()

        assert window.log_filter_combo.currentData() == "error"
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
        window._on_status("串口读取超时，连接已关闭；请重新连接后开始新实验/分段")
        assert "\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5" in window.recovery_hint_label.text()
        assert not window._acquiring
        assert not window._experiment_active

        window._on_experiment_save_result(str(directory), True, "已保存")

        status = window.statusBar().currentMessage()
        assert "尝试 3" in status
        assert "有效 2" in status
        assert "异常 1" in status
        assert str(directory / "measurements.csv") in status
        assert "开始新实验/分段" in window.recovery_hint_label.text()
    finally:
        window.close()
        qt_app.processEvents()


def test_communication_failure_stops_gui_state_and_explains_new_segment_recovery(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    window = MainWindow()
    try:
        window._acquiring = True
        window._experiment_active = True
        window._on_status("串口通信异常，连接已关闭；请重新连接后开始新实验/分段")

        assert not window._acquiring
        assert not window._experiment_active
        assert "\u4e0d\u4f1a\u81ea\u52a8\u91cd\u8bd5" in window.recovery_hint_label.text()
        assert "\u65b0\u5b9e\u9a8c" in window.recovery_hint_label.text()
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


def test_desktop_safe_measurements_export_saves_separate_copy_and_preserves_raw_csv(
    qt_app, monkeypatch, tmp_path
):
    directory = tmp_path / "experiment-run"
    directory.mkdir()
    source = directory / "measurements.csv"
    original = (
        b"sample_index,value_si,raw_reading,error\r\n"
        b"1,-1.25,-1.25000000E+00,\r\n"
        b"2,=2+2,=1+1,+device error\r\n"
    )
    source.write_bytes(original)
    destination = tmp_path / "measurements_excel_safe.csv"
    monkeypatch.setattr(
        gui_module.QFileDialog, "getSaveFileName",
        lambda *_args: (str(destination), "CSV 文件 (*.csv)"),
    )
    window = MainWindow()
    try:
        window._experiment_csv_path = source
        window._experiment_active = False
        window._refresh_file_workspace()
        assert window.files_safe_measurements_button.isEnabled()
        window._export_safe_measurements_csv()
        deadline = time.monotonic() + 3.0
        while window._safe_measurements_export_task is not None and time.monotonic() < deadline:
            QTest.qWait(10)

        assert window._safe_measurements_export_task is None, "后台测量安全副本导出超时"
        assert source.read_bytes() == original
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert rows[0]["value_si"] == "-1.25"
        assert rows[0]["raw_reading"] == "-1.25000000E+00"
        assert rows[1]["value_si"] == "'=2+2"
        assert rows[1]["raw_reading"] == "'=1+1"
        assert rows[1]["error"] == "'+device error"
        assert "原始 measurements.csv 未修改" in window.statusBar().currentMessage()
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_share_package_exports_redacted_copy_without_changing_raw_files(
    qt_app, monkeypatch, tmp_path
):
    from zipfile import ZipFile

    directory = tmp_path / "experiment-run"
    directory.mkdir()
    measurements = directory / "measurements.csv"
    events = directory / "events.csv"
    config = directory / "config.json"
    raw_measurements = (
        "sample_index,value_si,raw_reading,error\r\n"
        '1,1.25,1.25000000E+00,"COM3 SN-TEST8845A001 C:\\Users\\Alice\\run.csv"\r\n'
    ).encode("utf-8-sig")
    raw_events = (
        "event_index,elapsed_s,event\r\n"
        '1,0,"+note COM3 SN-TEST8845A001 D:\\Lab\\run.csv"\r\n'
    ).encode("utf-8-sig")
    raw_config = {
        "device": {"model": "8845A", "serial_number": "SN-TEST8845A001"},
        "connection": {"port": "COM3"},
        "output_root": "C:\\Users\\Alice\\Experiments",
    }
    measurements.write_bytes(raw_measurements)
    events.write_bytes(raw_events)
    config.write_text(json.dumps(raw_config), encoding="utf-8")
    destination = tmp_path / "experiment-run_share.zip"
    monkeypatch.setattr(
        gui_module.QFileDialog, "getSaveFileName",
        lambda *_args: (str(destination), "ZIP (*.zip)"),
    )
    window = MainWindow()
    try:
        window._experiment_csv_path = measurements
        window._experiment_active = False
        window._refresh_file_workspace()
        assert window.files_share_package_button.isEnabled()
        assert "\u8131\u654f" in window.files_share_package_button.toolTip()
        window._export_share_package()
        # Keep this bounded, but allow the Windows GUI worker's filesystem and
        # queued-signal path more than the old three-second deadline.
        deadline = time.monotonic() + 20.0
        while window._share_export_task is not None and time.monotonic() < deadline:
            QTest.qWait(10)

        assert window._share_export_task is None, "后台分享包导出超时"
        assert destination.is_file()
        assert measurements.read_bytes() == raw_measurements
        assert events.read_bytes() == raw_events
        with ZipFile(destination) as archive:
            assert set(archive.namelist()) == {"measurements.csv", "events.csv", "config.json"}
            payload = b"".join(archive.read(name) for name in archive.namelist())
            shared_config = json.loads(archive.read("config.json"))
            assert "SN-TEST8845A001" not in payload.decode("utf-8-sig")
            assert "COM3" not in payload.decode("utf-8-sig")
            assert "C:\\Users\\Alice" not in payload.decode("utf-8-sig")
            assert "D:\\Lab" not in payload.decode("utf-8-sig")
            assert shared_config["device"]["model"] == "8845A"
            assert shared_config["device"]["serial_number"] != "SN-TEST8845A001"
        assert "\u5206\u4eab\u5305" in window.statusBar().currentMessage()
        window._experiment_active = True
        window._refresh_file_workspace()
        assert not window.files_share_package_button.isEnabled()
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_safe_measurements_export_is_disabled_during_acquisition(qt_app, tmp_path):
    directory = tmp_path / "experiment-run"
    directory.mkdir()
    (directory / "measurements.csv").write_text(
        "raw_reading,error\n1.0,\n", encoding="utf-8"
    )
    window = MainWindow()
    try:
        window._experiment_csv_path = directory / "measurements.csv"
        window._experiment_active = False
        window._refresh_file_workspace()
        assert window.files_safe_measurements_button.isEnabled()
        window._experiment_active = True
        window._refresh_file_workspace()
        assert not window.files_safe_measurements_button.isEnabled()
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
    ("choice", "expected_start_count", "expected_duplicate_action"),
    [
        ("覆盖最近一次（先备份）", 1, "overwrite"),
        ("新建独立目录", 1, "create-copy"),
        ("返回改名", 0, ""),
        ("取消", 0, ""),
    ],
)
def test_duplicate_experiment_dialog_choices_preserve_existing_data_and_gate_start(
    qt_app, tmp_path, monkeypatch, choice, expected_start_count, expected_duplicate_action
):
    """Duplicate-name choices are explicit; overwrite is labeled as a backed-up action."""
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "duplicate-config"))
    root = tmp_path / "runs"
    old_run = root / "20261016_120000_000001_repeatable_run"
    old_run.mkdir(parents=True)
    old_measurements = old_run / "measurements.csv"
    original_data = b"sample_index,value_si\r\n1,123.456\r\n"
    old_measurements.write_bytes(original_data)
    (old_run / "config.json").write_text(
        json.dumps({"experiment_name": "repeatable run", "completion_status": "stopped"}),
        encoding="utf-8",
    )

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
        else:
            cancel = next((item for item in dialog.buttons() if item.text() == "取消"), None)
            if cancel is not None:
                dialog_choices.append(cancel.text())
                cancel.click()

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
        if expected_start_count:
            (
                _settings, submitted_name, submitted_root, _interval, one_shot,
                action, expected_path, _confirmed_settings,
            ) = start_requests[0]
            assert submitted_name == "repeatable run"
            assert submitted_root == str(root)
            assert one_shot is False
            assert action == expected_duplicate_action
            assert expected_path == str(old_run.resolve())
        else:
            assert not window._start_pending
    finally:
        window.close()
        qt_app.processEvents()


def test_desktop_preflight_lists_connection_readback_storage_and_mock_wiring(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "preflight-config"))
    monkeypatch.setattr(gui_module, "prepare_experiment_storage", lambda root: Path(root))
    monkeypatch.setattr(
        gui_module, "get_available_space_bytes", lambda _root: int(4.5 * 1024**3), raising=False
    )
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = True
        window._active_settings_by_mode[Mode.DCV.value] = {"mode": Mode.DCV.value}
        window.interval_spin.setValue(1.0)
        window._prepare_start_preflight(
            ModeSettings(Mode.DCV, autorange=True, nplc=1),
            tmp_path / "runs",
            wiring_confirmed=False,
        )
        text = window.preflight_status.text()
        assert "仪器已连接" in text
        assert "当前功能已回读" in text
        assert "保存目录可写" in text
        assert "按 4 KiB/样本粗估" in text
        assert "可记录约" in text
        assert "当前可用 4.5 GiB" in text
        assert "实际采样间隔、原始响应及错误文本长度会影响结果" in text
        assert "此估算不限制实验时长" in text
        assert "停止采集并尝试收尾文件" in text
        assert "记录可能不完整" in text
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


def test_preflight_rejects_space_that_falls_below_reserve_on_final_check(
    qt_app, tmp_path, monkeypatch
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "space-final-check-config"))
    monkeypatch.setattr(gui_module, "prepare_experiment_storage", lambda root: Path(root))
    monkeypatch.setattr(
        gui_module,
        "get_available_space_bytes",
        lambda _root: storage_module.MINIMUM_FREE_SPACE_BYTES - 1,
    )
    window = MainWindow()
    try:
        window._connected = True
        window._simulation = True
        window._active_settings_by_mode[Mode.DCV.value] = {"mode": Mode.DCV.value}
        with pytest.raises(OSError, match="可用空间不足"):
            window._prepare_start_preflight(
                ModeSettings(Mode.DCV, autorange=True, nplc=1),
                tmp_path / "runs",
                wiring_confirmed=False,
            )
        assert not window.preflight_status.text().startswith("采集预检通过")
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
        window._bridge.configure.connect(
            lambda _settings, _confirmed_settings: configure_calls.append(True)
        )
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


@pytest.mark.parametrize("raw_name", ["measurements.csv", "events.csv", "config.json", "run.log"])
def test_snapshot_destination_validation_rejects_every_current_run_raw_file(tmp_path, raw_name):
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    raw_files = {
        name: experiment_dir / name
        for name in ("measurements.csv", "events.csv", "config.json", "run.log")
    }
    for path in raw_files.values():
        path.write_text(f"original:{path.name}\n", encoding="utf-8")
    source = raw_files["measurements.csv"]
    destination = raw_files[raw_name]
    sidecar = Path(str(destination) + ".snapshot.json")

    with pytest.raises(ValueError, match="原始"):
        gui_module._validate_snapshot_destinations(source, destination, sidecar)


@pytest.mark.parametrize("alias_target", ["destination", "sidecar"])
def test_snapshot_destination_validation_rejects_hardlink_to_other_raw_file(tmp_path, alias_target):
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    source = experiment_dir / "measurements.csv"
    raw_events = experiment_dir / "events.csv"
    source.write_text("sample_index,value\n1,2\n", encoding="utf-8")
    raw_events.write_text("event_time,event_text\n", encoding="utf-8")
    destination = tmp_path / "snapshot.csv"
    sidecar = Path(str(destination) + ".snapshot.json")
    alias = destination if alias_target == "destination" else sidecar
    alias.hardlink_to(raw_events)

    with pytest.raises(ValueError, match="原始"):
        gui_module._validate_snapshot_destinations(source, destination, sidecar)


def test_snapshot_export_ui_rejects_experiment_events_csv_before_overwrite_confirmation(
    qt_app, monkeypatch, tmp_path,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    source = experiment_dir / "measurements.csv"
    destination = experiment_dir / "events.csv"
    raw_files = {
        source: b"measurements-original\r\n",
        destination: b"events-original\r\n",
        experiment_dir / "config.json": b'{"original":true}\n',
        experiment_dir / "run.log": b"log-original\n",
    }
    for path, contents in raw_files.items():
        path.write_bytes(contents)
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
    monkeypatch.setattr(
        gui_module.QThreadPool, "start", lambda _self, task: started_tasks.append(task)
    )
    window = MainWindow()
    monkeypatch.setattr(window, "_show_error", shown_errors.append)
    try:
        window._experiment_active = True
        window._experiment_csv_path = source

        window._export_running_snapshot()

        assert overwrite_questions == []
        assert shown_errors and "原始" in shown_errors[0]
        assert started_tasks == []
        assert not window._snapshot_pending
        assert window._snapshot_task is None
        assert {path: path.read_bytes() for path in raw_files} == raw_files
    finally:
        window._experiment_active = False
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("directory_target", ["destination", "sidecar"])
def test_snapshot_export_ui_rejects_directory_targets_before_overwrite_confirmation(
    qt_app, monkeypatch, tmp_path, directory_target,
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "app-config"))
    experiment_dir = tmp_path / "experiment"
    experiment_dir.mkdir()
    source = experiment_dir / "measurements.csv"
    source.write_bytes(b"sample_index,value\r\n1,1.25\r\n")
    destination = tmp_path / "snapshot.csv"
    sidecar = Path(str(destination) + ".snapshot.json")
    directory = destination if directory_target == "destination" else sidecar
    directory.mkdir()
    sentinel = directory / "keep.txt"
    sentinel.write_text("keep directory contents", encoding="utf-8")
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
    monkeypatch.setattr(
        gui_module.QThreadPool, "start", lambda _self, task: started_tasks.append(task)
    )
    window = MainWindow()
    monkeypatch.setattr(window, "_show_error", shown_errors.append)
    try:
        window._experiment_active = True
        window._experiment_csv_path = source

        window._export_running_snapshot()

        assert overwrite_questions == []
        assert shown_errors and "目录" in shown_errors[0]
        assert started_tasks == []
        assert directory.is_dir()
        assert sentinel.read_text(encoding="utf-8") == "keep directory contents"
        assert not list(tmp_path.glob(".8845a-snapshot-backup-*.tmp"))
        assert not window._snapshot_pending
        assert window._snapshot_task is None
    finally:
        window._experiment_active = False
        window.close()
        qt_app.processEvents()


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


@pytest.mark.parametrize("directory_target", ["destination", "sidecar"])
def test_running_csv_snapshot_task_refuses_directory_targets_without_moving_them(
    tmp_path, directory_target,
):
    source = tmp_path / "measurements.csv"
    source.write_bytes(b"sample_index,value\r\n1,1.25\r\n")
    destination = tmp_path / "snapshot.csv"
    sidecar = Path(str(destination) + ".snapshot.json")
    directory = destination if directory_target == "destination" else sidecar
    directory.mkdir()
    sentinel = directory / "keep.txt"
    sentinel.write_text("keep directory contents", encoding="utf-8")
    task = gui_module._CsvSnapshotTask(
        source, destination, "2026-10-19T12:34:56+08:00", allow_overwrite=True,
    )
    completed = []
    task.signals.completed.connect(lambda *args: completed.append(args))

    task.run()

    assert len(completed) == 1
    assert completed[0][-1] and "目录" in completed[0][-1]
    assert directory.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep directory contents"
    assert not list(tmp_path.glob(".8845a-snapshot-*.tmp"))
    assert not list(tmp_path.glob(".8845a-snapshot-meta-*.tmp"))
    assert not list(tmp_path.glob(".8845a-snapshot-backup-*.tmp"))
    assert not (sidecar.exists() and sidecar.is_file())
    assert not (destination.exists() and destination.is_file())


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
        assert "先断开" in window.scenario_combo.toolTip()
        assert "重新连接" in window.scenario_combo.toolTip()
        assert window.mock_scenario_status.text() == "MOCK场景：通信超时测试"
        window.scenario_combo.setCurrentText("长期漂移")
        assert window.mock_scenario_status.text() == "MOCK场景：通信超时测试"
    finally:
        window.close()
        qt_app.processEvents()


def test_mock_scenario_can_change_after_disconnect_and_is_used_on_reconnect(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "mock-scenario-reconnect-config"))
    window = MainWindow()

    def pump_until(predicate, timeout_s=3.0):
        deadline = time.monotonic() + timeout_s
        while not predicate() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
        qt_app.processEvents()
        return predicate()

    try:
        window.mock_mode_check.setChecked(True)
        window.scenario_combo.setCurrentText("阶跃响应与恢复")
        window._connect_mock()
        assert pump_until(lambda: window._connected)
        assert window._connected_mock_scenario == "阶跃响应与恢复"
        assert not window.scenario_combo.isEnabled()

        window._disconnect()
        assert pump_until(lambda: not window._connected)
        assert window.scenario_combo.isEnabled()

        window.scenario_combo.setCurrentText("长期漂移")
        window._connect_mock()
        assert pump_until(lambda: window._connected)
        assert window._connected_mock_scenario == "长期漂移"
        assert window._worker.instrument.scenario.name == "长期漂移"
        assert window.mock_scenario_status.text() == "MOCK场景：长期漂移"
    finally:
        if window._connected:
            window._disconnect()
            pump_until(lambda: not window._connected)
        window.close()
        deadline = time.monotonic() + 3.0
        while window.isVisible() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)
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


def test_editing_port_restores_profile_instead_of_using_stale_combo_data(
    qt_app, monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "FLUKE8845A_CONFIG_DIR", str(tmp_path / "stale-port-data-config")
    )
    window = MainWindow()
    try:
        window.port_combo.blockSignals(True)
        window.port_combo.clear()
        window.port_combo.addItem("COM3 · configured adapter", "COM3")
        window.port_combo.addItem("COM4 · existing adapter", "COM4")
        window.port_combo.setCurrentIndex(1)
        window.port_combo.blockSignals(False)
        window._serial_profile_port = "COM4"
        window._profiles["serial_by_port"] = {
            "COM3": {"baudrate": "115200"},
            "COM4": {"baudrate": "9600"},
        }
        window._restore_port_serial_settings("COM4")

        window.port_combo.setEditText("COM3")

        assert window._serial_profile_port == "COM3"
        assert window.baud_combo.currentText() == "115200"
        assert window._profiles["serial_by_port"]["COM4"]["baudrate"] == "9600"
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
        assert "选中区间 0–2 s" in region_text
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
        window._on_connected(True, "FLUKE,8845A,TEST8845A001,02/14/08-08:54")
        window._on_configuration({
            "idn": "FLUKE,8845A,TEST8845A001,02/14/08-08:54",
            "function": "VOLT:DC", "input_terminals": "FRONT", "simulation": False,
        })
        window.outguard_sw_edit.setText("2.10.882.24")
        window.inguard_sw_edit.setText("ad300210")
        window._firmware_version_changed()

        assert "前面板人工核对" in window.firmware_profile_status.text()
        assert "更新时间" in window.firmware_profile_status.text()
        saved = json.loads(window._profile_path.read_text(encoding="utf-8"))
        record = saved["instrument_firmware_by_serial"]["TEST8845A001"]
        assert record["source"] == "前面板人工核对"
        assert record["updated_at"]
        monkeypatch.setattr(
            gui_module.QMessageBox, "question",
            lambda *_args, **_kwargs: gui_module.QMessageBox.StandardButton.Yes,
        )
        window.clear_firmware_profile_button.click()
        assert "TEST8845A001" not in window._firmware_profiles
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
            scale_factor = float(os.environ["QT_SCALE_FACTOR"])
            physical_width, physical_height = (1920, 1080) if scale_factor <= 1.25 else (1366, 768)
            available = QRect(
                0, 0, round(physical_width / scale_factor), round(physical_height / scale_factor),
            )
            window._fit_configuration_dialog(available, initial=True)
            window.show()
            window._show_configuration()
            # _show_configuration also asks Qt for the offscreen screen's
            # geometry; restore the physical-to-logical test work area here.
            window._fit_configuration_dialog(available, initial=True)
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
            assert available.contains(dialog_frame), (available, dialog_frame, window.configuration_dialog.size())
            assert window.configuration_dialog.width() <= available.width() - 24, (window.configuration_dialog.width(), available)
            assert window.configuration_dialog.height() <= available.height() - 72, (
                window.configuration_dialog.width(), window.configuration_dialog.height(),
                (window.configuration_dialog.minimumWidth(), window.configuration_dialog.minimumHeight()),
                (window.configuration_dialog.maximumWidth(), window.configuration_dialog.maximumHeight()),
                window.configuration_dialog.frameGeometry(), available,
            )
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
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
