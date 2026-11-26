"""真实 Windows 窗口的跨卷崩溃恢复验收。仅使用本次创建的临时目录。"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
NAME = "跨卷恢复样例.pdf"
STAGES = ("copy_chunk", "committed")
TAG = "恢复验收"
NOTE = "移动前添加的跨卷备注"


def indexed_at(search, entry_id, root_id, relpath, sha):
    # Each side is indexed before the move. No refresh is performed later:
    # only a *confirmed* journal transition may change the indexed location.
    result = search.search()
    assert result["total"] == 1, result
    row, = result["rows"]
    assert (row["id"], row["root_id"], row["relpath"], row["digest"]) == (
        entry_id, root_id, relpath, sha), row
    assert row["tags"] == [TAG] and row["note"] == NOTE and row["favorite"], row


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def crash_state(item, stage, source, target, sha):
    assert Path(item["source"]) == source and Path(item["target"]) == target
    assert item["status"] in ("applying", "resumable")
    assert item["phase"] == ("copying" if stage == "copy_chunk" else "committed")
    transfer = json.loads(item["transfer"])
    temp = Path(transfer["temp"])
    assert temp.parent == target.parent and temp != target
    assert digest(source) == sha
    if stage == "copy_chunk":
        assert not target.exists()
        assert 0 < temp.stat().st_size < source.stat().st_size
        with temp.open("rb") as part, source.open("rb") as complete:
            data = part.read()
            assert data == complete.read(len(data))
    else:
        assert digest(target) == sha and not temp.exists()
    return temp


def drive_window(action, stage, scratch, destination, sha, source_id, target_id, entry_id):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QAbstractButton, QApplication, QFileDialog, QMessageBox

    import main as desktop
    from archive_desk import crossvolume
    from archive_desk.catalog_search import CatalogSearch
    from archive_desk.window import MainWindow

    source_root = scratch / "资料"
    source = source_root / NAME
    target = destination / "文档" / NAME
    callback_errors = []

    def wait_for(test):
        end = time.monotonic() + 30
        while time.monotonic() < end:
            if callback_errors:
                raise callback_errors[0]
            result = test()
            if result:
                return result
            QTest.qWait(30)
        raise AssertionError(f"窗口状态超时：{test}")

    def exercise():
        try:
            app = QApplication.instance()
            window = next(w for w in app.topLevelWidgets() if isinstance(w, MainWindow))
            journal = window.journal
            search = CatalogSearch(journal.path)
            assert app.platformName() == "windows" and window.isVisible()
            wait_for(lambda: window.windowHandle().isExposed())

            def click(name):
                control = window.findChild(QAbstractButton, name)
                assert control is not None, name
                wait_for(lambda: control.isVisible() and control.isEnabled())
                QTest.mouseClick(control, Qt.MouseButton.LeftButton)

            def confirm_click(name, expected_label):
                # Preflight may run in a worker before the dialog appears.
                deadline = time.monotonic() + 25
                def answer():
                    dialog = app.activeModalWidget()
                    if isinstance(dialog, QMessageBox):
                        ok = next((b for b in dialog.buttons() if b.text() == expected_label
                                   and dialog.buttonRole(b) == QMessageBox.ButtonRole.AcceptRole), None)
                        if ok is None:
                            callback_errors.append(AssertionError(f"意外的确认框：{dialog.text()}"))
                            dialog.reject()
                            return
                        QTest.mouseClick(ok, Qt.MouseButton.LeftButton)
                        return
                    if time.monotonic() > deadline:
                        callback_errors.append(AssertionError(f"没有显示确认按钮：{expected_label}"))
                        return
                    QTimer.singleShot(30, answer)
                QTimer.singleShot(30, answer)
                click(name)

            if action == "interrupt":
                assert not journal.batches()
                wait_for(lambda: window.job is None)
                with patch.object(QFileDialog, "getExistingDirectory", return_value=str(source_root)):
                    click("openButton")
                wait_for(lambda: window.job is None and len(window.model.plans) == 1)
                with patch.object(QFileDialog, "getExistingDirectory", return_value=str(destination)):
                    click("targetButton")
                wait_for(lambda: window.job is None and len(window.model.plans) == 1)
                plan, = window.model.plans
                assert plan.target == target and plan.file.path == source and plan.status == "ready"
                assert digest(source) == sha and not target.exists()
                indexed_at(search, entry_id, source_id, NAME, sha)
                click("selectVisibleButton")

                def cut_power(at, item_id):
                    if at == stage:
                        os._exit(73)

                with patch.object(crossvolume, "checkpoint", cut_power):
                    confirm_click("applyButton", "确认移动")
                    wait_for(lambda: False)
            else:
                wait_for(lambda: window.job is None and journal.batches())
                click("historyButton")
                batch_id = journal.batches()[0]["id"]

                def item():
                    row, = journal.items(batch_id)
                    return row

                wait_for(lambda: item()["status"] == "resumable")
                temporary = crash_state(item(), stage, source, target, sha)
                indexed_at(search, entry_id, source_id, NAME, sha)
                # Recovery on startup only classifies. The click explicitly allows
                # continuing this task, and the source survives until then.
                confirm_click("resumeButton", "确认继续")
                wait_for(lambda: item()["status"] == "moved" and window.job is None)
                assert not source.exists() and digest(target) == sha
                assert not temporary.exists()
                indexed_at(search, entry_id, target_id, target.relative_to(destination).as_posix(), sha)
                confirm_click("undoButton", "确认撤销")
                wait_for(lambda: item()["status"] == "undone" and window.job is None)
                assert digest(source) == sha and not target.exists()
                assert not temporary.exists()
                indexed_at(search, entry_id, source_id, NAME, sha)
                print(f"{stage}：启动探测保持原索引；显式继续及撤销后，索引、标签、备注、收藏、SHA-256 一致。",
                      flush=True)
                window.close()
                app.exit(0)
        except BaseException:
            traceback.print_exc()
            QApplication.instance().exit(1)

    class TestWindow(MainWindow):
        def show(self):
            super().show()
            QTimer.singleShot(400, exercise)

    with patch.object(desktop, "MainWindow", TestWindow):
        return desktop.main()


def verify_both_volumes(target_parent):
    from archive_desk.catalog import Catalog
    from archive_desk.catalog_search import CatalogSearch
    from archive_desk.journal import Journal

    for stage in STAGES:
        with (tempfile.TemporaryDirectory(prefix="archive-v2-source-") as left,
              tempfile.TemporaryDirectory(prefix=".archive-v2-target-", dir=target_parent) as right):
            scratch = Path(left)
            destination = Path(right)
            source_root = scratch / "资料"
            source_root.mkdir()
            assert source_root.stat().st_dev != destination.stat().st_dev, "本验收需要两个实际不同的卷"
            source = source_root / NAME
            target = destination / "文档" / NAME
            source.write_bytes(bytes(range(256)) * 12289)
            sha = digest(source)
            db_path = scratch / "state/history.sqlite3"
            catalog = Catalog(db_path)
            source_id = catalog.add_root("临时来源库", source_root)
            target_id = catalog.add_root("临时目标库", destination)
            for root_id in (source_id, target_id):
                assert catalog.refresh(root_id, hash_files=True)["published_state"] == "complete"
            search = CatalogSearch(db_path)
            entry, = search.search()["rows"]
            entry_id = entry["id"]
            search.add_tags([entry_id], [TAG])
            search.annotate(entry_id, note=NOTE, favorite=True)
            indexed_at(search, entry_id, source_id, NAME, sha)
            env = dict(os.environ, ARCHIVE_DESK_DATA_DIR=str(scratch / "state"), QT_QPA_PLATFORM="windows")
            def child(action):
                return subprocess.run([sys.executable, __file__, "--window", action, stage,
                                       str(scratch), str(destination), sha,
                                       str(source_id), str(target_id), str(entry_id)], env=env, cwd=PROJECT,
                                      timeout=110).returncode

            assert child("interrupt") == 73, f"未到达预期中断点：{stage}"
            journal = Journal(scratch / "state/history.sqlite3")
            batch, = journal.batches()
            item, = journal.items(batch["id"])
            crash_state(item, stage, source, target, sha)
            indexed_at(search, entry_id, source_id, NAME, sha)
            assert child("recover") == 0, "窗口恢复验收失败"
            assert digest(source) == sha and not target.exists()
            indexed_at(search, entry_id, source_id, NAME, sha)
            assert all(not p.is_file() for p in destination.rglob("*"))
            assert [p for p in source_root.rglob("*") if p.is_file()] == [source]


if __name__ == "__main__":
    if sys.argv[1:2] == ["--window"]:
        _, _, action, stage, scratch, target, sha, source_id, target_id, entry_id = sys.argv
        sys.exit(drive_window(action, stage, Path(scratch), Path(target), sha,
                              int(source_id), int(target_id), int(entry_id)))
    verify_both_volumes(Path(sys.argv[1]).absolute() if len(sys.argv) == 2 else PROJECT)
