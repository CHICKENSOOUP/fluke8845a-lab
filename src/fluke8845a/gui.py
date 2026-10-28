"""Chinese high-DPI desktop interface for the 8845A logger."""

from __future__ import annotations

from dataclasses import replace

import bisect
import csv
import heapq
import json
import logging
import math
import os
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QObject, QRunnable, QSignalBlocker, QStandardPaths, Qt, QThread, QThreadPool, QTimer,
    Signal, Slot, QPointF, QRect, QRectF, QUrl, QPropertyAnimation, QEasingCurve,
)
from PySide6.QtGui import QAction, QDesktopServices, QFont, QFontDatabase, QFontMetrics, QKeySequence, QPainter, QPen, QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSpinBox, QStatusBar, QVBoxLayout, QWidget,
    QTabWidget, QToolButton, QStackedLayout, QGridLayout, QToolBar, QButtonGroup,
)

import numpy as np
import pyqtgraph as pg

from . import __version__
from .analysis import RunningStats, engineering_value, relative_change_percent, summarize
from .build_identity import desktop_build_label
from .filter_policy import (
    DIGITAL_FILTER_INFO,
    digital_filter_enable_error,
    digital_filter_firmware_support,
)
from .history_import import load_history_csv, load_history_csv_range
from .modes import (
    DESCRIPTORS, Mode, descriptor, mode_from_scpi_function,
    mode_transition_requires_wiring_confirmation, range_label,
)
from .mock import MockScenario
from .moving_average import MovingAverageTrace
from .curve_export_task import CurveExportTask
from .curve_export import detailed_curve_path
from .plot_history import downsample_plot_data
from .scpi import (
    FlowControl, InstrumentIdentityError, ModeSettings, SerialSettings,
    current_autorange_block_reason, current_firmware_gate_evidence_note,
    parse_idn, select_range_firmware,
    unsupported_current_ranges,
)
from .versioning import compare_firmware
from .share_export import export_share_package
from .state import AcquisitionState
from .storage import (
    complete_csv_prefix_size, find_existing_experiment_directories,
    estimate_recording_capacity_seconds, format_available_space,
    format_estimated_recording_capacity, get_available_space_bytes,
    require_minimum_free_space, prepare_experiment_storage,
    validate_experiment_name,
    write_excel_safe_events_csv, write_excel_safe_measurements_csv,
)
from .worker import MAX_PENDING_GUI_ROWS, MeasurementWorker

log = logging.getLogger(__name__)
_REGISTERED_UI_FONT_IDS: list[int] = []
AUTO_FOLLOW_WINDOW_S = 120.0
AUTO_FOLLOW_MIN_WINDOW_S = 10.0
VOLTAGE_PLOT_ABSOLUTE_LIMIT_V = 1000.0
MAX_LIVE_RENDER_POINTS = 12_000
EVENT_MARKER_ASYNC_THRESHOLD = 256
EVENT_MARKER_BATCH_SIZE = 64
EVENT_MARKER_BATCH_INTERVAL_MS = 1


def _classify_log_message(message: str) -> str:
    text = str(message).strip()
    upper = text.upper()
    lower = text.casefold()
    if text.startswith("!") or any(token in lower for token in ("超时", "断线", "失败", "异常", "error", "timeout", "exception")):
        return "error"
    if text.startswith((">", "<")) or "SCPI" in upper:
        return "scpi"
    if any(token in lower for token in ("com", "串口", "连接", "断开", "识别", "computer", "local")):
        return "connection"
    return "acquisition"


def _window_dimensions_for_available_screen(
    available_width: int, available_height: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Return initial and minimum window dimensions that fit a display work area."""
    width = max(1, int(available_width) - 24)
    height = max(1, int(available_height) - 72)
    initial_size = (min(1540, width), min(960, height))
    minimum_size = (min(1040, initial_size[0]), min(700, initial_size[1]))
    return initial_size, minimum_size


def _available_geometry_for_widget(widget: QWidget):
    """Return the logical work area for a widget's screen, with a primary-screen fallback."""
    screen = widget.screen()
    if screen is None:
        screen = QApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def _fit_dialog_to_available_geometry(dialog: QDialog, available=None) -> None:
    """Clamp and center a secondary dialog within the current screen work area."""
    if available is None:
        available = _available_geometry_for_widget(dialog)
    if available is None:
        return
    # Leave a small allowance for native frame decorations, which are included
    # in frameGeometry() but not in QWidget.resize() on Windows.
    max_width = max(1, available.width() - 32)
    max_height = max(1, available.height() - 80)
    dialog.setMinimumSize(min(320, max_width), min(220, max_height))
    dialog.setMaximumSize(max_width, max_height)
    width = min(max(dialog.width(), dialog.minimumWidth()), max_width)
    height = min(max(dialog.height(), dialog.minimumHeight()), max_height)
    dialog.resize(width, height)
    dialog.move(
        available.x() + max(0, (available.width() - width) // 2),
        available.y() + max(0, (available.height() - height) // 2),
    )


def _finite_number(value) -> float | None:
    """Convert a saved numeric value without letting extreme integers crash startup."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_region_sample_index(value) -> int | None:
    """Accept only positive integer sample IDs from CSV or worker payloads."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        token = value.strip()
        if token and token.isascii() and token.isdecimal():
            try:
                parsed = int(token)
            except ValueError:
                return None
            return parsed if parsed > 0 else None
    return None


def _region_sample_rows_are_contiguous(rows: Iterable[dict], last_sample_index: int) -> bool:
    """Return whether incremental GUI rows cover every attempt after a CSV snapshot."""
    expected = last_sample_index + 1
    for row in rows:
        try:
            sample_index = _parse_region_sample_index(row.get("sample_index"))
        except AttributeError:
            sample_index = None
        if sample_index is None:
            return False
        if sample_index < expected:
            continue
        if sample_index != expected:
            return False
        expected += 1
    return True


def _region_successful_row_values(row: dict, sample_index: int) -> tuple[float, float]:
    """Parse a successful raw row or fail instead of silently understating statistics."""
    parsed = {}
    for field in ("elapsed_s", "value_si"):
        try:
            value = float(row[field])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"成功样本 #{sample_index} 的 {field} 无效") from exc
        if not math.isfinite(value):
            raise ValueError(f"成功样本 #{sample_index} 的 {field} 不是有限数值")
        parsed[field] = value
    return parsed["elapsed_s"], parsed["value_si"]


def _plot_y_margin(min_y: float, max_y: float) -> float:
    """Keep a useful panning area around flat data without limiting manual spans to a few ULPs."""
    y_span = max(max_y - min_y, abs(min_y) * 1e-12, abs(max_y) * 1e-12, 1e-12)
    magnitude_margin = max(abs(min_y), abs(max_y)) * 1e-6
    return max(y_span * 0.08, magnitude_margin, 1e-15)


def _plot_y_view_limits(min_y: float, max_y: float) -> tuple[float, float]:
    """Allow vertical zoom-out to the finite coordinate boundary, not a data-relative cap."""
    finite_bound = sys.float_info.max / 4
    center = min(finite_bound, max(-finite_bound, min_y / 2 + max_y / 2))
    return max(-finite_bound, center - finite_bound), min(finite_bound, center + finite_bound)


def _voltage_plot_value_is_visible(value: float) -> bool:
    return math.isfinite(value) and abs(value) <= VOLTAGE_PLOT_ABSOLUTE_LIMIT_V


def _mode_plot_y_view_limits(
    mode: Mode | str, origin: float, min_y: float, max_y: float,
) -> tuple[float, float]:
    """Return local ViewBox limits while keeping voltage's absolute viewport within ±1000 V."""
    if descriptor(mode).quantity == "voltage":
        limit = VOLTAGE_PLOT_ABSOLUTE_LIMIT_V
        safe_origin = origin if math.isfinite(origin) and abs(origin) <= limit else 0.0
        return -limit - safe_origin, limit - safe_origin
    return _plot_y_view_limits(min_y, max_y)


def _engineering_axis_scale(span: float, unit_si: str) -> tuple[float, str]:
    """Choose an engineering unit for a visible absolute magnitude."""
    base_unit = "Ω" if unit_si == "Ohm" else unit_si
    if not math.isfinite(span) or span <= 0 or unit_si not in {"V", "A", "Ohm"}:
        return 1.0, base_unit
    maximum_exponent = 9 if unit_si == "Ohm" else 3
    exponent = math.floor(math.log10(span) / 3) * 3
    exponent = min(maximum_exponent, max(-12, exponent))
    prefix = {
        -12: "p", -9: "n", -6: "µ", -3: "m", 0: "", 3: "k", 6: "M", 9: "G",
    }[exponent]
    return 10.0 ** exponent, f"{prefix}{base_unit}"


def _format_axis_tick(value: Decimal, step: float) -> str:
    if not value.is_finite():
        return str(value)
    magnitude = abs(value)
    try:
        float_value = float(value)
    except (OverflowError, ValueError):
        float_value = math.inf
    if not math.isfinite(float_value) or magnitude >= Decimal("1e12") or (magnitude and magnitude < Decimal("1e-6")):
        return format(value, ".6g")
    places = 0
    if math.isfinite(step) and 0 < step < 1:
        places = max(0, min(18, int(math.ceil(-math.log10(step)))))
    quantum = Decimal(1).scaleb(-places)
    with localcontext() as context:
        context.prec = max(32, len(value.as_tuple().digits) + places + 8)
        try:
            label = format(value.quantize(quantum), "f")
        except ArithmeticError:
            return format(value, ".6g")
    if "." in label:
        label = label.rstrip("0").rstrip(".")
    return label or "0"


def _copy_complete_csv_prefix(source: Path, destination: Path, *, chunk_size: int = 1024 * 1024) -> int:
    """Copy only complete CSV records from a file that may still be growing."""
    source = Path(source)
    destination = Path(destination)
    complete_end = complete_csv_prefix_size(source, chunk_size=chunk_size)
    if complete_end <= 0:
        raise ValueError("当前 CSV 尚无完整表头，无法导出快照")
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        remaining = complete_end
        while remaining:
            block = input_stream.read(min(chunk_size, remaining))
            if not block:
                raise OSError("快照期间原始 CSV 发生变化，未能复制完整数据")
            output_stream.write(block)
            remaining -= len(block)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    return complete_end


def _format_sampling_interval(rate_hz: float) -> str:
    """Format the measured interval between consecutive samples from a rate."""
    try:
        rate = float(rate_hz)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(rate) or rate <= 0:
        return "—"

    try:
        interval_s = 1.0 / rate
    except OverflowError:
        return "—"
    if not math.isfinite(interval_s):
        return "—"
    if interval_s >= 3600:
        return f"{interval_s / 3600:.4g} h"
    if interval_s >= 60:
        return f"{interval_s / 60:.4g} min"
    if interval_s >= 1:
        return f"{interval_s:.4g} s"
    if interval_s >= 1e-3:
        return f"{interval_s * 1e3:.4g} ms"
    return f"{interval_s * 1e6:.4g} µs"


def _wheel_zoom_axes(modifiers) -> tuple[bool, bool]:
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        return False, True
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        return True, False
    return True, True


class _MeasurementViewBox(pg.ViewBox):
    """Wheel zoom: both axes by default, Shift=X, Ctrl=Y; Alt has no special mapping."""

    userZoomed = Signal(bool, bool)
    dragFinished = Signal()
    rebaseRequested = Signal(float, object)
    yZoomLimitReached = Signal(float)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.zero_y = 0.0
        self.y_anchor_tolerance = 0.0
        self.pan_x_data_bounds: tuple[float, float] | None = None
        self.pan_y_data_bounds: tuple[float, float] | None = None
        self.y_anchor_provider = None
        self._last_zoom_rebase_changed = False
        self._last_zoom_rebase_delta = 0.0
        self.drag_in_progress = False
        self._drag_start_y_range = None

    def mouseDragEvent(self, event, axis=None) -> None:
        if event.isStart():
            self.drag_in_progress = True
            self._drag_start_y_range = tuple(self.viewRange()[1])
        try:
            super().mouseDragEvent(event, axis=axis)
        finally:
            if event.isFinish():
                self.drag_in_progress = False
                y_changed = self._drag_start_y_range is not None and tuple(self.viewRange()[1]) != self._drag_start_y_range
                self._drag_start_y_range = None
                self._last_drag_y_changed = y_changed
                self.dragFinished.emit()

    def set_pan_x_data_bounds(self, bounds: tuple[float, float] | None) -> None:
        if bounds is None:
            self.pan_x_data_bounds = None
            return
        lower, upper = (float(value) for value in bounds)
        if math.isfinite(lower) and math.isfinite(upper) and lower <= upper:
            self.pan_x_data_bounds = (lower, upper)
        else:
            self.pan_x_data_bounds = None

    def set_pan_y_data_bounds(self, bounds: tuple[float, float] | None) -> None:
        if bounds is None:
            self.pan_y_data_bounds = None
            return
        lower, upper = (float(value) for value in bounds)
        if math.isfinite(lower) and math.isfinite(upper) and lower <= upper:
            self.pan_y_data_bounds = (lower, upper)
        else:
            self.pan_y_data_bounds = None

    def _clamp_pan_delta(self, axis: int, delta, bounds: tuple[float, float] | None):
        if bounds is None or delta is None or not math.isfinite(float(delta)):
            return delta
        view_low, view_high = self.targetRange()[axis]
        span = float(view_high - view_low)
        center = float(view_low / 2 + view_high / 2)
        if not math.isfinite(span) or span <= 0 or not math.isfinite(center):
            return delta

        # Keep part of the data envelope visible during panning, while allowing
        # the zoom range itself to remain unchanged.
        center_margin = span * 0.3
        desired_center = center + float(delta)
        min_center = bounds[0] - center_margin
        max_center = bounds[1] + center_margin
        if not all(math.isfinite(value) for value in (desired_center, min_center, max_center)):
            return delta
        adjusted_center = min(max(desired_center, min_center), max_center)
        return adjusted_center - center

    def translateBy(self, t=None, x=None, y=None) -> None:
        tuple_translation = t is not None
        if tuple_translation:
            x, y = t[0], t[1]
        x = self._clamp_pan_delta(0, x, self.pan_x_data_bounds)
        y = self._clamp_pan_delta(1, y, self.pan_y_data_bounds)
        if tuple_translation:
            t = (x, y)
            x = y = None
        super().translateBy(t=t, x=x, y=y)

    def _scale_y_by(self, factor: float, center_y: float) -> None:
        if not math.isfinite(factor) or factor <= 0:
            return
        y_min, y_max = self.viewRange()[1]
        current_span = y_max - y_min
        if not math.isfinite(current_span) or current_span <= 0:
            return

        reference = max(abs(center_y), abs(y_min), abs(y_max))
        # Coordinates are offset from the first plotted reading, so a fixed
        # absolute floor is meaningful even when the actual reading is large.
        minimum_span = math.ulp(reference)
        requested_span = current_span * factor
        y_limits = self._effectiveLimits()[1]
        lower, upper = y_limits
        maximum_span = None
        if lower is not None and upper is not None:
            candidate_span = upper - lower
            if math.isfinite(candidate_span) and candidate_span > 0:
                maximum_span = candidate_span
        if not math.isfinite(requested_span):
            if maximum_span is None:
                return
            requested_span = maximum_span
        elif maximum_span is not None:
            requested_span = min(requested_span, maximum_span)
        span = max(minimum_span, requested_span)
        relative = (center_y - y_min) / current_span
        if not math.isfinite(relative):
            return
        relative = min(1.0, max(0.0, relative))
        if requested_span <= minimum_span * 4:
            half_span = minimum_span / 2
            low, high = center_y - half_span, center_y + half_span
            if high <= low:
                low = math.nextafter(center_y, -math.inf)
                high = math.nextafter(center_y, math.inf)
        else:
            low = center_y - span * relative
            high = center_y + span * (1 - relative)
            if high <= low:
                low = math.nextafter(center_y, -math.inf)
                high = math.nextafter(center_y, math.inf)

        if lower is not None and upper is not None and high - low > upper - lower:
            low, high = lower, upper
        elif lower is not None and low < lower:
            shift = lower - low
            low += shift
            high += shift
        if upper is not None and high > upper:
            shift = high - upper
            low -= shift
            high -= shift

        self.state["targetRange"][1] = [low, high]
        self.updateViewRange(forceY=True)

    def wheelEvent(self, event, axis=None) -> None:
        delta = event.delta()
        if not delta:
            event.ignore()
            return
        modifiers = event.modifiers()
        zoom_x, zoom_y = _wheel_zoom_axes(modifiers)
        if zoom_x:
            self.enableAutoRange(x=False)
        if zoom_y:
            # Resolve a pending live auto-range and take manual control before
            # mapping the cursor anchor or applying this wheel step. Disabling
            # auto-range after scaling can execute that pending update and erase
            # the very zoom this event just requested.
            self.enableAutoRange(y=False)
        factor = math.exp(-float(delta) / 120.0 * 0.18)
        scene_position = event.scenePos()
        center = self.mapSceneToView(scene_position)
        scene_bounds = self.sceneBoundingRect()
        x_min, x_max = self.viewRange()[0]
        y_min, y_max = self.viewRange()[1]
        # Wheel events from axis tick labels are forwarded to the ViewBox.
        # Their scene position can be just outside the plot rectangle, and
        # mapping that coordinate back to data can produce a tiny negative
        # offset that shifts the whole curve out of view during Y zoom.
        if scene_position.x() < scene_bounds.left() or scene_position.x() > scene_bounds.right():
            center.setX(x_min + (x_max - x_min) / 2)
        if scene_position.y() < scene_bounds.top() or scene_position.y() > scene_bounds.bottom():
            center.setY(y_min + (y_max - y_min) / 2)
        has_curve_anchor = False
        absolute_curve_anchor = None
        if zoom_y:
            # Anchor vertical zoom to a recorded sample near the pointer's
            # time. A linearly interpolated value is only a display estimate;
            # rebasing to it can leave a sub-ULP residual around a large
            # absolute reading and make further zoom impossible.
            provider = self.y_anchor_provider
            anchor = provider(float(center.x())) if callable(provider) else None
            if isinstance(anchor, (tuple, list)) and len(anchor) == 2:
                anchor, absolute_curve_anchor = anchor
            if anchor is not None and math.isfinite(float(anchor)):
                center.setY(float(anchor))
                has_curve_anchor = True
            else:
                absolute_curve_anchor = None
                center.setY(y_min / 2 + y_max / 2)
        # Use one stable wheel mapping across the plot and its axis items:
        # plain zooms both axes, Shift zooms time, and Ctrl zooms Y only. PyQtGraph
        # forwards axis-item events with an axis index, which otherwise makes
        # a plain wheel over the time ticks appear to stop vertical zooming.
        if zoom_x and not math.isfinite(center.x()):
            center.setX(x_min + (x_max - x_min) / 2)
        if zoom_y and not math.isfinite(center.y()):
            center.setY(y_min + (y_max - y_min) / 2)
        x_factor = factor if zoom_x else 1.0
        y_factor = factor if zoom_y else 1.0
        if zoom_y and factor < 1.0:
            y_anchor = center.y()
            y_min, y_max = self.viewRange()[1]
            reference = max(abs(y_anchor), abs(y_min), abs(y_max))
            floor = math.ulp(reference)
            if (y_max - y_min) * factor < floor * 16:
                self._last_zoom_rebase_changed = False
                self._last_zoom_rebase_delta = 0.0
                self.rebaseRequested.emit(float(y_anchor), absolute_curve_anchor)
                if self._last_zoom_rebase_changed:
                    # Rebasing changes only the Y data origin. Preserve the
                    # selected vertical anchor using the actual origin delta;
                    # remapping the mouse position here would replace a sample
                    # anchor with the cursor's screen Y for Ctrl+wheel.
                    center.setY(center.y() - self._last_zoom_rebase_delta)
        if not has_curve_anchor and abs(center.y()) <= self.y_anchor_tolerance:
            # Qt's scene-to-view transform can accumulate sub-ULP drift at a
            # fixed mouse position. Snap only fallback coordinates too small
            # to distinguish from the plotted origin; a curve-provided sample
            # is raw data and must retain even a one-ULP offset.
            center.setY(0.0)
        if zoom_x:
            self.scaleBy(x=x_factor, center=center)
        if zoom_y:
            previous_y_range = self.viewRange()[1]
            previous_span = previous_y_range[1] - previous_y_range[0]
            self._scale_y_by(y_factor, center.y())
            current_y_range = self.viewRange()[1]
            current_span = current_y_range[1] - current_y_range[0]
            reference = max(abs(center.y()), abs(current_y_range[0]), abs(current_y_range[1]))
            precision_floor = math.ulp(reference)
            if (
                factor < 1.0
                and current_span >= previous_span
                and current_span <= precision_floor * 16
            ):
                self.yZoomLimitReached.emit(float(current_span))
        self.userZoomed.emit(zoom_x, zoom_y)
        self.sigRangeChangedManually.emit([zoom_x, zoom_y])
        event.accept()


class _MeasurementPlotWidget(pg.PlotWidget):
    """Plot widget with predictable keyboard focus and chart shortcuts."""

    def __init__(self, *, view_box: _MeasurementViewBox, parent=None) -> None:
        super().__init__(parent=parent, viewBox=view_box, axisItems={
            "left": _OffsetValueAxis("left"), "bottom": _OffsetValueAxis("bottom"),
        })
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.key_handler = None

    def mousePressEvent(self, event) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        handler = self.key_handler
        if handler is not None and handler(event):
            event.accept()
            return
        super().keyPressEvent(event)


class _OffsetValueAxis(pg.AxisItem):
    """Render offset plot coordinates as absolute readings or explicit offsets."""

    def __init__(self, orientation: str, **kwargs) -> None:
        self.value_offset = 0.0
        self.display_factor = 1.0
        self.display_unit = ""
        super().__init__(orientation=orientation, **kwargs)
        self.enableAutoSIPrefix(False)

    def tickSpacing(self, minVal, maxVal, size):
        levels = super().tickSpacing(minVal, maxVal, size)
        if not levels or all(math.isfinite(spacing) and spacing > 0 for spacing, _offset in levels):
            return levels
        span = abs(float(maxVal) - float(minVal))
        if not math.isfinite(span) or span <= 0:
            return []
        spacing = span / 2
        if not math.isfinite(spacing) or spacing <= 0:
            spacing = span
        return [(spacing, 0.0)]

    def tickStrings(self, values, scale, spacing):
        offset = float(self.value_offset)
        factor = float(self.display_factor)
        if not math.isfinite(factor) or factor <= 0:
            factor = 1.0
        if not math.isfinite(offset):
            return super().tickStrings(values, scale, spacing)

        if offset == 0.0 and factor == 1.0:
            return super().tickStrings(values, scale, spacing)

        step = abs(float(spacing)) / factor
        if not math.isfinite(step) or step <= 0:
            step = 0.0
        factor_decimal = Decimal(str(factor))
        offset_decimal = Decimal(str(offset)) / factor_decimal
        relative = offset != 0.0 and (
            (math.isfinite(step) and step > 0 and abs(offset_decimal) > Decimal(str(step)) * Decimal("1e6"))
            or abs(float(spacing)) < math.ulp(offset) * 16
        )
        result = []
        for raw_value in values:
            value = float(raw_value)
            if not math.isfinite(value):
                result.append(f"{value:.6g}")
                continue
            delta = Decimal(str(value)) / factor_decimal
            absolute = (Decimal(str(offset)) + Decimal(str(value))) / factor_decimal
            shown = delta if relative else absolute
            label = _format_axis_tick(shown, step)
            if relative and shown > 0:
                label = f"+{label}"
            result.append(label)
        return result


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
                    sample_index = _parse_region_sample_index(row.get("sample_index"))
                    if sample_index is None:
                        raise ValueError("测量CSV含无效样本序号，无法保证区间统计完整")
                    expected_sample_index = last_sample_index + 1
                    if sample_index != expected_sample_index:
                        raise ValueError(
                            f"测量CSV样本序号不连续：期望 {expected_sample_index}，实际 {sample_index}"
                        )
                    last_sample_index = sample_index
                    if row.get("status") != "ok" or row.get("mode") != self.mode.value:
                        continue
                    elapsed, value = _region_successful_row_values(row, sample_index)
                    if self.left <= elapsed <= self.right:
                        stats.add(value)
            result = {"stats": stats, "last_sample_index": last_sample_index}
        except Exception as exc:
            error = str(exc)
        self.signals.completed.emit(self.request_id, result, error)


class _CsvSnapshotSignals(QObject):
    completed = Signal(str, str, str, int, object)


def _validate_snapshot_destinations(source: Path, destination: Path, sidecar: Path) -> None:
    source = Path(source)
    raw_files = {
        name: source.parent / name
        for name in ("measurements.csv", "events.csv", "config.json", "run.log")
    }
    raw_files.setdefault(source.name, source)
    for target in (Path(destination), Path(sidecar)):
        try:
            target_is_directory = target.is_dir()
        except (OSError, RuntimeError, ValueError) as exc:
            raise OSError(f"无法确认快照目标是否为目录：{target}：{exc}") from exc
        if target_is_directory:
            raise ValueError(f"快照目标不能是目录：{target}")
        for raw_name, raw_path in raw_files.items():
            try:
                same_file = raw_path.resolve(strict=False) == target.resolve(strict=False)
                if not same_file:
                    try:
                        same_file = os.path.samefile(raw_path, target)
                    except FileNotFoundError:
                        same_file = False
            except (OSError, RuntimeError, ValueError) as exc:
                raise OSError(
                    f"无法确认快照目标是否与原始实验文件相同：{target}：{exc}"
                ) from exc
            if same_file:
                raise ValueError(
                    f"快照文件或元数据不能覆盖当前实验的原始文件 {raw_name}：{target}"
                )


class _CsvSnapshotTask(QRunnable):
    """Export a stable complete-record prefix without blocking the GUI thread."""

    def __init__(
        self, source: Path, destination: Path, snapshot_time: str, *, allow_overwrite: bool,
    ) -> None:
        super().__init__()
        self.source = source
        self.destination = destination
        self.snapshot_time = snapshot_time
        self.allow_overwrite = allow_overwrite
        self.signals = _CsvSnapshotSignals()

    @staticmethod
    def _staging_path(directory: Path, prefix: str) -> Path:
        with tempfile.NamedTemporaryFile(prefix=prefix, suffix=".tmp", dir=directory, delete=False) as handle:
            path = Path(handle.name)
        path.unlink()
        return path

    def run(self) -> None:
        sidecar = Path(str(self.destination) + ".snapshot.json")
        csv_stage = None
        metadata_stage = None
        backups: list[tuple[Path, Path]] = []
        published: list[Path] = []
        committed = False
        error = None
        copied_bytes = 0
        try:
            _validate_snapshot_destinations(self.source, self.destination, sidecar)
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            csv_stage = self._staging_path(self.destination.parent, ".8845a-snapshot-")
            metadata_stage = self._staging_path(self.destination.parent, ".8845a-snapshot-meta-")
            copied_bytes = _copy_complete_csv_prefix(self.source, csv_stage)
            last_sample_index = 0
            with csv_stage.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or "sample_index" not in reader.fieldnames:
                    raise ValueError("快照 CSV 缺少 sample_index 列，无法标注快照样本边界")
                for row in reader:
                    try:
                        last_sample_index = max(last_sample_index, int(row["sample_index"]))
                    except (KeyError, TypeError, ValueError):
                        continue
            metadata_stage.write_text(json.dumps({
                "export_state": "in-progress-snapshot",
                "snapshot_time_local": self.snapshot_time,
                "source_file": str(self.source),
                "last_sample_index_at_export": last_sample_index,
                "complete_csv_bytes": copied_bytes,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if self.allow_overwrite:
                for target in (self.destination, sidecar):
                    if target.exists():
                        backup = self._staging_path(target.parent, ".8845a-snapshot-backup-")
                        os.replace(target, backup)
                        backups.append((backup, target))
                os.replace(csv_stage, self.destination)
                published.append(self.destination)
                csv_stage = None
                os.replace(metadata_stage, sidecar)
                published.append(sidecar)
                metadata_stage = None
            else:
                if os.name == "nt":
                    os.rename(csv_stage, self.destination)
                    published.append(self.destination)
                    csv_stage = None
                else:
                    os.link(csv_stage, self.destination)
                    published.append(self.destination)
                    csv_stage.unlink()
                    csv_stage = None
                if os.name == "nt":
                    os.rename(metadata_stage, sidecar)
                    published.append(sidecar)
                    metadata_stage = None
                else:
                    os.link(metadata_stage, sidecar)
                    published.append(sidecar)
                    metadata_stage.unlink()
                    metadata_stage = None
            committed = True
        except Exception as exc:
            error = str(exc)
            rollback_errors = []
            for target in reversed(published):
                try:
                    target.unlink(missing_ok=True)
                except OSError as rollback_exc:
                    rollback_errors.append(f"删除不完整快照 {target} 失败：{rollback_exc}")
            for backup, target in reversed(backups):
                try:
                    if backup.exists():
                        os.replace(backup, target)
                except OSError as rollback_exc:
                    rollback_errors.append(f"恢复旧快照 {target} 失败，备份保留在 {backup}：{rollback_exc}")
            if rollback_errors:
                error = f"{error}；回滚未完成：{'；'.join(rollback_errors)}"
        finally:
            cleanup_errors = []
            for path in (csv_stage, metadata_stage):
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as cleanup_exc:
                        cleanup_errors.append(f"清理快照暂存文件 {path} 失败：{cleanup_exc}")
            if committed:
                for backup, _target in backups:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError:
                        pass
            if cleanup_errors:
                details = "；".join(cleanup_errors)
                error = f"{error}；{details}" if error else details
        self.signals.completed.emit(
            str(self.destination), str(sidecar), self.snapshot_time, copied_bytes, error
        )


class _SafeEventsExportSignals(QObject):
    completed = Signal(str, object)


class _SafeEventsExportTask(QRunnable):
    """Stream a spreadsheet-safe event copy without blocking the GUI thread."""

    def __init__(self, source: Path, destination: Path) -> None:
        super().__init__()
        self.source = source
        self.destination = destination
        self.signals = _SafeEventsExportSignals()

    def run(self) -> None:
        error = None
        try:
            write_excel_safe_events_csv(self.source, self.destination)
        except Exception as exc:
            error = str(exc)
        self.signals.completed.emit(str(self.destination), error)


class _SafeMeasurementsExportTask(QRunnable):
    """Stream a spreadsheet-safe measurement copy outside the GUI thread."""

    def __init__(self, source: Path, destination: Path) -> None:
        super().__init__()
        self.source = source
        self.destination = destination
        self.signals = _SafeEventsExportSignals()

    def run(self) -> None:
        error = None
        try:
            write_excel_safe_measurements_csv(self.source, self.destination)
        except Exception as exc:
            error = str(exc)
        self.signals.completed.emit(str(self.destination), error)


class _SharePackageExportTask(QRunnable):
    """Create a redacted, fixed-content share package away from the GUI thread."""

    def __init__(self, source_directory: Path, destination: Path) -> None:
        super().__init__()
        self.source_directory = source_directory
        self.destination = destination
        self.signals = _SafeEventsExportSignals()

    def run(self) -> None:
        error = None
        try:
            export_share_package(self.source_directory, self.destination)
        except Exception as exc:
            error = str(exc)
        self.signals.completed.emit(str(self.destination), error)


class _HistoryLoadSignals(QObject):
    completed = Signal(str, object, object)


class _HistoryLoadTask(QRunnable):
    """Parse and downsample history off the GUI thread; return plain data only."""

    def __init__(self, path: str, fallback_mode: Mode, max_plot_points: int) -> None:
        super().__init__()
        self.path = path
        self.fallback_mode = fallback_mode
        self.max_plot_points = max_plot_points
        self.signals = _HistoryLoadSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            result = load_history_csv(
                self.path, self.fallback_mode, self.max_plot_points,
                is_cancelled=lambda: self._cancelled,
            )
            self.signals.completed.emit(self.path, result, None)
        except Exception as exc:
            self.signals.completed.emit(self.path, None, str(exc))


class _HistoryRangeLoadSignals(QObject):
    completed = Signal(int, str, object, object)


class _HistoryRangeLoadTask(QRunnable):
    """Read a bounded history window off the GUI thread; keep raw CSV untouched."""

    def __init__(
        self, request_id: int, path: Path, left: float, right: float,
        max_plot_points: int, range_index=None,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.path = path
        self.left = left
        self.right = right
        self.max_plot_points = max_plot_points
        self.range_index = range_index
        self.signals = _HistoryRangeLoadSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            result = load_history_csv_range(
                self.path, self.left, self.right, self.max_plot_points,
                is_cancelled=lambda: self._cancelled,
                range_index=self.range_index,
            )
            self.signals.completed.emit(self.request_id, str(self.path), result, None)
        except Exception as exc:
            self.signals.completed.emit(self.request_id, str(self.path), None, str(exc))


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
        self.setObjectName("wiringConfirmationDialog")
        self.instructions = instructions
        self.setWindowTitle("确认测量接线")
        self.setModal(True)
        self.resize(1040, 475)
        layout = QVBoxLayout(self)
        self.instructions_label = QLabel(instructions)
        self.instructions_label.setWordWrap(True)
        layout.addWidget(self.instructions_label)
        self.schematic = WiringSchematicWidget(
            mode, self, current_range_si=current_range_si, autorange=autorange,
        )
        available = _available_geometry_for_widget(self)
        if available is not None:
            max_width = max(1, available.width() - 24)
            max_height = max(1, available.height() - 72)
            self.schematic.setMinimumSize(
                min(860, max(1, max_width - 36)),
                min(275, max(120, max_height - 270)),
            )
        layout.addWidget(self.schematic, 1)
        self.note_label = QLabel("示意图依据 Fluke 8845A《Users Manual》中文本第 4-4、4-9 至 4-11 页整理。SCPI可回读量程与FRONT/REAR，不能确认表笔接入哪个电流孔，也不能检测保险丝或外部回路。")
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet("color: #596762;")
        layout.addWidget(self.note_label)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("我已断电并核对接线")
        self.button_box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)
        _fit_dialog_to_available_geometry(self, available)


class WorkerBridge(QObject):
    connect_real = Signal(str, object, object, bool, object)
    connect_mock = Signal(object, object)
    set_control_mode = Signal(str)
    set_firmware_versions = Signal(str, str)
    configure = Signal(object, object)
    configure_unconfirmed_mode = Signal(object)
    start_experiment = Signal(object, str, str, object, bool, str, str, object)
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
        frozen = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
        build_label = desktop_build_label(Path(sys.executable), __version__, frozen=frozen)
        self._base_window_title = f"Fluke 8845A 科研测量与长期记录 · {build_label}"
        self.setWindowTitle(self._base_window_title)
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            initial_size, minimum_size = _window_dimensions_for_available_screen(
                area.width(), area.height()
            )
        else:
            initial_size, minimum_size = (1540, 960), (1040, 700)
        self.setMinimumSize(*minimum_size)
        self.resize(*initial_size)
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
        self._serial_profile_port = ""
        self._loading_serial_profile = False
        firmware_profiles = self._profiles.get("instrument_firmware_by_serial", {})
        self._firmware_profiles = firmware_profiles if isinstance(firmware_profiles, dict) else {}
        self._current_mode = Mode.DCV
        self._plot_data_mode: Mode | None = None
        self._connected = False
        self._control_mode = "disconnected"
        self._control_mode_switch_pending = False
        self._simulation = False
        self._instrument_mode_readback: Mode | None = None
        self._connected_mock_scenario = ""
        self._instrument_mode_known = False
        self._active_settings_by_mode: dict[str, dict] = {}
        self._firmware = ""
        self._idn_version_candidate = ""
        self._device_serial = ""
        self._terminals = ""
        self._loading_mode_controls = False
        self._acquiring = False
        self._single_shot_running = False
        self._paused = False
        self._speed_update_pending = False
        self._diagnostic_pending = False
        self._configure_pending = False
        self._zero_pending = False
        self._start_pending = False
        self._locked_interval_s: float | None = None
        self._finalization_pending = False
        self._initial_connection_prompt_shown = False
        self._connection_pending = False
        self._connection_stage_status: dict[str, tuple[str, str]] = {}
        self._last_run_single = False
        self._user_stop_pending = False
        self._reset_plot_after_save = False
        self._new_measurement_after_save = False
        self._new_measurement_stop_finished = False
        self._new_measurement_save_succeeded = False
        self._new_measurement_save_directory = ""
        self._experiment_name_valid = True
        self._active_port = ""
        self._has_connected_before = False
        self._experiment_active = False
        self._event_save_pending = False
        self._last_value: float | None = None
        self._last_sample_index = 0
        self._last_actual_range_si: float | None = None
        self._latest_invalid_log_text = ""
        self._last_acquisition_issue = ""
        self._baseline: float | None = None
        self._plot_x: deque[float] = deque()
        self._plot_y: deque[float] = deque()
        self._average_trace = MovingAverageTrace(capacity=MAX_PLOT_POINTS)
        self._average_last_refresh = 0.0
        self._last_plot_frame_refresh = 0.0
        self._plot_frame_timer = QTimer(self)
        self._plot_frame_timer.setSingleShot(True)
        self._plot_frame_timer.timeout.connect(self._refresh_live_plot_now)
        self._y_axis_fit_timer = QTimer(self)
        self._y_axis_fit_timer.setSingleShot(True)
        self._y_axis_fit_timer.timeout.connect(lambda: self._update_chart_auto_ranges(update_x=False))
        self._average_refresh_timer = QTimer(self)
        self._average_refresh_timer.setSingleShot(True)
        self._average_refresh_timer.timeout.connect(self._refresh_average_now)
        self._plot_y_anchor_x = np.asarray([], dtype=float)
        self._plot_y_anchor_values = np.asarray([], dtype=float)
        self._plot_y_origin: float | None = None
        self._plot_x_nondecreasing = True
        self._plot_x_uniformly_spaced = True
        self._last_plot_zoom_limit_notice_s = 0.0
        self._plot_metadata: deque[dict] = deque()
        self._plot_break_before: deque[bool] = deque()
        self._plot_break_pending = False
        self._live_plot_snapshot: dict | None = None
        self._history_mode = False
        self._history_overview_data: dict | None = None
        self._history_detail_range: tuple[float, float] | None = None
        self._history_detail_is_downsampled = False
        self._history_range_sequence = 0
        self._history_range_pending = False
        self._history_range_task: _HistoryRangeLoadTask | None = None
        self._active_history_range_tasks: dict[int, _HistoryRangeLoadTask] = {}
        self._history_load_pending = False
        self._history_load_task: _HistoryLoadTask | None = None
        self._log_entries: deque[tuple[str, str]] = deque(maxlen=6000)
        self._experiment_csv_path: Path | None = None
        self._curve_export_task: CurveExportTask | None = None
        self._snapshot_pending = False
        self._snapshot_task: _CsvSnapshotTask | None = None
        self._safe_events_export_task: _SafeEventsExportTask | None = None
        self._safe_measurements_export_task: _SafeMeasurementsExportTask | None = None
        self._share_export_task: _SharePackageExportTask | None = None
        self._file_export_results: list[tuple[str, str]] = []
        self._region_stats_request_id = 0
        self._region_stats_tasks: dict[int, _RegionStatisticsTask] = {}
        self._region_stats_scanning = False
        self._region_stats_pending_rows: list[dict] = []
        self._region_stats_pending_overflow = False
        self._region_stats_mode: Mode | None = None
        self._region_stats_values: RunningStats | None = None
        self._region_stats_last_sample_index = 0
        self._region_stats_bounds: tuple[float, float] | None = None
        self._event_lines: list = []
        self._event_labels: list = []
        self._pending_event_markers: list[tuple[float, str]] = []
        self._pending_event_marker_index = 0
        self._event_markers_loading = False
        self._event_marker_timer = QTimer(self)
        self._event_marker_timer.setSingleShot(True)
        self._event_marker_timer.setInterval(EVENT_MARKER_BATCH_INTERVAL_MS)
        self._event_marker_timer.timeout.connect(self._append_event_marker_batch)
        self._last_elapsed = 0.0
        self._global_stats = {"count": 0, "minimum": None, "maximum": None, "mean": None, "sample_stddev": None}
        self._crosshair_x = None
        self._crosshair_y = None
        self._selected_plot_point: tuple[float, float, int] | None = None
        self._selected_plot_metadata: dict | None = None
        self._shutdown_started = False
        self._shutdown_finished = False
        self._shutdown_file_errors: list[str] = []
        self._shutdown_file_errors_reported = False

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
        self.port_combo.currentTextChanged.connect(self._on_serial_port_changed)
        self._serial_profile_port = self._selected_serial_profile_port()
        self._restore_port_serial_settings(self._serial_profile_port)
        self._load_mode_controls(self._current_mode)
        self._update_controls()
        self._worker_thread.start()
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_elapsed_label)
        self._clock_timer.start(250)
        self._history_range_timer = QTimer(self)
        self._history_range_timer.setSingleShot(True)
        self._history_range_timer.setInterval(180)
        self._history_range_timer.timeout.connect(self._load_history_detail_for_view)
        self.statusBar().showMessage("未连接。请确认仪器处于RS-232 / COMPUTER模式。")
        if self._config_fallback:
            self.statusBar().showMessage("用户配置目录不可写；参数暂存于临时目录。检查权限后重启可恢复持久化。", 15000)

    def _wire_worker(self) -> None:
        self._bridge.connect_real.connect(self._worker.connect_real_with_profiles)
        self._bridge.connect_mock.connect(self._worker.connect_mock)
        self._bridge.set_control_mode.connect(self._worker.set_control_mode)
        self._bridge.set_firmware_versions.connect(self._worker.set_firmware_versions)
        self._bridge.configure.connect(self._worker.configure_mode)
        self._bridge.configure_unconfirmed_mode.connect(self._worker.configure_mode_without_wiring_confirmation)
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
        self._worker.connection_progress.connect(self._on_connection_progress)
        self._worker.connection_stage.connect(self._on_connection_stage)
        self._worker.control_mode_changed.connect(self._on_control_mode_changed)
        self._worker.control_mode_result.connect(self._on_control_mode_result)
        self._worker.acquisition_state_changed.connect(self._on_acquisition_state_changed)
        self._worker.status_changed.connect(self._on_status)
        self._worker.log_line.connect(self._append_log)
        self._worker.scpi_logs_ready.connect(self._on_scpi_logs_ready)
        self._worker.configuration_ready.connect(self._on_configuration)
        self._worker.active_settings_ready.connect(self._on_active_settings)
        self._worker.zero_result.connect(self._on_zero_result)
        self._worker.sample_ready.connect(self._on_sample_ready)
        self._worker.experiment_started.connect(self._on_experiment_started)
        self._worker.experiment_finished.connect(self._on_experiment_finished)
        self._worker.experiment_save_result.connect(self._on_experiment_save_result)
        self._worker.event_recorded.connect(self._on_event_saved)
        self._worker.diagnostic_ready.connect(self._show_diagnostic)
        self._worker.error.connect(self._show_error)
        self._worker.measurement_speed_result.connect(self._on_measurement_speed_result)

    def _set_operation_status(self, text: str, *, state: str = "busy", detail: str = "") -> None:
        self.operation_status_label.setText(f"操作：{text}")
        self.operation_status_label.setToolTip(detail or text)
        self.operation_status_label.setProperty("state", state)
        self.operation_status_label.style().unpolish(self.operation_status_label)
        self.operation_status_label.style().polish(self.operation_status_label)

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("appRoot")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 12)
        root_layout.setSpacing(10)
        self.setCentralWidget(root)

        header = QFrame()
        header.setObjectName("appHeader")
        self.header = header
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
        self.connection_action_button = QPushButton("连接仪器")
        self.connection_action_button.setObjectName("connectionActionButton")
        self.connection_action_button.setMinimumHeight(38)
        self.connection_action_button.clicked.connect(self._show_connection_settings)
        header_layout.addWidget(self.connection_action_button)
        self.measurement_settings_button = QPushButton("测量参数")
        self.measurement_settings_button.setObjectName("measurementSettingsButton")
        self.measurement_settings_button.setMinimumHeight(38)
        self.measurement_settings_button.clicked.connect(self._show_measurement_settings)
        header_layout.addWidget(self.measurement_settings_button)
        self.mock_scenario_header = QLabel()
        self.mock_scenario_header.setObjectName("mockScenarioBanner")
        self.mock_scenario_header.setWordWrap(True)
        self.mock_scenario_header.hide()
        header_layout.addWidget(self.mock_scenario_header)
        self.instrument_readback_status = QLabel("未连接")
        self.instrument_readback_status.setObjectName("instrumentReadbackBadge")
        self.instrument_readback_status.setMinimumHeight(32)
        self.instrument_readback_status.hide()
        header_layout.addWidget(self.instrument_readback_status)
        root_layout.addWidget(header)

        setup_panel = self._build_setup_panel()
        chart_panel = self._build_chart_panel()
        self.mode_bar = QFrame()
        self.mode_bar.setObjectName("modeBar")
        mode_layout = QHBoxLayout(self.mode_bar)
        mode_layout.setContentsMargins(8, 6, 8, 10)
        mode_layout.setSpacing(6)
        self.mode_buttons: dict[Mode, QPushButton] = {}
        self.mode_button_group = QButtonGroup(self)
        self.mode_button_group.setExclusive(True)
        for mode, text in (
            (Mode.DCV, "直流电压"), (Mode.ACV, "交流电压"),
            (Mode.DCI, "直流电流"), (Mode.ACI, "交流电流"),
            (Mode.RES2, "两线电阻"), (Mode.RES4, "四线电阻"),
        ):
            button = QPushButton(text)
            button.setObjectName("modeButton")
            button.setCheckable(True)
            button.setMinimumHeight(42)
            button.setAccessibleName(f"{text} {mode.value}")
            button.setToolTip(descriptor(mode).safety)
            button.clicked.connect(lambda _checked=False, selected=mode: self._select_mode_button(selected))
            self.mode_button_group.addButton(button)
            self.mode_buttons[mode] = button
            mode_layout.addWidget(button, 1)
        self.mode_indicator = QFrame(self.mode_bar)
        self.mode_indicator.setObjectName("modeIndicator")
        self.mode_indicator.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.mode_indicator.raise_()
        self.mode_animation = QPropertyAnimation(self.mode_indicator, b"geometry", self)
        self.mode_animation.setDuration(180)
        self.mode_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.mode_bar.installEventFilter(self)
        self.mode_combo.currentIndexChanged.connect(self._sync_mode_buttons)
        self._sync_mode_buttons(animate=False)
        root_layout.addWidget(self.mode_bar)
        range_label_widget = QLabel("量程")
        range_label_widget.setStyleSheet("color: #e5f0ed; font-weight: 600;")
        self.main_range_combo = QComboBox()
        self.main_range_combo.setMinimumWidth(130)
        self.main_range_combo.setStyleSheet(
            "QComboBox { color: #1d2c2a; background: #fffefa; } "
            "QComboBox:disabled { color: #26352e; background: #e7ebe8; } "
            "QComboBox QAbstractItemView { color: #1d2c2a; background: #fffefa; }"
        )
        self.main_range_combo.currentIndexChanged.connect(self._main_range_changed)
        self.main_range_combo.activated.connect(self._main_range_activated)
        average_position = self._action_layout.indexOf(self.average_window_spin) - 1
        self._action_layout.insertWidget(average_position, range_label_widget)
        self._action_layout.insertWidget(average_position + 1, self.main_range_combo)
        self._pending_wiring_target = None
        self._pending_wiring_retry = None
        self._approved_wiring_target = None
        self._confirmed_range_target = None
        root_layout.addWidget(chart_panel, 1)

        self.configuration_dialog = QDialog(self)
        self.configuration_dialog.setObjectName("configurationDialog")
        self.configuration_dialog.setWindowTitle("仪器与实验设置")
        self.configuration_dialog.resize(1020, 780)
        self._fit_configuration_dialog(initial=True)
        dialog_layout = QVBoxLayout(self.configuration_dialog)
        dialog_layout.setContentsMargins(16, 12, 16, 14)
        dialog_layout.setSpacing(10)
        dialog_heading = QHBoxLayout()
        dialog_title = QLabel("连接仪器")
        self.configuration_title = dialog_title
        dialog_title.setObjectName("dialogTitle")
        dialog_heading.addWidget(dialog_title)
        self.configuration_nav_group = QButtonGroup(self)
        self.configuration_nav_group.setExclusive(True)
        self.configuration_nav_buttons: dict[str, QToolButton] = {}
        for key, label in (
            ("communications", "通信"),
            ("measurement", "测量"),
            ("experiment", "实验"),
            ("analysis", "分析总览"),
            ("chart", "曲线设置"),
        ):
            button = QToolButton(self.configuration_dialog)
            button.setObjectName(f"configurationSection_{key}")
            button.setText(label)
            button.setProperty("class", "secondaryButton")
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setAccessibleName(f"{label}设置")
            self.configuration_nav_group.addButton(button)
            button.clicked.connect(
                lambda _checked=False, section=key: self._select_configuration_section(section)
            )
            self.configuration_nav_buttons[key] = button
            dialog_heading.addWidget(button)
            button.hide()
        dialog_heading.addStretch(1)
        close_settings = QPushButton("完成")
        close_settings.setObjectName("secondaryButton")
        close_settings.clicked.connect(self.configuration_dialog.hide)
        dialog_heading.addWidget(close_settings)
        dialog_layout.addLayout(dialog_heading)
        setup_scroll = QScrollArea()
        setup_scroll.setObjectName("configurationScrollArea")
        setup_scroll.setWidgetResizable(True)
        setup_scroll.setFrameShape(QFrame.Shape.NoFrame)
        setup_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        setup_scroll.setWidget(setup_panel)
        self.setup_scroll_area = setup_scroll
        self.workspace_stack = QStackedLayout()
        self.workspace_stack.addWidget(setup_scroll)
        self.workspace_stack.addWidget(self.analysis_tabs)
        dialog_layout.addLayout(self.workspace_stack, 1)
        self._sync_configuration_navigation()
        self.configuration_dialog.installEventFilter(self)

        self.files_dialog = QDialog(self)
        self.files_dialog.setObjectName("experimentFilesDialog")
        self.files_dialog.setWindowTitle("实验文件")
        self.files_dialog.resize(760, 540)
        self.files_dialog.installEventFilter(self)
        files_layout = QVBoxLayout(self.files_dialog)
        files_heading = QLabel("实验文件与导出结果")
        files_heading.setObjectName("dialogTitle")
        files_layout.addWidget(files_heading)
        files_layout.addWidget(QLabel("当前实验目录"))
        self.files_directory_label = QLabel("尚无实验目录")
        self.files_directory_label.setObjectName("fileWorkspacePath")
        self.files_directory_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.files_directory_label.setWordWrap(True)
        files_layout.addWidget(self.files_directory_label)
        files_layout.addWidget(QLabel("实验目录文件"))
        self.experiment_file_list = QListWidget()
        self.experiment_file_list.setObjectName("experimentFileList")
        self.experiment_file_list.setAccessibleName("当前实验文件和导出结果")
        files_layout.addWidget(self.experiment_file_list, 1)
        files_layout.addWidget(QLabel("最近导出结果（本次运行）"))
        self.files_export_results_list = QListWidget()
        self.files_export_results_list.setObjectName("fileExportResultsList")
        self.files_export_results_list.setAccessibleName("本次运行导出的文件及保存路径")
        self.files_export_results_list.setMaximumHeight(88)
        files_layout.addWidget(self.files_export_results_list)
        files_actions = QGridLayout()
        self.refresh_files_button = QPushButton("刷新列表")
        self.refresh_files_button.clicked.connect(self._refresh_file_workspace)
        self.files_open_directory_button = QPushButton("打开目录")
        self.files_open_directory_button.clicked.connect(self._open_experiment_directory)
        self.files_copy_path_button = QPushButton("复制路径")
        self.files_copy_path_button.clicked.connect(self._copy_experiment_path)
        self.files_load_history_button = QPushButton("加载历史 CSV")
        self.files_load_history_button.clicked.connect(self._load_history)
        self.files_snapshot_button = QPushButton("导出进行中快照")
        self.files_snapshot_button.clicked.connect(self._export_running_snapshot)
        self.files_safe_events_button = QPushButton("导出事件安全副本")
        self.files_safe_events_button.clicked.connect(self._export_safe_events_csv)
        self.files_safe_measurements_button = QPushButton("导出测量安全副本")
        self.files_safe_measurements_button.setToolTip(
            "仅处理原始响应和错误文本；原始 measurements.csv 保持不变。请停止采集后导出。"
        )
        self.files_safe_measurements_button.clicked.connect(self._export_safe_measurements_csv)
        self.files_share_package_button = QPushButton("\u5bfc\u51fa\u8131\u654f\u5206\u4eab\u5305")
        self.files_share_package_button.setToolTip(
            "生成脱敏分享包（独立 ZIP 副本）：保留 8845A 型号、实验配置和有效数值；数值列中的非数值文本会脱敏。隐藏序列号、COM 口及本机路径；"
            "只包含 config.json、measurements.csv 和可选 events.csv，不含 run.log。CSV 中公式样式文本会加文本前缀，原始实验文件不修改。"
        )
        self.files_share_package_button.clicked.connect(self._export_share_package)
        self.files_close_button = QPushButton("关闭")
        self.files_close_button.clicked.connect(self.files_dialog.hide)
        for index, button in enumerate((
            self.refresh_files_button, self.files_open_directory_button,
            self.files_copy_path_button, self.files_load_history_button,
            self.files_snapshot_button, self.files_safe_events_button,
            self.files_safe_measurements_button,
            self.files_share_package_button,
            self.files_close_button,
        )):
            button.setProperty("class", "secondaryButton")
            files_actions.addWidget(button, index // 4, index % 4)
        files_layout.addLayout(files_actions)
        self._refresh_file_workspace()

        self.setStatusBar(QStatusBar(self))
        self.operation_status_label = QLabel("操作：就绪")
        self.operation_status_label.setObjectName("operationStatus")
        self.operation_status_label.setToolTip("当前没有正在执行的长耗时命令。")
        self.statusBar().addPermanentWidget(self.operation_status_label, 3)
        self.shutdown_status_label = QLabel()
        self.shutdown_status_label.setObjectName("shutdownStatus")
        self.shutdown_status_label.hide()
        self.statusBar().addPermanentWidget(self.shutdown_status_label, 1)
        self.critical_error_label = QLabel()
        self.critical_error_label.setObjectName("criticalErrorStatus")
        self.critical_error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.critical_error_label.hide()
        self.statusBar().addPermanentWidget(self.critical_error_label, 1)

        self.navigation_toolbar = QToolBar("工作区导航", self)
        self.navigation_toolbar.setObjectName("workspaceNavigationToolbar")
        self.navigation_toolbar.setMovable(False)
        self.navigation_toolbar.setFloatable(False)
        self.navigation_toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.navigation_toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.navigation_toolbar)
        self.navigation_buttons: dict[str, QToolButton] = {}
        self._navigation_button_group = QButtonGroup(self)
        self._navigation_button_group.setExclusive(True)
        navigation_items = (
            ("communications", "通信", "communicationsNavigation", "串口连接、仪器识别与 SCPI 日志"),
            ("acquisition", "采集", "acquisitionNavigation", "返回实时读数和采集曲线"),
            ("analysis", "分析", "analysisNavigation", "数据分析、基准值和曲线设置"),
            ("files", "实验文件", "experimentFilesNavigation", "查看当前实验文件、加载历史 CSV 或导出"),
            ("diagnostics", "诊断日志", "diagnosticsNavigation", "打开通信和 SCPI 日志；仪器诊断可从“仪器”菜单运行"),
        )
        for section, text, object_name, tooltip in navigation_items:
            button = QToolButton(self.navigation_toolbar)
            button.setObjectName(object_name)
            button.setText(text)
            button.setToolTip(tooltip)
            button.setAccessibleName(f"{text}工作区")
            button.setCheckable(True)
            button.setAutoRaise(True)
            self._navigation_button_group.addButton(button)
            self.navigation_toolbar.addWidget(button)
            button.clicked.connect(
                lambda _checked=False, target=section: self._navigate_workspace(target)
            )
            self.navigation_buttons[section] = button
        self.navigation_toolbar.addSeparator()
        self.new_measurement_button = QPushButton("新建测量")
        self.new_measurement_button.setObjectName("newMeasurementButton")
        self.new_measurement_button.setProperty("class", "primaryButton")
        self.new_measurement_button.setAccessibleName("新建测量；开始后自动显示新曲线")
        self.new_measurement_button.setMinimumWidth(146)
        self.new_measurement_button.setFixedHeight(36)
        self.new_measurement_button.setToolTip(
            "打开新实验设置并填写名称，然后选择单次测量或连续采集。"
            "开始后曲线自动换新，旧实验 CSV 保留，无需先清空。快捷键：Ctrl+N。"
        )
        self.new_measurement_button.clicked.connect(self._open_new_measurement_setup)
        self._measurement_action_layout.insertWidget(0, self.new_measurement_button)
        self.clear_plot_button = QPushButton("清空图表")
        self.clear_plot_button.setObjectName("clearPlotButton")
        self.clear_plot_button.setAccessibleName("清空当前图表显示，不删除已保存数据")
        self.clear_plot_button.setMinimumWidth(112)
        self.clear_plot_button.setFixedHeight(36)
        self.clear_plot_button.setToolTip(
            "确认后清除当前图表、统计和事件标记；已保存的 measurements.csv 和 events.csv 不会删除。"
        )
        self.clear_plot_button.clicked.connect(self._clear_plot_from_ui)
        self._measurement_action_layout.insertWidget(1, self.clear_plot_button)
        self.configuration_button = QPushButton("设置与分析…")
        self.configuration_button.setObjectName("configurationNavigation")
        self.configuration_button.setFixedHeight(30)
        self.configuration_button.setToolTip("打开仪器、采集、曲线、分析及日志设置。快捷键：Ctrl+,")
        self.configuration_button.setAccessibleName("打开设置与分析")
        self.configuration_button.clicked.connect(self._show_configuration)
        self.configuration_navigation_action = self.navigation_toolbar.addWidget(
            self.configuration_button
        )
        self.navigation_buttons["acquisition"].setChecked(True)
        self.statusBar().setSizeGripEnabled(False)

        self._build_log_dock()
        self._build_menus()
        self.log_dock.toggleViewAction().setText("显示通信日志面板")
        self.navigation_toolbar.hide()
        self.menuBar().hide()
        self.measurement_action_bar.hide()
        self._shortcut_actions["newMeasurementShortcutAction"].setEnabled(False)
        self._shortcut_actions["eventShortcutAction"].setEnabled(False)

    def _show_connection_settings(self) -> None:
        if (self._acquiring or self._start_pending or self._experiment_active
                or self._finalization_pending or self._configure_pending
                or self._connection_pending or self._shutdown_started):
            return
        self.configuration_title.setText("连接仪器")
        self.configuration_dialog.setWindowTitle("连接仪器")
        self._select_configuration_section("communications")
        self._show_configuration()

    def _show_measurement_settings(self) -> None:
        if (self._acquiring or self._start_pending or self._experiment_active
                or self._finalization_pending or self._configure_pending):
            return
        self.configuration_title.setText("测量参数")
        self.configuration_dialog.setWindowTitle("测量参数")
        self._select_configuration_section("measurement")
        self._show_configuration()

    def _select_mode_button(self, mode: Mode) -> None:
        if self._pending_wiring_retry is not None:
            self._cancel_wiring_confirmation()
        if not self.mode_combo.isEnabled() or self._experiment_active or self._finalization_pending:
            self._sync_mode_buttons()
            return
        self._set_combo_data(self.mode_combo, mode.value)
        self._sync_mode_buttons()

    def _sync_mode_buttons(self, *_args, animate: bool = True) -> None:
        if not hasattr(self, "mode_buttons"):
            return
        button = self.mode_buttons[self._current_mode]
        button.setChecked(True)
        target = QRect(button.x() + 8, self.mode_bar.height() - 6, max(0, button.width() - 16), 3)
        if animate and self.mode_bar.isVisible() and self.mode_indicator.width() > 0:
            self.mode_animation.stop()
            self.mode_animation.setStartValue(self.mode_indicator.geometry())
            self.mode_animation.setEndValue(target)
            self.mode_animation.start()
        else:
            self.mode_indicator.setGeometry(target)

    def _record_file_export_result(self, label: str, destination: str | Path) -> None:
        path_text = str(destination)
        self._file_export_results.insert(0, (label, path_text))
        del self._file_export_results[10:]
        if not hasattr(self, "files_export_results_list"):
            return
        self.files_export_results_list.clear()
        for result_label, result_path in self._file_export_results:
            item = QListWidgetItem(f"{result_label} · {Path(result_path).name}")
            item.setToolTip(result_path)
            self.files_export_results_list.addItem(item)

    def _show_configuration(self) -> None:
        self.files_dialog.hide()
        if hasattr(self, "navigation_buttons"):
            target = "analysis" if self.workspace_stack.currentIndex() == 1 else "communications"
            self.navigation_buttons[target].setChecked(True)
        self._fit_configuration_dialog()
        self.configuration_dialog.show()
        self.configuration_dialog.raise_()
        self.configuration_dialog.activateWindow()
        QTimer.singleShot(0, self._update_plot_toolbar_layout)

    @staticmethod
    def _suggest_experiment_name(mode: Mode) -> str:
        """Build a short, sortable default name; storage still guarantees unique run directories."""
        return f"实验_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode.value}"

    def _open_new_measurement_setup(self) -> None:
        """Open the experiment settings used to name and start a fresh run."""
        if self._history_load_pending:
            self.statusBar().showMessage("历史 CSV 正在后台加载，完成后可新建测量。", 8000)
            return
        if self._new_measurement_after_save:
            self.statusBar().showMessage("当前采集正在停止并保存，完成后会自动打开新测量设置。", 8000)
            return
        if self._acquiring:
            answer = QMessageBox.question(
                self,
                "停止并新建测量？",
                "将先停止并保存当前实验，再打开新测量设置。当前曲线和已保存 CSV 会保留到新测量开始；"
                "选择“否”会继续当前采集。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._new_measurement_after_save = True
            self._new_measurement_stop_finished = False
            self._new_measurement_save_succeeded = False
            self._update_controls()
            self._stop()
            return
        if self._experiment_active or self._start_pending:
            self.statusBar().showMessage(
                "当前实验正在收尾并保存，完成后可新建测量。当前曲线和 CSV 已保留。", 8000
            )
            return
        self.experiment_name.setText(self._suggest_experiment_name(self._current_mode))
        self._set_configuration_workspace(0)
        self._show_setup_section(2)
        self._show_configuration()
        QTimer.singleShot(0, self.experiment_name.setFocus)
        QTimer.singleShot(0, self.experiment_name.selectAll)

    def _start_from_configuration(self, *, one_shot: bool) -> None:
        """Start a run from the experiment settings and return to the live plot."""
        pending_before_start = self._start_pending
        self.configuration_dialog.hide()
        self.raise_()
        self.activateWindow()
        try:
            self._start(one_shot=one_shot)
        except Exception as exc:
            self._start_pending = pending_before_start
            self._update_controls()
            self.preflight_status.setText(f"启动失败：{exc}")
            self._show_error(str(exc))
        finally:
            if not self._start_pending:
                self.configuration_dialog.show()
                self.configuration_dialog.raise_()
                self.configuration_dialog.activateWindow()

    def _reset_measurement(self) -> None:
        if self._history_load_pending or self._start_pending or self._finalization_pending:
            self.statusBar().showMessage("请等待当前操作完成。", 5000)
            return
        if self._acquiring:
            self._reset_plot_after_save = True
            self._stop()
            self._update_controls()
            self.statusBar().showMessage("正在停止并保存…")
            return
        if self._experiment_active:
            self.statusBar().showMessage("正在保存，请稍候。", 5000)
            return
        self._reset_measurement_plot()

    def _reset_measurement_plot(self) -> None:
        self._clear_plot()
        self.plot_empty_title.setText("准备测量")
        self.plot_empty_hint.hide()
        self.plot_empty_new_measurement_button.hide()
        self._update_plot_data_limits()
        self.statusBar().showMessage("曲线已清空，原始数据已保留。", 5000)
        self._update_controls()

    def _clear_plot_from_ui(self) -> None:
        """Clear only the current in-memory chart; saved experiment files are untouched."""
        if self._history_load_pending:
            self.statusBar().showMessage("历史 CSV 正在后台加载，完成后再清空图表。", 8000)
            return
        if self._acquiring or self._experiment_active or self._start_pending:
            self.statusBar().showMessage(
                "请先停止并保存当前实验，再清空图表。当前 CSV 不会被删除。", 8000
            )
            return
        if not self._plot_x and not self._history_mode:
            self.statusBar().showMessage("当前没有可清空的曲线。", 5000)
            return
        self._clear_plot()
        self.plot_empty_title.setText("曲线已清空")
        self.plot_empty_hint.setText(
            "重新测量：点“新建测量”填写名称并选择单次或连续采集。"
            "新测量开始后自动换图，旧实验 CSV 保留；无需先清空。"
        )
        self.plot_empty_new_measurement_button.show()
        self._return_to_live_view()
        self._update_controls()
        self.statusBar().showMessage(
            "图表显示已清空；已保存的 measurements.csv 和 events.csv 未删除，可用“加载 CSV”恢复。",
            10000,
        )

    def _navigate_workspace(self, section: str) -> None:
        """Open a task workspace and keep the top-level navigation in sync."""
        navigation_section = "analysis" if section == "chart" else section
        if navigation_section not in self.navigation_buttons:
            navigation_section = "communications"
        self.navigation_buttons[navigation_section].setChecked(True)
        if section == "acquisition":
            self.configuration_dialog.hide()
            self.files_dialog.hide()
            self.raise_()
            self.activateWindow()
            return
        if section == "files":
            self.configuration_dialog.hide()
            self._refresh_file_workspace()
            _fit_dialog_to_available_geometry(self.files_dialog)
            self.files_dialog.show()
            self.files_dialog.raise_()
            self.files_dialog.activateWindow()
            return
        if section == "diagnostics":
            self.configuration_dialog.hide()
            self.files_dialog.hide()
            self.raise_()
            self.activateWindow()
            self.log_dock.show()
            self.log_dock.raise_()
            return
        if section == "analysis":
            self._select_configuration_section("analysis")
        elif section == "chart":
            self._select_configuration_section("chart")
        else:
            self._select_configuration_section("communications")
        self.files_dialog.hide()
        self._show_configuration()

    def _set_configuration_workspace(self, index: int) -> None:
        index = max(0, min(int(index), self.workspace_stack.count() - 1))
        self.workspace_stack.setCurrentIndex(index)
        if hasattr(self, "navigation_buttons"):
            target = "analysis" if index == 1 else "communications"
            self.navigation_buttons[target].setChecked(True)
        self._sync_configuration_navigation()
        QTimer.singleShot(0, self._update_plot_toolbar_layout)

    def _select_configuration_section(self, section: str) -> None:
        setup_sections = {"communications": 0, "measurement": 1, "experiment": 2}
        if section in setup_sections:
            self._set_configuration_workspace(0)
            self._show_setup_section(setup_sections[section])
        elif section == "analysis":
            self._set_configuration_workspace(1)
            self.analysis_tabs.setCurrentWidget(self.analysis_overview_tab)
        elif section == "chart":
            self._set_configuration_workspace(1)
            self.analysis_tabs.setCurrentWidget(self.chart_settings_tab)
        else:
            return
        self._sync_configuration_navigation()

    def _sync_configuration_navigation(self) -> None:
        if not hasattr(self, "configuration_nav_buttons"):
            return
        if self.workspace_stack.currentIndex() == 0:
            current_section = ("communications", "measurement", "experiment")[
                self.setup_section_stack.currentIndex()
            ]
        else:
            current_section = (
                "chart" if self.analysis_tabs.currentWidget() is self.chart_settings_tab
                else "analysis"
            )
        for section, button in self.configuration_nav_buttons.items():
            button.setChecked(section == current_section)

    def _show_setup_section(self, index: int) -> None:
        if not hasattr(self, "setup_section_stack"):
            return
        index = max(0, min(int(index), self.setup_section_stack.count() - 1))
        self.setup_section_stack.setCurrentIndex(index)
        self._sync_configuration_navigation()

    def _fit_configuration_dialog(self, available=None, *, initial: bool = False) -> None:
        if available is None:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is None:
                return
            available = screen.availableGeometry()
        # Reserve space for native title-bar and frame pixels so the outer
        # frame, not only the client area, remains inside the work area.
        max_width = max(1, available.width() - 32)
        max_height = max(1, available.height() - 80)
        min_width = min(760, max_width)
        min_height = min(520, max_height)
        dialog = self.configuration_dialog
        desired_width = 1020 if initial else dialog.width()
        desired_height = 780 if initial else dialog.height()
        width = min(max(desired_width, min_width), max_width)
        height = min(max(desired_height, min_height), max_height)
        dialog.setMinimumSize(min_width, min_height)
        dialog.setMaximumSize(max_width, max_height)
        dialog.resize(width, height)
        dialog.move(
            available.x() + max(0, (available.width() - width) // 2),
            available.y() + max(0, (available.height() - height) // 2),
        )

    def _build_setup_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("setupPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)

        self.setup_section_stack = QStackedLayout()
        self.setup_section_stack.setObjectName("settingsSectionStack")

        self.communication_workspace = QWidget()
        self.communication_workspace.setObjectName("communicationWorkspace")
        communication_layout = QVBoxLayout(self.communication_workspace)
        communication_layout.setContentsMargins(5, 5, 5, 5)
        communication_layout.setSpacing(8)
        measurement_page = QWidget()
        measurement_layout = QVBoxLayout(measurement_page)
        measurement_layout.setContentsMargins(5, 5, 5, 5)
        measurement_layout.setSpacing(7)
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
        self.connect_button.setProperty("connectionTarget", "instrument")
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
        self.idn_label.setToolTip(
            "*IDN?第四字段可由用户自定义；即使其中有可解析的点分数字，也不能证明它是 OutG SW。"
            "SYST:VERS?返回SCPI命令版本，不是仪器固件。DCI 400 mA 门槛依据人工核对的面板 OutG SW；"
            "ACI 官方资料未明确 1.0.700.18 门槛对应 OutG SW 还是 InG SW；ACI 不根据 OutG SW 或 InG SW 自动拒绝 400 mA 档，使用前请在仪表面板核对软件版本。"
        )
        form.addRow("识别结果", self.idn_label)
        self.connection_stages_label = QLabel("设备 待开始 · 串口 待开始 · 8845A 待开始 · 配置 待开始 · 就绪 待开始")
        self.connection_stages_label.setObjectName("secondaryText")
        self.connection_stages_label.setWordWrap(True)
        self.connection_stages_label.setToolTip("逐项显示设备选择、串口打开、8845A身份识别、配置回读和就绪状态。")
        form.addRow("连接进度", self.connection_stages_label)
        self.diagnose_button = QPushButton("运行 SCPI 诊断")
        self.diagnose_button.setObjectName("diagnoseButton")
        self.diagnose_button.setToolTip("在 Computer 控制模式下读取仪器诊断信息。")
        self.diagnose_button.clicked.connect(self._diagnose)
        self.diagnose_button.setEnabled(False)
        form.addRow("通信诊断", self.diagnose_button)
        self.outguard_sw_edit = QLineEdit()
        self.outguard_sw_edit.setMaxLength(64)
        self.outguard_sw_edit.setPlaceholderText("面板核对，例如 2.10.882.24")
        self.outguard_sw_edit.setToolTip(
            "从仪器前面板 INSTR SETUP → SYSTEM → VERSIONS + SN 读取，并按仪器序列号保存在本机。"
            "*IDN?修订字段可能是用户自定义内容，不用它判断固件。DCI 400 mA 门槛依据此处人工核对的 OutG SW；"
            "ACI 官方资料未明确 1.0.700.18 门槛对应 OutG SW 还是 InG SW；ACI 不根据 OutG SW 或 InG SW 自动拒绝 400 mA 档。使用前请在仪表面板核对软件版本。"
            "未知或无法解析不会按过旧拒绝量程。"
            "这是人工核对记录，不是通过SCPI读取。"
        )
        self._outguard_sw_tooltip = self.outguard_sw_edit.toolTip()
        self.inguard_sw_edit = QLineEdit()
        self.inguard_sw_edit.setMaxLength(64)
        self.inguard_sw_edit.setPlaceholderText("可选记录，例如 ad300210")
        self.inguard_sw_edit.setToolTip(
            "可选记录同一前面板版本页显示的 InG SW，并随实验配置保存。"
            "DCI 固件门槛不依据 InG SW；ACI 官方资料未明确版本门槛对应哪个字段，"
            "ACI 不根据 OutG SW 或 InG SW 自动拒绝 400 mA 档；使用前请在仪表面板核对软件版本。"
        )
        self._inguard_sw_tooltip = self.inguard_sw_edit.toolTip()
        self.outguard_sw_edit.editingFinished.connect(self._firmware_version_changed)
        self.inguard_sw_edit.editingFinished.connect(self._firmware_version_changed)
        form.addRow("OutG SW（面板核对）", self.outguard_sw_edit)
        form.addRow("InG SW（可选记录）", self.inguard_sw_edit)
        for field in (self.outguard_sw_edit, self.inguard_sw_edit):
            form.labelForField(field).hide()
            field.hide()
        firmware_profile_row = QHBoxLayout()
        self.firmware_profile_status = QLabel("尚无面板版本记录")
        self.firmware_profile_status.setObjectName("secondaryText")
        self.firmware_profile_status.setWordWrap(True)
        self.clear_firmware_profile_button = QPushButton("清除版本记录")
        self.clear_firmware_profile_button.setObjectName("secondaryButton")
        self.clear_firmware_profile_button.setToolTip("仅清除本机为当前仪器序列号保存的人工版本记录。")
        self._clear_firmware_profile_tooltip = self.clear_firmware_profile_button.toolTip()
        self.clear_firmware_profile_button.clicked.connect(self._clear_firmware_profile)
        firmware_profile_row.addWidget(self.firmware_profile_status, 1)
        firmware_profile_row.addWidget(self.clear_firmware_profile_button)
        form.addRow("版本记录", firmware_profile_row)
        form.labelForField(firmware_profile_row).hide()
        self.firmware_profile_status.hide()
        self.clear_firmware_profile_button.hide()
        self.control_mode_combo = QComboBox()
        self.control_mode_combo.addItem("未连接", "disconnected")
        self.control_mode_combo.addItem("Computer · 软件控制", "computer")
        self.control_mode_combo.addItem("Local · 面板操作", "local")
        control_mode_help = (
            "Computer：软件可发送SCPI命令、设置仪表并采集；仪表的RS-232接口协议仍须单独设为COMPUTER。\n"
            "Local：软件发送 SYST:LOC 请求后停用测量、参数设置、诊断和ZERO，串口连接保留；"
            "仪表不提供 Local 状态的 SCPI 回读，请核对面板。只能在停止采集后切换；"
            "切回Computer时会重新读取仪表配置。"
        )
        self.control_mode_combo.setToolTip(control_mode_help)
        self.control_mode_label = QLabel("控制方式")
        self.control_mode_label.setToolTip(control_mode_help)
        self.set_control_mode_button = QPushButton("切换")
        self.set_control_mode_button.setObjectName("secondaryButton")
        self.set_control_mode_button.setToolTip(control_mode_help)
        self.set_control_mode_button.clicked.connect(self._request_control_mode_change)
        control_mode_row = QHBoxLayout()
        control_mode_row.setSpacing(6)
        control_mode_row.addWidget(self.control_mode_combo, 1)
        control_mode_row.addWidget(self.set_control_mode_button)
        control_mode_column = QVBoxLayout()
        control_mode_column.setContentsMargins(0, 0, 0, 0)
        control_mode_column.setSpacing(4)
        control_mode_column.addLayout(control_mode_row)
        self.control_mode_explanation_label = QLabel(
            "Computer：软件可设置仪表并采集（RS-232 协议也须设为 COMPUTER）。"
            "Local：使用面板操作；软件停用测量、参数设置、诊断和 ZERO，串口连接保留。"
        )
        self.control_mode_explanation_label.setObjectName("secondaryText")
        self.control_mode_explanation_label.setWordWrap(True)
        control_mode_column.addWidget(self.control_mode_explanation_label)
        form.addRow(self.control_mode_label, control_mode_column)

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
        self.restore_serial_defaults_button = QPushButton("恢复推荐串口设置")
        self.restore_serial_defaults_button.setToolTip(
            "将当前所选 COM 端口恢复为应用推荐值：9600 baud、8N1、无流控、CRLF、2 秒。"
            "只更改并保存当前端口配置，不会连接或操作仪器。"
        )
        self.restore_serial_defaults_button.clicked.connect(self._restore_recommended_serial_settings)
        advanced_form.addRow("", self.restore_serial_defaults_button)
        advanced.setVisible(False)
        form.addRow("", advanced)

        def toggle_serial_advanced(checked: bool) -> None:
            advanced.setVisible(checked)
            self.serial_advanced_button.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )

        self.serial_advanced_button.toggled.connect(toggle_serial_advanced)
        communication_layout.addWidget(connection)

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
        self.autorange_check.toggled.connect(self._update_controls)
        self.autorange_check.toggled.connect(self._settings_changed)
        self.range_combo = QComboBox()
        self.range_combo.currentIndexChanged.connect(self._settings_changed)
        range_row = QHBoxLayout()
        range_row.setSpacing(6)
        range_row.addWidget(self.autorange_check)
        range_row.addWidget(self.range_combo, 1)
        mode_form.addRow("量程", range_row)
        self.range_limit_hint = QLabel()
        self.range_limit_hint.setObjectName("fieldHint")
        self.range_limit_hint.setWordWrap(True)
        self.range_limit_hint.hide()
        mode_form.addRow("", self.range_limit_hint)

        self.nplc_combo = QComboBox()
        self.nplc_combo.currentIndexChanged.connect(self._settings_changed)
        self.nplc_combo.currentIndexChanged.connect(self._update_digital_filter_availability)
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
        self.measurement_speed_help = QLabel()
        self.measurement_speed_help.setObjectName("fieldHint")
        self.measurement_speed_help.setWordWrap(True)
        mode_form.addRow("", self.measurement_speed_help)
        self.analog_filter_check = QCheckBox("启用直流模拟滤波器")
        self.analog_filter_check.toggled.connect(self._settings_changed)
        mode_form.addRow("模拟滤波", self.analog_filter_check)
        self.input_impedance_auto_check = QCheckBox("启用自动高输入阻抗")
        self.input_impedance_auto_check.setToolTip(
            "仅适用于直流电压 DCV。启用后，仪表在 100 mV、1 V、10 V 量程使用大于 10 GΩ 的输入阻抗；"
            "其他量程仍为 10 MΩ。默认关闭（10 MΩ）。"
        )
        self.input_impedance_auto_check.toggled.connect(self._settings_changed)
        mode_form.addRow("输入阻抗", self.input_impedance_auto_check)
        self.digital_filter_check = QCheckBox("直流数字滤波")
        self.digital_filter_check.setToolTip(
            DIGITAL_FILTER_INFO
        )
        self.digital_filter_check.toggled.connect(self._update_digital_filter_availability)
        self.digital_filter_check.toggled.connect(self._settings_changed)
        mode_form.addRow("数字滤波", self.digital_filter_check)
        self.autozero_check = QCheckBox("自动零点校正")
        self.autozero_check.setChecked(True)
        self.autozero_help = (
            "启用后，仪表按自动零点流程测量内部零点并补偿偏移，有助于稳定读数；"
            "额外零点测量会增加读数时间。它不会把被测输入的读数强制改成 0。"
        )
        self.autozero_check.setToolTip(self.autozero_help)
        self.autozero_check.toggled.connect(self._settings_changed)
        self.autozero_label = QLabel("零点校正")
        self.autozero_label.setToolTip(self.autozero_help)
        mode_form.addRow(self.autozero_label, self.autozero_check)
        self.autozero_speed_hint = QLabel("启用自动零点会增加测量时间；不会把被测读数强制改成 0。")
        self.autozero_speed_hint.setObjectName("fieldHint")
        self.autozero_speed_hint.setWordWrap(True)
        mode_form.addRow("", self.autozero_speed_hint)

        settings_comparison = QFrame()
        settings_comparison.setObjectName("settingsComparison")
        comparison_layout = QHBoxLayout(settings_comparison)
        comparison_layout.setContentsMargins(8, 7, 8, 7)
        comparison_layout.setSpacing(8)
        active_column = QWidget()
        active_layout = QVBoxLayout(active_column)
        active_layout.setContentsMargins(0, 0, 0, 0)
        active_layout.setSpacing(3)
        active_heading = QLabel("当前生效")
        active_heading.setObjectName("settingsSummaryHeading")
        self.settings_active_summary = QLabel("仪器设置待读回")
        self.settings_active_summary.setObjectName("settingsActiveSummary")
        self.settings_active_summary.setWordWrap(True)
        active_layout.addWidget(active_heading)
        active_layout.addWidget(self.settings_active_summary)
        pending_column = QWidget()
        pending_layout = QVBoxLayout(pending_column)
        pending_layout.setContentsMargins(0, 0, 0, 0)
        pending_layout.setSpacing(3)
        pending_heading = QLabel("待应用")
        pending_heading.setObjectName("settingsSummaryHeading")
        self.settings_pending_summary = QLabel("本机选择")
        self.settings_pending_summary.setObjectName("settingsPendingSummary")
        self.settings_pending_summary.setWordWrap(True)
        pending_layout.addWidget(pending_heading)
        pending_layout.addWidget(self.settings_pending_summary)
        comparison_layout.addWidget(active_column, 1)
        comparison_layout.addWidget(pending_column, 1)
        mode_form.addRow(settings_comparison)

        self.apply_button = QPushButton("应用仪器参数")
        self.apply_button.setObjectName("primaryButton")
        self.apply_button.clicked.connect(self._apply_settings_from_ui)
        self.initialize_parameters_button = QPushButton("恢复默认设置")
        self.initialize_parameters_button.setObjectName("secondaryButton")
        self.initialize_parameters_button.setToolTip(
            "将六种测量模式及软件目标采样间隔恢复为默认值。此操作仅恢复软件控件和配置，不会立即下发仪器；"
            "需要生效时点击“应用仪器参数”。串口设置和保存目录会保留，也不会复位仪表。"
        )
        self._initialize_parameters_tooltip = self.initialize_parameters_button.toolTip()
        self.initialize_parameters_button.clicked.connect(self._initialize_parameters)
        self.apply_speed_button = QPushButton("应用采集速度")
        self.apply_speed_button.setObjectName("secondaryButton")
        self.apply_speed_button.setToolTip(
            "连续采集或暂停时，把所选NPLC/交流带宽写入仪表并回读确认；从下一个读数起生效。"
            "不会更改测量模式、量程、滤波或接线端子。"
        )
        self.apply_speed_button.clicked.connect(self._apply_acquisition_speed)
        parameter_actions = QHBoxLayout()
        parameter_actions.setSpacing(6)
        parameter_actions.addWidget(self.apply_button, 1)
        parameter_actions.addWidget(self.initialize_parameters_button)
        mode_form.addRow("", parameter_actions)
        mode_form.addRow("仪器速度", self.apply_speed_button)
        self.zero_button = QPushButton("执行 ZERO 校正")
        self.zero_button.setObjectName("secondaryButton")
        self.zero_help = (
            "向仪表发送 ZERO:AUTO ONCE，立即启动一次零点测量并更新零点补偿。"
            "这不是校准调整，也不会把当前显示值清零。停止采集后执行；使用模拟滤波时，"
            "请保持量程、NPLC和触发条件与零点测量要求一致。"
        )
        self.zero_button.setToolTip(self.zero_help)
        self.zero_button.clicked.connect(self._request_zero)
        mode_form.addRow("", self.zero_button)
        self.zero_result_label = QLabel("")
        self.zero_result_label.setObjectName("zeroResultLabel")
        self.zero_result_label.setWordWrap(True)
        self.zero_result_label.hide()
        mode_form.addRow("ZERO执行反馈", self.zero_result_label)
        self.safety_text = QLabel()
        self.safety_text.setObjectName("safetyBox")
        self.safety_text.setWordWrap(True)
        mode_form.addRow("接线提示", self.safety_text)
        self.terminal_diagram = QFrame()
        self.terminal_diagram.setObjectName("terminalDiagram")
        self.terminal_diagram.setAccessibleName("当前测量模式的仪表端子示意图")
        terminal_diagram_layout = QVBoxLayout(self.terminal_diagram)
        terminal_diagram_layout.setContentsMargins(8, 6, 8, 6)
        terminal_diagram_layout.setSpacing(5)
        self.terminal_diagram_title = QLabel("INPUT")
        self.terminal_diagram_title.setObjectName("terminalDiagramTitle")
        terminal_diagram_layout.addWidget(self.terminal_diagram_title)
        terminal_groups_layout = QHBoxLayout()
        terminal_groups_layout.setContentsMargins(0, 0, 0, 0)
        terminal_groups_layout.setSpacing(12)
        terminal_diagram_layout.addLayout(terminal_groups_layout)

        self.terminal_socket_widgets = {}
        self.terminal_input_group = QWidget()
        terminal_input_layout = QVBoxLayout(self.terminal_input_group)
        terminal_input_layout.setContentsMargins(0, 0, 0, 0)
        terminal_input_layout.setSpacing(3)
        input_heading = QLabel("INPUT")
        input_heading.setObjectName("terminalGroupHeading")
        terminal_input_layout.addWidget(input_heading)
        input_sockets = QGridLayout()
        input_sockets.setContentsMargins(0, 0, 0, 0)
        input_sockets.setHorizontalSpacing(6)
        input_sockets.setVerticalSpacing(4)
        terminal_input_layout.addLayout(input_sockets)
        for key, label_text, tone, row, column in (
            ("hi", "HI", "high", 0, 0),
            ("lo", "LO", "low", 0, 1),
            ("ma", "mA / 400 mA", "current", 1, 0),
            ("10a", "10 A", "current_high", 1, 1),
        ):
            socket = QFrame()
            socket.setObjectName("terminalSocket")
            socket.setProperty("socketTone", tone)
            socket.setAccessibleName(f"{label_text}端子")
            socket_layout = QHBoxLayout(socket)
            socket_layout.setContentsMargins(7, 4, 7, 4)
            socket_layout.setSpacing(6)
            marker = QFrame()
            marker.setObjectName("terminalSocketMarker")
            marker.setProperty("socketTone", tone)
            marker.setFixedSize(16, 16)
            marker_color = {
                "high": "#c04a53", "low": "#565e5f",
                "current": "#c04a53", "current_high": "#a87930",
            }[tone]
            marker.setStyleSheet(
                f"background: #30383c; border: 2px solid {marker_color}; border-radius: 8px;"
            )
            socket_layout.addWidget(marker)
            socket_label = QLabel(label_text)
            socket_label.setObjectName("terminalSocketLabel")
            socket_layout.addWidget(socket_label, 1)
            self.terminal_socket_widgets[key] = socket
            input_sockets.addWidget(socket, row, column)
        terminal_groups_layout.addWidget(self.terminal_input_group, 1)

        self.terminal_sense_group = QWidget()
        terminal_sense_layout = QVBoxLayout(self.terminal_sense_group)
        terminal_sense_layout.setContentsMargins(0, 0, 0, 0)
        terminal_sense_layout.setSpacing(3)
        sense_heading = QLabel("SENSE")
        sense_heading.setObjectName("terminalGroupHeading")
        terminal_sense_layout.addWidget(sense_heading)
        sense_sockets = QVBoxLayout()
        sense_sockets.setContentsMargins(0, 0, 0, 0)
        sense_sockets.setSpacing(4)
        terminal_sense_layout.addLayout(sense_sockets)
        for key, label_text, tone in (
            ("sense_hi", "SENSE HI", "high"),
            ("sense_lo", "SENSE LO", "low"),
        ):
            socket = QFrame()
            socket.setObjectName("terminalSocket")
            socket.setProperty("socketTone", tone)
            socket.setAccessibleName(f"{label_text}端子")
            socket_layout = QHBoxLayout(socket)
            socket_layout.setContentsMargins(7, 4, 7, 4)
            socket_layout.setSpacing(6)
            marker = QFrame()
            marker.setObjectName("terminalSocketMarker")
            marker.setProperty("socketTone", tone)
            marker.setFixedSize(16, 16)
            marker_color = {"high": "#c04a53", "low": "#565e5f"}[tone]
            marker.setStyleSheet(
                f"background: #30383c; border: 2px solid {marker_color}; border-radius: 8px;"
            )
            socket_layout.addWidget(marker)
            socket_label = QLabel(label_text)
            socket_label.setObjectName("terminalSocketLabel")
            socket_layout.addWidget(socket_label, 1)
            self.terminal_socket_widgets[key] = socket
            sense_sockets.addWidget(socket)
        terminal_groups_layout.addWidget(self.terminal_sense_group, 1)

        self.terminal_diagram_note = QLabel(
            "仅供端子位置参考，不检测实际接线；请以仪器面板丝印和接线提示为准。"
        )
        self.terminal_diagram_note.setObjectName("terminalDiagramNote")
        self.terminal_diagram_note.setWordWrap(True)
        self.terminal_diagram_note.setAccessibleName("端子示意图限制说明")
        terminal_diagram_layout.addWidget(self.terminal_diagram_note)
        mode_form.addRow("端子示意", self.terminal_diagram)
        measurement_layout.addWidget(mode_box)

        experiment = QGroupBox("实验文件")
        experiment.setObjectName("sidebarCard")
        exp_form = QFormLayout(experiment)
        exp_form.setContentsMargins(12, 18, 12, 12)
        exp_form.setHorizontalSpacing(9)
        exp_form.setVerticalSpacing(7)
        self.experiment_name = QLineEdit(self._suggest_experiment_name(self._current_mode))
        self.experiment_name.textChanged.connect(self._validate_experiment_name_field)
        exp_form.addRow("实验名称", self.experiment_name)
        self.experiment_name_validation = QLabel("名称有效；如有同名实验，可选择先备份再覆盖、另存目录或改名。")
        self.experiment_name_validation.setObjectName("fieldHint")
        self.experiment_name_validation.setWordWrap(True)
        exp_form.addRow("", self.experiment_name_validation)
        self.preflight_status = QLabel("开始前检查仪器连接、当前配置回读、接线确认和保存目录。")
        self.preflight_status.setObjectName("fieldHint")
        self.preflight_status.setWordWrap(True)
        exp_form.addRow("采集预检", self.preflight_status)
        self.experiment_name.textChanged.connect(lambda _text: self._reset_preflight_status())
        configuration_start_actions = QHBoxLayout()
        configuration_start_actions.setSpacing(8)
        self.configuration_single_button = QPushButton("单次测量")
        self.configuration_single_button.setObjectName("configurationSingleButton")
        self.configuration_single_button.setProperty("class", "secondaryButton")
        self.configuration_single_button.setMinimumHeight(34)
        self.configuration_single_button.setToolTip(
            "完成采集预检后读取一次并保存。真实仪器会先要求确认接线和安全状态。"
        )
        self.configuration_single_button.clicked.connect(
            lambda: self._start_from_configuration(one_shot=True)
        )
        self.configuration_start_button = QPushButton("开始连续采集")
        self.configuration_start_button.setObjectName("configurationStartButton")
        self.configuration_start_button.setProperty("class", "primaryButton")
        self.configuration_start_button.setMinimumHeight(34)
        self.configuration_start_button.setToolTip(
            "完成采集预检后开始连续采集。真实仪器会先要求确认接线和安全状态。"
        )
        self.configuration_start_button.clicked.connect(
            lambda: self._start_from_configuration(one_shot=False)
        )
        configuration_start_actions.addWidget(self.configuration_single_button)
        configuration_start_actions.addWidget(self.configuration_start_button)
        configuration_start_actions.addStretch(1)
        exp_form.addRow("开始测量", configuration_start_actions)
        self.measurement_setup_hint = QLabel(
            "新测量开始后，主界面曲线自动切换到本次数据；旧实验 CSV 保存在各自目录。无需先清空曲线。"
        )
        self.measurement_setup_hint.setObjectName("fieldHint")
        self.measurement_setup_hint.setWordWrap(True)
        exp_form.addRow("", self.measurement_setup_hint)
        default_root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)) / "Fluke8845A_Experiments"
        saved_root = self._profiles.get("output_root", "")
        self.output_root = QLineEdit(saved_root or str(default_root))
        self.output_root.textChanged.connect(lambda _text: self._reset_preflight_status())
        self.output_root.editingFinished.connect(self._save_profiles)
        browse = QPushButton("选择")
        self.output_root_browse_button = browse
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self._choose_output_root)
        root_row = QHBoxLayout()
        root_row.setSpacing(6)
        root_row.addWidget(self.output_root, 1)
        root_row.addWidget(browse)
        exp_form.addRow("保存目录", root_row)
        self.recent_experiment_path = QLabel("尚无实验目录")
        self.recent_experiment_path.setWordWrap(True)
        self.recent_experiment_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_experiment_directory_button = QPushButton("打开实验目录")
        self.open_experiment_directory_button.setObjectName("secondaryButton")
        self.copy_experiment_path_button = QPushButton("复制路径")
        self.copy_experiment_path_button.setObjectName("secondaryButton")
        self.open_experiment_directory_button.clicked.connect(self._open_experiment_directory)
        self.copy_experiment_path_button.clicked.connect(self._copy_experiment_path)
        recent_path_row = QHBoxLayout()
        recent_path_row.addWidget(self.recent_experiment_path, 1)
        recent_path_row.addWidget(self.open_experiment_directory_button)
        recent_path_row.addWidget(self.copy_experiment_path_button)
        exp_form.addRow("最近实验", recent_path_row)
        self.recent_experiment_files_label = QLabel(
            "采集后尝试生成 CSV_打开说明.txt；若未生成，实验摘要会提示使用 events_excel_safe.csv。曲线 PNG 按需另存。"
        )
        self.recent_experiment_files_label.setObjectName("fieldHint")
        self.recent_experiment_files_label.setWordWrap(True)
        self.recent_experiment_files_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        exp_form.addRow("文件清单", self.recent_experiment_files_label)
        self.export_snapshot_button = QPushButton("导出进行中 CSV 快照…")
        self.export_snapshot_button.setObjectName("secondaryButton")
        self.export_snapshot_button.setToolTip(
            "采集中导出原始 CSV 的完整记录快照，不修改正在写入的原文件；快照时间保存在配套 JSON 中。"
        )
        self.export_snapshot_button.clicked.connect(self._export_running_snapshot)
        exp_form.addRow("运行中导出", self.export_snapshot_button)
        self.mock_mode_check = QCheckBox("使用明确标记的 MOCK 模拟仪器")
        self.mock_mode_check.setObjectName("mockCheck")
        self.mock_mode_check.toggled.connect(self._update_connect_label)
        exp_form.addRow("验证模式", self.mock_mode_check)
        self.scenario_combo = QComboBox()
        for label in ("阶跃响应与恢复", "长期漂移", "通信超时测试", "断线恢复测试"):
            self.scenario_combo.addItem(label)
        self.scenario_combo.setEnabled(False)
        self.mock_scenario_status = QLabel("MOCK场景：未连接")
        self.mock_scenario_status.setObjectName("secondaryText")
        self.mock_scenario_status.setWordWrap(True)
        self.scenario_combo.currentTextChanged.connect(self._update_mock_scenario_status)
        exp_form.addRow("Mock场景", self.scenario_combo)
        exp_form.addRow("当前模拟场景", self.mock_scenario_status)
        experiment_layout.addWidget(experiment)

        # Simulation is a connection choice; keep it next to the COM settings.
        exp_form.removeWidget(self.mock_mode_check)
        exp_form.removeWidget(self.scenario_combo)
        form.addRow("模拟模式", self.mock_mode_check)
        form.addRow("模拟场景", self.scenario_combo)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0, 86400)
        self.interval_spin.setDecimals(3)
        self.interval_spin.setSingleStep(0.1)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(1.0)
        self.interval_spin.setToolTip(
            "软件目标间隔与仪器测量速度独立。0 表示每次读数后不额外等待；"
            "开始测量后本次实验的间隔固定，实测间隔仍受仪器转换和通信耗时影响。"
        )
        self.interval_spin.valueChanged.connect(self._on_sampling_interval_changed)
        interval_row = QHBoxLayout()
        interval_row.setContentsMargins(0, 0, 0, 0)
        interval_row.setSpacing(5)
        self.target_interval_label = QLabel("目标采样间隔")
        self.target_interval_label.setObjectName("targetIntervalLabel")
        interval_row.addWidget(self.target_interval_label)
        self.interval_spin.setFixedWidth(150)
        interval_row.addWidget(self.interval_spin)
        self.fastest_interval_label = QLabel("0 = 最快可用")
        self.fastest_interval_label.setObjectName("fastestIntervalLabel")
        interval_row.addWidget(self.fastest_interval_label)
        interval_row.addStretch(1)
        self._interval_row = interval_row
        mode_form.insertRow(2, "", self._interval_row)
        mode_form.setRowVisible(self.mode_combo, False)
        mode_form.setRowVisible(range_row, False)
        mode_form.setRowVisible(self.apply_speed_button, False)
        mode_form.setRowVisible(self.measurement_speed_help, False)
        mode_form.setRowVisible(self.autozero_speed_hint, False)
        mode_form.setRowVisible(self.terminal_diagram, False)
        mode_form.setRowVisible(self.safety_text, False)
        form.setRowVisible(self.diagnose_button, False)
        form.setRowVisible(self.connection_stages_label, False)
        self.control_mode_explanation_label.hide()

        self.start_button = QPushButton("开始连续采集")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setToolTip("开始连续采集。快捷键：Ctrl+R")
        self.single_button = QPushButton("单次测量")
        self.single_button.setObjectName("secondaryButton")
        self.pause_button = QPushButton("暂停")
        self.pause_button.setObjectName("secondaryButton")
        self.pause_button.setToolTip("暂停或继续连续采集。快捷键：Ctrl+P")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setToolTip("停止并保存当前实验。快捷键：Ctrl+Shift+S")
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(lambda: self._begin_simple_measurement(one_shot=False))
        self.single_button.clicked.connect(lambda: self._begin_simple_measurement(one_shot=True))
        self.pause_button.clicked.connect(self._pause_or_resume)
        self.stop_button.clicked.connect(self._stop)
        self.experiment_status = QLabel("就绪")
        self.experiment_status.setObjectName("statusValue")
        self.actual_rate_label = QLabel("—")
        self.actual_rate_label.setObjectName("metricValue")
        self.actual_rate_label.setToolTip("依据连续有效读数的 PC 单调时钟时间戳计算相邻读数间隔，不由目标间隔推算。")

        communication_logs = QGroupBox("SCPI 收发与运行记录")
        communication_logs.setObjectName("communicationLogCard")
        communication_logs_layout = QVBoxLayout(communication_logs)
        communication_logs_layout.setContentsMargins(12, 16, 12, 10)
        communication_logs_layout.setSpacing(6)
        communication_toolbar = QHBoxLayout()
        communication_toolbar.addWidget(QLabel("筛选"))
        self.communication_log_filter_combo = QComboBox()
        self.communication_log_filter_combo.setObjectName("communicationLogFilter")
        for label, key in (
            ("全部日志", "all"), ("错误", "error"), ("SCPI命令", "scpi"),
            ("连接", "connection"), ("采集", "acquisition"),
        ):
            self.communication_log_filter_combo.addItem(label, key)
        self.communication_log_filter_combo.currentIndexChanged.connect(self._render_filtered_log)
        communication_toolbar.addWidget(self.communication_log_filter_combo)
        communication_toolbar.addStretch(1)
        self.communication_export_log_button = QPushButton("导出当前筛选")
        self.communication_export_log_button.setObjectName("communicationExportLogButton")
        self.communication_export_log_button.clicked.connect(self._export_filtered_logs)
        communication_toolbar.addWidget(self.communication_export_log_button)
        communication_logs_layout.addLayout(communication_toolbar)
        from PySide6.QtWidgets import QPlainTextEdit
        self.communication_log_text = QPlainTextEdit()
        self.communication_log_text.setObjectName("communicationLogText")
        self.communication_log_text.setReadOnly(True)
        self.communication_log_text.document().setMaximumBlockCount(6000)
        self.communication_log_text.setPlaceholderText("串口识别、SCPI命令收发和通信异常会显示在这里。")
        self.communication_log_text.setMinimumHeight(180)
        communication_logs_layout.addWidget(self.communication_log_text, 1)
        communication_layout.addWidget(communication_logs, 1)
        communication_logs.hide()
        measurement_layout.addStretch(1)
        experiment_layout.addStretch(1)
        self.setup_section_stack.addWidget(self.communication_workspace)
        self.setup_section_stack.addWidget(measurement_page)
        self.setup_section_stack.addWidget(experiment_page)
        layout.addLayout(self.setup_section_stack, 1)
        self._show_setup_section(0)

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
        reading_content = QWidget()
        reading_content.setObjectName("readingContent")
        reading_text = QVBoxLayout(reading_content)
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
        self.reading_mode_label.hide()
        reading_text.addWidget(self.latest_value_label)
        self.valid_sample_count_label = QLabel("有效样本 0")
        self.valid_sample_count_label.setObjectName("metricValue")
        self.valid_sample_count_label.setToolTip("当前实验累计的有效读数数目；无效读数不会计入。")
        reading_text.addWidget(self.valid_sample_count_label)
        self._reading_content = reading_content

        self._reading_divider = QFrame()
        self._reading_divider.setObjectName("cardDivider")
        self._reading_divider.setFrameShape(QFrame.Shape.VLine)
        reading_layout.addWidget(self._reading_divider)

        self._status_caption = QLabel("采集状态")
        self._status_caption.setObjectName("eyebrow")
        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self._rate_caption = QLabel("实测采样间隔")
        self._rate_caption.setObjectName("eyebrow")
        self._rate_caption.setToolTip(
            "实测间隔按相邻有效读数的 PC 单调时钟时间戳计算；"
            "不代表仪器内部转换时间，也不等于软件目标等待时间。"
        )
        self._elapsed_caption = QLabel("实验历时")
        self._elapsed_caption.setObjectName("eyebrow")
        self.elapsed_label = QLabel("00:00:00")
        self.elapsed_label.setToolTip("按单调时钟记录的实验历时包含暂停时间；暂停时显示冻结，继续采集后计入暂停时长。CSV 时间戳与历时精确保存在文件中。")
        self.elapsed_label.setObjectName("metricValue")

        status_tile = QFrame()
        status_tile.setObjectName("statusMetric")
        self._status_tile = status_tile
        status_tile.setObjectName("metricCell")
        status_tile.setMinimumWidth(132)
        status_layout = QVBoxLayout(status_tile)
        self._status_metric_layout = status_layout
        status_layout.setContentsMargins(10, 7, 10, 7)
        status_layout.setSpacing(3)
        status_layout.addWidget(self._status_caption)
        status_value_row = QHBoxLayout()
        status_value_row.setSpacing(6)
        status_value_row.addWidget(self.status_dot)
        status_value_row.addWidget(self.experiment_status, 1)
        status_layout.addLayout(status_value_row)
        self.latest_invalid_label = QLabel("最近无效读数：无")
        self.latest_invalid_label.setObjectName("latestInvalidLabel")
        self.latest_invalid_label.setWordWrap(True)
        self.latest_invalid_label.setToolTip("采集中的无效读数及其原因会显示在这里")
        self.latest_invalid_row = QHBoxLayout()
        self.latest_invalid_row.setContentsMargins(0, 0, 0, 0)
        self.latest_invalid_row.setSpacing(4)
        self.latest_invalid_row.addWidget(self.latest_invalid_label, 1)
        self.latest_invalid_log_button = QPushButton("查看异常日志")
        self.latest_invalid_log_button.setObjectName("secondaryButton")
        self.latest_invalid_log_button.setEnabled(False)
        self.latest_invalid_log_button.clicked.connect(self._jump_to_latest_invalid_log)
        self.latest_invalid_row.addWidget(self.latest_invalid_log_button, 0)
        status_layout.addLayout(self.latest_invalid_row)
        self.display_loss_warning = QLabel("")
        self.display_loss_warning.setObjectName("displayLossWarning")
        self.display_loss_warning.setWordWrap(True)
        self.display_loss_warning.setAccessibleName("曲线显示点丢弃提示")
        self.display_loss_warning.setToolTip(
            "此提示只针对实时曲线的显示缓存。每次仪器测量仍会逐点写入原始 measurements.csv。"
        )
        self.display_loss_warning.hide()
        status_layout.addWidget(self.display_loss_warning)
        self.voltage_range_warning = QLabel("")
        self.voltage_range_warning.setObjectName("voltageRangeWarning")
        self.voltage_range_warning.setWordWrap(True)
        self.voltage_range_warning.setAccessibleName("电压图表范围提示")
        self.voltage_range_warning.setToolTip(
            "±1000 V 是图表显示边界，不是仪器量程或输入额定值；8845A ACV 最大输入为 750 V RMS、1000 V peak "
            "或 8×10^7 V·Hz 乘积，以先达到的限制为准。"
            "超出图表边界的点只隐藏曲线显示，原始数据仍保留。"
        )
        self.voltage_range_warning.hide()
        status_layout.addWidget(self.voltage_range_warning)
        self.recovery_hint_label = QLabel("")
        self.recovery_hint_label.setObjectName("recoveryHintLabel")
        self.recovery_hint_label.setWordWrap(True)
        self.recovery_hint_label.hide()
        status_layout.addWidget(self.recovery_hint_label)
        rate_tile = QFrame()
        rate_tile.setObjectName("rateMetric")
        self._rate_tile = rate_tile
        rate_tile.setObjectName("metricCell")
        rate_tile.setMinimumWidth(104)
        rate_layout = QVBoxLayout(rate_tile)
        self._rate_metric_layout = rate_layout
        rate_layout.setContentsMargins(10, 7, 10, 7)
        rate_layout.setSpacing(3)
        rate_layout.addWidget(self._rate_caption)
        rate_layout.addWidget(self.actual_rate_label)
        elapsed_tile = QFrame()
        elapsed_tile.setObjectName("elapsedMetric")
        elapsed_tile.setObjectName("metricCell")
        self._elapsed_tile = elapsed_tile
        elapsed_tile.setMinimumWidth(112)
        elapsed_layout = QVBoxLayout(elapsed_tile)
        self._elapsed_metric_layout = elapsed_layout
        elapsed_layout.setContentsMargins(10, 7, 10, 7)
        elapsed_layout.setSpacing(3)
        elapsed_layout.addWidget(self._elapsed_caption)
        elapsed_layout.addWidget(self.elapsed_label)
        # Primary information follows the live-operator scan order: status,
        # reading, valid count, then measured interval and elapsed time.
        reading_layout.addWidget(status_tile, 1)
        reading_layout.addWidget(self._reading_divider)
        reading_layout.addWidget(reading_content, 2)
        reading_layout.addWidget(rate_tile, 1)
        reading_layout.addWidget(elapsed_tile, 1)
        layout.addWidget(reading_card)
        self._reading_layout = reading_layout
        self._metrics_layout = reading_layout
        self._reading_layout_mode = None
        self._short_chart_layout = None
        self._short_viewport_layout = None

        measurement_action_bar = QFrame()
        measurement_action_bar.setObjectName("measurementActionBar")
        self.measurement_action_bar = measurement_action_bar
        measurement_action_layout = QHBoxLayout(measurement_action_bar)
        measurement_action_layout.setContentsMargins(10, 5, 10, 5)
        measurement_action_layout.setSpacing(8)
        self._measurement_action_layout = measurement_action_layout
        self._new_measurement_idle_hint = (
            "新建测量：点“新建测量”（Ctrl+N）→ 填名称 → 点“单次测量”或“开始连续采集”。"
            "新测量开始后自动换图，旧 CSV 保留；“清空图表”只清除显示。"
        )
        self._new_measurement_compact_hint = (
            "重测：点“新建测量”→ 填名称并开始；新测量开始后自动换图，旧 CSV 保留。“清空图表”只清显示。"
        )
        self.new_measurement_hint = QLabel(self._new_measurement_idle_hint)
        self.new_measurement_hint.setObjectName("secondaryText")
        self.new_measurement_hint.setWordWrap(True)
        measurement_action_layout.addWidget(self.new_measurement_hint, 1)
        layout.addWidget(measurement_action_bar)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        self._action_bar = action_bar
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
        self.plot_toolbar = plot_toolbar
        plot_toolbar_layout = QGridLayout(plot_toolbar)
        self._plot_toolbar_layout = plot_toolbar_layout
        self._plot_toolbar_mode = None
        plot_toolbar_layout.setContentsMargins(2, 0, 2, 0)
        plot_toolbar_layout.setHorizontalSpacing(8)
        plot_toolbar_layout.setVerticalSpacing(4)
        self.follow_check = QCheckBox("滚动跟随")
        self.follow_check.setObjectName("chartFollowCheck")
        self.follow_check.setChecked(False)
        self.follow_check.setToolTip("开启后视图随最新读数滚动；关闭后可以固定查看历史区间")
        self.follow_check.toggled.connect(self._on_follow_toggled)
        plot_toolbar_layout.addWidget(self.follow_check, 0, 0)
        self.x_auto_check = QCheckBox("Full（全部）")
        self.x_auto_check.setChecked(True)
        self.x_auto_check.toggled.connect(self._on_x_auto_toggled)
        self.y_auto_check = QCheckBox("纵轴自动")
        self.y_auto_check.setChecked(True)
        self.y_auto_check.toggled.connect(self._on_y_auto_toggled)
        for check in (self.x_auto_check, self.y_auto_check, self.follow_check):
            check.setStyleSheet("QCheckBox { color: #e5f0ed; spacing: 6px; }")
        self.view_mode_label = QLabel("自动跟随最新数据")
        self.view_mode_label.setObjectName("chartViewMode")
        plot_toolbar_layout.addWidget(self.view_mode_label, 0, 1)
        self.acquisition_hint = QLabel("手动缩放任一坐标轴或平移时间轴会停止自动跟随")
        self.acquisition_hint.setObjectName("chartAcquisitionHint")
        self.acquisition_hint.setWordWrap(True)
        plot_toolbar_layout.addWidget(self.acquisition_hint, 0, 2)
        self.chart_gesture_help_button = QPushButton("操作说明")
        self.chart_gesture_help_button.setObjectName("secondaryButton")
        self.chart_gesture_help_button.setCheckable(True)
        self.chart_gesture_help_button.setToolTip("展开或收起图表的鼠标与键盘操作说明")
        plot_toolbar_layout.addWidget(self.chart_gesture_help_button, 0, 3)
        self.chart_source_label = QLabel("实时数据")
        self.chart_source_label.setObjectName("chartSourceLabel")
        self.chart_source_label.setToolTip("当前图表数据来源")
        plot_toolbar_layout.addWidget(self.chart_source_label, 0, 4)
        self.autoscale_button = QPushButton("全部数据")
        self.autoscale_button.setObjectName("secondaryButton")
        self.autoscale_button.setToolTip("自动缩放到当前数据集的完整时间与读数范围")
        self.autoscale_button.clicked.connect(self._enable_plot_autoscale)
        plot_toolbar_layout.addWidget(self.autoscale_button, 0, 5)
        self.quick_autoscale_button = QPushButton("自动缩放")
        self.quick_autoscale_button.setObjectName("secondaryButton")
        self.quick_autoscale_button.setToolTip("自动缩放到当前数据范围；也可双击曲线恢复自动缩放。")
        self.quick_autoscale_button.clicked.connect(self._enable_plot_autoscale)
        self._action_layout.addWidget(self.quick_autoscale_button)
        average_label = QLabel("平均点数")
        self.average_window_spin = QSpinBox()
        self.average_window_spin.setObjectName("averageWindowSpin")
        self.average_window_spin.setStyleSheet(
            "QSpinBox, QSpinBox QLineEdit { color: #1d2c2a; background: #fffefa; }"
        )
        self.average_window_spin.setRange(1, MAX_PLOT_POINTS)
        self.average_window_spin.setValue(self._average_trace.window)
        self.average_window_spin.setKeyboardTracking(False)
        self.average_window_spin.setToolTip("滑动平均点数；仅影响图表。长时间采集显示最近的平均结果。")
        self.average_window_spin.valueChanged.connect(self._on_average_window_changed)
        self._action_layout.addWidget(average_label)
        self._action_layout.addWidget(self.average_window_spin)
        self.reset_measurement_button = QPushButton("重新测量")
        self.reset_measurement_button.setObjectName("primaryButton")
        self.reset_measurement_button.setToolTip("停止并保存后清空曲线")
        self.reset_measurement_button.clicked.connect(self._reset_measurement)
        self._action_layout.addWidget(self.reset_measurement_button)
        self.save_curve_button = QPushButton("保存曲线")
        self.save_curve_button.setObjectName("primaryButton")
        self.save_curve_button.setToolTip("保存简洁和详细曲线 CSV")
        self.save_curve_button.clicked.connect(self._save_curve_csv)
        self._action_layout.addWidget(self.save_curve_button)
        self.return_live_button = QPushButton("回到实时窗口")
        self.return_live_button.setObjectName("primaryButton")
        self.return_live_button.setToolTip("历史视图返回刚才的实时曲线；实时视图跟随最新数据")
        self.return_live_button.setEnabled(False)
        self.return_live_button.clicked.connect(self._return_to_live_view)
        plot_toolbar_layout.addWidget(self.return_live_button, 0, 6)
        self.history_button = QPushButton("加载 CSV")
        self.history_button.setObjectName("secondaryButton")
        self.history_button.clicked.connect(self._load_history)
        plot_toolbar_layout.addWidget(self.history_button, 0, 7)
        self.screenshot_button = QPushButton("曲线截图")
        self.screenshot_button.setObjectName("secondaryButton")
        self.screenshot_button.clicked.connect(self._save_screenshot)
        plot_toolbar_layout.addWidget(self.screenshot_button, 0, 8)
        self.y_axis_span_control = QWidget(plot_toolbar)
        self.y_axis_span_control.setObjectName("yAxisSpanControl")
        y_axis_span_layout = QHBoxLayout(self.y_axis_span_control)
        y_axis_span_layout.setContentsMargins(0, 0, 0, 0)
        y_axis_span_layout.setSpacing(5)
        y_axis_span_label = QLabel("纵轴跨度")
        y_axis_span_label.setObjectName("secondaryText")
        y_axis_span_label.setToolTip(
            "范围按右侧单位输入；例如单位为 Ω 时，1e-6 表示 1 µΩ。"
            "最小跨度受计算机浮点精度限制，放大不会提升仪表分辨率；"
            "只改变图表显示，不修改原始读数。"
        )
        self.y_axis_span_edit = QLineEdit()
        self.y_axis_span_edit.setObjectName("yAxisSpanEdit")
        self.y_axis_span_edit.setFixedWidth(112)
        self.y_axis_span_edit.setPlaceholderText("例如 1e-6")
        self.y_axis_span_edit.setToolTip(
            "范围按右侧单位输入；例如单位为 Ω 时，1e-6 表示 1 µΩ。"
            "最小跨度受计算机浮点精度限制，放大不会提升仪表分辨率；"
            "只改变图表显示，不修改原始读数。"
        )
        self.y_axis_span_unit_label = QLabel("V")
        self.y_axis_span_unit_label.setObjectName("secondaryText")
        self.apply_y_axis_span_button = QPushButton("应用")
        self.apply_y_axis_span_button.setObjectName("secondaryButton")
        self.apply_y_axis_span_button.setFixedWidth(72)
        self.apply_y_axis_span_button.setToolTip("按输入跨度缩放纵轴，并保持当前纵轴中心")
        y_axis_span_layout.addWidget(y_axis_span_label)
        y_axis_span_layout.addWidget(self.y_axis_span_edit)
        y_axis_span_layout.addWidget(self.y_axis_span_unit_label)
        y_axis_span_layout.addWidget(self.apply_y_axis_span_button)
        self.apply_y_axis_span_button.clicked.connect(self._apply_plot_y_span)
        self.y_axis_span_edit.returnPressed.connect(self._apply_plot_y_span)
        self._plot_toolbar_widgets = (
            self.follow_check,
            self.x_auto_check,
            self.y_auto_check,
            self.view_mode_label,
            self.acquisition_hint,
            self.chart_gesture_help_button,
            self.chart_source_label,
            self.autoscale_button,
            self.return_live_button,
            self.history_button,
            self.screenshot_button,
        )

        self.chart_gesture_help = QLabel(
            "普通滚轮：缩放横纵轴\nShift + 滚轮：缩放时间轴\nCtrl + 滚轮：缩放纵轴"
        )
        self.chart_gesture_help.setObjectName("secondaryText")
        self.chart_gesture_help.setWordWrap(True)
        self.chart_gesture_help.setContentsMargins(8, 2, 8, 5)
        self.chart_gesture_help.hide()
        self.chart_gesture_help_button.toggled.connect(self.chart_gesture_help.setVisible)

        plot_frame = QFrame()
        plot_frame.setObjectName("plotCard")
        self._plot_frame = plot_frame
        plot_layout = QStackedLayout(plot_frame)
        plot_layout.setContentsMargins(4, 4, 4, 4)
        plot_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.view_box = _MeasurementViewBox()
        self.plot_widget = _MeasurementPlotWidget(view_box=self.view_box)
        self.plot_widget.setObjectName("trendPlot")
        self._plot_viewport = self.plot_widget.viewport()
        self._plot_viewport.installEventFilter(self)
        wheel_tooltip = "普通滚轮：缩放横纵轴\nShift + 滚轮：缩放时间轴\nCtrl + 滚轮：缩放纵轴"
        self.plot_widget.setToolTip(wheel_tooltip)
        self.plot_widget.viewport().setToolTip(wheel_tooltip)
        self.plot_widget.key_handler = self._handle_plot_key
        self.plot_widget.setMinimumHeight(230)
        self.plot_widget.setBackground("#fbf9f1")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.11)
        self.plot_widget.setLabel("bottom", "实验经过时间", units="s", color="#697470")
        self.plot_widget.setLabel("left", "测量值", color="#697470")
        legend = self.plot_widget.addLegend(offset=(12, 8))
        legend.setLabelTextColor("#526064")
        self.curve = self.plot_widget.plot([], [], pen=pg.mkPen("#138b92", width=2.2), name="原始读数")
        self.average_curve = self.plot_widget.plot([], [], pen=pg.mkPen("#df8b30", width=2.4), name="滑动平均")
        self.view_box.y_anchor_provider = self._plot_y_zoom_anchor_at_x
        plot_item = self.plot_widget.getPlotItem()
        self.y_axis = plot_item.getAxis("left")
        self.x_axis = plot_item.getAxis("bottom")
        for axis_name in ("left", "bottom"):
            axis = plot_item.getAxis(axis_name)
            axis.setTextPen(pg.mkPen("#526064"))
            axis.setPen(pg.mkPen("#96a09a"))
            axis.setTickFont(QFont("Microsoft YaHei UI", 10))
        plot_item.vb.sigRangeChanged.connect(self._position_event_labels)
        plot_item.vb.sigRangeChanged.connect(self._sync_plot_y_axis)
        plot_item.vb.sigRangeChanged.connect(self._sync_plot_x_axis)
        plot_item.vb.sigRangeChanged.connect(self._sync_plot_y_span_control)
        plot_item.vb.sigXRangeChanged.connect(self._on_plot_x_range_changed)
        self.view_box.userZoomed.connect(self._on_plot_wheel_zoom)
        self.view_box.yZoomLimitReached.connect(self._on_plot_y_zoom_limit_reached)
        self.view_box.rebaseRequested.connect(self._rebase_plot_for_zoom)
        self.view_box.sigRangeChangedManually.connect(self._on_plot_range_changed_manually)
        self.view_box.dragFinished.connect(self._on_plot_drag_finished)
        plot_item.setMenuEnabled(False)
        plot_item.vb.setMouseEnabled(x=True, y=True)
        plot_item.setClipToView(True)
        self.plot_widget.setXRange(0, 10, padding=0)
        plot_layout.addWidget(self.plot_widget)
        empty_overlay = QWidget()
        empty_overlay.setObjectName("plotEmptyOverlay")
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
        self.plot_empty_new_measurement_button = QPushButton("新建测量…")
        self.plot_empty_new_measurement_button.setObjectName("plotEmptyNewMeasurementButton")
        self.plot_empty_new_measurement_button.setProperty("class", "primaryButton")
        self.plot_empty_new_measurement_button.setAccessibleName("新建测量并重新绘制曲线")
        self.plot_empty_new_measurement_button.setMinimumWidth(124)
        self.plot_empty_new_measurement_button.setFixedHeight(34)
        self.plot_empty_new_measurement_button.setToolTip(
            "填写实验名称并选择单次测量或连续采集。开始后曲线自动换新，已保存的旧实验 CSV 保留。"
        )
        self.plot_empty_new_measurement_button.clicked.connect(self._open_new_measurement_setup)
        self.plot_empty_new_measurement_button.hide()
        empty_layout.addWidget(self.plot_empty_new_measurement_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.plot_empty_connect_button = QPushButton("连接设备或模拟器…")
        self.plot_empty_connect_button.setObjectName("plotEmptyConnectButton")
        self.plot_empty_connect_button.setToolTip("打开通讯设置，选择真实仪器或明确标记的 MOCK 模拟器。")
        self.plot_empty_connect_button.clicked.connect(
            lambda _checked=False: self._navigate_workspace("communications")
        )
        empty_layout.addWidget(self.plot_empty_connect_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.plot_empty_connect_button.hide()
        empty_layout.addStretch(3)
        plot_layout.addWidget(empty_overlay)
        self.plot_empty_overlay = empty_overlay
        self.plot_empty_overlay.raise_()
        layout.addWidget(plot_toolbar)
        layout.addWidget(plot_frame, 1)

        self.baseline_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#8a5d13", width=1.5, style=Qt.PenStyle.DashLine),
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
        self._region.sigRegionChanged.connect(self._sync_region_inputs)
        self._region.sigRegionChangeFinished.connect(self._on_region_change_finished)
        self._region_stats_label = QLabel("输入起止时间，或拖动图中的区间手柄")
        self._region_stats_label.setObjectName("secondaryText")
        plot_scene = self.plot_widget.getPlotItem().vb.scene()
        plot_scene.sigMouseClicked.connect(self._on_plot_clicked)
        self._locked_cursor_marker = pg.ScatterPlotItem(
            size=12, symbol="o", pen=pg.mkPen("#f7c948", width=2),
            brush=pg.mkBrush("#192536"),
        )
        self._locked_cursor_marker.setZValue(20)
        self.plot_widget.addItem(self._locked_cursor_marker, ignoreBounds=True)
        self._locked_cursor_marker.hide()

        self.analysis_tabs = QTabWidget()
        self.analysis_tabs.setObjectName("analysisTabs")
        self.analysis_tabs.setDocumentMode(True)
        self.analysis_tabs.tabBar().hide()
        self.analysis_tabs.setMinimumHeight(180)
        self.analysis_tabs.setMaximumHeight(16777215)

        stats_tab = QWidget()
        stats_tab.setObjectName("analysisStatisticsSection")
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
        region_input_row = QHBoxLayout()
        region_input_row.addWidget(QLabel("起点"))
        self.region_start_spin = QDoubleSpinBox()
        self.region_end_spin = QDoubleSpinBox()
        for spin in (self.region_start_spin, self.region_end_spin):
            spin.setRange(-1e9, 1e12)
            spin.setDecimals(6)
            spin.setSingleStep(1.0)
            spin.setSuffix(" s")
            spin.setKeyboardTracking(False)
            spin.setMaximumWidth(150)
        self.region_start_spin.setValue(0.0)
        self.region_end_spin.setValue(60.0)
        region_input_row.addWidget(self.region_start_spin)
        region_input_row.addWidget(QLabel("终点"))
        region_input_row.addWidget(self.region_end_spin)
        self.region_apply_button = QPushButton("应用区间")
        self.region_apply_button.setObjectName("secondaryButton")
        self.region_apply_button.clicked.connect(self._apply_region_inputs)
        region_input_row.addWidget(self.region_apply_button)
        region_input_row.addStretch(1)
        stats_layout.addLayout(region_input_row)
        stats_layout.addWidget(self.stats_label)
        stats_layout.addWidget(self._region_stats_label)
        stats_layout.addStretch(1)

        baseline_tab = QWidget()
        baseline_tab.setObjectName("analysisBaselineSection")
        baseline_layout = QVBoxLayout(baseline_tab)
        baseline_layout.setContentsMargins(12, 7, 12, 7)
        baseline_layout.setSpacing(5)
        baseline_title = QLabel("基准与相对变化")
        baseline_title.setObjectName("panelTitle")
        baseline_layout.addWidget(baseline_title)
        baseline_row = QHBoxLayout()
        baseline_row.addWidget(QLabel("R₀ / V₀ / I₀"))
        self.baseline_edit = QLineEdit()
        self.baseline_edit.setPlaceholderText("未设置")
        self.baseline_edit.setMaximumWidth(150)
        self.baseline_edit.editingFinished.connect(self._baseline_edited)
        baseline_row.addWidget(self.baseline_edit)
        self.set_baseline_button = QPushButton("当前值设为基准")
        self.set_baseline_button.setObjectName("secondaryButton")
        self.set_selected_baseline_button = QPushButton("锁定点设为基准")
        self.set_selected_baseline_button.setObjectName("secondaryButton")
        self.set_selected_baseline_button.setToolTip(
            "先在曲线上单击要用作基准的读数点。黄色圆圈是已锁定点；"
            "鼠标悬停不会改变它。"
        )
        self.mean_baseline_button = QPushButton("可见均值设为基准")
        self.mean_baseline_button.setObjectName("secondaryButton")
        self.clear_baseline_button = QPushButton("清除")
        self.clear_baseline_button.setObjectName("secondaryButton")
        self.set_baseline_button.clicked.connect(self._set_current_as_baseline)
        self.set_selected_baseline_button.clicked.connect(self._set_selected_point_as_baseline)
        self.mean_baseline_button.clicked.connect(self._set_mean_as_baseline)
        self.clear_baseline_button.clicked.connect(self._clear_baseline)
        baseline_row.addWidget(self.set_baseline_button)
        baseline_row.addWidget(self.set_selected_baseline_button)
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
        self.cursor_label = QLabel("鼠标悬停预览读数；单击曲线锁定一个基准点")
        self.cursor_label.setObjectName("secondaryText")
        baseline_layout.addWidget(self.cursor_label)
        baseline_layout.addStretch(1)
        event_tab = QWidget()
        event_tab.setObjectName("analysisEventsSection")
        event_layout = QVBoxLayout(event_tab)
        event_layout.setContentsMargins(12, 7, 12, 7)
        event_layout.setSpacing(6)
        event_title = QLabel("实验事件")
        event_title.setObjectName("panelTitle")
        event_layout.addWidget(event_title)
        event_layout.addWidget(QLabel("记录操作、环境变化或刺激切换；事件将写入实验记录并标记在曲线上。"))
        event_row = QHBoxLayout()
        self.event_edit = QLineEdit()
        self.event_edit.setObjectName("eventEdit")
        self.event_edit.setPlaceholderText("例如：接通负载、移除刺激、环境温度变化…")
        self.event_edit.returnPressed.connect(self._mark_event)
        self.event_button = QPushButton("记录实验事件")
        self.event_button.setObjectName("secondaryButton")
        self.event_button.setToolTip("需要先开始一次实验。快捷键：Ctrl+Enter")
        self.event_button.setEnabled(False)
        self.event_button.clicked.connect(self._mark_event)
        event_row.addWidget(self.event_edit, 1)
        event_row.addWidget(self.event_button)
        event_layout.addLayout(event_row)
        self.event_list_widget = QListWidget()
        self.event_list_widget.setObjectName("eventJumpList")
        self.event_list_widget.setAccessibleName("实验事件列表")
        self.event_list_widget.setToolTip("点击或按 Enter 定位到对应事件时间")
        self.event_list_widget.setMaximumHeight(132)
        self.event_list_widget.itemClicked.connect(self._focus_event_item)
        self.event_list_widget.itemActivated.connect(self._focus_event_item)
        event_layout.addWidget(self.event_list_widget, 1)
        self.export_events_safe_button = QPushButton("导出表格软件安全副本…")
        self.export_events_safe_button.setToolTip("仅处理自由文本事件列；原始 events.csv 保持不变。请停止采集后导出。")
        self.export_events_safe_button.clicked.connect(self._export_safe_events_csv)
        self.event_csv_safety_warning = QLabel(
            "安全提醒：原始 events.csv 保留事件原文。以 =、+、-、@ 开头的事件，"
            "在 Excel 等表格软件中可能被解释为公式。打开或分享前请导出表格软件安全副本；原始文件不会修改。"
        )
        self.event_csv_safety_warning.setObjectName("warningBanner")
        self.event_csv_safety_warning.setWordWrap(True)
        self.event_csv_safety_warning.setAccessibleName("事件 CSV 公式风险提示")
        event_layout.addWidget(self.event_csv_safety_warning)
        event_layout.addWidget(self.export_events_safe_button)
        event_layout.addStretch(1)

        overview_content = QWidget()
        overview_content.setObjectName("analysisOverviewContent")
        overview_layout = QVBoxLayout(overview_content)
        overview_layout.setContentsMargins(10, 10, 10, 10)
        overview_layout.setSpacing(12)
        overview_layout.addWidget(stats_tab)
        overview_layout.addWidget(baseline_tab)
        overview_layout.addWidget(event_tab)
        overview_layout.addStretch(1)
        self.analysis_overview_scroll_area = QScrollArea()
        self.analysis_overview_scroll_area.setObjectName("analysisOverviewScrollArea")
        self.analysis_overview_scroll_area.setWidgetResizable(True)
        self.analysis_overview_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.analysis_overview_scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.analysis_overview_scroll_area.setWidget(overview_content)
        self.analysis_overview_tab = self.analysis_overview_scroll_area
        self.analysis_tabs.addTab(self.analysis_overview_tab, "分析总览")

        chart_tab = QWidget()
        chart_tab.setObjectName("chartSettingsTab")
        chart_layout = QVBoxLayout(chart_tab)
        chart_layout.setContentsMargins(12, 7, 12, 7)
        chart_layout.setSpacing(8)
        chart_title = QLabel("曲线与视图")
        chart_title.setObjectName("panelTitle")
        chart_layout.addWidget(chart_title)
        chart_note = QLabel(
            "缩放、自动跟随、历史数据和截图只影响图表显示；原始测量数据保持不变。"
        )
        chart_note.setObjectName("secondaryText")
        chart_note.setWordWrap(True)
        chart_layout.addWidget(chart_note)
        chart_layout.addWidget(self.chart_gesture_help)
        chart_layout.addStretch(1)
        self.chart_settings_tab = chart_tab
        self.analysis_tabs.addTab(chart_tab, "曲线设置")
        self.analysis_tabs.installEventFilter(self)
        self.analysis_tabs.currentChanged.connect(self._on_analysis_section_changed)
        return panel

    def _on_analysis_section_changed(self, _index: int) -> None:
        self._sync_configuration_navigation()
        QTimer.singleShot(0, self._update_plot_toolbar_layout)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_readout_layout()

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "mode_bar", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, lambda: self._sync_mode_buttons(animate=False))
        if (
            watched is getattr(self, "_plot_viewport", None)
            and event.type() == QEvent.Type.MouseButtonDblClick
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._enable_plot_autoscale()
            return True
        if (
            watched is getattr(self, "files_dialog", None)
            and event.type() == QEvent.Type.Hide
            and hasattr(self, "navigation_buttons")
            and self.navigation_buttons["files"].isChecked()
        ):
            self.navigation_buttons["acquisition"].setChecked(True)
        if (
            watched is getattr(self, "configuration_dialog", None)
            and event.type() == QEvent.Type.Hide
            and hasattr(self, "navigation_buttons")
        ):
            active_navigation = next(
                (name for name, button in self.navigation_buttons.items() if button.isChecked()),
                None,
            )
            if active_navigation in {"communications", "analysis"}:
                self.navigation_buttons["acquisition"].setChecked(True)
        if (
            (
                watched is getattr(self, "configuration_dialog", None)
                or watched is getattr(self, "analysis_tabs", None)
            )
            and event.type() == QEvent.Type.Resize
        ):
            QTimer.singleShot(0, self._update_plot_toolbar_layout)
        return super().eventFilter(watched, event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._update_readout_layout)
        if not self._initial_connection_prompt_shown:
            self._initial_connection_prompt_shown = True
            QTimer.singleShot(0, self._open_initial_connection)

    def _open_initial_connection(self) -> None:
        if self.isVisible() and not self._connected and not self._connection_pending:
            self._show_connection_settings()

    def _update_readout_layout(self) -> None:
        if not hasattr(self, "_reading_layout"):
            return
        workspace_width = self.measurement_workspace.width()
        workspace_height = self.measurement_workspace.height()
        mode = "stacked" if workspace_width < 620 else "compact" if workspace_width < 820 else "wide"
        short_chart = workspace_height < 620
        # At 150% scaling a 1366×768 display leaves roughly 370 logical
        # pixels for this workspace after Windows and app chrome. Enter the
        # compact vertical layout before labels and toolbar controls are
        # compressed below their readable minimums.
        short_viewport = 0 < workspace_height < 420
        if (
            mode == self._reading_layout_mode
            and short_chart == self._short_chart_layout
            and short_viewport == self._short_viewport_layout
        ):
            return
        self._reading_layout_mode = mode
        self._short_chart_layout = short_chart
        self._short_viewport_layout = short_viewport
        narrow = mode != "wide"
        self._reading_layout.setSpacing(12 if narrow else 20)
        self._metrics_layout.setSpacing(4 if narrow else 6)
        self._status_caption.setText("状态" if narrow else "采集状态")
        self._rate_caption.setText("实测采样间隔")
        self._elapsed_caption.setText("历时" if narrow else "实验历时")
        self._reading_card.setProperty("compact", narrow)
        self._reading_card.setProperty("short", short_viewport)
        stacked = mode == "stacked"
        self._reading_card.setProperty("stacked", stacked)
        # Qt stylesheets do not always refresh descendant selectors when only
        # an ancestor's dynamic property changes. Mark the responsive labels
        # directly so their font sizes and size hints update at short heights.
        for widget in (
            self._reading_kicker,
            self.reading_mode_label,
            self.latest_value_label,
            self.valid_sample_count_label,
            self._status_caption,
            self.status_dot,
            self.experiment_status,
            self.latest_invalid_label,
            self.display_loss_warning,
            self._rate_caption,
            self.actual_rate_label,
            self._elapsed_caption,
            self.elapsed_label,
            self.recovery_hint_label,
        ):
            widget.setProperty("short", short_viewport)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.updateGeometry()
        for widget in (
            self.target_interval_label,
            self.interval_spin,
            self.fastest_interval_label,
        ):
            widget.setProperty("short", short_viewport)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.updateGeometry()
        self._reading_kicker.setVisible(not stacked and not short_viewport)
        self._reading_layout.setSpacing(6 if stacked else 12 if narrow else 20)
        if short_viewport:
            self._reading_layout.setContentsMargins(5, 2, 5, 2)
            for metric_layout in (
                self._status_metric_layout,
                self._rate_metric_layout,
                self._elapsed_metric_layout,
            ):
                metric_layout.setContentsMargins(5, 3, 5, 3)
                metric_layout.setSpacing(1)
            self._status_tile.setMinimumWidth(108)
            self._rate_tile.setMinimumWidth(86)
            self._elapsed_tile.setMinimumWidth(88)
            self.latest_invalid_label.setWordWrap(False)
            self.latest_invalid_label.setMaximumHeight(19)
            self.latest_invalid_label.setVisible(bool(self._latest_invalid_log_text))
            self.latest_invalid_log_button.setText("日志")
            self.latest_invalid_log_button.setFixedSize(36, 20)
            self.latest_invalid_log_button.hide()
            self.recovery_hint_label.setWordWrap(False)
            self.recovery_hint_label.setMaximumHeight(19)
        elif stacked:
            self._reading_layout.setContentsMargins(12, 6, 12, 6)
        else:
            self._reading_layout.setContentsMargins(18, 14, 18, 14)
        if not short_viewport:
            for metric_layout in (
                self._status_metric_layout,
                self._rate_metric_layout,
                self._elapsed_metric_layout,
            ):
                metric_layout.setContentsMargins(10, 7, 10, 7)
                metric_layout.setSpacing(3)
            self._status_tile.setMinimumWidth(132)
            self._rate_tile.setMinimumWidth(104)
            self._elapsed_tile.setMinimumWidth(112)
            self.latest_invalid_label.setWordWrap(True)
            self.latest_invalid_label.setMaximumHeight(16777215)
            self.latest_invalid_label.setVisible(bool(self._latest_invalid_log_text))
            self.latest_invalid_log_button.setText("查看异常日志")
            self.latest_invalid_log_button.setMinimumSize(0, 0)
            self.latest_invalid_log_button.setMaximumSize(16777215, 16777215)
            self.latest_invalid_log_button.hide()
            self.recovery_hint_label.setWordWrap(True)
            self.recovery_hint_label.setMaximumHeight(16777215)
        if short_viewport and self.recovery_hint_label.toolTip():
            self.recovery_hint_label.setText("采集已停止 · 悬停查看")
        elif not short_viewport and self.recovery_hint_label.toolTip():
            self.recovery_hint_label.setText(self.recovery_hint_label.toolTip())
        if self._latest_invalid_log_text:
            self._update_latest_invalid_summary()
        self._reading_text_layout.setSpacing(1 if stacked or short_viewport else 3)
        self._chart_panel_layout.setSpacing(3 if short_viewport else 5 if stacked or short_chart else 9)
        if short_viewport:
            self._measurement_action_layout.setContentsMargins(4, 1, 4, 1)
            self._measurement_action_layout.setSpacing(5)
            self.new_measurement_button.setFixedHeight(28)
            self.clear_plot_button.setFixedHeight(28)
            self.new_measurement_hint.setMaximumHeight(36)
        else:
            self._measurement_action_layout.setContentsMargins(10, 5, 10, 5)
            self._measurement_action_layout.setSpacing(8)
            self.new_measurement_button.setFixedHeight(36)
            self.clear_plot_button.setFixedHeight(36)
            self.new_measurement_hint.setMaximumHeight(16777215)
        self.new_measurement_hint.show()
        if stacked or short_chart:
            self._action_layout.setContentsMargins(10, 4, 10, 4)
        else:
            self._action_layout.setContentsMargins(10, 7, 10, 7)
        compact_chart = stacked or short_chart
        self._action_layout.setSpacing(4 if compact_chart else 8)
        self._action_bar.setProperty("short", short_viewport)
        self._action_bar.style().unpolish(self._action_bar)
        self._action_bar.style().polish(self._action_bar)
        self.plot_widget.setMinimumHeight(78 if short_viewport else 170 if compact_chart else 230)
        self._plot_frame.setMinimumHeight(82 if short_viewport else 172 if compact_chart else 232)
        self._reading_card.style().unpolish(self._reading_card)
        self._reading_card.style().polish(self._reading_card)
        self._reading_card.updateGeometry()
        self._reading_card.setMaximumHeight(98 if short_viewport else 16777215)
        self.measurement_workspace.updateGeometry()
        self._reading_layout.setDirection(
            QBoxLayout.Direction.TopToBottom if mode == "stacked" else QBoxLayout.Direction.LeftToRight
        )
        self._reading_divider.setFrameShape(
            QFrame.Shape.HLine if mode == "stacked" else QFrame.Shape.VLine
        )
        self._update_measurement_hint()

    def _update_measurement_hint(self) -> None:
        if not hasattr(self, "new_measurement_hint"):
            return
        compact_hint = bool(getattr(self, "_short_viewport_layout", False))
        if self._history_load_pending:
            text = "历史 CSV 正在加载；完成后可新建测量。"
        elif self._start_pending:
            text = "正在处理启动请求；稍候再新建测量。"
        elif self._new_measurement_after_save:
            text = "正在停止并保存当前实验；完成后自动打开新测量设置。"
        elif self._acquiring:
            text = (
                "采集中；点“新建测量”后先停止并保存，取消则继续当前采集。"
                if compact_hint else
                "当前实验仍在运行；点击“新建测量”后将先停止并保存，取消则继续当前采集。旧数据会保留。"
            )
        elif self._experiment_active:
            text = (
                "正在保存；完成后可新建测量或清空图表。"
                if compact_hint else
                "当前实验正在收尾并保存；完成后可新建测量或清空图表。原始 CSV 会保留。"
            )
        else:
            text = (
                self._new_measurement_compact_hint
                if compact_hint else self._new_measurement_idle_hint
            )
        self.new_measurement_hint.setText(text)

    def _update_latest_invalid_summary(self) -> None:
        """Keep the compact status card's error cause readable beside its log link."""
        if not self._latest_invalid_log_text:
            return
        detail = self.latest_invalid_label.toolTip()
        if not self._short_viewport_layout:
            self.latest_invalid_label.setText(detail)
            self.latest_invalid_log_button.setText("查看异常日志")
            return

        # In the short layout keep a concise, visible cause; the full status and
        # device error remain available from the tooltip and the log viewer.
        status = ""
        if "：" in detail and " · " in detail:
            status = detail.split("：", 1)[1].split(" · ", 1)[0].strip()
        status_labels = {
            "timeout": "串口超时",
            "scpi_timeout": "SCPI超时",
            "parse_error": "解析失败",
            "invalid": "无效响应",
        }
        cause = status_labels.get(status, status or "数据无效")
        sample = ""
        if "#" in detail:
            sample = detail.split("#", 1)[1].split("：", 1)[0]
        summary = f"{cause} · #{sample}" if sample else cause
        available_width = max(1, self.latest_invalid_label.contentsRect().width())
        summary = QFontMetrics(self.latest_invalid_label.font()).elidedText(
            summary, Qt.TextElideMode.ElideRight, available_width,
        )
        self.latest_invalid_label.setText(summary)
        self.latest_invalid_log_button.setText("日志")

    def _update_plot_toolbar_layout(self) -> None:
        if not hasattr(self, "_plot_toolbar_layout"):
            return
        if self._plot_toolbar_mode != "simple":
            toolbar_layout = self._plot_toolbar_layout
            while toolbar_layout.count():
                toolbar_layout.takeAt(0)
            for widget in self._plot_toolbar_widgets:
                widget.hide()
            self.y_axis_span_control.hide()
            self.follow_check.show()
            self.x_auto_check.show()
            self.y_auto_check.show()
            self.quick_autoscale_button.hide()
            toolbar_layout.addWidget(self.x_auto_check, 0, 0)
            toolbar_layout.addWidget(self.y_auto_check, 0, 1)
            toolbar_layout.addWidget(self.follow_check, 0, 2)
            toolbar_layout.setColumnStretch(3, 1)
            self._plot_toolbar_mode = "simple"

    def _on_plot_wheel_zoom(self, zoom_x: bool, zoom_y: bool) -> None:
        if zoom_y:
            self.y_auto_check.setChecked(False)
        if zoom_x:
            self.x_auto_check.setChecked(False)
            self._schedule_history_detail_reload()

    def _on_plot_drag_finished(self) -> None:
        if getattr(self.view_box, "_last_drag_y_changed", False):
            self.y_auto_check.setChecked(False)
        self._update_chart_auto_ranges()

    def _on_plot_y_zoom_limit_reached(self, span: float) -> None:
        now = time.monotonic()
        if now - self._last_plot_zoom_limit_notice_s < 2.0:
            return
        self._last_plot_zoom_limit_notice_s = now
        unit = self.y_axis_span_unit_label.text() if hasattr(self, "y_axis_span_unit_label") else ""
        self.statusBar().showMessage(
            f"纵轴已达到当前浮点精度下限，实际跨度 {span:.8g} {unit}；继续向上滚动不会继续缩小。"
            "自动缩放可恢复数据范围，原始读数不变。",
            6000,
        )

    def _on_plot_range_changed_manually(self, axes) -> None:
        if axes and any(axes):
            if axes[0]:
                self.x_auto_check.setChecked(False)
                self._schedule_history_detail_reload()
            if len(axes) > 1 and axes[1] and not self.view_box.drag_in_progress:
                self.y_auto_check.setChecked(False)

    def _on_follow_toggled(self, enabled: bool) -> None:
        if enabled and hasattr(self, "x_auto_check"):
            self.x_auto_check.setChecked(False)
        if enabled and hasattr(self, "view_box"):
            self._update_chart_auto_ranges()
        if hasattr(self, "view_mode_label"):
            self.view_mode_label.setText(
                "自动跟随最新数据" if enabled else "手动视图 · 自动跟随已关闭"
            )
        if hasattr(self, "return_live_button") and hasattr(self, "_plot_x"):
            self._set_return_live_enabled(bool(self._plot_x))

    def _on_x_auto_toggled(self, enabled: bool) -> None:
        if enabled:
            self.follow_check.setChecked(False)
        if hasattr(self, "view_box") and not self.view_box.drag_in_progress:
            self._update_chart_auto_ranges()

    def _on_y_auto_toggled(self, enabled: bool) -> None:
        if enabled and hasattr(self, "view_box"):
            self._update_chart_auto_ranges()

    def _on_plot_x_range_changed(self, *_args) -> None:
        if (hasattr(self, "y_auto_check") and self.y_auto_check.isChecked()
                and not getattr(self, "_updating_chart_auto_ranges", False)
                and not self._y_axis_fit_timer.isActive()):
            self._y_axis_fit_timer.start(60)

    def _move_plot_selection(self, step: int) -> bool:
        if not self._plot_x:
            return False
        if self._selected_plot_point is None:
            index = 0 if step >= 0 else len(self._plot_x) - 1
        else:
            index = self._nearest_plot_index(self._selected_plot_point[0])
            if index is None:
                index = 0
            else:
                index += step
        index = max(0, min(len(self._plot_x) - 1, index))
        return self._lock_plot_point_at(float(self._plot_x[index]))

    def _pan_plot(self, dx_fraction: float, dy_fraction: float) -> None:
        x_range, y_range = self.view_box.viewRange()
        x_span = max(1e-12, x_range[1] - x_range[0])
        y_span = y_range[1] - y_range[0]
        self.view_box.translateBy(
            x=dx_fraction * x_span if dx_fraction else None,
            y=dy_fraction * y_span if dy_fraction else None,
        )
        if dx_fraction:
            self.x_auto_check.setChecked(False)
            self._schedule_history_detail_reload()
        if dy_fraction:
            self.y_auto_check.setChecked(False)

    def _zoom_plot(self, factor: float, *, zoom_x: bool, zoom_y: bool) -> None:
        if zoom_y:
            self.view_box.enableAutoRange(y=False)
        x_range, y_range = self.view_box.viewRange()
        center = QPointF(
            (x_range[0] + x_range[1]) / 2,
            (y_range[0] + y_range[1]) / 2,
        )
        if zoom_x:
            self.view_box.scaleBy(x=factor, center=center)
        if zoom_y:
            reference = max(abs(center.y()), abs(y_range[0]), abs(y_range[1]))
            floor = math.ulp(reference)
            if factor < 1.0 and (y_range[1] - y_range[0]) * factor < floor * 16:
                old_origin = self._plot_y_origin or 0.0
                target_origin = old_origin + center.y()
                if math.isfinite(target_origin):
                    self._rebase_plot_y_origin(target_origin)
                    center.setY(0.0)
            self.view_box._scale_y_by(factor, center.y())
        self._on_plot_wheel_zoom(zoom_x, zoom_y)

    def _handle_plot_key(self, event) -> bool:
        key = event.key()
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        arrows = {
            Qt.Key.Key_Left: (-1.0, 0.0),
            Qt.Key.Key_Right: (1.0, 0.0),
            Qt.Key.Key_Up: (0.0, 1.0),
            Qt.Key.Key_Down: (0.0, -1.0),
        }
        if shift and key in arrows:
            dx, dy = arrows[key]
            self._pan_plot(dx * 0.1, dy * 0.1)
            return True
        if key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            factor = 0.8 if key == Qt.Key.Key_PageUp else 1.25
            self._zoom_plot(
                factor,
                zoom_x=control or (not control and not alt),
                zoom_y=alt or (not control and not alt),
            )
            return True
        if modifiers == Qt.KeyboardModifier.NoModifier:
            if key == Qt.Key.Key_Home:
                self._autoscale_plot()
                return True
            if key == Qt.Key.Key_End:
                self._return_to_live_view()
                return True
        return False

    def _build_log_dock(self) -> None:
        from PySide6.QtWidgets import QDockWidget, QPlainTextEdit

        self.log_dock = QDockWidget("SCPI通信与运行日志", self)
        self.log_dock.setObjectName("logDock")
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(6000)
        self.log_text.setPlaceholderText("连接识别、SCPI收发、通信异常和操作事件将显示在此处。")
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(6, 6, 6, 6)
        panel_layout.setSpacing(6)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("筛选"))
        self.log_filter_combo = QComboBox()
        for label, key in (
            ("全部日志", "all"), ("错误", "error"), ("SCPI命令", "scpi"),
            ("连接", "connection"), ("采集", "acquisition"),
        ):
            self.log_filter_combo.addItem(label, key)
        self.log_filter_combo.currentIndexChanged.connect(self._render_filtered_log)
        toolbar.addWidget(self.log_filter_combo)
        toolbar.addStretch(1)
        self.export_log_button = QPushButton("导出当前筛选")
        self.export_log_button.clicked.connect(self._export_filtered_logs)
        toolbar.addWidget(self.export_log_button)
        panel_layout.addLayout(toolbar)
        panel_layout.addWidget(self.log_text, 1)
        self.log_dock.setWidget(panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.resizeDocks([self.log_dock], [150], Qt.Orientation.Vertical)
        self.log_dock.hide()

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        self.experiment_files_action = QAction("实验文件…", self)
        self.experiment_files_action.setObjectName("experimentFilesAction")
        self.experiment_files_action.triggered.connect(
            lambda: self._navigate_workspace("files")
        )
        file_menu.addAction(self.experiment_files_action)
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
        capture_menu = self.menuBar().addMenu("采集")
        self._shortcut_actions: dict[str, QAction] = {}

        def add_shortcut_action(object_name: str, text: str, sequence: str, callback) -> QAction:
            action = QAction(text, self)
            action.setObjectName(object_name)
            if sequence:
                action.setShortcut(QKeySequence(sequence))
                action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            action.triggered.connect(callback)
            self.addAction(action)
            capture_menu.addAction(action)
            self._shortcut_actions[object_name] = action
            return action

        add_shortcut_action(
            "newMeasurementShortcutAction", "新建测量", "Ctrl+N",
            self._open_new_measurement_setup,
        )
        add_shortcut_action("startShortcutAction", "开始连续采集", "Ctrl+R", lambda: self._begin_simple_measurement(one_shot=False))
        add_shortcut_action("singleShortcutAction", "单次测量", "", lambda: self._begin_simple_measurement(one_shot=True))
        add_shortcut_action("pauseShortcutAction", "暂停 / 继续", "Ctrl+P", self._pause_or_resume)
        add_shortcut_action("stopShortcutAction", "停止并保存", "Ctrl+Shift+S", self._stop)
        add_shortcut_action("settingsShortcutAction", "测量参数", "Ctrl+,", self._show_measurement_settings)
        add_shortcut_action("eventShortcutAction", "记录实验事件", "Ctrl+Return", self._mark_event)
        chart_menu = self.menuBar().addMenu("曲线")
        self.autoscale_action = QAction("全部数据自动缩放", self)
        self.autoscale_action.setObjectName("autoscaleChartAction")
        self.autoscale_action.triggered.connect(self._autoscale_plot)
        chart_menu.addAction(self.autoscale_action)
        self.return_live_action = QAction("回到实时窗口", self)
        self.return_live_action.setObjectName("returnLiveChartAction")
        self.return_live_action.triggered.connect(self._return_to_live_view)
        chart_menu.addAction(self.return_live_action)
        chart_menu.addSeparator()
        self.follow_action = QAction("自动跟随", self)
        self.follow_action.setObjectName("followChartAction")
        self.follow_action.setCheckable(True)
        self.follow_action.setChecked(self.follow_check.isChecked())
        self.follow_action.toggled.connect(self.follow_check.setChecked)
        self.follow_check.toggled.connect(self.follow_action.setChecked)
        chart_menu.addAction(self.follow_action)
        self.chart_gesture_action = QAction("鼠标与键盘操作说明", self)
        self.chart_gesture_action.setObjectName("chartGestureHelpAction")
        self.chart_gesture_action.setCheckable(True)
        self.chart_gesture_action.setChecked(self.chart_gesture_help_button.isChecked())
        self.chart_gesture_action.toggled.connect(self.chart_gesture_help_button.setChecked)
        self.chart_gesture_help_button.toggled.connect(self.chart_gesture_action.setChecked)
        chart_menu.addAction(self.chart_gesture_action)
        chart_menu.addSeparator()
        self.clear_plot_action = QAction("清空图表", self)
        self.clear_plot_action.setObjectName("clearPlotAction")
        self.clear_plot_action.triggered.connect(self._clear_plot_from_ui)
        chart_menu.addAction(self.clear_plot_action)
        chart_settings_action = QAction("打开曲线设置…", self)
        chart_settings_action.setObjectName("chartSettingsAction")
        chart_settings_action.triggered.connect(lambda: self._navigate_workspace("chart"))
        chart_menu.addAction(chart_settings_action)
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
            QMainWindow { background: #edf2f1; }
            QWidget { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; }
            QMainWindow QWidget { color: #e7e8df; }
            QWidget#appRoot { background: #edf2f1; }
            QFrame#appHeader { background: #0d5055; border: 1px solid #0d5055; border-radius: 8px; }
            QPushButton#connectionActionButton { background: #efb750; border: 0; color: #1b3f40; font-weight: 700; padding: 7px 18px; }
            QPushButton#connectionActionButton:hover { background: #ffd078; }
            QPushButton#measurementSettingsButton { background: #f8fbfa; color: #124e52; border: 1px solid #c4e4df; font-weight: 700; padding: 7px 18px; }
            QPushButton#measurementSettingsButton:disabled, QPushButton#connectionActionButton:disabled { background: #a4b4b0; color: #e1e9e6; }
            QFrame#modeBar { background: #ffffff; border: 1px solid #c8d9d6; border-radius: 8px; }
            QPushButton#modeButton { background: transparent; color: #42615f; border: 0; border-radius: 5px; font-size: 14px; font-weight: 600; padding: 8px 10px; }
            QPushButton#modeButton:hover { background: #e3f3ef; color: #075e61; }
            QPushButton#modeButton:checked { background: #d7f0eb; color: #075e61; font-weight: 700; }
            QPushButton#modeButton:disabled { background: #f1f4f2; color: #a2b0ad; }
            QPushButton#modeButton:checked:disabled { background: #deebe7; color: #537a74; }
            QFrame#modeIndicator { background: #0d9997; border: 0; border-radius: 1px; }
            QLabel#brandIcon { background: #f1efe6; border: 1px solid #bdc0b7; border-radius: 6px; color: #176d72; font-weight: 800; }
            QLabel#mainTitle { color: #f1efe6; font-size: 18px; font-weight: 700; letter-spacing: .03em; }
            QMenuBar { background: #252d31; color: #e7e8df; padding: 2px 7px; }
            QMenuBar::item:selected, QMenu::item:selected { background: #414d51; color: #6de1e2; }
            QMenu { background: #30383c; color: #f1efe6; border: 1px solid #505b5f; padding: 4px; }
            QDialog#configurationDialog { background: #f1efe6; }
            QDialog#configurationDialog QWidget { color: #273136; }
            QDialog#configurationDialog QPushButton#primaryButton { color: #ffffff; }
            QDialog#configurationDialog QPushButton#primaryButton:disabled { color: #778c87; }
            QDialog#experimentFilesDialog { background: #f1efe6; }
            QDialog#experimentFilesDialog QWidget { color: #273136; }
            QDialog#wiringConfirmationDialog { background: #f1efe6; }
            QDialog#wiringConfirmationDialog QWidget { color: #273136; }
            QMessageBox QWidget { color: #273136; }
            QLabel#dialogTitle { color: #273136; font-size: 19px; font-weight: 700; }
            QFrame#settingsComparison { background: #e7eee9; border: 1px solid #b6c3bc; border-radius: 6px; }
            QLabel#settingsSummaryHeading { color: #3d4c4d; font-size: 12px; font-weight: 700; }
            QLabel#settingsActiveSummary, QLabel#settingsPendingSummary { color: #304043; font-size: 12px; }
            QWidget#setupPanel { background: #f1efe6; }
            QScrollArea { background: transparent; border: 0; }
            QTabWidget#setupTabs::pane, QTabWidget#analysisTabs::pane { background: transparent; border: 0; }
            QTabWidget#setupTabs QTabBar::tab, QTabWidget#analysisTabs QTabBar::tab {
                background: transparent; color: #596762; border: 0; border-bottom: 2px solid transparent; padding: 8px 14px; margin-right: 5px;
            }
            QTabWidget#setupTabs QTabBar::tab:selected, QTabWidget#analysisTabs QTabBar::tab:selected {
                color: #176d72; font-weight: 700; border-bottom-color: #29aab0;
            }
            QLabel#panelTitle { font-size: 16px; font-weight: 700; color: #273136; }
            QLabel#subtitle, QLabel#secondaryText { color: #596762; }
            QWidget#measurementWorkspace QLabel#secondaryText { color: #b7c2bc; }
            QLabel#latestInvalidLabel { color: #f4c579; font-size: 12px; }
            QLabel#latestInvalidLabel[short="true"] { font-size: 11px; }
            QLabel#displayLossWarning { color: #f4c579; font-size: 12px; font-weight: 700; }
            QLabel#displayLossWarning[short="true"] { font-size: 11px; }
            QLabel#voltageRangeWarning { color: #f4c579; font-size: 12px; }
            QLabel#recoveryHintLabel { color: #f4c579; font-size: 12px; }
            QLabel#recoveryHintLabel[short="true"] { font-size: 11px; }
            QLabel#warningBanner { color: #634411; background: #fff2cc; border: 1px solid #d6ad54; border-radius: 6px; padding: 8px 10px; font-weight: 650; }
            QGroupBox#sidebarCard { background: #faf9f4; border: 1px solid #cbd0c9; border-radius: 8px; margin-top: 12px; padding: 11px 8px 9px; font-weight: 650; }
            QGroupBox#sidebarCard::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #394448; background: #faf9f4; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {
                background: #fffefa; color: #273136; border: 1px solid #bfc7c4; border-radius: 5px; padding: 5px 8px; selection-background-color: #17858b; min-height: 25px;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus { border: 2px solid #075e63; }
            QPushButton:focus, QToolButton:focus, QCheckBox:focus { border: 2px solid #075e63; }
            QTabWidget QTabBar::tab:focus { border: 2px solid #075e63; border-radius: 4px; }
            QGraphicsView#trendPlot:focus { border: 2px solid #17858b; border-radius: 4px; }
            QListWidget#eventJumpList { background: #fffefa; border: 1px solid #cbd0c9; border-radius: 5px; }
            QListWidget#eventJumpList:focus { border: 2px solid #17858b; }
            QListWidget#eventJumpList::item:selected { background: #d8eeeb; color: #184c4c; }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { background: #e8e8e0; color: #919895; border-color: #d5d8d1; }
            QPushButton { background: #faf9f4; color: #334045; border: 1px solid #bfc7c4; border-radius: 5px; padding: 6px 10px; min-height: 25px; }
            QPushButton:hover { background: #e8f1ed; border-color: #7a9a96; }
            QPushButton:disabled { color: #99a09e; background: #e7e8e1; border-color: #d1d4cd; }
            QPushButton#primaryButton { background: #167b80; border-color: #167b80; color: #fffefa; font-weight: 700; }
            QPushButton#primaryButton:hover { background: #12666b; border-color: #12666b; }
            QPushButton#primaryButton:disabled { background: #bdcac6; border-color: #bdcac6; color: #778c87; }
            QPushButton#newMeasurementButton { background: #167b80; border-color: #167b80; color: #fffefa; font-weight: 700; padding-left: 12px; padding-right: 12px; }
            QPushButton#newMeasurementButton:hover { background: #12666b; border-color: #12666b; }
            QPushButton#plotEmptyNewMeasurementButton { background: #167b80; border: 1px solid #167b80; color: #fffefa; font-weight: 700; padding: 6px 12px; }
            QPushButton#plotEmptyNewMeasurementButton:hover { background: #12666b; border-color: #12666b; }
            QPushButton#clearPlotButton { color: #344247; }
            QFrame#measurementActionBar { background: #30383c; border: 1px solid #465156; border-radius: 7px; }
            QFrame#measurementActionBar QPushButton { min-height: 34px; }
            QFrame#measurementActionBar QLabel#secondaryText { color: #e1e8e1; font-size: 12px; font-weight: 600; }
            QPushButton#newMeasurementButton { background: #167b80; border-color: #167b80; color: #fffefa; font-weight: 700; padding-left: 12px; padding-right: 12px; }
            QPushButton#newMeasurementButton:hover { background: #12666b; border-color: #12666b; }
            QPushButton#clearPlotButton { color: #344247; }
            QPushButton#primaryButton[connectionTarget="mock"] { background: #8a5d13; border-color: #8a5d13; color: #fff8e6; }
            QPushButton#primaryButton[connectionTarget="mock"]:hover { background: #704b10; border-color: #704b10; }
            QPushButton#primaryButton[connectionTarget="mock"]:disabled { background: #5c4a2e; border-color: #6a5a3f; color: #c1b79d; }
            QPushButton#dangerButton { color: #982f2b; border-color: #d6aaa3; background: #fff8f4; }
            QPushButton#dangerButton:disabled { color: #a8b0ad; border-color: #d8dedb; background: #edf1ef; }
            QPushButton#secondaryButton { color: #344247; }
            QToolButton[class="secondaryButton"] { background: #faf9f4; color: #344247; border: 1px solid #bfc7c4; border-radius: 5px; padding: 6px 12px; min-height: 25px; }
            QToolButton[class="secondaryButton"]:checked { background: #d8eeeb; color: #176d72; border-color: #29aab0; font-weight: 700; }
            QToolBar#workspaceNavigationToolbar { background: #30383c; border: 1px solid #465156; border-radius: 7px; spacing: 5px; padding: 4px 8px; margin: 0px 18px 8px 18px; }
            QToolBar#workspaceNavigationToolbar::separator { background: #596367; width: 1px; margin: 4px 5px; }
            QToolBar#workspaceNavigationToolbar QToolButton { background: transparent; color: #d6ded8; border: 1px solid transparent; border-radius: 5px; padding: 4px 11px; min-height: 28px; font-size: 13px; }
            QToolBar#workspaceNavigationToolbar QToolButton:hover { background: #414d51; color: #73e2e2; border-color: #59676a; }
            QToolBar#workspaceNavigationToolbar QToolButton:checked { background: #1d262a; color: #73e2e2; border-color: #42aeb1; font-weight: 700; }
            QPushButton#configurationNavigation { background: #167b80; color: #fffefa; border: 1px solid #167b80; border-radius: 5px; padding: 2px 12px; min-height: 28px; font-size: 13px; font-weight: 700; }
            QPushButton#configurationNavigation:hover { background: #12666b; border-color: #42aeb1; }
            QToolButton#disclosureButton { color: #536166; border: 0; text-align: left; padding: 4px 2px; }
            QCheckBox { spacing: 7px; color: #3e4a4f; }
            QLabel#connectionBadge { padding: 6px 10px; background: #424c50; border: 1px solid #5b6669; border-radius: 6px; color: #d7ded9; font-size: 12px; font-weight: 700; }
            QLabel#connectionBadge[state="connected"] { color: #a9efce; background: #29433c; border-color: #47705f; }
            QLabel#connectionBadge[state="mock"] { color: #f4c579; background: #51412a; border-color: #8c6d3c; }
            QLabel#mockScenarioBanner { color: #f6d79a; background: #51412a; border: 1px solid #8c6d3c; border-radius: 6px; padding: 5px 8px; font-weight: 650; }
            QLabel#instrumentReadbackBadge { color: #d4ded9; background: #384347; border: 1px solid #58666a; border-radius: 6px; padding: 5px 8px; font-weight: 650; }
            QLabel#instrumentReadbackBadge[state="readback"] { color: #b9f0d1; background: #29433c; border-color: #47705f; }
            QLabel#instrumentReadbackBadge[state="local"] { color: #f6d79a; background: #51412a; border-color: #8c6d3c; }
            QLabel#instrumentReadbackBadge[state="mock"] { color: #f6d79a; background: #51412a; border-color: #8c6d3c; }
            QFrame#readingCard { background: #1d262a; border: 1px solid #4b585a; border-radius: 8px; }
            QFrame#readingCard QLabel#eyebrow { color: #a5b2ad; font-size: 12px; font-weight: 600; }
            QLabel#readingMode { color: #c7d2cc; font-size: 12px; font-weight: 600; }
            QLabel#readingValue { color: #73e2e2; font-family: "Cascadia Mono", Consolas, monospace; font-size: 42px; font-weight: 650; }
            QFrame#readingCard[compact="true"] QLabel#readingValue { font-size: 32px; }
            QFrame#readingCard[stacked="true"] QLabel#readingValue { font-size: 27px; }
            QFrame#readingCard[short="true"] QLabel#eyebrow { font-size: 11px; }
            QFrame#readingCard[short="true"] QLabel#readingMode { font-size: 11px; }
            QFrame#readingCard[short="true"] QLabel#readingValue { font-size: 17px; }
            QFrame#readingCard[short="true"] QLabel#metricValue,
            QFrame#readingCard[short="true"] QLabel#statusValue { font-size: 11px; }
            QLabel#eyebrow[short="true"] { font-size: 11px; }
            QLabel#readingMode[short="true"] { font-size: 11px; }
            QLabel#readingValue[short="true"] { font-size: 17px; }
            QLabel#metricValue[short="true"],
            QLabel#statusValue[short="true"] { font-size: 11px; }
            QFrame#metricCell { background: #303b3e; border: 1px solid #465458; border-radius: 6px; }
            QFrame#readingCard QFrame#metricCell QLabel#eyebrow { color: #a8b5ae; font-size: 12px; }
            QFrame#readingCard QLabel#metricValue, QFrame#readingCard QLabel#statusValue { color: #f0f0e7; font-size: 13px; font-weight: 700; }
            QFrame#readingCard[compact="true"] QLabel#metricValue, QFrame#readingCard[compact="true"] QLabel#statusValue { font-size: 12px; }
            QFrame#readingCard QLabel#statusDot[state="running"] { color: #69d8a4; }
            QFrame#readingCard QLabel#statusDot[state="paused"] { color: #e6a23a; }
            QFrame#readingCard QLabel#statusDot[state="idle"] { color: #8e9b98; }
            QFrame#cardDivider { color: #46565a; }
            QFrame#toolbarDivider { color: #596367; }
            QFrame#actionBar { background: #30383c; border: 1px solid #465156; border-radius: 7px; }
            QFrame#actionBar QPushButton { min-height: 30px; }
            QFrame#actionBar[short="true"] QPushButton { padding: 1px 6px; min-height: 22px; }
            QFrame#plotToolbar { background: #252d31; border: 1px solid #465156; border-radius: 8px; padding: 4px; }
            QDialog#configurationDialog QFrame#plotToolbar QLabel#secondaryText { color: #d6ded8; }
            QDialog#configurationDialog QFrame#plotToolbar QLabel#chartViewMode { color: #d6ded8; font-size: 12px; }
            QDialog#configurationDialog QFrame#plotToolbar QLabel#chartAcquisitionHint { color: #d6ded8; font-size: 12px; }
            QDialog#configurationDialog QFrame#plotToolbar QLabel#chartSourceLabel { color: #d6ded8; font-size: 12px; }
            QDialog#configurationDialog QFrame#plotToolbar QCheckBox#chartFollowCheck { color: #d6ded8; font-size: 12px; }
            QLabel#targetIntervalLabel { color: #344247; font-size: 12px; }
            QLabel#fastestIntervalLabel { color: #344247; font-size: 12px; }
            QFrame#actionBar QLabel#targetIntervalLabel, QFrame#actionBar QLabel#fastestIntervalLabel { color: #d6ded8; }
            QLabel#targetIntervalLabel[short="true"], QLabel#fastestIntervalLabel[short="true"] { font-size: 11px; }
            QLabel#chartViewMode { color: #d6ded8; font-size: 12px; }
            QLabel#chartAcquisitionHint { color: #d6ded8; font-size: 12px; }
            QCheckBox#chartFollowCheck { color: #d6ded8; font-size: 12px; }
            QLabel#chartSourceLabel { color: #d6ded8; font-size: 12px; }
            QFrame#plotToolbar[narrow="true"] QPushButton { padding: 3px 6px; min-height: 22px; }
            QFrame#plotToolbar[short="true"] QPushButton { padding: 1px 5px; min-height: 20px; }
            QFrame#plotToolbar[short="true"] QLabel#chartViewMode,
            QFrame#plotToolbar[short="true"] QLabel#chartSourceLabel { font-size: 11px; }
            QPushButton[short="true"] { padding: 1px 5px; min-height: 18px; }
            QLineEdit[short="true"], QComboBox[short="true"],
            QSpinBox[short="true"], QDoubleSpinBox[short="true"] {
                padding: 1px 4px; min-height: 18px;
            }
            QLabel#chartViewMode[short="true"],
            QLabel#chartSourceLabel[short="true"] { font-size: 11px; }
            QFrame#plotCard { background: #f1efe6; border: 1px solid #8d9690; border-radius: 8px; }
            QWidget#plotEmptyOverlay { background: transparent; }
            QLabel#plotEmptyTitle { color: #465457; font-size: 18px; font-weight: 700; }
            QLabel#plotEmptyHint { color: #53615b; font-size: 12px; }
            QLabel#toolbarLabel { color: #d6ded8; font-weight: 700; }
            QLabel#toolbarHint { color: #b7c2bc; font-size: 12px; }
            QLabel#safetyBox { background: #fff3d7; color: #75551e; border: 1px solid #e5c27d; border-radius: 6px; padding: 9px; }
            QFrame#terminalDiagram { background: #d8d4c8; border: 1px solid #777b77; border-radius: 6px; }
            QLabel#terminalDiagramTitle { color: #343b3a; font-weight: 700; }
            QLabel#terminalGroupHeading { color: #525a57; font-size: 11px; font-weight: 700; }
            QFrame#terminalSocket { background: #eeece3; border: 1px solid #a0a198; border-radius: 5px; }
            QLabel#terminalSocketLabel { color: #333b39; background: transparent; font-weight: 700; }
            QFrame#terminalSocketMarker { background: #30383c; border: 2px solid #c04a53; border-radius: 7px; }
            QFrame#terminalSocketMarker[socketTone="low"] { border-color: #565e5f; }
            QFrame#terminalSocketMarker[socketTone="current_high"] { border-color: #a87930; }
            QLabel#terminalDiagramNote { color: #4d5652; font-size: 11px; }
            QLabel#changeLabel { color: #966219; font-weight: 700; }
            QStatusBar { background: #252d31; color: #c1cbc5; border-top: 1px solid #414c50; }
            QLabel#operationStatus { color: #c1cbc5; padding: 2px 8px; font-size: 12px; }
            QLabel#operationStatus[state="busy"] { color: #f6d79a; font-weight: 650; }
            QLabel#operationStatus[state="success"] { color: #a9efce; font-weight: 650; }
            QLabel#operationStatus[state="error"] { color: #f4c579; font-weight: 650; }
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
                loaded = json.loads(self._profile_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.exception("无法读取配置文件")
            else:
                if isinstance(loaded, dict):
                    profiles = loaded
                else:
                    log.warning("配置文件顶层不是对象；使用默认设置")
        for key in ("serial", "serial_by_port", "instrument_firmware_by_serial"):
            if key not in profiles:
                profiles[key] = {}
            elif not isinstance(profiles[key], dict):
                log.warning("配置文件中的 %s 不是对象；使用默认设置", key)
                profiles[key] = {}
        saved_output_root = profiles.get("output_root", "")
        profiles["output_root"] = saved_output_root if isinstance(saved_output_root, str) else ""
        firmware_profiles = profiles["instrument_firmware_by_serial"]
        profiles["instrument_firmware_by_serial"] = {
            str(serial): {
                key: value.strip()[:128]
                for key, value in versions.items()
                if key in {"outguard_sw", "inguard_sw", "source", "updated_at"} and isinstance(value, str)
            }
            for serial, versions in firmware_profiles.items()
            if isinstance(versions, dict)
        }
        for mode, spec in DESCRIPTORS.items():
            default = self._default_mode_profile(mode)
            saved = profiles.get(mode.value, {})
            if not isinstance(saved, dict):
                log.warning("%s 配置不是对象；恢复该模式默认设置", mode.value)
                saved = {}
            for key, fallback in default.items():
                value = saved.get(key, fallback)
                if key in {"autorange", "analog_filter", "digital_filter", "input_impedance_auto", "autozero"}:
                    default[key] = (
                        value if isinstance(value, bool) and (key != "input_impedance_auto" or spec.input_impedance_auto)
                        else fallback
                    )
                elif key == "baseline":
                    if value is None:
                        default[key] = None
                    elif isinstance(value, (int, float)) and not isinstance(value, bool):
                        parsed = _finite_number(value)
                        default[key] = parsed if parsed is not None else fallback
                    else:
                        default[key] = fallback
                elif key in {"range_si", "nplc", "ac_bandwidth_hz"}:
                    allowed = {
                        "range_si": spec.ranges_si,
                        "nplc": spec.nplc_values,
                        "ac_bandwidth_hz": spec.ac_bandwidths_hz,
                    }[key]
                    parsed = (
                        _finite_number(value)
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                        else None
                    )
                    default[key] = (
                        None if value is None and fallback is None else
                        parsed if parsed is not None and value in allowed else fallback
                    )
            profiles[mode.value] = default
        return profiles

    @staticmethod
    def _default_mode_profile(mode: Mode) -> dict:
        spec = descriptor(mode)
        return {
            "autorange": True,
            "range_si": spec.ranges_si[0],
            "nplc": 1 if spec.nplc_values else None,
            "ac_bandwidth_hz": 20 if spec.ac_bandwidths_hz else None,
            "analog_filter": False,
            "digital_filter": False,
            "input_impedance_auto": False,
            "autozero": True,
            "baseline": None,
        }

    def _save_profiles(self) -> None:
        serial_settings = self._serial_settings_from_controls()
        serial_by_port = self._profiles.get("serial_by_port", {})
        if not isinstance(serial_by_port, dict):
            serial_by_port = {}
        selected_port = self._selected_serial_profile_port()
        if selected_port:
            serial_by_port[selected_port] = serial_settings
        payload = {
            # Keep the old single-profile field for compatibility with earlier releases.
            "serial": serial_settings,
            "serial_by_port": serial_by_port,
            "sampling_interval_s": self.interval_spin.value(),
            "output_root": (
                self.output_root.text() if hasattr(self, "output_root")
                else self._profiles.get("output_root", "")
            ),
            "instrument_firmware_by_serial": self._firmware_profiles,
            **{mode: data for mode, data in self._profiles.items() if mode in {m.value for m in Mode}},
        }
        try:
            temp = self._profile_path.with_suffix(".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self._profile_path)
        except OSError as exc:
            self._append_log(f"! 保存本机参数失败：{exc}")

    def _serial_settings_from_controls(self) -> dict:
        return {
            "baudrate": self.baud_combo.currentText(),
            "bytesize": self.data_bits_combo.currentText(),
            "parity": self.parity_combo.currentData(),
            "stopbits": self.stop_bits_combo.currentText(),
            "flow_control": self.flow_combo.currentData(),
            "eol": self.eol_combo.currentData(),
            "timeout_s": self.timeout_spin.value(),
        }

    def _selected_serial_profile_port(self) -> str:
        text = str(self.port_combo.currentText()).strip()
        index = self.port_combo.currentIndex()
        if index >= 0 and text == self.port_combo.itemText(index).strip():
            item_data = self.port_combo.itemData(index)
            if item_data:
                text = str(item_data).strip()
        return text.split(" ", 1)[0].upper() if text else ""

    def _restore_port_serial_settings(self, port: str) -> None:
        if not port:
            return
        profiles = self._profiles.get("serial_by_port", {})
        settings = profiles.get(port) if isinstance(profiles, dict) else None
        if not isinstance(settings, dict):
            # Migrate the former single serial profile only when no per-port
            # profiles exist. A new COM port must start from defaults, never
            # inherit another port's settings through the compatibility field.
            settings = self._profiles.get("serial", {}) if not profiles else {}
        self._apply_serial_settings_to_controls(settings)

    def _apply_serial_settings_to_controls(self, settings: dict) -> None:
        self._loading_serial_profile = True
        widgets = (self.baud_combo, self.data_bits_combo, self.parity_combo,
                   self.stop_bits_combo, self.flow_combo, self.eol_combo, self.timeout_spin)
        blocked = [(widget, widget.blockSignals(True)) for widget in widgets]
        try:
            self._set_combo_text(self.baud_combo, str(settings.get("baudrate", 9600)))
            self._set_combo_text(self.data_bits_combo, str(settings.get("bytesize", 8)))
            self._set_combo_data(self.parity_combo, settings.get("parity", "N"))
            self._set_combo_text(self.stop_bits_combo, str(settings.get("stopbits", 1)))
            self._set_combo_data(self.flow_combo, settings.get("flow_control", "none"))
            self._set_combo_data(self.eol_combo, settings.get("eol", "CRLF"))
            timeout_s = _finite_number(settings.get("timeout_s", 2.0))
            self.timeout_spin.setValue(timeout_s if timeout_s is not None else 2.0)
        finally:
            for widget, was_blocked in blocked:
                widget.blockSignals(was_blocked)
            self._loading_serial_profile = False

    def _restore_recommended_serial_settings(self) -> None:
        port = self._selected_serial_profile_port()
        recommended = SerialSettings()
        self._apply_serial_settings_to_controls({
            "baudrate": recommended.baudrate,
            "bytesize": recommended.bytesize,
            "parity": recommended.parity,
            "stopbits": recommended.stopbits,
            "flow_control": recommended.flow_control.value,
            "eol": recommended.eol,
            "timeout_s": recommended.timeout_s,
        })
        if port:
            profiles = self._profiles.setdefault("serial_by_port", {})
            if isinstance(profiles, dict):
                profiles[port] = self._serial_settings_from_controls()
        self._save_profiles()
        self.statusBar().showMessage(
            f"已将 {port or '当前'} 端口恢复为推荐串口设置；仪器连接未更改。", 8000
        )

    def _on_serial_port_changed(self, _text: str) -> None:
        if self._loading_serial_profile:
            return
        new_port = self._selected_serial_profile_port()
        if new_port == self._serial_profile_port:
            return
        if self._serial_profile_port:
            profiles = self._profiles.setdefault("serial_by_port", {})
            if isinstance(profiles, dict):
                profiles[self._serial_profile_port] = self._serial_settings_from_controls()
        self._serial_profile_port = new_port
        self._restore_port_serial_settings(new_port)
        self._save_profiles()

    def _restore_serial_settings(self) -> None:
        serial = self._profiles.get("serial", {})
        self._set_combo_text(self.baud_combo, str(serial.get("baudrate", 9600)))
        self._set_combo_text(self.data_bits_combo, str(serial.get("bytesize", 8)))
        self._set_combo_data(self.parity_combo, serial.get("parity", "N"))
        self._set_combo_text(self.stop_bits_combo, str(serial.get("stopbits", 1)))
        self._set_combo_data(self.flow_combo, serial.get("flow_control", "none"))
        self._set_combo_data(self.eol_combo, serial.get("eol", "CRLF"))
        timeout_s = _finite_number(serial.get("timeout_s", 2.0))
        if timeout_s is None:
            timeout_s = 2.0
        self.timeout_spin.setValue(timeout_s)
        sampling_interval = _finite_number(self._profiles.get("sampling_interval_s", 1.0))
        if sampling_interval is None or not 0 <= sampling_interval <= 86400:
            sampling_interval = 1.0
        self.interval_spin.setValue(sampling_interval)
        for widget in (self.baud_combo, self.data_bits_combo, self.parity_combo, self.stop_bits_combo, self.flow_combo, self.eol_combo):
            widget.currentIndexChanged.connect(self._save_serial_profile_if_ready)
        self.timeout_spin.valueChanged.connect(self._save_serial_profile_if_ready)

    def _save_serial_profile_if_ready(self, *_args) -> None:
        if not self._loading_serial_profile and hasattr(self, "_profile_path"):
            self._save_profiles()

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
                description = str(item.description or "串口设备")
                self.port_combo.addItem(f"{item.device} · {description}", item.device)
                self.port_combo.setItemData(row, description, Qt.ItemDataRole.ToolTipRole)
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
        if self._connection_pending:
            return
        self._persist_current_settings()
        try:
            serial_settings = self._serial_settings()
            settings = self._settings_for_mode(self._current_mode)
        except Exception as exc:
            self._show_error(str(exc))
            return
        self._active_port = str(port)
        self._connection_pending = True
        self.connect_button.setEnabled(False)
        self.connect_button.setToolTip(f"正在打开 {port} 并识别仪器；请勿重复点击。")
        self.connection_badge.setText(f"正在连接 {port}…")
        self.connection_badge.setProperty("state", "connecting")
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self._bridge.connect_real.emit(
            port, serial_settings, settings, False, self._firmware_profiles
        )

    def _disconnect(self) -> None:
        if self._acquiring:
            self._show_error("请先停止采集，再断开仪器")
            return
        self._bridge.shutdown.emit()
        # The worker remains available to reconnect after closing the device.

    def _on_connected(self, connected: bool, text: str) -> None:
        self._reset_preflight_status()
        self._connection_pending = False
        self._connected = connected
        self._simulation = "模拟" in text or "MOCK" in text
        if connected:
            self._has_connected_before = True
            self._connected_mock_scenario = self.scenario_combo.currentText() if self._simulation else ""
            if not self._simulation and not self._active_port:
                current = self.port_combo.currentData() or self.port_combo.currentText().split(" ", 1)[0]
                self._active_port = str(current)
        if not connected:
            self._cancel_wiring_confirmation()
            self._configure_pending = False
            self._zero_pending = False
            self._instrument_mode_readback = None
            self._connected_mock_scenario = ""
            self._control_mode = "disconnected"
            self._control_mode_switch_pending = False
            self._active_settings_by_mode.clear()
        self._set_combo_data(self.control_mode_combo, self._control_mode)
        if self._simulation:
            badge_text = "MOCK 模拟模式"
        elif connected:
            port_text = f"{self._active_port} · " if self._active_port else ""
            suffix = "LOCAL 请求 · 待面板确认" if self._control_mode == "local" else "COMPUTER · REMOTE"
            badge_text = f"已连接 · {port_text}8845A / {suffix}"
        else:
            badge_text = text if text and text != "未读取" else "未连接"
        self.connection_badge.setText(badge_text)
        self.plot_empty_hint.setText("开始测量后显示曲线" if connected else "连接仪器后开始测量")
        tooltip = text
        if connected and not self._simulation:
            tooltip = self._control_mode_tooltip()
        self.connection_badge.setToolTip(tooltip)
        self.connection_badge.setProperty(
            "state", "mock" if self._simulation else "connected" if connected else "disconnected"
        )
        self.connection_badge.style().unpolish(self.connection_badge)
        self.connection_badge.style().polish(self.connection_badge)
        self.idn_label.setText(f"设备身份：{text if connected else '未读取'}")
        if not connected and self._has_connected_before:
            self.connect_button.setText("重新连接")
            self.connect_button.setToolTip(
                f"使用上次的串口参数重新连接 {self._active_port or '所选端口'}。"
            )
        else:
            self._update_connect_label(self.mock_mode_check.isChecked())
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.mock_mode_check.setEnabled(not connected)
        if connected:
            self._load_mode_controls(self._current_mode)
            self._update_mock_scenario_status()
        else:
            self._firmware = ""
            self._idn_version_candidate = ""
            self._device_serial = ""
            self._set_firmware_fields("", "")
            self._terminals = ""
            self._instrument_mode_known = False
        self._update_readout_context()
        self._update_instrument_readback_status()
        self._update_controls()
        if connected and self.configuration_dialog.isVisible():
            self.configuration_dialog.hide()

    @Slot(str)
    def _on_connection_progress(self, message: str) -> None:
        self.statusBar().showMessage(message)
        self.connection_badge.setToolTip(message)
        if not self._connection_pending:
            return
        if message.startswith("连接失败："):
            self.connection_badge.setText("连接失败")
        elif message.startswith("正在启动模拟器"):
            self.connection_badge.setText("正在启动模拟器…")
        elif message.startswith("模拟器"):
            self.connection_badge.setText("模拟配置准备中…")
        elif message.startswith("正在打开"):
            self.connection_badge.setText(f"正在连接 · {self._active_port or '串口'}")
        elif message.startswith("已识别"):
            self.connection_badge.setText("身份已识别 · 读取配置…")
        elif "已就绪" in message:
            self.connection_badge.setText("连接阶段完成")

    @Slot(str, str, str)
    def _on_connection_stage(self, stage: str, status: str, message: str) -> None:
        if stage == "device" and status == "running":
            self._connection_stage_status.clear()
        self._connection_stage_status[stage] = (status, message)
        names = {
            "device": "设备", "port": "串口", "identify": "8845A",
            "configuration": "配置", "ready": "就绪",
        }
        state_names = {
            "running": "进行中", "success": "成功", "failure": "失败", "skipped": "模拟跳过",
        }
        order = ("device", "port", "identify", "configuration", "ready")
        rendered = []
        for key in order:
            current_status = self._connection_stage_status.get(key, ("pending", ""))[0]
            rendered.append(f"{names[key]} {state_names.get(current_status, '待开始')}")
        self.connection_stages_label.setText(" · ".join(rendered))
        self.connection_stages_label.setToolTip("\n".join(
            f"{names[key]}：{self._connection_stage_status[key][1]}"
            for key in order if key in self._connection_stage_status
        ))
        if self._connection_pending:
            self.statusBar().showMessage(message)
            badges = {
                ("device", "running"): "已选择设备 · 正在连接",
                ("port", "running"): f"正在打开串口 · {self._active_port or '设备'}",
                ("port", "success"): "串口已打开 · 正在识别",
                ("identify", "running"): "正在识别 Fluke 8845A",
                ("identify", "success"): "已识别 8845A · 正在读取配置",
                ("configuration", "running"): "正在读取仪器配置",
                ("configuration", "success"): "配置已读取 · 正在就绪",
                ("ready", "running"): "正在完成连接",
                ("ready", "success"): "连接阶段完成",
            }
            if status == "failure":
                self.connection_badge.setText(f"{names.get(stage, '连接')}失败")
            elif (stage, status) in badges:
                self.connection_badge.setText(badges[(stage, status)])

    def _control_mode_tooltip(self) -> str:
        if self._control_mode == "local":
            return (
                "LOCAL 请求：软件已发送 SYST:LOC，但仪表没有 SCPI 回读命令确认该状态；请核对仪表面板。"
                "软件不发送设置、诊断、ZERO或测量命令，串口连接保留。切回Computer后软件会重新读取当前仪表配置。"
            )
        return (
            "COMPUTER：软件通过SCPI控制仪表，除LOCAL键外，面板按键不可用。"
            "仪表的RS-232接口协议仍须单独设为COMPUTER；采集中请勿按LOCAL。"
        )

    def _update_instrument_readback_status(self) -> None:
        if not hasattr(self, "instrument_readback_status"):
            return
        if not self._connected:
            text, detail, state = "未连接", "连接仪器后显示配置回读状态。", "disconnected"
        elif self._configure_pending:
            text, detail, state = "配置中", "正在等待仪器配置与模式回读。", "pending"
        elif not self._instrument_mode_known:
            text, detail, state = "模式待确认", "当前仪器模式尚未确认；请重新应用测量参数并等待回读。", "pending"
        elif self._simulation:
            text, detail, state = "MOCK设置", "当前为模拟仪器配置，不代表真实仪器回读。", "mock"
        elif self._control_mode == "local":
            text = "Local请求 · 无法回读"
            detail = "软件已发送 SYST:LOC；仪表不提供状态回读（没有对应的 SCPI 查询），请核对面板。切回Computer后会重新读取当前配置。"
            state = "local"
        elif self._current_mode.value in self._active_settings_by_mode:
            text = "配置已回读"
            detail = f"当前功能 {descriptor(self._current_mode).label} 的仪器设置已回读。"
            state = "readback"
        else:
            text = "等待配置回读"
            detail = f"已识别 {descriptor(self._current_mode).label}；正在等待完整仪器设置回读。"
            state = "pending"
        label = self.instrument_readback_status
        label.setText(text)
        label.setToolTip(detail)
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)
        label.setVisible(bool(self._connected) and state in {"pending", "local"})

    @Slot(str)
    def _on_control_mode_changed(self, mode: str) -> None:
        self._control_mode = mode if mode in {"computer", "local", "disconnected"} else "disconnected"
        self._set_combo_data(self.control_mode_combo, self._control_mode)
        if self._connected and not self._simulation:
            suffix = "LOCAL 请求 · 待面板确认" if self._control_mode == "local" else "COMPUTER · REMOTE"
            port_text = f"{self._active_port} · " if self._active_port else ""
            self.connection_badge.setText(f"已连接 · {port_text}8845A / {suffix}")
            self.connection_badge.setToolTip(self._control_mode_tooltip())
        self._update_instrument_readback_status()
        self._update_controls()

    @Slot(bool, str)
    def _on_control_mode_result(self, success: bool, message: str) -> None:
        self._control_mode_switch_pending = False
        self._set_combo_data(self.control_mode_combo, self._control_mode)
        self.statusBar().showMessage(message, 12000 if success else 20000)
        self._update_controls()

    def _request_control_mode_change(self) -> None:
        if not self._connected or self._simulation:
            self.statusBar().showMessage("请先连接真实的 Fluke 8845A。", 8000)
            return
        if self._acquiring:
            self.statusBar().showMessage("请先停止采集，再切换控制方式。", 8000)
            self._set_combo_data(self.control_mode_combo, self._control_mode)
            return
        requested = str(self.control_mode_combo.currentData() or "")
        if requested not in {"computer", "local"}:
            self._set_combo_data(self.control_mode_combo, self._control_mode)
            return
        if requested == self._control_mode:
            return
        self._control_mode_switch_pending = True
        self._update_controls()
        self._bridge.set_control_mode.emit(requested)

    @Slot(object)
    def _on_configuration(self, details: dict) -> None:
        configuration_was_pending = self._configure_pending
        self._configure_pending = False
        self._simulation = bool(details.get("simulation", self._simulation))
        self._terminals = str(details.get("input_terminals") or "")
        idn = str(details.get("idn") or "")
        try:
            info = parse_idn(idn)
            self._idn_version_candidate = info.idn_version_candidate
            self._device_serial = info.serial_number
        except (csv.Error, IndexError, InstrumentIdentityError, StopIteration, ValueError):
            self._idn_version_candidate = ""
            self._device_serial = ""
        if self._simulation:
            self._set_firmware_fields("", "")
            self._firmware = ""
            self.firmware_profile_status.setText("MOCK不使用人工固件记录")
        else:
            saved_versions = self._firmware_profiles.get(self._device_serial, {})
            if not isinstance(saved_versions, dict):
                saved_versions = {}
            self._set_firmware_fields(
                str(saved_versions.get("outguard_sw", "")),
                str(saved_versions.get("inguard_sw", "")),
            )
            self._update_firmware_profile_status(saved_versions)
            self._firmware = select_range_firmware(
                self.outguard_sw_edit.text().strip()
            )[0]
            # Only manual front-panel OutG SW participates in the range gate.
            self._bridge.set_firmware_versions.emit(
                self.outguard_sw_edit.text().strip(), self.inguard_sw_edit.text().strip()
            )
        function = str(details.get("function") or "")
        detected_mode = mode_from_scpi_function(function)
        self._instrument_mode_readback = detected_mode
        self._instrument_mode_known = detected_mode is not None
        mode_changed = detected_mode is not None and detected_mode != self._current_mode
        if detected_mode is None:
            self._active_settings_by_mode.clear()
        if mode_changed:
            self._active_settings_by_mode.clear()
            self._current_mode = detected_mode
            self._baseline = self._profiles[detected_mode.value].get("baseline")
            old_block = self.mode_combo.blockSignals(True)
            self._set_combo_data(self.mode_combo, detected_mode.value)
            self.mode_combo.blockSignals(old_block)
        if self._connected:
            self._load_mode_controls(self._current_mode)
            self._update_controls()
        if configuration_was_pending:
            self._set_operation_status(
                "配置已回读", state="success",
                detail="仪器配置命令完成，已收到当前功能与设置回读。",
            )
        self._update_instrument_readback_status()
        if mode_changed and self._region.isVisible():
            self._update_region_stats()

    def _set_firmware_fields(self, outguard_version: str, inguard_version: str) -> None:
        outguard_blocked = self.outguard_sw_edit.blockSignals(True)
        inguard_blocked = self.inguard_sw_edit.blockSignals(True)
        try:
            self.outguard_sw_edit.setText(outguard_version)
            self.inguard_sw_edit.setText(inguard_version)
        finally:
            self.outguard_sw_edit.blockSignals(outguard_blocked)
            self.inguard_sw_edit.blockSignals(inguard_blocked)
        if hasattr(self, "firmware_bottom_label"):
            self.firmware_bottom_label.setText(
                f"OutG SW：{outguard_version or '未核对'}  ·  InG SW：{inguard_version or '未核对'}"
            )

    def _firmware_version_changed(self) -> None:
        outguard_version = self.outguard_sw_edit.text().strip()
        inguard_version = self.inguard_sw_edit.text().strip()
        if not self._simulation and self._device_serial:
            if outguard_version or inguard_version:
                self._firmware_profiles[self._device_serial] = {
                    "outguard_sw": outguard_version,
                    "inguard_sw": inguard_version,
                    "source": "前面板人工核对",
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            else:
                self._firmware_profiles.pop(self._device_serial, None)
            self._update_firmware_profile_status(self._firmware_profiles.get(self._device_serial, {}))
        self._firmware = select_range_firmware(outguard_version)[0]
        if self._connected and not self._simulation:
            self._bridge.set_firmware_versions.emit(outguard_version, inguard_version)
            self._save_profiles()
            if not self._acquiring:
                self._load_mode_controls(self._current_mode)

    def _update_firmware_profile_status(self, record: dict | None) -> None:
        record = record if isinstance(record, dict) else {}
        if not record.get("outguard_sw") and not record.get("inguard_sw"):
            self.firmware_profile_status.setText("尚无面板版本记录")
            return
        source = record.get("source") or "旧版本机记录（来源未标注）"
        updated_at = record.get("updated_at") or "更新时间未记录"
        self.firmware_profile_status.setText(
            f"序列号 {self._device_serial or '未知'} · 来源：{source} · 更新时间：{updated_at}"
        )

    def _clear_firmware_profile(self) -> None:
        if not self._device_serial:
            self._show_error("当前没有已识别的仪器序列号，无法定位要清除的版本记录。")
            return
        if self._device_serial not in self._firmware_profiles:
            self._set_firmware_fields("", "")
            self._update_firmware_profile_status({})
            return
        answer = QMessageBox.question(
            self, "清除人工版本记录",
            f"清除序列号 {self._device_serial} 的 OutG/InG 人工记录？仪器 *IDN? 身份信息不会改变。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._firmware_profiles.pop(self._device_serial, None)
        self._set_firmware_fields("", "")
        self._firmware = ""
        self._update_firmware_profile_status({})
        if self._connected and not self._simulation:
            self._bridge.set_firmware_versions.emit("", "")
            if not self._acquiring:
                self._load_mode_controls(self._current_mode)
        self._save_profiles()

    @Slot(object)
    def _on_active_settings(self, payload: dict) -> None:
        mode = str(payload.get("mode") or "")
        if mode in {item.value for item in Mode}:
            self._active_settings_by_mode[mode] = dict(payload)
            self._instrument_mode_readback = Mode(mode)
            self._update_readout_context()
            self.statusBar().showMessage(f"{mode} 仪器配置回读完成。")
            self._set_operation_status("配置已回读", state="success", detail=f"{mode} 仪器配置回读完成。")
            self._update_instrument_readback_status()
            self._update_controls()

    def _mode_changed(self, index: int) -> None:
        if index < 0:
            return
        mode = Mode(self.mode_combo.itemData(index))
        if mode == self._current_mode:
            self._load_mode_controls(mode)
            return
        if self._pending_wiring_retry is not None and self._approved_wiring_target is None:
            self._cancel_wiring_confirmation()
        if (self._acquiring or self._start_pending or self._experiment_active
                or self._finalization_pending or self._configure_pending):
            self._set_combo_data(self.mode_combo, self._current_mode.value)
            self.statusBar().showMessage("仪器操作或保存尚未完成，请稍后再切换模式。", 5000)
            return
        if self._plot_y and self._plot_data_mode is None:
            self._plot_data_mode = self._current_mode
        self._active_settings_by_mode.clear()
        self._current_mode = mode
        self._instrument_mode_known = not self._connected
        self._baseline = self._profiles[mode.value].get("baseline")
        if self._plot_y:
            self.baseline_line.hide()
        self._load_mode_controls(mode)
        if self._connected:
            applied = self._settings_for_mode(mode)
            self._queue_configuration(applied, mode_switch=not self._simulation)
        self._sync_plot_y_axis()

    def _mode_safety_instructions(self, mode: Mode) -> str:
        spec = descriptor(mode)
        safety = spec.safety
        if spec.safety_limits:
            safety = f"{safety}\n关键限值：{spec.safety_limits}"
        if spec.quantity != "current":
            return safety
        if mode == self._current_mode and hasattr(self, "range_combo"):
            autorange = self.autorange_check.isChecked()
            selected_range = self.range_combo.currentData()
        else:
            profile = self._profiles[mode.value]
            autorange = bool(profile.get("autorange", True))
            selected_range = profile.get("range_si", spec.ranges_si[0])
        range_value = None if selected_range is None else float(selected_range)
        return f"{safety}\n{current_input_guidance(range_value, autorange)}"

    def _refresh_safety_hint(self) -> None:
        if hasattr(self, "safety_text"):
            self.safety_text.setText(self._mode_safety_instructions(self._current_mode))
        if hasattr(self, "terminal_diagram_title"):
            self._refresh_terminal_diagram(self._current_mode)

    def _refresh_terminal_diagram(self, mode: Mode) -> None:
        current = mode in {Mode.DCI, Mode.ACI}
        four_wire = mode == Mode.RES4
        title = {
            Mode.DCV: "电压输入 · 并联连接",
            Mode.ACV: "电压输入 · 并联连接",
            Mode.DCI: "电流输入 · 串联连接",
            Mode.ACI: "电流输入 · 串联连接",
            Mode.RES2: "两线电阻 · INPUT HI / LO",
            Mode.RES4: "四线电阻 · INPUT 与 SENSE",
        }[mode]
        self.terminal_diagram_title.setText(title)
        self.terminal_socket_widgets["ma"].setVisible(current)
        self.terminal_socket_widgets["10a"].setVisible(current)
        self.terminal_sense_group.setVisible(four_wire)
        self.terminal_diagram.setToolTip(
            f"{title}。示意图不检测实际接线；接线前请核对仪器面板丝印和提示。"
        )

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
        firmware_field_note = current_firmware_gate_evidence_note(mode)
        self.autorange_check.setEnabled(autorange_reason is None)
        autorange_tooltip = autorange_reason or "由仪器在该模式已确认支持的量程间自动选择"
        if mode == Mode.ACI and not self._simulation:
            autorange_tooltip = f"{autorange_tooltip}。{firmware_field_note}。"
        self.autorange_check.setToolTip(autorange_tooltip)
        limit_messages = []
        if mode == Mode.ACI and not self._simulation:
            limit_messages.append(
                "ACI 400 mA 档要求软件版本 ≥1.0.700.18；官方资料未说明对应 OutG SW 还是 InG SW。"
                "本软件不根据 OutG SW 或 InG SW 自动拒绝 400 mA 档；使用 400 mA 固定档或自动量程前，请在仪表面板核对软件版本。"
            )
        if 0.4 in unsupported:
            minimum = "2.0" if mode == Mode.DCI else ""
            limit_messages.append(
                f"400 mA 档被禁用：当前固件版本 {self._firmware or '未核实'} 低于 {mode.value} 要求 {minimum}。"
                f"{firmware_field_note}下一步：核对并录入面板 OutG SW 后重新应用；确认前选用不超过100 mA的固定量程。"
            )
        if any(value in unsupported for value in (1.0, 3.0, 10.0)):
            terminals = self._terminals.strip() or "未确认"
            limit_messages.append(
                f"1/3/10 A 档被禁用：当前端子状态为 {terminals}；这些档位仅支持前面板电流端子。"
                "下一步：将电流线接到前面板电流插孔并刷新仪器配置，或选择适用于当前端子的固定量程。"
            )
        self.range_limit_hint.setText("\n".join(limit_messages))
        self.range_limit_hint.setVisible(bool(limit_messages))
        if autorange_reason and self.autorange_check.isChecked():
            self.autorange_check.setChecked(False)
            stored["autorange"] = False
        for i in range(1, self.range_combo.count()):
            if mode == Mode.ACI and self.range_combo.itemData(i) == 0.4 and not self._simulation:
                item = self.range_combo.model().item(i)
                if item is not None:
                    item.setToolTip(
                        "ACI 官方扩展规格要求软件版本 ≥1.0.700.18，但未说明对应 OutG SW 还是 InG SW。"
                        "本软件不会根据 OutG SW 或 InG SW 自动拒绝该档；使用前请在仪表面板核对软件版本。"
                    )
            if self.range_combo.itemData(i) in unsupported:
                item = self.range_combo.model().item(i)
                if item is not None:
                    item.setEnabled(False)
                    value = float(self.range_combo.itemData(i))
                    if value == 0.4:
                        minimum = "2.0" if mode == Mode.DCI else ""
                        reason = f"当前固件版本（{self._firmware}）低于该模式400 mA档要求（{minimum}）。{firmware_field_note}"
                        next_step = "核对并录入仪器 OutG 软件版本后重新应用；确认版本前请选用不超过100 mA的固定量程。"
                    else:
                        terminals = self._terminals.strip() or "未确认"
                        reason = f"当前端子状态为{terminals}；{range_label(value, spec.unit_si)}仅支持前面板电流端子。"
                        next_step = "将电流线接到前面板对应电流插孔并刷新仪器配置，或选择适用于当前端子的低电流固定量程。"
                    item.setToolTip(f"原因：{reason}\n下一步：{next_step}\n不会向仪器发送该量程。")
        if autorange_reason:
            if "固件版本低于" in autorange_reason:
                next_step = "核对并录入对应的 OutG 固件版本后重新应用；确认前关闭自动量程并选择已确认的固定量程。"
            else:
                next_step = "把电流线连接到前面板电流端子并刷新仪器配置；否则关闭自动量程，选择适用于当前端子的固定量程。"
            self.autorange_check.setToolTip(f"原因：{autorange_reason}。\n下一步：{next_step}")
            if mode == Mode.ACI and not self._simulation:
                self.autorange_check.setToolTip(
                    f"原因：{autorange_reason}。\n下一步：{next_step}\n{firmware_field_note}。"
                )
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
            self.nplc_combo.setVisible(True)
            self.nplc_row_label.setVisible(True)
            self.nplc_row_label.setText("积分时间 / NPLC")
            self.nplc_combo.setToolTip(
                f"{spec.label}使用 NPLC 控制积分时间；NPLC越低单次转换通常越快，越高越利于抑制工频噪声。"
            )
        else:
            self.nplc_combo.addItem("不适用", None)
            self.nplc_combo.setEnabled(False)
            self.nplc_combo.hide()
            self.nplc_row_label.hide()

        self.bandwidth_combo.clear()
        if spec.ac_bandwidths_hz:
            for value in spec.ac_bandwidths_hz:
                label = {3: "3 Hz · 慢", 20: "20 Hz · 中", 200: "200 Hz · 快"}[value]
                self.bandwidth_combo.addItem(label, value)
            band_idx = self.bandwidth_combo.findData(stored.get("ac_bandwidth_hz"))
            self.bandwidth_combo.setCurrentIndex(max(0, band_idx))
            self.bandwidth_combo.setEnabled(True)
            self.bandwidth_combo.setVisible(True)
            self.bandwidth_row_label.setVisible(True)
            self.bandwidth_row_label.setText("AC 带宽")
            self.bandwidth_combo.setToolTip(
                f"{spec.label}使用交流响应带宽；档位影响响应与噪声抑制，不等于每秒读数次数。"
            )
        else:
            self.bandwidth_combo.addItem("不适用", None)
            self.bandwidth_combo.setEnabled(False)
            self.bandwidth_combo.hide()
            self.bandwidth_row_label.hide()
        timing_effects = []
        if spec.nplc_values:
            timing_effects.append("NPLC 越高，积分越久")
        if spec.ac_bandwidths_hz:
            timing_effects.append("较低交流响应带宽可能延长响应和稳定时间")
        if spec.analog_filter:
            timing_effects.append("启用模拟滤波可能增加稳定等待")
        timing_effects.append("若启用自动零点，额外内部测量可能增加耗时")
        self.measurement_speed_help.setText(
            f"{spec.label}：{'；'.join(timing_effects)}，这些因素都可能拉长实测采样间隔。"
            "软件目标间隔只是仪器读数完成后的额外等待；实际间隔还包括仪器转换和串口通信耗时。"
        )
        self.analog_filter_check.setVisible(spec.analog_filter)
        self.analog_filter_check.setEnabled(spec.analog_filter)
        self.analog_filter_check.setChecked(bool(stored.get("analog_filter", False)) if spec.analog_filter else False)
        self.input_impedance_auto_check.setVisible(spec.input_impedance_auto)
        self.input_impedance_auto_check.setEnabled(spec.input_impedance_auto)
        self.input_impedance_auto_check.setChecked(
            bool(stored.get("input_impedance_auto", False)) if spec.input_impedance_auto else False
        )
        stored_digital_filter = bool(stored.get("digital_filter", False))
        self.digital_filter_check.setVisible(spec.digital_filter)
        self.digital_filter_check.setChecked(stored_digital_filter)
        self._update_digital_filter_availability()
        self.autozero_check.setChecked(bool(stored.get("autozero", True)))
        self.safety_text.setText(self._mode_safety_instructions(mode))
        self._refresh_terminal_diagram(mode)
        baseline = stored.get("baseline")
        self._baseline = float(baseline) if baseline is not None else None
        self.baseline_edit.setText("" if self._baseline is None else repr(self._baseline))
        self._sync_plot_y_axis()
        self.latest_value_label.setText("等待读数")
        self._update_range_enabled()
        self._loading_mode_controls = False
        self._update_readout_context()
        self._update_controls()

    def _sync_main_range(self) -> None:
        if not hasattr(self, "main_range_combo"):
            return
        combo = self.main_range_combo
        blocked = combo.blockSignals(True)
        try:
            combo.clear()
            for index in range(self.range_combo.count()):
                combo.addItem(self.range_combo.itemText(index), self.range_combo.itemData(index))
                source = self.range_combo.model().item(index)
                target = combo.model().item(index)
                if source is not None and target is not None:
                    target.setEnabled(source.isEnabled())
            combo.setCurrentIndex(0 if self.autorange_check.isChecked() else self.range_combo.currentIndex())
        finally:
            combo.blockSignals(blocked)

    def _cancel_wiring_confirmation(self) -> None:
        self._pending_wiring_target = None
        self._pending_wiring_retry = None
        self._approved_wiring_target = None

    def _confirm_inline_wiring(self, settings: ModeSettings, retry) -> bool:
        spec = descriptor(settings.mode)
        selected_range = "自动量程" if settings.autorange else range_label(float(settings.range_si), spec.unit_si)
        answer = QMessageBox.question(
            self, "确认接线", f"{spec.label} · {selected_range}\n确认接线正确？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _main_range_activated(self, index: int) -> None:
        if index == self.main_range_combo.currentIndex() and self._pending_wiring_retry is not None:
            self._cancel_wiring_confirmation()

    def _main_range_changed(self, index: int) -> None:
        if index < 0 or self._loading_mode_controls:
            return
        if self._pending_wiring_retry is not None and self._approved_wiring_target is None:
            self._cancel_wiring_confirmation()
        approved_current_range = False
        if (self._connected and not self._simulation
                and descriptor(self._current_mode).quantity == "current"):
            current = self._settings_for_mode(self._current_mode)
            requested = replace(current, autorange=index == 0,
                                range_si=None if index == 0 else float(self.main_range_combo.itemData(index)))
            if requested != current and not self._confirm_inline_wiring(
                requested, lambda: self.main_range_combo.setCurrentIndex(
                    self.main_range_combo.findData(requested.range_si)
                )
            ):
                self._sync_main_range()
                return
            approved_current_range = requested != current
        else:
            self._cancel_wiring_confirmation()
        if index == 0:
            self.autorange_check.setChecked(True)
        else:
            self.range_combo.setCurrentIndex(index)
            self.autorange_check.setChecked(False)
        if approved_current_range:
            applied = self._settings_for_mode(self._current_mode)
            self._confirmed_range_target = (applied.mode, applied.autorange, applied.range_si)
        if self._connected:
            self._apply_settings()

    def _update_range_enabled(self) -> None:
        self.range_combo.setEnabled(not self._acquiring and not self.autorange_check.isChecked())

    def _update_digital_filter_availability(self, *_args) -> None:
        if not hasattr(self, "digital_filter_check"):
            return
        spec = descriptor(self._current_mode)
        self.digital_filter_check.setVisible(spec.digital_filter)
        nplc = self.nplc_combo.currentData() if spec.nplc_values else None
        firmware_support = digital_filter_firmware_support(self._current_mode, self._firmware)
        nplc_error = digital_filter_enable_error(self._current_mode, nplc=nplc)
        reason = ""
        if not spec.digital_filter:
            reason = f"{spec.label}不支持直流数字滤波。"
        elif firmware_support is False:
            reason = "OutG SW 早于 2.0 不支持该功能；请在仪表 SYSTEM→VERSION 页面核对软件版本。"
        elif nplc_error:
            reason = nplc_error + "。"

        command_available = (
            not self._connected or self._simulation or self._control_mode == "computer"
        )
        setting_supported = spec.digital_filter and not reason
        enabled = setting_supported and command_available and not (
            self._acquiring or self._start_pending or self._experiment_active or self._finalization_pending
        )
        self.digital_filter_check.setEnabled(enabled)
        if spec.digital_filter:
            tooltip_parts = [DIGITAL_FILTER_INFO]
            if firmware_support is None:
                tooltip_parts.append("未知固件将尝试设置并回读，同时检查SCPI错误队列。")
            if reason:
                tooltip_parts.append(reason)
            elif not command_available:
                tooltip_parts.append("仪表处于 Local 模式；切换到 Computer 后可更改。")
            self.digital_filter_check.setToolTip("\n".join(tooltip_parts))

        if self.digital_filter_check.isChecked() and not setting_supported:
            blocked = self.digital_filter_check.blockSignals(True)
            self.digital_filter_check.setChecked(False)
            self.digital_filter_check.blockSignals(blocked)
            self._profiles[self._current_mode.value]["digital_filter"] = False
            if not self._loading_mode_controls:
                self._persist_current_settings()

    def _persist_current_settings(self) -> None:
        mode = self._current_mode
        stored = self._profiles[mode.value]
        stored.update({
            "autorange": self.autorange_check.isChecked(),
            "range_si": self.range_combo.currentData() if self.range_combo.currentData() is not None else descriptor(mode).ranges_si[0],
            "nplc": self.nplc_combo.currentData(),
            "ac_bandwidth_hz": self.bandwidth_combo.currentData(),
            "analog_filter": self.analog_filter_check.isChecked() if descriptor(mode).analog_filter else False,
            "digital_filter": self.digital_filter_check.isChecked() if descriptor(mode).digital_filter else False,
            "input_impedance_auto": (
                self.input_impedance_auto_check.isChecked()
                if descriptor(mode).input_impedance_auto else False
            ),
            "autozero": self.autozero_check.isChecked(),
            "baseline": self._baseline,
        })
        self._save_profiles()

    def _settings_changed(self, *_args) -> None:
        if not self._loading_mode_controls:
            self._cancel_wiring_confirmation()
            self._reset_preflight_status()
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
        selected_range_text = (
            "自动量程"
            if self.autorange_check.isChecked()
            else range_label(float(self.range_combo.currentData()), spec.unit_si)
            if self.range_combo.currentData() is not None
            else "量程待选"
        )
        selected_summary_parts = [selected_range_text]
        if spec.nplc_values and self.nplc_combo.currentData() is not None:
            selected_summary_parts.append(f"{float(self.nplc_combo.currentData()):g} NPLC")
        elif spec.ac_bandwidths_hz and self.bandwidth_combo.currentData() is not None:
            selected_summary_parts.append(f"{int(self.bandwidth_combo.currentData())} Hz 带宽")
        if active is not None:
            active_parts: list[str] = []
            active_autorange = bool(active.get("autorange", True))
            active_range = active.get("range_si")
            if active_autorange:
                active_parts.append("自动量程")
                if self._last_actual_range_si is not None:
                    range_text = range_label(self._last_actual_range_si, spec.unit_si)
                    active_parts.append(
                        f"MOCK模拟档位 {range_text}" if self._simulation
                        else f"本次实际量程 {range_text}"
                    )
                elif active.get("range_readback_si") is not None:
                    range_text = range_label(float(active["range_readback_si"]), spec.unit_si)
                    active_parts.append(
                        f"MOCK模拟档位 {range_text}" if self._simulation
                        else f"当前量程回读 {range_text}（快照）"
                    )
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
            if spec.digital_filter:
                if active.get("digital_filter_state") == "unsupported":
                    active_parts.append("数字滤波不支持（OutG SW < 2.0）")
                elif active.get("digital_filter"):
                    active_parts.append("数字滤波")
            if spec.input_impedance_auto:
                active_impedance = bool(active.get("input_impedance_auto", False))
                active_parts.append("自动高输入阻抗" if active_impedance else "10 MΩ输入阻抗")
                if self.input_impedance_auto_check.isChecked() != active_impedance:
                    pending.append(
                        "自动高输入阻抗"
                        if self.input_impedance_auto_check.isChecked()
                        else "10 MΩ输入阻抗"
                    )
            if bool(self.autorange_check.isChecked()) != active_autorange:
                pending.append("自动量程" if self.autorange_check.isChecked() else "固定量程")
            elif not active_autorange:
                selected_range = self.range_combo.currentData()
                if selected_range is not None and active_range != selected_range:
                    pending.append(f"量程 {range_label(float(selected_range), spec.unit_si)}")
            if spec.analog_filter and self.analog_filter_check.isChecked() != bool(active.get("analog_filter")):
                pending.append("模拟滤波" if self.analog_filter_check.isChecked() else "关闭模拟滤波")
            if spec.digital_filter and self.digital_filter_check.isChecked() != bool(active.get("digital_filter")):
                pending.append("数字滤波" if self.digital_filter_check.isChecked() else "关闭数字滤波")
            if self.autozero_check.isChecked() != bool(active.get("autozero", True)):
                pending.append("自动零点" if self.autozero_check.isChecked() else "关闭自动零点")
            if self._simulation:
                details.append("MOCK已应用 " + " · ".join(active_parts))
            else:
                details.append("仪表已应用 " + " · ".join(active_parts))
            if pending:
                details.append("待应用 " + "、".join(pending))
        else:
            selected_parts = list(selected_summary_parts)
            if spec.nplc_values and self.nplc_combo.currentData() is not None:
                if spec.digital_filter and self.digital_filter_check.isChecked():
                    selected_parts.append("数字滤波")
            if spec.input_impedance_auto:
                selected_parts.append(
                    "自动高输入阻抗" if self.input_impedance_auto_check.isChecked()
                    else "10 MΩ输入阻抗"
                )
            if self._connected and not self._simulation:
                details.append("仪表设置待读回")
            else:
                details.append(("MOCK选择 " if self._simulation else "本机已选 ") + " · ".join(selected_parts))
        self.reading_mode_label.setText("  ·  ".join(details))
        self.reading_mode_label.setWordWrap(True)
        if active is not None:
            active_mode = str(active.get("mode") or self._current_mode.value)
            try:
                active_mode_label = descriptor(Mode(active_mode)).label
            except ValueError:
                active_mode_label = spec.label
            effective = " · ".join([active_mode_label, *active_parts])
            pending_summary = (
                "待应用 · " + " · ".join([spec.label, *pending])
                if pending else "当前选择与仪器回读设置一致"
            )
        else:
            if self._connected and not self._simulation and self._control_mode == "local":
                effective = "Local模式 · 面板改动无法回读"
                pending_prefix = "待切回Computer后核对 · "
            elif self._connected and not self._simulation:
                effective = "仪器设置待读回"
                pending_prefix = "待应用 · "
            else:
                effective = "尚无仪器回读"
                pending_prefix = "MOCK选择 · " if self._simulation else "本机选择 · "
            pending_summary = pending_prefix + " · ".join([spec.label, *selected_summary_parts])
        self.settings_active_summary.setText(effective)
        self.settings_pending_summary.setText(pending_summary)

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
                digital_filter=self.digital_filter_check.isChecked() if descriptor(mode).digital_filter else False,
                input_impedance_auto=(
                    self.input_impedance_auto_check.isChecked()
                    if descriptor(mode).input_impedance_auto else False
                ),
            )
        stored = self._profiles[mode.value]
        return ModeSettings(
            mode, bool(stored.get("autorange", True)),
            None if stored.get("autorange", True) else float(stored.get("range_si", descriptor(mode).ranges_si[0])),
            stored.get("nplc") if descriptor(mode).nplc_values else None,
            stored.get("ac_bandwidth_hz") if descriptor(mode).ac_bandwidths_hz else None,
            bool(stored.get("analog_filter", False)) if descriptor(mode).analog_filter else False,
            bool(stored.get("autozero", True)),
            bool(stored.get("digital_filter", False)) if descriptor(mode).digital_filter else False,
            bool(stored.get("input_impedance_auto", False)) if descriptor(mode).input_impedance_auto else False,
        )

    def _settings_conflicts(self, settings: ModeSettings) -> list[str]:
        """Collect known incompatibilities before sending an instrument command."""
        conflicts: list[str] = []
        try:
            settings.validate()
        except (TypeError, ValueError) as exc:
            conflicts.append(f"测量模式/参数：{exc}")
            return conflicts
        if self._simulation or settings.mode not in {Mode.DCI, Mode.ACI}:
            return conflicts

        spec = descriptor(settings.mode)
        unsupported = unsupported_current_ranges(settings.mode, self._firmware, self._terminals)
        if settings.autorange:
            reason = current_autorange_block_reason(settings.mode, self._firmware, self._terminals)
            if reason:
                conflicts.append(f"量程：自动量程可能进入不兼容档位（{reason}）")
        for value in unsupported:
            if not settings.autorange and settings.range_si != value:
                continue
            reasons = []
            minimum = {Mode.DCI: (2, 0)}.get(settings.mode)
            if value == 0.4 and minimum is not None and compare_firmware(self._firmware, minimum) is False:
                reasons.append(f"固件 {self._firmware} 低于 400 mA 档要求")
                evidence_note = current_firmware_gate_evidence_note(settings.mode)
                if evidence_note:
                    reasons.append(evidence_note)
            if value in (1.0, 3.0, 10.0) and not self._terminals.strip().upper().startswith("FRON"):
                reasons.append(f"端子 {self._terminals or '未确认'} 不支持该前面板档位")
            if reasons:
                conflict_dimensions = []
                if value == 0.4 and minimum is not None and compare_firmware(self._firmware, minimum) is False:
                    conflict_dimensions.append("固件")
                if value in (1.0, 3.0, 10.0) and not self._terminals.strip().upper().startswith("FRON"):
                    conflict_dimensions.append("端子")
                dimension_label = "/".join(conflict_dimensions) or "仪器"
                conflicts.append(
                    f"量程：{range_label(value, spec.unit_si)} 与当前{dimension_label}状态冲突（{'；'.join(reasons)}）"
                )
        return conflicts

    def _apply_settings_from_ui(self) -> None:
        if self._pending_wiring_retry is not None:
            self._cancel_wiring_confirmation()
        self._apply_settings()

    def _apply_settings(self) -> None:
        if (self._configure_pending or self._acquiring or self._start_pending
                or self._experiment_active or self._finalization_pending):
            return
        try:
            self._persist_current_settings()
            settings = self._settings_for_mode(self._current_mode)
            approved_range = self._confirmed_range_target
            self._confirmed_range_target = None
            conflicts = self._settings_conflicts(settings)
            if conflicts:
                summary = "应用已取消，未向仪器发送配置命令。请先处理以下冲突项：\n\n• " + "\n• ".join(conflicts)
                self.statusBar().showMessage("测量设置存在已知冲突；未发送仪器命令。", 10000)
                QMessageBox.warning(self, "测量设置冲突", summary)
                return
            if not self._connected:
                QMessageBox.information(self, "参数已保存", "本模式参数已保存到本机。连接8845A后按“应用仪器参数”写入仪器。")
            elif (not self._simulation and self._instrument_mode_readback is not None
                  and self._instrument_mode_readback != settings.mode):
                self._queue_configuration(settings, mode_switch=True)
            elif not self._simulation:
                active = self._active_settings_by_mode.get(settings.mode.value, {})
                target = (settings.mode, settings.autorange, settings.range_si)
                active_range = (settings.mode, bool(active.get("autorange")), active.get("range_si"))
                needs_confirmation = (
                    mode_transition_requires_wiring_confirmation(self._instrument_mode_readback, settings.mode)
                    or (descriptor(settings.mode).quantity == "current" and target != active_range)
                )
                if needs_confirmation and approved_range != target and not self._confirm_inline_wiring(settings, self._apply_settings):
                    return
                self._queue_configuration(settings, confirmed_settings=settings if needs_confirmation else None)
            else:
                self._queue_configuration(settings)
        except Exception as exc:
            self._show_error(str(exc))

    def _queue_configuration(
        self, settings: ModeSettings, *, confirmed_settings: ModeSettings | None = None,
        mode_switch: bool = False,
    ) -> None:
        if self._configure_pending:
            return
        self._active_settings_by_mode.clear()
        self._instrument_mode_known = False
        self._configure_pending = True
        self._update_instrument_readback_status()
        self._update_controls()
        self.statusBar().showMessage("正在发送仪器配置命令，等待仪器回读…")
        self._set_operation_status(
            "正在应用配置",
            detail="进行中：发送仪器配置命令。预期结果：当前模式参数完成写入并收到配置回读。",
        )
        if mode_switch:
            self._bridge.configure_unconfirmed_mode.emit(settings)
        else:
            self._bridge.configure.emit(settings, confirmed_settings)

    def _initialize_parameters(self) -> None:
        if self._acquiring:
            self.statusBar().showMessage("请先停止采集，再初始化参数。", 5000)
            return
        answer = QMessageBox.question(
            self,
            "恢复默认测量设置",
            "将六种测量模式恢复为默认参数：自动量程、默认积分时间/交流带宽、关闭模拟滤波和自动高输入阻抗、启用自动零点并清除模式基准；"
            "软件目标采样间隔设为 1 秒。\n\n"
            "串口设置和保存目录会保留。此操作只改软件配置，不会复位仪表；若要让当前模式生效，请随后点击“应用仪器参数”。\n\n"
            "确定初始化吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        for mode in Mode:
            self._profiles[mode.value] = self._default_mode_profile(mode)
        self._profiles["sampling_interval_s"] = 1.0
        self.interval_spin.setValue(1.0)
        self._load_mode_controls(self._current_mode)
        self.baseline_line.hide()
        self.change_label.setText("未设置基准值")
        self._save_profiles()
        self._update_readout_context()
        self.statusBar().showMessage("软件测量参数已初始化；需要时请点击“应用仪器参数”写入仪表。", 10000)

    def _apply_acquisition_speed(self) -> None:
        self.statusBar().showMessage("测量参数在本次实验期间锁定；停止并保存后可重新设置。", 5000)

    @Slot(bool, str)
    def _on_measurement_speed_result(self, success: bool, message: str) -> None:
        self._speed_update_pending = False
        self.statusBar().showMessage(message, 12000 if success else 20000)
        self._update_readout_context()
        self._update_controls()

    def _request_zero(self) -> None:
        if self._zero_pending:
            return
        if self._acquiring:
            message = "请停止采集后再执行ZERO。模拟滤波条件下，ZERO应与当前量程、NPLC和触发设置一致。"
            self._on_zero_result(f"ZERO未执行：{message}")
            self._show_error(message)
            return
        if not self._zero_configuration_ready():
            self._on_zero_result("ZERO未执行：请先读取或应用当前模式配置，并确认配置回读。")
            return
        active = self._active_settings_by_mode.get(self._current_mode.value)
        if active is None:
            applied = "当前模式尚无仪器配置回读；请先应用参数并确认读回。"
        else:
            spec = descriptor(self._current_mode)
            if active.get("autorange"):
                range_text = "自动量程"
                range_readback = active.get("range_readback_si")
                if range_readback is not None and math.isfinite(float(range_readback)):
                    range_text += (
                        f"；当前量程回读 {range_label(float(range_readback), spec.unit_si)}"
                        "（当前快照，可能随输入变化）"
                    )
                else:
                    range_text += "（当前量程回读不可用）"
                if active.get("simulation"):
                    range_text += "（MOCK模拟）"
            else:
                range_text = f"固定量程 {range_label(active.get('range_si'), spec.unit_si)}"
            timing = (
                f"NPLC={active.get('nplc')}"
                if active.get("nplc") is not None
                else f"交流带宽={active.get('ac_bandwidth_hz')} Hz"
                if active.get("ac_bandwidth_hz") is not None
                else "无适用积分/带宽项"
            )
            analog_filter = (
                f"模拟滤波：{'开' if active.get('analog_filter') else '关'}"
                if spec.analog_filter else "模拟滤波：不适用"
            )
            applied = (
                f"已回读配置：{spec.label} · {range_text} · {timing} · "
                f"{analog_filter} · "
                f"自动零点：{'开' if active.get('autozero') else '关'}。"
            )
        answer = QMessageBox.question(
            self,
            "执行仪器 ZERO",
            "ZERO:AUTO ONCE 会立即启动一次仪表零点测量并更新零点补偿；它不会把当前被测读数强制清零，也不是校准调整。\n\n"
            f"{applied}\n\n"
            "请确认当前量程、测量速度与触发条件稳定。使用模拟滤波时，ZERO条件应与量程、NPLC和触发设置匹配。\n\n"
            "现在执行一次吗？",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._zero_pending = True
            self._update_controls()
            self.zero_result_label.setText("ZERO执行中；完成后显示仪器配置回读。")
            self.zero_result_label.show()
            self._set_operation_status("ZERO执行中", detail="进行中：仪表正在执行ZERO。预期结果：返回ZERO后的配置回读；不代表当前读数被清零或校准完成。")
            self._bridge.zero.emit()

    def _zero_configuration_ready(self) -> bool:
        return (
            self._instrument_mode_known
            and self._instrument_mode_readback == self._current_mode
            and self._current_mode.value in self._active_settings_by_mode
        )

    @Slot(str)
    def _on_zero_result(self, result: str) -> None:
        self._zero_pending = False
        self.zero_result_label.setText(result)
        self.zero_result_label.show()
        readback_failed = any(
            marker in result for marker in (
                "配置回读失败", "ZERO失败", "ZERO执行失败", "命令或执行确认失败", "ZERO未执行",
            )
        )
        self._set_operation_status(
            "ZERO执行失败" if readback_failed else "ZERO配置回读已返回",
            state="failure" if readback_failed else "success",
            detail=result,
        )
        self.statusBar().showMessage(result, 20000)
        self._update_controls()

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

    def _begin_simple_measurement(self, *, one_shot: bool) -> None:
        if self._pending_wiring_retry is not None:
            self._cancel_wiring_confirmation()
        if self._acquiring or self._start_pending or self._experiment_active or self._finalization_pending:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.experiment_name.setText(f"实验_{timestamp}_{self._current_mode.value}")
        self._start(one_shot=one_shot)
        if self._start_pending:
            self.configuration_dialog.hide()

    def _start(self, one_shot: bool) -> None:
        if self._history_load_pending:
            self.statusBar().showMessage("历史 CSV 正在后台加载，完成后再开始采集。", 5000)
            return
        if not self._validate_experiment_name_field():
            self.statusBar().showMessage(self.experiment_name_validation.text(), 8000)
            return
        if self._start_pending:
            self.statusBar().showMessage("采集启动请求正在处理，请等待仪器响应。", 5000)
            return
        if (self._diagnostic_pending or self._connection_pending or self._configure_pending
                or self._zero_pending or self._acquiring or self._experiment_active
                or self._finalization_pending):
            self.statusBar().showMessage("仪器命令正在执行，请等待完成后再启动采集。", 5000)
            return
        if not self._connected:
            self._show_error("请先连接仪器，或勾选MOCK模拟模式并点击“连接真实仪器”。")
            return
        if not self._simulation and not self._instrument_mode_known:
            self._show_error("未识别到仪器当前测量功能。请先逐项核对接线，再点击“应用仪器参数”明确配置一种支持模式。")
            return
        confirmed_settings = None
        if not self._simulation:
            confirmed_settings = self._settings_for_mode(self._current_mode)
            if not self._confirm_inline_wiring(confirmed_settings, lambda: self._start(one_shot)):
                return
        try:
            settings = self._settings_for_mode(self._current_mode)
            settings.validate()
            root = Path(self.output_root.text()).expanduser()
            if not self.experiment_name.text().strip():
                raise ValueError("请填写实验名称")
            validate_experiment_name(self.experiment_name.text())
            duplicate_name = self._prepare_start_preflight(settings, root, wiring_confirmed=not self._simulation)
            duplicate_action = ""
            expected_duplicate_path = ""
            if duplicate_name:
                duplicates = find_existing_experiment_directories(root, self.experiment_name.text())
                latest_name = duplicates[-1].name if duplicates else "（目录状态已变化）"
                expected_duplicate_path = str(duplicates[-1].resolve()) if duplicates else ""
                dialog = QMessageBox(self)
                dialog.setWindowTitle("同名实验已存在")
                dialog.setText(f"检测到 {len(duplicates)} 个同名实验目录。最近一次：{latest_name}")
                dialog.setInformativeText(
                    "选择“覆盖”时，最近一次实验目录会先移动到保存根目录下的 .8845a-backups，"
                    "旧数据仍可恢复。若元数据仍显示运行中，程序会先取得同名跨进程锁；"
                    "只有确认没有其他采集进程后才标记为“异常中断”并备份，活动实验会被拒绝覆盖。"
                    "程序随后为本次实验创建新目录。也可以另存独立目录、改名或取消。"
                )
                overwrite = dialog.addButton("覆盖最近一次（先备份）", QMessageBox.ButtonRole.AcceptRole)
                create_copy = dialog.addButton("新建独立目录", QMessageBox.ButtonRole.AcceptRole)
                rename = dialog.addButton("返回改名", QMessageBox.ButtonRole.ActionRole)
                cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                dialog.setDefaultButton(cancel)
                dialog.exec()
                if dialog.clickedButton() is rename:
                    self.preflight_status.setText("请修改实验名称后重新开始；旧实验数据未修改。")
                    self.experiment_name.setFocus()
                    self.experiment_name.selectAll()
                    return
                if dialog.clickedButton() is overwrite:
                    duplicate_action = "overwrite"
                    self.preflight_status.setText("将先备份最近一次同名实验，再创建本次实验目录。")
                elif dialog.clickedButton() is create_copy:
                    duplicate_action = "create-copy"
                    self.preflight_status.setText("将创建新的独立目录；已有实验保留不变。")
                if dialog.clickedButton() is not create_copy:
                    if dialog.clickedButton() is not overwrite:
                        self.preflight_status.setText("已取消启动；旧实验数据未修改。")
                        return
            interval = None if self.interval_spin.value() == 0 else self.interval_spin.value()
            if self.mock_mode_check.isChecked() and not self._simulation:
                raise ValueError("MOCK模式未连接。请先点击“连接真实仪器”以通过MOCK后端连接；UI会明确显示模拟状态。")
            self._persist_current_settings()
            self._locked_interval_s = self.interval_spin.value()
            self._start_pending = True
            self._last_run_single = bool(one_shot)
            self._user_stop_pending = False
            self.statusBar().showMessage("正在启动单次测量…" if one_shot else "正在启动连续采集…")
            self._update_controls()
            self._bridge.start_experiment.emit(
                settings, self.experiment_name.text().strip(), str(root), interval, one_shot,
                duplicate_action, expected_duplicate_path, confirmed_settings,
            )
        except Exception as exc:
            self._start_pending = False
            self.preflight_status.setText(f"采集预检未通过：{exc}")
            self._update_controls()
            self._show_error(str(exc))

    def _prepare_start_preflight(self, settings: ModeSettings, root: Path, *, wiring_confirmed: bool) -> bool:
        active = self._active_settings_by_mode.get(settings.mode.value)
        if active is None or active.get("mode") != settings.mode.value:
            raise ValueError("当前仪器配置没有可用的回读结果；请先应用仪器参数并确认回读")
        prepared_root = prepare_experiment_storage(root)
        free_bytes = require_minimum_free_space(get_available_space_bytes(prepared_root))
        available_space = format_available_space(free_bytes)
        target_interval_s = self.interval_spin.value() or None
        capacity_seconds = estimate_recording_capacity_seconds(free_bytes, target_interval_s)
        if capacity_seconds is None:
            capacity_forecast = format_estimated_recording_capacity(None)
        else:
            capacity_forecast = (
                f"按 4 KiB/样本粗估，目标间隔 {target_interval_s:g} s 时可记录"
                f"{format_estimated_recording_capacity(capacity_seconds)}；实际采样间隔、原始响应及错误文本长度会影响结果"
            )
        duplicates = find_existing_experiment_directories(prepared_root, self.experiment_name.text())
        identity = self.idn_label.text().replace("设备身份：", "").strip() or "已连接仪器"
        wiring = "模拟器无需实体接线" if self._simulation else (
            "接线已由操作员确认；软件无法检查表笔、保险丝或外部回路" if wiring_confirmed else "接线未确认"
        )
        self.preflight_status.setText(
            "采集预检通过：\n"
            f"✓ 仪器已连接：{identity}\n"
            f"✓ 当前功能已回读：{descriptor(settings.mode).label}；本次参数将在开始时再次应用并回读\n"
            f"✓ 保存目录可写：{prepared_root}（当前可用 {available_space}；启动至少预留 10 MiB；"
            f"{capacity_forecast}；此估算不限制实验时长。采集中每 5 秒复查空间，低于预留值时停止采集并尝试收尾文件，"
            "若收尾失败会提示记录可能不完整）\n"
            f"✓ {wiring}\n"
            "✓ 实验名称有效；不会覆盖旧数据。"
        )
        if duplicates:
            self.preflight_status.setText(
                self.preflight_status.text()
                + f"\n发现 {len(duplicates)} 个同名实验目录；最近一次为 {duplicates[-1].name}，"
                "确认后可备份并覆盖，或另存独立目录。"
            )
        return bool(duplicates)

    def _reset_preflight_status(self) -> None:
        if hasattr(self, "preflight_status"):
            self.preflight_status.setText("配置或路径已变化；开始采集时会重新检查连接、回读、接线与保存目录。")

    def _on_sampling_interval_changed(self, value: float) -> None:
        if self._acquiring or self._start_pending or self._experiment_active or self._finalization_pending:
            if self._locked_interval_s is not None and value != self._locked_interval_s:
                blocked = self.interval_spin.blockSignals(True)
                self.interval_spin.setValue(self._locked_interval_s)
                self.interval_spin.blockSignals(blocked)
            return
        self._profiles["sampling_interval_s"] = float(value)
        self._save_profiles()

    def _connect_mock(self) -> None:
        if self._connected or self._connection_pending:
            return
        try:
            settings = self._settings_for_mode(self._current_mode)
            settings.validate()
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.connect_button.setEnabled(False)
        self._connection_pending = True
        self.connection_badge.setText("正在启动MOCK…")
        self.statusBar().showMessage("正在启动模拟器…")
        self._update_controls()
        self._bridge.connect_mock.emit(settings, self._scenario())

    def _update_connect_label(self, mock: bool) -> None:
        self.connect_button.setText("启动MOCK模拟仪器" if mock else "连接真实仪器")
        self.connect_button.setProperty("connectionTarget", "mock" if mock else "instrument")
        self.connect_button.style().unpolish(self.connect_button)
        self.connect_button.style().polish(self.connect_button)
        self.scenario_combo.setEnabled(bool(mock) and not self._connected)
        self.mock_scenario_status.setVisible(bool(mock) or self._simulation)
        self._update_mock_scenario_status()

    def _update_mock_scenario_status(self, *_args) -> None:
        if self._simulation:
            selected = self._connected_mock_scenario or self.scenario_combo.currentText()
            status = f"MOCK场景：{selected}"
        elif self.mock_mode_check.isChecked():
            status = f"待连接MOCK场景：{self.scenario_combo.currentText()}"
        else:
            status = "MOCK场景：未连接"
        if self._connected:
            scenario_tooltip = (
                "当前设备已连接，模拟场景选择已锁定。切换场景请先断开连接，选好场景后重新连接模拟器。"
            )
        elif self.mock_mode_check.isChecked():
            scenario_tooltip = "模拟场景需在连接前选择；修改场景后重新连接模拟器即可生效。"
        else:
            scenario_tooltip = "此选项仅适用于 Mock 模拟仪器；先启用模拟模式，再选择连接前要使用的场景。"
        self.scenario_combo.setToolTip(scenario_tooltip)
        self.mock_scenario_status.setText(status)
        self.mock_scenario_header.setText(status)
        self.mock_scenario_header.hide()

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
            self._user_stop_pending = True
            self.statusBar().showMessage("正在结束当前读数并保存剩余数据到 CSV…")
            self._set_operation_status("正在停止并保存", detail="进行中：正在结束当前读数并保存剩余数据。预期结果：显示 measurements.csv 路径及尝试、有效、异常计数。")
            self._worker.request_stop()
            self._bridge.stop.emit()

    @Slot(str, bool)
    def _on_acquisition_state_changed(self, state: str, one_shot_running: bool) -> None:
        try:
            acquisition_state = AcquisitionState(state)
        except ValueError:
            log.error("Worker emitted an unknown acquisition state: %r", state)
            return
        self._acquiring = acquisition_state in {AcquisitionState.RUNNING, AcquisitionState.PAUSED}
        self._paused = acquisition_state is AcquisitionState.PAUSED
        self._single_shot_running = bool(one_shot_running)
        if self._acquiring:
            self._experiment_active = True
            if self._locked_interval_s is None:
                self._locked_interval_s = self.interval_spin.value()
        # The recorder remains active while the Worker flushes and finalizes
        # files after transitioning to STOPPED. _on_experiment_finished owns
        # clearing this lock so exports cannot race final CSV writes.
        dot_state = "paused" if self._paused else "running" if self._acquiring else "idle"
        self.status_dot.setProperty("state", dot_state)
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        self._update_controls()

    def _on_status(self, status: str) -> None:
        if status.startswith(("连续超时", "SCPI响应超时", "串口读取超时", "串口通信异常", "仪器已切回LOCAL", "RS-232当前不是Computer", "连接中断")) or "过量程" in status:
            self._last_acquisition_issue = status
            self._show_recovery_guidance(status)
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
        elif status.startswith(("串口读取超时", "串口通信异常")):
            display_status = "通信故障"
        elif status.startswith("SCPI"):
            display_status = "响应超时"
        else:
            display_status = status
        self.experiment_status.setText(display_status)
        self.experiment_status.setToolTip(status)
        self.statusBar().showMessage(status)
        self._update_controls()

    def _show_recovery_guidance(self, status: str) -> None:
        if status.startswith(("串口读取超时", "SCPI响应超时", "串口通信异常")):
            guidance = (
                "读取过程中出现串口超时或通信故障；当前实验已立即停止，失败尝试已记录并保存，串口连接已关闭。"
                "软件不会自动重试或续接旧 CSV。检查线缆、COM/波特率、结束符和仪表 Computer 模式，"
                "确认状态后重新连接并开始新实验/分段。"
            )
        elif status.startswith("连续超时"):
            guidance = (
                "多次读数超时后采集已停止，异常尝试已写入 CSV。真实仪器发生串口超时会立即停止并关闭连接；"
                "检查连接后重新连接并开始新实验/分段。"
            )
        elif status.startswith("仪器已切回LOCAL"):
            guidance = "仪器因 LOCAL 状态拒绝远程命令；当前实验记录已保留。切回 Computer 并重新连接后开始新实验。"
        elif status.startswith("RS-232当前不是Computer"):
            guidance = "串口终端模式下停止采集，已有 CSV 记录保留；在仪表切回 Computer 后重新连接并开始新实验。"
        elif status in {"普通读超时", "timeout"}:
            guidance = (
                "无效读数已记录。真实仪器的串口超时或通信异常会立即停止采集并关闭连接，不自动重试或续接旧 CSV。"
                "MOCK 超时行为按当前模拟场景设置。检查连接后重新连接并开始新实验/分段。"
            )
        elif "连接中断" in status:
            guidance = "连接中断后采集停止；已落盘的有效与异常尝试保留。检查线缆和串口设置，重新连接后开始新实验。"
        elif "过量程" in status:
            guidance = "过量程后采集停止；检查输入端子、保险丝、回路及量程，确认安全后重新连接或配置并开始新实验。"
        else:
            return
        attempts = int(self._global_stats.get("attempt_count", 0) or 0)
        if attempts:
            valid = int(self._global_stats.get("valid_count", self._global_stats.get("count", 0)) or 0)
            abnormal = int(self._global_stats.get("abnormal_count", max(0, attempts - valid)) or 0)
            guidance = f"{guidance} 当前 CSV 已保存 {attempts} 次尝试：有效 {valid}、异常 {abnormal}。"
        self.recovery_hint_label.setToolTip(guidance)
        self.recovery_hint_label.setText(
            "采集已停止 · 悬停查看" if self._short_viewport_layout else guidance
        )
        self.recovery_hint_label.show()

    def _jump_to_latest_invalid_log(self) -> None:
        if not self._latest_invalid_log_text:
            return
        error_index = self.log_filter_combo.findData("error")
        if error_index >= 0 and self.log_filter_combo.currentIndex() != error_index:
            self.log_filter_combo.setCurrentIndex(error_index)
        self.log_dock.show()
        self.log_dock.raise_()
        cursor = self.log_text.document().find(self._latest_invalid_log_text)
        if not cursor.isNull():
            self.log_text.setTextCursor(cursor)
            self.log_text.ensureCursorVisible()


    def _update_controls(self) -> None:
        idle = not self._acquiring and not self._start_pending and not self._experiment_active and not self._finalization_pending
        self._update_experiment_directory_controls()
        command_ready = (
            idle and not self._diagnostic_pending and not self._connection_pending
            and not self._configure_pending and not self._zero_pending
            and not self._control_mode_switch_pending
        )
        computer_control = not self._connected or self._simulation or self._control_mode == "computer"
        reason = (
            "正在连接仪器。" if self._connection_pending else
            "正在读取仪器诊断，完成后才能继续操作。" if self._diagnostic_pending else
            "正在应用仪器参数，等待配置回读。" if self._configure_pending else
            "ZERO正在执行，等待仪器结果。" if self._zero_pending else
            "采集中不能更改此参数。" if self._acquiring or self._start_pending else
            "Local 模式下不发送设置命令；切换到 Computer 后可操作。" if self._connected and not computer_control else
            "请先连接仪器或模拟器。" if not self._connected else ""
        )
        if self._connected and not computer_control and (self._acquiring or self._start_pending):
            reason += "\nLocal 模式下不发送设置命令；切换到 Computer 后可操作。"
        self.mode_combo.setEnabled(command_ready and computer_control)
        if hasattr(self, "mode_buttons"):
            for button in self.mode_buttons.values():
                button.setEnabled(command_ready and computer_control)
            self._sync_mode_buttons()
            self.connection_action_button.setEnabled(command_ready)
            self.connection_action_button.setText("连接设置" if self._connected else "连接仪器")
            self.measurement_settings_button.setEnabled(self._connected and command_ready and computer_control)
        spec = descriptor(self._current_mode)
        autorange_allowed = (
            self._simulation or spec.quantity != "current"
            or current_autorange_block_reason(self._current_mode, self._firmware, self._terminals) is None
        )
        self.autorange_check.setEnabled(command_ready and computer_control and autorange_allowed)
        if hasattr(self, "main_range_combo"):
            self._sync_main_range()
            self.main_range_combo.setEnabled(command_ready and computer_control and self._connected)
            auto_item = self.main_range_combo.model().item(0)
            if auto_item is not None:
                auto_item.setEnabled(autorange_allowed)
        self.range_combo.setEnabled(command_ready and computer_control and not self.autorange_check.isChecked())
        settings_command_pending = self._configure_pending or self._zero_pending
        self.nplc_combo.setEnabled(
            command_ready and computer_control and not settings_command_pending
            and bool(spec.nplc_values)
        )
        self.bandwidth_combo.setEnabled(
            command_ready and computer_control and not settings_command_pending
            and bool(spec.ac_bandwidths_hz)
        )
        self.analog_filter_check.setEnabled(command_ready and computer_control and spec.analog_filter)
        self.input_impedance_auto_check.setEnabled(
            command_ready and computer_control and spec.input_impedance_auto
        )
        self._update_digital_filter_availability()
        self.autozero_check.setEnabled(command_ready and computer_control)
        self.interval_spin.setEnabled(command_ready and computer_control)
        self.output_root.setEnabled(command_ready and computer_control)
        self.output_root_browse_button.setEnabled(command_ready and computer_control)
        self.apply_button.setEnabled(command_ready and self._connected and computer_control)
        self.initialize_parameters_button.setEnabled(command_ready and computer_control)
        initialize_reason = ""
        if not computer_control and self._connected:
            initialize_reason = "Local 模式下不更改软件测量配置；切换到 Computer 后可初始化。"
        elif not command_ready:
            initialize_reason = reason or "当前仪器操作尚未完成；完成后可初始化参数。"
        self.initialize_parameters_button.setToolTip(
            f"{initialize_reason}\n\n{self._initialize_parameters_tooltip}"
            if initialize_reason else self._initialize_parameters_tooltip
        )
        self.apply_speed_button.setEnabled(False)
        zero_ready = self._zero_configuration_ready()
        self.zero_button.setEnabled(command_ready and self._connected and computer_control and zero_ready)
        history_load_ready = not self._history_load_pending
        confirmed_settings_ready = (
            self._instrument_mode_known
            and self._current_mode.value in self._active_settings_by_mode
        )
        self.start_button.setEnabled(
            command_ready and self._connected and computer_control
            and confirmed_settings_ready and self._experiment_name_valid and history_load_ready and not self._experiment_active
        )
        self.single_button.setEnabled(
            command_ready and self._connected and computer_control
            and confirmed_settings_ready and self._experiment_name_valid and history_load_ready and not self._experiment_active
        )
        if hasattr(self, "configuration_start_button"):
            self.configuration_start_button.setEnabled(self.start_button.isEnabled())
            self.configuration_single_button.setEnabled(self.single_button.isEnabled())
        self.pause_button.setEnabled(self._acquiring and not self._single_shot_running)
        self.pause_button.setText("继续" if self._paused else "暂停")
        self.stop_button.setEnabled(self._acquiring)
        self.history_button.setEnabled(idle and not self._history_load_pending)
        self.history_button.setToolTip(
            "停止采集后加载历史 CSV；返回实时可恢复当前缓存曲线。"
            if idle else "请先停止采集，再切换到历史 CSV。"
        )
        self.disconnect_button.setEnabled(self._connected and command_ready)
        self.diagnose_button.setEnabled(self._connected and computer_control and command_ready)
        self.event_button.setEnabled(self._experiment_active and not self._event_save_pending)
        self.event_edit.setEnabled(self._experiment_active and not self._event_save_pending)
        self.export_snapshot_button.setEnabled(
            self._experiment_active and not self._snapshot_pending
            and self._experiment_csv_path is not None and self._experiment_csv_path.is_file()
        )
        self.connect_button.setEnabled(not self._connected)
        self.control_mode_combo.setEnabled(
            self._connected and not self._simulation and idle and not self._control_mode_switch_pending
        )
        firmware_lock_reason = (
            "连接真实仪器后才能记录面板软件版本。" if not self._connected else
            "MOCK 模拟模式不使用真实仪器的面板版本。" if self._simulation else
            "采集中不能更改面板版本记录；请先停止并保存当前实验。" if not idle else
            ""
        )
        for widget, help_text in (
            (self.outguard_sw_edit, self._outguard_sw_tooltip),
            (self.inguard_sw_edit, self._inguard_sw_tooltip),
        ):
            widget.setEnabled(not firmware_lock_reason)
            widget.setToolTip(
                f"{firmware_lock_reason}\n\n{help_text}"
                if firmware_lock_reason else help_text
            )
        saved_firmware = self._firmware_profiles.get(self._device_serial, {})
        has_saved_firmware = bool(
            isinstance(saved_firmware, dict)
            and (saved_firmware.get("outguard_sw") or saved_firmware.get("inguard_sw"))
        )
        clear_firmware_reason = firmware_lock_reason or (
            "尚无当前仪器的面板版本记录可清除。"
            if not self._device_serial or not has_saved_firmware else ""
        )
        self.clear_firmware_profile_button.setEnabled(
            self._connected and not self._simulation and idle
            and bool(self._device_serial) and has_saved_firmware
        )
        self.clear_firmware_profile_button.setToolTip(
            f"{clear_firmware_reason}\n\n{self._clear_firmware_profile_tooltip}"
            if clear_firmware_reason else self._clear_firmware_profile_tooltip
        )
        mock_selected = bool(self.mock_mode_check.isChecked() or self._simulation)
        self.scenario_combo.setEnabled(mock_selected and not self._connected)
        self.mock_scenario_status.setVisible(mock_selected)
        self._update_mock_scenario_status()
        self.set_control_mode_button.setEnabled(
            self._connected and not self._simulation and command_ready and not self._control_mode_switch_pending
        )
        self.mode_combo.setToolTip(reason)
        self.autorange_check.setToolTip(reason or self.autorange_check.toolTip())
        range_lock_reason = reason
        if not self._connected and reason == "请先连接仪器或模拟器。":
            range_lock_reason = ""
        range_tooltip_parts = [range_lock_reason]
        if self.autorange_check.isChecked():
            range_tooltip_parts.append("自动量程已启用；关闭自动量程后可选择固定量程。")
        self.range_combo.setToolTip(
            "\n".join(part for part in range_tooltip_parts if part)
            or "选择当前测量模式的固定量程。"
        )
        self.nplc_combo.setToolTip(
            reason if self._connected and not computer_control else
            "NPLC 决定积分时间；更高 NPLC 通常需要更长读数时间。应用速度后从下一次仪器读数起生效。"
        )
        self.bandwidth_combo.setToolTip(reason or "交流响应带宽按当前测量模式生效。")
        self.analog_filter_check.setToolTip(reason or "直流模拟滤波会影响读数稳定时间。")
        autozero_tooltip = self.autozero_help
        if reason:
            autozero_tooltip = f"{reason}\n\n{autozero_tooltip}"
        self.autozero_check.setToolTip(autozero_tooltip)
        self.apply_button.setToolTip(
            "正在应用仪器参数，等待配置回读。"
            if self._configure_pending else reason or "将当前模式参数写入仪器并等待回读。"
        )
        name_reason = self.experiment_name_validation.text() if not self._experiment_name_valid else ""
        start_reason = (
            "历史 CSV 正在后台加载，完成后再开始采集。"
            if self._history_load_pending else
            "正在处理启动请求，请稍候；完成后可继续操作。"
            if self._start_pending else
            "当前实验正在收尾并保存，完成后可开始新测量。"
            if self._experiment_active and not self._acquiring else
            "当前测量采集中；先点“停止并保存”，完成后再开始下一次测量。"
            if self._acquiring else
            reason or name_reason
        )
        self.start_button.setToolTip((start_reason or "开始连续采集；原始结果逐点保存到当前实验 CSV。") + " 快捷键：Ctrl+R。")
        self.single_button.setToolTip(start_reason or "只读取一次并保存为独立实验数据。")
        if hasattr(self, "configuration_start_button"):
            self.configuration_start_button.setToolTip(self.start_button.toolTip())
            self.configuration_single_button.setToolTip(self.single_button.toolTip())
        pause_tooltip = (
            "继续当前已暂停的采集。快捷键：Ctrl+P。" if self._paused else
            "单次测量不支持暂停；可停止当前读数。" if self._single_shot_running else
            "暂停当前连续采集。快捷键：Ctrl+P。" if self._acquiring else
            "当前没有连续采集；开始连续采集后可暂停。快捷键：Ctrl+P。"
        )
        self.pause_button.setToolTip(pause_tooltip)
        stop_tooltip = (
            "停止当前单次测量并保存已写入的数据。快捷键：Ctrl+Shift+S。"
            if self._single_shot_running else
            "停止当前采集并保存已写入的数据。快捷键：Ctrl+Shift+S。"
            if self._acquiring else
            "当前没有进行中的采集；开始连续采集后可停止并保存。快捷键：Ctrl+Shift+S。"
        )
        self.stop_button.setToolTip(stop_tooltip)
        can_start_new_measurement = (
            not self._history_load_pending
            and not self._start_pending
            and not self._new_measurement_after_save
            and (not self._experiment_active or self._acquiring)
        )
        self.new_measurement_button.setEnabled(can_start_new_measurement)
        self._update_measurement_hint()
        self.new_measurement_button.setToolTip(
            "正在停止并保存当前实验；完成后自动打开新测量设置。"
            if self._new_measurement_after_save else
            "历史 CSV 正在加载，完成后可新建测量。" if self._history_load_pending else
            "正在处理启动请求，请稍候；完成后可新建测量。" if self._start_pending else
            "点击后先停止并保存当前实验；确认框中选择“否”会继续采集。当前曲线和 CSV 会保留。"
            if self._acquiring else
            "打开新实验设置并填写名称，然后选择单次测量或连续采集。"
            "开始后曲线自动换新，旧实验 CSV 保留，无需先清空。快捷键：Ctrl+N。"
            if can_start_new_measurement else
            "当前实验正在收尾并保存，完成后可新建测量。"
            if self._experiment_active and not self._acquiring else
            "请先停止并保存当前实验，再新建测量。"
        )
        can_clear_plot = not (
            self._acquiring or self._experiment_active or self._start_pending
            or self._history_load_pending
        )
        clear_plot_tooltip = (
            "立即清除当前图表、统计和事件标记；已保存的 measurements.csv 和 events.csv 不会删除。"
            if can_clear_plot else
            "历史 CSV 正在加载，完成后再清空图表。" if self._history_load_pending else
            "正在处理启动请求，请稍候；完成后再清空图表。" if self._start_pending else
            "当前实验正在收尾并保存，完成后可清空图表；原始 CSV 会保留。"
            if self._experiment_active and not self._acquiring else
            "请先停止并保存当前实验，再清空图表；原始 CSV 会保留。"
        )
        if hasattr(self, "reset_measurement_button"):
            self.reset_measurement_button.setEnabled(
                not self._history_load_pending and not self._start_pending
                and not self._finalization_pending and not self._reset_plot_after_save
                and (not self._experiment_active or self._acquiring)
            )
            self.save_curve_button.setEnabled(
                self._experiment_csv_path is not None
                and self._experiment_csv_path.is_file()
                and self._curve_export_task is None
            )
        self.clear_plot_button.setEnabled(can_clear_plot)
        self.clear_plot_button.setToolTip(clear_plot_tooltip)
        clear_plot_action = getattr(self, "clear_plot_action", None)
        if clear_plot_action is not None:
            clear_plot_action.setEnabled(can_clear_plot)
            clear_plot_action.setToolTip(clear_plot_tooltip)
        self.event_button.setToolTip(
            ("请先开始一次实验，再记录事件。" if not self._experiment_active else "记录带有当前时间、样本号和读数的实验事件。")
            + " 快捷键：Ctrl+Enter。"
        )
        zero_reason = reason or (
            "请先读取或应用当前模式配置，并确认仪器配置回读后再执行 ZERO。"
            if self._connected and computer_control and not zero_ready else ""
        )
        zero_tooltip_parts = [
            "ZERO正在执行，等待仪器结果返回。" if self._zero_pending else zero_reason,
            self.zero_help,
        ]
        zero_tooltip = "\n\n".join(part for part in zero_tooltip_parts if part)
        self.zero_button.setToolTip(zero_tooltip)
        self.diagnose_button.setToolTip(reason or "读取仪器身份、配置与错误队列。")
        actions = getattr(self, "_shortcut_actions", {})
        for object_name, enabled in (
            ("newMeasurementShortcutAction", False),
            ("startShortcutAction", self.start_button.isEnabled()),
            ("singleShortcutAction", self.single_button.isEnabled()),
            ("pauseShortcutAction", self.pause_button.isEnabled()),
            ("stopShortcutAction", self.stop_button.isEnabled()),
            ("eventShortcutAction", False),
        ):
            action = actions.get(object_name)
            if action is not None:
                action.setEnabled(enabled)
        pause_action = actions.get("pauseShortcutAction")
        if pause_action is not None:
            pause_action.setText("继续采集" if self._paused else "暂停采集")
        single_action = actions.get("singleShortcutAction")
        if single_action is not None:
            single_action.setText(self.single_button.text())
            single_action.setToolTip(self.single_button.toolTip())
        return_live_action = getattr(self, "return_live_action", None)
        self._set_return_live_enabled(self.return_live_button.isEnabled())

    def _set_return_live_enabled(self, enabled: bool) -> None:
        """Keep the chart-toolbar button and menu action on the same view state."""
        enabled = bool(enabled)
        self.return_live_button.setEnabled(enabled)
        action = getattr(self, "return_live_action", None)
        if action is not None:
            action.setEnabled(enabled)

    @Slot()
    def _on_sample_ready(self) -> None:
        rows, rate, full_stats = self._worker.take_sample_update(
            include_display_dropped_rows=True,
        )
        self._on_sample(rows, rate, full_stats)

    @Slot(object, float, object)
    def _on_sample(self, rows: list[dict], rate: float, full_stats: dict) -> None:
        try:
            self.actual_rate_label.setText(
                _format_sampling_interval(rate) if rate > 0 else
                "等待第二个有效读数" if int(full_stats.get("valid_count", full_stats.get("count", 0))) == 1 else
                "等待有效读数"
            )
            self._global_stats = full_stats
            valid_count = int(full_stats.get("valid_count", full_stats.get("count", 0)) or 0)
            display_dropped_rows = int(full_stats.get("display_dropped_rows", 0) or 0)
            if display_dropped_rows > 0:
                self.display_loss_warning.setText(
                    "曲线显示丢点，CSV 原始数据完整。"
                    f"累计丢弃 {display_dropped_rows:,} 个曲线显示点。"
                )
                self.display_loss_warning.show()
            else:
                self.display_loss_warning.hide()
            self.valid_sample_count_label.setText(f"有效样本 {valid_count:,}")
            invalid_log_entries: list[tuple[str, str]] = []
            valid_rows: list[dict] = []
            for row in rows:
                try:
                    sample_index = int(row.get("sample_index") or 0)
                    if self._last_sample_index and sample_index > self._last_sample_index + 1:
                        self._plot_break_pending = True
                    self._last_sample_index = max(self._last_sample_index, sample_index)
                except (TypeError, ValueError):
                    pass
                row_status = str(row.get("status") or "无状态")
                invalid_value_reason = ""
                value = elapsed = None
                if row_status == "ok":
                    if row.get("value_si") in (None, ""):
                        invalid_value_reason = "有效状态样本缺少读数"
                    else:
                        try:
                            value = float(row["value_si"])
                            elapsed = float(row["elapsed_s"])
                        except (KeyError, TypeError, ValueError, OverflowError):
                            invalid_value_reason = "读数或经过时间无法解析"
                        else:
                            if not math.isfinite(value) or not math.isfinite(elapsed):
                                invalid_value_reason = "读数或经过时间不是有限数值"
                if row_status != "ok" or invalid_value_reason:
                    self._plot_break_pending = True
                    status = "非有限值" if "不是有限数值" in invalid_value_reason else row_status
                    error = invalid_value_reason or str(row.get("error") or "无详细原因")
                    self._latest_invalid_log_text = f"! 数据无效：{status} · {error}"
                    invalid_detail = (
                        f"最近无效读数 #{row.get('sample_index') or self._last_sample_index}："
                        f"{status} · {error}"
                        f"{' · 已从曲线中排除' if invalid_value_reason else ''}"
                    )
                    self.latest_invalid_label.setToolTip(invalid_detail)
                    self.latest_invalid_label.setText(invalid_detail)
                    self.latest_invalid_log_button.setEnabled(True)
                    self.latest_invalid_label.setVisible(True)
                    self.latest_invalid_log_button.hide()
                    self._update_latest_invalid_summary()
                    if status in {"timeout", "scpi_timeout"}:
                        self._show_recovery_guidance("普通读超时" if status == "timeout" else "SCPI响应超时")
                    log_timestamp = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
                    invalid_log_entries.append(
                        (self._latest_invalid_log_text, f"{log_timestamp}  {self._latest_invalid_log_text}")
                    )
                    continue
                assert value is not None and elapsed is not None
                self._last_value = value
                self._last_elapsed = elapsed
                self._last_actual_range_si = None
                reported_range = row.get("range_si")
                if reported_range not in (None, ""):
                    try:
                        parsed_range = float(reported_range)
                        self._last_actual_range_si = parsed_range if math.isfinite(parsed_range) else None
                    except (TypeError, ValueError, OverflowError):
                        self._last_actual_range_si = None
                self._plot_x.append(elapsed)
                self._plot_y.append(value)
                self._average_trace.append(elapsed, value, self._plot_break_pending)
                if self._plot_y_origin is None:
                    self._plot_y_origin = value
                self._plot_break_before.append(self._plot_break_pending)
                self._plot_break_pending = False
                self._plot_metadata.append({
                    "sample_index": row.get("sample_index"),
                    "raw_reading": row.get("raw_reading", ""),
                    "timestamp_local": row.get("timestamp_local", ""),
                    "timestamp_utc": row.get("timestamp_utc", ""),
                    "unit_si": row.get("unit_si", descriptor(self._current_mode).unit_si),
                    "mode": row.get("mode", self._current_mode.value),
                    "range_si": row.get("range_si"),
                })
                valid_rows.append(row)
            if invalid_log_entries:
                self._store_log_entries(invalid_log_entries)
            if len(self._plot_x) > MAX_PLOT_POINTS:
                compact_limit = max(5, int(MAX_PLOT_POINTS * 0.75))
                plot_x, plot_y, plot_breaks, plot_metadata = downsample_plot_data(
                    self._plot_x,
                    self._plot_y,
                    self._plot_break_before,
                    self._plot_metadata,
                    max_points=compact_limit,
                )
                self._plot_x = deque(plot_x)
                self._plot_y = deque(plot_y)
                self._plot_break_before = deque(plot_breaks)
                self._plot_metadata = deque(plot_metadata)
            if valid_rows:
                self.plot_empty_overlay.hide()
                self.save_curve_button.setEnabled(
                    self._experiment_csv_path is not None and self._experiment_csv_path.is_file()
                    and self._curve_export_task is None
                )
                self._schedule_live_plot_refresh()
                value = float(valid_rows[-1]["value_si"])
                elapsed = float(valid_rows[-1]["elapsed_s"])
                spec = descriptor(Mode(valid_rows[-1]["mode"]))
                displayed, unit = engineering_value(value, spec.unit_si)
                self.latest_value_label.setText(f"{displayed:.10g} {unit}")
                self._update_readout_context()
                if self._baseline is not None:
                    self.baseline_line.setValue(float(self._baseline) - (self._plot_y_origin or 0.0))
                    self.baseline_line.hide()
                    delta = value - self._baseline
                    percent = relative_change_percent(value, self._baseline, self._parse_threshold())
                    d_display, d_unit = engineering_value(delta, spec.unit_si)
                    relative = f"{percent:.8g}%" if percent is not None else "无效（基准接近零或阈值无效）"
                    self.change_label.setText(f"Δ={d_display:.8g} {d_unit} · ΔX/X₀={relative}")
                else:
                    self.baseline_line.hide()
                    self.change_label.setText("未设置基准值")
                self._set_return_live_enabled(True)
            if rows:
                self._update_stats(full_stats)
                if self._region.isVisible():
                    self._on_region_samples(rows)
        except Exception:
            log.exception("绘图更新失败")
            self._append_log("! 绘图刷新失败，采集和CSV保存仍在采集线程中继续")

    def _update_stats(self, full_stats: dict | None = None) -> None:
        stats = full_stats if full_stats is not None else summarize(self._plot_y)
        spec = descriptor(self._display_plot_mode())
        def fmt(value):
            if value is None:
                return "—"
            scaled, unit = engineering_value(value, spec.unit_si)
            return f"{scaled:.8g} {unit}"
        if isinstance(stats, dict):
            count, minimum, maximum, mean, deviation = (stats.get(k) for k in ("count", "minimum", "maximum", "mean", "sample_stddev"))
        else:
            count, minimum, maximum, mean, deviation = stats.count, stats.minimum, stats.maximum, stats.mean, stats.sample_stddev
        telemetry = stats if isinstance(stats, dict) else {}
        attempts = telemetry.get("attempt_count", count)
        valid = telemetry.get("valid_count", count)
        abnormal = telemetry.get("abnormal_count", max(0, int(attempts or 0) - int(valid or 0)))
        median = telemetry.get("interval_median_s")
        minimum_interval = telemetry.get("interval_min_s")
        maximum_interval = telemetry.get("interval_max_s")
        jitter = telemetry.get("interval_range_jitter_s")

        def interval(value):
            if value is None:
                return "等待第二个有效读数" if int(valid or 0) == 1 else "—"
            return _format_sampling_interval(1.0 / value) if value > 0 else "0 s"

        self.stats_label.setText(
            f"统计范围：全程 · 有效点 n={count} · 最大 {fmt(maximum)} (n={count}) · "
            f"最小 {fmt(minimum)} (n={count}) · 平均 {fmt(mean)} (n={count}) · "
            f"样本标准差 s {fmt(deviation)} (n={count})\n"
            f"采集遥测 · 尝试 {attempts} · 有效 {valid} · 异常 {abnormal} · 最近有效间隔中位数 {interval(median)} · "
            f"最小 {interval(minimum_interval)} · 最大 {interval(maximum_interval)} · 范围抖动 {interval(jitter)}"
        )

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
            self._region_stats_pending_overflow = False
            self._region_stats_values = None
            self._region_stats_last_sample_index = 0
            self._region_stats_mode = self._display_plot_mode()
            task = _RegionStatisticsTask(request_id, path, left, right, self._display_plot_mode())
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

    def _sync_region_inputs(self, *_args) -> None:
        left, right = sorted(self._region.getRegion())
        for spin, value in ((self.region_start_spin, left), (self.region_end_spin, right)):
            previous = spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(previous)

    def _on_region_change_finished(self) -> None:
        self._sync_region_inputs()
        self._update_region_stats()

    def _apply_region_inputs(self) -> None:
        left = self.region_start_spin.value()
        right = self.region_end_spin.value()
        if not math.isfinite(left) or not math.isfinite(right) or left >= right:
            self.statusBar().showMessage("统计区间无效：起点必须早于终点。", 5000)
            return
        self._region.setRegion((left, right))
        self._sync_region_inputs()
        if not self.region_check.isChecked():
            self.region_check.setChecked(True)
        else:
            self._update_region_stats()

    def _on_region_visibility_changed(self, visible: bool) -> None:
        self._region.setVisible(visible)
        if visible:
            self._update_region_stats()

    def _on_region_stats_finished(self, request_id: int, result: object, error: object) -> None:
        self._region_stats_tasks.pop(request_id, None)
        if self._shutdown_started:
            self._maybe_complete_shutdown()
            return
        if request_id != self._region_stats_request_id:
            return
        self._region_stats_scanning = False
        if self._region_stats_mode != self._display_plot_mode():
            self._region_stats_pending_rows.clear()
            self._region_stats_pending_overflow = False
            self._region_stats_values = None
            self._update_region_stats()
            return
        if error:
            self._region_stats_pending_rows.clear()
            self._region_stats_pending_overflow = False
            self._region_stats_label.setText(f"原始 CSV 区间统计失败：{error}")
            return
        if not isinstance(result, dict) or not isinstance(result.get("stats"), RunningStats):
            self._region_stats_pending_rows.clear()
            self._region_stats_pending_overflow = False
            self._region_stats_label.setText("原始 CSV 区间统计失败：返回结果无效")
            return

        stats = result["stats"]
        last_sample_index = int(result["last_sample_index"])
        left, right = self._region_stats_bounds or (0.0, 0.0)
        needs_rescan = self._region_stats_pending_overflow or not _region_sample_rows_are_contiguous(
            self._region_stats_pending_rows, last_sample_index
        )
        if needs_rescan:
            self._region_stats_pending_rows.clear()
            self._region_stats_pending_overflow = False
            self._region_stats_values = None
            self._update_region_stats()
            return
        for row in self._region_stats_pending_rows:
            sample_index = _parse_region_sample_index(row.get("sample_index"))
            if sample_index is None:
                self._region_stats_pending_rows.clear()
                self._region_stats_pending_overflow = False
                self._region_stats_values = None
                self._update_region_stats()
                return
            if sample_index <= last_sample_index:
                continue
            last_sample_index = sample_index
            if row.get("status") != "ok" or row.get("mode") != self._display_plot_mode().value:
                continue
            try:
                elapsed, value = _region_successful_row_values(row, sample_index)
            except ValueError:
                self._region_stats_pending_rows.clear()
                self._region_stats_pending_overflow = False
                self._region_stats_values = None
                self._update_region_stats()
                return
            if left <= elapsed <= right:
                stats.add(value)
        self._region_stats_pending_rows.clear()
        self._region_stats_pending_overflow = False
        self._region_stats_values = stats
        self._region_stats_last_sample_index = last_sample_index
        self._show_region_stats(stats, "完整原始 CSV")

    def _on_region_samples(self, rows: list[dict]) -> None:
        if self._region_stats_mode is not None and self._region_stats_mode != self._display_plot_mode():
            self._update_region_stats()
            return
        if self._region_stats_scanning:
            if len(self._region_stats_pending_rows) + len(rows) > MAX_PENDING_GUI_ROWS:
                self._region_stats_pending_overflow = True
            self._region_stats_pending_rows.extend(rows)
            if len(self._region_stats_pending_rows) > MAX_PENDING_GUI_ROWS:
                del self._region_stats_pending_rows[:-MAX_PENDING_GUI_ROWS]
            return
        if self._region_stats_values is None or self._region_stats_bounds is None:
            if self._experiment_csv_path is None:
                self._update_region_stats()
            return
        left, right = self._region_stats_bounds
        if not _region_sample_rows_are_contiguous(rows, self._region_stats_last_sample_index):
            self._region_stats_values = None
            self._update_region_stats()
            return
        changed = False
        for row in rows:
            sample_index = _parse_region_sample_index(row.get("sample_index"))
            if sample_index is None:
                self._region_stats_values = None
                self._update_region_stats()
                return
            if sample_index <= self._region_stats_last_sample_index:
                continue
            self._region_stats_last_sample_index = sample_index
            if row.get("status") != "ok" or row.get("mode") != self._display_plot_mode().value:
                continue
            try:
                elapsed, value = _region_successful_row_values(row, sample_index)
            except ValueError:
                self._region_stats_values = None
                self._update_region_stats()
                return
            if left <= elapsed <= right:
                self._region_stats_values.add(value)
                changed = True
        if changed:
            self._show_region_stats(self._region_stats_values, "完整原始 CSV · 实时更新")

    def _show_region_stats(self, stats, source: str) -> None:
        if self._region_stats_bounds is None:
            left, right = sorted(self._region.getRegion())
        else:
            left, right = self._region_stats_bounds
        spec = descriptor(self._display_plot_mode())

        def fmt(value: float | None) -> str:
            if value is None:
                return "—"
            scaled, unit = engineering_value(value, spec.unit_si)
            return f"{scaled:.8g} {unit}"

        self._region_stats_label.setText(
            f"选中区间 {left:.6g}–{right:.6g} s · n={stats.count} · 最大 {fmt(stats.maximum)} (n={stats.count}) · "
            f"最小 {fmt(stats.minimum)} (n={stats.count}) · 均值 {fmt(stats.mean)} (n={stats.count}) · "
            f"样本标准差 s {fmt(stats.sample_stddev)} (n={stats.count})"
            f" · {source}"
        )

    def _nearest_plot_index(self, x: float) -> int | None:
        try:
            x = float(x)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(x):
            return None
        x_values = self._plot_y_anchor_x
        y_values = self._plot_y_anchor_values
        if x_values.ndim != 1 or y_values.ndim != 1 or len(x_values) != len(y_values) or not len(x_values):
            return None
        valid = np.isfinite(x_values) & np.isfinite(y_values)
        if not np.any(valid):
            return None
        if not self._plot_x_nondecreasing:
            valid_indices = np.flatnonzero(valid)
            return int(valid_indices[np.argmin(np.abs(x_values[valid_indices] - x))])

        left_edge = int(np.searchsorted(x_values, x, side="left"))
        right_edge = int(np.searchsorted(x_values, x, side="right"))
        if left_edge < right_edge:
            exact = np.flatnonzero(valid[left_edge:right_edge])
            if exact.size:
                # Match zoom anchoring: use the last finite reading at a duplicate X.
                return left_edge + int(exact[-1])
        left = left_edge - 1
        while left >= 0 and not valid[left]:
            left -= 1
        right = right_edge
        while right < len(x_values) and not valid[right]:
            right += 1
        if left < 0:
            return right if right < len(x_values) else None
        if right >= len(x_values):
            return left
        return left if x - x_values[left] <= x_values[right] - x else right

    def _describe_plot_point(self, x: float, y: float, sample_number: int, metadata: dict | None = None) -> str:
        row = metadata or {}
        mode = self._display_plot_mode()
        try:
            mode = Mode(str(row.get("mode") or mode.value))
        except ValueError:
            pass
        spec = descriptor(mode)
        shown, unit = engineering_value(y, spec.unit_si)
        sample_index = row.get("sample_index") or sample_number
        raw = str(row.get("raw_reading", "")).strip() or "不可用（历史文件未保存原始响应）"
        local_time = str(row.get("timestamp_local", "")).strip() or "不可用（历史文件未保存本地时间）"
        range_text = ""
        if row.get("range_si") not in (None, ""):
            try:
                range_text = f" · 实际量程 {range_label(float(row['range_si']), spec.unit_si)}"
            except (TypeError, ValueError, OverflowError):
                pass
        return (
            f"样本 #{sample_index} · 原始 {raw} · 换算 {shown:.10g} {unit}{range_text} · "
            f"t={x:.7g} s · 本地时间 {local_time}"
        )

    def _lock_plot_point_at(self, x: float) -> bool:
        idx = self._nearest_plot_index(x)
        if idx is None:
            return False
        xs = list(self._plot_x)
        selected_x = float(xs[idx])
        selected_y = float(self._plot_y[idx])
        metadata = self._plot_metadata[idx] if idx < len(self._plot_metadata) else None
        sample_number = int(metadata.get("sample_index") or idx + 1) if metadata else idx + 1
        self._selected_plot_point = (selected_x, selected_y, sample_number)
        self._selected_plot_metadata = metadata
        self._locked_cursor_marker.setData([selected_x], [selected_y - (self._plot_y_origin or 0.0)])
        self._locked_cursor_marker.show()
        self.cursor_label.setText(
            f"已锁定：{self._describe_plot_point(selected_x, selected_y, sample_number, metadata)} · 单击曲线可重新选点"
        )
        return True

    def _on_plot_clicked(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        vb = self.plot_widget.getPlotItem().vb
        pos = event.scenePos()
        if not vb.sceneBoundingRect().contains(pos):
            return
        if callable(getattr(event, "double", None)) and event.double():
            self._enable_plot_autoscale()
            return

    def _on_mouse_moved(self, pos) -> None:
        """Keep the plot free of hover guides and selection markers."""
        return

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

    def _set_selected_point_as_baseline(self) -> None:
        if self._selected_plot_point is None:
            self._show_error("请先在曲线上单击一个读数点，锁定后再设为基准")
            return
        elapsed, value, sample_number = self._selected_plot_point
        self._baseline = value
        self.baseline_edit.setText(repr(self._baseline))
        self._store_baseline(f"图上锁定点设为基准（t={elapsed:.7g} s，点序号≈{sample_number}）")

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
            self.baseline_line.setValue(float(self._baseline) - (self._plot_y_origin or 0.0))
            self.baseline_line.hide()
        if self._experiment_active:
            if self._baseline is None:
                event_text = f"{event}: 已清除基准值"
            else:
                event_text = f"{event}: {self._baseline:.17g} {descriptor(self._display_plot_mode()).unit_si}"
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
        if self._event_save_pending:
            return
        self._event_save_pending = True
        self._update_controls()
        self._bridge.mark_event.emit(text)

    @Slot(object)
    def _on_event_saved(self, event: dict) -> None:
        """Confirm an event only after the worker has appended and flushed its CSV row."""
        self._event_save_pending = False
        event_text = str(event.get("event", ""))
        if self.event_edit.text().strip() == event_text:
            self.event_edit.clear()
        try:
            elapsed_s = float(event["elapsed_s"])
            sample_index = int(event["sample_index"])
        except (KeyError, TypeError, ValueError):
            self._append_log("! 事件已写入，但回执字段不完整；请检查 events.csv")
            self.statusBar().showMessage("事件已写入；回执字段不完整，请检查 events.csv", 12000)
            self._update_controls()
            return
        timestamp = str(event.get("timestamp_local", "时间未知"))
        reading = event.get("value_si")
        unit = str(event.get("unit_si", ""))
        reading_text = "" if reading in (None, "") else f" · {reading} {unit}".rstrip()
        message = (
            f"事件已保存到 events.csv · {timestamp} · t={elapsed_s:.7g} s"
            f" · 样本 {sample_index}{reading_text} · {event_text}"
        )
        self.statusBar().showMessage(message, 15000)
        self._append_log(message)
        self._add_event_marker(event_text, elapsed_s=elapsed_s)
        self._update_controls()

    def _add_event_marker(
        self, text: str, elapsed_s: float | None = None, *, reposition_labels: bool = True
    ) -> None:
        line = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#8a5d13", width=1, style=Qt.PenStyle.DashLine),
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
        item = QListWidgetItem(f"t={marker_elapsed:.7g} s · {text}")
        item.setData(Qt.ItemDataRole.UserRole, marker_elapsed)
        item.setToolTip("点击或按 Enter 将图表定位到该事件")
        self.event_list_widget.addItem(item)
        if reposition_labels:
            self._position_event_labels()

    def _add_event_markers(self, events: list[tuple[float, str]]) -> None:
        if not events:
            return
        if self._event_markers_loading:
            self._pending_event_markers.extend(events)
            return
        self._cancel_pending_event_marker_load()
        if len(events) <= EVENT_MARKER_ASYNC_THRESHOLD:
            for elapsed_s, text in events:
                self._add_event_marker(text, elapsed_s=elapsed_s, reposition_labels=False)
            self._position_event_labels()
            return
        self._pending_event_markers = list(events)
        self._pending_event_marker_index = 0
        self._event_markers_loading = True
        self._append_event_marker_batch()

    def _append_event_marker_batch(self) -> None:
        if not self._event_markers_loading or self._shutdown_started:
            return
        start = self._pending_event_marker_index
        end = min(start + EVENT_MARKER_BATCH_SIZE, len(self._pending_event_markers))
        plot_updates = self.plot_widget.updatesEnabled()
        list_updates = self.event_list_widget.updatesEnabled()
        self.plot_widget.setUpdatesEnabled(False)
        self.event_list_widget.setUpdatesEnabled(False)
        try:
            for elapsed_s, text in self._pending_event_markers[start:end]:
                self._add_event_marker(text, elapsed_s=elapsed_s, reposition_labels=False)
        finally:
            self.plot_widget.setUpdatesEnabled(plot_updates)
            self.event_list_widget.setUpdatesEnabled(list_updates)
        self._pending_event_marker_index = end
        if end < len(self._pending_event_markers):
            self._event_marker_timer.start()
            return
        self._pending_event_markers.clear()
        self._pending_event_marker_index = 0
        self._event_markers_loading = False
        self._position_event_labels()

    def _cancel_pending_event_marker_load(self) -> None:
        self._event_marker_timer.stop()
        self._pending_event_markers.clear()
        self._pending_event_marker_index = 0
        self._event_markers_loading = False

    def _focus_event_item(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        try:
            elapsed = float(item.data(Qt.ItemDataRole.UserRole))
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(elapsed):
            return
        x_range = self.view_box.viewRange()[0]
        span = max(1.0, float(x_range[1] - x_range[0]))
        self.follow_check.setChecked(False)
        self.view_box.setRange(
            xRange=(elapsed - span / 2, elapsed + span / 2), padding=0
        )

    def _display_plot_mode(self) -> Mode:
        return self._plot_data_mode if self._plot_y and self._plot_data_mode is not None else self._current_mode

    def _sync_plot_y_span_control(self, *_args) -> None:
        edit = getattr(self, "y_axis_span_edit", None)
        if edit is None:
            return
        unit = descriptor(self._display_plot_mode()).unit_si
        self.y_axis_span_unit_label.setText("Ω" if unit == "Ohm" else unit)
        if edit.hasFocus():
            return
        y_min, y_max = self.view_box.viewRange()[1]
        span = float(y_max - y_min)
        if math.isfinite(span) and span > 0:
            edit.setText(f"{span:.8g}")

    def _sync_plot_y_axis(self, *_args) -> None:
        axis = getattr(self, "y_axis", None)
        if isinstance(axis, _OffsetValueAxis):
            axis.value_offset = self._plot_y_origin or 0.0
            y_min, y_max = self.view_box.viewRange()[1]
            # Plot coordinates are deltas from the origin; units follow the
            # absolute reading, not the (often much smaller) noise span.
            midpoint = axis.value_offset + float(y_min) / 2 + float(y_max) / 2
            magnitude = abs(midpoint)
            if not math.isfinite(magnitude) or magnitude == 0:
                magnitude = abs(float(y_max) - float(y_min))
            axis.display_factor, axis.display_unit = _engineering_axis_scale(
                magnitude, descriptor(self._display_plot_mode()).unit_si,
            )
            spec = descriptor(self._display_plot_mode())
            title = spec.display_quantity
            span = float(y_max - y_min)
            if axis.value_offset and math.isfinite(span) and span > 0 and abs(axis.value_offset) > span * 1e6:
                origin_value, origin_unit = engineering_value(axis.value_offset, spec.unit_si)
                title = f"{title} · 刻度相对 {origin_value:.6g} {origin_unit}"
            if self._history_mode:
                title = f"历史数据 · {title}"
            elif self._plot_y and self._display_plot_mode() != self._current_mode:
                title = f"上一轮 · {title}"
            if (
                axis.labelText != title
                or axis.labelUnits != axis.display_unit
            ):
                self.plot_widget.setLabel(
                    "left", title, units=axis.display_unit, color="#697470",
                )
            axis.update()

    def _sync_plot_x_axis(self, *_args) -> None:
        axis = getattr(self, "x_axis", None)
        if not isinstance(axis, _OffsetValueAxis):
            return
        x_min, x_max = self.view_box.viewRange()[0]
        span = abs(float(x_max) - float(x_min))
        factor, unit = (3600.0, "h") if span >= 3600 else (60.0, "min") if span >= 60 else (1.0, "s")
        axis.display_factor, axis.display_unit = factor, unit
        if axis.labelUnits != unit:
            self.plot_widget.setLabel("bottom", "实验经过时间", units=unit, color="#697470")
        axis.update()

    def _apply_plot_y_span(self, *_args) -> None:
        try:
            requested_span = float(self.y_axis_span_edit.text().strip())
        except (TypeError, ValueError, OverflowError):
            self.statusBar().showMessage("请输入大于 0 的有限纵轴跨度", 8000)
            return
        if not math.isfinite(requested_span) or requested_span <= 0:
            self.statusBar().showMessage("请输入大于 0 的有限纵轴跨度", 8000)
            return
        if not self._plot_y:
            self.statusBar().showMessage("请先显示有效测量数据，再设置纵轴跨度", 8000)
            return
        y_min, y_max = self.view_box.viewRange()[1]
        previous_y_range = (float(y_min), float(y_max))
        previous_origin = self._plot_y_origin
        previous_follow = self.follow_check.isChecked()
        center = y_min + (y_max - y_min) / 2
        current_origin = self._plot_y_origin or 0.0
        target_origin = current_origin + center
        if math.isfinite(target_origin):
            self._rebase_plot_y_origin(target_origin)
            center = 0.0
        # Y is plotted as a delta from the first reading, avoiding the ULP
        # floor imposed by large absolute values such as 30 MΩ.
        minimum_span = math.ulp(center) * 2
        span = max(requested_span, minimum_span)
        self._update_plot_data_limits()
        y_limits = self.view_box._effectiveLimits()[1]
        maximum_span = None
        if y_limits[0] is not None and y_limits[1] is not None:
            maximum_span = float(y_limits[1] - y_limits[0])
            if math.isfinite(maximum_span) and maximum_span > 0:
                span = min(span, maximum_span)
                center = min(max(center, y_limits[0] + span / 2), y_limits[1] - span / 2)
        boundary_limited = span < max(requested_span, minimum_span) and maximum_span is not None
        lower, upper = center - span / 2, center + span / 2
        if not math.isfinite(lower) or not math.isfinite(upper) or upper <= lower:
            if previous_origin is not None:
                self._rebase_plot_y_origin(previous_origin)
                self.view_box.setRange(yRange=previous_y_range, padding=0)
            self.statusBar().showMessage("这个纵轴跨度小于图表可表示的范围", 8000)
            return
        self.follow_check.setChecked(False)
        self.view_box.state["targetRange"][1] = [lower, upper]
        self.view_box.updateViewRange(forceY=True)
        actual_y_min, actual_y_max = self.view_box.viewRange()[1]
        actual_span = float(actual_y_max - actual_y_min)
        unit = "Ω" if descriptor(self._display_plot_mode()).unit_si == "Ohm" else descriptor(self._display_plot_mode()).unit_si
        self._sync_plot_y_span_control()
        if not math.isfinite(actual_span) or actual_span <= 0:
            if previous_origin is not None:
                self._rebase_plot_y_origin(previous_origin)
            self.view_box.state["targetRange"][1] = list(previous_y_range)
            self.view_box.updateViewRange(forceY=True)
            self.follow_check.setChecked(previous_follow)
            self._sync_plot_y_span_control()
            self.statusBar().showMessage("图表边界无法应用该跨度，已恢复之前的纵轴视图", 8000)
        elif boundary_limited:
            self.statusBar().showMessage(f"受图表边界限制；当前纵轴实际跨度为 {actual_span:.8g} {unit}", 8000)
        elif span > requested_span:
            self.statusBar().showMessage(f"输入范围低于当前浮点精度；当前纵轴跨度为 {actual_span:.8g} {unit}", 8000)
        elif not math.isclose(actual_span, span, rel_tol=1e-6, abs_tol=minimum_span):
            self.statusBar().showMessage(f"受图表边界限制；当前纵轴实际跨度为 {actual_span:.8g} {unit}", 8000)
        else:
            self.statusBar().showMessage(f"纵轴实际跨度为 {actual_span:.8g} {unit}", 8000)

    def _position_event_labels(self, *_args) -> None:
        if self._event_markers_loading or not self._event_labels:
            return
        view = self.plot_widget.getPlotItem().vb
        _, y_range = view.viewRange()
        y_min, y_max = y_range
        scene_rect = view.sceneBoundingRect()
        units_per_pixel_y = (y_max - y_min) / max(scene_rect.height(), 1.0)
        layout_items: list[tuple[float, float, float, object, bool]] = []
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
            label_right = scene_x if right_aligned else scene_x + label_width
            layout_items.append((label_left, label_right, x_value, label, right_aligned))
        layout_items.sort(key=lambda item: (item[0], item[2]))
        active_rows: list[tuple[float, int]] = []
        available_rows: list[int] = []
        row_count = 0
        for label_left, label_right, x_value, label, right_aligned in layout_items:
            while active_rows and active_rows[0][0] + 5 <= label_left:
                _right_edge, available_row = heapq.heappop(active_rows)
                heapq.heappush(available_rows, available_row)
            if available_rows:
                row = heapq.heappop(available_rows)
            else:
                row = row_count
                row_count += 1
            heapq.heappush(active_rows, (label_right, row))
            y = y_max - (4 + row * 24) * units_per_pixel_y
            label.setPos(x_value, y)

    def _on_experiment_finished(self, directory: str) -> None:
        self._start_pending = False
        self._acquiring = False
        self._single_shot_running = False
        self._paused = False
        self._experiment_active = False
        self._finalization_pending = True
        if self._shutdown_started:
            self.shutdown_status_label.setText("正在确认数据保存结果，并关闭仪器连接与后台工作线程…")
            self.statusBar().showMessage(self.shutdown_status_label.text())
        self._append_log(f"实验文件夹：{directory}")
        self.status_dot.setProperty("state", "idle")
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        self._update_controls()
        if self._new_measurement_after_save:
            self._new_measurement_stop_finished = True
            self._maybe_open_new_measurement_after_save()

    @Slot(str, bool, str)
    def _on_experiment_save_result(self, directory: str, saved_ok: bool, detail: str) -> None:
        self._finalization_pending = False
        self._locked_interval_s = None
        if not saved_ok:
            self._reset_plot_after_save = False
            self._set_operation_status("保存失败", state="error", detail=detail)
            self.statusBar().showMessage(detail, 20000)
            self._user_stop_pending = False
            self._last_run_single = False
            self._clear_pending_new_measurement()
            self._update_controls()
            return
        csv_path = Path(directory) / "measurements.csv"
        if self._last_run_single:
            if self._last_value is None:
                result = "未收到有效读数"
            else:
                displayed, unit = engineering_value(self._last_value, descriptor(self._current_mode).unit_si)
                result = f"{displayed:.10g} {unit}"
            feedback = f"单次测量完成 · {result} · 已保存：{csv_path}"
        elif self._user_stop_pending:
            attempts = int(self._global_stats.get("attempt_count", 0) or 0)
            valid = int(self._global_stats.get("valid_count", self._global_stats.get("count", 0)) or 0)
            abnormal = int(self._global_stats.get("abnormal_count", max(0, attempts - valid)) or 0)
            feedback = f"停止并保存完成 · {attempts} 次 · 有效 {valid} · 异常 {abnormal} · {csv_path}"
        elif self._last_acquisition_issue:
            attempts = int(self._global_stats.get("attempt_count", 0) or 0)
            valid = int(self._global_stats.get("valid_count", self._global_stats.get("count", 0)) or 0)
            abnormal = int(self._global_stats.get("abnormal_count", max(0, attempts - valid)) or 0)
            feedback = f"异常停止，数据已保存 · 尝试 {attempts} · 有效 {valid} · 异常 {abnormal} · {csv_path}"
        else:
            feedback = f"实验数据已保存：{csv_path}"
        summary = feedback
        if feedback.startswith("停止并保存完成"):
            attempts = int(self._global_stats.get("attempt_count", 0) or 0)
            valid = int(self._global_stats.get("valid_count", self._global_stats.get("count", 0)) or 0)
            abnormal = int(self._global_stats.get("abnormal_count", max(0, attempts - valid)) or 0)
            summary = f"停止并保存完成 · 尝试 {attempts} / 有效 {valid} / 异常 {abnormal}"
        elif feedback.startswith("单次测量完成"):
            summary = feedback.split(" · 已保存：", 1)[0]
        self._set_operation_status(summary, state="success", detail=feedback)
        self.statusBar().showMessage(feedback, 20000)
        self._user_stop_pending = False
        self._last_run_single = False
        if self._new_measurement_after_save:
            self._new_measurement_save_succeeded = True
            self._new_measurement_save_directory = directory
        if self._reset_plot_after_save:
            self._reset_plot_after_save = False
            self._reset_measurement_plot()
        self._update_controls()
        self._maybe_open_new_measurement_after_save()

    def _clear_pending_new_measurement(self) -> None:
        self._new_measurement_after_save = False
        self._new_measurement_stop_finished = False
        self._new_measurement_save_succeeded = False
        self._new_measurement_save_directory = ""

    def _maybe_open_new_measurement_after_save(self) -> None:
        if not self._new_measurement_after_save:
            return
        if self._shutdown_started:
            self._clear_pending_new_measurement()
            return
        if not self._new_measurement_stop_finished or not self._new_measurement_save_succeeded:
            return
        self._clear_pending_new_measurement()
        self._open_new_measurement_setup()

    @Slot(str)
    def _on_experiment_started(self, directory: str) -> None:
        # Keep the current chart visible until the worker confirms it has
        # configured the instrument and created this run's recorder. If startup
        # fails, _show_error clears the pending state while preserving the old
        # plot and its saved CSV reference.
        self._clear_plot()
        self._plot_data_mode = self._current_mode
        for mode in Mode:
            self._profiles[mode.value]["baseline"] = None
        self._baseline = None
        self.baseline_edit.clear()
        self.baseline_line.hide()
        self.change_label.setText("未设置基准值")
        self.view_box.setLimits(xMin=None, xMax=None, minXRange=None, maxXRange=None)
        self.region_start_spin.setValue(0.0)
        self.region_end_spin.setValue(60.0)
        self._region.setRegion((0.0, 60.0))
        self._save_profiles()
        self._update_plot_data_limits()
        self._start_pending = False
        self._user_stop_pending = False
        self._last_acquisition_issue = ""
        self.recovery_hint_label.clear()
        self.recovery_hint_label.setToolTip("")
        self.recovery_hint_label.hide()
        self._experiment_csv_path = Path(directory) / "measurements.csv"
        self._update_controls()
        if self._region.isVisible():
            self._update_region_stats()

    def _choose_output_root(self) -> None:
        current = self.output_root.text()
        folder = QFileDialog.getExistingDirectory(self, "选择实验保存根目录", current)
        if folder:
            self.output_root.setText(folder)
            self._save_profiles()

    def _validate_experiment_name_field(self, *_args) -> bool:
        try:
            validate_experiment_name(self.experiment_name.text())
        except ValueError as exc:
            self._experiment_name_valid = False
            self.experiment_name_validation.setText(str(exc))
            self.experiment_name_validation.setStyleSheet("color: #b42318;")
        else:
            self._experiment_name_valid = True
            self.experiment_name_validation.setText("名称有效；如有同名实验，可选择先备份再覆盖、另存目录或改名。")
            self.experiment_name_validation.setStyleSheet("")
        if hasattr(self, "start_button"):
            self._update_controls()
        return self._experiment_name_valid

    def _experiment_directory(self) -> Path | None:
        csv_path = self._experiment_csv_path
        if csv_path is None:
            return None
        directory = csv_path if csv_path.is_dir() else csv_path.parent
        return directory if directory.is_dir() else None

    def _export_safe_events_csv(self) -> None:
        if self._safe_events_export_task is not None:
            self.statusBar().showMessage("事件安全副本正在后台生成，请稍候。", 8000)
            return
        if self._experiment_active:
            QMessageBox.information(self, "请先停止采集", "停止采集后再导出事件副本，原始 events.csv 会保持不变。")
            return
        directory = self._experiment_directory()
        source = None if directory is None else directory / "events.csv"
        if source is None or not source.is_file():
            self.statusBar().showMessage("没有可导出的实验事件 CSV。", 8000)
            return
        suggestion = source.with_name(f"{source.stem}_excel_safe.csv")
        destination_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "导出事件表格软件安全副本", str(suggestion), "CSV 文件 (*.csv)"
        )
        if not destination_text:
            return
        destination = Path(destination_text)
        if destination.suffix.casefold() != ".csv":
            destination = destination.with_suffix(".csv")
        try:
            task = _SafeEventsExportTask(source, destination)
            task.signals.completed.connect(self._on_safe_events_exported)
            self._safe_events_export_task = task
            self._update_experiment_directory_controls()
            self.statusBar().showMessage("正在后台生成事件安全副本；原始 events.csv 保持不变。")
            QThreadPool.globalInstance().start(task)
        except Exception as exc:
            self._safe_events_export_task = None
            self._update_experiment_directory_controls()
            self._show_error(f"事件安全副本导出失败：{exc}")

    @Slot(str, object)
    def _on_safe_events_exported(self, destination: str, error: object) -> None:
        self._safe_events_export_task = None
        if self._shutdown_started:
            self._record_shutdown_file_task_result("事件安全副本", error)
            return
        directory = self._experiment_directory()
        self.export_events_safe_button.setEnabled(
            directory is not None and not self._experiment_active
            and self._safe_events_export_task is None
        )
        self.files_safe_events_button.setEnabled(
            directory is not None and not self._experiment_active
            and self._safe_events_export_task is None
            and (directory / "events.csv").is_file()
        )
        self._update_export_action_tooltips(directory)
        if error:
            self._show_error(f"事件安全副本导出失败：{error}")
            return
        self.statusBar().showMessage(
            f"已导出事件安全副本；原始 events.csv 未修改：{destination}", 10000
        )
        self._record_file_export_result("事件安全副本", destination)
        self._refresh_file_workspace()

    def _export_safe_measurements_csv(self) -> None:
        if self._experiment_active:
            QMessageBox.information(
                self, "请先停止采集",
                "停止采集后再导出测量副本，原始 measurements.csv 会保持不变。",
            )
            return
        directory = self._experiment_directory()
        source = None if directory is None else directory / "measurements.csv"
        if source is None or not source.is_file():
            self.statusBar().showMessage("没有可导出的实验测量 CSV。", 8000)
            return
        suggestion = source.with_name(f"{source.stem}_excel_safe.csv")
        destination_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "导出测量表格软件安全副本", str(suggestion), "CSV 文件 (*.csv)"
        )
        if not destination_text:
            return
        destination = Path(destination_text)
        if destination.suffix.casefold() != ".csv":
            destination = destination.with_suffix(".csv")
        try:
            task = _SafeMeasurementsExportTask(source, destination)
            task.signals.completed.connect(self._on_safe_measurements_exported)
            self._safe_measurements_export_task = task
            self.files_safe_measurements_button.setEnabled(False)
            self._update_export_action_tooltips(directory)
            self.statusBar().showMessage(
                "正在后台生成测量安全副本；原始 measurements.csv 保持不变。"
            )
            QThreadPool.globalInstance().start(task)
        except Exception as exc:
            self._show_error(f"测量安全副本导出失败：{exc}")

    @Slot(str, object)
    def _on_safe_measurements_exported(self, destination: str, error: object) -> None:
        self._safe_measurements_export_task = None
        if self._shutdown_started:
            self._record_shutdown_file_task_result("测量安全副本", error)
            return
        directory = self._experiment_directory()
        self.files_safe_measurements_button.setEnabled(
            directory is not None and not self._experiment_active
            and self._safe_measurements_export_task is None
            and (directory / "measurements.csv").is_file()
        )
        self._update_export_action_tooltips(directory)
        if error:
            self._show_error(f"测量安全副本导出失败：{error}")
            return
        self.statusBar().showMessage(
            f"已导出测量安全副本；原始 measurements.csv 未修改：{destination}", 10000
        )
        self._record_file_export_result("测量安全副本", destination)
        self._refresh_file_workspace()

    def _export_share_package(self) -> None:
        if self._experiment_active:
            QMessageBox.information(
                self, "\u8bf7\u5148\u505c\u6b62\u91c7\u96c6",
                "\u5206\u4eab\u5305\u9700\u8981\u7a33\u5b9a\u7684\u5b9e\u9a8c\u6587\u4ef6\u3002\u505c\u6b62\u91c7\u96c6\u540e\u518d\u5bfc\u51fa\uff1b\u5206\u4eab\u5305\u662f\u5355\u72ec\u7684\u8131\u654f\u526f\u672c\uff0c\u539f\u59cb\u6570\u636e\u4e0d\u4f1a\u4fee\u6539\u3002",
            )
            return
        directory = self._experiment_directory()
        if directory is None or not (directory / "measurements.csv").is_file() or not (directory / "config.json").is_file():
            self.statusBar().showMessage("\u7f3a\u5c11 config.json \u6216 measurements.csv\uff0c\u65e0\u6cd5\u751f\u6210\u5b9e\u9a8c\u5206\u4eab\u5305\u3002", 8000)
            return
        suggestion = directory.with_name(f"{directory.name}_share.zip")
        destination_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "\u5bfc\u51fa\u8131\u654f\u5b9e\u9a8c\u5206\u4eab\u5305", str(suggestion), "ZIP \u6587\u4ef6 (*.zip)"
        )
        if not destination_text:
            return
        destination = Path(destination_text)
        if destination.suffix.casefold() != ".zip":
            destination = destination.with_suffix(".zip")
        task = _SharePackageExportTask(directory, destination)
        task.signals.completed.connect(self._on_share_package_exported)
        self._share_export_task = task
        self.files_share_package_button.setEnabled(False)
        self._update_export_action_tooltips(directory)
        self.statusBar().showMessage("\u6b63\u5728\u540e\u53f0\u751f\u6210\u8131\u654f\u5206\u4eab\u5305\uff1b\u539f\u59cb\u5b9e\u9a8c\u6587\u4ef6\u4fdd\u6301\u4e0d\u53d8\u3002")
        QThreadPool.globalInstance().start(task)

    @Slot(str, object)
    def _on_share_package_exported(self, destination: str, error: object) -> None:
        self._share_export_task = None
        if self._shutdown_started:
            self._record_shutdown_file_task_result("实验分享包", error)
            return
        directory = self._experiment_directory()
        self.files_share_package_button.setEnabled(
            directory is not None and not self._experiment_active
            and self._share_export_task is None
            and (directory / "measurements.csv").is_file()
            and (directory / "config.json").is_file()
        )
        self._update_export_action_tooltips(directory)
        if error:
            self._show_error(f"\u8131\u654f\u5206\u4eab\u5305\u5bfc\u51fa\u5931\u8d25\uff1a{error}")
            return
        self.statusBar().showMessage(
            f"\u5df2\u751f\u6210\u8131\u654f\u5206\u4eab\u5305\uff1b\u539f\u59cb\u5b9e\u9a8c\u6587\u4ef6\u672a\u4fee\u6539\uff1a{destination}", 12000
        )
        self._record_file_export_result("脱敏分享包", destination)
        self._refresh_file_workspace()

    def _update_experiment_directory_controls(self) -> None:
        directory = self._experiment_directory()
        if hasattr(self, "export_events_safe_button"):
            self.export_events_safe_button.setEnabled(
                directory is not None and not self._experiment_active
                and self._safe_events_export_task is None
            )
        if hasattr(self, "files_safe_events_button"):
            self.files_safe_events_button.setEnabled(
                directory is not None and not self._experiment_active
                and self._safe_events_export_task is None
                and (directory / "events.csv").is_file()
            )
        if hasattr(self, "files_safe_measurements_button"):
            self.files_safe_measurements_button.setEnabled(
                directory is not None and not self._experiment_active
                and self._safe_measurements_export_task is None
                and (directory / "measurements.csv").is_file()
            )
        if hasattr(self, "files_share_package_button"):
            self.files_share_package_button.setEnabled(
                directory is not None and not self._experiment_active
                and self._share_export_task is None
                and (directory / "measurements.csv").is_file()
                and (directory / "config.json").is_file()
            )
        available = directory is not None
        self.recent_experiment_path.setText(str(directory) if available else "尚无可用实验目录")
        if available:
            if (directory / "CSV_打开说明.txt").is_file():
                notice_summary = "CSV_打开说明.txt"
            else:
                notice_summary = "CSV 安全说明未生成；请先导出 events_excel_safe.csv 再用表格软件打开事件数据"
            self.recent_experiment_files_label.setText(
                "当前实验：measurements.csv · events.csv · config.json · run.log · "
                f"{notice_summary}；曲线 PNG 按需另存。"
            )
        else:
            self.recent_experiment_files_label.setText(
                "尚无实验数据；开始采集后软件会尝试生成 CSV_打开说明.txt。曲线 PNG 按需另存。"
            )
        self.open_experiment_directory_button.setEnabled(available)
        self.copy_experiment_path_button.setEnabled(available)
        self._update_export_action_tooltips(directory)

    def _update_export_action_tooltips(self, directory: Path | None = None) -> None:
        if directory is None:
            directory = self._experiment_directory()
        active = self._experiment_active
        snapshot_path = self._experiment_csv_path
        snapshot_ready = bool(snapshot_path is not None and snapshot_path.is_file())
        events_ready = bool(directory is not None and (directory / "events.csv").is_file())
        measurements_ready = bool(
            directory is not None and (directory / "measurements.csv").is_file()
        )
        config_ready = bool(directory is not None and (directory / "config.json").is_file())

        snapshot_help = (
            "采集中导出原始 CSV 的完整记录快照，不修改正在写入的原文件；快照时间保存在配套 JSON 中。"
        )
        events_help = (
            "若当前实验目录有 events.csv，可生成表格软件安全副本；原始文件保持不变。"
        )
        measurements_help = (
            "仅处理原始响应和错误文本，生成表格软件安全副本；原始 measurements.csv 保持不变。"
        )
        share_help = (
            "生成脱敏分享包；隐藏序列号、COM 口及本机路径，原始实验文件不修改。"
        )

        snapshot_reason = (
            "正在后台生成 CSV 快照。" if self._snapshot_pending else
            "当前没有正在进行的实验可导出快照。" if not active else
            "采集 CSV 尚未创建或暂不可读取；等到文件写出后再导出快照。" if not snapshot_ready else
            ""
        )
        export_reason = (
            "请先停止并保存当前采集，再导出安全副本或分享包。" if active else
            "尚无实验目录；完成并保存一次实验后即可导出。" if directory is None else
            ""
        )
        events_reason = export_reason or (
            "正在后台生成事件安全副本。" if self._safe_events_export_task is not None else ""
        )
        files_events_reason = export_reason or (
            "正在后台生成事件安全副本。" if self._safe_events_export_task is not None else
            "当前实验目录没有 events.csv，无法导出事件安全副本。" if not events_ready else ""
        )
        measurements_reason = export_reason or (
            "正在后台生成测量安全副本。" if self._safe_measurements_export_task is not None else
            "当前实验目录没有 measurements.csv，无法导出测量安全副本。"
            if not measurements_ready else ""
        )
        share_reason = export_reason or (
            "正在后台生成脱敏分享包。" if self._share_export_task is not None else
            "分享包需要 measurements.csv 和 config.json；当前实验尚缺少所需文件。"
            if not measurements_ready or not config_ready else ""
        )

        tooltip_by_kind = {
            "snapshot": (snapshot_reason, snapshot_help),
            "events": (events_reason, events_help),
            "files_events": (files_events_reason, events_help),
            "measurements": (measurements_reason, measurements_help),
            "share": (share_reason, share_help),
        }
        for attribute, kind in (
            ("export_snapshot_button", "snapshot"), ("files_snapshot_button", "snapshot"),
            ("export_events_safe_button", "events"), ("files_safe_events_button", "files_events"),
            ("files_safe_measurements_button", "measurements"),
            ("files_share_package_button", "share"),
        ):
            button = getattr(self, attribute, None)
            if button is None:
                continue
            reason, help_text = tooltip_by_kind[kind]
            button.setToolTip(f"{reason}\n\n{help_text}" if reason else help_text)

    def _refresh_file_workspace(self) -> None:
        if not hasattr(self, "experiment_file_list"):
            return
        directory = self._experiment_directory()
        self.experiment_file_list.clear()
        available = directory is not None
        self.files_directory_label.setText(str(directory) if available else "尚无实验目录")
        for button in (
            self.files_open_directory_button, self.files_copy_path_button,
            self.files_load_history_button,
        ):
            button.setEnabled(available)
        self.files_snapshot_button.setEnabled(
            self._experiment_active and self._experiment_csv_path is not None
            and self._experiment_csv_path.is_file()
        )
        self.files_safe_events_button.setEnabled(
            available and not self._experiment_active and self._safe_events_export_task is None
            and (directory / "events.csv").is_file()
        )
        self.files_safe_measurements_button.setEnabled(
            available and not self._experiment_active and self._safe_measurements_export_task is None
            and (directory / "measurements.csv").is_file()
        )
        self.files_share_package_button.setEnabled(
            available and not self._experiment_active and self._share_export_task is None
            and (directory / "measurements.csv").is_file()
            and (directory / "config.json").is_file()
        )
        self._update_export_action_tooltips(directory)
        if directory is None:
            self.experiment_file_list.addItem("尚无实验文件；完成一次测量后会在此列出原始数据和导出结果。")
            return
        try:
            entries = sorted(directory.iterdir(), key=lambda path: (not path.is_dir(), path.name.casefold()))
        except OSError as exc:
            self.experiment_file_list.addItem(f"无法读取实验目录：{exc}")
            return
        if not entries:
            self.experiment_file_list.addItem("当前实验目录为空。")
            return
        for path in entries[:2000]:
            try:
                details = "文件夹" if path.is_dir() else f"{path.stat().st_size:,} 字节"
            except OSError:
                details = "无法读取"
            item = QListWidgetItem(f"{path.name}    ·    {details}")
            item.setToolTip(str(path))
            self.experiment_file_list.addItem(item)
        if len(entries) > 2000:
            self.experiment_file_list.addItem(f"仅显示前 2000 项；目录中共有 {len(entries)} 项。")

    def _open_experiment_directory(self) -> None:
        directory = self._experiment_directory()
        if directory is None:
            self.statusBar().showMessage("尚无可打开的实验目录；开始一次测量后即可使用。", 8000)
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve()))):
            self._show_error(f"无法打开实验目录：{directory}")
            return
        self.statusBar().showMessage(f"已打开实验目录：{directory}", 8000)

    def _copy_experiment_path(self) -> None:
        directory = self._experiment_directory()
        if directory is None:
            self.statusBar().showMessage("尚无可复制的实验目录；开始一次测量后即可使用。", 8000)
            return
        QApplication.clipboard().setText(str(directory.resolve()))
        self.statusBar().showMessage("实验目录路径已复制。", 8000)

    def _export_running_snapshot(self) -> None:
        source = self._experiment_csv_path
        if not self._experiment_active or source is None or not source.is_file():
            self.statusBar().showMessage("当前没有正在写入的实验 CSV。", 8000)
            return
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        suggestion = source.with_name(f"{source.stem}_进行中快照_{stamp}.csv")
        destination_text, _selected_filter = QFileDialog.getSaveFileName(
            self, "导出进行中 CSV 快照", str(suggestion), "CSV 文件 (*.csv)"
        )
        if not destination_text:
            return
        destination = Path(destination_text)
        if destination.suffix.casefold() != ".csv":
            destination = destination.with_suffix(".csv")
        sidecar = Path(str(destination) + ".snapshot.json")
        try:
            _validate_snapshot_destinations(source, destination, sidecar)
        except (OSError, ValueError) as exc:
            self._show_error(f"无法导出快照：{exc}")
            return
        overwrite = destination.exists() or sidecar.exists()
        if overwrite:
            answer = QMessageBox.question(
                self,
                "确认替换快照文件",
                f"目标快照或其元数据已存在：\n{destination}\n\n是否替换这两个导出文件？原始实验 CSV 不会修改。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        snapshot_time = datetime.now().astimezone().isoformat(timespec="milliseconds")
        task = _CsvSnapshotTask(source, destination, snapshot_time, allow_overwrite=overwrite)
        task.signals.completed.connect(self._on_snapshot_exported)
        self._snapshot_pending = True
        self._snapshot_task = task
        self.statusBar().showMessage("正在后台导出完整 CSV 记录快照；原始采集继续运行。")
        self._update_controls()
        QThreadPool.globalInstance().start(task)

    @Slot(str, str, str, int, object)
    def _on_snapshot_exported(
        self, destination: str, sidecar: str, snapshot_time: str, copied_bytes: int, error: str | None,
    ) -> None:
        self._snapshot_pending = False
        self._snapshot_task = None
        if self._shutdown_started:
            self._record_shutdown_file_task_result("实验快照", error)
            return
        self._update_controls()
        if error:
            self._show_error(f"快照导出失败：{error}")
            return
        self.statusBar().showMessage(
            f"进行中快照已导出（{snapshot_time}，{copied_bytes} 字节完整记录）：{destination}；元数据：{sidecar}",
            20000,
        )
        self._record_file_export_result("进行中 CSV 快照", destination)
        self._record_file_export_result("快照时间元数据", sidecar)
        self._refresh_file_workspace()

    def _plot_y_anchor_at_x(self, plot_x: float) -> float | None:
        """Return the linearly interpolated visible curve value at a plot time."""
        if not math.isfinite(plot_x):
            return None
        try:
            x_values, y_values = self.curve.getData()
            if x_values is None or y_values is None:
                return None
            x_values = np.asarray(x_values)
            y_values = np.asarray(y_values)
            if x_values.ndim != 1 or y_values.ndim != 1 or not len(x_values) or len(x_values) != len(y_values):
                return None
            if not self._plot_x_nondecreasing:
                valid = np.isfinite(x_values) & np.isfinite(y_values)
                if not np.any(valid):
                    return None
                valid_indices = np.flatnonzero(valid)
                nearest_offset = int(np.argmin(np.abs(x_values[valid] - plot_x)))
                return float(y_values[valid_indices[nearest_offset]])

            left_edge = int(np.searchsorted(x_values, plot_x, side="left"))
            right_edge = int(np.searchsorted(x_values, plot_x, side="right"))
            if left_edge < right_edge:
                exact_values = y_values[left_edge:right_edge]
                exact_values = exact_values[np.isfinite(exact_values)]
                if exact_values.size:
                    return float(exact_values[-1])

            left = left_edge - 1
            while left >= 0 and not math.isfinite(float(y_values[left])):
                left -= 1
            right = left_edge
            while right < len(y_values) and not math.isfinite(float(y_values[right])):
                right += 1

            if left < 0:
                return float(y_values[right]) if right < len(y_values) else None
            if right >= len(y_values):
                return float(y_values[left])
            left_x = float(x_values[left])
            right_x = float(x_values[right])
            if right == left + 1 and right_x > left_x:
                fraction = min(1.0, max(0.0, (plot_x - left_x) / (right_x - left_x)))
                left_y = float(y_values[left])
                right_y = float(y_values[right])
                return left_y + fraction * (right_y - left_y)

            # A NaN between samples marks a real break in the trace. Anchor to
            # the nearest finite endpoint instead of interpolating across it.
            return float(y_values[left] if plot_x - left_x <= right_x - plot_x else y_values[right])
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    def _plot_y_zoom_anchor_at_x(self, plot_x: float) -> tuple[float, float] | None:
        """Return a nearby recorded sample as (plot coordinate, raw reading)."""
        if not math.isfinite(plot_x):
            return None
        try:
            x_values = self._plot_y_anchor_x
            y_values = self._plot_y_anchor_values
            if x_values.ndim != 1 or y_values.ndim != 1 or len(x_values) != len(y_values) or not len(x_values):
                return None
            if not self._plot_x_nondecreasing:
                valid_indices = np.flatnonzero(np.isfinite(x_values) & np.isfinite(y_values))
                if not valid_indices.size:
                    return None
                index = int(valid_indices[np.argmin(np.abs(x_values[valid_indices] - plot_x))])
            else:
                left_edge = int(np.searchsorted(x_values, plot_x, side="left"))
                right_edge = int(np.searchsorted(x_values, plot_x, side="right"))
                if left_edge < right_edge:
                    exact_indices = np.flatnonzero(np.isfinite(y_values[left_edge:right_edge]))
                    if exact_indices.size:
                        index = left_edge + int(exact_indices[-1])
                    else:
                        index = -1
                else:
                    index = -1
                if index < 0:
                    left = left_edge - 1
                    while left >= 0 and not math.isfinite(float(y_values[left])):
                        left -= 1
                    right = left_edge
                    while right < len(y_values) and not math.isfinite(float(y_values[right])):
                        right += 1
                    if left < 0:
                        index = right if right < len(y_values) else -1
                    elif right >= len(y_values):
                        index = left
                    else:
                        index = left if plot_x - float(x_values[left]) <= float(x_values[right]) - plot_x else right
            if index < 0:
                return None
            absolute = float(y_values[index])
            origin = self._plot_y_origin if self._plot_y_origin is not None else 0.0
            return absolute - origin, absolute
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    def _refresh_live_plot_now(self) -> None:
        self._last_plot_frame_refresh = time.monotonic()
        if not self._plot_x:
            return
        self._refresh_curve()
        self._update_plot_data_limits()
        self._update_chart_auto_ranges(update_x=not self.view_box.drag_in_progress)

    def _schedule_live_plot_refresh(self) -> None:
        elapsed = time.monotonic() - self._last_plot_frame_refresh
        if len(self._plot_x) < 1000 or elapsed >= 0.15:
            self._plot_frame_timer.stop()
            self._refresh_live_plot_now()
        elif not self._plot_frame_timer.isActive():
            self._plot_frame_timer.start(max(1, math.ceil((0.15 - elapsed) * 1000)))

    def _on_average_window_changed(self, count: int) -> None:
        self._average_trace.set_window(count)
        if hasattr(self, "average_curve"):
            self._average_refresh_timer.stop()
            self._refresh_average_now()

    def _refresh_average_now(self) -> None:
        self._average_last_refresh = time.monotonic()
        self.average_curve.setVisible(not self._history_mode)
        if self._history_mode:
            return
        self._refresh_average_curve(
            self._plot_y_origin or 0.0,
            descriptor(self._display_plot_mode()).quantity == "voltage",
        )

    def _schedule_average_refresh(self) -> None:
        elapsed = time.monotonic() - self._average_last_refresh
        if len(self._average_trace.samples) < 1000 or elapsed >= 0.2:
            self._average_refresh_timer.stop()
            self._refresh_average_now()
        elif not self._average_refresh_timer.isActive():
            self._average_refresh_timer.start(max(1, math.ceil((0.2 - elapsed) * 1000)))

    def _refresh_average_curve(self, origin: float, is_voltage: bool) -> None:
        samples = self._average_trace.samples
        if not samples:
            self.average_curve.setData([], [])
            return
        xs = [sample[0] for sample in samples]
        gaps = [sample[2] for sample in samples]
        ys = list(self._average_trace.values)
        if len(xs) > MAX_LIVE_RENDER_POINTS:
            xs, ys, gaps, _ = downsample_plot_data(
                xs, ys, gaps, [{}] * len(xs), max_points=MAX_LIVE_RENDER_POINTS,
            )
        draw_x, draw_y = [], []
        for index, (x, y, gap) in enumerate(zip(xs, ys, gaps)):
            if index and gap:
                draw_x.append((float(xs[index - 1]) + float(x)) / 2)
                draw_y.append(math.nan)
            draw_x.append(x)
            draw_y.append(
                math.nan if is_voltage and not _voltage_plot_value_is_visible(y) else y - origin
            )
        self.average_curve.setData(draw_x, draw_y, connect="finite", clipToView=True)

    def _refresh_curve(self) -> None:
        is_voltage = descriptor(self._display_plot_mode()).quantity == "voltage"
        if is_voltage:
            limit = VOLTAGE_PLOT_ABSOLUTE_LIMIT_V
            current_origin = self._plot_y_origin
            if current_origin is None or not math.isfinite(current_origin) or abs(current_origin) > limit:
                self._plot_y_origin = next(
                    (
                        float(value) for value in self._plot_y
                        if math.isfinite(float(value)) and abs(float(value)) <= limit
                    ),
                    0.0,
                )
        elif self._plot_y_origin is None:
            self._plot_y_origin = next(
                (float(value) for value in self._plot_y if math.isfinite(float(value))),
                None,
            )
        origin = self._plot_y_origin if self._plot_y_origin is not None else 0.0
        self.view_box.zero_y = -origin
        self.view_box.y_anchor_tolerance = math.ulp(origin)
        if hasattr(self, "y_axis"):
            self.y_axis.value_offset = origin
            self.y_axis.setToolTip(
                "纵轴使用相对坐标保持高读数下的精细缩放；极窄跨度时刻度显示为参考读数 + 偏移量。"
            )
        x_array = np.asarray(self._plot_x, dtype=float)
        plot_count = min(len(x_array), len(self._plot_y))
        measured_x = x_array[:plot_count]
        x_steps = np.diff(measured_x)
        self._plot_x_nondecreasing = bool(
            np.all(np.isfinite(measured_x)) and np.all(x_steps >= 0.0)
        )
        break_values = list(self._plot_break_before)
        break_values = break_values[:plot_count]
        if len(break_values) < plot_count:
            break_values.extend([False] * (plot_count - len(break_values)))
        has_any_break = any(break_values)
        self._plot_x_uniformly_spaced = bool(
            self._plot_x_nondecreasing
            and np.all(x_steps > 0.0)
            and not has_any_break
            and (
                len(x_steps) < 2
                or np.all(np.isclose(x_steps, x_steps[0], rtol=1e-3, atol=1e-9))
            )
        )

        render_x = self._plot_x
        render_y = self._plot_y
        render_breaks = break_values
        render_was_downsampled = (
            plot_count + sum(bool(value) for value in break_values[1:])
            > MAX_LIVE_RENDER_POINTS
        )
        if render_was_downsampled:
            # Older synthetic callers and some partial history states may not
            # carry cursor metadata for every point. Fill only those gaps so
            # the shared peak-preserving helper can still bound the display.
            source_x = list(self._plot_x)[:plot_count]
            source_y = list(self._plot_y)[:plot_count]
            source_breaks = break_values
            source_metadata = list(self._plot_metadata)[:plot_count]
            if len(source_metadata) != plot_count:
                source_metadata.extend([{}] * (plot_count - len(source_metadata)))

            render_limit = MAX_LIVE_RENDER_POINTS
            for _attempt in range(16):
                render_x, render_y, render_breaks, _render_metadata = downsample_plot_data(
                    source_x,
                    source_y,
                    source_breaks,
                    source_metadata,
                    max_points=render_limit,
                )
                rendered_count = len(render_x) + sum(bool(value) for value in render_breaks[1:])
                if rendered_count <= MAX_LIVE_RENDER_POINTS or render_limit <= 5:
                    break
                render_limit = max(5, render_limit - (rendered_count - MAX_LIVE_RENDER_POINTS))

            if rendered_count > MAX_LIVE_RENDER_POINTS:
                # A different point target can map to the same bucket size.
                # Remove non-break samples first, then carry any omitted gaps
                # forward so this final pass always enforces the display cap.
                non_extrema: list[int] = []
                extrema: list[int] = []
                for index in range(1, len(render_x) - 1):
                    if render_breaks[index]:
                        continue
                    previous_y = float(render_y[index - 1])
                    current_y = float(render_y[index])
                    next_y = float(render_y[index + 1])
                    is_extremum = (
                        math.isfinite(previous_y)
                        and math.isfinite(current_y)
                        and math.isfinite(next_y)
                        and (
                            (current_y < previous_y and current_y < next_y)
                            or (current_y > previous_y and current_y > next_y)
                        )
                    )
                    (extrema if is_extremum else non_extrema).append(index)
                removable = non_extrema + extrema
                excess = rendered_count - MAX_LIVE_RENDER_POINTS
                if len(removable) >= excess:
                    remove_positions = np.linspace(
                        0, len(removable) - 1, num=excess, dtype=int,
                    )
                    removed = {removable[int(position)] for position in remove_positions}
                    kept_indices = [
                        index for index in range(len(render_x)) if index not in removed
                    ]
                else:
                    # If nearly every retained point starts a new segment,
                    # preserve endpoints and spread the remaining display
                    # budget across the trace. The raw cache remains intact.
                    point_budget = max(1, (MAX_LIVE_RENDER_POINTS + 1) // 2)
                    kept_indices = np.linspace(
                        0, len(render_x) - 1,
                        num=min(point_budget, len(render_x)), dtype=int,
                    ).tolist()

                bounded_x: list[float] = []
                bounded_y: list[float] = []
                bounded_breaks: list[bool] = []
                previous = -1
                for index in kept_indices:
                    bounded_x.append(float(render_x[index]))
                    bounded_y.append(float(render_y[index]))
                    bounded_breaks.append(any(render_breaks[previous + 1:index + 1]))
                    previous = index
                render_x, render_y, render_breaks = bounded_x, bounded_y, bounded_breaks

        x_values: list[float] = []
        y_values: list[float] = []
        for index, (x, y) in enumerate(zip(render_x, render_y)):
            if index and index < len(render_breaks) and render_breaks[index]:
                # Keep X nondecreasing for pyqtgraph's clipToView bisect, and
                # retain an explicit NaN so the trace never bridges a gap.
                x_values.append(float(x))
                y_values.append(math.nan)
            x_values.append(float(x))
            raw_value = float(y)
            if is_voltage and (not math.isfinite(raw_value) or abs(raw_value) > VOLTAGE_PLOT_ABSOLUTE_LIMIT_V):
                y_values.append(math.nan)
            else:
                y_values.append(raw_value - origin)
        # PyQtGraph's clipToView bisects X, while autoDownsample assumes an
        # evenly spaced X sequence. Live display points are already decimated
        # above the render budget, while full-resolution samples remain in the
        # plot cache and are still written to the raw CSV by the worker.
        self.curve.setData(
            x_values,
            y_values,
            connect="finite",
            autoDownsample=self._plot_x_uniformly_spaced and not render_was_downsampled,
            downsampleMethod="peak",
            clipToView=self._plot_x_nondecreasing,
        )
        self._schedule_average_refresh()
        self._plot_y_anchor_x = x_array
        anchor_values = np.asarray(self._plot_y, dtype=float)
        has_out_of_range_voltage = False
        if is_voltage:
            out_of_range = np.isfinite(anchor_values) & (np.abs(anchor_values) > VOLTAGE_PLOT_ABSOLUTE_LIMIT_V)
            has_out_of_range_voltage = bool(np.any(out_of_range))
            anchor_values[out_of_range] = np.nan
        self._plot_y_anchor_values = anchor_values
        if hasattr(self, "voltage_range_warning"):
            if has_out_of_range_voltage:
                self.voltage_range_warning.setText(
                    "超出 −1000 至 +1000 V 的曲线点已隐藏；原始读数和 CSV 保留。"
                )
                self.voltage_range_warning.show()
            else:
                self.voltage_range_warning.hide()
        if self._baseline is not None:
            self.baseline_line.setValue(float(self._baseline) - origin)
            self.baseline_line.hide()
        if self._selected_plot_point is not None:
            selected_x, selected_y, _sample_number = self._selected_plot_point
            self._locked_cursor_marker.setData([selected_x], [selected_y - origin])
        if self._crosshair_y is not None and self._plot_y:
            self._crosshair_y.setPos(float(self._plot_y[-1]) - origin)
        self.y_axis.update()

    def _rebase_plot_for_zoom(self, center_y: float, absolute_anchor=None) -> None:
        old_origin = self._plot_y_origin or 0.0
        try:
            absolute_anchor = float(absolute_anchor)
        except (TypeError, ValueError, OverflowError):
            absolute_anchor = math.nan
        new_origin = absolute_anchor if math.isfinite(absolute_anchor) else old_origin + float(center_y)
        if math.isfinite(new_origin):
            self._rebase_plot_y_origin(new_origin)
        actual_origin = self._plot_y_origin or 0.0
        self.view_box._last_zoom_rebase_delta = actual_origin - old_origin
        self.view_box._last_zoom_rebase_changed = self.view_box._last_zoom_rebase_delta != 0.0

    def _rebase_plot_y_origin(self, new_origin: float) -> None:
        """Move the plot coordinate origin while preserving the visible range."""
        if not math.isfinite(new_origin):
            return
        if (
            descriptor(self._display_plot_mode()).quantity == "voltage"
            and abs(new_origin) > VOLTAGE_PLOT_ABSOLUTE_LIMIT_V
        ):
            return
        old_origin = self._plot_y_origin or 0.0
        if new_origin == old_origin:
            return
        try:
            low, high = (float(value) for value in self.view_box.viewRange()[1])
        except (AttributeError, TypeError, ValueError, IndexError, OverflowError):
            low = high = 0.0
        shift = old_origin - new_origin
        self._plot_y_origin = new_origin
        self._refresh_curve()
        self._update_plot_data_limits()
        if math.isfinite(low + shift) and math.isfinite(high + shift) and high + shift > low + shift:
            self.view_box.setRange(yRange=(low + shift, high + shift), padding=0)

    def _schedule_history_detail_reload(self) -> None:
        timer = getattr(self, "_history_range_timer", None)
        if (
            self._history_mode and self._history_overview_data is not None
            and self._experiment_csv_path is not None and timer is not None
        ):
            timer.start()

    def _show_history_overview(self) -> None:
        overview = self._history_overview_data
        if overview is None:
            return
        timer = getattr(self, "_history_range_timer", None)
        if timer is not None:
            timer.stop()
        task = self._history_range_task
        if task is not None:
            task.cancel()
        self._history_range_sequence += 1
        self._history_range_pending = False
        self._history_range_task = None
        self._history_detail_range = None
        self._history_detail_is_downsampled = False
        self._plot_x.clear()
        self._plot_y.clear()
        self._plot_metadata.clear()
        self._plot_break_before.clear()
        self._plot_x.extend(overview["x"])
        self._plot_y.extend(overview["y"])
        self._plot_metadata.extend(overview["metadata"])
        self._plot_break_before.extend(overview["break_before"])
        self._selected_plot_point = None
        self._selected_plot_metadata = None
        self._locked_cursor_marker.hide()
        self._refresh_curve()

    def _load_history_detail_for_view(self) -> None:
        if not self._history_mode or self._history_overview_data is None or self._experiment_csv_path is None:
            return
        try:
            x_bounds = self.view_box.viewRange()[0]
            left, right = float(x_bounds[0]), float(x_bounds[1])
        except (AttributeError, TypeError, ValueError, IndexError, OverflowError):
            return
        if not (math.isfinite(left) and math.isfinite(right)) or right <= left:
            return
        overview = self._history_overview_data
        full_left, full_right = float(overview["x"][0]), float(overview["x"][-1])
        full_span = max(full_right - full_left, 1e-12)
        requested_span = right - left
        if requested_span >= full_span * 0.85:
            self._show_history_overview()
            return

        detail_range = self._history_detail_range
        if (
            detail_range is not None and not self._history_detail_is_downsampled
            and detail_range[0] <= left and detail_range[1] >= right
        ):
            return
        left = max(full_left, left - requested_span * 0.05)
        right = min(full_right, right + requested_span * 0.05)
        previous = self._history_range_task
        if previous is not None:
            previous.cancel()
        self._history_range_sequence += 1
        request_id = self._history_range_sequence
        self._history_range_requested = (left, right)
        task = _HistoryRangeLoadTask(
            request_id, self._experiment_csv_path, left, right, MAX_PLOT_POINTS,
            range_index=overview.get("_range_index"),
        )
        task.signals.completed.connect(self._on_history_detail_loaded)
        self._history_range_task = task
        self._active_history_range_tasks[request_id] = task
        self._history_range_pending = True
        self.chart_source_label.setText("历史文件 · 正在读取当前时间窗细节")
        self.chart_source_label.setToolTip(
            f"正在后台扫描 {self._experiment_csv_path}；完整运行概览和原始 CSV 均保留。"
        )
        QThreadPool.globalInstance().start(task)

    @Slot(int, str, object, object)
    def _on_history_detail_loaded(self, request_id: int, path: str, result: object, error: object) -> None:
        self._active_history_range_tasks.pop(request_id, None)
        current_task = self._history_range_task
        if current_task is not None and current_task.request_id == request_id:
            self._history_range_task = None
            self._history_range_pending = False
        if self._shutdown_started:
            self._maybe_complete_shutdown()
            return
        if request_id != self._history_range_sequence:
            return
        if not self._history_mode or self._experiment_csv_path is None or str(self._experiment_csv_path) != path:
            return
        self.chart_source_label.setText(f"历史：{Path(path).name}")
        self.chart_source_label.setToolTip(f"当前图表数据来源：历史文件 {path}")
        if error:
            if error != "图表细节加载已取消":
                self.statusBar().showMessage(f"历史图表细节读取失败：{error}", 10000)
            return
        if not isinstance(result, dict):
            self.statusBar().showMessage("历史图表细节读取失败：后台任务未返回数据", 10000)
            return
        self._plot_x.clear()
        self._plot_y.clear()
        self._plot_metadata.clear()
        self._plot_break_before.clear()
        self._plot_x.extend(result.get("x", ()))
        self._plot_y.extend(result.get("y", ()))
        self._plot_metadata.extend(result.get("metadata", ()))
        self._plot_break_before.extend(result.get("break_before", ()))
        self._history_detail_range = self._history_range_requested
        self._history_detail_is_downsampled = (
            int(result.get("valid_count", 0) or 0) > len(self._plot_x)
        )
        self._selected_plot_point = None
        self._selected_plot_metadata = None
        self._locked_cursor_marker.hide()
        self._refresh_curve()
        self.plot_empty_overlay.setVisible(not self._plot_x)
        self._position_event_labels()

    def _update_plot_data_limits(self) -> None:
        points = self._history_overview_data if self._history_mode and self._history_overview_data else None
        plot_x = points["x"] if points is not None else self._plot_x
        plot_y = points["y"] if points is not None else self._plot_y
        if descriptor(self._display_plot_mode()).quantity == "voltage":
            plot_y = [
                float(value) for value in plot_y
                if _voltage_plot_value_is_visible(float(value))
            ]
        origin = self._plot_y_origin or 0.0
        if not plot_x or not plot_y:
            self.view_box.set_pan_x_data_bounds(None)
            self.view_box.set_pan_y_data_bounds(None)
            y_view_min, y_view_max = _mode_plot_y_view_limits(self._display_plot_mode(), origin, 0.0, 0.0)
            self.view_box.setLimits(yMin=y_view_min, yMax=y_view_max)
            return
        first_x = float(plot_x[0])
        last_x = float(plot_x[-1])
        min_x = min(float(value) for value in plot_x)
        max_x = max(float(value) for value in plot_x)
        min_y = min(float(value) for value in plot_y)
        max_y = max(float(value) for value in plot_y)
        self.view_box.set_pan_x_data_bounds((min_x, max_x))
        min_y -= origin
        max_y -= origin
        self.view_box.set_pan_y_data_bounds((min_y, max_y))
        x_margin = max((max_x - min_x) * 0.05, abs(min_x) * 1e-12, 1e-6)
        y_margin = _plot_y_margin(min_y + origin, max_y + origin)
        y_view_min, y_view_max = _mode_plot_y_view_limits(self._display_plot_mode(), origin, min_y, max_y)
        self.view_box.setLimits(
            xMin=0.0 if self.follow_check.isChecked() else min(0.0, min_x - x_margin),
            xMax=max_x + max(x_margin, AUTO_FOLLOW_WINDOW_S) if self.follow_check.isChecked() else max_x + x_margin,
            yMin=y_view_min,
            yMax=y_view_max,
        )

    def _enable_plot_autoscale(self) -> None:
        self.x_auto_check.setChecked(True)
        self.y_auto_check.setChecked(True)
        self._update_chart_auto_ranges()

    def _update_chart_auto_ranges(self, *, update_x: bool = True) -> None:
        """Fit each enabled axis independently without changing manual spans."""
        if getattr(self, "_updating_chart_auto_ranges", False):
            return
        if not hasattr(self, "view_box") or not self._plot_x:
            return
        self._updating_chart_auto_ranges = True
        try:
            view = self.view_box
            view.enableAutoRange(x=False, y=False)
            latest = float(self._plot_x[-1])
            if update_x and self.x_auto_check.isChecked():
                view.setXRange(0.0, max(latest + max(abs(latest) * 0.005, 1e-9), 1e-6), padding=0)
            elif update_x and self.follow_check.isChecked():
                x0, x1 = view.viewRange()[0]
                span = max(float(x1 - x0), 1e-9)
                right = max(latest, span)
                view.setXRange(right - span, right, padding=0)
            if not self.y_auto_check.isChecked():
                return
            x0, x1 = view.viewRange()[0]
            is_voltage = descriptor(self._display_plot_mode()).quantity == "voltage"
            values = [
                float(y) for x, y in zip(self._plot_x, self._plot_y)
                if x0 <= float(x) <= x1 and math.isfinite(float(y))
                and (not is_voltage or _voltage_plot_value_is_visible(float(y)))
            ]
            if not self._history_mode:
                values.extend(
                    float(y) for sample, y in zip(self._average_trace.samples, self._average_trace.values)
                    if x0 <= float(sample[0]) <= x1 and math.isfinite(float(y))
                    and (not is_voltage or _voltage_plot_value_is_visible(float(y)))
                )
            if values:
                minimum, maximum = min(values), max(values)
                margin = _plot_y_margin(minimum, maximum)
                origin = self._plot_y_origin or 0.0
                view.setYRange(minimum - origin - margin, maximum - origin + margin, padding=0)
        finally:
            self._updating_chart_auto_ranges = False

    def _autoscale_plot(self) -> None:
        self.follow_check.setChecked(False)
        self._fit_plot_to_data()

    def _fit_plot_to_data(self) -> None:
        if self._history_mode and self._history_overview_data is not None:
            self._show_history_overview()
        self._update_plot_data_limits()
        points = self._history_overview_data if self._history_mode and self._history_overview_data else None
        plot_x = points["x"] if points is not None else self._plot_x
        plot_y = points["y"] if points is not None else self._plot_y
        is_voltage = descriptor(self._display_plot_mode()).quantity == "voltage"
        if is_voltage:
            plot_y = [
                float(value) for value in plot_y
                if _voltage_plot_value_is_visible(float(value))
            ]
        if plot_x and plot_y:
            self.plot_widget.getPlotItem().enableAutoRange(x=False, y=False)
            first_x = float(plot_x[0])
            last_x = float(plot_x[-1])
            min_y = min(float(value) for value in plot_y)
            max_y = max(float(value) for value in plot_y)
            origin = self._plot_y_origin or 0.0
            min_y -= origin
            max_y -= origin
            x_margin = max((last_x - first_x) * 0.05, abs(first_x) * 1e-12, 1e-6)
            y_margin = _plot_y_margin(min_y + origin, max_y + origin)
            self.view_box.setRange(
                xRange=(0.0, max(last_x + x_margin, 1e-6)),
                yRange=(min_y - y_margin, max_y + y_margin),
                padding=0,
            )
        elif plot_x and is_voltage:
            self.plot_widget.getPlotItem().enableAutoRange(x=False, y=False)
            first_x = float(plot_x[0])
            last_x = float(plot_x[-1])
            origin = self._plot_y_origin or 0.0
            y_lower, y_upper = _mode_plot_y_view_limits(self._display_plot_mode(), origin, 0.0, 0.0)
            x_margin = max((last_x - first_x) * 0.05, abs(first_x) * 1e-12, 1e-6)
            self.view_box.setRange(
                xRange=(0.0, max(last_x + x_margin, 1e-6)),
                yRange=(y_lower, y_upper),
                padding=0,
            )
        else:
            self.plot_widget.getPlotItem().enableAutoRange(x=True, y=True)
            self.plot_widget.autoRange(padding=0.04)

    def _return_to_live_view(self) -> None:
        snapshot = self._live_plot_snapshot
        if self._history_mode and snapshot is not None:
            self._restore_live_plot_snapshot(snapshot)
            return
        self._history_mode = False
        self.chart_source_label.setText("实时数据")
        self.chart_source_label.setToolTip("当前图表数据来源：实时采集缓存")
        self._set_return_live_enabled(bool(self._plot_x))
        self.follow_check.setChecked(True)
        self._update_plot_data_limits()
        self._fit_plot_to_data()

    def _save_live_plot_snapshot(self) -> None:
        if self._history_mode or self._live_plot_snapshot is not None:
            return
        x_range, y_range = self.view_box.viewRange()
        self._live_plot_snapshot = {
            "x": list(self._plot_x),
            "y": list(self._plot_y),
            "break_before": list(self._plot_break_before),
            "metadata": [dict(row) for row in self._plot_metadata],
            "events": [(line.value(), label.toPlainText()) for line, label in zip(self._event_lines, self._event_labels)],
            "mode": self._current_mode,
            "csv_path": self._experiment_csv_path,
            "last_value": self._last_value,
            "last_elapsed": self._last_elapsed,
            "last_sample_index": self._last_sample_index,
            "global_stats": dict(self._global_stats),
            "baseline": self._baseline,
            "follow": self.follow_check.isChecked(),
            "x_range": tuple(x_range),
            "y_range": tuple(y_range),
            "rate_text": self.actual_rate_label.text(),
            "latest_text": self.latest_value_label.text(),
            "change_text": self.change_label.text(),
            "cursor_text": self.cursor_label.text(),
            "latest_invalid_text": self.latest_invalid_label.text(),
            "latest_invalid_tooltip": self.latest_invalid_label.toolTip(),
            "latest_invalid_log": self._latest_invalid_log_text,
            "recovery_hint": self.recovery_hint_label.text(),
            "recovery_hint_full": self.recovery_hint_label.toolTip() or self.recovery_hint_label.text(),
            "acquisition_issue": self._last_acquisition_issue,
        }

    def _restore_live_plot_snapshot(self, snapshot: dict) -> None:
        self._live_plot_snapshot = None
        self._clear_plot(preserve_live_snapshot=True)
        self._history_mode = False
        self._plot_x.extend(snapshot["x"])
        self._plot_y.extend(snapshot["y"])
        self._plot_break_before.extend(snapshot["break_before"])
        self._plot_metadata.extend(snapshot["metadata"])
        self._experiment_csv_path = snapshot["csv_path"]
        self._current_mode = snapshot["mode"]
        self.mode_combo.blockSignals(True)
        self._set_combo_data(self.mode_combo, self._current_mode.value)
        self.mode_combo.blockSignals(False)
        self._load_mode_controls(self._current_mode)
        self._last_value = snapshot["last_value"]
        self._last_elapsed = snapshot["last_elapsed"]
        self._last_sample_index = snapshot["last_sample_index"]
        self._global_stats = dict(snapshot["global_stats"])
        self._baseline = snapshot["baseline"]
        self.baseline_edit.setText("" if self._baseline is None else repr(self._baseline))
        if self._baseline is None:
            self.baseline_line.hide()
        else:
            self.baseline_line.setValue(float(self._baseline) - (self._plot_y_origin or 0.0))
            self.baseline_line.hide()
        self.latest_value_label.setText(snapshot["latest_text"])
        self.actual_rate_label.setText(snapshot["rate_text"])
        self.change_label.setText(snapshot["change_text"])
        self.cursor_label.setText(snapshot["cursor_text"])
        self.latest_invalid_label.setText(snapshot["latest_invalid_text"])
        self.latest_invalid_label.setToolTip(snapshot["latest_invalid_tooltip"])
        self._latest_invalid_log_text = snapshot["latest_invalid_log"]
        if self._latest_invalid_log_text:
            self._update_latest_invalid_summary()
        self.latest_invalid_label.setVisible(
            not self._short_viewport_layout or bool(self._latest_invalid_log_text)
        )
        self.latest_invalid_log_button.setEnabled(bool(self._latest_invalid_log_text))
        self.latest_invalid_log_button.hide()
        self._last_acquisition_issue = snapshot["acquisition_issue"]
        recovery_hint = snapshot.get("recovery_hint_full", snapshot["recovery_hint"])
        self.recovery_hint_label.setToolTip(recovery_hint)
        self.recovery_hint_label.setText(
            "采集已停止 · 悬停查看" if self._short_viewport_layout and recovery_hint else recovery_hint
        )
        self.recovery_hint_label.setVisible(bool(recovery_hint))
        self._refresh_curve()
        self._update_plot_data_limits()
        self.plot_empty_overlay.setVisible(not self._plot_x)
        self.follow_check.setChecked(bool(snapshot["follow"]))
        self._on_follow_toggled(bool(snapshot["follow"]))
        self._add_event_markers(snapshot["events"])
        if self._plot_x:
            self.view_box.setRange(
                xRange=snapshot["x_range"], yRange=snapshot["y_range"], padding=0
            )
        self.chart_source_label.setText("实时数据")
        self.chart_source_label.setToolTip("当前图表数据来源：实时采集缓存")
        self._set_return_live_enabled(bool(self._plot_x))
        self._update_stats(self._global_stats)

    def _clear_plot(self, *, preserve_live_snapshot: bool = False) -> None:
        history_timer = getattr(self, "_history_range_timer", None)
        if history_timer is not None:
            history_timer.stop()
        self._history_range_sequence += 1
        if self._history_range_task is not None:
            self._history_range_task.cancel()
        self._history_range_task = None
        self._history_range_pending = False
        self._history_overview_data = None
        self._history_detail_range = None
        self._history_detail_is_downsampled = False
        self._region_stats_request_id += 1
        for task in self._region_stats_tasks.values():
            task.cancel()
        self._region_stats_scanning = False
        self._region_stats_pending_rows.clear()
        self._region_stats_pending_overflow = False
        self._region_stats_mode = None
        self._region_stats_values = None
        self._region_stats_last_sample_index = 0
        self._region_stats_bounds = None
        self._experiment_csv_path = None
        self._plot_data_mode = None
        self._plot_x.clear()
        self._plot_y.clear()
        self._average_trace.clear()
        self._plot_frame_timer.stop()
        self._last_plot_frame_refresh = 0.0
        self._average_refresh_timer.stop()
        self._average_last_refresh = 0.0
        self._plot_y_anchor_x = np.asarray([], dtype=float)
        self._plot_y_anchor_values = np.asarray([], dtype=float)
        self._plot_y_origin = None
        self.view_box.zero_y = 0.0
        self.y_axis.value_offset = 0.0
        self._plot_metadata.clear()
        self._plot_break_before.clear()
        self._plot_break_pending = False
        self._latest_invalid_log_text = ""
        self.latest_invalid_label.setText("最近无效读数：无")
        self.latest_invalid_label.setToolTip("采集中的无效读数及其原因会显示在这里")
        self.latest_invalid_label.setVisible(not self._short_viewport_layout)
        self.latest_invalid_log_button.setEnabled(False)
        self.latest_invalid_log_button.hide()
        self.display_loss_warning.clear()
        self.display_loss_warning.hide()
        self._last_acquisition_issue = ""
        self.recovery_hint_label.clear()
        self.recovery_hint_label.setToolTip("")
        self.recovery_hint_label.hide()
        if not preserve_live_snapshot:
            self._live_plot_snapshot = None
            self._history_mode = False
            self.chart_source_label.setText("实时数据")
            self.chart_source_label.setToolTip("当前图表数据来源：实时采集缓存")
            self._set_return_live_enabled(False)
        self._selected_plot_point = None
        self._selected_plot_metadata = None
        self._locked_cursor_marker.hide()
        self._crosshair_x = None
        self._crosshair_y = None
        self._clear_event_markers()
        self._last_value = None
        self._last_elapsed = 0.0
        self._global_stats = {"count": 0, "minimum": None, "maximum": None, "mean": None, "sample_stddev": None}
        self._last_sample_index = 0
        self._last_actual_range_si = None
        self.valid_sample_count_label.setText("有效样本 0")
        self.curve.setData([], [])
        self.average_curve.setData([], [])
        self.plot_empty_overlay.show()
        self.plot_empty_title.setText("等待采样")
        self.plot_empty_hint.setText("连接 Fluke 8845A 后开始单次测量或连续采集")
        self.plot_empty_new_measurement_button.hide()
        for item in self.plot_widget.items():
            if isinstance(item, pg.InfiniteLine) and item is not self.baseline_line:
                self.plot_widget.removeItem(item)
        self.latest_value_label.setText("等待读数")
        self.actual_rate_label.setText("—")
        self._region_stats_label.setText("输入起止时间，或拖动图中的区间手柄")
        self.cursor_label.setText("鼠标悬停预览读数；单击曲线锁定一个基准点")
        self._update_stats()

    def _clear_event_markers(self) -> None:
        self._cancel_pending_event_marker_load()
        for item in (*self._event_lines, *self._event_labels):
            self.plot_widget.removeItem(item)
        self._event_lines.clear()
        self._event_labels.clear()
        if hasattr(self, "event_list_widget"):
            self.event_list_widget.clear()

    def _load_history(self) -> None:
        if self._acquiring or self._start_pending:
            self.statusBar().showMessage("采集运行中，请停止或暂停采集后再加载历史 CSV", 8000)
            return
        if getattr(self, "_history_load_pending", False):
            self.statusBar().showMessage("历史 CSV 正在后台加载，请稍候", 5000)
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择实验原始测量 CSV", self.output_root.text(), "CSV 文件 (*.csv)")
        if not path:
            return
        task = _HistoryLoadTask(path, self._current_mode, MAX_PLOT_POINTS)
        task.signals.completed.connect(self._on_history_loaded)
        self._history_load_pending = True
        self._history_load_task = task
        self.history_button.setEnabled(False)
        self._update_controls()
        self.statusBar().showMessage("正在后台读取并分析历史 CSV；界面仍可继续操作…")
        QThreadPool.globalInstance().start(task)

    @Slot(str, object, object)
    def _on_history_loaded(self, path: str, result: dict | None, error: str | None) -> None:
        if self._shutdown_started:
            self._history_load_pending = False
            self._history_load_task = None
            self._maybe_complete_shutdown()
            return
        self._history_load_pending = False
        self._history_load_task = None
        self.history_button.setEnabled(True)
        self._update_controls()
        if error == "历史 CSV 加载已取消":
            return
        if error:
            if error.startswith(("CSV混合测量模式：", "CSV混合单位：")):
                warning = (
                    f"警告：{error} 当前文件未加载，现有曲线保持不变；"
                    "请按单一测量模式和单位拆分文件后再导入。"
                )
                self.statusBar().showMessage(warning, 12000)
                self._append_log(f"! {warning}")
                QMessageBox.warning(self, "混合 CSV 未加载", warning)
                return
            self._show_error(f"加载历史CSV失败：{error}")
            return
        if result is None:
            self._show_error("加载历史CSV失败：后台任务未返回数据")
            return
        for warning in result["warnings"]:
            self._append_log(f"! {warning}")

        xs = result["x"]
        ys = result["y"]
        plot_metadata = result["metadata"]
        plot_break_before = result["break_before"]
        history_events = result["events"]
        valid_count = result["valid_count"]
        first_elapsed = result["first_elapsed"]
        last_elapsed = result["last_elapsed"]
        last_value = result["last_value"]
        mode_value = result["mode"]

        self._save_live_plot_snapshot()
        self._clear_plot(preserve_live_snapshot=True)
        self._history_mode = True
        self._history_overview_data = {
            "x": list(xs), "y": list(ys),
            "metadata": list(plot_metadata), "break_before": list(plot_break_before),
            "_range_index": result.get("_range_index"),
        }
        self._history_detail_range = None
        self._plot_x.extend(xs)
        self._plot_y.extend(ys)
        self._plot_break_before.extend(plot_break_before)
        self._plot_metadata.clear()
        self._plot_metadata.extend(plot_metadata)
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
        self._update_plot_data_limits()
        self.follow_check.setChecked(False)
        self.plot_widget.setXRange(first_elapsed, last_elapsed if last_elapsed > first_elapsed else first_elapsed + 1, padding=0.01)
        self.chart_source_label.setText(f"历史：{Path(path).name}")
        self.chart_source_label.setToolTip(f"当前图表数据来源：历史文件 {path}")
        self.view_mode_label.setText("历史文件视图")
        self._set_return_live_enabled(True)
        self._add_event_markers(history_events)
        spec = descriptor(self._current_mode)
        self.plot_widget.setLabel("left", f"历史数据 · {spec.display_quantity}", units=spec.unit_si, color="#697470")
        displayed, unit = engineering_value(last_value, spec.unit_si)
        self.latest_value_label.setText(f"{displayed:.10g} {unit}")
        self.valid_sample_count_label.setText(f"有效样本 {valid_count:,}")
        rate_span = last_elapsed - first_elapsed
        rate = (valid_count - 1) / rate_span if valid_count > 1 and rate_span > 0 else 0.0
        self.actual_rate_label.setText(_format_sampling_interval(rate))
        if self._baseline is None:
            self.baseline_line.hide()
            self.change_label.setText("未设置基准值")
        else:
            self.baseline_line.setValue(float(self._baseline) - (self._plot_y_origin or 0.0))
            self.baseline_line.hide()
            delta = last_value - self._baseline
            percent = relative_change_percent(last_value, self._baseline, self._parse_threshold())
            d_display, d_unit = engineering_value(delta, spec.unit_si)
            relative = f"{percent:.8g}%" if percent is not None else "无效（基准接近零或阈值无效）"
            self.change_label.setText(f"Δ={d_display:.8g} {d_unit} · ΔX/X₀={relative}")
        self._update_stats(result["stats"])
        if self._region.isVisible():
            self._update_region_stats()
        event_summary = f"；恢复 {len(history_events):,} 条关联事件标记" if history_events else ""
        QMessageBox.information(
            self, "历史数据已加载",
            f"加载 {valid_count:,} 个有效原始读数；图中显示 {len(self._plot_x):,} 个抽稀点"
            f"{event_summary}，CSV文件未作改动。",
        )

    def _screenshot_context_lines(self) -> tuple[str, str, str]:
        spec = descriptor(self._display_plot_mode())
        source = self._experiment_csv_path
        if source is None:
            experiment = self.experiment_name.text().strip()
        else:
            experiment = source.stem if source.name.lower() != "measurements.csv" else source.parent.name
        experiment = experiment or "未命名实验"
        try:
            x_range = self.plot_widget.getPlotItem().vb.viewRange()[0]
            if len(x_range) == 2 and all(math.isfinite(float(value)) for value in x_range):
                time_range = f"t={float(x_range[0]):.6g} — {float(x_range[1]):.6g} s"
            else:
                time_range = "无有效时间范围"
        except (AttributeError, TypeError, ValueError, OverflowError):
            time_range = "无有效时间范围"
        if self._baseline is None or not math.isfinite(float(self._baseline)):
            baseline = "未设置"
        else:
            shown, unit = engineering_value(float(self._baseline), spec.unit_si)
            baseline = f"{shown:.10g} {unit}".strip()
        exported_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        display_unit = "Ω" if spec.unit_si == "Ohm" else spec.unit_si
        return (
            f"实验：{experiment} · 模式：{spec.display_quantity} ({self._display_plot_mode().value}) · 单位：{display_unit}",
            f"导出时间：{exported_at} · 时间范围：{time_range}",
            f"基准线：{baseline}",
        )

    def _save_curve_csv(self) -> None:
        source = self._experiment_csv_path
        if self._curve_export_task is not None:
            self.statusBar().showMessage("正在保存曲线，请稍候。", 5000)
            return
        if source is None or not source.is_file():
            self.statusBar().showMessage("暂无可保存的测量数据。", 5000)
            return
        suggestion = source.with_name(f"{source.stem}_曲线.csv")
        path, _ = QFileDialog.getSaveFileName(self, "保存曲线 CSV", str(suggestion), "CSV 文件 (*.csv)")
        if not path:
            return
        destination = Path(path)
        if destination.suffix.casefold() != ".csv":
            destination = destination.with_suffix(".csv")
        if destination.exists() or detailed_curve_path(destination).exists():
            self.statusBar().showMessage("简洁或详细 CSV 已存在，请选择新文件名。", 8000)
            return
        task = CurveExportTask(source, destination, self.average_window_spin.value())
        task.signals.completed.connect(self._on_curve_csv_saved)
        self._curve_export_task = task
        self._update_controls()
        self.statusBar().showMessage("正在后台保存曲线 CSV…")
        QThreadPool.globalInstance().start(task)

    @Slot(str, int, object)
    def _on_curve_csv_saved(self, destination: str, count: int, error: object) -> None:
        self._curve_export_task = None
        if self._shutdown_started:
            self._record_shutdown_file_task_result("曲线 CSV", error)
            return
        self._update_controls()
        if error:
            message = f"保存曲线失败：{error}"
            self._append_log(f"! {message}")
            self.statusBar().showMessage(message, 15000)
            QMessageBox.warning(self, "保存曲线失败", message)
            return
        self.statusBar().showMessage(f"已保存 {count} 条曲线数据：{destination} 和 {detailed_curve_path(destination)}", 15000)
        self._record_file_export_result("曲线 CSV", destination)
        self._record_file_export_result("详细曲线 CSV", detailed_curve_path(destination))

    def _save_screenshot(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存曲线截图", "Fluke8845A_curve.png", "PNG 图像 (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        source = self.plot_widget.grab()
        scale = max(1.0, float(source.devicePixelRatio()))
        header_height = 78
        composite = QPixmap(source.width(), source.height() + round(header_height * scale))
        composite.setDevicePixelRatio(scale)
        composite.fill(QColor("#ffffff"))
        painter = QPainter(composite)
        painter.setPen(QColor("#25343a"))
        title_font = QFont(self.font())
        title_font.setPointSize(10)
        title_font.setBold(True)
        painter.setFont(title_font)
        title_line, time_line, baseline_line = self._screenshot_context_lines()
        painter.drawText(14, 22, title_line)
        painter.setPen(QColor("#53656b"))
        note_font = QFont(self.font())
        note_font.setPointSize(9)
        painter.setFont(note_font)
        painter.drawText(14, 45, time_line)
        painter.drawText(14, 66, baseline_line)
        painter.drawPixmap(0, header_height, source)
        painter.end()
        if not composite.save(path, "PNG"):
            self._show_error("曲线截图保存失败")
        else:
            self.statusBar().showMessage(f"截图已保存：{path}", 8000)
            self._record_file_export_result("曲线截图", path)
            self._refresh_file_workspace()

    def _diagnose(self) -> None:
        if self._diagnostic_pending:
            self.statusBar().showMessage("正在读取仪器诊断，请等待本次结果。", 5000)
            return
        if not self._connected or self._simulation or self._control_mode != "computer" or self._acquiring:
            self.statusBar().showMessage("诊断需要已连接的真实仪器、Computer 控制方式和空闲采集状态。", 8000)
            return
        self._diagnostic_pending = True
        self.statusBar().showMessage("正在读取仪器诊断（身份、配置与错误队列）…")
        self._update_controls()
        self._bridge.diagnose.emit()

    @Slot(str)
    def _show_diagnostic(self, text: str) -> None:
        self._diagnostic_pending = False
        self._update_controls()
        self.statusBar().showMessage("仪器诊断完成。", 8000)
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
        self._store_log_entry(message, f"{timestamp}  {message}")
        if message.startswith("目标采样间隔由") and "从样本 " in message:
            self.statusBar().showMessage(message, 15000)

    def _store_log_entry(self, message: str, formatted: str) -> None:
        self._store_log_entries([(message, formatted)])

    def _store_log_entries(self, entries: Iterable[tuple[str, str]]) -> None:
        """Append a log batch and rebuild the filtered views once for the batch."""
        categorized = [(_classify_log_message(message), formatted) for message, formatted in entries]
        if not categorized:
            return
        self._log_entries.extend(categorized)
        self._render_filtered_log()

    @Slot()
    def _render_filtered_log(self, *_args) -> None:
        if not hasattr(self, "log_filter_combo") or not hasattr(self, "communication_log_filter_combo"):
            return
        sender = self.sender()
        source_combo = (
            self.communication_log_filter_combo
            if sender is self.communication_log_filter_combo
            else self.log_filter_combo
        )
        category = source_combo.currentData()
        for combo in (self.log_filter_combo, self.communication_log_filter_combo):
            if combo.currentData() != category:
                blocker = QSignalBlocker(combo)
                combo.setCurrentIndex(combo.findData(category))
                del blocker
        lines = [line for item_category, line in self._log_entries if category == "all" or item_category == category]
        for log_view in (self.log_text, self.communication_log_text):
            scrollbar = log_view.verticalScrollBar()
            was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
            log_view.setPlainText("\n".join(lines))
            if was_at_bottom:
                scrollbar.setValue(scrollbar.maximum())

    def _export_filtered_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出当前筛选日志", "Fluke8845A_log.txt", "文本文件 (*.txt)")
        if not path:
            return
        if not path.lower().endswith(".txt"):
            path += ".txt"
        category_combo = (
            self.communication_log_filter_combo
            if self.sender() is self.communication_export_log_button
            else self.log_filter_combo
        )
        category = category_combo.currentData()
        lines = [line for item_category, line in self._log_entries if category == "all" or item_category == category]
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines))
                if lines:
                    stream.write("\n")
        except OSError as exc:
            self._show_error(f"日志导出失败：{exc}")
            return
        self.statusBar().showMessage(f"已导出 {len(lines)} 条筛选日志：{path}", 8000)

    @Slot()
    def _on_scpi_logs_ready(self) -> None:
        lines, dropped = self._worker.take_scpi_log_update()
        if not lines and not dropped:
            return
        entries = []
        if dropped:
            dropped_message = f"! SCPI显示日志缓冲曾丢弃较早的 {dropped} 行；测量读数和原始响应仍逐点保存在CSV中。"
            dropped_time = datetime.now().astimezone().strftime("%H:%M:%S.%f")[:-3]
            entries.append((dropped_message, f"{dropped_time}  {dropped_message}"))
        entries.extend((message, f"{timestamp}  {message}") for timestamp, message in lines)
        self._store_log_entries(entries)

    @Slot(str)
    def _show_error(self, message: str) -> None:
        operation_was_pending = self._configure_pending or self._zero_pending
        configuration_failed = self._configure_pending
        self._connection_pending = False
        self._start_pending = False
        self._diagnostic_pending = False
        self._configure_pending = False
        self._zero_pending = False
        self._event_save_pending = False
        if configuration_failed:
            self._instrument_mode_known = False
            self._active_settings_by_mode.clear()
            self._update_instrument_readback_status()
        if operation_was_pending:
            self._set_operation_status("操作失败", state="failure", detail=message)
        self.statusBar().showMessage(message, 12000)
        self._append_log(f"! {message}")
        self._update_controls()
        QMessageBox.warning(self, "操作未完成", message)

    @Slot()
    def _on_worker_thread_finished(self) -> None:
        self._shutdown_finished = True
        if self._shutdown_started:
            self._maybe_complete_shutdown()

    def _pending_background_shutdown_tasks(self) -> list[str]:
        file_tasks = (
            ("_safe_events_export_task", "事件安全副本"),
            ("_safe_measurements_export_task", "测量安全副本"),
            ("_share_export_task", "实验分享包"),
            ("_snapshot_task", "实验快照"),
            ("_curve_export_task", "曲线 CSV"),
        )
        pending = [label for attribute, label in file_tasks if getattr(self, attribute, None) is not None]
        if self._history_load_task is not None:
            pending.append("历史 CSV 读取")
        if self._active_history_range_tasks or self._history_range_task is not None:
            pending.append("历史图表区间读取")
        if self._region_stats_tasks:
            pending.append("区间统计")
        return pending

    def _set_shutdown_task_wait_status(self, labels: list[str]) -> None:
        names = "、".join(labels)
        message = f"正在等待后台任务完成（{names}）；完成后安全退出…"
        self.shutdown_status_label.setText(message)
        self.shutdown_status_label.show()
        self.statusBar().showMessage(message)

    def _record_shutdown_file_task_result(self, label: str, error: object) -> None:
        if error:
            self._shutdown_file_errors.append(f"{label}：{error}")
        self._maybe_complete_shutdown()

    def _maybe_complete_shutdown(self) -> None:
        if not self._shutdown_started or not self._shutdown_finished:
            return
        pending = self._pending_background_shutdown_tasks()
        if pending:
            self._set_shutdown_task_wait_status(pending)
            return
        if self._shutdown_file_errors and not self._shutdown_file_errors_reported:
            details = "\n".join(self._shutdown_file_errors)
            QMessageBox.warning(
                self,
                "后台文件任务未完成",
                f"程序关闭前，以下文件任务未能成功完成：\n\n{details}\n\n请检查目标文件和磁盘空间。",
            )
            self._shutdown_file_errors_reported = True
        self.close()

    def _cancel_pending_view_tasks_on_close(self) -> None:
        self._cancel_pending_event_marker_load()
        history_timer = getattr(self, "_history_range_timer", None)
        if history_timer is not None:
            history_timer.stop()
        self._history_range_sequence += 1
        active_range_tasks = tuple(self._active_history_range_tasks.values())
        for task in active_range_tasks:
            task.cancel()
        if self._history_range_task is not None and all(
            task is not self._history_range_task for task in active_range_tasks
        ):
            self._history_range_task.cancel()
        self._history_range_pending = False

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._shutdown_finished or not self._worker_thread.isRunning():
            self._cancel_pending_view_tasks_on_close()
            self._save_profiles()
            self._shutdown_started = True
            self._shutdown_finished = True
            self._clock_timer.stop()
            history_task = getattr(self, "_history_load_task", None)
            if history_task is not None:
                history_task.cancel()
            self._region_stats_request_id += 1
            self._region_stats_scanning = False
            self._region_stats_pending_rows.clear()
            self._region_stats_pending_overflow = False
            self._region_stats_mode = None
            for task in self._region_stats_tasks.values():
                task.cancel()
            pending_tasks = self._pending_background_shutdown_tasks()
            if pending_tasks:
                event.ignore()
                self.setEnabled(False)
                self.setWindowTitle(f"正在完成后台任务 · {self._base_window_title}")
                self._set_shutdown_task_wait_status(pending_tasks)
                return
            event.accept()
            return

        event.ignore()
        if self._shutdown_started:
            return

        if self._acquiring:
            answer = QMessageBox.question(
                self,
                "确认关闭",
                "关闭桌面软件窗口将停止当前采集，并等待当前读数结束、保存数据、关闭串口连接后退出。\n"
                "单独运行的 localhost 网页服务不会因此关闭。\n\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._clear_pending_new_measurement()
        self._cancel_pending_view_tasks_on_close()
        self._save_profiles()
        self._shutdown_started = True
        history_task = getattr(self, "_history_load_task", None)
        if history_task is not None:
            history_task.cancel()
        self._clock_timer.stop()
        self._region_stats_request_id += 1
        self._region_stats_scanning = False
        self._region_stats_pending_rows.clear()
        self._region_stats_pending_overflow = False
        self._region_stats_mode = None
        for task in self._region_stats_tasks.values():
            task.cancel()
        pending_tasks = self._pending_background_shutdown_tasks()
        if pending_tasks:
            self.shutdown_status_label.setText(
                "正在等待当前读数结束；随后完成后台任务并退出…" if self._acquiring
                else "正在关闭仪器连接并等待后台任务完成后退出…"
            )
        else:
            self.shutdown_status_label.setText(
                "正在等待当前读数结束；随后刷新并保存 CSV…" if self._acquiring
                else "正在等待当前仪器操作结束；随后关闭串口连接并退出…"
            )
        self.shutdown_status_label.show()
        self.statusBar().showMessage(self.shutdown_status_label.text())
        self.setEnabled(False)
        self.setWindowTitle(f"正在安全关闭 · {self._base_window_title}")
        self._append_log("正在等待当前读数结束、保存数据并关闭仪器连接…")
        self._worker.request_stop()
        self._bridge.close_app.emit()

