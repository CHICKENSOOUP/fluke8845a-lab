from fluke8845a.plot_history import downsample_plot_data


def test_peak_downsampling_retains_run_endpoints_global_extrema_and_gap_markers():
    xs = [float(index) for index in range(1000)]
    ys = [0.0] * len(xs)
    ys[333] = 12.0
    ys[777] = -9.0
    breaks = [False] * len(xs)
    breaks[515] = True
    metadata = [{"sample_index": index + 1} for index in range(len(xs))]

    sampled = downsample_plot_data(xs, ys, breaks, metadata, max_points=80)

    plot_x, plot_y, plot_breaks, plot_metadata = sampled
    assert len(plot_x) <= 80
    assert plot_x[0] == 0.0
    assert plot_x[-1] == 999.0
    assert max(plot_y) == 12.0
    assert min(plot_y) == -9.0
    assert any(plot_breaks)
    assert len(plot_x) == len(plot_y) == len(plot_breaks) == len(plot_metadata)
    assert all(meta["sample_index"] == int(x) + 1 for x, meta in zip(plot_x, plot_metadata))


def test_downsampling_preserves_input_without_mutating_it_when_within_budget():
    xs = [0.0, 1.0]
    ys = [2.0, 3.0]
    breaks = [False, True]
    metadata = [{"sample_index": 1}, {"sample_index": 2}]

    result = downsample_plot_data(xs, ys, breaks, metadata, max_points=10)

    assert result == (xs, ys, breaks, metadata)
    assert breaks == [False, True]


def test_downsampling_carries_later_gap_markers_to_the_next_selected_point():
    xs = [float(index) for index in range(10)]
    ys = [0.0] * len(xs)
    breaks = [False] * len(xs)
    breaks[3] = True
    breaks[7] = True
    metadata = [{"sample_index": index + 1} for index in range(len(xs))]

    sampled_x, _sampled_y, sampled_breaks, _sampled_metadata = downsample_plot_data(
        xs, ys, breaks, metadata, max_points=5,
    )

    assert sampled_x == [0.0, 3.0, 9.0]
    assert sampled_breaks == [False, True, True]
