from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from fluke8845a.gui import MainWindow
from fluke8845a.modes import Mode


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.mark.parametrize("reading,unit", [
    (166e-9, "nA"), (166e-6, "µA"), (5e-3, "mA"), (-166e-6, "µA"),
])
def test_current_axis_uses_absolute_reading_not_fluctuation(qt_app, monkeypatch, tmp_path, reading, unit):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path))
    window = MainWindow()
    try:
        window._current_mode = Mode.DCI
        window._plot_x.extend((0.0, 1.0))
        window._plot_y.extend((reading, reading + 1e-9))
        window._refresh_curve()
        window.view_box.setRange(yRange=(-1e-9, 2e-9), padding=0)
        window._sync_plot_y_axis()
        assert window.y_axis.display_unit == unit
        assert list(window._plot_y) == [reading, reading + 1e-9]
    finally:
        window.close()
        qt_app.processEvents()


@pytest.mark.parametrize("span,unit,factor", [(30, "s", 1), (120, "min", 60), (7200, "h", 3600)])
def test_time_axis_tracks_visible_duration_without_changing_seconds(qt_app, monkeypatch, tmp_path, span, unit, factor):
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path))
    window = MainWindow()
    try:
        window._plot_x.extend((0.0, float(span)))
        window._plot_y.extend((1.0, 1.1))
        window._refresh_curve()
        window.view_box.setRange(xRange=(0, span), padding=0)
        window._sync_plot_x_axis()
        assert window.x_axis.display_unit == unit
        assert window.x_axis.display_factor == factor
        assert float(window.x_axis.tickStrings((span,), 1.0, span / 2)[0]) == span / factor
        assert list(window._plot_x) == [0.0, float(span)]
    finally:
        window.close()
        qt_app.processEvents()
