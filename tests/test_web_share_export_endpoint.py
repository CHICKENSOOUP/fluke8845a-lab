from __future__ import annotations

import io
import csv
import json
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

pytest_plugins = ("tests.test_webapp",)

from tests.test_webapp import _http, _main_thread_wait


def _write_experiment(directory: Path, marker: str = "current") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "measurements.csv").write_bytes(
        f"timestamp_local,elapsed_s,value_si,unit_si,mode,status,raw_reading,error\r\n"
        f"2026-10-17T00:00:00+08:00,0,1.25,V,DCV,ok,1.250000 V,COM3 serial TEST8845A001 at {directory}\r\n".encode()
    )
    (directory / "events.csv").write_bytes(
        f"experiment_id,elapsed_s,event\r\nrun-1,0,{marker} event on COM3 serial TEST8845A001 at {directory}\r\n".encode()
    )
    (directory / "config.json").write_text(json.dumps({
        "serial_settings": {"port": "COM3", "baudrate": 115200},
        "device": {"serial_number": "TEST8845A001", "idn": "FLUKE,8845A,TEST8845A001,02/14/08-08:54"},
        "experiment_dir": str(directory),
        "description": f"COM3 · serial TEST8845A001 · {directory}",
    }), encoding="utf-8")
    (directory / "run.log").write_text("do not include this file", encoding="utf-8")
    (directory / "extra-secret.txt").write_text("do not include unknown files", encoding="utf-8")


def test_share_bundle_download_is_derived_from_last_experiment_and_redacts_config(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "finished-run"
    _write_experiment(run_dir, marker="=1+2")
    measurement_bytes = (run_dir / "measurements.csv").read_bytes()
    event_bytes = (run_dir / "events.csv").read_bytes()
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    status, headers, body = _main_thread_wait(app, lambda: _http(
        base_url + "/api/latest-share-bundle.zip?path=" + quote(str(tmp_path / "attacker"), safe="")
    ))

    assert status == 200
    assert headers.get_content_type() == "application/zip"
    disposition = headers.get("Content-Disposition", "")
    assert "Fluke8845A" in disposition and "filename*=UTF-8''" in disposition
    assert "%E8%84%B1%E6%95%8F" in disposition
    assert str(tmp_path) not in disposition
    with ZipFile(io.BytesIO(body)) as archive:
        assert set(archive.namelist()) == {"config.json", "measurements.csv", "events.csv"}
        measurements_text = archive.read("measurements.csv").decode("utf-8-sig")
        measurement_rows = list(csv.DictReader(io.StringIO(measurements_text)))
        assert measurement_rows[0]["value_si"] == "1.25"
        assert measurement_rows[0]["raw_reading"] == "1.250000 V"
        assert "COM3" not in measurement_rows[0]["error"]
        assert "TEST8845A001" not in measurement_rows[0]["error"]
        assert str(tmp_path) not in measurement_rows[0]["error"]
        events_text = archive.read("events.csv").decode("utf-8-sig")
        event_rows = list(csv.DictReader(io.StringIO(events_text)))
        assert event_rows[0]["event"].startswith("'=1+2 event")
        assert "COM3" not in event_rows[0]["event"]
        assert "TEST8845A001" not in event_rows[0]["event"]
        assert str(tmp_path) not in event_rows[0]["event"]
        sanitized = archive.read("config.json").decode("utf-8")
        assert "[已脱敏]" in sanitized
        assert "[本地路径已隐藏]" in sanitized
        assert "COM3" not in sanitized
        assert "TEST8845A001" not in sanitized
        assert str(tmp_path) not in sanitized
        assert "run.log" not in archive.namelist()
        assert "extra-secret.txt" not in archive.namelist()
    assert (run_dir / "measurements.csv").read_bytes() == measurement_bytes
    assert (run_dir / "events.csv").read_bytes() == event_bytes


def test_share_bundle_rejects_active_or_missing_experiment_without_a_download(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "active-run"
    _write_experiment(run_dir)

    runtime.store.update(experiment_dir=str(run_dir), last_experiment_dir=str(run_dir))
    active_status, active_headers, active_body = _main_thread_wait(app, lambda: _http(base_url + "/api/latest-share-bundle.zip"))
    assert active_status == 409
    assert "Content-Disposition" not in active_headers
    assert "停止采集" in json.loads(active_body)["error"]

    runtime.store.update(experiment_dir="", last_experiment_dir="")
    missing_status, missing_headers, missing_body = _main_thread_wait(app, lambda: _http(base_url + "/api/latest-share-bundle.zip"))
    assert missing_status == 404
    assert "Content-Disposition" not in missing_headers
    assert "实验" in json.loads(missing_body)["error"]


def test_share_bundle_reports_missing_required_source_files_as_json_error(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "incomplete-run"
    run_dir.mkdir()
    (run_dir / "measurements.csv").write_text("elapsed_s,value_si,status\n0,1,ok\n", encoding="utf-8")
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    status, headers, body = _main_thread_wait(app, lambda: _http(base_url + "/api/latest-share-bundle.zip"))

    assert status == 400
    assert headers.get_content_type() == "application/json"
    assert "Content-Disposition" not in headers
    error_text = json.loads(body)["error"]
    assert "config.json" in error_text
    assert str(run_dir) not in error_text
