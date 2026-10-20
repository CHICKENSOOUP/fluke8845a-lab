"""Create a privacy-reduced ZIP copy of a single experiment directory.

CSV files are streamed row-by-row into derived privacy-reduced, spreadsheet-safe
copies; valid numeric cells remain unchanged and nonnumeric content in numeric columns
is redacted. config.json is redacted recursively.
The archive never includes arbitrary files from the folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import csv
import io
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .path_security import is_symlink_or_junction as _is_symlink_or_junction
from .spreadsheet_safety import is_numeric_literal, spreadsheet_safe_cell


_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_CSV_BOM = b"\xef\xbb\xbf"
_REDACTED = "[已脱敏]"
_REDACTED_PATH = "[本地路径已隐藏]"

_SERIAL_KEYS = {
    "serial", "serialno", "serialnumber", "deviceserial", "deviceserialno",
    "deviceserialnumber", "instrumentserial", "instrumentserialno",
    "instrumentserialnumber", "serialid", "idnraw", "idnstring", "rawidn",
}
_PORT_KEYS = {
    "port", "comport", "comportname", "serialport", "deviceport",
    "activeport", "selectedport", "connectionport",
}
_COM_PORT_RE = re.compile(r"\bCOM\s*\d+\b", re.IGNORECASE)
_SERIAL_DEVICE_RE = re.compile(r"(?i)^/?dev/(?:tty(?:USB|ACM|S|AMA)\w*|serial/[^\s]+)$")
_RAW_DATA_COLUMNS = {
    "sampleindex", "eventindex", "elapsed", "elapsedseconds", "elapsedtime",
    "elapseds", "readduration", "readdurations", "actualinterval", "actualintervals",
    "valuesi", "rangesi", "nplc", "voltagev", "currenta", "resistanceohm",
}
_ABSOLUTE_PATH_RE = re.compile(
    r"(?ix)"
    r"(?<![A-Z0-9])"
    r"(?:"
    r"[A-Z]:[\\/]"  # Windows drive path
    r"|\\\\[^\\/\s,;\"'<>|?*]+[\\/][^\\/\s,;\"'<>|?*]+"  # UNC path
    r"|/(?:Users|home|mnt|tmp|var|private|Volumes|root|workspace|data|opt|media|srv)(?:/|$)"
    r"|/(?:[^/\s,;\"'<>|?*]+/){2,}"  # Other multi-component POSIX absolute paths
    r")"
    r"[^\r\n\"'<>|?*]*"
)


def _normalized_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_serial_key(key: Any) -> bool:
    return _normalized_key(key) in _SERIAL_KEYS


def _is_port_key(key: Any) -> bool:
    return _normalized_key(key) in _PORT_KEYS


def _collect_serial_values(value: Any) -> set[str]:
    serials: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_serial_key(key) and _normalized_key(key) not in {"idnraw", "idnstring", "rawidn"}:
                if isinstance(item, (str, int)) and not isinstance(item, bool):
                    token = str(item).strip()
                    if token and token.casefold() not in {"none", "unknown", _REDACTED.casefold()}:
                        serials.add(token)
            serials.update(_collect_serial_values(item))
    elif isinstance(value, list):
        for item in value:
            serials.update(_collect_serial_values(item))
    return serials


def _redact_text(value: str, serial_values: set[str]) -> str:
    result = value
    for serial in sorted(serial_values, key=len, reverse=True):
        if len(serial) >= 4:
            result = re.sub(re.escape(serial), _REDACTED, result, flags=re.IGNORECASE)
    result = _COM_PORT_RE.sub(_REDACTED, result)
    result = _ABSOLUTE_PATH_RE.sub(_REDACTED_PATH, result)
    return result


def _spreadsheet_safe_cell(value: str) -> str:
    """Prefix formula-like text in a derived CSV while preserving numeric literals."""
    return spreadsheet_safe_cell(value, preserve_numeric_literal=True)


def sanitize_config(config: Any) -> dict[str, Any]:
    """Return a recursive config copy with device, port and local-path identity hidden."""
    if not isinstance(config, dict):
        raise ValueError("config.json 顶层必须是 JSON 对象")
    serial_values = _collect_serial_values(config)

    def sanitize(value: Any, key: Any = None) -> Any:
        if key is not None and _is_serial_key(key):
            return _REDACTED
        if key is not None and _is_port_key(key):
            if isinstance(value, str) and value.strip():
                return _REDACTED
            return value
        if isinstance(value, dict):
            return {str(child_key): sanitize(child_value, child_key) for child_key, child_value in value.items()}
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        if isinstance(value, str):
            return _redact_text(value, serial_values)
        return value

    return sanitize(config)


def _directory_identity(details: os.stat_result) -> tuple[int, int]:
    return int(details.st_dev), int(details.st_ino)


@dataclass(frozen=True)
class _DirectoryIdentity:
    stat_identity: tuple[int, int]
    windows_file_id: tuple[int, bytes] | None = None


@dataclass(frozen=True)
class _PinnedDirectory:
    path: Path
    identity: _DirectoryIdentity | tuple[int, int]
    windows_handle: int | None = None
    directory_fd: int | None = None


def _windows_file_id(handle: int) -> tuple[int, bytes]:
    """Read a stable 128-bit Windows file identity directly from an open handle."""
    import ctypes
    from ctypes import wintypes

    class FileIdInfo(ctypes.Structure):
        _fields_ = [("volume_serial_number", ctypes.c_ulonglong), ("file_id", ctypes.c_ubyte * 16)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    details = FileIdInfo()
    if not get_info(
        wintypes.HANDLE(handle),
        18,  # FileIdInfo
        ctypes.byref(details),
        ctypes.sizeof(details),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(details.volume_serial_number), bytes(details.file_id)


def _windows_handle_attributes(handle: int) -> tuple[int, int]:
    """Return Windows file attributes and reparse tag for an open handle."""
    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("file_attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    details = FileAttributeTagInfo()
    if not get_info(
        wintypes.HANDLE(handle),
        9,  # FileAttributeTagInfo
        ctypes.byref(details),
        ctypes.sizeof(details),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(details.file_attributes), int(details.reparse_tag)


@contextmanager
def _windows_directory_handle(root: Path):
    """Open and pin a Windows directory object, without following a final reparse point."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(root),
        0x00000001 | 0x00000020 | 0x00000080 | 0x00100000,  # list, traverse, attrs, sync
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    fd = None
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        handle = None  # open_osfhandle now owns it
        native_handle = msvcrt.get_osfhandle(fd)
        opened = os.fstat(fd)
        attributes, _tag = _windows_handle_attributes(native_handle)
        if not (attributes & 0x10) or (attributes & 0x400):  # directory; not a reparse point
            raise ValueError("实验目录句柄不是普通目录或是 Windows reparse point")
        yield native_handle, opened, _windows_file_id(native_handle)
    finally:
        if fd is not None:
            os.close(fd)
        elif handle is not None:
            close_handle(handle)


