"""只读恢复计划及经校验的无覆盖文件提交。"""
import ctypes
import hashlib
import json
import os
from collections import defaultdict
from contextlib import ExitStack
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from ._winmove import (kernel, open_regular, open_staging, rename_without_replace,
                       set_delete_pending)
from .backup_format import assert_separate, object_path, pin_path, read_manifest, read_marker
from .core import ScanCancelled, check_cancel, validate_subdir
from .file_revision import content_digest
from .move_checks import Directories, check_path, require_vacant


@dataclass(frozen=True)
class RestoreEntry:
    path: str
    target: Path
    kind: str
    action: str
    reason: str
    size: int = 0
    mtime_ns: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class RestorePlan:
    repo: Path
    repository_id: str
    repository_identity: tuple
    version_id: str
    manifest_digest: str
    destination: Path
    entries: tuple[RestoreEntry, ...]
    directories: tuple
    version_status: str


def _signature(manifest):
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parents(relpath):
    return (p.as_posix() for p in PurePosixPath(relpath).parents if p != PurePosixPath("."))


def _selection(manifest, selected):
    files = {r["path"]: r for r in manifest["files"]}
    dirs = set(manifest["directories"])
    for path in (*files, *dirs):
        dirs.update(_parents(path))
    if selected is None:
        return files, dirs
    chosen = {selected} if isinstance(selected, str) else set(selected)
    if chosen - (dirs | files.keys()):
        raise ValueError("选中的相对路径不属于此备份版本")
    def included(path):
        return path in chosen or any(parent in chosen for parent in _parents(path))
    files = {p: r for p, r in files.items() if included(p)}
    dirs = {p for p in dirs if included(p)}
    for path in (*files, *dirs):
        dirs.update(_parents(path))
    return files, dirs


class _Preview:
    def __init__(self, root, root_identity, reserved, mode):
        self.root = root
        self.identities = {root: root_identity}
        self.missing = set()
        self.occupied = {}
        self.reserved = reserved
        self.mode = mode
        self.locations = {".": (root, True)}

    def _children(self, folder):
        if folder in self.missing:
            return {}
        if folder not in self.occupied:
            with ExitStack() as stack:
                if pin_path(stack, folder) != self.identities[folder]:
                    raise ValueError("目录在预览期间已被替换")
                self.occupied[folder] = {p.name.casefold(): p.name for p in folder.iterdir()}
        return self.occupied[folder]

    def _number(self, folder, parent_key, part, is_dir, occupied):
        base = Path(part)
        stem, ext = (part, "") if is_dir else (base.stem, base.suffix)
        number = 1
        while True:
            name = f"{stem} ({number}){ext}"
            key = name.casefold()
            if key not in self.reserved[parent_key] and key not in occupied:
                validate_subdir(name)
                self.reserved[parent_key].add(key)
                return folder / name
            number += 1

    def entry(self, relpath, metadata=None):
        is_dir = metadata is None
        kind = "directory" if is_dir else "file"
        part = PurePosixPath(relpath)
        parent_key = part.parent.as_posix()
        parent, parent_ok = self.locations[parent_key]
        target = parent / part.name
        action, reason = "skip", "上级目录无法按计划恢复"
        try:
            if parent_ok:
                names = self._children(parent)
                existing = names.get(part.name.casefold())
                action = "create_dir" if is_dir else "restore"
                reason = "将在确认后创建目录" if is_dir else "将校验对象后恢复文件"
                if existing is not None:
                    action = "skip"
                    reason = "目标已有同名项"
                    if is_dir:
                        try:
                            with ExitStack() as stack:
                                # Pin this parent first. A cached listing cannot
                                # authorize descent through a replaced directory.
                                if pin_path(stack, parent) != self.identities[parent]:
                                    raise ValueError("上级目录已被替换")
                                if existing != part.name:
                                    raise ValueError("目标目录名称大小写冲突")
                                self.identities[target] = pin_path(stack, target)
                            action, reason = "keep_dir", "沿用现有普通目录"
                        except (OSError, ValueError) as exc:
                            reason = f"目标不能用作目录：{exc}"
                    if action == "skip" and self.mode == "rename":
                        target = self._number(parent, parent_key, part.name, is_dir, names)
                        action = "create_dir" if is_dir else "rename"
                        reason = "同名项保留，恢复到编号后的新路径"
                if is_dir and action == "create_dir":
                    self.missing.add(target)
        except (OSError, ValueError) as exc:
            action, reason = "skip", str(exc)
        if is_dir:
            self.locations[relpath] = target, action != "skip"
        data = metadata or {}
        return RestoreEntry(relpath, target, kind, action, reason, data.get("size", 0),
                            data.get("mtime_ns", 0), data.get("sha256", ""))


