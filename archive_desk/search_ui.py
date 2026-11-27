"""档案分页检索、标注与条目详情。检索和根目录探测均在工作线程中进行。"""
import re
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QScrollArea, QSplitter, QVBoxLayout, QWidget,
)
from .catalog_widgets import button, records_table
from .file_model import modified_text, size_text
from .jobs import Job
from .text_widgets import PathLabel


def availability(entry):
    if not entry["active"]:
        return "历史位置"
    if not entry["online"]:
        return "离线"
    return "待复核" if entry["stale"] else "在线"


class SearchFilters(QDialog):
    def __init__(self,parent,values):
        super().__init__(parent)
        self.setWindowTitle("筛选档案")
        self.resize(410,360)
        self.values = {}
        self.fields = {}
        layout = QVBoxLayout(self)
        form = QFormLayout()
        for key,title,hint in (("ext","扩展名",".pdf"),("date_from","修改日期从","YYYY-MM-DD"),
                               ("date_to","至","YYYY-MM-DD"),("min_size","最小字节数","不限"),
                               ("max_size","最大字节数","不限"),("tag","标签","标签名称")):
            edit = QLineEdit(str(values.get(key,"")))
            edit.setObjectName(key)
            edit.setPlaceholderText(hint)
            self.fields[key] = edit
            form.addRow(title,edit)
        self.online = QComboBox()
        self.online.addItem("全部",None)
        self.online.addItem("在线",True)
        self.online.addItem("离线",False)
        self.online.setCurrentIndex(max(0,self.online.findData(values.get("online"))))
        form.addRow("盘符状态",self.online)
        self.favorite = QCheckBox("仅显示收藏")
        self.favorite.setChecked(bool(values.get("favorite")))
        form.addRow("",self.favorite)
        layout.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("应用筛选")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def save(self):
        data = {key:field.text().strip() for key,field in self.fields.items() if field.text().strip()}
        try:
            for key in ("min_size","max_size"):
                if key in data:
                    if not data[key].isascii() or not data[key].isdigit():
                        raise ValueError("文件大小应为非负整数字节数。")
                    data[key] = int(data[key])
                    if data[key] > 2**63-1:
                        raise ValueError("文件大小超过可检索范围。")
            for key in ("date_from","date_to"):
                if key in data and date.fromisoformat(data[key]).isoformat()!=data[key]:
                    raise ValueError("请输入 YYYY-MM-DD 格式的日期。")
            if data.get("min_size",0)>data.get("max_size",2**63-1):
                raise ValueError("最小字节数超过最大字节数。")
            if data.get("date_to") and data.get("date_from","")>data["date_to"]:
                raise ValueError("起始日期晚于截止日期。")
        except ValueError as exc:
            QMessageBox.warning(self,"请检查筛选条件",str(exc))
            return
        if self.online.currentData() is not None:
            data["online"] = self.online.currentData()
        if self.favorite.isChecked():
            data["favorite"] = True
        self.values = data
        self.accept()


