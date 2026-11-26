"""可复跑的 Windows 真窗口验收。所有演示文件、方案和日志均在独立临时目录内。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def verify(scratch):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (QAbstractButton, QApplication, QFileDialog,
                                   QInputDialog, QLineEdit, QMessageBox)

    from archive_desk.duplicates_ui import DuplicatesDialog
    from archive_desk.extras_ui import ProfileDialog
    from archive_desk.journal import Journal
    from archive_desk.profiles import ProfileStore
    from archive_desk.window import MainWindow

    app = QApplication([])
    assert app.platformName() == "windows"
    journal = Journal(scratch / "history.sqlite3")
    window = MainWindow(journal)

    def wait_for(test, timeout=12):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = test()
            if value:
                return value
            QTest.qWait(25)
        raise AssertionError(f"窗口状态超时：{test}")

    def click(parent, object_name):
        control = parent.findChild(QAbstractButton, object_name)
        assert control is not None and control.isEnabled(), object_name
        assert control.isVisible(), object_name
        QTest.mouseClick(control, Qt.MouseButton.LeftButton)

    def in_modal(trigger, dialog_type, exercise):
        # Actions dispatched through QTimer run inside QDialog.exec's nested event
        # loop. Propagate callback exceptions to the test process exit status.
        errors = []
        visited = []

        def run():
            try:
                dialog = app.activeModalWidget()
                assert isinstance(dialog, dialog_type), (dialog_type, dialog)
                wait_for(lambda: dialog.isVisible() and dialog.windowHandle().isExposed())
                visited.append(True)
                exercise(dialog)
            except BaseException as exc:
                errors.append(exc)
                dialog = app.activeModalWidget()
                if dialog:
                    dialog.reject()

        QTimer.singleShot(100, run)
        trigger()
        if errors:
            raise errors[0]
        assert visited, f"没有在窗口中执行测试：{dialog_type.__name__}"

    def snapshot():
        return {p.relative_to(window.root): hashlib.sha256(p.read_bytes()).digest() if p.is_file()
                else None for p in window.root.rglob("*")}

    def image_plan():
        return next(plan for plan in window.model.plans if plan.file.category == "图片")

    window.show()
    try:
        wait_for(lambda: window.isVisible() and window.windowHandle().isExposed())
        with patch.object(tempfile, "tempdir", str(scratch)):
            click(window, "demoButton")
            wait_for(lambda: window.job is None and len(window.model.plans) == 7)
        assert window.root.is_relative_to(scratch)
        original = snapshot()
        baseline = list(window.rules)
        old_image = image_plan()
        assert old_image.target.parent == window.root / "图片"

        def check_duplicates(dialog):
            wait_for(lambda: dialog.table.model() is not None)
            model = dialog.table.model()
            assert model.rowCount() == 2
            assert {model.index(i, 0).data() for i in range(2)} == {"第 1 组"}
            assert {model.index(i, 1).data() for i in range(2)} == {
                "会议纪要.txt", "会议纪要（副本）.txt"}
            assert "发现 1 组相同内容" in dialog.summary.text()
            click(dialog, "duplicateClose")

        in_modal(lambda: click(window, "duplicateButton"), DuplicatesDialog, check_duplicates)
        assert snapshot() == original and not journal.batches()
        print("重复件主窗口入口：显示 1 组、2 个文件，原目录一致。", flush=True)

        export_path = scratch / "saved.json"
        import_path = scratch / "import.json"
        profile_name = "Desktop-check"
        imported = "导入方案验收"
        new_target = "按项目整理/图片"
        store = ProfileStore(journal.path)

        def exercise_profiles(dialog):
            def name_profile(prompt):
                field = prompt.findChild(QLineEdit)
                assert field is not None
                QTest.keyClicks(field, profile_name)
                QTest.keyClick(field, Qt.Key.Key_Return)

            in_modal(lambda: click(dialog, "saveProfile"), QInputDialog, name_profile)
            assert store.load(profile_name) == baseline
            with patch.object(QFileDialog, "getSaveFileName",
                              return_value=(str(export_path), "JSON")):
                click(dialog, "exportProfile")
            exported = json.loads(export_path.read_text(encoding="utf-8"))
            assert exported["name"] == profile_name
            assert exported["rules"][0]["target"] == "图片"
            # 明确选择“更新已保存方案”只修改方案库，当前工作规则和目录保持一致。
            exported["rules"][0]["target"] = "相同名称更新"
            import_path.write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
            def update_existing(prompt):
                yes = next(b for b in prompt.buttons() if b.text()=="更新已保存方案")
                QTest.mouseClick(yes,Qt.MouseButton.LeftButton)
            with patch.object(QFileDialog, "getOpenFileName",
                              return_value=(str(import_path), "JSON")):
                in_modal(lambda:click(dialog,"importProfile"), QMessageBox, update_existing)
            assert store.load(profile_name)[0].target=="相同名称更新"
            assert window.rules==baseline and image_plan()==old_image
            exported["name"] = imported
            exported["rules"][0]["target"] = new_target
            import_path.write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
            with patch.object(QFileDialog, "getOpenFileName",
                              return_value=(str(import_path), "JSON")):
                click(dialog, "importProfile")
            assert dialog.names.currentText() == imported
            assert store.load(imported)[0].target == new_target
            assert window.rules == baseline and image_plan() == old_image
            assert snapshot() == original and not journal.batches()
            click(dialog, "useProfile")

        in_modal(lambda: click(window, "profileButton"), ProfileDialog, exercise_profiles)
        wait_for(lambda: window.job is None and len(window.model.plans) == 7)
        assert window.rules[0].target == new_target
        assert image_plan().target == window.root / new_target / old_image.file.path.name
        assert snapshot() == original and not journal.batches()
        assert not window.apply_button.isEnabled()
        print("规则方案主窗口入口：保存、导出、导入、显式使用通过；仅预览目标改变。", flush=True)
    finally:
        window.close()


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--window":
        verify(Path(os.environ["ARCHIVE_DESK_EXTRAS_SCRATCH"]))
    else:
        with tempfile.TemporaryDirectory(prefix="ArchiveDesk-extras-ui-") as temp:
            env = dict(os.environ, QT_QPA_PLATFORM="windows", ARCHIVE_DESK_EXTRAS_SCRATCH=temp)
            subprocess.run([sys.executable, __file__, "--window"], cwd=PROJECT, env=env,
                           check=True, timeout=65)
