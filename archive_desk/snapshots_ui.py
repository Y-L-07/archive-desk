"""命名快照、只读比对和差异报告。"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QInputDialog,
                               QLabel, QVBoxLayout, QWidget)

from .catalog_widgets import button, local_time, records_table
from .catalog_snapshots import SnapshotStore, export_diff

CATEGORIES = {"added":"新增", "missing":"缺失", "changed":"内容变更", "moved":"疑似移动／改名",
              "uncertain":"无法确认"}


class SnapshotsTab(QWidget):
    def __init__(self, page, db_path):
        super().__init__(page)
        self.page = page
        self.store = SnapshotStore(db_path)
        self.root_id = None
        self.diff = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12,12,12,12)
        hint = QLabel("快照仅保存索引清单和已有哈希。它不是文件备份，不能恢复文件内容。")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)
        controls = QHBoxLayout()
        self.old = QComboBox()
        self.old.setObjectName("oldSnapshot")
        self.new = QComboBox()
        self.new.setObjectName("newSnapshot")
        self.old.currentIndexChanged.connect(self.invalidate)
        self.new.currentIndexChanged.connect(self.invalidate)
        controls.addWidget(QLabel("从"))
        controls.addWidget(self.old,1)
        controls.addWidget(QLabel("到"))
        controls.addWidget(self.new,1)
        self.compare_button = button("比较快照","compareSnapshots",self.compare)
        self.create_button = button("新建快照…","createSnapshot",self.create)
        controls.addWidget(self.compare_button)
        layout.addLayout(controls)
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("新快照来源"))
        self.source = QComboBox()
        self.source.setObjectName("snapshotSource")
        self.source.setPlaceholderText("尚无已发布清单")
        source_row.addWidget(self.source,1)
        source_row.addWidget(self.create_button)
        layout.addLayout(source_row)
        self.coverage = QLabel()
        self.coverage.setObjectName("muted")
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        tools = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.setObjectName("diffFilter")
        self.filter.addItem("全部差异",None)
        for key,value in CATEGORIES.items():
            self.filter.addItem(value,key)
        self.filter.currentIndexChanged.connect(self.show_diff)
        tools.addWidget(self.filter)
        self.summary = QLabel()
        self.summary.setObjectName("diffSummary")
        self.summary.setWordWrap(True)
        tools.addWidget(self.summary,1)
        self.export_button = button("导出当前结果 CSV…","exportDiff",self.export)
        tools.addWidget(self.export_button)
        layout.addLayout(tools)
        columns = [("变化",lambda r:CATEGORIES[r["status"]],110),("原相对路径",lambda r:r["old_path"] or "—",None),
                   ("新相对路径",lambda r:r["new_path"] or "—",None),("判断依据",lambda r:r["reason"],None)]
        self.table,self.model = records_table(self,columns,"diffTable")
        layout.addWidget(self.table,1)
        self.invalidate()

    def reload(self, root_id, preferred_old=None, preferred_new=None):
        self.root_id = root_id
        previous_old = self.old.currentData() if preferred_old is None else preferred_old
        previous_new = self.new.currentData() if preferred_new is None else preferred_new
        rows = sorted(self.store.list_snapshots(root_id),key=lambda r:r["id"]) if root_id is not None else []
        selected_source = self.source.currentData()
        self.source.clear()
        choices = self.store.sources(root_id) if root_id is not None else []
        has_baseline = any(r["source"] == "last_complete" for r in choices)
        for record in choices:
            scope = "完整覆盖" if record["scan_state"] == "complete" else "部分覆盖（含盲区）"
            origin = "末次完整扫描原始清单" if record["source"] == "last_complete" else "当前已发布索引"
            text = f"{origin} · {scope} · 第 {record['generation']} 轮 · {local_time(record['indexed_at'])}"
            if not has_baseline and record["scan_state"] == "partial":
                text += " · 暂无完整扫描清单"
            self.source.addItem(text,record["source"])
            self.source.setItemData(self.source.count()-1,record,Qt.ItemDataRole.UserRole+1)
        self.source.setCurrentIndex(max(0,self.source.findData(selected_source)) if choices else -1)
        for combo, preferred, default in ((self.old,previous_old,max(0,len(rows)-2)),
                                           (self.new,previous_new,len(rows)-1)):
            combo.blockSignals(True)
            combo.clear()
            for record in rows:
                coverage = "部分索引" if record["scan_state"]=="partial" else "完整索引"
                text = f"{record['name']}  ·  {record['file_count']} 项"
                combo.addItem(text,record["id"])
                combo.setItemData(combo.count()-1,f"{local_time(record['indexed_at'])}（{coverage}）",
                                  Qt.ItemDataRole.UserRole+1)
                combo.setItemData(combo.count()-1,
                    f"{'末次完整扫描原始清单' if record['source']=='last_complete' else '当前已发布索引'} · {coverage}\n"
                    f"索引于 {record['indexed_at']}\n快照创建于 {record['created_at']}\n"
                    f"扫描提示 {record['issue_count']} 条",Qt.ItemDataRole.ToolTipRole)
            idx = combo.findData(preferred)
            combo.setCurrentIndex(idx if idx>=0 else default)
            combo.blockSignals(False)
        self.create_button.setEnabled(bool(choices))
        self.invalidate()

    def invalidate(self):
        self.diff = None
        self.model.replace([])
        old, new = self.old.currentData(), self.new.currentData()
        self.compare_button.setEnabled(old is not None and new is not None and old!=new)
        a = self.old.currentData(Qt.ItemDataRole.UserRole+1)
        b = self.new.currentData(Qt.ItemDataRole.UserRole+1)
        self.coverage.setVisible(bool(a or b))
        self.coverage.setText(f"索引时间　{a or '—'}  →  {b or '—'}")
        self.export_button.setEnabled(False)
        self.filter.setEnabled(False)
        if self.root_id is None:
            self.summary.setText("请先选择一个档案库。")
        elif self.old.count()<2:
            self.summary.setText("创建两个命名快照后即可比较。")
        else:
            self.summary.setText("选择两个不同的快照进行比较。")
        for combo in (self.old,self.new):
            combo.setToolTip(combo.currentData(Qt.ItemDataRole.ToolTipRole) or "")

    def create(self):
        root_id = self.root_id
        source = self.source.currentData()
        expected = self.source.currentData(Qt.ItemDataRole.UserRole+1)
        if source is None:
            return
        name,ok = QInputDialog.getText(self,"新建命名快照",f"{self.source.currentText()}\n快照名称")
        if ok and name.strip():
            self.page.task("正在保存索引快照…",
                lambda cancel:self.store.create(root_id,name.strip(),cancel=cancel,source=source,expected=expected),
                lambda sid:self.reload(root_id,preferred_new=sid))

    def compare(self):
        old, new = self.old.currentData(), self.new.currentData()
        if old is None or new is None or old==new:
            return
        def ready(rows):
            self.diff = rows
            self.filter.setEnabled(True)
            self.show_diff()
        self.page.task("正在比较快照…",lambda cancel:self.store.compare(old,new,cancel=cancel),ready)

    def show_diff(self):
        if self.diff is None:
            return
        category = self.filter.currentData()
        rows = [r for r in self.diff if r["status"]==category] if category else self.diff
        self.model.replace(rows)
        self.summary.setText(f"共 {len(self.diff):,} 条差异，当前显示 {len(rows):,} 条。")
        self.export_button.setEnabled(True)

    def export(self):
        path,_ = QFileDialog.getSaveFileName(self,"导出 UTF-8 CSV","快照差异.csv","CSV 报告 (*.csv)",
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            rows = self.model.records
            self.page.task("正在导出 UTF-8 CSV…",lambda _:export_diff(Path(path),rows),
                           lambda _:self.page.message(f"已导出 {len(rows):,} 条差异到 {path}。"),
                           cancellable=False)
