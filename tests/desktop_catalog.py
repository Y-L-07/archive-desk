"""在可见的 Windows 窗口验收 V3。所有文件仅来自独立临时目录。"""
import csv
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QInputDialog, QMessageBox

from archive_desk.journal import Journal
from archive_desk.window import MainWindow
from desktop_v2_rules import click, modal, wait


def confirm(box):
    yes = next(b for b in box.buttons() if box.buttonRole(b)==QMessageBox.ButtonRole.AcceptRole)
    QTest.mouseClick(yes,Qt.MouseButton.LeftButton)


def input_dialog(value):
    def fill(prompt):
        assert isinstance(prompt,QInputDialog)
        prompt.setTextValue(value)
        prompt.accept()
    return fill


def manifest(root):
    return {p.relative_to(root):hashlib.sha256(p.read_bytes()).digest() for p in root.rglob("*") if p.is_file()}


def exercise(work):
    root = work/"中文资料库"
    root.mkdir()
    manual = root/"项目手册.txt"
    manual.write_text("供阅览",encoding="utf-8")
    old = root/"现场纪要.txt"
    old.write_text("记录现场",encoding="utf-8")
    journal = Journal(work/"appdata"/"history.sqlite")
    window = MainWindow(journal)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    click(window,"catalogButton")
    tab = window.catalog
    pane = tab.search
    snaps = tab.snaps

    def settled():
        wait(lambda:window.job is None and pane.job is None and not pane.timer.isActive())

    def snap(name):
        modal(lambda:click(window,"createSnapshot"),input_dialog(name))
        settled()

    try:
        with patch.object(QFileDialog,"getExistingDirectory",return_value=str(root)):
            modal(lambda:click(window,"addArchiveRoot"),input_dialog("测试资料"))
        settled()
        assert tab.root_id and "尚未索引" in pane.summary.text()
        tab.hash.setChecked(True)
        before = manifest(root)
        click(window,"refreshArchive")
        settled()
        assert len(pane.model.records)==2 and all(r["digest"] for r in pane.model.records), window.notice.text()
        assert manifest(root)==before
        pane.keyword.setText("项目")
        settled()
        assert pane.current()["name"]==manual.name
        modal(lambda:click(window,"addArchiveTags"),input_dialog("项目组,已审"))
        settled()
        pane.note.setPlainText("可交付")
        pane.favorite.setChecked(True)
        click(window,"saveAnnotation")
        settled()
        assert pane.current()["note"]=="可交付" and pane.current()["favorite"]
        assert set(pane.current()["tags"])=={"项目组","已审"}
        def filters(dialog):
            dialog.fields["ext"].setText("txt")
            dialog.fields["tag"].setText("已审")
            dialog.fields["min_size"].setText("1")
            dialog.fields["date_from"].setText("2000-01-01")
            dialog.favorite.setChecked(True)
            dialog.online.setCurrentIndex(dialog.online.findData(True))
            box = dialog.findChild(QDialogButtonBox)
            QTest.mouseClick(box.button(QDialogButtonBox.StandardButton.Ok),Qt.MouseButton.LeftButton)
        modal(lambda:click(window,"archiveFilters"),filters)
        settled()
        assert pane.current()["name"]==manual.name
        modal(lambda:click(window,"saveQuery"),input_dialog("项目检索"))
        pane.keyword.setText("没有这个文件名")
        settled()
        assert not pane.model.records and "无匹配" in pane.summary.text()
        pane.saved.setCurrentIndex(pane.saved.findData("项目检索"))
        pane.saved.activated.emit(pane.saved.currentIndex())
        settled()
        assert pane.current()["name"]==manual.name and pane.keyword.text()=="项目"
        print("真实窗口建立资料库、索引哈希、多维筛选、标签、收藏、备注及保存搜索通过。",flush=True)

        tab.tabs.setCurrentWidget(snaps)
        snap("修改前")
        old.rename(root/"现场定稿.txt")
        (root/"新增记录.txt").write_text("新增",encoding="utf-8")
        manual.write_text("第二版手册",encoding="utf-8")
        click(window,"refreshArchive")
        settled()
        snap("修改后")
        assert snaps.new.count()==2
        snaps.old.setCurrentIndex(0)
        snaps.new.setCurrentIndex(1)
        after = manifest(root)
        click(window,"compareSnapshots")
        settled()
        assert {r["status"] for r in snaps.model.records}=={"changed","added","moved"},snaps.model.records
        snaps.filter.setCurrentIndex(snaps.filter.findData("moved"))
        assert len(snaps.model.records)==1
        report = work/"差异.csv"
        with patch.object(QFileDialog,"getSaveFileName",return_value=(str(report),"CSV")):
            click(window,"exportDiff")
        settled()
        with report.open(encoding="utf-8-sig",newline="") as reader:
            lines = list(csv.reader(reader))
        assert len(lines)==2 and any("现场" in cell for cell in lines[1])
        assert manifest(root)==after and not journal.batches()
        print("两个命名快照、只读差异、按类别导出 UTF-8 CSV 通过。",flush=True)

        tab.tabs.setCurrentWidget(pane)
        alternate = work/"路径已变化"
        root.rename(alternate)
        pane.keyword.setText("")
        pane.conditions={}
        pane.criteria_changed()
        settled()
        assert pane.current() and not pane.current()["online"]
        assert not pane.open_button.isVisible() and "上次见到的位置" in pane.detail_info.text()
        with patch.object(QFileDialog,"getExistingDirectory",return_value=str(alternate)):
            modal(lambda:click(window,"rebindArchive"),confirm)
        settled()
        assert pane.current()["online"]
        pane.keyword.setText("项目手册")
        settled()
        click(window,"previewArchiveEntry")
        settled()
        assert len(window.model.plans)==1 and window.model.plans[0].file.path==alternate/manual.name
        assert not window.model.checked and not journal.batches()
        click(window,"selectVisibleButton")
        modal(lambda:click(window,"applyButton"),confirm)
        settled()
        assert journal.batches()[0]["moved"]==1, window.notice.text()
        click(window,"historyArchiveButton")
        settled()
        assert window.pages.currentIndex()==3 and pane.current()["name"]==manual.name
        assert pane.current()["relpath"].replace("\\","/")==f"文档/{manual.name}"
        click(window,"archiveHistory")
        settled()
        assert window.pages.currentIndex()==1 and window.active_batch==journal.batches()[0]["id"]
        modal(lambda:click(window,"undoButton"),confirm)
        settled()
        assert manifest(alternate)==after
        print("离线浏览、显式重绑、V2 预览与确认、批次和档案双向跳转及撤销通过。",flush=True)

        click(window,"catalogButton")
        settled()
        cached_total = tab.registry.roots()[0]["file_count"]
        for i in range(400):
            (alternate / f"批量-{i:04}.txt").write_bytes(str(i).encode())
        click(window,"refreshArchive")
        wait(lambda: window.stop_button.isVisible() and " 项 " in window.progress_label.text())
        click(window,"stopOperationButton")
        settled()
        assert tab.registry.roots()[0]["file_count"]==cached_total
        assert "已取消" in tab.status.text() and "已取消" in window.notice.text()
        print("手动取消后台索引后，上次发布的条目数保持一致。",flush=True)

        refresh = tab.registry.refresh
        def cancel_after_commit(*args, **kwargs):
            result = refresh(*args, **kwargs)
            kwargs["cancel"].set()
            return result
        with patch.object(tab.registry, "refresh", cancel_after_commit):
            click(window,"refreshArchive")
            settled()
        assert tab.registry.roots()[0]["file_count"] == cached_total + 400
        assert "索引刷新完成" in window.notice.text()

        import tempfile
        with patch.object(tempfile,"tempdir",str(work)):
            click(window,"archiveDemo")
            settled()
        assert len(tab.registry.roots())==3 and snaps.new.count()==2
        assert not all(r["online"] for r in tab.registry.roots())
        snaps.filter.setCurrentIndex(0)
        click(window,"compareSnapshots")
        settled()
        assert {r["status"] for r in snaps.model.records}=={
            "added","missing","changed","moved","uncertain"}
        print("真实演示入口生成两个独立资料库及两轮快照，包含离线库和五类差异。",flush=True)
        original_rows = snaps.store.list_snapshots(tab.root_id)
        comparison = list(snaps.model.records)
        saved = {}
        for source, wanted_scope in (("last_complete", "完整覆盖"), ("current", "部分覆盖")):
            index = snaps.source.findData(source)
            assert index >= 0
            snaps.source.setCurrentIndex(index)
            shown = snaps.source.currentText()
            assert wanted_scope in shown and "第 " in shown
            snap("从所选清单创建-" + source)
            record = snaps.store.list_snapshots(tab.root_id)[0]
            assert record["source"] == source and record["id"] == snaps.new.currentData()
            saved[source] = record["id"]
        snaps.old.setCurrentIndex(snaps.old.findData(saved["last_complete"]))
        snaps.new.setCurrentIndex(snaps.new.findData(saved["current"]))
        click(window, "compareSnapshots")
        settled()
        assert snaps.model.records == comparison
        assert snaps.store.list_snapshots(tab.root_id)[2:] == original_rows
        print("部分覆盖后在窗口分别保存上一完整清单和当前带盲区清单，扫描依据及差异可核对，原快照仍在。",flush=True)
    finally:
        window.close()
        wait(lambda:not window.isVisible())


if __name__=="__main__":
    app=QApplication([])
    assert app.platformName()=="windows"
    with TemporaryDirectory(prefix="archive-desk-v3-ui-") as temporary:
        exercise(Path(temporary))
