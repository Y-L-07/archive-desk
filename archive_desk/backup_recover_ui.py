"""版本浏览、恢复预览与无覆盖恢复的前台控件。"""
from collections import Counter
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QMenu, QSplitter, QTabWidget, QTreeView,
                               QVBoxLayout, QWidget)
from shiboken6 import isValid

from .backup_format import read_manifest
from .backup_restore import prepare_restore, restore
from .backup_tools import (compare_versions, copy_repository, export_restore_plan,
                           retired_versions, search_version, set_retired)
from .backup_tree import VersionTree, build_tree
from .catalog_widgets import button, local_time, records_table


ACTIONS = {"restore":"恢复文件", "rename":"编号另存", "create_dir":"创建目录",
           "keep_dir":"沿用目录", "skip":"跳过"}
STATUS = {"complete":"完整", "incomplete":"部分成功", "damaged":"清单损坏"}


def text_label(value=""):
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def folder_dialog(parent, title):
    name = QFileDialog.getExistingDirectory(parent,title,"",QFileDialog.Option.ShowDirsOnly |
                                            QFileDialog.Option.DontResolveSymlinks)
    return Path(name) if name else None


def date_text(when):
    try:
        return local_time(when) if when else "日期未知"
    except (ValueError, OSError, OverflowError):
        return str(when)


