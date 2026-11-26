"""把用户审查过的重复组编入可撤销的移动计划。"""
from dataclasses import replace

from .core import Rule, check_cancel, directory_identity
from .planning import preview


def quarantine_preview(root, archive, groups, cancel=None, *, resolutions=None):
    reviewed = {}
    keeper_dirs = {}
    keepers = {keeper.path for keeper, _, _ in groups}
    for keeper, copies, checksum in groups:
        check_cancel(cancel)
        if not keeper.path.is_relative_to(root) or ".." in keeper.path.parts:
            raise ValueError("保留项位于来源目录之外")
        for folder in keeper.path.parents:
            if folder not in keeper_dirs:
                keeper_dirs[folder] = directory_identity(folder)
            if folder == root:
                break
        for file in copies:
            if file.path in keepers or file.path in reviewed:
                raise ValueError("同一文件被重复列入隔离计划，或已标记为保留项")
            reviewed[file.path] = (file, keeper, checksum)
    rule = Rule("重复件隔离", (), "隔离区", name_glob="*")
    plans = preview(root, [entry[0] for entry in reviewed.values()], [rule], cancel,
                    target_root=archive, resolutions=resolutions)
    result = []
    for plan in plans:
        _, keeper, checksum = reviewed[plan.file.path]
        directories = list(plan.directories)
        directories.extend(keeper_dirs[d] for d in keeper.path.parents if d.is_relative_to(root))
        result.append(replace(plan, kind="quarantine", keeper=keeper, expected_hash=checksum,
                              directories=tuple(dict.fromkeys(directories)),
                              reason=f"保留 {keeper.path}。SHA-256 相同。{plan.reason}"))
    return result
