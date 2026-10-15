"""Serial transport and conservative SCPI handling for the Fluke 8845A."""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable, Protocol

from .modes import Mode, ModeDescriptor, descriptor, mode_from_scpi_function

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

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


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
    # A semantic software version when the *IDN? revision field actually
    # contains one. For 8845A mode the field is commonly a date/time marker.
    firmware: str
    idn_raw: str
    idn_revision: str = ""


@dataclass(frozen=True, slots=True)
class ModeSettings:
    mode: Mode
    autorange: bool = True
    range_si: float | None = None
    nplc: float | None = None
    ac_bandwidth_hz: int | None = None
    analog_filter: bool = False
    autozero: bool = True

    def __post_init__(self) -> None:
        spec = descriptor(self.mode)
        if self.nplc is None and spec.nplc_values:
            object.__setattr__(self, "nplc", 1)
        if self.ac_bandwidth_hz is None and spec.ac_bandwidths_hz:
            object.__setattr__(self, "ac_bandwidth_hz", 20)

    def validate(self) -> ModeDescriptor:
        spec = descriptor(self.mode)
        if not self.autorange and self.range_si not in spec.ranges_si:
            raise ValueError(f"{spec.label}的固定量程不在8845A支持列表中")
        if self.nplc is not None and (not spec.nplc_values or self.nplc not in spec.nplc_values):
            raise ValueError(f"{spec.label}不支持此NPLC设置")
        if self.ac_bandwidth_hz is not None and self.ac_bandwidth_hz not in spec.ac_bandwidths_hz:
            raise ValueError(f"{spec.label}不支持此交流滤波档")
        if self.analog_filter and not spec.analog_filter:
            raise ValueError(f"{spec.label}不支持直流模拟滤波器")
        return spec


READ_RESPONSE_TIMEOUT_MARGIN_S = 3.0
AC_READ_RESPONSE_TIMEOUT_FLOOR_S = 8.0
CONSERVATIVE_LINE_FREQUENCY_HZ = 50.0


