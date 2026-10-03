# -*- mode: python ; coding: utf-8 -*-
"""单文件打包：一个可执行文件 = TUI + 内层 shell + 真 fastfetch。

## 为什么合成一个

以前是 ``jiahao`` 和 ``jiahaoshell`` 两个产物，分发时要保证它俩待在一起。
现在 TUI 用 ``[sys.executable, "--shell-mode"]`` **重新执行自己**来拉起内层
shell（见 ``src/jiahao/__main__.py``），一个文件就够了。

## vendor 的取舍

只打包**当前平台**的那份 fastfetch —— 不然 Windows 包里会塞进 Linux
二进制，白胖一倍。判断用的是 ``compat.machine_tag()``，因为
``platform.machine()`` 在 Windows 上叫 ``AMD64``、Linux 上叫 ``x86_64``。

许可文件无论如何都带上：fastfetch 是 MIT，再分发要带声明。
"""
import os
import platform
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "src"))
try:
    from jiahao.utils.compat import machine_tag
except Exception:                            # noqa: BLE001 - 打包早期不该炸
    def machine_tag() -> str:
        return platform.machine().lower()

_want = f"fastfetch-{platform.system().lower()}-{machine_tag()}"
if platform.system() == "Windows":
    _want += ".exe"

_vendor = []
if os.path.isdir("vendor"):
    for _name in sorted(os.listdir("vendor")):
        _path = os.path.join("vendor", _name)
        if not os.path.isfile(_path):
            continue
        # 别的平台的 fastfetch 不打包；许可/说明一律带上
        if _name.startswith("fastfetch-") and not _name.startswith("fastfetch-"):
            continue
        if (_name.startswith("fastfetch-")
                and not _name.startswith("fastfetch-LICENSE")
                and _name != _want):
            continue
        _vendor.append((_path, "vendor"))

# ---- 许可与免责声明 ----
#
# 打包进 exe 里（解包到 _MEIPASS），同时在发布 zip 里也放一份明文副本。
# 之所以两边都放：MIT/PSF 要求"再分发时保留声明"，而单文件 exe 里的
# 东西用户不一定找得到 —— 放外面一份最稳妥。
_legal = [
    (_f, ".")
    for _f in ("LICENSE", "THIRD-PARTY-NOTICES.txt", "DISCLAIMER.md")
    if os.path.isfile(_f)
]


a = Analysis(
    ["src/jiahao/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=_vendor + _legal,
    hiddenimports=["jiahao.main", "jiahao.term", "jiahao.jiahaoshell"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="jiahao",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 会让部分杀软误报，体积收益也不大
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
