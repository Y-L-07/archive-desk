"""按清单展示可勾选的文件树；仅访问清单元数据。"""
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal

from .core import check_cancel
from .file_model import size_text


@dataclass(eq=False, slots=True)
class Node:
    path: str
    parent: object = None
    size: int | None = None
    row: int = 0
    children: list = field(default_factory=list)
    state: Qt.CheckState = Qt.CheckState.Unchecked


def build_tree(manifest, cancel):
    root = Node("")
    nodes = {"": root}

    def add(path, size=None):
        missing = []
        ancestor = path
        while ancestor not in nodes:
            missing.append(ancestor)
            ancestor = ancestor.rpartition("/")[0]
        parent = nodes[ancestor]
        for name in reversed(missing):
            node = Node(name, parent, size if name == path else None, len(parent.children))
            parent.children.append(node)
            nodes[name] = node
            parent = node

    for path in manifest["directories"]:
        check_cancel(cancel)
        add(path)
    for record in manifest["files"]:
        check_cancel(cancel)
        add(record["path"], record["size"])
    for node in nodes.values():
        if node.children:
            node.children.sort(key=lambda n:(n.size is not None, n.path.casefold()))
            for row, child in enumerate(node.children):
                child.row = row
    return root


class VersionTree(QAbstractItemModel):
    selection_changed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.root = None

    def replace(self, root):
        self.beginResetModel()
        self.root = root
        self.endResetModel()
        self.selection_changed.emit()

    def columnCount(self, parent=QModelIndex()):
        return 2

    def rowCount(self, parent=QModelIndex()):
        if self.root is None:
            return 0
        if not parent.isValid():
            return 1
        return len(parent.internalPointer().children) if parent.column()==0 else 0

    def index(self, row, col, parent=QModelIndex()):
        if not self.hasIndex(row,col,parent):
            return QModelIndex()
        node = parent.internalPointer().children[row] if parent.isValid() else self.root
        return self.createIndex(row,col,node)

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        up = index.internalPointer().parent
        return self.createIndex(up.row,0,up) if up is not None else QModelIndex()

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("版本目录树", "大小")[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.path or "整个版本"
        if role == Qt.ItemDataRole.CheckStateRole and index.column()==0:
            return node.state
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column()==0:
                return PurePosixPath(node.path).name if node.path else "整个版本"
            return size_text(node.size) if node.size is not None else ""

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column()==0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role):
        if not index.isValid() or index.column()!=0 or role!=Qt.ItemDataRole.CheckStateRole:
            return False
        selected = Qt.CheckState(value)
        if selected not in (Qt.CheckState.Checked, Qt.CheckState.Unchecked):
            return False
        node = index.internalPointer()
        work = [node]
        while work:
            next_node = work.pop()
            next_node.state = selected
            work.extend(next_node.children)
        node = node.parent
        while node is not None:
            states = {child.state for child in node.children}
            node.state = states.pop() if len(states)==1 else Qt.CheckState.PartiallyChecked
            node = node.parent
        self.layoutChanged.emit()
        self.selection_changed.emit()
        return True

    def selected_paths(self):
        if self.root is None:
            return set()
        if self.root.state==Qt.CheckState.Checked:
            return None
        result = set()
        work = [self.root]
        while work:
            node = work.pop()
            if node.state==Qt.CheckState.Checked:
                result.add(node.path)
            elif node.state==Qt.CheckState.PartiallyChecked:
                work.extend(node.children)
        return result
