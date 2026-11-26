"""规则匹配、路径计算、批内冲突处理和执行前复核。所有步骤只读。"""
import hashlib
import json
import re
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, replace
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from threading import Event

from .core import (FileInfo, Plan, check_cancel, directory_identity, pin_snapshot_entry,
                   render_subdir, validate_subdir)


def _signature(rule):
    data = json.dumps(asdict(rule), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _match(rule, file, root, day):
    if not rule.enabled:
        return ""
    reasons = []
    name = file.path.name.casefold()
    if rule.extensions:
        ext = next((ext for ext in rule.extensions if name.endswith(ext.casefold())), None)
        if ext is None:
            return ""
        reasons.append(f"扩展名 {ext}")
    if rule.name_glob:
        if not fnmatchcase(name, rule.name_glob.casefold()):
            return ""
        reasons.append(f"名称匹配 {rule.name_glob}")
    if rule.source_subdir:
        subdir = validate_subdir(rule.source_subdir)
        if not file.path.parent.is_relative_to(root / subdir):
            return ""
        reasons.append(f"位于 {subdir}")
    if rule.min_size is not None and file.size < rule.min_size:
        return ""
    if rule.max_size is not None and file.size > rule.max_size:
        return ""
    if rule.min_size is not None or rule.max_size is not None:
        reasons.append(f"大小 {file.size} 字节符合范围")
    if rule.modified_from and day < rule.modified_from or rule.modified_to and day > rule.modified_to:
        return ""
    if rule.modified_from or rule.modified_to:
        reasons.append(f"修改日期 {day} 符合范围")
    return "、".join(reasons) or "匹配全部文件"


def _find_rule(file, root, rules):
    # Integer seconds preserve the local calendar day just before midnight.
    modified = datetime.fromtimestamp(file.mtime_ns // 1_000_000_000)
    day = modified.date().isoformat()
    for rule in rules:
        reason = _match(rule, file, root, day)
        if reason:
            return rule, reason, modified
    return None, "", modified


def _rule_folder(rule, archive, file, modified):
    return archive / render_subdir(rule.target, {
        "year": f"{modified.year:04d}", "month": f"{modified.month:02d}",
        "ext": file.path.suffix[1:].lower() or "无扩展名"})


class _PathInspection:
    def __init__(self, *roots):
        self.directories = {}
        self.names = {}
        for root in dict.fromkeys(roots):
            if ".." in root.parts:
                raise ValueError("所选根目录含无效的相对路径")
            with self._pinned_directory(root) as identity:
                self.directories[root] = identity

    @contextmanager
    def _pinned_directory(self, folder):
        # Lock from the volume root down. Only resolve a child after proving
        # its parent is an ordinary directory and keeping that parent pinned.
        with ExitStack() as stack:
            for path in (*reversed(folder.parents), folder):
                stack.enter_context(pin_snapshot_entry(path))
                identity = directory_identity(path)
                if path in self.directories and self.directories[path] != identity:
                    raise ValueError(f"目录在预览期间被替换：{path}")
                self.directories[path] = identity
            yield identity

    def _names_while_pinned(self, folder):
        if folder not in self.names:
            self.names[folder] = {p.name.casefold() for p in folder.iterdir()}
        return self.names[folder]

    def child_names(self, folder):
        if folder not in self.names:
            with self._pinned_directory(folder):
                return self._names_while_pinned(folder)
        return self.names[folder]

    def _inspect_child(self, path):
        with self._pinned_directory(path.parent):
            try:
                with pin_snapshot_entry(path):
                    return directory_identity(path)
            except FileNotFoundError:
                if path.name.casefold() in self._names_while_pinned(path.parent):
                    raise ValueError(f"目录名称大小写冲突：{path}")
                return None

    def chain(self, root, folder, allow_missing):
        if not folder.is_relative_to(root):
            raise ValueError("路径超出所选根目录")
        parts = folder.relative_to(root).parts
        if ".." in parts:
            raise ValueError("路径超出所选根目录")
        path = root
        result = [self.directories[root]]
        for part in parts:
            path = path / part
            if path not in self.directories:
                self.directories[path] = self._inspect_child(path)
            existing = self.directories[path]
            if existing is None:
                if allow_missing:
                    break
                raise ValueError(f"源目录已经不存在：{path}")
            result.append(existing)
        return tuple(result)

    def occupied(self, target):
        # Parent identities come from chain(); absent parents imply absent leafs.
        return (self.directories.get(target.parent) is not None
                and target.name.casefold() in self.child_names(target.parent))


def _filename(name):
    if not isinstance(name, str) or "/" in name or "\\" in name:
        raise ValueError("新文件名只能是一个文件名，不能包含目录")
    validate_subdir(name)
    return name


def _one_plan(file, root, archive, rules, signatures, resolution, inspection):
    strategy, manual = resolution
    p = Plan(file, None, "", "skip", "", source_root=root, target_root=archive,
             resolution=strategy)
    try:
        if not file.path.is_relative_to(root) or ".." in file.path.parts:
            raise ValueError("文件超出所选来源目录")
        source = inspection.chain(root, file.path.parent, False)
    except (ValueError, OSError) as exc:
        return replace(p, status="conflict", conflict_type="source", reason=str(exc))
    try:
        rule, reason, modified = _find_rule(file, root, rules)
        if rule is None:
            return replace(p, reason="没有启用的规则匹配此文件")
        p = replace(p, rule=rule.name, rule_signature=signatures[rule], reason=reason)
        folder = _rule_folder(rule, archive, file, modified)
        base = folder / _filename(file.path.name)
        p = replace(p, base_target=base, target=base)
        dirs = inspection.chain(archive, folder, True)
        p = replace(p, directories=tuple(dict.fromkeys((*source, *dirs))))
        if base == file.path:
            return replace(p, reason=f"{reason}；已经位于目标目录")
        if strategy not in ("skip", "number", "rename"):
            raise ValueError("未知冲突处理策略")
        if strategy == "rename":
            target = folder / _filename(manual)
            p = replace(p, target=target, reason=f"{reason}；手动命名")
        return replace(p, status="ready")
    except (OSError, ValueError, OverflowError) as exc:
        return replace(p, status="conflict", conflict_type="unsafe", reason=str(exc))


def preview(root: Path, files, rules, cancel: Event | None = None, *,
            target_root=None, resolutions=None) -> list[Plan]:
    root = Path(root).absolute()
    archive = Path(target_root).absolute() if target_root is not None else root
    rules = tuple(rules)
    signatures = {rule: _signature(rule) for rule in rules}
    inspection = _PathInspection(root, archive)
    plans = []
    for file in files:
        check_cancel(cancel)
        choice = (resolutions or {}).get(file.path, ("skip", ""))
        plans.append(_one_plan(file, root, archive, rules, signatures, choice, inspection))

    # Reserve explicit destinations before allocating numbers. File-system and
    # batch names share one case-insensitive namespace, including manual names.
    counts = Counter(str(p.target).casefold() for p in plans if p.status == "ready")
    reserved = set(counts)
    next_number = {}
    for i in sorted(range(len(plans)), key=lambda i: str(plans[i].file.path).casefold()):
        check_cancel(cancel)
        p = plans[i]
        if p.status != "ready" or p.resolution != "number":
            continue
        base = p.target
        key = str(base).casefold()
        try:
            if counts[key] == 1 and not inspection.occupied(base):
                continue
            number = next_number.get(key, 1)
            while True:
                check_cancel(cancel)
                name = _filename(f"{base.stem} ({number}){base.suffix}")
                target = base.with_name(name)
                number += 1
                if str(target).casefold() not in reserved and not inspection.occupied(target):
                    break
            next_number[key] = number
            reserved.add(str(target).casefold())
            plans[i] = replace(p, target=target, reason=f"{p.reason}；同名自动编号")
        except (OSError, ValueError) as exc:
            plans[i] = replace(p, status="conflict", conflict_type="unsafe", reason=str(exc))

    counts = Counter(str(p.target).casefold() for p in plans if p.status == "ready")
    for i, p in enumerate(plans):
        check_cancel(cancel)
        if p.status != "ready":
            continue
        try:
            if counts[str(p.target).casefold()] > 1:
                plans[i] = replace(p, status="conflict", conflict_type="batch",
                                   reason=f"{p.reason}；本批多个文件将移到同一目标位置")
            elif inspection.occupied(p.target):
                plans[i] = replace(p, status="conflict", conflict_type="existing",
                                   reason=f"{p.reason}；目标位置已有同名文件或目录")
        except (OSError, ValueError) as exc:
            plans[i] = replace(p, status="conflict", conflict_type="unsafe", reason=str(exc))
    return plans


def rule_is_current(plan, rules) -> bool:
    if (plan.source_root is None or plan.target_root is None or plan.target is None
            or not isinstance(plan.file, FileInfo) or not plan.file.path.is_relative_to(plan.source_root)
            or ".." in plan.file.path.parts):
        return False
    if plan.kind == "quarantine":
        if (not isinstance(plan.keeper, FileInfo) or plan.keeper.path == plan.file.path
                or not plan.keeper.path.is_relative_to(plan.source_root)
                or ".." in plan.keeper.path.parts or plan.keeper.size != plan.file.size
                or not isinstance(plan.expected_hash, str)
                or not re.fullmatch("[0-9a-f]{64}", plan.expected_hash)):
            return False
        folder = plan.target_root / "隔离区"
    elif plan.kind == "archive":
        rule, _, modified = _find_rule(plan.file, plan.source_root, rules)
        if rule is None or _signature(rule) != plan.rule_signature:
            return False
        folder = _rule_folder(rule, plan.target_root, plan.file, modified)
    else:
        return False
    return (plan.base_target == folder / plan.file.path.name and plan.target.parent == folder
            and plan.resolution in ("skip", "rename", "number")
            and (plan.resolution != "skip" or plan.base_target == plan.target))


def recheck_plans(plans, rules, cancel=None):
    from .scanner import snapshot_file

    result = []
    rules = tuple(rules)
    inspections = {}
    for p in plans:
        check_cancel(cancel)
        if p.status == "ready":
            try:
                if not rule_is_current(p, rules):
                    raise ValueError("命中规则发生变化，请重新预览")
                roots = (p.source_root, p.target_root)
                if roots not in inspections:
                    inspections[roots] = _PathInspection(*roots)
                inspection = inspections[roots]
                source = inspection.chain(p.source_root, p.file.path.parent, False)
                destination = inspection.chain(p.target_root, p.target.parent, True)
                keeper_dirs = (inspection.chain(p.source_root, p.keeper.path.parent, False)
                               if p.kind == "quarantine" else ())
                if set((*source, *destination, *keeper_dirs)) != set(p.directories):
                    raise ValueError("来源或目标目录结构已经改变")
                if inspection.occupied(p.target):
                    raise ValueError("目标已被占用")
                current = snapshot_file(p.file.path, cancel=cancel)
                if current != p.file:
                    raise ValueError("源文件自预览后已更改或被替换")
                if p.kind == "quarantine" and snapshot_file(p.keeper.path, cancel=cancel) != p.keeper:
                    raise ValueError("重复组的保留文件已更改，请重新检查重复件")
            except (ValueError, OSError, OverflowError, TypeError) as exc:
                p = replace(p, status="review", conflict_type="review", reason=str(exc))
        result.append(p)
    return result
