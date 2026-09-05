"""Validated persistence for ForgePact-backed custom item stats.

Hero Siege saves only the compact item definition.  ForgePact consumes the
numeric runtime file written here and reapplies the selected itemStatStruct
keys after the game constructs a matching item.  The JSON document is the
human-readable source of truth; the runtime file is deliberately tiny and has
no user-controlled strings.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

try:
    from stat_semantics import StatSemanticsDatabase
except ModuleNotFoundError:
    from HSItemEditor.stat_semantics import StatSemanticsDatabase


SCHEMA_VERSION = 1
ALL_SKILLS_KEY = 201
ALL_SKILLS_CLASS_KEY = 21
RUNTIME_HEADER = "HS_CUSTOM_ITEM_FORGE_V1"
MAX_ENTRIES = 2048
MAX_STATS_PER_ENTRY = 512
MAX_ABS_VALUE = 1.0e12
MAX_BACKUP_FILES = 80
# Only generation identity belongs here.  g/w/m are placement/equip-state
# fields and change during ordinary Shared Stash/character moves; matching them
# would make a forged item appear to lose its properties after being equipped.
# `i` and `s` are deliberately NOT part of the identity: the game's runtime
# itemDefinitionStruct carries them only for some items (measured 2026-09-02:
# a Traveler's Leather Jacket forged with i=200500036 never matched because the
# loaded item had no `i`), and ForgePact requires every selector field to be
# present and equal. a/b/c/j are always there.
SELECTOR_FIELDS = ("a", "b", "c", "j")
RUNTIME_SELECTOR_FIELDS = ("t",) + SELECTOR_FIELDS


class CustomForgeError(ValueError):
    """Raised before malformed forge data can reach the runtime sidecar."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finite_number(value: Any, *, field: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CustomForgeError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_ABS_VALUE:
        raise CustomForgeError(f"{field} is outside the safe numeric range")
    if number.is_integer():
        return int(number)
    return number


MAX_LORE_CHARS = 1000
# Stat 20 is the socket count; the save format has six socket payloads at most.
SOCKET_STAT_KEY = 20
MAX_SOCKETS = 6
# itemInfoStruct["27"] values the game draws (measured): 1 common, 3 rare,
# 5 legendary, 6 satanic, 7 angelic, 9 heroic, 10 unholy.
RARITY_IDS = {1, 2, 3, 5, 6, 7, 9, 10}
# Plugin-side behaviours an item can carry.  The runtime tags the created item
# struct (fp_mechanic) and arms the matching hook only when such an item exists.
MECHANICS = {"headhunter", "tyrant", "beacon"}
# Custom display name: the plugin writes it over itemInfoStruct["28"] and
# blanks the magic prefix/suffix, so the tooltip shows exactly this text.
MAX_NAME_CHARS = 48
# Special affix rows: gold text the plugin draws above the item's stat rows in the
# inventory tooltip.  Up to three rows, separated by newlines.
MAX_AFFIX_CHARS = 240
MAX_AFFIX_ROWS = 3


def _validate_name(name: Any) -> str | None:
    if name is None:
        return None
    if not isinstance(name, str):
        raise CustomForgeError("name must be text")
    clean = " ".join(name.split())
    if not clean:
        return None
    if len(clean) > MAX_NAME_CHARS:
        raise CustomForgeError(f"name is longer than {MAX_NAME_CHARS} characters")
    if any(ord(ch) < 32 for ch in clean) or "|" in clean or ";" in clean:
        raise CustomForgeError("name contains characters the game cannot show")
    return clean


def _validate_affix(affix: Any) -> str | None:
    if affix is None:
        return None
    if not isinstance(affix, str):
        raise CustomForgeError("affix text must be text")
    rows = [" ".join(row.split()) for row in affix.replace("\r", "").split("\n")]
    rows = [row for row in rows if row]
    if not rows:
        return None
    if len(rows) > MAX_AFFIX_ROWS:
        raise CustomForgeError(f"affix text has more than {MAX_AFFIX_ROWS} rows")
    clean = "\n".join(rows)
    if len(clean) > MAX_AFFIX_CHARS:
        raise CustomForgeError(f"affix text is longer than {MAX_AFFIX_CHARS} characters")
    if any(ord(ch) < 32 and ch != "\n" for ch in clean) or "|" in clean or ";" in clean:
        raise CustomForgeError("affix text contains characters the game cannot show")
    return clean


