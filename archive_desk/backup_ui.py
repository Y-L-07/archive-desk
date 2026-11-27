"""手动备份、独立仓库导入和完整性检查。"""
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QTabWidget, QVBoxLayout, QWidget)

from .backup_demo import create_backup_demo
from .backup_format import list_versions, verify_repository
from .backup_recover_ui import RecoveryTab, STATUS, folder_dialog, text_label
from .backup_writer import prepare_backup, write_backup
from .backup_tools import BackupPresets, export_csv
from .catalog import Catalog
from .catalog_widgets import button, records_table
from .file_model import size_text


class BackupPage(QFrame):
    def __init__(self,window):
        super().__init__(window)
        self.window = window
        self.db_path = window.journal.path
        self.registry = Catalog(self.db_path)
        self.presets = BackupPresets(self.db_path)
        self.last_report = None
        self.plan = self.target = self.repo = None
        self.sources = []
        self.setObjectName("center")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20,16,20,12)
        layout.setSpacing(11)
        title_row = QHBoxLayout()
        title = QLabel("备份与找回")
        title.setObjectName("title")
        title_row.addWidget(title)
        title_row.addStretch()
        title_row.addWidget(button("创建备份演示","backupDemoButton",self.demo))
        title_row.addWidget(button("导入已有仓库…","importBackupRepo",self.choose_import))
        layout.addLayout(title_row)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("backupTabs")
        self.tabs.addTab(self._writer(),"创建备份")
        self.recovery = RecoveryTab(self)
        self.tabs.addTab(self.recovery,"找回文件")
        self.tabs.addTab(self._verify(),"验证备份")
        layout.addWidget(self.tabs,1)
        self.refresh_sources()

    def task(self,caption,work,done,failed=None):
        if self.window.job is not None:
            return
        cancel = Event()
        next_update = 0.0
        def progress(done,total,message):
            nonlocal next_update
            if done==total or monotonic()>=next_update:
                self.window.progress_event.emit(done,total,str(message))
                next_update = monotonic()+.10
        def interrupted(stopped,error):
            self.window._note("操作已取消。" if stopped else f"操作未完成：{error}")
            if failed:
                failed(stopped,error)
        self.window._run(lambda:work(cancel,progress),done,caption,cancel,interrupted=interrupted)

    def _writer(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14,14,14,12)
        form = QFormLayout()
        self.sources_combo = QComboBox()
        self.sources_combo.setObjectName("backupSource")
        self.sources_combo.currentIndexChanged.connect(self.invalidate)
        form.addRow("来源档案库",self.sources_combo)
        self.source_label = text_label()
        form.addRow("",self.source_label)
        repo_row = QHBoxLayout()
        self.target_label = text_label("请选择独立目录")
        repo_row.addWidget(self.target_label,1)
        repo_row.addWidget(button("选择备份目录…","chooseBackupTarget",self.choose_target))
        form.addRow("备份仓库",repo_row)
        presets = QHBoxLayout()
        presets.addWidget(button("保存模板…","saveBackupPreset",self.save_preset))
        presets.addWidget(button("使用模板…","loadBackupPreset",self.load_preset))
        presets.addStretch()
        form.addRow("备份模板",presets)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.preflight = button("预检备份范围","prepareBackup",self.prepare)
        self.start = button("开始备份","executeBackup",self.execute)
        self.start.setProperty("primary",True)
        actions.addWidget(self.preflight)
        actions.addWidget(self.start)
        actions.addStretch()
        self.browse = button("浏览此仓库","browseWrittenBackup",lambda:self.open_repository(self.target))
        self.browse.setEnabled(False)
        actions.addWidget(self.browse)
        self.report_button = button("导出备份报告…","exportBackupReport",self.export_report)
        self.report_button.setEnabled(False)
        actions.addWidget(self.report_button)
        layout.addLayout(actions)
        self.stats = text_label()
        self.stats.setObjectName("backupSummary")
        layout.addWidget(self.stats)
        self.writer_table,self.writer_issues = records_table(page,[
            ("未纳入的路径",lambda r:r["path"] or "源目录",None),
            ("原因",lambda r:r["reason"],None)],"backupIssues")
        layout.addWidget(self.writer_table,1)
        return page

    def _verify(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14,14,14,12)
        row = QHBoxLayout()
        self.verify_repo = text_label("请导入要验证的仓库。")
        row.addWidget(self.verify_repo,1)
        self.verify_button = button("验证备份","verifyBackup",self.verify_imported)
        self.verify_button.setEnabled(False)
        row.addWidget(self.verify_button)
        layout.addLayout(row)
        self.verify_summary = text_label("核对已发布版本的清单、引用的内容对象与 SHA-256。")
        self.verify_summary.setObjectName("verifySummary")
        layout.addWidget(self.verify_summary)
        kinds = {"missing":"对象缺失","hash":"哈希不符","manifest":"清单损坏","unreadable":"对象无法读取",
                 "incomplete":"版本部分成功"}
        table,self.verify_rows = records_table(page,[
            ("类别",lambda r:kinds.get(r["kind"],r["kind"]),140),
            ("版本",lambda r:r["version"],None),("受影响的路径",lambda r:r["path"] or "整个版本",None),
            ("说明",lambda r:r["reason"],None)],"backupVerifyTable")
        layout.addWidget(table,1)
        return page

    def refresh_sources(self,selected=None):
        old = self.sources_combo.currentData() if selected is None else selected
        self.sources = self.registry.roots()
        self.sources_combo.blockSignals(True)
        self.sources_combo.clear()
        if not self.sources:
            self.sources_combo.addItem("请先添加档案库，或创建备份演示",None)
        for root in self.sources:
            self.sources_combo.addItem(root["name"],root["id"])
        self.sources_combo.setCurrentIndex(max(0,self.sources_combo.findData(old)))
        self.sources_combo.blockSignals(False)
        self.invalidate()

    def source(self):
        rid = self.sources_combo.currentData()
        return next((r for r in self.sources if r["id"]==rid),None)

    def invalidate(self,*_):
        self.plan = None
        root = self.source()
        self.source_label.setText(root["path"] if root else "添加来源后可直接预检。找回文件可以单独导入仓库。")
        self.stats.setText("预检会重新读取来源目录，统计文件数、估算容量并检查备份位置。")
        self.writer_issues.replace([])
        self.start.setEnabled(False)
        self.preflight.setEnabled(root is not None and self.target is not None)

    def choose_target(self):
        selected = folder_dialog(self,"选择独立的备份仓库目录")
        if selected:
            self.target = selected
            self.target_label.setText(str(selected))
            self.browse.setEnabled(False)
            self.invalidate()

    def save_preset(self):
        root = self.source()
        if not root or not self.target:
            self.window._note("请先选择来源档案库和备份目录。")
            return
        name, ok = QInputDialog.getText(self,"保存备份模板","模板名称")
        if ok and name.strip():
            if (any(p["name"] == name.strip() for p in self.presets.list()) and
                    not self.window.confirm("替换备份模板",name.strip(),"替换模板")):
                return
            self.presets.save(name,root["id"],self.target)
            self.window._note("备份模板已保存。")

    def load_preset(self):
        presets = {row["name"]:row for row in self.presets.list()}
        if not presets:
            self.window._note("请先保存一个备份模板。")
            return
        name, ok = QInputDialog.getItem(self,"使用备份模板","模板",list(presets),editable=False)
        if ok:
            preset = presets[name]
            self.target = Path(preset["target"])
            self.target_label.setText(str(self.target))
            self.browse.setEnabled(False)
            self.refresh_sources(preset["root_id"])

    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self,"保存备份报告","备份报告.csv","CSV (*.csv)")
        if path and self.last_report:
            report = self.last_report
            self.task("正在导出备份报告…",lambda *_:export_csv(Path(path),("项目","内容"),report),
                      lambda _:self.window._note("备份报告已保存。"))

    def prepare(self):
        root,repo = self.source(),self.target
        self.invalidate()
        def ready(plan):
            self.plan = plan
            self.stats.setText(f"预估 {plan.count:,} 个文件，共 {plan.size:,} 字节（{size_text(plan.size)}）"
                               f"　·　仓库可用 {plan.free:,} 字节\n{plan.disk_note}　·　预检提示 {len(plan.issues):,} 条")
            self.writer_issues.replace(plan.issues)
            self.start.setEnabled(True)
        self.task("正在预检备份范围…",lambda cancel,progress:prepare_backup(root,repo,cancel=cancel,
                  progress=progress),ready)

    def execute(self):
        plan = self.plan
        if plan is None:
            return
        details = (f"来源：{plan.source_name}\n{plan.source}\n\n备份仓库：{plan.repo}\n\n"
                   f"预估 {plan.count:,} 个文件，共 {plan.size:,} 字节；可用空间 {plan.free:,} 字节。"
                   f"\n{plan.disk_note}\n预检中有 {len(plan.issues):,} 条提示。将创建新的备份版本。")
        if not self.window.confirm("确认手动创建备份",details,"确认备份"):
            return
        self.plan = None
        self.start.setEnabled(False)
        started = monotonic()
        info = [("来源",str(plan.source)),("仓库",str(plan.repo))]
        def failed(stopped,error):
            self.last_report = [*info,("状态","已取消" if stopped else "失败"),
                                ("用时（秒）",round(monotonic()-started,2)),
                                ("说明",str(error) if error else "任务已取消")]
            self.report_button.setEnabled(True)
        def finished(result):
            self.last_report = [("版本",result["id"]),*info,
                                ("状态",STATUS[result["status"]]),("文件数",result["files"]),
                                ("读取字节",result["bytes_read"]),("新增对象",result["new_objects"]),
                                ("问题数",len(result["issues"])),("用时（秒）",round(result["elapsed"],2))]
            self.last_report.extend((item["path"],item["reason"]) for item in result["issues"])
            self.report_button.setEnabled(True)
            self.writer_issues.replace(result["issues"])
            self.stats.setText(f"备份{STATUS[result['status']]}　·　{result['files']:,} 个文件　·　"
                               f"{result['file_bytes']:,} 字节　·　新内容对象 {result['new_objects']:,} 个"
                               f"　·　提示 {len(result['issues']):,} 条\n版本 {result['id']}")
            self.browse.setEnabled(True)
            self.set_verify_repo(plan.repo)
        def run(cancel,progress):
            result = write_backup(plan,cancel=cancel,progress=progress)
            return dict(result,elapsed=monotonic()-started)
        self.task("正在读取文件并创建备份…",run,finished,failed=failed)

    def demo(self):
        def generated(result):
            self.target = result["repository"]
            self.tabs.setCurrentIndex(0)
            self.target_label.setText(str(self.target))
            self.browse.setEnabled(False)
            self.refresh_sources(result["root_id"])
            self.window._note(f"临时演示目录：{result['directory']}。请预检并确认后创建备份。")
        self.task("正在创建临时备份演示…",lambda *_:create_backup_demo(self.db_path),generated)

    def choose_import(self):
        repo = folder_dialog(self,"导入已有备份仓库")
        if repo:
            self.open_repository(repo)

    def open_repository(self,repo):
        def imported(rows):
            self.set_verify_repo(repo)
            self.tabs.setCurrentWidget(self.recovery)
            self.recovery.set_repository(repo,rows)
        self.task("正在核对仓库并读取版本列表…",lambda cancel,_:list_versions(repo,cancel=cancel),imported)

    def set_verify_repo(self,repo):
        self.repo = repo
        self.verify_repo.setText(f"仓库　{repo}")
        self.verify_rows.replace([])
        self.verify_summary.setText("准备就绪。点击验证备份以检查各版本。")
        self.verify_button.setEnabled(True)

    def verify_imported(self):
        # The recovery page may be browsing a previously imported repository.
        if self.tabs.currentWidget() is self.recovery and self.recovery.repo is not None:
            self.set_verify_repo(self.recovery.repo)
        repo = self.repo
        if repo is None:
            return
        self.tabs.setCurrentIndex(2)
        def checked(report):
            issues = report["issues"]
            self.verify_summary.setText(f"已检查 {report['versions']:,} 个版本，{report['objects']:,} 个内容对象。"
                                        f"完整性问题 {len(issues):,} 条，部分成功版本 {len(report['incomplete']):,} 个。")
            rows = list(issues)
            rows.extend(dict(kind="incomplete",version=vid,path="",reason="备份时有项目未被纳入，请查阅该版本的遗漏明细。")
                        for vid in report["incomplete"])
            self.verify_rows.replace(rows)
        self.task("正在验证备份仓库…",lambda cancel,progress:verify_repository(repo,cancel=cancel,
                  progress=progress),checked)
