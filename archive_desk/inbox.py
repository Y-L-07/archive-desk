"""Top-level, opt-in inbox. The caller schedules polls while the application is open."""
import json
import os
import stat
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

from ._winmove import open_regular, pin_directory
from .core import FileInfo, change_time_ns, check_cancel, reparse
from .file_revision import content_digest, read_usn
from .journal import database_connection


@dataclass
class InboxResult:
    root: Path | None
    candidates: list[FileInfo]
    pending: int
    warnings: list[str]


def _pin_root(stack, root):
    if ".." in root.parts:
        raise ValueError("收件箱目录不能包含相对跳转")
    for parent in reversed(root.parents):
        stack.enter_context(pin_directory(parent))
    return stack.enter_context(pin_directory(root))


def _decode_file(payload):
    data = json.loads(payload)
    return FileInfo(**dict(data, path=Path(data["path"])))


def _encode_file(file):
    return json.dumps(asdict(file), ensure_ascii=False, default=str, sort_keys=True)


def _outputs(db, root):
    """Known outputs include completed undos and already committed cross-volume copies."""
    outputs = {}
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone():
        return outputs
    prefix = str(root)
    if not prefix.endswith(os.sep):
        prefix += os.sep
    rows = db.execute("""
        SELECT source,target,status,direction,evidence,transfer FROM items
        WHERE (substr(target,1,?)=? COLLATE NOCASE OR substr(source,1,?)=? COLLATE NOCASE)
          AND status IN ('moved','undone','uncertain','resumable','applying','undoing','review')
    """, (len(prefix), prefix, len(prefix), prefix))
    for row in rows:
        transfer = json.loads(row["transfer"] or "{}")
        if row["status"] == "moved":
            path = Path(row["target"])
            record = json.loads(row["evidence"] or "{}")
            identity, digest = record.get("file"), record.get("sha256")
        else:
            path = Path(row["source"] if row["direction"] == "undo" else row["target"])
            identity = transfer.get("result") if transfer.get("mode") == "copy" else transfer.get("file")
            digest = transfer.get("sha256")
        if path.parent == root and identity and digest:
            outputs.setdefault((path.name, *identity), set()).add(digest)
    return outputs


