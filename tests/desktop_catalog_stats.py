"""在 Windows 档案页逐项核对空间统计及精确检索跳转。"""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from archive_desk.catalog import Catalog
from archive_desk.journal import Journal
from archive_desk.window import MainWindow
from desktop_v2_rules import click,wait


def run(work):
    db = work/"data"/"history.sqlite"
    registry = Catalog(db)
    roots = {}
    for group,files in (("一库",{"README":4,"图纸/说明.txt":9,"图纸/原件/改动.txt":10}),
                        ("二库",{"LICENSE":6,"图纸/方案.pdf":14,"附件.txt":7})):
        base = work/group
        base.mkdir()
        for name,size in files.items():
            path = base/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(b"x"*size)
        rid = registry.add_root(group,base)
        registry.refresh(rid)
        roots[group] = rid

    window = MainWindow(Journal(db))
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    click(window,"catalogButton")
    archive = window.catalog
    stats,search = archive.stats,archive.search
    def settled():
        wait(lambda:window.job is None and search.job is None and not search.timer.isActive())
    def drill(row):
        stats.table.selectRow(stats.model.records.index(row))
        click(window,"filesFromSpace")
        settled()
        result = search.model.records
        assert len(result)==row["count"] and sum(f["size"] for f in result)==row["bytes"]
        return result
    try:
        settled()
        archive.tabs.setCurrentWidget(stats)
        settled()
        assert sum(r["count"] for r in stats.model.records)==6
        assert sum(r["bytes"] for r in stats.model.records)==50
        empty_ext = next(r for r in stats.model.records if r["key"]=="")
        result = drill(empty_ext)
        assert archive.root_id is None and {r["name"] for r in result}=={"README","LICENSE"}
        assert search.clear_group.isVisible()

        archive.select_root(roots["一库"])
        archive.tabs.setCurrentWidget(stats)
        settled()
        stats.group.setCurrentIndex(stats.group.findData("directory"))
        stats.depth.setValue(2)
        click(window,"computeSpace")
        settled()
        top = next(r for r in stats.model.records if r["key"]=="")
        assert {r["name"] for r in drill(top)}=={"README"}
        assert archive.root_id==roots["一库"]
        archive.tabs.setCurrentWidget(stats)
        settled()
        named = next(r for r in stats.model.records if r["key"]=="图纸")
        assert {r["name"] for r in drill(named)}=={"说明.txt"}

        archive.select_root(None)
        archive.tabs.setCurrentWidget(stats)
        settled()
        named = next(r for r in stats.model.records if r["root_id"]==roots["二库"] and r["key"]=="图纸")
        assert {r["name"] for r in drill(named)}=={"方案.pdf"}
        assert archive.root_id==roots["二库"]
        click(window,"clearArchiveGroup")
        settled()
        assert len(search.model.records)==3
        print("Windows 可见空间统计页验收通过。两库 6 项共 50 字节；无扩展名、根目录、指定深度、跨库同名目录均精确跳转。")
    finally:
        window.close()
        wait(lambda:not window.isVisible())


if __name__=="__main__":
    app = QApplication([])
    assert app.platformName()=="windows"
    with TemporaryDirectory(prefix="archive-desk-stats-ui-") as folder:
        run(Path(folder))
