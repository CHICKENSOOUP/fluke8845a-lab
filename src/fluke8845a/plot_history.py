"""Bounded peak-preserving plot data helpers."""

from __future__ import annotations

import math
from typing import Any, Sequence


def downsample_plot_data(
    x_values: Sequence[float],
    y_values: Sequence[float],
    break_before: Sequence[bool],
    metadata: Sequence[dict[str, Any]],
    *,
    max_points: int,
) -> tuple[list[float], list[float], list[bool], list[dict[str, Any]]]:
    """Downsample an ordered trace while retaining run endpoints and local peaks.

    Each bucket contributes its first/last points, minimum and maximum, plus
    the first point after a gap when one exists. Gap markers from omitted points
    are carried to the next retained point so downsampling never reconnects
    across a missing-reading interval.
    """
    count = len(x_values)
    if not (len(y_values) == len(break_before) == len(metadata) == count):
        raise ValueError("绘图数据的时间、读数、断点和元数据长度必须一致")
    ys = list(y_values)
    limit = max(5, int(max_points))
    if count <= limit:
        return (
            [float(x) for x in x_values],
            [float(y) for y in y_values],
            [bool(value) for value in break_before],
            [dict(item) for item in metadata],
        )

    bucket_count = max(1, limit // 5)
    bucket_size = math.ceil(count / bucket_count)
    selected: list[int] = []
    for start in range(0, count, bucket_size):
        end = min(count, start + bucket_size)
        segment = ys[start:end]
        candidates = {
            start,
            end - 1,
            start + min(range(len(segment)), key=segment.__getitem__),
            start + max(range(len(segment)), key=segment.__getitem__),
        }
        gap_index = next(
            (index for index in range(start, end) if break_before[index]),
            None,
        )
        if gap_index is not None:
            candidates.add(gap_index)
        selected.extend(sorted(candidates))

    # The final partial bucket can create a few points above the nominal
    # budget. Thin only non-endpoint/non-gap points in that rare case.
    if len(selected) > limit:
        required = {0, count - 1}
        required.update(index for index in selected if break_before[index])
        optional = [index for index in selected if index not in required]
        available = max(0, limit - len(required))
        if len(optional) > available:
            stride = len(optional) / max(1, available)
            optional = [optional[min(len(optional) - 1, int(i * stride))] for i in range(available)]
        selected = sorted(required.union(optional))

    sampled_breaks: list[bool] = []
    previous = -1
    for index in selected:
        sampled_breaks.append(any(bool(break_before[item]) for item in range(previous + 1, index + 1)))
        previous = index

    return (
        [float(x_values[index]) for index in selected],
        [float(ys[index]) for index in selected],
        sampled_breaks,
        [dict(metadata[index]) for index in selected],
    )
