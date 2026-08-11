# -*- mode: python ; coding: utf-8 -*-
import os
block_cipher = None

a = Analysis(
    ['../pc_app/OsciFootswitch.py'],
    pathex=[os.path.abspath('../pc_app')],
    binaries=[],
    datas=[('../assets/icon.ico', '.')],
    hiddenimports=[
        'pyvisa_py',
        'serial.tools.list_ports',
        'PIL.Image'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# --onefile: all binaries and data are bundled inside the single EXE.
# On first launch PyInstaller extracts to a temp folder; subsequent launches
# reuse the cached extraction so startup time is normal.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='OsciFootswitch',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='../assets/icon.ico',
    version='../assets/version.txt'
)
