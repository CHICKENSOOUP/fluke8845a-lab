import pytest

from fluke8845a.webservice import WebCommandRouter, WebStateStore
from fluke8845a.state import AcquisitionState
from fluke8845a.worker import MeasurementWorker


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
