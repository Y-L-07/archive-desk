"""归档台的界面配色和线形图标。"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def icon(name, color="#507467", size=20):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size * 2 / 24, size * 2 / 24)
    p.setPen(QPen(QColor(color), 1.65, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    def line(*coords):
        points = [QPointF(*coords[i:i + 2]) for i in range(0, len(coords), 2)]
        p.drawPolyline(points)
    def rect(x,y,w,h,r=1.5):
        p.drawRoundedRect(QRectF(x,y,w,h),r,r)
    if name == "folder":
        line(3,19,3,6,9,6,11,8,21,8,21,19,3,19)
    elif name == "archive":
        rect(3,4,18,4); rect(5,8,14,12); line(10,12,14,12)
    elif name == "image":
        rect(3,4,18,16); line(4,17,10,11,14,15,17,12,21,16)
        p.drawEllipse(QPointF(16,8),1,1)
    elif name == "all":
        for x in (4,13):
            for y in (4,13):
                rect(x,y,6,6,1)
    elif name == "history":
        p.drawArc(QRectF(4,4,16,16), 52*16, -312*16)
        line(4,4,4,9,9,9); line(12,7,12,12,16,14)
    elif name == "file":
        line(6,3,14,3,19,8,19,21,6,21,6,3); line(14,3,14,8,19,8)
        line(9,12,16,12); line(9,16,14,16)
    elif name == "package":
        line(3,7,12,3,21,7,21,17,12,21,3,17,3,7,12,11,21,7)
        line(12,11,12,21); line(8,5,17,9)
    elif name == "installer":
        rect(3,4,18,13); line(12,8,12,13); line(9,10,12,13,15,10); line(8,21,16,21)
        line(12,17,12,21)
    elif name == "other":
        rect(3,4,18,16)
        for x in (8,12,16): p.drawPoint(QPointF(x,12))
    elif name == "search":
        p.drawEllipse(QRectF(4,4,12,12)); line(14.5,14.5,20,20)
    elif name == "demo":
        line(9,3,15,3); line(10,3,10,10,4,19,5,21,19,21,20,19,14,10,14,3)
        line(8,15,16,15)
    elif name == "refresh":
        p.drawArc(QRectF(4,4,16,16), 52*16, -309*16); line(3,4,3,9,8,9)
    elif name == "rules":
        for y,x in [(6,9),(12,16),(18,8)]:
            line(3,y,x-2,y); line(x+2,y,21,y); rect(x-2,y-2,4,4,1)
    elif name == "plan":
        rect(4,3,16,18); line(9,7,16,7); line(9,12,16,12); line(9,17,13,17)
    elif name == "arrow":
        line(4,12,20,12); line(14,6,20,12,14,18)
    elif name == "undo":
        line(4,10,15,10,18,11,20,14,20,17,18,20,11,20); line(9,5,4,10,9,15)
    elif name == "check":
        line(4,12,9,17,20,6)
    p.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


STYLE = """
QWidget { color:#263b35; font-family:"Microsoft YaHei UI","Microsoft YaHei","Segoe UI"; font-size:13px; }
QMainWindow, QFrame#center, QFrame#inspector, QFrame#footer, QDialog { background:#ffffff; }
QFrame#top { background:#153e36; }
QLabel#brand { color:#eef8f2; font-size:21px; font-weight:700; }
QFrame#top QPushButton { border:0; color:#cadbd4; background:transparent; font-weight:600;
    padding:8px 14px; border-radius:6px; }
