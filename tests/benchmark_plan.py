"""在系统临时目录创建一万份合成文件，分别计时扫描与预览。"""
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from archive_desk.core import default_rules
from archive_desk.planning import preview
from archive_desk.scanner import scan


def benchmark():
    extensions = (".jpg", ".pdf", ".zip", ".msi", ".dat")
    with tempfile.TemporaryDirectory(prefix="ArchiveDesk-scale-") as temp:
        root = Path(temp) / "来源"
        target = Path(temp) / "目标"
        root.mkdir()
        target.mkdir()
        for i in range(100):
            folder = root / f"收件{i:03}"
            folder.mkdir()
            for j in range(100):
                number = i * 100 + j
                (folder / f"样例{number:05}{extensions[j % 5]}").write_bytes(
                    f"synthetic-{number:05}".encode("ascii"))
        start = perf_counter()
        files = scan(root, recursive=True)
        scanned = perf_counter()
        plans = preview(root, files, default_rules(), target_root=target)
        planned = perf_counter()
        counts = Counter(p.status for p in plans)
        assert len(files) == 10000 and not files.warnings
        assert counts == {"ready": 8000, "skip": 2000} and not list(target.iterdir())
        print(json.dumps({"files": len(files), "scan_seconds": round(scanned-start, 3),
                          "preview_seconds": round(planned-scanned, 3), "counts": counts}))


if __name__ == "__main__":
    benchmark()
