# 隐藏桌面 · hide-desktop

Windows 小工具：**常驻后台，双击桌面空白处隐藏图标，再双击显示回来。**

给"想把桌面清空看壁纸、但又不想真的删图标"的人用。图标只是被隐藏（`SW_HIDE`），
随时双击空白处就能叫回来，不改动任何文件。

> 纯 Python 标准库 + `ctypes` 调 Win32 API，无第三方依赖。

## 特性

- **双击桌面空白处切换**图标显隐；在图标上双击仍然正常打开文件（不会误伤）
- 常驻后台，**无窗口、无托盘图标**，用完 `--exit` 关闭
- 可选开机自启；**开机只常驻，不擅自改你的图标状态**
- 幂等的命令行接口，方便脚本调用
- 自带五层自检（`--selftest`），含真实注入点击的端到端测试

## 用法

```
hide-desktop.exe                    常驻后台（双击桌面空白处 = 隐藏 / 显示图标）
hide-desktop.exe --hide             立即隐藏图标后退出
hide-desktop.exe --show             立即显示图标后退出
hide-desktop.exe --toggle           立即切换一次后退出
hide-desktop.exe --status           打印当前图标状态（终端可见）
hide-desktop.exe --exit             关闭正在运行的常驻实例
hide-desktop.exe --install-startup  设置开机自启（只常驻，不动图标）
hide-desktop.exe --uninstall-startup 取消开机自启
hide-desktop.exe --selftest         自检（会短暂切换图标，结束自动恢复原状）
hide-desktop.exe --help             显示帮助
```

无参数即进入常驻模式。启动第二次若已有实例在跑，会自动退出（单实例，靠命名互斥量）。

## 原理

### 桌面图标藏在哪

桌面图标的宿主窗口是一条固定层级：

```
Progman / WorkerW  →  SHELLDLL_DefView  →  SysListView32
```

对最底层的 `SysListView32` 调 `ShowWindow(SW_HIDE / SW_SHOW)` 即可整体隐藏 / 显示图标。
比发 `WM_COMMAND` 外壳消息可靠——新版本 Windows 会忽略那些消息。

状态**不落任何状态文件**，一律以 `IsWindowVisible` 实测为准，避免状态与实际不一致。

### 为什么是"轮询"而不是"鼠标钩子"

这是本项目最重要的一个选择，也是踩过坑的地方：

最初用 `WH_MOUSE_LL` 低级鼠标钩子监听双击，干净环境能用，但**常驻时会偶发彻底收不到事件**。

根因：低级钩子的回调是在"安装钩子的那个线程"里被派发的，且有 `LowLevelHooksTimeout`
（默认 300ms）。一旦回调超时，Windows 会**静默摘掉钩子**——不报错、不回调，表现为
"一开始能用，用着用着就死了"。只要回调线程被别的东西拖住（Python 的 GIL 被其他线程占用、
跨进程 `SendMessage` 卡住、枚举窗口变慢）就会踩中。

最终方案：**每 10ms 轮询一次** `GetAsyncKeyState(VK_LBUTTON)` + `GetCursorPos`，
检测"刚按下"的上升沿，配合 `GetDoubleClickTime()` 判定双击：

- 开销可忽略（每秒约 100 次、每次两次极轻量的 API 调用）
- 不装全局钩子 → 无超时、无跨进程回调、无 GIL 干扰
- 实测 100% 捕获点击（包括合成/注入的点击）

### 怎么区分"双击空白"和"双击图标"

双击第二击按下时，向 `SysListView32` 查当前选中项数量：

```c
SendMessageTimeoutW(lv, LVM_GETSELECTEDCOUNT, 0, 0, SMTO_ABORTIFHUNG, 250, &out);
```

- `> 0` → 第一击选中了某个图标 → **放行**，交给资源管理器正常打开文件
- `== 0` → 点在空白处 → 切换图标显隐

两个细节：

1. 用 `SendMessageTimeoutW` 而不是 `SendMessageW`。后者是同步阻塞的，资源管理器一卡，
   常驻循环就会被永久卡死。超时（返回 0）时**什么都不做**——问不出来就不动用户的桌面。
2. `LVM_GETSELECTEDCOUNT` 不带指针参数，跨进程调用安全。

### 双击容差

系统给的 `SM_CXDOUBLECLK` 默认只有 **4px**，那是给"精确重复点击"用的。
人手双击抖动轻松超过 5px，直接用系统值会大量漏判。所以位置容差取：

```python
max(系统值, 16)   # 像素
```

时间阈值用系统的 `GetDoubleClickTime()`（默认 500ms）。

## 构建

需要 Python 3.8+ 与 PyInstaller：

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name hide-desktop src/hide_desktop.py
```

产物在 `dist/hide-desktop.exe`。仓库不包含预编译二进制——请自行构建。

## 自检

```bash
python src/hide_desktop.py --selftest
```

五层，日志写在 exe 同目录的 `selftest.log`：

1. **显隐断言**：hide → `IsWindowVisible == False` → show → `== True` → 恢复原状
2. **双击判定阈值**：纯逻辑用例（快+近 = 双击 / 慢 / 远 / 手抖 8px 仍应识别）
3. **确定性测试**：把输入源抽成可替换的函数，测试时替换它们来驱动**真实的产品逻辑**
   （单击不触发 / 慢速两次不触发 / 空白双击触发 / 图标上双击放行）——不依赖任何注入 API
4. **真实注入端到端**：用 `SendInput` 注入真点击，断言图标真的切换
5. **真实读选中数校验**：单击图标应选中、单击空白应清选；并交叉校验
   `LVM_GETITEMCOUNT`，识别"图标存在但怎么点都读不到选中"这种读坏的情况

另有辅助脚本（均在 `src/`）：

| 脚本 | 用途 |
|---|---|
| `e2e_doubleclick_test.py` | 源码级端到端：起轮询 → 注入双击 → 断言切换 → 还原 |
| `e2e_exe_test.py` | exe 级端到端：启动常驻 → 注入双击 → 断言 → `--exit` |
| `audit_icon_guard.py` | 专门验证"双击图标不被误切换"这条路径 |
| `verify_setup.py` | 校验开机自启注册表项 |
| `hide_desktop_v1.py` | 历史版本（v1：跑一次即切换），保留备查 |

> 端到端脚本会临时把遮挡桌面的窗口挪开（**不最小化**——最小化后输入注入会被干扰），
> 测完精确还原窗口位置与图标状态。测试脚本的 `try/finally` 保证异常时也还原。

**自检第一原则：用户的桌面是用户的真实状态，不是测试场地。**
`--selftest` 整个过程包在 `try/finally` 里，无论哪一层失败，都会把图标恢复到原状。

## 已知限制

- 仅支持 Windows（依赖 `Progman/WorkerW → SysListView32` 这一桌面层级与 Win32 API）
- 若桌面被"第三方向 shells 替换"（少数美化工具），图标宿主窗口可能不是 `SysListView32`
- 双击判定基于"按下沿"，触摸板/触屏的双击手势未验证
- `--install-startup` 写的是当前用户的 `HKCU\...\Run`

## 许可

[MIT](LICENSE)