def _capture_directory_identity(root: Path, details: os.stat_result) -> _DirectoryIdentity:
    if os.name == "nt":
        with _windows_directory_handle(root) as (_handle, opened, file_id):
            identity = _directory_identity(opened)
            if identity != _directory_identity(details):
                raise ValueError("实验目录在身份确认期间发生变化，拒绝继续")
            return _DirectoryIdentity(identity, file_id)
    return _DirectoryIdentity(_directory_identity(details))


def _identity_stat_part(identity: _DirectoryIdentity | tuple[int, int]) -> tuple[int, int]:
    return identity.stat_identity if isinstance(identity, _DirectoryIdentity) else identity


def _reject_hard_linked_input(details: os.stat_result, name: str) -> None:
    if int(getattr(details, "st_nlink", 1)) > 1:
        raise ValueError(f"实验文件存在多个硬链接，无法确认内容来源：{name}")


def _verify_directory_identity(
    root: Path, identity: _DirectoryIdentity | tuple[int, int]
) -> None:
    try:
        if _is_symlink_or_junction(root):
            raise ValueError("实验目录在导出期间发生变化，拒绝继续")
        confirmed = root.lstat()
    except OSError as exc:
        raise ValueError("实验目录在导出期间不可用，拒绝继续") from exc
    if not stat.S_ISDIR(confirmed.st_mode) or _directory_identity(confirmed) != _identity_stat_part(identity):
        raise ValueError("实验目录在导出期间发生变化，拒绝继续")
    if os.name == "nt" and isinstance(identity, _DirectoryIdentity):
        try:
            with _windows_directory_handle(root) as (_handle, opened, file_id):
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or _directory_identity(opened) != identity.stat_identity
                    or file_id != identity.windows_file_id
                ):
                    raise ValueError("实验目录在导出期间发生变化，拒绝继续")
        except OSError as exc:
            raise ValueError("实验目录句柄在导出期间不可用，拒绝继续") from exc


