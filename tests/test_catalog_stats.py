import tempfile
import unittest
from pathlib import Path
from threading import Event

from archive_desk.catalog import Catalog, upsert_file
from archive_desk.catalog_search import CatalogSearch
from archive_desk.catalog_stats import CatalogStats
from archive_desk.core import FileInfo, ScanCancelled
from archive_desk.journal import database_connection


class CatalogStatsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.db = self.base/"index.db"
        cat = Catalog(self.db)
        self.roots = []
        for name in ("图书室","移动盘"):
            folder = self.base/name
            folder.mkdir()
            self.roots.append(cat.add_root(name,folder))
        with database_connection(self.db) as db:
            db.execute("UPDATE catalog_roots SET online=0 WHERE id=?", (self.roots[1],))
            # Published test records also cover distinct names allowed on case-sensitive NTFS.
            relpaths = ["README",".env","ROOT.TXT","A/a.txt","a/different.txt","A/B/a.bin",
                        "A/B/深/nested.txt","A/Bnot/sibling.txt","财务%_^/报告.pdf","财务X1^/报告.pdf"]
            for i, name in enumerate(relpaths):
                self._insert(db,self.roots[0],name,i+1,i+1)
            self._insert(db,self.roots[1],"A/a.txt",4,60)
            tombstone = self._insert(db,self.roots[0],"ignored.bin",99,1000)
            db.execute("UPDATE catalog_files SET active=0 WHERE id=?", (tombstone,))
        self.stats = CatalogStats(self.db)
        self.search = CatalogSearch(self.db)

    def _insert(self, db, rid, relpath, ino, size):
        info = FileInfo(self.base/relpath,size,100,rid,ino)
        return upsert_file(db,rid,relpath,info)

    def verify(self, mode, **kwargs):
        data = self.stats.group(mode, **kwargs)
        self.assertEqual(sum(r["count"] for r in data["rows"]),data["files"])
        self.assertEqual(sum(r["bytes"] for r in data["rows"]),data["bytes"])
        all_ids = set()
        for i, row in enumerate(data["rows"]):
            self.search.save_search("精确条件",row["filters"])
            result = self.search.search(self.search.saved_searches()["精确条件"],page_size=500)
            self.assertEqual(result["total"],row["count"])
            self.assertEqual(sum(f["size"] for f in result["rows"]),row["bytes"])
            ids = {f["id"] for f in result["rows"]}
            self.assertFalse(all_ids & ids)
            all_ids.update(ids)
        return data

    def test_extension_empty_multiple_libraries_and_unavailable_library(self):
        all_files = self.verify("extension")
        self.assertEqual((all_files["files"],all_files["bytes"],all_files["offline"]), (11,115,1))
        no_suffix = next(r for r in all_files["rows"] if not r["key"])
        self.assertEqual((no_suffix["count"],no_suffix["label"]), (2,"无扩展名"))
        self.assertEqual(self.verify("extension",root_id=self.roots[1])["bytes"],60)

    def test_shallow_files_and_deep_descendants_map_to_exact_search(self):
        for depth in (1,2,3,12):
            with self.subTest(depth=depth):
                self.verify("directory",depth=depth)
        groups = self.verify("directory",root_id=self.roots[0],depth=2)
        counts = {r["key"]:r["count"] for r in groups["rows"]}
        self.assertEqual(counts[""],3)
        self.assertEqual(counts["A"],1)
        self.assertEqual(counts["a"],1)
        self.assertEqual(counts["A/B"],2)
        self.assertEqual(counts["财务%_^"],1)

    def test_pagination_does_not_truncate_totals_or_merge_large_sums(self):
        with database_connection(self.db) as db:
            for i in range(3):
                self._insert(db,self.roots[0],f"huge/{i}.zip",100+i,2**62)
        all_rows = self.verify("extension")
        self.assertEqual(all_rows["rows"][0]["bytes"],3*2**62)
        accumulated = []
        for page in range(1,all_rows["total"]+1):
            data = self.stats.group("extension",page=page,page_size=1)
            self.assertEqual(data["bytes"],all_rows["bytes"])
            accumulated.extend(data["rows"])
        self.assertEqual(accumulated,all_rows["rows"])
        self.assertFalse(self.stats.group("extension",page=99,page_size=1)["rows"])

    def test_empty_cancel_and_invalid_bucket(self):
        self.assertEqual(self.stats.group("directory",root_id=99)["files"],0)
        cancel = Event()
        cancel.set()
        with self.assertRaises(ScanCancelled):
            self.stats.group("extension",cancel=cancel)
        for invalid in ({"depth":1,"path":"a/b"},{"depth":1,"path":".."},{"depth":0,"path":""}):
            with self.assertRaises(ValueError):
                self.search.search({"directory_bucket":invalid})
