import pytest

from fluke8845a import modes, scpi
from fluke8845a.filter_policy import (
    digital_filter_firmware_support,
    digital_filter_nplc_is_slow,
    digital_filter_setting_error,
)
from fluke8845a.versioning import compare_firmware


@pytest.mark.parametrize("mode", tuple(modes.Mode))
def test_digital_filter_enable_rejection_uses_one_mode_aware_policy(mode):
    policy = getattr(scpi, "digital_filter_enable_error", None)
    assert callable(policy), "数字滤波开启策略应由核心模块统一提供"

    reason = policy(mode)

    if modes.descriptor(mode).digital_filter:
        assert reason == scpi.DIGITAL_FILTER_DISABLED_REASON
    else:
        assert reason == f"{modes.descriptor(mode).label}不支持直流数字滤波"


def test_digital_filter_policy_explains_legacy_support_and_readback_gate():
    reason = scpi.DIGITAL_FILTER_DISABLED_REASON

    assert "OutG SW 早于 2.0 不支持该功能" in reason
    assert "跳过数字滤波命令和回读" in reason
    assert "版本未知" in reason
    assert "无法确认关闭时会拒绝应用 NPLC" in reason


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
    assert digital_filter_setting_error(modes.Mode.DCV, True) == scpi.DIGITAL_FILTER_DISABLED_REASON
    assert digital_filter_setting_error(
        modes.Mode.DCV, True, allow_device_readback=True
    ) is None
    assert digital_filter_setting_error(
        modes.Mode.ACV, True, allow_device_readback=True
    ) == f"{modes.descriptor(modes.Mode.ACV).label}不支持直流数字滤波"
    assert digital_filter_setting_error(modes.Mode.DCV, False) is None


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
