"""Windows catalog roots and incremental, atomically published index generations."""
import os
import sqlite3
import stat
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path, PurePosixPath
from threading import Lock
from time import monotonic

from ._winmove import open_regular, pin_directory
from .core import FileInfo, ScanCancelled, change_time_ns, check_cancel, reparse
from .file_revision import content_digest, read_usn
from .journal import Journal, database_connection

_BATCH = 256
_active = set()
_lock = Lock()


def ensure_catalog_schema(db):
    for sql in (
        """CREATE TABLE IF NOT EXISTS catalog_roots(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, dev TEXT NOT NULL,
            ino TEXT NOT NULL, online INTEGER NOT NULL DEFAULT 1, problem TEXT NOT NULL DEFAULT '',
            indexed_at TEXT, last_complete_at TEXT, scan_state TEXT NOT NULL DEFAULT 'never',
            published_state TEXT NOT NULL DEFAULT 'never', revision INTEGER NOT NULL DEFAULT 0,
            generation INTEGER NOT NULL DEFAULT 0, UNIQUE(dev,ino))""",
        """CREATE TABLE IF NOT EXISTS catalog_files(
            id INTEGER PRIMARY KEY AUTOINCREMENT, root_id INTEGER NOT NULL REFERENCES catalog_roots(id),
            relpath TEXT NOT NULL, name TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, dev TEXT NOT NULL, ino TEXT NOT NULL, change_ns TEXT,
            usn INTEGER, digest TEXT, active INTEGER NOT NULL DEFAULT 1, stale INTEGER NOT NULL DEFAULT 0)""",
        """CREATE UNIQUE INDEX IF NOT EXISTS catalog_location
            ON catalog_files(root_id,relpath) WHERE active=1""",
        """CREATE INDEX IF NOT EXISTS catalog_identity
            ON catalog_files(root_id,dev,ino,active)""",
        """CREATE INDEX IF NOT EXISTS catalog_extension ON catalog_files(ext,size) WHERE active=1""",
        """CREATE TABLE IF NOT EXISTS catalog_issues(
            root_id INTEGER NOT NULL REFERENCES catalog_roots(id),relpath TEXT NOT NULL,
            reason TEXT NOT NULL,PRIMARY KEY(root_id,relpath))""",
    ):
        db.execute(sql)
    from .catalog_search import ensure_search_schema
    ensure_search_schema(db)


def touch_roots(db, root_ids):
    db.executemany("UPDATE catalog_roots SET revision=revision+1 WHERE id=?",
                   ((root_id,) for root_id in set(root_ids) if root_id is not None))


def _values(relpath, info, digest):
    name = PurePosixPath(relpath).name
    return (relpath, name, PurePosixPath(name).suffix.lower(), info.size, info.mtime_ns,
            str(info.dev), str(info.ino), str(info.change_ns) if info.change_ns is not None else None,
            info.usn, digest)


_columns = "relpath,name,ext,size,mtime_ns,dev,ino,change_ns,usn,digest"


