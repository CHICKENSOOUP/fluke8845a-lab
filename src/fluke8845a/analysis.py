"""Scientific calculations; raw readings are never filtered or rounded here."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SummaryStats:
    count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    sample_stddev: float | None


def summarize(values: Iterable[float]) -> SummaryStats:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    if not clean:
        return SummaryStats(0, None, None, None, None)
    return SummaryStats(
        len(clean), min(clean), max(clean), statistics.fmean(clean),
        statistics.stdev(clean) if len(clean) >= 2 else None,
    )


class RunningStats:
    """Welford accumulator for lifetime statistics without unbounded RAM use."""

    def __init__(self) -> None:
        self.count = 0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.mean: float | None = None
        self._m2 = 0.0

    def add(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self.count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if self.mean is None:
            self.mean = value
        else:
            delta = value - self.mean
            self.mean += delta / self.count
            self._m2 += delta * (value - self.mean)

    @property
    def sample_stddev(self) -> float | None:
        return math.sqrt(self._m2 / (self.count - 1)) if self.count >= 2 else None

    def as_dict(self) -> dict:
        return {"count": self.count, "minimum": self.minimum, "maximum": self.maximum,
                "mean": self.mean, "sample_stddev": self.sample_stddev}


def relative_change_percent(value: float, baseline: float, threshold: float = 0.0) -> float | None:
    """Return dX/X0 percent, or None when X0 is too close to zero."""
    if not all(math.isfinite(v) for v in (value, baseline, threshold)) or threshold < 0:
        return None
    if abs(baseline) <= threshold:
        return None
    return (value - baseline) / baseline * 100.0


def absolute_change(value: float, baseline: float) -> float | None:
    if not math.isfinite(value) or not math.isfinite(baseline):
        return None
    return value - baseline


def engineering_value(value_si: float, unit_si: str) -> tuple[float, str]:
    """Scale only a display copy; callers must retain the original SI float."""
    if unit_si == "V":
        choices = ((1e-6, "µV"), (1e-3, "mV"), (1.0, "V"), (1e3, "kV"))
    elif unit_si == "A":
        choices = ((1e-9, "nA"), (1e-6, "µA"), (1e-3, "mA"), (1.0, "A"))
    else:
        choices = ((1.0, "Ω"), (1e3, "kΩ"), (1e6, "MΩ"), (1e9, "GΩ"))
    magnitude = abs(value_si)
    base = {"V": (1.0, "V"), "A": (1.0, "A"), "Ohm": (1.0, "Ω")}[unit_si]
    if magnitude == 0:
        scale, unit = base
    else:
        eligible = [item for item in choices if magnitude / item[0] >= 1]
        scale, unit = eligible[-1] if eligible else choices[0]
    return value_si / scale, unit