def _resolved_directory(path: str | os.PathLike[str]) -> tuple[Path, _DirectoryIdentity]:
    source = Path(path).expanduser()
    if os.name == "nt":
        if _is_symlink_or_junction(source):
            raise ValueError("实验目录不能是符号链接或 Windows reparse point")
        try:
            with _windows_directory_handle(source) as (_handle, source_stat, source_file_id):
                if not stat.S_ISDIR(source_stat.st_mode):
                    raise NotADirectoryError(f"实验路径不是目录：{source}")
                try:
                    source_path_stat = source.lstat()
                except OSError as exc:
                    raise ValueError("实验目录在身份确认期间发生变化，拒绝继续") from exc
                if _directory_identity(source_path_stat) != _directory_identity(source_stat):
                    raise ValueError("实验目录在身份确认期间发生变化，拒绝继续")
                try:
                    resolved = source.resolve(strict=True)
                    resolved_stat = resolved.lstat()
                except (OSError, RuntimeError) as exc:
                    raise ValueError(f"实验目录无法安全解析：{source}") from exc
                if (
                    _is_symlink_or_junction(resolved)
                    or not stat.S_ISDIR(resolved_stat.st_mode)
                    or _directory_identity(resolved_stat) != _directory_identity(source_stat)
                ):
                    raise ValueError("实验目录在解析期间发生变化，拒绝继续")
                identity = _capture_directory_identity(resolved, resolved_stat)
                if identity.windows_file_id != source_file_id:
                    raise ValueError("实验目录在解析期间发生变化，拒绝继续")
                return resolved, identity
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"实验目录不可用：{source}") from exc
    try:
        original_stat = source.lstat()
    except OSError as exc:
        raise FileNotFoundError(f"实验目录不可用：{source}") from exc
    if _is_symlink_or_junction(source):
        raise ValueError("实验目录不能是符号链接或 Windows reparse point")
    try:
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"实验目录无法安全解析：{source}") from exc
    try:
        resolved_stat = resolved.lstat()
    except OSError as exc:
        raise ValueError("实验目录在解析期间发生变化，拒绝继续") from exc
    if _is_symlink_or_junction(resolved):
        raise ValueError("实验目录在解析期间发生变化，拒绝继续")
    if not stat.S_ISDIR(resolved_stat.st_mode):
        raise NotADirectoryError(f"实验路径不是目录：{resolved}")
    if _directory_identity(resolved_stat) != _directory_identity(original_stat):
        raise ValueError("实验目录在解析期间发生变化，拒绝继续")
    identity = _capture_directory_identity(resolved, resolved_stat)
    return resolved, identity


