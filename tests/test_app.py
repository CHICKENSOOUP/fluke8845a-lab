from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fluke8845a import app as app_module
from fluke8845a import probe_provenance as probe_provenance_module
from fluke8845a.modes import Mode
from fluke8845a.scpi import InstrumentInfo, ModeSettings, Reading


def _delay_fake_read_responses(monkeypatch, transport):
    read_until = transport.read_until

    def delayed_read_until(terminator, timeout_s=None):
        response = read_until(terminator, timeout_s)
        if transport.last_command.upper() == "READ?":
            time.sleep(0.025)
        return response

    monkeypatch.setattr(transport, "read_until", delayed_read_until)


def test_probe_restore_can_reuse_confirmation_only_for_same_mode_and_range():
    confirmed_probe_settings = ModeSettings(
        Mode.DCI, autorange=False, range_si=10e-3, nplc=1,
    )
    previous_same_wiring_target = ModeSettings(
        Mode.DCI, autorange=False, range_si=10e-3, nplc=0.02,
    )
    previous_different_range = ModeSettings(
        Mode.DCI, autorange=False, range_si=100e-3, nplc=0.02,
    )
    previous_autorange = ModeSettings(
        Mode.DCI, autorange=True, range_si=None, nplc=0.02,
    )

    assert app_module._has_same_wiring_confirmation_target(
        previous_same_wiring_target, confirmed_probe_settings,
    )
    assert not app_module._has_same_wiring_confirmation_target(
        previous_different_range, confirmed_probe_settings,
    )
    assert not app_module._has_same_wiring_confirmation_target(
        previous_autorange, confirmed_probe_settings,
    )


def test_probe_restore_requires_new_confirmation_for_changed_current_range():
    probe_settings = ModeSettings(
        Mode.DCI, autorange=False, range_si=10e-3, nplc=1,
    )
    previous_same_range = ModeSettings(
        Mode.DCI, autorange=False, range_si=10e-3, nplc=0.02,
    )
    previous_other_range = ModeSettings(
        Mode.DCI, autorange=False, range_si=100e-3, nplc=0.02,
    )
    previous_other_quantity = ModeSettings(Mode.DCV, autorange=False, range_si=10)

    assert not app_module._probe_restore_requires_confirmation(
        previous_same_range, probe_settings,
    )
    assert app_module._probe_restore_requires_confirmation(
        previous_other_range, probe_settings,
    )
    assert app_module._probe_restore_requires_confirmation(
        previous_other_quantity, probe_settings,
    )


