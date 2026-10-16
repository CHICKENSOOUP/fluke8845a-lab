"""8845A-only measurement capabilities and their verified SCPI parameters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Mode(StrEnum):
    DCV = "DCV"
    ACV = "ACV"
    DCI = "DCI"
    ACI = "ACI"
    RES2 = "RES2"
    RES4 = "RES4"


@dataclass(frozen=True, slots=True)
class ModeDescriptor:
    mode: Mode
    label: str
    function: str
    quantity: str
    unit_si: str
    ranges_si: tuple[float, ...]
    nplc_values: tuple[float, ...] = ()
    ac_bandwidths_hz: tuple[int, ...] = ()
    analog_filter: bool = False
    digital_filter: bool = False
    input_impedance_auto: bool = False
    rms: bool = False
    wiring_group: str = "voltage"
    safety: str = ""

    @property
    def sense_prefix(self) -> str:
        return f"SENS:{self.function}"

    @property
    def display_quantity(self) -> str:
        return f"{self.label} (RMS)" if self.rms else self.label


NPLC_VALUES: Final[tuple[float, ...]] = (0.02, 0.2, 1, 10, 100)
AC_BANDWIDTHS_HZ: Final[tuple[int, ...]] = (3, 20, 200)


DESCRIPTORS: Final[dict[Mode, ModeDescriptor]] = {
    Mode.DCV: ModeDescriptor(
        Mode.DCV, "直流电压 DCV", "VOLT:DC", "voltage", "V",
        (0.1, 1, 10, 100, 1000), NPLC_VALUES, analog_filter=True, digital_filter=True,
        input_impedance_auto=True,
        wiring_group="voltage",
        safety="断开危险电压后接入电压 HI/LO 端子；电压测量并联在被测节点。核对输入电压和 CAT 等级。",
    ),
    Mode.ACV: ModeDescriptor(
        Mode.ACV, "交流电压 ACV · 真 RMS", "VOLT:AC", "voltage", "V",
        (0.1, 1, 10, 100, 750), ac_bandwidths_hz=AC_BANDWIDTHS_HZ,
        rms=True, wiring_group="voltage",
        safety="交流电压读数为仪器真 RMS 值。接电压 HI/LO 端子并联测量；8845A 上限 750 V RMS。",
    ),
    Mode.DCI: ModeDescriptor(
        Mode.DCI, "直流电流 DCI", "CURR:DC", "current", "A",
        (100e-6, 1e-3, 10e-3, 100e-3, 400e-3, 1, 3, 10), NPLC_VALUES,
        analog_filter=True, digital_filter=True, wiring_group="current",
        safety="电流测量必须串联：先关闭被测回路电源并核实保险丝和预期电流；严禁将电流输入并联到电压源。",
    ),
    Mode.ACI: ModeDescriptor(
        Mode.ACI, "交流电流 ACI · 真 RMS", "CURR:AC", "current", "A",
        (10e-3, 100e-3, 400e-3, 1, 3, 10), ac_bandwidths_hz=AC_BANDWIDTHS_HZ,
        rms=True, wiring_group="current",
        safety="交流电流读数为仪器真 RMS 值。必须串联测量：关闭回路电源并核实保险丝和预期电流；严禁将电流输入并联到电压源。",
    ),
    Mode.RES2: ModeDescriptor(
        Mode.RES2, "两线电阻 2W", "RES", "resistance", "Ohm",
        (100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6), NPLC_VALUES,
        analog_filter=True, digital_filter=True, wiring_group="resistance-2w",
        safety="电阻测量前必须使被测电路断电并确认无外加电压；两线法使用相应 HI/LO 端子。引线电阻会进入读数，可在适当条件下执行仪器 ZERO。",
    ),
    Mode.RES4: ModeDescriptor(
        Mode.RES4, "四线电阻 4W", "FRES", "resistance", "Ohm",
        (100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6), NPLC_VALUES,
        analog_filter=True, digital_filter=True, wiring_group="resistance-4w",
        safety="电阻测量前必须使被测电路断电并确认无外加电压；四线法将 FORCE 端接电流线、SENSE 端接被测电阻两端。",
    ),
}


def descriptor(mode: Mode | str) -> ModeDescriptor:
    return DESCRIPTORS[Mode(mode)]


def mode_from_scpi_function(response: str) -> Mode | None:
    """Map an instrument's SENS:FUNC? response to a supported 8845A mode."""
    normalized = response.strip().strip('\"\'').upper()
    # The 8845A can report the short SCPI mnemonics for DC functions as well as
    # the explicit CONF/SENS forms used by this application (e.g. VOLT vs
    # VOLT:DC). Match complete tokens only so unsupported functions never match
    # accidentally by substring.
    aliases = {
        "VOLT": Mode.DCV,
        "VOLT:DC": Mode.DCV,
        "VOLTAGE:DC": Mode.DCV,
        "VOLT:AC": Mode.ACV,
        "VOLTAGE:AC": Mode.ACV,
        "CURR": Mode.DCI,
        "CURR:DC": Mode.DCI,
        "CURRENT": Mode.DCI,
        "CURRENT:DC": Mode.DCI,
        "CURR:AC": Mode.ACI,
        "CURRENT:AC": Mode.ACI,
        "RES": Mode.RES2,
        "RESISTANCE": Mode.RES2,
        "FRES": Mode.RES4,
        "FRESISTANCE": Mode.RES4,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return Mode(normalized)
    except ValueError:
        pass
    for mode, spec in DESCRIPTORS.items():
        if normalized == spec.function.upper():
            return mode
    return None


def range_label(value: float, unit: str) -> str:
    """Human-friendly range text; instrument and CSV values remain SI."""
    if unit == "V":
        if value < 1:
            return f"{value * 1000:g} mV"
        return f"{value:g} V"
    if unit == "A":
        if value < 1e-6:
            return f"{value * 1e9:g} nA"
        if value < 1e-3:
            return f"{value * 1e6:g} µA"
        if value < 1:
            return f"{value * 1e3:g} mA"
        return f"{value:g} A"
    if value >= 1e6:
        return f"{value / 1e6:g} MΩ"
    if value >= 1e3:
        return f"{value / 1e3:g} kΩ"
    return f"{value:g} Ω"

