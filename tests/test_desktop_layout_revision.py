import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from fluke8845a.gui import MainWindow

@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    instance.setQuitOnLastWindowClosed(False)
    return instance

def test_connect_closes_dialog_and_settings_hides_range_and_directory(app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "config"))
    window = MainWindow()
    try:
        window.show()
        QTest.qWait(30)
        assert window.configuration_dialog.isVisible()
        window._on_connected(True, "MOCK")
        QTest.qWait(30)
        assert not window.configuration_dialog.isVisible()
        window._show_measurement_settings()
        QTest.qWait(30)
        assert window.configuration_dialog.isVisible()
        assert not window.autorange_check.isVisible()
        assert not window.range_combo.isVisible()
        assert not window.output_root.isVisible()
        assert window.main_range_combo.isVisible()
    finally:
        window.close()
        app.processEvents()


def test_current_mode_switch_does_not_request_wiring_confirmation(app, monkeypatch, tmp_path):
    from fluke8845a.modes import Mode
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "switch"))
    window = MainWindow()
    sent, asked = [], []
    try:
        window._bridge.configure_unconfirmed_mode.disconnect(window._worker.configure_mode_without_wiring_confirmation)
        window._bridge.configure_unconfirmed_mode.connect(sent.append)
        monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args))
        window._connected = True
        window._simulation = False
        window._control_mode = "computer"
        window._instrument_mode_known = True
        window._instrument_mode_readback = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        window._update_controls()
        window.mode_combo.setCurrentIndex(window.mode_combo.findData(Mode.DCI.value))
        assert len(sent) == 1 and sent[0].mode == Mode.DCI
        assert not asked
    finally:
        window._connected = False
        window._configure_pending = False
        window.close()
        app.processEvents()


@pytest.mark.parametrize("answer,expected", [("Yes", True), ("Cancel", False)])
def test_start_wiring_confirmation_is_modal_and_target_specific(app, monkeypatch, tmp_path, answer, expected):
    from fluke8845a.modes import Mode
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "start"))
    window = MainWindow()
    sent, asked = [], []
    try:
        window._bridge.start_experiment.disconnect(window._worker.start_experiment)
        window._bridge.start_experiment.connect(lambda *args: sent.append(args))
        monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args) or getattr(QMessageBox.StandardButton, answer))
        monkeypatch.setattr(window, "_prepare_start_preflight", lambda *args, **kwargs: False)
        window._connected = True
        window._simulation = False
        window._control_mode = "computer"
        window._terminals = "FRONT"
        window._instrument_mode_known = True
        window._instrument_mode_readback = Mode.DCV
        window._load_mode_controls(Mode.DCV)
        window.experiment_name.setText("test_run")
        window.output_root.setText(str(tmp_path / "runs"))
        window._update_controls()
        window._start(one_shot=False)
        assert len(asked) == 1
        assert "直流电压" in asked[0][2]
        assert bool(sent) is expected
        if sent:
            assert sent[0][-1] == sent[0][0]
    finally:
        window._connected = False
        window._start_pending = False
        window.close()
        app.processEvents()


@pytest.mark.parametrize("answer,expected", [("Yes", True), ("Cancel", False)])
def test_current_range_confirmation_is_modal_and_target_specific(app, monkeypatch, tmp_path, answer, expected):
    from fluke8845a.modes import Mode
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "range"))
    window = MainWindow()
    sent, asked = [], []
    try:
        window._bridge.configure.disconnect(window._worker.configure_mode)
        window._bridge.configure.connect(lambda *args: sent.append(args))
        monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args) or getattr(QMessageBox.StandardButton, answer))
        window._connected = True
        window._simulation = False
        window._control_mode = "computer"
        window._terminals = "FRONT"
        window._current_mode = Mode.DCI
        window._instrument_mode_readback = Mode.DCI
        window._instrument_mode_known = True
        window._load_mode_controls(Mode.DCI)
        window._update_controls()
        previous = window.main_range_combo.currentData()
        window.main_range_combo.setCurrentIndex(window.main_range_combo.findData(10.0))
        assert len(asked) == 1 and "10 A" in asked[0][2]
        assert bool(sent) is expected
        if sent:
            assert sent[0][0].range_si == 10.0
            assert sent[0][1] == sent[0][0]
        else:
            assert window.main_range_combo.currentData() == previous
    finally:
        window._connected = False
        window._configure_pending = False
        window.close()
        app.processEvents()
