"""真实窗口点击取消并撤销已完成项，所有文件均为临时生成。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from archive_desk.journal import Journal
from archive_desk.window import MainWindow
from desktop_v2_rules import click, modal, wait


def accept(dialog):
    assert isinstance(dialog,QMessageBox)
    confirm = next(b for b in dialog.buttons() if dialog.buttonRole(b)==QMessageBox.ButtonRole.AcceptRole)
    QTest.mouseClick(confirm,Qt.MouseButton.LeftButton)


def run(temp):
    source, target = temp / "来源", temp / "目标"
    source.mkdir()
    target.mkdir()
    contents = {f"归档-{i:03}.txt": f"sample {i}".encode() for i in range(100)}
    for name,data in contents.items():
        (source/name).write_bytes(data)
    journal = Journal(temp/"history.sqlite")
    window = MainWindow(journal)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    try:
        with patch.object(QFileDialog,"getExistingDirectory",return_value=str(source)):
            click(window,"openButton")
        wait(lambda:window.job is None)
        with patch.object(QFileDialog,"getExistingDirectory",return_value=str(target)):
            click(window,"targetButton")
        wait(lambda:window.job is None)
        click(window,"selectVisibleButton")
        modal(lambda:click(window,"applyButton"),accept)
        # 等文件实际提交后，在可见窗口点击；运行平台决定取消时成功的数量。
        wait(lambda:window.stop_button.isVisible() and window.progress_bar.maximum()==len(contents)
             and "成功 0" not in window.stats_label.text() and "成功 " in window.stats_label.text())
        click(window,"stopOperationButton")
        wait(lambda:window.job is None)
        batch, = journal.batches()
        moved = batch["moved"]
        stopped = batch["cancelled"]
        assert 0<moved<len(contents) and moved+stopped==len(contents),batch
        assert f"已移动 {moved}" in window.notice.text()
        assert f"已取消 {stopped}" in window.notice.text()
        for row in journal.items(batch["id"]):
            old,new = Path(row["source"]),Path(row["target"])
            if row["status"]=="moved":
                assert not old.exists() and new.read_bytes()==contents[old.name]
            else:
                assert row["status"]=="cancelled" and not new.exists()
                assert old.read_bytes()==contents[old.name]
        modal(lambda:click(window,"undoButton"),accept)
        wait(lambda:window.job is None)
        assert journal.batches()[0]["undone"]==moved
        assert all((source/name).read_bytes()==data for name,data in contents.items())
        assert all(not (target/"文档"/name).exists() for name in contents)
        print(f"Windows 可见窗口显示真实进度与统计；成功 {moved} 项后取消 {stopped} 项，随后撤销 {moved} 项。")
    finally:
        window.close()


if __name__=="__main__":
    app=QApplication([])
    assert app.platformName()=="windows"
    with TemporaryDirectory(prefix="archive-desk-cancel-ui-") as temp:
        run(Path(temp))
