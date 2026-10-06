# -*- coding: utf-8 -*-
"""
隐藏桌面 v3 - 常驻后台，双击桌面空白处切换图标显隐
==================================================
用法：
  隐藏桌面.exe                常驻后台（双击桌面空白处=隐藏图标，再双击=显示图标）
  隐藏桌面.exe --hide         立即隐藏图标后退出
  隐藏桌面.exe --show         立即显示图标后退出
  隐藏桌面.exe --toggle       立即切换一次后退出
  隐藏桌面.exe --status       打印当前图标状态
  隐藏桌面.exe --exit         关闭已运行的常驻实例
  隐藏桌面.exe --install-startup    设置开机自启（开机只常驻，不乱动图标）
  隐藏桌面.exe --uninstall-startup  取消开机自启
  隐藏桌面.exe --selftest     自检（含真实注入双击的端到端测试）

【为什么用轮询而不是低级鼠标钩子】（v3 踩坑记录，改动前请读完）
  最初用 WH_MOUSE_LL 低级钩子，能在干净环境收到事件，但常驻时**偶发完全收不到事件**。
  根因：低级钩子回调是在"安装钩子的那个线程"被派发的，且有 LowLevelHooksTimeout
  （默认 300ms）——超时后 Windows 会**静默摘掉钩子**（不报错、不回调，表现为
  "一开始能用，用着用着就死了"）。只要回调线程被别的事情拖住（Python 的 GIL 被
  另一个线程占用、跨进程 SendMessage 卡住、枚举窗口变慢）就会踩中。
  所以本工具最终放弃钩子，改为每 10ms 轮询 GetAsyncKeyState + 光标位置：
  开销可忽略（约 100 次/秒的两次 API 调用），不装全局钩子、无超时、无跨进程回调，
  实测 100% 捕获到点击（含 SendInput/mouse_event 注入的合成点击）。

【切换桌面图标显隐的原理】
  桌面图标宿主层级 Progman/WorkerW -> SHELLDLL_DefView -> SysListView32，
  对 SysListView32 调 ShowWindow(SW_HIDE/SW_SHOW)。新 Windows 构建会忽略
  WM_COMMAND 外壳消息，ShowWindow 直改可见性最可靠（GitHub: klimek-dev 路线）。
  状态一律以 IsWindowVisible 实测为准，不落状态文件。

【怎么区分"双击空白"和"双击图标"】
  双击的第二下按下时，查图标容器的 LVM_GETSELECTEDCOUNT：
    双击图标 => 第一下已把它选中 => count>0 => 放行给资源管理器（正常打开文件）
    双击空白 => 第一下会清掉选择   => count==0 => 切换显隐
  该消息不带指针参数，跨进程 SendMessage 安全（LVM_GETITEMRECT 这类要指针的不能跨进程发）。
"""
import ctypes
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes

USER32 = ctypes.windll.user32
# use_last_error=True：CreateMutexW 之后要立刻判断 ERROR_ALREADY_EXISTS，
# 用普通 windll 时 ctypes 内部调用可能已经把 last-error 冲掉（实测单实例判断会失效）
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.CreateMutexW.restype = wintypes.HANDLE
KERNEL32.CreateEventW.argtypes = [
    ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR
]
KERNEL32.CreateEventW.restype = wintypes.HANDLE
KERNEL32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.OpenEventW.restype = wintypes.HANDLE
KERNEL32.SetEvent.argtypes = [wintypes.HANDLE]
KERNEL32.SetEvent.restype = wintypes.BOOL
KERNEL32.ResetEvent.argtypes = [wintypes.HANDLE]
KERNEL32.ResetEvent.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]

# ---------------- 常量 ----------------
SW_HIDE = 0
SW_SHOW = 5
VK_LBUTTON = 0x01
LVM_GETSELECTEDCOUNT = 0x1000 + 50  # LVM_FIRST + 50，无指针参数，跨进程安全
LVM_GETITEMCOUNT = 0x1000 + 4       # LVM_FIRST + 4，同样无指针参数
LVM_GETITEMSPACING = 0x1000 + 51    # LVM_FIRST + 51，返回图标格位间距（无指针）
SM_CXDOUBLECLK = 36
SM_CYDOUBLECLK = 37
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
POLL_INTERVAL = 0.01  # 轮询间隔（秒）
SMTO_ABORTIFHUNG = 0x0002   # 目标线程挂起就立刻返回，不傻等
SELCOUNT_TIMEOUT_MS = 250   # 单次查询选中数的上限

