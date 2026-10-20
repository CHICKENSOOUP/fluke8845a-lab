from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from urllib.error import HTTPError
import urllib.request as urllib_request
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from PySide6.QtCore import QCoreApplication

from fluke8845a.modes import Mode
from fluke8845a.filter_policy import digital_filter_enable_error
from fluke8845a.scpi import FlowControl
from fluke8845a import webapp as webapp_module
from fluke8845a.webapp import _complete_csv_prefix_size, create_server
from fluke8845a import webservice as webservice_module
from fluke8845a import worker as worker_module
from fluke8845a import storage as storage_module
from fluke8845a.webservice import WebRuntime, mode_settings_from_payload, serial_settings_from_payload
from tests.fakes import FakeTransport

LOCAL_HTTP_OPENER = build_opener(ProxyHandler({}))


def test_connection_stage_history_is_ordered_and_keeps_latest_status():
    runtime = webservice_module.WebRuntime()
    try:
        for stage, status, message in (
            ("device", "success", "设备 COM3 已选定"),
            ("port", "running", "正在打开串口 COM3"),
            ("identify", "success", "已识别 Fluke 8845A"),
        ):
            runtime.router._on_connection_stage(stage, status, message)

        stages = runtime.store.snapshot()["connection_stages"]
        assert [(item["stage"], item["status"]) for item in stages] == [
            ("device", "success"), ("port", "running"), ("identify", "success"),
        ]
        assert stages[-1]["message"] == "已识别 Fluke 8845A"
    finally:
        runtime.stop()


@pytest.fixture
def qt_app():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def local_service(qt_app):
    runtime = WebRuntime()
    runtime.start()
    server = create_server(runtime, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield qt_app, runtime, server
    server.shutdown()
    server.server_close()
    runtime.stop()


def test_local_http_test_client_does_not_consult_proxy_configuration(local_service, monkeypatch):
    _app, _runtime, server = local_service

    def unexpected_proxy_bypass(_host):
        raise AssertionError("localhost test requests must not consult system proxy bypass rules")

    monkeypatch.setattr(urllib_request, "_opener", None)
    monkeypatch.setattr(urllib_request, "getproxies", lambda: {"http": "http://proxy.invalid:8080"})
    monkeypatch.setattr(urllib_request, "proxy_bypass", unexpected_proxy_bypass)

    status, _headers, body = _http(f"http://127.0.0.1:{server.server_port}/api/health")

    assert status == 200
    assert json.loads(body)["status"] == "ok"


@pytest.mark.parametrize("stderr_kind", ["missing", "closed"])
def test_http_health_response_survives_an_unavailable_stderr_stream(local_service, monkeypatch, stderr_kind):
    _app, _runtime, server = local_service
    stderr = webapp_module.sys.stderr

    class ClosedStream:
        def write(self, _value):
            raise OSError("stderr stream is unavailable")

        def flush(self):
            raise OSError("stderr stream is unavailable")

    stream = None if stderr_kind == "missing" else ClosedStream()
    monkeypatch.setattr(webapp_module.sys, "stderr", stream)
    try:
        status, _headers, body = _http(f"http://127.0.0.1:{server.server_port}/api/health")
    finally:
        webapp_module.sys.stderr = stderr

    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_web_runtime_waits_for_slow_worker_shutdown_before_returning(qt_app):
    runtime = WebRuntime()
    runtime.start()
    original_shutdown = runtime.worker.shutdown

    def delayed_shutdown():
        time.sleep(5.2)
        original_shutdown()

    runtime.worker.shutdown = delayed_shutdown
    try:
        assert runtime.stop() is True
        assert not runtime.thread.isRunning()
    finally:
        if runtime.thread.isRunning():
            runtime.thread.wait(3000)


def _main_thread_wait(app, task, timeout_s=3.0):
    result = {}
    finished = threading.Event()

    def invoke():
        try:
            result["value"] = task()
        except BaseException as exc:
            result["error"] = exc
        finally:
            finished.set()

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout_s
    while not finished.is_set() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.003)
    assert finished.is_set(), "localhost request exceeded the short test deadline"
    thread.join(timeout=0.1)
    if "error" in result:
        raise result["error"]
    return result["value"]


def test_broker_timeout_cancels_a_command_that_has_not_started(qt_app):
    from PySide6.QtCore import QObject, Qt, Signal, Slot

    class FakeRouter(QObject):
        command_completed = Signal(str, object)

        def __init__(self, broker):
            super().__init__()
            self.broker = broker
            self.started = []

        @Slot(str, str, object)
        def execute(self, request_id, action, _payload):
            begin = getattr(self.broker, "begin_operation", None)
            if begin is not None and not begin(request_id):
                return
            self.started.append(action)
            self.command_completed.emit(request_id, {"success": True})

    broker = webservice_module.CommandBroker()
    router = FakeRouter(broker)
    broker.bind(router)
    outcome = {}

    thread = threading.Thread(
        target=lambda: outcome.setdefault(
            "result", broker.request("connect_real", {"port": "COM3"}, timeout_s=0.02)
        ),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=0.5)
    assert not thread.is_alive(), "queued command should reach its short test timeout"

    qt_app.processEvents()

    assert outcome["result"]["cancelled"] is True
    assert router.started == [], "a command canceled while queued must not reach the instrument router"


def test_broker_reports_and_retains_commands_that_started_before_timeout(qt_app):
    from PySide6.QtCore import QObject, QTimer, Signal, Slot

    class FakeRouter(QObject):
        command_completed = Signal(str, object)

        def __init__(self, broker):
            super().__init__()
            self.broker = broker
            self.started = []

        @Slot(str, str, object)
        def execute(self, request_id, action, _payload):
            begin = getattr(self.broker, "begin_operation", None)
            if begin is not None and not begin(request_id):
                return
            self.started.append(action)
            QTimer.singleShot(
                80,
                lambda: self.command_completed.emit(request_id, {"success": True, "message": "fake complete"}),
            )

    broker = webservice_module.CommandBroker()
    router = FakeRouter(broker)
    broker.bind(router)
    result = _main_thread_wait(
        qt_app,
        lambda: broker.request("start", {"name": "fake"}, timeout_s=0.02),
    )

    assert result["pending"] is True
    assert result["success"] is False
    assert result["operation_id"]
    assert router.started == ["start"]

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        qt_app.processEvents()
        status = broker.operation_status(result["operation_id"])
        if status and not status["pending"]:
            break
        time.sleep(0.003)

    assert status == {"pending": False, "result": {"success": True, "message": "fake complete"}}


def test_command_status_endpoint_requires_same_origin_and_returns_pending_operation(local_service, monkeypatch):
    _app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    operation_id = "a" * 32
    monkeypatch.setattr(
        runtime.broker,
        "request",
        lambda _action, _payload: {
            "success": False,
            "pending": True,
            "operation_id": operation_id,
            "message": "仍在处理",
        },
    )
    monkeypatch.setattr(runtime.broker, "operation_status", lambda value: {"pending": True} if value == operation_id else None)

    status, _headers, body = _http(
        f"{base_url}/api/command",
        method="POST",
        payload={"action": "zero", "payload": {}},
        origin=base_url,
    )

    assert status == 202
    assert json.loads(body)["operation_id"] == operation_id

    status, _headers, body = _http(
        f"{base_url}/api/command-status?id={operation_id}",
        fetch_site="same-origin",
    )
    assert status == 200
    assert json.loads(body) == {"pending": True}

    status, _headers, body = _http(f"{base_url}/api/command-status?id={operation_id}")
    assert status == 403
    assert "缺少同源浏览器标记" in json.loads(body)["error"]


