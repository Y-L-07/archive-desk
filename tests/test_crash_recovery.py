"""关闭解释器模拟文件已改名、SQLite 尚未确认的中断窗口。"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from archive_desk.core import default_rules
from archive_desk.planning import preview
from archive_desk.scanner import scan
from archive_desk.journal import Journal
from archive_desk.operations import apply, resume, undo


class InterruptedJournal(Journal):
    def transition(self, item_id, previous, status, *args, **kwargs):
        if status == self.interrupt_before:
            os._exit(73)
        return super().transition(item_id, previous, status, *args, **kwargs)


def interrupt(scratch, status):
    root = scratch / "sample"
    journal = InterruptedJournal(scratch / "journal.db")
    journal.interrupt_before = status
    if status == "moved":
        apply(root, preview(root, scan(root), default_rules()), journal)
    else:
        undo(journal.batches()[0]["id"], journal)


@unittest.skipUnless(os.name == "nt", "Windows 文件操作测试")
class CrashRecoveryTest(unittest.TestCase):
    def test_restart_on_both_sides_of_undo(self):
        for state in ("moved", "undone"):
            with self.subTest(interrupted=state), tempfile.TemporaryDirectory() as directory:
                scratch = Path(directory)
                root = scratch / "sample"
                root.mkdir()
                original = root / "memo.txt"
                target = root / "文档" / "memo.txt"
                original.write_bytes(b"only copy")
                journal = Journal(scratch / "journal.db")
                if state == "undone":
                    apply(root, preview(root, scan(root), default_rules()), journal)
                child = subprocess.run([sys.executable, __file__, str(scratch), state],
                                       cwd=PROJECT, timeout=20)
                self.assertEqual(child.returncode, 73)
                journal = Journal(scratch / "journal.db")
                self.assertEqual(journal.recover(), 1)
                batch_id = journal.batches()[0]["id"]
                self.assertEqual(journal.items(batch_id)[0]["status"], "resumable")
                self.assertEqual(journal.recover(), 0)
                undo(batch_id, journal)
                occupied, empty = (target, original) if state == "moved" else (original, target)
                self.assertEqual(occupied.read_bytes(), b"only copy")
                self.assertFalse(empty.exists())
                resume(batch_id, journal)
                self.assertEqual(journal.items(batch_id)[0]["status"], state)
                self.assertEqual(occupied.read_bytes(), b"only copy")
                self.assertFalse(empty.exists())


if __name__ == "__main__":
    if len(sys.argv) == 3:
        interrupt(Path(sys.argv[1]), sys.argv[2])
    else:
        unittest.main()
