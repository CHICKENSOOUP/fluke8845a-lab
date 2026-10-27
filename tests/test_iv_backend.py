import pytest

from fluke8845a.webservice import WebCommandRouter, WebStateStore
from fluke8845a.state import AcquisitionState
from fluke8845a.worker import MeasurementWorker
from fluke8845a.mock import MockScenario
from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings


def test_voltage_boundary_is_recorded_and_exposed():
    store = WebStateStore()
    store.begin_experiment('run')
    worker = MeasurementWorker()
    worker._machine.start()
    worker.mark_event = lambda text: {
        'sample_index': 17, 'elapsed_s': '2.5', 'event': text,
    }
    router = WebCommandRouter(worker, store)
    result = router._execute('iv_voltage', {'voltage_v': -1.25})
    assert result['sample_index'] == 17
    assert result['voltage_v'] == -1.25
    assert store.snapshot()['iv_voltage_boundary'] == result['boundary']


def test_voltage_boundary_rejects_nonfinite_or_inactive():
    store = WebStateStore()
    worker = MeasurementWorker()
    router = WebCommandRouter(worker, store)
    with pytest.raises(ValueError):
        router._execute('iv_voltage', {'voltage_v': 1})
    worker._machine.start()
    with pytest.raises(ValueError):
        router._execute('iv_voltage', {'voltage_v': float('nan')})


@pytest.mark.parametrize('action', ['interval', 'speed'])
def test_iv_acquisition_rejects_runtime_parameter_changes(action):
    worker = MeasurementWorker()
    worker._machine.start()
    worker._iv_mode = True
    router = WebCommandRouter(worker, WebStateStore())
    with pytest.raises(ValueError, match='采集中'):
        router._execute(action, {})


def test_iv_mock_timeout_stops_at_first_failure_and_preserves_record(tmp_path):
    worker = MeasurementWorker()
    settings = ModeSettings(Mode.DCI)
    worker.connect_mock(settings, MockScenario(name='timeout', timeout_at_sample=1, read_latency_s=0))
    worker._iv_mode = True
    worker.start_experiment(settings, 'iv', str(tmp_path), 1, False)
    recorder = worker._recorder
    assert recorder is not None
    worker._take_sample()
    assert worker.state == AcquisitionState.STOPPED
    assert 'timeout' in (recorder.directory / 'measurements.csv').read_text(encoding='utf-8-sig')
    worker.shutdown()