def _http(url, *, method="GET", payload=None, origin=None, host=None, fetch_site=None):
    headers = {}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    if fetch_site is not None:
        headers["Sec-Fetch-Site"] = fetch_site
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with LOCAL_HTTP_OPENER.open(request, timeout=1.5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_csv_snapshot_prefix_omits_a_partial_trailing_record(tmp_path):
    path = tmp_path / "measurements.csv"
    path.write_bytes(b"header\r\nrow-one\r\npartial-row")

    size = _complete_csv_prefix_size(path)

    assert path.read_bytes()[:size] == b"header\r\nrow-one\r\n"


def test_safe_event_export_streams_source_without_reading_it_all_into_memory(
    tmp_path, local_service, monkeypatch,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "finished-run"
    run_dir.mkdir()
    event_path = run_dir / "events.csv"
    original = (
        b"event_index,event,mode,value_si,elapsed_s\r\n"
        b"1,=1+1,=MODE,=2+2,0\r\n"
        b"2,+SUM(A1),DCV,-1.25,1\r\n"
        b"3,-1+2,DCV,3.5,2\r\n"
        b"4,@HYPERLINK,DCV,4.5,3\r\n"
        b"5,ordinary,DCV,5.5,4\r\n"
    )
    event_path.write_bytes(original)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    original_read_bytes = Path.read_bytes

    def reject_event_read_bytes(path):
        if path.resolve() == event_path.resolve():
            raise AssertionError("event export must stream instead of reading the whole source")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_event_read_bytes)
    status, headers, body = _main_thread_wait(
        app, lambda: _http(base_url + "/api/latest-events-safe.csv")
    )

    assert status == 200
    assert int(headers["Content-Length"]) == len(body)
    assert b"'=1+1" in body
    assert b"'=MODE" in body
    assert b"'=2+2" in body
    assert b"'+SUM(A1)" in body
    assert b"'-1+2" in body
    assert b"'@HYPERLINK" in body
    assert b"-1.25" in body
    assert original_read_bytes(event_path) == original


def test_safe_measurement_export_escapes_formula_cells_and_preserves_raw_file(
    tmp_path, local_service, monkeypatch,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "finished-run"
    run_dir.mkdir()
    measurement_path = run_dir / "measurements.csv"
    original = (
        b"sample_index,value_si,raw_reading,error\r\n"
        b"1,-1.25,-1.25000000E+00,\r\n"
        b"2,=2+2,=1+1,+device error\r\n"
    )
    measurement_path.write_bytes(original)
    runtime.store.update(experiment_dir="", last_experiment_dir=str(run_dir))

    original_read_bytes = Path.read_bytes

    def reject_source_read_bytes(path):
        if path.resolve() == measurement_path.resolve():
            raise AssertionError("measurement safe export must stream the source")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_source_read_bytes)
    status, headers, body = _main_thread_wait(
        app, lambda: _http(base_url + "/api/latest-measurements-safe.csv")
    )

    assert status == 200
    assert headers.get("Content-Type", "").startswith("text/csv")
    assert "measurements_excel_safe.csv" in headers.get("Content-Disposition", "")
    assert b"-1.25,-1.25000000E+00" in body
    assert b"'=2+2,'=1+1,'+device error" in body
    assert original_read_bytes(measurement_path) == original


def test_safe_measurement_export_is_rejected_while_experiment_is_active(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    run_dir = tmp_path / "active-run"
    run_dir.mkdir()
    runtime.store.update(experiment_dir=str(run_dir), last_experiment_dir=str(run_dir))

    status, _headers, body = _main_thread_wait(
        app, lambda: _http(base_url + "/api/latest-measurements-safe.csv")
    )

    assert status == 409
    assert "停止采集" in json.loads(body)["error"]


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7])
def test_csv_snapshot_prefix_respects_quoted_newlines_and_chunk_boundaries(tmp_path, chunk_size):
    path = tmp_path / "measurements.csv"
    complete = b'header\r\n"first line\nsecond line","escaped ""quote"""\r\nrow-two,ok\r\n'
    path.write_bytes(complete + b'"unfinished\nrow')

    size = _complete_csv_prefix_size(path, chunk_size=chunk_size)

    assert size == len(complete)
    assert path.read_bytes()[:size] == complete


def _api_command(app, base_url, action, payload=None, *, origin=None):
    target_origin = origin if origin is not None else base_url
    return _main_thread_wait(app, lambda: _http(
        base_url + "/api/command", method="POST",
        payload={"action": action, "payload": payload or {}}, origin=target_origin,
    ))


def test_cross_origin_command_rejection_closes_connection_after_request_body(local_service):
    _app, _runtime, server = local_service
    payload = b'{"action":"connect_mock","payload":{"settings":{}}}'
    request = (
        f"POST /api/command HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.server_port}\r\n"
        "Origin: http://example.com\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: keep-alive\r\n\r\n"
    ).encode("ascii") + payload

    with socket.create_connection(("127.0.0.1", server.server_port), timeout=2) as client:
        client.sendall(request)
        client.settimeout(2)
        chunks = []
        while chunk := client.recv(4096):
            chunks.append(chunk)

    response = b"".join(chunks)
    header, body = response.split(b"\r\n\r\n", 1)
    headers = {
        key.strip().lower(): value.strip()
        for line in header.split(b"\r\n")[1:]
        if b":" in line
        for key, value in (line.split(b":", 1),)
    }
    assert header.startswith(b"HTTP/1.1 403")
    assert headers[b"connection"].lower() == b"close"
    assert len(body) == int(headers[b"content-length"])
    assert response.count(b"HTTP/1.1 ") == 1
    assert "命令请求必须来自本地界面" in json.loads(body)["error"]


def _read_state(app, base_url):
    status, _headers, body = _main_thread_wait(app, lambda: _http(base_url + "/api/state"))
    assert status == 200
    return json.loads(body)


def _wait_for_state(app, base_url, predicate, timeout_s=4.0):
    deadline = time.monotonic() + timeout_s
    last_state = None
    while time.monotonic() < deadline:
        last_state = _read_state(app, base_url)
        if predicate(last_state):
            return last_state
        time.sleep(0.025)
    raise AssertionError(f"short acquisition state did not arrive: {last_state}")


def test_web_stop_reports_failed_storage_finalization_as_failure(tmp_path, local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "scenario": "阶跃响应与恢复", "settings": settings,
    })
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "start", {
        "settings": settings, "name": "保存失败短测", "root": str(tmp_path), "interval_s": 0.02,
    })
    assert status == 200, body
    _wait_for_state(app, base_url, lambda state: bool(state.get("experiment_dir")))

    def fail_finish(_recorder, _status="stopped"):
        raise OSError("simulated close failure")

    monkeypatch.setattr("fluke8845a.storage.ExperimentRecorder.finish", fail_finish)
    status, _headers, body = _api_command(app, base_url, "stop")

    result = json.loads(body)
    assert status == 400
    assert result["success"] is False
    assert "文件" in result["message"] or "记录" in result["message"]


