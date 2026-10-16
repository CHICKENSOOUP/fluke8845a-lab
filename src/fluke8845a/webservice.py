"""Thread-safe bridge from the localhost API to the production acquisition worker."""

from __future__ import annotations

import math
import re
import threading
import time
import uuid
from collections import deque
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices

from .mock import Mock8845A, MockScenario
from .modes import DESCRIPTORS, Mode, descriptor, range_label
from .scpi import (
    CURRENT_RANGE_FIRMWARE_MINIMUM, FRONT_ONLY_CURRENT_RANGES,
    DIGITAL_FILTER_DISABLED_REASON, FlowControl, ModeSettings, SerialSettings,
    compare_firmware, current_autorange_block_reason, digital_filter_blocked_nplc_values,
    digital_filter_slow_read_block_reason, select_range_firmware, unsupported_current_ranges,
)
from .storage import (
    find_existing_experiment_directories, format_available_space, get_available_space_bytes,
    prepare_experiment_storage, require_minimum_free_space, validate_experiment_name,
)
from .state import AcquisitionState
from .worker import MeasurementWorker

_SAMPLE_BUFFER_SIZE = 5000
_LOG_BUFFER_SIZE = 300
_EVENT_BUFFER_SIZE = 200


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
    if payload.get("digital_filter", False) not in (False, None):
        if not spec.digital_filter:
            raise ValueError(f"{spec.label}不支持直流数字滤波")
        raise ValueError(DIGITAL_FILTER_DISABLED_REASON)
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
        digital_filter=False,
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
    """Synchronous HTTP-to-Qt request broker with a bounded wait per command."""

    dispatch = Signal(str, str, object)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, dict | None]] = {}

    def bind(self, router: "WebCommandRouter") -> None:
        self.dispatch.connect(router.execute, Qt.ConnectionType.QueuedConnection)
        router.command_completed.connect(self._complete, Qt.ConnectionType.QueuedConnection)

    def request(self, action: str, payload: dict, timeout_s: float = 20.0) -> dict:
        request_id = uuid.uuid4().hex
        ready = threading.Event()
        with self._lock:
            self._pending[request_id] = (ready, None)
        self.dispatch.emit(request_id, action, payload)
        if not ready.wait(timeout_s):
            with self._lock:
                self._pending.pop(request_id, None)
            return {"success": False, "message": "仪器命令等待超时；请检查状态后再操作"}
        with self._lock:
            _event, result = self._pending.pop(request_id, (ready, None))
        return result or {"success": False, "message": "命令没有返回结果"}

    @Slot(str, object)
    def _complete(self, request_id: str, result: dict) -> None:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return
            ready, _old_result = pending
            self._pending[request_id] = (ready, result)
            ready.set()


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
            range_firmware, range_source = select_range_firmware(info.firmware, "")
        return {
            "idn": info.idn_raw,
            "serial_number": info.serial_number,
            "firmware": info.firmware or None,
            "idn_revision": info.idn_revision,
            "front_panel_software": {
                "outguard_sw": outguard or None,
                "inguard_sw": inguard or None,
            },
            "range_firmware": range_firmware or None,
            "range_firmware_source": range_source,
            "digital_filter_blocked_nplc_values": {
                mode.value: digital_filter_blocked_nplc_values(mode, range_firmware)
                for mode in Mode if descriptor(mode).digital_filter
            },
            "digital_filter_slow_read_block_reasons": {
                mode.value: reason
                for mode in Mode if descriptor(mode).digital_filter
                for reason in [digital_filter_slow_read_block_reason(
                    mode,
                    next((value for value in descriptor(mode).nplc_values if value > 1), None),
                    range_firmware,
                )]
                if reason
            },
        }

    @staticmethod
    def _version_text(value, label: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{label}必须是文本")
        text = value.strip()
        if len(text) > 64:
            raise ValueError(f"{label}最多64个字符")
        return text

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
            if value in FRONT_ONLY_CURRENT_RANGES and not terminals.strip().upper().startswith("FRON"):
                reasons.append(f"端子 {terminals or '未确认'} 不支持该前面板档位")
            if reasons:
                conflicts.append(
                    f"量程：{range_label(value, spec.unit_si)} 与最近回读的固件/端子冲突（{'；'.join(reasons)}）"
                )
        return conflicts, basis

    @Slot(str, str, object)
    def execute(self, request_id: str, action: str, payload: dict) -> None:
        try:
            result = self._execute(action, payload if isinstance(payload, dict) else {})
        except _KnownConfigurationConflict as exc:
            result = self._configuration_conflict_response(exc.conflicts, exc.readback_basis)
        except (TypeError, ValueError, OSError, RuntimeError) as exc:
            result = {"success": False, "message": str(exc)}
        except Exception as exc:  # Preserve API availability after an unexpected backend exception.
            result = {"success": False, "message": f"操作失败：{exc}"}
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

        if action == "connect_real":
            if worker.instrument is not None and worker.instrument.connected:
                raise ValueError("已连接，请先断开当前仪器")
            port = str(payload.get("port", "")).strip().upper()
            if not re.fullmatch(r"COM[1-9][0-9]*", port):
                raise ValueError("请输入有效的 Windows 串口名，例如 COM3")
            settings = serial_settings_from_payload(payload.get("serial", {}))
            mode_settings = mode_settings_from_payload(payload.get("settings", {"mode": "DCV"}))
            worker.connect_real(port, settings, mode_settings, False)
            error = check_worker_error("仪器连接失败")
            if error:
                return error
            profiles = payload.get("front_panel_software_by_serial", {})
            if isinstance(profiles, dict) and worker.info is not None:
                versions = profiles.get(worker.info.serial_number, {})
                if isinstance(versions, dict):
                    outguard = self._version_text(versions.get("outguard_sw", ""), "OutG SW")
                    inguard = self._version_text(versions.get("inguard_sw", ""), "InG SW")
                    if outguard or inguard:
                        worker.set_firmware_versions(outguard, inguard)
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
            checks, duplicate_name = self._preflight(payload, worker)
            return {
                "success": True, "message": "采集前检查通过", "checks": checks,
                "duplicate_name": duplicate_name,
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
            worker.configure_mode(settings)
            error = check_worker_error("配置仪器参数失败")
            return error or {"success": True, "message": f"已应用并核对 {descriptor(settings.mode).label} 参数"}

        if action in ("start", "single"):
            self._preflight(payload, worker)
            settings = mode_settings_from_payload(payload.get("settings", {}))
            name = validate_experiment_name(str(payload.get("name", "")))
            root = Path(str(payload.get("root", "")).strip()).expanduser()
            interval = self._interval(payload.get("interval_s", 1.0))
            worker.start_experiment(settings, name, str(root), interval, action == "single")
            error = check_worker_error("实验启动失败")
            if error:
                return error
            if worker.state == AcquisitionState.STOPPED and action == "start":
                return {"success": False, "message": "实验没有进入采集状态"}
            return {"success": True, "message": "单次测量已启动" if action == "single" else "连续采集已启动"}

        if action == "interval":
            interval = self._interval(payload.get("interval_s"))
            result = worker.set_target_interval(interval)
            error = check_worker_error("采样间隔更新失败")
            return error or result

        if action == "speed":
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
            if not payload.get("zero_confirmed") is True:
                raise ValueError("请先在页面确认已核对 ZERO 条件")
            if worker.state != AcquisitionState.STOPPED:
                raise ValueError("请停止采集后再执行 ZERO")
            worker.zero()
            error = check_worker_error("ZERO执行失败")
            return error or {"success": True, "message": "ZERO命令已发送；执行后配置回读会显示在状态区。"}

        if action == "diagnose":
            worker.diagnose()
            error = check_worker_error("仪器诊断失败")
            return error or {"success": True, "message": "诊断查询已完成"}

        raise ValueError("未知的仪器操作")

    def _preflight(self, payload: dict, worker: MeasurementWorker) -> tuple[list[str], bool]:
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
        self._interval(payload.get("interval_s", 1.0))
        root = prepare_experiment_storage(Path(root_text).expanduser())
        available_space = format_available_space(
            require_minimum_free_space(get_available_space_bytes(root))
        )
        duplicates = find_existing_experiment_directories(root, name)

        device = self.store.snapshot().get("device") or {}
        identity = str(device.get("model") or ("MOCK模拟器" if worker.simulation else "Fluke 8845A"))
        wiring = "MOCK无需实体接线确认" if worker.simulation else "接线已确认；SCPI无法检查端子、保险丝和外部回路"
        checks = [
            f"仪器已连接：{identity}",
            f"当前模式已回读：{descriptor(settings.mode).label}；启动时将再次写入并回读所选参数",
            f"保存目录可写：{root}；当前可用 {available_space}，启动预留 10 MiB，"
            "采集中每 5 秒复查；低于预留值时停止采集并尝试收尾文件，"
            "若收尾失败会提示记录可能不完整；不预测长期实验总空间",
            wiring,
            f"实验名称有效：{name}；已有数据不会被覆盖",
        ]
        if duplicates:
            checks.append(f"发现 {len(duplicates)} 个同名实验目录；继续将创建新的时间戳目录")
        return checks, bool(duplicates)

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
        if self.thread.isRunning():
            if not self._stop_requested:
                self._stop_requested = True
                self.broker.dispatch.emit(uuid.uuid4().hex, "shutdown", {})
            deadline = time.monotonic() + timeout_ms / 1000
            while self.thread.isRunning() and time.monotonic() < deadline:
                QCoreApplication.processEvents()
                self.thread.wait(20)
            return not self.thread.isRunning()
        return True
