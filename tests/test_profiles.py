import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from archive_desk.core import default_rules
from archive_desk.journal import Journal
from archive_desk.profiles import MAX_BYTES, ProfileStore, export_profile, import_profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.path = self.root / "rules.json"
        self.db = self.root / "settings.db"
        self.rules = default_rules()[::-1]
        self.rules[1] = replace(self.rules[1], target="历史归档/压缩资料", enabled=False)

    def test_export_import_roundtrip_never_moves_matching_files(self):
        file = self.root / "document.docx"
        file.write_bytes(b"untouched")
        export_profile(self.path, "办公归档", self.rules)
        self.assertEqual(import_profile(self.path), ("办公归档", self.rules))
        self.assertEqual(file.read_bytes(), b"untouched")
        self.assertEqual({p.name for p in self.root.iterdir()}, {"document.docx", "rules.json"})

    def test_named_profiles_persist_order_and_reject_duplicate_names(self):
        journal = Journal(self.db)
        store = ProfileStore(journal.path)
        store.save("办公室", self.rules)
        with self.assertRaises(ValueError):
            store.save("办公室", default_rules())
        reopened = ProfileStore(self.db)
        self.assertEqual(reopened.names(), ["办公室"])
        self.assertEqual(reopened.load("办公室"), self.rules)
        self.assertFalse(journal.batches())

    def test_export_refuses_existing_destination(self):
        original = b"keep existing contents"
        self.path.write_bytes(original)
        with self.assertRaises(FileExistsError):
            export_profile(self.path, "已有方案", self.rules)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_input_and_escape_paths_are_rejected(self):
        export_profile(self.path, "示例", self.rules)
        valid = self.path.read_text(encoding="utf-8")
        for key, value in [("target", "../external"), ("target", "NUL.txt"),
                           ("target", "c:/external"), ("enabled", "false"),
                           ("extensions", [".pdf", "*"]), ("extensions", []), ("name", "\n")]:
            with self.subTest(key=key, value=value):
                bad = json.loads(valid)
                bad["rules"][1][key] = value
                self.path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    import_profile(self.path)
        for key, value in [("name", " "), ("rules", []), ("version", True), ("version", 99)]:
            with self.subTest(key=key, value=value):
                bad = json.loads(valid)
                bad[key] = value
                self.path.write_text(json.dumps(bad), encoding="utf-8")
                with self.assertRaises(ValueError):
                    import_profile(self.path)

    def test_encoded_version_and_size_checks_precede_any_write(self):
        store = ProfileStore(self.db)
        for invalid in [b" " * (MAX_BYTES + 1), b"not JSON", b"\xff\xff", b"{}", b"[" * 2000]:
            self.path.write_bytes(invalid)
            with self.assertRaises(ValueError):
                import_profile(self.path)
        rules = [replace(self.rules[0], target="C:/bad")]
        with self.assertRaises(ValueError):
            store.save("invalid", rules)
        self.assertEqual(store.names(), [])
        new = self.root / "absent.json"
        with self.assertRaises(ValueError):
            export_profile(new, "invalid", rules)
        self.assertFalse(new.exists())