def test_web_settings_accept_only_8845a_applicable_parameters():
    settings = mode_settings_from_payload({
        "mode": "DCV", "autorange": False, "range_si": 1, "nplc": 10,
        "analog_filter": True, "autozero": False,
    })
    assert settings.mode == Mode.DCV
    assert settings.range_si == 1
    assert settings.nplc == 10
    assert settings.analog_filter is True
    assert settings.digital_filter is False
    assert settings.autozero is False

    high_impedance = mode_settings_from_payload({
        "mode": "DCV", "autorange": True, "input_impedance_auto": True,
    })
    assert high_impedance.input_impedance_auto is True
    with pytest.raises(ValueError, match="输入阻抗"):
        mode_settings_from_payload({
            "mode": "ACV", "autorange": True, "input_impedance_auto": True,
        })

    with pytest.raises(ValueError, match="固定量程"):
        mode_settings_from_payload({"mode": "DCV", "autorange": False, "range_si": 0.3})
    with pytest.raises(ValueError, match="不支持此交流滤波档"):
        mode_settings_from_payload({"mode": "ACV", "autorange": True, "ac_bandwidth_hz": 10})
    with pytest.raises(ValueError, match="NPLC"):
        mode_settings_from_payload({"mode": "ACI", "autorange": True, "nplc": 1})
    for mode, nplc in ((Mode.ACV, None), (Mode.DCV, 1), (Mode.DCV, 10)):
        with pytest.raises(ValueError) as error:
            mode_settings_from_payload({
                "mode": mode.value, "autorange": True, "nplc": nplc,
                "digital_filter": True,
            })
        assert str(error.value) == digital_filter_enable_error(mode)
    with pytest.raises(ValueError, match="整数"):
        mode_settings_from_payload({"mode": "ACV", "autorange": True, "ac_bandwidth_hz": 3.5})


def test_web_serial_defaults_match_confirmed_rs232_setup():
    settings = serial_settings_from_payload({"eol": "CRLF", "parity": "N", "bytesize": 8, "flow_control": "none"})
    assert settings.baudrate == 9600
    assert settings.bytesize == 8
    assert settings.parity == "N"
    assert settings.eol == "CRLF"
    assert settings.flow_control == FlowControl.NONE
    assert settings.timeout_s == 2
    with pytest.raises(ValueError, match="超时"):
        serial_settings_from_payload({"timeout_s": 6})
    with pytest.raises(ValueError, match="整数"):
        serial_settings_from_payload({"baudrate": 9600.5})


@pytest.mark.parametrize("nplc", [0.02, 0.2, 1, 10, 100])
def test_web_payload_accepts_each_supported_nplc_value(nplc):
    settings = mode_settings_from_payload({
        "mode": "DCV", "autorange": True, "nplc": nplc,
    })

    assert settings.nplc == nplc


