from __future__ import annotations

import pytest

from fluke8845a.modes import Mode, mode_from_scpi_function, range_label
from fluke8845a.scpi import (
    FlowControl, InstrumentError, InstrumentIdentityError, InstrumentLocalModeError, ModeSettings, PySerialTransport, ReadingError, Rs232TerminalModeError,
    ScpiClient, ScpiProtocolError, ScpiTimeout, Serial8845A, SerialSettings,
    UnsupportedRange, compare_firmware, measurement_read_timeout_s, parse_idn, parse_reading,
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


def test_idn_parser_keeps_firmware_and_model():
    info = parse_idn('FLUKE,8845A,"SN,42",2.0')
    assert info.model == "8845A"
    assert info.serial_number == "SN,42"
    assert info.firmware == "2.0"


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


def test_non_8845a_is_rejected_before_remote_mode():
    transport = FakeTransport("FLUKE,8846A,123,2.0")
    instrument = Serial8845A("COM4", SerialSettings(), transport_factory=lambda *_: transport)
    with pytest.raises(InstrumentIdentityError):
        instrument.connect()
    assert "SYST:REM" not in transport.commands
    assert not transport.is_open


def test_serial_timeout_is_reported_and_closes_failed_connection():
    transport = FakeTransport()
    transport.timeouts.add("*IDN?")
    client = ScpiClient(transport, SerialSettings())
    client.open()
    with pytest.raises(ScpiTimeout):
        client.query("*IDN?")
    assert transport.is_open
    client.close()
    assert not transport.is_open


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

    with pytest.raises(OSError, match="simulated serial read failure"):
        instrument.read()
    assert not instrument.connected
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
    instrument.configure(ModeSettings(Mode.DCV, autorange=False, range_si=10, nplc=0.2, analog_filter=True))
    assert "CONF:VOLT:DC" in transport.commands
    assert "SENS:VOLT:DC:RANG:AUTO OFF" in transport.commands
    assert "SENS:VOLT:DC:RANG 10" in transport.commands
    assert "SENS:VOLT:DC:NPLC 0.2" in transport.commands
    assert "SENS:VOLT:DC:FILT:STAT ON" in transport.commands
    assert "ZERO:AUTO ON" in transport.commands
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
        analog_filter=True, autozero=False,
    )
    assert len(transport.commands) > command_count
    assert not any(command.startswith("CONF:") for command in transport.commands[command_count:])
    assert not any(":RANG " in command for command in transport.commands[command_count:])
    instrument.close()


def test_read_settings_refuses_a_mode_that_is_not_active():
    transport = FakeTransport()
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCV))

    with pytest.raises(InstrumentError, match="拒绝读取其他模式"):
        instrument.read_settings(Mode.RES2)

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
    with pytest.raises(ScpiProtocolError, match="配置后"):
        instrument.configure(ModeSettings(Mode.DCV))
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
        ("20.5", "调速参数回读不符"),
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
    with pytest.raises(UnsupportedRange, match="400 mA"):
        instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert not any(command.endswith("RANG 0.4") for command in transport.commands)
    instrument.close()


def test_400ma_aci_firmware_gate_and_supported_case():
    transport = FakeTransport("FLUKE,8845A,123,1.0.700.18")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.ACI, autorange=False, range_si=0.4, nplc=None, ac_bandwidth_hz=20))
    assert "SENS:CURR:AC:RANG 0.4" in transport.commands
    instrument.close()


def test_firmware_compare_returns_unknown_instead_of_guessing():
    assert compare_firmware("version unknown", (2, 0)) is None
    assert compare_firmware("1.0.0", (2, 0)) is None
    assert compare_firmware("1.0.700.18", (1, 0, 700, 18)) is True
    assert compare_firmware("1.0.700.18", (2, 0)) is False


def test_date_coded_idn_firmware_does_not_block_400ma_range():
    transport = FakeTransport("FLUKE,8845A,1234567,02/14/08-08:54")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCI, autorange=False, range_si=0.4))
    assert "SENS:CURR:DC:RANG 0.4" in transport.commands
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "firmware", "terminals"),
    [
        (Mode.DCI, "1.0", "FRONT"),
        (Mode.ACI, "1.0.700.17", "FRONT"),
        (Mode.DCI, "2.0", "REAR"),
        (Mode.ACI, "1.0.700.18", "REAR"),
    ],
)
def test_current_autorange_is_rejected_when_any_auto_range_limit_is_unverified(mode, firmware, terminals):
    transport = FakeTransport(f"FLUKE,8845A,123,{firmware}", terminals=terminals)
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    before = len(transport.commands)

    with pytest.raises(UnsupportedRange, match="自动量程"):
        instrument.configure(ModeSettings(mode, autorange=True))

    assert not any(command.startswith("CONF:") for command in transport.commands[before:])
    assert not any(":RANG:AUTO ON" in command for command in transport.commands[before:])
    instrument.close()


def test_current_autorange_allows_date_coded_idn_firmware_on_front_terminals():
    transport = FakeTransport("FLUKE,8845A,1234567,02/14/08-08:54", terminals="FRONT")
    instrument = Serial8845A("COM3", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(Mode.DCI, autorange=True))
    assert "SENS:CURR:DC:RANG:AUTO ON" in transport.commands
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "firmware", "terminals", "range_si"),
    [
        (Mode.DCI, "1.0.700.18", "FRONT", 0.4),
        (Mode.ACI, "1.0.700.17", "FRONT", 0.4),
        (Mode.DCI, "2.0", "REAR", 1.0),
        (Mode.ACI, "1.0.700.18", "REAR", 10.0),
    ],
)
def test_current_fixed_range_limits_are_rejected_before_mode_configuration(mode, firmware, terminals, range_si):
    transport = FakeTransport(f"FLUKE,8845A,123,{firmware}", terminals=terminals)
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    before = len(transport.commands)

    with pytest.raises(UnsupportedRange):
        instrument.configure(ModeSettings(mode, autorange=False, range_si=range_si))

    assert not any(command.startswith("CONF:") for command in transport.commands[before:])
    instrument.close()


@pytest.mark.parametrize(
    ("mode", "firmware"),
    [(Mode.DCI, "2.0"), (Mode.ACI, "1.0.700.18")],
)
def test_current_autorange_is_allowed_when_firmware_and_front_terminals_are_confirmed(mode, firmware):
    transport = FakeTransport(f"FLUKE,8845A,123,{firmware}", terminals="FRONT")
    instrument = Serial8845A("COM7", SerialSettings(), transport_factory=lambda *_: transport)
    instrument.connect()
    instrument.configure(ModeSettings(mode, autorange=True))

    assert f"CONF:{'CURR:DC' if mode == Mode.DCI else 'CURR:AC'}" in transport.commands
    assert "SENS:CURR:DC:RANG:AUTO ON" in transport.commands if mode == Mode.DCI else "SENS:CURR:AC:RANG:AUTO ON" in transport.commands
    instrument.close()

