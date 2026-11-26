"""档案检索及快照共用的分页表格。"""
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QPushButton, QTableView


def local_time(value):
    if not value:
        return "无"
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def button(caption, name, slot):
    control = QPushButton(caption)
    control.setObjectName(name)
    control.clicked.connect(slot)
    return control


class RecordModel(QAbstractTableModel):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.records = []

    def replace(self, records):
        self.beginResetModel()
        self.records = records
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section][0]
        return super().headerData(section, orientation, role)

    def data(self, index, role):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            getter = self.columns[index.column()][1]
            return str(getter(self.records[index.row()]))
        return None


def records_table(parent, columns, name, *, extended=False):
    table = QTableView(parent)
    table.setObjectName(name)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    if extended:
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    else:
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.verticalHeader().hide()
    table.verticalHeader().setDefaultSectionSize(34)
    table.setShowGrid(False)
    table.setWordWrap(False)
    model = RecordModel(columns, table)
    table.setModel(model)
    for i, (_, _, width) in enumerate(columns):
        if width:
            table.setColumnWidth(i, width)
        else:
            table.horizontalHeader().setSectionResizeMode(i,QHeaderView.ResizeMode.Stretch)
    return table, model
