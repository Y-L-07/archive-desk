"""仅在应用打开时轮询用户启用的本地收件箱。"""
import sqlite3
from pathlib import Path
from threading import Event

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableView, QVBoxLayout,
)

from .core import ScanCancelled
from .file_model import modified_text, size_text, type_icon
from .inbox import Inbox
from .jobs import Job


class CandidateModel(QAbstractTableModel):
    selection_changed = Signal()
    columns = ("选择", "文件名", "大小", "修改时间", "来源路径")

    def __init__(self, parent):
        super().__init__(parent)
        self.files = []
        self.selected = set()

    def replace(self, files):
        if files == self.files:
            return
        self.beginResetModel()
        self.files = list(files)
        self.selected.intersection_update(files)
        self.endResetModel()
        self.selection_changed.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        file = self.files[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if file in self.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(file.path)
        if role == Qt.ItemDataRole.DecorationRole and col == 1:
            return type_icon(file.category)
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1:
                return file.path.name
            if col == 2:
                return size_text(file.size)
            if col == 3:
                return modified_text(file.mtime_ns)
            if col == 4:
                return str(file.path)
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if (not index.isValid() or index.column() != 0
                or role != Qt.ItemDataRole.CheckStateRole):
            return False
        file = self.files[index.row()]
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.selected.add(file)
        else:
            self.selected.discard(file)
        self.dataChanged.emit(index, index, [role])
        self.selection_changed.emit()
        return True

    def toggle_all(self):
        if not self.files:
            return
        self.selected = set() if len(self.selected) == len(self.files) else set(self.files)
        self.dataChanged.emit(self.index(0, 0), self.index(len(self.files)-1, 0),
                              [Qt.ItemDataRole.CheckStateRole])
        self.selection_changed.emit()


class InboxPage(QFrame):
    preview_requested = Signal(object, object)
    count_changed = Signal(int)
    stopped = Signal()

    def __init__(self, parent, journal_path):
        super().__init__(parent)
        self.setObjectName("center")
        self.store = Inbox(journal_path)
        self.root = self.store.root()
        self.job = None
        self.changing = False
        self.closing = False
        self.workspace_busy = False
        self.started = False
        self.pending = 0
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.poll)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(13)
        title = QHBoxLayout()
        heading = QLabel("本地收件箱")
        heading.setObjectName("title")
        title.addWidget(heading)
        title.addStretch()
        self.pick = QPushButton("选择并启用文件夹…")
        self.pick.setObjectName("inboxChooseButton")
        self.pick.clicked.connect(self.choose)
        title.addWidget(self.pick)
        self.disable = QPushButton("停用监视")
        self.disable.setObjectName("inboxDisableButton")
        self.disable.clicked.connect(lambda: self.configure(None))
        title.addWidget(self.disable)
        layout.addLayout(title)
        self.directory = QLabel()
        self.directory.setTextFormat(Qt.TextFormat.PlainText)
        self.directory.setWordWrap(True)
        self.directory.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.directory)
        instruction = QLabel("打开应用期间只检查此目录的当前层。连续观察到稳定的文件后，可勾选并载入整理预览。")
        instruction.setObjectName("muted")
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        bar = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName("inboxStatus")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        self.select_all = QPushButton("全选候选")
        self.select_all.setObjectName("inboxSelectAll")
        bar.addWidget(self.select_all)
        layout.addLayout(bar)
        self.table = QTableView()
        self.table.setObjectName("inboxTable")
        self.model = CandidateModel(self)
        self.table.setModel(self.model)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(35)
        for col, width in enumerate((54, 230, 90, 152)):
            self.table.setColumnWidth(col,width)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        self.selection = QLabel()
        self.selection.setObjectName("muted")
        bottom.addWidget(self.selection)
        bottom.addStretch()
        self.ignore = QPushButton("忽略选中版本")
        self.ignore.setObjectName("inboxIgnoreButton")
        self.ignore.setToolTip("同一版本将从候选清单移除；文件再次修改后会重新观察。")
        self.ignore.clicked.connect(self.ignore_selected)
        bottom.addWidget(self.ignore)
        self.load = QPushButton("载入整理预览")
        self.load.setProperty("primary", True)
        self.load.setObjectName("inboxPreviewButton")
        self.load.clicked.connect(self.load_selected)
        bottom.addWidget(self.load)
        layout.addLayout(bottom)
        self.select_all.clicked.connect(self.model.toggle_all)
        self.model.selection_changed.connect(self.controls)
        self.controls()
        self.summary()

    def start(self):
        if self.started or self.closing:
            return
        self.started = True
        if self.root:
            self.timer.start()
            self.poll()

    def choose(self):
        path = QFileDialog.getExistingDirectory(self, "选择要监视的本地文件夹", str(self.root or ""),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if path:
            self.configure(Path(path))

    def configure(self, root):
        self.timer.stop()
        self.changing = True
        self.next_root = root
        if self.job:
            self.cancel.set()
            self.status.setText("正在停止本次检查…")
            self.controls()
        else:
            self._apply_config()

    def _apply_config(self):
        try:
            self.store.configure(self.next_root)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.status.setText(f"设置失败：{exc}")
        else:
            self.root = self.store.root()
            self.pending = 0
            self.model.replace([])
            self.count_changed.emit(0)
            self.summary()
        self.changing = False
        self.controls()
        if self.root and not self.closing:
            self.timer.start()
            self.poll()

    def controls(self):
        active = not self.changing and not self.closing
        can_edit = active and not self.workspace_busy
        self.pick.setEnabled(can_edit)
        self.disable.setEnabled(can_edit and self.root is not None)
        self.load.setEnabled(can_edit and bool(self.model.selected))
        self.ignore.setEnabled(can_edit and bool(self.model.selected))
        self.select_all.setEnabled(can_edit and bool(self.model.files))
        self.select_all.setText("取消全选" if self.model.files and
                                len(self.model.selected)==len(self.model.files) else "全选候选")
        amount = sum(file.size for file in self.model.selected)
        self.selection.setText(f"已勾选 {len(self.model.selected)} 项  ·  {amount:,} 字节")
        self.directory.setText(f"监视目录　{self.root}" if self.root else "尚未启用监视")

    def summary(self):
        self.status.setToolTip("")
        self.status.setText(f"稳定候选 {len(self.model.files)} 项　·　等待稳定 {self.pending} 项"
                            if self.root else "选择文件夹后开始观察新文件。")

    def set_workspace_busy(self, busy):
        self.workspace_busy = busy
        if self.job and busy:
            self.cancel.set()
        self.controls()

    def poll(self):
        if not self.root or self.workspace_busy or self.closing or self.changing or self.job:
            return
        self.cancel = Event()
        self.job = Job(self, lambda: self.store.poll(cancel=self.cancel))
        self.job.finished.connect(self._finished)
        self.job.start()

    def _finished(self):
        job = self.job
        self.job = None
        job.deleteLater()
        if self.closing:
            self.stopped.emit()
            return
        if self.changing:
            self._apply_config()
            return
        if isinstance(job.error, ScanCancelled) or self.cancel.is_set():
            return
        if job.error:
            self.status.setText(f"检查失败：{job.error}")
            self.model.replace([])
            self.count_changed.emit(0)
            return
        result = job.result
        if result.root != self.root:
            return
        self.pending = result.pending
        self.model.replace(result.candidates)
        self.count_changed.emit(len(result.candidates))
        self.summary()
        if result.warnings:
            self.status.setText(self.status.text() + f"　·　{len(result.warnings)} 条提示")
            self.status.setToolTip("\n".join(result.warnings))

    def load_selected(self):
        if self.root and self.model.selected and not self.workspace_busy:
            files = [f for f in self.model.files if f in self.model.selected]
            self.preview_requested.emit(self.root, files)

    def ignore_selected(self):
        files = list(self.model.selected)
        if self.job:
            self.cancel.set()
        try:
            self.store.acknowledge(files)
            remaining = self.store.candidates()
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.status.setText(f"更新候选状态失败：{exc}")
            return
        self.model.replace(remaining)
        self.count_changed.emit(len(self.model.files))
        self.summary()

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        if self.job:
            self.cancel.set()
            return False
        return True
