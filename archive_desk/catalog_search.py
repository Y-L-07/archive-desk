"""档案条目的检索、用户标注和历史关联。"""
import json
from contextlib import ExitStack
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ._winmove import open_regular, pin_directory
from .journal import database_connection


def ensure_search_schema(db):
    # execute() preserves the migration's surrounding transaction.
    for sql in (
        """CREATE TABLE IF NOT EXISTS catalog_labels(
           entry_id INTEGER NOT NULL REFERENCES catalog_files(id),
           tag TEXT NOT NULL COLLATE NOCASE, PRIMARY KEY(entry_id,tag))""",
        "CREATE INDEX IF NOT EXISTS catalog_labels_tag ON catalog_labels(tag,entry_id)",
        """CREATE TABLE IF NOT EXISTS catalog_notes(
           entry_id INTEGER PRIMARY KEY REFERENCES catalog_files(id),
           note TEXT NOT NULL DEFAULT '',favorite INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS catalog_saved_searches(
           name TEXT PRIMARY KEY COLLATE NOCASE, spec TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS catalog_history_links(
           item_id INTEGER NOT NULL REFERENCES items(id), entry_id INTEGER NOT NULL
           REFERENCES catalog_files(id), PRIMARY KEY(item_id,entry_id))""",
        "CREATE INDEX IF NOT EXISTS catalog_links_by_file ON catalog_history_links(entry_id)",
    ):
        db.execute(sql)


_FROM = """ FROM catalog_files f JOIN catalog_roots r ON r.id=f.root_id
            LEFT JOIN catalog_notes n ON n.entry_id=f.id """
_FIELDS = """SELECT f.*,r.name AS root_name,r.path AS root_path,r.online,
             COALESCE(n.note,'') AS note,COALESCE(n.favorite,0) AS favorite """


def _where(filters):
    known = {"text","ext","date_from","date_to","min_size","max_size","tag",
             "online","favorite","root_id","directory","ext_exact","directory_bucket"}
    if set(filters) - known:
        raise ValueError("含有无法识别的搜索条件")
    clauses, args = ["f.active=1"], []
    def term(sql, value):
        clauses.append(sql)
        args.append(value)
    text = str(filters.get("text") or "").strip()
    if text:
        term("instr(lower(f.relpath),lower(?))>0", text.replace("\\","/"))
    ext = str(filters.get("ext") or "").strip().lower()
    if ext:
        term("f.ext=?", "." + ext.lstrip("."))
    if "ext_exact" in filters:
        value = filters["ext_exact"]
        if not isinstance(value, str):
            raise ValueError("精确扩展名须为文本，可以是空字符串")
        term("f.ext=?", value)
    if "directory_bucket" in filters:
        bucket = filters["directory_bucket"]
        if not isinstance(bucket, dict) or set(bucket) != {"depth", "path"}:
            raise ValueError("目录统计筛选条件无效")
        depth, folder = bucket["depth"], bucket["path"]
        if type(depth) is not int or not 1 <= depth <= 32 or not isinstance(folder,str):
            raise ValueError("目录统计层级须为 1 至 32 的整数")
        parts = folder.split("/") if folder else []
        if len(parts)>depth or "\\" in folder or any(p in ("",".","..") for p in parts):
            raise ValueError("目录统计路径超出所选层级")
        # Binary equality keeps literal %, _, ^ and case-sensitive NTFS names
        # aligned with GROUP BY. A shallower bucket contains direct files only.
        if folder:
            prefix = folder + "/"
            clauses.append("substr(f.relpath,1,?)=? COLLATE BINARY")
            args.extend((len(prefix), prefix))
        if len(parts)<depth:
            term("instr(substr(f.relpath,?),'/')=0", len(folder)+2 if folder else 1)
    if filters.get("directory") is not None:
        term("f.relpath LIKE ? ESCAPE '^'", str(filters["directory"]).replace("^","^^")
             .replace("%","^%").replace("_","^_").rstrip("/") + "/%")
    for key, expression in (("min_size","f.size>=?"),("max_size","f.size<=?"),
                            ("root_id","f.root_id=?")):
        if filters.get(key) not in (None,""):
            value = int(filters[key])
            if not 0 <= value < 2**63:
                raise ValueError("大小或库编号超出有效范围")
            term(expression, value)
    if all(filters.get(k) not in (None, "") for k in ("min_size","max_size")):
        if int(filters["min_size"]) > int(filters["max_size"]):
            raise ValueError("最小字节数不能大于最大字节数")
    days = {}
    for key in ("date_from","date_to"):
        if filters.get(key):
            try:
                days[key] = date.fromisoformat(filters[key])
                boundary = days[key] + timedelta(days=int(key == "date_to"))
                epoch = int(datetime.combine(boundary,time()).timestamp()) * 10**9
            except (TypeError, ValueError, OSError, OverflowError) as exc:
                raise ValueError("请输入有效的本地日期 YYYY-MM-DD") from exc
            # A FLOAT parameter also handles dates beyond SQLite's ns integer range.
            term("f.mtime_ns>=?" if key == "date_from" else "f.mtime_ns<?",
                 epoch if -(2**63) <= epoch < 2**63 else float(epoch))
    if len(days) == 2 and days["date_from"] > days["date_to"]:
        raise ValueError("起始日期不能晚于截止日期")
    if filters.get("online") is not None:
        term("r.online=?", int(bool(filters["online"])))
    if filters.get("tag"):
        term("EXISTS (SELECT 1 FROM catalog_labels l WHERE l.entry_id=f.id AND l.tag=?)",
             str(filters["tag"]).strip())
    if filters.get("favorite"):
        clauses.append("n.favorite=1")
    return " WHERE " + " AND ".join(clauses), args


