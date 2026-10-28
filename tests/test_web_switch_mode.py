import pytest

from fluke8845a.modes import Mode
from fluke8845a.webservice import WebRuntime
from tests.test_webapp import qt_app


def test_switch_mode_uses_worker_unconfirmed_slot(qt_app, monkeypatch):
    runtime = WebRuntime()
    calls = []
    monkeypatch.setattr(runtime.worker, "configure_mode_without_wiring_confirmation", calls.append)
    result = runtime.router._execute("switch_mode", {"settings": {"mode": "ACV"}})
    assert result["success"]
    assert [item.mode for item in calls] == [Mode.ACV]


def test_switch_mode_refuses_during_acquisition(qt_app, monkeypatch):
    runtime = WebRuntime()
    runtime.worker._machine.start()
    calls = []
    monkeypatch.setattr(runtime.worker, "configure_mode_without_wiring_confirmation", calls.append)
    with pytest.raises(ValueError, match="停止采集"):
        runtime.router._execute("switch_mode", {"settings": {"mode": "ACV"}})
    assert calls == []


def test_normal_configure_still_requires_wiring_confirmation(qt_app):
    runtime = WebRuntime()
    with pytest.raises(ValueError, match="确认"):
        runtime.router._execute("configure", {"settings": {"mode": "ACV"}})


def test_start_still_requires_wiring_confirmation(qt_app):
    runtime = WebRuntime()
    runtime.worker.instrument = type("Connected", (), {"connected": True})()
    runtime.worker.control_mode = "computer"
    with pytest.raises(ValueError, match="确认"):
        runtime.router._execute("start", {"settings": {"mode": "ACV"}})


def test_switch_mode_propagates_worker_failure(qt_app, monkeypatch):
    runtime = WebRuntime()
    monkeypatch.setattr(runtime.worker, "configure_mode_without_wiring_confirmation",
                        lambda _settings: runtime.router._on_error("模拟错误"))
    result = runtime.router._execute("switch_mode", {"settings": {"mode": "ACV"}})
    assert result == {"success": False, "message": "模拟错误"}
