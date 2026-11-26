"""独立临时备份仓库的只读预览与恢复验收。"""
import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from archive_desk import backup_restore
from archive_desk.backup_format import object_path
from archive_desk.backup_restore import prepare_restore, restore


class RestoreTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.repo = self.scratch / "repo"
        self.dest = self.scratch / "恢复"
        self.repo.mkdir()
        self.dest.mkdir()
        for name in ("versions", "objects", "tmp"):
            (self.repo / name).mkdir()
        marker = {"format": "ArchiveDesk", "version": 1, "repository_id": uuid4().hex}
        (self.repo / "repository.json").write_text(json.dumps(marker), encoding="utf-8")
        self.vid = uuid4().hex
        self.mtime = 1714444444123456700

    def manifest(self, contents, directories=(), issues=()):
        parents = set(directories)
        records = []
        for name, data in contents.items():
            parents.update(p.as_posix() for p in Path(name).parents if p.as_posix() != ".")
            digest = hashlib.sha256(data).hexdigest()
            obj = object_path(self.repo, digest)
            obj.parent.mkdir(exist_ok=True)
            obj.write_bytes(data)
            records.append(dict(path=name, size=len(data), mtime_ns=self.mtime, sha256=digest))
        meta = dict(format=1, id=self.vid, created_at="2026-11-26T12:00:00+08:00",
                    source_name="演示源", status="incomplete" if issues else "complete",
                    files=records, directories=sorted(parents), issues=list(issues))
        self.save_manifest(meta)
        return meta

    def save_manifest(self, data):
        document = json.dumps(data, ensure_ascii=False).encode("utf-8")
        (self.repo / "versions" / f"{self.vid}.json").write_bytes(document)
        (self.repo / "versions" / f"{self.vid}.sha256").write_text(
            hashlib.sha256(document).hexdigest(), encoding="ascii")

    def plan(self, **options):
        return prepare_restore(self.repo, self.vid, self.dest, **options)

    def test_read_only_plan_and_full_restore_preserve_hash_and_mtime(self):
        data = {"资料/初稿.txt": "样例".encode(), "a.bin": b"", "浅层.txt": b"content"}
        self.manifest(data, ["空目录", "资料/空目录"])
        p = self.plan()
        self.assertEqual(list(self.dest.iterdir()), [])
        r = restore(p)
        self.assertEqual((r["restored"], r["failed"], r["created_dirs"]), (3, 0, 3), r)
        for path, raw in data.items():
            self.assertEqual((self.dest / path).read_bytes(), raw)
            self.assertEqual((self.dest / path).stat().st_mtime_ns, self.mtime)
        self.assertTrue((self.dest / "资料/空目录").is_dir())
        self.assertTrue((self.dest / "空目录").is_dir())

    def test_directory_selection_and_individual_file(self):
        self.manifest({"cat/a": b"cat", "catalog/b": b"other", "X/b": b"x", "X/c": b"c"},
                      ["cat/empty", "catalog/empty"])
        p = self.plan(selected=["cat", "X/b"])
        self.assertEqual({e.path for e in p.entries}, {"cat", "cat/a", "cat/empty", "X", "X/b"})
        self.assertEqual(restore(p)["restored"], 2)
        self.assertFalse((self.dest / "catalog").exists())
        self.assertFalse((self.dest / "X/c").exists())
        with self.assertRaises(ValueError):
            self.plan(selected=["../outside"])

    def test_rename_respects_original_sibling_names_and_blocking_parent(self):
        self.manifest({"a.txt": b"a", "a (1).txt": b"b", "child/deep.bin": b"x"})
        (self.dest / "A.TXT").write_bytes(b"keep")
        (self.dest / "child").write_bytes(b"keep2")
        skipped = self.plan()
        self.assertEqual(next(e.action for e in skipped.entries if e.path == "child/deep.bin"), "skip")
        p = self.plan(conflict="rename")
        self.assertEqual(next(e.target.name for e in p.entries if e.path == "a.txt"), "a (2).txt")
        self.assertEqual(restore(p)["restored"], 3)
        self.assertEqual((self.dest / "child (1)/deep.bin").read_bytes(), b"x")
        self.assertEqual((self.dest / "A.TXT").read_bytes(), b"keep")
        self.assertEqual((self.dest / "child").read_bytes(), b"keep2")

    def test_missing_and_corrupt_objects_leave_other_files_and_temps_alone(self):
        m = self.manifest({"a": b"one", "b": b"two", "c": b"good"})
        orphan = self.dest / ".archive-desk-restore-unrelated.tmp"
        orphan.write_bytes(b"not ours")
        p = self.plan()
        object_path(self.repo, m["files"][0]["sha256"]).write_bytes(b"bad")
        object_path(self.repo, m["files"][1]["sha256"]).unlink()
        r = restore(p)
        self.assertEqual((r["restored"], r["failed"]), (1, 2), r)
        self.assertEqual({q.name for q in self.dest.iterdir()}, {"c", orphan.name})
        self.assertEqual(orphan.read_bytes(), b"not ours")

    def test_occupancy_at_commit_does_not_overwrite_or_renumber(self):
        self.manifest({"a": b"backup", "b": b"other"})
        p = self.plan(conflict="rename")
        rename = backup_restore.rename_without_replace
        def race(handle, target):
            if target.name == "a":
                target.write_bytes(b"other user")
            rename(handle, target)
        with patch.object(backup_restore, "rename_without_replace", race):
            r = restore(p)
        self.assertEqual((r["restored"], r["skipped"]), (1, 1), r)
        self.assertEqual((self.dest / "a").read_bytes(), b"other user")
        self.assertEqual({f.name for f in self.dest.iterdir()}, {"a", "b"})

    def test_directory_replacement_or_unplanned_directory_appearance_skips_affected_only(self):
        self.manifest({"old/a": b"a", "new/b": b"b", "unaffected.txt": b"c"})
        (self.dest / "old").mkdir()
        p = self.plan()
        (self.dest / "old").rename(self.dest / "formerly")
        (self.dest / "old").mkdir()
        (self.dest / "new").mkdir()
        r = restore(p)
        self.assertEqual(r["restored"], 1, r)
        self.assertFalse((self.dest / "new/b").exists() or (self.dest / "old/a").exists())

    def test_cancellation_during_temp_verification_removes_only_own_file(self):
        self.manifest({"a": b"abc" * 800000, "b": b"x"})
        p = self.plan()
        cancel = Event()
        original = backup_restore.content_digest
        def cancelled_digest(stream, cancel):
            cancel.set()
            return original(stream, cancel)
        with patch.object(backup_restore, "content_digest", cancelled_digest):
            r = restore(p, cancel=cancel)
        self.assertTrue(r["cancelled"], r)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_link_substitution_cannot_restore_through_link(self):
        import _winapi
        self.manifest({"safe/a.txt": b"x"})
        folder = self.dest / "safe"
        folder.mkdir()
        p = self.plan()
        folder.rename(self.dest / "prior")
        outside = self.scratch / "outside"
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(folder))
        self.assertEqual(restore(p)["restored"], 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_repo_overlap_tampered_manifest_or_escape_fails_before_writing(self):
        data = self.manifest({"a.txt": b"a"})
        with self.assertRaises(ValueError):
            prepare_restore(self.repo, self.vid, self.repo / "objects")
        p = self.plan()
        outside = self.scratch / "not-a-destination.txt"
        r = restore(replace(p, entries=(replace(p.entries[0], target=outside),)))
        self.assertEqual(r["restored"], 0)
        self.assertFalse(outside.exists())
        data["files"][0]["path"] = "b.txt"
        self.save_manifest(data)
        with self.assertRaises(ValueError):
            restore(p)
        data["files"][0]["path"] = "../bad"
        self.save_manifest(data)
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(list(self.dest.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
