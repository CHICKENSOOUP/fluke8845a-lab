from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from fluke8845a import gui as gui_module
from fluke8845a import worker as worker_module
from fluke8845a.gui import MainWindow
from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings, Serial8845A, SerialSettings
from fluke8845a.state import AcquisitionState
from fluke8845a.worker import MeasurementWorker
from tests.fakes import FakeTransport


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.mark.parametrize("target", [Mode.DCI, Mode.ACI, Mode.RES2, Mode.RES4, Mode.ACV])
def test_real_state_mode_button_switches_without_wiring_confirmation(qt_app, monkeypatch, tmp_path, target):
    """Fake connected GUI state, with the worker signal disconnected: no hardware I/O."""
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "config"))
    window = MainWindow()
    requests = []
    try:
        window._bridge.configure_unconfirmed_mode.disconnect(window._worker.configure_mode_without_wiring_confirmation)
        window._bridge.configure_unconfirmed_mode.connect(requests.append)
        window._connected = True
        window._simulation = False
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._instrument_mode_readback = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        window._update_controls()
        monkeypatch.setattr(gui_module.QMessageBox, "question", lambda *_a, **_kw: pytest.fail("unexpected wiring dialog"))

        window.mode_buttons[target].click()

        assert len(requests) == 1
        assert requests[0].mode is target
        assert window._current_mode is target
        assert window._pending_wiring_retry is None
        assert not hasattr(window, "wiring_confirm_button")
    finally:
        window._connected = False
        window._configure_pending = False
        window.close()
        qt_app.processEvents()


def test_unconfirmed_worker_mode_change_does_not_authorize_experiment(qt_app, monkeypatch, tmp_path):
    """Serial8845A is backed solely by FakeTransport; this never opens a real port."""
    transport = FakeTransport()
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: Serial8845A(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    try:
        worker.connect_real("FAKE", SerialSettings(), ModeSettings(Mode.DCV))
        assert worker.instrument is not None and worker.instrument.connected
        transport.commands.clear()
        target = ModeSettings(Mode.DCI)

        worker.configure_mode_without_wiring_confirmation(target)
        assert worker.current_mode is Mode.DCI
        assert transport.commands
        assert not errors

        transport.commands.clear()
        worker.start_experiment(target, "not-confirmed", str(tmp_path), 0.1)
        assert errors and "接线" in errors[-1]
        assert transport.commands == []
        assert worker.state is AcquisitionState.STOPPED
        assert not (tmp_path / "not-confirmed").exists()
    finally:
        worker.shutdown()


def test_failed_switch_can_restore_original_mode_without_approving_current_range(qt_app, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(
        worker_module, "Serial8845A",
        lambda port, settings, callback: Serial8845A(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    worker.error.connect(errors.append)
    original = ModeSettings(Mode.DCI, autorange=False, range_si=0.1)
    try:
        worker.connect_real("FAKE", SerialSettings(), ModeSettings(Mode.DCV))
        assert worker.instrument is not None and worker.instrument.connected
        worker.configure_mode_without_wiring_confirmation(original)
        assert worker.current_mode is Mode.DCI
        configure = worker.instrument.configure
        monkeypatch.setattr(worker.instrument, "configure", lambda _settings: (_ for _ in ()).throw(ValueError("switch failed")))
        worker.configure_mode_without_wiring_confirmation(ModeSettings(Mode.ACV))
        assert errors and "switch failed" in errors[-1]
        assert worker.current_mode is Mode.DCI

        monkeypatch.setattr(worker.instrument, "configure", configure)
        errors.clear()
        transport.commands.clear()
        worker.configure_mode_without_wiring_confirmation(original)
        assert not errors
        assert transport.commands
        assert worker.current_mode is Mode.DCI

        errors.clear()
        transport.commands.clear()
        worker.configure_mode_without_wiring_confirmation(ModeSettings(Mode.DCI, autorange=False, range_si=0.01))
        assert errors
        assert transport.commands == []
    finally:
        worker.shutdown()


def test_confirmed_experiment_starts_after_unconfirmed_mode_switch(qt_app, monkeypatch, tmp_path):
    transport = FakeTransport()
    monkeypatch.setattr(
        worker_module, "Serial8845A",
        lambda port, settings, callback: Serial8845A(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    worker = MeasurementWorker()
    errors = []
    finished = []
    worker.error.connect(errors.append)
    worker.experiment_finished.connect(finished.append)
    try:
        worker.connect_real("FAKE", SerialSettings(), ModeSettings(Mode.DCV))
        target = ModeSettings(Mode.DCI)
        worker.configure_mode_without_wiring_confirmation(target)
        worker.start_experiment(target, "confirmed", str(tmp_path), 0, True, confirmed_settings=target)
        assert not errors
        assert finished and (Path(finished[0]) / "measurements.csv").is_file()
        assert worker.state is AcquisitionState.STOPPED
    finally:
        worker.shutdown()


def test_mode_buttons_remain_locked_during_acquisition(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "config-lock"))
    window = MainWindow()
    requests = []
    try:
        window._bridge.configure_unconfirmed_mode.disconnect(window._worker.configure_mode_without_wiring_confirmation)
        window._bridge.configure_unconfirmed_mode.connect(requests.append)
        window._connected = True
        window._simulation = False
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._on_acquisition_state_changed("running", False)
        window.mode_buttons[Mode.DCI].click()
        assert not window.mode_buttons[Mode.DCI].isEnabled()
        assert window._current_mode is Mode.DCV
        assert requests == []
    finally:
        window._connected = False
        window._acquiring = False
        window._experiment_active = False
        window.close()
        qt_app.processEvents()
