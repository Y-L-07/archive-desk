"""从指定 V3 提交合成旧数据，在其 SQLite 副本上验证 V4；不读取用户配置。"""
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BASELINE = "3f17aec"
NOTE = "旧版保存的备注"
TAG = "已审"
RULE_NAME = "旧版日常整理"
FILE_NAME = "迁移样本.txt"
CONTENTS = "归档台 V3 现场交接\n".encode("utf-8")


def seed_v3(old_tree, scratch):
    # The isolated subprocess must import the actual V3 modules from git archive.
    sys.path.insert(0, str(old_tree))
    import archive_desk
    assert Path(archive_desk.__file__).resolve().is_relative_to(old_tree)
    from archive_desk.catalog import Catalog
    from archive_desk.catalog_search import CatalogSearch
    from archive_desk.catalog_snapshots import SnapshotStore
    from archive_desk.core import default_rules
    from archive_desk.journal import Journal
    from archive_desk.operations import apply
    from archive_desk.planning import preview
    from archive_desk.profiles import ProfileStore
    from archive_desk.scanner import scan

    root = scratch / "模拟原库"
    root.mkdir()
    file = root / FILE_NAME
    file.write_bytes(CONTENTS)
    db = scratch / "v3-config/history.sqlite3"
    journal = Journal(db)
    catalog = Catalog(db)
    root_id = catalog.add_root("旧版资料", root)
    catalog.refresh(root_id, hash_files=True)
    search = CatalogSearch(db)
    row, = search.search()["rows"]
    entry_id = row["id"]
    search.add_tags([entry_id], [TAG])
    search.annotate(entry_id, note=NOTE, favorite=True)
    search.save_search("收藏资料", {"favorite": True})
    snapshots = SnapshotStore(db)
    original = snapshots.create(root_id, "归档前")
    profiles = ProfileStore(db)
    rules = [replace(rule, target="旧版归档/文稿") if ".txt" in rule.extensions else rule
             for rule in default_rules()]
    profiles.save(RULE_NAME, rules)
    profiles.save_active(rules)
    plan, = preview(root, scan(root), rules)
    assert plan.status == "ready"
    batch = apply(root, [plan], journal, rules=rules)
    item, = journal.items(batch)
    assert item["status"] == "moved" and not file.exists()
    assert plan.target.read_bytes() == CONTENTS
    catalog.refresh(root_id, hash_files=True)
    archived = snapshots.create(root_id, "归档后")
    diffs = snapshots.compare(original, archived)
    assert [r["status"] for r in diffs] == ["moved"]
    expected = dict(root_id=root_id, entry_id=entry_id, batch=batch, item=item["id"],
                    original=original, archived=archived, target=str(plan.target),
                    rules=[asdict(r) for r in rules], diffs=diffs,
                    snapshots=snapshots.list_snapshots(root_id))
    (scratch / "assertions.json").write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")


def prepare_v3(scratch):
    archive = scratch / "baseline.zip"
    tree = scratch / "v3-code"
    subprocess.run(["git", "-C", str(PROJECT), "archive", "--format=zip", "-o", str(archive),
                    BASELINE, "archive_desk"], check=True, timeout=20)
    with zipfile.ZipFile(archive) as package:
        package.extractall(tree)
    subprocess.run([sys.executable, "-I", "-B", __file__, "--seed", str(tree), str(scratch)],
                   cwd=scratch, check=True, timeout=45)


