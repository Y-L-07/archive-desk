import tempfile
import unittest
from os import name as os_name
from pathlib import Path
from threading import Event
from unittest.mock import patch

from archive_desk import duplicates
from archive_desk.core import ScanCancelled
from archive_desk.scanner import scan


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def make_file(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_only_size_candidates_are_hashed_and_contents_must_match(self):
        documents = {"one.txt": b"abc", "two.bin": b"abc", "three.txt": b"xyz",
                     "unique.txt": b"another size", "empty-a": b"", "empty-b": b""}
        for name, data in documents.items():
            self.make_file(name, data)
        files = scan(self.root)
        with patch.object(duplicates, "_hash_file", wraps=duplicates._hash_file) as hash_file:
            result = duplicates.find_duplicates(self.root, files + files)
        self.assertEqual(hash_file.call_count, 5)
        self.assertNotIn("unique.txt", {call.args[1].path.name for call in hash_file.call_args_list})
        self.assertEqual(result.skipped, [])
        self.assertEqual([{f.path.name for f in g.files} for g in result.groups],
                         [{"one.txt", "two.bin"}, {"empty-a", "empty-b"}])
        self.assertEqual({f.path.name: f.path.read_bytes() for f in files}, documents)

    def test_changed_or_missing_candidate_is_reported(self):
        a = self.make_file("a", b"sample")
        b = self.make_file("b", b"sample")
        self.make_file("c", b"sample")
        files = scan(self.root)
        a.write_bytes(b"edited")
        b.unlink()
        result = duplicates.find_duplicates(self.root, files)
        self.assertEqual(len(result.skipped), 2)
        self.assertFalse(result.groups)

    def test_changed_after_hash_is_excluded(self):
        a = self.make_file("a", b"same")
        self.make_file("b", b"same")
        original = duplicates._hash_file

        def change_earlier_file(root, file, cancel):
            if file.path.name == "b":
                a.write_bytes(b"diff")
            return original(root, file, cancel)

        with patch.object(duplicates, "_hash_file", change_earlier_file):
            result = duplicates.find_duplicates(self.root, scan(self.root))
        self.assertEqual(len(result.skipped), 1)
        self.assertFalse(result.groups)

    @unittest.skipUnless(os_name == "nt", "NTFS junction")
    def test_replaced_directory_is_never_followed(self):
        import _winapi
        self.make_file("inner/a", b"same")
        self.make_file("b", b"same")
        files = scan(self.root, True)
        (self.root / "inner").rename(self.root / "other")
        _winapi.CreateJunction(str(self.root / "other"), str(self.root / "inner"))
        result = duplicates.find_duplicates(self.root, files)
        self.assertFalse(result.groups)
        self.assertEqual(len(result.skipped), 1)

    def test_cancellation_checked_between_chunks(self):
        from io import BytesIO
        from contextlib import contextmanager
        cancel = Event()
        self.make_file("a", b"abc")
        self.make_file("b", b"abc")

        class Reader(BytesIO):
            def read(self, n):
                chunk = super().read(n)
                cancel.set()
                return chunk

        @contextmanager
        def fake_read(root, file):
            yield Reader(b"bytes")

        with patch.object(duplicates, "_checked_file", fake_read):
            with self.assertRaises(ScanCancelled):
                duplicates.find_duplicates(self.root, scan(self.root), cancel)


if __name__ == "__main__":
    unittest.main()
