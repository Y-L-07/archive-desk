from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from archive_desk.backup_format import list_versions, read_manifest, verify_repository
from archive_desk.backup_restore import prepare_restore, restore
from archive_desk.backup_tools import (
    BackupPresets, compare_versions, copy_repository, export_restore_plan,
    retired_versions, search_version, set_retired,
)
from archive_desk.backup_writer import prepare_backup, write_backup
from archive_desk.catalog import Catalog


class BackupToolsTests(TestCase):
    def test_presets_search_compare_and_reversible_version_removal(self):
        with TemporaryDirectory() as scratch:
            base = Path(scratch)
            source, repo, target, destination = [base / x for x in ("source","repo","copy","restore")]
            for path in (source,repo,target,destination):
                path.mkdir()
            catalog = Catalog(base/"app.sqlite3")
            rid = catalog.add_root("示例",source)
            store = BackupPresets(catalog.path)
            store.save("每周",rid,repo)
            self.assertEqual(BackupPresets(catalog.path).list(),
                             [{"name":"每周","root_id":rid,"target":str(repo)}])
            root = catalog.roots()[0]
            (source/"报告.TXT").write_text("first")
            old = write_backup(prepare_backup(root,repo))["id"]
            (source/"报告.TXT").write_text("second")
            (source/"新增.md").write_text("first")
            new = write_backup(prepare_backup(root,repo))["id"]
            before, after = read_manifest(repo,old), read_manifest(repo,new)
            self.assertEqual([(x["path"]) for x in search_version(after,"报告","txt","2000-01-01")],["报告.TXT"])
            self.assertEqual(list(search_version(after,"","txt","","2000-01-01")),[])
            self.assertEqual(set(compare_versions(before,after)),{("内容修改","报告.TXT"),("新增","新增.md")})
            set_retired(repo,old,retired=True)
            self.assertEqual([x["id"] for x in list_versions(repo)],[new])
            self.assertEqual(retired_versions(repo),[old])
            self.assertEqual(copy_repository(repo,target),(2,2))
            set_retired(target,old,retired=False)
            self.assertEqual(verify_repository(target)["issues"],[])
            self.assertEqual(retired_versions(repo),[old])
            (destination/"报告.TXT").write_text("keep")
            plan = prepare_restore(target,old,destination,conflict="skip")
            csv = base/"restore.csv"
            export_restore_plan(csv,plan)
            self.assertIn("报告.TXT",csv.read_text(encoding="utf-8-sig"))
            self.assertEqual(restore(plan)["skipped"],1)
            self.assertEqual((destination/"报告.TXT").read_text(),"keep")
            fresh = base/"fresh"
            fresh.mkdir()
            self.assertEqual(restore(prepare_restore(target,old,fresh))["restored"],1)
            self.assertEqual((fresh/"报告.TXT").read_text(),"first")
