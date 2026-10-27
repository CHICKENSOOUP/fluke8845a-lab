"""Pure 8845A digital-filter capability, validation, and firmware policy."""

from __future__ import annotations

from .modes import Mode, descriptor
from .versioning import compare_firmware


DIGITAL_FILTER_INFO = (
    "8845A数字滤波适用于DCV、DCI、2W和4W；仅在10或100 NPLC（慢于1 PLC）时有效。"
    "OutG SW 早于 2.0 不支持该功能。手册说明平均数量随功能和量程变化，未给出最大READ?响应时间；"
    "以实际采样间隔为准；READ?超时是本软件的有限等待保护值，不代表仪表时限保证。"
    "超时会记录通信失败并结束采集，不会生成读数。"
)
DIGITAL_FILTER_NPLC_ERROR = "直流数字滤波仅适用于10或100 NPLC（慢于1 PLC）"


def digital_filter_enable_error(mode: Mode | str, *, nplc: float | None = None) -> str | None:
    """Return a validation error only when this mode/rate cannot use the filter."""
    return digital_filter_setting_error(mode, True, nplc=nplc)


def digital_filter_setting_error(
    mode: Mode | str,
    enabled: bool,
    *,
    nplc: float | None = None,
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
    if nplc is None or nplc <= 1:
        return DIGITAL_FILTER_NPLC_ERROR
    return None


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
