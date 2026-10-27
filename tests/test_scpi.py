from __future__ import annotations

import sys
import math

import pytest

from fluke8845a.modes import Mode, descriptor, mode_from_scpi_function, range_label
from fluke8845a.scpi import (
    FlowControl, InstrumentError, InstrumentIdentityError, InstrumentLocalModeError, ModeSettings, PySerialTransport, ReadingError, Rs232TerminalModeError,
    ScpiClient, ScpiProtocolError, ScpiTimeout, Serial8845A, SerialSettings,
    UnsupportedRange, compare_firmware, measurement_read_timeout_s, parse_idn, parse_reading,
    select_range_firmware,
)
from tests.fakes import FakeTransport


@pytest.mark.parametrize("raw,expected", [("+1.25E-3", 0.00125), ("-2.0", -2.0), ("0", 0.0)])
def test_parse_reading_accepts_finite_numeric_scalars(raw, expected):
    parsed = parse_reading(raw)
    assert parsed.status == "ok"
    assert parsed.value_si == expected
    assert parsed.raw == raw


@pytest.mark.parametrize("raw,status", [("", "empty"), ("9.9E37", "overflow"), ("OL", "overflow"), ("nan", "invalid"), ("abc", "invalid")])
def test_parse_reading_never_turns_invalid_responses_into_zero(raw, status):
    parsed = parse_reading(raw)
    assert parsed.value_si is None
    assert parsed.status == status


def test_idn_parser_keeps_unverified_version_candidate_and_model():
    info = parse_idn('FLUKE,8845A,"SN,42",2.0')
    assert info.model == "8845A"
    assert info.serial_number == "SN,42"
    assert info.idn_version_candidate == "2.0"
    assert info.idn_revision == "2.0"
    assert info.firmware == ""


def test_idn_date_time_revision_is_not_misreported_as_firmware():
    info = parse_idn("FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    assert info.idn_revision == "02/14/08-08:54"
    assert info.idn_version_candidate == ""


def test_idn_multi_component_software_version_is_preserved():
    info = parse_idn("FLUKE,8845A,TEST8845A001,OutG SW 2.10.882.24")
    assert info.idn_version_candidate == "2.10.882.24"


def test_connect_reports_port_and_8845a_identification_stages_and_enters_remote_first():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    stages = []

    instrument.connect(progress_callback=lambda stage, status, message: stages.append((stage, status, message)))

    assert [(stage, status) for stage, status, _message in stages] == [
        ("port", "running"), ("port", "success"),
        ("identify", "running"), ("identify", "success"),
    ]
    assert transport.commands[:2] == ["SYST:REM", "*IDN?"]
    instrument.close()


def test_connect_drains_an_exactly_full_8845a_error_queue():
    transport = FakeTransport()
    logs = []
    transport.error_queue.extend(
        f'-100,"queued error {index}"' for index in range(16)
    )
    instrument = Serial8845A(
        "COM3", SerialSettings(), log_callback=logs.append,
        transport_factory=lambda *_: transport,
    )

    info = instrument.connect()

    assert info.model == "8845A"
    assert instrument.connected
    assert transport.commands.count("SYST:ERR?") == 17
    assert sum(line.startswith("! 连接时仪器已有错误：") for line in logs) == 16
    assert not transport.error_queue
    instrument.close()


@pytest.mark.parametrize("error_count", [15, 16])
def test_error_drain_at_capacity_reports_meter_errors_after_empty_terminator(error_count):
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()
    transport.error_queue.extend(
        f'-100,"queued error {index}"' for index in range(error_count)
    )

    with pytest.raises(ScpiProtocolError, match="配置后仪器报告错误") as error:
        instrument._drain_errors("配置后仪器报告错误", fail=True)

    assert "错误队列超过16条" not in str(error.value)
    assert transport.commands.count("SYST:ERR?") == error_count + 1
    assert not transport.error_queue
    instrument.close()


def test_error_drain_does_not_record_a_seventeenth_error():
    transport = FakeTransport()
    logs = []
    instrument = Serial8845A(
        "COM3", SerialSettings(), log_callback=logs.append,
        transport_factory=lambda *_: transport,
    )
    instrument.connect()
    transport.commands.clear()
    logs.clear()
    transport.error_queue.extend(
        f'-100,"queued error {index}"' for index in range(17)
    )

    with pytest.raises(ScpiProtocolError, match="错误队列超过16条"):
        instrument._drain_errors("连接诊断", fail=True)

    assert transport.commands.count("SYST:ERR?") == 17
    assert sum(line.startswith("! 连接诊断：") for line in logs) == 16
    assert not transport.error_queue
    instrument.close()


def test_connection_probe_rechecks_identity_without_changing_measurement_setup():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    info = instrument.check_connection()

    assert info.model == "8845A"
    assert transport.commands == ["*IDN?"]
    instrument.close()


@pytest.mark.parametrize(
    ("trigger_source", "trigger_count", "sample_count", "expected_message", "expected_queries"),
    [
        ("BUS", "1", "1", "IMM", ["TRIG:SOUR?"]),
        ("EXT", "1", "1", "IMM", ["TRIG:SOUR?"]),
        ("IMM", "2", "1", "TRIG:COUN?", ["TRIG:SOUR?", "TRIG:COUN?"]),
        ("IMM", "1", "2", "SAMP:COUN?", ["TRIG:SOUR?", "TRIG:COUN?", "SAMP:COUN?"]),
    ],
)
def test_configure_refuses_invalid_post_conf_trigger_state(
    trigger_source, trigger_count, sample_count, expected_message, expected_queries,
):
    class TriggerStateTransport(FakeTransport):
        def read_until(self, terminator, timeout_s=None):
            command = self.last_command.upper()
            response = {
                "TRIG:SOUR?": trigger_source,
                "TRIG:COUN?": trigger_count,
                "SAMP:COUN?": sample_count,
            }.get(command)
            if response is not None:
                return str(response).encode("ascii") + terminator
            return super().read_until(terminator, timeout_s)

    transport = TriggerStateTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    with pytest.raises(InstrumentError, match=expected_message):
        instrument.configure(ModeSettings(Mode.DCV))

    conf_index = transport.commands.index("CONF:VOLT:DC")
    trigger_query_commands = {"TRIG:SOUR?", "TRIG:COUN?", "SAMP:COUN?"}
    actual_queries = [command for command in transport.commands if command in trigger_query_commands]
    assert actual_queries == expected_queries
    assert conf_index < transport.commands.index(expected_queries[0])
    assert not any(
        command.startswith("SENS:VOLT:DC:RANG") or command.startswith("SENS:VOLT:DC:NPLC")
        for command in transport.commands[conf_index + 1:]
    )
    assert "READ?" not in transport.commands
    assert instrument._mode is None
    instrument.close()


def test_configure_accepts_immediate_single_read_trigger_without_mutating_it():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    instrument.configure(ModeSettings(Mode.DCV))

    commands = transport.commands
    trigger_queries = ["TRIG:SOUR?", "TRIG:COUN?", "SAMP:COUN?"]
    assert [command for command in commands if command in trigger_queries] == trigger_queries
    assert commands.index("CONF:VOLT:DC") < min(commands.index(command) for command in trigger_queries)
    assert not any(command in {"TRIG:SOUR IMM", "TRIG:COUN 1", "SAMP:COUN 1"} for command in commands)
    assert instrument._mode is Mode.DCV
    instrument.close()


@pytest.mark.parametrize(
    ("initial_source", "initial_trigger_count", "initial_sample_count"),
    [("BUS", "1", "1"), ("EXT", "1", "1"), ("IMM", "2", "1"), ("IMM", "1", "2")],
)
def test_conf_resets_nondefault_trigger_state_to_documented_single_point_preset(
    initial_source, initial_trigger_count, initial_sample_count,
):
    transport = FakeTransport()
    transport.trigger_source = initial_source
    transport.trigger_count = initial_trigger_count
    transport.sample_count = initial_sample_count
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    instrument.configure(ModeSettings(Mode.DCV))

    assert (transport.trigger_source, transport.trigger_count, transport.sample_count) == (
        "IMM", "1", "1",
    )
    assert instrument._mode is Mode.DCV
    instrument.close()


def test_connect_enters_remote_before_querying_a_meter_that_starts_local():
    class RemoteRequiredTransport(FakeTransport):
        remote = False

        def write(self, payload):
            super().write(payload)
            command = self.last_command.upper()
            if command == "SYST:REM":
                self.remote = True
            elif command == "SYST:LOC":
                self.remote = False

        def read_until(self, terminator, timeout_s=None):
            if self.last_command.endswith("?") and not self.remote:
                return b'+550,"Command not allowed in local"' + terminator
            return super().read_until(terminator, timeout_s)

    transport = RemoteRequiredTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)

    info = instrument.connect()

    assert info.model == "8845A"
    assert transport.commands[:2] == ["SYST:REM", "*IDN?"]
    instrument.close()