def test_mock_preflight_checks_connection_configuration_wiring_and_storage(tmp_path, local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    monkeypatch.setattr(
        webservice_module, "get_available_space_bytes", lambda _root: int(4.5 * 1024**3), raising=False
    )

    status, _headers, body = _api_command(app, base_url, "preflight", {
        "settings": settings, "name": "短测", "root": str(tmp_path), "interval_s": 0,
    })
    assert status == 400
    assert "连接仪器" in json.loads(body)["message"]

    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": settings, "scenario": "阶跃响应与恢复",
    })
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "preflight", {
        "settings": settings, "name": "短测", "root": str(tmp_path), "interval_s": 0,
    })
    assert status == 200, body
    result = json.loads(body)
    assert result["success"] is True
    assert any("仪器已连接" in item for item in result["checks"])
    assert any("模式" in item and "回读" in item for item in result["checks"])
    assert any("可写" in item for item in result["checks"])
    assert any("当前可用 4.5 GiB" in item for item in result["checks"])
    assert any("此估算不限制实验时长" in item for item in result["checks"])
    assert any("最快采集的实际采样速率未知，启动前无法预估可记录时长" in item for item in result["checks"])
    assert any("停止采集并尝试收尾文件" in item for item in result["checks"])
    assert any("记录可能不完整" in item for item in result["checks"])
    assert any("MOCK无需实体接线" in item for item in result["checks"])
    assert runtime.worker.state.value == "stopped"
    assert not runtime.store.snapshot()["experiment_dir"]

    duplicate = tmp_path / "20261016_120000_000001_短测"
    duplicate.mkdir()
    (duplicate / "config.json").write_text(
        json.dumps({"experiment_name": "短测", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    status, _headers, body = _api_command(app, base_url, "preflight", {
        "settings": settings, "name": "短测", "root": str(tmp_path), "interval_s": 0,
    })
    assert status == 200, body
    assert json.loads(body)["duplicate_name"] is True
    assert json.loads(body)["duplicate_path"] == str(duplicate.resolve())
    assert any("同名实验目录" in item for item in json.loads(body)["checks"])


def test_web_start_requires_explicit_duplicate_name_choice_and_preserves_existing_files(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200, body

    existing = tmp_path / "20261016_120000_000001_重复实验"
    existing.mkdir()
    original_csv = existing / "measurements.csv"
    original_bytes = b"preserve this original experiment\r\n"
    original_csv.write_bytes(original_bytes)
    (existing / "config.json").write_text(
        json.dumps({"experiment_name": "重复实验", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    payload = {
        "settings": settings,
        "name": "重复实验",
        "root": str(tmp_path),
        "interval_s": 10,
    }

    status, _headers, body = _api_command(app, base_url, "start", payload)

    assert status == 400
    result = json.loads(body)
    assert result["success"] is False
    assert "同名实验" in result["message"]
    assert runtime.worker.state.value == "stopped"
    assert not runtime.store.snapshot()["experiment_dir"]
    assert original_csv.read_bytes() == original_bytes

    payload["duplicate_name_action"] = "create-copy"
    status, _headers, body = _api_command(app, base_url, "start", payload)

    assert status == 200, body
    _wait_for_state(app, base_url, lambda state: bool(state.get("experiment_dir")))
    created = Path(_read_state(app, base_url)["experiment_dir"])
    assert created != existing
    assert created.parent == tmp_path
    assert created.is_dir()
    assert original_csv.read_bytes() == original_bytes

    status, _headers, body = _api_command(app, base_url, "stop")
    assert status == 200, body


def test_web_overwrite_archives_latest_same_name_run_before_starting_new_run(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200, body

    older = tmp_path / "20261016_120000_000001_重复实验"
    latest = tmp_path / "20261017_120000_000001_重复实验"
    older.mkdir()
    latest.mkdir()
    (older / "measurements.csv").write_bytes(b"older run stays in place\r\n")
    (older / "config.json").write_text(
        json.dumps({"experiment_name": "重复实验", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    original_bytes = b"archive this exact prior run\r\n"
    (latest / "measurements.csv").write_bytes(original_bytes)
    (latest / "config.json").write_text(
        json.dumps({"experiment_name": "重复实验", "completion_status": "stopped"}),
        encoding="utf-8",
    )
    payload = {
        "settings": settings,
        "name": "重复实验",
        "root": str(tmp_path),
        "interval_s": 10,
        "duplicate_name_action": "overwrite",
        "expected_duplicate_path": str(latest.resolve()),
    }

    status, _headers, body = _api_command(app, base_url, "start", payload)

    assert status == 200, body
    result = json.loads(body)
    assert result["success"] is True
    assert "备份" in result["message"]
    _wait_for_state(app, base_url, lambda state: bool(state.get("experiment_dir")))
    created = Path(_read_state(app, base_url)["experiment_dir"])
    assert created.parent == tmp_path
    assert created not in (older, latest)
    assert created.is_dir()
    assert older.is_dir()
    assert (older / "measurements.csv").read_bytes() == b"older run stays in place\r\n"
    assert not latest.exists()

    backup_runs = list((tmp_path / ".8845a-backups").iterdir())
    assert len(backup_runs) == 1
    backup = backup_runs[0]
    assert backup.name.startswith(latest.name + "_")
    assert (backup / "measurements.csv").read_bytes() == original_bytes
    metadata = json.loads((created / "config.json").read_text(encoding="utf-8"))
    assert metadata["replaced_experiment_backup"] == str(backup)

    status, _headers, body = _api_command(app, base_url, "stop")
    assert status == 200, body


def test_web_overwrite_reservation_failure_keeps_existing_run_in_place(tmp_path, local_service, monkeypatch):
    app, _runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200, body

    existing = tmp_path / "20261017_120000_000001_归档失败实验"
    existing.mkdir()
    original_csv = existing / "measurements.csv"
    original_bytes = b"still recoverable after a failed start\r\n"
    original_csv.write_bytes(original_bytes)
    (existing / "config.json").write_text(
        json.dumps({"experiment_name": "归档失败实验", "completion_status": "stopped"}),
        encoding="utf-8",
    )

    def fail_reservation(*_args, **_kwargs):
        raise OSError("test reservation failure")

    monkeypatch.setattr(worker_module, "reserve_experiment_directory", fail_reservation)
    status, _headers, body = _api_command(app, base_url, "start", {
        "settings": settings,
        "name": "归档失败实验",
        "root": str(tmp_path),
        "interval_s": 10,
        "duplicate_name_action": "overwrite",
        "expected_duplicate_path": str(existing.resolve()),
    })

    assert status == 400
    result = json.loads(body)
    assert result["success"] is False
    assert "test reservation failure" in result["message"]
    assert existing.is_dir()
    assert original_csv.read_bytes() == original_bytes
    assert not (tmp_path / ".8845a-backups").exists()
    with storage_module.ExperimentNameLock(tmp_path, "归档失败实验"):
        pass


def test_web_configuration_failure_reports_recovery_backup_path(tmp_path, local_service, monkeypatch):
    app, _runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200, body

    existing = tmp_path / "20261017_120000_000001_配置失败实验"
    existing.mkdir()
    original_csv = existing / "measurements.csv"
    original_bytes = b"still recoverable after a failed configuration\r\n"
    original_csv.write_bytes(original_bytes)
    (existing / "config.json").write_text(
        json.dumps({"experiment_name": "配置失败实验", "completion_status": "stopped"}),
        encoding="utf-8",
    )

    def fail_configuration(_instrument, _settings):
        raise worker_module.InstrumentError("test configuration failure")

    monkeypatch.setattr(worker_module.Mock8845A, "configure", fail_configuration)
    status, _headers, body = _api_command(app, base_url, "start", {
        "settings": settings,
        "name": "配置失败实验",
        "root": str(tmp_path),
        "interval_s": 10,
        "duplicate_name_action": "overwrite",
        "expected_duplicate_path": str(existing.resolve()),
    })

    assert status == 400
    result = json.loads(body)
    assert result["success"] is False
    assert "旧实验目录已备份至" in result["message"]
    backups = list((tmp_path / ".8845a-backups").iterdir())
    assert len(backups) == 1
    assert str(backups[0]) in result["message"]
    assert (backups[0] / "measurements.csv").read_bytes() == original_bytes
    assert not existing.exists()
    assert not list(path for path in tmp_path.glob("*_配置失败实验") if path.is_dir())
    with storage_module.ExperimentNameLock(tmp_path, "配置失败实验"):
        pass


def test_web_single_requires_explicit_duplicate_name_choice(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200, body

    existing = tmp_path / "20261016_120000_000001_单次重复实验"
    existing.mkdir()
    original_csv = existing / "measurements.csv"
    original_bytes = b"preserve this original experiment\r\n"
    original_csv.write_bytes(original_bytes)
    (existing / "config.json").write_text(
        json.dumps({"experiment_name": "单次重复实验", "completion_status": "stopped"}),
        encoding="utf-8",
    )

    status, _headers, body = _api_command(app, base_url, "single", {
        "settings": settings,
        "name": "单次重复实验",
        "root": str(tmp_path),
        "interval_s": 0,
    })

    assert status == 400
    assert "同名实验" in json.loads(body)["message"]
    assert runtime.worker.state.value == "stopped"
    assert not runtime.store.snapshot()["experiment_dir"]
    assert original_csv.read_bytes() == original_bytes


def test_web_zero_reports_configuration_readback_failure_as_unsuccessful(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True},
    })
    assert status == 200, body

    def fail_readback(_mode):
        raise RuntimeError("模拟回读失败")

    monkeypatch.setattr(runtime.worker.instrument, "read_settings", fail_readback)
    status, _headers, body = _api_command(
        app, base_url, "zero", {"zero_confirmed": True},
    )

    result = json.loads(body)
    assert status == 400
    assert result["success"] is False
    assert "MOCK模拟配置回读失败" in result["message"]
    assert "模拟回读失败" in result["message"]
    assert runtime.worker.instrument._zeroed is True


def test_web_zero_command_failure_replaces_the_previous_result_in_state(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True},
    })
    assert status == 200, body
    runtime.store.update(zero_result="previous successful ZERO feedback")

    def fail_zero():
        raise RuntimeError("simulated ZERO command failure")

    monkeypatch.setattr(runtime.worker.instrument, "zero", fail_zero)
    status, _headers, body = _api_command(
        app, base_url, "zero", {"zero_confirmed": True},
    )

    result = json.loads(body)
    assert status == 400
    assert result["success"] is False
    assert "ZERO失败" in result["message"]
    assert "ZERO失败" in result["zero_result"]
    assert "ZERO失败" in runtime.store.snapshot()["zero_result"]
    assert result["zero_result"] == runtime.store.snapshot()["zero_result"]
    assert "previous successful ZERO feedback" not in runtime.store.snapshot()["zero_result"]


def test_web_zero_precondition_rejection_replaces_the_previous_result(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True},
    })
    assert status == 200, body
    runtime.store.update(zero_result="previous successful ZERO feedback")
    zero_calls = []
    monkeypatch.setattr(runtime.worker.instrument, "zero", lambda: zero_calls.append(True))

    status, _headers, body = _api_command(app, base_url, "zero", {"zero_confirmed": False})

    result = json.loads(body)
    feedback = runtime.store.snapshot()["zero_result"]
    assert status == 400
    assert result["success"] is False
    assert "请先在页面确认" in result["message"]
    assert feedback.startswith("ZERO未执行：")
    assert result["zero_result"] == feedback
    assert "previous successful ZERO feedback" not in feedback
    assert not zero_calls


def test_web_zero_unexpected_worker_exception_reports_uncertain_state(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True},
    })
    assert status == 200, body
    runtime.store.update(zero_result="previous successful ZERO feedback")

    def fail_outside_worker_guard():
        raise RuntimeError("unexpected worker wrapper failure")

    monkeypatch.setattr(runtime.worker, "zero", fail_outside_worker_guard)
    status, _headers, body = _api_command(app, base_url, "zero", {"zero_confirmed": True})

    result = json.loads(body)
    feedback = runtime.store.snapshot()["zero_result"]
    assert status == 400
    assert result["success"] is False
    assert result["uncertain"] is True
    assert result["zero_result"] == feedback
    assert feedback.startswith("ZERO执行状态未确认：")
    assert "previous successful ZERO feedback" not in feedback


def test_web_zero_router_exception_clears_pending_feedback_as_uncertain(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True},
    })
    assert status == 200, body
    runtime.store.update(zero_result="previous successful ZERO feedback")

    def broken_state(_worker):
        raise RuntimeError("worker state unavailable")

    with monkeypatch.context() as patcher:
        patcher.setattr(worker_module.MeasurementWorker, "state", property(broken_state))
        status, _headers, body = _api_command(app, base_url, "zero", {"zero_confirmed": True})

    result = json.loads(body)
    feedback = runtime.store.snapshot()["zero_result"]
    assert status == 400
    assert result["success"] is False
    assert result["uncertain"] is True
    assert result["zero_result"] == feedback
    assert feedback.startswith("ZERO执行状态未确认：")
    assert "worker state unavailable" in feedback
    assert "previous successful ZERO feedback" not in feedback


def test_web_preflight_estimates_recording_capacity_from_target_interval(tmp_path, local_service, monkeypatch):
    app, _runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(webservice_module, "prepare_experiment_storage", lambda root: Path(root))
    monkeypatch.setattr(webservice_module, "get_available_space_bytes", lambda _root: 4 * 1024**3)
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}

    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": settings, "scenario": "阶跃响应与恢复",
    })
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "preflight", {
        "settings": settings, "name": "容量预估", "root": str(tmp_path), "interval_s": 1.0,
    })

    assert status == 200, body
    checks = json.loads(body)["checks"]
    assert any("按 4 KiB/样本粗估" in item and "可记录约" in item for item in checks)


