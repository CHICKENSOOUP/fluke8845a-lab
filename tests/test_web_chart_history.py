from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from PySide6.QtCore import QCoreApplication

from fluke8845a.storage import SAMPLE_FIELDS
from fluke8845a.webapp import create_server
from fluke8845a.webservice import WebRuntime
import fluke8845a.webservice as webservice_module

LOCAL_HTTP_OPENER = build_opener(ProxyHandler({}))


@pytest.fixture
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def chart_server(qt_app):
    runtime = WebRuntime()
    runtime.start()
    server = create_server(runtime, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield runtime, server
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)
    runtime.stop()


def _get(base_url: str, route: str):
    request = Request(base_url + route, headers={"Cache-Control": "no-store"})
    try:
        with LOCAL_HTTP_OPENER.open(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _write_measurements(path: Path, count: int = 1000) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()
        for index in range(count):
            status = "timeout" if index == 400 else "ok"
            value = 20.0 if index == 150 else 1.0 + (index % 7) / 100
            row = {field: "" for field in SAMPLE_FIELDS}
            row.update({
                "experiment_id": "chart-test",
                "sample_index": index + 1,
                "elapsed_s": repr(float(index)),
                "mode": "DCV",
                "value_si": repr(value) if status == "ok" else "",
                "unit_si": "V",
                "raw_reading": repr(value) if status == "ok" else "",
                "status": status,
                "error": "timeout" if status != "ok" else "",
            })
            writer.writerow(row)


def _wait_overview(base_url: str, *, path_query: str = ""):
    deadline = time.monotonic() + 4
    payload = {}
    while time.monotonic() < deadline:
        status, payload = _get(base_url, f"/api/chart/overview?width=120{path_query}")
        assert status == 200, payload
        if payload.get("status") == "ready":
            return payload
        time.sleep(0.02)
    raise AssertionError(f"chart index did not finish within the short test deadline: {payload}")


def test_chart_overview_is_full_run_bounded_and_range_detail_reads_original_csv(tmp_path, chart_server):
    runtime, server = chart_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "known-run"
    run_dir.mkdir()
    csv_path = run_dir / "measurements.csv"
    _write_measurements(csv_path)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    overview = _wait_overview(base_url)

    assert overview["total_attempts"] == 1000
    assert overview["total_valid"] == 999
    assert overview["rows"][0]["x"] == 0
    assert overview["rows"][-1]["x"] == 999
    assert max(point["y"] for point in overview["rows"]) == 20
    assert any(point["break_before"] for point in overview["rows"])
    assert len(overview["rows"]) <= 120 * 8

    request_id = "chart-range-test-0001"
    route = (
        f"/api/chart/range?job_id={overview['job_id']}&request_id={request_id}"
        "&start=100&end=200&width=120"
    )
    status, started = _get(base_url, route)
    assert status in (200, 202), started
    deadline = time.monotonic() + 3
    detail = started
    while time.monotonic() < deadline and detail.get("status") in ("running", "indexing"):
        time.sleep(0.02)
        _status, detail = _get(base_url, f"/api/chart/range?request_id={request_id}")
    assert detail.get("status") == "ready", detail
    assert all(100 <= point["x"] <= 200 for point in detail["rows"])
    assert any(point["x"] == 150 and point["y"] == 20 for point in detail["rows"])
    assert csv_path.is_file(), "chart indexing must leave the original CSV in place"


def test_chart_range_includes_sample_at_checkpoint_timestamp(tmp_path, chart_server):
    runtime, _server = chart_server
    run_dir = tmp_path / "checkpoint-boundary-run"
    run_dir.mkdir()
    _write_measurements(run_dir / "measurements.csv", count=300)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))
    overview = runtime.chart_history.overview(100)[1]
    deadline = time.monotonic() + 3
    while overview.get("status") != "ready" and time.monotonic() < deadline:
        time.sleep(0.01)
        overview = runtime.chart_history.overview(100)[1]
    assert overview.get("status") == "ready", overview

    request_id = "checkpoint-boundary-range-0001"
    status, started = runtime.chart_history.start_range(
        overview["job_id"], request_id, 255.0, 260.0, 100,
    )
    assert status in (200, 202), started
    deadline = time.monotonic() + 2
    detail = started
    while detail.get("status") in {"running", "indexing"} and time.monotonic() < deadline:
        time.sleep(0.01)
        status, detail = runtime.chart_history.range_status(request_id)
    assert status == 200 and detail.get("status") == "ready", detail
    assert [point["x"] for point in detail["rows"]] == [255.0, 256.0, 257.0, 258.0, 259.0, 260.0]