def prepare_restore(repo, version_id, destination, selected=None, *, conflict="skip",
                    cancel=None, progress=None):
    if conflict not in ("skip", "rename"):
        raise ValueError("冲突处理只能选择跳过或另存为编号名称")
    if not destination:
        raise ValueError("请选择恢复目标目录")
    root, repo = Path(destination).absolute(), Path(repo).absolute()
    assert_separate(repo, root)
    check_cancel(cancel)
    with ExitStack() as stack:
        repo_identity = pin_path(stack, repo)
        root_identity = pin_path(stack, root)
        marker = read_marker(repo)
        manifest = read_manifest(repo, version_id)
    files, dirs = _selection(manifest, selected)
    reserved = defaultdict(set)
    for path in (*files, *dirs):
        relative = PurePosixPath(path)
        reserved[relative.parent.as_posix()].add(relative.name.casefold())
    preview = _Preview(root, root_identity, reserved, conflict)
    entries = []
    total = len(files) + len(dirs)
    for path in (*sorted(dirs, key=lambda p: (p.count("/"), p)), *sorted(files)):
        check_cancel(cancel)
        if progress:
            progress(len(entries), total, path)
        entries.append(preview.entry(path, files.get(path)))
    check_cancel(cancel)
    if progress:
        progress(total, total, "")
    return RestorePlan(repo, marker["repository_id"], repo_identity, version_id, _signature(manifest),
                       root, tuple(entries), tuple(preview.identities.items()), manifest["status"])


def _mtime(handle, ns):
    ticks = ns // 100 + 116444736000000000
    if not 0 <= ticks < 2**63:
        raise ValueError("备份中的修改时间超出 Windows 可表示范围")
    stamp = wintypes.FILETIME(ticks & 0xffffffff, ticks >> 32)
    setter = kernel.SetFileTime
    setter.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 3
    setter.restype = wintypes.BOOL
    if not setter(handle, None, None, ctypes.byref(stamp)):
        raise ctypes.WinError(ctypes.get_last_error())


class _TargetChanged(ValueError):
    pass


def _vacant(target):
    try:
        require_vacant(target)
    except ValueError as exc:
        raise _TargetChanged(str(exc)) from exc


def _write_verified(source, entry, cancel):
    parent = entry.target.parent
    _vacant(entry.target)
    temp = parent / f".archive-desk-restore-{uuid4().hex}.tmp"
    with open_staging(temp, create=True) as (output, handle, _):
        published = False
        try:
            # A process exit during a long copy lets Windows discard this
            # exact temporary handle. Other .tmp files are never enumerated.
            set_delete_pending(handle, True)
            hasher = hashlib.sha256()
            count = 0
            while True:
                check_cancel(cancel)
                block = source.read(1024 * 1024)
                if not block:
                    break
                if output.write(block) != len(block):
                    raise OSError("临时文件写入不完整")
                count += len(block)
                hasher.update(block)
            if count != entry.size or hasher.hexdigest() != entry.sha256:
                raise ValueError("对象实际大小或 SHA-256 与清单不一致")
            os.fsync(output.fileno())
            if content_digest(output, cancel) != entry.sha256:
                raise ValueError("写入后的内容校验失败")
            _mtime(handle, entry.mtime_ns)
            os.fsync(output.fileno())
            check_cancel(cancel)
            _vacant(entry.target)
            set_delete_pending(handle, False)
            try:
                rename_without_replace(handle, entry.target)
            except FileExistsError as exc:
                raise _TargetChanged("目标名称在恢复期间被占用") from exc
            published = True
        finally:
            if not published:
                set_delete_pending(handle, True)


def restore(plan, *, cancel=None, progress=None):
    result = dict(restored=0, created_dirs=0, skipped=0, failed=0, cancelled=False, issues=[])
    expected = dict(plan.directories)
    created = {}
    try:
        check_cancel(cancel)
        assert_separate(plan.repo, plan.destination)
        with ExitStack() as repo_stack:
            if (pin_path(repo_stack, plan.repo) != plan.repository_identity or
                    read_marker(plan.repo)["repository_id"] != plan.repository_id):
                raise ValueError("备份仓库身份自预览后已变化")
            manifest = read_manifest(plan.repo, plan.version_id)
            if _signature(manifest) != plan.manifest_digest:
                raise ValueError("版本清单自预览后已变化，请重新预览")
            files = {r["path"]: r for r in manifest["files"]}
            total = len(plan.entries)
            for index, entry in enumerate(plan.entries):
                check_cancel(cancel)
                if progress:
                    progress(index, total, entry.path)
                try:
                    if entry.action == "skip":
                        raise _TargetChanged(entry.reason)
                    check_path(plan.destination, entry.target)
                    with ExitStack() as stack:
                        dirs = Directories(stack, expected, created)
                        is_dir = entry.kind == "directory"
                        folder = entry.target if is_dir else entry.target.parent
                        try:
                            dirs.pin(plan.destination, folder, allow_create=True)
                        except (FileNotFoundError, ValueError) as exc:
                            raise _TargetChanged(str(exc)) from exc
                        if is_dir:
                            if entry.action == "create_dir":
                                result["created_dirs"] += 1
                            continue
                        record = files[entry.path]
                        if (entry.size, entry.sha256, entry.mtime_ns) != (
                                record["size"], record["sha256"], record["mtime_ns"]):
                            raise ValueError("恢复条目与已验证清单不一致")
                        obj = object_path(plan.repo, entry.sha256)
                        pin_path(stack, obj.parent)
                        stream, _, info = stack.enter_context(open_regular(obj))
                        if info.st_size != entry.size:
                            raise ValueError("备份对象大小与清单不符")
                        _write_verified(stream, entry, cancel)
                        result["restored"] += 1
                except _TargetChanged as exc:
                    result["skipped"] += 1
                    result["issues"].append({"path": entry.path, "reason": str(exc)})
                except (OSError, ValueError, KeyError) as exc:
                    result["failed"] += 1
                    result["issues"].append({"path": entry.path, "reason": str(exc)})
            if progress:
                progress(total, total, "")
    except ScanCancelled:
        result["cancelled"] = True
    return result
