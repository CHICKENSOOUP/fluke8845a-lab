import pytest

from fluke8845a import modes, scpi
from fluke8845a.filter_policy import (
    DIGITAL_FILTER_INFO,
    digital_filter_firmware_support,
    digital_filter_nplc_is_slow,
    digital_filter_enable_error,
    digital_filter_setting_error,
)
from fluke8845a.versioning import compare_firmware


@pytest.mark.parametrize("mode", tuple(modes.Mode))
def test_digital_filter_policy_allows_slow_dc_modes_only(mode):
    reason = digital_filter_enable_error(mode, nplc=10)

    if modes.descriptor(mode).digital_filter:
        assert reason is None
    else:
        assert reason == f"{modes.descriptor(mode).label}不支持直流数字滤波"


def test_digital_filter_information_explains_firmware_and_variable_response_time():
    assert "OutG SW 早于 2.0 不支持该功能" in DIGITAL_FILTER_INFO
    assert "平均数量随功能和量程变化" in DIGITAL_FILTER_INFO
    assert "以实际采样间隔为准" in DIGITAL_FILTER_INFO


@pytest.mark.parametrize(
    ("mode", "firmware", "expected"),
    [
        (modes.Mode.DCV, "", None),
        (modes.Mode.DCI, "1.9.0.0", False),
        (modes.Mode.RES2, "2.10.882.24", True),
        (modes.Mode.ACV, "2.10.882.24", False),
    ],
)
def test_filter_policy_owns_documented_firmware_support(mode, firmware, expected):
    assert digital_filter_firmware_support(mode, firmware) is expected


def test_filter_policy_validates_requested_and_readback_filter_states():
    assert digital_filter_setting_error(modes.Mode.DCV, True, nplc=10) is None
    assert "NPLC" in digital_filter_setting_error(modes.Mode.DCV, True, nplc=1)
    assert "NPLC" in digital_filter_setting_error(modes.Mode.DCV, True)
    assert digital_filter_setting_error(
        modes.Mode.DCV, True, nplc=1, allow_device_readback=True
    ) is None
    assert digital_filter_setting_error(
        modes.Mode.ACV, True, nplc=10, allow_device_readback=True
    ) == f"{modes.descriptor(modes.Mode.ACV).label}不支持直流数字滤波"
    assert digital_filter_setting_error(modes.Mode.DCV, False, nplc=1) is None


def test_mode_settings_rejects_non_boolean_digital_filter_values():
    with pytest.raises(ValueError, match="数字滤波设置必须是布尔值"):
        scpi.ModeSettings(modes.Mode.DCV, nplc=10, digital_filter="ON").validate()


def test_firmware_version_comparison_lives_in_neutral_versioning_module():
    assert compare_firmware("2.0", (2, 0)) is True
    assert compare_firmware("2.10.882.24", (2, 0)) is True
    assert compare_firmware("1.9.0.0", (2, 0)) is False
    assert compare_firmware("02/14/08-08:54", (2, 0)) is None


@pytest.mark.parametrize(
    "firmware",
    [
        "OutG SW 2.10.882.24",
        "2.10.882.24 (panel checked)",
        "release 1.9.0.0, current 2.10.882.24",
        "1.2.3",
        "٢.١٠.٨٨٢.٢٤",
    ],
)
def test_firmware_comparison_rejects_embedded_or_ambiguous_versions(firmware):
    assert compare_firmware(firmware, (2, 0)) is None


def test_filter_nplc_slow_classification_is_shared_by_worker_and_frontends():
    assert digital_filter_nplc_is_slow(modes.Mode.DCV, 10)
    assert not digital_filter_nplc_is_slow(modes.Mode.ACV, 10)
    assert not digital_filter_nplc_is_slow(modes.Mode.DCV, 1)