def test_web_preflight_rejects_space_below_reserve_on_final_check(tmp_path, local_service, monkeypatch):
    app, _runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    monkeypatch.setattr(webservice_module, "prepare_experiment_storage", lambda root: Path(root))
    monkeypatch.setattr(
        webservice_module,
        "get_available_space_bytes",
        lambda _root: storage_module.MINIMUM_FREE_SPACE_BYTES - 1,
    )
    status, _headers, body = _api_command(app, base_url, "connect_mock", {
        "settings": settings, "scenario": "阶跃响应与恢复",
    })
    assert status == 200, body

    status, _headers, body = _api_command(app, base_url, "preflight", {
        "settings": settings, "name": "低空间", "root": str(tmp_path), "interval_s": 0,
    })

    assert status == 400
    assert "可用空间不足" in json.loads(body)["message"]


def test_preflight_rejects_unconfirmed_real_wiring_without_starting(tmp_path, local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport()
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A
    monkeypatch.setattr(
        worker_module, "Serial8845A",
        lambda port, settings, callback: Serial8845A(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {}, "settings": settings,
    })
    assert status == 200, body

    payload = {"settings": settings, "name": "短测", "root": str(tmp_path), "interval_s": 0}
    status, _headers, body = _api_command(app, base_url, "preflight", payload)
    assert status == 400
    assert "接线提示" in json.loads(body)["message"]
    assert runtime.worker.state.value == "stopped"
    assert not runtime.store.snapshot()["experiment_dir"]

    payload["safety_confirmed"] = True
    status, _headers, body = _api_command(app, base_url, "preflight", payload)
    assert status == 200, body
    assert any("接线已确认" in item for item in json.loads(body)["checks"])


def test_web_numeric_inputs_reject_extreme_integers_as_validation_errors():
    huge_integer = 10 ** 400
    with pytest.raises(ValueError, match="有限数值"):
        mode_settings_from_payload({
            "mode": "DCV", "autorange": False, "range_si": huge_integer,
        })
    with pytest.raises(ValueError, match="有限数值"):
        serial_settings_from_payload({"timeout_s": huge_integer})
    with pytest.raises(ValueError, match="整数"):
        serial_settings_from_payload({"baudrate": huge_integer})


def test_localhost_mock_api_enforces_origin_and_records_one_shot(tmp_path, local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    web_root = Path(__file__).resolve().parents[1] / "src" / "fluke8845a" / "web"

    status, headers, page = _main_thread_wait(app, lambda: _http(base_url + "/"))
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert page == (web_root / "index.html").read_bytes()
    page_text = page.decode("utf-8")
    assert b"Fluke 8845A" in page
    assert headers["Content-Security-Policy"]
    assert "worker-src 'self'" in headers["Content-Security-Policy"]
    assert b'id="settingsDialog"' in page
    assert b'id="openSettings"' in page
    assert b'id="autoScale"' in page
    assert b'id="controlMode"' in page
    assert b'id="applyControlMode"' in page
    assert "不是校准调整或去皮" in page_text
    assert "仪表RS-232协议仍须设为COMPUTER" in page_text
    assert b'id="plot"' in page
    assert b'class="control-rail"' not in page
    main_view = page.split(b"<dialog", 1)[0]
    assert b'id="mode"' not in main_view and b'id="serialTimeout"' not in main_view

    status, headers, stylesheet = _main_thread_wait(app, lambda: _http(base_url + "/app.css"))
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert stylesheet == (web_root / "app.css").read_bytes()
    assert b".settings-dialog" in stylesheet
    status, headers, script = _main_thread_wait(app, lambda: _http(base_url + "/app.js"))
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert script == (web_root / "app.js").read_bytes()
    script_text = script.decode("utf-8")
    assert b"event.altKey" in script and b"event.ctrlKey" in script and b"event.shiftKey" in script
    assert b"app.yView = zoomYRange" in script
    assert "自动测量内部零点并补偿偏移" in script_text
    assert "const settingsDisabled = acquiring || !computerControl;" in script_text

    status, worker_headers, history_worker = _main_thread_wait(app, lambda: _http(base_url + "/history_worker.js"))
    assert status == 200
    assert worker_headers["Cache-Control"] == "no-store"
    assert history_worker == (web_root / "history_worker.js").read_bytes()
    assert worker_headers["Content-Type"].startswith(("application/javascript", "text/javascript"))
    assert b"MAX_HISTORY_BYTES = 2 * 1024 * 1024 * 1024" in history_worker
    assert b"self.addEventListener(\"message\"" in history_worker

    status, _headers, metadata = _main_thread_wait(app, lambda: _http(base_url + "/api/meta"))
    assert status == 200
    modes = json.loads(metadata)["modes"]
    assert {mode["value"] for mode in modes} == {mode.value for mode in Mode}
    assert {mode["value"] for mode in modes if mode["digital_filter"]} == {
        "DCV", "DCI", "RES2", "RES4"
    }
    assert {mode["value"] for mode in modes if mode["input_impedance_auto"]} == {"DCV"}
    mode_by_name = {mode["value"]: mode for mode in modes}
    assert {name for name, mode in mode_by_name.items() if mode["safety_limits"]} == {
        "ACV", "DCI", "ACI", "RES2", "RES4"
    }
    assert "440 mA" in mode_by_name["DCI"]["safety_limits"]
    assert "1.0.700.18" in mode_by_name["ACI"]["safety_limits"]
    assert "100 MΩ" in mode_by_name["RES2"]["safety_limits"]
    assert "100 MΩ" in mode_by_name["RES4"]["safety_limits"]

    status, _headers, _body = _main_thread_wait(app, lambda: _http(base_url + "/api/health", host="example.com"))
    assert status == 403
    status, _headers, _body = _api_command(app, base_url, "connect_mock", origin="http://example.com")
    assert status == 403

    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200
    assert json.loads(body)["success"] is True
    state = _read_state(app, base_url)
    assert state["connected"] is True
    assert state["simulation"] is True

    for mode in Mode:
        mode_settings = {"mode": mode.value, "autorange": True, "autozero": True}
        if mode in (Mode.DCV, Mode.DCI, Mode.RES2, Mode.RES4):
            mode_settings["nplc"] = 1
        else:
            mode_settings["ac_bandwidth_hz"] = 20
        status, _headers, body = _api_command(app, base_url, "configure", {
            "settings": mode_settings,
        })
        assert status == 200, body

    run_settings = {"mode": "RES4", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "single", {
        "settings": run_settings,
        "name": "localhost短测", "root": str(tmp_path), "interval_s": 0,
    })
    assert status == 200, body
    state = _read_state(app, base_url)
    run_dir = state["last_experiment_dir"]
    assert run_dir
    assert state["simulation"] is True
    assert state["latest"]["mode"] == "RES4"
    assert state["latest"]["status"] == "ok"
    status, _headers, body = _api_command(app, base_url, "list_experiment_files")
    assert status == 200, body
    file_result = json.loads(body)
    assert file_result["success"] is True
    assert Path(file_result["directory"]).resolve() == Path(run_dir).resolve()
    assert {item["name"] for item in file_result["files"]} >= {
        "measurements.csv", "events.csv", "config.json", "run.log",
    }
    opened_directories = []
    monkeypatch.setattr(
        webservice_module.QDesktopServices, "openUrl",
        lambda url: opened_directories.append(url.toLocalFile()) or True,
    )
    status, _headers, body = _api_command(app, base_url, "open_experiment_directory")
    assert status == 200, body
    assert [Path(value).resolve() for value in opened_directories] == [Path(run_dir).resolve()]

    status, _headers, body = _api_command(app, base_url, "start", {
        "settings": run_settings,
        "name": "localhost连续短测", "root": str(tmp_path), "interval_s": 0.02,
    })
    assert status == 200, body
    state = _wait_for_state(app, base_url, lambda item: int((item.get("latest") or {}).get("sample_index", 0)) >= 3)

    status, snapshot_headers, snapshot_body = _main_thread_wait(app, lambda: _http(base_url + "/api/latest.csv"))
    assert status == 200
    assert snapshot_headers.get("X-8845A-Export-State") == "in-progress-snapshot"
    assert snapshot_headers.get("X-8845A-Snapshot-Time")
    assert "进行中快照" in unquote(snapshot_headers.get("Content-Disposition", ""))
    assert snapshot_body.endswith(b"\n")

    status, _headers, body = _api_command(app, base_url, "event", {"text": "负载切换"})
    assert status == 200, body
    event_result = json.loads(body)
    assert event_result["success"], event_result
    assert event_result.get("event"), event_result
    assert event_result["event"]["text"] == "负载切换"
    assert event_result["event"]["timestamp_local"]
    assert event_result["event"]["sample_index"] >= 1
    assert event_result["event"]["value_si"] is not None
    assert "样本" in event_result["message"] and "负载切换" in event_result["message"]
    state = _read_state(app, base_url)
    assert state["events"][-1]["text"] == "负载切换"
    status, _headers, body = _api_command(app, base_url, "event", {"text": "=1+1"})
    assert status == 200, body

    speed_settings = {**run_settings, "nplc": 0.2}
    status, _headers, body = _api_command(app, base_url, "speed", {"settings": speed_settings})
    assert status == 200, body
    assert json.loads(body)["success"] is True
    status, _headers, body = _api_command(app, base_url, "interval", {"interval_s": 0.05})
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "pause")
    assert status == 200, body
    assert "暂停" in _read_state(app, base_url)["status"]
    status, _headers, body = _api_command(app, base_url, "resume")
    assert status == 200, body
    state = _wait_for_state(app, base_url, lambda item: int((item.get("latest") or {}).get("sample_index", 0)) >= 5)
    status, _headers, body = _api_command(app, base_url, "stop")
    assert status == 200, body
    stop_result = json.loads(body)
    assert stop_result["success"] is True
    assert "尝试" in stop_result["message"] and "有效" in stop_result["message"]
    assert _read_state(app, base_url)["save_result"]["saved"] is True
    assert "measurements.csv" in stop_result["message"]
    state = _read_state(app, base_url)
    run_dir = state["last_experiment_dir"]
    assert state["simulation"] is True

    status, completed_headers, csv_body = _main_thread_wait(app, lambda: _http(base_url + "/api/latest.csv"))
    assert status == 200
    assert completed_headers.get("X-8845A-Export-State") == "saved"
    assert b"raw_reading" in csv_body
    run_path = tmp_path / Path(run_dir).name
    assert json.loads((run_path / "config.json").read_text(encoding="utf-8"))["simulation"] is True
    assert (run_path / "events.csv").read_text(encoding="utf-8-sig").count("负载切换") == 1
    raw_events = (run_path / "events.csv").read_text(encoding="utf-8-sig")
    assert "=1+1" in raw_events
    status, safe_headers, safe_events = _main_thread_wait(app, lambda: _http(base_url + "/api/latest-events-safe.csv"))
    assert status == 200
    assert safe_headers.get("Content-Type", "").startswith("text/csv")
    assert b"'=1+1" in safe_events
    assert "=1+1" in (run_path / "events.csv").read_text(encoding="utf-8-sig")
    assert (run_path / "config.json").is_file()
    assert runtime.worker.instrument is not None


def test_web_interval_response_reports_effective_sample_host_time_and_noops(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}

    status, _headers, body = _api_command(app, base_url, "connect_mock", {"settings": settings})
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "interval", {"interval_s": 0.5})
    assert status == 200, body
    idle_result = json.loads(body)
    assert idle_result["success"] is True
    assert idle_result["changed"] is False
    assert idle_result["no_op"] is True
    assert "未运行" in idle_result["message"]

    status, _headers, body = _api_command(app, base_url, "start", {
        "settings": settings,
        "name": "Web interval response",
        "root": str(tmp_path),
        "interval_s": 10,
    })
    assert status == 200, body
    state = _wait_for_state(app, base_url, lambda item: int((item.get("latest") or {}).get("sample_index", 0)) >= 1)

    status, _headers, body = _api_command(app, base_url, "interval", {"interval_s": 0.05})
    assert status == 200, body
    changed_result = json.loads(body)
    assert changed_result["success"] is True
    assert changed_result["changed"] is True
    assert changed_result["no_op"] is False
    assert changed_result["effective_sample_index"] == state["latest"]["sample_index"] + 1
    assert changed_result["host_timestamp_local"]
    assert changed_result["host_timestamp_utc"]
    assert datetime.fromisoformat(changed_result["host_timestamp_local"]).tzinfo is not None
    assert datetime.fromisoformat(changed_result["host_timestamp_utc"]).utcoffset().total_seconds() == 0
    assert changed_result["elapsed_s"] >= 0
    assert f"从样本 {changed_result['effective_sample_index']} 起生效" in changed_result["message"]
    assert "主机时间" in changed_result["message"] and "实验历时" in changed_result["message"]

    status, _headers, body = _api_command(app, base_url, "interval", {"interval_s": 0.05})
    assert status == 200, body
    unchanged_result = json.loads(body)
    assert unchanged_result["success"] is True
    assert unchanged_result["changed"] is False
    assert unchanged_result["no_op"] is True
    assert "未变化" in unchanged_result["message"]

    status, _headers, body = _api_command(app, base_url, "pause")
    assert status == 200, body
    state = _read_state(app, base_url)
    assert "暂停" in state["status"]
    status, _headers, body = _api_command(app, base_url, "interval", {"interval_s": 0.1})
    assert status == 200, body
    paused_result = json.loads(body)
    assert paused_result["success"] is True
    assert paused_result["changed"] is True
    assert paused_result["effective_sample_index"] >= state["latest"]["sample_index"] + 1

    status, _headers, body = _api_command(app, base_url, "stop")
    assert status == 200, body


