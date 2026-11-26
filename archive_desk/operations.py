"""Apply, undo and explicitly resume checked plans; all paths go through the same engine."""
import json
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from . import crossvolume
from ._winmove import open_for_move, open_regular
from .core import ScanCancelled, check_cancel, default_rules
from .file_revision import content_digest
from .move_checks import (Directories, check_keeper, check_path, check_snapshot, decode_plan,
                          fingerprint, require_vacant)
from .planning import rule_is_current


def _reason(exc):
    label = {2: "文件已经不存在", 3: "父目录已经不存在", 5: "权限不足或文件受保护",
             32: "文件或目录正被其他程序占用", 33: "文件被锁定",
             80: "目标名称已被占用", 183: "目标名称已被占用"}.get(getattr(exc, "winerror", None))
    return f"{label}（Windows {exc.winerror}）" if label else str(exc)


def _expected(data):
    return {Path(p): (dev, ino) for p, dev, ino in data["directories"]}


def _run(item, journal, *, direction, rules, created, cancel, recovery=False, inspect=False):
    plan = decode_plan(item["intent"]) if item.get("intent") else None
    undoing = direction == "undo"
    source, target = Path(item["source"]), Path(item["target"])
    root, archive = Path(item["root"]), Path(item["target_root"] or item["root"])
    if plan is not None and (plan.file.path != source or plan.target != target or
                             plan.source_root != root or plan.target_root != archive):
        raise ValueError("执行路径与已确认的整理计划不一致")
    if undoing:
        source, target, root, archive = target, source, archive, root
    check_path(root, source)
    check_path(archive, target)
    if source == target:
        raise ValueError("源文件与目标相同")
    data = json.loads(item["transfer"]) if recovery else {}
    evidence = json.loads(item["evidence"]) if undoing and item.get("evidence") else {}
    if recovery:
        expected = _expected(data)
    elif undoing:
        expected = _expected(evidence)
    else:
        expected = {d.path: (d.dev, d.ino) for d in plan.directories}
    job = crossvolume.Transfer(journal, item["id"], item["status"], direction,
                               item.get("phase") or "", data)
    with ExitStack() as stack:
        dirs = Directories(stack, expected, created)
        dirs.pin(root, source.parent)
        if not undoing and rules is not None:
            if plan is None or not rule_is_current(plan, rules):
                raise ValueError("规则或目标路径已变化，请重新预览")
        if plan and not undoing:
            check_keeper(stack, dirs, plan, cancel)
        source_stack = stack.enter_context(ExitStack())
        try:
            opened = source_stack.enter_context((open_regular if inspect else open_for_move)(source))
        except FileNotFoundError:
            if not recovery:
                raise
            opened = None
        if opened:
            digest = content_digest(opened[0], cancel)
            if recovery and (fingerprint(opened[2]) != data["file"] or digest != data["sha256"]):
                raise ValueError("源文件与操作意图中记录的身份、时间或哈希不一致")
            if undoing:
                if fingerprint(opened[2]) != evidence["file"] or digest != evidence["sha256"]:
                    raise ValueError("归档文件的身份、内容或修改时间已变化")
            elif plan:
                check_snapshot(plan.file, opened, digest, cancel)
                if plan.kind == "quarantine" and digest != plan.expected_hash:
                    raise ValueError("待隔离文件的内容与保留项不一致")
        exists = dirs.pin(archive, target.parent, allow_create=not recovery and not undoing,
                          inspect_missing=inspect and not recovery)
        if not recovery:
            if exists:
                require_vacant(target)
            if inspect:
                return "未开工。源文件、目标路径与预览记录一致，可继续执行"
            check_cancel(cancel)
            crossvolume.start(job, source, target, opened, dirs, digest,
                              cross=opened[2].st_dev != dirs.held[target.parent][0])
        return crossvolume.advance(job, source, target, opened, source_stack.close, stack,
                                   cancel, inspect=inspect)


def inspect_interrupted(item, rules=None, created=None):
    try:
        if item["intent"] and item["direction"] != "undo" and rules is not None:
            plan = decode_plan(item["intent"])
            if plan.status != "ready":
                return ("review" if plan.status == "review" else "skipped"), plan.reason
            if not rule_is_current(plan, rules):
                return "review", "命中规则已变化，请核对来源、临时副本及目标后重新预览"
        reason = _run(item, None, direction=item["direction"] or "apply", rules=None,
                      created=created or {}, cancel=None, recovery=bool(item["transfer"]), inspect=True)
        return "resumable", reason
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return ("uncertain" if item["transfer"] else "review"), f"请人工核对：{_reason(exc)}"


