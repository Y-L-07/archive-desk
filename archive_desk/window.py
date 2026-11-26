"""归档台桌面工作台。文件操作只由 operations 模块执行。"""
import sqlite3
from collections import Counter
from dataclasses import replace
from math import ceil
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QTableView, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from . import operations
from .jobs import Job
from .inbox_ui import InboxPage
from .core import ScanCancelled, default_rules
from .demo import create_demo
from .planning import preview, recheck_plans
from .quarantine import quarantine_preview
from .profiles import ProfileStore
from .scanner import scan, snapshot_file
from .file_model import CONFLICTS, FileFilter, FileModel, STATUS, STATUS_COLORS, TYPES, size_text, type_icon
from .rules_dialog import RulesDialog
from .theme import STYLE, icon


def label(text="", name="", wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, slot, name="", glyph=None, color="#507467"):
    widget = QPushButton(text)
    widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if glyph:
        widget.setIcon(icon(glyph, color))
        widget.setIconSize(QSize(17, 17))
    widget.clicked.connect(slot)
    return widget


def panel(name, margins, spacing=10, horizontal=False):
    widget = QFrame()
    widget.setObjectName(name)
    layout = QHBoxLayout(widget) if horizontal else QVBoxLayout(widget)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return widget, layout


def separator():
    widget = QFrame()
    widget.setObjectName("line")
    widget.setFixedHeight(1)
    return widget


class PathLabel(QTextEdit):
    """长路径可在任意字符处折行，复制时保留原文。"""
    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.document().setDocumentMargin(0)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, size):
        self.setFixedHeight(ceil(size.height()) + 2)

    def setText(self, text):
        self.setPlainText(text)
        self.setToolTip(text)