@contextmanager
def _pin_resolved_directory(path: str | os.PathLike[str]):
    """Keep the selected directory object open for the complete export."""
    source = Path(path).expanduser()
    if os.name == "nt":
        if _is_symlink_or_junction(source):
            raise ValueError("实验目录不能是符号链接或 Windows reparse point")
        try:
            with _windows_directory_handle(source) as (root_handle, opened, file_id):
                if not stat.S_ISDIR(opened.st_mode):
                    raise NotADirectoryError(f"实验路径不是目录：{source}")
                try:
                    source_stat = source.lstat()
                    resolved = source.resolve(strict=True)
                    resolved_stat = resolved.lstat()
                except (OSError, RuntimeError) as exc:
                    raise ValueError("实验目录在确认身份时发生变化，拒绝继续") from exc
                expected_stat = _directory_identity(opened)
                if (
                    _is_symlink_or_junction(resolved)
                    or not stat.S_ISDIR(source_stat.st_mode)
                    or not stat.S_ISDIR(resolved_stat.st_mode)
                    or _directory_identity(source_stat) != expected_stat
                    or _directory_identity(resolved_stat) != expected_stat
                ):
                    raise ValueError("实验目录在确认身份时发生变化，拒绝继续")
                pinned = _PinnedDirectory(
                    resolved,
                    _DirectoryIdentity(expected_stat, file_id),
                    windows_handle=root_handle,
                )
                yield pinned
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"实验目录不可用：{source}") from exc
        return

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ValueError("当前平台不支持安全固定实验目录")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
    try:
        directory_fd = os.open(source, flags)
    except OSError as exc:
        raise FileNotFoundError(f"实验目录不可用：{source}") from exc
    try:
        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise NotADirectoryError(f"实验路径不是目录：{source}")
        try:
            source_stat = source.lstat()
            resolved = source.resolve(strict=True)
            resolved_stat = resolved.lstat()
        except (OSError, RuntimeError) as exc:
            raise ValueError("实验目录在确认身份时发生变化，拒绝继续") from exc
        identity = _directory_identity(opened)
        if (
            _is_symlink_or_junction(source)
            or _is_symlink_or_junction(resolved)
            or not stat.S_ISDIR(source_stat.st_mode)
            or not stat.S_ISDIR(resolved_stat.st_mode)
            or _directory_identity(source_stat) != identity
            or _directory_identity(resolved_stat) != identity
        ):
            raise ValueError("实验目录在确认身份时发生变化，拒绝继续")
        yield _PinnedDirectory(resolved, _DirectoryIdentity(identity), directory_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _input_path(
    root: Path,
    name: str,
    *,
    required: bool,
    root_identity: _DirectoryIdentity | tuple[int, int],
) -> Path | None:
    _verify_directory_identity(root, root_identity)
    path = root / name
    try:
        details = path.lstat()
    except FileNotFoundError:
        if required:
            raise FileNotFoundError(f"实验目录缺少必需文件：{name}")
        return None
    if _is_symlink_or_junction(path):
        raise ValueError(f"实验文件不能是符号链接或 Windows reparse point：{name}")
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"实验文件必须是普通文件：{name}")
    _reject_hard_linked_input(details, name)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"实验文件无法安全解析：{name}") from exc
    if resolved.parent != root or resolved.name != name:
        raise ValueError(f"实验文件不能越出所选目录：{name}")
    _verify_directory_identity(root, root_identity)
    return path