class Inbox:
    def __init__(self, db_path, stable_seconds=5.0):
        self.db_path = Path(db_path).absolute()
        self.stable_seconds = max(0.0, float(stable_seconds))
        with database_connection(self.db_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inbox_settings(
                    id INTEGER PRIMARY KEY CHECK(id=1), root TEXT, identity TEXT,
                    revision INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO inbox_settings(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS inbox_observations(
                    root TEXT NOT NULL, name TEXT NOT NULL, stamp TEXT NOT NULL,
                    since REAL NOT NULL, state TEXT NOT NULL, snapshot TEXT,
                    PRIMARY KEY(root,name));
            """)

    def configure(self, root_or_none):
        root = Path(root_or_none).absolute() if root_or_none is not None else None
        with ExitStack() as stack:
            identity = json.dumps(_pin_root(stack, root)) if root else None
            with database_connection(self.db_path) as db:
                db.execute("""UPDATE inbox_settings SET root=?,identity=?,revision=revision+1 WHERE id=1""",
                           (str(root) if root else None, identity))

    def root(self):
        with database_connection(self.db_path) as db:
            root = db.execute("SELECT root FROM inbox_settings WHERE id=1").fetchone()[0]
        return Path(root) if root else None

    def _result(self, db, warnings):
        selected = db.execute("SELECT root,identity FROM inbox_settings WHERE id=1").fetchone()
        if not selected["root"]:
            return InboxResult(None, [], 0, warnings)
        key = json.dumps(list(selected))
        candidates = []
        pending = 0
        for row in db.execute("""SELECT state,snapshot FROM inbox_observations
                WHERE root=? AND state IN ('ready','waiting') ORDER BY name COLLATE NOCASE""", (key,)):
            if row["state"] == "ready":
                candidates.append(_decode_file(row["snapshot"]))
            else:
                pending += 1
        return InboxResult(Path(selected["root"]), candidates, pending, warnings)

    def candidates(self):
        with database_connection(self.db_path) as db:
            return self._result(db, []).candidates

    def acknowledge(self, files):
        # A stale preview must never consume a newer download at the same path.
        with database_connection(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT root,identity FROM inbox_settings WHERE id=1").fetchone()
            if row["root"]:
                key = json.dumps(list(row))
                db.executemany("""UPDATE inbox_observations SET state='consumed'
                                  WHERE root=? AND name=? AND snapshot=? AND state='ready'""",
                               ((key, f.path.name, _encode_file(f)) for f in files))
                db.execute("UPDATE inbox_settings SET revision=revision+1 WHERE id=1")

    def _ignored(self, path):
        name = path.name.casefold()
        if (name.startswith((".archive-desk-", "~$")) or
                name.endswith((".crdownload", ".part", ".partial", ".tmp", ".download",
                               ".opdownload", ".aria2", ".td", ".bc!"))):
            return True
        if path.parent != self.db_path.parent:
            return False
        db = self.db_path.name.casefold()
        return name in {db, db + "-wal", db + "-shm", db + "-journal", "instance.lock"}

    def poll(self, cancel=None):
        with database_connection(self.db_path) as db:
            selected = db.execute("SELECT root,identity,revision FROM inbox_settings WHERE id=1").fetchone()
            if not selected["root"]:
                return InboxResult(None, [], 0, [])
            root = Path(selected["root"])
            key = json.dumps([selected["root"], selected["identity"]])
            prior = {row["name"]: row for row in db.execute(
                "SELECT * FROM inbox_observations WHERE root=?", (key,))}
        seen, changes, warnings = set(), [], []
        output_signatures = None
        now = time.time()
        with ExitStack() as stack:
            check_cancel(cancel)
            if json.dumps(_pin_root(stack, root)) != selected["identity"]:
                raise ValueError("收件箱目录已被替换，请重新选择授权目录")
            with os.scandir(root) as entries:
                for entry in entries:
                    check_cancel(cancel)
                    path = Path(entry.path)
                    name = path.name
                    old = prior.get(name)
                    if self._ignored(path):
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        if reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
                            continue
                        seen.add(name)
                        with open_regular(path) as (stream, handle, st):
                            change, usn = change_time_ns(handle), read_usn(handle)
                            stamp = json.dumps([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, change, usn])
                            if old is None or old["stamp"] != stamp or now < old["since"]:
                                changes.append((key, name, stamp, now, "waiting", None))
                                continue
                            if old["state"] != "waiting" or now - old["since"] < self.stable_seconds:
                                continue
                            if output_signatures is None:
                                with database_connection(self.db_path) as db:
                                    output_signatures = _outputs(db, root)
                            own = output_signatures.get((name, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns))
                            digest = content_digest(stream, cancel) if usn is None or own else None
                            file = FileInfo(path, st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
                                            change, digest if usn is None else None, usn)
                            state = "own" if own and digest in own else "ready"
                            changes.append((key, name, stamp, old["since"], state, _encode_file(file)))
                    except (ValueError, OSError) as exc:
                        if len(warnings) < 20:
                            warnings.append(f"{name}：{exc}")
                        # Busy writers cannot yield a verified candidate. Keep a receipt
                        # for versions already handed off so a brief lock cannot replay one.
                        if old is not None and old["state"] not in ("consumed", "own"):
                            changes.append((key, name, "", now, "waiting", None))
        check_cancel(cancel)
        with database_connection(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            revision = db.execute("SELECT revision FROM inbox_settings WHERE id=1").fetchone()[0]
            if revision == selected["revision"]:
                db.executemany("""INSERT INTO inbox_observations VALUES(?,?,?,?,?,?)
                                  ON CONFLICT(root,name) DO UPDATE SET stamp=excluded.stamp,
                                  since=excluded.since,state=excluded.state,snapshot=excluded.snapshot""",
                               changes)
                missing = prior.keys() - seen
                db.executemany("DELETE FROM inbox_observations WHERE root=? AND name=?",
                               ((key, name) for name in missing))
                if changes or missing:
                    db.execute("UPDATE inbox_settings SET revision=revision+1 WHERE id=1")
            return self._result(db, warnings)
