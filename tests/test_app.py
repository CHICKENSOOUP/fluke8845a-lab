from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fluke8845a import app as app_module
from fluke8845a.modes import Mode
from fluke8845a.scpi import InstrumentInfo, ModeSettings, Reading


class Fake8845A:
    def __init__(self, port, settings, log_callback, *, report_path, function='"RES"', terminals='FRON'):
        self.port = port
        self.settings = settings
        self.log_callback = log_callback
        self.report_path = report_path
        self.function = function
        self.terminals = terminals
        self.connected = False
        self.closed = False
        self.configured = []
        self.active_settings = None

    def connect(self):
        assert json.loads(self.report_path.read_text(encoding="utf-8"))["status"] == "starting"
        self.connected = True
        self.log_callback("> SYST:REM")
        return InstrumentInfo("FLUKE", "8845A", "1234567", "2.0", "FLUKE,8845A,1234567,2.0")

    def read_mode_state(self):
        return {"function": self.function, "input_terminals": self.terminals}

    def configure(self, settings):
        self.configured.append(settings)
        self.active_settings = settings
        assert settings.mode == Mode.RES2
        assert settings.nplc in (0.02, 1)
        if settings.autorange:
            assert settings.range_si is None
            range_value = 100
        else:
            assert settings.range_si in (100, 100e6)
            range_value = settings.range_si
        self.log_callback("> CONF:RES")
        return {
            "function": '"RES"',
            "range_response": f"{range_value:.8E}",
            "autorange_response": "1" if settings.autorange else "0",
            "nplc_response": f"{settings.nplc:.8E}",
            "ac_bandwidth_response": None,
            "analog_filter_response": "1" if settings.analog_filter else "0",
            "autozero_response": "1" if settings.autozero else "0",
            "input_terminals": "FRON",
        }

    def read_settings(self, mode):
        assert mode == Mode.RES2
        return ModeSettings(
            Mode.RES2, autorange=True, range_si=None, nplc=0.02,
            analog_filter=False, autozero=False,
        )

    def read(self):
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat(timespec="microseconds")
        is_short_probe = self.active_settings is not None and self.active_settings.range_si == 100
        return Reading(
            raw="+1.55000000E-01" if is_short_probe else "+2.99778000E+07",
            value_si=0.155 if is_short_probe else 29_977_800.0,
            range_si=100.0 if is_short_probe else 100e6,
            started_monotonic_ns=1_000_000_000, received_monotonic_ns=1_200_000_000,
            timestamp_utc=now_iso, timestamp_local=now.astimezone().isoformat(timespec="microseconds"),
        )

    def close(self):
        self.log_callback("> SYST:LOC")
        self.connected = False
        self.closed = True


@pytest.mark.parametrize(
    ("function", "terminals"),
    [("\"RES\"", "FRON"), ("\"RES\"", "FRONT")],
)
def test_real_probe_writes_starting_report_then_performs_guarded_read_and_restores_local(
    monkeypatch, tmp_path, function, terminals
):
    report_path = tmp_path / "reports" / "probe.json"
    instances = []

    def factory(port, settings, log_callback):
        instance = Fake8845A(
            port, settings, log_callback, report_path=report_path,
            function=function, terminals=terminals,
        )
        instances.append(instance)
        return instance

    monkeypatch.setattr(app_module, "Serial8845A", factory)

    assert app_module.run_real_hardware_probe("COM3", report_path, wiring_confirmed=True) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["simulation"] is False
    assert report["sample"]["raw_reading"] == "+2.99778000E+07"
    assert report["requested_configuration"]["mode"] == "RES2"
    assert float(report["applied_configuration"]["nplc_response"]) == 1.0
    assert "> SYST:LOC" in report["communication_log"]
    assert instances[0].closed


def test_short_lead_probe_uses_100_ohm_range_and_restores_previous_setup(monkeypatch, tmp_path):
    report_path = tmp_path / "short-probe.json"
    instances = []

    def factory(port, settings, log_callback):
        instance = Fake8845A(port, settings, log_callback, report_path=report_path)
        instances.append(instance)
        return instance

    monkeypatch.setattr(app_module, "Serial8845A", factory)

    assert app_module.run_real_hardware_probe(
        "COM3", report_path, wiring_confirmed=False,
        shorted_leads_confirmed=True, baudrate=115200,
    ) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["operator_wiring_confirmation"] == "front_panel_2w_short"
    assert report["wiring"] == "用户确认前面板2W输入端子短接。"
    assert report["baudrate"] == 115200
    assert report["pre_configuration_settings"]["autorange"] is True
    assert report["requested_configuration"]["range_si"] == 100
    assert report["sample"]["raw_reading"] == "+1.55000000E-01"
    assert report["sample"]["range_si"] == 100
    assert report["restored_configuration"]["autorange_response"] == "1"
    assert float(report["restored_configuration"]["nplc_response"]) == 0.02
    assert len(instances[0].configured) == 2
    assert instances[0].configured[1].autorange is True
    assert instances[0].closed


