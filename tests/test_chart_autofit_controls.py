import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from fluke8845a.gui import MainWindow
from fluke8845a.modes import Mode


@pytest.fixture
def window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path))
    w = MainWindow()
    yield w
    w.close()
    app.processEvents()


def add_points(window, points):
    rows = [
        {"sample_index": int(x) + 1, "elapsed_s": x, "mode": Mode.DCV.value,
         "value_si": y, "status": "ok", "unit_si": "V"}
        for x, y in points
    ]
    window._on_sample(rows, 0.0, {"count": len(rows), "valid_count": len(rows)})


def test_default_full_time_and_independent_y_auto(window):
    assert window.x_auto_check.isChecked()
    assert window.y_auto_check.isChecked()
    assert not window.follow_check.isChecked()
    add_points(window, [(0, 1), (4, 2), (10, 3)])
    lo, hi = window.view_box.viewRange()[0]
    assert lo == pytest.approx(0)
    assert hi >= 10


def test_y_auto_uses_visible_x_window_only(window):
    window.average_window_spin.setValue(1)
    add_points(window, [(0, 100), (1, 1), (2, 2), (3, 3), (4, 100)])
    window.x_auto_check.setChecked(False)
    window.view_box.setXRange(0.9, 3.1, padding=0)
    window._on_plot_range_changed_manually([True, False])
    window._update_chart_auto_ranges()
    origin = window._plot_y_origin or 0.0
    lo, hi = window.view_box.viewRange()[1]
    assert lo + origin < 1
    assert 3 < hi + origin < 10


def test_follow_keeps_manual_span_and_tracks_latest_sample(window):
    add_points(window, [(0, 1), (5, 2), (10, 3)])
    window.follow_check.setChecked(True)
    assert not window.x_auto_check.isChecked()
    window.view_box.setXRange(5, 10, padding=0)
    window._on_plot_range_changed_manually([True, False])
    assert window.follow_check.isChecked()
    add_points(window, [(11, 4)])
    lo, hi = window.view_box.viewRange()[0]
    assert hi == pytest.approx(11)
    assert hi - lo == pytest.approx(5)


def test_auto_x_and_follow_are_exclusive(window):
    window.follow_check.setChecked(True)
    assert not window.x_auto_check.isChecked()
    window.x_auto_check.setChecked(True)
    assert not window.follow_check.isChecked()


def test_follow_does_not_jump_while_dragging(window):
    add_points(window, [(0, 1), (5, 2), (10, 3)])
    window.follow_check.setChecked(True)
    window.view_box.setXRange(3, 8, padding=0)
    window._on_plot_range_changed_manually([True, False])
    window.view_box.drag_in_progress = True
    add_points(window, [(11, 4)])
    assert window.view_box.viewRange()[0] == pytest.approx([3, 8])
    window.view_box.drag_in_progress = False
    window.view_box.dragFinished.emit()
    assert window.view_box.viewRange()[0] == pytest.approx([6, 11])


def test_manual_y_disables_only_y_auto(window):
    window.follow_check.setChecked(True)
    window._on_plot_range_changed_manually([False, True])
    assert window.follow_check.isChecked()
    assert not window.y_auto_check.isChecked()

def test_chart_controls_are_visible_on_main_window(window):
    from PySide6.QtTest import QTest
    window.show()
    QTest.qWait(30)
    window.configuration_dialog.hide()
    QTest.qWait(30)
    assert window.x_auto_check.isVisible()
    assert window.y_auto_check.isVisible()
    assert window.follow_check.isVisible()

def test_horizontal_drag_keeps_vertical_auto_enabled(window):
    add_points(window, [(0, 1), (5, 2), (10, 3)])
    window.follow_check.setChecked(True)
    window.view_box.drag_in_progress = True
    window.view_box.sigRangeChangedManually.emit([True, True])
    assert window.follow_check.isChecked()
    assert window.y_auto_check.isChecked()
    window.view_box.drag_in_progress = False
    window.view_box.dragFinished.emit()