def _run_item(item, journal, direction, rules, created, cancel, recovery):
    try:
        _run(item, journal, direction=direction, rules=rules, created=created,
             cancel=cancel, recovery=recovery and bool(item["transfer"]))
        return "undone" if direction == "undo" else "moved"
    except (OSError, ValueError, ScanCancelled, KeyError, TypeError) as exc:
        # Re-read the committed phase only on failure; never guess which action
        # finished when a staging file or a submitted destination already exists.
        current = journal.item(item["id"])
        status = current["status"]
        if status in ("applying", "undoing", "resumable") and current["transfer"]:
            transfer = json.loads(current["transfer"])
            if isinstance(exc, ScanCancelled) and transfer["mode"] == "copy":
                journal.transition(item["id"], status, "resumable", "已暂停，保留副本；继续前将重新核验")
                return "resumable"
            if (transfer["mode"] == "copy" or recovery):
                new, reason = inspect_interrupted(current, rules, created)
                if recovery and isinstance(exc, ValueError) and new == "resumable":
                    new = "uncertain"
                journal.transition(item["id"], status, new, f"{_reason(exc)}。{reason}")
                return new
        if direction == "undo":
            cancelled = isinstance(exc, ScanCancelled)
            journal.transition(item["id"], status, "moved", f"撤销跳过：{_reason(exc)}",
                               conflict=not cancelled)
            return "cancelled" if cancelled else "skipped"
        if isinstance(exc, ScanCancelled):
            new = "cancelled"
        elif (isinstance(exc, (ValueError, FileNotFoundError)) or
              getattr(exc, "winerror", None) in (80, 183)):
            new = "review"
        else:
            new = "failed"
        journal.transition(item["id"], status, new, _reason(exc))
        return new


def _batch(items, journal, *, mode, cancel, progress, stats, rules):
    created = journal.known_directories(items[0]["batch_id"]) if mode == "resume" and items else {}
    counts = Counter()
    total = len(items)
    source_set = {row["source"].casefold() for row in items}
    for i, item in enumerate(items):
        name = Path(item["source"]).name
        if progress:
            progress(i, total, name)
        if cancel is not None and cancel.is_set():
            if mode == "apply":
                journal.cancel_pending(item["batch_id"])
            counts["cancelled"] += total - i
            break
        direction = (item["direction"] or "apply") if mode == "resume" else mode
        if mode == "apply":
            plan = decode_plan(item["intent"])
            if plan.status != "ready":
                result = "review" if plan.status == "review" else "skipped"
                journal.transition(item["id"], "pending", result, plan.reason)
                counts[result] += 1
                continue
            if plan.keeper and str(plan.keeper.path).casefold() in source_set:
                journal.transition(item["id"], "pending", "review", "保留文件也在本次移动项中")
                counts["review"] += 1
                continue
        result = _run_item(item, journal, direction, rules, created, cancel, mode == "resume")
        counts[result] += 1
        if stats:
            stats(dict(counts))
    if stats:
        stats(dict(counts))
    if progress:
        progress(total, total, "")


def apply(root, plans, journal, *, cancel=None, progress=None, stats=None, rules=None, target_root=None):
    root = Path(root).absolute()
    archive = Path(target_root).absolute() if target_root is not None else root
    batch_id, _ = journal.begin_batch(root, plans, archive)
    _batch(journal.items(batch_id), journal, mode="apply", cancel=cancel, progress=progress,
           stats=stats, rules=tuple(default_rules() if rules is None else rules))
    return batch_id


def undo(batch_id, journal, *, cancel=None, progress=None, stats=None):
    items = [i for i in reversed(journal.items(batch_id)) if i["status"] == "moved"]
    _batch(items, journal, mode="undo", cancel=cancel, progress=progress, stats=stats, rules=())


def resume(batch_id, journal, *, cancel=None, progress=None, stats=None, rules=None, item_ids=None):
    allowed = set(item_ids) if item_ids is not None else None
    items = [i for i in journal.items(batch_id) if i["status"] == "resumable" and
             (allowed is None or i["id"] in allowed)]
    _batch(items, journal, mode="resume", cancel=cancel, progress=progress, stats=stats,
           rules=tuple(default_rules() if rules is None else rules))