class RecoveryTab(QWidget):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.repo = self.destination = self.plan = None
        self.versions = []
        self.version_issues = []
        self.pending_search = None
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.repo_label = text_label("导入已有仓库后，可按日期浏览版本。")
        layout.addWidget(self.repo_label)
        version_row = QHBoxLayout()
        version_row.addWidget(QLabel("备份版本"))
        self.version = QComboBox()
        self.version.setObjectName("backupVersions")
        self.version.currentIndexChanged.connect(self.load_version)
        version_row.addWidget(self.version,1)
        self.verify_button = button("验证此仓库","verifyBackupFromTree",page.verify_imported)
        version_row.addWidget(self.verify_button)
        self.search_button = button("搜索备份…","searchBackup",self.search_files)
        version_row.addWidget(self.search_button)
        self.manage = button("仓库操作","manageBackup",lambda:None)
        menu = QMenu(self.manage)
        menu.addAction("比较两个版本…",self.compare)
        menu.addAction("删除当前版本（可找回）…",self.retire)
        menu.addAction("找回已删除版本…",self.reinstate)
        menu.addAction("复制仓库…",self.copy)
        self.manage.setMenu(menu)
        version_row.addWidget(self.manage)
        layout.addLayout(version_row)
        self.version_info = text_label()
        self.version_info.setObjectName("muted")
        layout.addWidget(self.version_info)
        split = QSplitter()
        self.tree = QTreeView()
        self.tree.setObjectName("backupTree")
        self.tree.setUniformRowHeights(True)
        self.tree_model = VersionTree(self.tree)
        self.tree.setModel(self.tree_model)
        self.tree.setColumnWidth(0,234)
        self.tree_model.selection_changed.connect(self.invalidate)
        split.addWidget(self.tree)
        work = QWidget()
        right = QVBoxLayout(work)
        right.setContentsMargins(12,0,0,0)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("恢复到"))
        self.dest_label = text_label("请另选恢复目录")
        target_row.addWidget(self.dest_label,1)
        target_row.addWidget(button("选择目录…","chooseRestoreTarget",self.choose_target))
        right.addLayout(target_row)
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("同名冲突"))
        self.conflicts = QComboBox()
        self.conflicts.setObjectName("restoreConflict")
        self.conflicts.addItem("只恢复缺失文件（跳过已有名称）","skip")
        self.conflicts.addItem("编号另存","rename")
        self.conflicts.currentIndexChanged.connect(self.invalidate)
        action_row.addWidget(self.conflicts)
        action_row.addStretch()
        self.review = button("预览恢复计划","prepareRestore",self.prepare)
        action_row.addWidget(self.review)
        self.apply = button("执行恢复","executeRestore",self.execute)
        self.apply.setProperty("primary",True)
        action_row.addWidget(self.apply)
        right.addLayout(action_row)
        self.export_button = button("导出恢复清单…","exportRestorePlan",self.export)
        right.addWidget(self.export_button)
        self.summary = text_label()
        self.summary.setObjectName("restoreSummary")
        right.addWidget(self.summary)
        self.results = QTabWidget()
        self.preview_table,self.rows = records_table(self.results,[
            ("动作",lambda e:ACTIONS[e.action],88),("版本内路径",lambda e:e.path,None),
            ("实际目标",lambda e:e.target,None),("说明",lambda e:e.reason or "—",None)],"restorePreviewTable")
        self.results.addTab(self.preview_table,"恢复预览")
        self.issues_table,self.issues = records_table(self.results,[
            ("版本内路径",lambda e:e["path"] or "整个版本",None),
            ("原因",lambda e:e["reason"],None)],"restoreIssuesTable")
        self.results.addTab(self.issues_table,"问题明细")
        right.addWidget(self.results,1)
        split.addWidget(work)
        split.setSizes([320,780])
        layout.addWidget(split,1)
        self.invalidate()

    def set_repository(self, repo, versions):
        self.pending_search = None
        self.repo = repo
        self.repo_label.setText(f"仓库　{repo}")
        self.versions = versions
        self.version.blockSignals(True)
        self.version.clear()
        for row in versions:
            date = date_text(row["created_at"])
            self.version.addItem(f"{date}　{row['source_name']}　·　{STATUS[row['status']]}　·　{row['id'][:8]}",row["id"])
            self.version.setItemData(self.version.count()-1,row["id"],Qt.ItemDataRole.ToolTipRole)
        self.version.blockSignals(False)
        self.load_version()

    def load_version(self):
        self.version_issues = []
        self.tree_model.replace(None)
        self.issues.replace([])
        self.results.setCurrentIndex(0)
        idx = self.version.currentIndex()
        if idx<0 or self.repo is None:
            self.version_info.setText("仓库中尚无已发布版本。")
            return
        version = self.versions[idx]
        version_id,repo = version["id"],self.repo
        self.version_info.setText(f"版本 {version_id}　·　{version['files']:,} 个文件　·　{version['file_bytes']:,} 字节")
        if version["status"]=="damaged":
            self.issues.replace([{"path":"", "reason":version.get("error","清单损坏")}])
            self.results.setCurrentIndex(1)
            self.summary.setText("该版本清单损坏，请查看问题明细或验证仓库。")
            return
        def read(cancel,progress):
            manifest = read_manifest(repo,version_id)
            return build_tree(manifest,cancel),manifest["issues"]
        def ready(result):
            root,self.version_issues = result
            self.tree_model.replace(root)
            self.tree.expand(self.tree_model.index(0,0))
            if self.pending_search:
                version, path = self.pending_search
                self.pending_search = None
                if version == version_id:
                    index = self.tree_model.select_file(path)
                    if index is not None:
                        parent = index.parent()
                        while parent.isValid():
                            self.tree.expand(parent)
                            parent = parent.parent()
                        self.tree.setCurrentIndex(index)
                        self.tree.scrollTo(index)
            self.issues.replace(self.version_issues)
            if self.version_issues:
                self.version_info.setText(self.version_info.text()+f"　·　备份时遗漏 {len(self.version_issues):,} 项")
        self.page.task("正在读取版本目录树…",read,ready)

    def choose_target(self):
        dest = folder_dialog(self,"选择单独的恢复目录")
        if dest:
            self.destination = dest
            self.dest_label.setText(str(dest))
            self.invalidate()

    def invalidate(self,*_):
        self.plan = None
        self.rows.replace([])
        self.apply.setEnabled(False)
        self.export_button.setEnabled(False)
        self.search_button.setEnabled(any(v["status"] != "damaged" for v in self.versions))
        self.manage.setEnabled(self.repo is not None)
        self.verify_button.setEnabled(self.repo is not None)
        paths = self.tree_model.selected_paths()
        chosen = paths is None or bool(paths)
        self.review.setEnabled(chosen and self.destination is not None)
        caption = "整个版本" if paths is None else f"{len(paths):,} 个文件或目录"
        self.summary.setText(f"已选择 {caption}。请预览目标位置及冲突。" if chosen else "请在目录树中勾选要恢复的范围。")
        self.issues.replace(self.version_issues)

    def prepare(self):
        repo, vid, dest = self.repo,self.version.currentData(),self.destination
        paths, mode = self.tree_model.selected_paths(),self.conflicts.currentData()
        def ready(plan):
            self.plan = plan
            self.rows.replace(plan.entries)
            self.export_button.setEnabled(True)
            self.results.setCurrentIndex(0)
            counts = Counter(e.action for e in plan.entries)
            byte_total = sum(e.size for e in plan.entries if e.action in ("restore","rename"))
            self.summary.setText(f"恢复文件 {counts['restore']:,}　·　编号另存 {counts['rename']:,}　·　"
                                 f"创建目录 {counts['create_dir']:,}　·　跳过 {counts['skip']:,}　·　共 {byte_total:,} 字节")
            self.apply.setEnabled(any(e.action in ("create_dir","restore","rename") for e in plan.entries))
        self.page.task("正在计算恢复预览…",lambda cancel,progress:prepare_restore(
            repo,vid,dest,paths,conflict=mode,cancel=cancel,progress=progress),ready)

    def export(self):
        plan = self.plan
        if not plan:
            return
        target, _ = QFileDialog.getSaveFileName(self,"导出恢复清单","恢复清单.csv","CSV (*.csv)")
        if target:
            self.page.task("正在导出恢复清单…",lambda *_:export_restore_plan(Path(target),plan),
                           lambda _:self.page.window._note("恢复清单已保存。"))

    def search_files(self):
        opened_repo = self.repo
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("搜索备份版本")
        dialog.resize(800,480)
        layout = QVBoxLayout(dialog)
        row = QHBoxLayout()
        fields = []
        for placeholder in ("文件名或路径","扩展名，如 pdf","修改日期从 YYYY-MM-DD","修改日期到 YYYY-MM-DD"):
            field = QLineEdit()
            field.setPlaceholderText(placeholder)
            row.addWidget(field)
            fields.append(field)
        layout.addLayout(row)
        table, model = records_table(dialog, [("版本",lambda r:r[0],155),
                                               ("文件路径",lambda r:r[2],None)],"backupSearchResults")
        layout.addWidget(table)
        status = text_label()
        layout.addWidget(status)
        def run():
            if self.repo != opened_repo:
                status.setText("仓库已切换，请重新打开搜索。")
                return
            criteria = [f.text().strip() for f in fields]
            repo = self.repo
            versions = [v for v in self.versions if v["status"] != "damaged"]
            model.replace([])
            def work(cancel, progress):
                from .core import check_cancel
                matches = []
                for index, version in enumerate(versions):
                    check_cancel(cancel)
                    progress(index,len(versions),version["id"])
                    manifest = read_manifest(repo,version["id"])
                    for file in search_version(manifest,*criteria):
                        matches.append((date_text(version["created_at"]),version["id"],file["path"]))
                        if len(matches) == 5000:
                            return matches
                return matches
            def ready(matches):
                if isValid(dialog) and dialog.isVisible():
                    model.replace(matches)
                    status.setText(f"找到 {len(matches):,} 项" if len(matches) < 5000 else "显示前 5,000 项，请缩小搜索范围。")
            def failed(stopped,error):
                if isValid(dialog) and dialog.isVisible():
                    status.setText("搜索已取消。" if stopped else f"搜索失败：{error}")
            status.setText("正在搜索…")
            # A modal dialog would prevent the main window's cancel button from acting.
            self.page.task("正在搜索备份…",work,ready,failed=failed)
        def locate(index):
            if self.repo != opened_repo or self.page.window.job is not None:
                return
            _, vid, path = model.records[index.row()]
            combo_index = self.version.findData(vid)
            if combo_index < 0:
                status.setText("该版本已从仓库列表移出，请重新搜索。")
                return
            self.pending_search = (vid,path)
            dialog.close()
            if combo_index == self.version.currentIndex():
                self.load_version()
            else:
                self.version.setCurrentIndex(combo_index)
        table.doubleClicked.connect(locate)
        row.addWidget(button("搜索","runBackupSearch",run))
        layout.addWidget(text_label("双击结果以选择要恢复的文件。"))
        dialog.show()

    def compare(self):
        current = self.version.currentIndex()
        candidates = [v for i,v in enumerate(self.versions) if i != current and v["status"] != "damaged"]
        if current < 0 or self.versions[current]["status"] == "damaged" or not candidates:
            self.page.window._note("比较需要两个可读取的备份版本。")
            return
        versions = {f"{date_text(v['created_at'])} · {v['id'][:8]}":v for v in candidates}
        choice, ok = QInputDialog.getItem(self,"比较备份版本","与当前版本比较",list(versions),editable=False)
        if not ok:
            return
        first, second = sorted((self.versions[current],versions[choice]),
                               key=lambda v:datetime.fromisoformat(v["created_at"]))
        repo = self.repo
        def ready(rows):
            dialog = QDialog(self)
            dialog.setWindowTitle("备份版本差异")
            dialog.resize(720,440)
            box = QVBoxLayout(dialog)
            box.addWidget(text_label(f"{date_text(first['created_at'])} → {date_text(second['created_at'])} · {len(rows):,} 项变化"))
            table, model = records_table(dialog,[("变化",lambda r:r[0],130),("路径",lambda r:r[1],None)],"backupVersionDiff")
            model.replace(rows)
            box.addWidget(table)
            dialog.exec()
        self.page.task("正在比较版本…",lambda *_:list(compare_versions(
            read_manifest(repo,first["id"]),read_manifest(repo,second["id"]))),ready)

    def retire(self):
        vid, repo = self.version.currentData(), self.repo
        if vid and self.page.window.confirm("删除备份版本",f"版本 {vid}\n版本将移出列表，可从仓库操作中找回。","删除此版本"):
            self.page.task("正在移出版本…",lambda *_:set_retired(repo,vid,retired=True),
                           lambda _:self.page.open_repository(repo))

    def reinstate(self):
        repo = self.repo
        def ready(versions):
            if versions:
                vid, ok = QInputDialog.getItem(self,"找回已删除的版本","版本编号",versions,editable=False)
                if ok:
                    self.page.task("正在恢复版本…",lambda *_:set_retired(repo,vid,retired=False),
                                   lambda _:self.page.open_repository(repo))
            else:
                self.page.window._note("仓库没有已删除的版本。")
        self.page.task("正在读取已删除的版本…",lambda *_:retired_versions(repo),ready)

    def copy(self):
        target = folder_dialog(self,"选择存放仓库副本的空目录")
        repo = self.repo
        if target and self.page.window.confirm("复制备份仓库",f"{repo}\n→ {target}","确认复制"):
            self.page.task("正在复制并校验仓库…",
                           lambda cancel,progress:copy_repository(repo,target,cancel=cancel,progress=progress),
                           lambda result:self.page.window._note(f"已复制 {result[0]} 个版本、{result[1]} 个内容对象。"))

    def execute(self):
        plan = self.plan
        if plan is None:
            return
        msg = (f"版本：{plan.version_id}\n仓库：{plan.repo}\n恢复到：{plan.destination}\n\n"
               f"{self.summary.text()}\n\n将按预览写入并校验文件。")
        if not self.page.window.confirm("确认执行恢复",msg,"确认恢复"):
            return
        def finished(result):
            self.plan = None
            self.apply.setEnabled(False)
            self.export_button.setEnabled(False)
            prefix = "已取消，保留完成项" if result["cancelled"] else "恢复结束"
            self.summary.setText(f"{prefix}　·　已恢复文件 {result['restored']:,}　·　创建目录 {result['created_dirs']:,}　·　"
                                 f"跳过 {result['skipped']:,}　·　失败 {result['failed']:,}")
            self.issues.replace(result["issues"])
            if result["issues"]:
                self.results.setCurrentIndex(1)
        self.page.task("正在恢复并校验文件…",lambda cancel,progress:restore(plan,cancel=cancel,progress=progress),finished)
