from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from PySide6.QtCore import QCoreApplication

from fluke8845a.modes import Mode
from fluke8845a.scpi import FlowControl
from fluke8845a.webapp import create_server
from fluke8845a.webservice import WebRuntime, mode_settings_from_payload, serial_settings_from_payload


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


def _http(url, *, method="GET", payload=None, origin=None, host=None):
    headers = {}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=1.5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _api_command(app, base_url, action, payload=None, *, origin=None):
    target_origin = origin if origin is not None else base_url
    return _main_thread_wait(app, lambda: _http(
        base_url + "/api/command", method="POST",
        payload={"action": action, "payload": payload or {}}, origin=target_origin,
    ))


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


def test_web_settings_accept_only_8845a_applicable_parameters():
    settings = mode_settings_from_payload({
        "mode": "DCV", "autorange": False, "range_si": 1, "nplc": 0.02,
        "analog_filter": True, "autozero": False,
    })
    assert settings.mode == Mode.DCV
    assert settings.range_si == 1
    assert settings.nplc == 0.02
    assert settings.analog_filter is True
    assert settings.autozero is False

    with pytest.raises(ValueError, match="固定量程"):
        mode_settings_from_payload({"mode": "DCV", "autorange": False, "range_si": 0.3})
    with pytest.raises(ValueError, match="不支持此交流滤波档"):
        mode_settings_from_payload({"mode": "ACV", "autorange": True, "ac_bandwidth_hz": 10})
    with pytest.raises(ValueError, match="NPLC"):
        mode_settings_from_payload({"mode": "ACI", "autorange": True, "nplc": 1})
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


def test_localhost_mock_api_enforces_origin_and_records_one_shot(tmp_path, local_service):
    app, runtime, server = local_service
    base_url = f"http://127.0.0.1:{server.server_port}"

    status, headers, page = _main_thread_wait(app, lambda: _http(base_url + "/"))
    assert status == 200
    assert b"Fluke 8845A" in page
    assert headers["Content-Security-Policy"]
    assert b'id="settingsDialog"' in page
    assert b'id="openSettings"' in page
    assert b'id="autoScale"' in page
    assert b'id="plot"' in page
    assert b'class="control-rail"' not in page
    main_view = page.split(b"<dialog", 1)[0]
    assert b'id="mode"' not in main_view and b'id="serialTimeout"' not in main_view

    status, _headers, stylesheet = _main_thread_wait(app, lambda: _http(base_url + "/app.css"))
    assert status == 200
    assert b".settings-dialog" in stylesheet
    status, _headers, script = _main_thread_wait(app, lambda: _http(base_url + "/app.js"))
    assert status == 200
    assert b"event.altKey" in script and b"event.ctrlKey" in script and b"event.shiftKey" in script
    assert b"app.yView = zoomRange" in script

    status, _headers, metadata = _main_thread_wait(app, lambda: _http(base_url + "/api/meta"))
    assert status == 200
    assert {mode["value"] for mode in json.loads(metadata)["modes"]} == {mode.value for mode in Mode}

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
            "safety_confirmed": True, "settings": mode_settings,
        })
        assert status == 200, body

    run_settings = {"mode": "RES4", "autorange": True, "nplc": 1, "autozero": True}
    status, _headers, body = _api_command(app, base_url, "single", {
        "safety_confirmed": False, "settings": run_settings,
        "name": "localhost短测", "root": str(tmp_path), "interval_s": 0,
    })
    assert status == 400
    assert "接线" in json.loads(body)["message"]

    status, _headers, body = _api_command(app, base_url, "single", {
        "safety_confirmed": True, "settings": run_settings,
        "name": "localhost短测", "root": str(tmp_path), "interval_s": 0,
    })
    assert status == 200, body
    state = _read_state(app, base_url)
    run_dir = state["last_experiment_dir"]
    assert run_dir
    assert state["simulation"] is True
    assert state["latest"]["mode"] == "RES4"
    assert state["latest"]["status"] == "ok"

    status, _headers, body = _api_command(app, base_url, "start", {
        "safety_confirmed": True, "settings": run_settings,
        "name": "localhost连续短测", "root": str(tmp_path), "interval_s": 0.02,
    })
    assert status == 200, body
    state = _wait_for_state(app, base_url, lambda item: int((item.get("latest") or {}).get("sample_index", 0)) >= 3)

    status, _headers, body = _api_command(app, base_url, "event", {"text": "负载切换"})
    assert status == 200, body
    state = _read_state(app, base_url)
    assert state["events"][-1]["text"] == "负载切换"

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
    state = _read_state(app, base_url)
    run_dir = state["last_experiment_dir"]
    assert state["simulation"] is True

    status, _headers, csv_body = _main_thread_wait(app, lambda: _http(base_url + "/api/latest.csv"))
    assert status == 200
    assert b"raw_reading" in csv_body
    run_path = tmp_path / Path(run_dir).name
    assert json.loads((run_path / "config.json").read_text(encoding="utf-8"))["simulation"] is True
    assert (run_path / "events.csv").read_text(encoding="utf-8-sig").count("负载切换") == 1
    assert (run_path / "config.json").is_file()
    assert runtime.worker.instrument is not None