def test_web_state_reports_mock_scenario_for_all_modes_and_clears_for_real_or_disconnected(
    local_service, monkeypatch,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    assert _read_state(app, base_url).get("mock_scenario") is None

    scenarios = (
        "阶跃响应与恢复",
        "长期漂移",
        "通信超时测试",
        "断线恢复测试",
    )
    for mode in Mode:
        settings = {"mode": mode.value, "autorange": True, "autozero": True}
        if mode in (Mode.DCV, Mode.DCI, Mode.RES2, Mode.RES4):
            settings["nplc"] = 1
        else:
            settings["ac_bandwidth_hz"] = 20
        for scenario in scenarios:
            status, _headers, body = _api_command(app, base_url, "connect_mock", {
                "scenario": scenario, "settings": settings,
            })
            assert status == 200, body
            state = _read_state(app, base_url)
            assert state["simulation"] is True
            assert state["mock_scenario"] == scenario

            status, _headers, body = _api_command(app, base_url, "disconnect")
            assert status == 200, body
            state = _read_state(app, base_url)
            assert state["connected"] is False
            assert state["mock_scenario"] is None

    transport = FakeTransport()
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, serial_settings, callback: real_instrument_type(
            port, serial_settings, callback, transport_factory=lambda *_: transport
        ),
    )
    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {"eol": "CRLF"},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
    })
    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["connected"] is True
    assert state["simulation"] is False
    assert state["mock_scenario"] is None

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body
    assert _read_state(app, base_url)["mock_scenario"] is None


