"""Prefer the NTFS per-file change journal; fall back to a content snapshot."""
import ctypes
import hashlib
import struct
from ctypes import wintypes

from .core import check_cancel

_device_io = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
_device_io.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                       wintypes.LPVOID]
_device_io.restype = wintypes.BOOL
_READ_FILE_USN_DATA = 0x000900EB


def read_usn(handle):
    # V2 and V3 have 64-bit and 128-bit file references respectively.
    buffer = ctypes.create_string_buffer(4096)
    returned = wintypes.DWORD()
    if not _device_io(handle, _READ_FILE_USN_DATA, None, 0, buffer, len(buffer),
                      ctypes.byref(returned), None) or returned.value < 32:
        return None
    size, version = struct.unpack_from("<IH", buffer)
    offset = {2: 24, 3: 40}.get(version)
    if offset is None or not offset + 8 <= size <= returned.value:
        return None
    value, = struct.unpack_from("<q", buffer, offset)
    return value if value > 0 else None


def content_digest(stream, cancel=None):
    position = stream.tell()
    stream.seek(0)
    try:
        digest = hashlib.sha256()
        while True:
            check_cancel(cancel)
            block = stream.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)
    finally:
        stream.seek(position)


def revision_matches(file, stream, handle, *, digest=None, cancel=None):
    if file.usn is not None and read_usn(handle) != file.usn:
        return False
    return file.digest is None or (digest or content_digest(stream, cancel)) == file.digest
