# -*- mode: python ; coding: utf-8 -*-
import os


# vendor/ 里的第三方预编译产物要一起打包（现在只有真 fastfetch）。
#
# 用 datas 而不是 binaries：binaries 会让 PyInstaller 跑 ldd 把依赖
# 一起收进来，可能连 libc 都塞进包里，反而更脆。
# fastfetch 只依赖系统 libc/libm，直接拷文件就行。
#
# 代价是 onefile 解包时可能丢执行位 —— 运行时的 vendor_binary()
# 会补一个 chmod +x，见 src/jiahao/utils/fakecmd.py。
_vendor = [
    (os.path.join('vendor', name), 'vendor')
    for name in sorted(os.listdir('vendor'))
    if os.path.isfile(os.path.join('vendor', name))
] if os.path.isdir('vendor') else []


a = Analysis(
    ['src/jiahao/jiahaoshell.py'],
    pathex=[],
    binaries=[],
    datas=_vendor,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='jiahaoshell',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