def test_real_meter_requires_wiring_confirmation_for_configuration_and_measurement(local_service, monkeypatch, tmp_path):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport()
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )

    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {"eol": "CRLF"},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
    })
    assert status == 200, body

    actions = (
        ("configure", {"settings": {"mode": "RES2", "autorange": True, "nplc": 1}}),
        ("single", {
            "settings": {"mode": "RES2", "autorange": True, "nplc": 1},
            "name": "未确认的单次测量", "root": str(tmp_path), "interval_s": 0,
        }),
        ("start", {
            "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
            "name": "未确认的连续采集", "root": str(tmp_path), "interval_s": 0.02,
        }),
    )
    for action, payload in actions:
        status, _headers, body = _api_command(app, base_url, action, payload)
        assert status == 400
        assert "接线" in json.loads(body)["message"]
        assert runtime.worker.state.value == "stopped"

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


def test_web_real_meter_can_switch_local_and_computer_and_refresh_front_panel_state(
    local_service, monkeypatch,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport()
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )

    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {"eol": "CRLF"},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
        "front_panel_software_by_serial": {
            "TEST8845A001": {"outguard_sw": "2.10.882.24", "inguard_sw": "ad300210"},
        },
    })
    assert status == 200, body
    connected_state = _read_state(app, base_url)
    assert connected_state["control_mode"] == "computer"
    assert connected_state["device"]["front_panel_software"] == {
        "outguard_sw": "2.10.882.24", "inguard_sw": "ad300210",
    }

    status, _headers, body = _api_command(app, base_url, "control_mode", {"mode": "local"})
    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["connected"] is True
    assert state["control_mode"] == "local"
    assert "已停用" in state["status"] and "暂停" not in state["status"]

    status, _headers, body = _api_command(app, base_url, "configure", {
        "safety_confirmed": True,
        "settings": {"mode": "DCI", "autorange": True, "nplc": 1},
    })
    assert status == 400
    assert "LOCAL" in json.loads(body)["message"]

    transport.function = "CURR:DC"
    status, _headers, body = _api_command(app, base_url, "control_mode", {"mode": "computer"})
    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["control_mode"] == "computer"
    assert state["configuration"]["function"] == "CURR:DC"

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


def test_web_loads_saved_pre_2_firmware_before_initial_filter_readback(local_service, monkeypatch):
    app, _runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    from fluke8845a.scpi import Serial8845A

    class LegacyFirmwareTransport(FakeTransport):
        def read_until(self, terminator, timeout_s=None):
            if self.last_command.upper().endswith(":FILT:DIG?"):
                raise AssertionError("known unsupported filter query must be skipped")
            return super().read_until(terminator, timeout_s)

    transport = LegacyFirmwareTransport()
    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )

    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
        "front_panel_software_by_serial": {
            "TEST8845A001": {"outguard_sw": "1.9.0.0", "inguard_sw": "ad300210"},
        },
    })

    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["device"]["range_firmware"] == "1.9.0.0"
    assert state["active_settings"]["digital_filter_state"] == "unsupported"
    assert not any(command.endswith(":FILT:DIG?") or command.endswith(":FILT:DIG OFF") for command in transport.commands)

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


def test_web_uses_verified_outguard_fallback_for_ranges_without_filter_firmware_gate(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54", terminals="FRONT")
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )

    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {"eol": "CRLF"},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
        "front_panel_software_by_serial": {
            "TEST8845A001": {"outguard_sw": "2.10.882.24", "inguard_sw": "ad300210"},
        },
    })
    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["device"]["idn_version_candidate"] is None
    assert state["device"]["range_firmware"] == "2.10.882.24"
    assert state["device"]["range_firmware_source"] == "面板核对的 OutG SW"
    assert "digital_filter_blocked_nplc_values" not in state["device"]
    assert "digital_filter_slow_read_block_reasons" not in state["device"]

    def configure(range_si):
        return _api_command(app, base_url, "configure", {
            "safety_confirmed": True,
            "settings": {"mode": "DCI", "autorange": False, "range_si": range_si, "nplc": 1},
        })

    status, _headers, body = configure(0.4)
    assert status == 200, body

    status, _headers, body = _api_command(app, base_url, "firmware_versions", {
        "outguard_sw": "1.9.0.0", "inguard_sw": "ad300210",
    })
    assert status == 200, body
    state = _read_state(app, base_url)
    assert "digital_filter_blocked_nplc_values" not in state["device"]
    assert "digital_filter_slow_read_block_reasons" not in state["device"]
    assert state["active_settings"]["digital_filter_state"] == "unsupported"
    for range_si in (0.01, 0.1, 10.0):
        status, _headers, body = configure(range_si)
        assert status == 200, body
    status, _headers, body = configure(0.4)
    assert status == 400
    assert "400 mA" in json.loads(body)["message"]

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


