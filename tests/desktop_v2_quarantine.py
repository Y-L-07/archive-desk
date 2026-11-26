"""点击真实 Windows 主窗口验收重复件保留、隔离预览、执行和历史撤销。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def verify(scratch):
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QAbstractButton, QMessageBox
    from archive_desk.duplicates_ui import DuplicatesDialog
    from archive_desk.journal import Journal
    from archive_desk.window import MainWindow

    app = QApplication([])
    assert app.platformName() == "windows"
    journal = Journal(scratch / "history.db")
    window = MainWindow(journal)

    def wait_for(predicate):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            QTest.qWait(25)
        raise AssertionError(f"窗口状态超时：{predicate}")

    def click(parent, name):
        control = parent.findChild(QAbstractButton, name)
        assert control, name
        wait_for(lambda: control.isEnabled() and control.isVisible())
        QTest.mouseClick(control, Qt.MouseButton.LeftButton)

    def checkbox(table, idx):
        table.scrollTo(idx)
        QTest.qWait(30)
        rect = table.visualRect(idx)
        point = QPoint(rect.left() + 9, rect.center().y())
        assert table.viewport().rect().contains(point)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=point)

    def activate_modal(name, expected_type, exercise):
        errors, visited = [], []

        def run():
            try:
                dialog = app.activeModalWidget()
                assert isinstance(dialog, expected_type), (name, dialog)
                wait_for(lambda: dialog.isVisible() and dialog.windowHandle().isExposed())
                visited.append(True)
                exercise(dialog)
            except BaseException as exc:
                errors.append(exc)
                if app.activeModalWidget():
                    app.activeModalWidget().reject()

        QTimer.singleShot(100, run)
        click(window, name)
        if errors:
            raise errors[0]
        assert visited, name

    def approve(dialog):
        button = next(b for b in dialog.buttons()
                      if dialog.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    def manifest():
        return {p: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in window.root.rglob("*") if p.is_file()}

    window.show()
    try:
        wait_for(lambda: window.isVisible() and window.windowHandle().isExposed())
        click(window, "demoButton")
        wait_for(lambda: window.job is None and len(window.model.plans) == 7)
        assert window.root.is_relative_to(scratch)
        keeper_path = window.root / "会议纪要.txt"
        copy_path = window.root / "会议纪要（副本）.txt"
        initial = manifest()
        assert initial[keeper_path] == initial[copy_path]

        def choose(dialog):
            model = wait_for(lambda: dialog.table.model())
            assert model.rowCount() == 2 and not dialog.plan_button.isEnabled()
            names = [model.index(r, 1).data() for r in range(2)]
            keep = names.index(keeper_path.name)
            other = names.index(copy_path.name)
            for row in (keep, other):
                assert model.index(row, 4).data() and model.index(row, 4).data() != "时间不可用"
            select = lambda row: checkbox(dialog.table, model.index(row, model.keeper_column))
            check = lambda row: model.index(row, model.keeper_column).data(Qt.ItemDataRole.CheckStateRole)
            select(other)
            assert check(other) == Qt.CheckState.Checked
            select(keep)
            assert check(keep) == Qt.CheckState.Checked and check(other) == Qt.CheckState.Unchecked
            select(keep)
            assert not dialog.plan_button.isEnabled()
            select(keep)
            assert dialog.plan_button.isEnabled()
            click(dialog, "addQuarantinePlan")

        activate_modal("duplicateButton", DuplicatesDialog, choose)
        wait_for(lambda: window.job is None and any(p.kind == "quarantine" for p in window.model.plans))
        p, = window.model.plans
        assert p.status == "ready" and p.keeper.path == keeper_path and p.file.path == copy_path
        assert p.target == window.root / "隔离区" / copy_path.name
        assert p.expected_hash == initial[keeper_path]
        assert not p.target.parent.exists() and manifest() == initial and not journal.batches()
        assert not window.model.checked and not window.apply_button.isEnabled()
        checkbox(window.table, window.table.model().index(0, 0))
        assert window.model.checked == {0}
        activate_modal("applyButton", QMessageBox, approve)
        wait_for(lambda: journal.batches() and journal.batches()[0]["moved"] == 1 and window.job is None)
        batch = journal.batches()[0]
        row, = journal.items(batch["id"])
        assert row["source"] == str(copy_path) and json.loads(row["intent"])["kind"] == "quarantine"
        moved = dict(initial)
        moved[p.target] = moved.pop(copy_path)
        assert manifest() == moved
        click(window, "historyButton")
        activate_modal("undoButton", QMessageBox, approve)
        wait_for(lambda: journal.items(batch["id"])[0]["status"] == "undone" and window.job is None)
        assert manifest() == initial
        print("实窗验收通过：指定保留项、加入只读隔离计划、勾选确认、移动 1 个副本、历史撤销；所有原始文件 SHA-256 一致。")
    finally:
        window.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--window":
        verify(Path(sys.argv[2]))
    else:
        with tempfile.TemporaryDirectory(prefix="archive-v2-quarantine-") as scratch:
            env = dict(os.environ, TEMP=scratch, TMP=scratch, QT_QPA_PLATFORM="windows")
            subprocess.run([sys.executable, __file__, "--window", scratch], env=env, cwd=PROJECT,
                           check=True, timeout=90)
