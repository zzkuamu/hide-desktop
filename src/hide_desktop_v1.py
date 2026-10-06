# -*- coding: utf-8 -*-
"""
隐藏桌面 - 双击切换桌面图标显隐的小工具
用法（双击 exe 即等于 --toggle）：
  隐藏桌面.exe                 切换桌面图标（显示<->隐藏）
  隐藏桌面.exe --hide          隐藏桌面图标
  隐藏桌面.exe --show          显示桌面图标
  隐藏桌面.exe --status        打印当前状态（仅控制台构建可见）
  隐藏桌面.exe --install-startup    设置开机自启（开机自动隐藏图标）
  隐藏桌面.exe --uninstall-startup  取消开机自启
  隐藏桌面.exe --selftest      自检（隐藏->断言->显示->断言->恢复原状）

原理（GitHub: klimek-dev/desktop-icon-toggler、mucahitimre/... 等已验证路线）：
  桌面图标宿主层级 Progman/WorkerW -> SHELLDLL_DefView -> SysListView32，
  直接对该 SysListView32 调 ShowWindow(SW_HIDE/SW_SHOW)。
  最新 Windows 构建会忽略 WM_COMMAND 外壳消息，ShowWindow 直改可见性最可靠。
  状态以 IsWindowVisible 实测为准，不需要状态文件。
"""
import ctypes
import os
import sys
import time
import winreg

USER32 = ctypes.windll.user32
SW_HIDE = 0
SW_SHOW = 5
STARTUP_REG = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "隐藏桌面"

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "selftest.log")


def log(msg):
    """--selftest 在无控制台构建下把结果写文件"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def find_icon_listviews():
    """枚举顶层窗口，找出所有含 SHELLDLL_DefView -> SysListView32 的桌面图标容器"""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
    )

    def enum_proc(hwnd, _lparam):
        defview = USER32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
        if defview:
            listview = USER32.FindWindowExW(defview, 0, "SysListView32", None)
            if listview:
                found.append(listview)
        return True

    USER32.EnumWindows(WNDENUMPROC(enum_proc), 0)
    return found


def wait_for_listviews(timeout_s=20.0):
    """开机自启时 explorer 可能还没就绪，重试找图标容器"""
    deadline = time.time() + timeout_s
    while True:
        views = find_icon_listviews()
        if views:
            return views
        if time.time() >= deadline:
            return []
        time.sleep(1.0)


def current_visible(views):
    return any(USER32.IsWindowVisible(v) for v in views)


def set_icons(views, show):
    api = USER32.ShowWindow
    for v in views:
        api(v, SW_SHOW if show else SW_HIDE)
    # 断言式收尾：调用完立刻回读，保证操作真的生效
    time.sleep(0.15)
    return current_visible(views) == bool(show)


def cmd_toggle():
    views = wait_for_listviews(timeout_s=5.0)
    if not views:
        return 2
    ok = set_icons(views, not current_visible(views))
    return 0 if ok else 1


def cmd_set(show):
    views = wait_for_listviews()
    if not views:
        return 2
    ok = set_icons(views, show)
    return 0 if ok else 1


def cmd_status():
    views = find_icon_listviews()
    if not views:
        log("status: desktop listview NOT FOUND")
        return 2
    log("status: icons %s" % ("VISIBLE" if current_visible(views) else "HIDDEN"))
    return 0


def _exe_path():
    # PyInstaller onefile 下 sys.executable 就是 exe 本身
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def cmd_install_startup():
    exe = _exe_path()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, '"%s" --hide' % exe)
    log("startup installed: %s --hide" % exe)
    return 0


def cmd_uninstall_startup():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, STARTUP_NAME)
    except FileNotFoundError:
        pass
    log("startup removed")
    return 0


def cmd_selftest():
    """负向友好自检：每一步都断言真生效，失败返回非 0"""
    try:
        os.remove(LOG_PATH)
    except OSError:
        pass
    views = wait_for_listviews(timeout_s=10.0)
    if not views:
        log("FAIL: desktop listview not found")
        return 2
    orig = current_visible(views)
    log("selftest: original state = %s" % ("VISIBLE" if orig else "HIDDEN"))

    if not set_icons(views, False):
        log("FAIL: hide did not take effect")
        return 1
    log("PASS: hide -> icons HIDDEN")

    if not set_icons(views, True):
        log("FAIL: show did not take effect")
        return 1
    log("PASS: show -> icons VISIBLE")

    if not set_icons(views, orig):
        log("FAIL: restore original state failed")
        return 1
    log("PASS: original state restored")
    log("SELFTEST OK")
    return 0


def main():
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "--toggle"
    table = {
        "--toggle": cmd_toggle,
        "--hide": lambda: cmd_set(False),
        "--show": lambda: cmd_set(True),
        "--status": cmd_status,
        "--install-startup": cmd_install_startup,
        "--uninstall-startup": cmd_uninstall_startup,
        "--selftest": cmd_selftest,
    }
    func = table.get(arg, cmd_toggle)
    sys.exit(func())


if __name__ == "__main__":
    main()