def upsert_file(db, root_id, relpath, info, *, digest=None, prior_id=None):
    """Updates one verified file in the caller's transaction; an explicit prior_id carries its metadata."""
    relpath = str(relpath).replace("\\", "/")
    if (not relpath or PurePosixPath(relpath).is_absolute() or
            any(part in ("", ".", "..") for part in relpath.split("/"))):
        raise ValueError("索引需要有效的库内相对路径")
    values = _values(relpath, info, digest or info.digest)
    current = db.execute("SELECT * FROM catalog_files WHERE root_id=? AND relpath=? AND active=1",
                         (root_id, relpath)).fetchone()
    entry_id = prior_id
    if current:
        if entry_id is None and (current["dev"], current["ino"]) == (str(info.dev), str(info.ino)):
            entry_id = current["id"]
        if current["id"] != entry_id:
            db.execute("UPDATE catalog_files SET active=0 WHERE id=?", (current["id"],))
    if entry_id is None:
        old = db.execute("""SELECT id FROM catalog_files WHERE root_id=? AND relpath=? AND dev=?
                      AND ino=? ORDER BY id DESC LIMIT 1""",
                         (root_id, relpath, str(info.dev), str(info.ino))).fetchone()
        entry_id = old["id"] if old else None
    if entry_id is None:
        cursor = db.execute(f"INSERT INTO catalog_files(root_id,{_columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (root_id, *values))
        return cursor.lastrowid
    cursor = db.execute(f"UPDATE catalog_files SET root_id=?,({_columns})=(?,?,?,?,?,?,?,?,?,?),"
                        "active=1,stale=0 WHERE id=?", (root_id, *values, entry_id))
    if not cursor.rowcount:
        raise ValueError("原索引条目已不存在")
    return entry_id


def _root_identity(stack, path):
    if not path.is_absolute() or ".." in path.parts or str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("请选择有效的绝对目录")
    for folder in reversed(path.parents):
        stack.enter_context(pin_directory(folder))
    dev, ino = stack.enter_context(pin_directory(path))
    if not dev or not ino:
        raise ValueError("无法取得可靠的卷或根目录标识")
    return str(dev), str(ino)


def _matches(root, identity):
    return (root["dev"], root["ino"]) == identity


class Catalog:
    def __init__(self, db_path):
        self.path = Journal(db_path).path

    def add_root(self, name, path):
        name = name.strip()
        if not name or len(name) > 120 or not name.isprintable():
            raise ValueError("请输入 1 至 120 个可显示字符作为库名称")
        path = Path(path).absolute()
        with ExitStack() as stack:
            dev, ino = _root_identity(stack, path)
            with database_connection(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM catalog_roots WHERE dev=? AND ino=?", (dev,ino)).fetchone():
                    raise ValueError("此目录已登记；目录位置变动时请重新绑定原有库")
                return db.execute("INSERT INTO catalog_roots(name,path,dev,ino) VALUES(?,?,?,?)",
                                  (name, str(path), dev, ino)).lastrowid

    def _root(self, db, root_id):
        row = db.execute("SELECT * FROM catalog_roots WHERE id=?", (root_id,)).fetchone()
        if row is None:
            raise ValueError("指定的库不存在")
        return dict(row)

    def roots(self):
        with database_connection(self.path) as db:
            rows = [dict(r) for r in db.execute("""SELECT r.*,
                      (SELECT COUNT(*) FROM catalog_files f WHERE f.root_id=r.id AND f.active=1) AS file_count,
                      (SELECT COUNT(*) FROM catalog_issues i WHERE i.root_id=r.id) AS issue_count
                      FROM catalog_roots r ORDER BY r.id""")]
        with _lock:
            for row in rows:
                if (self.path, row["id"]) in _active:
                    row["scan_state"] = "running"
        return rows

    def issues(self, root_id):
        with database_connection(self.path) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM catalog_issues WHERE root_id=? ORDER BY relpath", (root_id,))]

    def probe(self, root_id):
        with database_connection(self.path) as db:
            root = self._root(db, root_id)
        problem = ""
        try:
            with ExitStack() as stack:
                if not _matches(root, _root_identity(stack, Path(root["path"]))):
                    raise ValueError("此路径的卷或目录身份与登记信息不同，请核对原设备")
        except (OSError, ValueError) as exc:
            problem = str(exc)
        # An identity check needs only attribute access. It must not clear a
        # scan error such as permission denied while enumerating this root.
        reason = problem or (root["problem"] if root["online"] else "")
        availability = int(not problem)
        if (availability, reason) != (root["online"], root["problem"]):
            with database_connection(self.path) as db:
                db.execute("UPDATE catalog_roots SET online=?,problem=? WHERE id=? AND path=? AND revision=?",
                           (availability, reason, root_id, root["path"], root["revision"]))
        return not problem

    def rebind(self, root_id, path, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("请确认新的绑定位置")
        path = Path(path).absolute()
        with ExitStack() as stack:
            identity = _root_identity(stack, path)
            with database_connection(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                root = self._root(db, root_id)
                if not _matches(root, identity):
                    raise ValueError("所选目录的卷或目录身份不匹配；请连接原设备并选择原目录")
                db.execute("UPDATE catalog_roots SET path=?,online=1,problem='',revision=revision+1 WHERE id=?",
                           (str(path), root_id))

    def _entries(self, folder):
        return os.scandir(folder)

    def refresh(self, root_id, *, hash_files=False, cancel=None, progress=None):
        job = (self.path, root_id)
        with _lock:
            if job in _active:
                raise ValueError("这个库已有索引任务")
            _active.add(job)
        try:
            with database_connection(self.path) as db:
                root = self._root(db, root_id)
                return self._refresh(db, root, hash_files, cancel, progress)
        except (Exception, KeyboardInterrupt) as exc:
            if 'root' in locals():
                state = "cancelled" if isinstance(exc, ScanCancelled) else "failed"
                self.probe(root_id)
                with database_connection(self.path) as db:
                    db.execute("UPDATE catalog_roots SET scan_state=?,problem=? WHERE id=? AND revision=?",
                               (state, str(exc), root_id, root["revision"]))
            raise
        finally:
            with _lock:
                _active.discard(job)

    def _refresh(self, db, root, hash_files, cancel, progress):
        root_id = root["id"]
        path = Path(root["path"])
        counts = {"seen": 0, "hashed": 0, "reused": 0, "issues": 0}
        pending, blind = [], {}
        next_tick = 0.0
        db.execute("""CREATE TEMP TABLE observed(
            relpath TEXT PRIMARY KEY, name TEXT,ext TEXT,size INTEGER,mtime_ns INTEGER,dev TEXT,ino TEXT,
            change_ns TEXT,usn INTEGER,digest TEXT,nlinks INTEGER,previous INTEGER)""")
        db.execute("CREATE INDEX temp.observed_identity ON observed(dev,ino)")

        def flush():
            if pending:
                db.executemany("INSERT INTO observed VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", pending)
                db.commit()  # Only connection-private temporary rows exist at this point.
                pending.clear()

        def record(file, rel):
            nonlocal next_tick
            with open_regular(file) as (stream, handle, st):
                usn = read_usn(handle)
                change = change_time_ns(handle)
                digest = None
                if usn is not None:
                    cached = db.execute("""SELECT digest FROM catalog_files WHERE root_id=? AND relpath=?
                        AND active=1 AND dev=? AND ino=? AND size=? AND mtime_ns=? AND change_ns=? AND usn=?
                        AND digest IS NOT NULL""", (root_id,rel,str(st.st_dev),str(st.st_ino),st.st_size,
                                                    st.st_mtime_ns,str(change),usn)).fetchone()
                    if cached:
                        digest = cached["digest"]
                        counts["reused"] += 1
                if digest is None and hash_files:
                    digest = content_digest(stream, cancel)
                    counts["hashed"] += 1
                info = FileInfo(file,st.st_size,st.st_mtime_ns,st.st_dev,st.st_ino,change,digest,usn)
                pending.append((*_values(rel, info, digest), st.st_nlink))
            counts["seen"] += 1
            if len(pending) >= _BATCH:
                flush()
            if progress and monotonic() >= next_tick:
                progress(counts["seen"], rel)
                next_tick = monotonic() + 0.1

        skip = {self.path, *(Path(str(self.path)+suf) for suf in ("-journal","-shm","-wal"))}

        def walk(folder):
            with self._entries(folder) as entries:
                for entry in entries:
                    check_cancel(cancel)
                    file = Path(entry.path)
                    if file in skip:
                        continue
                    rel = file.relative_to(path).as_posix()
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if reparse(info):
                            blind[rel] = "链接或重解析点，未跟随"
                        elif stat.S_ISDIR(info.st_mode):
                            with pin_directory(file):
                                walk(file)
                        elif stat.S_ISREG(info.st_mode):
                            record(file, rel)
                    except (OSError, ValueError) as exc:
                        blind[rel] = str(exc)

        check_cancel(cancel)
        with ExitStack() as stack:
            if not _matches(root, _root_identity(stack, path)):
                raise ValueError("库根目录身份与登记时不同，已保留现有索引")
            walk(path)
            flush()
            if progress:
                progress(counts["seen"], "")
            check_cancel(cancel)
            current = path.lstat()
            if not _matches(root, (str(current.st_dev),str(current.st_ino))):
                raise ValueError("发布前未能验证库根目录身份")
            self._publish(db, root, blind, cancel)
        counts["issues"] = len(blind)
        return {**self._root(db, root_id), "file_count": db.execute(
            "SELECT COUNT(*) FROM catalog_files WHERE root_id=? AND active=1",(root_id,)).fetchone()[0],
                "issue_count": len(blind), "counts": counts}

    def _publish(self, db, root, blind, cancel):
        root_id = root["id"]
        db.execute("BEGIN IMMEDIATE")
        if self._root(db, root_id)["revision"] != root["revision"]:
            raise ValueError("索引期间库绑定或文件记录已变化，请重新刷新")
        if cancel is not None:
            db.set_progress_handler(lambda: int(cancel.is_set()), 4000)
        try:
            # Stable IDs keep annotations across rebuilds. A same-path replacement
            # gets a fresh ID. A unique same-volume rename can retain the old one.
            db.execute("""UPDATE observed AS o SET previous=(
                SELECT f.id FROM catalog_files f WHERE f.root_id=? AND f.relpath=o.relpath
                AND f.dev=o.dev AND f.ino=o.ino ORDER BY f.active DESC,f.id DESC LIMIT 1)""", (root_id,))
            db.execute("""UPDATE observed AS o SET previous=(
                SELECT min(f.id) FROM catalog_files f WHERE f.root_id=? AND f.active=1
                  AND f.dev=o.dev AND f.ino=o.ino HAVING COUNT(*)=1)
                WHERE previous IS NULL AND nlinks=1 AND NOT EXISTS (
                  SELECT 1 FROM observed alt WHERE alt.dev=o.dev AND alt.ino=o.ino
                  AND alt.relpath!=o.relpath)""", (root_id,))
            db.execute("CREATE INDEX temp.observed_previous ON observed(previous)")
            db.execute("""UPDATE catalog_files AS f SET active=0 WHERE root_id=? AND active=1
                          AND EXISTS (SELECT 1 FROM observed o WHERE o.relpath=f.relpath
                                      AND o.previous IS NOT f.id)""", (root_id,))
            db.execute("""CREATE TEMP TABLE unseen AS SELECT id,relpath FROM catalog_files f
                   WHERE root_id=? AND active=1
                   AND NOT EXISTS(SELECT 1 FROM observed o WHERE o.previous=f.id)""", (root_id,))
            missing = db.execute("SELECT id,relpath FROM unseen")
            # Use bounded batches and match complete path components. A denied
            # subtree is unknown; enumerated siblings remain authoritative.
            blocked = {p.casefold() for p in blind}
            while rows := missing.fetchmany(_BATCH):
                check_cancel(cancel)
                updates = []
                for row in rows:
                    path = PurePosixPath(row["relpath"].casefold())
                    unknown = path.as_posix() in blocked or any(p.as_posix() in blocked for p in path.parents)
                    updates.append((int(unknown), int(unknown), row["id"]))
                db.executemany("UPDATE catalog_files SET active=?,stale=? WHERE id=?", updates)
            # One set-based merge. Unchanged entries cause no catalog_files UPDATE.
            cols = _columns.split(",")
            all_cols = [*cols, "active", "stale"]
            lhs = ",".join(all_cols)
            rhs = ",".join("excluded."+c for c in all_cols)
            db.execute(f"""INSERT INTO catalog_files(id,root_id,{lhs})
                SELECT previous,?,{_columns},1,0 FROM observed WHERE 1
                ON CONFLICT(id) DO UPDATE SET ({lhs})=({rhs})
                WHERE ({','.join('catalog_files.'+c for c in all_cols)}) IS NOT ({rhs})""", (root_id,))
            db.execute("DELETE FROM catalog_issues WHERE root_id=?", (root_id,))
            db.executemany("INSERT INTO catalog_issues(root_id,relpath,reason) VALUES(?,?,?)",
                           ((root_id, p, why) for p, why in blind.items()))
            state = "partial" if blind else "complete"
            now = datetime.now().astimezone().isoformat(timespec="microseconds")
            db.execute("""UPDATE catalog_roots SET online=1,problem='',indexed_at=?,scan_state=?,
                  published_state=?,last_complete_at=COALESCE(?,last_complete_at),
                  generation=generation+1,revision=revision+1 WHERE id=?""",
                       (now,state,state,None if blind else now,root_id))
            check_cancel(cancel)
            db.commit()
        except sqlite3.OperationalError:
            check_cancel(cancel)
            raise
        finally:
            db.set_progress_handler(None, 0)