def test_connect_reports_identification_failure_for_wrong_model_and_restores_local():
    transport = FakeTransport("FLUKE,8846A,123,2.0")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    stages = []

    with pytest.raises(InstrumentIdentityError):
        instrument.connect(progress_callback=lambda stage, status, message: stages.append((stage, status, message)))

    assert [(stage, status) for stage, status, _message in stages] == [
        ("port", "running"), ("port", "success"),
        ("identify", "running"), ("identify", "failure"),
    ]
    assert transport.commands[:3] == ["SYST:REM", "*IDN?", "SYST:LOC"]


def test_connect_reports_serial_open_failure_without_entering_identification():
    class FailedOpenTransport(FakeTransport):
        def open(self):
            raise OSError("mock port is busy")

    transport = FailedOpenTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    stages = []

    with pytest.raises(Exception, match="mock port is busy"):
        instrument.connect(progress_callback=lambda stage, status, message: stages.append((stage, status, message)))

    assert [(stage, status) for stage, status, _message in stages] == [
        ("port", "running"), ("port", "failure"),
    ]


def test_control_mode_switches_use_only_documented_remote_and_local_commands():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    assert instrument.set_control_mode("local") is None
    assert transport.commands == ["SYST:LOC"]
    info = instrument.set_control_mode("computer")
    assert info is not None and info.model == "8845A"
    assert transport.commands == ["SYST:LOC", "SYST:REM", "*IDN?"]
    instrument.close()


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        ("VOLT:DC", Mode.DCV), ("VOLT:AC", Mode.ACV),
        ("CURR:DC", Mode.DCI), ("CURR:AC", Mode.ACI),
        ('"RES"', Mode.RES2), ("FRES", Mode.RES4),
        ('"VOLT"', Mode.DCV), ("CURR", Mode.DCI),
        ("RESISTANCE", Mode.RES2), ("FRESISTANCE", Mode.RES4),
        ("DCV", Mode.DCV), ("CAP", None),
    ],
)
def test_supported_modes_are_mapped_from_instrument_function(function, expected):
    assert mode_from_scpi_function(function) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1e-9, "1 nA"), (100e-6, "100 µA"), (10e-3, "10 mA"), (1.0, "1 A")],
)
def test_current_range_label_uses_readable_engineering_units(value, expected):
    assert range_label(value, "A") == expected


def test_non_8845a_is_rejected_and_local_control_is_restored():
    transport = FakeTransport("FLUKE,8846A,123,2.0")
    instrument = Serial8845A("COM4", SerialSettings(), transport_factory=lambda *_: transport)
    with pytest.raises(InstrumentIdentityError):
        instrument.connect()
    assert transport.commands == ["SYST:REM", "*IDN?", "SYST:LOC"]
    assert not transport.is_open


def test_serial_timeout_invalidates_session_before_a_late_response_can_be_read():
    class DelayedResponseTransport(FakeTransport):
        read_calls = 0

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            self.read_calls += 1
            if self.read_calls == 1:
                return b"partial"
            return b"late response" + terminator

    transport = DelayedResponseTransport()
    client = ScpiClient(transport, SerialSettings())
    client.open()
    with pytest.raises(ScpiTimeout):
        client.query("*IDN?")
    assert not client.is_open
    assert not transport.is_open

    with pytest.raises(InstrumentError, match="SCPI连接未打开"):
        client.query("SYST:ERR?")
    assert transport.commands == ["*IDN?"]
    assert transport.read_calls == 1


def test_scpi_write_io_failure_closes_transport_and_marks_client_closed():
    class FailingWriteTransport(FakeTransport):
        def write(self, payload: bytes):
            raise OSError("simulated serial write failure")

    transport = FailingWriteTransport()
    client = ScpiClient(transport, SerialSettings())
    client.open()

    with pytest.raises(InstrumentError, match="SCPI命令写入失败") as error:
        client.write("SYST:REM")

    assert not client.is_open
    assert not transport.is_open
    assert isinstance(error.value.__cause__, OSError)


def test_scpi_discard_input_io_failure_closes_transport_and_marks_client_closed():
    class FailingDiscardTransport(FakeTransport):
        fail_discard = False

        def discard_input(self):
            if self.fail_discard:
                raise OSError("simulated input-buffer failure")

    transport = FailingDiscardTransport()
    client = ScpiClient(transport, SerialSettings())
    client.open()
    transport.fail_discard = True

    with pytest.raises(InstrumentError, match="SCPI输入缓冲操作失败") as error:
        client.query("*IDN?")

    assert not client.is_open
    assert not transport.is_open
    assert isinstance(error.value.__cause__, OSError)


def test_scpi_open_input_buffer_failure_rolls_back_transport():
    class FailingDiscardTransport(FakeTransport):
        def discard_input(self):
            raise OSError("simulated initial input-buffer failure")

    transport = FailingDiscardTransport()
    client = ScpiClient(transport, SerialSettings())

    with pytest.raises(InstrumentError, match="SCPI输入缓冲操作失败") as error:
        client.open()

    assert not client.is_open
    assert not transport.is_open
    assert isinstance(error.value.__cause__, OSError)


def test_instrument_close_releases_transport_after_read_exception():
    class FailingReadTransport(FakeTransport):
        fail_read = False

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.fail_read and self.last_command.upper() == "READ?":
                raise OSError("simulated serial read failure")
            return super().read_until(terminator, timeout_s)

    transport = FailingReadTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))
    transport.fail_read = True

    with pytest.raises(InstrumentError, match="SCPI响应读取失败") as error:
        instrument.read()
    assert not instrument.connected
    assert not transport.is_open
    assert isinstance(error.value.__cause__, OSError)
    instrument.close()
    assert not transport.is_open


def test_serial_terminator_configuration_is_respected():
    settings = SerialSettings(eol="CRLF", flow_control=FlowControl.RTSCTS)
    transport = FakeTransport(settings=settings)
    client = ScpiClient(transport, settings)
    client.open()
    assert client.query("*IDN?").startswith("FLUKE,8845A")
    assert transport.commands[0] == "*IDN?"
    assert settings.terminator == b"\r\n"


def test_default_serial_settings_match_confirmed_instrument_parameters():
    settings = SerialSettings()
    assert (settings.baudrate, settings.bytesize, settings.parity, settings.stopbits) == (9600, 8, "N", 1)
    assert settings.flow_control == FlowControl.NONE
    assert settings.eol == "CRLF"


@pytest.mark.parametrize(
    ("flow_control", "expected_flow"),
    [
        (FlowControl.NONE, {"xonxoff": False, "rtscts": False}),
        (FlowControl.XONXOFF, {"xonxoff": True, "rtscts": False}),
        (FlowControl.RTSCTS, {"xonxoff": False, "rtscts": True}),
    ],
)
def test_pyserial_transport_maps_windows_serial_settings_without_opening_hardware(
    monkeypatch, flow_control, expected_flow
):
    from types import SimpleNamespace

    captured = {}
    opened_port = SimpleNamespace(is_open=True)

    def serial_factory(**kwargs):
        captured.update(kwargs)
        return opened_port

    serial_stub = SimpleNamespace(
        PARITY_NONE="N",
        PARITY_EVEN="E",
        PARITY_ODD="O",
        STOPBITS_ONE=1,
        STOPBITS_TWO=2,
        Serial=serial_factory,
    )
    monkeypatch.setitem(sys.modules, "serial", serial_stub)
    settings = SerialSettings(
        baudrate=115200,
        bytesize=8,
        parity="N",
        stopbits=1,
        flow_control=flow_control,
        eol="CRLF",
        timeout_s=1.25,
    )
    transport = PySerialTransport("COM3", settings)

    transport.open()

    assert captured == {
        "port": "COM3",
        "baudrate": 115200,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "timeout": 1.25,
        "write_timeout": 1.25,
        **expected_flow,
        "dsrdtr": False,
    }
    assert transport._port is opened_port