def measurement_read_timeout_s(settings: ModeSettings, base_timeout_s: float) -> float:
    """Add a conservative response window for slow 8845A conversions.

    The user-selected serial timeout remains the floor for ordinary commands.
    AC functions get a conservative fixed floor; NPLC functions scale with the
    longest common mains period (50 Hz), counting an additional conversion for
    autozero. These are host-side wait guards, not meter timing guarantees.
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
class Reading:
    raw: str
    value_si: float
    range_si: float | None
    started_monotonic_ns: int
    received_monotonic_ns: int
    timestamp_utc: str
    timestamp_local: str
    status: str = "ok"

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
            try:
                if self._port.is_open:
                    self._port.close()
            finally:
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
            self._port.reset_input_buffer()


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
    firmware = ""
    if version_match:
        candidate = version_match.group(1)
        if len(candidate.split(".")) in (2, 4):
            firmware = candidate
    return InstrumentInfo(
        manufacturer=fields[0], model=fields[1].upper(),
        serial_number=fields[2] if len(fields) > 2 else "",
        firmware=firmware,
        idn_raw=response.strip(),
        idn_revision=revision,
    )


def compare_firmware(firmware: str, minimum: tuple[int, ...]) -> bool | None:
    """Return None for an unrecognized firmware version (never guess support)."""
    match = re.search(r"\d+(?:\.\d+){1,3}", firmware)
    if not match:
        return None
    found = tuple(int(piece) for piece in match.group().split("."))
    # The 8845A's documented gates use either a two-part generation (2.0) or
    # a four-part build (1.0.700.18). A three-part value such as 1.0.0 is
    # ambiguous/date-like and must not be treated as a confirmed firmware.
    if len(found) not in (2, 4):
        return None
    size = max(len(found), len(minimum))
    return found + (0,) * (size - len(found)) >= minimum + (0,) * (size - len(minimum))


CURRENT_RANGE_FIRMWARE_MINIMUM = {
    Mode.DCI: (2, 0),
    Mode.ACI: (1, 0, 700, 18),
}
FRONT_ONLY_CURRENT_RANGES = (1.0, 3.0, 10.0)


def current_autorange_block_reason(mode: Mode, firmware: str, terminals: str | None) -> str | None:
    """Explain why current autorange must be blocked for known unsupported capabilities."""
    minimum = CURRENT_RANGE_FIRMWARE_MINIMUM.get(mode)
    if minimum is None:
        return None
    # Some 8845A units return a date-coded *IDN? field rather than a semantic
    # software version. An unparseable version is not evidence that 400 mA is
    # unsupported; only an explicitly older version blocks the range.
    if compare_firmware(firmware, minimum) is False:
        return "固件版本低于400 mA档要求"
    if not (terminals or "").strip().upper().startswith("FRON"):
        return "当前输入端子未确认是前面板；自动量程可能进入仅前面板支持的1/3/10 A档"
    return None


def unsupported_current_ranges(mode: Mode, firmware: str, terminals: str | None) -> tuple[float, ...]:
    """Return current ranges explicitly unsupported by known 8845A state."""
    minimum = CURRENT_RANGE_FIRMWARE_MINIMUM.get(mode)
    if minimum is None:
        return ()
    unsupported: set[float] = set()
    if compare_firmware(firmware, minimum) is False:
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

    def _log(self, direction: str, text: str) -> None:
        line = f"{direction} {text}"
        log.info("%s", line)
        self._log_callback(line)

    def open(self) -> None:
        self.transport.open()
        self.is_open = True
        self.transport.discard_input()

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self.is_open = False

    def write(self, command: str) -> None:
        if not self.is_open:
            raise InstrumentError("SCPI连接未打开")
        if "\r" in command or "\n" in command:
            raise ValueError("单条SCPI命令不能包含结束符")
        self._log(">", command)
        try:
            self.transport.write(command.encode("ascii") + self.settings.terminator)
        except Exception:
            self.is_open = False
            raise

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError("SCPI响应超时必须为正的有限数")
        self.transport.discard_input()
        self.write(command)
        try:
            if timeout_s is None:
                payload = self.transport.read_until(self.settings.terminator)
            else:
                payload = self.transport.read_until(self.settings.terminator, timeout_s)
        except Exception:
            self.is_open = False
            raise
        if not payload.endswith(self.settings.terminator):
            partial = payload.decode("ascii", errors="replace").strip()
            effective_timeout = self.settings.timeout_s if timeout_s is None else timeout_s
            self._log("!", f"{command}超时（{effective_timeout:g}s）；部分响应={partial!r}")
            self.transport.discard_input()
            raise ScpiTimeout(
                f"等待{command}响应超时（{effective_timeout:g}s）；部分数据：{partial!r}",
                command=command,
                partial_response=partial,
                timeout_s=effective_timeout,
            )
        response = payload[: -len(self.settings.terminator)].decode("ascii", errors="replace").strip()
        self._log("<", response)
        if response.casefold() == command.strip().casefold():
            raise Rs232TerminalModeError(
                "检测到8845A回显SCPI命令，RS-232当前处于终端模式；"
                "请在仪表前面板切换至Computer模式后重新连接。"
            )
        if re.match(r"\+?550\s*,", response) and "command not allowed in local" in response.casefold():
            self._log("!", "8845A拒绝远程命令：仪器已处于LOCAL本地控制")
            raise InstrumentLocalModeError(response)
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

    @property
    def connected(self) -> bool:
        return self._client.is_open

    @property
    def measurement_read_timeout_s(self) -> float:
        if self._active_settings is None:
            return self.settings.timeout_s
        return measurement_read_timeout_s(self._active_settings, self.settings.timeout_s)

    def connect(self) -> InstrumentInfo:
        self._client.open()
        try:
            idn = self._client.query("*IDN?")
            info = parse_idn(idn)
            if "FLUKE" not in info.manufacturer.upper() or info.model != "8845A":
                raise InstrumentIdentityError(f"需要Fluke 8845A，当前识别为：{idn}")
            self.info = info
            self._client.write("SYST:REM")
            self._drain_errors("连接时仪器已有错误", fail=False)
            return info
        except Exception:
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
        nplc = (
            self._readback_number("NPLC", self._client.query(f"{prefix}:NPLC?"))
            if spec.nplc_values else None
        )
        ac_bandwidth_hz = (
            int(self._readback_number("交流带宽", self._client.query("SENS:DET:BAND?")))
            if spec.ac_bandwidths_hz else None
        )
        analog_filter = (
            self._readback_boolean("模拟滤波", self._client.query(f"{prefix}:FILT:STAT?"))
            if spec.analog_filter else False
        )
        autozero = self._readback_boolean("自动零点", self._client.query("ZERO:AUTO?"))
        settings = ModeSettings(
            mode=mode,
            autorange=autorange,
            range_si=None if autorange else range_si,
            nplc=nplc,
            ac_bandwidth_hz=ac_bandwidth_hz,
            analog_filter=analog_filter,
            autozero=autozero,
        )
        settings.validate()
        return settings

    def _drain_errors(self, heading: str, *, fail: bool) -> list[str]:
        errors: list[str] = []
        for _ in range(16):
            response = self._client.query("SYST:ERR?")
            code, _message = self._parse_error(response)
            if code == 0:
                if fail and errors:
                    raise ScpiProtocolError(f"{heading}：" + "；".join(errors))
                return errors
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

    @staticmethod
    def _close_numeric(actual: float, expected: float) -> bool:
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)

    def configure(self, settings: ModeSettings) -> dict[str, str | float | bool | None]:
        spec = settings.validate()
        if not self.connected:
            raise InstrumentError("仪器未连接")
        assert self.info is not None
        self._drain_errors("配置前仪器错误", fail=False)
        terminals = self._client.query("ROUT:TERM?") if spec.quantity == "current" else None
        unsupported = unsupported_current_ranges(settings.mode, self.info.firmware, terminals)
        if spec.quantity == "current" and settings.autorange:
            reason = current_autorange_block_reason(settings.mode, self.info.firmware, terminals)
            if reason:
                raise UnsupportedRange(
                    f"8845A {spec.label}：{reason}。请关闭自动量程并选用已确认支持的固定量程。"
                )
        if not settings.autorange and settings.range_si in unsupported:
            if settings.range_si == 0.4:
                need = "2.0" if settings.mode == Mode.DCI else "1.0.700.18"
                detail = f"仪器回报的软件版本低于400 mA档要求的{need}"
            else:
                detail = "1 A、3 A和10 A量程仅在前面板电流端子支持"
            raise UnsupportedRange(f"8845A {spec.label}：{detail}，为避免无效/危险配置已拒绝。")
        # Invalidate the cached mode before changing the instrument. A failed
        # write or readback must not leave the acquisition layer believing that
        # the old or requested function is still safely configured.
        self._mode = None
        self._active_settings = None
        self._client.write(f"CONF:{spec.function}")
        prefix = spec.sense_prefix
        if settings.autorange:
            self._client.write(f"{prefix}:RANG:AUTO ON")
        else:
            self._client.write(f"{prefix}:RANG:AUTO OFF")
            assert settings.range_si is not None
            self._client.write(f"{prefix}:RANG {settings.range_si:.12g}")
        if settings.nplc is not None:
            self._client.write(f"{prefix}:NPLC {settings.nplc:g}")
        if settings.ac_bandwidth_hz is not None:
            # DETector:BANDwidth is the documented global AC filter selector.
            self._client.write(f"SENS:DET:BAND {settings.ac_bandwidth_hz}")
        if spec.analog_filter:
            self._client.write(f"{prefix}:FILT:STAT {'ON' if settings.analog_filter else 'OFF'}")
        self._client.write(f"ZERO:AUTO {'ON' if settings.autozero else 'OFF'}")
        self._drain_errors("配置后仪器报告错误", fail=True)
        actual_range = self._client.query(f"{prefix}:RANG?")
        actual_autorange = self._client.query(f"{prefix}:RANG:AUTO?")
        actual_nplc = self._client.query(f"{prefix}:NPLC?") if settings.nplc is not None else None
        actual_band = self._client.query("SENS:DET:BAND?") if settings.ac_bandwidth_hz is not None else None
        actual_filter = self._client.query(f"{prefix}:FILT:STAT?") if spec.analog_filter else None
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
            actual_band_value = self._readback_number("交流带宽", actual_band)
            if not self._close_numeric(actual_band_value, settings.ac_bandwidth_hz):
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
        actual_zero_value = self._readback_boolean("自动零点", actual_zero)
        if actual_zero_value != settings.autozero:
            raise ScpiProtocolError(
                f"自动零点回读不符：期望{'ON' if settings.autozero else 'OFF'}，"
                f"实际{actual_zero}"
            )
        self._mode = settings.mode
        self._active_settings = settings
        read_timeout = self.measurement_read_timeout_s
        if read_timeout > self.settings.timeout_s:
            self._logger(
                f"~ 慢速{spec.label} READ?响应上限按当前积分/滤波设置扩展为{read_timeout:g}s"
            )
        return {
            "function": actual_function,
            "range_response": actual_range,
            "autorange_response": actual_autorange,
            "nplc_response": actual_nplc,
            "ac_bandwidth_response": actual_band,
            "analog_filter_response": actual_filter,
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
        if settings.nplc is not None:
            self._client.write(f"{prefix}:NPLC {settings.nplc:g}")
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
            actual_band_value = self._readback_number("交流带宽", actual_band)
            band_matches = self._close_numeric(actual_band_value, settings.ac_bandwidth_hz)
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
        )
        read_timeout = self.measurement_read_timeout_s
        if read_timeout > self.settings.timeout_s:
            self._logger(f"~ 慢速READ?响应上限已更新为{read_timeout:g}s")
        return {
            "nplc_response": actual_nplc,
            "ac_bandwidth_response": actual_band,
            "measurement_read_timeout_s": read_timeout,
        }

    def read(self) -> Reading:
        if not self.connected or self._mode is None:
            raise InstrumentError("请先连接并配置测量模式")
        from datetime import datetime, timezone

        started_ns = time.monotonic_ns()
        now = datetime.now(timezone.utc)
        timestamp_utc = now.isoformat(timespec="microseconds")
        timestamp_local = now.astimezone().isoformat(timespec="microseconds")
        raw = self._client.query("READ?", timeout_s=self.measurement_read_timeout_s)
        parsed = parse_reading(raw)
        if parsed.value_si is None:
            raise ReadingError(parsed.detail, parsed.raw)
        spec = descriptor(self._mode)
        range_response = self._client.query(f"{spec.sense_prefix}:RANG?")
        range_value = parse_reading(range_response)
        received_ns = time.monotonic_ns()
        return Reading(
            raw=raw, value_si=parsed.value_si,
            range_si=range_value.value_si if range_value.status == "ok" else None,
            started_monotonic_ns=started_ns, received_monotonic_ns=received_ns,
            timestamp_utc=timestamp_utc, timestamp_local=timestamp_local,
        )

    def single(self) -> Reading:
        """Explicit alternative single-shot SCPI query, selected by the user."""
        if not self.connected or self._mode is None:
            raise InstrumentError("请先连接并配置测量模式")
        from datetime import datetime, timezone

        spec = descriptor(self._mode)
        started_ns = time.monotonic_ns()
        now = datetime.now(timezone.utc)
        timestamp_utc = now.isoformat(timespec="microseconds")
        timestamp_local = now.astimezone().isoformat(timespec="microseconds")
        raw = self._client.query(f"MEAS:{spec.function}?")
        parsed = parse_reading(raw)
        received_ns = time.monotonic_ns()
        if parsed.value_si is None:
            raise ReadingError(parsed.detail)
        return Reading(raw, parsed.value_si, None, started_ns, received_ns, timestamp_utc, timestamp_local)

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

