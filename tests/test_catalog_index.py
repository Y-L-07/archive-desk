import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from threading import Event
from unittest.mock import patch

from archive_desk.catalog import Catalog, touch_roots
from archive_desk.core import ScanCancelled, default_rules
from archive_desk.journal import Journal, database_connection
from archive_desk.operations import undo
from archive_desk.planning import preview
from archive_desk.scanner import scan


class CatalogIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.root = self.temp / "资料库"
        self.root.mkdir()
        self.cat = Catalog(self.temp / "state" / "history.db")
        self.rid = self.cat.add_root("测试库", self.root)

    def make(self, name, content):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        return file

    def files(self):
        with database_connection(self.cat.path) as db:
            return {r["relpath"]:dict(r) for r in db.execute(
                "SELECT * FROM catalog_files WHERE root_id=? AND active=1", (self.rid,))}

    def test_strong_hash_cache_and_metadata_only_weak_volume(self):
        f = self.make("a.txt", b"first")
        other = self.make("子目录/b.TXT", b"other")
        with patch("archive_desk.catalog.content_digest", side_effect=AssertionError("元数据刷新读了文件内容")):
            self.cat.refresh(self.rid)
        self.assertTrue(all(r["digest"] is None for r in self.files().values()))
        initial = self.cat.refresh(self.rid, hash_files=True)
        self.assertEqual(initial["counts"]["hashed"], 2)
        self.assertEqual(set(self.files()), {"a.txt", "子目录/b.TXT"})
        self.assertEqual(self.files()["子目录/b.TXT"]["ext"], ".txt")
        f.write_bytes(b"changed")
        latest = self.cat.refresh(self.rid, hash_files=True)
        expected_reuse = int(self.files()["子目录/b.TXT"]["usn"] is not None)
        self.assertEqual(latest["counts"]["reused"], expected_reuse)
        self.assertEqual(self.files()["a.txt"]["digest"], hashlib.sha256(b"changed").hexdigest())
        self.assertEqual(other.read_bytes(), b"other")
        with patch("archive_desk.catalog.read_usn", return_value=None):
            self.cat.refresh(self.rid)
        self.assertTrue(all(r["digest"] is None for r in self.files().values()))

    def test_partial_scan_preserves_blind_subtree_and_logs_reason(self):
        self.make("限制/合同.txt", b"contract")
        gone = self.make("已移走.txt", b"old")
        old = self.cat.refresh(self.rid)
        gone.rename(self.temp / "elsewhere.txt")
        self.make("新闻.txt", b"new")
        entries = self.cat._entries
        def denied(folder):
            if folder == self.root / "限制":
                raise PermissionError("模拟目录访问受限")
            return entries(folder)
        with patch.object(self.cat, "_entries", denied):
            partial = self.cat.refresh(self.rid)
        self.assertEqual(partial["published_state"], "partial")
        self.assertEqual(partial["last_complete_at"], old["last_complete_at"])
        self.assertEqual(self.files()["限制/合同.txt"]["stale"], 1)
        self.assertEqual(self.files()["新闻.txt"]["stale"], 0)
        self.assertNotIn("已移走.txt", self.files())
        issue, = self.cat.issues(self.rid)
        self.assertEqual(issue["relpath"], "限制")
        self.assertIn("受限", issue["reason"])
        self.cat.refresh(self.rid)
        self.assertEqual(self.files()["限制/合同.txt"]["stale"], 0)
        self.assertEqual(self.cat.issues(self.rid), [])

    def test_cancel_after_staging_and_root_enumeration_failure_keep_published_view(self):
        self.make("已索引.txt", b"old")
        old = self.cat.refresh(self.rid)
        before = self.files()
        self.make("新增.txt", b"new")
        cancel = Event()
        def interrupt(count, path):
            self.assertEqual(self.files(), before)
            cancel.set()
        with patch("archive_desk.catalog._BATCH", 1), self.assertRaises(ScanCancelled):
            self.cat.refresh(self.rid, cancel=cancel, progress=interrupt)
        self.assertEqual(self.cat.roots()[0]["scan_state"], "cancelled")
        with patch.object(self.cat, "_entries", side_effect=PermissionError("根目录拒绝枚举")):
            with self.assertRaises(PermissionError):
                self.cat.refresh(self.rid)
        self.assertEqual(self.cat.roots()[0]["scan_state"], "failed")
        self.assertEqual(self.cat.roots()[0]["indexed_at"], old["indexed_at"])
        self.assertEqual(self.files(), before)
        self.assertTrue(self.cat.probe(self.rid))
        self.assertIn("拒绝", self.cat.roots()[0]["problem"])

    def test_staged_refresh_cannot_overwrite_committed_transfer_generation(self):
        self.make("old.png", b"old")
        self.cat.refresh(self.rid)
        before = self.files()
        self.make("new.png", b"new")
        def touch(count, rel):
            if not rel:
                with database_connection(self.cat.path) as db:
                    touch_roots(db, [self.rid])
        with self.assertRaisesRegex(ValueError, "变化"):
            self.cat.refresh(self.rid, progress=touch)
        self.assertEqual(self.files(), before)

    def test_same_path_new_folder_offline_rebind_requires_same_identity_and_confirmation(self):
        self.make("only.txt", b"private")
        self.cat.refresh(self.rid)
        new_mount = self.temp / "模拟新盘位"
        self.root.rename(new_mount)
        old = self.files()
        self.root.mkdir()
        self.make("intruder.txt", b"wrong drive")
        self.assertFalse(self.cat.probe(self.rid))
        with self.assertRaises(ValueError):
            self.cat.refresh(self.rid)
        self.assertEqual(self.files(), old)
        self.assertEqual(self.cat.roots()[0]["online"], 0)
        with self.assertRaises(ValueError):
            self.cat.rebind(self.rid, self.root, confirmed=True)
        with self.assertRaises(ValueError):
            self.cat.rebind(self.rid, new_mount)
        self.cat.rebind(self.rid, new_mount, confirmed=True)
        self.assertTrue(self.cat.probe(self.rid))
        self.assertEqual(self.cat.refresh(self.rid)["file_count"], 1)
        with self.assertRaises(ValueError):
            self.cat.add_root("重复登记", new_mount)

    def test_rename_and_rebuild_keep_markings_but_replacement_does_not_inherit(self):
        path = self.make("a.txt", b"original")
        self.cat.refresh(self.rid)
        fid = self.files()["a.txt"]["id"]
        with database_connection(self.cat.path) as db:
            db.execute("INSERT INTO catalog_notes VALUES(?,'备注',1)", (fid,))
            db.execute("INSERT INTO catalog_labels VALUES(?,'重要')", (fid,))
        moved = self.root / "b.txt"
        path.rename(moved)
        self.cat.refresh(self.rid)
        self.assertEqual(self.files()["b.txt"]["id"], fid)
        moved.rename(self.temp / "saved-original")
        self.make("b.txt", b"new file")
        self.cat.refresh(self.rid)
        self.assertNotEqual(self.files()["b.txt"]["id"], fid)
        with database_connection(self.cat.path) as db:
            self.assertEqual(db.execute("SELECT note FROM catalog_notes WHERE entry_id=?", (fid,)).fetchone()[0],
                             "备注")
            self.assertEqual(db.execute("SELECT active FROM catalog_files WHERE id=?", (fid,)).fetchone()[0], 0)

    def test_junction_excluded_and_volume_ids_stored_without_integer_overflow(self):
        import _winapi
        outside = self.temp / "others"
        outside.mkdir()
        (outside / "private.txt").write_text("not authorized")
        _winapi.CreateJunction(str(outside), str(self.root / "链接"))
        self.make("合法.txt", b"ok")
        self.cat.refresh(self.rid)
        self.assertEqual(set(self.files()), {"合法.txt"})
        self.assertEqual(self.cat.roots()[0]["dev"], str(self.root.stat().st_dev))
        self.assertEqual(self.cat.issues(self.rid)[0]["relpath"], "链接")


