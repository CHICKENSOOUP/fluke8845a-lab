from __future__ import annotations

import math
from datetime import datetime

import pytest

from fluke8845a.modes import DESCRIPTORS, Mode
from fluke8845a.mock import Mock8845A, MockScenario
from fluke8845a.scpi import ModeSettings


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

