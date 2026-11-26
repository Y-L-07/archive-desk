"""在用户选择的目录中采集普通文件快照。"""
import os
import stat
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from time import monotonic

from .core import (FileInfo, change_time_ns, check_cancel, directory_identity,
                   pin_snapshot_entry, reparse)
from ._winmove import open_regular
from .file_revision import content_digest, read_usn


class ScanResult(list[FileInfo]):
    def __init__(self):
        super().__init__()
        self.warnings: list[str] = []


def _snapshot(path, cancel):
    with open_regular(path) as (stream, handle, st):
        change = change_time_ns(handle)
        usn = read_usn(handle)
        digest = content_digest(stream, cancel) if usn is None else None
        return FileInfo(path, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
                        change, digest, usn)


def snapshot_file(path: Path, cancel: Event | None = None) -> FileInfo:
    path = Path(path).absolute()
    with ExitStack() as stack:
        for parent in reversed(path.parents):
            check_cancel(cancel)
            stack.enter_context(pin_snapshot_entry(parent))
            directory_identity(parent)
        return _snapshot(path, cancel)


def scan(root: Path, recursive: bool = False, cancel: Event | None = None, *, progress=None) -> ScanResult:
    """以选择的目录为边界。Windows 上遍历期间持有父目录句柄，禁止改名替换。"""
    root = Path(root).absolute()
    result = ScanResult()
    next_update = 0.0

    def walk(folder: Path):
        nonlocal next_update
        check_cancel(cancel)
        with pin_snapshot_entry(folder):
            directory_identity(folder)
            with os.scandir(folder) as entries:
                for entry in entries:
                    check_cancel(cancel)
                    path = Path(entry.path)
                    try:
                        if entry.is_symlink():
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if reparse(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if recursive:
                                walk(path)
                        elif stat.S_ISREG(info.st_mode):
                            result.append(_snapshot(path, cancel))
                            if progress and monotonic() >= next_update:
                                progress(len(result), str(path))
                                next_update = monotonic() + 0.12
                    except (OSError, ValueError) as exc:
                        result.warnings.append(f"{path}：{exc}")

    with ExitStack() as stack:
        for parent in reversed(root.parents):
            stack.enter_context(pin_snapshot_entry(parent))
            directory_identity(parent)
        walk(root)
    result.sort(key=lambda item: str(item.path).casefold())
    if progress:
        progress(len(result), str(root))
    return result
