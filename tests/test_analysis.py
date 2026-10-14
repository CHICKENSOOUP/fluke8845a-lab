import math

import pytest

from fluke8845a.analysis import RunningStats, engineering_value, relative_change_percent, summarize


def test_relative_change_handles_negative_values_and_zero_threshold():
    assert relative_change_percent(12, 10, 1e-9) == pytest.approx(20)
    assert relative_change_percent(-9, -10, 1e-9) == pytest.approx(-10)
    assert relative_change_percent(1, 0, 1e-12) is None
    assert relative_change_percent(1, 1e-13, 1e-12) is None
    assert relative_change_percent(1, 1, math.nan) is None


def test_summary_uses_sample_standard_deviation_and_ignores_nonfinite():
    result = summarize([1, 2, 3, math.nan])
    assert result.count == 3
    assert result.mean == 2
    assert result.sample_stddev == pytest.approx(1)
    assert summarize([]).mean is None
    assert summarize([4]).sample_stddev is None


def test_running_stats_are_online_and_unbounded_sample_count():
    stats = RunningStats()
    for value in [1.0, 2.0, 3.0]:
        stats.add(value)
    assert stats.as_dict() == {"count": 3, "minimum": 1.0, "maximum": 3.0, "mean": 2.0, "sample_stddev": 1.0}


@pytest.mark.parametrize("value,unit,expected", [
    (0.0023, "V", (2.3, "mV")), (0.0, "V", (0.0, "V")),
    (1e-9, "A", (1.0, "nA")), (1200, "Ohm", (1.2, "kΩ")),
])
def test_engineering_formatting_does_not_change_si_value(value, unit, expected):
    actual = engineering_value(value, unit)
    assert actual[0] == pytest.approx(expected[0])
    assert actual[1] == expected[1]

