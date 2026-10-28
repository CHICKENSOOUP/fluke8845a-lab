import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


def test_moving_average_recalculates_and_does_not_cross_gaps():
    from fluke8845a.moving_average import MovingAverageTrace

    trace = MovingAverageTrace(window=2, capacity=5)
    for x, y, gap in [(0, 1, False), (1, 3, False), (2, 9, True), (3, 5, False)]:
        trace.append(x, y, gap)
    assert list(trace.values) == pytest.approx([1, 2, 9, 7])
    trace.set_window(3)
    assert list(trace.values) == pytest.approx([1, 2, 9, 7])
    trace.append(4, 7, False)
    assert list(trace.values)[-1] == pytest.approx(7)
    trace.append(5, 11, False)
    assert len(trace.samples) == len(trace.values) == 5
    assert list(trace.values)[-1] == pytest.approx(23 / 3)


def test_live_average_control_and_left_edge(monkeypatch, tmp_path):
    from fluke8845a.gui import MainWindow
    from fluke8845a.modes import Mode

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path))
    window = MainWindow()
    try:
        window.average_window_spin.setValue(2)
        window._on_sample([
            {"sample_index": i, "elapsed_s": x, "mode": Mode.DCV.value,
             "value_si": y, "status": "ok", "unit_si": "V"}
            for i, (x, y) in enumerate(((0.2, 1.0), (1.0, 3.0), (2.0, 5.0)), 1)
        ], 0.0, {"count": 3, "valid_count": 3})
        assert list(window._average_trace.values) == pytest.approx([1, 2, 4])
        assert window.view_box.viewRange()[0][0] == pytest.approx(0)
        window._acquiring = True
        window.average_window_spin.setValue(3)
        assert list(window._average_trace.values) == pytest.approx([1, 2, 3])
        assert len(window.average_curve.getData()[0]) == 3
        assert list(window._plot_y) == pytest.approx([1, 3, 5])
        window._on_sample([
            {"sample_index": 4, "status": "timeout"},
            {"sample_index": 5, "elapsed_s": 4.0, "mode": Mode.DCV.value,
             "value_si": 12.0, "status": "ok", "unit_si": "V"},
        ], 0.0, {"count": 4, "valid_count": 4})
        assert list(window._average_trace.values)[-1] == pytest.approx(12.0)
        assert window.average_curve.getData()[1][-2] != window.average_curve.getData()[1][-2]
    finally:
        window._acquiring = False
        window.close()
        app.processEvents()
