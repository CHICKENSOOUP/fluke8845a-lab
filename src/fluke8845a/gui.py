"""Chinese high-DPI desktop interface for the 8845A logger."""

from __future__ import annotations

import bisect
import csv
import json
import logging
import math
import os
import sys
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QObject, QRunnable, QStandardPaths, Qt, QThread, QThreadPool, QTimer,
    Signal, Slot, QPointF, QRectF,
)
from PySide6.QtGui import QAction, QFont, QFontDatabase, QPainter, QPen, QColor, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QStatusBar, QVBoxLayout, QWidget,
    QTabWidget, QToolButton, QStackedLayout,
)

import pyqtgraph as pg

from .analysis import RunningStats, engineering_value, relative_change_percent, summarize
from .modes import DESCRIPTORS, Mode, descriptor, mode_from_scpi_function, range_label
from .mock import MockScenario
from .scpi import (
    FlowControl, ModeSettings, SerialSettings, current_autorange_block_reason,
    unsupported_current_ranges,
)
from .worker import MeasurementWorker

log = logging.getLogger(__name__)
_REGISTERED_UI_FONT_IDS: list[int] = []
AUTO_FOLLOW_WINDOW_S = 120.0
AUTO_FOLLOW_MIN_WINDOW_S = 10.0


def _wheel_zoom_axes(modifiers) -> tuple[bool, bool]:
    if modifiers & Qt.KeyboardModifier.AltModifier:
        return False, True
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        return True, True
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return True, False
    return False, True


class _MeasurementViewBox(pg.ViewBox):
    """Wheel zoom: Y by default, Shift=X, Ctrl=both, Alt=Y around zero."""

    userZoomed = Signal(bool, bool)

    def wheelEvent(self, event) -> None:
        delta = event.delta()
        if not delta:
            event.ignore()
            return
        modifiers = event.modifiers()
        factor = math.exp(-float(delta) / 120.0 * 0.18)
        center = self.mapSceneToView(event.scenePos())
        zoom_x, zoom_y = _wheel_zoom_axes(modifiers)
        x_factor = factor if zoom_x else 1.0
        y_factor = factor if zoom_y else 1.0
        if modifiers & Qt.KeyboardModifier.AltModifier:
            center.setY(0.0)
        self.scaleBy((x_factor, y_factor), center=center)
        self.sigRangeChangedManually.emit([zoom_x, zoom_y])
        self.userZoomed.emit(zoom_x, zoom_y)
        event.accept()


class _RegionStatisticsSignals(QObject):
    completed = Signal(int, object, object)


