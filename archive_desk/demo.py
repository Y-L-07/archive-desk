"""在独立临时目录创建演示用文件。"""
import tempfile
from pathlib import Path


def create_demo() -> Path:
    """只在新建的独立临时目录中写入演示数据，保留以供跨会话撤销。"""
    import base64
    import zipfile

    root = Path(tempfile.mkdtemp(prefix="ArchiveDesk-demo-"))
    (root / "文档").mkdir()
    (root / "旧资料").mkdir()
    text = "周会纪要\n下周三前确认交付清单。\n"
    (root / "会议纪要.txt").write_text(text, encoding="utf-8")
    (root / "会议纪要（副本）.txt").write_text(text, encoding="utf-8")
    (root / "收支记录.csv").write_text("日期,项目,金额\n11-01,文具,20\n", encoding="utf-8-sig")
    (root / "文档" / "收支记录.csv").write_text("保留已有账表\n", encoding="utf-8-sig")
    (root / "旧资料" / "读书笔记.md").write_text("# 读书笔记\n", encoding="utf-8")
    # Recursive mode exposes a batch collision with equal names and lengths,
    # but different contents. Both must remain outside the duplicate group.
    for source, payload in (("上海来件", b"SH-01"), ("杭州来件", b"HZ-01")):
        (root / source).mkdir()
        (root / source / "交接单.txt").write_bytes(payload)
    (root / "参考图片.png").write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAAAwCAIAAAAuKetIAAABD0lEQVR4nO3PwQ1CIRCE4a3EuizAq1XYgiXYjwV48u7ZsyYk5IXwgB12mUdCMlfY/5PX5z31hF6wAOyCBWAXTA/4Pm/bTQZI6gEDE5Ct1xoWYAEWALp9fdz9DKofEMC/PszDoH2uBsR6Q0PPdICk/ggGBSBbTze0Agr1XEMToFpPNNQBjfUsQwWgqqcYSgCgfrxhFwDXDzbkAZ317YbT5RxmCTCprxpi+nYGAMP6PUM2HWaIa31iqKYDDPGuD1OlqxjiXQ+nNzLEr94kvcqQKdILDGOAa3qWYQYYlp4wDACU9LguADe9C0DvxgH0YhxAb8UB9EocQO/DAfQyHEBvwgH0GhxA78AB9IIFYBd07gdNgtaAFn0njQAAAABJRU5ErkJggg=="))
    with zipfile.ZipFile(root / "资料备份.zip", "w") as bundle:
        bundle.writestr("readme.txt", "Archive Desk sample")
    (root / "安装示例.msi").write_bytes(b"Archive Desk demo placeholder")
    (root / "测量记录.dat").write_bytes(b"sample data")
    return root
