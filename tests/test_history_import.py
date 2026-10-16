import csv

import pytest

from fluke8845a.history_import import load_history_csv
from fluke8845a.modes import Mode


def _write_measurements(path, rows):
    fields = ("experiment_id", "sample_index", "elapsed_s", "mode", "unit_si", "value_si", "status")
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_history_import_validates_stats_and_downsamples_without_editing_source(tmp_path):
    source = tmp_path / "measurements.csv"
    rows = [
        {"experiment_id": "run-1", "sample_index": index, "elapsed_s": index - 1,
         "mode": "DCV", "unit_si": "V", "value_si": index * 2, "status": "ok"}
        for index in range(1, 11)
    ]
    _write_measurements(source, rows)
    original = source.read_bytes()

    result = load_history_csv(source, Mode.RES2, max_plot_points=4)

    assert result["valid_count"] == 10
    assert result["mode"] == "DCV"
    assert len(result["x"]) <= 4
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
