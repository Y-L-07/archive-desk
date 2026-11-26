"""Durable per-item SQLite journal. Each state transition commits independently."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from dataclasses import asdict
from pathlib import Path


@contextmanager
def database_connection(path):
    with closing(sqlite3.connect(path, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        with db:
            yield db


class Journal:
    def __init__(self, db_path: Path):
        self.path = Path(db_path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 3:
                raise ValueError("此数据库由更新版本的归档台创建，请使用相应版本打开")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, root TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
                    source TEXT NOT NULL, target TEXT NOT NULL, rule TEXT NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    evidence TEXT, undo_conflict INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS items_by_batch ON items(batch_id);
            """)
            # Additive migration keeps V1 ids, evidence, profile data and undo state.
            db.execute("BEGIN IMMEDIATE")
            for table, columns in (("batches", ("target_root",)),
                                   ("items", ("intent", "direction", "phase", "transfer"))):
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for column in columns:
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            db.execute("UPDATE batches SET target_root=root WHERE target_root IS NULL")
            from .catalog import ensure_catalog_schema
            ensure_catalog_schema(db)
            db.execute("PRAGMA user_version=3")

    def _connection(self):
        return database_connection(self.path)

    def begin_batch(self, root, plans, target_root=None):
        entries = []
        with self._connection() as db:
            batch = db.execute("INSERT INTO batches(root,target_root,created_at) VALUES (?,?,?)",
                               (str(root), str(target_root or root), datetime.now().astimezone().isoformat(timespec="seconds")))
            for plan in plans:
                item = db.execute("""
                    INSERT INTO items(batch_id,source,target,rule,status,intent)
                    VALUES (?,?,?,?,'pending',?)
                """, (batch.lastrowid, str(plan.file.path), str(plan.target or ""), plan.rule,
                      json.dumps(asdict(plan), default=str)))
                entries.append(item.lastrowid)
            return batch.lastrowid, entries

    def transition(self, item_id, previous, status, reason="", evidence=None, conflict=False,
                   *, direction=None, phase=None, transfer=None):
        with self._connection() as db:
            result = db.execute("""
                UPDATE items SET status=?, reason=?, undo_conflict=?,
                                 evidence=COALESCE(?,evidence),direction=COALESCE(?,direction),
                                 phase=COALESCE(?,phase),transfer=COALESCE(?,transfer)
                WHERE id=? AND status=?
            """, (status, reason, int(conflict), json.dumps(evidence) if evidence else None,
                  direction, phase, json.dumps(transfer) if transfer is not None else None, item_id, previous))
            if result.rowcount != 1:
                raise RuntimeError("操作日志状态已变化，请查看历史记录")
            if phase == "done" and (direction, status) in (("apply", "moved"), ("undo", "undone")):
                from .catalog_events import committed_transfer
                committed_transfer(db, item_id, transfer)

    def recover(self, rules=None) -> int:
        """Called after acquiring the application's single-instance lock."""
        from .operations import inspect_interrupted
        from .core import default_rules
        rules = tuple(default_rules() if rules is None else rules)
        with self._connection() as db:
            result = db.execute("""
                UPDATE items SET status='uncertain',
                    reason='上次会话在文件移动或撤销过程中中断，请人工核对原路径和目标路径。'
                WHERE status IN ('applying','undoing') AND transfer IS NULL
            """)
            pending = db.execute("""
                UPDATE items SET status='skipped', reason='上次会话中断，该项尚未开始。'
                WHERE status='pending' AND intent IS NULL
            """)
            count = result.rowcount + pending.rowcount
            interrupted = db.execute("""SELECT i.*, b.root,b.target_root FROM items i
                JOIN batches b ON b.id=i.batch_id WHERE
                (status IN ('applying','undoing') AND transfer IS NOT NULL)
                OR status IN ('pending','resumable') AND intent IS NOT NULL""").fetchall()
        known = {bid: self.known_directories(bid) for bid in {r["batch_id"] for r in interrupted}}
        for row in interrupted:
            status, reason = inspect_interrupted(dict(row), rules, known[row["batch_id"]])
            if status != row["status"] or reason != row["reason"]:
                self.transition(row["id"], row["status"], status, reason)
                count += 1
        return count

    def known_directories(self, batch_id):
        with self._connection() as db:
            rows = db.execute("SELECT transfer FROM items WHERE batch_id=? AND transfer IS NOT NULL",
                              (batch_id,))
            return {Path(path): (dev, ino) for row in rows
                    for path, dev, ino in json.loads(row["transfer"])["directories"]}

    def batches(self):
        with self._connection() as db:
            rows = db.execute("""
                SELECT b.*, COUNT(i.id) AS total,
                    COUNT(CASE WHEN status='moved' THEN 1 END) AS moved,
                    COUNT(CASE WHEN status='undone' THEN 1 END) AS undone,
                    COUNT(CASE WHEN status='skipped' THEN 1 END) AS skipped,
                    COUNT(CASE WHEN status='uncertain' THEN 1 END) AS uncertain,
                    COUNT(CASE WHEN status='review' THEN 1 END) AS review,
                    COUNT(CASE WHEN status='failed' THEN 1 END) AS failed,
                    COUNT(CASE WHEN status='cancelled' THEN 1 END) AS cancelled,
                    COUNT(CASE WHEN status='resumable' THEN 1 END) AS resumable,
                    COUNT(CASE WHEN status='pending' THEN 1 END) AS pending,
                    COUNT(CASE WHEN status IN ('applying','undoing') THEN 1 END) AS active,
                    COUNT(CASE WHEN undo_conflict=1 THEN 1 END) AS undo_conflicts
                FROM batches b LEFT JOIN items i ON i.batch_id=b.id
                GROUP BY b.id ORDER BY b.id DESC
            """)
            return [dict(row) for row in rows]

    def items(self, batch_id):
        with self._connection() as db:
            rows = db.execute("""SELECT i.*, b.root,b.target_root FROM items i JOIN batches b ON b.id=i.batch_id
                                 WHERE i.batch_id=? ORDER BY i.id""", (batch_id,))
            return [dict(row) for row in rows]

    def item(self, item_id):
        with self._connection() as db:
            row = db.execute("""SELECT i.*, b.root,b.target_root FROM items i
                JOIN batches b ON b.id=i.batch_id WHERE i.id=?""", (item_id,)).fetchone()
            return dict(row)

    def cancel_pending(self, batch_id):
        with self._connection() as db:
            db.execute("""UPDATE items SET status='cancelled',reason='用户取消，尚未执行'
                          WHERE batch_id=? AND status='pending'""", (batch_id,))
