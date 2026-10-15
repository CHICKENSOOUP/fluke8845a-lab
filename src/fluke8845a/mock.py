"""Deterministic, explicitly simulated 8845A-like backend for UI and test work."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .modes import Mode, descriptor
from .scpi import InstrumentInfo, ModeSettings, Reading, compare_firmware


@dataclass(frozen=True, slots=True)
class MockScenario:
    name: str = "阶跃响应"
    noise_fraction: float = 2e-4
    drift_fraction_per_minute: float = 1e-5
    step_at_s: float = 3.0
    step_fraction: float = 0.05
    recovery_at_s: float | None = 15.0
    response_tau_s: float = 1.2
    read_latency_s: float = 0.015
    timeout_at_sample: int | None = None
    disconnect_at_sample: int | None = None
    seed: int = 8845


class Mock8845A:
    """Implements the same operations as the real backend, with all data marked Mock."""

    def __init__(self, scenario: MockScenario | None = None, log_callback=None) -> None:
        self.scenario = scenario or MockScenario()
        self._log = log_callback or (lambda _line: None)
        self.info = InstrumentInfo(
            "MOCK FLUKE", "8845A-MOCK", "SIM-0001", "",
            "MOCK,8845A-MOCK,SIM-0001,MOCK", idn_revision="MOCK",
        )
        self.connected = False
        self._settings: ModeSettings | None = None
        self._started_ns = 0
        self._sample_number = 0
        self._rng = random.Random(self.scenario.seed)
        self._zeroed = False

    def connect(self) -> InstrumentInfo:
        self.connected = True
        self._started_ns = time.monotonic_ns()
        self._sample_number = 0
        self._rng = random.Random(self.scenario.seed)
        self._log(f"MOCK已连接，场景={self.scenario.name}；所有读数均为模拟数据")
        return self.info

    def close(self, *, restore_local: bool = True) -> None:
        del restore_local  # The mock has no remote/local front-panel state.
        self.connected = False
        self._log("MOCK已断开")

    def configure(self, settings: ModeSettings) -> dict[str, str | float | bool | None]:
        settings.validate()
        if not self.connected:
            raise ConnectionError("Mock未连接")
        self._settings = settings
        spec = descriptor(settings.mode)
        selected_range = settings.range_si if not settings.autorange else next(
            (r for r in spec.ranges_si if r >= self._base_value(spec.quantity) * 1.1),
            spec.ranges_si[-1],
        )
        self._log(f"MOCK配置：{spec.label}；量程={'自动' if settings.autorange else selected_range} SI")
        return {
            "function": spec.function,
            "range_response": "AUTO" if settings.autorange else str(selected_range),
            "autorange_response": str(settings.autorange),
            "nplc_response": str(settings.nplc) if settings.nplc is not None else None,
            "ac_bandwidth_response": str(settings.ac_bandwidth_hz) if settings.ac_bandwidth_hz is not None else None,
            "analog_filter_response": str(settings.analog_filter) if spec.analog_filter else None,
            "autozero_response": str(settings.autozero),
            "input_terminals": "FRONT (simulated)",
        }

    def set_measurement_speed(self, settings: ModeSettings) -> dict[str, str | None]:
        settings.validate()
        if not self.connected or self._settings is None:
            raise ConnectionError("MOCK未连接或尚未配置")
        if settings.mode != self._settings.mode:
            raise ValueError("MOCK实时调速只能用于当前测量模式")
        self._settings = ModeSettings(
            mode=self._settings.mode,
            autorange=self._settings.autorange,
            range_si=self._settings.range_si,
            nplc=settings.nplc,
            ac_bandwidth_hz=settings.ac_bandwidth_hz,
            analog_filter=self._settings.analog_filter,
            autozero=self._settings.autozero,
        )
        self._log(
            f"MOCK调速：NPLC={self._settings.nplc}；交流带宽={self._settings.ac_bandwidth_hz} Hz"
        )
        return {
            "nplc_response": str(self._settings.nplc) if self._settings.nplc is not None else None,
            "ac_bandwidth_response": str(self._settings.ac_bandwidth_hz)
            if self._settings.ac_bandwidth_hz is not None else None,
        }

    def _base_value(self, quantity: str) -> float:
        return {"voltage": 1.2, "current": 0.012, "resistance": 1000.0}[quantity]

    def read(self) -> Reading:
        if not self.connected:
            raise ConnectionError("MOCK断线")
        if self._settings is None:
            raise RuntimeError("MOCK尚未配置")
        started_ns = time.monotonic_ns()
        now = datetime.now(timezone.utc)
        self._sample_number += 1
        if self.scenario.disconnect_at_sample == self._sample_number:
            self.connected = False
            raise ConnectionError(f"MOCK模拟断线（第{self._sample_number}次采集）")
        if self.scenario.timeout_at_sample == self._sample_number:
            time.sleep(max(self.scenario.read_latency_s, 0.05))
            raise TimeoutError(f"MOCK模拟通信超时（第{self._sample_number}次采集）")
        if self.scenario.read_latency_s > 0:
            time.sleep(self.scenario.read_latency_s)

        elapsed = (started_ns - self._started_ns) / 1e9
        spec = descriptor(self._settings.mode)
        base = self._base_value(spec.quantity)
        drift = base * self.scenario.drift_fraction_per_minute * elapsed / 60
        step_elapsed = elapsed - self.scenario.step_at_s
        step = 0.0
        if step_elapsed >= 0:
            step += base * self.scenario.step_fraction * (1 - math.exp(-step_elapsed / max(self.scenario.response_tau_s, 1e-9)))
        if self.scenario.recovery_at_s is not None and elapsed >= self.scenario.recovery_at_s:
            recovery_elapsed = elapsed - self.scenario.recovery_at_s
            held = base * self.scenario.step_fraction * (1 - math.exp(-max(self.scenario.recovery_at_s - self.scenario.step_at_s, 0) / max(self.scenario.response_tau_s, 1e-9)))
            step -= held * (1 - math.exp(-recovery_elapsed / max(self.scenario.response_tau_s, 1e-9)))
        noise = self._rng.gauss(0.0, abs(base) * self.scenario.noise_fraction)
        value = base + drift + step + noise
        raw = f"{value:.15E}"
        received_ns = time.monotonic_ns()
        utc = now.isoformat(timespec="microseconds")
        local = now.astimezone().isoformat(timespec="microseconds")
        rang = self._settings.range_si if not self._settings.autorange else next(
            (r for r in spec.ranges_si if r >= abs(value) * 1.1), spec.ranges_si[-1]
        )
        return Reading(raw, value, rang, started_ns, received_ns, utc, local)

    def single(self) -> Reading:
        return self.read()

    def diagnose(self) -> dict[str, str | None]:
        if not self.connected:
            raise ConnectionError("MOCK未连接")
        return {"idn": self.info.idn_raw, "function": self._settings.mode.value if self._settings else None,
                "terminals": "FRONT (simulated)", "error": "0,\"No error (simulated)\""}

    def zero(self) -> None:
        if not self.connected:
            raise ConnectionError("MOCK未连接")
        self._zeroed = True
        self._log("MOCK零点命令已模拟执行；未操作任何真实仪器")