@pytest.mark.parametrize(
    ("outguard_sw", "filter_commands_expected"),
    [("1.9.0.0", False), ("", True)],
)
def test_web_speed_allows_slow_nplc_with_known_legacy_or_unknown_filter_firmware(
    tmp_path, local_service, monkeypatch, outguard_sw, filter_commands_expected,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54")
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, settings, callback: real_instrument_type(
            port, settings, callback, transport_factory=lambda *_: transport
        ),
    )
    base_settings = {"mode": "DCV", "autorange": True, "nplc": 1, "autozero": True}
    try:
        status, _headers, body = _api_command(app, base_url, "connect_real", {
            "port": "COM3", "serial": {}, "settings": base_settings,
        })
        assert status == 200, body

        status, _headers, body = _api_command(app, base_url, "firmware_versions", {
            "outguard_sw": outguard_sw, "inguard_sw": "",
        })
        assert status == 200, body
        assert _read_state(app, base_url)["device"]["range_firmware"] == (outguard_sw or None)

        status, _headers, body = _api_command(app, base_url, "start", {
            "safety_confirmed": True,
            "settings": base_settings,
            "name": f"slow NPLC {outguard_sw or 'unknown'}",
            "root": str(tmp_path),
            "interval_s": 0.05,
        })
        assert status == 200, body
        state = _wait_for_state(
            app, base_url, lambda item: int((item.get("latest") or {}).get("sample_index", 0)) >= 1,
        )
        assert state["active_settings"]["nplc"] == 1

        commands_before_speed = list(transport.commands)
        status, _headers, body = _api_command(app, base_url, "speed", {
            "settings": {**base_settings, "nplc": 10},
        })
        assert status == 200, body
        result = json.loads(body)
        assert result["success"] is True
        assert runtime.worker.state.value == "running"

        speed_commands = transport.commands[len(commands_before_speed):]
        off = "SENS:VOLT:DC:FILT:DIG OFF"
        readback = "SENS:VOLT:DC:FILT:DIG?"
        nplc = "SENS:VOLT:DC:NPLC 10"
        if filter_commands_expected:
            assert speed_commands.index(off) < speed_commands.index(readback) < speed_commands.index(nplc)
        else:
            assert off not in speed_commands
            assert readback not in speed_commands
            assert nplc in speed_commands
    finally:
        if runtime.worker.state.value != "stopped":
            _api_command(app, base_url, "stop")
        if runtime.worker.instrument is not None and runtime.worker.instrument.connected:
            _api_command(app, base_url, "disconnect")


def test_web_configure_aggregates_cached_firmware_and_terminal_conflicts_before_scpi(
    local_service, monkeypatch,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54", terminals="REAR")
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, serial_settings, callback: real_instrument_type(
            port, serial_settings, callback, transport_factory=lambda *_: transport
        ),
    )
    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
    })
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "firmware_versions", {
        "outguard_sw": "1.9.0.0", "inguard_sw": "ad300210",
    })
    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["device"]["range_firmware"] == "1.9.0.0"
    assert state["configuration"]["input_terminals"] == "REAR"
    commands_before = list(transport.commands)

    status, _headers, body = _api_command(app, base_url, "configure", {
        "safety_confirmed": True,
        "settings": {"mode": "DCI", "autorange": True, "nplc": 1},
    })

    assert status == 400
    result = json.loads(body)
    assert result["success"] is False
    assert any("400 mA" in item and "OutG SW" in item and "1.9.0.0" in item for item in result["conflicts"])
    assert any("1 A" in item and "REAR" in item for item in result["conflicts"])
    assert result["readback_basis"]["range_firmware"] == "1.9.0.0"
    assert result["readback_basis"]["range_firmware_source"] == "面板核对的 OutG SW"
    assert result["readback_basis"]["input_terminals"] == "REAR"
    assert "最近一次仪器回读" in result["message"]
    assert transport.commands == commands_before

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


def test_web_configure_does_not_use_parseable_idn_candidate_as_firmware(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,1.0.700.17", terminals="FRONT")
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, serial_settings, callback: real_instrument_type(
            port, serial_settings, callback, transport_factory=lambda *_: transport
        ),
    )
    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
    })
    assert status == 200, body
    device = _read_state(app, base_url)["device"]
    assert device["idn_version_candidate"] == "1.0.700.17"
    assert device["range_firmware"] is None

    status, _headers, body = _api_command(app, base_url, "configure", {
        "safety_confirmed": True,
        "settings": {"mode": "DCI", "autorange": False, "range_si": 0.4, "nplc": 1},
    })

    assert status == 200, body
    assert json.loads(body)["success"] is True
    assert "CONF:CURR:DC" in transport.commands

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


def test_web_configure_returns_structured_conflict_for_mode_validation_failure(local_service, monkeypatch):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport()
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, serial_settings, callback: real_instrument_type(
            port, serial_settings, callback, transport_factory=lambda *_: transport
        ),
    )
    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
    })
    assert status == 200, body
    commands_before = list(transport.commands)

    status, _headers, body = _api_command(app, base_url, "configure", {
        "safety_confirmed": True,
        "settings": {"mode": "DCI", "autorange": False, "range_si": 0.3, "nplc": 1},
    })

    assert status == 400
    result = json.loads(body)
    assert result["success"] is False
    assert any("模式/参数" in item and "量程" in item for item in result["conflicts"])
    assert result["readback_basis"]["input_terminals"] == "FRONT"
    assert transport.commands == commands_before

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body


def test_web_start_preflight_blocks_known_conflicts_before_worker_configuration(
    tmp_path, local_service, monkeypatch,
):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"
    transport = FakeTransport("FLUKE,8845A,TEST8845A001,02/14/08-08:54", terminals="REAR")
    from fluke8845a import worker as worker_module
    from fluke8845a.scpi import Serial8845A

    real_instrument_type = Serial8845A
    monkeypatch.setattr(
        worker_module,
        "Serial8845A",
        lambda port, serial_settings, callback: real_instrument_type(
            port, serial_settings, callback, transport_factory=lambda *_: transport
        ),
    )
    status, _headers, body = _api_command(app, base_url, "connect_real", {
        "port": "COM3", "serial": {},
        "settings": {"mode": "DCV", "autorange": True, "nplc": 1},
    })
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "configure", {
        "safety_confirmed": True,
        "settings": {"mode": "DCI", "autorange": False, "range_si": 0.1, "nplc": 1},
    })
    assert status == 200, body
    status, _headers, body = _api_command(app, base_url, "firmware_versions", {
        "outguard_sw": "1.9.0.0", "inguard_sw": "ad300210",
    })
    assert status == 200, body
    commands_before = list(transport.commands)

    status, _headers, body = _api_command(app, base_url, "start", {
        "safety_confirmed": True,
        "settings": {"mode": "DCI", "autorange": True, "nplc": 1},
        "name": "受限电流量程",
        "root": str(tmp_path),
        "interval_s": 1,
    })

    assert status == 400
    result = json.loads(body)
    assert any("400 mA" in item for item in result["conflicts"])
    assert result["readback_basis"]["range_firmware"] == "1.9.0.0"
    assert result["readback_basis"]["input_terminals"] == "REAR"
    assert transport.commands == commands_before
    assert runtime.worker.state.value == "stopped"
    assert not runtime.store.snapshot()["experiment_dir"]

    status, _headers, body = _api_command(app, base_url, "disconnect")
    assert status == 200, body
