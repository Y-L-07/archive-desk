import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event

from archive_desk.core import FileInfo, Rule, ScanCancelled, default_rules, validate_subdir
from archive_desk.planning import preview
from archive_desk.scanner import scan


class PreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def file(self, name, data=b"demo"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def snapshot(self):
        return sorted((str(p.relative_to(self.root)),
                       p.read_bytes() if p.is_file() else None,
                       p.stat().st_mtime_ns) for p in self.root.rglob("*"))

    def test_preview_never_creates_dirs_or_changes_files(self):
        self.file("x.PnG")
        before = self.snapshot()
        plan, = preview(self.root, scan(self.root), default_rules())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.rule, "图片")
        self.assertEqual(plan.target, self.root / "图片/x.PnG")
        self.assertEqual(plan.file.category, "图片")
        self.assertEqual(plan.directories[0].path, self.root)

    def test_first_enabled_match_wins(self):
        self.file("part.TAR.GZ")
        files = scan(self.root)
        first = Rule("源代码", (".tar.gz",), r"项目\压缩资料")
        rules = [first, *default_rules()]
        plan, = preview(self.root, files, rules)
        self.assertEqual(plan.target, self.root / "项目/压缩资料/part.TAR.GZ")
        rules[0] = replace(first, enabled=False)
        plan, = preview(self.root, files, rules)
        self.assertEqual(plan.target, self.root / "压缩包/part.TAR.GZ")

    def test_conflict_stays_put_and_already_sorted_files_are_skipped(self):
        self.file("a.pdf", b"new")
        self.file("文档/a.pdf", b"old")
        plans = {p.file.path.relative_to(self.root): p for p in
                 preview(self.root, scan(self.root, True), default_rules())}
        self.assertEqual(plans[Path("a.pdf")].status, "conflict")
        self.assertEqual(plans[Path("文档/a.pdf")].status, "skip")
        self.assertEqual((self.root / "文档/a.pdf").read_bytes(), b"old")

    def test_multiple_sources_with_same_destination_are_all_conflicts(self):
        self.file("2023/resume.docx")
        self.file("2024/Resume.docx")
        plans = preview(self.root, scan(self.root, True), default_rules())
        self.assertEqual([p.status for p in plans], ["conflict", "conflict"])

    def test_invalid_targets_including_device_names_and_ntfs_streams(self):
        self.file("a.pdf")
        files = scan(self.root)
        for target in ("", ".", "..", "../outside", "inside/../outside",
                       "/outside", r"C:\out", "C:relative", r"\\server\share",
                       r"\\?\C:\escape", "doc:stream", "aux.log", "COM¹",
                       "nul .abc", "trailing.", "trailing ", "doc//empty", "a?b"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    validate_subdir(target)
                p, = preview(self.root, files, [Rule("测试", (".pdf",), target)])
                self.assertEqual(p.status, "conflict")

    def test_reject_external_source(self):
        file = FileInfo(self.root.parent / "outside.pdf", 0, 0, 0, 0)
        p, = preview(self.root, [file], default_rules())
        self.assertEqual(p.status, "conflict")

    def test_plain_file_blocks_target_subdirectory(self):
        self.file("文档", b"occupied")
        self.file("a.docx")
        p = next(p for p in preview(self.root, scan(self.root), default_rules())
                 if p.file.path.name == "a.docx")
        self.assertEqual(p.status, "conflict")
        self.assertEqual((self.root / "文档").read_bytes(), b"occupied")

    def test_scan_depth_and_cancellation(self):
        self.file("top.txt")
        self.file("nested/note.txt")
        self.assertEqual(len(scan(self.root)), 1)
        self.assertEqual(len(scan(self.root, recursive=True)), 2)
        cancel = Event()
        cancel.set()
        with self.assertRaises(ScanCancelled):
            scan(self.root, True, cancel)
        with self.assertRaises(ScanCancelled):
            preview(self.root, scan(self.root), default_rules(), cancel)

    def test_junction_never_traversed(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            other = Path(elsewhere)
            (other / "private.txt").write_text("outside", encoding="utf-8")
            junction = self.root / "文档"
            if os.name == "nt":
                import _winapi
                _winapi.CreateJunction(str(other), str(junction))
            else:
                junction.symlink_to(other, target_is_directory=True)
            self.file("inside.txt")
            self.assertEqual([p.path.name for p in scan(self.root, True)], ["inside.txt"])
            with self.assertRaises(ValueError):
                scan(junction)
            p, = preview(self.root, scan(self.root), default_rules())
            self.assertEqual(p.status, "conflict")
            self.assertFalse((other / "inside.txt").exists())

    @unittest.skipUnless(os.name == "nt", "Windows NTFS change time")
    def test_change_time_survives_restoring_modification_time(self):
        path = self.file("x.txt", b"000")
        original, = scan(self.root)
        path.write_bytes(b"111")
        os.utime(path, ns=(path.stat().st_atime_ns, original.mtime_ns))
        edited, = scan(self.root)
        self.assertEqual((original.ino, original.size, original.mtime_ns),
                         (edited.ino, edited.size, edited.mtime_ns))
        # NTFS timestamps may coincide within one clock tick; the archive
        # snapshot includes a journal revision or a digest on such volumes.
        self.assertNotEqual((original.change_ns, original.usn, original.digest),
                            (edited.change_ns, edited.usn, edited.digest))


if __name__ == "__main__":
    unittest.main()