def _open_windows_relative_file(root_handle: int, name: str):
    """Open one allowlisted filename relative to a pinned Windows directory handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    if name not in {"measurements.csv", "events.csv", "config.json"}:
        raise ValueError("实验文件名必须是单个目录项")

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum_length", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root_directory", wintypes.HANDLE),
            ("object_name", ctypes.POINTER(UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security_descriptor", wintypes.LPVOID),
            ("security_quality_of_service", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("status_or_pointer", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    text_buffer = ctypes.create_unicode_buffer(name)
    text_bytes = len(name.encode("utf-16-le"))
    unicode_name = UnicodeString(text_bytes, text_bytes + 2, ctypes.cast(text_buffer, wintypes.LPWSTR))
    object_attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        wintypes.HANDLE(root_handle),
        ctypes.pointer(unicode_name),
        0x40,  # OBJ_CASE_INSENSITIVE
        None,
        None,
    )
    io_status = IoStatusBlock()
    file_handle = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll")
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    nt_create_file.restype = ctypes.c_long
    status = nt_create_file(
        ctypes.byref(file_handle),
        0x00000001 | 0x00000080 | 0x00100000,  # read data, read attrs, synchronize
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        1,  # FILE_OPEN; never create or overwrite
        0x00000040 | 0x00200000 | 0x00000020,  # NON_DIRECTORY | OPEN_REPARSE | SYNCHRONOUS
        None,
        0,
    )
    if status < 0:
        ntstatus_to_dos_error = ntdll.RtlNtStatusToDosError
        ntstatus_to_dos_error.argtypes = [ctypes.c_long]
        ntstatus_to_dos_error.restype = wintypes.ULONG
        error_code = int(ntstatus_to_dos_error(status))
        raise OSError(error_code, f"无法相对实验目录打开文件：{name}")

    fd = None
    stream = None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    try:
        fd = msvcrt.open_osfhandle(int(file_handle.value), os.O_RDONLY | os.O_BINARY)
        file_handle = wintypes.HANDLE()
        stream = os.fdopen(fd, "rb")
        fd = None
        native_handle = msvcrt.get_osfhandle(stream.fileno())
        attributes, _tag = _windows_handle_attributes(native_handle)
        details = os.fstat(stream.fileno())
        if (attributes & 0x400) or (attributes & 0x10) or not stat.S_ISREG(details.st_mode):
            raise ValueError(f"输入是 Windows reparse point、目录或非普通文件：{name}")
        return stream
    except BaseException:
        if stream is not None:
            try:
                stream.close()
            except BaseException:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        elif file_handle.value:
            close_handle(file_handle)


def _open_verified(
    path: Path,
    *,
    root: Path | None = None,
    root_identity: _DirectoryIdentity | tuple[int, int] | None = None,
    pinned_directory: _PinnedDirectory | None = None,
):
    if pinned_directory is not None and (
        root != pinned_directory.path or root_identity != pinned_directory.identity
    ):
        raise ValueError("固定实验目录与输入路径身份不一致")
    if root is not None and root_identity is not None:
        _verify_directory_identity(root, root_identity)
        if path.parent != root:
            raise ValueError("输入文件不在已验证的实验目录中")
    before = path.lstat()
    if _is_symlink_or_junction(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"输入已变为符号链接、Windows reparse point 或非普通文件：{path.name}")
    _reject_hard_linked_input(before, path.name)
    if pinned_directory is not None and os.name == "nt":
        if pinned_directory.windows_handle is None:
            raise ValueError("固定实验目录句柄不可用")
        stream = _open_windows_relative_file(pinned_directory.windows_handle, path.name)
    elif pinned_directory is not None and os.name != "nt":
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None or pinned_directory.directory_fd is None:
            raise ValueError("当前平台不支持安全的固定目录文件打开")
        file_fd = os.open(path.name, os.O_RDONLY | no_follow, dir_fd=pinned_directory.directory_fd)
        stream = os.fdopen(file_fd, "rb")
    elif root is not None and root_identity is not None and os.name == "nt":
        with _windows_directory_handle(root) as (root_handle, opened_root, file_id):
            if (
                _directory_identity(opened_root) != _identity_stat_part(root_identity)
                or (
                    isinstance(root_identity, _DirectoryIdentity)
                    and file_id != root_identity.windows_file_id
                )
            ):
                raise ValueError("实验目录在打开输入文件前发生变化，拒绝继续")
            stream = _open_windows_relative_file(root_handle, path.name)
    elif root is not None and root_identity is not None and os.name != "nt":
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise ValueError("当前平台不支持安全的相对目录文件打开")
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            opened_root = os.fstat(directory_fd)
            if _directory_identity(opened_root) != _identity_stat_part(root_identity):
                raise ValueError("实验目录在打开输入文件前发生变化，拒绝继续")
            file_fd = os.open(path.name, os.O_RDONLY | no_follow, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        stream = os.fdopen(file_fd, "rb")
    else:
        stream = path.open("rb")
    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _directory_identity(opened) != _directory_identity(before):
            raise ValueError(f"输入文件在打开时发生变化：{path.name}")
        _reject_hard_linked_input(opened, path.name)
        if root is not None and root_identity is not None:
            _verify_directory_identity(root, root_identity)
    except BaseException:
        stream.close()
        raise
    return stream, opened


def _read_config(
    path: Path, *, root: Path, root_identity: _DirectoryIdentity | tuple[int, int],
    pinned_directory: _PinnedDirectory | None = None,
) -> tuple[bytes, set[str]]:
    stream, opened = _open_verified(
        path, root=root, root_identity=root_identity, pinned_directory=pinned_directory
    )
    try:
        if opened.st_size > _MAX_CONFIG_BYTES:
            raise ValueError("config.json 超过 16 MiB，拒绝在内存中解析")
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError("config.json 超过 16 MiB，拒绝在内存中解析")
        if len(raw) != opened.st_size or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise RuntimeError("config.json 在导出期间发生变化，请停止采集后重试")
        _reject_hard_linked_input(after, "config.json")
        _verify_directory_identity(root, root_identity)
    finally:
        stream.close()
    try:
        config = json.loads(raw.decode("utf-8-sig"))
        serial_values = _collect_serial_values(config)
        sanitized = sanitize_config(config)
        payload = (json.dumps(sanitized, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        return payload, serial_values
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"config.json 无法安全解析或脱敏：{exc}") from exc


def _write_bytes_member(archive: ZipFile, name: str, payload: bytes) -> None:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 0
    with archive.open(info, "w", force_zip64=True) as member:
        member.write(payload)


def _csv_line_terminator(stream) -> str:
    """Keep the source's first physical newline style without loading the file."""
    prefix = stream.read(64 * 1024)
    candidates = [(prefix.find(token), token) for token in (b"\r\n", b"\n", b"\r")]
    candidates = [(index, token) for index, token in candidates if index >= 0]
    stream.seek(0)
    if not candidates:
        return "\r\n"
    _, newline = min(candidates, key=lambda item: item[0])
    return newline.decode("ascii")


