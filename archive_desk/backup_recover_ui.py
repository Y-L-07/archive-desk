"""版本浏览、恢复预览与无覆盖恢复的前台控件。"""
from collections import Counter
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QSplitter,
                               QTabWidget, QTreeView, QVBoxLayout, QWidget)

from .backup_format import read_manifest
from .backup_restore import prepare_restore, restore
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
        self.conflicts.addItem("跳过已有名称","skip")
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
            self.results.setCurrentIndex(0)
            counts = Counter(e.action for e in plan.entries)
            byte_total = sum(e.size for e in plan.entries if e.action in ("restore","rename"))
            self.summary.setText(f"恢复文件 {counts['restore']:,}　·　编号另存 {counts['rename']:,}　·　"
                                 f"创建目录 {counts['create_dir']:,}　·　跳过 {counts['skip']:,}　·　共 {byte_total:,} 字节")
            self.apply.setEnabled(any(e.action in ("create_dir","restore","rename") for e in plan.entries))
        self.page.task("正在计算恢复预览…",lambda cancel,progress:prepare_restore(
            repo,vid,dest,paths,conflict=mode,cancel=cancel,progress=progress),ready)

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
            prefix = "已取消，保留完成项" if result["cancelled"] else "恢复结束"
            self.summary.setText(f"{prefix}　·　已恢复文件 {result['restored']:,}　·　创建目录 {result['created_dirs']:,}　·　"
                                 f"跳过 {result['skipped']:,}　·　失败 {result['failed']:,}")
            self.issues.replace(result["issues"])
            if result["issues"]:
                self.results.setCurrentIndex(1)
        self.page.task("正在恢复并校验文件…",lambda cancel,progress:restore(plan,cancel=cancel,progress=progress),finished)