def test_pyserial_transport_leaves_no_open_port_when_serial_constructor_fails(monkeypatch):
    from types import SimpleNamespace

    def fail_open(**_kwargs):
        raise OSError("simulated COM3 unavailable")

    serial_stub = SimpleNamespace(
        PARITY_NONE="N",
        PARITY_EVEN="E",
        PARITY_ODD="O",
        STOPBITS_ONE=1,
        STOPBITS_TWO=2,
        Serial=fail_open,
    )
    monkeypatch.setitem(sys.modules, "serial", serial_stub)
    transport = PySerialTransport("COM3", SerialSettings())

    with pytest.raises(InstrumentError, match="打开串口COM3失败"):
        transport.open()

    assert transport._port is None


@pytest.mark.parametrize(("bytesize", "parity"), [(8, "N"), (7, "E"), (7, "O")])
def test_serial_settings_accept_only_documented_data_and_parity_pairs(bytesize, parity):
    settings = SerialSettings(bytesize=bytesize, parity=parity)

    assert (settings.bytesize, settings.parity) == (bytesize, parity)


@pytest.mark.parametrize(("bytesize", "parity"), [(7, "N"), (8, "E"), (8, "O")])
def test_serial_settings_reject_unsupported_data_and_parity_pairs(bytesize, parity):
    with pytest.raises(ValueError, match="仅支持 8N、7E 或 7O"):
        SerialSettings(bytesize=bytesize, parity=parity)


@pytest.mark.parametrize("timeout_s", [0, -1, float("nan"), float("inf"), float("-inf")])
def test_serial_timeout_must_be_positive_and_finite(timeout_s):
    with pytest.raises(ValueError, match="正的有限数"):
        SerialSettings(timeout_s=timeout_s)


@pytest.mark.parametrize("fails", [False, True])
def test_pyserial_transport_restores_base_timeout_after_scoped_read(fails):
    class StubPort:
        is_open = True
        timeout = 2.0

        def read_until(self, _terminator):
            assert self.timeout == 7.0
            if fails:
                raise OSError("simulated read failure")
            return b"ok\r\n"

    transport = PySerialTransport("COM3", SerialSettings(timeout_s=2.0))
    port = StubPort()
    transport._port = port
    if fails:
        with pytest.raises(InstrumentError, match="串口读取失败"):
            transport.read_until(b"\r\n", timeout_s=7.0)
    else:
        assert transport.read_until(b"\r\n", timeout_s=7.0) == b"ok\r\n"
    assert port.timeout == 2.0


def test_pyserial_transport_close_failure_keeps_port_reference_for_retry():
    class StubPort:
        is_open = True

        def close(self):
            raise OSError("simulated close failure")

    transport = PySerialTransport("COM3", SerialSettings())
    port = StubPort()
    transport._port = port

    with pytest.raises(OSError, match="simulated close failure"):
        transport.close()

    assert transport._port is port


def test_pyserial_transport_wraps_input_buffer_io_failure():
    class StubPort:
        is_open = True

        def reset_input_buffer(self):
            raise OSError("simulated reset failure")

    transport = PySerialTransport("COM3", SerialSettings())
    transport._port = StubPort()

    with pytest.raises(InstrumentError, match="清空串口输入缓冲失败") as error:
        transport.discard_input()

    assert isinstance(error.value.__cause__, OSError)


def test_scpi_partial_response_timeout_invalidates_client_if_port_close_fails():
    class FailingCloseTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            return b"partial"

        def close(self):
            raise OSError("simulated timeout close failure")

    transport = FailingCloseTransport()
    client = ScpiClient(transport, SerialSettings())
    client.open()

    with pytest.raises(ScpiTimeout) as error:
        client.query("*IDN?")

    assert error.value.partial_response == "partial"
    assert not client.is_open
    assert transport.is_open  # underlying close failed, but the SCPI session stays unusable
    with pytest.raises(InstrumentError, match="SCPI连接未打开"):
        client.query("SYST:ERR?")
    assert transport.commands == ["*IDN?"]


def test_read_response_timeout_expands_for_slow_measurement_settings():
    assert measurement_read_timeout_s(ModeSettings(Mode.DCI, nplc=100), 2.0) == pytest.approx(7.0)
    assert measurement_read_timeout_s(ModeSettings(Mode.DCI, nplc=100, autozero=False), 2.0) == pytest.approx(5.0)
    assert measurement_read_timeout_s(ModeSettings(Mode.ACV, ac_bandwidth_hz=3), 2.0) == 8.0
    assert measurement_read_timeout_s(ModeSettings(Mode.DCV, nplc=1), 10.0) == 10.0


def test_read_query_uses_derived_timeout_only_for_read_response():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.01, nplc=100))
    transport.read_timeouts.clear()

    instrument.read()

    timeouts = dict(transport.read_timeouts)
    assert timeouts["READ?"] == pytest.approx(7.0)
    assert timeouts["SENS:CURR:DC:RANG?"] is None
    instrument.close()


def test_single_measurement_uses_configured_read_path_and_returns_verified_range():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV, autorange=False, range_si=1.0, nplc=0.2))
    transport.commands.clear()

    reading = instrument.single()

    assert reading.raw == "+1.2345678901234567E+00"
    assert reading.value_si == pytest.approx(1.2345678901234567)
    assert reading.range_si == pytest.approx(1.0)
    assert "READ?" in transport.commands
    assert "SENS:VOLT:DC:RANG?" in transport.commands
    assert not any(command.startswith("MEAS:") for command in transport.commands)
    instrument.close()


def test_read_response_boundary_is_before_the_following_range_query(monkeypatch):
    import fluke8845a.scpi as scpi_module

    events = []
    clock_value = 0

    def perf_counter_ns():
        nonlocal clock_value
        clock_value += 1_000_000
        events.append("host-clock")
        return clock_value

    class OrderedTransport(FakeTransport):
        def read_until(self, terminator, timeout_s=None):
            if self.last_command == "READ?":
                events.append("read-response")
            elif self.last_command.endswith(":RANG?"):
                events.append("range-readback")
            return super().read_until(terminator, timeout_s)

    transport = OrderedTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))
    events.clear()
    monkeypatch.setattr(scpi_module.time, "perf_counter_ns", perf_counter_ns)

    reading = instrument.read()

    assert events == [
        "host-clock", "read-response", "host-clock",
        "host-clock", "range-readback", "host-clock",
    ]
    assert reading.read_duration_s == pytest.approx(0.001)
    assert reading.read_response_end_local
    assert reading.read_response_end_utc.endswith("+00:00")
    assert reading.read_request_start_monotonic_ns < reading.read_response_end_monotonic_ns
    assert reading.timestamp_local == reading.read_request_start_local
    assert reading.timestamp_utc == reading.read_request_start_utc
    assert [command for command in transport.commands if command == "READ?"] == ["READ?"]
    instrument.close()


def test_read_timeout_carries_host_request_and_response_boundaries():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))
    transport.timeouts.add("READ?")

    with pytest.raises(ScpiTimeout) as caught:
        instrument.read()

    timing = caught.value.read_timing
    assert timing.read_request_start_local
    assert timing.read_request_start_utc.endswith("+00:00")
    assert timing.read_request_start_monotonic_ns <= timing.read_response_end_monotonic_ns
    assert timing.read_response_end_local
    assert timing.read_response_end_utc.endswith("+00:00")
    instrument.close()


def test_reading_error_preserves_overrange_status_and_raw_response():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCI))
    original_read_until = transport.read_until

    def respond_overrange(terminator, timeout_s=None):
        if transport.last_command == "READ?":
            return b"OL" + terminator
        return original_read_until(terminator, timeout_s)

    transport.read_until = respond_overrange
    with pytest.raises(ReadingError) as error:
        instrument.read()

    assert error.value.status == "overflow"
    assert error.value.raw == "OL"
    assert error.value.read_timing.read_response_end_monotonic_ns >= error.value.read_timing.read_request_start_monotonic_ns
    instrument.close()


