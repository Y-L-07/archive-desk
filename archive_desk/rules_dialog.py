"""工作规则中心及单文件只读试跑。"""
import re
from dataclasses import replace
from pathlib import Path
from threading import Event

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .core import Rule, ScanCancelled, validate_rule
from .jobs import Job
from .planning import preview
from .scanner import snapshot_file


def condition_text(rule):
    parts = []
    if rule.extensions:
        parts.append(" ".join(rule.extensions))
    if rule.name_glob:
        parts.append(f"名称 {rule.name_glob}")
    if rule.source_subdir:
        parts.append(f"来源 {rule.source_subdir}")
    if rule.min_size is not None:
        parts.append(f"≥ {rule.min_size:,} 字节")
    if rule.max_size is not None:
        parts.append(f"≤ {rule.max_size:,} 字节")
    if rule.modified_from:
        parts.append(f"从 {rule.modified_from}")
    if rule.modified_to:
        parts.append(f"至 {rule.modified_to}")
    return "  ·  ".join(parts) or "所有文件"


def action(text, slot, name):
    widget = QPushButton(text)
    widget.setObjectName(name)
    widget.clicked.connect(slot)
    return widget


def controls(dialog, ok_text, save):
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    box.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
    box.button(QDialogButtonBox.StandardButton.Ok).setObjectName("saveButton")
    box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    box.accepted.connect(save)
    box.rejected.connect(dialog.reject)
    return box


class RuleEditor(QDialog):
    def __init__(self, parent, rule):
        super().__init__(parent)
        self.rule = rule
        self.setWindowTitle("编辑规则" if rule.name else "新建规则")
        self.resize(540, 460)
        root = QVBoxLayout(self)
        root.setContentsMargins(20,18,20,16)
        form = QFormLayout()
        form.setSpacing(9)
        self.fields = {}
        def field(key, text, hint=""):
            w = QLineEdit(text)
            w.setObjectName(key)
            w.setPlaceholderText(hint)
            self.fields[key] = w
            return w
        form.addRow("规则名称", field("name",rule.name,"例如：项目图片"))
        form.addRow("扩展名", field("extensions", " ".join(rule.extensions), ".jpg .png，可用空格或逗号分隔"))
        form.addRow("名称通配符", field("name_glob",rule.name_glob,"例如：报告-*.pdf"))
        form.addRow("来源子目录", field("source_subdir",rule.source_subdir,"相对于扫描目录，包含其下层目录"))
        for title, first, second, placeholder in (
                ("大小范围（字节）","min_size","max_size","不限"),
                ("修改日期","modified_from","modified_to","YYYY-MM-DD")):
            row = QHBoxLayout()
            for key in (first, second):
                if key == second:
                    row.addWidget(QLabel("至"))
                value = getattr(rule, key)
                row.addWidget(field(key, str(value) if value is not None else "", placeholder))
            form.addRow(title, row)
        form.addRow("目标子目录", field("target",rule.target,"例如：图片/{year}/{month}/{ext}"))
        template = QLabel("空白条件表示不限，所有填写的条件须同时满足。{year}、{month} 取自文件的本地修改日期，{ext} 为小写扩展名（无点）。")
        template.setObjectName("muted")
        template.setWordWrap(True)
        form.addRow("", template)
        self.enabled = QCheckBox("启用此规则")
        self.enabled.setChecked(rule.enabled)
        form.addRow("", self.enabled)
        root.addLayout(form)
        root.addStretch()
        root.addWidget(controls(self, "确定", self.save))

    def save(self):
        data = {key: widget.text().strip() for key, widget in self.fields.items()}
        for key in ("min_size","max_size"):
            value = data[key]
            if value and (not value.isascii() or not value.isdigit() or len(value) > 20):
                QMessageBox.warning(self, "请检查文件大小", "大小应为非负整数字节数。")
                self.fields[key].setFocus()
                return
            data[key] = int(value) if value else None
        exts = re.split(r"[\s,，;；]+", data["extensions"])
        data["extensions"] = tuple(dict.fromkeys(
            (ext if ext.startswith(".") else "." + ext).lower() for ext in exts if ext))
        rule = Rule(**data, enabled=self.enabled.isChecked())
        try:
            validate_rule(rule)
        except ValueError as exc:
            QMessageBox.warning(self, "请检查规则", str(exc))
            return
        self.rule = rule
        self.accept()