class MainWindow(QMainWindow):
    progress_event = Signal(int, int, str)
    stats_event = Signal(object)

    def __init__(self, journal, recovery_count=0):
        super().__init__()
        self.journal = journal
        self.root = self.target_root = None
        self.resolutions = {}
        self.user_skipped = set()
        self.files = []
        self.profiles = ProfileStore(journal.path)
        config_message = ""
        try:
            self.rules = self.profiles.load_active()
        except (ValueError, sqlite3.Error, OSError) as exc:
            self.rules = default_rules()
            config_message = f"工作规则读取失败，当前使用内置规则。{exc}"
        self.category = "全部文件"
        self._on_invalidate = None
        self.job = self.cancel_event = self.active_batch = None
        self.needs_scan = False
        self.counts = Counter()
        self.visible_ready = set()
        self.quarantine_groups = ()
        self.recovery_count = recovery_count
        self.setWindowTitle("归档台")
        self.setMinimumSize(960, 590)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1400, screen.width()-24), min(835, screen.height()-62))
        self.setStyleSheet(STYLE)
        container, base = panel("base", (0,0,0,0), 0)
        self.setCentralWidget(container)
        base.addWidget(self._header())
        self.notice = label("", "notice", True)
        self.notice.hide()
        base.addWidget(self.notice)
        self.progress_panel, progress_row = panel("field", (17,5,17,5), 10, True)
        self.progress_label = label("","muted")
        self.progress_label.setMinimumWidth(0)
        self.progress_label.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        progress_row.addWidget(self.progress_label,1)
        self.stats_label = label("","muted")
        progress_row.addWidget(self.stats_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(140)
        self.progress_bar.setRange(0,0)
        progress_row.addWidget(self.progress_bar)
        self.stop_button = button("取消任务",self._cancel_scan,"stopOperationButton")
        progress_row.addWidget(self.stop_button)
        self.progress_panel.hide()
        base.addWidget(self.progress_panel)
        self.progress_event.connect(self._show_progress)
        self.stats_event.connect(self._show_stats)
        self.pages = QStackedWidget()
        self.workspace = QWidget()
        workspace_layout = QVBoxLayout(self.workspace)
        workspace_layout.setContentsMargins(0,0,0,0)
        workspace_layout.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        body.addWidget(self._sidebar())
        body.addWidget(self._center(), 1)
        body.addWidget(self._inspector())
        workspace_layout.addLayout(body, 1)
        workspace_layout.addWidget(self._footer())
        self.pages.addWidget(self.workspace)
        self.pages.addWidget(self._history())
        self.inbox = InboxPage(self, journal.path)
        self.inbox.preview_requested.connect(self._load_inbox)
        self.inbox.count_changed.connect(lambda count:
            self.inbox_button.setText(f"收件箱 ({count})" if count else "收件箱"))
        self.inbox.stopped.connect(self.close)
        self.pages.addWidget(self.inbox)
        base.addWidget(self.pages, 1)
        self._render_rules()
        self._update_table()
        self._show_detail()
        self._sync_controls()
        if config_message:
            self._note(config_message)
        if recovery_count:
            self.switch_page(1)
            self._note(f"上次运行有 {recovery_count} 项操作未确认。请在历史中对照原位置和目标位置手动核对。")

    def start_recovery(self):
        def inspect():
            count = self.journal.recover(rules=list(self.rules))
            outstanding = [b for b in self.journal.batches() if b["resumable"] or b["uncertain"]]
            return count, outstanding
        def recovered(result):
            count, outstanding = result
            if outstanding:
                self.active_batch = outstanding[0]["id"]
                self.switch_page(1)
                safe = sum(b["resumable"] for b in outstanding)
                manual = sum(b["uncertain"] for b in outstanding)
                self._note(f"中断批次已核对。可安全继续 {safe} 项，待人工确认 {manual} 项。")
            elif count:
                self.switch_page(1)
                self._note(f"已核对上次中断的 {count} 项操作，请查看历史。")
        self._run(inspect, recovered, "正在核对上次未完成的操作…")

    def _header(self):
        frame, row = panel("top", (18,11,18,11), 8, True)
        mark = QLabel()
        mark.setPixmap(icon("archive", "#e0edda", 29).pixmap(QSize(29,29)))
        row.addWidget(mark)
        row.addWidget(label("归档台", "brand"))
        row.addSpacing(24)
        self.workspace_button = button("整理工作台", lambda: self.switch_page(0), "workspaceButton")
        self.history_button = button("历史", lambda: self.switch_page(1), "historyButton", "history", "#d2e5d6")
        self.inbox_button = button("收件箱", lambda: self.switch_page(2), "inboxButton", "archive", "#d2e5d6")
        group = QButtonGroup(frame)
        for widget in (self.workspace_button, self.inbox_button, self.history_button):
            widget.setCheckable(True)
            group.addButton(widget)
            row.addWidget(widget)
        self.workspace_button.setChecked(True)
        row.addStretch()
        self.demo_button = button("创建演示目录", self.create_demo, "demoButton", "demo", "#d2e5d6")
        self.open_button = button("选择文件夹", self.choose_folder, "openButton", "folder")
        row.addWidget(self.demo_button)
        row.addWidget(self.open_button)
        return frame

    def _sidebar(self):
        frame, col = panel("sidebar", (16,18,16,14), 6)
        col.addWidget(label("来源目录", "sectionTitle"))
        self.folder_name = label("尚未选择", "panelTitle")
        self.folder_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        col.addWidget(self.folder_name)
        self.folder_path = PathLabel()
        self.folder_path.setObjectName("muted")
        self.folder_path.hide()
        col.addWidget(self.folder_path)
        self.recursive = QCheckBox("包含子文件夹")
        self.recursive.setObjectName("recursiveCheck")
        self.recursive.toggled.connect(self._rescan)
        col.addWidget(self.recursive)
        scan_row = QHBoxLayout()
        self.refresh_button = button("重新扫描", self._rescan, "refreshButton", "refresh")
        self.cancel_button = button("取消", self._cancel_scan, "cancelButton")
        self.cancel_button.hide()
        scan_row.addWidget(self.refresh_button)
        scan_row.addWidget(self.cancel_button)
        col.addLayout(scan_row)
        target_header = QHBoxLayout()
        target_header.addWidget(label("目标根目录", "sectionTitle"))
        target_header.addStretch()
        self.target_button = button("更改", self.choose_target, "targetButton")
        target_header.addWidget(self.target_button)
        col.addLayout(target_header)
        self.target_path = PathLabel()
        self.target_path.setObjectName("muted")
        self.target_path.setText("使用来源目录")
        col.addWidget(self.target_path)
        col.addSpacing(6)
        col.addWidget(separator())
        col.addSpacing(6)
        col.addWidget(label("文件类型", "sectionTitle"))
        self.filter_buttons = {}
        group = QButtonGroup(frame)
        for category in TYPES:
            control = button(category, lambda checked=False, c=category: self._set_category(c), "typeFilter")
            control.setCheckable(True)
            control.setIcon(type_icon(category))
            control.setIconSize(QSize(16, 16))
            control.setChecked(category == self.category)
            group.addButton(control)
            self.filter_buttons[category] = control
            col.addWidget(control)
        col.addSpacing(4)
        col.addWidget(separator())
        rule_header = QHBoxLayout()
        rule_header.addWidget(label("整理规则", "sectionTitle"))
        rule_header.addStretch()
        self.rules_button = button("规则中心", self.edit_rules, "rulesButton", "rules")
        self.rules_button.setAccessibleName("编辑整理规则")
        rule_header.addWidget(self.rules_button)
        col.addLayout(rule_header)
        self.rules_col = QVBoxLayout()
        self.rules_col.setSpacing(3)
        col.addLayout(self.rules_col)
        self.profile_button = button("规则方案", self.edit_profiles, "profileButton", "archive")
        col.addSpacing(4)
        col.addWidget(self.profile_button)
        col.addStretch()
        scroller = QScrollArea()
        scroller.setObjectName("sideScroll")
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setWidget(frame)
        scroller.setWidgetResizable(True)
        scroller.setFixedWidth(205)
        return scroller

    def _center(self):
        frame, col = panel("center", (19,20,18,16), 12)
        title = QHBoxLayout()
        self.list_title = label("文件清单", "title")
        title.addWidget(self.list_title)
        self.count_label = label("0", "count")
        title.addWidget(self.count_label)
        title.addStretch()
        self.duplicate_button = button("重复件检查", self.show_duplicates, "duplicateButton")
        title.addWidget(self.duplicate_button)
        self.exit_quarantine = button("返回文件清单", self.leave_quarantine, "leaveQuarantineButton")
        title.addWidget(self.exit_quarantine)
        self.exit_quarantine.hide()
        self.select_button = button("全选可移动项", self.toggle_visible, "selectVisibleButton")
        title.addWidget(self.select_button)
        col.addLayout(title)
        self.search = QLineEdit()
        self.search.setObjectName("searchEdit")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setPlaceholderText("搜索文件名或路径")
        self.search.textChanged.connect(self._filter)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        self.batch_tools = QPushButton("批量操作")
        self.batch_tools.setObjectName("batchToolsButton")
        menu = QMenu(self.batch_tools)
        menu.addAction("跳过当前筛选结果", lambda: self.bulk_skip(True))
        menu.addAction("还原当前筛选结果的手动跳过", lambda: self.bulk_skip(False))
        menu.addAction("为当前筛选结果中的名称冲突自动编号", self.number_visible)
        menu.addAction("重新核对已勾选项", lambda: self.recheck_selected(False))
        self.batch_tools.setMenu(menu)
        search_row.addWidget(self.batch_tools)
        col.addLayout(search_row)
        facets = QHBoxLayout()
        self.facets = []
        for title, key in (("全部来源","source"),("全部目标","target"),
                           ("全部规则","rule"),("全部冲突类型","conflict")):
            combo = QComboBox()
            combo.setMinimumWidth(76)
            combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            combo.setObjectName(key+"Filter")
            combo.setAccessibleName(title)
            combo.addItem(title,None)
            combo.currentIndexChanged.connect(self._filter)
            self.facets.append(combo)
            facets.addWidget(combo, 1)
        col.addLayout(facets)
        self.table_stack = QStackedWidget()
        self.model = FileModel(self)
        self.proxy = FileFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setObjectName("fileTable")
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        # scan() already orders by path in its worker. A header click requests
        # an explicit sort without repeating comparison work on every initial load.
        self.table.sortByColumn(-1, Qt.SortOrder.AscendingOrder)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(37)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setMinimumSectionSize(36)
        for col_index, width in enumerate((44,165,70,60,72,136,195)):
            self.table.setColumnWidth(col_index, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table_stack.addWidget(self.table)
        empty, empty_col = panel("emptyPage", (12,15,12,15), 12)
        empty_col.addStretch()
        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_icon.setPixmap(icon("folder", "#72967b", 58).pixmap(QSize(58,58)))
        empty_col.addWidget(self.empty_icon)
        self.empty_title = label("", "emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_col.addWidget(self.empty_title)
        self.empty_hint = label("", "muted", True)
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_col.addWidget(self.empty_hint)
        self.empty_action = button("选择文件夹", self.choose_folder, "emptyOpenButton", "folder")
        empty_col.addWidget(self.empty_action, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_col.addStretch()
        self.table_stack.addWidget(empty)
        col.addWidget(self.table_stack, 1)
        self.model.selectionChanged.connect(self._selection_changed)
        self.table.selectionModel().currentRowChanged.connect(self._show_detail)
        return frame

    def _inspector(self):
        frame, col = panel("inspector", (16,23,16,18), 13)
        frame.setFixedWidth(239)
        title = QHBoxLayout()
        symbol = QLabel()
        symbol.setPixmap(icon("plan").pixmap(20,20))
        title.addWidget(symbol)
        title.addWidget(label("整理计划", "panelTitle"))
        title.addStretch()
        col.addLayout(title)
        col.addWidget(separator())
        self.detail_stack = QStackedWidget()
        blank = label("在文件清单中选择一项，即可查看命中规则和移动路径。", "muted", True)
        blank.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.detail_stack.addWidget(blank)
        card, details = panel("detailPage", (0,0,0,0), 10)
        self.detail_badge = label()
        details.addWidget(self.detail_badge, 0, Qt.AlignmentFlag.AlignLeft)
        self.detail_name = PathLabel()
        self.detail_name.setObjectName("bigName")
        details.addWidget(self.detail_name)
        self.detail_meta = label("", "muted", True)
        details.addWidget(self.detail_meta)
        details.addSpacing(4)
        details.addWidget(label("命中规则", "sectionTitle"))
        self.detail_rule = label("", wrap=True)
        details.addWidget(self.detail_rule)
        details.addSpacing(3)
        self.path_fields = []
        for title, name in (("原位置", "field"), ("目标位置", "destination")):
            field, box = panel(name, (9,8,9,8), 5)
            box.addWidget(label(title, "sectionTitle"))
            text = PathLabel()
            box.addWidget(text)
            details.addWidget(field)
            self.path_fields.append(text)
        self.detail_reason = label("", "muted", True)
        details.addWidget(self.detail_reason)
        self.conflict_buttons = QWidget()
        actions = QHBoxLayout(self.conflict_buttons)
        actions.setContentsMargins(0,0,0,0)
        actions.addWidget(button("自动编号", lambda: self.resolve_current("number"), "numberButton"))
        actions.addWidget(button("手动改名", lambda: self.resolve_current("rename"), "renameButton"))
        details.addWidget(self.conflict_buttons)
        details.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(card)
        self.detail_stack.addWidget(scroll)
        col.addWidget(self.detail_stack, 1)
        return frame

    def _footer(self):
        frame, row = panel("footer", (18,11,18,11), 12, True)
        self.summary = label("待移动 0     已跳过 0     冲突 0")
        row.addWidget(self.summary)
        row.addStretch()
        self.selected_text = label("已勾选 0 项", "muted")
        row.addWidget(self.selected_text)
        self.apply_button = button("应用选中项", self.apply_checked, "applyButton", "arrow", "#edf8ee")
        self.apply_button.setProperty("primary", True)
        row.addWidget(self.apply_button)
        return frame

    def _history(self):
        frame, col = panel("center", (23,20,23,18), 10)
        row = QHBoxLayout()
        row.addWidget(label("操作历史", "title"))
        row.addStretch()
        self.resume_button = button("继续可安全项", self.resume_current, "resumeButton", "arrow")
        row.addWidget(self.resume_button)
        self.undo_button = button("撤销此批", self.undo_current, "undoButton", "undo")
        row.addWidget(self.undo_button)
        col.addLayout(row)
        self.history_hint = label("应用选中项后，可在此查看批次结果并撤销移动。", "muted")
        col.addWidget(self.history_hint)
        self.batch_table = self._log_table("batchTable", ["批次", "执行时间", "来源 → 目标", "执行项", "批次结果"], 2)
        self.batch_table.itemSelectionChanged.connect(self._load_items)
        self.batch_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        col.addWidget(self.batch_table, 2)
        self.batch_title = label("批次明细", "panelTitle")
        col.addWidget(self.batch_title)
        self.item_table = self._log_table("itemTable", ["执行状态", "原位置", "目标位置", "命中规则", "说明"], 1)
        self.item_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.item_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        col.addWidget(self.item_table, 3)
        return frame

    def _log_table(self, name, fields, stretchy):
        table = QTableWidget(0, len(fields))
        table.setObjectName(name)
        table.setHorizontalHeaderLabels(fields)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(34)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(stretchy, QHeaderView.ResizeMode.Stretch)
        return table

    def switch_page(self, index):
        self.pages.setCurrentIndex(index)
        self.workspace_button.setChecked(index == 0)
        self.history_button.setChecked(index == 1)
        self.inbox_button.setChecked(index == 2)
        if index == 1:
            self.refresh_history(self.active_batch)
        elif index == 0 and self.needs_scan and self.job is None:
            self._rescan()

    def refresh_history(self, preferred=None):
        batches = self.journal.batches()
        self.batch_table.blockSignals(True)
        self.batch_table.setRowCount(len(batches))
        selected = 0
        for row, record in enumerate(batches):
            if preferred == record["id"]:
                selected = row
            outcome = self._outcome(record)
            source = record["root"]
            destination = record["target_root"] or source
            path = source if destination == source else f"{source} → {destination}"
            vals = [record["id"],record["created_at"],path,record["total"],outcome]
            self._set_log_row(self.batch_table, row, vals)
        if batches:
            self.batch_table.selectRow(selected)
        self.batch_table.blockSignals(False)
        if not batches:
            self.history_hint.setText("暂无操作记录。先在整理工作台勾选文件并应用。")
        self._load_items()

    @staticmethod
    def _outcome(record):
        fields = (("moved","已移动"),("undone","已撤销"),("skipped","已跳过"),
                  ("failed","失败"),("cancelled","已取消"),("review","待复核"),
                  ("uncertain","待人工确认"),("resumable","可安全继续"),
                  ("undo_conflicts","撤销冲突"),("pending","未开始"),("active","进行中"))
        return "  ·  ".join(f"{title} {record[key]}" for key,title in fields if record[key]) or "无执行项"

    @staticmethod
    def _set_log_row(table, row, values):
        for col, value in enumerate(values):
            text = str(value) if value is not None else "—"
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            table.setItem(row, col, item)

    def _load_items(self):
        row = self.batch_table.currentRow()
        valid = row >= 0 and row < self.batch_table.rowCount()
        self.active_batch = int(self.batch_table.item(row,0).text()) if valid else None
        items = self.journal.items(self.active_batch) if valid else []
        self.item_table.setRowCount(len(items))
        statuses = {"moved":"已移动", "skipped":"已跳过", "undone":"已撤销",
                    "uncertain":"待人工确认", "pending":"已登记", "applying":"执行中", "undoing":"撤销中",
                    "review":"待复核","failed":"失败","resumable":"可安全继续","cancelled":"已取消"}
        undo_count = resume_count = 0
        for i, record in enumerate(items):
            status = statuses.get(record["status"], record["status"])
            undo_count += record["status"] == "moved"
            resume_count += record["status"] == "resumable"
            if record["undo_conflict"]:
                status = "撤销冲突"
            self._set_log_row(self.item_table, i, [status, record["source"], record["target"],
                                                   record["rule"], record["reason"] or "—"])
        self.batch_title.setText(f"批次 #{self.active_batch} 明细" if valid else "批次明细")
        if valid:
            self.history_hint.setText(f"共 {self.batch_table.rowCount()} 个批次   ·   当前批次有 {undo_count} 项可撤销")
        self.undo_button.setEnabled(bool(undo_count) and self.job is None)
        self.resume_button.setEnabled(bool(resume_count) and self.job is None)
        self.resume_button.setToolTip(f"已核对可继续 {resume_count} 项")

    def _note(self, message=""):
        self.notice.setText(message)
        self.notice.setToolTip("")
        self.notice.setVisible(bool(message))

    def _render_rules(self):
        while self.rules_col.count():
            widget = self.rules_col.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        for rule in self.rules:
            name = label(f"{rule.name}  →  {rule.target}" if rule.enabled else f"{rule.name}  ·  已停用", "muted")
            name.setToolTip(f"{rule.name} → {rule.target}\n" + "  ".join(rule.extensions))
            name.setMinimumWidth(0)
            name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.rules_col.addWidget(name)

    def _sync_controls(self):
        idle = self.job is None
        self.inbox.set_workspace_busy(not idle)
        for widget in (self.open_button, self.demo_button, self.rules_button, self.recursive,
                       self.empty_action):
            widget.setEnabled(idle)
        self.refresh_button.setEnabled(idle and self.root is not None)
        self.table.setEnabled(idle)
        self.cancel_button.setVisible(not idle and self.cancel_event is not None)
        self.cancel_button.setEnabled(self.cancel_event is not None and not self.cancel_event.is_set())
        self.refresh_button.setVisible(self.cancel_button.isHidden())
        self.apply_button.setEnabled(idle and bool(self.model.checked))
        self.select_button.setEnabled(idle and bool(self.visible_ready))
        self.batch_table.setEnabled(idle)
        self.profile_button.setEnabled(idle)
        self.target_button.setEnabled(idle and self.root is not None)
        self.batch_tools.setEnabled(idle and bool(self.model.plans))
        self.conflict_buttons.setEnabled(idle)
        self.duplicate_button.setEnabled(idle and bool(self.files) and not self.needs_scan)
        self.exit_quarantine.setEnabled(idle)
        if not idle:
            self.undo_button.setEnabled(False)
            self.resume_button.setEnabled(False)
        self.stop_button.setVisible(not idle and self.cancel_event is not None)
        self.stop_button.setEnabled(self.cancel_event is not None and not self.cancel_event.is_set())

    def _show_progress(self, done, total, filename):
        if self.job is None or (self.cancel_event and self.cancel_event.is_set()):
            return
        self.progress_bar.setRange(0,max(total,0))
        if total:
            self.progress_bar.setValue(done)
        count = f"{done:,}/{total:,}" if total else f"{done:,}"
        self.progress_label.setText(f"{self.progress_title}   {count} 项   {Path(filename).name}")
        self.progress_label.setToolTip(str(filename))

    def _show_stats(self, counts):
        if self.job is None:
            return
        success = counts.get("moved",0) + counts.get("undone",0)
        skipped = counts.get("skipped",0)
        failed = counts.get("failed",0)
        review = counts.get("review",0) + counts.get("uncertain",0)
        self.stats_label.setText(f"成功 {success}   跳过 {skipped}   失败 {failed}   待核对 {review}")

    def _run(self, task, success, progress, cancel=None, finish_on_cancel=False):
        if self.job is not None:
            return
        self.cancel_event = cancel
        self.job = Job(self, task)
        self._on_success = success
        self.job.finished.connect(self._job_finished)
        self.finish_on_cancel = finish_on_cancel
        self.progress_title = progress.rstrip("…")
        self.progress_panel.show()
        self.progress_bar.setRange(0,0)
        self.stats_label.setText("成功 0   跳过 0   失败 0" if finish_on_cancel else "")
        self.progress_label.setText(progress)
        self._note()
        self._sync_controls()
        self.job.start()

    def _job_finished(self):
        job = self.job
        callback = self._on_success
        cancelled = self.cancel_event is not None and self.cancel_event.is_set()
        self.job = self.cancel_event = self._on_success = None
        self.progress_panel.hide()
        invalidator = self._on_invalidate
        self._on_invalidate = None
        if invalidator is not None and (cancelled or job.error):
            invalidator()
        self._sync_controls()
        self._note()
        if (cancelled and not self.finish_on_cancel) or isinstance(job.error, ScanCancelled):
            self._note("已取消。点击「重新扫描」可再次查看此文件夹。")
        elif job.error:
            self._note(f"操作未完成：{job.error}。可查看历史或重新扫描。")
            if self.pages.currentIndex() == 1:
                self.refresh_history(self.active_batch)
        else:
            callback(job.result)
        job.deleteLater()
        if self.pages.currentIndex() == 1 and self.job is None:
            self._load_items()

    def choose_folder(self):
        value = QFileDialog.getExistingDirectory(self, "选择要整理的文件夹", "",
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if value:
            self.set_folder(Path(value))

    def create_demo(self):
        try:
            demo = create_demo()
        except OSError as exc:
            self._note(f"演示目录创建失败：{exc}")
            return
        self.set_folder(demo)

    def set_folder(self, root):
        self._set_source(root)
        self._rescan()

    def _set_source(self, root):
        self.root = Path(root)
        self.target_root = self.root
        self.resolutions.clear()
        self.user_skipped.clear()
        self.target_path.setText("使用来源目录")
        self.folder_name.setText(self.folder_name.fontMetrics().elidedText(
            self.root.name or str(self.root), Qt.TextElideMode.ElideMiddle, 168))
        self.folder_name.setToolTip(str(self.root))
        self.folder_path.setText(str(self.root))
        self.folder_path.setToolTip(str(self.root))
        self.folder_path.show()
        self.needs_scan = False
        self.switch_page(0)
        self.search.clear()
        self._set_category("全部文件")
        self.filter_buttons["全部文件"].setChecked(True)
        for combo in self.facets:
            combo.setCurrentIndex(0)

    def _load_inbox(self, root, candidates):
        if self.job is not None or not candidates:
            return
        previous_target = self.target_root if root == self.root else root
        self._quarantine_mode(())
        self._set_source(root)
        self.target_root = previous_target
        self.target_path.setText("使用来源目录" if root == previous_target else str(previous_target))
        self.files = []
        self._set_plans([])
        cancel = Event()
        rules = list(self.rules)

        def build():
            valid, warnings = [], []
            for candidate in candidates:
                try:
                    current = snapshot_file(candidate.path, cancel)
                    if current != candidate:
                        warnings.append(f"{candidate.path}：自入箱后已变化")
                    else:
                        valid.append(current)
                except (OSError, ValueError) as exc:
                    warnings.append(f"{candidate.path}：{exc}")
            plans = preview(root, valid, rules, cancel, target_root=previous_target)
            return valid, plans, warnings

        def loaded(result):
            self.files, plans, warnings = result
            self._set_plans(plans)
            self._note(f"已从收件箱载入 {len(plans)} 项，仅生成预览。"
                       + (f"有 {len(warnings)} 条提示：{warnings[0]}" if warnings else ""))
            self.notice.setToolTip("\n".join(warnings))

        self._run(build, loaded, "正在核对收件箱候选并生成预览…", cancel)

    def _rescan(self):
        if self.root is None or self.job is not None:
            return
        self._quarantine_mode(())
        self.files = []
        self.user_skipped.clear()
        self.resolutions.clear()
        self.needs_scan = False
        self._set_plans([])
        root = self.root
        recursive = self.recursive.isChecked()
        rules = list(self.rules)
        cancel = Event()
        def scan_and_plan():
            files = scan(root, recursive, cancel,
                         progress=lambda n,path: self.progress_event.emit(n,0,path))
            if cancel.is_set():
                raise ScanCancelled()
            return files, preview(root, files, rules, cancel, target_root=self.target_root)
        self._run(scan_and_plan, self._scanned, "正在扫描文件并计算整理预览…", cancel)

    def _cancel_scan(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.progress_label.setText("正在取消，等待当前文件安全收尾…")

    def _scanned(self, result):
        self.files, plans = result
        self._set_plans(plans)
        warnings = getattr(self.files, "warnings", [])
        if warnings:
            self._note(f"扫描中有 {len(warnings)} 条提示。{warnings[0]}")
            self.notice.setToolTip("\n".join(warnings))
        if self.root:
            self.folder_path.setToolTip(str(self.root))

    def _set_plans(self, plans, selected_paths=()):
        plans = [replace(p, status="skip", reason="本次手动跳过") if p.file.path in self.user_skipped else p for p in plans]
        self.counts = Counter(plan.status for plan in plans)
        categories = Counter(plan.file.category for plan in plans)
        for key, ctrl in self.filter_buttons.items():
            ctrl.setText(f" {key}   {len(plans) if key == '全部文件' else categories[key]}")
        self.model.replace(plans)
        self._refresh_facets(plans)
        if selected_paths:
            self.model.select_rows({i for i,p in enumerate(plans)
                                    if p.file.path in selected_paths and p.status=="ready"}, True)
        self._filter()
        self.summary.setText(f"待移动  {self.counts['ready']}     已跳过  {self.counts['skip']}     "
                             f"冲突  {self.counts['conflict']}     待复核  {self.counts['review']}")
        self._sync_controls()

    def _set_category(self, category):
        self.category = category
        self._filter()

    def _refresh_facets(self, plans):
        values = (sorted({str(p.file.path.parent) for p in plans}),
                  sorted({str(p.target.parent) if p.target else "" for p in plans}),
                  sorted({p.rule for p in plans}), sorted({p.conflict_type for p in plans}))
        for i,(combo,options) in enumerate(zip(self.facets, values)):
            previous = combo.currentData()
            combo.blockSignals(True)
            title = combo.itemText(0)
            combo.clear()
            combo.addItem(title, None)
            for value in options:
                if i < 2 and value:
                    root = self.root if i==0 else self.target_root
                    try:
                        relative = Path(value).relative_to(root)
                        text = str(relative) if relative.parts else "根目录"
                    except (ValueError,TypeError):
                        text = value
                elif i==3:
                    text = CONFLICTS.get(value, value)
                else:
                    text = value or ("未匹配" if i==2 else "无目标")
                combo.addItem(text, value)
                combo.setItemData(combo.count()-1, value or text, Qt.ItemDataRole.ToolTipRole)
            if previous is not None and combo.findData(previous) >= 0:
                combo.setCurrentIndex(combo.findData(previous))
            combo.blockSignals(False)

    def _filter(self):
        self.proxy.update_filter(self.search.text(), self.category,
                                 tuple(combo.currentData() for combo in self.facets))
        self.visible_ready = self._visible_ready_rows()
        self._update_table()
        self._selection_changed()

    def _update_table(self):
        visible = self.proxy.rowCount()
        self.count_label.setText(str(visible) if visible == len(self.model.plans)
                                 else f"{visible} / {len(self.model.plans)}")
        self.table_stack.setCurrentIndex(0 if visible else 1)
        self.empty_action.setVisible(self.root is None)
        if self.root is None:
            self.empty_title.setText("选择一个文件夹开始整理")
            self.empty_hint.setText("扫描后可逐项预览移动路径，也可以先创建演示目录。")
        elif self.model.plans:
            self.empty_title.setText("没有匹配的文件")
            self.empty_hint.setText("调整搜索词，或在左侧切换文件类型。")
        else:
            self.empty_title.setText("当前清单为空")
            self.empty_hint.setText("可重新扫描、包含子文件夹，或选择其他文件夹。")
        if visible:
            self.table.selectRow(0)
        self._show_detail()
        self.select_button.setEnabled(self.job is None and bool(self.visible_ready))

    def _show_detail(self, *_):
        if not hasattr(self, "detail_stack"):
            return
        idx = self.proxy.mapToSource(self.table.currentIndex())
        if not idx.isValid() or idx.row() >= len(self.model.plans):
            self.detail_stack.setCurrentIndex(0)
            return
        plan = self.model.plans[idx.row()]
        self.detail_stack.setCurrentIndex(1)
        self.detail_name.setText(plan.file.path.name)
        self.detail_meta.setText(f"{plan.file.category}   ·   {size_text(plan.file.size)}")
        self.detail_badge.setText(STATUS[plan.status])
        color = STATUS_COLORS[plan.status]
        self.detail_badge.setStyleSheet(f"background:#eff3eb;color:{color};padding:3px 8px;border-radius:4px;font-weight:600")
        self.detail_rule.setText(plan.rule or "无匹配规则")
        for widget, path in zip(self.path_fields, (plan.file.path, plan.target)):
            widget.setText(str(path) if path else "—")
            widget.setToolTip(widget.toPlainText())
        self.detail_reason.setText(plan.reason or "将按命中规则移动到目标子目录。")
        self.conflict_buttons.setVisible(plan.conflict_type in {"existing","batch"} or
                                         plan.resolution in {"number","rename"})

    def _visible_ready_rows(self):
        if self.proxy.unfiltered:
            return {row for row,plan in enumerate(self.model.plans) if plan.status=="ready"}
        rows = set()
        for i in range(self.proxy.rowCount()):
            index = self.proxy.mapToSource(self.proxy.index(i, 0))
            if self.model.plans[index.row()].status == "ready":
                rows.add(index.row())
        return rows

    def toggle_visible(self):
        rows = self.visible_ready
        self.model.select_rows(rows, not rows.issubset(self.model.checked))

    def _selection_changed(self):
        if not hasattr(self, "selected_text"):
            return
        selected = self.model.checked
        hidden = len(selected - self.visible_ready)
        extra = f"（含筛选外 {hidden} 项）" if hidden else ""
        amount = sum(self.model.plans[row].file.size for row in selected)
        self.selected_text.setText(f"已勾选 {len(selected)} 项  ·  {amount:,} 字节{extra}")
        self.apply_button.setEnabled(self.job is None and bool(selected))
        all_checked = bool(self.visible_ready) and self.visible_ready.issubset(selected)
        self.select_button.setText("取消当前勾选" if all_checked else "全选可移动项")

    def show_duplicates(self):
        from .duplicates_ui import show_duplicates
        if self.root is not None and self.files and self.job is None:
            groups = show_duplicates(self, self.root, self.files)
            if groups:
                self._quarantine_mode(groups)
                self.user_skipped.clear()
                self.resolutions.clear()
                self.search.clear()
                self._set_category("全部文件")
                self.filter_buttons["全部文件"].setChecked(True)
                for combo in self.facets:
                    combo.setCurrentIndex(0)
                self.replan()

    def _quarantine_mode(self, groups):
        self.quarantine_groups = tuple(groups)
        self.list_title.setText("隔离计划" if groups else "文件清单")
        self.exit_quarantine.setVisible(bool(groups))
        self.duplicate_button.setVisible(not groups)

    def leave_quarantine(self):
        self._quarantine_mode(())
        self.resolutions.clear()
        self.user_skipped.clear()
        self.replan()

    def edit_profiles(self):
        from .extras_ui import edit_profiles
        rules = edit_profiles(self, self.rules, self.journal)
        if rules is not None:
            self.set_rules(rules)

    def edit_rules(self):
        dialog = RulesDialog(self, self.rules, self.root, self.target_root)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_rules(dialog.rules)

    def set_rules(self, rules):
        try:
            self.profiles.save_active(rules)
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._note(f"工作规则未保存：{exc}")
            return
        self.rules = list(rules)
        self.resolutions.clear()
        self.user_skipped.clear()
        self._render_rules()
        self.replan()

    def choose_target(self):
        selected = QFileDialog.getExistingDirectory(self, "选择目标根目录", str(self.target_root or self.root),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontResolveSymlinks)
        if selected:
            self.target_root = Path(selected)
            self.target_path.setText("使用来源目录" if self.target_root == self.root else str(self.target_root))
            self.user_skipped.clear()
            self.resolutions.clear()
            self.replan()

    def replan(self, preserve=False):
        if self.root is None or self.job is not None:
            return
        old = {self.model.plans[i].file.path: self.model.plans[i] for i in self.model.checked} if preserve else {}
        root, files, rules = self.root, self.files, list(self.rules)
        target_root, resolutions = self.target_root, dict(self.resolutions)
        groups = self.quarantine_groups
        cancel = Event()
        def finished(plans):
            selected = {p.file.path for p in plans if old.get(p.file.path) == p}
            self._set_plans(plans, selected)
        def failed_preview():
            # Cancelled or failed recomputation invalidates the previous plan.
            self._set_plans([])
        self._on_invalidate = failed_preview
        def calculate():
            if groups:
                return quarantine_preview(root, target_root, groups, cancel, resolutions=resolutions)
            return preview(root, files, rules, cancel, target_root=target_root, resolutions=resolutions)
        self._run(calculate, finished, "正在更新整理预览…", cancel)

    def _visible_plans(self):
        return [self.model.plans[self.proxy.mapToSource(self.proxy.index(i,0)).row()]
                for i in range(self.proxy.rowCount())]

    def bulk_skip(self, skip):
        paths = {p.file.path for p in self._visible_plans()}
        if skip:
            self.user_skipped.update(paths)
        else:
            self.user_skipped.difference_update(paths)
        self.replan(preserve=True)

    def number_visible(self):
        for plan in self._visible_plans():
            if plan.conflict_type in {"existing","batch"}:
                self.resolutions[plan.file.path] = ("number","")
        self.replan(preserve=True)

    def resolve_current(self, mode):
        index = self.proxy.mapToSource(self.table.currentIndex())
        if not index.isValid():
            return
        plan = self.model.plans[index.row()]
        name = ""
        if mode=="rename":
            name, ok = QInputDialog.getText(self,"手动改名","目标文件名（含扩展名）",text=plan.file.path.name)
            if not ok:
                return
        self.resolutions[plan.file.path] = (mode, name)
        self.user_skipped.discard(plan.file.path)
        self.replan(preserve=True)

    def confirm(self, title, message, action):
        dialog = QMessageBox(QMessageBox.Icon.Question, title, message, parent=self)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        ok = dialog.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.exec()
        return dialog.clickedButton() is ok

    def apply_checked(self):
        if self.job is not None or not self.model.checked:
            return
        plans = [self.model.plans[i] for i in sorted(self.model.checked)]
        targets = Counter(str(p.target.parent.relative_to(self.target_root)) for p in plans)
        listing = "\n".join(f"  {name}   ·   {n} 项" for name, n in list(targets.items())[:8])
        if len(targets) > 8:
            listing += f"\n  等 {len(targets)} 个子目录"
        message = (f"将移动 {len(plans)} 个文件，共 {sum(p.file.size for p in plans):,} 字节。\n\n来源：{self.root}\n目标根目录：{self.target_root}\n目标子目录：\n{listing}"
                   "\n\n执行前会再次检查文件。遇到变化或同名目标时会跳过，并写入历史。")
        if self.confirm("确认应用整理计划", message, "确认移动"):
            self.recheck_selected(True)

    def recheck_selected(self, apply_after):
        if self.job is not None or not self.model.checked:
            return
        selected = [self.model.plans[i] for i in sorted(self.model.checked)]
        rules = list(self.rules)
        cancel = Event()
        def checked(plans):
            changed = {p.file.path:p for p in plans if p.status=="review"}
            if changed:
                safe = {p.file.path for p in plans if p.status=="ready"}
                self._set_plans([changed.get(p.file.path,p) for p in self.model.plans],safe)
                self._note(f"{len(changed)} 项在预览后发生变化，已标为待复核。请检查明细并重新扫描。")
            elif apply_after:
                self._execute(plans)
            else:
                self._note(f"已核对 {len(plans)} 项，文件和目标与预览一致。")
        self._run(lambda: recheck_plans(selected,rules,cancel), checked, "正在核对选中项…", cancel)

    def _execute(self, plans):
        root, target, rules = self.root, self.target_root, list(self.rules)
        self._transfer(lambda **kw: operations.apply(root,plans,self.journal,
                       target_root=target,rules=rules,**kw),self._applied,"正在应用选中项…")

    def _transfer(self, operation, success, description):
        cancel = Event()
        next_item = next_stats = 0.0
        def progress(done,total,filename):
            nonlocal next_item
            if done == total or monotonic() >= next_item:
                self.progress_event.emit(done,total,str(filename))
                next_item = monotonic() + .10
        def stats(counts):
            nonlocal next_stats
            if monotonic() >= next_stats:
                self.stats_event.emit(dict(counts))
                next_stats = monotonic() + .10
        self._run(lambda: operation(cancel=cancel,progress=progress,stats=stats),
                  success,description,cancel,finish_on_cancel=True)

    def resume_current(self):
        batch_id = self.active_batch
        if self.job is not None or batch_id is None:
            return
        count = sum(item["status"]=="resumable" for item in self.journal.items(batch_id))
        if not count:
            return
        message = (f"批次 #{batch_id} 有 {count} 项可安全继续。将再次核对日志、原文件、"
                   "临时文件及目标，再完成该项移动或撤销。")
        if self.confirm("确认继续中断的操作",message,"确认继续"):
            rules = list(self.rules)
            self._transfer(lambda **kw: operations.resume(batch_id,self.journal,rules=rules,**kw),
                           lambda result:self._applied(batch_id),"正在继续已核对项…")

    def _applied(self, batch_id):
        self._quarantine_mode(())
        self.active_batch = batch_id
        self.needs_scan = True
        self.files = []
        self._set_plans([])
        self.switch_page(1)
        record = next((b for b in self.journal.batches() if b["id"]==batch_id),None)
        outcome = self._outcome(record) if record else "请查看下方明细"
        self._note(f"批次 #{batch_id}：{outcome}")

    def undo_current(self):
        batch_id = self.active_batch
        if self.job is not None or batch_id is None:
            return
        if self.confirm("确认撤销此批", f"将检查批次 #{batch_id} 中已移动的文件。\n\n只有文件仍与记录一致且原位置空闲时，才会移回。", "确认撤销"):
            self._transfer(lambda **kw: operations.undo(batch_id,self.journal,**kw), self._undone, "正在撤销此批…")

    def _undone(self, _):
        self.needs_scan = self.root is not None
        self.switch_page(1)
        self._note("撤销检查已完成。可在批次明细中查看已撤销项和冲突原因。")

    def showEvent(self, event):
        super().showEvent(event)
        self.inbox.start()

    def closeEvent(self, event):
        if self.job is not None:
            if self.cancel_event is not None:
                self._cancel_scan()
            else:
                self._note("正在完成当前操作，请稍后关闭窗口。")
            event.ignore()
        elif self.inbox.shutdown():
            event.accept()
        else:
            self._note("正在结束收件箱检查…")
            self.setEnabled(False)
            event.ignore()
