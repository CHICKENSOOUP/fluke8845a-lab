"""Small, deterministic acquisition state machine shared by GUI and tests."""

from __future__ import annotations

from enum import StrEnum


class AcquisitionState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class InvalidTransition(RuntimeError):
    pass


class AcquisitionStateMachine:
    def __init__(self) -> None:
        self.state = AcquisitionState.STOPPED

    def start(self) -> None:
        if self.state != AcquisitionState.STOPPED:
            raise InvalidTransition(f"不能从{self.state.value}状态开始实验")
        self.state = AcquisitionState.RUNNING

    def pause(self) -> None:
        if self.state != AcquisitionState.RUNNING:
            raise InvalidTransition(f"不能从{self.state.value}状态暂停")
        self.state = AcquisitionState.PAUSED

    def resume(self) -> None:
        if self.state != AcquisitionState.PAUSED:
            raise InvalidTransition(f"不能从{self.state.value}状态继续")
        self.state = AcquisitionState.RUNNING

    def stop(self) -> None:
        if self.state == AcquisitionState.STOPPED:
            raise InvalidTransition("当前没有进行中的实验")
        self.state = AcquisitionState.STOPPED