class RulesDialog(QDialog):
    def __init__(self, parent, rules, root=None, target_root=None):
        super().__init__(parent)
        self.trial_job = None
        self.close_after_trial = False
        self.root = root
        self.target_root = target_root
        self.rules = list(rules)
        self.setWindowTitle("规则中心")
        self.setObjectName("rulesDialog")
        self.resize(830, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20,18,20,16)
        layout.setSpacing(10)
        layout.addWidget(QLabel("高优先级在上，每个文件使用首条命中的启用规则。"))
        toolbar = QHBoxLayout()
        for title, slot, name in (
                ("新建",self.add,"newRule"), ("编辑",self.edit,"editRule"),
                ("复制",self.copy,"copyRule"), ("移除",self.remove,"removeRule"),
                ("上移",lambda: self.reorder(-1),"raiseRule"), ("下移",lambda: self.reorder(1),"lowerRule")):
            toolbar.addWidget(action(title,slot,name))
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.grid = QTableWidget(0,4)
        self.grid.setObjectName("ruleGrid")
        self.grid.setHorizontalHeaderLabels(["启用","规则名称","匹配条件","目标子目录"])
        self.grid.verticalHeader().setDefaultSectionSize(34)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.grid.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
        self.grid.setColumnWidth(0,54)
        self.grid.setColumnWidth(3,164)
        self.grid.cellDoubleClicked.connect(lambda row, col: self.edit() if col != 0 else None)
        self.grid.itemChanged.connect(self._toggled)
        layout.addWidget(self.grid,1)
        self.result = QLabel("试跑可查看一个文件按当前规则整理的结果。")
        self.result.setObjectName("trialResult")
        self.result.setTextFormat(Qt.TextFormat.PlainText)
        self.result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result.setWordWrap(True)
        layout.addWidget(self.result)
        bottom = QHBoxLayout()
        self.trial_button = action("用指定文件试跑…",self.try_file,"trialButton")
        bottom.addWidget(self.trial_button)
        bottom.addStretch()
        bottom.addWidget(controls(self, "保存工作规则", self.accept))
        layout.addLayout(bottom)
        self.populate(0)

    def populate(self, index):
        self.grid.blockSignals(True)
        self.grid.setRowCount(len(self.rules))
        for row, rule in enumerate(self.rules):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            self.grid.setItem(row,0,check)
            for col, value in enumerate((rule.name,condition_text(rule),rule.target),1):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.grid.setItem(row,col,item)
        self.grid.blockSignals(False)
        if self.rules:
            self.grid.selectRow(index)
        self.result.setText("试跑可查看一个文件按当前规则整理的结果。")

    def _toggled(self,item):
        if item.column()==0:
            row = item.row()
            self.rules[row] = replace(self.rules[row],enabled=item.checkState()==Qt.CheckState.Checked)
            self.result.setText("规则状态已修改，可重新试跑。")

    def add(self):
        dialog = RuleEditor(self,Rule("",(),"归档"))
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.rules.append(dialog.rule)
            self.populate(len(self.rules)-1)

    def edit(self):
        row = self.grid.currentRow()
        if row < 0:
            return
        dialog = RuleEditor(self,self.rules[row])
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.rules[row] = dialog.rule
            self.populate(row)

    def copy(self):
        row = self.grid.currentRow()
        if row >= 0:
            original = self.rules[row]
            self.rules.insert(row+1,replace(original,name=original.name[:74]+"（副本）"))
            self.populate(row+1)

    def remove(self):
        row = self.grid.currentRow()
        if row >= 0:
            del self.rules[row]
            self.populate(min(row,len(self.rules)-1))

    def reorder(self, delta):
        row = self.grid.currentRow()
        other = row + delta
        if row >= 0 and 0 <= other < len(self.rules):
            self.rules[row],self.rules[other] = self.rules[other],self.rules[row]
            self.populate(other)

    def accept(self):
        if not 1 <= len(self.rules) <= 64:
            QMessageBox.warning(self,"规则数量","请保留 1 至 64 条工作规则，可通过取消勾选停用规则。")
            return
        super().accept()

    def try_file(self):
        if self.trial_job is not None:
            self.trial_cancel.set()
            self.trial_button.setEnabled(False)
            self.result.setText("正在取消试跑…")
            return
        name, _ = QFileDialog.getOpenFileName(self,"选择试跑文件",str(self.root or ""),"所有文件 (*)",
                                              options=QFileDialog.Option.DontResolveSymlinks)
        if not name:
            return
        path = Path(name).absolute()
        # 从源目录外选择文件时，以该文件的父目录作为试跑来源。
        source = self.root if self.root and path.is_relative_to(self.root) else path.parent
        target = self.target_root or source
        rules = list(self.rules)
        self.trial_cancel = Event()
        def run():
            file = snapshot_file(path,self.trial_cancel)
            plan, = preview(source,[file],rules,self.trial_cancel,target_root=target)
            return plan
        self.trial_job = Job(self,run)
        self.trial_job.finished.connect(self._trial_finished)
        self.result.setText(f"正在读取并试跑：{path}")
        self.grid.setEnabled(False)
        for widget in self.findChildren(QPushButton):
            widget.setEnabled(widget is self.trial_button)
        self.trial_button.setText("取消试跑")
        self.trial_job.start()

    def _trial_finished(self):
        job = self.trial_job
        self.trial_job = None
        self.grid.setEnabled(True)
        for widget in self.findChildren(QPushButton):
            widget.setEnabled(True)
        self.trial_button.setText("用指定文件试跑…")
        if isinstance(job.error,ScanCancelled) or self.trial_cancel.is_set():
            self.result.setText("试跑已取消。")
        elif job.error:
            self.result.setText(f"试跑失败：{job.error}")
        else:
            plan = job.result
            self.result.setText(f"来源：{plan.file.path}\n命中：{plan.rule or '无'}　·　{plan.reason}\n目标：{plan.target or '—'}")
        job.deleteLater()
        if self.close_after_trial:
            super().reject()

    def reject(self):
        if self.trial_job:
            self.trial_cancel.set()
            self.close_after_trial = True
        else:
            super().reject()

    def closeEvent(self,event):
        if self.trial_job:
            self.trial_cancel.set()
            self.close_after_trial = True
            event.ignore()
        else:
            super().closeEvent(event)