def test_range_query_failure_does_not_replace_read_response_timing(monkeypatch):
    import fluke8845a.scpi as scpi_module

    clock_value = 0

    def perf_counter_ns():
        nonlocal clock_value
        clock_value += 1_000_000
        return clock_value

    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))
    transport.timeouts.add("SENS:VOLT:DC:RANG?")
    transport.commands.clear()
    monkeypatch.setattr(scpi_module.time, "perf_counter_ns", perf_counter_ns)

    with pytest.raises(ScpiTimeout) as caught:
        instrument.read()

    assert caught.value.measurement_raw == "+1.2345678901234567E+00"
    read_timing = caught.value.read_timing
    range_query_timing = caught.value.query_timing
    assert read_timing.read_response_end_monotonic_ns < range_query_timing.read_request_start_monotonic_ns
    assert [command for command in transport.commands if command in {"READ?", "SENS:VOLT:DC:RANG?"}] == [
        "READ?", "SENS:VOLT:DC:RANG?",
    ]
    instrument.close()


@pytest.mark.parametrize("mode", list(Mode))
@pytest.mark.parametrize("actual_range", ["not-a-number", "nan", "99999"])
def test_read_rejects_invalid_or_unsupported_actual_range(mode, actual_range):
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(mode))
    transport.range_value = actual_range

    with pytest.raises(ScpiProtocolError, match="量程回读"):
        instrument.read()

    assert "READ?" in transport.commands
    assert f"{descriptor(mode).sense_prefix}:RANG?" in transport.commands
    instrument.close()


def test_serial_flow_control_normalizes_known_values_and_rejects_unknown_ones():
    assert SerialSettings(flow_control="xonxoff").flow_control == FlowControl.XONXOFF
    with pytest.raises(ValueError, match="流控制"):
        SerialSettings(flow_control="hardware-and-magic")


def test_command_echo_is_reported_as_terminal_mode_instead_of_reading_fake_data():
    transport = FakeTransport()
    transport.echo_commands = True
    client = ScpiClient(transport, SerialSettings())
    client.open()
    with pytest.raises(Rs232TerminalModeError, match="Computer模式"):
        client.query("*IDN?")
    client.close()


def test_local_control_rejection_is_not_parsed_as_a_measurement():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))
    transport.local_error_on_read = True

    with pytest.raises(InstrumentLocalModeError, match="LOCAL本地控制") as error:
        instrument.read()

    assert error.value.raw == '+550,"Command not allowed in local"'
    assert instrument.connected
    instrument.close()


def test_dcv_configuration_uses_conf_range_nplc_filter_and_checks_error_queue():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    result = instrument.configure(ModeSettings(Mode.DCV, autorange=False, range_si=10, nplc=0.2, analog_filter=True))
    assert "CONF:VOLT:DC" in transport.commands
    assert "SENS:VOLT:DC:RANG:AUTO OFF" in transport.commands
    assert "SENS:VOLT:DC:RANG 10" in transport.commands
    assert "SENS:VOLT:DC:NPLC 0.2" in transport.commands
    assert "SENS:VOLT:DC:FILT:STAT ON" in transport.commands
    assert "SENS:VOLT:DC:FILT:DIG OFF" in transport.commands
    assert "ZERO:AUTO ON" in transport.commands
    assert result["digital_filter_response"] == "0"
    instrument.close()


def test_dcv_auto_input_impedance_is_explicitly_written_and_read_back():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    result = instrument.configure(ModeSettings(Mode.DCV, input_impedance_auto=True))

    assert "INP:IMP:AUTO ON" in transport.commands
    assert "INP:IMP:AUTO?" in transport.commands
    assert result["input_impedance_auto_response"] == "1"
    assert instrument.read_settings(Mode.DCV).input_impedance_auto is True
    instrument.close()


def test_dcv_configuration_explicitly_restores_default_10_megohm_input_impedance():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    result = instrument.configure(ModeSettings(Mode.DCV))

    assert "INP:IMP:AUTO OFF" in transport.commands
    assert result["input_impedance_auto_response"] == "0"
    instrument.close()


def test_known_pre_2_outguard_rejects_digital_filter_before_changing_mode():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("1.9.0.0", "")

    with pytest.raises(UnsupportedRange, match="OutG SW.*早于 2.0"):
        instrument.configure(ModeSettings(Mode.DCV, nplc=10, digital_filter=True))

    assert not any(command.startswith("CONF:") for command in transport.commands)
    assert not any(":FILT:DIG ON" in command for command in transport.commands)
    instrument.close()


def test_unknown_outguard_allows_filter_probe_and_keeps_read_timeout_finite():
    transport = FakeTransport(idn="FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM7", SerialSettings(timeout_s=2), transport_factory=lambda *_: transport)
    instrument.connect()

    result = instrument.configure(ModeSettings(Mode.DCV, nplc=10, digital_filter=True))

    assert result["digital_filter_response"] == "1"
    assert math.isfinite(result["measurement_read_timeout_s"])
    assert result["measurement_read_timeout_s"] >= 2
    assert instrument._active_settings.digital_filter is True
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        (Mode.DCV, "SENS:VOLT:DC"),
        (Mode.DCI, "SENS:CURR:DC"),
        (Mode.RES2, "SENS:RES"),
        (Mode.RES4, "SENS:FRES"),
    ],
)
def test_slow_dc_configuration_enables_and_verifies_requested_digital_filter(mode, prefix):
    transport = FakeTransport(idn="FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("2.10.882.24", "")
    transport.commands.clear()

    settings = ModeSettings(mode, nplc=10, digital_filter=True)
    result = instrument.configure(settings)

    off = f"{prefix}:FILT:DIG OFF"
    readback = f"{prefix}:FILT:DIG?"
    nplc = f"{prefix}:NPLC 10"
    on = f"{prefix}:FILT:DIG ON"
    assert transport.commands.index(off) < transport.commands.index(readback) < transport.commands.index(nplc)
    assert transport.commands.index(nplc) < transport.commands.index(on)
    assert transport.commands.index(on) < len(transport.commands) - 1 - transport.commands[::-1].index(readback)
    assert result["digital_filter_response"] == "1"
    assert instrument.read_settings(mode).digital_filter is True
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        (Mode.DCV, "SENS:VOLT:DC"),
        (Mode.DCI, "SENS:CURR:DC"),
        (Mode.RES2, "SENS:RES"),
        (Mode.RES4, "SENS:FRES"),
    ],
)
@pytest.mark.parametrize("revision", ["1.0", "02/14/08-08:54"])
def test_slow_dc_configuration_disables_and_verifies_digital_filter_before_nplc(mode, prefix, revision):
    transport = FakeTransport(idn=f"FLUKE,8845A,TEST8845A001,{revision}")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    settings = (
        ModeSettings(mode, autorange=False, range_si=0.01, nplc=10)
        if mode == Mode.DCI else ModeSettings(mode, nplc=10)
    )
    result = instrument.configure(settings)

    off = f"{prefix}:FILT:DIG OFF"
    readback = f"{prefix}:FILT:DIG?"
    nplc = f"{prefix}:NPLC 10"
    assert transport.commands.index(off) < transport.commands.index(readback) < transport.commands.index(nplc)
    assert result["digital_filter_response"] == "0"
    instrument.close()


def test_slow_dc_configuration_does_not_write_nplc_when_filter_readback_stays_on():
    class FilterStaysOnTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command.upper().endswith(":FILT:DIG?"):
                return b"1" + terminator
            return super().read_until(terminator, timeout_s)

    transport = FilterStaysOnTransport(idn="FLUKE,8845A,TEST8845A001,1.0")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    with pytest.raises(ScpiProtocolError, match="数字滤波回读不符"):
        instrument.configure(ModeSettings(Mode.DCV, nplc=10))

    assert "SENS:VOLT:DC:NPLC 10" not in transport.commands
    assert instrument._mode is None
    instrument.close()