class _RegionStatisticsTask(QRunnable):
    """Scan a stable CSV prefix off the GUI thread for exact interval statistics."""

    def __init__(self, request_id: int, path: Path, left: float, right: float, mode: Mode) -> None:
        super().__init__()
        self.request_id = request_id
        self.path = path
        self.left = left
        self.right = right
        self.mode = mode
        self.signals = _RegionStatisticsSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @staticmethod
    def _snapshot_lines(stream, byte_count: int):
        remaining = byte_count
        first_line = True
        while remaining > 0:
            raw_line = stream.readline(remaining)
            if not raw_line:
                break
            remaining -= len(raw_line)
            if not raw_line.endswith(b"\n"):
                break
            yield raw_line.decode("utf-8-sig" if first_line else "utf-8")
            first_line = False

    def run(self) -> None:
        result = None
        error = None
        try:
            byte_count = self.path.stat().st_size
            stats = RunningStats()
            last_sample_index = 0
            with self.path.open("rb") as stream:
                reader = csv.DictReader(self._snapshot_lines(stream, byte_count))
                required = {"sample_index", "elapsed_s", "mode", "value_si", "status"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise ValueError("测量CSV缺少区间统计所需列")
                for row in reader:
                    if self._cancelled:
                        raise InterruptedError("区间统计任务已取消")
                    try:
                        sample_index = int(row["sample_index"])
                    except (TypeError, ValueError):
                        continue
                    last_sample_index = max(last_sample_index, sample_index)
                    if row.get("status") != "ok" or row.get("mode") != self.mode.value:
                        continue
                    try:
                        elapsed = float(row["elapsed_s"])
                        value = float(row["value_si"])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(elapsed) and math.isfinite(value) and self.left <= elapsed <= self.right:
                        stats.add(value)
            result = {"stats": stats, "last_sample_index": last_sample_index}
        except Exception as exc:
            error = str(exc)
        self.signals.completed.emit(self.request_id, result, error)


def _ensure_windows_ui_font() -> None:
    """Register the installed YaHei font if a headless Qt plugin omitted system fonts."""
    if os.name != "nt" or "Microsoft YaHei UI" in QFontDatabase.families():
        return
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename in ("msyhl.ttc", "msyh.ttc"):
        font_path = fonts_dir / filename
        if not font_path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            _REGISTERED_UI_FONT_IDS.append(font_id)
        if "Microsoft YaHei UI" in QFontDatabase.families():
            break
MAX_PLOT_POINTS = 100_000


def current_input_guidance(range_si: float | None, autorange: bool) -> str:
    """Map the selected current range to the separate mA and 10 A front jacks."""
    if autorange or range_si is None:
        selection = (
            "自动量程时按最大预期电流选孔：不超过400 mA使用独立的mA/400 mA孔与LO；"
            "可能超过400 mA且不超过10 A时使用10 A孔与LO。只能连接其中一个红色电流孔。"
        )
    elif range_si <= 0.4:
        selection = f"当前固定量程 {range_label(range_si, 'A')}：红表笔接独立的mA/400 mA孔，黑表笔接LO。"
    else:
        selection = f"当前固定量程 {range_label(range_si, 'A')}：红表笔接独立的10 A孔，黑表笔接LO。"
    return (
        selection
        + " 按仪器手册核对屏幕上的mA/10 A输入选择；串口只能回读量程及FRONT/REAR，"
        "不能检测表笔实际插孔。"
    )


class WiringSchematicWidget(QWidget):
    """Compact mode-specific schematics checked against the Chinese user manual."""

    def __init__(
        self, mode: Mode, parent: QWidget | None = None, *,
        current_range_si: float | None = None, autorange: bool = True,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.current_range_si = current_range_si
        self.autorange = autorange
        self.setMinimumSize(860, 275)
        self.setObjectName("wiringSchematic")

    @staticmethod
    def _text(painter: QPainter, rect: QRectF, value: str, color: QColor | None = None) -> None:
        if color is not None:
            painter.setPen(color)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, value)

    @staticmethod
    def _wire(
        painter: QPainter, points: list[QPointF], color: QColor,
        width: float = 2.4, *, dashed: bool = False,
    ) -> None:
        style = Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine
        painter.setPen(QPen(color, width, style, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for first, second in zip(points, points[1:]):
            painter.drawLine(first, second)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        painter.fillRect(self.rect(), QColor("#f3f6fa"))
        gap, outer = 12.0, 14.0
        card_w = (self.width() - 2 * outer - gap) / 2
        card_h = self.height() - 2 * outer
        for index, correct in enumerate((True, False)):
            rect = QRectF(outer + index * (card_w + gap), outer, card_w, card_h)
            color = QColor("#16845b") if correct else QColor("#bf4141")
            painter.setPen(QPen(QColor("#d8dee8"), 1))
            painter.setBrush(QColor("#ffffff"))
            painter.drawRoundedRect(rect, 10, 10)
            title = "✓ 正确接线" if correct else "✕ 错误接线示例"
            self._text(painter, QRectF(rect.left() + 10, rect.top() + 5, rect.width() - 20, 28), title, color)
            painter.setPen(QPen(color, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            area = QRectF(rect.left() + 12, rect.top() + 38, rect.width() - 24, rect.height() - 48)
            self._draw_mode(painter, area, correct, color)
        painter.end()

    def _draw_mode(self, painter: QPainter, area: QRectF, correct: bool, color: QColor) -> None:
        mode = self.mode
        if mode in (Mode.DCV, Mode.ACV):
            self._draw_voltage(painter, area, correct, color)
        elif mode in (Mode.DCI, Mode.ACI):
            self._draw_current(painter, area, correct, color)
        elif mode == Mode.RES2:
            self._draw_resistance(painter, area, correct, color, four_wire=False)
        else:
            self._draw_resistance(painter, area, correct, color, four_wire=True)

    def _draw_two_terminal_meter(self, painter: QPainter, x: float, y: float, w: float, h: float) -> tuple[QPointF, QPointF]:
        box = QRectF(x, y, w, h)
        painter.setPen(QPen(QColor("#526173"), 1.4))
        painter.setBrush(QColor("#edf2f7"))
        painter.drawRoundedRect(box, 6, 6)
        self._text(painter, QRectF(x + 4, y + 5, w - 8, 28), "8845A", QColor("#263445"))
        hi = QPointF(x, y + h * 0.43)
        lo = QPointF(x, y + h * 0.76)
        painter.setBrush(QColor("#ffffff"))
        for point, label in ((hi, "HI  V/Ω"), (lo, "LO")):
            painter.drawEllipse(point, 4.2, 4.2)
            self._text(painter, QRectF(x + 8, point.y() - 10, w - 12, 20), label, QColor("#263445"))
        return hi, lo

    def _draw_voltage(self, painter: QPainter, area: QRectF, correct: bool, color: QColor) -> None:
        x, y, w, h = area.x(), area.y(), area.width(), area.height()
        node = QRectF(x + 4, y + h * 0.30, w * 0.28, h * 0.42)
        painter.setPen(QPen(QColor("#526173"), 1.4))
        painter.setBrush(QColor("#f7f9fc"))
        painter.drawRoundedRect(node, 5, 5)
        self._text(painter, node.adjusted(3, 6, -3, -6), "被测回路\n测量两端", QColor("#263445"))
        meter_x, meter_y, meter_w, meter_h = x + w * 0.67, y + h * 0.23, w * 0.29, h * 0.60
        hi, lo = self._draw_two_terminal_meter(painter, meter_x, meter_y, meter_w, meter_h)
        plus = QPointF(node.right(), node.top() + 12)
        minus = QPointF(node.right(), node.bottom() - 12)
        if correct:
            self._wire(painter, [plus, QPointF(hi.x() - 16, plus.y()), QPointF(hi.x() - 16, hi.y()), hi], color)
            self._wire(painter, [minus, QPointF(lo.x() - 27, minus.y()), QPointF(lo.x() - 27, lo.y()), lo], color)
            self._text(painter, QRectF(x + w * .30, y + h * .73, w * .34, 22), "HI / LO 并联跨接测量点", color)
        else:
            load = QRectF(x + w * .36, y + h * .36, w * .17, h * .30)
            painter.setPen(QPen(QColor("#526173"), 1.4))
            painter.setBrush(QColor("#fff7f4"))
            painter.drawRect(load)
            self._text(painter, load, "负载", QColor("#263445"))
            self._wire(painter, [plus, QPointF(load.left(), plus.y())], color)
            self._wire(painter, [QPointF(load.right(), plus.y()), QPointF(hi.x() - 15, plus.y()), QPointF(hi.x() - 15, hi.y()), hi], color)
            self._wire(painter, [lo, QPointF(lo.x() - 27, lo.y()), QPointF(lo.x() - 27, minus.y()), minus], color)
            self._text(painter, QRectF(x + w * .24, y + h * .73, w * .72, 22), "错误示例：把电压表串入负载回路", color)

    def _draw_current(self, painter: QPainter, area: QRectF, correct: bool, color: QColor) -> None:
        x, y, w, h = area.x(), area.y(), area.width(), area.height()
        source = QRectF(x + 2, y + h * .30, w * .18, h * .42)
        painter.setPen(QPen(QColor("#526173"), 1.4))
        painter.setBrush(QColor("#f7f9fc"))
        painter.drawRoundedRect(source, 5, 5)
        self._text(painter, source.adjusted(3, 4, -3, -4), "被测回路\n+    −", QColor("#263445"))
        meter = QRectF(x + w * .33, y + h * .14, w * .39, h * .68)
        painter.setPen(QPen(QColor("#526173"), 1.4))
        painter.setBrush(QColor("#edf2f7"))
        painter.drawRoundedRect(meter, 6, 6)
        self._text(painter, QRectF(meter.left() + 4, meter.top() + 4, meter.width() - 8, 24), "8845A", QColor("#263445"))
        mA_input = QPointF(meter.left(), meter.top() + meter.height() * .42)
        tenA_input = QPointF(meter.left(), meter.top() + meter.height() * .62)
        low_input = QPointF(meter.left(), meter.top() + meter.height() * .82)
        selected_input = (
            tenA_input if self.current_range_si is not None and self.current_range_si > .4
            else mA_input
        )
        for point in (mA_input, tenA_input, low_input):
            painter.setBrush(QColor("#ffffff"))
            if correct and (point == selected_input or (self.autorange and point in (mA_input, tenA_input))):
                painter.setBrush(QColor("#dff3e9"))
            painter.drawEllipse(point, 4.1, 4.1)
        self._text(painter, QRectF(meter.left() + 7, mA_input.y() - 10, meter.width() - 11, 20), "mA / 400 mA", QColor("#263445"))
        self._text(painter, QRectF(meter.left() + 7, tenA_input.y() - 10, meter.width() - 11, 20), "10 A", QColor("#263445"))
        self._text(painter, QRectF(meter.left() + 7, low_input.y() - 10, meter.width() - 11, 20), "LO", QColor("#263445"))
        load = QRectF(x + w * .81, y + h * .39, w * .16, h * .28)
        painter.setPen(QPen(QColor("#526173"), 1.4))
        painter.setBrush(QColor("#f7f9fc"))
        painter.drawRect(load)
        self._text(painter, load, "负载", QColor("#263445"))
        src_hi, src_lo = QPointF(source.right(), source.top() + 16), QPointF(source.right(), source.bottom() - 16)
        main_input = selected_input if not self.autorange and self.current_range_si is not None else mA_input
        if correct:
            if self.autorange or self.current_range_si is None:
                for port in (mA_input, tenA_input):
                    self._wire(painter, [src_hi, QPointF(port.x() - 12, src_hi.y()), QPointF(port.x() - 12, port.y()), port], color, dashed=True)
            else:
                self._wire(painter, [src_hi, QPointF(main_input.x() - 12, src_hi.y()), QPointF(main_input.x() - 12, main_input.y()), main_input], color)
            self._wire(painter, [low_input, QPointF(load.left() - 12, low_input.y()), QPointF(load.left() - 12, load.center().y()), QPointF(load.left(), load.center().y())], color)
            self._wire(painter, [QPointF(load.right(), load.center().y()), QPointF(load.right() + 8, load.center().y()), QPointF(load.right() + 8, src_lo.y()), src_lo], color)
            if self.autorange or self.current_range_si is None:
                guidance = "串联；≤400 mA 接 mA/LO，>400 mA 接 10 A/LO；二选一"
            elif self.current_range_si <= .4:
                guidance = "串联；当前量程使用 mA/400 mA 与 LO 两个孔"
            else:
                guidance = "串联；当前量程使用 10 A 与 LO 两个孔"
            self._text(painter, QRectF(x + 1, y + h * .82, w - 2, 22), guidance, color)
        else:
            self._wire(painter, [src_hi, QPointF(load.left() - 10, src_hi.y()), QPointF(load.left() - 10, load.center().y()), QPointF(load.left(), load.center().y())], color)
            self._wire(painter, [QPointF(load.right(), load.center().y()), QPointF(load.right() + 8, load.center().y()), QPointF(load.right() + 8, src_lo.y()), src_lo], color)
            self._wire(painter, [src_hi, QPointF(main_input.x() - 22, src_hi.y()), QPointF(main_input.x() - 22, main_input.y()), main_input], color)
            self._wire(painter, [low_input, QPointF(low_input.x() + 22, low_input.y()), QPointF(low_input.x() + 22, src_lo.y()), src_lo], color)
            self._text(painter, QRectF(x + 1, y + h * .82, w - 2, 22), "危险：任一红色电流孔并联在电压源两端", color)

    def _draw_resistance(self, painter: QPainter, area: QRectF, correct: bool, color: QColor, *, four_wire: bool) -> None:
        x, y, w, h = area.x(), area.y(), area.width(), area.height()
        resistor = QRectF(x + 8, y + h * .40, w * .27, h * .25)
        painter.setPen(QPen(QColor("#526173"), 1.4))
        painter.setBrush(QColor("#f7f9fc"))
        painter.drawRoundedRect(resistor, 5, 5)
        self._text(painter, resistor, "被测电阻", QColor("#263445"))
        terminal_x = resistor.right()
        high = QPointF(terminal_x, resistor.top() + 8)
        low = QPointF(terminal_x, resistor.bottom() - 8)
        if four_wire:
            meter = QRectF(x + w * .58, y + h * .19, w * .38, h * .66)
            painter.setPen(QPen(QColor("#526173"), 1.4))
            painter.setBrush(QColor("#edf2f7"))
            painter.drawRoundedRect(meter, 6, 6)
            self._text(painter, QRectF(meter.left() + 4, meter.top() + 3, meter.width() - 8, 25), "8845A  ·  4W", QColor("#263445"))
            rows = [meter.top() + 38 + i * (meter.height() - 48) / 3 for i in range(4)]
            labels = ("FORCE HI", "SENSE HI", "FORCE LO", "SENSE LO")
            points = []
            for yy, label in zip(rows, labels):
                pt = QPointF(meter.left(), yy)
                points.append(pt)
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(pt, 3.8, 3.8)
                self._text(painter, QRectF(meter.left() + 6, yy - 10, meter.width() - 10, 18), label, QColor("#263445"))
            if correct:
                self._wire(painter, [high, QPointF(points[0].x() - 17, high.y()), QPointF(points[0].x() - 17, points[0].y()), points[0]], color)
                self._wire(painter, [high, QPointF(points[1].x() - 28, high.y()), QPointF(points[1].x() - 28, points[1].y()), points[1]], color)
                self._wire(painter, [low, QPointF(points[2].x() - 17, low.y()), QPointF(points[2].x() - 17, points[2].y()), points[2]], color)
                self._wire(painter, [low, QPointF(points[3].x() - 28, low.y()), QPointF(points[3].x() - 28, points[3].y()), points[3]], color)
                self._text(painter, QRectF(x + 2, y + h * .80, w * .54, 22), "HI、LO 两侧各接 FORCE + SENSE", color)
            else:
                self._wire(painter, [high, QPointF(points[0].x() - 17, high.y()), QPointF(points[0].x() - 17, points[0].y()), points[0]], color)
                self._wire(painter, [high, QPointF(points[3].x() - 28, high.y()), QPointF(points[3].x() - 28, points[3].y()), points[3]], color)
                self._wire(painter, [low, QPointF(points[2].x() - 17, low.y()), QPointF(points[2].x() - 17, points[2].y()), points[2]], color)
                self._wire(painter, [low, QPointF(points[1].x() - 28, low.y()), QPointF(points[1].x() - 28, points[1].y()), points[1]], color)
                self._text(painter, QRectF(x + 2, y + h * .86, w * .96, 20), "错误示例：SENSE HI / LO 接反", color)
        else:
            hi, lo = self._draw_two_terminal_meter(painter, x + w * .67, y + h * .23, w * .29, h * .60)
            if correct:
                self._wire(painter, [high, QPointF(hi.x() - 18, high.y()), QPointF(hi.x() - 18, hi.y()), hi], color)
                self._wire(painter, [low, QPointF(lo.x() - 26, low.y()), QPointF(lo.x() - 26, lo.y()), lo], color)
                self._text(painter, QRectF(x + w * .32, y + h * .72, w * .63, 25), "断电后用 HI / LO 两线跨接电阻", color)
            else:
                self._wire(painter, [high, QPointF(hi.x() - 18, high.y()), QPointF(hi.x() - 18, hi.y()), hi], color)
                self._wire(painter, [low, QPointF(lo.x() - 26, low.y()), QPointF(lo.x() - 26, lo.y()), lo], color)
                source = QRectF(x + w * .36, y + h * .02, w * .25, h * .22)
                painter.setPen(QPen(color, 1.4))
                painter.setBrush(QColor("#fff7f4"))
                painter.drawRoundedRect(source, 4, 4)
                self._text(painter, source, "外加电源\n带电", color)
                self._wire(painter, [QPointF(source.left() + 8, source.bottom()), QPointF(source.left() + 8, high.y() - 8), high], color)
                self._wire(painter, [QPointF(source.right() - 8, source.bottom()), QPointF(source.right() - 8, low.y() + 8), low], color)
                self._text(painter, QRectF(x + w * .29, y + h * .72, w * .69, 25), "危险示例：电阻仍接在带电回路中", color)


class WiringConfirmationDialog(QDialog):
    def __init__(
        self, mode: Mode, instructions: str, parent: QWidget | None = None, *,
        current_range_si: float | None = None, autorange: bool = True,
    ) -> None:
        super().__init__(parent)
        self.instructions = instructions
        self.setWindowTitle("确认测量接线")
        self.setModal(True)
        self.resize(1040, 475)
        layout = QVBoxLayout(self)
        message = QLabel(instructions)
        message.setWordWrap(True)
        layout.addWidget(message)
        self.schematic = WiringSchematicWidget(
            mode, self, current_range_si=current_range_si, autorange=autorange,
        )
        layout.addWidget(self.schematic, 1)
        note = QLabel("示意图依据 Fluke 8845A《Users Manual》中文本第 4-4、4-9 至 4-11 页整理。SCPI可回读量程与FRONT/REAR，不能确认表笔接入哪个电流孔，也不能检测保险丝或外部回路。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #687386;")
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("我已断电并核对接线")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class WorkerBridge(QObject):
    connect_real = Signal(str, object, object, bool)
    connect_mock = Signal(object, object)
    configure = Signal(object)
    start_experiment = Signal(object, str, str, object, bool)
    set_sampling_interval = Signal(object)
    set_measurement_speed = Signal(object)
    pause = Signal()
    resume = Signal()
    stop = Signal()
    mark_event = Signal(str)
    zero = Signal()
    diagnose = Signal()
    shutdown = Signal()
    close_app = Signal()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Fluke 8845A 科研测量与长期记录")
        self.resize(1540, 960)
        self.setMinimumSize(1040, 700)
        _ensure_windows_ui_font()
        self.setFont(QFont("Microsoft YaHei UI", 10))
        config_override = os.environ.get("FLUKE8845A_CONFIG_DIR")
        self._app_config_dir = Path(config_override) if config_override else Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
        self._config_fallback = False
        try:
            self._app_config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._app_config_dir = Path(tempfile.gettempdir()) / "Fluke8845A" / "user_config"
            self._app_config_dir.mkdir(parents=True, exist_ok=True)
            self._config_fallback = True
        self._profile_path = self._app_config_dir / "profiles.json"
        self._profiles = self._load_profiles()
        self._current_mode = Mode.DCV
        self._connected = False
        self._simulation = False
        self._instrument_mode_known = False
        self._active_settings_by_mode: dict[str, dict] = {}
        self._firmware = ""
        self._terminals = ""
        self._loading_mode_controls = False
        self._acquiring = False
        self._paused = False
        self._speed_update_pending = False
        self._experiment_active = False
        self._last_value: float | None = None
        self._baseline: float | None = None
        self._plot_x: deque[float] = deque(maxlen=MAX_PLOT_POINTS)
        self._plot_y: deque[float] = deque(maxlen=MAX_PLOT_POINTS)
        self._experiment_csv_path: Path | None = None
        self._region_stats_request_id = 0
        self._region_stats_tasks: dict[int, _RegionStatisticsTask] = {}
        self._region_stats_scanning = False
        self._region_stats_pending_rows: list[dict] = []
        self._region_stats_values: RunningStats | None = None
        self._region_stats_last_sample_index = 0
        self._region_stats_bounds: tuple[float, float] | None = None
        self._event_lines: list = []
        self._event_labels: list = []
        self._last_elapsed = 0.0
        self._global_stats = {"count": 0, "minimum": None, "maximum": None, "mean": None, "sample_stddev": None}
        self._crosshair_x = None
        self._crosshair_y = None
        self._shutdown_started = False
        self._shutdown_finished = False

        self._worker_thread = QThread(self)
        self._worker_thread.finished.connect(self._on_worker_thread_finished)
        self._worker = MeasurementWorker()
        self._worker.moveToThread(self._worker_thread)
        self._bridge = WorkerBridge(self)
        self._wire_worker()
        self._build_ui()
        self._apply_style()
        self._restore_serial_settings()
        self._refresh_ports()
        self._load_mode_controls(self._current_mode)
        self._worker_thread.start()
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_elapsed_label)
        self._clock_timer.start(250)
        self.statusBar().showMessage("未连接。请确认仪器处于RS-232 / COMPUTER模式。")
        if self._config_fallback:
            self.statusBar().showMessage("用户配置目录不可写；参数暂存于临时目录。检查权限后重启可恢复持久化。", 15000)

    def _wire_worker(self) -> None:
        self._bridge.connect_real.connect(self._worker.connect_real)
        self._bridge.connect_mock.connect(self._worker.connect_mock)
        self._bridge.configure.connect(self._worker.configure_mode)
        self._bridge.start_experiment.connect(self._worker.start_experiment)
        self._bridge.set_sampling_interval.connect(self._worker.set_target_interval)
        self._bridge.set_measurement_speed.connect(self._worker.set_measurement_speed)
        self._bridge.pause.connect(self._worker.pause)
        self._bridge.resume.connect(self._worker.resume)
        self._bridge.stop.connect(self._worker.stop)
        self._bridge.mark_event.connect(self._worker.mark_event)
        self._bridge.zero.connect(self._worker.zero)
        self._bridge.diagnose.connect(self._worker.diagnose)
        self._bridge.shutdown.connect(self._worker.shutdown)
        self._bridge.close_app.connect(self._worker.shutdown_app)
        self._worker.connected_changed.connect(self._on_connected)
        self._worker.status_changed.connect(self._on_status)
        self._worker.log_line.connect(self._append_log)
        self._worker.scpi_logs_ready.connect(self._on_scpi_logs_ready)
        self._worker.configuration_ready.connect(self._on_configuration)
        self._worker.active_settings_ready.connect(self._on_active_settings)
        self._worker.sample_ready.connect(self._on_sample_ready)
        self._worker.experiment_started.connect(self._on_experiment_started)
        self._worker.experiment_finished.connect(self._on_experiment_finished)
        self._worker.diagnostic_ready.connect(self._show_diagnostic)
        self._worker.error.connect(self._show_error)
        self._worker.measurement_speed_result.connect(self._on_measurement_speed_result)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 12)
        root_layout.setSpacing(10)
        self.setCentralWidget(root)

        header = QFrame()
        header.setObjectName("appHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 6, 14, 6)
        header_layout.setSpacing(10)
        self.brand_icon = QLabel()
        self.brand_icon.setObjectName("brandIcon")
        self.brand_icon.setFixedSize(52, 38)
        icon_candidates = (
            Path(__file__).resolve().parents[2] / "assets" / "fluke8845a_app_icon_v3.png",
            Path(sys.argv[0]).resolve().parent / "assets" / "fluke8845a_app_icon_v3.png",
        )
        icon_path = next((path for path in icon_candidates if path.is_file()), None)
        brand_pixmap = QPixmap(str(icon_path)) if icon_path is not None else QPixmap()
        if not brand_pixmap.isNull():
            # Trim transparent margins so the instrument mark reads clearly at toolbar size.
            crop = brand_pixmap.copy(
                20, 255, max(1, brand_pixmap.width() - 40),
                min(770, max(1, brand_pixmap.height() - 255)),
            )
            self.brand_icon.setPixmap(crop.scaled(
                self.brand_icon.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            self.brand_icon.setText("8845A")
        title = QLabel("Fluke 8845A")
        title.setObjectName("mainTitle")
        title_stack = QVBoxLayout()
        title_stack.setSpacing(0)
        title_stack.addWidget(title)
        header_layout.addWidget(self.brand_icon)
        header_layout.addLayout(title_stack)
        header_layout.addStretch(1)
        self.connection_badge = QLabel("未连接")
        self.connection_badge.setObjectName("connectionBadge")
        self.connection_badge.setMinimumHeight(32)
        header_layout.addWidget(self.connection_badge)
        root_layout.addWidget(header)

        setup_panel = self._build_setup_panel()
        chart_panel = self._build_chart_panel()
        root_layout.addWidget(chart_panel, 1)

        self.configuration_dialog = QDialog(self)
        self.configuration_dialog.setObjectName("configurationDialog")
        self.configuration_dialog.setWindowTitle("仪器与实验设置")
        self.configuration_dialog.resize(1020, 780)
        dialog_layout = QVBoxLayout(self.configuration_dialog)
        dialog_layout.setContentsMargins(16, 12, 16, 14)
        dialog_layout.setSpacing(10)
        dialog_heading = QHBoxLayout()
        dialog_title = QLabel("仪器与实验设置")
        dialog_title.setObjectName("dialogTitle")
        dialog_heading.addWidget(dialog_title)
        dialog_heading.addStretch(1)
        close_settings = QPushButton("完成")
        close_settings.setObjectName("secondaryButton")
        close_settings.clicked.connect(self.configuration_dialog.hide)
        dialog_heading.addWidget(close_settings)
        dialog_layout.addLayout(dialog_heading)
        self.configuration_tabs = QTabWidget()
        self.configuration_tabs.setObjectName("configurationTabs")
        setup_scroll = QScrollArea()
        setup_scroll.setObjectName("configurationScrollArea")
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.Shape.NoFrame)
        setup_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        setup_scroll.setWidget(setup_panel)
        self.setup_scroll_area = setup_scroll
        self.configuration_tabs.addTab(setup_scroll, "连接与采集")
        self.configuration_tabs.addTab(self.analysis_tabs, "分析与事件")
        dialog_layout.addWidget(self.configuration_tabs, 1)

        self.setStatusBar(QStatusBar(self))
        self.configuration_button = QPushButton("设置与分析…")
        self.configuration_button.setObjectName("statusAction")
        self.configuration_button.setToolTip("打开串口、测量、实验、分析及日志设置")
        self.configuration_button.clicked.connect(self._show_configuration)
        self.statusBar().addPermanentWidget(self.configuration_button)
        self.statusBar().setSizeGripEnabled(False)

        self._build_log_dock()
        self._build_menus()
        self.log_dock.toggleViewAction().setText("显示通信日志面板")

    def _show_configuration(self) -> None:
        self.configuration_dialog.show()
        self.configuration_dialog.raise_()
        self.configuration_dialog.activateWindow()

    def _build_setup_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("setupPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)
        panel_title = QLabel("实验设置")
        panel_title.setObjectName("panelTitle")
        panel_subtitle = QLabel("连接仪器、选择测量条件并管理实验")
        panel_subtitle.setObjectName("secondaryText")
        layout.addWidget(panel_title)
        layout.addWidget(panel_subtitle)

        self.setup_tabs = QTabWidget()
        self.setup_tabs.setObjectName("setupTabs")
        self.setup_tabs.setDocumentMode(True)
        instrument_page = QWidget()
        instrument_layout = QVBoxLayout(instrument_page)
        instrument_layout.setContentsMargins(5, 9, 5, 5)
        instrument_layout.setSpacing(7)
        experiment_page = QWidget()
        experiment_layout = QVBoxLayout(experiment_page)
        experiment_layout.setContentsMargins(5, 9, 5, 5)
        experiment_layout.setSpacing(7)

        connection = QGroupBox("仪器连接")
        connection.setObjectName("sidebarCard")
        form = QFormLayout(connection)
        form.setContentsMargins(12, 18, 12, 12)
        form.setHorizontalSpacing(9)
        form.setVerticalSpacing(7)
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.port_combo.setMinimumContentsLength(14)
        self.port_combo.setToolTip("选择COM端口；悬停可查看完整设备名称")
        self.scan_button = QPushButton("扫描")
        self.scan_button.setObjectName("secondaryButton")
        ports = QHBoxLayout()
        ports.setSpacing(6)
        ports.addWidget(self.port_combo, 1)
        ports.addWidget(self.scan_button)
        form.addRow("COM端口", ports)

        self.connect_button = QPushButton("连接真实仪器")
        self.connect_button.setObjectName("primaryButton")
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setObjectName("secondaryButton")
        self.disconnect_button.setEnabled(False)
        self.connect_button.clicked.connect(self._connect_action)
        self.disconnect_button.clicked.connect(self._disconnect)
        self.scan_button.clicked.connect(self._refresh_ports)
        link_row = QHBoxLayout()
        link_row.setSpacing(6)
        link_row.addWidget(self.connect_button, 1)
        link_row.addWidget(self.disconnect_button)
        form.addRow("", link_row)
        self.idn_label = QLabel("设备身份：未读取")
        self.idn_label.setWordWrap(True)
        self.idn_label.setObjectName("secondaryText")
        form.addRow("识别结果", self.idn_label)

        self.serial_advanced_button = QToolButton()
        self.serial_advanced_button.setObjectName("disclosureButton")
        self.serial_advanced_button.setText("串口高级参数")
        self.serial_advanced_button.setCheckable(True)
        self.serial_advanced_button.setChecked(False)
        self.serial_advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        self.serial_advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        form.addRow("", self.serial_advanced_button)
        advanced = QWidget()
        advanced.setObjectName("advancedSettings")
        advanced_form = QFormLayout(advanced)
        advanced_form.setContentsMargins(2, 3, 2, 2)
        advanced_form.setHorizontalSpacing(8)
        advanced_form.setVerticalSpacing(6)
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"])
        advanced_form.addRow("波特率", self.baud_combo)
        self.data_bits_combo = QComboBox()
        self.data_bits_combo.addItems(["8", "7"])
        self.parity_combo = QComboBox()
        self.parity_combo.addItem("无", "N")
        self.parity_combo.addItem("偶校验", "E")
        self.parity_combo.addItem("奇校验", "O")
        bits_row = QHBoxLayout()
        bits_row.setSpacing(5)
        bits_row.addWidget(self.data_bits_combo)
        bits_row.addWidget(QLabel("校验"))
        bits_row.addWidget(self.parity_combo)
        advanced_form.addRow("数据位", bits_row)
        self.stop_bits_combo = QComboBox()
        self.stop_bits_combo.addItems(["1", "2"])
        self.flow_combo = QComboBox()
        self.flow_combo.addItem("无", FlowControl.NONE.value)
        self.flow_combo.addItem("软件 XON/XOFF", FlowControl.XONXOFF.value)
        self.flow_combo.addItem("硬件 RTS/CTS", FlowControl.RTSCTS.value)
        stop_flow = QHBoxLayout()
        stop_flow.setSpacing(5)
        stop_flow.addWidget(self.stop_bits_combo)
        stop_flow.addWidget(QLabel("流控"))
        stop_flow.addWidget(self.flow_combo)
        advanced_form.addRow("停止位", stop_flow)
        self.eol_combo = QComboBox()
        for label, data in (("CR", "CR"), ("LF", "LF"), ("CRLF", "CRLF")):
            self.eol_combo.addItem(label, data)
        advanced_form.addRow("命令结束符", self.eol_combo)
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0.1, 30)
        self.timeout_spin.setSingleStep(0.5)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setValue(2.0)
        self.timeout_spin.setToolTip("普通SCPI命令的等待上限。READ?会按当前积分/滤波设置自动延长，防止慢速测量被过早判超时。")
        advanced_form.addRow("响应超时", self.timeout_spin)
        advanced.setVisible(False)
        form.addRow("", advanced)

        def toggle_serial_advanced(checked: bool) -> None:
            advanced.setVisible(checked)
            self.serial_advanced_button.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )

        self.serial_advanced_button.toggled.connect(toggle_serial_advanced)
        instrument_layout.addWidget(connection)

        mode_box = QGroupBox("测量设置")
        mode_box.setObjectName("sidebarCard")
        mode_form = QFormLayout(mode_box)
        mode_form.setContentsMargins(12, 18, 12, 12)
        mode_form.setHorizontalSpacing(9)
        mode_form.setVerticalSpacing(7)
        self.mode_combo = QComboBox()
        for mode, spec in DESCRIPTORS.items():
            self.mode_combo.addItem(spec.label, mode.value)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_form.addRow("测量模式", self.mode_combo)

        self.autorange_check = QCheckBox("自动量程")
        self.autorange_check.setChecked(True)
        self.autorange_check.toggled.connect(self._update_range_enabled)
        self.autorange_check.toggled.connect(self._settings_changed)
        self.range_combo = QComboBox()
        self.range_combo.currentIndexChanged.connect(self._settings_changed)
        range_row = QHBoxLayout()
        range_row.setSpacing(6)
        range_row.addWidget(self.autorange_check)
        range_row.addWidget(self.range_combo, 1)
        mode_form.addRow("量程", range_row)

        self.nplc_combo = QComboBox()
        self.nplc_combo.currentIndexChanged.connect(self._settings_changed)
        self.nplc_row_label = QLabel("积分时间 / NPLC")
        self.nplc_combo.setToolTip(
            "适用于直流电压、电流和电阻。NPLC越低，单次转换通常越快；NPLC越高，积分时间越长，"
            "通常更利于抑制工频噪声。连续采集中可选择后应用采集速度。"
        )
        mode_form.addRow(self.nplc_row_label, self.nplc_combo)
        self.bandwidth_combo = QComboBox()
        self.bandwidth_combo.currentIndexChanged.connect(self._settings_changed)
        self.bandwidth_row_label = QLabel("AC 带宽")
        self.bandwidth_combo.setToolTip(
            "适用于交流电压和交流电流。带宽档位影响响应与噪声抑制，不等于每秒读数次数。"
        )
        mode_form.addRow(self.bandwidth_row_label, self.bandwidth_combo)
        self.analog_filter_check = QCheckBox("启用直流模拟滤波器")
        self.analog_filter_check.toggled.connect(self._settings_changed)
        mode_form.addRow("模拟滤波", self.analog_filter_check)
        self.autozero_check = QCheckBox("自动零点校正")
        self.autozero_check.setChecked(True)
        self.autozero_check.toggled.connect(self._settings_changed)
        mode_form.addRow("零点校正", self.autozero_check)

        self.apply_button = QPushButton("应用仪器参数")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self._apply_settings)
        self.apply_speed_button = QPushButton("应用采集速度")
        self.apply_speed_button.setObjectName("secondaryButton")
        self.apply_speed_button.setToolTip(
            "连续采集或暂停时，把所选NPLC/交流带宽写入仪表并回读确认；从下一个读数起生效。"
            "不会更改测量模式、量程、滤波或接线端子。"
        )
        self.apply_speed_button.clicked.connect(self._apply_acquisition_speed)
        mode_form.addRow("", self.apply_button)
        mode_form.addRow("仪器速度", self.apply_speed_button)
        self.zero_button = QPushButton("执行 ZERO 校正")
        self.zero_button.setObjectName("secondaryButton")
        self.zero_button.clicked.connect(self._request_zero)
        mode_form.addRow("", self.zero_button)
        self.safety_text = QLabel()
        self.safety_text.setObjectName("safetyBox")
        self.safety_text.setWordWrap(True)
        mode_form.addRow("接线提示", self.safety_text)
        instrument_layout.addWidget(mode_box)

        experiment = QGroupBox("实验文件")
        experiment.setObjectName("sidebarCard")
        exp_form = QFormLayout(experiment)
        exp_form.setContentsMargins(12, 18, 12, 12)
        exp_form.setHorizontalSpacing(9)
        exp_form.setVerticalSpacing(7)
        self.experiment_name = QLineEdit(f"实验_{datetime.now().strftime('%Y%m%d_%H%M')}")
        exp_form.addRow("实验名称", self.experiment_name)
        default_root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "Fluke8845A_Experiments"
        self.output_root = QLineEdit(str(default_root))
        browse = QPushButton("选择")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self._choose_output_root)
        root_row = QHBoxLayout()
        root_row.setSpacing(6)
        root_row.addWidget(self.output_root, 1)
        root_row.addWidget(browse)
        exp_form.addRow("保存目录", root_row)
        self.mock_mode_check = QCheckBox("使用明确标记的 MOCK 模拟仪器")
        self.mock_mode_check.setObjectName("mockCheck")
        self.mock_mode_check.toggled.connect(self._update_connect_label)
        exp_form.addRow("验证模式", self.mock_mode_check)
        self.scenario_combo = QComboBox()
        for label in ("阶跃响应与恢复", "长期漂移", "通信超时测试", "断线恢复测试"):
            self.scenario_combo.addItem(label)
        self.scenario_combo.setEnabled(False)
        exp_form.addRow("Mock场景", self.scenario_combo)
        experiment_layout.addWidget(experiment)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0, 86400)
        self.interval_spin.setDecimals(3)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(1.0)
        self.interval_spin.setToolTip(
            "软件目标采样间隔，与仪器测量速度独立。采集时可即时修改；0 表示每次读数后不额外等待，"
            "实际频率仍受仪器转换与串口限制。"
        )
        self.interval_spin.valueChanged.connect(self._on_sampling_interval_changed)
        interval_row = QHBoxLayout()
        interval_row.setContentsMargins(0, 0, 0, 0)
        interval_row.addWidget(QLabel("目标采样间隔"))
        self.interval_spin.setMinimumWidth(110)
        interval_row.addWidget(self.interval_spin)
        interval_row.addWidget(QLabel("0 = 最快可用"))
        interval_row.addStretch(1)
        experiment_layout.addLayout(interval_row)

        self.start_button = QPushButton("开始连续采集")
        self.start_button.setObjectName("primaryButton")
        self.single_button = QPushButton("单次测量")
        self.single_button.setObjectName("secondaryButton")
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("secondaryButton")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self._start_continuous)
        self.single_button.clicked.connect(self._start_single)
        self.pause_button.clicked.connect(self._pause_or_resume)
        self.stop_button.clicked.connect(self._stop)
        self.experiment_status = QLabel("就绪")
        self.experiment_status.setObjectName("statusValue")
        self.actual_rate_label = QLabel("— Hz")
        self.actual_rate_label.setObjectName("metricValue")
        self.actual_rate_label.setToolTip("依据连续有效读数的PC单调时钟时间戳计算，不由目标间隔推算。")

        instrument_layout.addStretch(1)
        experiment_layout.addStretch(1)
        self.setup_tabs.addTab(instrument_page, "仪器")
        self.setup_tabs.addTab(experiment_page, "实验")
        layout.addWidget(self.setup_tabs, 1)

        self.log_box = QLineEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setPlaceholderText("SCPI收发与运行诊断记录显示在日志面板中。")
        return panel

    def _build_chart_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("measurementWorkspace")
        self.measurement_workspace = panel
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self._chart_panel_layout = layout

        reading_card = QFrame()
        reading_card.setObjectName("readingCard")
        self._reading_card = reading_card
        reading_layout = QHBoxLayout(reading_card)
        reading_layout.setContentsMargins(18, 14, 18, 14)
        reading_layout.setSpacing(16)
        reading_text = QVBoxLayout()
        self._reading_text_layout = reading_text
        reading_text.setSpacing(3)
        reading_kicker = QLabel("实时读数")
        self._reading_kicker = reading_kicker
        reading_kicker.setObjectName("eyebrow")
        self.reading_mode_label = QLabel("直流电压 DCV · 自动量程 · 1 NPLC")
        self.reading_mode_label.setObjectName("readingMode")
        self.latest_value_label = QLabel("等待读数")
        self.latest_value_label.setObjectName("readingValue")
        self.latest_value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        reading_text.addWidget(reading_kicker)
        reading_text.addWidget(self.reading_mode_label)
        reading_text.addWidget(self.latest_value_label)
        reading_layout.addLayout(reading_text, 3)

        self._reading_divider = QFrame()
        self._reading_divider.setObjectName("cardDivider")
        self._reading_divider.setFrameShape(QFrame.Shape.VLine)
        reading_layout.addWidget(self._reading_divider)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(8)
        self._status_caption = QLabel("采集状态")
        self._status_caption.setObjectName("eyebrow")
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self._rate_caption = QLabel("实测频率")
        self._rate_caption.setObjectName("eyebrow")
        self._elapsed_caption = QLabel("实验历时")
        self._elapsed_caption.setObjectName("eyebrow")
        self.elapsed_label = QLabel("00:00:00")
        self.elapsed_label.setObjectName("metricValue")

        status_tile = QFrame()
        status_tile.setObjectName("metricCell")
        status_tile.setMinimumWidth(132)
        status_layout = QVBoxLayout(status_tile)
        status_layout.setContentsMargins(10, 7, 10, 7)
        status_layout.setSpacing(3)
        status_layout.addWidget(self._status_caption)
        status_value_row = QHBoxLayout()
        status_value_row.setSpacing(6)
        status_value_row.addWidget(self.status_dot)
        status_value_row.addWidget(self.experiment_status, 1)
        status_layout.addLayout(status_value_row)
        metrics_row.addWidget(status_tile, 1)

        rate_tile = QFrame()
        rate_tile.setObjectName("metricCell")
        rate_tile.setMinimumWidth(104)
        rate_layout = QVBoxLayout(rate_tile)
        rate_layout.setContentsMargins(10, 7, 10, 7)
        rate_layout.setSpacing(3)
        rate_layout.addWidget(self._rate_caption)
        rate_layout.addWidget(self.actual_rate_label)
        metrics_row.addWidget(rate_tile, 1)

        elapsed_tile = QFrame()
        elapsed_tile.setObjectName("metricCell")
        elapsed_tile.setMinimumWidth(112)
        elapsed_layout = QVBoxLayout(elapsed_tile)
        elapsed_layout.setContentsMargins(10, 7, 10, 7)
        elapsed_layout.setSpacing(3)
        elapsed_layout.addWidget(self._elapsed_caption)
        elapsed_layout.addWidget(self.elapsed_label)
        metrics_row.addWidget(elapsed_tile, 1)
        reading_layout.addLayout(metrics_row, 2)
        layout.addWidget(reading_card)
        self._reading_layout = reading_layout
        self._metrics_layout = metrics_row
        self._reading_layout_mode = None
        self._short_chart_layout = None

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QHBoxLayout(action_bar)
        self._action_layout = action_layout
        action_layout.setContentsMargins(10, 7, 10, 7)
        action_layout.setSpacing(8)
        self.start_button.setMinimumWidth(124)
        self.single_button.setMinimumWidth(80)
        self.pause_button.setMinimumWidth(64)
        self.stop_button.setMinimumWidth(64)
        action_layout.addWidget(self.start_button)
        action_layout.addWidget(self.single_button)
        action_layout.addWidget(self.pause_button)
        action_layout.addWidget(self.stop_button)
        action_divider = QFrame()
        action_divider.setObjectName("toolbarDivider")
        action_divider.setFrameShape(QFrame.Shape.VLine)
        action_layout.addWidget(action_divider)
        action_layout.addStretch(1)
        layout.addWidget(action_bar)

        plot_toolbar = QFrame()
        plot_toolbar.setObjectName("plotToolbar")
        plot_toolbar_layout = QHBoxLayout(plot_toolbar)
        plot_toolbar_layout.setContentsMargins(2, 0, 2, 0)
        plot_toolbar_layout.setSpacing(8)
        self.follow_check = QCheckBox("自动跟随")
        self.follow_check.setChecked(True)
        self.follow_check.setToolTip("开启后视图随最新读数滚动；关闭后可以固定查看历史区间")
        plot_toolbar_layout.addWidget(self.follow_check)
        self.acquisition_hint = QLabel("滚轮：纵轴  ·  Shift：时间  ·  Ctrl：双轴  ·  Alt：以零点缩放")
        self.acquisition_hint.setObjectName("secondaryText")
        plot_toolbar_layout.addWidget(self.acquisition_hint)
        plot_toolbar_layout.addStretch(1)
        self.autoscale_button = QPushButton("自动缩放")
        self.autoscale_button.setObjectName("secondaryButton")
        self.autoscale_button.setToolTip("自动缩放坐标轴以显示曲线数据")
        self.autoscale_button.clicked.connect(self._autoscale_plot)
        plot_toolbar_layout.addWidget(self.autoscale_button)
        self.history_button = QPushButton("加载 CSV")
        self.history_button.setObjectName("secondaryButton")
        self.history_button.clicked.connect(self._load_history)
        plot_toolbar_layout.addWidget(self.history_button)
        self.screenshot_button = QPushButton("曲线截图")
        self.screenshot_button.setObjectName("secondaryButton")
        self.screenshot_button.clicked.connect(self._save_screenshot)
        plot_toolbar_layout.addWidget(self.screenshot_button)
        layout.addWidget(plot_toolbar)

        plot_frame = QFrame()
        plot_frame.setObjectName("plotCard")
        self._plot_frame = plot_frame
        plot_layout = QStackedLayout(plot_frame)
        plot_layout.setContentsMargins(4, 4, 4, 4)
        plot_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.view_box = _MeasurementViewBox()
        self.plot_widget = pg.PlotWidget(viewBox=self.view_box)
        self.plot_widget.setObjectName("trendPlot")
        self.plot_widget.setMinimumHeight(230)
        self.plot_widget.setBackground("#fbf9f1")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.11)
        self.plot_widget.setLabel("bottom", "实验经过时间", units="s", color="#697470")
        self.plot_widget.setLabel("left", "测量值", color="#697470")
        legend = self.plot_widget.addLegend(offset=(12, 8))
        legend.setLabelTextColor("#526064")
        self.curve = self.plot_widget.plot([], [], pen=pg.mkPen("#138b92", width=2.2), name="原始读数")
        plot_item = self.plot_widget.getPlotItem()
        for axis_name in ("left", "bottom"):
            axis = plot_item.getAxis(axis_name)
            axis.setTextPen(pg.mkPen("#526064"))
            axis.setPen(pg.mkPen("#96a09a"))
            axis.setTickFont(QFont("Microsoft YaHei UI", 10))
        plot_item.vb.sigRangeChanged.connect(self._position_event_labels)
        self.view_box.userZoomed.connect(self._on_plot_wheel_zoom)
        plot_item.setMenuEnabled(False)
        plot_item.vb.setMouseEnabled(x=True, y=True)
        plot_item.setClipToView(True)
        self.plot_widget.setXRange(0, 10, padding=0)
        plot_layout.addWidget(self.plot_widget)
        empty_overlay = QWidget()
        empty_overlay.setObjectName("plotEmptyOverlay")
        empty_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        empty_layout = QVBoxLayout(empty_overlay)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.addStretch(2)
        self.plot_empty_title = QLabel("等待采样")
        self.plot_empty_title.setObjectName("plotEmptyTitle")
        self.plot_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.plot_empty_title)
        self.plot_empty_hint = QLabel("连接 Fluke 8845A 后开始单次测量或连续采集")
        self.plot_empty_hint.setObjectName("plotEmptyHint")
        self.plot_empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.plot_empty_hint)
        empty_layout.addStretch(3)
        plot_layout.addWidget(empty_overlay)
        self.plot_empty_overlay = empty_overlay
        self.plot_empty_overlay.raise_()
        layout.addWidget(plot_frame, 1)

        self.baseline_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#d97706", width=1.5, style=Qt.PenStyle.DashLine),
        )
        self.plot_widget.addItem(self.baseline_line, ignoreBounds=True)
        self.baseline_line.hide()
        self._region = pg.LinearRegionItem(
            values=(0, 60), brush=pg.mkBrush(14, 116, 144, 24), movable=True
        )
        self._region.setZValue(-10)
        self.plot_widget.addItem(self._region)
        self._region.hide()
        self.region_check = QCheckBox("显示局部统计区间")
        self.region_check.toggled.connect(self._on_region_visibility_changed)
        self._region.sigRegionChangeFinished.connect(self._update_region_stats)
        self._region_stats_label = QLabel("拖动图中区间手柄选择时间范围")
        self._region_stats_label.setObjectName("secondaryText")
        self.plot_widget.getPlotItem().vb.scene().sigMouseMoved.connect(self._on_mouse_moved)

        self.analysis_tabs = QTabWidget()
        self.analysis_tabs.setObjectName("analysisTabs")
        self.analysis_tabs.setDocumentMode(True)
        self.analysis_tabs.setMinimumHeight(180)
        self.analysis_tabs.setMaximumHeight(16777215)

        stats_tab = QWidget()
        stats_layout = QVBoxLayout(stats_tab)
        stats_layout.setContentsMargins(12, 7, 12, 7)
        stats_layout.setSpacing(4)
        self.stats_label = QLabel("n=0 · 最大值 — · 最小值 — · 平均值 — · 标准差 —")
        self.stats_label.setWordWrap(True)
        stats_header = QHBoxLayout()
        stats_header.addWidget(QLabel("全程统计"))
        stats_header.addWidget(self.region_check)
        stats_header.addStretch(1)
        stats_layout.addLayout(stats_header)
        stats_layout.addWidget(self.stats_label)
        stats_layout.addWidget(self._region_stats_label)
        stats_layout.addStretch(1)
        self.analysis_tabs.addTab(stats_tab, "统计")

        baseline_tab = QWidget()
        baseline_layout = QVBoxLayout(baseline_tab)
        baseline_layout.setContentsMargins(12, 7, 12, 7)
        baseline_layout.setSpacing(5)
        baseline_row = QHBoxLayout()
        baseline_row.addWidget(QLabel("R₀ / V₀ / I₀"))
        self.baseline_edit = QLineEdit()
        self.baseline_edit.setPlaceholderText("未设置")
        self.baseline_edit.setMaximumWidth(150)
        self.baseline_edit.editingFinished.connect(self._baseline_edited)
        baseline_row.addWidget(self.baseline_edit)
        self.set_baseline_button = QPushButton("当前值设为基准")
        self.set_baseline_button.setObjectName("secondaryButton")
        self.mean_baseline_button = QPushButton("可见均值设为基准")
        self.mean_baseline_button.setObjectName("secondaryButton")
        self.clear_baseline_button = QPushButton("清除")
        self.clear_baseline_button.setObjectName("secondaryButton")
        self.set_baseline_button.clicked.connect(self._set_current_as_baseline)
        self.mean_baseline_button.clicked.connect(self._set_mean_as_baseline)
        self.clear_baseline_button.clicked.connect(self._clear_baseline)
        baseline_row.addWidget(self.set_baseline_button)
        baseline_row.addWidget(self.mean_baseline_button)
        baseline_row.addWidget(self.clear_baseline_button)
        baseline_row.addStretch(1)
        baseline_layout.addLayout(baseline_row)
        change_row = QHBoxLayout()
        change_row.addWidget(QLabel("近零阈值 (SI)"))
        self.baseline_threshold = QLineEdit("1e-12")
        self.baseline_threshold.setMaximumWidth(120)
        change_row.addWidget(self.baseline_threshold)
        change_row.addSpacing(8)
        change_row.addWidget(QLabel("相对基准变化"))
        self.change_label = QLabel("Δ — · ΔX/X₀ —")
        self.change_label.setObjectName("changeLabel")
        change_row.addWidget(self.change_label, 1)
        baseline_layout.addLayout(change_row)
        self.cursor_label = QLabel("游标：移动鼠标到曲线上读取邻近数据点")
        self.cursor_label.setObjectName("secondaryText")
        baseline_layout.addWidget(self.cursor_label)
        baseline_layout.addStretch(1)
        self.analysis_tabs.addTab(baseline_tab, "基准变化")

        event_tab = QWidget()
        event_layout = QVBoxLayout(event_tab)
        event_layout.setContentsMargins(12, 7, 12, 7)
        event_layout.setSpacing(6)
        event_layout.addWidget(QLabel("记录操作、环境变化或刺激切换；事件将写入实验记录并标记在曲线上。"))
        event_row = QHBoxLayout()
        self.event_edit = QLineEdit()
        self.event_edit.setPlaceholderText("例如：接通负载、移除刺激、环境温度变化…")
        self.event_button = QPushButton("记录实验事件")
        self.event_button.setObjectName("secondaryButton")
        self.event_button.clicked.connect(self._mark_event)
        event_row.addWidget(self.event_edit, 1)
        event_row.addWidget(self.event_button)
        event_layout.addLayout(event_row)
        event_layout.addStretch(1)
        self.analysis_tabs.addTab(event_tab, "实验事件")
        return panel

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_readout_layout()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._update_readout_layout)

    def _update_readout_layout(self) -> None:
        if not hasattr(self, "_reading_layout"):
            return
        workspace_width = self.measurement_workspace.width()
        workspace_height = self.measurement_workspace.height()
        mode = "stacked" if workspace_width < 620 else "compact" if workspace_width < 820 else "wide"
        short_chart = workspace_height < 620
        if mode == self._reading_layout_mode and short_chart == self._short_chart_layout:
            return
        self._reading_layout_mode = mode
        self._short_chart_layout = short_chart
        narrow = mode != "wide"
        self._reading_layout.setSpacing(12 if narrow else 20)
        self._metrics_layout.setSpacing(4 if narrow else 6)
        self._status_caption.setText("状态" if narrow else "采集状态")
        self._rate_caption.setText("频率" if narrow else "实测频率")
        self._elapsed_caption.setText("历时" if narrow else "实验历时")
        self._reading_card.setProperty("compact", narrow)
        stacked = mode == "stacked"
        self._reading_card.setProperty("stacked", stacked)
        self._reading_kicker.setVisible(not stacked)
        self._reading_layout.setSpacing(6 if stacked else 12 if narrow else 20)
        if stacked:
            self._reading_layout.setContentsMargins(12, 6, 12, 6)
        else:
            self._reading_layout.setContentsMargins(18, 14, 18, 14)
        self._reading_text_layout.setSpacing(1 if stacked else 3)
        self._chart_panel_layout.setSpacing(5 if stacked or short_chart else 9)
        if stacked or short_chart:
            self._action_layout.setContentsMargins(10, 4, 10, 4)
        else:
            self._action_layout.setContentsMargins(10, 7, 10, 7)
        compact_chart = stacked or short_chart
        self.plot_widget.setMinimumHeight(190 if compact_chart else 230)
        self._plot_frame.setMinimumHeight(192 if compact_chart else 232)
        self._reading_card.style().unpolish(self._reading_card)
        self._reading_card.style().polish(self._reading_card)
        self._reading_card.updateGeometry()
        self.measurement_workspace.updateGeometry()
        self._reading_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if mode == "stacked" else QBoxLayout.Direction.LeftToRight
        )
        self._reading_divider.setFrameShape(
            QFrame.Shape.HLine if mode == "stacked" else QFrame.Shape.VLine
        )

    def _on_plot_wheel_zoom(self, zoom_x: bool, _zoom_y: bool) -> None:
        if zoom_x:
            self.follow_check.setChecked(False)

    def _autoscale_plot(self) -> None:
        self.follow_check.setChecked(False)
        self.plot_widget.getPlotItem().enableAutoRange(x=True, y=True)
        self.plot_widget.autoRange(padding=0.04)

    def _build_log_dock(self) -> None:
        from PySide6.QtWidgets import QDockWidget, QPlainTextEdit

        self.log_dock = QDockWidget("SCPI通信与运行日志", self)
        self.log_dock.setObjectName("logDock")
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(6000)
        self.log_text.setPlaceholderText("连接识别、SCPI收发、通信异常和操作事件将显示在此处。")
        self.log_dock.setWidget(self.log_text)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.resizeDocks([self.log_dock], [150], Qt.Orientation.Vertical)
        self.log_dock.hide()

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        load_action = QAction("加载历史 CSV…", self)
        load_action.triggered.connect(self._load_history)
        file_menu.addAction(load_action)
        export_action = QAction("导出当前曲线 PNG…", self)
        export_action.triggered.connect(self._save_screenshot)
        file_menu.addAction(export_action)
        file_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        instrument_menu = self.menuBar().addMenu("仪器")
        diag = QAction("SCPI诊断…", self)
        diag.triggered.connect(self._diagnose)
        instrument_menu.addAction(diag)
        docs = QAction("打开中文使用说明", self)
        docs.triggered.connect(self._open_manual)
        instrument_menu.addAction(docs)
        instrument_menu.addSeparator()
        instrument_menu.addAction(self.log_dock.toggleViewAction())
        help_menu = self.menuBar().addMenu("帮助")
        license_action = QAction("Qt 与第三方许可…", self)
        license_action.triggered.connect(self._open_license_notice)
        help_menu.addAction(license_action)

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background: #252d31; color: #273136; }
            QWidget { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; color: #273136; }
            QWidget#appRoot { background: #252d31; }
            QFrame#appHeader { background: #30383c; border: 1px solid #465156; border-radius: 9px; }
            QLabel#brandIcon { background: #f1efe6; border: 1px solid #bdc0b7; border-radius: 6px; color: #176d72; font-weight: 800; }
            QLabel#mainTitle { color: #f1efe6; font-size: 18px; font-weight: 700; letter-spacing: .03em; }
            QMenuBar { background: #252d31; color: #e7e8df; padding: 2px 7px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #414d51; color: #6de1e2; }
            QMenu { background: #30383c; color: #f1efe6; border: 1px solid #505b5f; padding: 4px; }
            QDialog#configurationDialog { background: #f1efe6; }
            QDialog#configurationDialog QWidget { color: #273136; }
            QLabel#dialogTitle { color: #273136; font-size: 19px; font-weight: 700; }
            QWidget#setupPanel { background: #f1efe6; }
            QScrollArea { background: transparent; border: 0; }
            QTabWidget#configurationTabs::pane, QTabWidget#setupTabs::pane, QTabWidget#analysisTabs::pane { background: transparent; border: 0; }
            QTabWidget#configurationTabs QTabBar::tab, QTabWidget#setupTabs QTabBar::tab, QTabWidget#analysisTabs QTabBar::tab {
                background: transparent; color: #697579; border: 0; border-bottom: 2px solid transparent; padding: 8px 14px; margin-right: 5px;
            }
            QTabWidget#configurationTabs QTabBar::tab:selected, QTabWidget#setupTabs QTabBar::tab:selected, QTabWidget#analysisTabs QTabBar::tab:selected {
                color: #176d72; font-weight: 700; border-bottom-color: #29aab0;
            }
            QLabel#panelTitle { font-size: 16px; font-weight: 700; color: #273136; }
            QLabel#subtitle, QLabel#secondaryText { color: #677277; }
            QWidget#measurementWorkspace QLabel#secondaryText { color: #b7c2bc; }
            QGroupBox#sidebarCard { background: #faf9f4; border: 1px solid #cbd0c9; border-radius: 8px; margin-top: 12px; padding: 11px 8px 9px; font-weight: 650; }
            QGroupBox#sidebarCard::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #394448; background: #faf9f4; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
                background: #fffefa; color: #273136; border: 1px solid #bfc7c4; border-radius: 5px; padding: 5px 8px; selection-background-color: #17858b; min-height: 25px;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #17858b; }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background: #e8e8e0; color: #919895; border-color: #d5d8d1; }
            QPushButton { background: #faf9f4; color: #334045; border: 1px solid #bfc7c4; border-radius: 5px; padding: 6px 10px; min-height: 25px; }
            QPushButton:hover { background: #e8f1ed; border-color: #7a9a96; }
            QPushButton:disabled { color: #99a09e; background: #e7e8e1; border-color: #d1d4cd; }
            QPushButton#primaryButton { background: #167b80; border-color: #167b80; color: #fffefa; font-weight: 700; }
            QPushButton#primaryButton:hover { background: #12666b; border-color: #12666b; }
            QPushButton#dangerButton { color: #982f2b; border-color: #d6aaa3; background: #fff8f4; }
            QPushButton#secondaryButton { color: #344247; }
            QPushButton#statusAction { background: #414b4f; color: #e9e9df; border: 1px solid #596468; padding: 3px 12px; min-height: 22px; }
            QPushButton#statusAction:hover { color: #6de1e2; border-color: #42aeb1; }
            QToolButton#disclosureButton { color: #536166; border: 0; text-align: left; padding: 4px 2px; }
            QCheckBox { spacing: 7px; color: #3e4a4f; }
            QLabel#connectionBadge { padding: 6px 10px; background: #424c50; border: 1px solid #5b6669; border-radius: 6px; color: #d7ded9; font-size: 11px; font-weight: 700; }
            QLabel#connectionBadge[state="connected"] { color: #a9efce; background: #29433c; border-color: #47705f; }
            QLabel#connectionBadge[state="mock"] { color: #f4c579; background: #51412a; border-color: #8c6d3c; }
            QFrame#readingCard { background: #1d262a; border: 1px solid #4b585a; border-radius: 8px; }
            QFrame#readingCard QLabel#eyebrow { color: #a5b2ad; font-size: 10px; font-weight: 600; }
            QLabel#readingMode { color: #c7d2cc; font-size: 12px; font-weight: 600; }
            QLabel#readingValue { color: #73e2e2; font-family: "Cascadia Mono", Consolas, monospace; font-size: 42px; font-weight: 650; }
            QFrame#readingCard[compact="true"] QLabel#readingValue { font-size: 32px; }
            QFrame#readingCard[stacked="true"] QLabel#readingValue { font-size: 27px; }
            QFrame#metricCell { background: #303b3e; border: 1px solid #465458; border-radius: 6px; }
            QFrame#readingCard QFrame#metricCell QLabel#eyebrow { color: #a8b5ae; font-size: 9px; }
            QFrame#readingCard QLabel#metricValue, QFrame#readingCard QLabel#statusValue { color: #f0f0e7; font-size: 13px; font-weight: 700; }
            QFrame#readingCard[compact="true"] QLabel#metricValue, QFrame#readingCard[compact="true"] QLabel#statusValue { font-size: 11px; }
            QFrame#readingCard QLabel#statusDot[state="running"] { color: #69d8a4; }
            QFrame#readingCard QLabel#statusDot[state="paused"] { color: #e6a23a; }
            QFrame#readingCard QLabel#statusDot[state="idle"] { color: #8e9b98; }
            QFrame#cardDivider { color: #46565a; }
            QFrame#toolbarDivider { color: #596367; }
            QFrame#actionBar { background: #30383c; border: 1px solid #465156; border-radius: 7px; }
            QFrame#actionBar QPushButton { min-height: 30px; }
            QFrame#plotCard { background: #f1efe6; border: 1px solid #8d9690; border-radius: 8px; }
            QWidget#plotEmptyOverlay { background: transparent; }
            QLabel#plotEmptyTitle { color: #465457; font-size: 18px; font-weight: 700; }
            QLabel#plotEmptyHint { color: #7c8782; font-size: 11px; }
            QLabel#toolbarLabel { color: #d6ded8; font-weight: 700; }
            QLabel#toolbarHint { color: #9ca8a2; font-size: 9px; }
            QLabel#safetyBox { background: #fff3d7; color: #75551e; border: 1px solid #e5c27d; border-radius: 6px; padding: 9px; }
            QLabel#changeLabel { color: #966219; font-weight: 700; }
            QStatusBar { background: #252d31; color: #c1cbc5; border-top: 1px solid #414c50; }
            QDockWidget { color: #e2e7df; }
            QDockWidget::title { background: #30383c; padding: 6px 10px; border-top: 1px solid #465156; }
            QScrollBar:vertical { background: #e7e7de; width: 10px; margin: 1px; }
            QScrollBar::handle:vertical { background: #aab4ae; min-height: 26px; border-radius: 5px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)
        self.interval_spin.setMinimumHeight(max(38, self.interval_spin.sizeHint().height()))
    def _load_profiles(self) -> dict:
        profiles = {}
        if self._profile_path.exists():
            try:
                profiles = json.loads(self._profile_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.exception("无法读取配置文件")
        for mode, spec in DESCRIPTORS.items():
            default = {"autorange": True, "range_si": spec.ranges_si[0],
                       "nplc": 1 if spec.nplc_values else None,
                       "ac_bandwidth_hz": 20 if spec.ac_bandwidths_hz else None,
                       "analog_filter": False, "autozero": True, "baseline": None}
            default.update(profiles.get(mode.value, {}))
            profiles[mode.value] = default
        return profiles

    def _save_profiles(self) -> None:
        payload = {
            "serial": {"baudrate": self.baud_combo.currentText(), "bytesize": self.data_bits_combo.currentText(),
                       "parity": self.parity_combo.currentData(), "stopbits": self.stop_bits_combo.currentText(),
                       "flow_control": self.flow_combo.currentData(), "eol": self.eol_combo.currentData(),
                       "timeout_s": self.timeout_spin.value()},
            "sampling_interval_s": self.interval_spin.value(),
            **{mode: data for mode, data in self._profiles.items() if mode in {m.value for m in Mode}},
        }
        try:
            temp = self._profile_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self._profile_path)
        except OSError as exc:
            self._append_log(f"! 保存本机参数失败：{exc}")

    def _restore_serial_settings(self) -> None:
        serial = self._profiles.get("serial", {})
        self._set_combo_text(self.baud_combo, str(serial.get("baudrate", 9600)))
        self._set_combo_text(self.data_bits_combo, str(serial.get("bytesize", 8)))
        self._set_combo_data(self.parity_combo, serial.get("parity", "N"))
        self._set_combo_text(self.stop_bits_combo, str(serial.get("stopbits", 1)))
        self._set_combo_data(self.flow_combo, serial.get("flow_control", "none"))
        self._set_combo_data(self.eol_combo, serial.get("eol", "CRLF"))
        self.timeout_spin.setValue(float(serial.get("timeout_s", 2.0)))
        try:
            sampling_interval = float(self._profiles.get("sampling_interval_s", 1.0))
        except (TypeError, ValueError):
            sampling_interval = 1.0
        if not math.isfinite(sampling_interval) or not 0 <= sampling_interval <= 86400:
            sampling_interval = 1.0
        self.interval_spin.setValue(sampling_interval)
        for widget in (self.baud_combo, self.data_bits_combo, self.parity_combo, self.stop_bits_combo, self.flow_combo, self.eol_combo):
            widget.currentIndexChanged.connect(self._save_profiles)
        self.timeout_spin.valueChanged.connect(self._save_profiles)

    @staticmethod
    def _set_combo_text(combo: QComboBox, text: str) -> None:
        idx = combo.findText(text)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    @staticmethod
    def _set_combo_data(combo: QComboBox, data) -> None:
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _refresh_ports(self) -> None:
        current_text = self.port_combo.currentText()
        current_port = self.port_combo.currentData() or current_text.split(" ", 1)[0]
        self.port_combo.clear()
        try:
            from serial.tools import list_ports
            for item in list_ports.comports():
                row = self.port_combo.count()
                self.port_combo.addItem(item.device, item.device)
                self.port_combo.setItemData(row, item.description, Qt.ItemDataRole.ToolTipRole)
        except ImportError:
            self._append_log("! pyserial未安装，无法扫描COM端口")
        if current_port:
            idx = next((i for i in range(self.port_combo.count()) if self.port_combo.itemData(i) == current_port), -1)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
            else:
                self.port_combo.setEditText(current_port)
        if self.port_combo.count() == 0 and not current_port:
            self.port_combo.setEditText("COM1")
        if self.port_combo.isEditable() and self.port_combo.lineEdit():
            self.port_combo.lineEdit().setCursorPosition(0)

    def _serial_settings(self) -> SerialSettings:
        return SerialSettings(
            baudrate=int(self.baud_combo.currentText()), bytesize=int(self.data_bits_combo.currentText()),
            parity=self.parity_combo.currentData(), stopbits=int(self.stop_bits_combo.currentText()),
            flow_control=FlowControl(self.flow_combo.currentData()), eol=self.eol_combo.currentData(),
            timeout_s=self.timeout_spin.value(),
        )

    def _connect_real(self) -> None:
        port = self.port_combo.currentData() or self.port_combo.currentText().split(" ", 1)[0]
        if not port:
            self._show_error("请选择COM端口")
            return
        self._persist_current_settings()
        try:
            serial_settings = self._serial_settings()
            settings = self._settings_for_mode(self._current_mode)
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.connect_button.setEnabled(False)
        self.connection_badge.setText("正在连接…")
        self.connection_badge.setProperty("state", "connecting")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self._bridge.connect_real.emit(port, serial_settings, settings, False)

    def _disconnect(self) -> None:
        if self._acquiring:
            self._show_error("请先停止采集，再断开仪器")
            return
        self._bridge.shutdown.emit()
        # The worker remains available to reconnect after closing the device.

    def _on_connected(self, connected: bool, text: str) -> None:
        self._connected = connected
        self._simulation = "模拟" in text or "MOCK" in text
        if not connected:
            self._active_settings_by_mode.clear()
        if self._simulation:
            badge_text = "MOCK 模拟模式"
        elif connected:
            badge_text = "已连接 · 8845A / REMOTE"
        else:
            badge_text = text if text and text != "未读取" else "未连接"
        self.connection_badge.setText(badge_text)
        tooltip = text
        if connected and not self._simulation:
            tooltip = (
                f"{text}\nREMOTE远程控制已启用：除LOCAL键外，仪表面板按键不可用。"
                "若需用面板改挡，请先断开软件连接；采集中不要按LOCAL。"
            )
        self.connection_badge.setToolTip(tooltip)
        self.connection_badge.setProperty(
            "state", "mock" if self._simulation else "connected" if connected else "disconnected"
        )
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.idn_label.setText(f"设备身份：{text if connected else '未读取'}")
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.mock_mode_check.setEnabled(not connected)
        if connected:
            self._load_mode_controls(self._current_mode)
        else:
            self._firmware = ""
            self._terminals = ""
            self._instrument_mode_known = False
        self._update_readout_context()
        self._update_controls()

    @Slot(object)
    def _on_configuration(self, details: dict) -> None:
        self._simulation = bool(details.get("simulation", self._simulation))
        self._terminals = str(details.get("input_terminals") or "")
        idn = str(details.get("idn") or "")
        try:
            fields = next(csv.reader([idn]))
            self._firmware = fields[3].strip() if len(fields) > 3 else ""
        except (csv.Error, IndexError):
            self._firmware = ""
        function = str(details.get("function") or "")
        detected_mode = mode_from_scpi_function(function)
        self._instrument_mode_known = detected_mode is not None
        if detected_mode is not None and detected_mode != self._current_mode:
            self._active_settings_by_mode.clear()
            self._current_mode = detected_mode
            self._baseline = self._profiles[detected_mode.value].get("baseline")
            old_block = self.mode_combo.blockSignals(True)
            self._set_combo_data(self.mode_combo, detected_mode.value)
            self.mode_combo.blockSignals(old_block)
        if self._connected:
            self._load_mode_controls(self._current_mode)

    @Slot(object)
    def _on_active_settings(self, payload: dict) -> None:
        mode = str(payload.get("mode") or "")
        if mode in {item.value for item in Mode}:
            self._active_settings_by_mode[mode] = dict(payload)
            self._update_readout_context()

    def _mode_changed(self, index: int) -> None:
        if index < 0:
            return
        mode = Mode(self.mode_combo.itemData(index))
        if mode == self._current_mode:
            self._load_mode_controls(mode)
            return
        if self._acquiring:
            self._set_combo_data(self.mode_combo, self._current_mode.value)
            self._show_error("采集中或暂停时不能切换测量模式。请先停止实验并新建数据段。")
            return
        old_spec, new_spec = descriptor(self._current_mode), descriptor(mode)
        requires_safety_check = (
            old_spec.wiring_group != new_spec.wiring_group
            or old_spec.wiring_group == "current"
            or new_spec.wiring_group == "current"
        )
        if requires_safety_check:
            transition_steps = []
            if old_spec.wiring_group == "current":
                transition_steps.append("先关闭被测回路，并将测试线从独立的mA/400 mA或10 A电流插孔移回V/Ω插孔，再切换离开电流模式。")
            transition_steps.append(self._mode_safety_instructions(mode))
            target_profile = self._profiles[mode.value]
            target_autorange = bool(target_profile.get("autorange", True))
            target_range = None if target_autorange else float(target_profile.get("range_si", new_spec.ranges_si[0]))
            dialog = WiringConfirmationDialog(
                mode,
                f"切换为“{new_spec.label}”前，请逐项确认：\n\n" + "\n\n".join(transition_steps) + "\n\n切换SCPI模式不会移动测试线，也不能检查外部回路。",
                self,
                current_range_si=target_range,
                autorange=target_autorange,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self._set_combo_data(self.mode_combo, self._current_mode.value)
                return
        self._active_settings_by_mode.clear()
        self._current_mode = mode
        self._instrument_mode_known = not self._connected
        self._baseline = self._profiles[mode.value].get("baseline")
        self._load_mode_controls(mode)
        if self._connected:
            self._bridge.configure.emit(self._settings_for_mode(mode))
        self._clear_plot()

    def _mode_safety_instructions(self, mode: Mode) -> str:
        spec = descriptor(mode)
        if spec.quantity != "current":
            return spec.safety
        if mode == self._current_mode and hasattr(self, "range_combo"):
            autorange = self.autorange_check.isChecked()
            selected_range = self.range_combo.currentData()
        else:
            profile = self._profiles[mode.value]
            autorange = bool(profile.get("autorange", True))
            selected_range = profile.get("range_si", spec.ranges_si[0])
        range_value = None if selected_range is None else float(selected_range)
        return f"{spec.safety}\n{current_input_guidance(range_value, autorange)}"

    def _refresh_safety_hint(self) -> None:
        if hasattr(self, "safety_text"):
            self.safety_text.setText(self._mode_safety_instructions(self._current_mode))

    def _load_mode_controls(self, mode: Mode) -> None:
        self._loading_mode_controls = True
        spec = descriptor(mode)
        stored = self._profiles[mode.value]
        self.autorange_check.blockSignals(True)
        self.autorange_check.setChecked(bool(stored.get("autorange", True)))
        self.autorange_check.blockSignals(False)
        self.range_combo.clear()
        self.range_combo.addItem("自动量程", None)
        for value in spec.ranges_si:
            self.range_combo.addItem(range_label(value, spec.unit_si), value)
        unsupported = [] if self._simulation else list(
            unsupported_current_ranges(mode, self._firmware, self._terminals)
        )
        autorange_reason = None if self._simulation or spec.quantity != "current" else current_autorange_block_reason(
            mode, self._firmware, self._terminals
        )
        self.autorange_check.setEnabled(autorange_reason is None)
        self.autorange_check.setToolTip(autorange_reason or "由仪器在该模式已确认支持的量程间自动选择")
        if autorange_reason and self.autorange_check.isChecked():
            self.autorange_check.setChecked(False)
            stored["autorange"] = False
        for i in range(1, self.range_combo.count()):
            if self.range_combo.itemData(i) in unsupported:
                item = self.range_combo.model().item(i)
                if item is not None:
                    item.setEnabled(False)
                    item.setToolTip("仪器回报的固件版本低于门槛或当前输入端子不支持此档；不会向仪器发送该量程。")
        idx = self.range_combo.findData(stored.get("range_si"))
        selected_item = self.range_combo.model().item(idx) if idx > 0 else None
        if selected_item is not None and not selected_item.isEnabled():
            idx = next(
                (i for i in range(1, self.range_combo.count())
                 if self.range_combo.model().item(i).isEnabled()),
                0,
            )
            if idx > 0:
                stored["range_si"] = self.range_combo.itemData(idx)
                stored["autorange"] = False
                self.autorange_check.setChecked(False)
        if idx >= 0:
            self.range_combo.setCurrentIndex(idx)
        elif self.range_combo.count() > 1:
            self.range_combo.setCurrentIndex(1)
        self.range_combo.setEnabled(not self.autorange_check.isChecked())

        self.nplc_combo.clear()
        if spec.nplc_values:
            for index, value in enumerate(spec.nplc_values):
                speed = " · 快" if index == 0 else " · 慢" if index == len(spec.nplc_values) - 1 else ""
                self.nplc_combo.addItem(f"{value:g} NPLC{speed}", value)
            nplc_idx = self.nplc_combo.findData(stored.get("nplc"))
            self.nplc_combo.setCurrentIndex(max(0, nplc_idx))
            self.nplc_combo.setEnabled(True)
            self.nplc_row_label.setText("积分时间 / NPLC")
        else:
            self.nplc_combo.addItem("不适用", None)
            self.nplc_combo.setEnabled(False)
            self.nplc_row_label.setText("积分时间 / NPLC")

        self.bandwidth_combo.clear()
        if spec.ac_bandwidths_hz:
            for value in spec.ac_bandwidths_hz:
                label = {3: "3 Hz · 慢", 20: "20 Hz · 中", 200: "200 Hz · 快"}[value]
                self.bandwidth_combo.addItem(label, value)
            band_idx = self.bandwidth_combo.findData(stored.get("ac_bandwidth_hz"))
            self.bandwidth_combo.setCurrentIndex(max(0, band_idx))
            self.bandwidth_combo.setEnabled(True)
            self.bandwidth_row_label.setText("AC 带宽")
        else:
            self.bandwidth_combo.addItem("不适用", None)
            self.bandwidth_combo.setEnabled(False)
            self.bandwidth_row_label.setText("AC 带宽")
        self.analog_filter_check.setVisible(spec.analog_filter)
        self.analog_filter_check.setEnabled(spec.analog_filter)
        self.analog_filter_check.setChecked(bool(stored.get("analog_filter", False)) if spec.analog_filter else False)
        self.autozero_check.setChecked(bool(stored.get("autozero", True)))
        self.safety_text.setText(self._mode_safety_instructions(mode))
        baseline = stored.get("baseline")
        self._baseline = float(baseline) if baseline is not None else None
        self.baseline_edit.setText("" if self._baseline is None else repr(self._baseline))
        self.plot_widget.setLabel("left", spec.display_quantity, units=spec.unit_si, color="#697470")
        self.latest_value_label.setText("等待读数")
        self._update_range_enabled()
        self._loading_mode_controls = False
        self._update_readout_context()

    def _update_range_enabled(self) -> None:
        self.range_combo.setEnabled(not self._acquiring and not self.autorange_check.isChecked())

    def _persist_current_settings(self) -> None:
        mode = self._current_mode
        stored = self._profiles[mode.value]
        stored.update({
            "autorange": self.autorange_check.isChecked(),
            "range_si": self.range_combo.currentData() if self.range_combo.currentData() is not None else descriptor(mode).ranges_si[0],
            "nplc": self.nplc_combo.currentData(),
            "ac_bandwidth_hz": self.bandwidth_combo.currentData(),
            "analog_filter": self.analog_filter_check.isChecked() if descriptor(mode).analog_filter else False,
            "autozero": self.autozero_check.isChecked(),
            "baseline": self._baseline,
        })
        self._save_profiles()

    def _settings_changed(self, *_args) -> None:
        if not self._loading_mode_controls:
            self._persist_current_settings()
            self._refresh_safety_hint()
            self._update_readout_context()

    def _update_readout_context(self) -> None:
        if not hasattr(self, "reading_mode_label"):
            return
        spec = descriptor(self._current_mode)
        active = self._active_settings_by_mode.get(self._current_mode.value)
        details = [spec.label]
        pending: list[str] = []
        if active is not None:
            active_parts: list[str] = []
            active_autorange = bool(active.get("autorange", True))
            active_range = active.get("range_si")
            if active_autorange:
                active_parts.append("自动量程")
            elif active_range is not None:
                active_parts.append(range_label(float(active_range), spec.unit_si))
            if spec.nplc_values:
                active_nplc = active.get("nplc")
                selected_nplc = self.nplc_combo.currentData()
                if active_nplc is not None:
                    active_parts.append(f"{float(active_nplc):g} NPLC")
                if selected_nplc is not None and active_nplc != selected_nplc:
                    pending.append(f"{float(selected_nplc):g} NPLC")
            elif spec.ac_bandwidths_hz:
                active_bandwidth = active.get("ac_bandwidth_hz")
                selected_bandwidth = self.bandwidth_combo.currentData()
                if active_bandwidth is not None:
                    active_parts.append(f"{int(active_bandwidth)} Hz 带宽")
                if selected_bandwidth is not None and active_bandwidth != selected_bandwidth:
                    pending.append(f"{int(selected_bandwidth)} Hz 带宽")
            if spec.analog_filter and active.get("analog_filter"):
                active_parts.append("模拟滤波")
            if bool(self.autorange_check.isChecked()) != active_autorange:
                pending.append("自动量程" if self.autorange_check.isChecked() else "固定量程")
            elif not active_autorange:
                selected_range = self.range_combo.currentData()
                if selected_range is not None and active_range != selected_range:
                    pending.append(f"量程 {range_label(float(selected_range), spec.unit_si)}")
            if spec.analog_filter and self.analog_filter_check.isChecked() != bool(active.get("analog_filter")):
                pending.append("模拟滤波" if self.analog_filter_check.isChecked() else "关闭模拟滤波")
            if self.autozero_check.isChecked() != bool(active.get("autozero", True)):
                pending.append("自动零点" if self.autozero_check.isChecked() else "关闭自动零点")
            if self._simulation:
                details.append("MOCK已应用 " + " · ".join(active_parts))
            else:
                details.append("仪表已应用 " + " · ".join(active_parts))
            if pending:
                details.append("待应用 " + "、".join(pending))
        else:
            if self.autorange_check.isChecked():
                selected_range_text = "自动量程"
            else:
                selected_range = self.range_combo.currentData()
                selected_range_text = range_label(float(selected_range), spec.unit_si) if selected_range is not None else "量程待选"
            selected_parts = [selected_range_text]
            if spec.nplc_values and self.nplc_combo.currentData() is not None:
                selected_parts.append(f"{float(self.nplc_combo.currentData()):g} NPLC")
            elif spec.ac_bandwidths_hz and self.bandwidth_combo.currentData() is not None:
                selected_parts.append(f"{int(self.bandwidth_combo.currentData())} Hz 带宽")
            if self._connected and not self._simulation:
                details.append("仪表设置待读回")
            else:
                details.append(("MOCK选择 " if self._simulation else "本机已选 ") + " · ".join(selected_parts))
        self.reading_mode_label.setText("  ·  ".join(details))
        self.reading_mode_label.setWordWrap(True)

    def _settings_for_mode(self, mode: Mode) -> ModeSettings:
        if mode == self._current_mode:
            autorange = self.autorange_check.isChecked()
            current_range = self.range_combo.currentData()
            return ModeSettings(
                mode=mode, autorange=autorange,
                range_si=None if autorange else (float(current_range) if current_range is not None else descriptor(mode).ranges_si[0]),
                nplc=self.nplc_combo.currentData() if descriptor(mode).nplc_values else None,
                ac_bandwidth_hz=self.bandwidth_combo.currentData() if descriptor(mode).ac_bandwidths_hz else None,
                analog_filter=self.analog_filter_check.isChecked() if descriptor(mode).analog_filter else False,
                autozero=self.autozero_check.isChecked(),
            )
        stored = self._profiles[mode.value]
        return ModeSettings(
            mode, bool(stored.get("autorange", True)),
            None if stored.get("autorange", True) else float(stored.get("range_si", descriptor(mode).ranges_si[0])),
            stored.get("nplc") if descriptor(mode).nplc_values else None,
            stored.get("ac_bandwidth_hz") if descriptor(mode).ac_bandwidths_hz else None,
            bool(stored.get("analog_filter", False)) if descriptor(mode).analog_filter else False,
            bool(stored.get("autozero", True)),
        )

    def _apply_settings(self) -> None:
        try:
            self._persist_current_settings()
            settings = self._settings_for_mode(self._current_mode)
            settings.validate()
            if not self._connected:
                QMessageBox.information(self, "参数已保存", "本模式参数已保存到本机。连接8845A后按“应用仪器参数”写入仪器。")
            elif not self._instrument_mode_known:
                spec = descriptor(self._current_mode)
                answer = QMessageBox.warning(
                    self, "确认配置测量模式",
                    f"程序无法识别仪器当前功能。此操作将把仪器配置为“{spec.label}”。\n\n{self._mode_safety_instructions(self._current_mode)}\n\n确认仪器接线状态适用于该功能？",
                    QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                    QMessageBox.StandardButton.Cancel,
                )
                if answer == QMessageBox.StandardButton.Ok:
                    self._bridge.configure.emit(settings)
            else:
                self._bridge.configure.emit(settings)
        except Exception as exc:
            self._show_error(str(exc))

    def _apply_acquisition_speed(self) -> None:
        if not self._connected or not self._acquiring:
            self._show_error("请先连接仪器并开始连续采集；运行中或暂停时可应用速度")
            return
        if self._speed_update_pending:
            return
        try:
            settings = self._settings_for_mode(self._current_mode)
            settings.validate()
            spec = descriptor(self._current_mode)
            if not spec.nplc_values and not spec.ac_bandwidths_hz:
                raise ValueError(f"{spec.label}没有可调整的测量速度参数")
            self._persist_current_settings()
            self._speed_update_pending = True
            self._update_controls()
            self.statusBar().showMessage("正在应用并核对仪器测量速度…")
            self._bridge.set_measurement_speed.emit(settings)
        except Exception as exc:
            self._show_error(str(exc))

    @Slot(bool, str)
    def _on_measurement_speed_result(self, success: bool, message: str) -> None:
        self._speed_update_pending = False
        self.statusBar().showMessage(message, 12000 if success else 20000)
        self._update_readout_context()
        self._update_controls()

    def _request_zero(self) -> None:
        if self._acquiring:
            self._show_error("请停止采集后再执行ZERO。模拟滤波条件下，ZERO应与当前量程、NPLC和触发设置一致。")
            return
        answer = QMessageBox.question(self, "执行仪器 ZERO", "将对真实仪器执行 ZERO:AUTO ONCE。请确认输入条件与当前量程、NPLC及触发方法匹配。是否继续？")
        if answer == QMessageBox.StandardButton.Yes:
            self._bridge.zero.emit()

    def _scenario(self) -> MockScenario:
        selected = self.scenario_combo.currentText()
        if selected == "长期漂移":
            return MockScenario(name=selected, noise_fraction=1e-4, drift_fraction_per_minute=3e-4,
                                step_at_s=1e9, recovery_at_s=None, read_latency_s=0.005)
        if selected == "通信超时测试":
            return MockScenario(name=selected, timeout_at_sample=8, read_latency_s=0.03)
        if selected == "断线恢复测试":
            return MockScenario(name=selected, disconnect_at_sample=8, read_latency_s=0.03)
        return MockScenario(name=selected)

    def _start_continuous(self) -> None:
        self._start(one_shot=False)

    def _start_single(self) -> None:
        self._start(one_shot=True)

    def _start(self, one_shot: bool) -> None:
        if not self._connected:
            self._show_error("请先连接仪器，或勾选MOCK模拟模式并点击“连接真实仪器”。")
            return
        if not self._simulation and not self._instrument_mode_known:
            self._show_error("未识别到仪器当前测量功能。请先逐项核对接线，再点击“应用仪器参数”明确配置一种支持模式。")
            return
        if self._current_mode in (Mode.DCI, Mode.ACI, Mode.RES2, Mode.RES4):
            spec = descriptor(self._current_mode)
            autorange = self.autorange_check.isChecked()
            selected_range = self.range_combo.currentData()
            range_si = None if autorange or selected_range is None else float(selected_range)
            dialog = WiringConfirmationDialog(
                self._current_mode,
                f"开始“{spec.label}”前必须再次核对接线和安全状态：\n\n"
                f"{self._mode_safety_instructions(self._current_mode)}\n\n"
                "SCPI不能检测表笔插孔、保险丝或外部回路。确认已关闭被测电路电源，并核对接线和预期量程后再继续。",
                self,
                current_range_si=range_si,
                autorange=autorange,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
        try:
            settings = self._settings_for_mode(self._current_mode)
            settings.validate()
            root = Path(self.output_root.text()).expanduser()
            if not self.experiment_name.text().strip():
                raise ValueError("请填写实验名称")
            interval = None if self.interval_spin.value() == 0 else self.interval_spin.value()
            if self.mock_mode_check.isChecked() and not self._simulation:
                raise ValueError("MOCK模式未连接。请先点击“连接真实仪器”以通过MOCK后端连接；UI会明确显示模拟状态。")
            self._persist_current_settings()
            self._clear_plot()
            self._bridge.start_experiment.emit(settings, self.experiment_name.text().strip(), str(root), interval, one_shot)
        except Exception as exc:
            self._show_error(str(exc))

    def _on_sampling_interval_changed(self, value: float) -> None:
        self._profiles["sampling_interval_s"] = float(value)
        self._save_profiles()
        if not self._acquiring:
            return
        interval = None if value == 0 else float(value)
        self._bridge.set_sampling_interval.emit(interval)

    def _connect_mock(self) -> None:
        if self._connected:
            return
        try:
            settings = self._settings_for_mode(self._current_mode)
            settings.validate()
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.connect_button.setEnabled(False)
        self.connection_badge.setText("正在启动MOCK…")
        self._bridge.connect_mock.emit(settings, self._scenario())

    def _update_connect_label(self, mock: bool) -> None:
        self.connect_button.setText("启动MOCK模拟仪器" if mock else "连接真实仪器")
        self.scenario_combo.setEnabled(mock)

    def _connect_action(self) -> None:
        if self.mock_mode_check.isChecked():
            self._connect_mock()
        else:
            self._connect_real()

    def _pause_or_resume(self) -> None:
        if self._paused:
            self._bridge.resume.emit()
        else:
            self._bridge.pause.emit()

    def _stop(self) -> None:
        if self._acquiring:
            self._worker.request_stop()
            self._bridge.stop.emit()

    def _on_status(self, status: str) -> None:
        if "连续采集中" in status:
            display_status = "连续采集中"
        elif "单次测量中" in status:
            display_status = "单次测量中"
        elif "暂停" in status:
            display_status = "已暂停"
        elif status.startswith("就绪"):
            display_status = "就绪"
        elif status.startswith("连续超时"):
            display_status = "连续超时"
        elif status.startswith("SCPI"):
            display_status = "响应超时"
        else:
            display_status = status
        self.experiment_status.setText(display_status)
        self.experiment_status.setToolTip(status)
        self.statusBar().showMessage(status)
        if "连续采集中" in status or "单次测量中" in status:
            self._acquiring = True
            self._paused = False
            self._experiment_active = True
        elif "暂停" in status:
            self._acquiring = True
            self._paused = True
            self._experiment_active = True
        elif status.startswith("连续超时"):
            self._acquiring = False
            self._paused = False
            self._experiment_active = False
        elif status.startswith("就绪") or "已执行" in status:
            self._acquiring = False
            self._paused = False
            # The recorder may be closed slightly before this status reaches the GUI.
            if status.startswith("就绪"):
                self._experiment_active = False
        elif status.startswith("SCPI"):
            self._acquiring = False
            self._paused = False
            self._experiment_active = False
        dot_state = "paused" if self._paused else "running" if self._acquiring else "idle"
        self.status_dot.setProperty("state", dot_state)
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        self._update_controls()

    def _update_controls(self) -> None:
        idle = not self._acquiring
        self.mode_combo.setEnabled(idle)
        spec = descriptor(self._current_mode)
        autorange_allowed = (
            self._simulation or spec.quantity != "current"
            or current_autorange_block_reason(self._current_mode, self._firmware, self._terminals) is None
        )
        self.autorange_check.setEnabled(idle and autorange_allowed)
        self.range_combo.setEnabled(idle and not self.autorange_check.isChecked())
        # Measurement speed remains editable while acquiring; the Apply Speed
        # action serializes the instrument update in the worker thread.
        self.nplc_combo.setEnabled(bool(spec.nplc_values) and (idle or self._connected))
        self.bandwidth_combo.setEnabled(bool(spec.ac_bandwidths_hz) and (idle or self._connected))
        self.analog_filter_check.setEnabled(idle and spec.analog_filter)
        self.autozero_check.setEnabled(idle)
        # Interval remains editable during acquisition and is applied by the worker immediately.
        self.interval_spin.setEnabled(True)
        self.apply_button.setEnabled(idle and self._connected)
        self.apply_speed_button.setEnabled(
            self._acquiring and self._connected and not self._speed_update_pending
            and bool(spec.nplc_values or spec.ac_bandwidths_hz)
        )
        self.zero_button.setEnabled(idle and self._connected)
        self.start_button.setEnabled(idle and self._connected)
        self.single_button.setEnabled(idle and self._connected)
        self.pause_button.setEnabled(self._acquiring)
        self.pause_button.setText("继续" if self._paused else "暂停")
        self.stop_button.setEnabled(self._acquiring)
        self.disconnect_button.setEnabled(self._connected and idle)
        self.connect_button.setEnabled(not self._connected)

    @Slot()
    def _on_sample_ready(self) -> None:
        rows, rate, full_stats = self._worker.take_sample_update()
        self._on_sample(rows, rate, full_stats)

    @Slot(object, float, object)
    def _on_sample(self, rows: list[dict], rate: float, full_stats: dict) -> None:
        try:
            self.actual_rate_label.setText(f"{rate:.4g} Hz" if rate else "计算中…")
            self._global_stats = full_stats
            for row in rows:
                if row.get("status") != "ok" or row.get("value_si") in (None, ""):
                    self._append_log(f"! 数据无效：{row.get('status')} · {row.get('error')}")
                    continue
                value = float(row["value_si"])
                elapsed = float(row["elapsed_s"])
                self._last_value = value
                self._last_elapsed = elapsed
                self._plot_x.append(elapsed)
                self._plot_y.append(value)
            valid_rows = [r for r in rows if r.get("status") == "ok" and r.get("value_si") not in (None, "")]
            if valid_rows:
                self.plot_empty_overlay.hide()
                self.curve.setData(list(self._plot_x), list(self._plot_y), autoDownsample=True, clipToView=True)
                value = float(valid_rows[-1]["value_si"])
                elapsed = float(valid_rows[-1]["elapsed_s"])
                spec = descriptor(Mode(valid_rows[-1]["mode"]))
                displayed, unit = engineering_value(value, spec.unit_si)
                self.latest_value_label.setText(f"{displayed:.10g} {unit}")
                if self._baseline is not None:
                    self.baseline_line.setValue(self._baseline)
                    self.baseline_line.show()
                    delta = value - self._baseline
                    percent = relative_change_percent(value, self._baseline, self._parse_threshold())
                    d_display, d_unit = engineering_value(delta, spec.unit_si)
                    relative = f"{percent:.8g}%" if percent is not None else "无效（基准接近零或阈值无效）"
                    self.change_label.setText(f"Δ={d_display:.8g} {d_unit} · ΔX/X₀={relative}")
                else:
                    self.baseline_line.hide()
                    self.change_label.setText("未设置基准值")
                if self.follow_check.isChecked():
                    right = max(AUTO_FOLLOW_MIN_WINDOW_S, elapsed + 1.0)
                    left = max(0.0, right - AUTO_FOLLOW_WINDOW_S)
                    self.plot_widget.setXRange(left, right, padding=0)
            if rows:
                self._update_stats(full_stats)
                if self._region.isVisible():
                    self._on_region_samples(rows)
        except Exception:
            log.exception("绘图更新失败")
            self._append_log("! 绘图刷新失败，采集和CSV保存仍在采集线程中继续")

    def _update_stats(self, full_stats: dict | None = None) -> None:
        stats = full_stats if full_stats is not None else summarize(self._plot_y)
        spec = descriptor(self._current_mode)
        def fmt(value):
            if value is None:
                return "—"
            scaled, unit = engineering_value(value, spec.unit_si)
            return f"{scaled:.8g} {unit}"
        if isinstance(stats, dict):
            count, minimum, maximum, mean, deviation = (stats.get(k) for k in ("count", "minimum", "maximum", "mean", "sample_stddev"))
        else:
            count, minimum, maximum, mean, deviation = stats.count, stats.minimum, stats.maximum, stats.mean, stats.sample_stddev
        self.stats_label.setText(f"全程有效点 n={count} · 最大 {fmt(maximum)} · 最小 {fmt(minimum)} · 平均 {fmt(mean)} · 样本标准差 s {fmt(deviation)}")

    def _update_region_stats(self) -> None:
        if not self._region.isVisible():
            return
        left, right = sorted(self._region.getRegion())
        self._region_stats_bounds = (left, right)
        path = self._experiment_csv_path
        if path is not None and path.is_file():
            for task in self._region_stats_tasks.values():
                task.cancel()
            self._region_stats_request_id += 1
            request_id = self._region_stats_request_id
            self._region_stats_scanning = True
            self._region_stats_pending_rows.clear()
            self._region_stats_values = None
            self._region_stats_last_sample_index = 0
            task = _RegionStatisticsTask(request_id, path, left, right, self._current_mode)
            task.signals.completed.connect(self._on_region_stats_finished)
            self._region_stats_tasks[request_id] = task
            self._region_stats_label.setText(
                f"{left:.6g}–{right:.6g} s · 正在后台按原始 CSV 计算区间统计…"
            )
            QThreadPool.globalInstance().start(task)
            return

        # A plot-only fallback is explicitly labeled; it may contain a rolling
        # cache or uniformly decimated history points, so it is not a raw-data statistic.
        values = [y for x, y in zip(self._plot_x, self._plot_y) if left <= x <= right]
        self._show_region_stats(summarize(values), "图中缓存点（非全量）")

    def _on_region_visibility_changed(self, visible: bool) -> None:
        self._region.setVisible(visible)
        if visible:
            self._update_region_stats()

    def _on_region_stats_finished(self, request_id: int, result: object, error: object) -> None:
        self._region_stats_tasks.pop(request_id, None)
        if request_id != self._region_stats_request_id:
            return
        self._region_stats_scanning = False
        if error:
            self._region_stats_pending_rows.clear()
            self._region_stats_label.setText(f"原始 CSV 区间统计失败：{error}")
            return
        if not isinstance(result, dict) or not isinstance(result.get("stats"), RunningStats):
            self._region_stats_pending_rows.clear()
            self._region_stats_label.setText("原始 CSV 区间统计失败：返回结果无效")
            return

        stats = result["stats"]
        last_sample_index = int(result["last_sample_index"])
        left, right = self._region_stats_bounds or (0.0, 0.0)
        for row in self._region_stats_pending_rows:
            try:
                sample_index = int(row.get("sample_index", 0))
            except (TypeError, ValueError):
                continue
            if sample_index <= last_sample_index:
                continue
            last_sample_index = sample_index
            if row.get("status") != "ok" or row.get("mode") != self._current_mode.value:
                continue
            try:
                elapsed = float(row["elapsed_s"])
                value = float(row["value_si"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(elapsed) and math.isfinite(value) and left <= elapsed <= right:
                stats.add(value)
        self._region_stats_pending_rows.clear()
        self._region_stats_values = stats
        self._region_stats_last_sample_index = last_sample_index
        self._show_region_stats(stats, "完整原始 CSV")

    def _on_region_samples(self, rows: list[dict]) -> None:
        if self._region_stats_scanning:
            self._region_stats_pending_rows.extend(rows)
            return
        if self._region_stats_values is None or self._region_stats_bounds is None:
            if self._experiment_csv_path is None:
                self._update_region_stats()
            return
        left, right = self._region_stats_bounds
        changed = False
        for row in rows:
            try:
                sample_index = int(row.get("sample_index", 0))
            except (TypeError, ValueError):
                continue
            if sample_index <= self._region_stats_last_sample_index:
                continue
            self._region_stats_last_sample_index = sample_index
            if row.get("status") != "ok" or row.get("mode") != self._current_mode.value:
                continue
            try:
                elapsed = float(row["elapsed_s"])
                value = float(row["value_si"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(elapsed) and math.isfinite(value) and left <= elapsed <= right:
                self._region_stats_values.add(value)
                changed = True
        if changed:
            self._show_region_stats(self._region_stats_values, "完整原始 CSV · 实时更新")

    def _show_region_stats(self, stats, source: str) -> None:
        if self._region_stats_bounds is None:
            left, right = sorted(self._region.getRegion())
        else:
            left, right = self._region_stats_bounds
        spec = descriptor(self._current_mode)

        def fmt(value: float | None) -> str:
            if value is None:
                return "—"
            scaled, unit = engineering_value(value, spec.unit_si)
            return f"{scaled:.8g} {unit}"

        self._region_stats_label.setText(
            f"{left:.6g}–{right:.6g} s · n={stats.count} · 最大 {fmt(stats.maximum)} · "
            f"最小 {fmt(stats.minimum)} · 均值 {fmt(stats.mean)} · 样本标准差 s {fmt(stats.sample_stddev)}"
            f" · {source}"
        )

    def _on_mouse_moved(self, pos) -> None:
        vb = self.plot_widget.getPlotItem().vb
        if not self.plot_widget.sceneBoundingRect().contains(pos):
            return
        point = vb.mapSceneToView(pos)
        x = point.x()
        if not self._plot_x:
            return
        xs = list(self._plot_x)
        idx = min(max(bisect.bisect_left(xs, x), 0), len(xs) - 1)
        if idx > 0 and abs(xs[idx - 1] - x) < abs(xs[idx] - x):
            idx -= 1
        y = self._plot_y[idx]
        spec = descriptor(self._current_mode)
        shown, unit = engineering_value(y, spec.unit_si)
        if self._crosshair_x is None:
            self._crosshair_x = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#d7e2ec", style=Qt.PenStyle.DotLine))
            self._crosshair_y = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#d7e2ec", style=Qt.PenStyle.DotLine))
            self.plot_widget.addItem(self._crosshair_x, ignoreBounds=True)
            self.plot_widget.addItem(self._crosshair_y, ignoreBounds=True)
        self._crosshair_x.setPos(xs[idx])
        self._crosshair_y.setPos(y)
        self.cursor_label.setText(f"t={xs[idx]:.7g} s · {shown:.10g} {unit} · 点序号≈{idx + 1}")

    def _parse_threshold(self) -> float:
        try:
            value = float(self.baseline_threshold.text().strip())
            if value < 0 or not math.isfinite(value):
                raise ValueError
            self.baseline_threshold.setStyleSheet("")
            return value
        except ValueError:
            self.baseline_threshold.setStyleSheet("border: 1px solid #e76f51;")
            return math.nan

    def _baseline_edited(self) -> None:
        text = self.baseline_edit.text().strip()
        if not text:
            self._baseline = None
        else:
            try:
                value = float(text)
                if not math.isfinite(value):
                    raise ValueError
                self._baseline = value
            except ValueError:
                self._show_error("基准值必须是有限SI数值，例如1.234e-3")
                return
        self._store_baseline("手动设置基准值")

    def _set_current_as_baseline(self) -> None:
        if self._last_value is None:
            self._show_error("还没有有效测量值")
            return
        self._baseline = self._last_value
        self.baseline_edit.setText(repr(self._baseline))
        self._store_baseline("当前读数设为基准")

    def _set_mean_as_baseline(self) -> None:
        stats = summarize(self._plot_y)
        if stats.mean is None:
            self._show_error("当前没有可用测量值")
            return
        self._baseline = stats.mean
        self.baseline_edit.setText(repr(self._baseline))
        self._store_baseline("可见曲线均值设为基准")

    def _store_baseline(self, event: str) -> None:
        self._profiles[self._current_mode.value]["baseline"] = self._baseline
        self._save_profiles()
        if self._baseline is None:
            self.baseline_line.hide()
            self.change_label.setText("未设置基准值")
        else:
            self.baseline_line.setValue(self._baseline)
            self.baseline_line.show()
        if self._experiment_active:
            if self._baseline is None:
                event_text = f"{event}: 已清除基准值"
            else:
                event_text = f"{event}: {self._baseline:.17g} {descriptor(self._current_mode).unit_si}"
            self._bridge.mark_event.emit(event_text)
            self._add_event_marker(event_text)

    def _clear_baseline(self) -> None:
        self._baseline = None
        self.baseline_edit.clear()
        self._store_baseline("清除基准值")

    def _mark_event(self) -> None:
        text = self.event_edit.text().strip()
        if not text:
            self._show_error("请输入事件内容")
            return
        if not self._experiment_active:
            self._show_error("请先开始一次实验，再记录事件")
            return
        self._bridge.mark_event.emit(text)
        self.event_edit.clear()
        self._add_event_marker(text)

    def _add_event_marker(self, text: str, elapsed_s: float | None = None) -> None:
        line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#e9c46a", width=1, style=Qt.PenStyle.DashLine),
        )
        label = pg.TextItem(
            text, color="#f5d27d", anchor=(0, 0), fill=pg.mkBrush("#1a2430"),
        )
        label.setZValue(line.zValue() + 1)
        marker_elapsed = self._last_elapsed if elapsed_s is None else float(elapsed_s)
        if not math.isfinite(marker_elapsed):
            raise ValueError("事件标记时间必须是有限数值")
        line.setPos(marker_elapsed)
        label.setPos(marker_elapsed, 0)
        self.plot_widget.addItem(line, ignoreBounds=True)
        self.plot_widget.addItem(label, ignoreBounds=True)
        self._event_lines.append(line)
        self._event_labels.append(label)
        self._position_event_labels()

    def _position_event_labels(self, *_args) -> None:
        if not self._event_labels:
            return
        view = self.plot_widget.getPlotItem().vb
        _, y_range = view.viewRange()
        y_min, y_max = y_range
        scene_rect = view.sceneBoundingRect()
        units_per_pixel_y = (y_max - y_min) / max(scene_rect.height(), 1.0)
        row_edges: list[float] = []
        markers = sorted(zip(self._event_lines, self._event_labels), key=lambda item: item[0].value())
        for line, label in markers:
            x_value = line.value()
            scene_x = view.mapViewToScene(QPointF(x_value, y_max)).x()
            label_width = label.mapRectToScene(label.boundingRect()).width()
            if not math.isfinite(label_width) or label_width <= 0:
                label_width = label.textItem.boundingRect().width()
            right_aligned = scene_x + label_width > scene_rect.right() - 3
            label.setAnchor((1, 0) if right_aligned else (0, 0))
            label_left = scene_x - label_width if right_aligned else scene_x
            row = next(
                (i for i, right_edge in enumerate(row_edges) if label_left >= right_edge + 5),
                len(row_edges),
            )
            if row == len(row_edges):
                row_edges.append(scene_x + label_width if not right_aligned else scene_x)
            else:
                row_edges[row] = scene_x + label_width if not right_aligned else scene_x
            y = y_max - (4 + row * 24) * units_per_pixel_y
            label.setPos(x_value, y)

    def _on_experiment_finished(self, directory: str) -> None:
        self._experiment_active = False
        self._experiment_csv_path = Path(directory) / "measurements.csv"
        self.statusBar().showMessage(f"实验数据已保存：{directory}", 12000)
        self._append_log(f"实验文件夹：{directory}")

    @Slot(str)
    def _on_experiment_started(self, directory: str) -> None:
        self._experiment_csv_path = Path(directory) / "measurements.csv"
        if self._region.isVisible():
            self._update_region_stats()

    def _choose_output_root(self) -> None:
        current = self.output_root.text()
        folder = QFileDialog.getExistingDirectory(self, "选择实验保存根目录", current)
        if folder:
            self.output_root.setText(folder)

    def _refresh_curve(self) -> None:
        self.curve.setData(list(self._plot_x), list(self._plot_y), autoDownsample=True, clipToView=True)

    def _clear_plot(self) -> None:
        self._region_stats_request_id += 1
        for task in self._region_stats_tasks.values():
            task.cancel()
        self._region_stats_scanning = False
        self._region_stats_pending_rows.clear()
        self._region_stats_values = None
        self._region_stats_last_sample_index = 0
        self._region_stats_bounds = None
        self._experiment_csv_path = None
        self._plot_x.clear()
        self._plot_y.clear()
        self._clear_event_markers()
        self._last_value = None
        self._last_elapsed = 0.0
        self._global_stats = {"count": 0, "minimum": None, "maximum": None, "mean": None, "sample_stddev": None}
        self.curve.setData([], [])
        self.plot_empty_overlay.show()
        for item in self.plot_widget.items():
            if isinstance(item, pg.InfiniteLine) and item is not self.baseline_line:
                self.plot_widget.removeItem(item)
        self.latest_value_label.setText("等待读数")
        self.actual_rate_label.setText("— Hz")
        self._region_stats_label.setText("局部统计：拖动图中区间手柄选择时间范围")
        self._update_stats()

    def _clear_event_markers(self) -> None:
        for item in (*self._event_lines, *self._event_labels):
            self.plot_widget.removeItem(item)
        self._event_lines.clear()
        self._event_labels.clear()

    def _load_history(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择实验原始测量 CSV", self.output_root.text(), "CSV 文件 (*.csv)")
        if not path:
            return

        def valid_samples():
            with open(path, newline="", encoding="utf-8-sig") as stream:
                reader = csv.DictReader(stream)
                for row in reader:
                    if row.get("status") != "ok" or not row.get("value_si"):
                        continue
                    value = float(row["value_si"])
                    elapsed = float(row["elapsed_s"])
                    if math.isfinite(value) and math.isfinite(elapsed):
                        yield elapsed, value, row.get("mode", "")

        try:
            stats = RunningStats()
            valid_count = 0
            first_elapsed = None
            last_elapsed = None
            last_value = None
            mode_value = None
            for elapsed, value, mode_value in valid_samples():
                first_elapsed = elapsed if first_elapsed is None else first_elapsed
                last_elapsed = elapsed
                last_value = value
                stats.add(value)
                valid_count += 1
            if not valid_count:
                raise ValueError("CSV中没有有效数值")

            history_events: list[tuple[float, str]] = []
            events_path = Path(path).with_name("events.csv")
            if events_path.is_file():
                try:
                    with events_path.open(newline="", encoding="utf-8-sig") as stream:
                        for event_row in csv.DictReader(stream):
                            event_text = str(event_row.get("event", "")).strip()
                            try:
                                event_elapsed = float(event_row.get("elapsed_s", ""))
                            except (TypeError, ValueError):
                                continue
                            if (
                                event_text and math.isfinite(event_elapsed)
                                and first_elapsed <= event_elapsed <= last_elapsed
                            ):
                                history_events.append((event_elapsed, event_text))
                except (OSError, UnicodeError, csv.Error) as exc:
                    self._append_log(f"! 关联events.csv读取失败，继续加载测量CSV：{exc}")

            max_plot_points = max(2, MAX_PLOT_POINTS)
            step = max(1, math.ceil((valid_count - 1) / (max_plot_points - 1)))
            xs: list[float] = []
            ys: list[float] = []
            valid_index = 0
            last_sample = None
            for sample in valid_samples():
                elapsed, value, _mode = sample
                if valid_index % step == 0:
                    xs.append(elapsed)
                    ys.append(value)
                valid_index += 1
                last_sample = (elapsed, value)
            if valid_index != valid_count:
                raise ValueError("CSV在加载过程中发生变化，请停止采集后重试")
            if last_sample is not None and (not xs or (xs[-1], ys[-1]) != last_sample):
                xs.append(last_sample[0])
                ys.append(last_sample[1])

            self._clear_plot()
            self._plot_x.extend(xs)
            self._plot_y.extend(ys)
            self._experiment_csv_path = Path(path)
            self._current_mode = Mode(mode_value or self._current_mode.value)
            self.mode_combo.blockSignals(True)
            self._set_combo_data(self.mode_combo, self._current_mode.value)
            self.mode_combo.blockSignals(False)
            self._load_mode_controls(self._current_mode)
            self._last_value = last_value
            self._last_elapsed = last_elapsed or 0.0
            self.plot_empty_overlay.hide()
            self._refresh_curve()
            self.follow_check.setChecked(False)
            self.plot_widget.setXRange(first_elapsed, last_elapsed if last_elapsed > first_elapsed else first_elapsed + 1, padding=0.01)
            for event_elapsed, event_text in history_events:
                self._add_event_marker(event_text, elapsed_s=event_elapsed)
            spec = descriptor(self._current_mode)
            self.plot_widget.setLabel("left", f"历史数据 · {spec.display_quantity}", units=spec.unit_si, color="#697470")
            displayed, unit = engineering_value(last_value, spec.unit_si)
            self.latest_value_label.setText(f"{displayed:.10g} {unit}")
            rate_span = last_elapsed - first_elapsed
            rate = (valid_count - 1) / rate_span if valid_count > 1 and rate_span > 0 else 0.0
            self.actual_rate_label.setText(f"{rate:.4g} Hz" if rate else "— Hz")
            if self._baseline is None:
                self.baseline_line.hide()
                self.change_label.setText("未设置基准值")
            else:
                self.baseline_line.setValue(self._baseline)
                self.baseline_line.show()
                delta = last_value - self._baseline
                percent = relative_change_percent(last_value, self._baseline, self._parse_threshold())
                d_display, d_unit = engineering_value(delta, spec.unit_si)
                relative = f"{percent:.8g}%" if percent is not None else "无效（基准接近零或阈值无效）"
                self.change_label.setText(f"Δ={d_display:.8g} {d_unit} · ΔX/X₀={relative}")
            self._update_stats(stats.as_dict())
            if self._region.isVisible():
                self._update_region_stats()
            event_summary = f"；恢复 {len(history_events):,} 条关联事件标记" if history_events else ""
            QMessageBox.information(
                self, "历史数据已加载",
                f"加载 {valid_count:,} 个有效原始读数；图中显示 {len(self._plot_x):,} 个抽稀点"
                f"{event_summary}，CSV文件未作改动。",
            )
        except Exception as exc:
            self._show_error(f"加载历史CSV失败：{exc}")

    def _save_screenshot(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存曲线截图", "Fluke8845A_curve.png", "PNG 图像 (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if not self.plot_widget.grab().save(path, "PNG"):
            self._show_error("曲线截图保存失败")
        else:
            self.statusBar().showMessage(f"截图已保存：{path}", 8000)

    def _diagnose(self) -> None:
        self._bridge.diagnose.emit()

    @Slot(str)
    def _show_diagnostic(self, text: str) -> None:
        QMessageBox.information(self, "8845A SCPI诊断", text)

    def _open_manual(self) -> None:
        path = self._find_document("user-guide.md")
        if path.exists():
            import os
            os.startfile(str(path))
        else:
            self.statusBar().showMessage("使用说明位于工程 docs/user-guide.md", 10000)

    def _open_license_notice(self) -> None:
        path = self._find_document("qt-licensing-and-third-party-notices.md")
        if path.exists():
            import os
            os.startfile(str(path))
        else:
            self.statusBar().showMessage("许可说明随包位于 docs/qt-licensing-and-third-party-notices.md", 10000)

    @staticmethod
    def _find_document(filename: str) -> Path:
        candidates = (
            Path(sys.argv[0]).resolve().parent / "docs" / filename,
            Path(__file__).resolve().parents[2] / "docs" / filename,
            Path.cwd() / "docs" / filename,
        )
        return next((path for path in candidates if path.is_file()), candidates[0])

    def _update_elapsed_label(self) -> None:
        seconds = int(self._last_elapsed)
        self.elapsed_label.setText(f"历时 {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.appendPlainText(f"{timestamp}  {message}")

    @Slot()
    def _on_scpi_logs_ready(self) -> None:
        lines, dropped = self._worker.take_scpi_log_update()
        if not lines and not dropped:
            return
        display_lines = []
        if dropped:
            display_lines.append(
                f"! SCPI显示日志缓冲曾丢弃较早的 {dropped} 行；测量读数和原始响应仍逐点保存在CSV中。"
            )
        display_lines.extend(f"{timestamp}  {message}" for timestamp, message in lines)
        self.log_text.appendPlainText("\n".join(display_lines))

    @Slot(str)
    def _show_error(self, message: str) -> None:
        self.statusBar().showMessage(message, 12000)
        self._append_log(f"! {message}")
        QMessageBox.warning(self, "操作未完成", message)

    @Slot()
    def _on_worker_thread_finished(self) -> None:
        self._shutdown_finished = True
        if self._shutdown_started:
            self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._shutdown_finished or not self._worker_thread.isRunning():
            event.accept()
            return

        event.ignore()
        if self._shutdown_started:
            return

        self._shutdown_started = True
        self._clock_timer.stop()
        for task in self._region_stats_tasks.values():
            task.cancel()
        self.setEnabled(False)
        self.setWindowTitle("正在安全关闭 · Fluke 8845A 科研测量与长期记录")
        self._append_log("正在等待当前读数结束、保存数据并关闭仪器连接…")
        self._worker.request_stop()
        self._bridge.close_app.emit()