def test_chart_bucket_preserves_all_discontinuities_between_representatives():
    bucket = webservice_module._ChartBucket()
    for sample_index in range(100):
        bucket.add({
            "id": sample_index,
            "x": float(sample_index),
            "y": 5.0 if sample_index == 22 else 1.0,
            "break_before": sample_index in {10, 20, 30},
        })

    points = bucket.points()

    assert [point["id"] for point in points] == [0, 10, 22, 30, 99]
    assert [point["break_before"] for point in points] == [False, True, True, True, False]


def test_chart_bucket_merge_carries_discontinuities_forward():
    def make_bucket(start, end, gaps, peak=None):
        bucket = webservice_module._ChartBucket()
        for sample_index in range(start, end):
            bucket.add({
                "id": sample_index,
                "x": float(sample_index),
                "y": 5.0 if sample_index == peak else 1.0,
                "break_before": sample_index in gaps,
            })
        return bucket

    left = make_bucket(0, 40, {10, 20, 30}, peak=22)
    right = make_bucket(40, 80, {50, 60, 70})
    left.merge(right)

    points = left.points()
    for gap_index in {10, 20, 30, 50, 60, 70}:
        next_retained = next(point for point in points if point["id"] >= gap_index)
        assert next_retained["break_before"], f"gap before sample {gap_index} was lost"


def test_chart_downsampling_preserves_gap_when_sample_ids_repeat_at_different_times():
    rows = [
        {"id": 0, "x": 0.0, "y": 1.0, "break_before": False},
        {"id": 2, "x": 2.0, "y": 1.0, "break_before": True},
        {"id": 3, "x": 3.0, "y": 2.0, "break_before": False},
        {"id": 2, "x": 4.0, "y": 1.0, "break_before": True},
    ]

    points = webservice_module._downsample_chart_points(rows, 0.0, 4.0, 1)

    first_after_gap = next(point for point in points if point["x"] >= 2.0)
    assert first_after_gap["break_before"], points


def test_chart_endpoints_derive_csv_path_from_runtime_and_do_not_accept_a_path(tmp_path, chart_server):
    runtime, server = chart_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    selected = tmp_path / "selected-run"
    selected.mkdir()
    _write_measurements(selected / "measurements.csv", count=40)
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_measurements(outside / "measurements.csv", count=10)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(selected))

    rejected_path = urlencode({"path": str(outside / "measurements.csv")})
    overview = _wait_overview(base_url, path_query=f"&{rejected_path}")

    assert overview["total_attempts"] == 40
    assert overview["rows"][-1]["x"] == 39


def test_chart_index_scan_is_started_off_the_http_request_thread(tmp_path, chart_server, monkeypatch):
    runtime, server = chart_server
    run_dir = tmp_path / "slow-index-run"
    run_dir.mkdir()
    _write_measurements(run_dir / "measurements.csv", count=100)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))
    release_scan = threading.Event()
    started_scan = threading.Event()
    original_scan = runtime.chart_history._scan_index

    def blocked_scan(job):
        started_scan.set()
        release_scan.wait(2)
        original_scan(job)

    monkeypatch.setattr(runtime.chart_history, "_scan_index", blocked_scan)
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        started_at = time.monotonic()
        status, payload = _get(base_url, "/api/chart/overview?width=100")
        elapsed = time.monotonic() - started_at
        assert status == 200, payload
        assert elapsed < 0.25, f"HTTP request waited for CSV scan ({elapsed:.3f}s)"
        assert started_scan.wait(1)
        assert payload["status"] == "scanning"
    finally:
        release_scan.set()


def test_chart_index_snapshot_waits_for_atomic_overview_update(tmp_path, chart_server, monkeypatch):
    runtime, _server = chart_server
    run_dir = tmp_path / "atomic-index-run"
    run_dir.mkdir()
    _write_measurements(run_dir / "measurements.csv", count=2)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    entered_update = threading.Event()
    release_update = threading.Event()
    original_add_attempt = webservice_module._ChartOverview.add_attempt

    def blocked_add_attempt(overview, *args):
        entered_update.set()
        assert release_update.wait(2)
        return original_add_attempt(overview, *args)

    monkeypatch.setattr(webservice_module._ChartOverview, "add_attempt", blocked_add_attempt)
    status, initial = runtime.chart_history.overview(100)
    assert status == 200, initial
    job = runtime.chart_history._job
    assert job is not None
    assert entered_update.wait(1)

    snapshot_done = threading.Event()
    snapshots = []
    snapshot_thread = threading.Thread(
        target=lambda: (snapshots.append(job.snapshot(100)), snapshot_done.set()), daemon=True
    )
    snapshot_thread.start()
    try:
        assert not snapshot_done.wait(0.05), "snapshot must not observe a partially updated overview"
    finally:
        release_update.set()
    assert snapshot_done.wait(1)
    snapshot_thread.join(timeout=1)
    job.thread.join(timeout=1)
    assert snapshots[0]["total_attempts"] >= 1