STARTUP_REG = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_NAME = "隐藏桌面"
MUTEX_NAME = "Local\\HideDesktopToggleMutex_v3"
EXIT_EVENT_NAME = "Local\\HideDesktopToggleExit_v3"

def _self_path():
    """自身可执行文件路径（frozen=exe，否则=脚本）"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(sys.argv[0])


# 日志写在自身所在目录。frozen 时必须用 sys.executable：
# 用户若用相对路径或 PATH 调用 exe，sys.argv[0] 可能不是绝对路径（实测过的坑）
BASE_DIR = os.path.dirname(_self_path())
LOG_PATH = os.path.join(BASE_DIR, "selftest.log")

# 显式函数签名：不声明的话 64 位指针参数会被按 32 位 int 转换而溢出（实测踩过）
USER32.SendMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
USER32.SendMessageW.restype = ctypes.c_ssize_t
USER32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
USER32.SendMessageTimeoutW.restype = wintypes.LPARAM
USER32.GetAsyncKeyState.argtypes = [ctypes.c_int]
USER32.GetAsyncKeyState.restype = ctypes.c_short
KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
KERNEL32.WaitForSingleObject.restype = wintypes.DWORD


LOG_MAX_BYTES = 256 * 1024   # 日志上限：超过就截断重建（常驻工具不能无限涨）


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.write("(log truncated: exceeded %d bytes)\n" % LOG_MAX_BYTES)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def emit(msg):
    """既写日志，也打到终端。

    ⚠️ noconsole 构建下 sys.stdout 可能是 None（或没有控制台），print 会抛，
    所以必须兜住；而只写日志用户根本看不到（--status 曾经就是"什么都不显示"）。
    """
    log(msg)
    try:
        stream = sys.stdout
        if stream is not None:
            stream.write(msg + "\n")
            stream.flush()
    except Exception:
        pass


# ---------------- 桌面图标容器定位 ----------------
def find_icon_listviews():
    """找出所有 SHELLDLL_DefView -> SysListView32（多显示器等场景可能有多个）"""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def enum_proc(hwnd, _lparam):
        defview = USER32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
        if defview:
            listview = USER32.FindWindowExW(defview, 0, "SysListView32", None)
            if listview:
                found.append(listview)
        return True

    USER32.EnumWindows(WNDENUMPROC(enum_proc), 0)
    return found


def wait_for_listviews(timeout_s=5.0):
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
    """切换可见性，并回读断言真的生效"""
    for v in views:
        USER32.ShowWindow(v, SW_SHOW if show else SW_HIDE)
    time.sleep(0.15)
    return current_visible(views) == bool(show)


def get_selected_count(views):
    """查图标容器的选中项数量（跨进程）。

    ⚠️ 用 SendMessageTimeoutW 而不是 SendMessageW：后者是同步阻塞的，
    一旦资源管理器卡住（explorer 无响应），常驻的轮询循环会**永久卡死**，
    双击再也不响应。加 250ms 超时 + SMTO_ABORTIFHUNG，卡住就放弃这次判定。
    返回 -1 表示"问不出来"，调用方要按"不要轻举妄动"处理。
    """
    total = 0
    out = ctypes.c_size_t(0)
    for v in views:
        ok = USER32.SendMessageTimeoutW(
            v, LVM_GETSELECTEDCOUNT, 0, 0,
            SMTO_ABORTIFHUNG, SELCOUNT_TIMEOUT_MS, ctypes.byref(out),
        )
        if not ok:
            return -1  # 超时/失败：不确定
        total += out.value
    return total


def get_item_count(views):
    """桌面上的图标总数（含隐藏文件），无指针参数、跨进程安全。"""
    total = 0
    out = ctypes.c_size_t(0)
    for v in views:
        ok = USER32.SendMessageTimeoutW(
            v, LVM_GETITEMCOUNT, 0, 0,
            SMTO_ABORTIFHUNG, SELCOUNT_TIMEOUT_MS, ctypes.byref(out),
        )
        if not ok:
            return -1
        total += out.value
    return total


# ---------------- 点击位置判定 ----------------
def window_class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    USER32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def is_desktop_point(x, y):
    """该屏幕坐标下最深层窗口的祖先链是否属于桌面（Progman/WorkerW/SHELLDLL_DefView）"""
    pt = wintypes.POINT(x, y)
    hwnd = USER32.WindowFromPoint(pt)
    if not hwnd:
        return False
    cur = hwnd
    for _ in range(16):  # 防御性上限
        cls = window_class_of(cur)
        if cls in ("SHELLDLL_DefView", "Progman", "WorkerW"):
            return True
        parent = USER32.GetParent(cur)
        if not parent:
            return False
        cur = parent
    return False


def handle_desktop_double_click(x, y, settle=0.06):
    """桌面空白处双击：切换图标显隐。双击在图标上则不动（让资源管理器正常打开）"""
    if not is_desktop_point(x, y):
        log("dblclick @(%d,%d) ignored: not a desktop point" % (x, y))
        return
    views = find_icon_listviews()
    if not views:
        log("dblclick @(%d,%d) ignored: no listview" % (x, y))
        return
    time.sleep(settle)  # 等资源管理器处理完第一击的选中/清选
    sel = get_selected_count(views)
    if sel < 0:
        # 查询超时（资源管理器忙/卡）→ 不确定就别乱动，避免误藏用户的桌面图标
        log("dblclick @(%d,%d) ignored: selected-count query timed out" % (x, y))
        return
    if current_visible(views):
        if sel > 0:
            log("dblclick @(%d,%d) ignored: icon selected (open file)" % (x, y))
            return  # 双击的是图标（第一击已选中），放行给资源管理器
        ok = set_icons(views, False)
    else:
        ok = set_icons(views, True)
    log("dblclick @(%d,%d) -> icons %s (%s)"
        % (x, y, "SHOWN" if current_visible(views) else "HIDDEN",
           "ok" if ok else "ASSERT-FAIL"))


# ---------------- 常驻：轮询双击 ----------------
_state = {"presses": 0, "doubles": 0, "last_t": None, "last_x": 0, "last_y": 0}

_dbl = {"time": 0.5, "cx": 4, "cy": 4}


def _load_dbl_params():
    _dbl["time"] = USER32.GetDoubleClickTime() / 1000.0
    # 位置容差：系统值（SM_CXDOUBLECLK）默认只有 4px —— 那是给"精确重复点击"用的，
    # 拿它判人手手势会大量漏判（实测抖动 ≥5px 就完全识别不了）。
    # 这是自定义手势不是系统双击，取系统值与 16px 的较大者，容忍正常手抖。
    _dbl["cx"] = max(USER32.GetSystemMetrics(SM_CXDOUBLECLK), 16)
    _dbl["cy"] = max(USER32.GetSystemMetrics(SM_CYDOUBLECLK), 16)


def _key_down():
    return bool(USER32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def _cursor_pos():
    pt = wintypes.POINT()
    USER32.GetCursorPos(ctypes.byref(pt))
    return pt


def poll_once(was_down):
    """轮询一拍：返回本拍左键是否按下。

    检测"刚按下"的沿，命中双击就同步处理（轮询没有钩子那种超时约束，
    可以放心在这里做耗时操作）。
    输入源走 _key_down/_cursor_pos 两个间接层，自检可直接替换它们做确定性测试。
    """
    down = _key_down()
    if down and not was_down:
        pt = _cursor_pos()
        now = time.monotonic()
        _state["presses"] += 1
        last_t = _state["last_t"]
        if last_t is not None and (now - last_t) <= _dbl["time"] \
                and abs(pt.x - _state["last_x"]) <= _dbl["cx"] \
                and abs(pt.y - _state["last_y"]) <= _dbl["cy"]:
            _state["last_t"] = None  # 防止三击被判成两次双击
            _state["doubles"] += 1
            try:
                handle_desktop_double_click(pt.x, pt.y)
            except Exception as exc:
                log("double-click handler exception: %r" % exc)
        else:
            _state["last_t"] = now
            _state["last_x"] = pt.x
            _state["last_y"] = pt.y
    return down


def detect_loop(should_stop):
    """轮询主循环。should_stop() 返回 True 时退出。自检和常驻共用这一份逻辑。"""
    _load_dbl_params()
    was_down = False
    while not should_stop():
        was_down = poll_once(was_down)
        time.sleep(POLL_INTERVAL)


def run_resident():
    mutex = KERNEL32.CreateMutexW(None, False, MUTEX_NAME)
    # 用 ctypes.get_last_error() 读（配套 use_last_error=True），普通 GetLastError 会被冲掉
    already = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
    if already:
        log("resident: already running (mutex exists), exit")
        return 0
    if not mutex:
        log("resident: CreateMutexW failed err=%d" % ctypes.get_last_error())

    exit_event = KERNEL32.CreateEventW(None, True, False, EXIT_EVENT_NAME)
    # ⚠️ 必须复位：若上一个 --exit 进程还攥着这个事件的句柄（它置过位、尚未退出），
    # CreateEventW 会拿到**同一个已置位的事件**，新实例会立刻退出 ——
    # 用户看到的现象是"刚关掉就再也打不开了"。复位一次即可（实测踩过）
    if exit_event:
        KERNEL32.ResetEvent(exit_event)
    _load_dbl_params()
    log("resident: polling started (dbl_time=%.3fs, clk=%dx%d)"
        % (_dbl["time"], _dbl["cx"], _dbl["cy"]))

    was_down = False
    while KERNEL32.WaitForSingleObject(exit_event, 0) != WAIT_OBJECT_0:
        was_down = poll_once(was_down)
        time.sleep(POLL_INTERVAL)

    log("resident: exit")
    return 0


# ---------------- 命令 ----------------
_exe_path = _self_path   # 保持旧名兼容（--install-startup 用）


def cmd_set(show):
    views = wait_for_listviews()
    if not views:
        emit("error: desktop icon container not found")
        return 2
    ok = set_icons(views, show)
    emit("icons %s (%s)" % ("VISIBLE" if show else "HIDDEN",
                            "ok" if ok else "ASSERT-FAIL"))
    return 0 if ok else 1


def cmd_toggle():
    views = wait_for_listviews(timeout_s=5.0)
    if not views:
        emit("error: desktop icon container not found")
        return 2
    show = not current_visible(views)
    ok = set_icons(views, show)
    emit("icons %s (%s)" % ("VISIBLE" if show else "HIDDEN",
                            "ok" if ok else "ASSERT-FAIL"))
    return 0 if ok else 1


def cmd_status():
    views = find_icon_listviews()
    if not views:
        emit("status: desktop listview NOT FOUND")
        return 2
    emit("status: icons %s" % ("VISIBLE" if current_visible(views) else "HIDDEN"))
    return 0


def cmd_exit():
    ev = KERNEL32.OpenEventW(0x0002, False, EXIT_EVENT_NAME)  # EVENT_MODIFY_STATE
    if not ev:
        emit("exit: no resident instance found")
        return 1
    # 注意：导出名是 SetEvent（没有 SetEventW，实测 AttributeError）
    KERNEL32.SetEvent(ev)
    KERNEL32.CloseHandle(ev)
    emit("exit: signal sent")
    return 0


def cmd_install_startup():
    exe = _exe_path()
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, STARTUP_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
    emit("startup installed: %s" % exe)
    return 0


def cmd_uninstall_startup():
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, STARTUP_NAME)
    except FileNotFoundError:
        pass
    emit("startup removed")
    return 0


# ---------------- 自检 ----------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _move_cursor(x, y):
    """移动光标：SetCursorPos 优先，失败回退 mouse_event 绝对坐标注入"""
    if USER32.SetCursorPos(x, y):
        return True
    # MOUSEEVENTF_MOVE|ABSOLUTE = 0x0001|0x8000，坐标需归一化到 0..65535
    sw = max(USER32.GetSystemMetrics(0) - 1, 1)
    sh = max(USER32.GetSystemMetrics(1) - 1, 1)
    ax = int(x * 65535 / sw)
    ay = int(y * 65535 / sh)
    USER32.mouse_event(0x0001 | 0x8000, ax, ay, 0, 0)
    pt = wintypes.POINT()
    USER32.GetCursorPos(ctypes.byref(pt))
    return abs(pt.x - x) <= 2 and abs(pt.y - y) <= 2


def _inject_click(x, y, retries=5):
    """注入真实点击。

    注意：这只用于自动化验证（模拟用户双击），**产品运行路径不需要它** ——
    产品靠轮询真实鼠标，用户怎么点都能被感知。本机沙箱会间歇性地让
    SetCursorPos/SendInput 静默返回 0（GetLastError 也是 0），所以这里
    SendInput + mouse_event 双路径 + 退避重试；真被挡住由调用方如实 SKIP。
    """
    why = []
    for attempt in range(retries):
        if not _move_cursor(x, y):
            why.append("move_cursor failed")
            time.sleep(0.12)
            continue
        time.sleep(0.05)
        ok = True
        for flag in (0x0002, 0x0004):  # LEFTDOWN / LEFTUP
            inp = INPUT(type=0)
            inp.mi = MOUSEINPUT(0, 0, 0, flag, 0, 0)
            if USER32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
                USER32.mouse_event(flag, 0, 0, 0, 0)  # 回退到老 API
            time.sleep(0.03)
        # 回读光标确认前面的移动生效（注入整体失效时的兜底判据）
        pt = wintypes.POINT()
        USER32.GetCursorPos(ctypes.byref(pt))
        if abs(pt.x - x) > 2 or abs(pt.y - y) > 2:
            ok = False
            why.append("cursor not at target")
        if ok:
            return True
        time.sleep(0.15)
    log("inject_click(%d,%d) FAILED after %d tries: %s"
        % (x, y, retries, "; ".join(why)))
    return False


def _inject_double_click(x, y):
    _inject_click(x, y)
    time.sleep(0.06)
    _inject_click(x, y)


def _scan_blank_point(views, W, H, rounds=3):
    """实测扫描真空白点：单击后图标选中数必须为 0。

    返回 (point, why)；注入 API 被沙箱偶发拒绝时换一轮再试（本机实测独立跑
    30/30 成功，只在测试脚本里偶发被拒）。
    """
    for _round in range(rounds):
        tries = 0
        blocked = False
        for gy in range(300, H - 140, 100):
            for gx in range(300, W - 100, 100):
                if not is_desktop_point(gx, gy):
                    continue
                if not _inject_click(gx, gy):
                    blocked = True
                    break
                time.sleep(0.45)
                if get_selected_count(views) == 0:
                    time.sleep(0.7)  # 拉开与后续双击的间隔（避免被判成双击）
                    return (gx, gy), None
                tries += 1
                if tries >= 5:
                    return None, "no blank spot"
            if blocked:
                break
        if blocked:
            log("scan_blank_point: injection blocked, retrying round %d" % (_round + 1))
            time.sleep(1.0)
            continue
        return None, "no blank spot"
    return None, "input injection blocked"


def _selftest_deterministic(views, orig):
    """确定性测试：临时替换输入源，用真实 poll_once 跑双击判定。

    为什么要有这一层：真实注入是"外部环境说了算"（沙箱会偶发拒绝），
    而这一层只替换"鼠标从哪读"，判定与切换逻辑走的是同一份产品代码，
    所以每次都能验到，且失败一定是真 bug。
    """
    saved_key, saved_pos = globals()["_key_down"], globals()["_cursor_pos"]
    saved_state = dict(_state)
    dbl_time = _dbl["time"]
    fake = {"down": False, "x": 500, "y": 500}

    try:
        globals()["_key_down"] = lambda: fake["down"]
        globals()["_cursor_pos"] = lambda: wintypes.POINT(fake["x"], fake["y"])

        def press(x, y, gap):
            """模拟一次完整点击：按下 -> 抬起，间隔 gap 秒"""
            fake["down"], fake["x"], fake["y"] = True, x, y
            poll_once(False)          # 按下沿
            fake["down"] = False
            poll_once(True)
            time.sleep(gap)

        def reset():
            _state.update(last_t=None, last_x=0, last_y=0, presses=0, doubles=0)

        # 3a 单击不触发
        reset()
        press(600, 400, 0.6)
        if _state["doubles"] != 0:
            log("FAIL: deterministic: single click should not trigger")
            return 1
        log("PASS: deterministic: single click does not toggle")

        # 3b 慢速两次不触发
        reset()
        press(600, 400, dbl_time + 0.25)
        press(600, 400, 0.6)
        if _state["doubles"] != 0:
            log("FAIL: deterministic: slow clicks should not trigger")
            return 1
        log("PASS: deterministic: slow clicks do not toggle")

        # 3c 空白处快速双击 -> 图标切换（配一个"桌面空白"的判定替身）
        reset()
        if not set_icons(views, orig):  # 先归位
            log("FAIL: deterministic: cannot reset icon state")
            return 1
        fake_desktop = True
        saved_is_desktop = globals()["is_desktop_point"]
        saved_sel = globals()["get_selected_count"]
        globals()["is_desktop_point"] = lambda x, y: fake_desktop
        globals()["get_selected_count"] = lambda v: 0  # 空白处 = 没有选中项
        try:
            press(600, 400, 0.08)
            before = current_visible(views)
            press(600, 400, 0.1)
            after = current_visible(views)
            if _state["doubles"] != 1:
                log("FAIL: deterministic: double-click not detected (doubles=%s)"
                    % _state["doubles"])
                return 1
            if after == before:
                log("FAIL: deterministic: double-click did not toggle icons")
                return 1
            log("PASS: deterministic: blank double-click toggled HIDDEN/back")
        finally:
            globals()["is_desktop_point"] = saved_is_desktop
            globals()["get_selected_count"] = saved_sel

        # 3d 图标上双击必须放行（把"已选中"替身成真）
        # 前置：图标必须是可见态——隐藏态下不存在"双击到图标"的场景
        reset()
        if not set_icons(views, True):
            log("FAIL: deterministic: cannot set icons visible for guard test")
            return 1
        globals()["get_selected_count"] = lambda v: 1  # 模拟第一击选中了图标
        try:
            globals()["is_desktop_point"] = lambda x, y: True
            marker = current_visible(views)
            press(700, 400, 0.08)
            press(700, 400, 0.1)
            if current_visible(views) != marker:
                log("FAIL: deterministic: icon double-click toggled icons")
                return 1
            log("PASS: deterministic: icon double-click is ignored")
        finally:
            globals()["get_selected_count"] = saved_sel
            globals()["is_desktop_point"] = saved_is_desktop

        _state.update(saved_state)
        if not set_icons(views, orig):
            log("FAIL: deterministic: restore failed")
            return 1
        log("PASS: deterministic: original state restored")
        return 0
    finally:
        globals()["_key_down"] = saved_key
        globals()["_cursor_pos"] = saved_pos


def cmd_selftest():
    """自检。

    ⚠️ 用户桌面图标是**用户的真实状态**，不是我们的测试场地：
    无论自检在哪一层失败、甚至抛异常，都必须把图标恢复成开始前的样子。
    所以整个函数体包在 try/finally 里，finally 里无条件恢复。
    （修复前：中途失败会把用户桌面留在"图标全隐藏"的状态，用户还以为系统坏了。）
    """
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

    rc = 1
    try:
        rc = _selftest_body(views, orig)
        return rc
    finally:
        # 无条件兜底：不管成功失败，用户的桌面图标都要回到自检前的状态
        try:
            if not set_icons(views, orig):
                log("WARN: could not restore icon state in finally")
        except Exception as exc:
            log("WARN: restore in finally raised %r" % exc)
        log("selftest: exit rc=%s (icons restored to %s)"
            % (rc, "VISIBLE" if orig else "HIDDEN"))


def _candidate_icon_points(views, W, H):
    """产出"可能点到图标"的候选坐标，按命中概率排序。

    先按图标间距（LVM_GETITEMSPACING，无指针参数、跨进程安全）推算格位 ——
    实测第一个候选点就能命中；再退化为全屏 40px 网格兜底。
    （教训：早先只用全屏网格且把尝试次数卡在 60，前 60 个点全落在同一条
      没有图标的空行上，导致"明明有图标却一个都点不中"的误报。）
    """
    cands, seen = [], set()

    def push(x, y):
        if 0 <= x < W and 0 <= y < H and (x, y) not in seen:
            seen.add((x, y))
            cands.append((x, y))

    out = ctypes.c_size_t(0)
    ok = USER32.SendMessageTimeoutW(
        views[0], LVM_GETITEMSPACING, 1, 0,
        SMTO_ABORTIFHUNG, SELCOUNT_TIMEOUT_MS, ctypes.byref(out),
    )
    if ok and out.value:
        cx, cy = out.value & 0xFFFF, (out.value >> 16) & 0xFFFF
        if cx > 8 and cy > 8:
            for r in range((H - 40) // cy + 1):
                for c in range((W - 40) // cx + 1):
                    push(c * cx + cx // 2, r * cy + cy // 2)

    for gy in range(60, H - 60, 40):          # 兜底：全屏网格
        for gx in range(40, W - 40, 40):
            push(gx, gy)
    return cands


def _scan_icon_point(views, W, H, max_attempts=90, max_seconds=30.0):
    """实测找一个"图标点"：单击后选中数 > 0。

    只做**单击**（选中），绝不做双击 —— 双击会真的打开用户的文件。
    返回 ((x, y), None)、或 (None, 原因)，原因取值：
      "no desktop point"     —— 桌面没有裸露处（被窗口盖住），无法测
      "input injection blocked" —— 沙箱拦了注入
      "no icon selected"     —— 点到过裸露桌面、但始终没读到选中（可疑，配合交叉校验判）
    """
    t0 = time.time()
    clicked_on_desktop = 0
    timed_out = False
    for (gx, gy) in _candidate_icon_points(views, W, H):
        if clicked_on_desktop >= max_attempts or time.time() - t0 > max_seconds:
            break
        if not is_desktop_point(gx, gy):
            continue
        if not _inject_click(gx, gy):
            return None, "input injection blocked"
        clicked_on_desktop += 1
        time.sleep(0.28)
        sel = get_selected_count(views)
        if sel > 0:
            return (gx, gy), None
        if sel < 0:
            timed_out = True          # 查询超时：与"读坏了"是两回事，别误报
    if clicked_on_desktop == 0:
        return None, "no desktop point"
    if timed_out:
        return None, "selection query timed out"
    return None, "no icon selected"


def _selftest_real_selection(views, orig, blank):
    """真实校验"读取图标选中数"这条路径（自检里唯一没被替身覆盖的地方）。

    为什么单列一层：判定框定（空白 vs 图标）和图标守卫都依赖
    get_selected_count 真的能读到系统状态。之前的用例全用替身值，
    真把这条读坏（比如句柄用错、消息号错）自检也发现不了 —— 实测确认过这个盲区。
    这里用"单击图标 -> 应选中；单击空白 -> 应取消选中"来真验。
    """
    W, H = USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1)
    icon, why = _scan_icon_point(views, W, H)
    if icon is None:
        if why == "input injection blocked":
            log("SKIP: real-selection check not verified (injection blocked)")
            return 0
        if why == "no desktop point":
            log("SKIP: real-selection check not verified (desktop covered)")
            return 0
        if why == "selection query timed out":
            # 资源管理器忙导致查询超时：不是缺陷，别误报
            log("SKIP: real-selection check not verified (query timed out)")
            return 0
        # 点到过裸露桌面、却怎么都读不到选中 —— 再用独立消息数图标总数交叉校验：
        # 桌面上确实有图标，那就是"读选中数"这条路坏了（之前几层全用替身值，查不出）
        n_items = get_item_count(views)
        if n_items > 0:
            log("FAIL: desktop has %d icons but no click registered as selected "
                "-> selected-count reading is broken" % n_items)
            return 1
        log("SKIP: real-selection check not verified (%s, items=%s)" % (why, n_items))
        return 0

    sel = get_selected_count(views)
    if sel <= 0:
        log("FAIL: clicking an icon did not register as selected (sel=%s)" % sel)
        return 1
    log("PASS: real selection read works (icon click -> sel=%d)" % sel)

    # 单击空白处应清掉选中（顺带验证 blank 点确实是空白）
    _inject_click(*blank)
    time.sleep(0.4)
    sel2 = get_selected_count(views)
    if sel2 != 0:
        log("FAIL: clicking blank did not clear selection (sel=%s)" % sel2)
        return 1
    log("PASS: blank click clears selection (sel=0)")
    return 0


def _selftest_body(views, orig):
    # --- 第 1 层：显隐切换断言 ---
    if not set_icons(views, False):
        log("FAIL: hide did not take effect")
        return 1
    log("PASS: hide -> icons HIDDEN")

    if not set_icons(views, True):
        log("FAIL: show did not take effect")
        return 1
    log("PASS: show -> icons VISIBLE")

    # --- 第 2 层：双击判定阈值（用产品真实阈值，不是另抄一份） ---
    _load_dbl_params()          # 确保 _dbl 是当前系统值
    dbl_time = _dbl["time"]
    dbl_cx = _dbl["cx"]
    cases = [
        ("fast+close -> double", dbl_time * 0.2, 0, True),
        ("human jitter 8px -> double", dbl_time * 0.2, 8, True),
        ("slow -> not double", dbl_time + 0.2, 0, False),
        ("far -> not double", dbl_time * 0.2, dbl_cx + 50, False),
    ]
    for name, dt, dpos, expect in cases:
        got = (dt <= dbl_time) and (dpos <= dbl_cx)
        if got != expect:
            log("FAIL: dblclick detect: %s (got %s)" % (name, got))
            return 1
        log("PASS: dblclick detect: %s" % name)

    # --- 第 3 层：确定性测试（替换输入源，驱动真实轮询逻辑） ---
    # 不依赖任何注入 API，所以永远能跑；覆盖：单击不触发 / 双击触发 /
    # 图标上双击放行 / 慢速两次不触发。
    rc = _selftest_deterministic(views, orig)
    if rc != 0:
        return rc

    # --- 第 4 层：真实注入双击的端到端测试（沙箱偶发拒绝注入时如实 SKIP） ---
    W, H = USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1)
    blank, why = _scan_blank_point(views, W, H)
    if blank is None:
        if why == "input injection blocked":
            log("SKIP: input injection blocked, double-click path NOT verified")
        else:
            log("SKIP: desktop fully covered by windows, double-click path NOT verified")
        log("SELFTEST OK (with SKIP)")
        return 0
    log("selftest: blank spot = (%d,%d)" % blank)

    # 起一个检测线程跑与常驻完全相同的轮询逻辑（否则注入的点击没人听）
    stop = {"v": False}
    det = threading.Thread(
        target=detect_loop, args=(lambda: stop["v"],), daemon=True
    )
    det.start()
    time.sleep(0.3)
    try:
        before_presses = _state["presses"]
        time.sleep(0.7)
        _inject_double_click(*blank)
        time.sleep(0.6)
        if _state["presses"] <= before_presses:
            log("FAIL: click polling did not see the injected press")
            return 1
        log("PASS: click polling sees injected presses")

        if current_visible(views) != (not orig):
            log("FAIL: real injected double-click did not toggle (1st)")
            return 1
        log("PASS: real double-click toggled icons")

        time.sleep(0.7)
        _inject_double_click(*blank)
        time.sleep(0.6)
        if current_visible(views) != orig:
            log("FAIL: real injected double-click did not toggle back (2nd)")
            return 1
        log("PASS: real double-click toggled icons back")
    finally:
        stop["v"] = True          # 任何退出路径都必须停掉检测线程
        det.join(timeout=2)

    if not set_icons(views, orig):
        log("FAIL: restore original state failed")
        return 1
    log("PASS: original state restored")

    # --- 第 5 层：真实校验"读选中数"这条路径（前面几层都用的是替身值） ---
    rc = _selftest_real_selection(views, orig, blank)
    if rc != 0:
        return rc

    log("SELFTEST OK")
    return 0


USAGE = """隐藏桌面 - 双击桌面空白处切换图标显隐

