"""Qt 工作线程。结果和异常由 GUI 线程在 finished 后读取。"""
from PySide6.QtCore import QThread


class Job(QThread):
    def __init__(self, parent, task):
        super().__init__(parent)
        self.task = task
        self.result = self.error = None

    def run(self):
        try:
            self.result = self.task()
        except Exception as exc:
            self.error = exc
