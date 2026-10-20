import csv

import pytest

import fluke8845a.history_import as history_import_module
from fluke8845a.history_import import load_history_csv, load_history_csv_range
from fluke8845a.modes import Mode


def _write_measurements(path, rows):
    fields = ("experiment_id", "sample_index", "elapsed_s", "mode", "unit_si", "value_si", "status")
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.mark.parametrize("loader", ["overview", "range"])
def test_history_import_rejects_duplicate_columns_before_dict_reader_overwrites_values(tmp_path, loader):
    source = tmp_path / "duplicate-value-column.csv"
    source.write_text(
        "elapsed_s,value_si,value_si,status\n0,1.25,9.75,ok\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复列.*value_si"):
        if loader == "overview":
            load_history_csv(source, Mode.DCV, max_plot_points=20)
        else:
            load_history_csv_range(source, 0, 1, max_plot_points=20)


def test_history_import_validates_stats_and_downsamples_without_editing_source(tmp_path):
    source = tmp_path / "measurements.csv"
    rows = [
        {"experiment_id": "run-1", "sample_index": index, "elapsed_s": index - 1,
         "mode": "DCV", "unit_si": "V", "value_si": index * 2, "status": "ok"}
        for index in range(1, 11)
    ]
    _write_measurements(source, rows)
    original = source.read_bytes()

    result = load_history_csv(source, Mode.RES2, max_plot_points=5)

    assert result["valid_count"] == 10
    assert result["mode"] == "DCV"
    assert len(result["x"]) <= 5
    assert result["x"][-1] == 9.0 and result["y"][-1] == 20.0
    assert result["stats"]["mean"] == 11.0
    assert source.read_bytes() == original


def test_history_import_stops_when_background_task_is_cancelled(tmp_path):
    source = tmp_path / "large.csv"
    rows = [
        {"experiment_id": "run-1", "sample_index": index, "elapsed_s": index,
         "mode": "DCV", "unit_si": "V", "value_si": index, "status": "ok"}
        for index in range(1, 100)
    ]
    _write_measurements(source, rows)
    checks = 0

    def cancel_after_a_few_rows():
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(InterruptedError, match="已取消"):
        load_history_csv(source, Mode.DCV, 100, is_cancelled=cancel_after_a_few_rows)

    assert checks == 3


@pytest.mark.parametrize("loader", ["overview", "range"])
def test_history_import_can_cancel_during_complete_prefix_scan(tmp_path, monkeypatch, loader):
    source = tmp_path / "large.csv"
    header = "experiment_id,sample_index,elapsed_s,mode,unit_si,value_si,status\r\n"
    rows = "".join(
        f"run-1,{index},{index},DCV,V,{index},ok\r\n"
        for index in range(6000)
    )
    source.write_text(header + rows, encoding="utf-8")
    assert source.stat().st_size > 2 * 64 * 1024

    original_prefix_scan = history_import_module.complete_csv_prefix_size
    prefix_scan_completed = False

    def track_prefix_scan(*args, **kwargs):
        nonlocal prefix_scan_completed
        result = original_prefix_scan(*args, **kwargs)
        prefix_scan_completed = True
        return result

    monkeypatch.setattr(history_import_module, "complete_csv_prefix_size", track_prefix_scan)
    checks = 0

    def cancel_after_second_scan_checkpoint():
        nonlocal checks
        checks += 1
        return checks >= 2

    with pytest.raises(InterruptedError, match="已取消"):
        if loader == "overview":
            load_history_csv(
                source, Mode.DCV, max_plot_points=100,
                is_cancelled=cancel_after_second_scan_checkpoint,
            )
        else:
            load_history_csv_range(
                source, 0, 100, max_plot_points=100,
                is_cancelled=cancel_after_second_scan_checkpoint,
            )

    assert checks == 2
    assert not prefix_scan_completed


