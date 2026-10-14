"""Application entry point."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QObject, QTimer, Qt, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from . import __version__
from .gui import MainWindow, WiringConfirmationDialog
from .modes import Mode
from .scpi import FlowControl, ModeSettings, Serial8845A, SerialSettings
from .worker import ACTUAL_RATE_WINDOW_POINTS


class _MockE2EReceiver(QObject):
    """Marshal worker completion callbacks onto the GUI thread."""

    def __init__(self, callback) -> None:
        super().__init__()
        self._callback = callback

    @Slot(str)
    def experiment_finished(self, directory: str) -> None:
        self._callback(directory)


def _application_icon_path() -> Path | None:
    """Find the app icon in source runs and the portable Windows package."""
    icon_name = Path("assets") / "fluke8845a_app_icon_v3.png"
    candidates = (
        Path(sys.argv[0]).resolve().parent / icon_name,
        Path(__file__).resolve().parents[2] / icon_name,
    )
    return next((path for path in candidates if path.is_file()), None)


def _reconstruct_gui_rate_from_rows(rows: list[dict], visible_sample_count: int) -> tuple[float, int]:
    """Rebuild the GUI's rolling receipt rate from the currently plotted CSV rows."""
    visible = [
        row for row in rows
        if row.get("status") == "ok" and row.get("value_si", "") != ""
    ][:visible_sample_count]
    if visible_sample_count < 2 or len(visible) != visible_sample_count:
        raise ValueError("可见有效点数不足，无法核验GUI实测频率")
    receipt_elapsed = []
    for row in visible:
        if row.get("read_duration_s", "") == "":
            raise ValueError("有效CSV读数缺少读取耗时，无法重建接收时刻")
        receipt_elapsed.append(float(row["elapsed_s"]) + float(row["read_duration_s"]))
    rate_window = receipt_elapsed[-ACTUAL_RATE_WINDOW_POINTS:]
    span = rate_window[-1] - rate_window[0]
    if span <= 0:
        raise ValueError("CSV接收时间戳不是严格递增")
    return (len(rate_window) - 1) / span, len(rate_window)


