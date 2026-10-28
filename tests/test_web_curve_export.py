import csv
import io
from pathlib import Path
import zipfile
from urllib.parse import urlencode

import pytest

from tests.test_webapp import _http, _main_thread_wait, local_service, qt_app
from fluke8845a import path_security


def _export_url(server, runtime, directory, **kwargs):
    query = {"generation": runtime.store.snapshot()["generation"], "directory": str(directory), **kwargs}
    return f"http://127.0.0.1:{server.server_port}/api/curve-export.zip?{urlencode(query)}"


def test_curve_export_contains_both_csvs_from_complete_snapshot(tmp_path, local_service):
    app, runtime, server = local_service
    source = tmp_path / "measurements.csv"
    original = (b"sample_index,timestamp_local,elapsed_s,value_si,unit_si,status,raw_reading\r\n"
                b"1,2026-01-01T00:00:00,0,1,A,ok,1.0\r\n"
                b"2,2026-01-01T00:00:01,1,3,A,ok,3.0\r\n"
                b"3,partial")
    source.write_bytes(original)
    runtime.store.update(experiment_dir=str(tmp_path), last_experiment_dir="")
    status, headers, body = _main_thread_wait(app, lambda: _http(
        _export_url(server, runtime, tmp_path, window=2)))
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert headers["X-8845A-Export-State"] == "in-progress-snapshot"
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert set(archive.namelist()) == {"curve.csv", "curve_detailed.csv"}
        simple = list(csv.DictReader(io.StringIO(archive.read("curve.csv").decode("utf-8-sig"))))
        detailed = list(csv.DictReader(io.StringIO(archive.read("curve_detailed.csv").decode("utf-8-sig"))))
    assert [row["moving_average_value_si"] for row in simple] == ["1.0", "2.0"]
    assert [row["raw_reading"] for row in detailed] == ["1.0", "3.0"]
    assert source.read_bytes() == original


@pytest.mark.parametrize("window", ["0", "100001", "abc", "1.5"])
def test_curve_export_rejects_invalid_average_window(local_service, window):
    app, _runtime, server = local_service
    status, _headers, _body = _main_thread_wait(app, lambda: _http(
        f"http://127.0.0.1:{server.server_port}/api/curve-export.zip?window={window}"))
    assert status == 400


def test_curve_export_rejects_linked_source(tmp_path, local_service, monkeypatch):
    app, runtime, server = local_service
    source = tmp_path / "measurements.csv"
    source.write_text("sample_index,elapsed_s,value_si,status\n1,0,1,ok\n")
    runtime.store.update(experiment_dir="", last_experiment_dir=str(tmp_path))
    original = path_security.is_symlink_or_junction
    monkeypatch.setattr(path_security, "is_symlink_or_junction", lambda path: Path(path) == source or original(path))
    status, _headers, _body = _main_thread_wait(app, lambda: _http(
        _export_url(server, runtime, tmp_path, window=2)))
    assert status == 404


def test_curve_export_reports_invalid_source(local_service, tmp_path):
    app, runtime, server = local_service
    (tmp_path / "measurements.csv").write_text("broken,header\n1,2\n")
    runtime.store.update(experiment_dir="", last_experiment_dir=str(tmp_path))
    status, _headers, body = _main_thread_wait(app, lambda: _http(
        _export_url(server, runtime, tmp_path)))
    assert status == 500
    assert b"error" in body


def test_curve_export_rejects_stale_experiment(local_service, tmp_path):
    app, runtime, server = local_service
    first = tmp_path / "old"
    second = tmp_path / "new"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        (directory / "measurements.csv").write_text("sample_index,elapsed_s,value_si,status\n1,0,1,ok\n")
    runtime.store.update(experiment_dir=str(first), last_experiment_dir="")
    stale_url = _export_url(server, runtime, first)
    runtime.store.update(experiment_dir=str(second), last_experiment_dir="")
    status, _headers, body = _main_thread_wait(app, lambda: _http(stale_url))
    assert status == 409
    assert b"error" in body


def test_curve_export_requires_experiment_identity(local_service):
    app, _runtime, server = local_service
    status, _headers, _body = _main_thread_wait(app, lambda: _http(
        f"http://127.0.0.1:{server.server_port}/api/curve-export.zip"))
    assert status == 400


def test_curve_export_rejects_new_generation_with_same_directory(local_service, tmp_path):
    app, runtime, server = local_service
    (tmp_path / "measurements.csv").write_text("sample_index,elapsed_s,value_si,status\n1,0,1,ok\n")
    runtime.store.begin_experiment(str(tmp_path))
    stale_url = _export_url(server, runtime, tmp_path)
    runtime.store.begin_experiment(str(tmp_path))
    status, _headers, _body = _main_thread_wait(app, lambda: _http(stale_url))
    assert status == 409


def test_curve_export_rechecks_identity_after_generating(local_service, tmp_path, monkeypatch):
    from fluke8845a import webapp
    app, runtime, server = local_service
    (tmp_path / "measurements.csv").write_text("sample_index,elapsed_s,value_si,status\n1,0,1,ok\n")
    runtime.store.begin_experiment(str(tmp_path))
    url = _export_url(server, runtime, tmp_path)
    original = webapp.write_curve_csv
    def change_generation(*args):
        result = original(*args)
        runtime.store.begin_experiment(str(tmp_path))
        return result
    monkeypatch.setattr(webapp, "write_curve_csv", change_generation)
    status, _headers, _body = _main_thread_wait(app, lambda: _http(url))
    assert status == 409