def test_slow_dc_configuration_does_not_write_nplc_when_filter_off_command_errors():
    class FilterOffCommandErrorTransport(FakeTransport):
        def write(self, payload: bytes):
            super().write(payload)
            if payload.decode("ascii").strip().upper().endswith(":FILT:DIG OFF"):
                self.error_queue.append('104,"Command not supported"')

    transport = FilterOffCommandErrorTransport(idn="FLUKE,8845A,TEST8845A001,1.0")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    with pytest.raises(ScpiProtocolError, match="关闭数字滤波"):
        instrument.configure(ModeSettings(Mode.DCV, nplc=10))

    assert "SENS:VOLT:DC:NPLC 10" not in transport.commands
    assert "SENS:VOLT:DC:FILT:DIG?" not in transport.commands
    instrument.close()


@pytest.mark.parametrize("filter_response", ["not-a-state", "timeout"])
def test_slow_dc_configuration_fails_closed_when_filter_off_cannot_be_confirmed(filter_response):
    class UnconfirmedFilterTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command.upper().endswith(":FILT:DIG?"):
                if filter_response == "timeout":
                    return b"partial"
                return b"not-a-state" + terminator
            return super().read_until(terminator, timeout_s)

    transport = UnconfirmedFilterTransport(idn="FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM7", SerialSettings(timeout_s=0.05), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    with pytest.raises((ScpiProtocolError, ScpiTimeout)):
        instrument.configure(ModeSettings(Mode.DCV, nplc=10))

    assert "SENS:VOLT:DC:NPLC 10" not in transport.commands
    instrument.close()


def test_live_slow_nplc_update_rechecks_filter_off_before_writing_nplc():
    transport = FakeTransport(idn="FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV, nplc=1))
    transport.commands.clear()

    instrument.set_measurement_speed(ModeSettings(Mode.DCV, nplc=10))

    off = "SENS:VOLT:DC:FILT:DIG OFF"
    readback = "SENS:VOLT:DC:FILT:DIG?"
    nplc = "SENS:VOLT:DC:NPLC 10"
    assert transport.commands.index(off) < transport.commands.index(readback) < transport.commands.index(nplc)
    instrument.close()


def test_live_slow_nplc_update_fails_closed_when_filter_readback_is_on():
    class FilterReadbackCanStayOnTransport(FakeTransport):
        filter_stays_on = False

        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.filter_stays_on and self.last_command.upper().endswith(":FILT:DIG?"):
                return b"1" + terminator
            return super().read_until(terminator, timeout_s)

    transport = FilterReadbackCanStayOnTransport(idn="FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV, nplc=1))
    transport.commands.clear()
    transport.filter_stays_on = True

    with pytest.raises(ScpiProtocolError, match="数字滤波回读不符"):
        instrument.set_measurement_speed(ModeSettings(Mode.DCV, nplc=10))

    assert "SENS:VOLT:DC:NPLC 10" not in transport.commands
    instrument.close()


def test_readback_accepts_enabled_digital_filter_when_filter_is_inactive_at_one_nplc():
    transport = FakeTransport()
    transport.digital_filters["SENS:VOLT:DC"] = "1"
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    settings = instrument.read_settings(Mode.DCV)

    assert settings.nplc == 1
    assert settings.digital_filter is True
    instrument.close()


def test_dc_digital_filter_is_probed_and_explicitly_disabled_on_supported_firmware():
    transport = FakeTransport(idn="FLUKE,8845A,TEST8845A001,1.0")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("2.10.882.24", "")

    settings = instrument.read_settings(Mode.DCV)
    assert settings.digital_filter is False
    assert "SENS:VOLT:DC:FILT:DIG?" in transport.commands
    transport.commands.clear()
    result = instrument.configure(ModeSettings(Mode.DCV))

    assert result["digital_filter_response"] == "0"
    assert "SENS:VOLT:DC:FILT:DIG OFF" in transport.commands
    assert "SENS:VOLT:DC:FILT:DIG?" in transport.commands
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "prefix"),
    [
        (Mode.DCV, "SENS:VOLT:DC"),
        (Mode.DCI, "SENS:CURR:DC"),
        (Mode.RES2, "SENS:RES"),
        (Mode.RES4, "SENS:FRES"),
    ],
)
def test_known_pre_2_digital_filter_firmware_skips_filter_commands_but_keeps_nplc(mode, prefix):
    transport = FakeTransport(idn="FLUKE,8845A,TEST8845A001,1.0")
    transport.function = descriptor(mode).function
    transport.range_value = descriptor(mode).ranges_si[0]
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("1.9.0.0", "")

    actual = instrument.read_settings(mode)
    assert actual.digital_filter is False
    assert f"{prefix}:FILT:DIG?" not in transport.commands

    transport.commands.clear()
    configured_settings = ModeSettings(
        mode, autorange=False, range_si=descriptor(mode).ranges_si[0], nplc=10,
    )
    result = instrument.configure(configured_settings)
    assert result["digital_filter_response"] is None
    assert f"{prefix}:FILT:DIG OFF" not in transport.commands
    assert f"{prefix}:FILT:DIG?" not in transport.commands
    assert f"{prefix}:NPLC 10" in transport.commands

    transport.commands.clear()
    instrument.set_measurement_speed(ModeSettings(mode, nplc=1))
    assert f"{prefix}:FILT:DIG OFF" not in transport.commands
    assert f"{prefix}:FILT:DIG?" not in transport.commands
    assert f"{prefix}:NPLC 1" in transport.commands
    instrument.close()


def test_unknown_outguard_at_one_nplc_also_explicitly_disables_and_reads_filter():
    transport = FakeTransport(idn="FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    settings = instrument.read_settings(Mode.DCV)
    assert "SENS:VOLT:DC:FILT:DIG?" in transport.commands
    transport.commands.clear()
    result = instrument.configure(ModeSettings(Mode.DCV, nplc=1))

    assert settings.nplc == 1
    assert settings.digital_filter is False
    assert "SENS:VOLT:DC:FILT:DIG OFF" in transport.commands
    assert "SENS:VOLT:DC:FILT:DIG?" in transport.commands
    assert result["digital_filter_response"] == "0"
    instrument.close()


@pytest.mark.parametrize("firmware", ["02/14/08-08:54", "1.0"])
def test_unverified_outguard_can_configure_slow_nplc_after_filter_readback(firmware):
    transport = FakeTransport(idn=f"FLUKE,8845A,TEST8845A001,{firmware}")
    transport.nplc = "10"
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.commands.clear()

    actual = instrument.read_settings(Mode.DCV)
    assert actual.nplc == 10
    assert actual.digital_filter is False

    transport.commands.clear()
    instrument.configure(ModeSettings(Mode.DCV, nplc=10))
    assert "SENS:VOLT:DC:FILT:DIG OFF" in transport.commands
    assert "SENS:VOLT:DC:FILT:DIG?" in transport.commands
    assert "SENS:VOLT:DC:NPLC 10" in transport.commands
    instrument.close()


@pytest.mark.parametrize("firmware", ["02/14/08-08:54", "1.0"])
def test_unverified_outguard_can_apply_live_slow_nplc_after_filter_readback(firmware):
    transport = FakeTransport(idn=f"FLUKE,8845A,TEST8845A001,{firmware}")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV, nplc=1))
    transport.commands.clear()

    instrument.set_measurement_speed(ModeSettings(Mode.DCV, nplc=10))

    assert transport.commands.index("SENS:VOLT:DC:FILT:DIG OFF") < transport.commands.index(
        "SENS:VOLT:DC:FILT:DIG?"
    ) < transport.commands.index("SENS:VOLT:DC:NPLC 10")
    instrument.close()


@pytest.mark.parametrize(
    "settings",
    [
        ModeSettings(Mode.ACV, digital_filter=True),
        ModeSettings(Mode.ACI, digital_filter=True),
        ModeSettings(Mode.DCV, nplc=1, digital_filter=True),
    ],
)
def test_digital_filter_rejects_inapplicable_modes_and_fast_nplc(settings):
    with pytest.raises(ValueError, match="数字滤波"):
        settings.validate()


@pytest.mark.parametrize("nplc", [10, 100])
def test_mode_settings_accepts_digital_filter_request_at_slow_nplc(nplc):
    settings = ModeSettings(Mode.DCV, nplc=nplc, digital_filter=True)

    assert settings.validate() == descriptor(Mode.DCV)