def run_real_hardware_probe(
    port: str,
    report_path: Path,
    *,
    wiring_confirmed: bool,
    shorted_leads_confirmed: bool = False,
    baudrate: int = 9600,
) -> int:
    """Perform one guarded RES2 read with the production driver and write a JSON report."""
    if wiring_confirmed and shorted_leads_confirmed:
        raise ValueError("30 MΩ电阻与短接线确认不能同时使用")
    confirmation = (
        "front_panel_2w_short" if shorted_leads_confirmed
        else "front_panel_2w_30mohm" if wiring_confirmed
        else None
    )
    report_path = report_path.expanduser().resolve()
    report: dict = {
        "software_version": __version__,
        "simulation": False,
        "probe_type": "single_real_res2_read",
        "port": port,
        "baudrate": baudrate,
        "started_local": datetime.now().astimezone().isoformat(timespec="microseconds"),
        "operator_wiring_confirmation": confirmation,
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({**report, "status": "starting"}, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        print(f"Hardware probe report already exists; choose a new path: {report_path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Unable to prepare hardware probe report before connecting: {exc}", file=sys.stderr)
        return 1
    communication_log: list[str] = []
    instrument: Serial8845A | None = None
    previous_settings: ModeSettings | None = None
    if confirmation is None:
        report.update(status="rejected", error="需要明确确认前面板2W端子当前接线状态。")
    else:
        try:
            settings = SerialSettings(
                baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
                flow_control=FlowControl.NONE, eol="CRLF", timeout_s=2.0,
            )
            instrument = Serial8845A(port, settings, communication_log.append)
            info = instrument.connect()
            report["device"] = asdict(info)
            state = instrument.read_mode_state()
            report["pre_configuration_state"] = state
            function = state["function"].strip().strip('"').upper()
            terminals = state["input_terminals"].strip().strip('"').upper()
            if function != "RES":
                raise RuntimeError(f"拒绝改变当前仪表功能：期望RES，实际{state['function']}")
            if not terminals.startswith("FRON"):
                raise RuntimeError(f"拒绝测试：期望前面板端子，实际{state['input_terminals']}")

            if shorted_leads_confirmed:
                previous_settings = instrument.read_settings(Mode.RES2)
                report["pre_configuration_settings"] = asdict(previous_settings)
                mode_settings = ModeSettings(
                    Mode.RES2, autorange=False, range_si=100, nplc=1,
                    analog_filter=False, autozero=True,
                )
                report["wiring"] = "用户确认前面板2W输入端子短接。"
            else:
                mode_settings = ModeSettings(
                    Mode.RES2, autorange=False, range_si=100e6, nplc=1,
                    analog_filter=False, autozero=True,
                )
                report["wiring"] = "用户确认前面板2W端子连接标称30 MΩ电阻。"
            applied = instrument.configure(mode_settings)
            report["requested_configuration"] = asdict(mode_settings)
            report["applied_configuration"] = applied
            readback_nplc = float(str(applied["nplc_response"]).strip().strip('"'))
            readback_range = float(str(applied["range_response"]).strip().strip('"'))
            expected_range = 100 if shorted_leads_confirmed else 100e6
            if abs(readback_nplc - 1) > 1e-12 or abs(readback_range - expected_range) > 1e-9:
                raise RuntimeError(
                    f"量程或NPLC回读与探测配置不符：range={applied['range_response']}, nplc={applied['nplc_response']}"
                )
            reading = instrument.read()
            report["sample"] = {
                "timestamp_local": reading.timestamp_local,
                "timestamp_utc": reading.timestamp_utc,
                "started_monotonic_ns": reading.started_monotonic_ns,
                "received_monotonic_ns": reading.received_monotonic_ns,
                "read_duration_s": reading.read_duration_s,
                "raw_reading": reading.raw,
                "value_si": reading.value_si,
                "unit_si": "Ohm",
                "range_si": reading.range_si,
                "status": reading.status,
            }
            report["status"] = "ok"
        except Exception as exc:
            report["status"] = "error"
            report["error"] = str(exc)
        finally:
            if instrument is not None and instrument.connected:
                if shorted_leads_confirmed and previous_settings is not None:
                    try:
                        report["restored_configuration"] = instrument.configure(previous_settings)
                    except Exception as restore_exc:
                        report["restore_error"] = str(restore_exc)
                        report["status"] = "error"
                instrument.close()
    report["communication_log"] = communication_log
    report["finished_local"] = datetime.now().astimezone().isoformat(timespec="microseconds")
    try:
        temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_report.replace(report_path)
    except OSError as exc:
        print(f"Unable to write hardware probe report: {exc}", file=sys.stderr)
        return 1
    return 0 if report.get("status") == "ok" else 1


def run_serial_runtime_smoke() -> int:
    """Import pyserial and enumerate ports, catching packaging failures without a GUI."""
    try:
        import serial  # noqa: F401 - explicit dynamic-package availability check
        from serial.tools import list_ports

        tuple(list_ports.comports())
    except Exception as exc:
        print(f"Serial runtime check failed: {exc}", file=sys.stderr)
        return 1
    return 0


def run_real_gui_speed_probe(
    port: str,
    report_path: Path,
    *,
    wiring_confirmed: bool,
    shorted_leads_confirmed: bool = False,
    baudrate: int = 9600,
    duration_s: float = 12.0,
    visible_window: bool = False,
) -> int:
    """Exercise the production GUI, worker thread and recorder on a guarded RES2 load."""
    if wiring_confirmed and shorted_leads_confirmed:
        raise ValueError("30 MΩ电阻与短接线确认不能同时使用")
    confirmation = (
        "front_panel_2w_short" if shorted_leads_confirmed
        else "front_panel_2w_30mohm" if wiring_confirmed
        else None
    )
    report_path = report_path.expanduser().resolve()
    report: dict = {
        "software_version": __version__,
        "simulation": False,
        "probe_type": (
            "real_gui_visible_res2_speed_and_interval_change"
            if visible_window else "real_gui_res2_speed_and_interval_change"
        ),
        "port": port,
        "baudrate": baudrate,
        "duration_s": duration_s,
        "visible_window_requested": visible_window,
        "started_local": datetime.now().astimezone().isoformat(timespec="microseconds"),
        "operator_wiring_confirmation": confirmation,
    }
    try:
        if confirmation is None:
            raise ValueError("必须明确确认前面板2W端子接着标称30 MΩ电阻或输入端子短接")
        if not 8.0 <= duration_s <= 3600.0:
            raise ValueError("GUI硬件探测运行时间必须在8到3600秒之间")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({**report, "status": "starting"}, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        print(f"Hardware GUI probe report already exists; choose a new path: {report_path}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Unable to prepare guarded GUI probe: {exc}", file=sys.stderr)
        return 1

    communication_log: list[str] = []
    errors: list[str] = []
    configuration_updates: list[dict] = []
    speed_results: list[tuple[bool, str]] = []
    interval_results: list[dict] = []
    experiment_directories: list[str] = []
    window = None
    app = None
    previous_settings: ModeSettings | None = None
    restore_error: str | None = None
    old_qt_platform = os.environ.get("QT_QPA_PLATFORM")
    old_config_dir = os.environ.get("FLUKE8845A_CONFIG_DIR")
    run_error: str | None = None
    try:
        # Snapshot the live configuration before the GUI worker connects. This is
        # read-only, and lets both guarded probe loads restore the operator's setup.
        probe_settings = SerialSettings(
            baudrate=baudrate, bytesize=8, parity="N", stopbits=1,
            flow_control=FlowControl.NONE, eol="CRLF", timeout_s=2.0,
        )
        snapshot = Serial8845A(port, probe_settings, communication_log.append)
        try:
            info = snapshot.connect()
            state = snapshot.read_mode_state()
            report["device"] = asdict(info)
            report["pre_configuration"] = state
            function = state["function"].strip().strip('"').upper()
            terminals = state["input_terminals"].strip().strip('"').upper()
            if function != "RES":
                raise RuntimeError(f"拒绝改变当前仪表功能：期望RES，实际{state['function']}")
            if not terminals.startswith("FRON"):
                raise RuntimeError(f"拒绝测试：期望前面板端子，实际{state['input_terminals']}")
            previous_settings = snapshot.read_settings(Mode.RES2)
            report["pre_configuration_settings"] = asdict(previous_settings)
        finally:
            snapshot.close()

        if visible_window:
            os.environ.pop("QT_QPA_PLATFORM", None)
        else:
            os.environ["QT_QPA_PLATFORM"] = "offscreen"
        with tempfile.TemporaryDirectory(prefix="Fluke8845A_gui_probe_config_") as private_config:
            os.environ["FLUKE8845A_CONFIG_DIR"] = private_config
            QApplication.setApplicationName("Fluke8845A Hardware GUI Probe")
            QApplication.setOrganizationName("Fluke8845A")
            app = QApplication.instance() or QApplication([sys.argv[0]])
            app.setQuitOnLastWindowClosed(False)
            window = MainWindow()
            if visible_window:
                window.showMaximized()
                window.raise_()
                window.activateWindow()
            else:
                window.hide()

            # Capture worker output without opening modal dialogs in this unattended diagnostic.
            window._worker.error.disconnect(window._show_error)
            window._worker.error.connect(
                lambda message: errors.append(str(message)), Qt.ConnectionType.DirectConnection
            )
            window._worker.log_line.connect(
                lambda message: communication_log.append(str(message)), Qt.ConnectionType.DirectConnection
            )
            window._worker.configuration_ready.connect(
                lambda details: configuration_updates.append(dict(details)), Qt.ConnectionType.DirectConnection
            )
            window._worker.measurement_speed_result.connect(
                lambda success, message: speed_results.append((bool(success), str(message))),
                Qt.ConnectionType.DirectConnection,
            )
            window._worker.experiment_finished.connect(
                lambda directory: experiment_directories.append(str(directory)),
                Qt.ConnectionType.DirectConnection,
            )

            window.port_combo.setCurrentText(port)
            window.baud_combo.setCurrentText(str(baudrate))
            window.data_bits_combo.setCurrentText("8")
            window.parity_combo.setCurrentIndex(window.parity_combo.findData("N"))
            window.stop_bits_combo.setCurrentText("1")
            window.flow_combo.setCurrentIndex(window.flow_combo.findData(FlowControl.NONE.value))
            window.eol_combo.setCurrentIndex(window.eol_combo.findData("CRLF"))
            window.timeout_spin.setValue(2.0)
            window.output_root.setText(str(report_path.parent / f"{report_path.stem}_experiment_runs"))
            window.experiment_name.setText("real_gui_speed_interval_probe")
            window.interval_spin.setValue(1.0)
            window._connect_real()

            def pump_until(predicate, timeout_s: float, description: str) -> None:
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline:
                    app.processEvents()
                    if errors:
                        raise RuntimeError(errors[-1])
                    if predicate():
                        return
                    time.sleep(0.005)
                raise TimeoutError(f"等待{description}超时")

            pump_until(lambda: window._connected, 15.0, "GUI仪器连接")
            if visible_window and (not window.isVisible() or not window.plot_widget.isVisible()):
                raise RuntimeError("可见GUI或实时曲线区域未显示")
            idn = window.idn_label.text()
            terminals = window._terminals.strip().strip('"').upper()
            if "FLUKE,8845A," not in idn.upper():
                raise RuntimeError(f"GUI识别身份不是Fluke 8845A：{idn}")
            if window._current_mode != Mode.RES2 or not terminals.startswith("FRON"):
                raise RuntimeError(
                    f"拒绝配置：需要当前功能RES和前面板端子；实际模式={window._current_mode.value}，端子={terminals}"
                )
            target_range = 100 if shorted_leads_confirmed else 100e6
            window.autorange_check.setChecked(False)
            range_index = window.range_combo.findData(target_range)
            if range_index < 0:
                raise RuntimeError(f"GUI没有提供8845A支持的{target_range:g} Ω固定量程")
            window.range_combo.setCurrentIndex(range_index)
            window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(1))
            window.analog_filter_check.setChecked(False)
            window.autozero_check.setChecked(True)
            prior_config_count = len(configuration_updates)
            window._apply_settings()
            pump_until(
                lambda: len(configuration_updates) > prior_config_count,
                10.0,
                "仪表配置与SCPI回读",
            )
            applied = configuration_updates[-1]
            actual_range = float(str(applied.get("range_response", "nan")).strip().strip('"'))
            actual_nplc = float(str(applied.get("nplc_response", "nan")).strip().strip('"'))
            if not math.isclose(actual_range, target_range, rel_tol=1e-9) or not math.isclose(actual_nplc, 1.0, abs_tol=1e-12):
                raise RuntimeError(f"GUI配置回读与探测设置不符：{applied}")
            report["device_idn"] = idn.removeprefix("设备身份：")
            report["wiring"] = (
                "用户确认前面板2W输入端子短接。" if shorted_leads_confirmed
                else "用户确认前面板2W端子连接标称30 MΩ电阻。"
            )
            report["initial_configuration_readback"] = applied

            # This CLI flag stands in for the normal GUI's wiring diagram confirmation,
            # after the instrument identity, function and terminals have been checked.
            with patch.object(
                WiringConfirmationDialog, "exec", return_value=QDialog.DialogCode.Accepted
            ):
                window._start_continuous()
            run_started = time.monotonic()
            first_change_sent = False
            second_change_sent = False
            first_interval_sent = False
            second_interval_sent = False
            first_change_at = min(2.0, duration_s * 0.22)
            second_change_at = min(6.0, duration_s * 0.55)
            first_interval_at = min(1.25, duration_s * 0.16)
            second_interval_at = min(4.5, duration_s * 0.45)
            while time.monotonic() - run_started < duration_s:
                app.processEvents()
                if errors:
                    raise RuntimeError(errors[-1])
                if not window._connected:
                    raise RuntimeError("连续采集期间仪器连接中断")
                elapsed = time.monotonic() - run_started
                if elapsed >= first_interval_at and not first_interval_sent:
                    prior_changes = sum("目标采样间隔由" in line for line in communication_log)
                    window.interval_spin.setValue(0.5)
                    pump_until(
                        lambda: sum("目标采样间隔由" in line for line in communication_log) > prior_changes,
                        5.0,
                        "目标采样间隔调整为0.5秒",
                    )
                    interval_results.append({"displayed_interval_s": 0.5, "logged": True})
                    first_interval_sent = True
                if elapsed >= second_interval_at and not second_interval_sent:
                    prior_changes = sum("目标采样间隔由" in line for line in communication_log)
                    window.interval_spin.setValue(0.0)
                    pump_until(
                        lambda: sum("目标采样间隔由" in line for line in communication_log) > prior_changes,
                        5.0,
                        "目标采样间隔调整为最快可用",
                    )
                    interval_results.append({"displayed_interval_s": 0.0, "logged": True})
                    second_interval_sent = True
                if elapsed >= first_change_at and not first_change_sent:
                    pump_until(lambda: not window._speed_update_pending, 5.0, "初始速度参数空闲")
                    window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(0.2))
                    window._apply_acquisition_speed()
                    first_change_sent = True
                if elapsed >= second_change_at and not second_change_sent:
                    pump_until(lambda: not window._speed_update_pending, 5.0, "恢复速度参数空闲")
                    window.nplc_combo.setCurrentIndex(window.nplc_combo.findData(1))
                    window._apply_acquisition_speed()
                    second_change_sent = True
                time.sleep(0.005)
            pump_until(lambda: not window._speed_update_pending, 5.0, "最后一次速度回读")
            if not first_change_sent or not second_change_sent or not first_interval_sent or not second_interval_sent:
                raise RuntimeError("GUI硬件探测没有完成两次速度及两次间隔变更")
            if len(speed_results) < 2 or not all(item[0] for item in speed_results[-2:]):
                raise RuntimeError(f"GUI实时调速未全部获仪表回读确认：{speed_results}")
            displayed_actual_rate = window.actual_rate_label.text()
            if not displayed_actual_rate.endswith(" Hz"):
                raise RuntimeError(f"GUI未显示有效的实测采样频率：{displayed_actual_rate}")
            report["displayed_actual_rate"] = displayed_actual_rate
            report["displayed_sample_count"] = len(window._plot_rows)
            window._stop()
            pump_until(lambda: bool(experiment_directories), 10.0, "实验停止并保存文件")
            pump_until(lambda: not window._acquiring, 5.0, "采集线程进入停止状态")
            report["experiment_directory"] = experiment_directories[-1]
            report["speed_change_results"] = [
                {"success": success, "message": message} for success, message in speed_results
            ]
            report["interval_change_results"] = interval_results
            report["collection_duration_s"] = time.monotonic() - run_started
            if visible_window:
                app.processEvents()
                plot_x, plot_y = window.curve.getData()
                screenshot_path = report_path.with_suffix(".png")
                screenshot = window.grab()
                if screenshot.isNull() or not screenshot.save(str(screenshot_path), "PNG"):
                    raise RuntimeError("无法保存可见GUI硬件验证截图")
                if plot_x is None or plot_y is None or len(plot_x) < 3 or len(plot_x) != len(plot_y):
                    raise RuntimeError("可见曲线未保留至少3个同步数据点")
                report["visible_gui"] = {
                    "window_visible": window.isVisible(),
                    "plot_visible": window.plot_widget.isVisible(),
                    "window_size_px": [window.width(), window.height()],
                    "plot_size_px": [window.plot_widget.width(), window.plot_widget.height()],
                    "curve_point_count": len(plot_x),
                    "curve_last_elapsed_s": float(plot_x[-1]),
                    "curve_last_value_si": float(plot_y[-1]),
                    "screenshot_path": str(screenshot_path),
                }
    except Exception as exc:
        run_error = str(exc)
    finally:
        if window is not None:
            try:
                if app is not None:
                    # Stop and flush an interrupted probe before restoring the
                    # instrument. Never configure the DMM while a READ? loop runs.
                    if window._acquiring:
                        window._stop()
                        stop_deadline = time.monotonic() + 15.0
                        while time.monotonic() < stop_deadline and window._acquiring:
                            app.processEvents()
                            time.sleep(0.01)
                    if window._acquiring:
                        restore_error = "停止连续采集超时，未安全写回探测前设置"
                    elif window._connected and previous_settings is not None:
                        previous_count = len(configuration_updates)
                        previous_error_count = len(errors)
                        window._bridge.configure.emit(previous_settings)
                        restore_deadline = time.monotonic() + 10.0
                        while time.monotonic() < restore_deadline:
                            app.processEvents()
                            if len(configuration_updates) > previous_count or len(errors) > previous_error_count:
                                break
                            time.sleep(0.01)
                        if len(configuration_updates) <= previous_count:
                            restore_error = (
                                errors[-1] if len(errors) > previous_error_count
                                else "等待探测前设置回读超时"
                            )
                        else:
                            restored = configuration_updates[-1]
                            expected = previous_settings
                            bool_value = lambda key: str(restored.get(key, "")).strip().strip('"').upper() in {"ON", "1"}
                            restored_range = float(str(restored.get("range_response", "nan")).strip().strip('"'))
                            matches = (
                                bool_value("autorange_response") == expected.autorange
                                and math.isclose(restored_range, float(restored_range if expected.autorange else expected.range_si), rel_tol=1e-9)
                                and math.isclose(float(str(restored.get("nplc_response", "nan")).strip().strip('"')), float(expected.nplc), abs_tol=1e-12)
                                and bool_value("autozero_response") == expected.autozero
                                and bool_value("analog_filter_response") == expected.analog_filter
                            )
                            if not matches:
                                restore_error = f"探测前参数回读不符：{restored}；期望={asdict(expected)}"
                            report["restored_configuration"] = restored
                    app.processEvents()
                window.close()
                if app is not None:
                    app.processEvents()
            except Exception as exc:
                run_error = run_error or f"关闭GUI/仪器失败：{exc}"
        if restore_error is not None:
            report["restore_error"] = restore_error
            run_error = run_error or restore_error
        if old_qt_platform is None:
            os.environ.pop("QT_QPA_PLATFORM", None)
        else:
            os.environ["QT_QPA_PLATFORM"] = old_qt_platform
        if old_config_dir is None:
            os.environ.pop("FLUKE8845A_CONFIG_DIR", None)
        else:
            os.environ["FLUKE8845A_CONFIG_DIR"] = old_config_dir

    report["communication_log"] = communication_log
    report["errors"] = errors
    report["finished_local"] = datetime.now().astimezone().isoformat(timespec="microseconds")
    report["experiment_directory"] = experiment_directories[-1] if experiment_directories else report.get("experiment_directory")
    try:
        if run_error is not None:
            report.update(status="error", error=run_error)
        else:
            experiment_directory = Path(str(report["experiment_directory"]))
            metadata = json.loads((experiment_directory / "config.json").read_text(encoding="utf-8"))
            with (experiment_directory / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            changes = metadata.get("measurement_speed_changes", [])
            interval_changes = metadata.get("target_interval_changes", [])
            if metadata.get("simulation") is not False:
                raise RuntimeError("实验元数据错误地标记为模拟数据")
            if metadata.get("mode") != Mode.RES2.value or not rows:
                raise RuntimeError("GUI实验没有产生RES2原始读数")
            invalid = [row for row in rows if row.get("status") != "ok" or not row.get("raw_reading")]
            if invalid:
                raise RuntimeError(f"GUI实验包含{len(invalid)}条异常/空响应测量记录")
            reconstructed_rate, rate_window_count = _reconstruct_gui_rate_from_rows(
                rows, int(report["displayed_sample_count"])
            )
            displayed_rate = float(str(report["displayed_actual_rate"]).removesuffix(" Hz"))
            if not math.isclose(displayed_rate, reconstructed_rate, rel_tol=1e-3):
                raise RuntimeError(
                    f"GUI频率显示与CSV单调时钟读数时间不符：显示{displayed_rate:g} Hz，"
                    f"CSV重建{reconstructed_rate:g} Hz"
                )
            if len(changes) != 2 or [change.get("nplc") for change in changes] != [0.2, 1]:
                raise RuntimeError(f"配置文件中的实时NPLC变更记录不符：{changes}")
            for change in changes:
                index = int(change["effective_sample_index"]) - 1
                if index < 0 or index >= len(rows) or not math.isclose(
                    float(rows[index]["nplc"]), float(change["nplc"]), abs_tol=1e-12
                ):
                    raise RuntimeError(f"第{index + 1}条CSV记录未反映生效NPLC={change['nplc']}")
            interval_values = [change.get("target_interval_s") for change in interval_changes]
            if len(interval_changes) != 2 or interval_values != [0.5, None]:
                raise RuntimeError(f"配置文件中的实时采样间隔变更记录不符：{interval_changes}")
            interval_observations = []
            for change in interval_changes:
                index = int(change["effective_sample_index"]) - 1
                if index < 1 or index >= len(rows):
                    raise RuntimeError(f"采样间隔变更对应的有效样本序号超出CSV范围：{change}")
                observed = rows[index].get("actual_interval_s", "")
                if not observed:
                    raise RuntimeError(f"第{index + 1}条CSV记录缺少实际间隔：{change}")
                interval_observations.append({
                    "target_interval_s": change["target_interval_s"],
                    "effective_sample_index": change["effective_sample_index"],
                    "actual_interval_s_at_effective_sample": float(observed),
                })
            if not any(line.strip().upper() == "> SYST:LOC" for line in communication_log):
                raise RuntimeError("GUI关闭时未观察到SYST:LOC释放仪器到LOCAL")
            report["sample_count"] = len(rows)
            report["invalid_count"] = len(invalid)
            report["csv_reconstructed_actual_rate_hz"] = reconstructed_rate
            report["csv_rate_window_sample_count"] = rate_window_count
            report["measurement_speed_changes"] = changes
            report["target_interval_changes"] = interval_changes
            report["interval_observations"] = interval_observations
            report["final_configuration"] = metadata.get("actual_device_configuration")
            report["status"] = "ok"
    except Exception as exc:
        report.update(status="error", error=str(exc))
    try:
        temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_report.replace(report_path)
    except OSError as exc:
        print(f"Unable to write GUI hardware probe report: {exc}", file=sys.stderr)
        return 1
    return 0 if report.get("status") == "ok" else 1


def run_mock_six_mode_gui_probe(report_path: Path) -> int:
    """Exercise the production GUI, worker, plots, and recorder in every Mock mode."""
    report_path = report_path.expanduser().resolve()
    report: dict = {
        "software_version": __version__,
        "simulation": True,
        "probe_type": "mock_gui_six_mode_e2e",
        "started_local": datetime.now().astimezone().isoformat(timespec="microseconds"),
        "modes": [],
    }
    executable = Path(sys.argv[0]).resolve()
    if executable.suffix.lower() == ".exe" and executable.is_file():
        with executable.open("rb") as stream:
            report["executable_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest().upper()
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with report_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({**report, "status": "starting"}, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        print(f"Mock GUI report already exists; choose a new path: {report_path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Unable to prepare Mock GUI report: {exc}", file=sys.stderr)
        return 1

    previous_config_dir = os.environ.get("FLUKE8845A_CONFIG_DIR")
    app: QApplication | None = None
    window: MainWindow | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="fluke8845a-mock-e2e-") as temporary_root:
            temporary_root_path = Path(temporary_root)
            os.environ["FLUKE8845A_CONFIG_DIR"] = str(temporary_root_path / "config")
            output_root = temporary_root_path / "experiments"
            QApplication.setApplicationName("Fluke 8845A Mock E2E")
            QApplication.setOrganizationName("Fluke8845A")
            app = QApplication(sys.argv[:1])
            window = MainWindow()
            window.mock_mode_check.setChecked(True)
            window.output_root.setText(str(output_root))
            window.interval_spin.setValue(0.0)

            modes = list(Mode)
            state = {
                "mode_index": 0,
                "phase": "connecting",
                "deadline": time.monotonic() + 20.0,
                "finished": False,
                "error": None,
            }
            gui_warnings: list[str] = []
            timer = QTimer()
            timer.setInterval(10)

            def finish(error: str | None = None) -> None:
                if state["finished"]:
                    return
                state["finished"] = True
                state["error"] = error
                report["status"] = "error" if error else "ok"
                if error:
                    report["error"] = error
                report["finished_local"] = datetime.now().astimezone().isoformat(timespec="microseconds")
                timer.stop()
                if window is not None:
                    window.close()
                if app is not None:
                    app.quit()

            def select_mode(mode: Mode) -> None:
                previous_signals = window.mode_combo.blockSignals(True)
                index = window.mode_combo.findData(mode.value)
                if index < 0:
                    window.mode_combo.blockSignals(previous_signals)
                    raise RuntimeError(f"GUI缺少测量模式：{mode.value}")
                window.mode_combo.setCurrentIndex(index)
                window.mode_combo.blockSignals(previous_signals)
                window._current_mode = mode
                window._load_mode_controls(mode)
                window._clear_plot()
                window.experiment_name.setText(f"冻结包Mock验收 {mode.value}")

            def start_current_mode() -> None:
                mode = modes[state["mode_index"]]
                select_mode(mode)
                state["phase"] = "acquiring"
                state["deadline"] = time.monotonic() + 15.0
                window._start_continuous()

            def on_experiment_finished(directory: str) -> None:
                if state["phase"] != "awaiting_finish":
                    finish(f"{modes[state['mode_index']].value}实验意外结束")
                    return
                mode = modes[state["mode_index"]]
                try:
                    run_directory = Path(directory)
                    with (run_directory / "measurements.csv").open(
                        newline="", encoding="utf-8-sig"
                    ) as stream:
                        rows = list(csv.DictReader(stream))
                    metadata = json.loads((run_directory / "config.json").read_text(encoding="utf-8"))
                    valid_rows = [row for row in rows if row.get("status") == "ok"]
                    if metadata.get("simulation") is not True or metadata.get("mode") != mode.value:
                        raise RuntimeError("实验配置没有正确标记Mock身份或测量模式")
                    if len(valid_rows) < 3 or len(valid_rows) != len(rows):
                        raise RuntimeError(f"有效记录不足或存在异常读数：{len(valid_rows)}/{len(rows)}")
                    if any(row.get("mode") != mode.value or not row.get("raw_reading") for row in rows):
                        raise RuntimeError("原始CSV测量模式不一致或缺少原始读数")
                    if not all((run_directory / name).is_file() for name in (
                        "events.csv", "run.log"
                    )):
                        raise RuntimeError("实验事件CSV或运行日志缺失")
                    report["modes"].append({
                        "mode": mode.value,
                        "sample_count": len(rows),
                        "valid_sample_count": len(valid_rows),
                        "simulation": metadata["simulation"],
                        "files_verified": [
                            "measurements.csv", "config.json", "events.csv", "run.log"
                        ],
                    })
                except Exception as exc:
                    finish(f"{mode.value}验收失败：{exc}")
                    return

                state["mode_index"] += 1
                if state["mode_index"] >= len(modes):
                    finish()
                    return
                try:
                    start_current_mode()
                except Exception as exc:
                    finish(str(exc))

            def on_warning(*args) -> None:
                if args:
                    gui_warnings.append(str(args[-1]))

            def pump() -> None:
                if state["finished"]:
                    return
                if gui_warnings:
                    finish(f"GUI操作告警：{gui_warnings[-1]}")
                    return
                if time.monotonic() > state["deadline"]:
                    phase = state["phase"]
                    finish(f"{modes[state['mode_index']].value}阶段超时（{phase}）")
                    return
                if state["phase"] == "connecting" and window._connected:
                    try:
                        start_current_mode()
                    except Exception as exc:
                        finish(str(exc))
                elif state["phase"] == "acquiring" and len(window._plot_rows) >= 3:
                    state["phase"] = "awaiting_finish"
                    state["deadline"] = time.monotonic() + 10.0
                    window._stop()

            receiver = _MockE2EReceiver(on_experiment_finished)
            window._worker.experiment_finished.connect(receiver.experiment_finished)
            timer.timeout.connect(pump)
            try:
                with patch.object(
                    WiringConfirmationDialog,
                    "exec",
                    return_value=QDialog.DialogCode.Accepted,
                ), patch.object(QMessageBox, "warning", side_effect=on_warning):
                    first_mode = modes[0]
                    select_mode(first_mode)
                    window._connect_mock()
                    timer.start()
                    QTimer.singleShot(90_000, lambda: finish("六种模式Mock验收总超时"))
                    app.exec()
            finally:
                timer.stop()
                if not state["finished"]:
                    finish("Mock GUI事件循环意外结束")
                window.close()
                app.processEvents()
            report["mode_count"] = len(report["modes"])
    except Exception as exc:
        report.update(status="error", error=str(exc))
    finally:
        if previous_config_dir is None:
            os.environ.pop("FLUKE8845A_CONFIG_DIR", None)
        else:
            os.environ["FLUKE8845A_CONFIG_DIR"] = previous_config_dir

    try:
        temporary_report = report_path.with_suffix(report_path.suffix + ".tmp")
        temporary_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_report.replace(report_path)
    except OSError as exc:
        print(f"Unable to write Mock GUI report: {exc}", file=sys.stderr)
        return 1
    return 0 if report.get("status") == "ok" and report.get("mode_count") == len(Mode) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fluke 8845A 科研测量与长期记录")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument("--mock", action="store_true", help="启动并连接明确标记的模拟仪器")
    backend.add_argument("--mock-e2e", action="store_true", help="后台逐模式验证Mock GUI采集及文件保存")
    backend.add_argument("--probe-real", action="store_true", help="真实仪器RES2安全连接及单点诊断；不显示GUI")
    backend.add_argument("--probe-real-gui", action="store_true", help="隐藏GUI连接并实测运行中NPLC调速、采样间隔与CSV记录")
    backend.add_argument("--probe-real-gui-visible", action="store_true", help="显示生产GUI并验证真实RES2曲线刷新；保存窗口截图")
    backend.add_argument("--serial-smoke", action="store_true", help="无GUI地导入pyserial并枚举端口；用于打包检查")
    parser.add_argument("--port", help="--probe-real使用的串口，例如COM3")
    parser.add_argument(
        "--baudrate", type=int, default=9600,
        choices=(1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400),
        help="--probe-real/--probe-real-gui串口波特率（默认9600）",
    )
    parser.add_argument("--report", type=Path, help="--probe-real/--probe-real-gui的JSON诊断报告路径")
    wiring = parser.add_mutually_exclusive_group()
    wiring.add_argument("--confirm-front-2w-30mohm", action="store_true",
                        help="确认前面板2W端子当前接着标称30 MOhm电阻")
    wiring.add_argument("--confirm-front-2w-short", action="store_true",
                        help="确认前面板2W输入端子当前短接；探测后恢复原仪器测量设置")
    parser.add_argument("--duration", type=float, default=12.0,
                        help="--probe-real-gui/--probe-real-gui-visible连续采集秒数（8至3600秒，默认12秒）")
    parser.add_argument("--smoke", action="store_true", help="启动界面短暂检查后退出")
    parser.add_argument("--version", action="version", version=f"Fluke8845A {__version__}")
    args = parser.parse_args(argv)
    if args.serial_smoke:
        if (
            args.smoke or args.port or args.report or args.confirm_front_2w_30mohm
            or args.confirm_front_2w_short or args.baudrate != 9600
        ):
            parser.error("--serial-smoke cannot be combined with other runtime modes or hardware options")
        return run_serial_runtime_smoke()
    if args.mock_e2e:
        if (
            args.smoke or args.port or args.confirm_front_2w_30mohm
            or args.confirm_front_2w_short or args.baudrate != 9600
        ):
            parser.error("--mock-e2e不能与--smoke或真实硬件选项组合")
        if not args.report:
            parser.error("--mock-e2e requires --report PATH")
        return run_mock_six_mode_gui_probe(args.report)
    if args.probe_real:
        if args.smoke:
            parser.error("--probe-real cannot be combined with --smoke")
        if not args.report:
            parser.error("--probe-real requires --report PATH")
        if not args.confirm_front_2w_30mohm and not args.confirm_front_2w_short:
            parser.error("--probe-real requires --confirm-front-2w-30mohm or --confirm-front-2w-short")
        return run_real_hardware_probe(args.port or "COM3", args.report,
                                       wiring_confirmed=args.confirm_front_2w_30mohm,
                                       shorted_leads_confirmed=args.confirm_front_2w_short,
                                       baudrate=args.baudrate)
    if args.probe_real_gui or args.probe_real_gui_visible:
        probe_name = "--probe-real-gui-visible" if args.probe_real_gui_visible else "--probe-real-gui"
        if args.smoke:
            parser.error(f"{probe_name} cannot be combined with --smoke")
        if not args.report:
            parser.error(f"{probe_name} requires --report PATH")
        if not args.confirm_front_2w_30mohm and not args.confirm_front_2w_short:
            parser.error(f"{probe_name} requires --confirm-front-2w-30mohm or --confirm-front-2w-short")
        if not math.isfinite(args.duration) or not 8 <= args.duration <= 3600:
            parser.error(f"--duration must be between 8 and 3600 seconds for {probe_name}")
        return run_real_gui_speed_probe(
            args.port or "COM3", args.report,
            wiring_confirmed=args.confirm_front_2w_30mohm,
            shorted_leads_confirmed=args.confirm_front_2w_short,
            baudrate=args.baudrate,
            duration_s=args.duration,
            visible_window=args.probe_real_gui_visible,
        )
    if (
        args.port or args.report or args.confirm_front_2w_30mohm
        or args.confirm_front_2w_short or args.baudrate != 9600
    ):
        parser.error("hardware connection options are only valid with a hardware probe")
    QApplication.setApplicationName("Fluke8845A Lab Logger")
    QApplication.setOrganizationName("Fluke8845A")
    app = QApplication(sys.argv[:1] + ([] if argv is None else []))
    icon_path = _application_icon_path()
    app_icon = QIcon(str(icon_path)) if icon_path else QIcon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    window = MainWindow()
    if not app_icon.isNull():
        window.setWindowIcon(app_icon)
    window.show()
    if args.mock:
        window.mock_mode_check.setChecked(True)
        window._connect_mock()
    if args.smoke:
        QTimer.singleShot(700, window.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

