import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from fluke8845a.gui import MainWindow
from fluke8845a.modes import Mode


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "config"))
    w = MainWindow()
    w._bridge.configure.disconnect(w._worker.configure_mode)
    w._bridge.configure_unconfirmed_mode.disconnect(w._worker.configure_mode_without_wiring_confirmation)
    w._bridge.start_experiment.disconnect(w._worker.start_experiment)
    w._connected = True
    w._simulation = False
    w._control_mode = "computer"
    w._terminals = "FRONT"
    w._instrument_mode_known = True
    w._instrument_mode_readback = Mode.DCV
    w._load_mode_controls(Mode.DCV)
    w.output_root.setText(str(tmp_path / "runs"))
    w.experiment_name.setText("test_run")
    w._update_controls()
    yield w
    w._connected = False
    w._start_pending = False
    w._configure_pending = False
    w.close()
    app.processEvents()


def test_action_row_and_firmware_labels(window, app):
    window.resize(1600, 900)
    window.show()
    app.processEvents()
    widgets = (window.main_range_combo, window.start_button, window.single_button,
               window.pause_button, window.stop_button, window.average_window_spin,
               window.reset_measurement_button, window.save_curve_button)
    assert all(w.isVisible() for w in widgets)
    assert all(w.parentWidget() is window.start_button.parentWidget() for w in widgets)
    assert max(w.mapTo(window, w.rect().center()).y() for w in widgets) - min(
        w.mapTo(window, w.rect().center()).y() for w in widgets
    ) <= 5
    assert not hasattr(window, "firmware_bottom_label") or not window.firmware_bottom_label.isVisible()
    assert not hasattr(window, "wiring_confirm_button") or not window.wiring_confirm_button.isVisible()


@pytest.mark.parametrize("answer,expected", [(QMessageBox.StandardButton.Yes, 1), (QMessageBox.StandardButton.Cancel, 0)])
def test_start_wiring_popup_accept_or_cancel(window, monkeypatch, tmp_path, answer, expected):
    asked, sent = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append((args, kwargs)) or answer)
    monkeypatch.setattr(window, "_prepare_start_preflight", lambda *args, **kwargs: False)
    window._bridge.start_experiment.connect(lambda *args: sent.append(args))
    window._start(one_shot=False)
    assert len(asked) == 1
    assert len(asked[0][0][2]) <= 80
    assert len(sent) == expected
    assert not hasattr(window, "wiring_confirm_button") or not window.wiring_confirm_button.isVisible()
    if sent:
        assert sent[0][-1] == sent[0][0]


@pytest.mark.parametrize("answer,expected", [(QMessageBox.StandardButton.Yes, 1), (QMessageBox.StandardButton.Cancel, 0)])
def test_current_range_wiring_popup_accept_or_cancel(window, monkeypatch, answer, expected):
    asked, sent = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append((args, kwargs)) or answer)
    window._bridge.configure.connect(lambda *args: sent.append(args))
    window._current_mode = Mode.DCI
    window._instrument_mode_readback = Mode.DCI
    window._load_mode_controls(Mode.DCI)
    window._update_controls()
    original = window.main_range_combo.currentData()
    window.main_range_combo.setCurrentIndex(window.main_range_combo.findData(10.0))
    assert len(asked) == 1
    assert len(asked[0][0][2]) <= 80
    assert len(sent) == expected
    assert not hasattr(window, "wiring_confirm_button") or not window.wiring_confirm_button.isVisible()
    if sent:
        assert sent[0][0].range_si == 10.0
        assert sent[0][1] == sent[0][0]
    else:
        assert window.main_range_combo.currentData() == original


def test_mode_switch_has_no_wiring_popup(window, monkeypatch):
    asked, sent = [], []
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: asked.append(args) or QMessageBox.StandardButton.Cancel)
    window._bridge.configure_unconfirmed_mode.connect(sent.append)
    window.mode_buttons[Mode.DCI].click()
    assert not asked
    assert len(sent) == 1 and sent[0].mode == Mode.DCI


def test_plot_hover_and_click_do_not_show_crosshair_or_select_point(window, app):
    window.resize(1600, 900)
    window.show()
    window._on_sample([
        {"sample_index": i, "elapsed_s": x, "mode": Mode.DCV.value,
         "value_si": y, "status": "ok", "unit_si": "V"}
        for i, (x, y) in enumerate(((0.2, 1.0), (1.0, 3.0), (2.0, 5.0)), 1)
    ], 0.0, {"count": 3, "valid_count": 3})
    app.processEvents()
    view = window.plot_widget.getPlotItem().vb
    position = window.plot_widget.mapFromScene(view.sceneBoundingRect().center())
    QTest.mouseMove(window.plot_widget.viewport(), position)
    QTest.qWait(30)
    QTest.mouseClick(window.plot_widget.viewport(), Qt.MouseButton.LeftButton, pos=position)
    app.processEvents()
    assert window._crosshair_x is None or not window._crosshair_x.isVisible()
    assert window._crosshair_y is None or not window._crosshair_y.isVisible()
    assert window._selected_plot_point is None
    assert not window._locked_cursor_marker.isVisible()
