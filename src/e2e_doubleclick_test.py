# -*- coding: utf-8 -*-
"""
端到端验证（v3 轮询版）：真实注入双击 -> 断言图标真的切换
========================================================
与正式路径 (hide_desktop.run_resident) 使用同一份 poll_once/detect_loop 逻辑。
流程：临时最小化遮挡桌面的大窗口（finally 还原）
     -> 后台线程跑 detect_loop（与常驻同一份代码）
     -> 实测扫出真空白点 -> 注入双击 -> 断言图标切换 -> 再双击 -> 断言切回
     -> 还原窗口与图标状态
必须真跑通过才允许交付。
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


def stat():
    return "presses=%s doubles=%s" % (hd._state["presses"], hd._state["doubles"])


def main():
    views = hd.find_icon_listviews()
    assert views, "desktop listview not found"
    orig = hd.current_visible(views)
    print("original icons:", "VISIBLE" if orig else "HIDDEN")

    # 1) 把遮挡桌面的大窗口挪到左半屏（finally 还原原矩形）
    #    实测：窗口被 SW_MINIMIZE 后沙箱会拦截输入注入（move_cursor 1/5）；
    #    MoveWindow 挪开则注入可用（5/5），且能真实露出桌面。
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
                U.ShowWindow(h, 9)
                time.sleep(0.4)
                U.GetWindowRect(h, ctypes.byref(r))
            moved.append((h, r.left, r.top, r.right, r.bottom))
            U.MoveWindow(h, 0, 0, W // 2, H, True)
        return True

    U.EnumWindows(ENUM(cb), 0)
    time.sleep(1.0)
    print("moved aside:", [(hd.window_class_of(h), (l, t, rr, b))
                           for h, l, t, rr, b in moved])

    stop = {"v": False}
    det = threading.Thread(target=hd.detect_loop, args=(lambda: stop["v"],),
                           daemon=True)
    rc = 1
    try:
        det.start()
        time.sleep(0.3)

        # 2) 实测扫出真空白点
        blank, why = hd._scan_blank_point(views, W, H)
        print("blank spot:", blank, "|", stat())
        assert blank, "no verified blank point (%s)" % why

        # 3) 注入双击 -> 图标应切换
        p0 = hd._state["presses"]
        hd._inject_double_click(*blank)
        time.sleep(0.9)
        v1 = hd.current_visible(views)
        print("after 1st double-click:", "VISIBLE" if v1 else "HIDDEN", "|", stat())
        assert hd._state["presses"] > p0, "polling did not see injected presses"
        assert hd._state["doubles"] >= 1, "polling did not detect a double-click"
        assert v1 != orig, "1st double-click did not toggle"

        # 4) 再双击 -> 图标应切回
        time.sleep(0.8)
        hd._inject_double_click(*blank)
        time.sleep(0.9)
        v2 = hd.current_visible(views)
        print("after 2nd double-click:", "VISIBLE" if v2 else "HIDDEN", "|", stat())
        assert v2 == orig, "2nd double-click did not toggle back"

        # 5) 双击在图标上必须放行（不能真注入双击，否则会打开文件）：
        #    单击选中图标后直接调用与轮询同一入口。
        icon_pt = None
        for gy in range(300, H - 140, 100):
            for gx in range(300, W - 100, 100):
                if not hd.is_desktop_point(gx, gy):
                    continue
                hd._inject_click(gx, gy)
                time.sleep(0.45)
                if hd.get_selected_count(views) > 0:
                    icon_pt = (gx, gy)
                    break
            if icon_pt:
                break
        if icon_pt:
            before = hd.current_visible(views)
            hd.handle_desktop_double_click(*icon_pt)
            after = hd.current_visible(views)
            print("double-click on icon @%s: %s -> %s (must not toggle)"
                  % (icon_pt, "VISIBLE" if before else "HIDDEN",
                     "VISIBLE" if after else "HIDDEN"))
            assert before == after, "double-click on ICON toggled icons (wrong!)"
            print("PASS: double-click on icon is ignored")
        else:
            print("note: no icon found at bare points, icon-guard check skipped")

        rc = 0
        print("E2E DOUBLE-CLICK TEST PASS")
    finally:
        stop["v"] = True
        det.join(timeout=2)
        for h, l, t, r, b in moved:
            U.MoveWindow(h, l, t, r - l, b - t, True)
        time.sleep(0.6)
        print("restored windows:", len(moved))
        print("icons restored:", hd.set_icons(views, orig))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
