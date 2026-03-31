# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ANEMI Room Charge Importer.
Builds:
  - macOS: .app bundle  (BUNDLE block)
  - Windows: single .exe (EXE block)

Usage:
  pyinstaller anemi_room_charge.spec --clean --noconfirm
"""

import platform
import os

IS_MAC = platform.system() == "Darwin"

block_cipher = None

a = Analysis(
    ['toast_sales_importer.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('version.txt', '.'),
        ('version.json', '.'),
        ('updater', 'updater'),
        ('config/firebase.json', 'config'),
        ('icons/app_icon.ico', 'icons'),
        ('icons/app_icon.png', 'icons'),
    ],
    hiddenimports=[
        'updater',
        'updater.updater',
        'updater.update_dialog',
        'updater.version_manager',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RoomChargeAndSales',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon='icons/app_icon.ico',
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if IS_MAC:
    app = BUNDLE(
        exe,
        name='Room Charge & Sales.app',
        icon='icons/app_icon.ico',
        bundle_identifier='com.stamhad.room-charge-and-sales',
        info_plist={
            'CFBundleShortVersionString': open('version.txt').read().strip(),
            'CFBundleName': 'Room Charge & Sales',
            'NSHighResolutionCapable': True,
        },
    )
