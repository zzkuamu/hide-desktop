# -*- coding: utf-8 -*-
"""
exe 级端到端验证：直接跑打包好的 隐藏桌面.exe（常驻模式）
流程：临时最小化遮挡窗口 -> 启动 exe -> 注入双击 -> 断言图标切换
     -> 再双击断言切回 -> exe --exit 关闭 -> 断言进程退出 -> 还原窗口/图标
"""
import ctypes
import importlib.util
import os
import subprocess
import threading
import time
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 构建产物名随构建方式而定（如 隐藏桌面.exe / hide-desktop.exe），自动探测
_cands = sorted(f for f in os.listdir(ROOT) if f.lower().endswith(".exe"))
if not _cands:
    raise SystemExit("根目录下没有 exe，请先构建（见 README）")
EXE = os.path.join(ROOT, _cands[0])
spec = importlib.util.spec_from_file_location(
    "hd", os.path.join(ROOT, "src", "hide_desktop.py")
)
hd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hd)

U = hd.USER32


def proc_alive(proc):
    return proc.poll() is None


def main():
    views = hd.find_icon_listviews()
    assert views, "desktop listview not found"
    orig = hd.current_visible(views)
    print("exe:", EXE)
    print("original icons:", "VISIBLE" if orig else "HIDDEN")

    # 先确保没有旧实例。注意：不要用 capture_output —— PyInstaller onefile 的
    # bootloader 会派生子进程，管道不关闭会让 communicate() 一直等（实测挂 15s 超时）
    subprocess.run([EXE, "--exit"], timeout=20)
    time.sleep(0.8)

    # 1) 把遮挡桌面的大窗口挪到左半屏（不最小化！）
    #    实测：窗口被 SW_MINIMIZE 后，沙箱会拦截输入注入（move_cursor 1/5）；
    #    改成 MoveWindow 挪开则注入可用（5/5），且能真实露出桌面。
    moved = []
    W, H = U.GetSystemMetrics(0), U.GetSystemMetrics(1)
    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        if not U.IsWindowVisible(h):
            return True
        cls = hd.window_class_of(h)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return True
        r = wintypes.RECT()
        U.GetWindowRect(h, ctypes.byref(r))
        if (r.right - r.left) > W * 0.5 and (r.bottom - r.top) > H * 0.5:
            if U.IsIconic(h):
                U.ShowWindow(h, 9)  # SW_RESTORE
                time.sleep(0.4)
                U.GetWindowRect(h, ctypes.byref(r))
            moved.append((h, r.left, r.top, r.right, r.bottom))
            U.MoveWindow(h, 0, 0, W // 2, H, True)
        return True

    U.EnumWindows(ENUM(cb), 0)
    time.sleep(1.0)
    print("moved aside:", [(hd.window_class_of(h), (l, t, rr, b))
                           for h, l, t, rr, b in moved])

    proc = None
    rc = 1
    try:
        # 启动 exe（常驻）
        proc = subprocess.Popen([EXE], cwd=ROOT)
        time.sleep(2.5)
        assert proc_alive(proc), "exe exited immediately (should stay resident)"
        print("exe started, resident (pid=%s)" % proc.pid)

        # 扫真空白点（用 hd 的注入工具；沙箱偶发拒绝时会重试）
        blank, why = hd._scan_blank_point(views, W, H)
        print("blank spot:", blank)
        assert blank, "no verified blank point (%s)" % why

        # 双击 -> 隐藏
        hd._inject_double_click(*blank)
        time.sleep(1.2)
        v1 = hd.current_visible(views)
        print("after 1st double-click:", "VISIBLE" if v1 else "HIDDEN")
        assert v1 != orig, "exe did not toggle icons on double-click"

        # 再双击 -> 显示回来
        time.sleep(0.8)
        hd._inject_double_click(*blank)
        time.sleep(1.2)
        v2 = hd.current_visible(views)
        print("after 2nd double-click:", "VISIBLE" if v2 else "HIDDEN")
        assert v2 == orig, "exe did not toggle icons back"

        rc = 0
        print("EXE E2E PASS")
    finally:
        if proc and proc_alive(proc):
            r = subprocess.run([EXE, "--exit"], timeout=20)
            print("--exit rc =", r.returncode)
        time.sleep(1.2)
        if proc:
            print("exe alive after --exit:", proc_alive(proc))
        for h, l, t, r, b in moved:
            U.MoveWindow(h, l, t, r - l, b - t, True)
        time.sleep(0.6)
        print("restored windows:", len(moved))
        print("icons restored:", hd.set_icons(views, orig))
    # 收尾断言：--exit 之后进程必须真的退出
    assert proc is None or not proc_alive(proc), "--exit 没有关掉常驻实例"
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