class SearchTab(QWidget):
    stopped = Signal()
    PAGE_SIZE = 100

    def __init__(self,page,store):
        super().__init__(page)
        self.page = page
        self.store = store
        self.conditions = {}
        self.page_no = 1
        self.generation = 0
        self.pinned = None
        self.job = None
        self.closing = False
        self.preserve_entry = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._launch)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12,12,12,10)
        top = QHBoxLayout()
        self.keyword = QLineEdit()
        self.keyword.setObjectName("archiveQuery")
        self.keyword.setPlaceholderText("搜索文件名或相对路径")
        self.keyword.setClearButtonEnabled(True)
        self.keyword.textChanged.connect(self.criteria_changed)
        top.addWidget(self.keyword,1)
        self.filter_button = button("筛选…","archiveFilters",self.filters_dialog)
        top.addWidget(self.filter_button)
        self.saved = QComboBox()
        self.saved.setObjectName("savedQueries")
        self.saved.activated.connect(self.load_saved)
        top.addWidget(self.saved)
        top.addWidget(button("保存搜索…","saveQuery",self.save_search))
        layout.addLayout(top)
        controls = QHBoxLayout()
        self.summary = QLabel("输入关键词或选择档案库。")
        self.summary.setObjectName("searchSummary")
        self.summary.setWordWrap(True)
        controls.addWidget(self.summary,1)
        self.back = button("返回检索","returnSearch",self.criteria_changed)
        self.back.hide()
        controls.addWidget(self.back)
        self.clear_group = button("清除分组","clearArchiveGroup",self.release_group)
        self.clear_group.hide()
        controls.addWidget(self.clear_group)
        self.tag_button = button("给选中项加标签…","addArchiveTags",self.add_tags)
        controls.addWidget(self.tag_button)
        layout.addLayout(controls)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        columns = [("文件名",lambda r:r["name"],170),("档案库",lambda r:r["root_name"],110),
                   ("相对路径",lambda r:r["relpath"],None),("大小",lambda r:size_text(r["size"]),80),
                   ("状态",availability,70)]
        self.table,self.model = records_table(self,columns,"archiveTable",extended=True)
        splitter.addWidget(self.table)
        card = QFrame()
        card.setObjectName("detailPage")
        card.setMinimumWidth(210)
        box = QVBoxLayout(card)
        box.setContentsMargins(12,0,1,2)
        self.detail_title = QLabel("档案详情")
        self.detail_title.setObjectName("panelTitle")
        self.detail_title.setWordWrap(True)
        self.detail_title.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.detail_title)
        self.detail_info = PathLabel()
        self.detail_info.setText("选择一条记录，查看位置与标注。")
        box.addWidget(self.detail_info)
        self.favorite = QCheckBox("收藏")
        self.favorite.setObjectName("archiveFavorite")
        box.addWidget(self.favorite)
        self.note = QPlainTextEdit()
        self.note.setObjectName("archiveNote")
        self.note.setPlaceholderText("此文件的备注")
        self.note.setFixedHeight(67)
        box.addWidget(self.note)
        self.save_note = button("保存备注与收藏","saveAnnotation",self.annotate)
        box.addWidget(self.save_note)
        self.open_button = button("打开所在目录","openArchiveFolder",self.open_folder)
        box.addWidget(self.open_button)
        self.content_button = button("预览文件内容","archiveContentPreview",self.preview_content)
        box.addWidget(self.content_button)
        self.preview_button = button("送入整理预览","previewArchiveEntry",self.send_preview)
        box.addWidget(self.preview_button)
        self.history_button = button("查看关联历史","archiveHistory",self.open_history)
        box.addWidget(self.history_button)
        box.addStretch()
        scroll = QScrollArea()
        scroll.setWidget(card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0,1)
        splitter.setSizes([790,258])
        layout.addWidget(splitter,1)
        bottom = QHBoxLayout()
        self.page_label = QLabel()
        bottom.addWidget(self.page_label)
        bottom.addStretch()
        self.prev = button("上一页","archivePrev",lambda:self.turn(-1))
        self.next = button("下一页","archiveNext",lambda:self.turn(1))
        bottom.addWidget(self.prev)
        bottom.addWidget(self.next)
        layout.addLayout(bottom)
        self.table.selectionModel().selectionChanged.connect(self.set_detail)
        self.set_detail()
        self.prev.setEnabled(False)
        self.next.setEnabled(False)
        self.reload_saved()

    def reload_saved(self):
        self.saved_data = self.store.saved_searches()
        self.saved.clear()
        self.saved.addItem("常用搜索…",None)
        for name in self.saved_data:
            self.saved.addItem(name,name)

    def filters(self):
        values = dict(self.conditions)
        if self.page.root_id is not None:
            values["root_id"] = self.page.root_id
        if self.keyword.text().strip():
            values["text"] = self.keyword.text().strip()
        return values

    def criteria_changed(self,*_):
        self.pinned = None
        self.page_no = 1
        self.back.hide()
        self.queue()

    def filters_dialog(self):
        dialog = SearchFilters(self,self.conditions)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            group = {k:v for k,v in self.conditions.items() if k in ("ext_exact","directory_bucket")}
            self.conditions = {**group,**dialog.values}
            self.criteria_changed()

    def save_search(self):
        name,ok = QInputDialog.getText(self,"保存常用搜索","搜索名称")
        if ok and name.strip():
            try:
                self.store.save_search(name.strip(),self.filters())
                self.reload_saved()
                self.page.message(f"已保存搜索「{name.strip()}」。")
            except Exception as exc:
                self.page.message(f"保存搜索失败：{exc}")

    def load_saved(self,index):
        name = self.saved.itemData(index)
        if name is None:
            return
        self.use_filters(self.saved_data[name])

    def release_group(self):
        self.conditions.pop("ext_exact",None)
        self.conditions.pop("directory_bucket",None)
        self.criteria_changed()

    def use_filters(self,values):
        filters = dict(values)
        self.page.select_root(filters.pop("root_id",None))
        self.keyword.setText(filters.pop("text",""))
        self.conditions = filters
        self.criteria_changed()

    def turn(self,delta):
        self.page_no = max(1,self.page_no+delta)
        self.queue()

    def queue(self,delay=150):
        if self.closing:
            return
        self.generation += 1
        self.timer.start(delay)
        self.filter_button.setText(f"筛选 ({len(self.conditions)})…" if self.conditions else "筛选…")
        self.clear_group.setVisible("ext_exact" in self.conditions or "directory_bucket" in self.conditions)

    def _launch(self):
        if self.closing or self.job:
            return
        filters, number, pin = self.filters(), self.page_no, self.pinned
        self.sent_generation = self.generation
        def fetch():
            for root in self.page.registry.roots():
                self.page.registry.probe(root["id"])
            if pin is not None:
                row = self.store.detail(pin)
                rows = [row] if row else []
                data = dict(rows=rows,total=len(rows),unindexed=0,offline=0)
            else:
                data = self.store.search(filters,page=number,page_size=self.PAGE_SIZE)
            return data,self.page.registry.roots()
        self.job = Job(self,fetch)
        self.job.finished.connect(self._received)
        self.job.start()

    def _received(self):
        job = self.job
        self.job = None
        job.deleteLater()
        if self.closing:
            self.stopped.emit()
            return
        if self.sent_generation!=self.generation:
            self.timer.stop()
            self._launch()
            return
        if job.error:
            self.model.replace([])
            self.summary.setText(f"检索失败：{job.error}")
            self.set_detail()
            return
        data, roots = job.result
        self.page.update_root_status(roots)
        total = data["total"]
        pages = max(1,(total+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        if self.page_no>pages:
            self.page_no = pages
            self.queue(0)
            return
        self.model.replace(data["rows"])
        self.page_label.setText(f"第 {self.page_no} / {pages} 页　·　每页 {self.PAGE_SIZE} 项")
        self.prev.setEnabled(self.page_no>1)
        self.next.setEnabled(self.page_no<pages)
        suffix = []
        if data["unindexed"]:
            suffix.append(f"{data['unindexed']} 个库尚未索引")
        if data["offline"]:
            suffix.append(f"{data['offline']} 个库当前离线，显示上次索引")
        title = f"找到 {total:,} 条记录" if total else "无匹配记录"
        if not roots:
            title = "尚无档案库。请先添加文件夹并刷新索引。"
        if "ext_exact" in self.conditions:
            suffix.insert(0,self.conditions["ext_exact"] or "无扩展名")
        if "directory_bucket" in self.conditions:
            bucket = self.conditions["directory_bucket"]
            suffix.insert(0,f"第 {bucket['depth']} 层 · {bucket['path'] or '根目录文件'}")
        self.summary.setText("　·　".join([title,*suffix]))
        if data["rows"]:
            index = next((i for i,r in enumerate(data["rows"]) if r["id"]==self.preserve_entry),0)
            self.table.selectRow(index)
        self.preserve_entry = None
        self.set_detail()

    def current(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        row = self.table.currentIndex().row()
        if not any(i.row()==row for i in rows):
            row = rows[0].row()
        return self.model.records[row] if 0<=row<len(self.model.records) else None

    def set_detail(self,*_):
        entry = self.current()
        self.tag_button.setEnabled(entry is not None)
        for control in (self.note,self.favorite,self.save_note,self.history_button):
            control.setEnabled(entry is not None)
        can_open = bool(entry and entry["online"] and entry["active"] and not entry["stale"])
        self.open_button.setVisible(can_open)
        self.preview_button.setVisible(can_open)
        self.content_button.setVisible(can_open)
        self.favorite.setChecked(bool(entry and entry["favorite"]))
        self.note.setPlainText(entry["note"] if entry else "")
        if not entry:
            self.detail_title.setText("档案详情")
            self.detail_info.setText("选择一条记录，查看位置与标注。")
            return
        self.detail_title.setText(entry["name"])
        label = "当前位置" if can_open else "上次见到的位置"
        path = str(Path(entry["root_path"])/entry["relpath"])
        tags = "、".join(entry["tags"]) or "无"
        self.detail_info.setText(f"{label}\n{path}\n\n修改时间　{modified_text(entry['mtime_ns'])}"
                                 f"\n大小　{entry['size']:,} 字节\n标签　{tags}"
                                 f"\nSHA-256　{entry['digest'] or '未计算'}")
        self.detail_info.setToolTip(self.detail_info.text())

    def annotate(self):
        entry = self.current()
        if entry:
            try:
                self.store.annotate(entry["id"],note=self.note.toPlainText(),favorite=self.favorite.isChecked())
                self.preserve_entry = entry["id"]
                self.queue(0)
            except Exception as exc:
                self.page.message(f"标注保存失败：{exc}")

    def add_tags(self):
        ids = [self.model.records[i.row()]["id"] for i in self.table.selectionModel().selectedRows()]
        text,ok = QInputDialog.getText(self,"添加标签",f"为选中的 {len(ids)} 条档案加标签（多个用逗号分隔）")
        if ok:
            tags = [s.strip() for s in re.split("[,，]",text) if s.strip()]
            if tags:
                self.page.task("正在保存标签…",lambda _:self.store.add_tags(ids,tags),
                               lambda _:self.queue(0),cancellable=False)

    def open_folder(self):
        entry = self.current()
        if entry:
            self.page.open_entry(entry, preview=False)

    def send_preview(self):
        entry = self.current()
        if entry:
            self.page.open_entry(entry, preview=True)

    def preview_content(self):
        entry = self.current()
        if entry:
            self.page.preview_content(entry)

    def open_history(self):
        entry = self.current()
        if entry:
            self.page.history_for(entry["id"])

    def locate(self,entry_id):
        self.pinned = entry_id
        self.page_no = 1
        self.back.show()
        self.queue(0)

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        return self.job is None
