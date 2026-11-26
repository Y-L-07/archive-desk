import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from archive_desk.catalog import Catalog
from archive_desk.core import ScanCancelled, default_rules
from archive_desk.journal import Journal, database_connection
from archive_desk.operations import apply, undo
from archive_desk.planning import preview
from archive_desk.scanner import scan


class CompleteBaselineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.path = self.base/"catalog.db"
        self.cat = Catalog(self.path)
        self.root = self.base/"库"
        self.root.mkdir()
        self.rid = self.cat.add_root("样例",self.root)

    def baseline(self, rid=None):
        rid = self.rid if rid is None else rid
        with database_connection(self.path) as db:
            header = db.execute("SELECT * FROM catalog_complete_baselines WHERE root_id=?", (rid,)).fetchone()
            rows = db.execute("SELECT * FROM catalog_complete_files WHERE root_id=? ORDER BY relpath", (rid,))
            return dict(header) if header else None, {r["relpath"]:dict(r) for r in rows}

    def partial(self, rid, blind):
        entries = self.cat._entries
        def read(folder):
            if folder == blind:
                raise PermissionError("模拟受限目录")
            return entries(folder)
        with patch.object(self.cat, "_entries", read):
            return self.cat.refresh(rid, hash_files=True)

    def test_partial_failure_cancel_and_next_complete_manifest(self):
        blind = self.root/"受限"
        blind.mkdir()
        (blind/"合同.txt").write_bytes(b"metadata")
        f = self.root/"旧名.txt"
        f.write_bytes(b"previous")
        old = self.cat.refresh(self.rid, hash_files=True)
        baseline = self.baseline()
        f.rename(self.root/"已改名.txt")
        (self.root/"新增.txt").write_bytes(b"added")
        result = self.partial(self.rid,blind)
        self.assertEqual(result["published_state"], "partial")
        self.assertEqual(result["baseline_generation"],old["generation"])
        self.assertEqual(self.baseline(),baseline)
        with patch.object(self.cat, "_entries", side_effect=PermissionError("离线")):
            with self.assertRaises(PermissionError):
                self.cat.refresh(self.rid)
        cancel = Event()
        cancel.set()
        with self.assertRaises(ScanCancelled):
            self.cat.refresh(self.rid,cancel=cancel)
        self.assertEqual(self.baseline(),baseline)
        # A failure after replacing even the baseline must roll back both views.
        save = self.cat._save_complete
        def fail(*args):
            save(*args)
            raise OSError("模拟提交前失败")
        with patch.object(self.cat, "_save_complete",fail), self.assertRaises(OSError):
            self.cat.refresh(self.rid,hash_files=True)
        self.assertEqual(self.baseline(),baseline)
        new = self.cat.refresh(self.rid,hash_files=True)
        header, rows = self.baseline()
        self.assertEqual((header["indexed_at"],header["generation"]),(new["indexed_at"],new["generation"]))
        self.assertEqual(set(rows),{"受限/合同.txt","新增.txt","已改名.txt"})

    def test_transfers_modify_live_index_without_altering_the_last_full_scan(self):
        src = self.root/"文件.txt"
        src.write_bytes(b"original")
        self.cat.refresh(self.rid,hash_files=True)
        before = self.baseline()
        journal = Journal(self.path)
        bid = apply(self.root,preview(self.root,scan(self.root),default_rules()),journal)
        self.assertFalse(src.exists())
        self.assertEqual(self.baseline(),before)
        with database_connection(self.path) as db:
            live = db.execute("SELECT relpath FROM catalog_files WHERE root_id=? AND active=1",(self.rid,)).fetchall()
            self.assertEqual([r[0] for r in live],["文档/文件.txt"])
        undo(bid,journal)
        self.assertEqual(src.read_bytes(),b"original")
        self.assertEqual(self.baseline(),before)

    def test_first_partial_has_no_baseline_and_empty_complete_has_header(self):
        denied = self.root/"受限"
        denied.mkdir()
        self.partial(self.rid,denied)
        self.assertEqual(self.baseline(),(None,{}))
        self.assertIsNone(self.cat.roots()[0]["baseline_indexed_at"])
        complete = self.cat.refresh(self.rid)
        header, files = self.baseline()
        self.assertEqual(header["generation"],complete["generation"])
        self.assertEqual(files,{})
        (self.root/"临时.txt").write_bytes(b"new")
        self.partial(self.rid,denied)
        self.assertEqual(self.baseline(),(header,{}))

    def test_v3_upgrade_backfills_only_provably_complete_manifests(self):
        (self.root/"可信.txt").write_bytes(b"clean")
        complete = self.cat.refresh(self.rid,hash_files=True)
        partial_root = self.base/"partial"
        partial_root.mkdir()
        partial_id = self.cat.add_root("部分",partial_root)
        denied = partial_root/"受限"
        denied.mkdir()
        self.cat.refresh(partial_id)
        self.partial(partial_id,denied)
        changed_root = self.base/"moved"
        changed_root.mkdir()
        changed_id = self.cat.add_root("扫描后移动过",changed_root)
        (changed_root/"moved.txt").write_bytes(b"moving")
        self.cat.refresh(changed_id)
        journal = Journal(self.path)
        apply(changed_root,preview(changed_root,scan(changed_root),default_rules()),journal)
        # Exactly the two new tables are absent in the older V3 layout.
        with database_connection(self.path) as db:
            db.execute("DROP TABLE catalog_complete_files")
            db.execute("DROP TABLE catalog_complete_baselines")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],3)
        self.cat = Catalog(self.path)
        header,files = self.baseline()
        self.assertEqual(header["generation"],complete["generation"])
        self.assertEqual(set(files),{"可信.txt"})
        self.assertEqual(self.baseline(partial_id),(None,{}))
        self.assertEqual(self.baseline(changed_id),(None,{}))
        self.cat.refresh(changed_id)
        self.assertIsNotNone(self.baseline(changed_id)[0])
