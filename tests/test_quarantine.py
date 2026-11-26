from pathlib import Path
import tempfile
import unittest

from archive_desk.duplicates import find_duplicates
from archive_desk.journal import Journal
from archive_desk.operations import apply, undo
from archive_desk.quarantine import quarantine_preview
from archive_desk.scanner import scan


class QuarantineTests(unittest.TestCase):
    def test_nested_keeper_collision_and_undo(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "来件"
            target = base / "归档"
            for child in ("甲", "乙", "丙"):
                (root / child).mkdir(parents=True)
            target.mkdir()
            keeper = root / "甲" / "保留.txt"
            copies = [root / each / "副本.txt" for each in ("乙", "丙")]
            for file in [keeper, *copies]:
                file.write_bytes(b"same data")
            different = root / "甲" / "副本.txt"
            different.write_bytes(b"else data")
            group, = find_duplicates(root, scan(root, True)).groups
            members = {f.path: f for f in group.files}
            selected = [(members[keeper], [members[p] for p in copies], group.digest)]
            self.assertNotIn(different, members)
            self.assertTrue(all(p.conflict_type == "batch"
                                for p in quarantine_preview(root, target, selected)))
            plans = quarantine_preview(root, target, selected,
                                       resolutions={p: ("number", "") for p in copies})
            self.assertFalse(any(target.iterdir()))
            journal = Journal(base / "state.db")
            batch = apply(root, plans, journal, rules=[], target_root=target)
            self.assertEqual(journal.batches()[0]["moved"], 2)
            self.assertTrue(keeper.is_file() and different.is_file())
            undo(batch, journal)
            self.assertEqual(journal.batches()[0]["undone"], 2)
            self.assertTrue(all(path.read_bytes() == b"same data" for path in copies))

    def test_changed_keeper_blocks_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a.txt", "b.txt"):
                (root / name).write_bytes(b"same")
            group, = find_duplicates(root, scan(root)).groups
            keep, copy = group.files
            plans = quarantine_preview(root, root, [(keep, [copy], group.digest)])
            keep.path.write_bytes(b"edit")
            journal = Journal(root / "state.db")
            apply(root, plans, journal)
            self.assertEqual(journal.batches()[0]["review"], 1)
            self.assertEqual(copy.path.read_bytes(), b"same")
            self.assertFalse((root / "隔离区").exists())
