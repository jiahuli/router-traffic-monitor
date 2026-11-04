# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('web', 'web'), ('icon.ico', '.')],
    hiddenimports=['app', 'collector', 'storage', 'router_client', 'services'],
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
    name='路由器流量监控',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