def _validate_mechanic(mechanic: Any) -> str | None:
    if mechanic is None or mechanic == "":
        return None
    if not isinstance(mechanic, str):
        raise CustomForgeError("mechanic must be text")
    clean = mechanic.strip().lower()
    if not clean:
        return None
    if clean not in MECHANICS:
        raise CustomForgeError(f"unknown mechanic {mechanic!r}; supported: {', '.join(sorted(MECHANICS))}")
    return clean


def _validate_extras(lore: Any, rarity: Any) -> tuple[str | None, int | None]:
    clean_lore: str | None = None
    if lore is not None:
        if not isinstance(lore, str):
            raise CustomForgeError("description must be text")
        text = lore.strip()
        if len(text) > MAX_LORE_CHARS:
            raise CustomForgeError(f"description is longer than {MAX_LORE_CHARS} characters")
        if any(ord(ch) < 32 and ch not in "\n" for ch in text):
            raise CustomForgeError("description contains control characters")
        clean_lore = text or None
    clean_rarity: int | None = None
    if rarity is not None and rarity != "":
        if isinstance(rarity, bool) or not isinstance(rarity, (int, float)) or float(rarity) != int(rarity):
            raise CustomForgeError("rarity must be one of the game's rarity ids")
        if int(rarity) not in RARITY_IDS:
            raise CustomForgeError("rarity must be one of the game's rarity ids")
        clean_rarity = int(rarity)
    return clean_lore, clean_rarity


def _format_number(value: int | float) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return format(number, ".15g")


def item_selector(item_type: Any, data: Mapping[str, Any]) -> dict[str, int | float]:
    """Build the exact identity visible in the game's itemDefinitionStruct."""

    if not isinstance(data, Mapping):
        raise CustomForgeError("item data is not an object")
    selector: dict[str, int | float] = {
        "t": _finite_number(item_type, field="item type")
    }
    for field in SELECTOR_FIELDS:
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        selector[field] = _finite_number(value, field=f"item field {field}")
    if "a" not in selector or "b" not in selector:
        raise CustomForgeError("item has no stable a/b identity; it cannot be custom forged")
    return selector


