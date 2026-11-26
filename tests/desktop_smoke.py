"""在真实 Windows 窗口中跨两个进程验收。此脚本只操作新建临时目录。"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_window(phase, scratch):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QAbstractButton, QMessageBox

    import main as desktop
    from archive_desk.journal import Journal
    from archive_desk.window import MainWindow

    def wait_for(predicate, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = predicate()
            if result:
                return result
            QTest.qWait(40)
        raise AssertionError(f"等待窗口状态超时 {predicate}")

    def exercise():
        try:
            app = QApplication.instance()
            assert app.platformName() == "windows", app.platformName()
            window = next(w for w in app.topLevelWidgets() if isinstance(w, MainWindow))
            assert window.isVisible() and window.windowHandle().isExposed()
            journal = Journal(desktop.data_directory() / "history.sqlite3")
            manifest = scratch / "verification.json"

            def click(name):
                button = window.findChild(QAbstractButton, name)
                assert button is not None, name
                wait_for(lambda: button.isVisible() and button.isEnabled())
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)

            def answer_confirmation(accept):
                roles = ((QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole)
                         if accept else (QMessageBox.ButtonRole.RejectRole,))

                def answer():
                    dialog = app.activeModalWidget()
                    if isinstance(dialog, QMessageBox):
                        for button in dialog.buttons():
                            if dialog.buttonRole(button) in roles:
                                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                                return
                    QTimer.singleShot(60, answer)

                QTimer.singleShot(60, answer)

            if phase == "apply":
                assert not journal.batches()
                click("demoButton")
                wait_for(lambda: window.job is None and len(window.model.plans) == 7)
                assert window.counts == {"ready": 5, "skip": 1, "conflict": 1}
                assert not window.apply_button.isEnabled()
                click("selectVisibleButton")
                # 记录生成的演示数据，实际移动成功后再与原始摘要比较。
                from PySide6.QtWidgets import QTableView
                table = window.findChild(QTableView, "fileTable")
                assert table.model().rowCount() > 0
                initial = {
                    str(path): digest(path)
                    for path in (scratch / "temp").rglob("*") if path.is_file()
                }
                assert initial
                answer_confirmation(False)
                click("applyButton")
                assert not journal.batches()
                assert all(digest(Path(path)) == checksum for path, checksum in initial.items())
                answer_confirmation(True)
                click("applyButton")
                batch = wait_for(lambda: next((b for b in journal.batches()
                                               if b["moved"] == b["total"] == 5), None))
                entries = journal.items(batch["id"])
                assert all(row["status"] == "moved" for row in entries), entries
                for row in entries:
                    source = Path(row["source"])
                    target = Path(row["target"])
                    assert source.is_relative_to(scratch) and not source.exists()
                    assert target.is_relative_to(scratch) and digest(target) == initial[str(source)]
                manifest.write_text(json.dumps(initial), encoding="utf-8")
                print(f"可见桌面窗口完成创建、扫描、预览、勾选及移动 {len(entries)} 项", flush=True)
            else:
                initial = json.loads(manifest.read_text(encoding="utf-8"))
                batch = journal.batches()[0]
                click("historyButton")
                answer_confirmation(True)
                click("undoButton")
                wait_for(lambda: journal.batches()[0]["undone"] == batch["moved"])
                for row in journal.items(batch["id"]):
                    assert row["status"] == "undone" and not Path(row["target"]).exists()
                    assert digest(Path(row["source"])) == initial[row["source"]]
                print(f"新进程的历史窗口撤销 {batch['moved']} 项，逐项内容校验一致", flush=True)
            QTest.qWait(250)
            window.close()
            app.exit(0)
        except BaseException:
            traceback.print_exc()
            QApplication.instance().exit(1)

    class TestWindow(MainWindow):
        def show(self):
            super().show()
            QTimer.singleShot(500, exercise)

    desktop.MainWindow = TestWindow
    return desktop.main()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        sys.exit(run_window(sys.argv[1], Path(sys.argv[2])))
    with tempfile.TemporaryDirectory(prefix="archive-desk-window-") as folder:
        scratch = Path(folder)
        temp = scratch / "temp"
        temp.mkdir()
        env = dict(os.environ, TEMP=str(temp), TMP=str(temp),
                   ARCHIVE_DESK_DATA_DIR=str(scratch / "appdata"), QT_QPA_PLATFORM="windows")
        for phase in ("apply", "undo"):
            subprocess.run([sys.executable, __file__, phase, str(scratch)],
                           env=env, cwd=PROJECT, check=True, timeout=90)
