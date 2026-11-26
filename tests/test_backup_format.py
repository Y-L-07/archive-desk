import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from archive_desk.backup_format import (assert_separate, check_relative, disk_note, list_versions,
                                       object_path, read_manifest, read_marker, verify_repository)


class BackupFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "仓库"
        self.repo.mkdir()
        (self.repo / "versions").mkdir()
        marker = {"format":"ArchiveDesk", "version":1, "repository_id":uuid4().hex}
        (self.repo / "repository.json").write_text(json.dumps(marker), encoding="utf-8")
        self.version = uuid4().hex
        self.digest = hashlib.sha256(b"one").hexdigest()
        self.obj = object_path(self.repo, self.digest)
        self.obj.parent.mkdir(parents=True)
        self.obj.write_bytes(b"one")
        self.doc = dict(format=1, id=self.version, created_at="2026-01-01T12:00:00+08:00",
                        source_name="中文样例", status="complete", directories=["中文", "空目录"],
                        files=[dict(path="中文/原件.txt", size=3, mtime_ns=0, sha256=self.digest)], issues=[])
        self.save(self.doc)

    def save(self, doc):
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        path = self.repo / "versions" / (doc["id"] + ".json")
        path.write_bytes(raw)
        path.with_suffix(".sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")
        return path

    def test_import_without_database_and_identify_damaged_manifests(self):
        self.assertEqual(read_marker(self.repo)["version"], 1)
        self.assertEqual(read_manifest(self.repo, self.version), self.doc)
        self.assertEqual(verify_repository(self.repo)["issues"], [])
        self.assertEqual(list_versions(self.repo)[0]["file_bytes"], 3)
        path = self.repo / "versions" / (self.version + ".json")
        path.write_bytes(path.read_bytes().replace(b'2026', b'2027'))
        self.assertEqual(verify_repository(self.repo)["issues"][0]["kind"], "manifest")
        self.assertEqual(list_versions(self.repo)[0]["status"], "damaged")
        self.assertFalse(any(self.repo.rglob("*.sqlite3")))

    def test_corrupt_or_missing_shared_object_lists_all_affected_files(self):
        second = copy.deepcopy(self.doc)
        second["id"] = uuid4().hex
        second["files"][0]["path"] = "另一份.txt"
        self.save(second)
        for kind in ("hash","missing"):
            if kind == "hash":
                self.obj.write_bytes(b"two")
            else:
                self.obj.unlink()
            report = verify_repository(self.repo)
            self.assertEqual(report["objects"], 1)
            self.assertEqual({(r["kind"],r["version"],r["path"]) for r in report["issues"]},
                             {(kind,self.version,"中文/原件.txt"),(kind,second["id"],"另一份.txt")})

    def test_unsafe_paths_and_ambiguous_component_names(self):
        for name in ("../外部", "/abs", "C:relative", "a\\b", "prn.txt", "x:stream", "a/.", "a ", "a//b",
                     "LPT¹.pdf", "name?", "\\\\machine\\share", "X./child"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                check_relative(name)
        for directories, names in ((["Docs","docs"], []), (["Doc"], ["doc/a"]), (["A"], ["A"]),
                                   ([], ["parent","parent/file"]), ([], ["x", "X"])):
            altered = dict(self.doc, directories=directories,
                           files=[dict(self.doc["files"][0],path=name) for name in names])
            self.save(altered)
            with self.subTest(names=names), self.assertRaises(ValueError):
                read_manifest(self.repo, self.version)

    def test_directory_guards_disk_note_and_link_substitution(self):
        import _winapi
        other = self.base / "来源"
        other.mkdir()
        assert_separate(self.repo, other)
        for a,b in ((self.repo,self.repo), (self.base,self.repo), (self.repo,self.base)):
            with self.assertRaises(ValueError):
                assert_separate(a,b)
        link = self.base / "联接点"
        _winapi.CreateJunction(str(self.repo), str(link))
        with self.assertRaises(ValueError):
            assert_separate(other,link)
        with patch("archive_desk.backup_paths._disk_numbers",side_effect=OSError):
            self.assertIn("无法确认",disk_note(self.repo,other))
        folder = self.obj.parent
        outside = other / "对象"
        folder.rename(outside)
        _winapi.CreateJunction(str(outside), str(folder))
        self.assertEqual(verify_repository(self.repo)["issues"][0]["kind"], "unreadable")
