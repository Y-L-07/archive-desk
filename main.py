"""归档台的桌面入口。"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QLockFile, QStandardPaths, QTimer, QTranslator
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from archive_desk.journal import Journal
from archive_desk.window import MainWindow


def data_directory() -> Path:
    override = os.environ.get("ARCHIVE_DESK_DATA_DIR")
    if override:
        return Path(override).absolute()
    return Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ArchiveDesk")
    app.setApplicationDisplayName("归档台")
    QLocale.setDefault(QLocale("zh_CN"))
    translator = QTranslator(app)
    if translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "archive.ico")))
    app.setFont(QFont("Microsoft YaHei UI", 9))
    directory = data_directory()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # 持锁直至进程退出，避免两个实例同时处理一份未完成日志。
        lock = QLockFile(str(directory / "instance.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(None, "归档台", "当前数据目录正在使用或无法加锁。请查看已打开的归档台窗口，并确认数据目录可写。")
            return 1
        journal = Journal(directory / "history.sqlite3")
        window = MainWindow(journal)
    except Exception as exc:
        QMessageBox.critical(None, "归档台启动失败", f"无法打开工作台。\n{exc}")
        return 1
    window.show()
    QTimer.singleShot(0, window.start_recovery)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
