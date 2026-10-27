import pytest

from fluke8845a.webservice import WebCommandRouter, WebStateStore
from fluke8845a.state import AcquisitionState
from fluke8845a.worker import MeasurementWorker
from fluke8845a.mock import MockScenario
from fluke8845a.modes import Mode
from fluke8845a.scpi import ModeSettings, Serial8845A, SerialSettings
from tests.fakes import FakeTransport


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


def test_user_default_firmware_provenance_is_preserved_in_state_and_run(tmp_path):
    import json

    transport = FakeTransport(terminals='FRONT')
    instrument = Serial8845A('COM3', SerialSettings(), transport_factory=lambda *_: transport)
    worker = MeasurementWorker()
    worker.instrument = instrument
    worker.info = instrument.connect()
    worker.control_mode = 'computer'
    logs = []
    worker.log_line.connect(logs.append)
    store = WebStateStore()
    router = WebCommandRouter(worker, store)
    result = router._execute('firmware_versions', {
        'outguard_sw': '2.10.882.24', 'inguard_sw': '', 'source': 'user_default',
    })
    assert '未经仪器核验' in result['message']
    assert store.snapshot()['device']['range_firmware_source'] == '用户指定默认值，未经仪器核验'
    assert store.snapshot()['device']['front_panel_software']['outguard_sw'] is None
    assert any('用户指定默认值' in line and '未经仪器核验' in line for line in logs)
    settings = ModeSettings(Mode.DCI, autorange=False, range_si=0.4)
    worker.start_experiment(settings, 'iv-default', str(tmp_path), 1, False, confirmed_settings=settings)
    recorder = worker._recorder
    assert recorder is not None
    worker.stop()
    device = json.loads((recorder.directory / 'config.json').read_text(encoding='utf-8'))['device']
    assert device['range_firmware'] == '2.10.882.24'
    assert device['range_firmware_source'] == '用户指定默认值，未经仪器核验'
    assert device['front_panel_software']['outguard_sw'] is None
    assert '用户指定默认值' in (recorder.directory / 'run.log').read_text(encoding='utf-8')
    worker.shutdown()
