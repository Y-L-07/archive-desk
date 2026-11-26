"""在前台打开只读的重复件报告，哈希工作在可取消的线程内执行。"""
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QThread, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView,
                               QLabel, QPlainTextEdit, QProgressBar, QPushButton,
                               QTableView, QVBoxLayout)

from .core import FileInfo, ScanCancelled
from .duplicates import find_duplicates
from .file_model import modified_text, size_text


class DuplicateJob(QThread):
    progress = Signal(int, int, str)

    def __init__(self, parent, root, files):
        super().__init__(parent)
        self.root = root
        self.files = files
        self.cancel = Event()
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = find_duplicates(self.root, self.files, self.cancel, self.progress.emit)
        except Exception as exc:
            self.error = exc


class DuplicateModel(QAbstractTableModel):
    headings = ("重复组", "文件名称", "大小", "当前路径", "修改时间", "保留")
    keeper_column = 5
    keepers_changed = Signal()

    def __init__(self, result, parent):
        super().__init__(parent)
        self.rows = [(index, group, file) for index, group in enumerate(result.groups, 1)
                     for file in group.files]
        self.keepers = {}

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headings)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headings[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        number, group, file = self.rows[index.row()]
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == self.keeper_column:
            return (Qt.CheckState.Checked if self.keepers.get(number) == index.row()
                    else Qt.CheckState.Unchecked)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{file.path}\nSHA-256　{group.digest}"
        if role == Qt.ItemDataRole.DisplayRole:
            return (f"第 {number} 组", file.path.name, size_text(group.size),
                    str(file.path), modified_text(file.mtime_ns), None)[index.column()]
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == self.keeper_column:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if not (index.isValid() and index.column() == self.keeper_column
                and role == Qt.ItemDataRole.CheckStateRole):
            return False
        row = index.row()
        number = self.rows[row][0]
        previous = self.keepers.pop(number, None)
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.keepers[number] = row
        if previous is not None and previous != row:
            old = self.index(previous, self.keeper_column)
            self.dataChanged.emit(old, old, [role])
        self.dataChanged.emit(index, index, [role])
        self.keepers_changed.emit()
        return True

    def selected_groups(self) -> list[tuple[FileInfo, tuple[FileInfo, ...], str]]:
        selected = []
        for number, row in sorted(self.keepers.items()):
            _, group, keeper = self.rows[row]
            copies = tuple(file for file in group.files if file.path != keeper.path)
            selected.append((keeper, copies, group.digest))
        return selected


class DuplicatesDialog(QDialog):
    def __init__(self, parent, root, files):
        super().__init__(parent)
        self.setWindowTitle("重复件检查")
        self.setObjectName("duplicatesDialog")
        self.resize(890, 480)
        self.close_when_finished = False
        self.selected = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        self.summary = QLabel("正在按文件大小筛选候选项…")
        self.summary.setObjectName("duplicateSummary")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        layout.addWidget(self.bar)
        self.table = QTableView()
        self.table.setObjectName("duplicateResults")
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        self.instructions = QLabel("每组勾选一个保留文件，其他副本可加入隔离计划。")
        self.instructions.setWordWrap(True)
        self.instructions.hide()
        layout.addWidget(self.instructions)
        self.skips = QPlainTextEdit()
        self.skips.setReadOnly(True)
        self.skips.setMaximumHeight(90)
        self.skips.hide()
        layout.addWidget(self.skips)

        buttons = QHBoxLayout()
        self.details_button = QPushButton("查看跳过原因")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self.skips.setVisible)
        self.details_button.hide()
        buttons.addWidget(self.details_button)
        buttons.addStretch()
        self.close_button = QPushButton("取消检查")
        self.close_button.setObjectName("duplicateClose")
        self.close_button.clicked.connect(self.stop_or_close)
        buttons.addWidget(self.close_button)
        self.plan_button = QPushButton("加入隔离计划")
        self.plan_button.setObjectName("addQuarantinePlan")
        self.plan_button.setProperty("primary", True)
        self.plan_button.setEnabled(False)
        self.plan_button.clicked.connect(self.add_selected)
        buttons.addWidget(self.plan_button)
        layout.addLayout(buttons)

        self.job = DuplicateJob(self, root, tuple(files))
        self.job.progress.connect(self._progress)
        self.job.finished.connect(self._finished)

    def _progress(self, done, total, file):
        if self.job.cancel.is_set():
            return
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)
        if file:
            self.summary.setText(f"正在核对 {done + 1} / {total}　·　{file}")

    def _finished(self):
        self.bar.hide()
        error = self.job.error
        if isinstance(error, ScanCancelled):
            self.summary.setText("已取消检查。")
        elif error:
            self.summary.setText(f"检查失败。{error}")
        else:
            report = self.job.result
            model = DuplicateModel(report, self.table)
            self.table.setModel(model)
            self.table.setColumnWidth(0, 76)
            self.table.setColumnWidth(1, 184)
            self.table.setColumnWidth(2, 84)
            self.table.setColumnWidth(4, 145)
            self.table.setColumnWidth(model.keeper_column, 50)
            header = self.table.horizontalHeader()
            header.moveSection(header.visualIndex(model.keeper_column), 0)
            header.moveSection(header.visualIndex(4), 4)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            model.keepers_changed.connect(self.update_selection)
            rows = model.rowCount()
            self.instructions.setVisible(bool(rows))
            outcome = f"发现 {len(report.groups)} 组相同内容，共 {rows} 个文件" if rows else "未发现相同内容"
            self.summary.setText(f"{outcome}　·　候选 {report.candidates} 项　·　跳过 {len(report.skipped)} 项")
            if report.skipped:
                self.skips.setPlainText("\n".join(report.skipped))
                self.details_button.show()
        self.close_button.setEnabled(True)
        self.close_button.setText("关闭")
        if self.close_when_finished:
            super().reject()

    def update_selection(self):
        model = self.table.model()
        count = sum(len(model.rows[row][1].files) - 1 for row in model.keepers.values())
        self.plan_button.setEnabled(bool(count))
        self.plan_button.setText(f"加入隔离计划（{count} 个副本）" if count else "加入隔离计划")

    def add_selected(self):
        self.selected = self.table.model().selected_groups()
        if self.selected:
            self.accept()

    def _cancel(self):
        self.job.cancel.set()
        self.close_button.setText("正在取消…")
        self.close_button.setEnabled(False)

    def stop_or_close(self):
        if self.job.isRunning():
            self._cancel()
        else:
            self.reject()

    def reject(self):
        if self.job.isRunning():
            self.close_when_finished = True
            self._cancel()
        else:
            super().reject()

    def closeEvent(self, event):
        if self.job.isRunning():
            self.close_when_finished = True
            self._cancel()
            event.ignore()
        else:
            super().closeEvent(event)


def show_duplicates(parent, root, files) -> list[tuple[FileInfo, tuple[FileInfo, ...], str]] | None:
    dialog = DuplicatesDialog(parent, root, files)
    dialog.job.start()
    dialog.exec()
    selected = dialog.selected
    dialog.deleteLater()
    return selected
