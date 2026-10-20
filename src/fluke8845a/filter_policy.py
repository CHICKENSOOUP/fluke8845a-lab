"""Pure 8845A digital-filter capability, validation, and firmware policy."""

from __future__ import annotations

from .modes import Mode, descriptor
from .versioning import compare_firmware


DIGITAL_FILTER_DISABLED_REASON = (
    "软件不开放数字滤波开启：官方手册未给出最大READ?响应时间。"
    "官方手册说明 OutG SW 早于 2.0 不支持该功能，软件会跳过数字滤波命令和回读；"
    "版本为 2.0 或以上、或版本未知时，"
    "软件会在应用 NPLC 前关闭滤波并回读确认，无法确认关闭时会拒绝应用 NPLC。"
)


def digital_filter_enable_error(mode: Mode | str) -> str:
    """Return the single policy reason when a user requests digital filtering.

    For OutG SW 2.0 or later, and for unknown revisions, the driver attempts to
    disable and verify the filter before applying NPLC. For documented older
    firmware it skips the unsupported filter commands. This helper only
    centralizes request validation; it does not define SCPI behavior.
    """
    return digital_filter_setting_error(mode, True) or DIGITAL_FILTER_DISABLED_REASON


def digital_filter_setting_error(
    mode: Mode | str,
    enabled: bool,
    *,
    allow_device_readback: bool = False,
) -> str | None:
    """Validate a filter setting through the same policy at every app boundary."""
    if not enabled:
        return None
    spec = descriptor(mode)
    if not spec.digital_filter:
        return f"{spec.label}不支持直流数字滤波"
    if allow_device_readback:
        return None
    return DIGITAL_FILTER_DISABLED_REASON


def digital_filter_firmware_support(mode: Mode | str, firmware: str) -> bool | None:
    """Return support using the 8845A manual's OutG SW 2.0 cutoff.

    Users Manual Rev. 3 (PDF p. 47 / printed p. 3-3) says digital filtering is
    unavailable before OutGuard SW 2.0. Unknown revisions remain unknown;
    callers must not infer support from an IDN date/revision string.
    """
    if not descriptor(mode).digital_filter:
        return False
    return compare_firmware(firmware, (2, 0))


def digital_filter_nplc_is_slow(mode: Mode | str, nplc: float | None) -> bool:
    """Whether this measurement mode/NPLC may activate the digital filter."""
    return descriptor(mode).digital_filter and nplc is not None and nplc > 1
