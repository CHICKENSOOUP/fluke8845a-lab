"""Serial transport and conservative SCPI handling for the Fluke 8845A."""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Protocol

from .filter_policy import (
    digital_filter_firmware_support,
    digital_filter_setting_error,
)
from .modes import Mode, ModeDescriptor, descriptor, mode_from_scpi_function
from .versioning import compare_firmware

log = logging.getLogger(__name__)


class InstrumentError(RuntimeError):
    """Base instrument/SCPI failure."""


class ScpiTimeout(InstrumentError):
    def __init__(
        self,
        message: str,
        *,
        command: str = "",
        partial_response: str = "",
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.partial_response = partial_response
        self.timeout_s = timeout_s


class ScpiProtocolError(InstrumentError):
    pass


class Rs232TerminalModeError(InstrumentError):
    """The meter is in interactive terminal mode rather than computer mode."""


class InstrumentLocalModeError(InstrumentError):
    """A SCPI command was rejected because the front panel is in LOCAL control."""

    def __init__(self, response: str) -> None:
        super().__init__(
            "8845A已切回LOCAL本地控制，远程命令被拒绝；采集应停止，重新连接以恢复远程控制。"
        )
        self.raw = response


class InstrumentIdentityError(InstrumentError):
    pass


class UnsupportedRange(InstrumentError):
    pass


class ReadingError(InstrumentError):
    """Invalid/overflow reading; never convert such a value to zero."""

    def __init__(self, message: str, raw: str = "", status: str = "invalid") -> None:
        super().__init__(message)
        self.raw = raw
        self.status = status


class FlowControl(StrEnum):
    NONE = "none"
    XONXOFF = "xonxoff"
    RTSCTS = "rtscts"


@dataclass(frozen=True, slots=True)
class SerialSettings:
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: float = 1
    flow_control: FlowControl = FlowControl.NONE
    eol: str = "CRLF"
    timeout_s: float = 2.0

    def __post_init__(self) -> None:
        try:
            normalized_flow = FlowControl(self.flow_control)
        except (TypeError, ValueError) as exc:
            raise ValueError("流控制必须为none、xonxoff或rtscts") from exc
        object.__setattr__(self, "flow_control", normalized_flow)
        if self.baudrate not in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400):
            raise ValueError("不支持的8845A波特率")
        if self.bytesize not in (7, 8) or self.parity not in ("N", "E", "O"):
            raise ValueError("数据位或校验位设置无效")
        if (self.bytesize, self.parity) not in {(8, "N"), (7, "E"), (7, "O")}:
            raise ValueError("8845A 串口格式仅支持 8N、7E 或 7O")
        if self.stopbits not in (1, 2):
            raise ValueError("停止位必须为1或2")
        if self.eol not in ("CR", "LF", "CRLF"):
            raise ValueError("EOL必须为CR、LF或CRLF")
        try:
            valid_timeout = math.isfinite(self.timeout_s) and self.timeout_s > 0
        except TypeError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError("通信超时必须为正的有限数")

    @property
    def terminator(self) -> bytes:
        return {"CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}[self.eol]


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    manufacturer: str
    model: str
    serial_number: str
    # A parseable number found in *IDN?'s revision field. It is only a
    # diagnostic candidate: the field may be user-defined and is not verified
    # as OutG SW or suitable for firmware-gated range decisions.
    idn_version_candidate: str
    idn_raw: str
    idn_revision: str = ""

    @property
    def firmware(self) -> str:
        """Deprecated compatibility alias; IDN revision is not verified firmware."""
        return ""


@dataclass(frozen=True, slots=True)
class ModeSettings:
    mode: Mode
    autorange: bool = True
    range_si: float | None = None
    nplc: float | None = None
    ac_bandwidth_hz: int | None = None
    analog_filter: bool = False
    autozero: bool = True
    digital_filter: bool = False
    input_impedance_auto: bool = False
    # Last instrument range readback. For autorange this is only a snapshot;
    # range_si remains None so the requested autorange setting stays explicit.
    range_readback_si: float | None = None

    def __post_init__(self) -> None:
        spec = descriptor(self.mode)
        if self.nplc is None and spec.nplc_values:
            object.__setattr__(self, "nplc", 1)
        if self.ac_bandwidth_hz is None and spec.ac_bandwidths_hz:
            object.__setattr__(self, "ac_bandwidth_hz", 20)

    def validate(self, *, allow_inactive_digital_filter: bool = False) -> ModeDescriptor:
        spec = descriptor(self.mode)
        if not isinstance(self.input_impedance_auto, bool):
            raise ValueError("自动输入阻抗必须为布尔值")
        if not isinstance(self.digital_filter, bool):
            raise ValueError("数字滤波设置必须是布尔值")
        if not self.autorange and self.range_si not in spec.ranges_si:
            raise ValueError(f"{spec.label}的固定量程不在8845A支持列表中")
        if self.nplc is not None and (not spec.nplc_values or self.nplc not in spec.nplc_values):
            raise ValueError(f"{spec.label}不支持此NPLC设置")
        if self.ac_bandwidth_hz is not None and self.ac_bandwidth_hz not in spec.ac_bandwidths_hz:
            raise ValueError(f"{spec.label}不支持此交流滤波档")
        if self.analog_filter and not spec.analog_filter:
            raise ValueError(f"{spec.label}不支持直流模拟滤波器")
        filter_error = digital_filter_setting_error(
            self.mode,
            self.digital_filter,
            nplc=self.nplc,
            allow_device_readback=allow_inactive_digital_filter,
        )
        if filter_error:
            raise ValueError(filter_error)
        if self.input_impedance_auto and not spec.input_impedance_auto:
            raise ValueError(f"{spec.label}不支持DCV自动输入阻抗设置")
        return spec


READ_RESPONSE_TIMEOUT_MARGIN_S = 3.0
AC_READ_RESPONSE_TIMEOUT_FLOOR_S = 8.0
CONSERVATIVE_LINE_FREQUENCY_HZ = 50.0
def measurement_read_timeout_s(settings: ModeSettings, base_timeout_s: float) -> float:
    """Add a conservative response window for slow 8845A conversions.

    The user-selected serial timeout remains the floor for ordinary commands.
    AC functions get a conservative fixed floor; NPLC functions scale with the
    longest common mains period (50 Hz), counting an additional conversion for
    autozero. Digital-filter averaging is not included because the supplied
    manual gives no maximum response time. This remains a finite host-side wait
    guard, not a meter timing guarantee; a timeout is a failed read.
    """
    spec = settings.validate()
    required = float(base_timeout_s)
    if spec.ac_bandwidths_hz:
        required = max(required, AC_READ_RESPONSE_TIMEOUT_FLOOR_S)
    elif settings.nplc is not None:
        conversions = 2 if settings.autozero else 1
        conversion_window_s = conversions * settings.nplc / CONSERVATIVE_LINE_FREQUENCY_HZ
        filter_margin_s = 1.0 if settings.analog_filter else 0.0
        required = max(
            required,
            READ_RESPONSE_TIMEOUT_MARGIN_S + conversion_window_s + filter_margin_s,
        )
    return required


@dataclass(frozen=True, slots=True)
class ParsedReading:
    raw: str
    value_si: float | None
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReadTiming:
    """Host clock boundaries around one SCPI request and its response."""

    read_request_start_monotonic_ns: int
    read_response_end_monotonic_ns: int
    read_request_start_utc: str
    read_request_start_local: str
    read_response_end_utc: str
    read_response_end_local: str

    @property
    def read_duration_s(self) -> float:
        return (self.read_response_end_monotonic_ns - self.read_request_start_monotonic_ns) / 1e9


def _host_clock_boundary() -> tuple[int, str, str]:
    monotonic_ns = time.perf_counter_ns()
    now = datetime.now(timezone.utc)
    return (
        monotonic_ns,
        now.isoformat(timespec="microseconds"),
        now.astimezone().isoformat(timespec="microseconds"),
    )


def _attach_query_timing(error: Exception, timing: ReadTiming, attribute: str) -> None:
    try:
        setattr(error, attribute, timing)
    except (AttributeError, TypeError):
        # Preserve the original exception even for unusual slot-only exception types.
        pass


@dataclass(frozen=True, slots=True)
class Reading:
    raw: str
    value_si: float
    range_si: float | None
    started_monotonic_ns: int
    received_monotonic_ns: int
    timestamp_utc: str
    timestamp_local: str
    status: str = "ok"
    read_response_end_utc: str = ""
    read_response_end_local: str = ""

    @property
    def read_request_start_monotonic_ns(self) -> int:
        return self.started_monotonic_ns

    @property
    def read_response_end_monotonic_ns(self) -> int:
        return self.received_monotonic_ns

    @property
    def read_request_start_utc(self) -> str:
        return self.timestamp_utc

    @property
    def read_request_start_local(self) -> str:
        return self.timestamp_local

    @property
    def read_duration_s(self) -> float:
        return (self.received_monotonic_ns - self.started_monotonic_ns) / 1e9


class ByteTransport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, payload: bytes) -> None: ...
    def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes: ...
    def discard_input(self) -> None: ...


