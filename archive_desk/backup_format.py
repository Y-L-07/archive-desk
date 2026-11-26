"""ArchiveDesk content repository v1. All reading and verification work without SQLite."""
import hashlib
import json
import os
import re
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from ._winmove import open_regular
from .backup_paths import assert_separate, disk_note, pin_path
from .core import check_cancel, validate_subdir
from .file_revision import content_digest

FORMAT = 1
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_ID = re.compile("[0-9a-f]{32}")
_HASH = re.compile("[0-9a-f]{64}")


def check_relative(value):
    if not isinstance(value, str) or "\\" in value or len(value) > 32760:
        raise ValueError("清单路径须为使用 / 分隔的库内相对路径")
    validate_subdir(value)
    return value


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("仓库或版本编号无效")
    return value


def object_path(repo, digest):
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("内容对象的 SHA-256 无效")
    return Path(repo) / "objects" / digest[:2] / digest


def _read(path, limit):
    with open_regular(path) as (stream, _, info):
        if info.st_size > limit:
            raise ValueError(f"超出格式大小限制：{path.name}")
        data = stream.read(info.st_size + 1)
        if len(data) != info.st_size or len(data) > limit:
            raise ValueError("读取时文件长度发生变化")
        return data


def _json(raw):
    def fields(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("JSON 含重复字段")
        return value
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=fields)
    except (UnicodeError, RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("仓库 JSON 不是有效 UTF-8 文档") from exc
    if not isinstance(value, dict):
        raise ValueError("仓库 JSON 根节点应为对象")
    return value


def read_marker(repo):
    with ExitStack() as stack:
        pin_path(stack, repo)
        data = _json(_read(Path(repo) / "repository.json", 8192))
    if (data.get("format") != "ArchiveDesk" or type(data.get("version")) is not int
            or data["version"] != FORMAT):
        raise ValueError("不支持的备份仓库格式或版本")
    _id(data.get("repository_id"))
    return data


def validate_manifest(data, expected_id):
    if (not isinstance(data, dict) or type(data.get("format")) is not int
            or data["format"] != FORMAT or _id(data.get("id")) != _id(expected_id)):
        raise ValueError("版本清单格式或编号不匹配")
    source = data.get("source_name")
    date = data.get("created_at")
    if not isinstance(source, str) or not source.strip() or not source.isprintable():
        raise ValueError("版本来源名称无效")
    if not isinstance(date, str) or datetime.fromisoformat(date).utcoffset() is None:
        raise ValueError("备份时间必须包含时区")
    if data.get("status") not in ("complete", "incomplete"):
        raise ValueError("备份完成状态无效")
    for key in ("files", "directories", "issues"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"版本清单缺少有效的 {key} 列表")
    if bool(data["issues"]) != (data["status"] == "incomplete"):
        raise ValueError("版本状态与记录的问题不一致")
    seen = {}
    for path in data["directories"]:
        key = check_relative(path).casefold()
        if key in seen:
            raise ValueError(f"重复或大小写冲突的目录：{path}")
        seen[key] = (path, True)
    for file in data["files"]:
        if not isinstance(file, dict):
            raise ValueError("版本文件记录无效")
        path = check_relative(file.get("path"))
        if path.casefold() in seen:
            raise ValueError(f"重复或大小写冲突的路径：{path}")
        seen[path.casefold()] = (path, False)
        for key in ("size", "mtime_ns"):
            number = file.get(key)
            if type(number) is not int or (key == "size" and not 0 <= number < 2**63):
                raise ValueError(f"文件大小或修改时间无效：{path}")
        if not -11644473600000000000 <= file["mtime_ns"] < (2**63-1)*100-11644473600000000000:
            raise ValueError(f"文件修改时间超出 Windows 可表示范围：{path}")
        object_path(".", file.get("sha256"))
    for path, _ in seen.values():
        parent, sep, _ = path.rpartition("/")
        if sep and seen.get(parent.casefold()) != (parent, True):
            raise ValueError(f"父目录缺失、大小写冲突或被文件占用：{path}")
    for issue in data["issues"]:
        if (not isinstance(issue, dict) or not isinstance(issue.get("path"), str)
                or not isinstance(issue.get("reason"), str) or not issue["reason"].strip()):
            raise ValueError("不完整版本缺少可读的问题记录")
        # The reason is display-only text. Issue paths are never restore targets.
        if issue["path"] not in ("", "."):
            check_relative(issue["path"])
    return data


def read_manifest(repo, version_id):
    version_id = _id(version_id)
    repo = Path(repo)
    with ExitStack() as stack:
        pin_path(stack, repo / "versions")
        read_marker(repo)
        document = _read(repo / "versions" / (version_id + ".json"), MAX_MANIFEST_BYTES)
        checksum = _read(repo / "versions" / (version_id + ".sha256"), 80)
        if checksum.strip() != hashlib.sha256(document).hexdigest().encode("ascii"):
            raise ValueError("版本清单 SHA-256 校验不符")
        return validate_manifest(_json(document), version_id)


def _version_ids(repo):
    # A .sha256 without its final .json is an unpublished transaction.
    with os.scandir(Path(repo) / "versions") as entries:
        return sorted(e.name[:-5] for e in entries if e.name.lower().endswith(".json"))


def list_versions(repo, *, cancel=None):
    versions = []
    with ExitStack() as stack:
        read_marker(repo)
        pin_path(stack, Path(repo) / "versions")
        for version_id in _version_ids(repo):
            check_cancel(cancel)
            try:
                data = read_manifest(repo, version_id)
                versions.append(dict(id=version_id, created_at=data["created_at"],
                    source_name=data["source_name"], status=data["status"], files=len(data["files"]),
                    file_bytes=sum(file["size"] for file in data["files"])))
            except (OSError, ValueError) as exc:
                versions.append(dict(id=version_id, created_at="", source_name="", status="damaged",
                                     files=0, file_bytes=0, error=str(exc)))
    return sorted(versions, key=lambda r: (r["created_at"], r["id"]), reverse=True)


def verify_repository(repo, *, cancel=None, progress=None):
    """Read each referenced content object once, attribute failures to all references."""
    refs = defaultdict(list)
    issues, incomplete = [], []
    repo = Path(repo)
    with ExitStack() as stack:
        read_marker(repo)
        pin_path(stack, repo / "versions")
        ids = _version_ids(repo)
        for n, version_id in enumerate(ids):
            check_cancel(cancel)
            if progress:
                progress(n, len(ids), "核对清单 " + version_id)
            try:
                manifest = read_manifest(repo, version_id)
            except (ValueError, OSError) as exc:
                issues.append(dict(kind="manifest", version=version_id, path="", reason=str(exc)))
                continue
            if manifest["status"] == "incomplete":
                incomplete.append(version_id)
            for file in manifest["files"]:
                refs[file["sha256"]].append((version_id, file["path"], file["size"]))
        for n, (digest, files) in enumerate(refs.items()):
            check_cancel(cancel)
            path = object_path(repo, digest)
            if progress:
                progress(n, len(refs), files[0][1])
            problem, reason, size = "", "", None
            try:
                with ExitStack() as obj:
                    pin_path(obj, path.parent)
                    stream, _, info = obj.enter_context(open_regular(path))
                    size = info.st_size
                    if content_digest(stream, cancel) != digest:
                        problem, reason = "hash", "对象 SHA-256 不符"
            except FileNotFoundError:
                problem, reason = "missing", "内容对象缺失"
            except (OSError, ValueError) as exc:
                problem, reason = "unreadable", str(exc)
            for version_id, rel, expected in files:
                if problem or size != expected:
                    issues.append(dict(kind=problem or "manifest", version=version_id, path=rel,
                                       reason=reason or "清单中的大小与已校验对象不一致"))
        check_cancel(cancel)
        if progress:
            progress(len(refs), len(refs), "校验结束")
    return dict(versions=len(ids), objects=len(refs), issues=issues, incomplete=incomplete)