def test_live_measurement_speed_can_enable_and_disable_digital_filter():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("2.10.882.24", "")
    instrument.configure(ModeSettings(Mode.DCV, nplc=1))
    transport.commands.clear()

    enabled = instrument.set_measurement_speed(
        ModeSettings(Mode.DCV, nplc=10, digital_filter=True)
    )

    off = "SENS:VOLT:DC:FILT:DIG OFF"
    readback = "SENS:VOLT:DC:FILT:DIG?"
    nplc = "SENS:VOLT:DC:NPLC 10"
    on = "SENS:VOLT:DC:FILT:DIG ON"
    assert transport.commands.index(off) < transport.commands.index(readback) < transport.commands.index(nplc)
    assert transport.commands.index(nplc) < transport.commands.index(on)
    assert enabled["digital_filter_response"] == "1"
    assert instrument._active_settings.digital_filter is True

    transport.commands.clear()
    disabled = instrument.set_measurement_speed(ModeSettings(Mode.DCV, nplc=1))
    assert transport.commands.index(off) < transport.commands.index(readback) < transport.commands.index(
        "SENS:VOLT:DC:NPLC 1"
    )
    assert disabled["digital_filter_response"] == "0"
    assert instrument._active_settings.digital_filter is False
    instrument.close()


def test_read_settings_reports_the_instrument_setup_without_reconfiguring():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(
        Mode.RES2, autorange=False, range_si=100, nplc=0.2,
        analog_filter=True, autozero=False,
    ))
    command_count = len(transport.commands)

    settings = instrument.read_settings(Mode.RES2)

    assert settings == ModeSettings(
        Mode.RES2, autorange=False, range_si=100, nplc=0.2,
        analog_filter=True, autozero=False, range_readback_si=100,
    )
    assert len(transport.commands) > command_count
    assert not any(command.startswith("CONF:") for command in transport.commands[command_count:])
    assert not any(":RANG " in command for command in transport.commands[command_count:])
    instrument.close()


def test_read_settings_rejects_fractional_ac_bandwidth_readback():
    transport = FakeTransport()
    transport.function = "VOLT:AC"
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    transport.bandwidth_response_override = "20.5"

    with pytest.raises(ScpiProtocolError, match="交流带宽回读不是整数"):
        instrument.read_settings(Mode.ACV)

    instrument.close()


@pytest.mark.parametrize(
    ("response", "expected"),
    [("3", 3), ("20", 20), ("200", 200), ("20.0", 20), ("2e1", 20)],
)
def test_read_settings_accepts_integer_ac_bandwidth_readback(response, expected):
    transport = FakeTransport()
    transport.function = "VOLT:AC"
    transport.bandwidth_response_override = response
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    settings = instrument.read_settings(Mode.ACV)

    assert settings.ac_bandwidth_hz == expected
    instrument.close()


def test_read_settings_rejects_tiny_fractional_ac_bandwidth_readback():
    transport = FakeTransport()
    transport.function = "VOLT:AC"
    transport.bandwidth_response_override = "20.00000000000000001"
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    with pytest.raises(ScpiProtocolError, match="交流带宽回读不是整数"):
        instrument.read_settings(Mode.ACV)

    instrument.close()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ("20_0", "交流带宽回读不是有效SCPI数值"),
        ("nan", "交流带宽回读不是有限数值"),
        ("inf", "交流带宽回读不是有限数值"),
        ("not-a-number", "交流带宽回读不是有效数值"),
    ],
)
def test_read_settings_rejects_invalid_ac_bandwidth_response(response, error):
    transport = FakeTransport()
    transport.function = "VOLT:AC"
    transport.bandwidth_response_override = response
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    with pytest.raises(ScpiProtocolError, match=error):
        instrument.read_settings(Mode.ACV)

    instrument.close()


def test_read_settings_reports_unsupported_ac_bandwidth_as_protocol_error():
    transport = FakeTransport()
    transport.function = "VOLT:AC"
    transport.bandwidth_response_override = "21"
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    with pytest.raises(ScpiProtocolError, match="回读值不在8845A支持列表中"):
        instrument.read_settings(Mode.ACV)

    instrument.close()


@pytest.mark.parametrize("nplc", [10, 100])
def test_read_settings_preserves_enabled_digital_filter_at_slow_nplc(nplc):
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("2.0", "")
    transport.nplc = str(nplc)
    transport.digital_filters["SENS:VOLT:DC"] = "1"

    settings = instrument.read_settings(Mode.DCV)

    assert settings.nplc == nplc
    assert settings.digital_filter is True
    instrument.close()


def test_read_settings_refuses_a_mode_that_is_not_active():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))

    with pytest.raises(InstrumentError, match="拒绝读取其他模式"):
        instrument.read_settings(Mode.RES2)

    instrument.close()


def test_read_settings_preserves_autorange_and_current_range_readback_separately():
    transport = FakeTransport()
    transport.range_value = 10
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    settings = instrument.read_settings(Mode.DCV)

    assert settings.autorange is True
    assert settings.range_si is None
    assert settings.range_readback_si == 10
    assert "SENS:VOLT:DC:RANG?" in transport.commands
    instrument.close()


def test_read_settings_rejects_current_range_outside_8845a_supported_values():
    transport = FakeTransport()
    transport.range_value = 2
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    with pytest.raises(ScpiProtocolError, match="量程回读不在8845A"):
        instrument.read_settings(Mode.DCV)

    instrument.close()


def test_zero_once_does_not_change_autozero_switch_state_in_transport_model():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    initial_autozero = transport.autozero

    instrument.zero()

    assert "ZERO:AUTO ONCE" in transport.commands
    assert transport.autozero == initial_autozero
    instrument.close()


def test_acv_does_not_accept_nplc_and_uses_documented_detector_bandwidth():
    with pytest.raises(ValueError, match="NPLC"):
        ModeSettings(Mode.ACV, nplc=1, ac_bandwidth_hz=20).validate()
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.ACV, nplc=None, ac_bandwidth_hz=3))
    assert "SENS:DET:BAND 3" in transport.commands
    assert not any(command.endswith(":NPLC 1") for command in transport.commands)
    assert instrument.read().value_si == pytest.approx(1.2345678901234567)
    instrument.close()


def test_scpi_error_queue_stops_invalid_configuration():
    transport = FakeTransport()
    transport.inject_config_error = True
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    with pytest.raises(ScpiProtocolError, match="CONF配置后"):
        instrument.configure(ModeSettings(Mode.DCV))
    assert "SENS:VOLT:DC:FILT:DIG OFF" not in transport.commands
    instrument.close()


def test_config_function_readback_is_verified():
    transport = FakeTransport()
    transport.force_function = "CURR:DC"
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    with pytest.raises(ScpiProtocolError, match="回读不符"):
        instrument.configure(ModeSettings(Mode.DCV))
    instrument.close()


def test_config_accepts_the_8845a_short_dc_function_readback():
    class ShortFunctionTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command.upper() == "SENS:FUNC?":
                return b'"VOLT"' + terminator
            return super().read_until(terminator, timeout_s)

    transport = ShortFunctionTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    result = instrument.configure(ModeSettings(Mode.DCV))
    assert result["function"] == '"VOLT"'
    assert instrument.read().value_si == pytest.approx(1.2345678901234567)
    instrument.close()


@pytest.mark.parametrize(
    ("settings", "query", "wrong_response", "error_match"),
    [
        (
            ModeSettings(Mode.DCV),
            "SENS:VOLT:DC:RANG:AUTO?",
            "0",
            "自动量程回读不符",
        ),
        (
            ModeSettings(Mode.DCV, autorange=False, range_si=10),
            "SENS:VOLT:DC:RANG?",
            "100",
            "固定量程回读不符",
        ),
        (
            ModeSettings(Mode.DCV, nplc=0.2),
            "SENS:VOLT:DC:NPLC?",
            "1",
            "NPLC回读不符",
        ),
        (
            ModeSettings(Mode.ACV, ac_bandwidth_hz=3),
            "SENS:DET:BAND?",
            "20",
            "交流带宽回读不符",
        ),
        (
            ModeSettings(Mode.DCV, analog_filter=True),
            "SENS:VOLT:DC:FILT:STAT?",
            "0",
            "模拟滤波回读不符",
        ),
        (
            ModeSettings(Mode.DCV),
            "SENS:VOLT:DC:FILT:DIG?",
            "1",
            "数字滤波回读不符",
        ),
        (
            ModeSettings(Mode.DCV, autozero=False),
            "ZERO:AUTO?",
            "1",
            "自动零点回读不符",
        ),
    ],
)
def test_configuration_rejects_every_mismatched_parameter_readback(
    settings, query, wrong_response, error_match
):
    class MismatchedReadbackTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command.upper() == query:
                return wrong_response.encode("ascii") + terminator
            return super().read_until(terminator, timeout_s)

    transport = MismatchedReadbackTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    with pytest.raises(ScpiProtocolError, match=error_match):
        instrument.configure(settings)
    assert instrument._mode is None
    instrument.close()


