"""Only isolated generated files are used, including on the second real volume."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from archive_desk import crossvolume
from archive_desk.core import default_rules
from archive_desk.journal import Journal
from archive_desk.operations import apply, resume, undo
from archive_desk.planning import preview
from archive_desk.scanner import scan


def interrupt(scratch, archive, mode, phase):
    def die(here, item_id):
        if here == phase:
            os._exit(81)
    crossvolume.checkpoint = die
    j = Journal(scratch / "db")
    if mode == "undo":
        undo(j.batches()[0]["id"], j)
    else:
        root = scratch / "input"
        apply(root, preview(root, scan(root), default_rules(), target_root=archive),
              j, target_root=archive)


class TransferTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="archive-desk-transaction-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.root = self.scratch / "input"
        self.root.mkdir()
        self.j = Journal(self.scratch / "db")
        self.src = self.root / "样例.txt"
        self.content = b"0123456789" * 230000
        self.src.write_bytes(self.content)

    def other_volume(self):
        base = Path(os.environ.get("ARCHIVE_DESK_CROSS_TEST_DIR", PROJECT))
        if base.stat().st_dev == self.root.stat().st_dev:
            self.skipTest("需要设置 ARCHIVE_DESK_CROSS_TEST_DIR 指向另一卷的可写目录")
        temp = tempfile.TemporaryDirectory(dir=base, prefix="archive-desk-test-")
        self.addCleanup(temp.cleanup)
        return Path(temp.name)

    def plan(self, archive=None):
        return preview(self.root, scan(self.root), default_rules(), target_root=archive)

    def child_crash(self, archive, mode, stage):
        child = subprocess.run([sys.executable, __file__, str(self.scratch), str(archive),
                                mode, stage], cwd=PROJECT, timeout=30)
        self.assertEqual(child.returncode, 81)
        self.j = Journal(self.scratch / "db")
        batch = self.j.batches()[0]["id"]
        return batch, self.j.items(batch)[0]

    def cross_crash(self, mode, stage):
        archive = self.other_volume()
        plan, = self.plan(archive)
        if mode == "undo":
            apply(self.root, [plan], self.j, target_root=archive)
        old, new = (plan.target, self.src) if mode == "undo" else (self.src, plan.target)
        batch, item = self.child_crash(archive, mode, stage)
        partial = Path(json.loads(item["transfer"])["temp"])
        self.assertEqual(self.j.recover(), 1)
        self.assertEqual(self.j.items(batch)[0]["status"], "resumable")
        self.assertEqual(old.read_bytes(), self.content)
        if stage == "copy_chunk":
            self.assertFalse(new.exists())
            self.assertEqual(partial.read_bytes(), self.content[:crossvolume.BLOCK])
        else:
            self.assertFalse(partial.exists())
            self.assertEqual(new.read_bytes(), self.content)
        resume(batch, self.j)
        state = "undone" if mode == "undo" else "moved"
        self.assertEqual(self.j.items(batch)[0]["status"], state)
        self.assertFalse(old.exists())
        self.assertFalse(partial.exists())
        self.assertEqual(new.read_bytes(), self.content)
        if mode == "apply":
            undo(batch, self.j)
            self.assertEqual(self.j.items(batch)[0]["status"], "undone")
            self.assertEqual(self.src.read_bytes(), self.content)

    def test_half_copied_apply(self):
        self.cross_crash("apply", "copy_chunk")

    def test_committed_apply(self):
        self.cross_crash("apply", "committed")

    def test_half_copied_undo(self):
        self.cross_crash("undo", "copy_chunk")

    def test_committed_undo(self):
        self.cross_crash("undo", "committed")

    def test_replaced_partial_is_left_for_manual_inspection(self):
        archive = self.other_volume()
        batch, item = self.child_crash(archive, "apply", "copy_chunk")
        tmp = Path(json.loads(item["transfer"])["temp"])
        tmp.rename(tmp.with_suffix(".saved"))
        tmp.write_bytes(b"foreign recovery data")
        self.j.recover()
        self.assertEqual(self.j.items(batch)[0]["status"], "uncertain")
        resume(batch, self.j)
        self.assertEqual(tmp.read_bytes(), b"foreign recovery data")
        self.assertEqual(self.src.read_bytes(), self.content)
        self.assertFalse(Path(item["target"]).exists())

    def test_source_change_after_recovery_blocks_completion(self):
        archive = self.other_volume()
        batch, item = self.child_crash(archive, "apply", "committed")
        self.j.recover()
        self.src.write_bytes(b"new source")
        resume(batch, self.j)
        self.assertEqual(self.j.items(batch)[0]["status"], "uncertain")
        self.assertEqual(self.src.read_bytes(), b"new source")
        self.assertEqual(Path(item["target"]).read_bytes(), self.content)

    def test_partial_cancel_can_resume_and_other_items_stay_cancelled(self):
        archive = self.other_volume()
        (self.root / "额外.txt").write_bytes(self.content)
        event = Event()
        with patch.object(crossvolume, "checkpoint", lambda stage, _: event.set()):
            batch = apply(self.root, self.plan(archive), self.j, target_root=archive, cancel=event)
        first, other = self.j.items(batch)
        self.assertEqual((first["status"], other["status"]), ("resumable", "cancelled"))
        self.assertTrue(Path(first["source"]).exists() and Path(other["source"]).exists())
        resume(batch, self.j)
        self.assertEqual([r["status"] for r in self.j.items(batch)], ["moved", "cancelled"])

    def test_ads_prevents_loss_on_cross_volume(self):
        archive = self.other_volume()
        ads = Path(str(self.src) + ":extra")
        ads.write_bytes(b"important stream")
        plan, = self.plan(archive)
        batch = apply(self.root, [plan], self.j, target_root=archive)
        self.assertIn("数据流", self.j.items(batch)[0]["reason"])
        self.assertFalse(plan.target.exists())
        self.assertEqual(self.src.read_bytes(), self.content)
        self.assertEqual(ads.read_bytes(), b"important stream")

    def test_pending_recover_reads_only_then_rechecks_current_rule(self):
        plan, = self.plan()
        bid, _ = self.j.begin_batch(self.root, [plan])
        self.assertEqual(self.j.recover(), 1)
        self.assertEqual(self.j.items(bid)[0]["status"], "resumable")
        self.assertFalse(plan.target.parent.exists())
        rules = [replace(r, target="新位置") for r in default_rules()]
        resume(bid, self.j, rules=rules)
        self.assertEqual(self.j.items(bid)[0]["status"], "review")
        self.assertTrue(self.src.exists())
        self.assertFalse(plan.target.parent.exists())

    def test_committed_item_does_not_block_later_failure_reporting(self):
        (self.root / "额外.txt").write_bytes(b"another")
        plans = self.plan()
        original = crossvolume.rename_without_replace
        def one_failure(handle, target):
            if target.name == self.src.name:
                raise PermissionError("模拟文件受保护")
            original(handle, target)
        with patch.object(crossvolume, "rename_without_replace", one_failure):
            batch = apply(self.root, plans, self.j)
        self.assertEqual({i["status"] for i in self.j.items(batch)}, {"failed", "moved"})


class MigrationTest(unittest.TestCase):
    def test_v1_ids_paths_and_undo_survive_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orig = root / "旧.txt"
            parent = root / "文档"
            parent.mkdir()
            dst = parent / orig.name
            dst.write_bytes(b"old contents")
            st = dst.stat()
            evidence = {"file": [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns],
                        "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(),
                        "directories": [[str(p), p.stat().st_dev, p.stat().st_ino] for p in (root, parent)]}
            db = root / "legacy.db"
            conn = sqlite3.connect(db)
            with conn:
                conn.executescript("""
                    CREATE TABLE batches(id INTEGER PRIMARY KEY, root TEXT, created_at TEXT);
                    CREATE TABLE items(id INTEGER PRIMARY KEY, batch_id INTEGER, source TEXT,
                        target TEXT,rule TEXT,status TEXT,reason TEXT,evidence TEXT,
                        undo_conflict INTEGER DEFAULT 0);
                """)
                conn.execute("INSERT INTO batches VALUES(17,?,'2020-01-01')", (str(root),))
                for idx, status in enumerate(("moved", "applying", "pending")):
                    conn.execute("INSERT INTO items VALUES(?,17,?,?,'文档',?,'',?,0)",
                                 (21 + idx, str(orig), str(dst), status, json.dumps(evidence)))
            conn.close()
            journal = Journal(db)
            self.assertEqual(journal.recover(), 2)
            self.assertEqual([i["status"] for i in journal.items(17)], ["moved","uncertain","skipped"])
            undo(17, journal)
            self.assertEqual(journal.item(21)["status"], "undone")
            self.assertEqual(orig.read_bytes(), b"old contents")
            self.assertFalse(dst.exists())


if __name__ == "__main__":
    if len(sys.argv) == 5:
        interrupt(Path(sys.argv[1]), Path(sys.argv[2]), *sys.argv[3:])
    else:
        unittest.main()
