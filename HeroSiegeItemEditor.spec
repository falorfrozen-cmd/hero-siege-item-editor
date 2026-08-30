# -*- mode: python ; coding: utf-8 -*-
"""Reproducible one-file build for the Season 10 item editor.

Run from this directory after the generated roll database has been installed:
    py -3 -m PyInstaller --clean HeroSiegeItemEditor.spec

Keeping the generated model as Python code and the verified profile database as
data is intentional: both are independently hash-checked by roll_profile_db at
startup before any Perfect/Best Possible seed can be exposed.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


source_dir = Path(SPECPATH).resolve()
required_data = (
    "hs_full_catalog.json",
    "hs_runewords.json",
    "hs_sets.json",
    "hs_perfect_roll_profiles.json",
    "hs_dice_skill_targets.json",
)
datas = [(str(source_dir / name), ".") for name in required_data]
datas.append((str(source_dir / "item_icons"), "item_icons"))

binaries = []
hiddenimports = [
    "webview",
    "roll_profile_db",
    "generated_pool_model",
    "dice_skill_selector",
    "game_build_identity",
    "infinite_vault",
]
webview_bundle = collect_all("webview")
datas += webview_bundle[0]
binaries += webview_bundle[1]
hiddenimports += webview_bundle[2]


a = Analysis(
    [str(source_dir / "hs_item_editor_gui.py")],
    pathex=[str(source_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    name="HeroSiegeItemEditor",
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
)