class PySerialTransport:
    """Lazy pyserial wrapper so protocol tests do not need a COM port."""

    def __init__(self, port: str, settings: SerialSettings) -> None:
        self.port_name = port
        self.settings = settings
        self._port = None

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on installation
            raise InstrumentError("缺少pyserial，请运行 pip install -r requirements.txt") from exc
        parity = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN, "O": serial.PARITY_ODD}[self.settings.parity]
        try:
            self._port = serial.Serial(
                port=self.port_name,
                baudrate=self.settings.baudrate,
                bytesize=self.settings.bytesize,
                parity=parity,
                stopbits=serial.STOPBITS_ONE if self.settings.stopbits == 1 else serial.STOPBITS_TWO,
                timeout=self.settings.timeout_s,
                write_timeout=self.settings.timeout_s,
                xonxoff=self.settings.flow_control == FlowControl.XONXOFF,
                rtscts=self.settings.flow_control == FlowControl.RTSCTS,
                dsrdtr=False,
            )
        except Exception as exc:
            raise InstrumentError(f"打开串口{self.port_name}失败：{exc}") from exc

    def close(self) -> None:
        if self._port is not None:
            port = self._port
            if port.is_open:
                port.close()
            self._port = None

    def write(self, payload: bytes) -> None:
        if self._port is None or not self._port.is_open:
            raise InstrumentError("串口未打开")
        try:
            count = self._port.write(payload)
            self._port.flush()
            if count != len(payload):
                raise InstrumentError(f"串口短写：{count}/{len(payload)}字节")
        except InstrumentError:
            raise
        except Exception as exc:
            raise InstrumentError(f"串口写入失败：{exc}") from exc

    def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
        if self._port is None or not self._port.is_open:
            raise InstrumentError("串口未打开")
        previous_timeout = self._port.timeout
        try:
            if timeout_s is not None:
                self._port.timeout = timeout_s
            return bytes(self._port.read_until(terminator))
        except Exception as exc:
            raise InstrumentError(f"串口读取失败：{exc}") from exc
        finally:
            if timeout_s is not None and self._port is not None and self._port.is_open:
                self._port.timeout = previous_timeout

    def discard_input(self) -> None:
        if self._port is not None and self._port.is_open:
            try:
                self._port.reset_input_buffer()
            except Exception as exc:
                raise InstrumentError(f"清空串口输入缓冲失败：{exc}") from exc


def parse_reading(raw: str) -> ParsedReading:
    """Parse one SCPI numeric scalar, explicitly rejecting empty/overflow values."""
    text = raw.strip()
    if not text:
        return ParsedReading(raw, None, "empty", "仪器返回空响应")
    if text.upper() in {"OL", "OVER", "OVERLOAD"}:
        return ParsedReading(raw, None, "overflow", f"仪器返回无效读数：{text}")
    try:
        value = float(text)
    except ValueError:
        return ParsedReading(raw, None, "invalid", f"无法解析SCPI数值：{text!r}")
    if not math.isfinite(value):
        return ParsedReading(raw, None, "invalid", f"非有限读数：{text}")
    # IEEE 884x uses approximately 9.9E37 for an out-of-range result.
    if abs(value) >= 9.0e37:
        return ParsedReading(raw, None, "overflow", f"8845A溢出读数：{text}")
    return ParsedReading(raw, value, "ok")


