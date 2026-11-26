"""可完整换行、选择与复制的路径显示。"""
from math import ceil

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QFrame, QSizePolicy, QTextEdit


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

    def text(self):
        return self.toPlainText()
