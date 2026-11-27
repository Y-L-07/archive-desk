"""Real-file backup benchmark; creates all data in a fresh temporary directory."""
import hashlib
import json
import platform
import sys
import tempfile
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_desk.backup_format import verify_repository
from archive_desk.backup_writer import prepare_backup, write_backup
from archive_desk.catalog import Catalog


def example(index):
    return hashlib.shake_256(index.to_bytes(8, "little")).digest(4096)


def execute(root, repository):
    start = perf_counter()
    plan = prepare_backup(root, repository)
    confirmed = perf_counter()
    result = write_backup(plan)
    finished = perf_counter()
    assert plan.count == 10_000 and result["status"] == "complete", result
    size = sum(p.stat().st_size for p in repository.rglob("*") if p.is_file())
    stats = dict(preview_s=round(confirmed-start,3),backup_s=round(finished-confirmed,3),
                 total_s=round(finished-start,3),source_bytes=plan.size,bytes_read=result["bytes_read"],
                 new_objects=result["new_objects"],repository_bytes=size)
    print(json.dumps(stats),flush=True)
    return stats


def main():
    print(platform.platform(),platform.machine(),"Python",platform.python_version(),flush=True)
    with tempfile.TemporaryDirectory(prefix="ArchiveDesk-V4-10000-") as temp:
        base = Path(temp)
        source, repository = base/"源库",base/"离线仓库"
        source.mkdir()
        repository.mkdir()
        paths = []
        for group in range(100):
            folder = source / f"项目-{group:03d}"
            folder.mkdir()
            for index in range(group*100,(group+1)*100):
                path = folder / f"文档-{index:05d}.bin"
                path.write_bytes(example(index % 9000))
                paths.append(path)
        registry = Catalog(base/"appdata"/"history.sqlite3")
        registry.add_root("合成基准",source)
        root, = registry.roots()
        first = execute(root,repository)
        assert first["new_objects"] == 9000, first
        for i in range(20):
            paths[i].write_bytes(example(10_000+i))
        for i in range(20,30):
            paths[i].rename(paths[i].with_name(f"改名-{i}.bin"))
        for i in range(30,35):
            paths[i].unlink()
            (paths[i].parent / f"新增-{i}.bin").write_bytes(example(11_000+i))
        second = execute(root,repository)
        assert second["new_objects"] == 25, second
        report = verify_repository(repository)
        assert (report["versions"], report["objects"], report["issues"]) == (2,9025,[]), report
        print("Two versions, 9,025 distinct objects verified.",flush=True)


if __name__ == "__main__":
    main()
