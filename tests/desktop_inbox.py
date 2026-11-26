"""两个真实 Windows 应用进程验证收件箱的稳定观察、补扫和只读预览。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import os
import subprocess
import sys
import time
import traceback

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))


def run_phase(phase, work):
    from PySide6.QtCore import QPoint, QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QFileDialog, QPushButton

    import main as desktop

    def wait_until(check, timeout=35):
        end = time.monotonic() + timeout
        while not check():
            if time.monotonic() >= end:
                raise AssertionError("收件箱窗口等待超时")
            QApplication.processEvents()
            time.sleep(.015)

    def click(window, name):
        button = window.findChild(QPushButton, name)
        assert button and button.isVisible() and button.isEnabled(), name
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)

    root = work / "来件"
    first = root / "分段写入.txt"
    offline = root / "离线送达.txt"

    def verify(window):
        try:
            app = QApplication.instance()
            assert app.platformName() == "windows"
            wait_until(lambda: window.windowHandle().isExposed() and window.job is None)
            page = window.inbox
            click(window, "inboxButton")
            if phase == "start":
                assert page.root is None
                with patch.object(QFileDialog, "getExistingDirectory", return_value=str(root)):
                    click(window, "inboxChooseButton")
                first.write_bytes(b"first")
                wait_until(lambda: page.pending == 1)
                assert not page.model.files
                with first.open("ab") as stream:
                    stream.write(b"-final")
                wait_until(lambda: page.model.rowCount() == 1)
                assert page.model.files[0].size == len(b"first-final")
                assert not page.model.selected and not page.load.isEnabled()
                assert not window.model.plans and not window.journal.batches()
                click(window, "inboxSelectAll")
                click(window, "inboxPreviewButton")
                wait_until(lambda: window.job is None and len(window.model.plans) == 1)
                assert window.pages.currentIndex() == 0
                assert window.model.plans[0].file.path == first
                assert not window.model.checked and not window.apply_button.isEnabled()
                assert not window.journal.batches() and first.read_bytes() == b"first-final"
                assert list(root.iterdir()) == [first]
                print("窗口内启用监视，分段写入稳定后进入候选。手选载入预览，执行勾选为零。", flush=True)
            else:
                assert page.root == root
                wait_until(lambda: {f.path for f in page.model.files} == {first, offline})
                assert not window.model.plans and not window.journal.batches()
                assert first.read_bytes() == b"first-final" and offline.read_bytes() == b"offline"
                row = next(i for i,f in enumerate(page.model.files) if f.path == first)
                rect = page.table.visualRect(page.model.index(row,0))
                QTest.mouseClick(page.table.viewport(),Qt.MouseButton.LeftButton,
                                 pos=QPoint(rect.x()+12,rect.center().y()))
                click(window,"inboxIgnoreButton")
                assert {f.path for f in page.model.files} == {offline}
                assert first.read_bytes() == b"first-final"
                click(window, "inboxDisableButton")
                wait_until(lambda: page.root is None and page.job is None)
                assert not page.model.files and not page.timer.isActive()
                assert page.store.root() is None
                print("新进程保留仅预览的候选，并补扫离线来件。显式忽略仅移除该版本候选，停用后无监视定时器。", flush=True)
            window.close()
            wait_until(lambda: not window.isVisible())
            app.exit(0)
        except BaseException:
            traceback.print_exc()
            QApplication.instance().exit(1)

    base = desktop.MainWindow
    class TestWindow(base):
        def show(self):
            super().show()
            QTimer.singleShot(300,lambda: verify(self))
    desktop.MainWindow = TestWindow
    return desktop.main()


if __name__ == "__main__":
    if len(sys.argv) == 3:
        sys.exit(run_phase(sys.argv[1],Path(sys.argv[2])))
    with TemporaryDirectory(prefix="archive-desk-inbox-ui-") as temp:
        work = Path(temp)
        root = work / "来件"
        root.mkdir()
        env = dict(os.environ, ARCHIVE_DESK_DATA_DIR=str(work / "data"),
                   QT_QPA_PLATFORM="windows", PYTHONUTF8="1")
        for phase in ("start","restart"):
            if phase == "restart":
                (root / "离线送达.txt").write_bytes(b"offline")
            subprocess.run([sys.executable,__file__,phase,str(work)], cwd=PROJECT,
                           env=env,check=True,timeout=70)