@pytest.mark.parametrize(
    ("query", "response", "settings", "error_match"),
    [
        ("SENS:VOLT:DC:RANG?", "not-a-number", ModeSettings(Mode.DCV), "量程回读不是有效数值"),
        ("SENS:VOLT:DC:RANG:AUTO?", "maybe", ModeSettings(Mode.DCV), "自动量程回读不是有效数值"),
        ("SENS:VOLT:DC:NPLC?", "nan", ModeSettings(Mode.DCV), "NPLC回读不是有限数值"),
    ],
)
def test_configuration_rejects_malformed_or_nonfinite_readbacks(
    query, response, settings, error_match
):
    class MalformedReadbackTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command.upper() == query:
                return response.encode("ascii") + terminator
            return super().read_until(terminator, timeout_s)

    transport = MalformedReadbackTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    with pytest.raises(ScpiProtocolError, match=error_match):
        instrument.configure(settings)
    assert instrument._mode is None
    instrument.close()


@pytest.mark.parametrize("mode", list(Mode))
def test_each_supported_mode_uses_only_its_applicable_scpi_settings(mode):
    from fluke8845a.modes import descriptor

    spec = descriptor(mode)
    settings = ModeSettings(mode)
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()

    instrument.configure(settings)

    assert f"CONF:{spec.function}" in transport.commands
    assert f"{spec.sense_prefix}:RANG:AUTO ON" in transport.commands
    if spec.nplc_values:
        assert f"{spec.sense_prefix}:NPLC {settings.nplc:g}" in transport.commands
    else:
        assert not any(":NPLC " in command for command in transport.commands)
    if spec.ac_bandwidths_hz:
        assert f"SENS:DET:BAND {settings.ac_bandwidth_hz}" in transport.commands
    else:
        assert not any(command.startswith("SENS:DET:BAND ") for command in transport.commands)
    if spec.analog_filter:
        assert f"{spec.sense_prefix}:FILT:STAT OFF" in transport.commands
    else:
        assert not any(":FILT:STAT " in command for command in transport.commands)
    if spec.digital_filter:
        assert f"{spec.sense_prefix}:FILT:DIG OFF" in transport.commands
        assert f"{spec.sense_prefix}:FILT:DIG?" in transport.commands
    else:
        assert not any(":FILT:DIG " in command or ":FILT:DIG?" in command for command in transport.commands)
    instrument.close()


def test_live_speed_update_changes_only_active_nplc_and_reads_back_confirmation():
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    original = ModeSettings(Mode.RES2, autorange=False, range_si=100e6, nplc=1)
    instrument.configure(original)
    transport.commands.clear()

    actual = instrument.set_measurement_speed(
        ModeSettings(Mode.RES2, autorange=False, range_si=100e6, nplc=0.2)
    )

    assert actual["nplc_response"] == "0.2"
    assert actual["ac_bandwidth_response"] is None
    assert "SENS:RES:NPLC 0.2" in transport.commands
    assert "SENS:RES:NPLC?" in transport.commands
    assert not any(command.startswith("CONF:") for command in transport.commands)
    assert not any(":RANG" in command or command.startswith("ZERO:AUTO") for command in transport.commands)
    instrument.close()


def test_live_speed_update_rejects_readback_mismatch():
    class WrongNplcTransport(FakeTransport):
        def read_until(self, terminator: bytes, timeout_s: float | None = None) -> bytes:
            if self.last_command.upper().endswith(":NPLC?"):
                return b"1" + terminator
            return super().read_until(terminator, timeout_s)

    transport = WrongNplcTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))

    with pytest.raises(ScpiProtocolError, match="调速参数回读不符"):
        instrument.set_measurement_speed(ModeSettings(Mode.DCV, nplc=0.2))
    instrument.close()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ("20.5", "交流带宽回读不是整数"),
        ("200", "调速参数回读不符"),
        ("nan", "交流带宽回读不是有限数值"),
        ("inf", "交流带宽回读不是有限数值"),
        ("not-a-number", "交流带宽回读不是有效数值"),
    ],
)
def test_live_ac_bandwidth_update_rejects_invalid_or_nonmatching_readback(response, error):
    transport = FakeTransport()
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.ACV))
    transport.bandwidth_response_override = response

    with pytest.raises(ScpiProtocolError, match=error):
        instrument.set_measurement_speed(ModeSettings(Mode.ACV, ac_bandwidth_hz=20))
    instrument.close()


def test_400ma_dci_firmware_gate_is_conservative():
    transport = FakeTransport("FLUKE,8845A,123,1.0.700.18")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("1.0.700.18", "")
    with pytest.raises(UnsupportedRange, match="400 mA"):
        instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert not any(command.endswith("RANG 0.4") for command in transport.commands)
    instrument.close()


@pytest.mark.parametrize("outguard_version", ["", "1.0.700.18"])
def test_400ma_aci_allows_unknown_or_supported_outguard_version(outguard_version):
    transport = FakeTransport("FLUKE,8845A,123,1.0.700.18")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions(outguard_version, "")
    instrument.configure(ModeSettings(Mode.ACI, autorange=False, range_si=0.4, nplc=None, ac_bandwidth_hz=20))
    assert "SENS:CURR:AC:RANG 0.4" in transport.commands
    instrument.close()


def test_400ma_aci_is_not_gated_by_outguard_when_spec_field_is_ambiguous():
    transport = FakeTransport("FLUKE,8845A,123,1.0.700.17", terminals="FRONT")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("1.0.700.17", "ad300210")

    instrument.configure(ModeSettings(Mode.ACI, autorange=False, range_si=0.4, ac_bandwidth_hz=20))

    assert "SENS:CURR:AC:RANG 0.4" in transport.commands
    instrument.close()


def test_aci_autorange_is_not_gated_by_unmapped_outguard_version():
    transport = FakeTransport("FLUKE,8845A,123,1.0.700.17", terminals="FRONT")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("1.0.700.17", "ad300210")

    instrument.configure(ModeSettings(Mode.ACI, autorange=True, ac_bandwidth_hz=20))

    assert "SENS:CURR:AC:RANG:AUTO ON" in transport.commands
    instrument.close()


def test_firmware_compare_returns_unknown_instead_of_guessing():
    assert compare_firmware("version unknown", (2, 0)) is None
    assert compare_firmware("1.0.0", (2, 0)) is None
    assert compare_firmware("1.0.700.18", (1, 0, 700, 18)) is True
    assert compare_firmware("1.0.700.18", (2, 0)) is False


@pytest.mark.parametrize(
    ("mode", "firmware"),
    [
        (Mode.DCV, ""),
        (Mode.DCI, ""),
        (Mode.RES2, "1.0.700.18"),
        (Mode.RES4, "2.10.882.24"),
        (Mode.ACV, ""),
    ],
)
def test_digital_filter_firmware_support_uses_documented_outguard_cutoff(mode, firmware):
    from fluke8845a.filter_policy import digital_filter_firmware_support

    if mode in (Mode.ACV, Mode.ACI):
        assert digital_filter_firmware_support(mode, firmware) is False
    elif firmware in ("", "02/14/08-08:54"):
        assert digital_filter_firmware_support(mode, firmware) is None
    elif firmware == "1.0.700.18":
        assert digital_filter_firmware_support(mode, firmware) is False
    else:
        assert digital_filter_firmware_support(mode, firmware) is True


