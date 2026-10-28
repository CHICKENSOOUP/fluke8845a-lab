import json
import csv
from urllib.parse import urlencode

import pytest

from tests.test_webapp import _http, _main_thread_wait, local_service, qt_app
from fluke8845a import webapp


def _average_url(server, runtime, directory, **kwargs):
    query = {"generation": runtime.store.snapshot()["generation"], "directory": str(directory), **kwargs}
    return f"http://127.0.0.1:{server.server_port}/api/chart/moving-average?{urlencode(query)}"


def test_average_history_reads_all_samples_and_preserves_gap(local_service, tmp_path):
    app, runtime, server = local_service
    source = tmp_path / "measurements.csv"
    with source.open("w", newline="", encoding="utf-8") as stream:
        stream.write("sample_index,elapsed_s,value_si,status,unit_si\n")
        for i in range(1, 5102):
            if i == 5050:
                stream.write(f"{i},{i},,error,A\n")
            else:
                stream.write(f"{i},{i},{i},ok,A\n")
        stream.write("5102,unfinished")
    runtime.store.update(experiment_dir=str(tmp_path), last_experiment_dir="")
    status, _headers, body = _main_thread_wait(app, lambda: _http(
        _average_url(server, runtime, tmp_path, window=10000, width=4096)))
    assert status == 200
    result = json.loads(body)
    rows = result["rows"]
    assert result["total_attempts"] == 5101
    assert rows[0]["id"] == 1 and rows[-1]["id"] == 5101
    assert rows[-1]["y"] == pytest.approx((5051 + 5101) / 2)
    assert any(row["break_before"] and row["id"] >= 5051 for row in rows)
    assert len(rows) <= 6 * 4096


@pytest.mark.parametrize("query", ["window=0", "window=100001", "window=bad", "width=0", "width=4097"])
def test_average_history_rejects_invalid_parameters(local_service, query):
    app, _runtime, server = local_service
    status, _headers, _body = _main_thread_wait(app, lambda: _http(
        f"http://127.0.0.1:{server.server_port}/api/chart/moving-average?{query}"))
    assert status == 400


def test_average_history_streams_source_without_generating_csv(local_service, tmp_path, monkeypatch):
    app, runtime, server = local_service
    source = tmp_path / "measurements.csv"
    source.write_text("sample_index,elapsed_s,value_si,status\n1,0,2,ok\n3,1,8,ok\n4,2,10,ok\n")
    runtime.store.update(experiment_dir=str(tmp_path), last_experiment_dir="")
    def no_export(*_args, **_kwargs):
        raise AssertionError("history must not generate full CSV files")
    monkeypatch.setattr(webapp, "write_curve_csv", no_export)
    status, _headers, body = _main_thread_wait(app, lambda: _http(
        _average_url(server, runtime, tmp_path, window=2, width=100)))
    assert status == 200
    rows = json.loads(body)["rows"]
    assert [row["y"] for row in rows] == [2, 8, 9]
    assert rows[1]["break_before"]


def test_average_history_matches_curve_export(local_service, tmp_path):
    from fluke8845a.curve_export import write_curve_csv
    app, runtime, server = local_service
    source = tmp_path / "measurements.csv"
    source.write_text("sample_index,elapsed_s,value_si,status\n1,0,2,ok\n2,1,4,ok\n3,2,,error\n5,3,-1,ok\n6,4,5,ok\n")
    target = tmp_path / "reference.csv"
    write_curve_csv(source, target, 3)
    with target.open(encoding="utf-8-sig", newline="") as stream:
        expected = [float(row["moving_average_value_si"]) for row in csv.DictReader(stream)
                    if row["moving_average_value_si"]]
    runtime.store.update(experiment_dir=str(tmp_path), last_experiment_dir="")
    status, _headers, body = _main_thread_wait(app, lambda: _http(
        _average_url(server, runtime, tmp_path, window=3, width=100)))
    assert status == 200
    assert [row["y"] for row in json.loads(body)["rows"]] == expected


def test_average_history_rejects_stale_experiment(local_service, tmp_path):
    app, runtime, server = local_service
    (tmp_path / "measurements.csv").write_text("sample_index,elapsed_s,value_si,status\n1,0,1,ok\n")
    runtime.store.begin_experiment(str(tmp_path))
    url = _average_url(server, runtime, tmp_path)
    runtime.store.begin_experiment(str(tmp_path))
    status, _headers, _body = _main_thread_wait(app, lambda: _http(url))
    assert status == 409


def test_average_history_seed_includes_bounded_recent_attempts(local_service, tmp_path):
    app, runtime, server = local_service
    (tmp_path / "measurements.csv").write_text(
        "sample_index,elapsed_s,value_si,status\n1,0,1,ok\n2,1,2,ok\n3,2,,error\n4,3,4,ok\n")
    runtime.store.begin_experiment(str(tmp_path))
    status, _headers, body = _main_thread_wait(app, lambda: _http(
        _average_url(server, runtime, tmp_path, window=3, seed=1)))
    assert status == 200
    seed = json.loads(body)["seed_rows"]
    assert [row["id"] for row in seed] == [2, 3, 4]
    assert [row["status"] for row in seed] == ["ok", "error", "ok"]
    assert seed[-1]["break_before"] is True


def test_average_history_rechecks_experiment_before_response(local_service, tmp_path, monkeypatch):
    app, runtime, server = local_service
    (tmp_path / "measurements.csv").write_text("sample_index,elapsed_s,value_si,status\n1,0,1,ok\n")
    runtime.store.begin_experiment(str(tmp_path))
    url = _average_url(server, runtime, tmp_path)
    original = webapp._downsample_chart_points
    def switch(*args):
        result = original(*args)
        runtime.store.begin_experiment(str(tmp_path))
        return result
    monkeypatch.setattr(webapp, "_downsample_chart_points", switch)
    status, _headers, _body = _main_thread_wait(app, lambda: _http(url))
    assert status == 409


def test_average_history_requires_identity(local_service):
    app, _runtime, server = local_service
    status, _headers, _body = _main_thread_wait(app, lambda: _http(
        f"http://127.0.0.1:{server.server_port}/api/chart/moving-average"))
    assert status == 400
