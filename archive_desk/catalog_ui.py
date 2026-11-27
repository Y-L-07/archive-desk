"""多库索引管理、跨库检索和命名快照页。"""
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QPlainTextEdit, QSizePolicy, QTabWidget, QVBoxLayout,
)
from .catalog import Catalog
from .catalog_search import CatalogSearch
from .catalog_widgets import button, local_time
from .scanner import snapshot_file
from .search_ui import SearchTab
from .snapshots_ui import SnapshotsTab
from .stats_ui import StatsTab


STATES = {"never":"尚未索引", "running":"正在索引", "complete":"完整索引", "partial":"部分覆盖",
          "cancelled":"上次扫描已取消", "failed":"上次扫描失败"}


class CatalogPage(QFrame):
    stopped = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("center")
        self.window = window
        self.db_path = window.journal.path
        self.registry = Catalog(self.db_path)
        self.store = CatalogSearch(self.db_path)
        self.root_id = None
        self.roots = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20,16,20,12)
        layout.setSpacing(10)
        header = QHBoxLayout()
        title = QLabel("档案库")
        title.setObjectName("title")
        header.addWidget(title)
        self.selector = QComboBox()
        self.selector.setMinimumWidth(145)
        self.selector.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self.selector.setObjectName("archiveRoots")
        self.selector.currentIndexChanged.connect(self.root_changed)
        header.addWidget(self.selector,1)
        header.addWidget(button("添加库…","addArchiveRoot",self.add))
        self.rebind_button = button("重新绑定…","rebindArchive",self.rebind)
        header.addWidget(self.rebind_button)
        self.hash = QCheckBox("计算 SHA-256")
        self.hash.setObjectName("archiveHash")
        self.hash.setToolTip("刷新时按需计算内容哈希；证据仍有效的已知哈希可复用。")
        header.addWidget(self.hash)
        self.refresh_button = button("刷新索引","refreshArchive",self.refresh)
        header.addWidget(self.refresh_button)
        header.addWidget(button("检查重复件","catalogDuplicates",self.check_duplicates))
        header.addWidget(button("生成两库演示","archiveDemo",self.demo))
        layout.addLayout(header)
        info = QHBoxLayout()
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("archiveStatus")
        info.addWidget(self.status,1)
        self.issues_button = button("扫描提示…","archiveIssues",self.issues)
        info.addWidget(self.issues_button)
        layout.addLayout(info)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("archiveTabs")
        self.search = SearchTab(self,self.store)
        self.search.stopped.connect(self.stopped)
        self.snaps = SnapshotsTab(self,self.db_path)
        self.tabs.addTab(self.search,"检索与标注")
        self.tabs.addTab(self.snaps,"快照比对")
        self.stats = StatsTab(self,self.db_path)
        self.tabs.addTab(self.stats,"空间统计")
        self.tabs.currentChanged.connect(self.tab_changed)
        layout.addWidget(self.tabs,1)
        self.reload()

    def tab_changed(self,index):
        if self.tabs.widget(index) is self.stats and self.stats.dirty:
            self.stats.compute()

    def message(self,text):
        self.window._note(text)

    def task(self,description,work,done,*,cancellable=True):
        if self.window.job is not None:
            return
        cancel = Event() if cancellable else None
        def interrupted(stopped,error):
            self.reload()
            self.message("已取消档案任务，已发布的索引和快照仍可查阅。" if stopped else
                         f"档案任务未完成：{error}")
        self.window._run(lambda:work(cancel),done,description,cancel,interrupted=interrupted)

    def reload(self, preferred=None):
        rid = self.root_id if preferred is None else preferred
        rows = self.registry.roots()
        self.selector.blockSignals(True)
        self.selector.clear()
        self.selector.addItem("全部档案库",None)
        for row in rows:
            self.selector.addItem(row["name"],row["id"])
            self.selector.setItemData(self.selector.count()-1,row["path"],Qt.ItemDataRole.ToolTipRole)
        self.selector.setCurrentIndex(max(0,self.selector.findData(rid)))
        self.selector.blockSignals(False)
        self.roots = rows
        self.root_changed()

    def select_root(self,rid):
        self.selector.setCurrentIndex(max(0,self.selector.findData(rid)))

    def root_changed(self):
        self.root_id = self.selector.currentData()
        self.update_root_status(self.roots)
        self.snaps.reload(self.root_id)
        self.stats.reset()
        self.search.criteria_changed()

    def update_root_status(self,roots):
        self.roots = roots
        record = next((r for r in roots if r["id"]==self.root_id),None)
        self.rebind_button.setEnabled(record is not None)
        self.refresh_button.setEnabled(record is not None)
        self.hash.setEnabled(record is not None)
        self.issues_button.setVisible(bool(record and record["issue_count"]))
        if not record:
            online = sum(bool(r["online"]) for r in roots)
            self.status.setText(f"{len(roots)} 个档案库　·　在线 {online}　·　离线 {len(roots)-online}")
            return
        state = STATES.get(record["scan_state"],record["scan_state"])
        availability = "在线" if record["online"] else "离线 · 上次见到的位置"
        self.status.setText(f"{availability}　{record['path']}\n{state}　·　{record['file_count']:,} 项　·　"
                            f"最近索引 {local_time(record['indexed_at'])}　·　"
                            f"上次完整索引 {local_time(record['last_complete_at'])}"
                            + (f"　·　{record['issue_count']} 条扫描提示" if record["issue_count"] else ""))
        self.status.setToolTip(record.get("problem") or record["path"])

    def add(self):
        directory = QFileDialog.getExistingDirectory(self,"选择档案库根目录","",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if not directory:
            return
        name,ok = QInputDialog.getText(self,"添加档案库","库名称",text=Path(directory).name)
        if ok and name.strip():
            self.task("正在绑定档案库…",lambda _:self.registry.add_root(name.strip(),Path(directory)),
                      lambda rid:self.reload(rid),cancellable=False)

    def rebind(self):
        record = next((r for r in self.roots if r["id"]==self.root_id),None)
        if not record:
            return
        path = QFileDialog.getExistingDirectory(self,"选择原档案库的新路径","",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if not path:
            return
        msg = f"档案库：{record['name']}\n原位置：{record['path']}\n新位置：{path}\n\n将核对卷与根目录身份后更新绑定。"
        if self.window.confirm("确认重新绑定档案库",msg,"确认重绑"):
            self.task("正在核验新绑定…",
                lambda _:self.registry.rebind(record["id"],Path(path),confirmed=True),
                lambda _:self.reload(record["id"]),cancellable=False)

    def refresh(self):
        rid = self.root_id
        if rid is None:
            return
        hash_files = self.hash.isChecked()
        last = 0.0
        def progress(count,path):
            nonlocal last
            if monotonic()>=last:
                self.window.progress_event.emit(count,0,str(path))
                last = monotonic()+.10
        def completed(row):
            counts = row["counts"]
            self.reload(rid)
            self.message(f"索引刷新完成。观察 {counts['seen']:,} 项，计算哈希 {counts['hashed']:,} 项，"
                         f"复用哈希 {counts['reused']:,} 项，提示 {counts['issues']:,} 条。")
        self.task("正在刷新档案索引…",lambda cancel:self.registry.refresh(rid,hash_files=hash_files,
                  progress=progress,cancel=cancel),completed)

    def issues(self):
        if self.root_id is None:
            return
        def show(rows):
            dialog = QDialog(self)
            dialog.setWindowTitle("索引覆盖与扫描提示")
            dialog.resize(650,375)
            box = QVBoxLayout(dialog)
            text = QPlainTextEdit()
            text.setReadOnly(True)
            text.setPlainText("\n".join(f"{r['relpath'] or '.'}　{r['reason']}" for r in rows))
            box.addWidget(text)
            box.addWidget(button("关闭","closeIssues",dialog.accept))
            dialog.exec()
        rid = self.root_id
        self.task("正在读取扫描提示…",lambda _:self.registry.issues(rid),show,cancellable=False)

    def demo(self):
        from .catalog_demo import create_catalog_demo
        def completed(data):
            self.reload(data["root_id"])
            self.snaps.reload(data["root_id"],data["old_id"],data["new_id"])
            self.tabs.setCurrentWidget(self.snaps)
            self.message(f"演示目录：{data['directory']}。已选中同一库的两轮快照，可点击比较。")
        self.task("正在生成独立演示档案库及快照…",lambda cancel:create_catalog_demo(
            self.db_path,cancel=cancel,progress=lambda msg:self.window.progress_event.emit(0,0,str(msg))),completed)

    def open_entry(self,entry,preview):
        def inspect(cancel):
            parent = self.store.folder_for(entry["id"])
            if not preview:
                return parent
            file = snapshot_file(parent/entry["name"],cancel)
            if (str(file.dev)!=entry["dev"] or str(file.ino)!=entry["ino"] or
                    file.size!=entry["size"] or file.mtime_ns!=entry["mtime_ns"]):
                raise ValueError("文件与档案索引不一致，请先刷新该库。")
            return file
        def opened(result):
            if preview:
                self.window._load_candidates(Path(entry["root_path"]),[result],"档案库")
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(result))):
                self.message("Windows 无法打开此目录。")
        self.task("正在核对档案位置…",inspect,opened)

    def open_from_item(self,item_id):
        def show(rows):
            if not rows:
                self.message("该历史项尚无关联档案记录。")
                return
            choice = rows[0]
            if len(rows)>1:
                names = [f"{r['root_name']}　{r['relpath']}　(#{r['id']})" for r in rows]
                text,ok = QInputDialog.getItem(self,"关联档案","选择条目",names,editable=False)
                if not ok:
                    return
                choice = rows[names.index(text)]
            self.window.switch_page(3)
            self.select_root(choice["root_id"])
            self.tabs.setCurrentWidget(self.search)
            self.search.locate(choice["id"])
        self.task("正在查找关联档案…",lambda _:self.store.entries_for_item(item_id),show,cancellable=False)

    def preview_content(self,entry):
        from .content_preview import read_preview, show_preview
        def work(_):
            path = self.store.folder_for(entry["id"]) / entry["name"]
            return path,read_preview(path,entry)
        self.task("正在加载文件预览…",work,lambda result:show_preview(self,*result))

    def check_duplicates(self):
        from contextlib import ExitStack
        from .backup_paths import pin_path
        from .duplicates_ui import show_duplicates
        from .scanner import scan
        record = next((r for r in self.roots if r["id"] == self.root_id),None)
        if not record:
            self.message("请先选择要检查重复件的档案库。")
            return
        root = Path(record["path"])
        def work(cancel):
            with ExitStack() as stack:
                identity = pin_path(stack,root)
                if tuple(map(str,identity)) != (record["dev"],record["ino"]):
                    raise ValueError("档案库的目录身份已变化")
                return scan(root,recursive=True,cancel=cancel)
        def ready(files):
            self.message(f"扫描到 {len(files):,} 项，有 {len(files.warnings):,} 条读取提示。")
            groups = show_duplicates(self,root,files) if files else None
            if groups:
                self.window.load_quarantine(root,files,groups)
        self.task("正在扫描档案库以检查重复件…",work,ready)

    def history_for(self,entry_id):
        def show(rows):
            if not rows:
                self.message("该档案尚无关联整理历史。")
                return
            choice = rows[0]
            if len(rows)>1:
                names = [f"批次 #{r['batch_id']}　{r['source']} → {r['target']}　(#{r['id']})" for r in rows]
                text,ok = QInputDialog.getItem(self,"关联历史","选择操作",names,editable=False)
                if not ok:
                    return
                choice = rows[names.index(text)]
            self.window.show_history_item(choice["batch_id"],choice["id"])
        self.task("正在查找关联历史…",lambda _:self.store.history_for_entry(entry_id),show,cancellable=False)