class CatalogMigrationTests(unittest.TestCase):
    def test_backup_of_v2_schema_preserves_journal_and_can_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "library"
            root.mkdir()
            source = root / "旧记录.txt"
            source.write_bytes(b"V2 archive")
            plan, = preview(root, scan(root), default_rules())
            plan.target.parent.mkdir()
            source.rename(plan.target)
            st = plan.target.stat()
            ev = {"file":[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns],
                  "sha256":hashlib.sha256(b"V2 archive").hexdigest(),
                  "directories":[[str(p),p.stat().st_dev,p.stat().st_ino] for p in (root,plan.target.parent)]}
            original, backup = base / "v2.db", base / "backup.db"
            with sqlite3.connect(original) as old:
                old.executescript("""PRAGMA user_version=2;
                    CREATE TABLE batches(id INTEGER PRIMARY KEY,root TEXT,created_at TEXT,target_root TEXT);
                    CREATE TABLE items(id INTEGER PRIMARY KEY,batch_id INTEGER,source TEXT,target TEXT,
                    rule TEXT,status TEXT,reason TEXT,evidence TEXT,undo_conflict INTEGER DEFAULT 0,
                    intent TEXT,direction TEXT,phase TEXT,transfer TEXT);
                    CREATE TABLE profiles(name TEXT PRIMARY KEY,spec TEXT NOT NULL);
                    INSERT INTO profiles VALUES('旧方案','原始配置');
                """)
                old.execute("INSERT INTO batches VALUES(23,?,'2026-01-01',?)", (str(root), str(root)))
                old.execute("INSERT INTO items VALUES(42,23,?,?,'文档','moved','已移动',?,0,?,'apply','done',?)",
                            (str(source),str(plan.target),json.dumps(ev),json.dumps(asdict(plan),default=str),
                             json.dumps({**ev,"mode":"rename","source":str(source),"target":str(plan.target)})))
                old.commit()
                with sqlite3.connect(backup) as clone:
                    old.backup(clone)
                clone.close()
            old.close()
            journal = Journal(backup)
            with database_connection(backup) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT spec FROM profiles").fetchone()[0], "原始配置")
                self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])
            undo(23,journal)
            self.assertEqual(journal.item(42)["status"], "undone")
            self.assertEqual(source.read_bytes(), b"V2 archive")
            self.assertFalse(plan.target.exists())
            with database_connection(original) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT status FROM items").fetchone()[0], "moved")