def _with_tags(db, rows):
    result = [dict(row, tags=[]) for row in rows]
    records = {row["id"]: row for row in result}
    if result:
        placeholders = ",".join("?" for _ in result)
        for entry_id, tag in db.execute(
                f"SELECT entry_id,tag FROM catalog_labels WHERE entry_id IN ({placeholders}) ORDER BY tag",
                list(records)):
            records[entry_id]["tags"].append(tag)
    return result


class CatalogSearch:
    def __init__(self, db_path):
        self.path = Path(db_path)

    def search(self, filters=None, page=1, page_size=100):
        if not 1 <= page_size <= 500 or page < 1:
            raise ValueError("页码从 1 开始，每页最多 500 项")
        filters = filters or {}
        where, args = _where(filters)
        with database_connection(self.path) as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*)" + _FROM + where, args).fetchone()[0]
            rows = db.execute(_FIELDS + _FROM + where + " ORDER BY f.root_id,f.relpath,f.id LIMIT ? OFFSET ?",
                              [*args, page_size, (page-1)*page_size])
            result = _with_tags(db,rows)
            scope = " WHERE id=?" if filters.get("root_id") else ""
            unindexed, offline = db.execute(
                "SELECT COUNT(*) FILTER (WHERE indexed_at IS NULL),COUNT(*) FILTER (WHERE online=0) "
                "FROM catalog_roots" + scope, (filters["root_id"],) if scope else ()).fetchone()
        return dict(rows=result,total=total,unindexed=unindexed,offline=offline)

    def detail(self, entry_id):
        with database_connection(self.path) as db:
            rows = _with_tags(db, db.execute(_FIELDS + _FROM + " WHERE f.id=?", (entry_id,)))
            return rows[0] if rows else None

    def add_tags(self, ids, tags):
        if isinstance(tags,str):
            tags = [tags]
        tags = sorted({s.strip() for s in tags if s.strip()})
        if not tags or any(len(tag)>100 or not tag.isprintable() for tag in tags):
            raise ValueError("标签须为 1 至 100 个可显示字符")
        with database_connection(self.path) as db:
            db.executemany("INSERT OR IGNORE INTO catalog_labels(entry_id,tag) VALUES(?,?)",
                           ((entry_id,tag) for entry_id in set(ids) for tag in tags))

    def annotate(self, entry_id, *, note=None, favorite=None):
        with database_connection(self.path) as db:
            db.execute("INSERT OR IGNORE INTO catalog_notes(entry_id) VALUES(?)",(entry_id,))
            db.execute("UPDATE catalog_notes SET note=COALESCE(?,note), favorite=COALESCE(?,favorite) "
                       "WHERE entry_id=?",(note,None if favorite is None else int(bool(favorite)),entry_id))

    def save_search(self, name, filters):
        name = name.strip()
        if not name or len(name)>100:
            raise ValueError("搜索名称须为 1 至 100 个字符")
        _where(filters)
        with database_connection(self.path) as db:
            db.execute("INSERT INTO catalog_saved_searches(name,spec) VALUES(?,?) "
                       "ON CONFLICT(name) DO UPDATE SET spec=excluded.spec",
                       (name,json.dumps(filters, ensure_ascii=False)))

    def saved_searches(self):
        with database_connection(self.path) as db:
            return {row["name"]:json.loads(row["spec"]) for row in db.execute(
                "SELECT * FROM catalog_saved_searches ORDER BY name")}

    def folder_for(self, entry_id):
        from .catalog import Catalog
        entry = self.detail(entry_id)
        if not entry or not entry["active"] or entry["stale"]:
            raise ValueError("条目尚待刷新，当前仅可查看上次见到的位置")
        Catalog(self.path).probe(entry["root_id"])
        with database_connection(self.path) as db:
            root = db.execute("SELECT * FROM catalog_roots WHERE id=?",(entry["root_id"],)).fetchone()
        if not root["online"]:
            raise ValueError("库离线或根目录身份不符，请检查或重新绑定")
        base = Path(root["path"])
        path = base / entry["relpath"]
        if not path.is_relative_to(base) or ".." in path.parts or path == base:
            raise ValueError("档案相对路径无效")
        with ExitStack() as stack:
            for folder in reversed(path.parents):
                dev, ino = stack.enter_context(pin_directory(folder))
                if folder == base and (str(dev),str(ino)) != (root["dev"],root["ino"]):
                    raise ValueError("档案库根目录已被替换")
            _, _, st = stack.enter_context(open_regular(path))
            if (str(st.st_dev),str(st.st_ino)) != (entry["dev"],entry["ino"]):
                raise ValueError("文件已被替换，请刷新档案库")
        return path.parent

    def history_for_entry(self, entry_id):
        from .catalog_events import matching_history
        with database_connection(self.path) as db:
            return matching_history(db,entry_id)

    def entries_for_item(self, item_id):
        from .catalog_events import matching_entries
        with database_connection(self.path) as db:
            ids = matching_entries(db,item_id)
            if not ids:
                return []
            slots = ",".join("?" for _ in ids)
            return _with_tags(db,db.execute(_FIELDS+_FROM+f" WHERE f.id IN ({slots})",ids))
