"""Two real Windows application processes exercise portable backup, restore and verification."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import traceback

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))


def contents(folder):
    return {path.relative_to(folder).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


def directories(folder):
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_dir())


def run_phase(phase,work):
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QLineEdit, QMessageBox, QPushButton, QTableView

    import main as desktop
    from archive_desk.backup_format import list_versions, object_path
    from desktop_v2_rules import click,modal,wait

    def verify(window):
        try:
            assert QApplication.instance().platformName()=="windows"
            wait(lambda:window.windowHandle().isExposed() and window.job is None)
            click(window,"backupButton")
            page = window.backups
            recovery = page.recovery

            def settle():
                wait(lambda:window.job is None)

            def press(name):
                click(window,name)

            def answer(box,role):
                assert isinstance(box,QMessageBox)
                btn = next(b for b in box.buttons() if box.buttonRole(b)==role)
                QTest.mouseClick(btn,Qt.MouseButton.LeftButton)

            def confirm(name,accepted=True):
                role = QMessageBox.ButtonRole.AcceptRole if accepted else QMessageBox.ButtonRole.RejectRole
                modal(lambda:press(name),lambda box:answer(box,role))
                settle()

            def pick(name,directory):
                with patch.object(QFileDialog,"getExistingDirectory",return_value=str(directory)):
                    press(name)
                settle()

            def toggle(index):
                recovery.tree.setCurrentIndex(index)
                recovery.tree.scrollTo(index)
                QTest.keyClick(recovery.tree,Qt.Key.Key_Space)
                assert index.data(Qt.ItemDataRole.CheckStateRole)==Qt.CheckState.Checked

            meta_path = work/"expected.json"
            if phase=="write":
                press("backupDemoButton")
                settle()
                registered = page.source()
                assert registered and not registered["indexed_at"]
                source,repo = Path(registered["path"]),page.target
                assert source.resolve().is_relative_to(work.resolve()) and repo.resolve().is_relative_to(work.resolve())
                old,d1 = contents(source),directories(source)
                assert not list(repo.iterdir())
                press("prepareBackup")
                settle()
                assert page.plan.count==len(old)
                assert page.plan.size==sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
                assert not list(repo.iterdir())
                confirm("executeBackup",False)
                assert not list(repo.iterdir())
                confirm("executeBackup")
                one, = list_versions(repo)
                with patch.object(QInputDialog,"getText",return_value=("演示模板",True)):
                    press("saveBackupPreset")
                with patch.object(QInputDialog,"getItem",return_value=("演示模板",True)):
                    press("loadBackupPreset")
                assert page.target==repo and page.source()["id"]==registered["id"]
                report = work/"report.csv"
                with patch.object(QFileDialog,"getSaveFileName",return_value=(str(report),"CSV")):
                    press("exportBackupReport")
                settle()
                assert "读取字节" in report.read_text(encoding="utf-8-sig")
                assert one["status"]=="complete" and one["files"]==len(old),window.notice.text()
                assert contents(source)==old
                (source/"交接"/"项目说明.txt").write_text("第二版，保留旧版内容。",encoding="utf-8")
                (source/"新增样例.txt").write_bytes(b"new")
                newer = contents(source)
                press("prepareBackup")
                settle()
                confirm("executeBackup")
                two,older = list_versions(repo)
                assert older["id"]==one["id"] and two["files"]==len(newer) and two["status"]=="complete"
                assert "新内容对象 2" in page.stats.text()
                # Start a real additional backup, then cancel it from the visible progress bar.
                extra = source/"取消测试.bin"
                with extra.open("wb") as stream:
                    block = bytes(range(256))*4096
                    for _ in range(48):
                        stream.write(block)
                press("prepareBackup")
                settle()
                modal(lambda:press("executeBackup"),lambda box:answer(box,QMessageBox.ButtonRole.AcceptRole))
                wait(lambda:window.job is not None and window.stop_button.isVisible() and
                     window.progress_bar.maximum()>0)
                press("stopOperationButton")
                settle()
                assert len(list_versions(repo))==2 and "已取消" in window.notice.text()
                assert extra.stat().st_size==48*1024*1024
                meta_path.write_text(json.dumps(dict(source=str(source),repo=str(repo),old=old,dirs=d1,
                    latest=newer,old_id=one["id"],new_id=two["id"]),ensure_ascii=False),encoding="utf-8")
                print(f"未索引临时来源经预检及确认发布两版；第一版 {len(old)} 个文件，第二版新增 2 个内容对象。第三次在进度条出现后点取消，未发布新版本。",flush=True)
            else:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                assert not page.sources and not window.journal.batches()
                assert not Path(meta["source"]).exists()
                portable = work/"portable"
                pick("importBackupRepo",portable)
                assert recovery.version.count()==2 and recovery.repo==portable
                press("searchBackup")
                dialog = next(d for d in recovery.findChildren(QDialog) if d.windowTitle()=="搜索备份版本")
                dialog.findChildren(QLineEdit)[0].setText("项目说明")
                QTest.mouseClick(dialog.findChild(QPushButton,"runBackupSearch"),Qt.MouseButton.LeftButton)
                settle()
                search = dialog.findChild(QTableView,"backupSearchResults")
                assert search.model().rowCount()==2
                search.doubleClicked.emit(search.model().index(1,1))
                settle()
                assert recovery.version.currentData()==meta["old_id"]
                assert recovery.tree_model.selected_paths()=={"交接/项目说明.txt"}
                recovery.version.setCurrentIndex(recovery.version.findData(meta["old_id"]))
                settle()
                root = recovery.tree_model.index(0,0)
                toggle(root)
                assert recovery.tree_model.selected_paths() is None
                whole = work/"恢复旧版"
                whole.mkdir()
                pick("chooseRestoreTarget",whole)
                press("prepareRestore")
                settle()
                assert not list(whole.iterdir()) and len(recovery.plan.entries)>=len(meta["old"])
                plan_csv = work/"restore.csv"
                with patch.object(QFileDialog,"getSaveFileName",return_value=(str(plan_csv),"CSV")):
                    press("exportRestorePlan")
                settle()
                assert "项目说明" in plan_csv.read_text(encoding="utf-8-sig")
                confirm("executeRestore",False)
                assert not list(whole.iterdir())
                confirm("executeRestore")
                assert contents(whole)==meta["old"]
                assert directories(whole)==meta["dirs"]
                assert "失败 0" in recovery.summary.text()
                print("空白配置导入复制的仓库，原源已移开。整个旧版本逐文件 SHA-256 和全部目录（含空目录）一致。",flush=True)

                recovery.version.setCurrentIndex(recovery.version.findData(meta["new_id"]))
                settle()
                root = recovery.tree_model.index(0,0)
                indexes = [recovery.tree_model.index(i,0,root) for i in range(recovery.tree_model.rowCount(root))]
                child = next(index for index in indexes if index.data()=="交接")
                toggle(child)
                assert recovery.tree_model.selected_paths()=={"交接"}
                partial = work/"部门找回"
                (partial/"交接").mkdir(parents=True)
                collision = partial/"交接"/"项目说明.txt"
                collision.write_bytes(b"keep this file")
                pick("chooseRestoreTarget",partial)
                press("prepareRestore")
                settle()
                assert any(e.action=="skip" and e.path=="交接/项目说明.txt" for e in recovery.plan.entries)
                recovery.conflicts.setCurrentIndex(recovery.conflicts.findData("rename"))
                assert not recovery.apply.isEnabled()
                press("prepareRestore")
                settle()
                named = next(e for e in recovery.plan.entries if e.action=="rename")
                confirm("executeRestore")
                assert named.target!=collision and collision.read_bytes()==b"keep this file"
                assert hashlib.sha256(named.target.read_bytes()).hexdigest()==meta["latest"][named.path]
                assert not (partial/"参考").exists() and not (partial/"样例.bin").exists()
                print("新版只勾选一个目录，跳过和编号另存预览均显示实际目标；执行后重名原件完好。",flush=True)

                press("verifyBackupFromTree")
                settle()
                assert not page.verify_rows.records and "已检查 2 个版本" in page.verify_summary.text()
                # Inject faults only in this generated, portable copy after successful recovery.
                assert portable.resolve().parent==work.resolve()
                missing = object_path(portable,meta["old"]["交接/清单.txt"])
                broken = object_path(portable,meta["old"]["样例.bin"])
                for path in (missing,broken):
                    assert path.resolve().is_relative_to(portable.resolve())
                missing.rename(work/"removed-object")
                broken.write_bytes(b"bad object")
                (portable/"versions"/("a"*32+".json")).write_bytes(b"bad manifest")
                press("verifyBackup")
                settle()
                report = page.verify_rows.records
                assert {"hash","missing","manifest"} <= {r["kind"] for r in report}
                assert {r["version"] for r in report if r["kind"]=="hash"}=={meta["old_id"],meta["new_id"]}
                assert all(r["path"]=="样例.bin" for r in report if r["kind"]=="hash")
                assert contents(whole)==meta["old"]
                print("仓库正常时验证通过；注入故障后显示对象缺失、哈希不符及清单损坏，并定位受影响的版本和路径。",flush=True)
            window.close()
            wait(lambda:not window.isVisible())
            QApplication.instance().exit(0)
        except BaseException:
            traceback.print_exc()
            QApplication.instance().exit(1)

    base_class = desktop.MainWindow
    class TestWindow(base_class):
        def show(self):
            super().show()
            QTimer.singleShot(300,lambda:verify(self))
    desktop.MainWindow = TestWindow
    return desktop.main()


if __name__=="__main__":
    if len(sys.argv)==3:
        sys.exit(run_phase(sys.argv[1],Path(sys.argv[2])))
    with TemporaryDirectory(prefix="archive-desk-backup-ui-") as temporary:
        work = Path(temporary)
        spool = work/"temp"
        spool.mkdir()
        for phase in ("write","recover"):
            if phase=="recover":
                meta = json.loads((work/"expected.json").read_text(encoding="utf-8"))
                source = Path(meta["source"]).resolve()
                repo = Path(meta["repo"]).resolve()
                assert source.is_relative_to(work.resolve()) and repo.is_relative_to(work.resolve())
                shutil.copytree(repo,work/"portable")
                source.rename(work/"source-offline")
            env = dict(os.environ,ARCHIVE_DESK_DATA_DIR=str(work/f"db-{phase}"),TEMP=str(spool),
                       TMP=str(spool),QT_QPA_PLATFORM="windows",PYTHONUTF8="1")
            subprocess.run([sys.executable,__file__,phase,str(work)],cwd=PROJECT,env=env,check=True,timeout=100)
