"""创建两个独立的本地演示资料库，外部变更仅作用于本次新建的样例。"""
import tempfile
from pathlib import Path

from .catalog import Catalog
from .catalog_snapshots import SnapshotStore
from .core import check_cancel


class _DemoCatalog(Catalog):
    blind_folder = None

    def _entries(self, folder):
        if self.blind_folder is not None and Path(folder) == self.blind_folder:
            raise PermissionError("演示模拟：当前账户无法读取此子目录")
        return super()._entries(folder)


def create_catalog_demo(db_path, cancel=None, progress=None):
    """返回演示目录、本地与离线库 ID、两库各自的旧/新快照 ID。"""
    def step(message):
        check_cancel(cancel)
        if progress:
            progress(message)

    step("正在生成中文演示文件…")
    base = Path(tempfile.mkdtemp(prefix="ArchiveDesk-catalog-"))
    project, removable = base / "项目资料", base / "移动资料盘"
    project.mkdir()
    removable.mkdir()

    def write(root, name, contents):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    changed = write(project, "文稿/项目说明.txt", "初稿内容")
    missing = write(project, "收件/临时通知.txt", "外部删除示例")
    renamed = write(project, "收件/会议纪要.txt", "唯一的会议记录")
    duplicate = write(project, "草稿/同内容副本.txt", "两份文件的相同内容")
    write(project, "参考/固定副本.txt", "两份文件的相同内容")
    write(project, "受限目录/合同.txt", "先前读到的合同摘要")
    write(project, "参考/索引须知.txt", "跨扫描保留")
    note = write(removable, "资料/外拍记录.txt", "第一轮")
    write(removable, "资料/设备清单.txt", "便携设备")

    cat = _DemoCatalog(db_path)
    snapshots = SnapshotStore(db_path)
    token = base.name.rsplit("-", 1)[-1]
    root_id = cat.add_root(f"项目资料 · {token}", project)
    offline_id = cat.add_root(f"演示移动盘 · {token}", removable)
    step("正在扫描两个资料库并保存第一轮快照…")
    cat.refresh(root_id, hash_files=True, cancel=cancel)
    old = snapshots.create(root_id, "第一轮 · 原始清单", cancel)
    cat.refresh(offline_id, hash_files=False, cancel=cancel)
    usb_old = snapshots.create(offline_id, "第一轮 · 外业资料", cancel)

    step("正在模拟外部新增、删除、修改和改名…")
    write(project, "交付/验收清单.txt", "新入库文件")
    changed.write_text("更新说明", encoding="utf-8")
    missing.unlink()
    target = project / "交付/会议纪要（定稿）.txt"
    renamed.rename(target)
    duplicate.rename(project / "参考/同内容新副本.txt")
    note.write_text("第二轮增加了记录，只有元数据可比较", encoding="utf-8")

    step("正在保存第二轮清单与未覆盖范围…")
    # Scope the simulated permission error to this instance and this new
    # directory. The user's ACLs and every other library scanner are untouched.
    cat.blind_folder = project / "受限目录"
    cat.refresh(root_id, hash_files=True, cancel=cancel)
    new = snapshots.create(root_id, "第二轮 · 外部变更", cancel)
    cat.refresh(offline_id, hash_files=False, cancel=cancel)
    usb_new = snapshots.create(offline_id, "第二轮 · 离线前清单", cancel)

    step("正在模拟移动盘离线…")
    offline_path = base / "移动资料盘（模拟已拔出）"
    removable.rename(offline_path)
    if cat.probe(offline_id):
        raise RuntimeError("演示盘的离线状态未被识别")
    return {"directory": base, "root_id": root_id, "offline_id": offline_id,
            "old_id": old, "new_id": new, "offline_old_id": usb_old,
            "offline_new_id": usb_new, "offline_path": offline_path}