def test_completed_chart_ranges_do_not_consume_active_request_limit(tmp_path, chart_server):
    runtime, _server = chart_server
    run_dir = tmp_path / "many-range-requests"
    run_dir.mkdir()
    _write_measurements(run_dir / "measurements.csv", count=20)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status, overview = runtime.chart_history.overview(100)
        assert status == 200, overview
        if overview["status"] == "ready":
            break
        time.sleep(0.01)
    assert overview["status"] == "ready", overview

    for index in range(9):
        request_id = f"range-request-{index:08d}"
        status, payload = runtime.chart_history.start_range(
            overview["job_id"], request_id, 0, 19, 100
        )
        assert status == 202, payload
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            status, payload = runtime.chart_history.range_status(request_id)
            if status == 200 and payload["status"] != "running":
                break
            time.sleep(0.01)
        assert status == 200 and payload["status"] == "ready", payload


def test_recently_completed_chart_range_survives_ttl_from_creation(tmp_path, chart_server):
    runtime, _server = chart_server
    index = webservice_module._ChartIndexJob(tmp_path / "unused.csv", lambda: True)
    request = webservice_module._ChartRangeJob("range-ttl-test-0001", index, 0, 1, 10)
    request.created = time.monotonic() - webservice_module._CHART_RANGE_JOB_TTL_S - 1
    request.finished_at = time.monotonic()
    request.status = "ready"
    request.rows = [{"x": 0.5, "y": 2.0}]
    runtime.chart_history._range_jobs[request.request_id] = request

    status, payload = runtime.chart_history.range_status(request.request_id)

    assert status == 200, payload
    assert payload["status"] == "ready"
    assert payload["rows"] == request.rows


def test_cancel_received_before_range_start_prevents_later_scan(tmp_path, chart_server):
    runtime, server = chart_server
    run_dir = tmp_path / "cancel-before-start"
    run_dir.mkdir()
    _write_measurements(run_dir / "measurements.csv", count=20)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))
    overview = _wait_overview(f"http://127.0.0.1:{server.server_port}")

    request_id = "cancel-before-start-0001"
    cancel_status, cancelled = runtime.chart_history.cancel_range(request_id)
    start_status, started = runtime.chart_history.start_range(
        overview["job_id"], request_id, 0, 19, 100
    )

    assert cancel_status == 200, cancelled
    assert cancelled["status"] == "cancelled"
    assert start_status == 200, started
    assert started["status"] == "cancelled"
    assert request_id not in runtime.chart_history._range_jobs


def test_cancelled_range_thread_keeps_capacity_until_scan_exits(tmp_path, chart_server, monkeypatch):
    runtime, server = chart_server
    run_dir = tmp_path / "cancel-running-range"
    run_dir.mkdir()
    _write_measurements(run_dir / "measurements.csv", count=20)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))
    overview = _wait_overview(f"http://127.0.0.1:{server.server_port}")

    release_workers = threading.Event()
    first_worker_started = threading.Event()

    def blocked_scan(request):
        first_worker_started.set()
        release_workers.wait()
        with request.lock:
            request.status = "cancelled" if request.cancel_event.is_set() else "ready"
            request.finished_at = time.monotonic()

    monkeypatch.setattr(runtime.chart_history, "_scan_range", blocked_scan)
    first_id = "cancel-running-range-0001"
    try:
        status, started = runtime.chart_history.start_range(
            overview["job_id"], first_id, 0, 19, 100
        )
        assert status == 202, started
        assert first_worker_started.wait(1)
        cancel_status, cancelling = runtime.chart_history.cancel_range(first_id)
        assert cancel_status == 200, cancelling
        assert cancelling["status"] == "cancelling"
        assert runtime.chart_history._range_jobs[first_id].finished_at is None

        accepted = 0
        last_status = 0
        last_payload = {}
        for index in range(8):
            request_id = f"capacity-after-cancel-{index:08d}"
            last_status, last_payload = runtime.chart_history.start_range(
                overview["job_id"], request_id, 0, 19, 100
            )
            if last_status == 429:
                break
            assert last_status == 202, last_payload
            accepted += 1

        assert accepted == 7
        assert last_status == 429, last_payload
    finally:
        release_workers.set()
        for request in list(runtime.chart_history._range_jobs.values()):
            if request.thread is not None:
                request.thread.join(timeout=1)