def parse_idn(response: str) -> InstrumentInfo:
    fields = next(csv.reader(io.StringIO(response.strip())))
    if len(fields) < 2:
        raise InstrumentIdentityError(f"*IDN?响应格式无效：{response!r}")
    fields = [field.strip() for field in fields]
    revision = fields[3] if len(fields) > 3 else ""
    version_match = re.search(r"(?<![\d.])(?:v\s*)?(\d+(?:\.\d+){1,3})(?![\d.])", revision, re.I)
    version_candidate = ""
    if version_match:
        candidate = version_match.group(1)
        if len(candidate.split(".")) in (2, 4):
            version_candidate = candidate
    return InstrumentInfo(
        manufacturer=fields[0], model=fields[1].upper(),
        serial_number=fields[2] if len(fields) > 2 else "",
        idn_version_candidate=version_candidate,
        idn_raw=response.strip(),
        idn_revision=revision,
    )


def select_range_firmware(outguard_version: str) -> tuple[str, str]:
    """Select only manually panel-verified OutG SW for firmware range gates."""
    if compare_firmware(outguard_version, (0, 0)) is not None:
        return str(outguard_version).strip(), "面板核对的 OutG SW"
    return "", "未知"


CURRENT_RANGE_FIRMWARE_MINIMUM = {
    Mode.DCI: (2, 0),
}
# For the 8845A, DCI marks 1 A front-only; 3 A is part of the 10 A range and
# the user manual says 10 A current cannot use the rear terminals. ACI marks
# 1/3/10 A front-only. Keep this terminal gate separate from the 400 mA
# firmware gate above.
FRONT_ONLY_CURRENT_RANGES = (1.0, 3.0, 10.0)


def current_firmware_gate_evidence_note(mode: Mode) -> str:
    """Describe the documented firmware-field mapping for the current range gate."""
    if mode == Mode.ACI:
        return (
            "ACI 官方资料要求软件版本至少为 1.0.700.18，但未说明对应 OutG SW 还是 InG SW；"
            "本软件不根据 OutG SW 或 InG SW 自动拒绝 400 mA 档，使用前请在仪表面板核对软件版本"
        )
    if mode == Mode.DCI:
        return "DCI 的 400 mA 门槛由官方用户手册明确对应 OutG SW"
    return ""


def current_autorange_block_reason(mode: Mode, firmware: str, terminals: str | None) -> str | None:
    """Explain why current autorange must be blocked for known unsupported capabilities."""
    if mode not in {Mode.DCI, Mode.ACI}:
        return None
    minimum = CURRENT_RANGE_FIRMWARE_MINIMUM.get(mode)
    # The only version passed here is manually verified OutG SW. An unknown or
    # unparseable value is not evidence that 400 mA is unsupported; only an
    # explicitly older OutG SW blocks DCI. ACI's software threshold is not
    # mapped to OutG or InG in the supplied official specifications.
    if minimum is not None and compare_firmware(firmware, minimum) is False:
        reason = "固件版本低于400 mA档要求"
        evidence_note = current_firmware_gate_evidence_note(mode)
        return f"{reason}；{evidence_note}" if evidence_note else reason
    if not (terminals or "").strip().upper().startswith("FRON"):
        terminal_name = (terminals or "").strip() or "未确认"
        return (
            f"当前输入端子为 {terminal_name}，未确认是前面板；"
            "自动量程可能进入仅前面板支持的1/3/10 A档"
        )
    return None


def unsupported_current_ranges(mode: Mode, firmware: str, terminals: str | None) -> tuple[float, ...]:
    """Return current ranges explicitly unsupported by known 8845A state."""
    if mode not in {Mode.DCI, Mode.ACI}:
        return ()
    minimum = CURRENT_RANGE_FIRMWARE_MINIMUM.get(mode)
    unsupported: set[float] = set()
    if minimum is not None and compare_firmware(firmware, minimum) is False:
        unsupported.add(0.4)
    if not (terminals or "").strip().upper().startswith("FRON"):
        unsupported.update(FRONT_ONLY_CURRENT_RANGES)
    return tuple(sorted(unsupported))


