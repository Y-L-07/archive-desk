import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from archive_desk.catalog import Catalog
from archive_desk.catalog_search import CatalogSearch
from archive_desk.core import default_rules
from archive_desk.journal import Journal
from archive_desk.operations import apply, undo
from archive_desk.planning import preview
from archive_desk.scanner import scan


@unittest.skipUnless(os.name == "nt", "档案库绑定使用 Windows 目录句柄")
class SearchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.db = self.base / "history.sqlite3"
        self.root = self.base / "业务"
        self.root.mkdir()
        self.catalog = Catalog(self.db)
        self.library = self.catalog.add_root("项目", self.root)
        self.search = CatalogSearch(self.db)
        self.journal = Journal(self.db)

    def plan(self, target=None):
        return preview(self.root, scan(self.root), default_rules(), target_root=target)

    def test_filters_marks_searches_and_offline_results(self):
        folder = self.root / "杭州项目"
        folder.mkdir()
        for idx in range(12):
            file = folder / f"第{idx:02d}次交接.PDF"
            file.write_bytes(bytes(idx + 1))
            dt = datetime(2025, 4, idx + 1, 13).timestamp()
            os.utime(file, (dt, dt))
        removable = self.base / "硬盘"
        removable.mkdir()
        other = self.catalog.add_root("移动盘", removable)
        (removable / "交接说明.txt").write_text("ABC", encoding="utf-8")
        self.assertEqual(self.search.search()["unindexed"], 2)
        self.catalog.refresh(self.library)
        self.catalog.refresh(other)
        self.assertEqual(self.search.search({"text":"交接"})["total"], 13)
        filters = dict(text="杭州项目", date_from="2025-04-02", date_to="2025-04-06",
                       ext="PDF", min_size=2, max_size=6)
        rows = self.search.search(filters, page=2, page_size=2)
        self.assertEqual((rows["total"], [x["size"] for x in rows["rows"]]), (5, [4, 5]))
        ids = [r["id"] for r in rows["rows"]]
        self.search.add_tags(ids, ["复核", "四月"])
        self.search.annotate(ids[0], favorite=True, note="已签收")
        self.search.save_search("待复核", {"tag":"复核", "favorite":True})
        self.catalog.refresh(self.library)
        saved = CatalogSearch(self.db).saved_searches()["待复核"]
        marked, = self.search.search(saved)["rows"]
        self.assertEqual((marked["id"], marked["note"], set(marked["tags"])),
                         (ids[0], "已签收", {"复核","四月"}))
        removable.rename(self.base / "已拔出")
        self.assertFalse(self.catalog.probe(other))
        last, = self.search.search({"online":False})["rows"]
        self.assertEqual(last["relpath"], "交接说明.txt")
        with self.assertRaises(ValueError):
            self.search.folder_for(last["id"])
        self.assertEqual(self.search.folder_for(ids[0]), folder)
        empty = self.search.search({"text":"不存在"})
        self.assertEqual((empty["total"], empty["offline"], empty["unindexed"]), (0, 1, 0))

    def test_committed_move_and_undo_link_same_entry_and_preserve_annotations(self):
        a = self.root / "审批.txt"
        a.write_bytes(b"one")
        self.catalog.refresh(self.library)
        initial, = self.search.search()["rows"]
        entry_id = initial["id"]
        self.search.annotate(entry_id, note="合同")
        dest = self.base / "档案盘"
        dest.mkdir()
        other_lib = self.catalog.add_root("目标卷", dest)
        batch = apply(self.root, self.plan(dest), self.journal, target_root=dest)
        item, = self.journal.items(batch)
        self.assertEqual(item["status"], "moved")
        filed, = self.search.entries_for_item(item["id"])
        self.assertEqual((filed["id"],filed["root_id"],filed["relpath"],filed["note"]),
                         (entry_id,other_lib,"文档/审批.txt","合同"))
        self.assertEqual(self.search.history_for_entry(entry_id)[0]["batch_id"], batch)
        self.assertEqual(self.search.search({"root_id":self.library})["total"], 0)
        a.write_bytes(b"occupied")
        undo(batch, self.journal)
        self.assertEqual(self.journal.items(batch)[0]["undo_conflict"], 1)
        self.assertEqual(self.search.detail(entry_id)["root_id"], other_lib)
        a.rename(self.root / "占用.txt")
        undo(batch, self.journal)
        restored, = self.search.search()["rows"]
        self.assertEqual((restored["id"],restored["root_id"],restored["relpath"],restored["note"]),
                         (entry_id,self.library,"审批.txt","合同"))
        self.assertEqual(self.search.history_for_entry(entry_id)[0]["status"], "undone")

    def test_failed_or_unconfirmed_move_has_no_index_effect(self):
        src = self.root / "待审.txt"
        src.write_text("初稿", encoding="utf-8")
        self.catalog.refresh(self.library)
        plans = self.plan()
        src.write_text("修改过的内容", encoding="utf-8")
        batch = apply(self.root, plans, self.journal)
        item, = self.journal.items(batch)
        row, = self.search.search()["rows"]
        self.assertEqual((item["status"],row["relpath"]), ("review",src.name))
        self.assertEqual(self.search.entries_for_item(item["id"]), [])
        self.assertEqual(self.search.history_for_entry(row["id"]), [])

    def test_archive_before_index_uses_identity_to_resolve_history(self):
        src = self.root / "过去.txt"
        src.write_bytes(b"old")
        batch = apply(self.root,self.plan(),self.journal)
        item, = self.journal.items(batch)
        # Start a second, overlapping library after the historical move.
        nested = self.catalog.add_root("文档", self.root / "文档")
        self.catalog.refresh(nested, hash_files=True)
        row, = self.search.search({"root_id":nested})["rows"]
        self.assertIn(row["id"], [r["id"] for r in self.search.entries_for_item(item["id"])])
        self.assertEqual(self.search.history_for_entry(row["id"])[0]["id"], item["id"])
        target = Path(item["target"])
        target.rename(self.base / "original")
        target.write_bytes(b"new")
        self.catalog.refresh(nested, hash_files=True)
        replacement, = self.search.search({"root_id":nested})["rows"]
        self.assertNotEqual(row["id"], replacement["id"])
        self.assertEqual(self.search.history_for_entry(replacement["id"]), [])
        self.assertNotIn(replacement["id"], [r["id"] for r in self.search.entries_for_item(item["id"])])
