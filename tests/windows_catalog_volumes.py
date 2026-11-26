"""对新建临时目录进行真实 Windows 盘符重绑、DACL 及快照验收。"""
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from archive_desk.catalog import Catalog
from archive_desk.catalog_snapshots import SnapshotStore


def windows_command(name, *arguments, cwd=None):
    binary = Path(os.environ["SystemRoot"]) / "System32" / f"{name}.exe"
    return subprocess.run([str(binary), *map(str, arguments)], cwd=cwd, timeout=15,
                          capture_output=True, check=True).stdout


kernel = ctypes.WinDLL("kernel32", use_last_error=True)
query_device = kernel.QueryDosDeviceW
query_device.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
query_device.restype = wintypes.DWORD
define_device = kernel.DefineDosDeviceW
define_device.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR)
define_device.restype = wintypes.BOOL
security = ctypes.WinDLL("advapi32", use_last_error=True)
get_security = security.GetFileSecurityW
get_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD))
get_security.restype = wintypes.BOOL
set_security = security.SetFileSecurityW
set_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
set_security.restype = wintypes.BOOL
DACL_INFORMATION = 4


def saved_dacl(path):
    size = wintypes.DWORD()
    get_security(str(path), DACL_INFORMATION, None, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(size.value)
    if not get_security(str(path), DACL_INFORMATION, descriptor, len(descriptor), ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return descriptor


def device(letter):
    buf = ctypes.create_unicode_buffer(32768)
    if query_device(letter, buf, len(buf)):
        return buf.value
    error = ctypes.get_last_error()
    if error != 2:
        raise ctypes.WinError(error)
    return None


def free_letters():
    # Query the DOS namespace as well as the logical-drive mask, avoiding
    # already mapped, reserved and disconnected DOS devices.
    mask = kernel.GetLogicalDrives()
    if not mask:
        raise ctypes.WinError(ctypes.get_last_error())
    available = []
    for code in range(ord("Z"), ord("D") - 1, -1):
        letter = chr(code) + ":"
        if not mask & (1 << (code - ord("A"))) and device(letter) is None:
            available.append(letter)
            if len(available) == 2:
                return available
    raise unittest.SkipTest("需要两个空闲盘符，系统现有映射保持原状")


@contextmanager
def mapped(letter, folder):
    if device(letter) is not None:
        raise RuntimeError(f"盘符 {letter} 已被占用")
    raw_target = "\\??\\" + str(folder)
    windows_command("subst", letter, folder)
    try:
        if (device(letter) or "").casefold() != raw_target.casefold():
            raise RuntimeError("SUBST 映射与新建样例目录不一致")
        yield Path(letter + "\\")
    finally:
        # DDD_RAW_TARGET_PATH | DDD_REMOVE_DEFINITION | DDD_EXACT_MATCH_ON_REMOVE.
        # This undoes only our exact temporary mapping, even if another mapping
        # was pushed onto the same letter before cleanup.
        if not define_device(0x1 | 0x2 | 0x4, letter, raw_target):
            raise ctypes.WinError(ctypes.get_last_error())


class CatalogWindowsIntegration(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="archive-catalog-windows-")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve(strict=True)
        self.db = self.scratch / "test.db"
        self.catalog = Catalog(self.db)
        self.snapshots = SnapshotStore(self.db)

    def test_real_subst_rebind_and_same_path_wrong_directory(self):
        first, second = free_letters()
        volume = self.scratch / "盘符样例"
        fake = self.scratch / "同盘另一目录"
        real_root = volume / "资料"
        impostor = fake / "资料"
        real_root.mkdir(parents=True)
        impostor.mkdir(parents=True)
        (real_root / "收据.txt").write_bytes(b"saved manifest")
        (impostor / "收据.txt").write_bytes(b"unrelated file")
        with mapped(first, volume) as drive:
            root_id = self.catalog.add_root("资料盘", drive / "资料")
            before = self.catalog.refresh(root_id, hash_files=True)
            original = self.snapshots.create(root_id, "第一次扫描")
        self.assertFalse(self.catalog.probe(root_id))

        with mapped(first, fake) as wrong_drive, mapped(second, volume) as new_drive:
            self.assertFalse(self.catalog.probe(root_id))
            with self.assertRaises(ValueError):
                self.catalog.rebind(root_id, wrong_drive / "资料", confirmed=True)
            with self.assertRaises(ValueError):
                self.catalog.refresh(root_id, hash_files=True)
            state, = self.catalog.roots()
            self.assertEqual((state["generation"], state["online"]), (before["generation"], 0))
            self.assertEqual(self.snapshots.list_snapshots(root_id)[0]["id"], original)
            with self.assertRaises(ValueError):
                self.catalog.rebind(root_id, new_drive / "资料")
            self.catalog.rebind(root_id, new_drive / "资料", confirmed=True)
            self.assertTrue(self.catalog.probe(root_id))
            after = self.catalog.refresh(root_id, hash_files=True)
            self.assertEqual((before["dev"], before["ino"]), (after["dev"], after["ino"]))
            self.assertEqual(Path(after["path"]), new_drive / "资料")
            existing = self.snapshots.create(root_id, "新盘符扫描")
            self.assertEqual(self.snapshots.compare(original, existing), [])
        self.assertFalse(self.catalog.probe(root_id))
        self.assertEqual(self.snapshots.compare(original, existing), [])
        self.assertEqual((impostor / "收据.txt").read_bytes(), b"unrelated file")
        self.assertIsNone(device(first))
        self.assertIsNone(device(second))
        print(f"SUBST {first} → {second} 身份重绑通过，同路径假根被拒绝；两个映射已清理。")

    def test_denied_directory_list_is_uncertain_then_exact_dacl_restored(self):
        source = self.scratch / "本地样例"
        private = source / "仅本次受限"
        readable = source / "公开"
        private.mkdir(parents=True)
        readable.mkdir()
        document = private / "报告.txt"
        document.write_bytes(b"retain me")
        obsolete = readable / "旧稿.txt"
        obsolete.write_bytes(b"remove this generated example")
        root_id = self.catalog.add_root("权限样例", source)
        self.catalog.refresh(root_id, hash_files=True)
        original = self.snapshots.create(root_id, "正常权限")
        original_dacl = saved_dacl(private)
        sid = re.search(rb"S-1-[0-9-]+", windows_command("whoami", "/user", "/fo", "csv", "/nh"))
        self.assertIsNotNone(sid)
        # The deny ACE applies only to listing this new directory. Restore the
        # DACL directly by name, which needs WRITE_DAC but never lists it.
        try:
            windows_command("icacls", private.name, "/deny", "*" + sid[0].decode("ascii") + ":(RD)",
                            cwd=source)
            with self.assertRaises(PermissionError):
                list(private.iterdir())
            obsolete.unlink()
            scan = self.catalog.refresh(root_id, hash_files=True)
            self.assertEqual((scan["published_state"], scan["issue_count"]), ("partial", 1))
            partial = self.snapshots.create(root_id, "受限扫描")
            differences = self.snapshots.compare(original, partial)
            actual = {(r["old_path"], r["status"]) for r in differences}
            self.assertEqual(actual, {("仅本次受限/报告.txt", "uncertain"), ("公开/旧稿.txt", "missing")})
            self.assertEqual(self.snapshots.issues(partial)[0]["relpath"], private.name)
        finally:
            if not set_security(str(private), DACL_INFORMATION, original_dacl):
                raise ctypes.WinError(ctypes.get_last_error())
        self.assertEqual(original_dacl.raw, saved_dacl(private).raw)
        self.assertEqual(document.read_bytes(), b"retain me")
        scan = self.catalog.refresh(root_id, hash_files=True)
        self.assertEqual((scan["published_state"], scan["issue_count"]), ("complete", 0))
        recovered = self.snapshots.create(root_id, "权限恢复")
        self.assertEqual([(r["old_path"], r["status"]) for r in self.snapshots.compare(original, recovered)],
                         [("公开/旧稿.txt", "missing")])
        print("真实拒绝目录列表访问已记录为无法确认；可读相邻目录删除被识别，恢复前后 DACL 一致。")


if __name__ == "__main__":
    unittest.main(verbosity=2)
