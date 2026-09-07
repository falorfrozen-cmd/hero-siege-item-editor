"""Report whether the ForgePact runtime that applies Custom Forge stats is present.

Custom Forge never touches the save file's stats: Hero Siege rebuilds every
item's ``itemStatStruct`` from the compact definition on load, so the editor
writes a sidecar (``hs_custom_item_forge.runtime``) and the ForgePact plugin
(``mods\\aurie\\BloodPactPlugin.dll``) overlays those stats while the game
creates the item.  Without that plugin, or with a plugin build that predates
the sidecar format, forged stats silently never appear in game.

This module answers three questions the editor could not answer before:

* Which Hero Siege installation will actually run?  (ForgePact's own panel
  config, the running ``Hero_Siege.exe`` and the Steam default are checked.)
* Is a Custom-Forge-capable plugin installed there?  A capable build embeds the
  sidecar header string, which older builds never mention.
* Did the plugin already read the current sidecar?  The plugin writes
  ``bp_ipc\\customforge_status.json`` on every start; its timestamp relative to
  the sidecar tells whether a restart is still needed.

Everything here is read-only and must never block editing.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

RUNTIME_MARKER = b"HS_CUSTOM_ITEM_FORGE_V1"
PLUGIN_RELATIVE = Path("mods") / "aurie" / "BloodPactPlugin.dll"
STATUS_RELATIVE = Path("bp_ipc") / "customforge_status.json"
AURIE_CORE_NAME = "AurieCore.dll"
SIDECAR_NAME = "hs_custom_item_forge.runtime"
FORGEPACT_CONFIG_NAME = "forgepact.json"
STEAM_DEFAULT_EXE = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\HeroSiege\bin\Hero_Siege.exe"
)
# Plugin builds are well under a megabyte; refuse to scan anything absurd.
MAX_PLUGIN_SCAN_BYTES = 64 * 1024 * 1024
CREATE_NO_WINDOW = 0x08000000

ExeLookup = Callable[[], "Path | None"]


def forgepact_game_exe(root: Path) -> Path | None:
    """Return the game executable the ForgePact panel is configured for."""

    try:
        payload = json.loads((Path(root) / FORGEPACT_CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("game_exe") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)


_RUNNING_EXE_CACHE: dict = {"at": 0.0, "value": None, "valid": False}


def running_game_exe() -> Path | None:
    """Cached for 3 s: the PowerShell process lookup takes ~0.3 s and several requests
    per page ask for it."""
    import time as _time
    now = _time.monotonic()
    if _RUNNING_EXE_CACHE["valid"] and now - _RUNNING_EXE_CACHE["at"] < 3.0:
        return _RUNNING_EXE_CACHE["value"]
    value = _running_game_exe_uncached()
    _RUNNING_EXE_CACHE.update(at=now, value=value, valid=True)
    return value


def _running_game_exe_uncached() -> Path | None:
    """Return the full path of a running Hero_Siege.exe, or None."""

    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='Hero_Siege.exe'\" "
        "| Select-Object -First 1 -ExpandProperty ExecutablePath"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=8, creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    candidate = line[0].strip()
    return Path(candidate) if candidate else None


def plugin_supports_forge(dll: Path) -> bool:
    """A plugin build can only apply the sidecar if it knows the header string."""

    try:
        size = dll.stat().st_size
        if size <= 0 or size > MAX_PLUGIN_SCAN_BYTES:
            return False
        return RUNTIME_MARKER in dll.read_bytes()
    except OSError:
        return False


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def sidecar_entry_count(sidecar: Path) -> int | None:
    try:
        text = sidecar.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return sum(1 for line in text.splitlines() if line.strip().startswith("item|"))


def inspect_game_dir(bin_dir: Path, source: str) -> dict[str, Any]:
    """Collect the plugin facts for one ``...\\HeroSiege\\bin`` folder."""

    bin_dir = Path(bin_dir)
    plugin = bin_dir / PLUGIN_RELATIVE
    status_path = bin_dir / STATUS_RELATIVE
    plugin_found = plugin.is_file()
    return {
        "source": source,
        "gameDir": str(bin_dir),
        "exists": bin_dir.is_dir(),
        "aurieCore": (bin_dir / AURIE_CORE_NAME).is_file(),
        "pluginPath": str(plugin),
        "pluginFound": plugin_found,
        "pluginSupportsForge": plugin_supports_forge(plugin) if plugin_found else False,
        "pluginMtime": _mtime(plugin),
        "status": read_status(status_path),
        "statusMtime": _mtime(status_path),
    }


def _candidates(root: Path, running_exe: ExeLookup, steam_default: Path) -> list[dict[str, Any]]:
    seen: set[str] = set()
    ordered: list[tuple[str, Path | None]] = []
    try:
        ordered.append(("running", running_exe()))
    except Exception:
        ordered.append(("running", None))
    ordered.append(("forgepact", forgepact_game_exe(root)))
    ordered.append(("steam", steam_default))
    result = []
    for source, exe in ordered:
        if exe is None:
            continue
        bin_dir = Path(exe).parent
        key = os.path.normcase(str(bin_dir))
        if key in seen:
            continue
        seen.add(key)
        result.append(inspect_game_dir(bin_dir, source))
    return result


def _verdict(target: dict[str, Any], sidecar_mtime: float | None, sidecar_entries: int | None) -> dict[str, Any]:
    game_dir = target["gameDir"]
    if not target["pluginFound"]:
        if not target["aurieCore"]:
            return {
                "code": "forgepact_missing", "level": "danger",
                "message": (
                    "ForgePact is not installed in this Hero Siege folder. Forged stats "
                    "will not appear in game until ForgePact's plugin is installed there."
                ),
            }
        return {
            "code": "plugin_missing", "level": "danger",
            "message": (
                "Aurie is installed but the ForgePact plugin (BloodPactPlugin.dll) is "
                "missing. Use ForgePact's Install Mod Plugin, then start the game."
            ),
        }
    if not target["pluginSupportsForge"]:
        return {
            "code": "plugin_outdated", "level": "danger",
            "message": (
                "The installed ForgePact plugin predates Custom Forge and will ignore "
                "forged items. Install the current ForgePact plugin, then restart the game."
            ),
        }
    status = target.get("status")
    if status is None:
        return {
            "code": "awaiting_game_start", "level": "warn",
            "message": (
                "A Custom-Forge-capable ForgePact plugin is installed, but the game has "
                "not reported yet. Start Hero Siege (through ForgePact) to apply forged stats."
            ),
        }
    detail = str(status.get("detail") or "")
    status_mtime = target.get("statusMtime")
    # The plugin reads the forged-item file once, at game start.  A status older
    # than the current file is therefore only a stale report, never proof that
    # the game looked somewhere else.
    stale = (
        sidecar_mtime is not None and status_mtime is not None
        and sidecar_mtime > status_mtime + 1.0
    )
    if detail == "no runtime file":
        if sidecar_mtime is None:
            return {
                "code": "no_forged_items", "level": "warn",
                "message": (
                    "No forged items exist yet. Forge one, then start Hero Siege to apply it."
                ),
            }
        if stale:
            return {
                "code": "restart_required", "level": "warn",
                "message": (
                    "The game was last started before this forge existed. Restart Hero Siege "
                    "to apply the current forge."
                ),
            }
        return {
            "code": "sidecar_not_found", "level": "danger",
            "message": (
                "The plugin started after this forge and still found no forged-item file. "
                "The game runs under a different Windows user or LOCALAPPDATA than this editor."
            ),
        }
    if detail == "unsupported runtime schema":
        return {
            "code": "schema_mismatch", "level": "danger",
            "message": (
                "The plugin rejected the forged-item file format. Update ForgePact and the "
                "editor to matching versions."
            ),
        }
    if status.get("hooksActive") is False:
        return {
            "code": "hooks_failed", "level": "danger",
            "message": "The plugin could not install its item hooks. Check ForgePact's log.",
        }
    if stale:
        return {
            "code": "restart_required", "level": "warn",
            "message": (
                "Forged items changed after the game last read them. Restart Hero Siege "
                "to apply the current forge."
            ),
        }
    reported = status.get("entries")
    if (
        isinstance(reported, int) and isinstance(sidecar_entries, int)
        and reported != sidecar_entries
    ):
        return {
            "code": "restart_required", "level": "warn",
            "message": (
                f"The game last loaded {reported} forged item(s); the file now holds "
                f"{sidecar_entries}. Restart Hero Siege to apply the current forge."
            ),
        }
    if detail == "runtime stats applied":
        return {
            "code": "applied", "level": "ok",
            "message": "ForgePact applied the forged stats in the last game session.",
        }
    if detail == "runtime hooks installed":
        return {
            "code": "hooks_installed", "level": "ok",
            "message": (
                "ForgePact is ready; forged stats are applied the moment such an item is loaded."
            ),
        }
    return {
        "code": "unknown_detail", "level": "warn",
        "message": f"ForgePact reported: {detail or 'no detail'}.",
    }


def runtime_status(
    root: Path,
    *,
    running_exe: ExeLookup = running_game_exe,
    steam_default: Path = STEAM_DEFAULT_EXE,
) -> dict[str, Any]:
    """Return the banner payload for the Custom Forge dialogs.

    ``root`` is the editor's ``%LOCALAPPDATA%\\Hero_Siege`` folder, which holds
    both the sidecar and ForgePact's own panel configuration.
    """

    root = Path(root)
    sidecar = root / SIDECAR_NAME
    sidecar_mtime = _mtime(sidecar)
    sidecar_entries = sidecar_entry_count(sidecar)
    candidates = _candidates(root, running_exe, steam_default)
    existing = [c for c in candidates if c["exists"]]
    base: dict[str, Any] = {
        "sidecarPath": str(sidecar),
        "sidecarExists": sidecar_mtime is not None,
        "sidecarEntries": sidecar_entries,
        "candidates": candidates,
    }
    if not existing:
        return {
            **base,
            "code": "game_not_found", "level": "danger",
            "gameDir": None, "source": None,
            "message": (
                "No Hero Siege installation was found. Set the game path in ForgePact "
                "or start Hero Siege once, then reopen this dialog."
            ),
        }
    target = existing[0]
    verdict = _verdict(target, sidecar_mtime, sidecar_entries)
    return {
        **base,
        **verdict,
        "gameDir": target["gameDir"],
        "source": target["source"],
        "pluginFound": target["pluginFound"],
        "pluginSupportsForge": target["pluginSupportsForge"],
        "status": target.get("status"),
    }