def test_dci_gui_probe_stops_before_configuration_when_restore_range_is_unconfirmed(
    monkeypatch, tmp_path,
):
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A
    from tests.fakes import FakeTransport

    transport = FakeTransport()
    transport.function = "CURR:DC"
    transport.range_value = 100e-3
    transport.autorange = "0"
    transport.nplc = "0.02"
    serial_type = Serial8845A

    def serial_factory(port, settings, callback):
        return serial_type(
            port, settings, callback,
            transport_factory=lambda *_args: transport,
        )

    monkeypatch.setattr(app_module, "Serial8845A", serial_factory)
    monkeypatch.setattr(worker_module, "Serial8845A", serial_factory)
    monkeypatch.setattr(app_module, "MIN_HARDWARE_PROBE_DURATION_S", 0.1)
    monkeypatch.setattr(app_module, "MAX_HARDWARE_PROBE_DURATION_S", 1.0)
    monkeypatch.setattr(app_module, "HARDWARE_PROBE_WALL_LIMIT_S", 3.0)
    report_path = tmp_path / "dci-probe.json"

    result = app_module.run_real_gui_speed_probe(
        "COM3", report_path,
        wiring_confirmed=False,
        dci_current_source_confirmed=True,
        duration_s=0.2,
        measurement_mode=Mode.DCI,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert report["pre_configuration_settings"]["range_si"] == 100e-3
    assert report["status"] == "error"
    assert "--confirm-restore-current-settings" in report["error"]
    assert not any(command.startswith("CONF:") for command in transport.commands)
    assert not any(":RANG 0.01" in command for command in transport.commands)


@pytest.mark.parametrize("recorder_startup_delay_s", [0.0, 3.1])
def test_dci_gui_probe_confirms_target_and_restores_prior_range_with_fake_transport(
    monkeypatch, tmp_path, recorder_startup_delay_s,
):
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A
    from tests.fakes import FakeTransport

    transport = FakeTransport()
    transport.function = "CURR:DC"
    transport.range_value = 100e-3
    transport.autorange = "0"
    transport.nplc = "0.02"
    _delay_fake_read_responses(monkeypatch, transport)
    serial_type = Serial8845A

    def serial_factory(port, settings, callback):
        return serial_type(
            port, settings, callback,
            transport_factory=lambda *_args: transport,
        )

    monkeypatch.setattr(app_module, "Serial8845A", serial_factory)
    monkeypatch.setattr(worker_module, "Serial8845A", serial_factory)
    monkeypatch.setattr(app_module, "MIN_HARDWARE_PROBE_DURATION_S", 0.1)
    monkeypatch.setattr(app_module, "MAX_HARDWARE_PROBE_DURATION_S", 4.0)
    monkeypatch.setattr(app_module, "HARDWARE_PROBE_WALL_LIMIT_S", 8.0)
    monkeypatch.setattr(app_module, "HARDWARE_PROBE_CLEANUP_RESERVE_S", 0.5)
    if recorder_startup_delay_s:
        from fluke8845a.storage import ExperimentRecorder

        original_create = ExperimentRecorder.create

        def delayed_create(cls, *args, **kwargs):
            # Exercise the production startup path on a slow filesystem. The
            # acquisition duration starts after the recorder is ready.
            time.sleep(recorder_startup_delay_s)
            return original_create(*args, **kwargs)

        monkeypatch.setattr(ExperimentRecorder, "create", classmethod(delayed_create))
        monkeypatch.setattr(app_module, "HARDWARE_PROBE_WALL_LIMIT_S", 12.0)
    report_path = tmp_path / "dci-probe-restore.json"

    result = app_module.run_real_gui_speed_probe(
        "COM3", report_path,
        wiring_confirmed=False,
        dci_current_source_confirmed=True,
        restore_wiring_confirmed=True,
        duration_s=3.5,
        measurement_mode=Mode.DCI,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0, report
    assert report["status"] == "ok"
    assert report["sample_count"] >= 2
    assert report["measurement_speed_changes"] == []
    assert report["target_interval_changes"] == []
    assert report["restore_wiring_confirmation"] == "explicitly_confirmed"
    assert float(report["restored_configuration"]["range_response"]) == 100e-3
    assert any(":RANG 0.01" in command for command in transport.commands)
    assert any(":RANG 0.1" in command for command in transport.commands)


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
        return InstrumentInfo("FLUKE", "8845A", "1234567", "2.0", "FLUKE,8845A,TEST8845A001,2.0")

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
    assert "--confirm-front-2w-30-megaohm" in error
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


def test_real_probe_cli_accepts_unambiguous_30_megaohm_confirmation_alias(tmp_path, monkeypatch):
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
    report = tmp_path / "30-megaohm.json"
    result = app_module.main([
        "--probe-real", "--port", "COM3", "--report", str(report),
        "--confirm-front-2w-30-megaohm",
    ])

    assert result == 0
    assert captured == {
        "port": "COM3",
        "report_path": report,
        "wiring_confirmed": True,
        "shorted_leads_confirmed": False,
        "baudrate": 9600,
    }


def test_gui_probe_cli_requires_explicit_wiring_confirmation(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        app_module.main([
            "--probe-real-gui", "--port", "COM3", "--report", str(tmp_path / "gui-probe.json")
        ])

    assert exit_info.value.code == 2
    error = capsys.readouterr().err
    assert "--confirm-front-2w-30-megaohm" in error
    assert "--confirm-front-2w-short" in error


def test_gui_probe_cli_caps_test_duration_at_one_minute_without_opening_hardware(tmp_path, monkeypatch, capsys):
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
    assert "--duration must be between 8 and 50 seconds" in capsys.readouterr().err


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


def test_current_gui_probe_cli_routes_explicit_restore_confirmation(tmp_path, monkeypatch):
    captured = {}

    def fake_probe(port, report_path, **kwargs):
        captured.update(port=port, report_path=report_path, **kwargs)
        return 0

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    report = tmp_path / "dci-restore.json"
    assert app_module.main([
        "--probe-real-gui-dci", "--report", str(report), "--duration", "8",
        "--confirm-front-dci-5ma-series", "--confirm-restore-current-settings",
    ]) == 0

    assert captured["restore_wiring_confirmed"] is True
    assert captured["measurement_mode"] is Mode.DCI


def test_restore_current_settings_confirmation_is_rejected_for_voltage_probe(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        app_module.main([
            "--probe-real-gui-dcv", "--report", str(tmp_path / "dcv.json"),
            "--duration", "8", "--confirm-front-voltage-source",
            "--confirm-restore-current-settings",
        ])

    assert exit_info.value.code == 2
    assert "only valid with --probe-real-gui-dci/aci" in capsys.readouterr().err


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
    ("mode", "function", "initial_range", "source_options", "expected_range"),
    [
        (
            Mode.ACV, "VOLT:AC", 10.0,
            {
                "acv_voltage_source_confirmed": True,
                "acv_source_vrms": 1.0,
                "acv_source_vpeak": 1.41421356,
                "acv_source_frequency_hz": 50.0,
                "acv_range_v": 10.0,
                "ac_bandwidth_hz": 20,
            },
            10.0,
        ),
        (
            Mode.RES4, "FRES", 100e6,
            {"res4_four_wire_confirmed": True, "res4_range_ohm": 100e6},
            100e6,
        ),
    ],
)
def test_acv_and_res4_gui_probe_complete_with_fake_8845a(
    monkeypatch, tmp_path, mode, function, initial_range, source_options, expected_range,
):
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A
    from tests.fakes import FakeTransport

    transport = FakeTransport()
    transport.function = function
    transport.range_value = initial_range
    transport.autorange = "0"
    transport.nplc = "0.02"
    transport.bandwidth = 20
    _delay_fake_read_responses(monkeypatch, transport)
    serial_type = Serial8845A

    def serial_factory(port, settings, callback):
        return serial_type(
            port, settings, callback,
            transport_factory=lambda *_args: transport,
        )

    monkeypatch.setattr(app_module, "Serial8845A", serial_factory)
    monkeypatch.setattr(worker_module, "Serial8845A", serial_factory)
    monkeypatch.setattr(app_module, "MIN_HARDWARE_PROBE_DURATION_S", 0.1)
    monkeypatch.setattr(app_module, "MAX_HARDWARE_PROBE_DURATION_S", 4.0)
    monkeypatch.setattr(app_module, "HARDWARE_PROBE_WALL_LIMIT_S", 8.0)
    monkeypatch.setattr(app_module, "HARDWARE_PROBE_CLEANUP_RESERVE_S", 1.0)
    monkeypatch.setattr(app_module, "HARDWARE_PROBE_GUI_DRAIN_RESERVE_S", 0.1)
    report_path = tmp_path / f"{mode.value.lower()}-fake-probe.json"

    result = app_module.run_real_gui_speed_probe(
        "COM3", report_path, wiring_confirmed=False, duration_s=3.0,
        measurement_mode=mode, **source_options,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 0, report
    assert report["simulation"] is False
    assert report["status"] == "ok"
    assert report["mode"] == mode.value
    assert report["initial_configuration_readback"]["input_terminals"] == "FRONT"
    assert float(report["initial_configuration_readback"]["range_response"]) == expected_range
    assert report["sample_count"] >= 3
    assert report["invalid_count"] == 0
    assert any(line.strip().upper() == "> SYST:LOC" for line in report["communication_log"])
    if mode == Mode.ACV:
        assert report["operator_wiring_confirmation"] == "front_panel_acv_voltage_source"
        assert report["acv_source"]["waveform_independently_verified"] is False
    else:
        assert report["operator_wiring_confirmation"] == "front_panel_res4_four_wire_unpowered_dut"
        assert report["res4_dut"]["four_wire_topology_independently_verified"] is False


@pytest.mark.parametrize(
    ("mode", "bandwidth", "expected"),
    [
        (Mode.DCV, None, (0.2, 1)),
        (Mode.DCI, None, (0.2, 1)),
        (Mode.RES2, None, (0.2, 1)),
        (Mode.RES4, None, (0.2, 1)),
        (Mode.ACI, 3, (200, 3)),
        (Mode.ACI, 200, (20, 200)),
    ],
)
def test_hardware_probe_speed_changes_use_documented_parameters(mode, bandwidth, expected):
    assert app_module._probe_speed_change_values(mode, bandwidth) == expected


@pytest.mark.parametrize(
    ("mode_flag", "confirmation", "mode", "extra_args", "expected_kwargs"),
    [
        (
            "--probe-real-gui-acv", "--confirm-front-acv-voltage-source", Mode.ACV,
            ["--acv-source-vrms", "120", "--acv-source-vpeak", "170",
             "--acv-frequency-hz", "1000", "--acv-range-v", "750",
             "--ac-bandwidth-hz", "20"],
            {
                "acv_voltage_source_confirmed": True,
                "acv_source_vrms": 120.0,
                "acv_source_vpeak": 170.0,
                "acv_source_frequency_hz": 1000.0,
                "acv_range_v": 750.0,
                "ac_bandwidth_hz": 20,
            },
        ),
        (
            "--probe-real-gui-res4", "--confirm-front-res4-four-wire", Mode.RES4,
            ["--res4-range-ohm", "100000000"],
            {"res4_four_wire_confirmed": True, "res4_range_ohm": 100000000.0},
        ),
    ],
)
def test_gui_probe_cli_routes_acv_and_res4(
    tmp_path, monkeypatch, mode_flag, confirmation, mode, extra_args, expected_kwargs,
):
    captured = {}

    def fake_probe(port, report_path, **kwargs):
        captured.update(port=port, report_path=report_path, **kwargs)
        return 29

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", fake_probe)
    report = tmp_path / f"{mode.value.lower()}-probe.json"

    assert app_module.main([
        mode_flag, "--port", "COM3", "--baudrate", "115200", "--duration", "8",
        "--report", str(report), confirmation, *extra_args,
    ]) == 29

    assert captured["measurement_mode"] is mode
    assert captured["report_path"] == report
    for key, value in expected_kwargs.items():
        assert captured[key] == value


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--confirm-front-acv-voltage-source"],
        ["--confirm-front-acv-voltage-source", "--acv-source-vrms", "12",
         "--acv-source-vpeak", "17", "--acv-frequency-hz", "50",
         "--acv-range-v", "10", "--ac-bandwidth-hz", "20"],
        ["--confirm-front-acv-voltage-source", "--acv-source-vrms", "12",
         "--acv-source-vpeak", "1001", "--acv-frequency-hz", "1000",
         "--acv-range-v", "100", "--ac-bandwidth-hz", "20"],
        ["--confirm-front-acv-voltage-source", "--acv-source-vrms", "120",
         "--acv-source-vpeak", "170", "--acv-frequency-hz", "1000",
         "--acv-range-v", "100", "--ac-bandwidth-hz", "20"],
        ["--confirm-front-acv-voltage-source", "--acv-source-vrms", "300",
         "--acv-source-vpeak", "425", "--acv-frequency-hz", "300000",
         "--acv-range-v", "750", "--ac-bandwidth-hz", "200"],
        ["--confirm-front-acv-voltage-source", "--acv-source-vrms", "1",
         "--acv-source-vpeak", "1.5", "--acv-frequency-hz", "10",
         "--acv-range-v", "10", "--ac-bandwidth-hz", "20"],
    ],
)
def test_acv_gui_probe_rejects_missing_or_unsafe_source_before_connect(
    tmp_path, monkeypatch, extra_args,
):
    def forbidden_probe(*_args, **_kwargs):
        pytest.fail("invalid ACV request must be rejected before opening the serial port")

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", forbidden_probe)
    with pytest.raises(SystemExit) as exit_info:
        app_module.main([
            "--probe-real-gui-acv", "--report", str(tmp_path / "acv.json"),
            "--duration", "8", *extra_args,
        ])
    assert exit_info.value.code == 2


def test_res4_gui_probe_requires_four_wire_confirmation_and_supported_range(tmp_path, monkeypatch):
    def forbidden_probe(*_args, **_kwargs):
        pytest.fail("invalid RES4 request must be rejected before opening the serial port")

    monkeypatch.setattr(app_module, "run_real_gui_speed_probe", forbidden_probe)
    for args in (
        ["--confirm-front-res4-four-wire", "--res4-range-ohm", "10"],
        ["--res4-range-ohm", "100"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            app_module.main([
                "--probe-real-gui-res4", "--report", str(tmp_path / "res4.json"),
                "--duration", "8", *args,
            ])
        assert exit_info.value.code == 2


@pytest.mark.parametrize(
    ("mode", "bandwidth", "frequency", "expected"),
    [
        (Mode.ACV, 20, 50, (3, 20)),
        (Mode.ACV, 200, 1000, (20, 200)),
        (Mode.ACI, 20, 60, (3, 20)),
    ],
)
def test_ac_speed_probe_uses_only_bands_within_declared_signal_frequency(
    mode, bandwidth, frequency, expected,
):
    assert app_module._probe_speed_change_values(mode, bandwidth, frequency) == expected


def test_ac_speed_probe_refuses_signal_frequency_that_cannot_support_two_bands():
    with pytest.raises(ValueError, match="两个交流带宽档"):
        app_module._probe_speed_change_values(Mode.ACV, 3, 10)


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


def test_mock_e2e_subprocess_completes_all_six_modes_and_saves_each_run(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "mock-e2e-report.json"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["FLUKE8845A_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(repository_root / "src"), env.get("PYTHONPATH", ""),
    ]))

    result = subprocess.run(
        [sys.executable, "-m", "fluke8845a.app", "--mock-e2e", "--report", str(report_path)],
        cwd=repository_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["simulation"] is True
    assert report["mode_count"] == 6
    assert {result["mode"] for result in report["modes"]} == {
        mode.value for mode in Mode
    }
    assert all(result["valid_sample_count"] >= 3 for result in report["modes"])
    wheel_zoom = report["wheel_zoom_validation"]
    assert wheel_zoom["simulation"] is True
    assert wheel_zoom["mode"] == Mode.DCV.value
    assert wheel_zoom["wheel_event_count"] >= 1400
    assert wheel_zoom["wheel_events_accepted"] == wheel_zoom["wheel_event_count"]
    assert wheel_zoom["initial_span_si"] >= wheel_zoom["final_span_si"] * 1e6
    assert wheel_zoom["final_span_si"] <= 1e-323
    assert wheel_zoom["precision_limit_reached"] is True
    assert wheel_zoom["follow_disabled"] is True
    assert wheel_zoom["plot_values_preserved"] is True
    assert wheel_zoom["raw_readings_preserved"] is True


def test_mock_e2e_failure_stops_active_acquisition_before_temp_cleanup(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "mock-e2e-failure-report.json"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["FLUKE8845A_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(repository_root / "src"), env.get("PYTHONPATH", ""),
    ]))
    script = r"""
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox
import fluke8845a.app as app_module

original_start = app_module.MainWindow._start_continuous
def start_then_fail(window):
    original_start(window)
    QTimer.singleShot(0, lambda: QMessageBox.warning(window, "forced test error", "forced"))
app_module.MainWindow._start_continuous = start_then_fail
raise SystemExit(app_module.run_mock_six_mode_gui_probe(Path(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(report_path)],
        cwd=repository_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert "forced" in report["error"]


def test_mock_e2e_unexpected_event_loop_exit_waits_for_active_worker(tmp_path):
    repository_root = Path(__file__).resolve().parents[1]
    report_path = tmp_path / "mock-e2e-loop-exit-report.json"
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["FLUKE8845A_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [
        str(repository_root / "src"), env.get("PYTHONPATH", ""),
    ]))
    script = r"""
import sys
from pathlib import Path
from PySide6.QtCore import QTimer, QCoreApplication
import fluke8845a.app as app_module

original_start = app_module.MainWindow._start_continuous
def start_then_quit_loop(window):
    original_start(window)
    QTimer.singleShot(0, QCoreApplication.quit)
app_module.MainWindow._start_continuous = start_then_quit_loop
raise SystemExit(app_module.run_mock_six_mode_gui_probe(Path(sys.argv[1])))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(report_path)],
        cwd=repository_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "error"
    assert "事件循环意外结束" in report["error"]


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


def test_gui_rate_reconstruction_rejects_nonmonotonic_receipt_timestamps():
    rows = [
        {"status": "ok", "value_si": "1", "elapsed_s": "0", "read_duration_s": "0.02"},
        {"status": "ok", "value_si": "1", "elapsed_s": "0.2", "read_duration_s": "1.0"},
        {"status": "ok", "value_si": "1", "elapsed_s": "0.4", "read_duration_s": "0.02"},
    ]

    with pytest.raises(ValueError, match="receipt timestamps"):
        app_module._reconstruct_gui_rate_from_rows(rows, visible_sample_count=3)


def test_capture_runtime_source_identity_records_source_files(tmp_path):
    for source in probe_provenance_module.PROBE_SOURCE_FILES:
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source: {source}\n", encoding="utf-8")

    identity = probe_provenance_module.capture_runtime_source_identity(
        executable_path=tmp_path / "python.exe", source_root=tmp_path,
    )

    assert identity["kind"] == "source_tree_files"
    assert set(identity["source_file_sha256"]) == set(probe_provenance_module.PROBE_SOURCE_FILES)
    for source, digest in identity["source_file_sha256"].items():
        assert digest == hashlib.sha256((tmp_path / source).read_bytes()).hexdigest().upper()


def test_probe_source_identity_covers_every_python_module_in_the_package():
    repository_root = Path(__file__).resolve().parents[1]
    package_root = repository_root / "src" / "fluke8845a"
    package_python_files = {
        f"src/fluke8845a/{path.name}" for path in package_root.glob("*.py")
    }

    assert set(probe_provenance_module.PROBE_SOURCE_FILES) == package_python_files


@pytest.mark.parametrize(
    "dependency",
    [
        "src/fluke8845a/filter_policy.py",
        "src/fluke8845a/versioning.py",
        "src/fluke8845a/state.py",
        "src/fluke8845a/path_security.py",
    ],
)
def test_capture_runtime_source_identity_detects_probe_dependency_changes(tmp_path, dependency):
    required_sources = set(probe_provenance_module.PROBE_SOURCE_FILES) | {dependency}
    for source in required_sources:
        path = tmp_path / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"source: {source}\n", encoding="utf-8")

    kwargs = {"executable_path": tmp_path / "python.exe", "source_root": tmp_path}
    before = probe_provenance_module.capture_runtime_source_identity(**kwargs)
    dependency_path = tmp_path / dependency
    dependency_path.write_text(dependency_path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    after = probe_provenance_module.capture_runtime_source_identity(**kwargs)

    assert before != after


def test_capture_runtime_source_identity_supports_installed_package_layout(tmp_path):
    package_dir = tmp_path / "site-packages" / "fluke8845a"
    package_dir.mkdir(parents=True)
    for source in probe_provenance_module.PROBE_SOURCE_FILES:
        (package_dir / Path(source).name).write_text(f"installed: {source}\n", encoding="utf-8")

    identity = probe_provenance_module.capture_runtime_source_identity(
        executable_path=tmp_path / "python.exe",
        source_root=tmp_path / "site-packages",
        package_file=package_dir / "probe_provenance.py",
    )

    assert identity["kind"] == "source_tree_files"
    for source, digest in identity["source_file_sha256"].items():
        assert digest == hashlib.sha256((package_dir / Path(source).name).read_bytes()).hexdigest().upper()


def test_capture_runtime_source_identity_binds_frozen_executable(tmp_path):
    executable = tmp_path / "Fluke8845A.exe"
    executable.write_bytes(b"frozen executable bytes")
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest().upper()
    provenance = {
        "format_version": 1,
        "head_commit": "a" * 40,
        "worktree_dirty": False,
        "source_tree_sha256": "B" * 64,
        "executables": {"desktop": {"sha256": executable_digest}},
    }
    (tmp_path / "BUILD-PROVENANCE.json").write_text(json.dumps(provenance), encoding="utf-8")

    identity = probe_provenance_module.capture_runtime_source_identity(executable_path=executable)

    assert identity == {
        "kind": "frozen_executable",
        "head_commit": "a" * 40,
        "worktree_dirty": False,
        "source_tree_sha256": "B" * 64,
        "executable_sha256": executable_digest,
    }


def test_capture_runtime_source_identity_accepts_nuitka_dist_build_identity(tmp_path):
    dist_directory = tmp_path / "Fluke8845A.dist"
    dist_directory.mkdir()
    executable = dist_directory / "Fluke8845A.exe"
    executable.write_bytes(b"Nuitka standalone executable bytes")
    executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest().upper()
    build_identity = {
        "format_version": 1,
        "head_commit": "d" * 40,
        "worktree_dirty": True,
        "source_tree_sha256": "E" * 64,
        "executables": {"desktop": {"sha256": executable_digest}},
    }
    (dist_directory / "BUILD-IDENTITY.json").write_text(json.dumps(build_identity), encoding="utf-8")

    identity = probe_provenance_module.capture_runtime_source_identity(executable_path=executable)

    assert identity["kind"] == "frozen_executable"
    assert identity["head_commit"] == "d" * 40
    assert identity["source_tree_sha256"] == "E" * 64
    assert identity["executable_sha256"] == executable_digest


@pytest.mark.parametrize("marker_json", ["null", "[]"])
def test_capture_runtime_source_identity_rejects_non_object_build_marker(tmp_path, marker_json):
    executable = tmp_path / "Fluke8845A.exe"
    executable.write_bytes(b"frozen executable bytes")
    (tmp_path / "BUILD-PROVENANCE.json").write_text(marker_json, encoding="utf-8")

    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        probe_provenance_module.capture_runtime_source_identity(executable_path=executable)


def test_capture_runtime_source_identity_rejects_frozen_binary_mismatch(tmp_path):
    executable = tmp_path / "Fluke8845A.exe"
    executable.write_bytes(b"changed executable bytes")
    provenance = {
        "format_version": 1,
        "head_commit": "a" * 40,
        "worktree_dirty": False,
        "source_tree_sha256": "b" * 64,
        "executables": {"desktop": {"sha256": "c" * 64}},
    }
    (tmp_path / "BUILD-PROVENANCE.json").write_text(json.dumps(provenance), encoding="utf-8")

    with pytest.raises(ValueError, match="EXE与BUILD-PROVENANCE"):
        probe_provenance_module.capture_runtime_source_identity(executable_path=executable)


def test_desktop_cli_rejects_removed_mock_mode():
    with pytest.raises(SystemExit) as result:
        app_module.main(["--mock"])
    assert result.value.code == 2
