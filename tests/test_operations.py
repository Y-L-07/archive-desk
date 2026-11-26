import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from archive_desk.core import default_rules
from archive_desk.planning import preview
from archive_desk.scanner import scan
from archive_desk.journal import Journal
from archive_desk.operations import apply, undo


@unittest.skipUnless(os.name == "nt", "移动和撤销使用 Windows 文件句柄")
class MoveTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.temp = Path(scratch.name)
        self.root = self.temp / "samples"
        self.root.mkdir()
        self.db = self.temp / "state" / "history.sqlite3"
        self.journal = Journal(self.db)

    def make(self, name, content=b"sample"):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        return file

    def plans(self):
        return preview(self.root, scan(self.root, recursive=True), default_rules())

    def test_restart_and_undo_with_original_occupied_then_retry(self):
        a, b = self.make("a.txt", b"a"), self.make("b.txt", b"b")
        batch = apply(self.root, self.plans(), self.journal)
        self.assertFalse(a.exists() or b.exists())
        self.journal = Journal(self.db)
        self.assertEqual(self.journal.recover(), 0)
        self.assertEqual(self.journal.batches()[0]["moved"], 2)
        a.write_bytes(b"new a")
        undo(batch, self.journal)
        left, right = self.journal.items(batch)
        self.assertEqual((left["status"], left["undo_conflict"], right["status"]),
                         ("moved", 1, "undone"))
        self.assertEqual(b.read_bytes(), b"b")
        self.assertEqual(a.read_bytes(), b"new a")
        self.assertEqual(Path(left["target"]).read_bytes(), b"a")
        a.rename(self.root / "new-a.txt")
        undo(batch, self.journal)
        self.assertEqual(self.journal.batches()[0]["undone"], 2)
        self.assertEqual(a.read_bytes(), b"a")

    def test_partial_failure_logged_for_changed_and_missing_sources(self):
        a, b, c = self.make("a.txt"), self.make("b.txt"), self.make("c.txt")
        plans = self.plans()
        a.write_bytes(b"edited after preview")
        b.rename(self.root / "renamed")
        batch = apply(self.root, plans, self.journal)
        items = self.journal.items(batch)
        self.assertEqual([i["status"] for i in items], ["review", "review", "moved"])
        self.assertTrue(all(i["reason"] for i in items))
        self.assertEqual(self.journal.batches()[0]["review"], 2)
        self.assertFalse(c.exists())
        self.assertEqual(a.read_bytes(), b"edited after preview")

    def test_change_even_with_original_size_and_mtime_is_skipped(self):
        a = self.make("a.txt", b"AAA")
        plan, = self.plans()
        a.write_bytes(b"BBB")
        os.utime(a, ns=(a.stat().st_atime_ns, plan.file.mtime_ns))
        batch = apply(self.root, [plan], self.journal)
        self.assertEqual(self.journal.items(batch)[0]["status"], "review")
        self.assertEqual(a.read_bytes(), b"BBB")

    def test_digest_prevents_undo_after_same_size_and_mtime_edit(self):
        a = self.make("a.txt", b"AAA")
        plan, = self.plans()
        batch = apply(self.root, [plan], self.journal)
        plan.target.write_bytes(b"BBB")
        os.utime(plan.target, ns=(plan.target.stat().st_atime_ns, plan.file.mtime_ns))
        undo(batch, self.journal)
        self.assertEqual(self.journal.items(batch)[0]["undo_conflict"], 1)
        self.assertFalse(a.exists())
        self.assertEqual(plan.target.read_bytes(), b"BBB")

    def test_name_taken_immediately_before_atomic_rename_is_preserved(self):
        a = self.make("a.txt")
        plan, = self.plans()
        transition = self.journal.transition

        def race(item_id, previous, status, *args, **kwargs):
            transition(item_id, previous, status, *args, **kwargs)
            if status == "applying":
                plan.target.write_bytes(b"occupied")

        with patch.object(self.journal, "transition", race):
            batch = apply(self.root, [plan], self.journal)
        self.assertEqual(a.read_bytes(), b"sample")
        self.assertEqual(plan.target.read_bytes(), b"occupied")
        self.assertEqual(self.journal.items(batch)[0]["status"], "review")

    def test_ordinary_directory_replacement_after_preview(self):
        folder = self.root / "文档"
        folder.mkdir()
        self.make("a.txt")
        plan, = self.plans()
        folder.rename(self.root / "before")
        folder.mkdir()
        batch = apply(self.root, [plan], self.journal)
        self.assertEqual(self.journal.items(batch)[0]["status"], "review")
        self.assertFalse(plan.target.exists())
        self.assertTrue(plan.file.path.exists())

    def test_junction_substitution_cannot_move_outside_root(self):
        import _winapi
        folder = self.root / "文档"
        folder.mkdir()
        self.make("a.txt")
        plan, = self.plans()
        folder.rename(self.root / "before")
        outside = self.temp / "outside"
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(folder))
        batch = apply(self.root, [plan], self.journal)
        self.assertEqual(self.journal.items(batch)[0]["status"], "review")
        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_rechecks_escaped_destination(self):
        a = self.make("a.txt")
        plan, = self.plans()
        escaped = self.root / ".." / "a.txt"
        batch = apply(self.root, [replace(plan, target=escaped)], self.journal)
        self.assertEqual(self.journal.items(batch)[0]["status"], "review")
        self.assertFalse(escaped.exists())
        self.assertTrue(a.exists())
