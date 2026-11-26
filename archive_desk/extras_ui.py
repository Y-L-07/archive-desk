"""命名规则方案的本地管理对话框。"""

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .rules_dialog import condition_text
from .profiles import ProfileStore, export_profile, import_profile


class ProfileDialog(QDialog):
    def __init__(self, parent, rules, store):
        super().__init__(parent)
        self.store = store
        self.current_rules = list(rules)
        self.selected_rules = []
        self.setWindowTitle("规则方案")
        self.setObjectName("profileDialog")
        self.resize(670, 405)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        heading.addWidget(QLabel("已保存方案"))
        self.names = QComboBox()
        self.names.setObjectName("profileNames")
        self.names.setPlaceholderText("尚未保存方案")
        self.names.currentIndexChanged.connect(self.show_profile)
        heading.addWidget(self.names, 1)
        save = QPushButton("保存当前规则…")
        save.setObjectName("saveProfile")
        save.clicked.connect(self.save_current)
        heading.addWidget(save)
        layout.addLayout(heading)
        self.rules_grid = QTableWidget(0, 4)
        self.rules_grid.setHorizontalHeaderLabels(["规则", "状态", "匹配条件", "目标子目录"])
        self.rules_grid.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rules_grid.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_grid.verticalHeader().setDefaultSectionSize(32)
        self.rules_grid.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.rules_grid.setShowGrid(False)
        layout.addWidget(self.rules_grid, 1)
        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        actions = QHBoxLayout()
        load = QPushButton("导入 JSON…")
        load.setObjectName("importProfile")
        load.clicked.connect(self.import_file)
        self.export = QPushButton("导出 JSON…")
        self.export.setObjectName("exportProfile")
        self.export.clicked.connect(self.export_file)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        self.apply = QPushButton("使用此方案")
        self.apply.setObjectName("useProfile")
        self.apply.setProperty("primary", True)
        self.apply.clicked.connect(self.accept)
        for btn in (load, self.export):
            actions.addWidget(btn)
        actions.addStretch()
        actions.addWidget(close)
        actions.addWidget(self.apply)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self, selected=None):
        self.names.blockSignals(True)
        self.names.clear()
        self.names.addItems(self.store.names())
        if selected is not None:
            self.names.setCurrentText(selected)
        self.names.blockSignals(False)
        self.show_profile()

    def show_profile(self):
        name = self.names.currentText()
        self.selected_rules = self.store.load(name) if name else []
        self.rules_grid.setRowCount(len(self.selected_rules))
        for row, rule in enumerate(self.selected_rules):
            values = [rule.name, "启用" if rule.enabled else "停用",
                      condition_text(rule), rule.target]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.rules_grid.setItem(row, col, item)
        self.apply.setEnabled(bool(name))
        self.export.setEnabled(bool(name))
        self.hint.setText("使用方案后，工作台将按此顺序重新预览文件。" if name else
                          "保存当前规则或导入 JSON，即可在这里预览和使用方案。")

    def save_current(self):
        name, ok = QInputDialog.getText(self, "保存当前规则", "方案名称")
        if ok:
            self.save_rules(name.strip(), self.current_rules)

    def save_rules(self, name, rules, *, update_existing=False):
        try:
            self.store.save(name, rules, update_existing=update_existing)
            self.refresh(name)
            self.hint.setText("方案已保存。选择“使用此方案”可将规则载入工作台。")
        except (OSError, ValueError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "保存方案失败", str(exc))

    def import_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入规则方案", "", "规则方案 (*.json)")
        if filename:
            try:
                name, rules = import_profile(Path(filename))
                existing = next((saved for saved in self.store.names()
                                 if saved.casefold() == name.casefold()), None)
                update = False
                if existing:
                    ask = QMessageBox(QMessageBox.Icon.Question,"已有同名方案",
                        f"「{existing}」已经存在。可更新已保存的方案，或为导入的方案另取名称。",parent=self)
                    ask.setTextFormat(Qt.TextFormat.PlainText)
                    overwrite = ask.addButton("更新已保存方案",QMessageBox.ButtonRole.AcceptRole)
                    copy = ask.addButton("另存方案",QMessageBox.ButtonRole.ActionRole)
                    stop = ask.addButton("取消",QMessageBox.ButtonRole.RejectRole)
                    ask.setDefaultButton(stop)
                    ask.exec()
                    if ask.clickedButton() is overwrite:
                        update = True
                        name = existing
                    elif ask.clickedButton() is copy:
                        name, ok = QInputDialog.getText(self,"另存方案","新方案名称",text=f"{name}（副本）")
                        if not ok:
                            return
                    else:
                        return
                self.save_rules(name.strip(), rules, update_existing=update)
            except (OSError, ValueError, sqlite3.Error) as exc:
                QMessageBox.warning(self, "导入方案失败", str(exc))

    def export_file(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出到新 JSON 文件", "归档规则.json", "规则方案 (*.json)",
            options=QFileDialog.Option.DontConfirmOverwrite)
        if filename:
            try:
                export_profile(Path(filename), self.names.currentText(), self.selected_rules)
                self.hint.setText(f"方案已导出到 {filename}")
            except FileExistsError:
                QMessageBox.warning(self, "该文件已存在", "请为导出的方案选择一个新文件名。")
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "导出方案失败", str(exc))


def edit_profiles(parent, rules, journal):
    try:
        dialog = ProfileDialog(parent, rules, ProfileStore(journal.path))
    except (OSError, ValueError, sqlite3.Error) as exc:
        QMessageBox.warning(parent, "无法打开规则方案", str(exc))
        return None
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.selected_rules
    return None
