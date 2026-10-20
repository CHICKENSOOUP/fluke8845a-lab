"""Regression checks for model-specific ranges in the supplied Fluke specs."""

import pytest

from fluke8845a.modes import (
    AC_BANDWIDTHS_HZ, DESCRIPTORS, Mode, NPLC_VALUES,
    mode_transition_requires_wiring_confirmation,
)


@pytest.mark.parametrize(
    ("previous", "target", "required"),
    [
        (None, Mode.DCV, True),
        (Mode.DCV, Mode.ACV, False),
        (Mode.DCV, Mode.DCI, True),
        (Mode.DCI, Mode.ACI, True),
        (Mode.RES2, Mode.RES4, True),
        (Mode.RES2, Mode.RES2, False),
    ],
)
def test_wiring_confirmation_policy_matches_terminal_group_changes(previous, target, required):
    assert mode_transition_requires_wiring_confirmation(previous, target) is required


def test_descriptors_match_documented_8845a_ranges_and_applicable_controls():
    expected_ranges = {
        Mode.DCV: (0.1, 1, 10, 100, 1000),
        Mode.ACV: (0.1, 1, 10, 100, 750),
        Mode.DCI: (100e-6, 1e-3, 10e-3, 100e-3, 400e-3, 1, 3, 10),
        Mode.ACI: (10e-3, 100e-3, 400e-3, 1, 3, 10),
        Mode.RES2: (100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6),
        Mode.RES4: (100, 1e3, 10e3, 100e3, 1e6, 10e6, 100e6),
    }
    nplc_modes = {Mode.DCV, Mode.DCI, Mode.RES2, Mode.RES4}
    ac_bandwidth_modes = {Mode.ACV, Mode.ACI}
    analog_filter_modes = {Mode.DCV, Mode.DCI, Mode.RES2, Mode.RES4}
    digital_filter_modes = {Mode.DCV, Mode.DCI, Mode.RES2, Mode.RES4}

    assert set(DESCRIPTORS) == set(Mode)
    for mode, ranges in expected_ranges.items():
        spec = DESCRIPTORS[mode]
        assert spec.ranges_si == ranges
        assert spec.nplc_values == (NPLC_VALUES if mode in nplc_modes else ())
        assert spec.ac_bandwidths_hz == (AC_BANDWIDTHS_HZ if mode in ac_bandwidth_modes else ())
        assert spec.analog_filter is (mode in analog_filter_modes)
        assert spec.digital_filter is (mode in digital_filter_modes)
        assert spec.input_impedance_auto is (mode == Mode.DCV)

    # These ranges belong to 8846A-only capabilities and must never reach the 8845A GUI.
    assert 10 not in DESCRIPTORS[Mode.RES2].ranges_si
    assert 10 not in DESCRIPTORS[Mode.RES4].ranges_si
    assert 100e-6 not in DESCRIPTORS[Mode.ACI].ranges_si
    assert 1e-3 not in DESCRIPTORS[Mode.ACI].ranges_si
    assert DESCRIPTORS[Mode.ACV].ranges_si[-1] == 750


@pytest.mark.parametrize(
    ("mode", "required_limits"),
    [
        (Mode.DCI, ("100 µA–10 A", "400 mA 连续", "550 mA", "440 mA", "11 A", "2.0")),
        (Mode.ACI, ("10 mA–10 A", "550 mA", "440 mA", "11 A", "1.0.700.18", "3:1")),
        (Mode.RES2, ("100 Ω–100 MΩ",)),
        (Mode.RES4, ("100 Ω–100 MΩ",)),
    ],
)
def test_8845a_current_and_resistance_descriptors_include_critical_limits(mode, required_limits):
    limits = DESCRIPTORS[mode].safety_limits

    for limit in required_limits:
        assert limit in limits


def test_acv_mode_description_includes_input_limits_and_frequency_ranges():
    description = f"{DESCRIPTORS[Mode.ACV].safety}\n{DESCRIPTORS[Mode.ACV].safety_limits}"

    for detail in (
        "750 V RMS",
        "1000 V peak",
        "8×10^7 V·Hz",
        "慢档 3 Hz–300 kHz",
        "中档 20 Hz–300 kHz",
        "快档 200 Hz–300 kHz",
        "06_Extended_Specifications.pdf",
        "PDF p.4 / printed p.3",
    ):
        assert detail in description


def test_aci_mode_description_includes_frequency_ranges():
    description = f"{DESCRIPTORS[Mode.ACI].safety}\n{DESCRIPTORS[Mode.ACI].safety_limits}"

    for detail in (
        "慢档 3 Hz–10 kHz",
        "中档 20 Hz–10 kHz",
        "快档 200 Hz–10 kHz",
        "06_Extended_Specifications.pdf",
        "PDF p.8 / printed p.7",
    ):
        assert detail in description
    assert "软件版本 ≥ 1.0.700.18" in description
    assert "未说明对应哪个软件字段" in description
    assert "OutG SW ≥ 1.0.700.18" not in description


@pytest.mark.parametrize("mode", [Mode.ACV, Mode.DCI, Mode.ACI, Mode.RES2, Mode.RES4])
def test_auto_input_impedance_is_rejected_outside_dcv(mode):
    from fluke8845a.scpi import ModeSettings

    with pytest.raises(ValueError, match="输入阻抗"):
        ModeSettings(mode, input_impedance_auto=True).validate()