QFrame#top QPushButton:hover { background:#245247; }
QFrame#top QPushButton:checked { color:white; background:#346053; }
QFrame#top QPushButton#openButton { color:#1d483a; background:#deecd9; }
QFrame#top QPushButton#openButton:hover { background:#f1f8eb; }
QFrame#top QPushButton#demoButton { border:1px solid #527166; }
QFrame#sidebar, QScrollArea#sideScroll { background:#f3f6f2; border:0; }
QFrame#inspector { border-left:1px solid #dbe3dc; }
QFrame#footer { border-top:1px solid #cfdcd3; }
QLabel#title { font-size:22px; font-weight:700; color:#193d32; }
QLabel#panelTitle { font-size:15px; font-weight:700; color:#284b3e; }
QLabel#sectionTitle { font-size:12px; font-weight:700; color:#577466; }
QLabel#muted { color:#61756c; font-size:12px; }
QLabel#emptyTitle { font-size:18px; font-weight:600; color:#365647; }
QTextEdit { border:0; background:transparent; padding:0; }
QTextEdit#muted { color:#61756c; font-size:12px; }
QScrollArea { border:0; background:#ffffff; }
QFrame#detailPage { background:#ffffff; }
QTextEdit#bigName { font-size:17px; font-weight:700; color:#224436; }
QLabel#count { font-weight:600; color:#42705a; background:#eaf2e7; border-radius:9px; padding:1px 7px; }
QPushButton { background:#ffffff; border:1px solid #cddad0; border-radius:5px; padding:6px 11px; }
QPushButton:hover { background:#edf4ed; border-color:#a6c1b1; }
QPushButton:pressed { background:#dce9de; }
QPushButton:disabled { color:#85948c; border-color:#dde4df; background:#f2f5f1; }
QPushButton#primary { color:#ffffff; background:#256749; border:1px solid #256749; font-weight:600; padding:9px 18px; }
QPushButton#primary:hover { background:#1c543b; }
QPushButton#primary:disabled { color:#eef4ed; background:#94ac9c; border-color:#94ac9c; }
QPushButton[primary="true"] { color:#ffffff; background:#256749; border:1px solid #256749; font-weight:600; padding:9px 18px; }
QPushButton[primary="true"]:hover { background:#1c543b; }
QPushButton[primary="true"]:disabled { color:#eef4ed; background:#94ac9c; border-color:#94ac9c; }
QPushButton#typeFilter { background:transparent; border:0; text-align:left; padding:5px 8px; }
QPushButton#typeFilter:hover { background:#e8eee6; }
QPushButton#typeFilter:checked { color:#1c563d; background:#dfeadd; font-weight:600; }
QPushButton#link { color:#276a4a; padding:3px 4px; font-size:12px; background:transparent; border:0; }
QPushButton#link:hover { background:#e4eee4; }
QLineEdit, QComboBox, QListWidget { border:1px solid #cddad0; background:#fff; border-radius:5px; padding:5px 8px; }
QLineEdit:focus, QComboBox:focus { border-color:#4b8e68; }
QTableView { border:1px solid #d9e3da; border-radius:5px; background:#fff;
    selection-background-color:#e4efe5; selection-color:#173f2e; gridline-color:#e5ebe5; outline:0; }
QTableView::item { padding:3px; border-bottom:1px solid #edf1ec; }
QTableView::item:hover { background:#f2f7f0; }
QTableView::item:selected { background:#e4efe5; color:#163f2d; }
QHeaderView::section { background:#f1f5f0; color:#536b5d; font-size:12px; font-weight:600;
    padding:7px 4px; border:0; border-bottom:1px solid #dce6dc; }
QCheckBox { spacing:7px; }
QFrame#line { background:#dce4db; max-height:1px; }
QFrame#field { background:#f3f6f2; border:1px solid #dfe7de; border-radius:6px; }
QFrame#destination { background:#edf4ea; border:1px solid #d8e6d5; border-radius:6px; }
QFrame#emptyPage { background:#fafcf8; border:1px solid #dce6da; border-radius:7px; }
QLabel#notice { color:#765523; background:#faf3df; padding:7px; border-radius:5px; }
QSplitter::handle { background:#dce4dd; }
QToolTip { background:#213b30; color:white; padding:4px; }
"""