def test_history_import_can_cancel_during_events_prefix_scan(tmp_path, monkeypatch):
    source = tmp_path / "measurements.csv"
    _write_measurements(source, [{
        "experiment_id": "run-1", "sample_index": 1, "elapsed_s": 0,
        "mode": "DCV", "unit_si": "V", "value_si": 1, "status": "ok",
    }])
    events = source.with_name("events.csv")
    with events.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("experiment_id", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerows({
            "experiment_id": "run-1", "elapsed_s": index / 10,
            "event": f"event-{index:05d}",
        } for index in range(6000))
    assert events.stat().st_size > 2 * 64 * 1024

    original_prefix_scan = history_import_module.complete_csv_prefix_size
    event_prefix_scan_completed = False

    def track_events_prefix_scan(path, *args, **kwargs):
        nonlocal event_prefix_scan_completed
        result = original_prefix_scan(path, *args, **kwargs)
        if path == events:
            event_prefix_scan_completed = True
        return result

    monkeypatch.setattr(
        history_import_module, "complete_csv_prefix_size", track_events_prefix_scan,
    )
    checks = 0

    def cancel_during_events_scan():
        nonlocal checks
        checks += 1
        return checks >= 4

    with pytest.raises(InterruptedError, match="历史 CSV 加载已取消"):
        load_history_csv(
            source, Mode.DCV, max_plot_points=100,
            is_cancelled=cancel_during_events_scan,
        )

    assert checks == 4
    assert not event_prefix_scan_completed


def test_history_import_ignores_and_reports_unterminated_csv_tails(tmp_path):
    source = tmp_path / "measurements.csv"
    _write_measurements(source, [
        {"experiment_id": "run-1", "sample_index": 1, "elapsed_s": 0,
         "mode": "DCV", "unit_si": "V", "value_si": 1, "status": "ok"},
        {"experiment_id": "run-1", "sample_index": 2, "elapsed_s": 1,
         "mode": "DCV", "unit_si": "V", "value_si": 2, "status": "ok"},
    ])
    with source.open("ab") as stream:
        stream.write(b'run-1,3,2,DCV,V,999,"unfinished status')
    measurement_original = source.read_bytes()

    events_path = source.with_name("events.csv")
    with events_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("experiment_id", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerow({"experiment_id": "run-1", "elapsed_s": 0.5, "event": "valid event"})
    with events_path.open("ab") as stream:
        stream.write(b'run-1,0.75,"unfinished event')
    events_original = events_path.read_bytes()

    overview = load_history_csv(source, Mode.DCV, max_plot_points=20)
    indexed = load_history_csv_range(
        source, 0, 1, max_plot_points=20, range_index=overview["_range_index"],
    )
    unindexed = load_history_csv_range(source, 0, 1, max_plot_points=20)

    assert overview["valid_count"] == 2
    assert overview["row_count"] == 2
    assert overview["stats"]["maximum"] == 2
    assert overview["events"] == [(0.5, "valid event")]
    assert any("measurements.csv" in warning and "未组成完整记录" in warning for warning in overview["warnings"])
    assert any("events.csv" in warning and "未完成" in warning for warning in overview["warnings"])
    assert indexed["valid_count"] == 2
    assert unindexed["valid_count"] == 2
    assert source.read_bytes() == measurement_original
    assert events_path.read_bytes() == events_original


def test_history_import_keeps_syntactically_complete_eof_records_without_newline(tmp_path):
    source = tmp_path / "third-party.csv"
    source.write_bytes(
        "experiment_id,sample_index,elapsed_s,mode,unit_si,value_si,status\r\n"
        "run-1,1,0,DCV,V,1,ok\r\n"
        "run-1,2,1,DCV,V,2,ok"
    .encode("utf-8-sig"))
    events = source.with_name("events.csv")
    events.write_bytes(
        "experiment_id,elapsed_s,event\r\nrun-1,0.5,final event"
    .encode("utf-8-sig"))
    measurement_original = source.read_bytes()
    events_original = events.read_bytes()

    overview = load_history_csv(source, Mode.DCV, max_plot_points=20)
    detail = load_history_csv_range(
        source, 1, 1, max_plot_points=20, range_index=overview["_range_index"],
    )

    assert overview["valid_count"] == 2
    assert overview["row_count"] == 2
    assert overview["y"][-1] == 2
    assert overview["events"] == [(0.5, "final event")]
    assert any("third-party.csv" in warning and "无法判断" in warning for warning in overview["warnings"])
    assert any("events.csv" in warning and "无法判断" in warning for warning in overview["warnings"])
    assert detail["x"] == [1.0]
    assert source.read_bytes() == measurement_original
    assert events.read_bytes() == events_original


def test_history_events_match_full_elapsed_range_when_measurement_times_are_unsorted(tmp_path):
    source = tmp_path / "unsorted-measurements.csv"
    _write_measurements(source, [
        {"experiment_id": "run-unsorted", "sample_index": 1, "elapsed_s": 0,
         "mode": "DCV", "unit_si": "V", "value_si": 1, "status": "ok"},
        {"experiment_id": "run-unsorted", "sample_index": 2, "elapsed_s": 4,
         "mode": "DCV", "unit_si": "V", "value_si": 2, "status": "ok"},
        {"experiment_id": "run-unsorted", "sample_index": 3, "elapsed_s": 2,
         "mode": "DCV", "unit_si": "V", "value_si": 3, "status": "ok"},
    ])
    events = source.with_name("events.csv")
    with events.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("experiment_id", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerow({"experiment_id": "run-unsorted", "elapsed_s": "3", "event": "inside extent"})

    result = load_history_csv(source, Mode.DCV, max_plot_points=10)

    assert result["events"] == [(3.0, "inside extent")]


def test_history_import_peak_downsampling_preserves_spikes_and_discontinuities(tmp_path):
    source = tmp_path / "peaks.csv"
    rows = []
    for index in range(100):
        status = "timeout" if index == 49 else "ok"
        value = 25.0 if index == 20 else -12.0 if index == 77 else 0.0
        rows.append({
            "experiment_id": "run-peaks",
            "sample_index": index + 1,
            "elapsed_s": index,
            "mode": "DCV",
            "unit_si": "V",
            "value_si": "" if status != "ok" else value,
            "status": status,
        })
    _write_measurements(source, rows)

    result = load_history_csv(source, Mode.DCV, max_plot_points=20)

    assert len(result["x"]) <= 20
    assert max(result["y"]) == 25.0
    assert min(result["y"]) == -12.0
    assert any(result["break_before"])


def test_history_import_keeps_multiple_breaks_inside_one_downsample_bucket(tmp_path):
    source = tmp_path / "multiple-gaps.csv"
    rows = []
    for index in range(10):
        rows.append({
            "experiment_id": "run-gaps",
            "sample_index": len(rows) + 1,
            "elapsed_s": index,
            "mode": "DCV",
            "unit_si": "V",
            "value_si": 0.0,
            "status": "ok",
        })
        if index in (2, 6):
            rows.append({
                "experiment_id": "run-gaps",
                "sample_index": len(rows) + 1,
                "elapsed_s": index + 0.5,
                "mode": "DCV",
                "unit_si": "V",
                "value_si": "",
                "status": "timeout",
            })
    _write_measurements(source, rows)

    result = load_history_csv(source, Mode.DCV, max_plot_points=5)

    assert result["x"] == [0.0, 3.0, 9.0]
    assert result["break_before"] == [False, True, True]


def test_history_range_load_keeps_peaks_gaps_and_only_returns_visible_window(tmp_path):
    source = tmp_path / "range.csv"
    rows = []
    for index in range(100):
        status = "timeout" if index == 49 else "ok"
        value = 25.0 if index == 20 else -12.0 if index == 77 else 0.0
        rows.append({
            "experiment_id": "range-run",
            "sample_index": index + 1,
            "elapsed_s": index,
            "mode": "DCV",
            "unit_si": "V",
            "value_si": "" if status != "ok" else value,
            "status": status,
        })
    _write_measurements(source, rows)

    result = load_history_csv_range(source, 45.0, 82.0, max_plot_points=12)

    assert 45.0 <= result["x"][0] <= result["x"][-1] <= 82.0
    assert len(result["x"]) <= 12
    assert max(result["y"]) == 0.0
    assert min(result["y"]) == -12.0
    assert any(result["break_before"])
    assert result["valid_count"] == 37


def test_loaded_history_reuses_sparse_index_for_narrow_late_range(tmp_path):
    source = tmp_path / "indexed-history.csv"
    _write_measurements(source, [
        {
            "experiment_id": "indexed-run", "sample_index": index + 1,
            "elapsed_s": index, "mode": "DCV", "unit_si": "V",
            "value_si": index % 13, "status": "ok",
        }
        for index in range(10_000)
    ])

    overview = load_history_csv(source, Mode.DCV, max_plot_points=100)
    indexed = load_history_csv_range(
        source, 9_000.0, 9_010.0, max_plot_points=100,
        range_index=overview["_range_index"],
    )
    reference = load_history_csv_range(source, 9_000.0, 9_010.0, max_plot_points=100)

    assert indexed["x"] == reference["x"]
    assert indexed["y"] == reference["y"]
    assert indexed["metadata"] == reference["metadata"]
    assert indexed["break_before"] == reference["break_before"]
    assert indexed["valid_count"] == reference["valid_count"]
    assert indexed["rows_seen"] < 512
    assert reference["rows_seen"] == 10_000


def test_history_range_index_handles_multiline_csv_fields_before_checkpoint(tmp_path):
    source = tmp_path / "multiline-indexed-history.csv"
    fields = (
        "experiment_id", "sample_index", "elapsed_s", "mode", "unit_si",
        "value_si", "status", "raw_reading",
    )
    with source.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index in range(2_000):
            writer.writerow({
                "experiment_id": "multiline-run", "sample_index": index + 1,
                "elapsed_s": index, "mode": "DCV", "unit_si": "V",
                "value_si": index, "status": "ok",
                "raw_reading": '"quoted, text"\ncontinued' if index == 300 else str(index),
            })

    overview = load_history_csv(source, Mode.DCV, max_plot_points=100)
    result = load_history_csv_range(
        source, 1_900.0, 1_910.0, max_plot_points=100,
        range_index=overview["_range_index"],
    )

    assert result["x"] == list(map(float, range(1_900, 1_911)))
    assert result["y"] == list(map(float, range(1_900, 1_911)))
    assert result["rows_seen"] < 512


def test_history_range_index_preserves_gap_state_at_checkpoint(tmp_path):
    source = tmp_path / "gap-checkpoint-history.csv"
    _write_measurements(source, [
        {
            "experiment_id": "gap-run", "sample_index": index + 1,
            "elapsed_s": index, "mode": "DCV", "unit_si": "V",
            "value_si": "" if index == 255 else index,
            "status": "timeout" if index == 255 else "ok",
        }
        for index in range(600)
    ])

    overview = load_history_csv(source, Mode.DCV, max_plot_points=100)
    indexed = load_history_csv_range(
        source, 256.0, 260.0, max_plot_points=20,
        range_index=overview["_range_index"],
    )
    reference = load_history_csv_range(source, 256.0, 260.0, max_plot_points=20)

    for field in ("x", "y", "break_before", "metadata", "valid_count"):
        assert indexed[field] == reference[field]
    assert indexed["break_before"][0]
    assert indexed["rows_seen"] < 20


def test_history_range_index_falls_back_when_csv_changes_or_elapsed_is_unsorted(tmp_path):
    source = tmp_path / "mutable-history.csv"
    _write_measurements(source, [
        {
            "experiment_id": "mutable-run", "sample_index": index + 1,
            "elapsed_s": elapsed, "mode": "DCV", "unit_si": "V",
            "value_si": index, "status": "ok",
        }
        for index, elapsed in enumerate((0, 1, 3, 2, 4))
    ])

    unordered = load_history_csv(source, Mode.DCV, max_plot_points=20)
    assert not unordered["_range_index"].monotonic_elapsed
    unordered_result = load_history_csv_range(
        source, 2.0, 4.0, max_plot_points=20,
        range_index=unordered["_range_index"],
    )
    assert unordered_result["x"] == [3.0, 2.0, 4.0]
    assert unordered_result["rows_seen"] == 5

    with source.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("mutable-run", 6, 5, "DCV", "V", 5, "ok"))
    changed_result = load_history_csv_range(
        source, 4.0, 5.0, max_plot_points=20,
        range_index=unordered["_range_index"],
    )
    assert changed_result["x"] == [4.0, 5.0]
    assert changed_result["rows_seen"] == 6


@pytest.mark.parametrize(
    ("measurement_ids", "statuses", "valid_count"),
    [
        (["run-1", ""], ["ok", "timeout"], 1),
        (["run-1", "run-2"], ["ok", "ok"], 2),
    ],
)
def test_measurement_rows_with_missing_or_mixed_ids_load_without_pairing_events(
    tmp_path, measurement_ids, statuses, valid_count,
):
    source = tmp_path / "measurements.csv"
    _write_measurements(source, [
        {"experiment_id": experiment_id, "sample_index": index, "elapsed_s": index - 1,
         "mode": "DCV", "unit_si": "V", "value_si": index, "status": status}
        for index, (experiment_id, status) in enumerate(zip(measurement_ids, statuses), 1)
    ])
    events = source.with_name("events.csv")
    with events.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("experiment_id", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerow({"experiment_id": "run-1", "elapsed_s": "0", "event": "must not attach"})

    result = load_history_csv(source, Mode.DCV, max_plot_points=10)

    assert result["valid_count"] == valid_count
    assert result["events"] == []
    assert any("测量 CSV" in warning and "experiment_id" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("event_ids", "warning_fragment"),
    [
        (["run-1", ""], "缺失"),
        (["run-1", "run-2"], "不一致"),
    ],
)
def test_events_are_not_paired_if_any_event_id_is_missing_or_mixed(
    tmp_path, event_ids, warning_fragment,
):
    source = tmp_path / "measurements.csv"
    _write_measurements(source, [
        {"experiment_id": "run-1", "sample_index": 1, "elapsed_s": 0,
         "mode": "DCV", "unit_si": "V", "value_si": 1, "status": "ok"},
    ])
    events = source.with_name("events.csv")
    with events.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=("experiment_id", "elapsed_s", "event"))
        writer.writeheader()
        writer.writerows([
            {"experiment_id": experiment_id, "elapsed_s": "0", "event": f"event {index}"}
            for index, experiment_id in enumerate(event_ids, 1)
        ])

    result = load_history_csv(source, Mode.DCV, max_plot_points=10)

    assert result["valid_count"] == 1
    assert result["events"] == []
    assert any("events.csv" in warning and warning_fragment in warning for warning in result["warnings"])