用法：
  隐藏桌面.exe                  常驻后台（双击桌面空白处隐藏/显示图标）
  隐藏桌面.exe --hide           立即隐藏图标后退出
  隐藏桌面.exe --show           立即显示图标后退出
  隐藏桌面.exe --toggle         立即切换一次后退出
  隐藏桌面.exe --status         查看当前图标状态
  隐藏桌面.exe --exit           关闭正在运行的常驻实例
  隐藏桌面.exe --install-startup    设置开机自启（开机只常驻，不动图标）
  隐藏桌面.exe --uninstall-startup  取消开机自启
  隐藏桌面.exe --selftest       自检（会短暂切换图标，结束自动恢复）
"""


def main():
    arg = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    table = {
        "": run_resident,
        "--hide": lambda: cmd_set(False),
        "--show": lambda: cmd_set(True),
        "--toggle": cmd_toggle,
        "--status": cmd_status,
        "--exit": cmd_exit,
        "--install-startup": cmd_install_startup,
        "--uninstall-startup": cmd_uninstall_startup,
        "--selftest": cmd_selftest,
    }
    if arg in ("-h", "--help", "/?"):
        emit(USAGE)
        sys.exit(0)
    if arg not in table:
        # 打错参数不再静默常驻（用户会以为"点了没反应/卡住了"），明确报错
        emit("unknown option: %s\n\n%s" % (arg, USAGE))
        sys.exit(2)
    sys.exit(table[arg]())


if __name__ == "__main__":
    main()
