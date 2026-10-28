import csv
import os
import time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtTest import QTest
from fluke8845a.gui import MainWindow

def test_curve_button_exports_full_raw_csv_and_average_on_background_worker(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    monkeypatch.setenv("FLUKE8845A_CONFIG_DIR", str(tmp_path / "config"))
    raw = tmp_path / "measurements.csv"
    dest = tmp_path / "curve.csv"
    raw.write_text("sample_index,elapsed_s,value_si,raw_reading,status\n1,0,1,1.000,ok\n2,1,3,3.000,ok\n", encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_kw: (str(dest), ""))
    w = MainWindow()
    try:
        w._experiment_csv_path = raw
        w._plot_x.extend([0., 1.])
        w._plot_y.extend([1., 3.])
        w.average_window_spin.setValue(2)
        w._save_curve_csv()
        deadline = time.monotonic() + 5
        while w._curve_export_task is not None and time.monotonic() < deadline:
            QTest.qWait(10)
        assert w._curve_export_task is None
        with dest.open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        assert list(rows[0]) == ["timestamp_local", "elapsed_s", "value_si", "moving_average_value_si", "unit_si"]
        assert [float(row["moving_average_value_si"]) for row in rows] == [1., 2.]
        with (tmp_path / "curve_detailed.csv").open(encoding="utf-8-sig", newline="") as source:
            detailed = list(csv.DictReader(source))
        assert [row["raw_reading"] for row in detailed] == ["1.000", "3.000"]
    finally:
        w.close()
        app.processEvents()
