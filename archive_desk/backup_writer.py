"""Create self-contained, content-addressed backup versions on a Windows filesystem."""
import ctypes
import hashlib
import json
import msvcrt
import os
import shutil
import stat
import struct
import re
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from . import backup_format as fmt
from ._winmove import (DELETE_ACCESS, GENERIC_READ, INVALID_HANDLE, OPEN_EXISTING, OPEN_REPARSE_POINT,
                       close_handle, create_file, extended_path, kernel,
                       open_entry, open_regular, open_staging, pin_directory,
                       rename_without_replace, set_delete_pending, set_information)
from .core import change_time_ns, check_cancel, reparse
from .file_revision import read_usn
from .move_checks import require_vacant

BLOCK = 1024 * 1024
ATTEMPTS = 3
LOCK_NAME = ".archive-desk-writer.lock"


def checkpoint(stage, path):
    """Test hook for terminating an isolated process or exercising concurrent source edits."""


@dataclass(frozen=True)
class BackupPlan:
    source: Path
    repo: Path
    count: int
    size: int
    free: int
    disk_note: str
    issues: list
    source_name: str
    source_identity: tuple
    repo_identity: tuple
    repository_id: str | None


class _SourceIssue(ValueError):
    pass


def _stream_issue(handle):
    # The version format stores the unnamed data stream and modification time.
    # Read-only, compressed, sparse and hard-linked regular files still have
    # valid primary content. A named stream is an independently lost payload.
    getinfo = kernel.GetFileInformationByHandleEx
    getinfo.argtypes = (wintypes.HANDLE,ctypes.c_int,wintypes.LPVOID,wintypes.DWORD)
    getinfo.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(65536)
    if not getinfo(handle,7,buffer,len(buffer)):
        return "主数据已保存；无法验证是否还存在其他 NTFS 命名流"
    following,length = struct.unpack_from("<II",buffer)
    if following or bytes(buffer[24:24+length]).decode("utf-16-le",errors="replace") != "::$DATA":
        return "主数据已保存；此仓库格式未保存附加 NTFS 命名流"
    return None


def _src(function, *args):
    # Only errors from source-side operations may yield an incomplete version.
    # Repository I/O failures always abort publication of the entire version.
    try:
        return function(*args)
    except (ValueError, OSError) as exc:
        raise _SourceIssue(str(exc)) from exc


def _canonical(path):
    """Reject DOS short-name aliases, even when an unrelated file has the requested short name."""
    data = wintypes.WIN32_FIND_DATAW()
    find = kernel.FindFirstFileW
    find.argtypes = (wintypes.LPCWSTR,ctypes.POINTER(wintypes.WIN32_FIND_DATAW))
    find.restype = wintypes.HANDLE
    handle = find(extended_path(path),ctypes.byref(data))
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if data.cFileName != path.name:
            raise ValueError(f"仓库内部名称有大小写或短名冲突：{path.name}")
    finally:
        kernel.FindClose.argtypes = (wintypes.HANDLE,)
        kernel.FindClose(handle)


def _directory(stack, path):
    try:
        identity = stack.enter_context(pin_directory(path))
    except FileNotFoundError:
        path.mkdir()
        identity = stack.enter_context(pin_directory(path))
    _canonical(path)
    return identity


def _base_state(repo, *, locked=False):
    names = {entry.name for entry in repo.iterdir()}
    if not names:
        return None
    marker = repo/"repository.json"
    if marker.name not in names:
        if LOCK_NAME not in names:
            raise ValueError("请选择空目录或含有效标识的归档台备份仓库")
        # The process can stop immediately after CREATE_NEW of its first lock.
        # Never truncate or delete this file. Reacquiring the lock rechecks it.
        if not locked:
            with open_regular(repo/LOCK_NAME) as (_,_,info):
                _canonical(repo/LOCK_NAME)
                if info.st_nlink != 1 or info.st_size:
                    raise ValueError("仓库初始化锁不属于可继续使用的空锁文件")
        for name in names-{LOCK_NAME}:
            match = re.fullmatch(r"\.archive-desk-initialize-([0-9a-f]{32})\.part",name)
            if not match:
                raise ValueError("目录含有无法识别的内容，请选择空目录或有效仓库")
            with open_regular(repo/name) as (stream,_,info):
                _canonical(repo/name)
                raw = stream.read(1025)
                valid = {"format":"ArchiveDesk","version":fmt.FORMAT,"repository_id":match[1]}
                if info.st_nlink != 1 or raw != json.dumps(valid).encode("ascii"):
                    raise ValueError("未能确认初始化临时文件的来源，已保留原文件")
        return None
    with open_regular(marker) as (_, _, info):
        _canonical(marker)
        if info.st_nlink != 1:
            raise ValueError("仓库标识不应是硬链接")
        return fmt.read_marker(repo)["repository_id"]


