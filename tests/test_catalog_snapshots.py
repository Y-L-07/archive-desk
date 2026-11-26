"""在临时 SQLite 目录上验证可复查的快照证据和孤立的双库演示。"""
import codecs
import csv
import hashlib
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from threading import Event
from unittest.mock import patch

from archive_desk.catalog import ensure_catalog_schema
from archive_desk.catalog_snapshots import SnapshotStore, export_diff
from archive_desk.core import ScanCancelled
from archive_desk.journal import database_connection


def sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.db = self.scratch / "catalog.db"
        with database_connection(self.db) as db:
            ensure_catalog_schema(db)
            for n in (1, 2):
                db.execute("INSERT INTO catalog_roots(id,name,path,dev,ino) VALUES(?,?,?,?,?)",
                           (n, f"资料库{n}", str(self.scratch / str(n)), "dev", str(n)))
        self.store = SnapshotStore(self.db)

    def publish(self, rows, *, issues=(), root=1):
        with database_connection(self.db) as db:
            db.execute("DELETE FROM catalog_files WHERE root_id=?", (root,))
            db.execute("DELETE FROM catalog_issues WHERE root_id=?", (root,))
            state = "partial" if issues else "complete"
            db.execute("""UPDATE catalog_roots SET indexed_at='2026-11-26T13:45:00+08:00',
                          generation=generation+1,scan_state=?,published_state=? WHERE id=?""",
                       (state, state, root))
            db.executemany("INSERT INTO catalog_issues(root_id,relpath,reason) VALUES(?,?,?)",
                           ((root, p, "不可访问") for p in issues))
            for i, (p, content, stamp, known, stale) in enumerate(rows):
                db.execute("""INSERT INTO catalog_files
                    (root_id,relpath,name,ext,size,mtime_ns,digest,dev,ino,stale)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (root, p, Path(p).name, Path(p).suffix.lower(),
                    len(content.encode()), stamp, sha(content) if known else None, "dev", str(i), stale))

    def test_manifest_comparison_counts_whole_snapshot_uniqueness_and_blind_prefixes(self):
        common = [("参考/仍在.txt", "重复", 1, True, False),
                  ("普通.txt", "ok", 1, True, False)]
        before = [("旧名/唯一.txt", "move", 1, True, False),
                  ("旧名/多份.txt", "重复", 1, True, False),
                  ("待改.txt", "old", 1, True, False),
                  ("仅元数据.txt", "xxx", 1, False, False),
                  ("消失.txt", "gone", 1, True, False),
                  ("盲区/保留.txt", "stale", 1, True, False),
                  ("盲区/不再出现.txt", "maybe", 1, True, False),
                  ("盲区之外/缺失.txt", "sure", 1, True, False)]
        self.publish(common + before, issues=["曾经无法访问"])
        old = self.store.create(1, "一")
        after = [("新名/唯一.txt", "move", 2, True, False),
                 ("新名/多份.txt", "重复", 1, True, False),
                 ("待改.txt", "new", 1, True, False),
                 ("仅元数据.txt", "yyy", 2, False, False),
                 ("新增.txt", "new file", 1, True, False),
                 ("盲区/保留.txt", "stale", 1, True, True),
                 ("曾经无法访问/报告.txt", "not proven new", 1, True, False)]
        self.publish(common + after, issues=["盲区"])
        new = self.store.create(1, "二")
        diffs = self.store.compare(old, new)
        self.assertEqual(Counter(r["status"] for r in diffs),
                         {"added": 1, "missing": 2, "changed": 1, "moved": 1, "uncertain": 6})
        move, = [r for r in diffs if r["status"] == "moved"]
        self.assertEqual((move["old_path"], move["new_path"]), ("旧名/唯一.txt", "新名/唯一.txt"))
        ambiguous = [r for r in diffs if "无法唯一配对" in r["reason"]]
        self.assertEqual(len(ambiguous), 2)
        self.assertTrue(all(bool(r["old_path"]) != bool(r["new_path"]) for r in ambiguous))
        changed, = [r for r in diffs if r["status"] == "changed"]
        self.assertEqual((changed["old_digest"], changed["new_digest"]), (sha("old"), sha("new")))
        self.assertEqual(self.store.issues(new)[0]["relpath"], "盲区")
        self.assertEqual(self.store.list_snapshots(1)[0]["scan_state"], "partial")

    def test_prior_published_snapshot_offline_reverse_compare_and_export(self):
        self.publish([("=公式.txt", "x", 1, True, False)])
        old = self.store.create(1, "原始清单")
        self.publish([])
        with database_connection(self.db) as db:
            db.execute("UPDATE catalog_roots SET scan_state='failed',online=0 WHERE id=1")
        new = self.store.create(1, "上次发布")
        rows = self.store.compare(old, new)
        self.assertEqual([r["status"] for r in rows], ["missing"])
        self.assertEqual(self.store.compare(new, old)[0]["status"], "added")
        out = self.scratch / "差异表.csv"
        export_diff(out, rows)
        self.assertTrue(out.read_bytes().startswith(codecs.BOM_UTF8))
        with out.open(encoding="utf-8-sig", newline="") as handle:
            header, row = csv.reader(handle)
        self.assertIn("判断依据", header)
        self.assertEqual(row[:2], ["缺失", "'=公式.txt"])
        with self.assertRaises(FileExistsError):
            export_diff(out, [])
        self.assertEqual(self.store.compare(old, new), rows)
        self.assertFalse((self.scratch / "1").exists())

    def test_never_scanned_invalid_scope_duplicate_name_and_cancellation(self):
        with self.assertRaises(ValueError):
            self.store.create(1, "未扫描")
        self.publish([])
        old = self.store.create(1, "原始")
        with self.assertRaises(ValueError):
            self.store.create(1, "原始")
        self.publish([], root=2)
        other = self.store.create(2, "原始")
        with self.assertRaises(ValueError):
            self.store.compare(old, other)
        stop = Event()
        stop.set()
        with self.assertRaises(ScanCancelled):
            self.store.create(1, "中断", cancel=stop)
        self.assertEqual(len(self.store.list_snapshots(1)), 1)
        with self.assertRaises(ScanCancelled):
            self.store.compare(old, other, cancel=stop)

    def test_cancel_after_manifest_insert_rolls_back_all_rows(self):
        self.publish([("样例.txt", "hello", 1, True, False)])
        # Interrupt on the final cancellation check, after all INSERTs, before
        # database_connection commits. Existing snapshots must stay intact.
        first = self.store.create(1, "已发布")
        def interrupt_on_exit():
            yield False
            while True:
                yield True
        stops = interrupt_on_exit()
        flag = Event()
        with patch.object(flag, "is_set", side_effect=lambda: next(stops)):
            with self.assertRaises(ScanCancelled):
                self.store.create(1, "新快照", flag)
        self.assertEqual([r["id"] for r in self.store.list_snapshots(1)], [first])
        with database_connection(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM catalog_snapshot_files").fetchone()[0], 1)


class CatalogDemoTests(unittest.TestCase):
    def test_two_real_roots_two_generations_partial_coverage_and_offline(self):
        from archive_desk.catalog import Catalog
        from archive_desk.catalog_demo import create_catalog_demo
        with tempfile.TemporaryDirectory() as scratch:
            workspace = Path(scratch)
            db = workspace / "demo.db"
            messages = []
            with patch.object(tempfile, "tempdir", scratch):
                data = create_catalog_demo(db, progress=messages.append)
            self.assertTrue(data["directory"].is_relative_to(workspace))
            self.assertTrue(data["offline_path"].is_dir())
            store = SnapshotStore(db)
            self.assertEqual(len(store.list_snapshots(data["root_id"])), 2)
            self.assertEqual(len(store.list_snapshots(data["offline_id"])), 2)
            roots = {row["id"]: row for row in Catalog(db).roots()}
            self.assertFalse(roots[data["offline_id"]]["online"])
            self.assertEqual(roots[data["root_id"]]["published_state"], "partial")
            results = store.compare(data["old_id"], data["new_id"])
            self.assertEqual(Counter(r["status"] for r in results),
                             {"added": 1, "missing": 1, "changed": 1, "moved": 1, "uncertain": 3})
            offline = store.compare(data["offline_old_id"], data["offline_new_id"])
            self.assertEqual([r["status"] for r in offline], ["uncertain"])
            self.assertGreaterEqual(len(messages), 4)


if __name__ == "__main__":
    unittest.main()
