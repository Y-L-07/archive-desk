"""Resumable, no-replace Windows transfer protocol for both move directions."""
import os
from pathlib import Path
from uuid import uuid4

from ._winmove import (copy_times, cross_copy_supported, open_regular, open_staging,
                       remove_checked_handle, rename_without_replace)
from .core import check_cancel
from .file_revision import content_digest
from .move_checks import fingerprint, require_vacant

BLOCK = 1024 * 1024


def checkpoint(stage, item_id):
    """Non-mutating integration-test hook for terminating an isolated child process."""


class Transfer:
    def __init__(self, journal, item_id, status, direction, phase="", data=None):
        self.journal = journal
        self.item_id = item_id
        self.status = status
        self.direction = direction
        self.phase = phase
        self.data = data or {}

    def record(self, status, phase, reason="", **kwargs):
        self.journal.transition(self.item_id, self.status, status, reason, direction=self.direction,
                                phase=phase, transfer=self.data, **kwargs)
        self.status, self.phase = status, phase

    @property
    def working(self):
        return "applying" if self.direction == "apply" else "undoing"

    def complete(self, result):
        evidence = {"directories": self.data["directories"], "sha256": self.data["sha256"],
                    "file": result}
        if self.direction == "apply":
            self.record("moved", "done", "已移动", evidence=evidence)
        else:
            self.record("undone", "done", "已撤销")


def start(job, source, target, opened, dirs, digest, *, cross):
    _, handle, info = opened
    if cross:
        cross_copy_supported(handle, info)
    job.data = {"mode": "copy" if cross else "rename", "source": str(source), "target": str(target),
                "file": fingerprint(info), "directories": dirs.evidence(), "sha256": digest}
    if cross:
        job.data["temp"] = str(target.with_name(f".archive-desk-{job.item_id}-{uuid4().hex}.part"))
    job.record(job.working, "intent")


def _present(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _verify_prefix(src, partial, size, cancel):
    src.seek(0)
    partial.seek(0)
    remaining = size
    while remaining:
        check_cancel(cancel)
        block = partial.read(min(BLOCK, remaining))
        if not block or src.read(len(block)) != block:
            raise ValueError("绑定临时文件与源文件前缀不一致")
        remaining -= len(block)


def _remove_source(job, source, opened, close_source):
    # The target remains open against writes/renames until removal of this
    # exact source handle finishes. Reconfirm path identity and extra streams.
    stream, handle, _ = opened
    info = os.fstat(stream.fileno())
    if fingerprint(source.lstat()) != job.data["file"] or fingerprint(info) != job.data["file"]:
        raise ValueError("移除前源文件的身份或大小、修改时间已变化")
    cross_copy_supported(handle, info)
    remove_checked_handle(handle)
    close_source()
    if _present(source):
        raise ValueError("源文件移除尚未确认，请核对两侧")


def advance(job, source, target, opened, close_source, stack, cancel, *, inspect=False):
    """Read-only inspection and explicit recovery share the same evidence checks."""
    data = job.data
    copying = data["mode"] == "copy"
    if data["source"] != str(source) or data["target"] != str(target):
        raise ValueError("阶段记录与已确认路径不一致")
    temporary = Path(data["temp"]) if copying else None
    if copying and (temporary.parent != target.parent or temporary in (source, target)
                    or not temporary.name.startswith(f".archive-desk-{job.item_id}-")
                    or not temporary.name.endswith(".part")):
        raise ValueError("临时文件不属于本项操作")
    has_temp = copying and _present(temporary)
    if _present(target):
        expected = data.get("result") if copying else data["file"]
        if (has_temp or expected is None or not copying and opened is not None
                or copying and job.phase not in ("verified", "committed")):
            raise ValueError("目标位置被占用或与阶段记录不一致")
        deststream, desthandle, destinfo = stack.enter_context(open_regular(target))
        if (fingerprint(destinfo) != expected or
                content_digest(deststream, cancel) != data["sha256"]):
            raise ValueError("已提交目标与记录的身份、大小、时间或哈希不一致")
        if copying:
            cross_copy_supported(desthandle, destinfo)
        if inspect:
            return "目标已核验；可确认后移除核验一致的源文件" if opened else "目标已核验；可确认完成日志"
        if opened:
            job.record(job.working, "committed")
            _remove_source(job, source, opened, close_source)
        job.complete(expected)
        return
    if not opened:
        raise ValueError("两侧缺少可验证的文件")
    require_vacant(target)
    if not copying:
        if inspect:
            return "源文件已核验，目标位置空闲，可继续改名"
        if job.status != job.working:
            job.record(job.working, "intent")
        rename_without_replace(opened[1], target)
        job.complete(data["file"])
        return

    src, source_handle, source_stat = opened
    cross_copy_supported(source_handle, source_stat)
    if job.phase == "committed" or (not has_temp and data.get("temp_id")):
        raise ValueError("阶段记录中的目标或绑定临时文件缺失")
    if has_temp and not data.get("temp_id"):
        raise ValueError("临时名称已被占用，无法证明文件属于本次操作")
    if inspect and not has_temp:
        return "源文件已核验，临时目录和目标位置可用"
    if inspect:
        partial, handle, info = stack.enter_context(open_regular(temporary))
    else:
        job.record(job.working, job.phase)
        partial, handle, info = stack.enter_context(open_staging(temporary, create=not has_temp))
    if has_temp:
        if list((info.st_dev, info.st_ino)) != data["temp_id"] or info.st_size > source_stat.st_size:
            raise ValueError("临时文件的身份或大小与记录不符")
        cross_copy_supported(handle, info)
        _verify_prefix(src, partial, info.st_size, cancel)
    if inspect:
        return f"已核验 {info.st_size:,} 字节临时副本，可从断点继续"
    data["temp_id"] = [info.st_dev, info.st_ino]
    job.record(job.working, "copying")
    src.seek(info.st_size)
    partial.seek(info.st_size)
    while True:
        check_cancel(cancel)
        block = src.read(BLOCK)
        if not block:
            break
        if partial.write(block) != len(block):
            raise OSError("写入临时副本的字节数不完整")
        checkpoint("copy_chunk", job.item_id)
    os.fsync(partial.fileno())
    if (os.fstat(partial.fileno()).st_size != source_stat.st_size
            or content_digest(partial, cancel) != data["sha256"]):
        raise ValueError("临时副本的大小或 SHA-256 校验失败")
    copy_times(source_handle, handle)
    os.fsync(partial.fileno())
    data["result"] = fingerprint(os.fstat(partial.fileno()))
    job.record(job.working, "verified")
    check_cancel(cancel)
    require_vacant(target)
    rename_without_replace(handle, target)
    os.fsync(partial.fileno())
    job.record(job.working, "committed")
    checkpoint("committed", job.item_id)
    check_cancel(cancel)
    _remove_source(job, source, opened, close_source)
    job.complete(data["result"])
