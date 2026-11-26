import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from archive_desk.core import ScanCancelled, default_rules
from archive_desk.inbox import Inbox
from archive_desk.journal import Journal
from archive_desk.operations import apply, undo
from archive_desk.planning import preview
from archive_desk.scanner import scan, snapshot_file


class InboxTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.root = self.base / "下载样例"
        self.root.mkdir()
        self.journal = Journal(self.base / "app" / "history.db")
        self.box = Inbox(self.journal.path, stable_seconds=5)

    def poll(self, now):
        with patch("archive_desk.inbox.time.time", return_value=now):
            return self.box.poll()

    def test_stability_restarts_on_growth_events_coalesce_and_ack_checks_version(self):
        f = self.root / "resumé.txt"
        f.write_bytes(b"first")
        self.assertIsNone(self.poll(100).root)
        self.box.configure(self.root)
        self.assertEqual(self.poll(100).pending, 1)
        self.assertFalse(self.poll(104).candidates)
        f.write_bytes(b"second version")
        self.assertFalse(self.poll(105).candidates)
        self.assertFalse(self.poll(109).candidates)
        ready = self.poll(110)
        self.assertEqual(ready.candidates, [snapshot_file(f)])
        self.assertEqual(self.poll(115).candidates, ready.candidates)
        f.write_bytes(b"third")
        self.assertEqual(self.poll(116).pending, 1)
        latest = self.poll(121).candidates
        self.box.acknowledge(ready.candidates)
        self.assertEqual(self.box.candidates(), latest)
        self.box.acknowledge(latest)
        self.assertEqual(self.poll(125).candidates, [])
        self.assertEqual(f.read_bytes(), b"third")
        self.assertEqual(self.journal.batches(), [])

    def test_reopen_discovers_files_from_downtime_and_keeps_receipts(self):
        a = self.root / "a.txt"
        a.write_text("old")
        self.box.configure(self.root)
        self.poll(10)
        self.box.acknowledge(self.poll(20).candidates)
        b = self.root / "offline.txt"
        b.write_text("while closed")
        self.box = Inbox(self.journal.path)
        self.assertEqual(self.box.root(), self.root)
        self.assertEqual(self.poll(100).pending, 1)
        self.assertEqual([f.path for f in self.poll(106).candidates], [b])
        self.box = Inbox(self.journal.path)
        self.assertEqual([f.path for f in self.box.candidates()], [b])
        self.box.configure(None)
        self.assertIsNone(Inbox(self.journal.path).root())
        self.assertEqual(self.poll(120).candidates, [])

    def test_ignores_temp_files_junctions_subdirs_and_journal_sidecars(self):
        import _winapi
        root = self.journal.path.parent
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("unseen")
        _winapi.CreateJunction(str(outside), str(root / "下载链接"))
        (root / "older").mkdir()
        (root / "older" / "hidden.txt").write_text("nested")
        for name in ("a.part", "b.CRDOWNLOAD", ".archive-desk-1-abc.part", "report.tmp", "instance.lock"):
            (root / name).write_text("ignore")
        f = root / "finished.zip"
        f.write_text("eligible")
        self.box.configure(root)
        self.assertEqual(self.poll(1).pending, 1)
        self.assertEqual([f.path for f in self.poll(7).candidates], [f])
        self.assertEqual(list(outside.iterdir()), [outside / "private.txt"])

    def test_ignores_own_moves_and_undos_without_masking_new_file_at_same_path(self):
        src = self.root / "record.txt"
        src.write_bytes(b"own output")
        plan, = preview(self.root, scan(self.root), default_rules())
        batch = apply(self.root, [plan], self.journal)
        self.box.configure(plan.target.parent)
        self.poll(1)
        self.assertFalse(self.poll(7).candidates)
        self.box.configure(self.root)
        undo(batch, self.journal)
        self.poll(10)
        self.assertFalse(self.poll(16).candidates)
        src.write_bytes(b"new download")
        self.poll(20)
        self.assertEqual(self.poll(26).candidates, [snapshot_file(src)])

    def test_replaced_root_is_never_silently_adopted(self):
        self.box.configure(self.root)
        self.root.rename(self.base / "old")
        self.root.mkdir()
        (self.root / "new.txt").write_text("different directory")
        with self.assertRaisesRegex(ValueError, "替换"):
            self.box.poll()
        self.assertFalse(self.box.candidates())
        self.box.configure(self.root)
        self.assertEqual(self.poll(1).pending, 1)

    def test_cancelled_observation_cannot_advance_state(self):
        self.box.configure(self.root)
        (self.root / "ready.txt").write_text("candidate")
        self.poll(1)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(ScanCancelled):
            self.box.poll(cancelled)
        self.assertEqual(self.box.candidates(), [])
