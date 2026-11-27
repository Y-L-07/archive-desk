import hashlib
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from archive_desk import backup_writer as writer
from archive_desk.backup_format import list_versions, object_path, read_manifest, verify_repository
from archive_desk.core import ScanCancelled


class WriterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source, self.repo = self.base/"源",self.base/"库"
        self.source.mkdir()
        self.repo.mkdir()
        st = self.source.stat()
        self.root = dict(name="样例",path=str(self.source),dev=str(st.st_dev),ino=str(st.st_ino))

    def run_backup(self):
        return writer.write_backup(writer.prepare_backup(self.root,self.repo))

    def test_primary_data_of_readonly_hardlinks_and_named_streams(self):
        f = self.source/"只读.txt"
        f.write_bytes(b"readonly")
        os.chmod(f,stat.S_IREAD)
        self.addCleanup(os.chmod,f,stat.S_IWRITE)
        first = self.source/"副本.txt"
        first.write_bytes(b"linked")
        os.link(first,self.source/"同一文件.txt")
        tagged = self.source/"带流.bin"
        tagged.write_bytes(b"default")
        ads = Path(str(tagged)+":user-data")
        ads.write_bytes(b"independent metadata")
        result = self.run_backup()
        self.assertEqual((result["status"],result["files"],result["new_objects"]),("incomplete",4,3))
        self.assertEqual(result["issues"][0]["path"],tagged.name)
        self.assertIn("命名流",result["issues"][0]["reason"])
        manifest = read_manifest(self.repo,result["id"])
        raw = (self.repo/"versions"/(result["id"]+".json")).read_text(encoding="utf-8")
        self.assertIn("带流.bin",raw)
        self.assertIn('\n  "files": [',raw)
        for row in manifest["files"]:
            self.assertEqual(object_path(self.repo,row["sha256"]).read_bytes(),
                             (self.source/row["path"]).read_bytes())
        self.assertEqual(ads.read_bytes(),b"independent metadata")

    def test_same_size_same_mtime_new_bytes_are_not_reused(self):
        f = self.source/"变更.txt"
        f.write_bytes(b"AAA")
        mtime = f.stat().st_mtime_ns
        first = self.run_backup()
        f.write_bytes(b"BBB")
        os.utime(f,ns=(f.stat().st_atime_ns,mtime))
        second = self.run_backup()
        self.assertEqual(second["new_objects"],1)
        self.assertEqual(self.run_backup()["new_objects"],0)
        for result,content in ((first,b"AAA"),(second,b"BBB")):
            file, = read_manifest(self.repo,result["id"])["files"]
            self.assertEqual(file["sha256"],hashlib.sha256(content).hexdigest())

    def test_real_mid_read_edit_retries_then_succeeds(self):
        f = self.source/"growing.bin"
        f.write_bytes(b"A"*(writer.BLOCK*2))
        edited = False
        def mutate(stage,path):
            nonlocal edited
            if stage == "source_chunk" and not edited:
                with path.open("ab") as stream:
                    stream.write(b"late")
                edited = True
        with patch.object(writer,"checkpoint",mutate):
            result = self.run_backup()
        self.assertTrue(edited)
        self.assertEqual((result["status"],result["retries"]),("complete",1))
        file, = read_manifest(self.repo,result["id"])["files"]
        self.assertEqual(file["sha256"],hashlib.sha256(f.read_bytes()).hexdigest())

    def test_one_large_file_reports_byte_progress_before_completion(self):
        f = self.source/"单个大文件"
        f.write_bytes(b"batch" * 500000)
        plan = writer.prepare_backup(self.root,self.repo)
        updates = []
        with patch.object(writer,"monotonic",side_effect=itertools.count()):
            writer.write_backup(plan, progress=lambda *args: updates.append(args))
        self.assertTrue(any(done==0 and total==1 and "1,048,576 字节" in label
                            for done,total,label in updates))
        self.assertTrue(any("复制到临时对象" in label for _,_,label in updates))
        self.assertTrue(any("回读校验仓库对象" in label for _,_,label in updates))

    def test_perpetually_growing_source_is_bounded_and_other_files_succeed(self):
        f = self.source/"busy"
        f.write_bytes(b"x"*32)
        (self.source/"stable").write_bytes(b"recover this")
        def mutate(stage,path):
            if stage=="source_chunk" and path==f:
                with path.open("ab") as stream:
                    stream.write(b"!")
        with patch.object(writer,"checkpoint",mutate):
            result = self.run_backup()
        self.assertEqual((result["status"],result["retries"],result["files"]),("incomplete",2,1))
        self.assertEqual([i["path"] for i in result["issues"]],["busy"])
        self.assertEqual(f.stat().st_size,32+writer.ATTEMPTS)
        self.assertEqual(verify_repository(self.repo)["issues"],[])

    def test_disk_full_and_corrupt_object_never_publish(self):
        f = self.source/"record"
        f.write_bytes(b"previous")
        first = self.run_backup()
        f.write_bytes(b"new bytes")
        check_space = writer._space
        def fail(repo,size):
            if size == len(b"new bytes"):
                raise OSError(28,"模拟空间不足")
            check_space(repo,size)
        with patch.object(writer,"_space",fail), self.assertRaisesRegex(OSError,"不足"):
            self.run_backup()
        obj = object_path(self.repo,hashlib.sha256(b"new bytes").hexdigest())
        obj.parent.mkdir(exist_ok=True)
        obj.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError,"SHA-256"):
            self.run_backup()
        self.assertEqual(obj.read_bytes(),b"corrupted")
        self.assertEqual([i["id"] for i in list_versions(self.repo)],[first["id"]])

    def test_os_lock_is_exclusive_and_works_after_holder_exits(self):
        self.run_backup()
        plan = writer.prepare_backup(self.root,self.repo)
        locked, release = Event(),Event()
        def hold(stage,path):
            if stage == "locked":
                locked.set()
                self.assertTrue(release.wait(10))
        with patch.object(writer,"checkpoint",hold), ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(writer.write_backup,plan)
            try:
                self.assertTrue(locked.wait(5))
                with self.assertRaisesRegex(ValueError,"已有写入任务"):
                    writer.write_backup(plan)
            finally:
                release.set()
            self.assertEqual(task.result()["status"],"complete")
        self.assertEqual(self.run_backup()["status"],"complete")

    def test_swapped_or_offline_source_and_repo_fail_before_creating_files(self):
        plan = writer.prepare_backup(self.root,self.repo)
        self.source.rename(self.base/"source.offline")
        with self.assertRaises(OSError):
            writer.write_backup(plan)
        self.assertEqual(list(self.repo.iterdir()),[])
        (self.base/"source.offline").rename(self.source)
        self.repo.rename(self.base/"other")
        self.repo.mkdir()
        with self.assertRaisesRegex(ValueError,"身份|位置"):
            writer.write_backup(plan)
        self.assertEqual(list(self.repo.iterdir()),[])

    def test_hardlinked_lock_is_not_modified_and_unknown_parts_survive_cancel(self):
        self.run_backup()
        lock = self.repo/writer.LOCK_NAME
        lock.rename(self.base/"old-lock")
        user_file = self.base/"unrelated"
        user_file.write_bytes(b"user")
        os.link(user_file,lock)
        with self.assertRaises(ValueError):
            self.run_backup()
        self.assertEqual(user_file.read_bytes(),b"user")
        lock.unlink()
        (self.base/"old-lock").rename(lock)
        other = self.repo/"tmp"/"unknown.part"
        other.write_bytes(b"keep")
        cancel = Event()
        (self.source/"large").write_bytes(b"large"*400000)
        def stop(stage,path):
            if stage=="copy_chunk":
                cancel.set()
        with patch.object(writer,"checkpoint",stop), self.assertRaises(ScanCancelled):
            writer.write_backup(writer.prepare_backup(self.root,self.repo),cancel=cancel)
        self.assertEqual(len(list_versions(self.repo)),1)
        self.assertEqual(list((self.repo/"tmp").iterdir()),[other])
        self.assertEqual(other.read_bytes(),b"keep")

    def test_crash_while_initializing_can_be_restarted(self):
        child = """
import os,sys,json
from pathlib import Path
from archive_desk import backup_writer as w
w.checkpoint=lambda stage,p: os._exit(73) if stage==sys.argv[3] else None
w.write_backup(w.prepare_backup(json.loads(sys.argv[1]),Path(sys.argv[2])))
"""
        for stage in ("locked","before_marker","marker_committed"):
            with self.subTest(stage=stage):
                dest = self.base/stage
                dest.mkdir()
                process = subprocess.run([sys.executable,"-c",child,json.dumps(self.root),str(dest),stage],
                                         cwd=Path(__file__).resolve().parents[1],timeout=20)
                self.assertEqual(process.returncode,73)
                result = writer.write_backup(writer.prepare_backup(self.root,dest))
                self.assertEqual(result["status"],"complete")
                self.assertEqual(len(list_versions(dest)),1)

    def test_unrecognized_nonempty_repository_is_untouched(self):
        other = self.repo/"unknown.part"
        other.write_bytes(b"unrelated")
        with self.assertRaises(ValueError):
            writer.prepare_backup(self.root,self.repo)
        self.assertEqual([p.name for p in self.repo.iterdir()],["unknown.part"])
