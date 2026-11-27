"""Make a temporary source library and a separate empty repository for manual backup."""
import tempfile
from pathlib import Path

from .catalog import Catalog


def create_backup_demo(db_path):
    directory = Path(tempfile.mkdtemp(prefix="ArchiveDesk-backup-demo-"))
    source, repository = directory / "项目资料", directory / "备份仓库"
    for folder in (source / "交接", source / "参考", source / "空目录", repository):
        folder.mkdir(parents=True)
    note = "第一版交接记录。\n修改本文件后再备份，可以分别找回两个版本。\n"
    (source / "交接" / "项目说明.txt").write_text(note, encoding="utf-8")
    same = "两份文件名不同，备份仓库只需要一个内容对象。\n".encode("utf-8")
    (source / "交接" / "清单.txt").write_bytes(same)
    (source / "参考" / "清单副本.txt").write_bytes(same)
    (source / "样例.bin").write_bytes(bytes(range(256)) * 32)
    name = "备份演示 · " + directory.name.rsplit("-", 1)[-1]
    root_id = Catalog(db_path).add_root(name, source)
    return dict(root_id=root_id, source=source, repository=repository, directory=directory)
