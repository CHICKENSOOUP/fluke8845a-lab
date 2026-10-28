"""Background task for curve CSV export."""
from pathlib import Path
from PySide6.QtCore import QObject, QRunnable, Signal
from .curve_export import write_curve_csv

class CurveExportSignals(QObject):
    completed = Signal(str, int, object)

class CurveExportTask(QRunnable):
    def __init__(self, source: Path, destination: Path, window: int):
        super().__init__()
        self.source, self.destination, self.window = source, destination, window
        self.signals = CurveExportSignals()

    def run(self):
        try:
            count = write_curve_csv(self.source, self.destination, self.window)
        except Exception as exc:
            self.signals.completed.emit(str(self.destination), 0, str(exc))
        else:
            self.signals.completed.emit(str(self.destination), count, None)
