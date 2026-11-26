"""Qt 表格与筛选模型，仅保存预览和本次勾选。"""
from datetime import datetime
from functools import lru_cache

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor

from .theme import icon


TYPES = {
    "全部文件": ("all", "#42715b"), "图片": ("image", "#47897c"),
    "文档": ("file", "#5a79a6"), "压缩包": ("package", "#a58140"),
    "安装包": ("installer", "#8371a3"), "其他": ("other", "#79837d")
}
STATUS = {"ready": "待移动", "skip": "已跳过", "conflict": "冲突", "review": "待复核"}
STATUS_COLORS = {"ready": "#256849", "skip": "#738276", "conflict": "#a86522", "review": "#a04f37"}
CONFLICTS = {"existing":"目标已存在", "batch":"批内重名", "unsafe":"路径不安全",
             "source":"来源异常", "review":"待复核", "":"无冲突"}


@lru_cache(maxsize=16)
def type_icon(category):
    return icon(*TYPES.get(category, TYPES["其他"]))


def size_text(value):
    if value < 1024:
        return f"{value} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"


def modified_text(ns):
    try:
        return datetime.fromtimestamp(ns / 10**9).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OverflowError, OSError):
        return "时间不可用"


class FileModel(QAbstractTableModel):
    selectionChanged = Signal()
    headers = ("执行", "名称", "预览", "扩展名", "大小", "修改时间", "当前路径")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.plans = []
        self.checked = set()

    def replace(self, plans):
        self.beginResetModel()
        self.plans = list(plans)
        self.checked.clear()
        self.endResetModel()
        self.selectionChanged.emit()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.plans)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        plan = self.plans[row]
        file = plan.file
        if role == Qt.ItemDataRole.CheckStateRole and col == 0 and plan.status == "ready":
            return Qt.CheckState.Checked if row in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DecorationRole and col == 1:
            return type_icon(file.category)
        if role == Qt.ItemDataRole.ForegroundRole and col == 2:
            return QColor(STATUS_COLORS[plan.status])
        if role == Qt.ItemDataRole.ToolTipRole:
            return str(file.path) if col in (1,6) else plan.reason or STATUS[plan.status]
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 4:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.UserRole):
            return None
        if col == 0:
            return (row in self.checked) if role == Qt.ItemDataRole.UserRole else None
        if col == 1:
            return file.path.name
        if col == 2:
            return STATUS[plan.status]
        if col == 3:
            return file.path.suffix.lower() or "—"
        if col == 4:
            return file.size if role == Qt.ItemDataRole.UserRole else size_text(file.size)
        if col == 5:
            return file.mtime_ns if role == Qt.ItemDataRole.UserRole else modified_text(file.mtime_ns)
        return str(file.path)

    def flags(self, index):
        flag = super().flags(index)
        if index.isValid() and index.column() == 0 and self.plans[index.row()].status == "ready":
            return flag | Qt.ItemFlag.ItemIsUserCheckable
        return flag

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.CheckStateRole or not (self.flags(index) & Qt.ItemFlag.ItemIsUserCheckable):
            return False
        if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
            self.checked.add(index.row())
        else:
            self.checked.discard(index.row())
        self.dataChanged.emit(index, index, [role])
        self.selectionChanged.emit()
        return True

    def select_rows(self, rows, enabled):
        if enabled:
            self.checked.update(rows)
        else:
            self.checked.difference_update(rows)
        if self.plans:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.plans)-1, 0),
                                  [Qt.ItemDataRole.CheckStateRole])
        self.selectionChanged.emit()


class FileFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = ""
        self.category = "全部文件"
        self.facets = (None, None, None, None)
        self.unfiltered = True
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def update_filter(self, text, category, facets=(None, None, None, None)):
        self.search = text.strip().casefold()
        self.category = category
        self.facets = facets
        self.unfiltered = not self.search and category=="全部文件" and all(v is None for v in facets)
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if self.unfiltered:
            return True
        plan = self.sourceModel().plans[row]
        file = plan.file
        if self.category != "全部文件" and self.category != file.category:
            return False
        source, target, rule, conflict = self.facets
        if source is not None and str(file.path.parent) != source:
            return False
        if target is not None:
            destination = str(plan.target.parent) if plan.target is not None else ""
            if destination != target:
                return False
        if rule is not None and plan.rule != rule:
            return False
        if conflict is not None and plan.conflict_type != conflict:
            return False
        return not self.search or self.search in str(file.path).casefold()
