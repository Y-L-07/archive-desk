"""资料库已发布清单的命名快照和基于证据的只读比较。"""
import csv
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from .core import check_cancel
from .journal import database_connection


STATUS_NAMES = {"added": "新增", "missing": "缺失", "changed": "内容变更",
                "moved": "疑似移动或改名", "uncertain": "无法确认"}


@contextmanager
def _cancelable(db, cancel):
    check_cancel(cancel)
    if cancel is not None:
        db.set_progress_handler(lambda: int(cancel.is_set()), 8000)
    try:
        yield
        check_cancel(cancel)
    except sqlite3.OperationalError:
        check_cancel(cancel)
        raise
    finally:
        db.set_progress_handler(None, 0)


class SnapshotStore:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with database_connection(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS catalog_snapshots(
                    id INTEGER PRIMARY KEY, root_id INTEGER NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE, created_at TEXT NOT NULL,
                    indexed_at TEXT NOT NULL, generation INTEGER NOT NULL, scan_state TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'current',
                    file_count INTEGER NOT NULL DEFAULT 0, issue_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(root_id,name), FOREIGN KEY(root_id) REFERENCES catalog_roots(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS catalog_snapshot_files(
                    snapshot_id INTEGER NOT NULL REFERENCES catalog_snapshots(id) ON DELETE CASCADE,
                    relpath TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                    digest TEXT, stale INTEGER NOT NULL, PRIMARY KEY(snapshot_id,relpath)
                ) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS catalog_snap_hash ON catalog_snapshot_files(snapshot_id,digest);
                CREATE TABLE IF NOT EXISTS catalog_snapshot_issues(
                    snapshot_id INTEGER NOT NULL REFERENCES catalog_snapshots(id) ON DELETE CASCADE,
                    relpath TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS catalog_snap_issue ON catalog_snapshot_issues(snapshot_id);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "source" not in {r["name"] for r in db.execute("PRAGMA table_info(catalog_snapshots)")}:
                db.execute("ALTER TABLE catalog_snapshots ADD COLUMN source TEXT NOT NULL DEFAULT 'current'")

    def sources(self, root_id):
        with database_connection(self.path) as db:
            db.execute("BEGIN")
            return self._sources(db, root_id)

    @staticmethod
    def _sources(db, root_id):
        root = db.execute("SELECT * FROM catalog_roots WHERE id=?", (root_id,)).fetchone()
        if root is None:
            raise ValueError("资料库不存在")
        choices = []
        if root["indexed_at"] and root["published_state"] in ("complete", "partial"):
            choices.append(dict(source="current", indexed_at=root["indexed_at"],
                                generation=root["generation"], revision=root["revision"],
                                scan_state=root["published_state"]))
        # Older databases can carry a timestamp without the corresponding
        # manifest. Only an actual retained header proves a baseline exists.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_complete_baselines'").fetchone():
            baseline = db.execute("SELECT * FROM catalog_complete_baselines WHERE root_id=?", (root_id,)).fetchone()
            if baseline:
                choices.append(dict(source="last_complete", indexed_at=baseline["indexed_at"],
                                    generation=baseline["generation"], scan_state="complete", revision=None))
        return choices

    def create(self, root_id, name, cancel=None, *, source="current", expected=None):
        name = name.strip() if isinstance(name, str) else ""
        if not name or len(name) > 100 or not name.isprintable():
            raise ValueError("快照名称须为 1 至 100 个可显示字符")
        with database_connection(self.path) as db, _cancelable(db, cancel):
            # Refresh publishes one generation atomically. Take its header, live
            # rows, retained blind-region rows, and issue records in one transaction.
            db.execute("BEGIN IMMEDIATE")
            choices = self._sources(db, root_id)
            header = next((r for r in choices if r["source"] == source), None)
            if header is None:
                raise ValueError("所选快照来源尚无可用清单，请先完成相应扫描")
            if expected is not None and header != expected:
                raise ValueError("所选清单在确认期间发生变化，请刷新后重新选择")
            if db.execute("SELECT 1 FROM catalog_snapshots WHERE root_id=? AND name=?",
                          (root_id, name)).fetchone():
                raise ValueError("该资料库已存在同名快照")
            created = datetime.now().astimezone().isoformat(timespec="seconds")
            snapshot = db.execute("""INSERT INTO catalog_snapshots
                (root_id,name,created_at,indexed_at,generation,scan_state,source) VALUES(?,?,?,?,?,?,?)""",
                (root_id, name, created, header["indexed_at"], header["generation"],
                 header["scan_state"], source)).lastrowid
            if source == "last_complete":
                files = db.execute("""INSERT INTO catalog_snapshot_files
                    SELECT ?,relpath,size,mtime_ns,digest,0 FROM catalog_complete_files
                    WHERE root_id=?""", (snapshot, root_id)).rowcount
                issues = 0
            else:
                files = db.execute("""INSERT INTO catalog_snapshot_files
                    SELECT ?,relpath,size,mtime_ns,digest,stale FROM catalog_files
                    WHERE root_id=? AND active=1""", (snapshot, root_id)).rowcount
                issues = db.execute("""INSERT INTO catalog_snapshot_issues(snapshot_id,relpath,reason)
                    SELECT ?,relpath,reason FROM catalog_issues WHERE root_id=?""", (snapshot, root_id)).rowcount
            db.execute("UPDATE catalog_snapshots SET file_count=?,issue_count=? WHERE id=?",
                       (files, issues, snapshot))
            return snapshot

    def list_snapshots(self, root_id):
        with database_connection(self.path) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM catalog_snapshots WHERE root_id=? ORDER BY id DESC", (root_id,))]

    def issues(self, snapshot_id):
        with database_connection(self.path) as db:
            return [dict(row) for row in db.execute(
                "SELECT relpath,reason FROM catalog_snapshot_issues WHERE snapshot_id=? ORDER BY relpath",
                (snapshot_id,))]

    def compare(self, old_id, new_id, cancel=None):
        if old_id == new_id:
            raise ValueError("请选择两个不同的快照")
        with database_connection(self.path) as db, _cancelable(db, cancel):
            db.execute("BEGIN")
            roots = db.execute("SELECT root_id FROM catalog_snapshots WHERE id IN (?,?)",
                               (old_id, new_id)).fetchall()
            if len(roots) != 2 or roots[0]["root_id"] != roots[1]["root_id"]:
                raise ValueError("比较需要同一资料库的两个已有快照")
            return _compare(db, old_id, new_id, cancel)


def _coverage(db, snapshot):
    return {str(PurePosixPath(row["relpath"] or ".")).casefold(): row["reason"]
            for row in db.execute("SELECT relpath,reason FROM catalog_snapshot_issues WHERE snapshot_id=?",
                                  (snapshot,))}


def _blind_reason(path, coverage):
    if coverage:
        path = PurePosixPath(path)
        for part in (path, *path.parents):
            reason = coverage.get(str(part).casefold())
            if reason is not None:
                return f"范围「{part}」未能完整扫描：{reason}"
    return ""


def _diff(status, old, new, reason):
    result = {"status": status, "reason": reason}
    for prefix, entry in (("old", old), ("new", new)):
        result[prefix + "_path"] = entry["relpath"] if entry else ""
        for key in ("size", "mtime_ns", "digest"):
            result[f"{prefix}_{key}"] = entry[key] if entry else None
    return result


def _compare(db, old_id, new_id, cancel):
    old_blind, new_blind = _coverage(db, old_id), _coverage(db, new_id)
    output = []
    removed, added = defaultdict(list), defaultdict(list)
    # The primary key serves both joins. Equal paths with no reported change
    # never need to reside in Python memory together.
    matched = db.execute("""SELECT a.*, b.size AS nsize,b.mtime_ns AS ntime,
                            b.digest AS nhash,b.stale AS nstale FROM catalog_snapshot_files a
        JOIN catalog_snapshot_files b ON b.snapshot_id=? AND b.relpath=a.relpath
        WHERE a.snapshot_id=?""", (new_id, old_id))
    for old in matched:
        check_cancel(cancel)
        new = dict(relpath=old["relpath"], size=old["nsize"], mtime_ns=old["ntime"],
                   digest=old["nhash"], stale=old["nstale"])
        if old["stale"] or new["stale"]:
            reason = _blind_reason(old["relpath"], new_blind if new["stale"] else old_blind)
            output.append(_diff("uncertain", old, new, reason or "至少一次扫描仅保留了上次已知记录"))
        elif old["digest"] and new["digest"]:
            if old["digest"] != new["digest"]:
                output.append(_diff("changed", old, new, "同一路径的 SHA-256 不同"))
            elif old["size"] != new["size"]:
                output.append(_diff("uncertain", old, new, "哈希与文件大小的证据不一致"))
        elif old["size"] != new["size"] or old["mtime_ns"] != new["mtime_ns"]:
            output.append(_diff("uncertain", old, new, "大小或修改时间变化，缺少双侧哈希，仅能判断可能变更"))

    for current, other, opposite_blind, buckets, is_old in (
            (old_id, new_id, new_blind, removed, True), (new_id, old_id, old_blind, added, False)):
        rows = db.execute("""SELECT a.* FROM catalog_snapshot_files a WHERE a.snapshot_id=?
            AND NOT EXISTS (SELECT 1 FROM catalog_snapshot_files b WHERE b.snapshot_id=?
                            AND b.relpath=a.relpath)""", (current, other))
        for row in rows:
            check_cancel(cancel)
            proof_gap = _blind_reason(row["relpath"], opposite_blind)
            if row["stale"] or proof_gap:
                output.append(_diff("uncertain", row if is_old else None, None if is_old else row,
                                    proof_gap or "该路径仅有上次已知记录，无法确认当时的状态"))
            else:
                buckets[row["digest"]].append(row)

    counts = {}
    for key in removed.keys() & added.keys():
        check_cancel(cancel)
        if not key:
            continue
        # Count in both *whole* manifests, including files which never changed
        # path. One removed + one added candidate is insufficient on its own.
        if len(removed[key]) == len(added[key]) == 1:
            counts[key] = [db.execute("""SELECT count(*) FROM catalog_snapshot_files
                            WHERE snapshot_id=? AND digest=?""", (s, key)).fetchone()[0]
                           for s in (old_id, new_id)]
        old, new = removed[key], added[key]
        if counts.get(key) == [1, 1] and old[0]["size"] == new[0]["size"]:
            output.append(_diff("moved", old[0], new[0], "两份清单中 SHA-256 均唯一且相同，内容一致，路径不同"))
        else:
            reason = "相同哈希存在多个候选或证据不一致，无法唯一配对路径"
            output.extend(_diff("uncertain", r, None, reason) for r in old)
            output.extend(_diff("uncertain", None, r, reason) for r in new)
        del removed[key], added[key]
    for same_hash in removed.values():
        output.extend(_diff("missing", row, None, "后一个快照的已扫描范围内没有此路径") for row in same_hash)
    for same_hash in added.values():
        output.extend(_diff("added", None, row, "前一个快照的已扫描范围内没有此路径") for row in same_hash)
    return sorted(output, key=lambda r: ((r["new_path"] or r["old_path"]).casefold(), r["status"]))


def _csv_text(value):
    # Excel evaluates formula-looking cells even when they are quoted in CSV.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def export_diff(path, rows):
    fields = ("old_path", "new_path", "reason", "old_size", "new_size", "old_mtime_ns",
              "new_mtime_ns", "old_digest", "new_digest")
    with Path(path).open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("分类", "原相对路径", "新相对路径", "判断依据", "原字节数", "新字节数",
                         "原修改时间（纳秒）", "新修改时间（纳秒）", "原 SHA-256", "新 SHA-256"))
        for row in rows:
            writer.writerow([STATUS_NAMES[row["status"]], *(_csv_text(row[k]) for k in fields)])
