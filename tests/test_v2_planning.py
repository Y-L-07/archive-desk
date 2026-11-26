import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from archive_desk import planning
from archive_desk.core import Rule, default_rules, directory_identity, validate_rule
from archive_desk.planning import preview, recheck_plans, rule_is_current
from archive_desk.profiles import ProfileStore, export_profile, import_profile
from archive_desk.scanner import scan


class V2Plans(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.root = self.scratch / "source"
        self.target = self.scratch / "archive"
        self.root.mkdir()
        self.target.mkdir()
        self.rule = Rule("发票", (".pdf",), "{year}/{month}/{ext}", name_glob="INV-*.pdf",
                         source_subdir="收件", min_size=4, max_size=4,
                         modified_from="2025-03-01", modified_to="2025-03-31")

    def write(self, relative, contents=b"data"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        when = datetime(2025, 3, 31, 23, 59, 59).timestamp()
        os.utime(path, (when, when))
        return path

    def preview(self, rules=None, resolutions=None):
        return preview(self.root, scan(self.root, True), rules or [self.rule],
                       target_root=self.target, resolutions=resolutions)

    @unittest.skipUnless(os.name == "nt", "Windows 目录联接")
    def test_replaced_cached_parent_blocks_child_stat_and_listing(self):
        import _winapi
        inspected = self.target / "待归档"
        inspected.mkdir()
        outside = self.scratch / "独立样例"
        (outside / "月份").mkdir(parents=True)
        inspection = planning._PathInspection(self.target)
        inspection.chain(self.target, inspected, False)
        inspected.rename(self.target / "先前目录")
        _winapi.CreateJunction(str(outside), str(inspected))
        child = inspected / "月份"
        with (patch.object(planning, "directory_identity", wraps=directory_identity) as identify,
              patch.object(Path, "iterdir", autospec=True) as listing):
            with self.assertRaises(ValueError):
                inspection.chain(self.target, child, False)
            with self.assertRaises(ValueError):
                inspection.child_names(inspected)
            self.assertNotIn(child, [call.args[0] for call in identify.call_args_list])
            listing.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows 目录共享模式")
    def test_ancestor_is_locked_during_listing_and_names_are_cached(self):
        parent = self.target / "归档"
        month = parent / "五月"
        month.mkdir(parents=True)
        (month / "memo.txt").write_bytes(b"sample")
        inspection = planning._PathInspection(self.target)
        inspection.chain(self.target, month, False)
        original = Path.iterdir

        def checked_listing(folder):
            self.assertEqual(folder, month)
            with self.assertRaises(PermissionError):
                parent.rename(self.target / "已移开")
            return original(folder)

        with patch.object(Path, "iterdir", autospec=True, side_effect=checked_listing) as listing:
            self.assertIn("memo.txt", inspection.child_names(month))
            self.assertIn("memo.txt", inspection.child_names(month))
            self.assertEqual(listing.call_count, 1)

    def test_combined_conditions_priority_trial_and_readonly_target(self):
        item = self.write("收件/外地/Inv-000.PDF")
        self.write("Inv-001.pdf")
        self.write("收件/Other.pdf")
        self.write("收件/Inv-002.pdf", b"fifth")
        older = self.write("收件/Inv-003.pdf")
        os.utime(older, (0, 0))
        rule2 = Rule("兜底", (), "其他", name_glob="*")
        plans = self.preview([self.rule, rule2])
        found = next(p for p in plans if p.file.path == item)
        self.assertEqual(found.rule, self.rule.name)
        self.assertEqual(found.target, self.target / "2025/03/pdf/Inv-000.PDF")
        for reason in ("扩展名", "名称匹配", "位于", "大小", "修改日期"):
            self.assertIn(reason, found.reason)
        self.assertTrue(all(p.rule == rule2.name for p in plans if p is not found))
        self.assertEqual(list(self.target.iterdir()), [])
        trial, = preview(self.root, [found.file], [self.rule, rule2], target_root=self.target)
        self.assertEqual(trial, found)
        self.assertFalse(rule_is_current(found, [rule2, self.rule]))

    def test_pattern_semantics_and_date_size_boundaries(self):
        self.write("收件/Inv-123.pdf")
        for invalid in (replace(self.rule, min_size=5), replace(self.rule, modified_from="2025-04-01"),
                        replace(self.rule, modified_from="2025-02-30"), replace(self.rule, source_subdir="../no"),
                        replace(self.rule, target="out/{unknown}"), replace(self.rule, target="{ext}/NUL")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_rule(invalid)
        for rule in (replace(self.rule, max_size=3, min_size=0), replace(self.rule, enabled=False),
                     replace(self.rule, modified_to="2025-03-30")):
            self.assertEqual(self.preview([rule])[0].status, "skip")

    def test_external_archive_and_invalid_expansion(self):
        self.write("payload.CON")
        names = Rule("所有文件", (), "{ext}", name_glob="*")
        p, = self.preview([names])
        self.assertEqual((p.status, p.conflict_type), ("conflict", "unsafe"))
        self.assertEqual(list(self.target.iterdir()), [])
        self.write("no-extension")
        plans = self.preview([names])
        p = next(p for p in plans if p.file.path.suffix == "")
        self.assertEqual(p.target, self.target / "无扩展名/no-extension")
        self.assertEqual((p.source_root, p.target_root), (self.root, self.target))

    def test_existing_and_batch_conflicts_with_number_and_manual_names(self):
        a = self.write("a/notes.pdf")
        b = self.write("b/Notes.pdf")
        c = self.write("c/single.pdf")
        folder = self.target / "文档"
        folder.mkdir()
        (folder / "NOTES (1).PDF").write_bytes(b"original")
        (folder / "SINGLE.PDF").write_bytes(b"original")
        base = self.preview(default_rules())
        self.assertEqual([p.conflict_type for p in base], ["batch", "batch", "existing"])
        options = {a: ("number", ""), b: ("number", ""), c: ("rename", "notes (2).pdf")}
        plans = self.preview(default_rules(), options)
        self.assertTrue(all(p.status == "ready" for p in plans))
        self.assertEqual({p.target.name.lower() for p in plans},
                         {"notes (2).pdf", "notes (3).pdf", "notes (4).pdf"})
        self.assertEqual((folder / "NOTES (1).PDF").read_bytes(), b"original")
        bad = self.preview(default_rules(), {a: ("rename", "../escape"), b: ("rename", "NUL.txt")})
        self.assertEqual([p.conflict_type for p in bad[:2]], ["unsafe", "unsafe"])

    def test_preflight_marks_only_affected_items_and_no_changes(self):
        a = self.write("a.pdf")
        self.write("b.txt")
        rules = [Rule("PDF", (".pdf",), "pdf"), Rule("TXT", (".txt",), "txt")]
        plans = self.preview(rules)
        self.assertEqual(recheck_plans(plans, rules), plans)
        self.assertEqual([p.status for p in recheck_plans(plans, [replace(rules[0], target="另存"), rules[1]])],
                         ["review", "ready"])
        a.write_bytes(b"new")
        self.assertEqual([p.status for p in recheck_plans(plans, rules)], ["review", "ready"])
        (self.target / "txt").mkdir()
        (self.target / "txt/b.txt").write_bytes(b"occupied")
        self.assertTrue(all(p.status == "review" for p in recheck_plans(plans, rules)))
        self.assertEqual((self.target / "txt/b.txt").read_bytes(), b"occupied")

    def test_quarantine_recheck_requires_current_keeper_and_fixed_subdirectory(self):
        a = self.write("保留/a.pdf")
        self.write("b.pdf")
        plans = self.preview([Rule("重复件隔离", (), "隔离区")])
        keeper = next(p.file for p in plans if p.file.path == a)
        candidate = next(p for p in plans if p.file.path != a)
        p = replace(candidate, kind="quarantine", keeper=keeper,
                    directories=(*candidate.directories, directory_identity(a.parent)),
                    expected_hash=hashlib.sha256(b"data").hexdigest())
        self.assertEqual(recheck_plans([p], default_rules()), [p])
        self.assertFalse(rule_is_current(replace(p, target=self.target / "other/b.pdf"), default_rules()))
        invalid = [replace(p, keeper=None), replace(p, file=None),
                   replace(p, keeper=replace(keeper, path=self.scratch / "outside.pdf"))]
        self.assertEqual([row.status for row in recheck_plans(invalid + [p], default_rules())],
                         ["review", "review", "review", "ready"])
        a.write_bytes(b"changed")
        self.assertEqual(recheck_plans([p], default_rules())[0].status, "review")

    def test_profiles_v1_v2_and_active_rules_are_distinct_from_import(self):
        store = ProfileStore(self.scratch / "profiles.db")
        self.assertEqual(store.load_active(), default_rules())
        store.save_active([self.rule])
        fresh = ProfileStore(self.scratch / "profiles.db")
        self.assertEqual(fresh.load_active(), [self.rule])
        path = self.scratch / "rules.json"
        export_profile(path, "方案", [self.rule])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(import_profile(path), ("方案", [self.rule]))
        legacy = Rule("办公", (".txt",), "文档", False)
        fields = {k: v for k, v in asdict(legacy).items() if k in ("name", "extensions", "target", "enabled")}
        path.write_text(json.dumps({"version": 1, "rules": [fields], "name": "旧方案"}), encoding="utf-8")
        self.assertEqual(import_profile(path), ("旧方案", [legacy]))
        self.assertEqual(store.load_active(), [self.rule])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_confirmed_profile_update_only_changes_named_profile(self):
        store = ProfileStore(self.scratch / "profiles.db")
        store.save("原方案", default_rules())
        store.save("另一方案", default_rules())
        store.save_active(default_rules())
        with self.assertRaises(ValueError):
            store.save("原方案", [self.rule])
        store.save("原方案", [self.rule], update_existing=True)
        self.assertEqual(store.load("原方案"), [self.rule])
        self.assertEqual(store.load("另一方案"), default_rules())
        self.assertEqual(store.load_active(), default_rules())
        with self.assertRaises(ValueError):
            store.save("不存在", [self.rule], update_existing=True)


if __name__ == "__main__":
    unittest.main()
