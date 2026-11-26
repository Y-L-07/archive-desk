import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_desk._winmove import open_regular
from archive_desk.file_revision import revision_matches
from archive_desk.scanner import scan, snapshot_file


class RevisionTests(unittest.TestCase):
    def test_fallback_digest_detects_edit_with_equal_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "报告.txt"
            path.write_bytes(b"old")
            with patch("archive_desk.scanner.read_usn", return_value=None):
                file, = scan(root)
                self.assertEqual(file, snapshot_file(path))
            path.write_bytes(b"new")
            os.utime(path, ns=(path.stat().st_atime_ns, file.mtime_ns))
            with open_regular(path) as (stream, handle, info):
                self.assertEqual((file.ino, file.size, file.mtime_ns),
                                 (info.st_ino, info.st_size, info.st_mtime_ns))
                stream.seek(1)
                self.assertFalse(revision_matches(file, stream, handle))
                self.assertEqual(stream.tell(), 1)