def _wait_for_index_status(job, expected: str) -> tuple[str, str]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with job.lock:
            status, error = job.status, job.error
        if status == expected:
            return status, error
        time.sleep(0.005)
    with job.lock:
        return job.status, job.error


def test_failed_chart_index_is_reused_for_unchanged_source(tmp_path, chart_server, monkeypatch):
    runtime, _server = chart_server
    run_dir = tmp_path / "malformed-chart-source"
    run_dir.mkdir()
    csv_path = run_dir / "measurements.csv"
    csv_path.write_text("wrong,columns\n1,2\n", encoding="utf-8")
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    calls = 0
    calls_lock = threading.Lock()
    original_scan = runtime.chart_history._scan_index

    def counted_scan(job):
        nonlocal calls
        with calls_lock:
            calls += 1
        original_scan(job)

    monkeypatch.setattr(runtime.chart_history, "_scan_index", counted_scan)
    status, initial = runtime.chart_history.overview(120)
    assert status == 200, initial
    failed_job = runtime.chart_history._job
    assert failed_job is not None
    assert _wait_for_index_status(failed_job, "error")[0] == "error"
    assert "缺少图表所需列" in failed_job.error

    for _ in range(6):
        status, current = runtime.chart_history.overview(120)
        assert status == 200, current
        assert current["job_id"] == initial["job_id"]
        assert current["status"] == "error"

    with calls_lock:
        assert calls == 1


def test_failed_chart_index_retries_after_source_file_changes(tmp_path, chart_server, monkeypatch):
    runtime, _server = chart_server
    run_dir = tmp_path / "repaired-chart-source"
    run_dir.mkdir()
    csv_path = run_dir / "measurements.csv"
    csv_path.write_text("wrong,columns\n1,2\n", encoding="utf-8")
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    calls = 0
    calls_lock = threading.Lock()
    original_scan = runtime.chart_history._scan_index

    def counted_scan(job):
        nonlocal calls
        with calls_lock:
            calls += 1
        original_scan(job)

    monkeypatch.setattr(runtime.chart_history, "_scan_index", counted_scan)
    status, initial = runtime.chart_history.overview(120)
    assert status == 200, initial
    failed_job = runtime.chart_history._job
    assert failed_job is not None
    assert _wait_for_index_status(failed_job, "error")[0] == "error"

    _write_measurements(csv_path, count=3)
    deadline = time.monotonic() + 2
    latest = {}
    while time.monotonic() < deadline:
        status, latest = runtime.chart_history.overview(120)
        assert status == 200, latest
        if latest["status"] == "ready":
            break
        time.sleep(0.005)

    assert latest["job_id"] != initial["job_id"]
    assert latest["status"] == "ready", latest
    assert latest["total_attempts"] == 3
    with calls_lock:
        assert calls == 2


def test_failed_chart_index_is_reused_when_source_signature_is_unavailable(
    tmp_path, chart_server, monkeypatch,
):
    runtime, _server = chart_server
    run_dir = tmp_path / "unstatable-chart-source"
    run_dir.mkdir()
    csv_path = run_dir / "measurements.csv"
    csv_path.write_text("wrong,columns\n1,2\n", encoding="utf-8")
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))
    source_signature = runtime.chart_history._source_signature
    monkeypatch.setattr(runtime.chart_history, "_source_signature", lambda _path: None)

    calls = 0
    calls_lock = threading.Lock()
    original_scan = runtime.chart_history._scan_index

    def counted_scan(job):
        nonlocal calls
        with calls_lock:
            calls += 1
        original_scan(job)

    monkeypatch.setattr(runtime.chart_history, "_scan_index", counted_scan)
    status, initial = runtime.chart_history.overview(120)
    assert status == 200, initial
    failed_job = runtime.chart_history._job
    assert failed_job is not None
    assert _wait_for_index_status(failed_job, "error")[0] == "error"

    for _ in range(6):
        status, current = runtime.chart_history.overview(120)
        assert status == 200, current
        assert current["job_id"] == initial["job_id"]
        assert current["status"] == "error"

    with calls_lock:
        assert calls == 1

    _write_measurements(csv_path, count=2)
    monkeypatch.setattr(runtime.chart_history, "_source_signature", source_signature)
    deadline = time.monotonic() + 2
    latest = {}
    while time.monotonic() < deadline:
        status, latest = runtime.chart_history.overview(120)
        assert status == 200, latest
        if latest["status"] == "ready":
            break
        time.sleep(0.005)

    assert latest["job_id"] != initial["job_id"]
    assert latest["status"] == "ready", latest
    assert latest["total_attempts"] == 2
    with calls_lock:
        assert calls == 2
