"""Dedicated Qt thread for serial access, acquisition and incremental file writes."""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from .analysis import RunningStats
from .modes import Mode, descriptor, mode_from_scpi_function
from .mock import Mock8845A, MockScenario
from .scpi import (
    InstrumentError, InstrumentLocalModeError, ModeSettings, ReadingError,
    Rs232TerminalModeError, ScpiTimeout, Serial8845A, SerialSettings, UnsupportedRange,
    digital_filter_firmware_support, digital_filter_nplc_is_slow,
)
from .state import AcquisitionState, AcquisitionStateMachine, InvalidTransition
from .storage import ExperimentRecorder, prepare_experiment_storage, validate_experiment_name

log = logging.getLogger(__name__)
ACTUAL_RATE_WINDOW_POINTS = 21
MAX_PENDING_GUI_ROWS = 10_000
MAX_PENDING_GUI_SCPI_LOG_LINES = 5_000


class MeasurementWorker(QObject):
    connection_progress = Signal(str)
    connection_stage = Signal(str, str, str)
    connected_changed = Signal(bool, str)
    control_mode_changed = Signal(str)
    control_mode_result = Signal(bool, str)
    status_changed = Signal(str)
    log_line = Signal(str)
    scpi_logs_ready = Signal()
    configuration_ready = Signal(object)
    active_settings_ready = Signal(object)
    # A coalesced wake-up prevents the Qt event queue growing during GUI stalls.
    sample_ready = Signal()
    experiment_started = Signal(str)
    experiment_finished = Signal(str)
    experiment_save_result = Signal(str, bool, str)
    event_recorded = Signal(object)
    diagnostic_ready = Signal(str)
    measurement_speed_result = Signal(bool, str)
    zero_result = Signal(str)
    error = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.instrument: Serial8845A | Mock8845A | None = None
        self.info = None
        self.simulation = False
        self.control_mode = "disconnected"
        self.current_mode: Mode | None = None
        self._settings_by_mode: dict[Mode, ModeSettings] = {}
        self._machine = AcquisitionStateMachine()
        self._recorder: ExperimentRecorder | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._take_sample)
        self._target_interval_s: float | None = 1.0
        self._last_value: float | None = None
        self._valid_receipts_ns: deque[int] = deque(maxlen=ACTUAL_RATE_WINDOW_POINTS)
        self._consecutive_timeouts = 0
        self._one_shot = False
        self._pending_rows: list[dict] = []
        self._last_gui_emit_ns = 0
        self._running_stats = RunningStats()
        self._ui_mailbox_lock = Lock()
        self._ui_pending_rows: deque[dict] = deque(maxlen=MAX_PENDING_GUI_ROWS)
        self._ui_notification_pending = False
        self._ui_pending_scpi_logs: deque[tuple[str, str]] = deque(maxlen=MAX_PENDING_GUI_SCPI_LOG_LINES)
        self._ui_dropped_scpi_logs = 0
        self._ui_scpi_log_notification_pending = False
        self._ui_latest_hz = 0.0
        self._ui_latest_stats: dict = self._measurement_stats()
        self._stop_requested = Event()

    def _ready_status(self) -> str:
        if self.simulation:
            return "就绪（MOCK模拟）"
        if self.control_mode == "local":
            return "LOCAL 本地操作（前面板可用；软件测量、配置和ZERO已停用）"
        return "就绪（REMOTE远程控制；除LOCAL外面板按键已禁用）"

    def _require_computer_control(self) -> bool:
        if self.control_mode == "computer" or self.simulation:
            return True
        self.error.emit("仪表当前为LOCAL本地操作。请先切换到Computer软件控制，再执行此操作。")
        return False

    @property
    def state(self) -> AcquisitionState:
        return self._machine.state

    def _write_log(self, line: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        with self._ui_mailbox_lock:
            if len(self._ui_pending_scpi_logs) == MAX_PENDING_GUI_SCPI_LOG_LINES:
                self._ui_dropped_scpi_logs += 1
            self._ui_pending_scpi_logs.append((timestamp, line))
            notify = not self._ui_scpi_log_notification_pending
            if notify:
                self._ui_scpi_log_notification_pending = True
        if notify:
            self.scpi_logs_ready.emit()
        # Every raw reading is already preserved in measurements.csv. Keep the
        # run log bounded to setup commands and faults during long experiments.
        if self._recorder is not None and (line.startswith("! ") or (line.startswith("> ") and line != "> READ?")):
            try:
                self._recorder.log("SCPI", line)
            except OSError:
                log.exception("SCPI run-log write failed")

    def take_scpi_log_update(self) -> tuple[list[tuple[str, str]], int]:
        """Drain a bounded batch of SCPI UI logs without queuing one Qt event per line."""
        with self._ui_mailbox_lock:
            lines = list(self._ui_pending_scpi_logs)
            self._ui_pending_scpi_logs.clear()
            dropped = self._ui_dropped_scpi_logs
            self._ui_dropped_scpi_logs = 0
            self._ui_scpi_log_notification_pending = False
            return lines, dropped

    def _emit_active_settings(self, settings: ModeSettings) -> None:
        """Publish only settings confirmed by a readback or a verified configure call."""
        filter_state = None
        spec = descriptor(settings.mode)
        if spec.digital_filter:
            if self.simulation:
                filter_state = "simulation"
            else:
                firmware = ""
                if self.instrument is not None and hasattr(self.instrument, "range_firmware"):
                    firmware = self.instrument.range_firmware[0] or ""
                if digital_filter_firmware_support(settings.mode, firmware) is True:
                    filter_state = "readback_on" if settings.digital_filter else "readback_off"
                elif digital_filter_nplc_is_slow(settings.mode, settings.nplc):
                    filter_state = "unprobed"
                else:
                    filter_state = "unprobed_inactive"
        self.active_settings_ready.emit({
            "mode": settings.mode.value,
            "autorange": settings.autorange,
            "range_si": settings.range_si,
            "nplc": settings.nplc,
            "ac_bandwidth_hz": settings.ac_bandwidth_hz,
            "analog_filter": settings.analog_filter,
            "digital_filter": settings.digital_filter,
            "digital_filter_state": filter_state,
            "autozero": settings.autozero,
            "input_impedance_auto": (
                settings.input_impedance_auto
                if descriptor(settings.mode).input_impedance_auto else None
            ),
        })

    @Slot(str, object, object, bool)
    def connect_real(self, port: str, serial_settings: SerialSettings, mode_settings: ModeSettings, use_mock: bool = False) -> None:
        self._connect(port, serial_settings, mode_settings, use_mock, None)

    @Slot(object, object)
    def connect_mock(self, mode_settings: ModeSettings, scenario: MockScenario) -> None:
        self._connect("MOCK", SerialSettings(), mode_settings, True, scenario)

    @Slot(str, str)
    def set_firmware_versions(self, outguard_version: str, inguard_version: str) -> None:
        """Store operator-verified front-panel software versions without SCPI writes."""
        if self.state != AcquisitionState.STOPPED:
            self.error.emit("采集中不能修改面板软件版本记录")
            return
        if (
            self.simulation or self.instrument is None
            or not getattr(self.instrument, "connected", False)
            or not hasattr(self.instrument, "set_firmware_versions")
        ):
            return
        outguard_version = str(outguard_version or "").strip()
        inguard_version = str(inguard_version or "").strip()
        self.instrument.set_firmware_versions(outguard_version, inguard_version)
        self.log_line.emit(
            "已记录操作员从仪表面板核对的软件版本："
            f"OutG SW={outguard_version.strip() or '未核对'}，"
            f"InG SW={inguard_version.strip() or '未核对'}；未通过SCPI读取"
        )

    def _connect(self, port, serial_settings, mode_settings, use_mock, scenario) -> None:
        if self.instrument is not None and self.instrument.connected:
            self.error.emit("请先断开当前仪器连接")
            return
        try:
            self._connection_stage_states: dict[str, str] = {}
            self._emit_connection_stage("device", "running", f"已选择设备：{port}")
            self._emit_connection_stage("device", "success", f"设备 {port} 已选定")
            active_stage = "device"
            self.connection_progress.emit(
                "正在启动模拟器…" if use_mock else f"正在打开 {port} 并识别 Fluke 8845A…"
            )
            self.instrument = Mock8845A(scenario, self._write_log) if use_mock else Serial8845A(port, serial_settings, self._write_log)
            if use_mock:
                self._emit_connection_stage("port", "skipped", "MOCK 不打开真实串口")
                self._emit_connection_stage("identify", "skipped", "MOCK 不查询真实仪器身份")
                self.info = self.instrument.connect()
            else:
                self.info = self.instrument.connect(progress_callback=self._on_serial_connection_progress)
            active_stage = "configuration"
            self._emit_connection_stage(
                "configuration", "running",
                "模拟配置准备中…" if use_mock else "正在读取当前测量功能、端子和仪器参数…",
            )
            self.connection_progress.emit(
                f"模拟器 {getattr(scenario, 'name', 'Mock')} 已启动，正在准备模拟配置…" if use_mock else
                f"已识别 Fluke {self.info.model}，序列号 {self.info.serial_number or '未提供'}；正在读取当前测量功能和输入端子…"
            )
            self.simulation = use_mock
            self.control_mode = "computer"
            if use_mock:
                self.current_mode = mode_settings.mode
                self._settings_by_mode[mode_settings.mode] = mode_settings
                actual = self.instrument.configure(mode_settings)
                active_settings = mode_settings
                self.log_line.emit(f"MOCK模式已配置：{actual}")
            else:
                actual = self.instrument.read_mode_state()
                self.current_mode = mode_from_scpi_function(actual["function"])
                self._settings_by_mode.clear()
                active_settings = None
                if self.current_mode is not None:
                    try:
                        active_settings = self.instrument.read_settings(self.current_mode)
                    except ScpiTimeout:
                        raise
                    except Exception as exc:
                        self.log_line.emit(f"! 已连接，但当前设置回读失败：{exc}")
                self.log_line.emit(f"连接时只读当前仪器状态：{actual}")
            self._emit_connection_stage("configuration", "success", "当前测量功能和连接配置读取完成")
            active_stage = "ready"
            self._emit_connection_stage("ready", "running", "正在完成连接并启用界面…")
            self.connection_progress.emit(
                "模拟配置已准备，模拟器已就绪。" if use_mock else
                "当前测量功能和连接配置读取完成，仪器已就绪。"
            )
            self.configuration_ready.emit({"idn": self.info.idn_raw, **actual, "simulation": use_mock})
            if active_settings is not None:
                self._emit_active_settings(active_settings)
            self._emit_connection_stage("ready", "success", "连接已就绪")
            self.connected_changed.emit(True, f"{'模拟' if use_mock else '已连接'} · {self.info.idn_raw}")
            self.control_mode_changed.emit(self.control_mode)
            self.status_changed.emit(self._ready_status())
        except Exception as exc:
            failed_stage = getattr(self, "_connection_active_stage", locals().get("active_stage", "device"))
            if getattr(self, "_connection_stage_states", {}).get(failed_stage) != "failure":
                self._emit_connection_stage(failed_stage, "failure", f"{_connection_stage_label(failed_stage)}失败：{exc}")
            self.connection_progress.emit(f"连接失败：{exc}")
            self.log_line.emit(f"! 连接失败：{exc}")
            if self.instrument is not None:
                try:
                    self.instrument.close()
                except Exception:
                    pass
            self.instrument = None
            self.info = None
            self.control_mode = "disconnected"
            self.control_mode_changed.emit(self.control_mode)
            self.connected_changed.emit(False, "未连接")
            self.error.emit(str(exc))

    def _emit_connection_stage(self, stage: str, status: str, message: str) -> None:
        if status == "running" and stage == "device":
            self._connection_stage_states = {}
        if status == "running":
            self._connection_active_stage = stage
        self._connection_stage_states[stage] = status
        self.connection_stage.emit(stage, status, message)

    def _on_serial_connection_progress(self, stage: str, status: str, message: str) -> None:
        self._emit_connection_stage(stage, status, message)
        self.connection_progress.emit(message)


    @Slot(str)
    def set_control_mode(self, requested: str) -> None:
        mode = str(requested).strip().casefold()
        if mode not in {"computer", "local"}:
            self.control_mode_result.emit(False, "控制方式只能选择 Computer 或 Local")
            return
        if self.state != AcquisitionState.STOPPED:
            self.control_mode_result.emit(False, "请先停止采集，再切换仪器控制方式")
            self.error.emit("请先停止采集，再切换仪器控制方式")
            return
        if self.instrument is None or not self.instrument.connected:
            self.control_mode_result.emit(False, "请先连接8845A，再切换仪器控制方式")
            self.error.emit("请先连接8845A，再切换仪器控制方式")
            return
        if self.simulation:
            self.control_mode_result.emit(False, "MOCK模拟器没有仪器面板控制方式")
            self.error.emit("MOCK模拟器没有仪器面板控制方式")
            return
        if mode == self.control_mode:
            self.control_mode_result.emit(True, f"仪器已处于{'Computer' if mode == 'computer' else 'Local'}模式")
            return

        try:
            refreshed_info = self.instrument.set_control_mode(mode)
            if refreshed_info is not None:
                self.info = refreshed_info
            self.control_mode = mode
            if mode == "local":
                self.current_mode = None
                self._settings_by_mode.clear()
                self.configuration_ready.emit({
                    "idn": self.info.idn_raw, "function": "", "input_terminals": "",
                    "simulation": False,
                })
                message = "已发送SYST:LOC；仪表交由前面板操作，软件测量与配置已暂停"
            else:
                state = self.instrument.read_mode_state()
                self.current_mode = mode_from_scpi_function(state["function"])
                self._settings_by_mode.clear()
                active_settings = None
                if self.current_mode is not None:
                    try:
                        active_settings = self.instrument.read_settings(self.current_mode)
                    except ScpiTimeout:
                        raise
                    except Exception as exc:
                        self.log_line.emit(f"! 切回Computer后读取当前参数失败：{exc}")
                self.configuration_ready.emit({
                    "idn": self.info.idn_raw, **state, "simulation": False,
                })
                if active_settings is not None:
                    self._emit_active_settings(active_settings)
                message = "已切回Computer软件控制，并重新读取仪表当前状态"
            self.control_mode_changed.emit(self.control_mode)
            self.status_changed.emit(self._ready_status())
            self.log_line.emit(message)
            self.control_mode_result.emit(True, message)
        except Exception as exc:
            self.log_line.emit(f"! 切换仪器控制方式失败：{exc}")
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
            else:
                try:
                    self.instrument.close(restore_local=True)
                except Exception as close_exc:
                    self.log_line.emit(f"! 切换失败后释放仪器控制权失败：{close_exc}")
                self.instrument = None
                self.control_mode = "disconnected"
                self.control_mode_changed.emit(self.control_mode)
                self.connected_changed.emit(False, "控制方式切换失败，连接已关闭")
            self.error.emit(f"切换仪器控制方式失败：{exc}")
            self.control_mode_result.emit(False, str(exc))

    @Slot(object)
    def configure_mode(self, settings: ModeSettings) -> None:
        if self.state != AcquisitionState.STOPPED:
            self.error.emit("必须先停止采集，再切换测量模式")
            return
        if self.instrument is None or not self.instrument.connected:
            self.error.emit("请先连接8845A或启用MOCK模式")
            return
        if not self._require_computer_control():
            return
        try:
            actual = self.instrument.configure(settings)
            self.current_mode = settings.mode
            self._settings_by_mode[settings.mode] = settings
            self.configuration_ready.emit({"idn": self.info.idn_raw, **actual, "simulation": self.simulation})
            self._emit_active_settings(settings)
            self.log_line.emit(f"模式切换完成：{descriptor(settings.mode).label}")
            self.status_changed.emit(self._ready_status())
        except Exception as exc:
            self.log_line.emit(f"! 配置测量模式失败：{exc}")
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
            self.error.emit(str(exc))

    @Slot(object, str, str, object, bool)
    def start_experiment(self, mode_settings: ModeSettings, name: str, root: str, interval_s: float | None, one_shot: bool = False) -> None:
        if self.state != AcquisitionState.STOPPED:
            self.error.emit("当前已有实验运行，请先停止")
            return
        if self.instrument is None or not self.instrument.connected:
            self.error.emit("仪器尚未连接")
            return
        if not self._require_computer_control():
            return
        try:
            mode_settings.validate()
            if interval_s is not None and interval_s < 0:
                raise ValueError("目标采样间隔不能为负数")
            name = validate_experiment_name(name)
            experiment_root = prepare_experiment_storage(root)
            # Do all filesystem checks before issuing any configuration command.
            actual = self.instrument.configure(mode_settings)
            self.current_mode = mode_settings.mode
            self._settings_by_mode[mode_settings.mode] = mode_settings
            self._valid_receipts_ns.clear()
            self._last_value = None
            self._consecutive_timeouts = 0
            self._running_stats = RunningStats()
            with self._ui_mailbox_lock:
                self._ui_latest_stats = self._measurement_stats()
            self._pending_rows.clear()
            self._last_gui_emit_ns = 0
            self._target_interval_s = interval_s
            self._one_shot = one_shot
            self._stop_requested.clear()
            info = self.info
            panel_software = {
                "outguard_sw": getattr(self.instrument, "outguard_version", "") or None,
                "inguard_sw": getattr(self.instrument, "inguard_version", "") or None,
            }
            if hasattr(self.instrument, "range_firmware"):
                range_firmware, range_firmware_source = self.instrument.range_firmware
            else:
                range_firmware, range_firmware_source = (
                    info.firmware or "", "*IDN?" if info.firmware else "未知"
                )
            metadata = {
                "simulation": self.simulation,
                "control_mode": self.control_mode,
                "device": {"manufacturer": info.manufacturer, "model": info.model,
                           "serial_number": info.serial_number,
                           "firmware": info.firmware or None,
                           "idn_revision": info.idn_revision,
                           "firmware_source": "*IDN? semantic version" if info.firmware else None,
                           "front_panel_software": panel_software,
                           "range_firmware": range_firmware or None,
                           "range_firmware_source": range_firmware_source,
                           "idn_raw": info.idn_raw},
                "connection": {"port": "MOCK" if self.simulation else getattr(self.instrument, "port", ""),
                               "serial_settings": asdict(getattr(self.instrument, "settings", SerialSettings()))},
                "target_interval_s": interval_s,
                "initial_target_interval_s": interval_s,
                "target_interval_changes": [],
                "actual_interval_method": "PC monotonic timestamp delta from consecutive measurement attempts; frequency uses valid sample receipt timestamps.",
                "timestamp_definition": "timestamp_local/timestamp_utc and elapsed_s mark host command start. The instrument conversion is not hardware-timestamped; read_duration_s bounds serial command/read time.",
                "acquisition_method": "one READ? per point; the host reads every response before sending the next query.",
                "mock_scenario": asdict(self.instrument.scenario) if self.simulation else None,
            }
            self._recorder = ExperimentRecorder.create(experiment_root, name, metadata, mode_settings)
            self._recorder.metadata["actual_device_configuration"] = actual
            self._recorder._write_metadata()
            self._emit_active_settings(mode_settings)
            self.experiment_started.emit(str(self._recorder.directory))
            self._machine.start()
            self.status_changed.emit("单次测量中（MOCK模拟）" if self.simulation and one_shot else
                                     "连续采集中（MOCK模拟）" if self.simulation else
                                     "单次测量中" if one_shot else "连续采集中")
            self.log_line.emit(f"实验已启动：{self._recorder.directory}")
            self._recorder.log("INFO", f"采集目标间隔={interval_s}; 实际频率由时间戳计算")
            if one_shot:
                self._take_sample()
            else:
                self._timer.start(0)
        except Exception as exc:
            self.log_line.emit(f"! 实验启动失败：{exc}")
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
            if self._recorder is not None:
                self._finish_experiment("start_error")
            self.error.emit(str(exc))

    def _delay_to_next_attempt_ms(self, last_attempt_ns: int | None) -> int:
        interval = self._target_interval_s
        if interval is None or last_attempt_ns is None:
            return 0
        elapsed_s = (time.monotonic_ns() - last_attempt_ns) / 1e9
        return max(0, int((interval - elapsed_s) * 1000))

    @Slot(object)
    def set_target_interval(self, interval_s: float | None) -> dict:
        """Apply an interval edit and report its effective sample and host time."""
        try:
            interval = None if interval_s is None else float(interval_s)
            if interval is not None and (not math.isfinite(interval) or interval < 0):
                raise ValueError("目标采样间隔必须为非负有限数")
            if self.state not in (AcquisitionState.RUNNING, AcquisitionState.PAUSED) or self._recorder is None:
                return {
                    "success": True,
                    "changed": False,
                    "no_op": True,
                    "message": "采集未运行，目标采样间隔未变更。",
                }
            if interval == self._target_interval_s:
                return {
                    "success": True,
                    "changed": False,
                    "no_op": True,
                    "message": "目标采样间隔未变化；未记录新的生效样本。",
                }

            previous = self._target_interval_s
            self._target_interval_s = interval
            now_ns = time.monotonic_ns()
            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone()
            change = {
                "timestamp_local": now_local.isoformat(timespec="microseconds"),
                "timestamp_utc": now_utc.isoformat(timespec="microseconds"),
                "elapsed_s": max(0.0, (now_ns - (self._recorder.started_monotonic_ns or now_ns)) / 1e9),
                "effective_sample_index": self._recorder.sample_count + 1,
                "previous_interval_s": previous,
                "target_interval_s": interval,
            }
            self._recorder.metadata["target_interval_s"] = interval
            self._recorder.metadata.setdefault("target_interval_changes", []).append(change)
            previous_text = "最快可用" if previous is None else f"{previous:g} 秒"
            interval_text = "最快可用（0 秒额外等待）" if interval is None else f"{interval:g} 秒"
            message = (
                f"目标采样间隔由{previous_text}改为{interval_text}；从样本 {change['effective_sample_index']} 起生效"
                "（样本编号对应测量尝试，包含无效读数）；"
                f"主机时间 {change['timestamp_local']}；实验历时 {change['elapsed_s']:.3f} s"
            )
            self._recorder.append_event(message, value_si=self._last_value)
            self._recorder.log("INFO", message)
            self.log_line.emit(message)

            if self.state == AcquisitionState.RUNNING:
                self._timer.stop()
                self._timer.start(self._delay_to_next_attempt_ms(self._recorder.last_attempt_ns))
            return {
                "success": True,
                "changed": True,
                "no_op": False,
                "message": message,
                "effective_sample_index": change["effective_sample_index"],
                "host_timestamp_local": change["timestamp_local"],
                "host_timestamp_utc": change["timestamp_utc"],
                "elapsed_s": change["elapsed_s"],
            }
        except OSError as exc:
            self._storage_fault(exc)
            return {"success": False, "changed": False, "no_op": False, "message": str(exc)}
        except (TypeError, ValueError) as exc:
            self.error.emit(str(exc))
            return {"success": False, "changed": False, "no_op": False, "message": str(exc)}

    @Slot(object)
    def set_measurement_speed(self, requested: ModeSettings) -> None:
        """Apply NPLC/bandwidth only; range, function, filter and zero stay fixed."""
        if self.state not in (AcquisitionState.RUNNING, AcquisitionState.PAUSED):
            self.measurement_speed_result.emit(False, "请在连续采集或暂停期间调整测量速度")
            return
        if self._recorder is None or self.instrument is None or not self.instrument.connected:
            self.measurement_speed_result.emit(False, "实验记录或仪器连接不可用，无法调整速度")
            return
        if not self._require_computer_control():
            self.measurement_speed_result.emit(False, "请先切换到Computer软件控制，再调整测量速度")
            return
        try:
            requested.validate()
            if requested.mode != self._recorder.mode:
                raise ValueError("采集过程中不能更改测量模式")
            current = self._recorder.settings
            if (requested.nplc, requested.ac_bandwidth_hz) == (current.nplc, current.ac_bandwidth_hz):
                self.measurement_speed_result.emit(True, "当前测量速度已经是所选值")
                return

            actual = self.instrument.set_measurement_speed(requested)
            configuration = dict(self._recorder.metadata.get("actual_device_configuration", {}))
            configuration.update(actual)
            self._recorder.record_measurement_speed_change(
                nplc=requested.nplc,
                ac_bandwidth_hz=requested.ac_bandwidth_hz,
                device_configuration=configuration,
            )
            stored = self._settings_by_mode.get(requested.mode, current)
            applied_settings = replace(
                stored, nplc=requested.nplc, ac_bandwidth_hz=requested.ac_bandwidth_hz
            )
            self._settings_by_mode[requested.mode] = applied_settings
            self._emit_active_settings(applied_settings)
            parts = []
            if requested.nplc is not None:
                parts.append(f"NPLC={requested.nplc:g}")
            if requested.ac_bandwidth_hz is not None:
                parts.append(f"交流带宽={requested.ac_bandwidth_hz} Hz")
            self.measurement_speed_result.emit(True, f"测量速度已回读确认：{'，'.join(parts)}；从下一读数起生效")
        except UnsupportedRange as exc:
            self.log_line.emit(f"~ 实时调速未应用：{exc}")
            self.measurement_speed_result.emit(False, f"实时调速未应用，采集继续：{exc}")
        except OSError as exc:
            self._storage_fault(exc)
            self.measurement_speed_result.emit(False, f"调速记录写入失败：{exc}")
        except Exception as exc:
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
            self.log_line.emit(f"! 实时调速失败，采集已停止：{exc}")
            reconnect = "响应超时，串口已关闭，请重新连接。" if isinstance(exc, ScpiTimeout) else ""
            self.error.emit(f"实时调速失败：{exc}。{reconnect}为避免数据条件不明，当前采集已停止。")
            try:
                if self.state != AcquisitionState.STOPPED:
                    self._machine.stop()
                self._finish_experiment("speed_update_error")
            except Exception:
                log.exception("could not safely stop after speed update failure")
            self.measurement_speed_result.emit(False, f"实时调速失败，采集已停止：{exc}")

    @Slot()
    def pause(self) -> None:
        try:
            self._machine.pause()
            self._timer.stop()
            self.status_changed.emit("已暂停")
            if self._recorder:
                self._recorder.append_event("暂停采集")
        except InvalidTransition as exc:
            self.error.emit(str(exc))
        except OSError as exc:
            self._storage_fault(exc)

    @Slot()
    def resume(self) -> None:
        try:
            self._machine.resume()
            self.status_changed.emit("连续采集中（MOCK模拟）" if self.simulation else "连续采集中")
            if self._recorder:
                self._recorder.append_event("继续采集")
            last_attempt_ns = self._recorder.last_attempt_ns if self._recorder else None
            self._timer.start(self._delay_to_next_attempt_ms(last_attempt_ns))
        except InvalidTransition as exc:
            self.error.emit(str(exc))
        except OSError as exc:
            self._storage_fault(exc)

    @Slot()
    def stop(self) -> None:
        self._stop_requested.clear()
        self._timer.stop()
        try:
            self._machine.stop()
        except InvalidTransition:
            return
        self._finish_experiment("stopped")

    def request_stop(self) -> None:
        """Thread-safe stop request used to interrupt zero-delay timer churn."""
        self._stop_requested.set()

    def _take_sample(self) -> None:
        if self.state != AcquisitionState.RUNNING or self._recorder is None or self.instrument is None:
            return
        if self._stop_requested.is_set():
            self.stop()
            return
        routine_start_ns = time.monotonic_ns()
        host_time = datetime.now(timezone.utc)
        host_utc = host_time.isoformat(timespec="microseconds")
        host_local = host_time.astimezone().isoformat(timespec="microseconds")
        try:
            self._recorder.check_storage_reserve()
        except OSError as storage_exc:
            self._storage_fault(storage_exc)
            return
        try:
            reading = self.instrument.read()
        except Exception as exc:
            code = ("instrument_local" if isinstance(exc, InstrumentLocalModeError) else
                    "timeout" if isinstance(exc, (TimeoutError, ScpiTimeout)) else
                    "overflow" if isinstance(exc, ReadingError) and exc.status == "overflow" else
                    "invalid" if isinstance(exc, ReadingError) else "communication_error")
            now_ns = time.monotonic_ns()
            try:
                row = self._recorder.append_reading(
                    None, mode=self._recorder.mode, started_monotonic_ns=routine_start_ns,
                    timestamp_local=host_local,
                    timestamp_utc=host_utc,
                    read_duration_s=(now_ns - routine_start_ns) / 1e9,
                    status=code, error=str(exc),
                    raw_reading=getattr(exc, "raw", getattr(exc, "partial_response", "")),
                )
            except OSError as storage_exc:
                self._storage_fault(storage_exc)
                return
            try:
                self._recorder.log("ERROR", str(exc))
            except OSError as storage_exc:
                self._storage_fault(storage_exc)
                return
            self.log_line.emit(f"! {code}：{exc}")
            self._queue_ui_row(row)
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
                self._finish_experiment("scpi_timeout")
                self.status_changed.emit("SCPI响应超时，已停止采集并关闭连接；请重新连接")
                return
            if code == "timeout":
                self._consecutive_timeouts += 1
            else:
                self._consecutive_timeouts = 0
            if not self.instrument.connected:
                self.connected_changed.emit(False, "连接中断")
                try:
                    self.instrument.close()
                except Exception:
                    pass
                self._finish_experiment("disconnected")
                return
            if code == "instrument_local":
                self.connected_changed.emit(False, "仪器已切回LOCAL本地控制")
                try:
                    self.instrument.close(restore_local=False)
                except Exception:
                    log.exception("could not close the LOCAL-mode serial session")
                self.control_mode = "disconnected"
                self.control_mode_changed.emit(self.control_mode)
                self._finish_experiment("instrument_local")
                self.status_changed.emit("仪器已切回LOCAL；采集已停止，请重新连接恢复远程控制")
                return
            if isinstance(exc, Rs232TerminalModeError):
                self.connected_changed.emit(
                    False, "RS-232终端模式；串口已关闭，请切换到Computer模式"
                )
                try:
                    self.instrument.close(restore_local=False)
                except Exception:
                    log.exception("could not close the RS-232 terminal-mode session")
                self.control_mode = "disconnected"
                self.control_mode_changed.emit(self.control_mode)
                self._finish_experiment("rs232_terminal_mode")
                self.status_changed.emit(
                    "RS-232当前不是Computer模式；串口已关闭，请在仪表上切换后重新连接"
                )
                return
            # The Fluke 8845A current-input overload must not lead to another
            # continuous READ?. See Extended Specifications Rev. 3, PDF pp.7–8
            # (printed pp.6–7), DCI/ACI overrange limits.
            if code == "overflow" and descriptor(self._recorder.mode).quantity == "current":
                self._finish_experiment("current_overrange")
                self.status_changed.emit(
                    "电流读数出现仪器过量程，已停止连续采集；请检查预期电流、量程及电流输入端子，确认回路安全后再重新开始"
                )
                return
            if code == "timeout" and self._consecutive_timeouts >= 3:
                self._finish_experiment("repeated_timeouts")
                self.status_changed.emit("连续超时，采集已停止；请检查连接后重试")
                return
            if code == "communication_error" and isinstance(exc, InstrumentError):
                self._finish_experiment("instrument_error")
                return
            if self._one_shot:
                self.stop()
                return
        else:
            try:
                row = self._recorder.append_reading(reading, mode=self._recorder.mode)
            except OSError as storage_exc:
                self._storage_fault(storage_exc)
                return
            self._last_value = reading.value_si
            self._valid_receipts_ns.append(reading.received_monotonic_ns)
            self._running_stats.add(reading.value_si)
            self._consecutive_timeouts = 0
            self._queue_ui_row(row)
            if self._one_shot:
                self.stop()
                return
        if self.state != AcquisitionState.RUNNING:
            return
        if self._stop_requested.is_set():
            self.stop()
            return
        if self._target_interval_s is None:
            delay_ms = 0
        else:
            elapsed = (time.monotonic_ns() - routine_start_ns) / 1e9
            delay_ms = max(0, int((self._target_interval_s - elapsed) * 1000))
        self._timer.start(delay_ms)

    def _close_after_scpi_timeout(self) -> None:
        """Close a desynchronized serial session before another query can consume late bytes."""
        instrument = self.instrument
        if instrument is not None:
            try:
                instrument.close()
            except Exception as exc:
                self.log_line.emit(f"! 超时后关闭串口失败：{exc}")
        self.control_mode = "disconnected"
        self.control_mode_changed.emit(self.control_mode)
        self.connected_changed.emit(False, "SCPI响应超时，连接已关闭")
        self.status_changed.emit("SCPI响应超时，串口连接已关闭；请重新连接")

    def _queue_ui_row(self, row: dict) -> None:
        self._pending_rows.append(row)
        now = time.monotonic_ns()
        if self._last_gui_emit_ns == 0 or now - self._last_gui_emit_ns >= 100_000_000 or row.get("status") != "ok":
            self._flush_ui_rows()

    def _flush_ui_rows(self, *, attempt_count: int | None = None) -> None:
        if self._pending_rows:
            rows, self._pending_rows = self._pending_rows, []
            latest_stats = self._measurement_stats(attempt_count=attempt_count)
            with self._ui_mailbox_lock:
                self._ui_pending_rows.extend(rows)
                self._ui_latest_hz = self._actual_hz()
                self._ui_latest_stats = latest_stats
                notify = not self._ui_notification_pending
                if notify:
                    self._ui_notification_pending = True
            if notify:
                self.sample_ready.emit()
        self._last_gui_emit_ns = time.monotonic_ns()

    def _measurement_stats(self, *, attempt_count: int | None = None) -> dict:
        """Build a bounded telemetry snapshot without changing recorded readings."""
        stats = self._running_stats.as_dict()
        valid_count = self._running_stats.count
        if attempt_count is None:
            attempt_count = self._recorder.sample_count if self._recorder is not None else 0
        stats.update({
            "attempt_count": attempt_count,
            "valid_count": valid_count,
            "abnormal_count": max(0, attempt_count - valid_count),
        })

        receipts = tuple(self._valid_receipts_ns)
        intervals_s = [
            (receipts[index] - receipts[index - 1]) / 1e9
            for index in range(1, len(receipts))
        ]
        if intervals_s:
            minimum = min(intervals_s)
            maximum = max(intervals_s)
            stats.update({
                "interval_median_s": statistics.median(intervals_s),
                "interval_min_s": minimum,
                "interval_max_s": maximum,
                "interval_range_jitter_s": maximum - minimum,
            })
        else:
            stats.update({
                "interval_median_s": None,
                "interval_min_s": None,
                "interval_max_s": None,
                "interval_range_jitter_s": None,
            })
        return stats

    def take_sample_update(self) -> tuple[list[dict], float, dict]:
        """Atomically drain the bounded display mailbox from the GUI thread.

        The worker writes every attempt to disk before it reaches this mailbox.
        If a GUI stalls, only the most recent MAX_PENDING_GUI_ROWS are retained
        for display; the original CSV and streaming statistics remain complete.
        """
        with self._ui_mailbox_lock:
            rows = list(self._ui_pending_rows)
            self._ui_pending_rows.clear()
            self._ui_notification_pending = False
            return rows, self._ui_latest_hz, dict(self._ui_latest_stats)

    def _actual_hz(self) -> float:
        if len(self._valid_receipts_ns) < 2:
            return 0.0
        span = (self._valid_receipts_ns[-1] - self._valid_receipts_ns[0]) / 1e9
        return (len(self._valid_receipts_ns) - 1) / span if span > 0 else 0.0

    def _storage_fault(self, exc: OSError) -> None:
        self.log_line.emit(f"! CSV写入失败，已停止采集：{exc}")
        log.exception("experiment storage failed")
        self._timer.stop()
        if self.state != AcquisitionState.STOPPED:
            try:
                self._machine.stop()
            except InvalidTransition:
                pass
        self.status_changed.emit("数据写入失败，采集已停止；检查磁盘空间和文件权限")
        self.error.emit(f"测量数据无法写入磁盘：{exc}")
        self._finish_experiment("storage_error")

    @Slot(str)
    def mark_event(self, text: str) -> dict | None:
        if self._recorder is None:
            self.error.emit("请先开始一次实验，再添加事件")
            return None
        try:
            event = self._recorder.append_event(text, value_si=self._last_value)
            self.log_line.emit(f"事件已记录：{text}")
            self.event_recorded.emit(event)
            return event
        except OSError as exc:
            self._storage_fault(exc)
        except Exception as exc:
            self.error.emit(str(exc))
        return None

    @Slot()
    def zero(self) -> None:
        if self.state != AcquisitionState.STOPPED:
            self.error.emit("请停止采集后再执行ZERO")
            return
        if self.instrument is None or not self.instrument.connected:
            self.error.emit("仪器未连接")
            return
        if not self._require_computer_control():
            return
        try:
            self.instrument.zero()
            if self.current_mode is None:
                self.zero_result.emit("ZERO:AUTO ONCE 命令已发送并检查错误队列；当前测量模式未知，无法回读配置。")
                return
            try:
                actual = self.instrument.read_settings(self.current_mode)
                self._emit_active_settings(actual)
                pieces = [f"模式 {actual.mode.value}"]
                pieces.append("自动量程" if actual.autorange else f"固定量程 {actual.range_si:g}")
                if actual.nplc is not None:
                    pieces.append(f"{actual.nplc:g} NPLC")
                if actual.ac_bandwidth_hz is not None:
                    pieces.append(f"交流带宽 {actual.ac_bandwidth_hz} Hz")
                pieces.append(f"模拟滤波 {'开' if actual.analog_filter else '关'}")
                pieces.append(f"自动零点 {'开' if actual.autozero else '关'}")
                self.zero_result.emit(
                    "ZERO:AUTO ONCE 命令已发送且错误队列无报错；执行后配置回读：" + " · ".join(pieces)
                    + "。此回读只确认配置；本软件没有零点值查询项，不代表已取得ZERO测量结果。"
                )
            except Exception as readback_error:
                if isinstance(readback_error, ScpiTimeout):
                    self._close_after_scpi_timeout()
                self.zero_result.emit(
                    f"ZERO:AUTO ONCE 命令已发送且错误队列无报错，但执行后配置回读失败：{readback_error}。"
                )
            self.status_changed.emit("ZERO命令已发送；配置回读结果见状态提示")
        except Exception as exc:
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
            self.error.emit(f"ZERO失败：{exc}")

    @Slot()
    def diagnose(self) -> None:
        if self.instrument is None or not self.instrument.connected:
            self.error.emit("仪器未连接")
            return
        if not self._require_computer_control():
            return
        try:
            result = self.instrument.diagnose()
            self.diagnostic_ready.emit("\n".join(f"{key}: {value}" for key, value in result.items()))
        except Exception as exc:
            if isinstance(exc, ScpiTimeout):
                self._close_after_scpi_timeout()
            self.error.emit(f"诊断查询失败：{exc}")

    def _finish_experiment(self, status: str) -> None:
        self._timer.stop()
        recorder, self._recorder = self._recorder, None
        finalization_error: Exception | None = None
        result_message = ""
        if recorder is not None:
            directory = str(recorder.directory)
            self._flush_ui_rows(attempt_count=recorder.sample_count)
            try:
                recorder.finish(status)
            except Exception as exc:
                finalization_error = exc
                result_message = f"实验文件关闭或元数据写入失败：{exc}；记录可能不完整：{directory}"
                self.log_line.emit(f"! {result_message}")
                log.exception("could not finalize experiment storage")
                self.error.emit(result_message)
            else:
                if status == "storage_error":
                    result_message = f"实验已停止；数据记录可能不完整：{directory}"
                    self.log_line.emit(f"! {result_message}")
                elif status == "start_error":
                    result_message = f"实验启动失败；数据记录可能不完整：{directory}"
                    self.log_line.emit(f"! {result_message}")
                else:
                    result_message = f"实验数据已保存：{directory}"
                    self.log_line.emit(f"实验保存完成：{directory}")
        if self.state != AcquisitionState.STOPPED:
            self._machine.stop()
        if recorder is not None:
            self.experiment_finished.emit(directory)
            saved_ok = finalization_error is None and status not in {"storage_error", "start_error"}
            self.experiment_save_result.emit(directory, saved_ok, result_message)
        if finalization_error is not None:
            self.status_changed.emit(result_message)
        elif status in {"storage_error", "start_error"}:
            self.status_changed.emit(result_message)
        elif self.instrument is None or not self.instrument.connected:
            self.status_changed.emit("连接中断")
        else:
            self.status_changed.emit(self._ready_status())

    @Slot()
    def shutdown(self) -> None:
        self._timer.stop()
        if self._recorder is not None:
            if self.state != AcquisitionState.STOPPED:
                self._machine.stop()
            self._finish_experiment("application_exit")
        if self.instrument is not None:
            try:
                self.instrument.close()
            except Exception as exc:
                self.log_line.emit(f"! 关闭仪器失败：{exc}")
            self.instrument = None
        self.control_mode = "disconnected"
        self.control_mode_changed.emit(self.control_mode)
        self.connected_changed.emit(False, "已安全退出")

    @Slot()
    def shutdown_app(self) -> None:
        self.shutdown()
        QThread.currentThread().quit()


def _connection_stage_label(stage: str) -> str:
    return {
        "device": "设备选择", "port": "串口打开", "identify": "8845A 识别",
        "configuration": "配置读取", "ready": "就绪",
    }.get(stage, "连接")

