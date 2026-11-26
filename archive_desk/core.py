"""共享的数据模型、内置分类和路径安全检查。"""
from __future__ import annotations

import ctypes
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import Path, PureWindowsPath
from threading import Event


@dataclass(frozen=True)
class Rule:
    name: str
    extensions: tuple[str, ...]
    target: str
    enabled: bool = True
    name_glob: str = ""
    source_subdir: str = ""
    min_size: int | None = None
    max_size: int | None = None
    modified_from: str = ""
    modified_to: str = ""


def default_rules() -> list[Rule]:
    return [
        Rule("图片", (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff", ".svg", ".avif"), "图片"),
        Rule("办公文档", (".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf", ".txt", ".md", ".csv", ".odt", ".ods", ".odp", ".rtf", ".wps"), "文档"),
        Rule("压缩包", (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz"), "压缩包"),
        Rule("安装包", (".exe", ".msi", ".msix", ".appx", ".appxbundle", ".msixbundle"), "安装包"),
    ]


_TYPES = {ext: rule.target for rule in default_rules() for ext in rule.extensions}


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    mtime_ns: int
    dev: int
    ino: int
    change_ns: int | None = None
    digest: str | None = None
    usn: int | None = None

    @cached_property
    def category(self) -> str:
        return _TYPES.get(self.path.suffix.lower(), "其他")


@dataclass(frozen=True)
class DirIdentity:
    path: Path
    dev: int
    ino: int


@dataclass(frozen=True)
class Plan:
    file: FileInfo
    target: Path | None
    rule: str
    status: str
    reason: str
    directories: tuple[DirIdentity, ...] = ()
    source_root: Path | None = None
    target_root: Path | None = None
    base_target: Path | None = None
    rule_signature: str = ""
    conflict_type: str = ""
    resolution: str = "skip"
    kind: str = "archive"
    keeper: FileInfo | None = None
    expected_hash: str = ""


class ScanCancelled(Exception):
    """扫描或计划计算被主动取消。"""


def reparse(st: os.stat_result) -> bool:
    return (stat.S_ISLNK(st.st_mode) or
            bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT))


if os.name == "nt":
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _open = _kernel.CreateFileW
    _open.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _open.restype = wintypes.HANDLE
    _close = _kernel.CloseHandle
    _close.argtypes = [wintypes.HANDLE]
    _close.restype = wintypes.BOOL
    _info = _kernel.GetFileInformationByHandleEx
    _info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _info.restype = wintypes.BOOL

    class _BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int64) for name in ("creation", "access", "write", "change")] + [
            ("attributes", wintypes.DWORD)]


def change_time_ns(handle: int) -> int:
    """Read Windows change time from an open handle, in ns since 1601."""
    basic = _BasicInfo()
    if not _info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
        raise ctypes.WinError(ctypes.get_last_error())
    return basic.change * 100


@contextmanager
def pin_snapshot_entry(path: Path, *, directory: bool = True):
    if os.name != "nt":
        yield None
        return
    # Directory handles also deny writes to keep reparse data stable below them.
    share = 1 if directory else 3
    handle = _open(str(path), 0x80, share, None, 3, 0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield handle
    finally:
        _close(handle)


def directory_identity(path: Path) -> DirIdentity:
    st = path.lstat()
    if reparse(st) or not stat.S_ISDIR(st.st_mode):
        raise ValueError(f"路径不是普通目录或包含链接：{path}")
    return DirIdentity(path, st.st_dev, st.st_ino)


def check_cancel(cancel: Event | None):
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("已取消")


_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])", re.I)


def validate_subdir(target: str) -> Path:
    """接受一个或多个 Windows 子目录组件；拒绝设备名、盘符、ADS 和相对跳转。"""
    if not isinstance(target, str) or not target or PureWindowsPath(target).anchor:
        raise ValueError("目标必须是根目录内的相对子目录")
    parts = re.split(r"[\\/]", target)
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((" ", ".")) or
                len(part.encode("utf-16-le")) // 2 > 255 or
                any(ord(ch) < 32 or ch in '<>:"|?*' for ch in part) or
                _RESERVED.fullmatch(part.split(".", 1)[0].rstrip(" "))):
            raise ValueError(f"无效的目标子目录：{target}")
    return Path(*parts)


_VARIABLE = re.compile(r"\{(year|month|ext)\}")


def render_subdir(template: str, variables: dict[str, str]) -> Path:
    if not isinstance(template, str) or len(template) > 1024:
        raise ValueError("目标模板须为不超过 1024 字符的文本")
    expanded = _VARIABLE.sub(lambda match: variables[match.group(1)], template)
    if "{" in expanded or "}" in expanded:
        raise ValueError("目标模板只支持 {year}、{month}、{ext}")
    return validate_subdir(expanded)


def validate_rule(rule: Rule) -> None:
    if (not isinstance(rule.name, str) or not rule.name or len(rule.name) > 80
            or rule.name != rule.name.strip() or not rule.name.isprintable()):
        raise ValueError("规则名称须为 1 至 80 个可显示字符，首尾不能有空白")
    if type(rule.enabled) is not bool:
        raise ValueError("规则启用状态须为 true 或 false")
    if not isinstance(rule.extensions, (list, tuple)) or len(rule.extensions) > 128:
        raise ValueError("扩展名应为不超过 128 项的列表")
    for ext in rule.extensions:
        if not isinstance(ext, str) or len(ext) > 40 or not re.fullmatch(r"(?:\.[\w+~-]+)+", ext):
            raise ValueError("扩展名无效，可使用 .pdf 或 .tar.gz")
    if (not isinstance(rule.name_glob, str) or len(rule.name_glob) > 255 or
            any(ord(c) < 32 or c in "/\\" for c in rule.name_glob)):
        raise ValueError("名称通配符只匹配文件名，不能含路径分隔符")
    if not isinstance(rule.source_subdir, str):
        raise ValueError("来源子目录应为相对路径文本")
    if rule.source_subdir:
        validate_subdir(rule.source_subdir)
    for limit in (rule.min_size, rule.max_size):
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("文件大小限制须为非负整数字节")
    if rule.min_size is not None and rule.max_size is not None and rule.min_size > rule.max_size:
        raise ValueError("最小文件大小不能超过最大文件大小")
    for day in (rule.modified_from, rule.modified_to):
        if not isinstance(day, str):
            raise ValueError("修改日期应为 YYYY-MM-DD 文本")
        if day:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except ValueError:
                raise ValueError("修改日期须为有效的 YYYY-MM-DD") from None
    if rule.modified_to and rule.modified_from > rule.modified_to:
        raise ValueError("起始修改日期不能晚于截止日期")
    if not (rule.extensions or rule.name_glob or rule.source_subdir or rule.min_size is not None
            or rule.max_size is not None or rule.modified_from or rule.modified_to):
        raise ValueError("请至少指定一个条件；使用名称通配符 * 可匹配所有文件")
    render_subdir(rule.target, {"year": "2000", "month": "01", "ext": "txt"})
