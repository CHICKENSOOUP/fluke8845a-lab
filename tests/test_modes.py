"""Regression checks for model-specific ranges in the supplied Fluke specs."""

from fluke8845a.modes import AC_BANDWIDTHS_HZ, DESCRIPTORS, Mode, NPLC_VALUES


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

    assert set(DESCRIPTORS) == set(Mode)
    for mode, ranges in expected_ranges.items():
        spec = DESCRIPTORS[mode]
        assert spec.ranges_si == ranges
        assert spec.nplc_values == (NPLC_VALUES if mode in nplc_modes else ())
        assert spec.ac_bandwidths_hz == (AC_BANDWIDTHS_HZ if mode in ac_bandwidth_modes else ())
        assert spec.analog_filter is (mode in analog_filter_modes)

    # These ranges belong to 8846A-only capabilities and must never reach the 8845A GUI.
    assert 10 not in DESCRIPTORS[Mode.RES2].ranges_si
    assert 10 not in DESCRIPTORS[Mode.RES4].ranges_si
    assert 100e-6 not in DESCRIPTORS[Mode.ACI].ranges_si
    assert 1e-3 not in DESCRIPTORS[Mode.ACI].ranges_si
    assert DESCRIPTORS[Mode.ACV].ranges_si[-1] == 750

