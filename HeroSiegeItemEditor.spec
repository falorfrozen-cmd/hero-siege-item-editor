# -*- mode: python ; coding: utf-8 -*-
"""Reproducible one-file build for the Season 10 item editor.

Run from this directory after the generated roll database has been installed:
    py -3 -m PyInstaller --clean HeroSiegeItemEditor.spec

Keeping the generated roll implementation as Python code and the verified
profile/tooltip databases as data is intentional: the runtime independently
hash-checks every build-bound asset before Perfect/Best seeds or exact tooltip
numbers can be exposed.
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
    "hs_tooltip_roll_models.json",
)
datas = [(str(source_dir / name), ".") for name in required_data]
datas.append((str(source_dir / "item_icons"), "item_icons"))

binaries = []
hiddenimports = [
    "webview",
    "roll_profile_db",
    "generated_pool_model",
    "dice_skill_selector",
    "torch_class_selector",
    "game_build_identity",
    "hss_recovery",
    "infinite_vault",
    "exact_tooltip",
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
