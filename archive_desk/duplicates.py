"""重复件检查。只读取扫描快照内大小相同、身份未变的普通文件。"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from time import monotonic

from ._winmove import open_regular, pin_directory
from .core import FileInfo, ScanCancelled, change_time_ns, validate_subdir
from .file_revision import content_digest, read_usn


@dataclass(frozen=True)
class DuplicateGroup:
    digest: str
    size: int
    files: tuple[FileInfo, ...]


@dataclass
class DuplicateResult:
    groups: list[DuplicateGroup]
    skipped: list[str]
    candidates: int


def _cancelled(cancel: Event | None):
    if cancel and cancel.is_set():
        raise ScanCancelled("已取消重复件检查")


@contextmanager
def _checked_file(root: Path, file: FileInfo):
    # Pin each ancestor before opening its child. The read handle denies writes
    # and renames for the duration of hashing, including via another hard link.
    if not root.is_absolute() or not file.path.is_relative_to(root):
        raise ValueError("文件不在所选根目录内")
    validate_subdir(str(file.path.relative_to(root)))
    with ExitStack() as stack:
        for directory in reversed(file.path.parents):
            stack.enter_context(pin_directory(directory))
        stream, handle, st = stack.enter_context(open_regular(file.path))
        if ((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) !=
                (file.dev, file.ino, file.size, file.mtime_ns) or
                file.change_ns is not None and change_time_ns(handle) != file.change_ns):
            raise ValueError("文件自扫描后已更改或被替换")
        if file.usn is not None and read_usn(handle) != file.usn:
            raise ValueError("文件内容在扫描后已变化")
        yield stream


def _hash_file(root: Path, file: FileInfo, cancel: Event | None) -> str:
    digest = hashlib.sha256()
    with _checked_file(root, file) as stream:
        while True:
            _cancelled(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                result = digest.hexdigest()
                if file.digest is not None and file.digest != result:
                    raise ValueError("文件内容在扫描后已变化")
                return result
            digest.update(chunk)


def find_duplicates(root: Path, files, cancel: Event | None = None, progress=None) -> DuplicateResult:
    sizes = defaultdict(list)
    seen = set()
    for file in files:
        _cancelled(cancel)
        if file.path not in seen:
            sizes[file.size].append(file)
            seen.add(file.path)
    candidates = [file for same_size in sizes.values() if len(same_size) > 1 for file in same_size]
    skipped = []
    hashes = defaultdict(list)
    total = len(candidates)
    last_update = 0.0
    for index, file in enumerate(candidates):
        _cancelled(cancel)
        now = monotonic()
        if progress and now - last_update >= 0.1:
            progress(index, total, file.path.name)
            last_update = now
        try:
            digest = _hash_file(root, file, cancel)
        except (ValueError, OSError) as exc:
            skipped.append(f"{file.path}　{exc}")
        else:
            hashes[file.size, digest].append(file)

    groups = []
    for (size, digest), members in hashes.items():
        if len(members) < 2:
            continue
        current = []
        # A file hashed early might have changed while hashing a later file.
        for file in members:
            _cancelled(cancel)
            try:
                with _checked_file(root, file) as stream:
                    if file.usn is None and content_digest(stream, cancel) != digest:
                        raise ValueError("文件内容在校验期间已变化")
                    current.append(file)
            except (ValueError, OSError) as exc:
                skipped.append(f"{file.path}　{exc}")
        if len(current) > 1:
            groups.append(DuplicateGroup(digest, size, tuple(current)))
    groups.sort(key=lambda group: (-group.size, group.digest))
    if progress:
        progress(total, total, "")
    return DuplicateResult(groups, skipped, total)