def test_real_probe_rejects_unconfirmed_wiring_without_opening_com_port(monkeypatch, tmp_path):
    report_path = tmp_path / "probe.json"

    def forbidden_factory(*_args):
        pytest.fail("probe must not connect without an explicit wiring confirmation")

    monkeypatch.setattr(app_module, "Serial8845A", forbidden_factory)

    assert app_module.run_real_hardware_probe("COM3", report_path, wiring_confirmed=False) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "rejected"
    assert report["communication_log"] == []


def test_real_probe_never_overwrites_an_existing_report(monkeypatch, tmp_path):
    report_path = tmp_path / "probe.json"
    original = "preserve previous diagnostic"
    report_path.write_text(original, encoding="utf-8")

    def forbidden_factory(*_args):
        pytest.fail("probe must refuse the existing report before opening the COM port")

    monkeypatch.setattr(app_module, "Serial8845A", forbidden_factory)

    assert app_module.run_real_hardware_probe("COM3", report_path, wiring_confirmed=True) == 1
    assert report_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(("function", "terminals"), [("\"VOLT:DC\"", "FRON"), ("\"RES\"", "REAR")])
def test_real_probe_refuses_to_reconfigure_wrong_function_or_terminal(monkeypatch, tmp_path, function, terminals):
    report_path = tmp_path / "probe.json"
    instances = []

    def factory(port, settings, log_callback):
        instance = Fake8845A(
            port, settings, log_callback, report_path=report_path,
            function=function, terminals=terminals,
        )
        instances.append(instance)
        return instance

    monkeypatch.setattr(app_module, "Serial8845A", factory)

    assert app_module.run_real_hardware_probe("COM3", report_path, wiring_confirmed=True) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert "拒绝" in report["error"]
    assert instances[0].configured == []
    assert instances[0].closed


