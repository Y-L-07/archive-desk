"""Measure SQLite record import and end-to-end paged searches on synthetic metadata."""
import argparse
import multiprocessing
import platform
import sqlite3
import sys
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from archive_desk.catalog import Catalog, upsert_file
from archive_desk.catalog_search import CatalogSearch
from archive_desk.core import FileInfo
from archive_desk.journal import database_connection


def window_benchmark(path):
    """Run in a separate Windows process. Input and paint events target this window only."""
    from PySide6.QtCore import QEvent, QEventLoop, QObject, Qt, QTimer
    from PySide6.QtGui import QInputMethodEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from archive_desk.journal import Journal
    from archive_desk.window import MainWindow

    app = QApplication([])
    assert app.platformName() == "windows"
    window = MainWindow(Journal(path))
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.show()
    assert QTest.qWaitForWindowExposed(window)
    QTest.mouseClick(window.catalog_button, Qt.MouseButton.LeftButton)
    pane = window.catalog.search
    search = CatalogSearch(path)
    wanted = {
        (word, number): search.search({"text": word}, page=number, page_size=100)
        for word, number in (("项目记录", 1), ("交接清单", 1), ("交接清单", 2))
    }

    class PaintObserver(QObject):
        def __init__(self):
            super().__init__(window)
            self.expected = None
            self.loop = None

        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Paint and self.expected is not None:
                rows = pane.model.records
                if len(rows) == 100 and [r["id"] for r in rows] == self.expected:
                    self.expected = None
                    # Quit after this event finishes, so the table can draw its new page.
                    QTimer.singleShot(0, self.loop.quit)
            return False

    observer = PaintObserver()
    viewport = pane.table.viewport()
    viewport.installEventFilter(observer)
    heartbeat = QTimer(window)
    heartbeat.setTimerType(Qt.TimerType.PreciseTimer)
    heartbeat.setInterval(10)
    intervals = []
    ticks = 0
    last_tick = 0.0

    def tick():
        nonlocal last_tick, ticks
        now = perf_counter()
        intervals.append(1000 * (now-last_tick))
        last_tick = now
        ticks += 1

    heartbeat.timeout.connect(tick)

    def enter_text(word):
        # QTest.keyClicks only supports ASCII. Send a Chinese IME commit to the actual QLineEdit.
        QTest.keyClick(pane.keyword, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        commit = QInputMethodEvent()
        commit.setCommitString(word)
        QApplication.sendEvent(pane.keyword, commit)
        assert pane.keyword.text() == word

    def measure(word, number, action, record=True):
        nonlocal last_tick
        reply = wanted[word, number]
        ids = [r["id"] for r in reply["rows"]]
        assert len(ids) == 100
        if pane.model.records:
            assert pane.model.records[0]["id"] != ids[0]
        observer.expected = ids
        observer.loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(observer.loop.quit)
        timeout.start(20_000)
        start = perf_counter()
        if record:
            last_tick = start
            heartbeat.start()
        action()
        observer.loop.exec()
        end = perf_counter()
        if record:
            heartbeat.stop()
            intervals.append(1000 * (end-last_tick))
        timeout.stop()
        assert observer.expected is None, "Expected page was not painted within 20 s"
        assert pane.table.model().rowCount() == 100 and pane.model.records[0]["id"] == ids[0]
        assert pane.table.model().index(0, 0).data() == reply["rows"][0]["name"]
        assert f"{reply['total']:,}" in pane.summary.text() and pane.page_no == number
        return 1000 * (end-start)

    query_times, page_times = [], []
    try:
        for _ in range(7):
            measure("项目记录", 1, lambda:enter_text("项目记录"), record=False)
            query_times.append(measure("交接清单", 1, lambda:enter_text("交接清单")))
            assert pane.next.isVisible() and pane.next.isEnabled()
            page_times.append(measure("交接清单", 2, lambda:QTest.mouseClick(
                pane.next, Qt.MouseButton.LeftButton)))
        for name, number, samples in (("Chinese input -> first paint", 1, query_times),
                                      ("next-page click -> new paint", 2, page_times)):
            first = wanted["交接清单", number]["rows"][0]["id"]
            print(f"Windows Widgets {name}: 20,000 hits, 100 rows, first id {first}, "
                  f"median {median(samples):.1f} ms "
                  f"(range {min(samples):.1f}–{max(samples):.1f}, n=7)",flush=True)
        assert ticks > 0
        print(f"Windows GUI heartbeat during 14 timed actions: target 10 ms, "
              f"{ticks} ticks, max observed interval {max(intervals):.1f} ms",flush=True)
    finally:
        viewport.removeEventFilter(observer)
        window.close()


def benchmark(gui=False):
    print(platform.platform(), platform.machine(), "Python", platform.python_version(),
          "SQLite", sqlite3.sqlite_version)
    count = 100_000
    with tempfile.TemporaryDirectory(prefix="ArchiveDesk-index-benchmark-") as temporary:
        base = Path(temporary)
        path = base / "bench.sqlite3"
        catalog = Catalog(path)
        roots = []
        for label in ("项目归档", "移动硬盘"):
            (base / label).mkdir()
            roots.append(catalog.add_root(label, base / label))
        started = perf_counter()
        with database_connection(path) as db:
            for idx in range(count):
                stem = "交接清单" if idx % 5 == 0 else "项目记录"
                name = f"项目{idx % 113:03d}/{stem}-{idx:06d}.pdf"
                entry = FileInfo(Path(name), 2048 + idx, 1_750_000_000_000_000_000 + idx, 1, idx + 1)
                upsert_file(db, roots[idx % 2], name, entry)
                if idx % 500 == 499:
                    db.commit()
            db.execute("UPDATE catalog_roots SET indexed_at=CURRENT_TIMESTAMP,last_complete_at=CURRENT_TIMESTAMP,"
                       "published_state='complete',scan_state='complete'")
        print(f"import {count:,} rows, 500 per transaction: {perf_counter()-started:.3f} s")
        search = CatalogSearch(path)
        assert search.search(page_size=1)["total"] == count
        for name, filters, page, expected in (
                ("Chinese first page", {"text":"交接清单"}, 1, 20_000),
                ("Chinese page 100", {"text":"交接清单"}, 100, 20_000),
                ("all page 1000", {}, 1000, 100_000)):
            times = []
            for _ in range(7):
                start = perf_counter()
                reply = search.search(filters, page=page, page_size=100)
                times.append(1000 * (perf_counter() - start))
                assert reply["total"] == expected and len(reply["rows"]) == 100
                if filters:
                    assert all("交接清单" in row["name"] for row in reply["rows"])
            print(f"{name}: {expected:,} matches, median {median(times):.1f} ms "
                  f"(range {min(times):.1f}–{max(times):.1f}, n=7)")

        if gui:
            process = multiprocessing.get_context("spawn").Process(target=window_benchmark, args=(path,))
            process.start()
            process.join()
            if process.exitcode != 0:
                raise SystemExit(f"Windows GUI benchmark exited with status {process.exitcode}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gui", action="store_true", help="also time actual Windows search and next-page paints")
    benchmark(gui=parser.parse_args().gui)