def _copy_csv_member(
    archive: ZipFile,
    root: Path,
    name: str,
    serial_values: set[str],
    root_identity: _DirectoryIdentity | tuple[int, int],
    pinned_directory: _PinnedDirectory | None = None,
) -> None:
    path = _input_path(root, name, required=True, root_identity=root_identity)
    assert path is not None
    source, before = _open_verified(
        path, root=root, root_identity=root_identity, pinned_directory=pinned_directory
    )
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 0
    text_source = None
    try:
        line_terminator = _csv_line_terminator(source)
        text_source = io.TextIOWrapper(source, encoding="utf-8-sig", newline="", errors="strict")
        with archive.open(info, "w", force_zip64=True) as member:
            member.write(_CSV_BOM)
            text_target = io.TextIOWrapper(member, encoding="utf-8", newline="")
            try:
                reader = csv.reader(text_source, strict=True)
                writer = csv.writer(text_target, lineterminator=line_terminator)
                try:
                    header = next(reader)
                except StopIteration as exc:
                    raise ValueError(f"{name} 为空，无法安全脱敏") from exc
                if not header or len(header) > 1024:
                    raise ValueError(f"{name} 表头无效或列数超过限制")
                sanitized_header = [
                    _spreadsheet_safe_cell(_redact_text(cell, serial_values))
                    for cell in header
                ]
                writer.writerow(sanitized_header)
                for row in reader:
                    if len(row) != len(header):
                        raise ValueError(f"{name} 存在列数与表头不一致的记录")
                    sanitized_row = []
                    for column, cell in zip(header, row):
                        normalized = _normalized_key(column)
                        if normalized in _RAW_DATA_COLUMNS:
                            stripped = cell.strip()
                            serial_collision = any(
                                len(serial) >= 4 and serial.casefold() == stripped.casefold()
                                for serial in serial_values
                            )
                            if not stripped or (
                                    is_numeric_literal(stripped) and not serial_collision
                            ):
                                sanitized_cell = cell
                            else:
                                sanitized_cell = _REDACTED
                        else:
                            sanitized_cell = _redact_text(cell, serial_values)
                        sanitized_row.append(_spreadsheet_safe_cell(sanitized_cell))
                    writer.writerow(sanitized_row)
                text_target.flush()
            finally:
                text_target.detach()
        text_source.detach()
        after = os.fstat(source.fileno())
        if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
            raise RuntimeError(f"{name} 在导出期间发生变化，请停止采集后重试")
        _reject_hard_linked_input(after, name)
        _verify_directory_identity(root, root_identity)
    finally:
        try:
            if text_source is not None:
                try:
                    text_source.detach()
                except ValueError:
                    pass
        finally:
            source.close()


