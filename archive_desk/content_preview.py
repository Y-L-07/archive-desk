"""Bounded previews of a file whose catalog identity was checked."""
from contextlib import ExitStack, closing
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QPlainTextEdit, QScrollArea, QVBoxLayout

from ._winmove import open_regular
from .backup_paths import pin_path


TEXT_LIMIT = 128 * 1024
MEDIA_LIMIT = 32 * 1024 * 1024
TEXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".ini", ".yaml", ".yml", ".py", ".html"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico"}


def read_preview(path, entry):
    kind = path.suffix.lower()
    with ExitStack() as stack:
        pin_path(stack,path.parent)
        stream, _, info = stack.enter_context(open_regular(path))
        if (str(info.st_dev),str(info.st_ino),info.st_size,info.st_mtime_ns) != (
                entry["dev"],entry["ino"],entry["size"],entry["mtime_ns"]):
            raise ValueError("文件已经变化，请刷新档案库后重试")
        if kind in TEXT:
            raw = stream.read(TEXT_LIMIT)
            for encoding in ("utf-16",) if raw.startswith((b"\xff\xfe",b"\xfe\xff")) else ("utf-8-sig","gb18030"):
                try:
                    value = raw.decode(encoding)
                    break
                except UnicodeError:
                    value = raw.decode("utf-8",errors="replace")
            return value + ("\n\n［预览仅显示文件开头］" if info.st_size > TEXT_LIMIT else "")
        if kind not in IMAGES and kind != ".pdf":
            raise ValueError("此类型暂不支持预览")
        if info.st_size > MEDIA_LIMIT:
            raise ValueError("图片和 PDF 的预览上限为 32 MiB")
        raw = stream.read(MEDIA_LIMIT+1)
    if kind == ".pdf":
        import pypdfium2
        with closing(pypdfium2.PdfDocument(raw)) as document:
            if not len(document):
                raise ValueError("PDF 没有可预览的页面")
            with closing(document[0]) as page:
                width, height = page.get_size()
                if min(width,height) <= 0:
                    raise ValueError("PDF 页面尺寸无效")
                with closing(page.render(scale=min(2, 1440/max(width,height)),force_bitmap_format=(
                        pypdfium2.raw.FPDFBitmap_BGRA))) as bitmap:
                    return QImage(bytes(bitmap.buffer),bitmap.width,bitmap.height,bitmap.stride,
                                  QImage.Format.Format_ARGB32).copy()
    buffer = QBuffer()
    buffer.setData(QByteArray(raw))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer, kind[1:].encode("ascii"))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > 32_000_000:
        raise ValueError("图片尺寸无效或超过预览上限")
    reader.setScaledSize(size.scaled(QSize(1440,1440),Qt.AspectRatioMode.KeepAspectRatio))
    picture = reader.read()
    if picture.isNull():
        raise ValueError(reader.errorString())
    return picture


def show_preview(parent, path: Path, content):
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"文件预览 · {path.name}" + ("（首页）" if path.suffix.lower()==".pdf" else ""))
    dialog.resize(780,580)
    layout = QVBoxLayout(dialog)
    if isinstance(content,str):
        view = QPlainTextEdit(content)
        view.setReadOnly(True)
        layout.addWidget(view)
    else:
        scroll = QScrollArea()
        view = QLabel()
        view.setPixmap(QPixmap.fromImage(content))
        scroll.setWidget(view)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(scroll)
    dialog.exec()
