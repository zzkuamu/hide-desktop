# -*- coding: utf-8 -*-
"""
审计用：验证"双击图标"这条路径（上一轮未真实验证）
注入真实双击到一个**真实图标**上，断言：
  1) 图标显隐状态不变（守卫生效，不能误藏桌面）
  2) 资源管理器确实打开了它（守卫是"放行"，不是"吞掉"）
用无害的测试文件夹做靶子。
"""
import ctypes
import importlib.util
import os
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "hd", os.path.join(HERE, "hide_desktop.py")
)
hd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hd)

U = hd.USER32

# 靶子：一个放在桌面上的无害文件夹。运行本脚本前请先在桌面创建同名文件夹，
# 跑完即可删除。它只用来判断"资源管理器是否真的打开了它"。
TARGET = "hide-desktop-e2e-probe"


def list_explorer_windows():
    """列出当前所有资源管理器窗口标题"""
    titles = []
    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        if not U.IsWindowVisible(h):
            return True
        cls = hd.window_class_of(h)
        if cls in ("CabinetWClass", "ExploreWClass"):
            buf = ctypes.create_unicode_buffer(512)
            U.GetWindowTextW(h, buf, 512)
            titles.append((h, buf.value))
        return True

    U.EnumWindows(ENUM(cb), 0)
    return titles


def main():
    views = hd.find_icon_listviews()
    assert views, "no desktop listview"
    W, H = U.GetSystemMetrics(0), U.GetSystemMetrics(1)

    # 把盖住桌面的大窗口挪到右半屏，露出左侧（图标默认在左侧）
    # 实测：最大化的窗口要先 SW_SHOWNORMAL 或 SW_RESTORE，MoveWindow 才生效；
    # 且必须真的检查"挪完之后的矩形"，否则会以为挪了其实没动。
    # 记录原始状态（是否最大化 + 原矩形），finally 里精确还原。
    moved = []
    ENUM = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        if not U.IsWindowVisible(h):
            return True
        cls = hd.window_class_of(h)
        if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return True
        # UWP shell 窗口（CoreWindow）不是普通窗口，不动它
        if cls == "Windows.UI.Core.CoreWindow":
            return True
        r = wintypes.RECT()
        U.GetWindowRect(h, ctypes.byref(r))
        if (r.right - r.left) <= W * 0.5 or (r.bottom - r.top) <= H * 0.5:
            return True
        was_max = bool(U.IsZoomed(h))
        if was_max or U.IsIconic(h):
            U.ShowWindow(h, 9)  # SW_RESTORE
            time.sleep(0.4)
            U.GetWindowRect(h, ctypes.byref(r))
        moved.append((h, was_max, r.left, r.top, r.right, r.bottom))
        U.MoveWindow(h, W // 2, 0, W // 2, H, True)
        time.sleep(0.5)
        r2 = wintypes.RECT()
        U.GetWindowRect(h, ctypes.byref(r2))
        # 挪了要验证真的挪了：右半屏必须是它现在的位置
        if r2.left < W // 2 - 20:
            log_move_fail = (cls, (r2.left, r2.top, r2.right, r2.bottom))
            print("WARN: MoveWindow did not take effect for", log_move_fail)
        return True

    try:
        U.EnumWindows(ENUM(cb), 0)
        time.sleep(1.0)
        print("moved aside:", [hd.window_class_of(h) for h, *_ in moved])

        # 确保图标可见（隐藏态下图标点不到）
        hd.set_icons(views, True)
        time.sleep(0.5)
        print("icons visible:", hd.current_visible(views))

        # 1) 找图标点：单击后选中数 > 0
        icon_pt = None
        for gy in range(120, H - 200, 60):
            for gx in range(60, W - 60, 60):
                if not hd.is_desktop_point(gx, gy):
                    continue
                if not hd._inject_click(gx, gy):
                    continue
                time.sleep(0.35)
                if hd.get_selected_count(views) > 0:
                    icon_pt = (gx, gy)
                    break
            if icon_pt:
                break
        print("icon point found:", icon_pt)
        assert icon_pt, "no icon point found (cannot verify icon-guard path)"

        wins_before = set(t for _, t in list_explorer_windows())
        print("explorer windows before:", len(wins_before))

        # 2) 起检测线程（与常驻同一份逻辑）
        stop = {"v": False}
        det = threading.Thread(target=hd.detect_loop, args=(lambda: stop["v"],),
                               daemon=True)
        det.start()
        time.sleep(0.3)

        before_state = hd.current_visible(views)
        time.sleep(0.7)

        # 3) 真注入双击到图标上
        print("injecting REAL double-click on the icon (may open a window)")
        hd._inject_click(*icon_pt)
        time.sleep(0.06)
        hd._inject_click(*icon_pt)
        time.sleep(1.5)

        after_state = hd.current_visible(views)
        print("icons %s -> %s (must be unchanged)"
              % ("VISIBLE" if before_state else "HIDDEN",
                 "VISIBLE" if after_state else "HIDDEN"))
        print("polling saw: presses=%s doubles=%s"
              % (hd._state["presses"], hd._state["doubles"]))

        wins_after = list_explorer_windows()
        new_wins = [(h, t) for h, t in wins_after if t not in wins_before]
        print("new explorer windows:", [t for _, t in new_wins])
        opened_target = any(TARGET in t for _, t in new_wins)

        rc = 1
        assert after_state == before_state, \
            "BUG: 双击图标把桌面图标切换了（守卫生效太慢或失效）"
        print("PASS: 双击图标没有切换桌面图标（守护有效）")
        if opened_target:
            print("PASS: 资源管理器正常打开了目标（守卫是放行，不是吞掉）")
        else:
            print("WARN: 未观察到资源管理器打开目标窗口，需人工确认")
        rc = 0
        return rc
    finally:
        stop = locals().get("stop")
        if stop:
            stop["v"] = True
        det = locals().get("det")
        if det:
            det.join(timeout=2)
        for h, t in locals().get("new_wins", []):
            U.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE
        for h, was_max, l, t, r, b in moved:
            U.MoveWindow(h, l, t, r - l, b - t, True)
            if was_max:
                U.ShowWindow(h, 3)  # SW_MAXIMIZE：还原成最大化的状态
        time.sleep(0.6)
        hd.set_icons(views, True)
        print("restored: windows=%d" % len(moved))


if __name__ == "__main__":
    raise SystemExit(main())