def _destination_path(path: str | os.PathLike[str], root: Path) -> Path:
    requested = Path(path).expanduser()
    if not requested.name:
        raise ValueError("请指定 ZIP 文件名")
    try:
        parent = requested.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileNotFoundError(f"导出目录必须已存在：{requested.parent}") from exc
    if not parent.is_dir():
        raise NotADirectoryError(f"导出路径的父目录不是文件夹：{parent}")
    destination = parent / requested.name
    normalized_destination = os.path.normcase(os.path.abspath(destination))
    for name in ("measurements.csv", "events.csv", "config.json"):
        source = root / name
        if os.path.lexists(source) and normalized_destination == os.path.normcase(os.path.abspath(source)):
            raise ValueError(f"导出文件不能覆盖实验原始文件：{name}")
    if os.path.lexists(destination):
        raise FileExistsError(f"目标文件已存在，为保护现有数据拒绝覆盖：{destination}")
    if destination.suffix.casefold() != ".zip":
        raise ValueError("分享包文件名必须以 .zip 结尾")
    return destination


def _publish_no_clobber(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise FileExistsError(f"目标文件已存在，为保护现有数据拒绝覆盖：{destination}")
    except (NotImplementedError, OSError):
        # Some filesystems do not support hard links. Reserve the target with
        # O_EXCL so a concurrent export cannot silently replace an existing file.
        # Copy into that exclusively-created file instead of os.replace(), which
        # could overwrite a path swapped in after the reservation.
        try:
            descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise FileExistsError(f"目标文件已存在，为保护现有数据拒绝覆盖：{destination}")
        created = os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as target, temporary.open("rb") as source:
                shutil.copyfileobj(source, target, _COPY_CHUNK_BYTES)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            try:
                current = destination.lstat()
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    destination.unlink()
            except FileNotFoundError:
                pass
            raise
    else:
        temporary.unlink()


def export_share_package(
    experiment_directory: str | os.PathLike[str],
    output_zip: str | os.PathLike[str],
) -> Path:
    """Write a privacy-reduced ZIP copy without changing experiment source files.

    The archive contains only sanitized ``config.json`` and derived, privacy-reduced,
    spreadsheet-safe copies of ``measurements.csv`` plus optional ``events.csv``.
    Valid numeric cells remain unchanged; nonnumeric content in numeric columns,
    identifying text and formula-like text are redacted/escaped in the copies.
    ``run.log`` and unknown files are excluded.
    Existing destinations are never overwritten.
    """
    with _pin_resolved_directory(experiment_directory) as pinned:
        return _export_share_package_from_pinned_directory(pinned, output_zip)


def _export_share_package_from_pinned_directory(
    pinned: _PinnedDirectory,
    output_zip: str | os.PathLike[str],
) -> Path:
    root = pinned.path
    root_identity = pinned.identity
    measurements = _input_path(root, "measurements.csv", required=True, root_identity=root_identity)
    config = _input_path(root, "config.json", required=True, root_identity=root_identity)
    events = _input_path(root, "events.csv", required=False, root_identity=root_identity)
    assert measurements is not None and config is not None
    destination = _destination_path(output_zip, root)

    config_payload, serial_values = _read_config(
        config, root=root, root_identity=root_identity, pinned_directory=pinned
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=".fluke-share-", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_STORED, allowZip64=True) as archive:
            _write_bytes_member(archive, "config.json", config_payload)
            _copy_csv_member(
                archive, root, "measurements.csv", serial_values, root_identity,
                pinned_directory=pinned,
            )
            if events is not None:
                _copy_csv_member(
                    archive, root, "events.csv", serial_values, root_identity,
                    pinned_directory=pinned,
                )
        _verify_directory_identity(root, root_identity)
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        _publish_no_clobber(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
