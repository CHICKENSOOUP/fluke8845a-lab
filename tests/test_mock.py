from __future__ import annotations

import csv
import json
import math
from datetime import datetime

import pytest

from fluke8845a.modes import DESCRIPTORS, Mode
from fluke8845a.mock import Mock8845A, MockScenario
from fluke8845a.scpi import ModeSettings
from fluke8845a.storage import EVENT_FIELDS, SAMPLE_FIELDS, ExperimentRecorder
from scripts.generate_demo import generate


@pytest.mark.parametrize("mode", list(Mode))
def test_mock_supports_all_six_modes_with_valid_timestamps_and_units(mode):
    scenario = MockScenario(noise_fraction=0, drift_fraction_per_minute=0, step_at_s=1e9, recovery_at_s=None, read_latency_s=0)
    mock = Mock8845A(scenario)
    mock.connect()
    spec = DESCRIPTORS[mode]
    settings = ModeSettings(mode, nplc=1 if spec.nplc_values else None,
                            ac_bandwidth_hz=20 if spec.ac_bandwidths_hz else None)
    mock.configure(settings)
    reading = mock.read()
    assert math.isfinite(reading.value_si)
    assert reading.range_si is not None
    assert datetime.fromisoformat(reading.timestamp_utc).tzinfo is not None
    assert reading.received_monotonic_ns >= reading.started_monotonic_ns
    mock.close()


def test_mock_timeout_and_disconnect_are_injected_without_values():
    timeout = Mock8845A(MockScenario(timeout_at_sample=1, read_latency_s=0))
    timeout.connect()
    timeout.configure(ModeSettings(Mode.DCV))
    with pytest.raises(TimeoutError):
        timeout.read()

    disconnected = Mock8845A(MockScenario(disconnect_at_sample=1))
    disconnected.connect()
    disconnected.configure(ModeSettings(Mode.DCI))
    with pytest.raises(ConnectionError):
        disconnected.read()
    assert not disconnected.connected


def test_mock_read_duration_and_recorded_interval_use_high_resolution_clock(tmp_path, monkeypatch):
    import fluke8845a.mock as mock_module

    clock_ns = [1_000_000_000]

    def sleep(seconds):
        clock_ns[0] += round(seconds * 1_000_000_000)

    monkeypatch.setattr(mock_module.time, "monotonic_ns", lambda: 1_000_000_000)
    monkeypatch.setattr(mock_module.time, "perf_counter_ns", lambda: clock_ns[0])
    monkeypatch.setattr(mock_module.time, "sleep", sleep)

    mock = Mock8845A(MockScenario(read_latency_s=0.01))
    mock.connect()
    mock.configure(ModeSettings(Mode.DCV))
    recorder = ExperimentRecorder.create(
        tmp_path, "high resolution clock", {"simulation": True}, ModeSettings(Mode.DCV)
    )
    try:
        first = mock.read()
        first_row = recorder.append_reading(first, mode=Mode.DCV)
        second = mock.read()
        second_row = recorder.append_reading(second, mode=Mode.DCV)

        assert first.read_duration_s == pytest.approx(0.01)
        assert first_row["read_duration_s"] == repr(0.01)
        assert second_row["actual_interval_s"] == repr(0.01)
    finally:
        recorder.finish("test")
        mock.close()


def test_mock_labels_input_impedance_as_simulated_and_preserves_it_during_speed_change():
    mock = Mock8845A(MockScenario(read_latency_s=0))
    mock.connect()
    settings = ModeSettings(Mode.DCV, input_impedance_auto=True)
    result = mock.configure(settings)
    assert result["input_impedance_auto_response"] == "True"
    assert mock.read_settings(Mode.DCV).input_impedance_auto is True

    mock.set_measurement_speed(ModeSettings(Mode.DCV, nplc=0.2))
    assert mock.read_settings(Mode.DCV).input_impedance_auto is True
    mock.close()


def test_mock_step_drift_and_noise_are_repeatably_configurable():
    settings = ModeSettings(Mode.RES2, nplc=1)
    mock = Mock8845A(MockScenario(
        seed=10, read_latency_s=0, drift_fraction_per_minute=0,
        step_at_s=0, step_fraction=0, recovery_at_s=None,
    ))
    mock.connect()
    mock.configure(settings)
    a = mock.read().raw
    mock.close()
    another = Mock8845A(MockScenario(
        seed=10, read_latency_s=0, drift_fraction_per_minute=0,
        step_at_s=0, step_fraction=0, recovery_at_s=None,
    ))
    another.connect()
    another.configure(settings)
    b = another.read().raw
    assert a == b


def test_demo_generator_writes_explicit_six_mode_experiment_bundles(tmp_path):
    output = tmp_path / "mock_examples"

    generate(output)

    assert {path.name for path in output.iterdir() if path.is_dir()} == {mode.value for mode in Mode}
    assert "全部由内置 MOCK" in (output / "README.md").read_text(encoding="utf-8")
    for mode in Mode:
        experiment = output / mode.value
        config = json.loads((experiment / "config.json").read_text(encoding="utf-8"))
        assert config["simulation"] is True
        assert "不是8845A实测数据" in config["example_data_notice"]
        for filename in ("measurements.csv", "events.csv", "run.log"):
            assert (experiment / filename).is_file()
        with (experiment / "measurements.csv").open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            measurements = list(reader)
            assert reader.fieldnames == list(SAMPLE_FIELDS)
        with (experiment / "events.csv").open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            events = list(reader)
            assert reader.fieldnames == list(EVENT_FIELDS)
        assert config["software_version"] == "2.0.0"
        assert config["sample_count"] == len(measurements) == 30
        assert config["event_count"] == len(events) == 4
        assert all(row["experiment_id"] == config["experiment_id"] for row in measurements + events)
        assert all(float(row["read_duration_s"]) > 0 for row in measurements)
        assert all(float(row["actual_interval_s"]) > 0 for row in measurements[1:])

