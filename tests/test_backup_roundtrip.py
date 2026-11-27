import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from archive_desk.backup_demo import create_backup_demo
from archive_desk.backup_format import object_path, read_manifest, verify_repository
from archive_desk.backup_restore import prepare_restore, restore
from archive_desk.backup_writer import prepare_backup, write_backup
from archive_desk.catalog import Catalog


def contents(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class BackupRoundtrip(unittest.TestCase):
    def test_two_versions_and_fresh_machine_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            db = base / "machine-a" / "history.sqlite3"
            # Keep the demo in this test's private temporary directory.
            from unittest.mock import patch
            with patch.object(tempfile, "tempdir", str(base)):
                demo = create_backup_demo(db)
            source, repo = demo["source"], demo["repository"]
            root, = Catalog(db).roots()
            expected_old = contents(source)
            first = write_backup(prepare_backup(root,repo))
            self.assertEqual(first["status"],"complete")
            self.assertEqual(first["new_objects"],len(set(expected_old.values())))
            self.assertEqual(contents(source), expected_old)

            renamed = source / "新名字.bin"
            (source / "样例.bin").rename(renamed)
            (source / "交接/项目说明.txt").write_text("第二版",encoding="utf-8")
            (source / "参考/清单副本.txt").unlink()
            expected_new = contents(source)
            second = write_backup(prepare_backup(root,repo))
            self.assertEqual(second["new_objects"],1)
            self.assertEqual(contents(source),expected_new)
            self.assertNotEqual(first["id"],second["id"])
            old, new = [read_manifest(repo,v["id"]) for v in (first,second)]
            old_refs = {r["path"]:r["sha256"] for r in old["files"]}
            new_refs = {r["path"]:r["sha256"] for r in new["files"]}
            self.assertEqual((old_refs,new_refs),(expected_old,expected_new))
            self.assertEqual(old_refs["样例.bin"],new_refs["新名字.bin"])
            self.assertEqual(len(list((repo/"objects").glob("*/*"))),first["new_objects"]+1)

            machine_b = base / "machine-b" / "history.sqlite3"
            self.assertEqual(Catalog(machine_b).roots(),[])
            portable = base / "独立导入"
            shutil.copytree(repo,portable)
            source.rename(base / "源目录不在线")
            for version, expected in ((first,expected_old),(second,expected_new)):
                dest = base / uuid4().hex
                dest.mkdir()
                result = restore(prepare_restore(portable,version["id"],dest))
                self.assertEqual((result["failed"],result["skipped"],result["restored"]),
                                 (0,0,len(expected)))
                self.assertEqual(contents(dest),expected)
                self.assertTrue((dest / "空目录").is_dir())
            self.assertEqual(verify_repository(portable)["issues"],[])
            object_path(portable,new_refs["新名字.bin"]).write_bytes(b"damaged")
            damaged = verify_repository(portable)
            self.assertEqual({i["version"] for i in damaged["issues"]},{first["id"],second["id"]})
            dest = base / "部分找回"
            dest.mkdir()
            result = restore(prepare_restore(portable,first["id"],dest))
            self.assertEqual((result["failed"],result["restored"]),(1,len(expected_old)-1))
            self.assertEqual(contents(dest),{p:h for p,h in expected_old.items() if p!="样例.bin"})
