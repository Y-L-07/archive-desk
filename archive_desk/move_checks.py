"""Checks shared by forward moves, undo and read-only recovery inspection."""
import json
from pathlib import Path

from ._winmove import case_sensitive_directory, open_regular, pin_directory
from .core import DirIdentity, FileInfo, Plan, change_time_ns, validate_subdir
from .file_revision import content_digest, revision_matches


def check_path(root, path):
    if (not root.is_absolute() or ".." in root.parts or path == root
            or any(str(p).startswith(("\\\\.\\", "\\\\?\\")) for p in (root, path))):
        raise ValueError("根目录或文件路径无效")
    if not path.is_relative_to(root):
        raise ValueError("路径超出授权根目录")
    validate_subdir(str(path.relative_to(root)))


class Directories:
    def __init__(self, stack, expected, created):
        self.stack = stack
        self.expected = expected
        self.created = created
        self.held = {}

    def pin(self, root, folder, allow_create=False, inspect_missing=False):
        for path in (*reversed(folder.parents), folder):
            if path in self.held:
                continue
            known = self.expected.get(path, self.created.get(path))
            if path.is_relative_to(root) and known is None:
                if not allow_create or path == root:
                    raise ValueError(f"缺少目录的预览记录：{path}")
                require_vacant(path)
                if inspect_missing:
                    return False
                try:
                    path.mkdir()
                except FileExistsError:
                    raise ValueError(f"预览后目标目录已出现：{path}") from None
            identity = self.stack.enter_context(pin_directory(path))
            if known is not None and tuple(known) != identity:
                raise ValueError(f"目录被替换：{path}")
            self.held[path] = identity
            if path.is_relative_to(root) and known is None:
                self.created[path] = identity
        return True

    def evidence(self):
        return [[str(path), *identity] for path, identity in self.held.items()]


def fingerprint(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def check_snapshot(file, opened, digest, cancel):
    stream, handle, info = opened
    if (fingerprint(info) != [file.dev, file.ino, file.size, file.mtime_ns]
            or file.change_ns is not None and change_time_ns(handle) != file.change_ns
            or not revision_matches(file, stream, handle, digest=digest, cancel=cancel)):
        raise ValueError("文件在预览后被修改或替换")


def require_vacant(path):
    try:
        path.lstat()
    except FileNotFoundError:
        if not case_sensitive_directory(path.parent):
            return
        if all(child.name.casefold() != path.name.casefold() for child in path.parent.iterdir()):
            return
    raise ValueError(f"位置已被占用：{path}")


def decode_plan(text):
    raw = json.loads(text)
    def file(data):
        return FileInfo(**dict(data, path=Path(data["path"])))
    raw["file"] = file(raw["file"])
    if raw.get("keeper"):
        raw["keeper"] = file(raw["keeper"])
    raw["directories"] = tuple(DirIdentity(Path(d["path"]), d["dev"], d["ino"])
                               for d in raw["directories"])
    for key in ("target", "base_target", "source_root", "target_root"):
        if raw.get(key):
            raw[key] = Path(raw[key])
    return Plan(**raw)


def check_keeper(stack, dirs, plan, cancel=None):
    if plan.kind != "quarantine":
        return
    keeper = plan.keeper
    if keeper is None or keeper.path in (plan.file.path, plan.target):
        raise ValueError("保留项与隔离计划冲突")
    check_path(plan.source_root, keeper.path)
    dirs.pin(plan.source_root, keeper.path.parent)
    opened = stack.enter_context(open_regular(keeper.path))
    digest = content_digest(opened[0], cancel)
    check_snapshot(keeper, opened, digest, cancel)
    if digest != plan.expected_hash:
        raise ValueError("保留文件的内容与确认时不一致")