class ScpiClient:
    """One-thread-at-a-time SCPI session with explicit CR/LF and timeout handling."""

    def __init__(
        self,
        transport: ByteTransport,
        settings: SerialSettings,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.settings = settings
        self._log_callback = log_callback or (lambda _line: None)
        self.is_open = False
        self.last_query_timing: ReadTiming | None = None

    def _log(self, direction: str, text: str) -> None:
        line = f"{direction} {text}"
        log.info("%s", line)
        self._log_callback(line)

    def open(self) -> None:
        try:
            self.transport.open()
            self.is_open = True
            self._discard_input()
        except Exception:
            self._close_failed_transport()
            raise

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self.is_open = False

    def _close_failed_transport(self) -> None:
        """Release the underlying port after an I/O failure, even if close itself fails."""
        try:
            self.transport.close()
        except Exception:
            log.exception("关闭故障的 SCPI 串口失败")
        finally:
            self.is_open = False

    def _discard_input(self) -> None:
        try:
            self.transport.discard_input()
        except Exception as exc:
            self._close_failed_transport()
            if isinstance(exc, InstrumentError):
                raise
            raise InstrumentError(f"SCPI输入缓冲操作失败：{exc}") from exc

    def write(self, command: str) -> None:
        if not self.is_open:
            raise InstrumentError("SCPI连接未打开")
        if "\r" in command or "\n" in command:
            raise ValueError("单条SCPI命令不能包含结束符")
        self._log(">", command)
        payload = command.encode("ascii") + self.settings.terminator
        try:
            self.transport.write(payload)
        except Exception as exc:
            self._close_failed_transport()
            if isinstance(exc, InstrumentError):
                raise
            raise InstrumentError(f"SCPI命令写入失败：{exc}") from exc

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        if not self.is_open:
            raise InstrumentError("SCPI连接未打开")
        if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("SCPI响应超时必须为正的有限数")
        self.last_query_timing = None
        self._discard_input()
        request_start_ns, request_start_utc, request_start_local = _host_clock_boundary()
        try:
            self.write(command)
        except Exception as exc:
            response_end_ns, response_end_utc, response_end_local = _host_clock_boundary()
            timing = ReadTiming(
                request_start_ns, response_end_ns,
                request_start_utc, request_start_local,
                response_end_utc, response_end_local,
            )
            self.last_query_timing = timing
            _attach_query_timing(exc, timing, "query_timing")
            raise
        try:
            if timeout_s is None:
                payload = self.transport.read_until(self.settings.terminator)
            else:
                payload = self.transport.read_until(self.settings.terminator, timeout_s)
        except Exception as exc:
            response_end_ns, response_end_utc, response_end_local = _host_clock_boundary()
            timing = ReadTiming(
                request_start_ns, response_end_ns,
                request_start_utc, request_start_local,
                response_end_utc, response_end_local,
            )
            self.last_query_timing = timing
            self._close_failed_transport()
            read_error = (
                exc if isinstance(exc, InstrumentError)
                else InstrumentError(f"SCPI响应读取失败：{exc}")
            )
            _attach_query_timing(read_error, timing, "query_timing")
            if read_error is exc:
                raise
            raise read_error from exc
        response_end_ns, response_end_utc, response_end_local = _host_clock_boundary()
        timing = ReadTiming(
            request_start_ns, response_end_ns,
            request_start_utc, request_start_local,
            response_end_utc, response_end_local,
        )
        self.last_query_timing = timing
        if not payload.endswith(self.settings.terminator):
            partial = payload.decode("ascii", errors="replace").strip()
            effective_timeout = self.settings.timeout_s if timeout_s is None else timeout_s
            self._log("!", f"{command}超时（{effective_timeout:g}s）；部分响应={partial!r}")
            error = ScpiTimeout(
                f"等待{command}响应超时（{effective_timeout:g}s）；部分数据：{partial!r}",
                command=command,
                partial_response=partial,
                timeout_s=effective_timeout,
            )
            _attach_query_timing(error, timing, "query_timing")
            # A response may arrive after the timeout. Clearing the current input
            # buffer cannot prove that a late response will not contaminate the
            # next query, so invalidate the session before another command can run.
            self._close_failed_transport()
            raise error
        response = payload[: -len(self.settings.terminator)].decode("ascii", errors="replace").strip()
        self._log("<", response)
        if response.casefold() == command.strip().casefold():
            error = Rs232TerminalModeError(
                "检测到8845A回显SCPI命令，RS-232当前处于终端模式；"
                "请在仪表前面板切换至Computer模式后重新连接。"
            )
            _attach_query_timing(error, timing, "query_timing")
            raise error
        if re.match(r"\+?550\s*,", response) and "command not allowed in local" in response.casefold():
            self._log("!", "8845A拒绝远程命令：仪器已处于LOCAL本地控制")
            error = InstrumentLocalModeError(response)
            _attach_query_timing(error, timing, "query_timing")
            raise error
        return response


class Serial8845A:
    """Public instrument facade; accepts an 8845A and rejects other models."""

    def __init__(
        self,
        port: str,
        settings: SerialSettings,
        log_callback: Callable[[str], None] | None = None,
        transport_factory: Callable[[str, SerialSettings], ByteTransport] = PySerialTransport,
    ) -> None:
        self.port = port
        self.settings = settings
        self._logger = log_callback or (lambda _line: None)
        self._client = ScpiClient(transport_factory(port, settings), settings, self._logger)
        self.info: InstrumentInfo | None = None
        self._mode: Mode | None = None
        self._active_settings: ModeSettings | None = None
        self.outguard_version = ""
        self.inguard_version = ""
        self.firmware_source = "panel"

    def set_firmware_versions(self, outguard_version: str = "", inguard_version: str = "") -> None:
        """Store software revisions manually read from this meter's front panel."""
        self.outguard_version = str(outguard_version or "").strip()
        self.inguard_version = str(inguard_version or "").strip()
        self.firmware_source = "panel"

    @property
    def range_firmware(self) -> tuple[str, str]:
        version, source = select_range_firmware(self.outguard_version)
        if version and self.firmware_source == "user_default":
            source = "用户指定默认值，未经仪器核验"
        return version, source

    @property
    def connected(self) -> bool:
        return self._client.is_open

    @property
    def measurement_read_timeout_s(self) -> float:
        if self._active_settings is None:
            return self.settings.timeout_s
        return measurement_read_timeout_s(self._active_settings, self.settings.timeout_s)

    def connect(
        self,
        *,
        progress_callback: Callable[[str, str, str], None] | None = None,
    ) -> InstrumentInfo:
        """Open the serial port and identify the meter, optionally reporting progress.

        The callback is observational only: it does not change the SCPI command
        sequence or instrument configuration.
        """
        report = progress_callback or (lambda _stage, _status, _message: None)
        report("port", "running", f"正在打开串口 {self.port}…")
        try:
            self._client.open()
        except Exception as exc:
            report("port", "failure", f"串口打开失败：{exc}")
            raise
        report("port", "success", f"串口 {self.port} 已打开")
        remote_command_attempted = False
        try:
            report("identify", "running", "正在查询并核验 Fluke 8845A 身份…")
            # The programming manual requires REMOTE before any RS-232 exchange.
            remote_command_attempted = True
            self._client.write("SYST:REM")
            idn = self._client.query("*IDN?")
            info = parse_idn(idn)
            if "FLUKE" not in info.manufacturer.upper() or info.model != "8845A":
                raise InstrumentIdentityError(f"需要Fluke 8845A，当前识别为：{idn}")
            self.info = info
            self._drain_errors("连接时仪器已有错误", fail=False)
            report("identify", "success", f"已识别 Fluke 8845A，序列号 {info.serial_number or '未提供'}")
            return info
        except Exception as exc:
            report("identify", "failure", f"8845A 身份识别失败：{exc}")
            if remote_command_attempted and self._client.is_open:
                try:
                    self._client.write("SYST:LOC")
                except Exception as recovery_exc:
                    self._logger(f"! 身份识别失败后恢复本地控制失败：{recovery_exc}")
            self._client.close()
            raise

    def set_control_mode(self, mode: str) -> InstrumentInfo | None:
        """Switch between front-panel LOCAL and SCPI REMOTE control.

        Returning to REMOTE verifies the command channel again and refreshes
        the identity; LOCAL is write-only because SCPI queries are unavailable
        while the front panel owns control.
        """
        if not self.connected:
            raise InstrumentError("仪器未连接")
        normalized = str(mode).strip().casefold()
        if normalized == "local":
            self._client.write("SYST:LOC")
            self._mode = None
            self._active_settings = None
            return None
        if normalized != "computer":
            raise ValueError("控制方式必须为 Local 或 Computer")
        self._client.write("SYST:REM")
        info = parse_idn(self._client.query("*IDN?"))
        if "FLUKE" not in info.manufacturer.upper() or info.model != "8845A":
            raise InstrumentIdentityError(f"需要Fluke 8845A，当前识别为：{info.idn_raw}")
        self.info = info
        return info

    def check_connection(self) -> InstrumentInfo:
        """Verify that the open Computer-mode session still identifies this 8845A."""
        if not self.connected:
            raise InstrumentError("仪器未连接")
        info = parse_idn(self._client.query("*IDN?"))
        if "FLUKE" not in info.manufacturer.upper() or info.model != "8845A":
            raise InstrumentIdentityError(f"连接检查未识别到 Fluke 8845A：{info.idn_raw}")
        if (
            self.info is not None and self.info.serial_number
            and info.serial_number != self.info.serial_number
        ):
            raise InstrumentIdentityError(
                "连接检查发现仪器序列号与初始识别结果不一致："
                f"{self.info.serial_number!r} -> {info.serial_number!r}"
            )
        self.info = info
        return info

    def close(self, *, restore_local: bool = True) -> None:
        try:
            if self._client.is_open and restore_local:
                try:
                    self._client.write("SYST:LOC")
                except Exception as exc:
                    self._logger(f"! 本地控制恢复失败：{exc}")
        finally:
            self._client.close()
            self._mode = None
            self._active_settings = None

    def read_mode_state(self) -> dict[str, str]:
        """Read the selected function and input terminals without reconfiguring."""
        if not self.connected:
            raise InstrumentError("仪器未连接")
        return {
            "function": self._client.query("SENS:FUNC?"),
            "input_terminals": self._client.query("ROUT:TERM?"),
        }

    def read_settings(self, mode: Mode) -> ModeSettings:
        """Read the selected mode's current settings without changing the instrument."""
        if not self.connected:
            raise InstrumentError("仪器未连接")
        spec = descriptor(mode)
        actual_function = self._client.query("SENS:FUNC?")
        actual_mode = mode_from_scpi_function(actual_function)
        if actual_mode != mode:
            raise InstrumentError(
                f"拒绝读取其他模式的配置：期望{spec.function}，实际{actual_function}"
            )

        prefix = spec.sense_prefix
        autorange = self._readback_boolean(
            "自动量程", self._client.query(f"{prefix}:RANG:AUTO?")
        )
        range_si = self._readback_number("量程", self._client.query(f"{prefix}:RANG?"))
        supported_range = next(
            (value for value in spec.ranges_si if self._close_numeric(range_si, value)),
            None,
        )
        if supported_range is None:
            raise ScpiProtocolError(
                f"量程回读不在8845A {spec.label}支持列表中：{range_si:g}"
            )
        nplc = (
            self._readback_number("NPLC", self._client.query(f"{prefix}:NPLC?"))
            if spec.nplc_values else None
        )
        ac_bandwidth_hz = None
        if spec.ac_bandwidths_hz:
            bandwidth_response = self._client.query("SENS:DET:BAND?")
            ac_bandwidth_hz = self._readback_ac_bandwidth(
                bandwidth_response, spec.ac_bandwidths_hz
            )
        analog_filter = (
            self._readback_boolean("模拟滤波", self._client.query(f"{prefix}:FILT:STAT?"))
            if spec.analog_filter else False
        )
        filter_support = digital_filter_firmware_support(mode, self.range_firmware[0])
        digital_filter = (
            self._readback_boolean("数字滤波", self._client.query(f"{prefix}:FILT:DIG?"))
            if spec.digital_filter and filter_support is not False else False
        )
        input_impedance_auto = (
            self._readback_boolean("自动输入阻抗", self._client.query("INP:IMP:AUTO?"))
            if spec.input_impedance_auto else False
        )
        autozero = self._readback_boolean("自动零点", self._client.query("ZERO:AUTO?"))
        settings = ModeSettings(
            mode=mode,
            autorange=autorange,
            range_si=None if autorange else range_si,
            range_readback_si=supported_range,
            nplc=nplc,
            ac_bandwidth_hz=ac_bandwidth_hz,
            analog_filter=analog_filter,
            autozero=autozero,
            digital_filter=digital_filter,
            input_impedance_auto=input_impedance_auto,
        )
        # Preserve the read-back switch state even when NPLC makes the filter
        # inactive; user-supplied configuration still rejects that combination.
        settings.validate(allow_inactive_digital_filter=True)
        return settings

    def _drain_errors(self, heading: str, *, fail: bool) -> list[str]:
        errors: list[str] = []
        # The meter can hold 16 errors; allow one extra query for the empty-queue sentinel.
        for _ in range(17):
            response = self._client.query("SYST:ERR?")
            code, _message = self._parse_error(response)
            if code == 0:
                if fail and errors:
                    raise ScpiProtocolError(f"{heading}：" + "；".join(errors))
                return errors
            if len(errors) == 16:
                # A nonempty 17th response exceeds the 8845A's documented
                # queue capacity; do not record an entry beyond that bound.
                raise ScpiProtocolError("8845A错误队列超过16条，已停止读取以防死循环")
            errors.append(response)
            self._logger(f"! {heading}：{response}")
        raise ScpiProtocolError("8845A错误队列超过16条，已停止读取以防死循环")

    @staticmethod
    def _parse_error(response: str) -> tuple[int, str]:
        match = re.match(r"\s*([+-]?\d+)\s*,?\s*(.*)$", response)
        if not match:
            raise ScpiProtocolError(f"SYST:ERR?响应格式错误：{response!r}")
        return int(match.group(1)), match.group(2).strip()

    @staticmethod
    def _readback_number(name: str, response: str) -> float:
        try:
            value = float(response.strip())
        except (AttributeError, ValueError) as exc:
            raise ScpiProtocolError(f"{name}回读不是有效数值：{response!r}") from exc
        if not math.isfinite(value):
            raise ScpiProtocolError(f"{name}回读不是有限数值：{response!r}")
        return value

    def _verify_single_read_trigger(self) -> None:
        """Refuse trigger states that make one-point READ? block or return many points."""
        source_response = self._client.query("TRIG:SOUR?")
        source = source_response.strip().strip("\"'").upper()
        if source != "IMM":
            raise InstrumentError(
                "单点 READ? 仅支持立即触发；TRIG:SOUR? 必须回读 IMM，"
                f"当前为 {source_response!r}。请在仪表上检查触发设置后重试。"
            )

        trigger_count = self._readback_number(
            "TRIG:COUN?", self._client.query("TRIG:COUN?")
        )
        if trigger_count != 1:
            raise InstrumentError(
                "单点 READ? 要求 TRIG:COUN? 回读为1，"
                f"当前为 {trigger_count:g}。请在仪表上检查触发设置后重试。"
            )

        sample_count = self._readback_number(
            "SAMP:COUN?", self._client.query("SAMP:COUN?")
        )
        if sample_count != 1:
            raise InstrumentError(
                "单点 READ? 要求 SAMP:COUN? 回读为1，"
                f"当前为 {sample_count:g}。请在仪表上检查触发设置后重试。"
            )

    @classmethod
    def _readback_ac_bandwidth(
        cls, response: str, supported_values: tuple[int, ...]
    ) -> int:
        """Parse the exact integer bandwidth options documented for AC functions."""
        cls._readback_number("交流带宽", response)
        token = response.strip()
        if re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?",
            token,
        ) is None:
            raise ScpiProtocolError(f"交流带宽回读不是有效SCPI数值：{response!r}")
        try:
            exact_bandwidth = Decimal(token)
        except (AttributeError, InvalidOperation, TypeError) as exc:
            raise ScpiProtocolError(
                f"交流带宽回读不是有效数值：{response!r}"
            ) from exc
        if not exact_bandwidth.is_finite():
            raise ScpiProtocolError(
                f"交流带宽回读不是有限数值：{response!r}"
            )
        if exact_bandwidth != exact_bandwidth.to_integral_value():
            raise ScpiProtocolError(
                f"交流带宽回读不是整数：{token!r}"
            )
        bandwidth_hz = int(exact_bandwidth)
        if bandwidth_hz not in supported_values:
            raise ScpiProtocolError(
                "交流带宽回读值不在8845A支持列表中："
                f"{bandwidth_hz} Hz"
            )
        return bandwidth_hz

    @classmethod
    def _readback_boolean(cls, name: str, response: str) -> bool:
        token = response.strip().strip("\"'").upper()
        if token in {"ON", "TRUE"}:
            return True
        if token in {"OFF", "FALSE"}:
            return False
        value = cls._readback_number(name, token)
        if value == 1:
            return True
        if value == 0:
            return False
        raise ScpiProtocolError(f"{name}回读不是ON/OFF或0/1：{response!r}")

    def _disable_digital_filter(
        self, prefix: str, *, error_heading: str
    ) -> str:
        """Disable and verify the function's digital filter before NPLC changes."""
        return self._set_digital_filter(prefix, False, error_heading=error_heading)

    def _enable_digital_filter(
        self, prefix: str, *, error_heading: str
    ) -> str:
        """Enable and verify the function's digital filter after NPLC changes."""
        return self._set_digital_filter(prefix, True, error_heading=error_heading)

    def _set_digital_filter(
        self, prefix: str, enabled: bool, *, error_heading: str
    ) -> str:
        """Write, drain SCPI errors, and verify a function's digital-filter state."""
        expected = "ON" if enabled else "OFF"
        self._client.write(f"{prefix}:FILT:DIG {expected}")
        self._drain_errors(error_heading, fail=True)
        actual = self._client.query(f"{prefix}:FILT:DIG?")
        if self._readback_boolean("数字滤波", actual) != enabled:
            raise ScpiProtocolError(
                f"数字滤波回读不符：期望{expected}，实际{actual}"
            )
        return actual

    @staticmethod
    def _close_numeric(actual: float, expected: float) -> bool:
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)

    def configure(self, settings: ModeSettings) -> dict[str, str | float | bool | None]:
        if not self.connected:
            raise InstrumentError("仪器未连接")
        spec = settings.validate()
        assert self.info is not None
        range_firmware, firmware_source = self.range_firmware
        filter_support = digital_filter_firmware_support(settings.mode, range_firmware)
        if settings.digital_filter and filter_support is False:
            raise UnsupportedRange(
                "面板核对的 OutG SW 早于 2.0，不支持直流数字滤波；"
                "请在仪表 SYSTEM→VERSION 页面核对软件版本"
            )
        self._drain_errors("配置前仪器错误", fail=False)
        terminals = self._client.query("ROUT:TERM?") if spec.quantity == "current" else None
        unsupported = unsupported_current_ranges(settings.mode, range_firmware, terminals)
        if spec.quantity == "current" and settings.autorange:
            reason = current_autorange_block_reason(settings.mode, range_firmware, terminals)
            if reason:
                raise UnsupportedRange(
                    f"8845A {spec.label}：{reason}。请关闭自动量程并选用已确认支持的固定量程。"
                )
        if not settings.autorange and settings.range_si in unsupported:
            if settings.range_si == 0.4:
                need = "2.0" if settings.mode == Mode.DCI else ""
                detail = f"{firmware_source}版本{range_firmware}低于400 mA档要求的{need}"
                evidence_note = current_firmware_gate_evidence_note(settings.mode)
                if evidence_note:
                    detail += f"；{evidence_note}"
            else:
                detail = "1 A、3 A和10 A量程仅在前面板电流端子支持"
            raise UnsupportedRange(f"8845A {spec.label}：{detail}，为避免无效/危险配置已拒绝。")
        # Invalidate cached acquisition state before changing the instrument. A
        # failed write or readback must not leave the previous mode marked as
        # safely configured.
        self._mode = None
        self._active_settings = None
        self._client.write(f"CONF:{spec.function}")
        self._drain_errors("CONF配置后仪器报告错误", fail=True)
        # CONF applies the documented measurement preset; verify its single-
        # point immediate-trigger state before writing further settings or
        # permitting a later READ?.
        self._verify_single_read_trigger()
        prefix = spec.sense_prefix
        actual_digital_filter = None
        if spec.digital_filter and filter_support is not False:
            # Programmers Manual Rev. 3, Table 13 (PDF pp. 42–43 / printed
            # pp. 32–33) and filter commands (PDF pp. 58–60 / printed pp.
            # 48–50) document this control for DCV/DCI/RES/FRES. CONF presets
            # the filter ON (Table 18, PDF p. 51 / printed p. 41), so turn it
            # OFF and verify that state before allowing any NPLC write.
            actual_digital_filter = self._disable_digital_filter(
                prefix, error_heading="关闭数字滤波时仪器报告错误"
            )
        elif spec.digital_filter:
            self._logger(
                "~ 面板核对的 OutG SW 低于2.0；该版本不支持数字滤波，"
                "跳过滤波命令，继续配置NPLC"
            )
        if settings.autorange:
            self._client.write(f"{prefix}:RANG:AUTO ON")
        else:
            self._client.write(f"{prefix}:RANG:AUTO OFF")
            assert settings.range_si is not None
            self._client.write(f"{prefix}:RANG {settings.range_si:.12g}")
        if settings.nplc is not None:
            self._client.write(f"{prefix}:NPLC {settings.nplc:g}")
        if settings.digital_filter:
            # OutG SW may be unknown because *IDN? does not report it. In that
            # case probe the documented command, inspect the error queue, and
            # require an ON readback before reporting a successful configure.
            if filter_support is None:
                self._logger("~ OutG SW未核对；尝试设置直流数字滤波并检查错误队列及状态回读")
            actual_digital_filter = self._enable_digital_filter(
                prefix, error_heading="开启数字滤波时仪器报告错误"
            )
        if settings.ac_bandwidth_hz is not None:
            # DETector:BANDwidth is the documented global AC filter selector.
            self._client.write(f"SENS:DET:BAND {settings.ac_bandwidth_hz}")
        if spec.analog_filter:
            self._client.write(f"{prefix}:FILT:STAT {'ON' if settings.analog_filter else 'OFF'}")
        if spec.input_impedance_auto:
            # Programmers Manual Rev. 3, PDF pp. 60–61 (printed pp. 50–51):
            # AUTO ON selects >10 GΩ only on 100 mV, 1 V, and 10 V DCV ranges;
            # OFF is 10 MΩ on every range. Table 18 presets AUTO OFF.
            self._client.write(
                f"INP:IMP:AUTO {'ON' if settings.input_impedance_auto else 'OFF'}"
            )
        self._client.write(f"ZERO:AUTO {'ON' if settings.autozero else 'OFF'}")
        self._drain_errors("配置后仪器报告错误", fail=True)
        actual_range = self._client.query(f"{prefix}:RANG?")
        actual_autorange = self._client.query(f"{prefix}:RANG:AUTO?")
        actual_nplc = self._client.query(f"{prefix}:NPLC?") if settings.nplc is not None else None
        actual_band = self._client.query("SENS:DET:BAND?") if settings.ac_bandwidth_hz is not None else None
        actual_filter = self._client.query(f"{prefix}:FILT:STAT?") if spec.analog_filter else None
        actual_input_impedance_auto = (
            self._client.query("INP:IMP:AUTO?") if spec.input_impedance_auto else None
        )
        actual_zero = self._client.query("ZERO:AUTO?")
        terminal = self._client.query("ROUT:TERM?")
        actual_function = self._client.query("SENS:FUNC?")
        actual_mode = mode_from_scpi_function(actual_function)
        if actual_mode != settings.mode:
            raise ScpiProtocolError(f"配置后功能回读不符：期望{spec.function}，实际{actual_function}")

        actual_autorange_value = self._readback_boolean("自动量程", actual_autorange)
        if actual_autorange_value != settings.autorange:
            raise ScpiProtocolError(
                f"自动量程回读不符：期望{'ON' if settings.autorange else 'OFF'}，"
                f"实际{actual_autorange}"
            )

        actual_range_value = self._readback_number("量程", actual_range)
        supported_range = next(
            (value for value in spec.ranges_si if self._close_numeric(actual_range_value, value)),
            None,
        )
        if supported_range is None:
            raise ScpiProtocolError(
                f"量程回读不在8845A {spec.label}支持列表中：{actual_range}"
            )
        if not settings.autorange and not self._close_numeric(actual_range_value, settings.range_si):
            raise ScpiProtocolError(
                f"固定量程回读不符：期望{settings.range_si:g}，实际{actual_range}"
            )

        if settings.nplc is not None:
            actual_nplc_value = self._readback_number("NPLC", actual_nplc)
            if not self._close_numeric(actual_nplc_value, settings.nplc):
                raise ScpiProtocolError(
                    f"NPLC回读不符：期望{settings.nplc:g}，实际{actual_nplc}"
                )
        if settings.ac_bandwidth_hz is not None:
            actual_band_value = self._readback_ac_bandwidth(
                actual_band, spec.ac_bandwidths_hz
            )
            if actual_band_value != settings.ac_bandwidth_hz:
                raise ScpiProtocolError(
                    f"交流带宽回读不符：期望{settings.ac_bandwidth_hz} Hz，实际{actual_band}"
                )
        if spec.analog_filter:
            actual_filter_value = self._readback_boolean("模拟滤波", actual_filter)
            if actual_filter_value != settings.analog_filter:
                raise ScpiProtocolError(
                    f"模拟滤波回读不符：期望{'ON' if settings.analog_filter else 'OFF'}，"
                    f"实际{actual_filter}"
                )
        if spec.input_impedance_auto:
            actual_input_impedance_value = self._readback_boolean(
                "自动输入阻抗", actual_input_impedance_auto
            )
            if actual_input_impedance_value != settings.input_impedance_auto:
                raise ScpiProtocolError(
                    "自动输入阻抗回读不符：期望"
                    f"{'ON' if settings.input_impedance_auto else 'OFF'}，"
                    f"实际{actual_input_impedance_auto}"
                )
        actual_zero_value = self._readback_boolean("自动零点", actual_zero)
        if actual_zero_value != settings.autozero:
            raise ScpiProtocolError(
                f"自动零点回读不符：期望{'ON' if settings.autozero else 'OFF'}，"
                f"实际{actual_zero}"
            )
        self._mode = settings.mode
        self._active_settings = replace(settings, range_readback_si=actual_range_value)
        read_timeout = self.measurement_read_timeout_s
        if read_timeout > self.settings.timeout_s:
            self._logger(
                f"~ 慢速{spec.label} READ?有限等待时间按积分设置调整为{read_timeout:g}s；"
                "数字滤波平均等待时长未由手册定量，超时会作为通信失败处理"
            )
        return {
            "function": actual_function,
            "range_response": actual_range,
            "range_readback_si": actual_range_value,
            "autorange_response": actual_autorange,
            "nplc_response": actual_nplc,
            "ac_bandwidth_response": actual_band,
            "analog_filter_response": actual_filter,
            "input_impedance_auto_response": actual_input_impedance_auto,
            "digital_filter_response": actual_digital_filter,
            "autozero_response": actual_zero,
            "input_terminals": terminal,
            "measurement_read_timeout_s": read_timeout,
        }

    def set_measurement_speed(self, settings: ModeSettings) -> dict[str, str | float | None]:
        """Change only the active function's integration/bandwidth while acquiring."""
        spec = settings.validate()
        if not self.connected:
            raise InstrumentError("仪器未连接")
        if self._mode is None or settings.mode != self._mode:
            raise InstrumentError("实时调速只能用于当前已配置的测量模式")

        prefix = spec.sense_prefix
        self._drain_errors("调速前仪器错误", fail=False)
        filter_support = digital_filter_firmware_support(
            settings.mode, self.range_firmware[0]
        )
        if settings.digital_filter and filter_support is False:
            raise UnsupportedRange(
                "面板核对的 OutG SW 早于 2.0，不支持直流数字滤波；"
                "请在仪表 SYSTEM→VERSION 页面核对软件版本"
            )
        actual_digital_filter = None
        if spec.digital_filter and settings.nplc is not None and filter_support is not False:
            actual_digital_filter = self._disable_digital_filter(
                prefix, error_heading="实时调速关闭数字滤波时仪器报告错误"
            )
        if settings.nplc is not None:
            self._client.write(f"{prefix}:NPLC {settings.nplc:g}")
        if settings.digital_filter:
            if filter_support is None:
                self._logger("~ OutG SW未核对；尝试实时开启数字滤波并检查错误队列及状态回读")
            actual_digital_filter = self._enable_digital_filter(
                prefix, error_heading="实时开启数字滤波时仪器报告错误"
            )
        if settings.ac_bandwidth_hz is not None:
            self._client.write(f"SENS:DET:BAND {settings.ac_bandwidth_hz}")
        self._drain_errors("调速后仪器报告错误", fail=True)

        actual_nplc = self._client.query(f"{prefix}:NPLC?") if settings.nplc is not None else None
        actual_band = self._client.query("SENS:DET:BAND?") if settings.ac_bandwidth_hz is not None else None
        nplc_matches = settings.nplc is None
        if settings.nplc is not None:
            actual_nplc_value = self._readback_number("NPLC", actual_nplc)
            nplc_matches = self._close_numeric(actual_nplc_value, settings.nplc)
        band_matches = settings.ac_bandwidth_hz is None
        if settings.ac_bandwidth_hz is not None:
            actual_band_value = self._readback_ac_bandwidth(
                actual_band, spec.ac_bandwidths_hz
            )
            band_matches = actual_band_value == settings.ac_bandwidth_hz
        if not nplc_matches or not band_matches:
            raise ScpiProtocolError(
                f"调速参数回读不符：期望NPLC={settings.nplc}、交流带宽={settings.ac_bandwidth_hz} Hz；"
                f"实际NPLC={actual_nplc}、交流带宽={actual_band} Hz"
            )
        if self._active_settings is None:
            raise InstrumentError("仪器测量设置尚未确认")
        self._active_settings = replace(
            self._active_settings,
            nplc=settings.nplc,
            ac_bandwidth_hz=settings.ac_bandwidth_hz,
            digital_filter=settings.digital_filter,
        )
        read_timeout = self.measurement_read_timeout_s
        if read_timeout > self.settings.timeout_s:
            self._logger(f"~ 慢速READ?响应上限已更新为{read_timeout:g}s")
        return {
            "nplc_response": actual_nplc,
            "ac_bandwidth_response": actual_band,
            "digital_filter_response": actual_digital_filter,
            "measurement_read_timeout_s": read_timeout,
        }

    def read(self) -> Reading:
        if not self.connected or self._mode is None:
            raise InstrumentError("请先连接并配置测量模式")
        try:
            raw = self._client.query("READ?", timeout_s=self.measurement_read_timeout_s)
        except Exception as exc:
            timing = getattr(exc, "query_timing", None) or self._client.last_query_timing
            if timing is not None:
                _attach_query_timing(exc, timing, "read_timing")
            raise
        timing = self._client.last_query_timing
        if timing is None:
            raise InstrumentError("READ?请求缺少主机时钟边界")
        parsed = parse_reading(raw)
        if parsed.value_si is None:
            error = ReadingError(parsed.detail, parsed.raw, parsed.status)
            _attach_query_timing(error, timing, "read_timing")
            raise error
        spec = descriptor(self._mode)
        try:
            range_response = self._client.query(f"{spec.sense_prefix}:RANG?")
        except Exception as exc:
            exc.measurement_raw = raw
            _attach_query_timing(exc, timing, "read_timing")
            raise
        range_value = parse_reading(range_response)
        if range_value.value_si is None:
            error = ScpiProtocolError(
                f"量程回读不是有效数值：{range_response!r}"
            )
            error.measurement_raw = raw
            _attach_query_timing(error, timing, "read_timing")
            raise error
        if not any(
            self._close_numeric(range_value.value_si, supported_range)
            for supported_range in spec.ranges_si
        ):
            error = ScpiProtocolError(
                f"量程回读不在8845A {spec.label}支持列表中：{range_response!r}"
            )
            error.measurement_raw = raw
            _attach_query_timing(error, timing, "read_timing")
            raise error
        return Reading(
            raw=raw, value_si=parsed.value_si,
            range_si=range_value.value_si,
            started_monotonic_ns=timing.read_request_start_monotonic_ns,
            received_monotonic_ns=timing.read_response_end_monotonic_ns,
            timestamp_utc=timing.read_request_start_utc,
            timestamp_local=timing.read_request_start_local,
            read_response_end_utc=timing.read_response_end_utc,
            read_response_end_local=timing.read_response_end_local,
        )

    def single(self) -> Reading:
        """Take one reading without replacing the already verified setup."""
        # Programmers Manual Rev. 3, MEASure command (printed p. 39 / PDF p. 49):
        # MEAS? applies preselected defaults, so READ? must be used to preserve
        # the range, integration time, and filters configured by this driver.
        return self.read()

    def diagnose(self) -> dict[str, str | None]:
        if not self.connected:
            raise InstrumentError("仪器未连接")
        return {
            "idn": self._client.query("*IDN?"),
            "function": self._client.query("SENS:FUNC?"),
            "terminals": self._client.query("ROUT:TERM?"),
            "error": self._client.query("SYST:ERR?"),
        }

    def zero(self) -> None:
        if not self.connected:
            raise InstrumentError("仪器未连接")
        self._client.write("ZERO:AUTO ONCE")
        self._drain_errors("ZERO操作后仪器报告错误", fail=True)