def selector_id(selector: Mapping[str, Any]) -> str:
    canonical = json.dumps(selector, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_custom_forge_catalog(base: Path) -> dict[str, Any]:
    path = Path(base) / "hs_custom_forge_catalog.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CustomForgeError(f"Custom Forge catalog could not be loaded: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise CustomForgeError("Custom Forge catalog schema is unsupported")
    stats = payload.get("stats")
    donors = payload.get("donors")
    if not isinstance(stats, list) or not isinstance(donors, list):
        raise CustomForgeError("Custom Forge catalog is incomplete")
    return payload


class CustomForgeStore:
    """Atomic JSON + runtime-sidecar writer with bounded validation."""

    def __init__(
        self,
        root: Path,
        catalog: Mapping[str, Any],
        semantics: StatSemanticsDatabase,
    ):
        self.root = Path(root)
        self.json_path = self.root / "hs_custom_item_forge.json"
        self.runtime_path = self.root / "hs_custom_item_forge.runtime"
        self.backup_dir = self.root / "custom_forge_backups"
        self.catalog = catalog
        self.semantics = semantics
        # Every observed key plus every key the game code itself was proven
        # to read (the semantics artifact keeps the evidence). Keys that only
        # exist as unnamed constants stay out.
        self.allowed_stats = {
            int(row["key"])
            for row in catalog.get("stats", [])
            if isinstance(row, dict) and isinstance(row.get("key"), int)
        }
        self.allowed_stats.update(
            int(key)
            for key in range(0, 10000)
            if (meta := semantics.get(key)) is not None
            and meta["name"] != "unknown"
            and meta["evidence"].get("confidence") in ("code", "code-heuristic")
        )
        self.presets = {
            str(prop["id"]): prop
            for donor in catalog.get("donors", [])
            if isinstance(donor, dict)
            for prop in donor.get("properties", [])
            if isinstance(prop, dict) and isinstance(prop.get("id"), str)
        }
        self.defaults = {
            int(row["key"]): row.get("recommendedValue")
            for row in catalog.get("stats", [])
            if isinstance(row, dict)
            and isinstance(row.get("key"), int)
            and isinstance(row.get("recommendedValue"), (int, float))
            and not isinstance(row.get("recommendedValue"), bool)
        }
        for key in self.allowed_stats:
            if key not in self.defaults and semantics.safe_editable(key):
                self.defaults[key] = 1

    def _empty(self) -> dict[str, Any]:
        return {"schemaVersion": SCHEMA_VERSION, "updatedAt": _utc_now(), "items": {}}

    def load(self) -> dict[str, Any]:
        if not self.json_path.exists():
            return self._empty()
        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CustomForgeError(f"Custom Forge settings are unreadable: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
            raise CustomForgeError("Custom Forge settings schema is unsupported")
        items = payload.get("items")
        if not isinstance(items, dict) or len(items) > MAX_ENTRIES:
            raise CustomForgeError("Custom Forge settings contain an invalid item table")
        # Migrate entries written with the old i/s identity fields: keep only
        # the fields the runtime always carries and re-key them. The newest
        # entry wins when two collapse onto the same identity.
        migrated: dict[str, Any] = {}
        changed = False
        for entry_id, entry in items.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("selector"), Mapping):
                migrated[entry_id] = entry
                continue
            selector = {
                field: value
                for field, value in entry["selector"].items()
                if field in RUNTIME_SELECTOR_FIELDS
            }
            new_id = selector_id(selector)
            if new_id != entry_id or selector != dict(entry["selector"]):
                changed = True
                entry = dict(entry)
                entry["selector"] = selector
            previous = migrated.get(new_id)
            if previous is None or str(entry.get("updatedAt") or "") >= str(previous.get("updatedAt") or ""):
                migrated[new_id] = entry
        if changed:
            payload["items"] = migrated
            self._commit(payload)
        return payload

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _backup_existing(self) -> str:
        existing = [path for path in (self.json_path, self.runtime_path) if path.exists()]
        if not existing:
            return ""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        names = []
        for path in existing:
            destination = self.backup_dir / f"{path.name}.{stamp}.bak"
            shutil.copy2(path, destination)
            names.append(destination.name)
        backups = sorted(
            self.backup_dir.glob("*.bak"),
            key=lambda candidate: candidate.stat().st_mtime_ns,
            reverse=True,
        )
        for expired in backups[MAX_BACKUP_FILES:]:
            expired.unlink(missing_ok=True)
        return ", ".join(names)

    def _validate_stats(self, stats: Mapping[Any, Any]) -> dict[str, int | float]:
        if not isinstance(stats, Mapping) or not stats:
            raise CustomForgeError("add at least one stat or unique property")
        if len(stats) > MAX_STATS_PER_ENTRY:
            raise CustomForgeError("too many stats were selected for one item")
        clean: dict[str, int | float] = {}
        for raw_key, raw_value in stats.items():
            try:
                key = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise CustomForgeError(f"invalid stat key: {raw_key}") from exc
            if key not in self.allowed_stats:
                raise CustomForgeError(f"stat #{key} is not observed in this game build")
            value = _finite_number(raw_value, field=f"stat #{key}")
            if key == SOCKET_STAT_KEY and (not isinstance(value, int) or not 0 <= value <= MAX_SOCKETS):
                raise CustomForgeError(f"sockets must be a whole number from 0 to {MAX_SOCKETS}")
            clean[str(key)] = value
        present = {int(key) for key in clean}
        for key in present:
            linked = set(self.semantics.linked_keys(key))
            if not linked.issubset(present):
                missing = ", ".join(f"#{member}" for member in sorted(linked - present))
                raise CustomForgeError(
                    f"{self.semantics.display_name(key)} is incomplete; missing {missing}"
                )
        return dict(sorted(clean.items(), key=lambda pair: int(pair[0])))

    def _expanded_exclusions(self, raw_keys: Any) -> set[int]:
        if raw_keys is None:
            return set()
        if not isinstance(raw_keys, list) or len(raw_keys) > MAX_STATS_PER_ENTRY:
            raise CustomForgeError("invalid linked-stat removal list")
        excluded: set[int] = set()
        for raw_key in raw_keys:
            try:
                key = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise CustomForgeError(f"invalid stat key: {raw_key}") from exc
            if key not in self.allowed_stats:
                raise CustomForgeError(f"stat #{key} is not observed in this game build")
            excluded.update(self.semantics.linked_keys(key))
        return excluded

    def _runtime_text(self, payload: Mapping[str, Any]) -> str:
        lines = [RUNTIME_HEADER]
        items = payload.get("items", {})
        for entry_id in sorted(items):
            entry = items[entry_id]
            selector = entry["selector"]
            selector_text = ";".join(
                f"{field}={_format_number(selector[field])}"
                for field in RUNTIME_SELECTOR_FIELDS
                if field in selector
            )
            stats_text = ";".join(
                f"{key}={_format_number(value)}"
                for key, value in sorted(
                    entry["stats"].items(), key=lambda pair: int(pair[0])
                )
            )
            keep = "1" if entry.get("keepNative", True) else "0"
            line = f"item|{selector_text}|keep={keep}|{stats_text}"
            extras = []
            lore = entry.get("lore")
            if isinstance(lore, str) and lore:
                # Percent-encoded so the description can never contain the
                # '|' and ';' separators of the runtime line.
                extras.append("lore=" + quote(lore, safe=""))
            rarity = entry.get("rarity")
            if isinstance(rarity, int) and not isinstance(rarity, bool):
                extras.append(f"rarity={rarity}")
            mechanic = entry.get("mechanic")
            if isinstance(mechanic, str) and mechanic in MECHANICS:
                extras.append(f"mechanic={mechanic}")
            name = entry.get("name")
            if isinstance(name, str) and name:
                extras.append("name=" + quote(name, safe=""))
            affix = entry.get("affix")
            if isinstance(affix, str) and affix:
                extras.append("affix=" + quote(affix, safe=""))
            if extras:
                line += "|" + ";".join(extras)
            lines.append(line)
        return "\n".join(lines) + "\n"

    def _commit(self, payload: dict[str, Any]) -> str:
        payload["updatedAt"] = _utc_now()
        backup_name = self._backup_existing()
        source = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        runtime = self._runtime_text(payload)
        self._atomic_write(self.json_path, source)
        self._atomic_write(self.runtime_path, runtime)
        return backup_name

    def get(self, selector: Mapping[str, Any]) -> dict[str, Any] | None:
        entry = self.load()["items"].get(selector_id(selector))
        return dict(entry) if isinstance(entry, dict) else None

    def apply(
        self,
        selector: Mapping[str, Any],
        *,
        label: str,
        stats: Mapping[Any, Any],
        keep_native: bool,
        preset_ids: list[Any] | None = None,
        excluded_keys: list[Any] | None = None,
        lore: Any = None,
        rarity: Any = None,
        mechanic: Any = None,
        name: Any = None,
        affix: Any = None,
    ) -> dict[str, Any]:
        clean_stats = self._validate_stats(stats)
        clean_name = _validate_name(name)
        clean_affix = _validate_affix(affix)
        clean_lore, clean_rarity = _validate_extras(lore, rarity)
        clean_mechanic = _validate_mechanic(mechanic)
        payload = self.load()
        items = payload["items"]
        entry_id = selector_id(selector)
        if entry_id not in items and len(items) >= MAX_ENTRIES:
            raise CustomForgeError("Custom Forge item limit reached")
        now = _utc_now()
        prior = items.get(entry_id, {})
        items[entry_id] = {
            "label": str(label)[:256],
            "selector": dict(selector),
            "stats": clean_stats,
            "keepNative": bool(keep_native),
            "presetIds": [str(value) for value in (preset_ids or [])],
            "excludedKeys": sorted(self._expanded_exclusions(excluded_keys)),
            "lore": clean_lore,
            "rarity": clean_rarity,
            "mechanic": clean_mechanic,
            "name": clean_name,
            "affix": clean_affix,
            "createdAt": prior.get("createdAt", now),
            "updatedAt": now,
        }
        backup_name = self._commit(payload)
        return {"entry": items[entry_id], "backup": backup_name}

    def remove(self, selector: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.load()
        removed = payload["items"].pop(selector_id(selector), None)
        if removed is None:
            return {"removed": False, "backup": ""}
        return {"removed": True, "backup": self._commit(payload)}

    def retarget(
        self, old_selector: Mapping[str, Any], new_selector: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Follow an editor-controlled seed change without losing overrides."""

        old_id = selector_id(old_selector)
        new_id = selector_id(new_selector)
        if old_id == new_id:
            return {"moved": False, "backup": ""}
        payload = self.load()
        entry = payload["items"].get(old_id)
        if not isinstance(entry, dict):
            return {"moved": False, "backup": ""}
        if new_id in payload["items"]:
            raise CustomForgeError(
                "the new item seed already has a different Custom Forge configuration"
            )
        payload["items"].pop(old_id)
        entry["selector"] = dict(new_selector)
        entry["updatedAt"] = _utc_now()
        payload["items"][new_id] = entry
        return {"moved": True, "backup": self._commit(payload)}

    def merge_presets(
        self,
        preset_ids: list[Any],
        explicit_stats: Mapping[Any, Any],
        *,
        excluded_keys: list[Any] | None = None,
        trusted_stats: Mapping[Any, Any] | None = None,
        keep_native: bool = True,
    ) -> dict[str, int | float]:
        merged: dict[int, int | float] = {}
        authorized_unsafe: dict[int, int | float] = {}
        trusted: dict[int, int | float] = {}
        if not isinstance(preset_ids, list) or len(preset_ids) > MAX_STATS_PER_ENTRY:
            raise CustomForgeError("invalid unique property selection")
        for raw_id in preset_ids:
            preset = self.presets.get(str(raw_id))
            if preset is None:
                raise CustomForgeError(f"unknown unique property preset: {raw_id}")
            preset_stats = preset.get("stats", {})
            if not isinstance(preset_stats, Mapping):
                raise CustomForgeError(f"invalid unique property preset: {raw_id}")
            for raw_key, raw_value in preset_stats.items():
                key = int(raw_key)
                value = _finite_number(raw_value, field=f"stat #{key}")
                merged[key] = value
                if not self.semantics.safe_editable(key):
                    authorized_unsafe[key] = value
        if trusted_stats is not None:
            if not isinstance(trusted_stats, Mapping):
                raise CustomForgeError("existing Custom Forge stats are invalid")
            for raw_key, raw_value in trusted_stats.items():
                key = int(raw_key)
                if key in self.allowed_stats:
                    trusted[key] = _finite_number(raw_value, field=f"stat #{key}")
        if not isinstance(explicit_stats, Mapping):
            raise CustomForgeError("stats must be an object")
        for raw_key, raw_value in explicit_stats.items():
            try:
                key = int(raw_key)
            except (TypeError, ValueError) as exc:
                raise CustomForgeError(f"invalid stat key: {raw_key}") from exc
            if key not in self.allowed_stats:
                raise CustomForgeError(f"stat #{key} is not observed in this game build")
            value = _finite_number(raw_value, field=f"stat #{key}")
            if not self.semantics.safe_editable(key):
                preset_value = authorized_unsafe.get(key)
                trusted_value = trusted.get(key)
                # Skill and class identities may be chosen from the verified
                # talent / class lists; any other number is refused.
                if (
                    value != preset_value
                    and value != trusted_value
                    and not self.semantics.valid_identity(key, value)
                ):
                    raise CustomForgeError(
                        f"{self.semantics.display_name(key)} (#{key}) is read-only; "
                        "choose a skill or class from the list, or a verified donor"
                    )
                if value != preset_value and value != trusted_value:
                    authorized_unsafe[key] = value
            merged[key] = value

        excluded = self._expanded_exclusions(excluded_keys)
        for key in excluded:
            merged.pop(key, None)
        # 'All Skills: Class' (#21) is only the class tag of 'to All Skills' (#201);
        # written alone the game draws nothing.  A keep-native item may still own
        # #201 natively, so only a configuration that replaces every stat is checked.
        if ALL_SKILLS_CLASS_KEY in merged and ALL_SKILLS_KEY not in merged and not keep_native:
            raise CustomForgeError(
                "All Skills: Class only works together with 'to All Skills' (#201); "
                "add 'to All Skills' or remove the class"
            )

        # Complete every directed linked family. Safe numeric companions use
        # catalog defaults; identity fields can only come from a verified
        # preset or an unchanged, previously trusted configuration.
        changed = True
        while changed:
            changed = False
            for key in tuple(merged):
                for member in self.semantics.linked_keys(key):
                    if member in excluded or member in merged:
                        continue
                    if member in authorized_unsafe:
                        merged[member] = authorized_unsafe[member]
                    elif member in trusted:
                        merged[member] = trusted[member]
                    elif self.semantics.safe_editable(member) and member in self.defaults:
                        merged[member] = _finite_number(
                            self.defaults[member], field=f"stat #{member}"
                        )
                    else:
                        raise CustomForgeError(
                            f"{self.semantics.display_name(key)} needs its linked stat "
                            f"#{member}: pick the skill or class from the list, or "
                            "choose a verified unique-property donor"
                        )
                    changed = True
        return self._validate_stats(merged)


__all__ = [
    "CustomForgeError",
    "CustomForgeStore",
    "item_selector",
    "load_custom_forge_catalog",
    "selector_id",
]
