"""按已发布的档案清单统计占用，并跳转到精确对应的检索条件。"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget,
)

from .catalog_stats import CatalogStats
from .catalog_widgets import button, records_table
from .file_model import size_text


class ShareBar(QStyledItemDelegate):
    def paint(self, painter, option, index):
        # Preserve the native row-selection background before painting the bar.
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt,index)
        opt.text = ""
        style = opt.widget.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem,opt,painter,opt.widget)
        row = index.model().records[index.row()]
        fraction = row["fraction"]
        bounds = option.rect.adjusted(8,4,-6,-4)
        label_width = 56
        width = max(0,bounds.width()-label_width-6)
        bar = QRectF(bounds.left(),bounds.center().y()-3,width,6)
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#deeadf"))
        painter.drawRoundedRect(bar,3,3)
        bar.setWidth(width*fraction)
        painter.setBrush(QColor("#518769"))
        painter.drawRoundedRect(bar,3,3)
        painter.setPen(QColor("#354f40"))
        painter.drawText(bounds,Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         f"{fraction:.1%}")
        painter.restore()


class StatsTab(QWidget):
    PAGE_SIZE = 100

    def __init__(self,page,db_path):
        super().__init__(page)
        self.page = page
        self.store = CatalogStats(db_path)
        self.number = 1
        self.dirty = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12,12,12,10)
        top = QHBoxLayout()
        top.addWidget(QLabel("分组"))
        self.group = QComboBox()
        self.group.setObjectName("spaceGroup")
        self.group.addItem("扩展名","extension")
        self.group.addItem("目录","directory")
        top.addWidget(self.group)
        self.depth = QSpinBox()
        self.depth.setObjectName("spaceDepth")
        self.depth.setRange(1,32)
        self.depth.setSuffix(" 层")
        self.depth.setAccessibleName("目录层级")
        top.addWidget(self.depth)
        top.addWidget(button("计算统计","computeSpace",self.compute))
        top.addStretch()
        self.open = button("查看该组文件","filesFromSpace",self.drill)
        top.addWidget(self.open)
        layout.addLayout(top)
        self.summary = QLabel("")
        self.summary.setObjectName("spaceSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        columns = [("分组",lambda r:r["label"],None),
                   ("档案库",lambda r:r["scope_name"],170),
                   ("文件数",lambda r:f"{r['count']:,}",95),
                   ("总字节数",lambda r:f"{r['bytes']:,}",145),
                   ("占比",lambda r:f"{r['fraction']:.1%}",160)]
        self.table,self.model = records_table(self,columns,"spaceTable")
        self.table.setItemDelegateForColumn(4,ShareBar(self.table))
        self.table.doubleClicked.connect(self.drill)
        self.table.selectionModel().selectionChanged.connect(self.selection)
        layout.addWidget(self.table,1)
        foot = QHBoxLayout()
        self.page_info = QLabel()
        foot.addWidget(self.page_info)
        foot.addStretch()
        self.prev = button("上一页","spacePrev",lambda:self.turn(-1))
        self.next = button("下一页","spaceNext",lambda:self.turn(1))
        foot.addWidget(self.prev)
        foot.addWidget(self.next)
        layout.addLayout(foot)
        self.group.currentIndexChanged.connect(self.reset)
        self.depth.valueChanged.connect(self.reset)
        self.reset()

    def reset(self,*_):
        self.dirty = True
        self.number = 1
        self.model.replace([])
        self.open.setEnabled(False)
        self.prev.setEnabled(False)
        self.next.setEnabled(False)
        self.page_info.setText("")
        self.depth.setEnabled(self.group.currentData()=="directory")
        self.summary.setText("统计使用最近发布的索引。选择分组方式后可计算各组文件数与字节数。")

    def selection(self,*_):
        self.open.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def turn(self,delta):
        self.number = max(1,self.number+delta)
        self.compute()

    def compute(self):
        grouping = self.group.currentData()
        rid = self.page.root_id
        depth,number = self.depth.value(),self.number
        def show(result):
            self.dirty = False
            size = result["bytes"]
            roots = {r["id"]:r["name"] for r in self.page.roots}
            rows = [dict(row,fraction=(row["bytes"]/size if size else 0.0),
                         scope_name=roots.get(row["root_id"],"全部档案库")) for row in result["rows"]]
            self.model.replace(rows)
            if rows:
                self.table.selectRow(0)
            self.selection()
            pages = max(1,(result["total"]+self.PAGE_SIZE-1)//self.PAGE_SIZE)
            self.prev.setEnabled(number>1)
            self.next.setEnabled(number<pages)
            self.page_info.setText(f"第 {number} / {pages} 页　·　共 {result['total']:,} 个分组")
            details = [f"已索引 {result['files']:,} 项",f"合计 {size:,} 字节（{size_text(size)}）"]
            if result["offline"]:
                details.append(f"离线库 {result['offline']} 个，采用上次记录")
            if result["unindexed"]:
                details.append(f"未索引 {result['unindexed']} 个库")
            self.summary.setText("　·　".join(details))
        self.page.task("正在汇总空间占用…",lambda cancel:self.store.group(
            grouping,root_id=rid,depth=depth,page=number,page_size=self.PAGE_SIZE,cancel=cancel),show)

    def drill(self,*_):
        indices = self.table.selectionModel().selectedRows()
        if indices:
            filters = self.model.records[indices[0].row()]["filters"]
            self.page.tabs.setCurrentWidget(self.page.search)
            self.page.search.use_filters(filters)
