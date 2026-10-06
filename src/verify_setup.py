# -*- coding: utf-8 -*-
"""
一次性验证脚本：确认"开机自启"注册表项指向本项目根目录下的 exe，
且是 v3 常驻模式（不带 --hide，避免开机就擅自隐藏图标）。

路径全部动态推导，不含任何机器相关信息。
"""
import os
import winreg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_exe():
    """在本项目根目录下找构建好的 exe（名字随构建产物而定）"""
    cands = [f for f in os.listdir(ROOT) if f.lower().endswith(".exe")]
    if not cands:
        raise SystemExit("根目录下没有 exe，请先构建（见 README）")
    # exe 是本机绝对路径（大小写不敏感去重）
    return os.path.join(ROOT, sorted(cands)[0])


EXE = find_exe()

k = winreg.OpenKey(
    winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
)
v, _ = winreg.QueryValueEx(k, "隐藏桌面")
print("Run value =", repr(v))
assert v.strip('"') == EXE, "自启键指向的不是本项目根目录的 exe：%r != %r" % (v, EXE)
assert "--hide" not in v, "v3 常驻模式不应带 --hide（否则开机就擅自藏图标）"
assert os.path.exists(EXE), "exe missing"
print("STARTUP OK: 纯常驻，开机不擅自改图标")
