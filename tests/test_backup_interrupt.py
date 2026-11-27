"""Terminate a writer process while copying and immediately before version publication."""
import hashlib
import subprocess
import sys
import tempfile
import unittest
from ctypes import WinError
from pathlib import Path
from unittest.mock import patch

from archive_desk.backup_format import list_versions, verify_repository
from archive_desk.backup_restore import prepare_restore, restore
from archive_desk.backup_writer import prepare_backup, write_backup
from archive_desk.catalog import Catalog


_CRASH = """
import os
import sys
from pathlib import Path
from archive_desk import backup_writer as writer
from archive_desk.catalog import Catalog
def cut(stage, path):
    if stage == sys.argv[3]:
        os._exit(73)
writer.checkpoint = cut
root, = Catalog(Path(sys.argv[1])).roots()
writer.write_backup(writer.prepare_backup(root, Path(sys.argv[2])))
"""


class ProcessExitTests(unittest.TestCase):
    def exercise(self, stage):
        with tempfile.TemporaryDirectory(prefix="ArchiveDesk-V4-cut-") as temp:
            base = Path(temp)
            db, source, repo, dest = base/"app"/"db", base/"源", base/"仓库", base/"找回"
            for folder in (source,repo,dest):
                folder.mkdir()
            cat = Catalog(db)
            cat.add_root("故障注入",source)
            root, = cat.roots()
            file = source/"记录.bin"
            old = b"original"
            file.write_bytes(old)
            first = write_backup(prepare_backup(root,repo))
            unknown = repo/"tmp"/"其他程序.part"
            unknown.write_bytes(b"user")
            fresh = bytes(range(256))*14001
            file.write_bytes(fresh)
            before = (file.stat().st_mtime_ns,hashlib.sha256(fresh).hexdigest())
            child = subprocess.run([sys.executable,"-c",_CRASH,str(db),str(repo),stage],
                                   cwd=Path(__file__).resolve().parents[1],timeout=60)
            self.assertEqual(child.returncode,73)
            self.assertEqual([v["id"] for v in list_versions(repo)],[first["id"]])
            self.assertEqual(verify_repository(repo)["issues"],[])
            self.assertEqual(restore(prepare_restore(repo,first["id"],dest))["restored"],1)
            self.assertEqual((dest/file.name).read_bytes(),old)
            retried = write_backup(prepare_backup(root,repo))
            self.assertEqual(retried["status"],"complete")
            self.assertEqual(retried["new_objects"], 0 if stage=="before_manifest" else 1)
            self.assertEqual(len(list_versions(repo)),2)
            self.assertEqual((file.stat().st_mtime_ns,hashlib.sha256(file.read_bytes()).hexdigest()),before)
            self.assertEqual(unknown.read_bytes(),b"user")

    def test_exit_during_object_copy(self):
        self.exercise("copy_chunk")

    def test_exit_before_publishing_manifest(self):
        self.exercise("before_manifest")

    def test_device_disconnected_after_first_block_preserves_prior_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            src, repo, dest = (base / name for name in ("src","repo","restore"))
            for folder in (src,repo,dest):
                folder.mkdir()
            catalog = Catalog(base/"state"/"db")
            catalog.add_root("拔盘测试",src)
            root, = catalog.roots()
            path = src/"资料.bin"
            path.write_bytes(b"first")
            first = write_backup(prepare_backup(root,repo))
            body = bytes(range(256))*8208
            path.write_bytes(body)
            stamp = path.stat().st_mtime_ns
            def disconnect(stage, _):
                if stage == "copy_chunk":
                    raise WinError(1167)  # ERROR_DEVICE_NOT_CONNECTED
            with patch("archive_desk.backup_writer.checkpoint",disconnect), self.assertRaises(OSError):
                write_backup(prepare_backup(root,repo))
            self.assertEqual([v["id"] for v in list_versions(repo)],[first["id"]])
            self.assertEqual(restore(prepare_restore(repo,first["id"],dest))["restored"],1)
            self.assertEqual((dest/path.name).read_bytes(),b"first")
            self.assertEqual((path.stat().st_mtime_ns, path.read_bytes()),(stamp,body))
