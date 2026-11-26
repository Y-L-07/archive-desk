"""Windows directory identities and disk placement used by repository operations."""
import ctypes
import re
import struct
from contextlib import ExitStack
from ctypes import wintypes
from pathlib import Path, PureWindowsPath

from ._winmove import (INVALID_HANDLE, OPEN_EXISTING, READ_ATTRIBUTES, close_handle,
                       create_file, kernel, open_entry, pin_directory)


def pin_path(stack, path, *, create=False):
    """Hold every ancestor against replacement until the caller closes its stack."""
    path = Path(path)
    if (not path.is_absolute() or ".." in path.parts or
            str(path).startswith(("\\\\?\\", "\\\\.\\", "\\??\\"))):
        raise ValueError("请选择普通绝对目录，路径不能含上级跳转或设备命名空间")
    for folder in (*reversed(path.parents), path):
        try:
            identity = stack.enter_context(pin_directory(folder))
        except FileNotFoundError:
            if not create:
                raise
            folder.mkdir(exist_ok=True)
            identity = stack.enter_context(pin_directory(folder))
    return identity


def _final_path(path, volume_format):
    handle = open_entry(path, READ_ATTRIBUTES)
    try:
        fn = kernel.GetFinalPathNameByHandleW
        fn.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        fn.restype = wintypes.DWORD
        n = fn(handle, None, 0, volume_format)
        if not n:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(n + 1)
        used = fn(handle, buffer, len(buffer), volume_format)
        if not 0 < used < len(buffer):
            raise OSError("无法确认目录最终位置")
        return buffer.value
    finally:
        close_handle(handle)


def assert_separate(a, b):
    with ExitStack() as stack:
        left_id = pin_path(stack, a)
        right_id = pin_path(stack, b)
        left = PureWindowsPath(_final_path(Path(a), 2))
        right = PureWindowsPath(_final_path(Path(b), 2))
        if left_id == right_id or left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError("两个目录相同或相互嵌套，请选择独立目录")


def _disk_numbers(path):
    # VOLUME_NAME_GUID resolves drive aliases. Query Windows' physical extents
    # instead of treating different drive letters as independent devices.
    location = _final_path(Path(path), 1)
    match = re.match(r"^(\\\\\?\\Volume\{[0-9a-f-]+\})\\", location, re.I)
    if not match:
        raise OSError("无法确定卷标识")
    handle = create_file(match.group(1), 0, 7, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = ctypes.create_string_buffer(65536)
        returned = wintypes.DWORD()
        ioctl = kernel.DeviceIoControl
        ioctl.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                          wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
        ioctl.restype = wintypes.BOOL
        if not ioctl(handle, 0x00560000, None, 0, result, len(result), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        count, = struct.unpack_from("<I", result)
        if not count or returned.value < 8 + count * 24:
            raise OSError("卷设备范围不完整")
        return {struct.unpack_from("<I", result, 8 + i * 24)[0] for i in range(count)}
    finally:
        close_handle(handle)


def disk_note(a, b):
    try:
        shared = _disk_numbers(a) & _disk_numbers(b)
    except OSError:
        return "无法确认源与目标是否位于不同物理磁盘。"
    if shared:
        return "源与目标位于同一物理磁盘或共用磁盘，此备份不能防止该盘故障。"
    return "Windows 报告源卷与目标卷使用不同的磁盘设备。"