def test_probe_cli_requires_report_and_explicit_wiring_confirmation(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        app_module.main(["--probe-real", "--port", "COM3", "--report", str(tmp_path / "probe.json")])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "--confirm-front-2w-30mohm" in error
    assert "--confirm-front-2w-short" in error


def test_short_wiring_cli_uses_explicit_confirmation_and_baudrate(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(port, report_path, *, wiring_confirmed, shorted_leads_confirmed, baudrate):
        captured.update(
            port=port,
            report_path=report_path,
            wiring_confirmed=wiring_confirmed,
            shorted_leads_confirmed=shorted_leads_confirmed,
            baudrate=baudrate,
        )
        return 0

    monkeypatch.setattr(app_module, "run_real_hardware_probe", fake_probe)
    result = app_module.main([
        "--probe-real", "--port", "COM3", "--baudrate", "115200",
        "--report", str(tmp_path / "short.json"), "--confirm-front-2w-short",
    ])

    assert result == 0
    assert captured == {
        "port": "COM3",
        "report_path": tmp_path / "short.json",
        "wiring_confirmed": False,
        "shorted_leads_confirmed": True,
        "baudrate": 115200,
    }


def test_gui_probe_cli_requires_explicit_wiring_confirmation(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        app_module.main([
            "--probe-real-gui", "--port", "COM3", "--report", str(tmp_path / "gui-probe.json")
        ])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "--confirm-front-2w-30mohm" in error
    assert "--confirm-front-2w-short" in error


def test_gui_probe_cli_caps_test_duration_at_one_minute_without_opening_hardware(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(port, report_path, *, wiring_confirmed, shorted_leads_confirmed, baudrate, duration_s, visible_window):
        captured.update(
            port=port,
            report_path=report_path,
            wiring_confirmed=wiring_confirmed,
            shorted_leads_confirmed=shorted_leads_confirmed,
            baudrate=baudrate,
            duration_s=duration_s,
            visible_window=visible_window,
        )
        return 17

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    result = app_module.main([
        "--probe-real-gui", "--port", "COM3", "--duration", "50",
        "--baudrate", "115200", "--report", str(tmp_path / "minute.json"), "--confirm-front-2w-30mohm",
    ])

    assert result == 17
    assert captured == {
        "port": "COM3",
        "report_path": tmp_path / "minute.json",
        "wiring_confirmed": True,
        "shorted_leads_confirmed": False,
        "baudrate": 115200,
        "duration_s": 50.0,
        "visible_window": False,
    }

    with pytest.raises(SystemExit) as exit_info:
        app_module.main([
            "--probe-real-gui", "--port", "COM3", "--duration", "50.01",
            "--report", str(tmp_path / "too-long.json"), "--confirm-front-2w-30mohm",
        ])
    assert exit_info.value.code == 2


def test_gui_probe_cli_routes_confirmed_short_and_baudrate(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(port, report_path, *, wiring_confirmed, shorted_leads_confirmed, baudrate, duration_s, visible_window):
        captured.update(
            port=port,
            report_path=report_path,
            wiring_confirmed=wiring_confirmed,
            shorted_leads_confirmed=shorted_leads_confirmed,
            baudrate=baudrate,
            duration_s=duration_s,
            visible_window=visible_window,
        )
        return 23

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    report = tmp_path / "short-gui.json"
    result = app_module.main([
        "--probe-real-gui", "--port", "COM3", "--baudrate", "115200",
        "--report", str(report), "--confirm-front-2w-short", "--duration", "8",
    ])

    assert result == 23
    assert captured == {
        "port": "COM3",
        "report_path": report,
        "wiring_confirmed": False,
        "shorted_leads_confirmed": True,
        "baudrate": 115200,
        "duration_s": 8.0,
        "visible_window": False,
    }


def test_visible_gui_probe_cli_routes_visible_window_flag(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(port, report_path, *, wiring_confirmed, shorted_leads_confirmed, baudrate, duration_s, visible_window):
        captured.update(
            port=port,
            report_path=report_path,
            wiring_confirmed=wiring_confirmed,
            shorted_leads_confirmed=shorted_leads_confirmed,
            baudrate=baudrate,
            duration_s=duration_s,
            visible_window=visible_window,
        )
        return 31

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    report = tmp_path / "visible-short.json"
    result = app_module.main([
        "--probe-real-gui-visible", "--port", "COM3", "--baudrate", "115200",
        "--report", str(report), "--confirm-front-2w-short", "--duration", "50",
    ])

    assert result == 31
    assert captured == {
        "port": "COM3",
        "report_path": report,
        "wiring_confirmed": False,
        "shorted_leads_confirmed": True,
        "baudrate": 115200,
        "duration_s": 50.0,
        "visible_window": True,
    }


@pytest.mark.parametrize(
    ("mode_flag", "confirmation", "expected_mode", "expected_confirmation"),
    [
        ("--probe-real-gui-dcv", "--confirm-front-voltage-source", Mode.DCV, "voltage_source_confirmed"),
        ("--probe-real-gui-dci", "--confirm-front-dci-5ma-series", Mode.DCI, "dci_current_source_confirmed"),
    ],
)
def test_gui_probe_cli_routes_legacy_voltage_and_current_modes(
    tmp_path, monkeypatch, mode_flag, confirmation, expected_mode, expected_confirmation
):
    captured = {}

    def fake_probe(port, report_path, **kwargs):
        captured.update(port=port, report_path=report_path, **kwargs)
        return 0

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    report = tmp_path / f"{expected_mode.value}.json"

    assert app_module.main([
        mode_flag, "--port", "COM3", "--baudrate", "115200", "--duration", "8",
        "--report", str(report), confirmation,
    ]) == 0

    assert captured["measurement_mode"] == expected_mode
    assert captured[expected_confirmation] is True
    assert captured["report_path"] == report
    assert captured["duration_s"] == 8.0
    assert not any("outguard" in key or "inguard" in key for key in captured)


def test_aci_gui_probe_cli_routes_explicit_source_and_supported_settings(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(port, report_path, **kwargs):
        captured.update(port=port, report_path=report_path, **kwargs)
        return 0

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    report = tmp_path / "aci.json"
    assert app_module.main([
        "--probe-real-gui-aci", "--report", str(report), "--duration", "8",
        "--confirm-front-aci-current-series", "--aci-current-arms", "0.005",
        "--aci-frequency-hz", "60", "--aci-range-a", "0.01", "--ac-bandwidth-hz", "20",
    ]) == 0

    assert captured["measurement_mode"] == Mode.ACI
    assert captured["aci_current_source_confirmed"] is True
    assert captured["aci_source_current_arms"] == 0.005
    assert captured["aci_source_frequency_hz"] == 60
    assert captured["aci_range_a"] == 0.01
    assert captured["ac_bandwidth_hz"] == 20


@pytest.mark.parametrize(
    "extra_args",
    [
        [],
        ["--confirm-front-aci-current-series"],
        [
            "--confirm-front-aci-current-series", "--aci-current-arms", "0.01",
            "--aci-frequency-hz", "60", "--aci-range-a", "0.01", "--ac-bandwidth-hz", "20",
        ],
    ],
)
def test_aci_gui_probe_rejects_missing_confirmation_or_invalid_source_before_connect(
    tmp_path, monkeypatch, extra_args
):
    def forbidden_probe(*_args, **_kwargs):
        pytest.fail("invalid ACI request must be rejected before opening the serial port")

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", forbidden_probe)
    args = ["--probe-real-gui-aci", "--report", str(tmp_path / "aci.json"), *extra_args]
    with pytest.raises(SystemExit) as exit_info:
        app_module.main(args)
    assert exit_info.value.code == 2


def test_real_dci_gui_probe_refuses_to_switch_a_different_live_instrument_mode(monkeypatch, tmp_path):
    class ReadOnlySnapshot:
        def __init__(self, *_args):
            self.closed = False

        def connect(self):
            return InstrumentInfo(
                "FLUKE", "8845A", "PRIVATE", "", "FLUKE,8845A,PRIVATE,02/14/08-08:54",
                idn_revision="02/14/08-08:54",
            )

        def read_mode_state(self):
            return {"function": '"VOLT:DC"', "input_terminals": '"FRON"'}

        def read_settings(self, _mode):
            pytest.fail("probe must refuse before reading or changing settings")

        def close(self):
            self.closed = True

    snapshots = []

    def factory(*args):
        snapshot = ReadOnlySnapshot(*args)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(app_module, "Serial8845A", factory)
    report_path = tmp_path / "wrong-live-mode.json"
    assert app_module.run_real_gui_speed_probe(
        "COM3", report_path, wiring_confirmed=False,
        dci_current_source_confirmed=True, measurement_mode=Mode.DCI,
    ) == 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert "拒绝切换仪表功能" in report["error"]
    assert snapshots[0].closed


@pytest.mark.parametrize(
    ("mode", "bandwidth", "expected"),
    [
        (Mode.DCV, None, (0.2, 1)),
        (Mode.DCI, None, (0.2, 1)),
        (Mode.RES2, None, (0.2, 1)),
        (Mode.ACI, 3, (200, 3)),
        (Mode.ACI, 200, (20, 200)),
    ],
)
def test_hardware_probe_speed_changes_use_documented_parameters(mode, bandwidth, expected):
    assert app_module._probe_speed_change_values(mode, bandwidth) == expected


def test_mock_e2e_cli_requires_report(capsys):
    with pytest.raises(SystemExit) as exit_info:
        app_module.main(["--mock-e2e"])

    assert exit_info.value.code == 2
    assert "--mock-e2e requires --report PATH" in capsys.readouterr().err


def test_mock_e2e_cli_routes_report_path(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(report_path):
        captured["report_path"] = report_path
        return 19

    report_path = tmp_path / "six-modes.json"
    monkeypatch.setattr(app_module, "run_mock_six_mode_gui_probe", fake_probe)
    result = app_module.main(["--mock-e2e", "--report", str(report_path)])

    assert result == 19
    assert captured["report_path"] == report_path


def test_serial_runtime_smoke_imports_pyserial_and_enumerates_ports_without_gui():
    assert app_module.main(["--serial-smoke"]) == 0


def test_gui_rate_reconstruction_uses_the_last_21_visible_valid_receipts():
    rows = [
        {"status": "ok", "value_si": "1", "elapsed_s": "0", "read_duration_s": "0.1"},
        {"status": "ok", "value_si": "1", "elapsed_s": "10", "read_duration_s": "0.1"},
    ]
    for index in range(2, 25):
        rows.append({
            "status": "ok",
            "value_si": "1",
            "elapsed_s": repr(10 + (index - 1) * 0.5),
            "read_duration_s": "0.1",
        })

    rate, window_count = app_module._reconstruct_gui_rate_from_rows(rows, visible_sample_count=25)

    assert window_count == 21
    assert rate == pytest.approx(2.0)