def validate_copy(scratch):
    from archive_desk.catalog import Catalog
    from archive_desk.catalog_search import CatalogSearch
    from archive_desk.catalog_snapshots import SnapshotStore
    from archive_desk.journal import Journal
    from archive_desk.operations import undo
    from archive_desk.profiles import ProfileStore

    expected = json.loads((scratch / "assertions.json").read_text(encoding="utf-8"))
    root = scratch / "模拟原库"
    db = scratch / "upgrade/history.sqlite3"
    db.parent.mkdir()
    donor = scratch / "v3-config/history.sqlite3"
    donor_checksum = hashlib.sha256(donor.read_bytes()).digest()
    with closing(sqlite3.connect(donor.as_uri()+"?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(db)) as out:
            source.backup(out)

    journal = Journal(db)
    cat = Catalog(db)
    registered, = cat.roots()
    assert registered["id"] == expected["root_id"] and Path(registered["path"]) == root
    search = CatalogSearch(db)
    snaps = SnapshotStore(db)
    assert snaps.list_snapshots(registered["id"]) == expected["snapshots"]
    assert snaps.compare(expected["original"], expected["archived"]) == expected["diffs"]
    profile = ProfileStore(db)
    saved = profile.load(RULE_NAME)
    assert json.loads(json.dumps([asdict(r) for r in saved])) == expected["rules"]
    assert profile.load_active() == saved
    assert search.saved_searches()["收藏资料"] == {"favorite": True}

    def check_annotation(relpath):
        entry, = search.search({"favorite": True, "tag": TAG})["rows"]
        assert entry["id"] == expected["entry_id"] and entry["relpath"] == relpath
        assert entry["note"] == NOTE and entry["digest"] == hashlib.sha256(CONTENTS).hexdigest()
        assert entry["tags"] == [TAG]
        item, = search.history_for_entry(entry["id"])
        assert item["id"] == expected["item"]

    target = Path(expected["target"])
    check_annotation(target.relative_to(root).as_posix())
    assert journal.item(expected["item"])["status"] == "moved" and target.read_bytes() == CONTENTS
    undo(expected["batch"], journal)
    assert journal.item(expected["item"])["status"] == "undone"
    assert (root / FILE_NAME).read_bytes() == CONTENTS and not target.exists()
    check_annotation(FILE_NAME)
    assert snaps.compare(expected["original"], expected["archived"]) == expected["diffs"]
    assert hashlib.sha256(donor.read_bytes()).digest() == donor_checksum
    with closing(sqlite3.connect(donor.as_uri()+"?mode=ro", uri=True)) as old:
        assert old.execute("SELECT status FROM items WHERE id=?", (expected["item"],)).fetchone() == ("moved",)
    print(f"V3 {BASELINE} 实际旧记录的 SQLite 副本升级通过。标签、收藏、备注、方案、快照、历史链接均保留，原批次在 V4 已撤销。")
    return cat.roots()[0]


def fresh_config(scratch, root):
    from archive_desk.backup_format import list_versions, read_marker, verify_repository
    from archive_desk.backup_restore import prepare_restore, restore
    from archive_desk.backup_writer import prepare_backup, write_backup
    from archive_desk.catalog import Catalog
    from archive_desk.journal import Journal

    repo = scratch / "从升级后库创建的仓库"
    repo.mkdir()
    report = write_backup(prepare_backup(root, repo))
    assert report["status"] == "complete", report
    # Copy only repository files to a separate location. The verification runs
    # without passing a catalog root or migrated database to the restore APIs.
    machine = scratch / "空白配置"
    portable = machine / "独立仓库"
    shutil.copytree(repo, portable)
    journal = Journal(machine / "appdata/history.sqlite3")
    assert not journal.batches() and not Catalog(journal.path).roots()
    assert read_marker(portable)["repository_id"] == read_marker(repo)["repository_id"]
    version, = list_versions(portable)
    assert version["id"] == report["id"] and not verify_repository(portable)["issues"]
    dest = machine / "恢复目录"
    dest.mkdir()
    outcome = restore(prepare_restore(portable, version["id"], dest))
    assert (outcome["restored"], outcome["skipped"], outcome["failed"]) == (1, 0, 0), outcome
    assert (dest / FILE_NAME).read_bytes() == CONTENTS
    assert not journal.batches() and not Catalog(journal.path).roots()
    print("只携带仓库的空白配置可读取版本、校验并恢复，SHA-256 一致。")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--seed"]:
        seed_v3(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.path.insert(0, str(PROJECT))
        with tempfile.TemporaryDirectory(prefix="archive-v3-to-v4-") as path:
            work = Path(path).resolve()
            prepare_v3(work)
            registered_root = validate_copy(work)
            fresh_config(work, registered_root)
