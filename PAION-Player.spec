# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = []
hidden += collect_submodules("flask")
hidden += collect_submodules("jinja2")
hidden += collect_submodules("werkzeug")
hidden += collect_submodules("click")
hidden += collect_submodules("itsdangerous")
hidden += collect_submodules("markupsafe")

# Your modules (if present)
hidden += ["verify_aifm"]

a = Analysis(
    ["SRC/paion_player.py"],
    pathex=[".", "SRC"],
    binaries=[],
    datas=[("SRC", "SRC")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="PAION",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

app = BUNDLE(
    exe,
    name="PAION.app",
    icon=None,
    bundle_identifier=None,
)
