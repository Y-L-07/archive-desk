"""Paged statistics over the published catalog, with exact saved-search drill-down."""
import sqlite3
from pathlib import Path

from .core import check_cancel
from .journal import database_connection


class _ByteSum:
    """Return an exact decimal string even for combined catalogs exceeding SQLite int64."""
    def __init__(self):
        self.total = 0

    def step(self, value):
        self.total += int(value)

    def finalize(self):
        return str(self.total)


def _directory_at(relpath, depth):
    parent, _, _ = relpath.rpartition("/")
    return "/".join(parent.split("/", depth)[:depth])


class CatalogStats:
    def __init__(self, db_path):
        self.path = Path(db_path)

    def group(self, group_by, *, root_id=None, depth=1, page=1, page_size=100, cancel=None):
        if group_by not in ("extension", "directory"):
            raise ValueError("请选择按扩展名或目录层级统计")
        if type(depth) is not int or not 1 <= depth <= 32:
            raise ValueError("目录层级须为 1 至 32 的整数")
        if type(page) is not int or page<1 or type(page_size) is not int or not 1<=page_size<=500:
            raise ValueError("统计页码从 1 开始，每页最多 500 项")
        if root_id is not None and (type(root_id) is not int or not 1<=root_id<2**63):
            raise ValueError("库编号无效")
        check_cancel(cancel)
        scope = [] if root_id is None else [root_id]
        where = " WHERE active=1" + (" AND root_id=?" if scope else "")
        with database_connection(self.path) as db:
            db.create_aggregate("archive_byte_sum",1,_ByteSum)
            db.create_function("archive_folder",2,_directory_at,deterministic=True)
            db.execute("BEGIN")
            if cancel:
                db.set_progress_handler(lambda: int(cancel.is_set()), 2048)
            try:
                if group_by == "directory":
                    axes, keys, args = "root_id, archive_folder(relpath,?) AS bucket", "1,2", [depth,*scope]
                else:
                    axes, keys, args = "NULL AS root_id, ext AS bucket", "2", scope
                # Keep all aggregation inside this connection. Only the requested
                # page travels to Qt, irrespective of the number of distinct groups.
                db.execute(f"""CREATE TEMP TABLE buckets AS
                    SELECT {axes}, COUNT(*) AS n, archive_byte_sum(size) AS bytes
                    FROM catalog_files {where} GROUP BY {keys}""", args)
                summary = db.execute("""SELECT COUNT(*),COALESCE(SUM(n),0),
                         COALESCE(archive_byte_sum(bytes),'0') FROM buckets""").fetchone()
                roots = db.execute("""SELECT COUNT(*) FILTER (WHERE indexed_at IS NULL),
                              COUNT(*) FILTER (WHERE online=0) FROM catalog_roots"""
                                   + (" WHERE id=?" if scope else ""),scope).fetchone()
                rows = []
                for row in db.execute("""SELECT b.*,r.name AS root_name FROM buckets b
                        LEFT JOIN catalog_roots r ON r.id=b.root_id
                        ORDER BY length(b.bytes) DESC,b.bytes DESC,b.n DESC,b.root_id,b.bucket
                        LIMIT ? OFFSET ?""",(page_size,(page-1)*page_size)):
                    key = row["bucket"]
                    if group_by == "directory":
                        label = key or "根目录文件"
                        filters = {"root_id":row["root_id"],"directory_bucket":{"path":key,"depth":depth}}
                    else:
                        label = key or "无扩展名"
                        filters = {"ext_exact":key}
                        if root_id is not None:
                            filters["root_id"] = root_id
                    rows.append(dict(label=label,key=key,count=row["n"],bytes=int(row["bytes"]),
                                     root_id=row["root_id"] if group_by=="directory" else root_id,
                                     root_name=row["root_name"],filters=filters))
                check_cancel(cancel)
                return dict(rows=rows,total=summary[0],files=summary[1],bytes=int(summary[2]),
                            unindexed=roots[0],offline=roots[1])
            except sqlite3.OperationalError:
                check_cancel(cancel)
                raise
            finally:
                db.set_progress_handler(None,0)
