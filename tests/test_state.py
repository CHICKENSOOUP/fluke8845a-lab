import pytest

from fluke8845a.state import AcquisitionState, AcquisitionStateMachine, InvalidTransition


def test_acquisition_state_machine_start_pause_resume_stop():
    machine = AcquisitionStateMachine()
    assert machine.state == AcquisitionState.STOPPED
    machine.start()
    machine.pause()
    machine.resume()
    machine.stop()
    assert machine.state == AcquisitionState.STOPPED


@pytest.mark.parametrize("operation", ["pause", "resume", "stop"])
def test_invalid_state_transitions_are_rejected(operation):
    machine = AcquisitionStateMachine()
    with pytest.raises(InvalidTransition):
        getattr(machine, operation)()
    machine.start()
    with pytest.raises(InvalidTransition):
        machine.start()

