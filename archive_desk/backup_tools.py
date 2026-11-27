"""Backup settings, reporting, and repository maintenance."""
import csv
import hashlib
import os
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from . import backup_format as fmt, backup_writer as writer
from ._winmove import open_for_move, open_regular, rename_without_replace
from .core import check_cancel
from .file_revision import content_digest
from .journal import database_connection
from .move_checks import require_vacant


class BackupPresets:
    def __init__(self, db_path):
        self.path = db_path
        with database_connection(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS backup_presets(
                       name TEXT PRIMARY KEY NOT NULL, root_id INTEGER NOT NULL,
                       target TEXT NOT NULL, FOREIGN KEY(root_id) REFERENCES catalog_roots(id))""")

    def save(self, name, root_id, target):
        name = name.strip()
        if not name:
            raise ValueError("请输入模板名称")
        with database_connection(self.path) as db:
            db.execute("INSERT INTO backup_presets VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE "
                       "SET root_id=excluded.root_id,target=excluded.target", (name, root_id, str(target)))

    def list(self):
        with database_connection(self.path) as db:
            return [dict(row) for row in db.execute("SELECT * FROM backup_presets ORDER BY name")]


def search_version(manifest, query="", extension="", start="", end=""):
    since = date.fromisoformat(start) if start else date.min
    until = date.fromisoformat(end) if end else date.max
    if since > until:
        raise ValueError("起始日期晚于截止日期")
    needle = query.replace("\\", "/").casefold()
    suffix = "." + extension.lower().lstrip(".") if extension else ""
    for row in manifest["files"]:
        path = row["path"]
        if needle not in path.casefold() or (suffix and PurePosixPath(path).suffix.casefold() != suffix):
            continue
        if start or end:
            try:
                modified = datetime.fromtimestamp(row["mtime_ns"] / 1_000_000_000).date()
            except (OSError, OverflowError, ValueError):
                continue
            if not since <= modified <= until:
                continue
        yield row


def compare_versions(older, newer):
    previous = {row["path"]: row for row in older["files"]}
    current = {row["path"]: row for row in newer["files"]}
    for path in sorted(previous.keys() | current.keys()):
        if path not in previous:
            action = "新增"
        elif path not in current:
            action = "新版未收录" if newer["status"] == "incomplete" else "删除"
        elif previous[path]["sha256"] != current[path]["sha256"]:
            action = "内容修改"
        elif previous[path]["mtime_ns"] != current[path]["mtime_ns"]:
            action = "修改时间变化"
        else:
            continue
        yield action, path


def retired_versions(repo):
    with ExitStack() as stack:
        fmt.pin_path(stack, repo / "versions")
        fmt.read_marker(repo)
        return sorted(p.stem for p in (repo / "versions").glob("*.retired")
                      if fmt._ID.fullmatch(p.stem))


def set_retired(repo, version, *, retired):
    fmt._id(version)
    with ExitStack() as stack:
        fmt.pin_path(stack, repo / "versions")
        stack.enter_context(writer._writer_lock(repo))
        manifest = fmt.read_manifest(repo, version, retired=not retired)
        old, new = (".json", ".retired") if retired else (".retired", ".json")
        source = repo / "versions" / (version + old)
        target = source.with_suffix(new)
        stream, handle, stat = stack.enter_context(open_for_move(source))
        if stat.st_nlink != 1:
            raise ValueError("版本清单具有多个硬链接")
        raw = stream.read(fmt.MAX_MANIFEST_BYTES + 1)
        digest = hashlib.sha256(raw).hexdigest().encode("ascii")
        checksum = fmt._read(source.with_suffix(".sha256"), 80).strip()
        if digest != checksum or fmt.validate_manifest(fmt._json(raw), version) != manifest:
            raise ValueError("版本清单已变化")
        require_vacant(target)
        rename_without_replace(handle, target)


def _copy_object(source, target, digest, size, cancel):
    with ExitStack() as stack:
        fmt.pin_path(stack, source.parent)
        writer._directory(stack, target.parent)
        stream, _, info = stack.enter_context(open_regular(source))
        if info.st_size != size:
            raise ValueError(f"对象大小不符 {digest}")
        with writer._stage(target.parent) as stage:
            while block := stream.read(writer.BLOCK):
                check_cancel(cancel)
                if stage.stream.write(block) != len(block):
                    raise OSError("内容对象写入不完整")
            os.fsync(stage.stream.fileno())
            if content_digest(stage.stream, cancel) != digest:
                raise ValueError(f"对象 SHA-256 不符 {digest}")
            check_cancel(cancel)
            stage.commit(target)


def copy_repository(repo, target, *, cancel=None, progress=None):
    with ExitStack() as stack:
        fmt.pin_path(stack, repo / "versions")
        fmt.pin_path(stack, target)
        fmt.assert_separate(repo, target)
        fmt.read_marker(repo)
        stack.enter_context(writer._writer_lock(repo))
        if any(target.iterdir()):
            raise ValueError("请为仓库副本选择一个空目录")
        stack.enter_context(writer._writer_lock(target))
        if {p.name for p in target.iterdir()} != {writer.LOCK_NAME}:
            raise ValueError("目标目录内容已变化")
        marker = fmt._read(repo / "repository.json", 8192)
        fmt.read_marker(repo)
        versions = [(row["id"], False) for row in fmt.list_versions(repo, cancel=cancel)]
        versions.extend((vid, True) for vid in retired_versions(repo))
        objects = {}
        for vid, retired in versions:
            check_cancel(cancel)
            for row in fmt.read_manifest(repo, vid, retired=retired)["files"]:
                key, size = row["sha256"], row["size"]
                if objects.setdefault(key, size) != size:
                    raise ValueError("版本清单中的共享对象大小不一致")
        for name in ("tmp", "objects", "versions"):
            writer._directory(stack, target / name)
        for i, (digest, size) in enumerate(objects.items()):
            check_cancel(cancel)
            if progress:
                progress(i, len(objects), digest)
            _copy_object(fmt.object_path(repo, digest), fmt.object_path(target, digest), digest, size, cancel)
        for vid, retired in versions:
            check_cancel(cancel)
            suffix = ".retired" if retired else ".json"
            path = Path("versions") / (vid + suffix)
            raw = fmt._read(repo / path, fmt.MAX_MANIFEST_BYTES)
            manifest = fmt.read_manifest(repo, vid, retired=retired)
            if fmt.validate_manifest(fmt._json(raw), vid) != manifest:
                raise ValueError("复制期间版本清单发生变化")
            for row in manifest["files"]:
                if objects.get(row["sha256"]) != row["size"]:
                    raise ValueError("复制期间出现新的对象引用")
            checksum = (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii")
            writer._publish_bytes(target / "tmp", (target / path).with_suffix(".sha256"), checksum)
            writer._publish_bytes(target / "tmp", target / path, raw)
        check_cancel(cancel)
        writer._publish_bytes(target / "tmp", target / "repository.json", marker, final=True)
        return len(versions), len(objects)


def export_csv(path, headings, rows):
    def safe(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        table = csv.writer(stream)
        table.writerow(headings)
        table.writerows(tuple(safe(value) for value in row) for row in rows)


def export_restore_plan(path, plan):
    export_csv(path, ("版本", "版本路径", "实际目标", "操作", "原因", "字节数"),
               ((plan.version_id, e.path, str(e.target), e.action, e.reason, e.size)
                for e in plan.entries))