@pytest.mark.parametrize(
    ("outguard_version", "expected"),
    [
        ("1.0", ("1.0", "面板核对的 OutG SW")),
        ("", ("", "未知")),
        ("2.10.882.24", ("2.10.882.24", "面板核对的 OutG SW")),
        ("ad300210", ("", "未知")),
    ],
)
def test_range_firmware_selection_uses_only_panel_verified_outguard(outguard_version, expected):
    assert select_range_firmware(outguard_version) == expected


def test_date_coded_idn_firmware_does_not_block_400ma_range():
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    info = instrument.connect()
    assert info.idn_version_candidate == ""
    assert info.idn_revision == "02/14/08-08:54"
    instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert "SENS:CURR:DC:RANG 0.4" in transport.commands
    instrument.close()


@pytest.mark.parametrize("idn_candidate", ["1.0.700.17", "2.10.882.24"])
def test_parseable_idn_candidate_does_not_satisfy_or_block_400ma_gate(idn_candidate):
    transport = FakeTransport(f"FLUKE,8845A,TEST8845A001,{idn_candidate}", terminals="FRONT")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    info = instrument.connect()

    assert info.idn_version_candidate == idn_candidate
    assert instrument.range_firmware == ("", "未知")
    instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert "SENS:CURR:DC:RANG 0.4" in transport.commands
    instrument.close()


def test_verified_front_panel_outguard_firmware_overrides_idn_version_candidate():
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,1.0.0.0", terminals="FRONT")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    info = instrument.connect()
    assert info.idn_version_candidate == "1.0.0.0"

    instrument.set_firmware_versions("2.10.882.24", "ad300210")
    assert instrument.range_firmware == ("2.10.882.24", "面板核对的 OutG SW")
    instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert "SENS:CURR:DC:RANG 0.4" in transport.commands
    instrument.close()


def test_known_old_outguard_blocks_only_400ma_and_inguard_never_satisfies_gate():
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,2.10.882.24", terminals="FRONT")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions("1.9.0.0", "ad300210")

    for range_si in (0.01, 0.1, 10.0):
        instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=range_si))
    with pytest.raises(UnsupportedRange, match="400 mA"):
        instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert "SENS:CURR:DC:RANG 0.01" in transport.commands
    assert "SENS:CURR:DC:RANG 0.1" in transport.commands
    assert "SENS:CURR:DC:RANG 10" in transport.commands
    assert not any(command.endswith("RANG 0.4") for command in transport.commands)
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "outguard_version", "range_si"),
    [
        (Mode.DCI, "1.0.700.18", 0.01),
        (Mode.DCI, "1.0.700.18", 0.1),
        (Mode.DCI, "1.0.700.18", 1.0),
        (Mode.DCI, "1.0.700.18", 3.0),
        (Mode.DCI, "1.0.700.18", 10.0),
        (Mode.ACI, "1.0.700.17", 0.01),
        (Mode.ACI, "1.0.700.17", 0.1),
        (Mode.ACI, "1.0.700.17", 1.0),
        (Mode.ACI, "1.0.700.17", 3.0),
        (Mode.ACI, "1.0.700.17", 10.0),
    ],
)
def test_old_outguard_only_blocks_fixed_400ma_range(mode, outguard_version, range_si):
    transport = FakeTransport("FLUKE,8845A,123,2.10.882.24", terminals="FRONT")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions(outguard_version, "")

    instrument.configure(ModeSettings(mode, autorange=False, range_si=range_si))

    function = "CURR:DC" if mode == Mode.DCI else "CURR:AC"
    assert f"SENS:{function}:RANG {range_si:g}" in transport.commands
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "outguard_version", "terminals"),
    [
        (Mode.DCI, "1.0", "FRONT"),
        (Mode.DCI, "2.0", "REAR"),
        (Mode.ACI, "1.0.700.18", "REAR"),
    ],
)
def test_current_autorange_is_rejected_when_any_auto_range_limit_is_unverified(mode, outguard_version, terminals):
    transport = FakeTransport("FLUKE,8845A,123,2.10.882.24", terminals=terminals)
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions(outguard_version, "")
    before = len(transport.commands)

    with pytest.raises(UnsupportedRange, match="自动量程"):
        instrument.configure(ModeSettings(mode, autorange=True))

    assert not any(command.startswith("CONF:") for command in transport.commands[before:])
    assert not any(":RANG:AUTO ON" in command for command in transport.commands[before:])
    instrument.close()


def test_current_autorange_allows_date_coded_idn_firmware_on_front_terminals():
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54", terminals="FRONT")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCI, autorange=True))
    assert "SENS:CURR:DC:RANG:AUTO ON" in transport.commands
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "outguard_version", "terminals", "range_si"),
    [
        (Mode.DCI, "1.0.700.18", "FRONT", 0.4),
        (Mode.DCI, "2.0", "REAR", 1.0),
        (Mode.ACI, "1.0.700.18", "REAR", 10.0),
    ],
)
def test_current_fixed_range_limits_are_rejected_before_mode_configuration(mode, outguard_version, terminals, range_si):
    transport = FakeTransport("FLUKE,8845A,123,2.10.882.24", terminals=terminals)
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions(outguard_version, "")
    before = len(transport.commands)

    with pytest.raises(UnsupportedRange) as exc_info:
        instrument.configure(ModeSettings(mode, autorange=False, range_si=range_si))

    assert not any(command.startswith("CONF:") for command in transport.commands[before:])
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "outguard_version"),
    [(Mode.DCI, "2.0"), (Mode.ACI, "1.0.700.17")],
)
def test_current_autorange_is_allowed_on_front_with_mode_specific_firmware_evidence(mode, outguard_version):
    transport = FakeTransport("FLUKE,8845A,123,1.0", terminals="FRONT")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.set_firmware_versions(outguard_version, "")
    instrument.configure(ModeSettings(mode, autorange=True))

    assert f"CONF:{'CURR:DC' if mode == Mode.DCI else 'CURR:AC'}" in transport.commands
    assert "SENS:CURR:DC:RANG:AUTO ON" in transport.commands if mode == Mode.DCI else "SENS:CURR:AC:RANG:AUTO ON" in transport.commands
    instrument.close()


def _exercise_ac_bandwidth_readback(path, response, expected_bandwidth=20):
    transport = FakeTransport()
    if path == "read_settings":
        transport.function = "VOLT:AC"
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    try:
        if path == "read_settings":
            transport.bandwidth_response_override = response
            return instrument.read_settings(Mode.ACV).ac_bandwidth_hz
        if path == "configure":
            transport.bandwidth_response_override = response
            result = instrument.configure(ModeSettings(Mode.ACV, ac_bandwidth_hz=expected_bandwidth))
            return result["ac_bandwidth_response"]
        if path == "live_speed":
            instrument.configure(ModeSettings(Mode.ACV))
            transport.bandwidth_response_override = response
            result = instrument.set_measurement_speed(
                ModeSettings(Mode.ACV, ac_bandwidth_hz=expected_bandwidth)
            )
            return result["ac_bandwidth_response"]
        raise AssertionError(f"unknown AC bandwidth readback path: {path}")
    finally:
        instrument.close()


@pytest.mark.parametrize("path", ["read_settings", "configure", "live_speed"])
@pytest.mark.parametrize(
    ("response", "expected"),
    [("3", 3), ("20", 20), ("200", 200), ("20.0", 20), ("2e1", 20)],
)
def test_all_ac_bandwidth_readback_paths_accept_exact_supported_integers(path, response, expected):
    assert _exercise_ac_bandwidth_readback(path, response, expected) == (
        expected if path == "read_settings" else response
    )


@pytest.mark.parametrize("path", ["read_settings", "configure", "live_speed"])
@pytest.mark.parametrize(
    ("response", "error"),
    [
        ("20.00000000000000001", "交流带宽回读不是整数"),
        ("20.5", "交流带宽回读不是整数"),
        ("nan", "交流带宽回读不是有限数值"),
        ("inf", "交流带宽回读不是有限数值"),
        ("not-a-number", "交流带宽回读不是有效数值"),
        ("2_0", "交流带宽回读不是有效SCPI数值"),
        ("21", "交流带宽回读值不在8845A支持列表中"),
    ],
)
def test_all_ac_bandwidth_readback_paths_apply_the_same_strict_integer_validation(path, response, error):
    with pytest.raises(ScpiProtocolError, match=error):
        _exercise_ac_bandwidth_readback(path, response)

