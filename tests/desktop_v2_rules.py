"""在可见 Windows 窗口验证 V2 规则中心与只读计划。"""
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton, QInputDialog

from archive_desk.journal import Journal
from archive_desk.rules_dialog import RuleEditor
from archive_desk.window import MainWindow


def wait(check):
    end = time.monotonic() + 25
    while not check():
        if time.monotonic() > end:
            raise AssertionError("窗口状态超时")
        QApplication.processEvents()
        time.sleep(.012)


def click(parent,name):
    btn = parent.findChild(QPushButton,name)
    assert btn is not None and btn.isEnabled(), name
    QTest.mouseClick(btn, Qt.MouseButton.LeftButton)


def modal(trigger, exercise):
    failures = []
    def work():
        dialog = QApplication.activeModalWidget()
        try:
            assert dialog is not None
            exercise(dialog)
        except BaseException as exc:
            failures.append(exc)
            if dialog:
                dialog.reject()
    QTimer.singleShot(100,work)
    trigger()
    if failures:
        raise failures[0]


def run(work):
    source = work / "来源"
    target = work / "目标"
    docs = source / "项目"
    docs.mkdir(parents=True)
    target.mkdir()
    day = datetime.now()
    folder = target / "成品" / day.strftime("%Y") / day.strftime("%m") / "txt"
    folder.mkdir(parents=True)
    first = docs / "报告-a.txt"
    second = docs / "报告-b.txt"
    first.write_text("alpha",encoding="utf-8")
    second.write_text("bravo",encoding="utf-8")
    (folder / first.name).write_text("occupied",encoding="utf-8")
    journal = Journal(work / "audit.sqlite3")
    window = MainWindow(journal)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    def settle():
        wait(lambda: window.job is None)
    def plan(path):
        return next(p for p in window.model.plans if p.file.path == path)

    try:
        with patch.object(QFileDialog,"getExistingDirectory",return_value=str(source)):
            click(window,"openButton")
        settle()
        window.recursive.setChecked(True)
        settle()
        with patch.object(QFileDialog,"getExistingDirectory",return_value=str(target)):
            click(window,"targetButton")
        settle()

        def edit_rules(dialog):
            def fill(editor):
                assert isinstance(editor,RuleEditor)
                values = dict(name="项目报告",extensions="txt",name_glob="报告-*.txt",source_subdir="项目",
                              min_size="2",max_size="100",modified_from=day.strftime("%Y-01-01"),
                              modified_to=day.strftime("%Y-12-31"),target="成品/{year}/{month}/{ext}")
                for key,text in values.items():
                    editor.fields[key].setText(text)
                click(editor,"saveButton")
            modal(lambda:click(dialog,"newRule"),fill)
            for _ in range(4):
                click(dialog,"raiseRule")
            click(dialog,"copyRule")
            assert dialog.rules[1].name.endswith("（副本）")
            dialog.grid.item(1,0).setCheckState(Qt.CheckState.Unchecked)
            with patch.object(QFileDialog,"getOpenFileName",return_value=(str(first),"")):
                click(dialog,"trialButton")
            wait(lambda: str(folder/first.name) in dialog.result.text())
            assert "项目报告" in dialog.result.text()
            click(dialog,"saveButton")
        modal(lambda:click(window,"rulesButton"),edit_rules)
        settle()
        assert plan(first).conflict_type=="existing" and plan(second).status=="ready"
        assert plan(first).rule=="项目报告" and plan(first).target.parent==folder
        for combo, value in zip(window.facets,(str(docs),str(folder),"项目报告","existing")):
            combo.setCurrentIndex(combo.findData(value))
        assert window.proxy.rowCount()==1
        click(window,"numberButton")
        settle()
        assert plan(first).status=="ready" and "(1)" in plan(first).target.name
        window.search.setText(first.name)
        with patch.object(QInputDialog,"getText",return_value=("手选名称.txt",True)):
            click(window,"renameButton")
        settle()
        assert plan(first).target==folder/"手选名称.txt"
        window.bulk_skip(True)
        settle()
        assert plan(first).status=="skip"
        window.bulk_skip(False)
        settle()
        assert plan(first).status=="ready"
        click(window,"selectVisibleButton")
        assert "5 字节" in window.selected_text.text()
        # 仅测试程序改动生成的目标，模拟预览与执行之间的变化。
        (folder/"手选名称.txt").write_text("late",encoding="utf-8")
        window.recheck_selected(False)
        settle()
        assert plan(first).status=="review" and not window.model.checked
        assert first.read_bytes()==b"alpha" and second.read_bytes()==b"bravo"
        assert not journal.batches()
        assert (folder/first.name).read_bytes()==b"occupied"
        assert (folder/"手选名称.txt").read_bytes()==b"late"
        rules = window.rules
    finally:
        window.close()
    fresh = MainWindow(Journal(journal.path))
    assert fresh.rules == rules and fresh.rules[0].name=="项目报告" and not fresh.rules[1].enabled
    fresh.close()
    print("V2 Windows 规则中心、试跑、筛选、编号、改名、跳过、字节数、待复核和规则持久化通过。")


if __name__=="__main__":
    app = QApplication([])
    assert app.platformName()=="windows"
    with TemporaryDirectory(prefix="archive-desk-v2-rules-") as scratch:
        run(Path(scratch))