def _inventory(source, cancel, progress):
    files, folders, issues = {}, {}, {}
    next_tick = 0.0

    def walk(parent):
        nonlocal next_tick
        check_cancel(cancel)
        with os.scandir(parent) as entries:
            names = [e.name for e in entries]
        counts = {}
        for name in names:
            key = name.casefold()
            counts[key] = counts.get(key,0) + 1
        for name in sorted(names):
            check_cancel(cancel)
            path = parent/name
            rel = path.relative_to(source).as_posix()
            try:
                fmt.check_relative(rel)
            except ValueError as exc:
                folder = parent.relative_to(source).as_posix()
                issues[(folder,name)] = f"名称无法安全还原：{name}（{exc}）"
                continue
            try:
                if counts[name.casefold()] != 1:
                    raise ValueError("存在 Windows 大小写名称冲突")
                st = path.lstat()
                if reparse(st):
                    raise ValueError("链接或重解析点，已跳过")
                if stat.S_ISDIR(st.st_mode):
                    with pin_directory(path) as identity:
                        folders[rel] = identity
                        walk(path)
                elif stat.S_ISREG(st.st_mode):
                    files[rel] = (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
                else:
                    raise ValueError("不是可备份的普通文件")
            except (ValueError,OSError) as exc:
                issues[(rel,"")] = str(exc)
            if progress and monotonic() >= next_tick:
                progress(len(files),0,rel)
                next_tick = monotonic() + .12
    walk(source)
    return files, folders, [{"path":p,"reason":reason} for (p,_),reason in issues.items()]


def prepare_backup(root, repo, *, cancel=None, progress=None):
    source, repo = Path(root["path"]).absolute(), Path(repo).absolute()
    check_cancel(cancel)
    with ExitStack() as stack:
        identity = fmt.pin_path(stack,source)
        if identity != (int(root["dev"]),int(root["ino"])):
            raise ValueError("源目录与已登记的卷和目录身份不同")
        destination = fmt.pin_path(stack,repo)
        fmt.assert_separate(source,repo)
        marker = _base_state(repo)
        files, _, issues = _inventory(source,cancel,progress)
        size = sum(record[2] for record in files.values())
        check_cancel(cancel)
        return BackupPlan(source,repo,len(files),size,shutil.disk_usage(repo).free,
                          fmt.disk_note(source,repo),issues,root["name"],identity,destination,marker)


@contextmanager
def _writer_lock(repo):
    # OPEN_ALWAYS with share mask zero opens an existing lock without changing
    # it. Network filesystems also mediate this lock between different machines.
    path = repo/LOCK_NAME
    handle = create_file(extended_path(path),GENERIC_READ,0,None,4,OPEN_REPARSE_POINT,None)
    if handle == INVALID_HANDLE:
        error = ctypes.get_last_error()
        if error in (32,33):
            raise ValueError("此仓库已有写入任务") from None
        raise ctypes.WinError(error)
    try:
        fd = msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    with os.fdopen(fd,"rb") as lock:
        st = os.fstat(lock.fileno())
        _canonical(path)
        if not stat.S_ISREG(st.st_mode) or reparse(st) or st.st_nlink != 1 or st.st_size != 0:
            raise ValueError("仓库锁不是独立的空普通文件")
        yield


class _Stage:
    def __init__(self, stream, handle, delete_on_close):
        self.stream, self.handle, self.delete_on_close = stream, handle, delete_on_close

    def commit(self, path):
        require_vacant(path)
        if self.delete_on_close:
            # The OS owns crash cleanup from CREATE_NEW, including a process
            # exit between rename and clearing the disposition on this handle.
            rename_without_replace(self.handle,path)
            flags = wintypes.DWORD(8)  # FILE_DISPOSITION_FLAG_ON_CLOSE without DELETE
            if not set_information(self.handle,21,ctypes.byref(flags),4):
                raise ctypes.WinError(ctypes.get_last_error())
            return
        set_delete_pending(self.handle,False)
        try:
            rename_without_replace(self.handle,path)
        except BaseException:
            set_delete_pending(self.handle,True)
            raise


@contextmanager
def _stage(folder, name=None):
    path = folder/(name or (uuid4().hex + ".part"))
    handle = create_file(extended_path(path),GENERIC_READ|0x40000000|DELETE_ACCESS,1,None,1,
                         OPEN_REPARSE_POINT|0x04000000,None)  # FILE_FLAG_DELETE_ON_CLOSE
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle,os.O_RDWR|os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    with os.fdopen(fd,"r+b",buffering=0) as stream:
        flags = wintypes.DWORD(9)  # ON_CLOSE|DELETE; probes support without lifting protection.
        if set_information(handle,21,ctypes.byref(flags),4):
            yield _Stage(stream,handle,True)
            return
        error = ctypes.get_last_error()
        if error not in (1,50,87,124):
            raise ctypes.WinError(error)
    # Older filesystem providers can still use the ordinary disposition API.
    # Never sweep residual names under tmp/. Only this newly created handle is touched.
    with open_staging(path,create=True) as (stream,handle,st):
        if st.st_nlink != 1:
            raise ValueError("新建的临时文件出现额外硬链接")
        set_delete_pending(handle,True)
        yield _Stage(stream,handle,False)


def _space(repo, amount):
    if shutil.disk_usage(repo).free < amount:
        raise OSError("备份仓库可用空间不足")


def _publish_bytes(folder, dest, data, *, before=None, stage_name=None, final=False):
    _space(folder,len(data))
    with _stage(folder,stage_name) as stage:
        if stage.stream.write(data) != len(data):
            raise OSError("写入仓库控制文件不完整")
        os.fsync(stage.stream.fileno())
        stage.stream.seek(0)
        if stage.stream.read() != data:
            raise OSError("仓库控制文件回读不一致")
        if before:
            before()
        stage.commit(dest)
        if not final:
            os.fsync(stage.stream.fileno())


@contextmanager
def _shared_source(path):
    handle = create_file(extended_path(path),GENERIC_READ,3,None,OPEN_EXISTING,OPEN_REPARSE_POINT,None)
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close_handle(handle)
        raise
    with os.fdopen(fd,"rb",buffering=0) as stream:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or reparse(st):
            raise ValueError("源文件已替换为链接或目录")
        yield stream,handle,st


def _stamp(stream,handle):
    st = os.fstat(stream.fileno())
    return st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,change_time_ns(handle),read_usn(handle)


def _read_hash(path, stream, size, counts, cancel, output=None, notify=None):
    digest = hashlib.sha256()
    remaining = size
    while remaining:
        check_cancel(cancel)
        chunk = _src(stream.read,min(BLOCK,remaining))
        if not chunk:
            raise _SourceIssue("读取期间源文件缩短")
        remaining -= len(chunk)
        counts["bytes_read"] += len(chunk)
        digest.update(chunk)
        if output is None:
            checkpoint("source_chunk",path)
        else:
            if output.write(chunk) != len(chunk):
                raise OSError("写入备份对象的字节数不完整")
            checkpoint("copy_chunk",path)
        if notify:
            notify("读取源文件" if output is None else "复制到临时对象",size-remaining)
    extra = _src(stream.read,1)
    if extra:
        counts["bytes_read"] += len(extra)
        raise _SourceIssue("读取期间源文件增长")
    return digest.hexdigest()


def _repository_hash(stream, cancel, notify):
    stream.seek(0)
    digest = hashlib.sha256()
    read = 0
    while True:
        check_cancel(cancel)
        block = stream.read(BLOCK)
        if not block:
            return digest.hexdigest()
        digest.update(block)
        read += len(block)
        if notify:
            notify("回读校验仓库对象",read)


def _hold_object(stack, path, size, digest, cancel, notify):
    # A native handle costs no CRT descriptor. Keep it until the version is
    # published so an earlier verified object cannot be replaced or removed.
    pin = open_entry(path,GENERIC_READ)
    stack.callback(close_handle,pin)
    with open_regular(path) as (stream,_,st):
        _canonical(path)
        if st.st_nlink != 1 or st.st_size != size or _repository_hash(stream,cancel,notify) != digest:
            raise ValueError(f"已有对象的身份、大小或 SHA-256 不符合仓库记录：{digest}")


def _archive_file(plan, rel, record, folders, stack, prefixes, verified, counts, cancel, notify):
    src = plan.source/rel
    with ExitStack() as item:
        parent = plan.source
        for part in src.relative_to(plan.source).parts[:-1]:
            parent = parent/part
            ident = _src(item.enter_context,pin_directory(parent))
            if ident != folders[parent.relative_to(plan.source).as_posix()]:
                raise _SourceIssue("来源子目录在扫描后被替换")
        first, handle, _ = _src(item.enter_context,_shared_source(src))
        initial = _src(_stamp,first,handle)
        if initial[:2] != record[:2]:
            raise _SourceIssue("源文件在扫描后被替换")
        first_hash = _read_hash(src,first,initial[2],counts,cancel,notify=notify)
        if _src(_stamp,first,handle) != initial:
            raise _SourceIssue("读取过程中源文件发生变化")
        stream, stable, st = _src(item.enter_context,open_regular(src))
        if _src(_stamp,stream,stable) != initial:
            raise _SourceIssue("锁定复核前源文件发生变化")
        stream_issue = _stream_issue(stable)
        path = fmt.object_path(plan.repo,first_hash)
        if path.parent not in prefixes:
            _directory(stack,path.parent)
            prefixes.add(path.parent)
        if first_hash in verified:
            exists = True
        else:
            try:
                path.lstat()
                exists = True
            except FileNotFoundError:
                exists = False
        if not exists:
            _space(plan.repo,st.st_size)
            with _stage(plan.repo/"tmp") as temp:
                if _read_hash(src,stream,st.st_size,counts,cancel,temp.stream,notify) != first_hash:
                    raise _SourceIssue("源文件两次读取的 SHA-256 不同")
                os.fsync(temp.stream.fileno())
                if os.fstat(temp.stream.fileno()).st_size != st.st_size or _repository_hash(temp.stream,cancel,notify) != first_hash:
                    raise OSError("临时对象的长度或 SHA-256 回读校验失败")
                if _src(_stamp,stream,stable) != initial:
                    raise _SourceIssue("复制期间文件元数据发生变化")
                temp.commit(path)
                os.fsync(temp.stream.fileno())
                counts["new_objects"] += 1
            checkpoint("object_committed",path)
        elif initial[-1] is None and _read_hash(src,stream,st.st_size,counts,cancel,notify=notify) != first_hash:
            raise _SourceIssue("没有可靠变更记录，锁定读取的 SHA-256 不一致")
        if first_hash not in verified:
            _hold_object(stack,path,st.st_size,first_hash,cancel,notify)
            verified.add(first_hash)
        stream_issue = _stream_issue(stable) or stream_issue
        if _src(_stamp,stream,stable) != initial:
            raise _SourceIssue("读取后文件元数据发生变化")
        return {"path":rel,"size":st.st_size,"mtime_ns":st.st_mtime_ns,"sha256":first_hash}, initial, stream_issue


def write_backup(plan, *, cancel=None, progress=None):
    check_cancel(cancel)
    counts = {"bytes_read":0,"new_objects":0,"retries":0}
    with ExitStack() as stack:
        if fmt.pin_path(stack,plan.source) != plan.source_identity:
            raise ValueError("源目录身份与预检不一致")
        if fmt.pin_path(stack,plan.repo) != plan.repo_identity:
            raise ValueError("备份仓库位置与预检不一致")
        fmt.assert_separate(plan.source,plan.repo)
        if _base_state(plan.repo) != plan.repository_id:
            raise ValueError("仓库标识与预检不一致，请重新预检")
        _space(plan.repo,1)
        stack.enter_context(_writer_lock(plan.repo))
        checkpoint("locked",plan.repo)
        existing = _base_state(plan.repo,locked=True)
        if existing != plan.repository_id:
            raise ValueError("加锁后仓库标识与预检不一致")
        if existing is None:
            marker = {"format":"ArchiveDesk","version":fmt.FORMAT,"repository_id":uuid4().hex}
            _publish_bytes(plan.repo,plan.repo/"repository.json",json.dumps(marker).encode("ascii"),
                           stage_name=f".archive-desk-initialize-{marker['repository_id']}.part",
                           before=lambda: checkpoint("before_marker",plan.repo))
            checkpoint("marker_committed",plan.repo)
        # Pin the marker and each internal directory for the whole transaction.
        marker_pin = open_entry(plan.repo/"repository.json",GENERIC_READ)
        stack.callback(close_handle,marker_pin)
        actual_marker = _base_state(plan.repo,locked=True)
        if actual_marker != (existing or marker["repository_id"]):
            raise ValueError("仓库标识发生变化")
        for name in ("versions","objects","tmp"):
            _directory(stack,plan.repo/name)
        inventory, folders, issues = _inventory(plan.source,cancel,progress)
        files, stamps = [], {}
        prefixes, verified = set(), set()
        next_tick = 0.0
        def report(index, message):
            nonlocal next_tick
            if progress and monotonic() >= next_tick:
                progress(index,len(inventory),message)
                next_tick = monotonic()+.12
        for n,(rel,record) in enumerate(inventory.items()):
            check_cancel(cancel)
            report(n,rel)
            starting_bytes = counts["bytes_read"]
            def notify(phase, amount):
                report(n,f"{rel} · {phase} {amount:,} 字节 · 本文件累计读取 {counts['bytes_read']-starting_bytes:,} 字节")
            for attempt in range(ATTEMPTS):
                try:
                    file, stamp, warning = _archive_file(plan,rel,record,folders,stack,prefixes,
                                                        verified,counts,cancel,notify if progress else None)
                    files.append(file)
                    stamps[rel] = stamp
                    if warning:
                        issues.append({"path":rel,"reason":warning})
                    break
                except _SourceIssue as exc:
                    if attempt + 1 == ATTEMPTS:
                        issues.append({"path":rel,"reason":str(exc)})
                    else:
                        counts["retries"] += 1
                        check_cancel(cancel)
        # Catch additions, disappearances and directory replacements between
        # enumeration and completion. This is a file backup, not a VSS snapshot.
        latest, last_dirs, final_issues = _inventory(plan.source,cancel,None)
        seen_issues = {(row["path"],row["reason"]) for row in issues}
        for issue in final_issues:
            if (issue["path"],issue["reason"]) not in seen_issues:
                issues.append(issue)
        changes = (set(inventory)^set(latest)) | (set(folders)^set(last_dirs))
        changes.update(rel for rel in folders.keys() & last_dirs.keys() if folders[rel] != last_dirs[rel])
        changes.update(rel for rel,stamp in stamps.items() if rel in latest and latest[rel] != stamp[:4])
        for rel in sorted(changes):
            issues.append({"path":rel,"reason":"备份过程中源目录清单或文件身份、大小、修改时间发生变化"})
        version = uuid4().hex
        manifest = dict(format=fmt.FORMAT,id=version,source_name=plan.source_name,
                        created_at=datetime.now().astimezone().isoformat(timespec="microseconds"),
                        status="incomplete" if issues else "complete",files=files,
                        directories=sorted(folders),issues=issues)
        fmt.validate_manifest(manifest,version)
        raw = (json.dumps(manifest,ensure_ascii=False,indent=2)+"\n").encode("utf-8",errors="backslashreplace")
        if len(raw)>fmt.MAX_MANIFEST_BYTES:
            raise ValueError("版本清单超过格式大小上限")
        check_cancel(cancel)
        checksum = plan.repo/"versions"/(version+".sha256")
        dest = checksum.with_suffix(".json")
        require_vacant(dest)
        sidecar = (hashlib.sha256(raw).hexdigest()+"\n").encode("ascii")
        _publish_bytes(plan.repo/"tmp",checksum,sidecar)
        sidecar_stream,_,sidecar_info = stack.enter_context(open_regular(checksum))
        _canonical(checksum)
        if sidecar_info.st_nlink != 1 or sidecar_stream.read(len(sidecar)+1) != sidecar:
            raise ValueError("发布前清单校验文件已发生变化")
        def final_check():
            checkpoint("before_manifest",dest)
            check_cancel(cancel)
        _publish_bytes(plan.repo/"tmp",dest,raw,before=final_check,final=True)
        if progress:
            progress(len(inventory),len(inventory),"版本已发布")
        return dict(id=version,status=manifest["status"],files=len(files),issues=issues,
                    file_bytes=sum(f["size"] for f in files),**counts)
