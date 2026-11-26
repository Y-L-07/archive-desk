"""Measure SQLite record import and end-to-end paged searches on synthetic metadata."""
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_desk.catalog import Catalog, upsert_file
from archive_desk.catalog_search import CatalogSearch
from archive_desk.core import FileInfo
from archive_desk.journal import database_connection


def benchmark():
    print(platform.platform(), platform.machine(), "Python", platform.python_version(),
          "SQLite", sqlite3.sqlite_version)
    count = 100_000
    with tempfile.TemporaryDirectory(prefix="ArchiveDesk-index-benchmark-") as temporary:
        base = Path(temporary)
        path = base / "bench.sqlite3"
        catalog = Catalog(path)
        roots = []
        for label in ("项目归档", "移动硬盘"):
            (base / label).mkdir()
            roots.append(catalog.add_root(label, base / label))
        started = perf_counter()
        with database_connection(path) as db:
            for idx in range(count):
                stem = "交接清单" if idx % 5 == 0 else "项目记录"
                name = f"项目{idx % 113:03d}/{stem}-{idx:06d}.pdf"
                entry = FileInfo(Path(name), 2048 + idx, 1_750_000_000_000_000_000 + idx, 1, idx + 1)
                upsert_file(db, roots[idx % 2], name, entry)
                if idx % 500 == 499:
                    db.commit()
            db.execute("UPDATE catalog_roots SET indexed_at=CURRENT_TIMESTAMP,last_complete_at=CURRENT_TIMESTAMP,"
                       "published_state='complete',scan_state='complete'")
        print(f"import {count:,} rows, 500 per transaction: {perf_counter()-started:.3f} s")
        search = CatalogSearch(path)
        assert search.search(page_size=1)["total"] == count
        for name, filters, page, expected in (
                ("Chinese first page", {"text":"交接清单"}, 1, 20_000),
                ("Chinese page 100", {"text":"交接清单"}, 100, 20_000),
                ("all page 1000", {}, 1000, 100_000)):
            times = []
            for _ in range(7):
                start = perf_counter()
                reply = search.search(filters, page=page, page_size=100)
                times.append(1000 * (perf_counter() - start))
                assert reply["total"] == expected and len(reply["rows"]) == 100
                if filters:
                    assert all("交接清单" in row["name"] for row in reply["rows"])
            print(f"{name}: {expected:,} matches, median {median(times):.1f} ms "
                  f"(range {min(times):.1f}–{max(times):.1f}, n=7)")


if __name__ == "__main__":
    benchmark()
