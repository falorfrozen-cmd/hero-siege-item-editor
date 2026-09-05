"""Fail-closed consumer for the Season 10 item-stat semantics artifact."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
DEFAULT_FILENAME = "hs_stat_semantics_s10.json"
CONFIDENCE_LEVELS = frozenset({"code", "code-heuristic", "none"})
VALUE_KINDS = frozenset({
    "flat",
    "percent",
    "boolean",
    "skill_id",
    "skill_level",
    "chance_percent",
    "class_id",
    "element_id",
    "duration",
    "internal_enum",
    "unknown",
})
GROUP_LABELS = {
    "when_attacking": "Chance when Attacking",
    "when_strike": "Chance when Striking",
    "when_spellhit": "Chance when Spellhit",
    "when_kill": "Chance after Each Kill",
    "when_casting": "Chance when Casting",
    "when_struck": "Chance when Struck",
    "when_blocking": "Chance after Blocking",
    "skill_grant": "Skill Grant",
    "sub_skill_grant": "Sub-Skill Grant",
    "damage_taken_over": "Incoming Damage Taken Over Time",
    "flask": "Flask Charges",
    "base_damage": "Base Weapon Damage",
    "projectiles": "Projectile Property",
}


TALENT_TABLE_FILENAME = "hs_talent_table_s10.json"
# Identity kinds that are chosen from a list instead of typed as a number.
PICKER_KINDS = {"skill_id": "talent", "class_id": "class"}
CLASS_NAMES = {
    1: "Viking", 2: "Pyromancer", 3: "Marksman", 4: "Pirate", 5: "Nomad",
    6: "Redneck", 7: "Necromancer", 8: "Samurai", 9: "Paladin", 10: "Amazon",
    11: "Demon Slayer", 12: "Demonspawn", 13: "Shaman", 14: "White Mage",
    15: "Marauder", 16: "Plague Doctor", 17: "Shield Lancer", 18: "Jötunn",
    19: "Illusionist", 20: "Exo", 21: "Butcher", 22: "Stormweaver", 23: "Bard",
    24: "Prophet",
}


class StatSemanticsError(ValueError):
    """Raised when the immutable semantics artifact is missing or inconsistent."""


def _validate_talent_table(document: Any, expected_exe_sha256: str) -> dict[int, dict[str, Any]]:
    """Validate the runtime talentStructMap dump: id -> slug/name/class."""
    if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
        _fail("talent table schemaVersion is unsupported")
    if str(document.get("exeSha256") or "").upper() != str(expected_exe_sha256).upper():
        _fail("talent table executable SHA-256 does not match the editor build")
    raw = document.get("talents")
    if not isinstance(raw, dict) or not raw:
        _fail("talent table contains no talents")
    talents: dict[int, dict[str, Any]] = {}
    for raw_id, meta in raw.items():
        talent_id = _canonical_key(raw_id, field="talent id")
        if not isinstance(meta, dict) or not isinstance(meta.get("slug"), str) or not meta["slug"]:
            _fail(f"talent #{talent_id} has no slug")
        class_id = meta.get("classId")
        if class_id is not None and (isinstance(class_id, bool) or class_id not in CLASS_NAMES):
            _fail(f"talent #{talent_id} has an invalid classId")
        talents[talent_id] = {
            "id": talent_id,
            "slug": meta["slug"],
            "name": str(meta.get("name") or ""),
            "classId": class_id,
            "className": CLASS_NAMES.get(class_id) if class_id is not None else None,
        }
    return talents


def _fail(message: str) -> None:
    raise StatSemanticsError(message)


def _canonical_key(raw: Any, *, field: str) -> int:
    if isinstance(raw, bool):
        _fail(f"{field} must be an integer stat id")
    try:
        key = int(raw)
    except (TypeError, ValueError):
        _fail(f"{field} must be an integer stat id")
    if str(key) != str(raw) or not 0 <= key <= 9999:
        _fail(f"{field} is not a canonical stat id")
    return key


def _validate_document(document: Any, expected_exe_sha256: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        _fail("stat semantics top level must be an object")
    if document.get("schemaVersion") != SCHEMA_VERSION:
        _fail(f"stat semantics schemaVersion must be {SCHEMA_VERSION}")
    expected = str(expected_exe_sha256).upper()
    actual = str(document.get("exeSha256") or "").upper()
    if not re.fullmatch(r"[0-9A-F]{64}", expected) or actual != expected:
        _fail("stat semantics executable SHA-256 does not match the editor build")
    raw_stats = document.get("stats")
    if not isinstance(raw_stats, dict) or not raw_stats:
        _fail("stat semantics contains no stats")

    stats: dict[str, dict[str, Any]] = {}
    for raw_key, raw_meta in raw_stats.items():
        key = _canonical_key(raw_key, field="stat key")
        if not isinstance(raw_meta, dict):
            _fail(f"stat #{key} metadata must be an object")
        meta = copy.deepcopy(raw_meta)
        for field in ("name", "plainDescription", "valueKind", "unit", "group", "role"):
            if not isinstance(meta.get(field), str):
                _fail(f"stat #{key} {field} must be text")
        if meta["valueKind"] not in VALUE_KINDS:
            _fail(f"stat #{key} has unsupported valueKind {meta['valueKind']!r}")
        if not isinstance(meta.get("safeEditable"), bool):
            _fail(f"stat #{key} safeEditable must be boolean")
        if not isinstance(meta.get("observed"), bool):
            _fail(f"stat #{key} observed must be boolean")
        if meta.get("higherIsBetter") not in (True, False, None):
            _fail(f"stat #{key} higherIsBetter must be true, false or null")
        evidence = meta.get("evidence")
        if not isinstance(evidence, dict) or evidence.get("confidence") not in CONFIDENCE_LEVELS:
            _fail(f"stat #{key} evidence is incomplete")
        linked = meta.get("linkedKeys")
        if not isinstance(linked, list) or not linked:
            _fail(f"stat #{key} linkedKeys must be a non-empty list")
        linked_keys = tuple(_canonical_key(value, field=f"stat #{key} linked key") for value in linked)
        if len(linked_keys) != len(set(linked_keys)) or key not in linked_keys:
            _fail(f"stat #{key} linkedKeys must be unique and include itself")
        meta["linkedKeys"] = list(linked_keys)
        stats[str(key)] = meta

    # Validate the semantics artifact against itself. Never compare these
    # families to the older roll/socket catalog: that stale guard previously
    # rejected valid measured data.
    for raw_key, meta in stats.items():
        family = frozenset(meta["linkedKeys"])
        for member in family:
            member_meta = stats.get(str(member))
            if member_meta is None:
                _fail(f"stat #{raw_key} links to missing stat #{member}")
            # A family may deliberately contain independently editable child
            # fields (base damage #22 includes #447-451, whose own families
            # are self-only). Closure therefore means following any member's
            # links cannot escape the declaring family, not that every member
            # must repeat the exact same list.
            if not frozenset(member_meta["linkedKeys"]).issubset(family):
                _fail(f"stat #{raw_key} linked family is not closed at stat #{member}")

    validated = copy.deepcopy(document)
    validated["exeSha256"] = expected
    validated["stats"] = stats
    return validated


class StatSemanticsDatabase:
    """Immutable validated stat metadata plus catalog decoration helpers."""

    def __init__(
        self,
        path: Path,
        document: Mapping[str, Any],
        talents: Mapping[int, Mapping[str, Any]] | None = None,
    ) -> None:
        self.path = Path(path)
        self._document = copy.deepcopy(dict(document))
        self._stats: dict[int, dict[str, Any]] = {
            int(key): copy.deepcopy(value)
            for key, value in self._document["stats"].items()
        }
        self._talents: dict[int, dict[str, Any]] = {
            int(key): dict(value) for key, value in (talents or {}).items()
        }

    # ---- identity pickers -------------------------------------------------
    def picker_kind(self, key: Any) -> str | None:
        """'talent' / 'class' for keys chosen from a list, else None."""
        meta = self.get(key)
        if meta is None:
            return None
        return PICKER_KINDS.get(str(meta.get("valueKind")))

    def valid_identity(self, key: Any, value: Any) -> bool:
        """True when ``value`` is a legitimate id for a picker-kind key."""
        kind = self.picker_kind(key)
        if kind is None or isinstance(value, bool):
            return False
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if not number.is_integer():
            return False
        ident = int(number)
        if kind == "class":
            return ident in CLASS_NAMES
        return ident in self._talents

    def talent(self, talent_id: Any) -> dict[str, Any] | None:
        try:
            meta = self._talents.get(int(talent_id))
        except (TypeError, ValueError):
            return None
        return dict(meta) if meta else None

    def pickers(self) -> dict[str, list[dict[str, Any]]]:
        """Option lists for the UI: every known talent and every class."""
        talents = []
        for talent_id in sorted(self._talents):
            meta = self._talents[talent_id]
            label = meta["name"] or meta["slug"]
            talents.append({
                "id": talent_id,
                "label": label,
                "slug": meta["slug"],
                "classId": meta["classId"],
                "className": meta["className"],
            })
        classes = [{"id": cid, "label": name} for cid, name in CLASS_NAMES.items()]
        return {"talents": talents, "classes": classes}

    @property
    def exe_sha256(self) -> str:
        return str(self._document["exeSha256"])

    @property
    def key_count(self) -> int:
        return len(self._stats)

    def get(self, key: Any) -> dict[str, Any] | None:
        try:
            stat_key = int(key)
        except (TypeError, ValueError):
            return None
        meta = self._stats.get(stat_key)
        return copy.deepcopy(meta) if meta is not None else None

    def linked_keys(self, key: Any) -> tuple[int, ...]:
        meta = self.get(key)
        return tuple(meta["linkedKeys"]) if meta is not None else ()

    def safe_editable(self, key: Any) -> bool:
        meta = self.get(key)
        return bool(meta and meta["safeEditable"])

    def display_name(self, key: Any) -> str:
        meta = self.get(key)
        if meta is None or meta["name"] == "unknown":
            return f"Stat #{int(key)}"
        return str(meta["name"])

    def _decorate_stat(self, row: Mapping[str, Any]) -> dict[str, Any]:
        decorated = copy.deepcopy(dict(row))
        key = int(decorated["key"])
        meta = self._stats.get(key)
        if meta is None:
            _fail(f"observed catalog stat #{key} is absent from semantics JSON")
        unknown = meta["name"] == "unknown"
        decorated.update({
            "label": f"Stat #{key}" if unknown else meta["name"],
            "pickerKind": PICKER_KINDS.get(str(meta.get("valueKind"))),
            "plainDescription": meta["plainDescription"],
            "valueKind": meta["valueKind"],
            "unit": meta["unit"],
            "higherIsBetter": meta["higherIsBetter"],
            "group": meta["group"],
            "role": meta["role"],
            "linkedKeys": list(meta["linkedKeys"]),
            "safeEditable": meta["safeEditable"],
            "confidence": meta["evidence"]["confidence"],
            "evidenceNote": str(meta["evidence"].get("note") or ""),
            "observed": meta["observed"],
            "advanced": unknown,
            "percent": meta["valueKind"] in {"percent", "chance_percent"},
        })
        return decorated

    def _code_only_rows(self, stat_map: Mapping[int, Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Catalog rows for keys the game code reads but no drop has shown.

        Without a row the UI cannot offer the key at all (the Spellhit proc
        family lived only in the API). The click default is the median
        catalog default of the observed keys with the same valueKind, so a
        proc level starts where the other proc levels start; flags start at 1.
        """
        by_kind: dict[str, list[float]] = {}
        for row in stat_map.values():
            value = row.get("recommendedValue")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                by_kind.setdefault(str(row.get("valueKind")), []).append(float(value))
        rows: list[dict[str, Any]] = []
        for key in sorted(self._stats):
            meta = self._stats[key]
            if key in stat_map or meta["name"] == "unknown":
                continue
            kind = str(meta.get("valueKind"))
            picker = PICKER_KINDS.get(kind)
            if not meta["safeEditable"] and picker is None:
                continue
            default: int | float | None
            if picker is not None:
                default = None
            elif kind == "boolean":
                default = 1
            else:
                samples = sorted(by_kind.get(kind, []))
                default = samples[len(samples) // 2] if samples else 1
                if float(default).is_integer():
                    default = int(default)
            row = self._decorate_stat({
                "key": key,
                "label": meta["name"],
                "percent": kind in {"percent", "chance_percent"},
                "minimumObserved": None,
                "maximumObserved": None,
                "recommendedValue": default,
                "examples": [],
                "advanced": False,
            })
            row["codeOnly"] = True
            rows.append(row)
        return rows

    def decorate_catalog(self, catalog: Mapping[str, Any]) -> dict[str, Any]:
        decorated = copy.deepcopy(dict(catalog))
        raw_stats = decorated.get("stats")
        raw_donors = decorated.get("donors")
        if not isinstance(raw_stats, list) or not isinstance(raw_donors, list):
            _fail("Custom Forge catalog is incomplete")
        stats = [self._decorate_stat(row) for row in raw_stats]
        stat_map = {int(row["key"]): row for row in stats}
        stats.extend(self._code_only_rows(stat_map))
        stat_map = {int(row["key"]): row for row in stats}

        donors: list[dict[str, Any]] = []
        for raw_donor in raw_donors:
            donor = copy.deepcopy(dict(raw_donor))
            properties = donor.get("properties", [])
            donor_values: dict[int, int | float] = {}
            for prop in properties:
                if not isinstance(prop, dict) or not isinstance(prop.get("stats"), dict):
                    continue
                for raw_key, value in prop["stats"].items():
                    donor_values[int(raw_key)] = value

            output_properties: list[dict[str, Any]] = []
            seen_families: set[tuple[int, ...]] = set()
            for raw_property in properties:
                if not isinstance(raw_property, dict) or not isinstance(raw_property.get("stats"), dict):
                    continue
                prop = copy.deepcopy(raw_property)
                is_complete = prop.get("kind") == "complete_donor"
                keys = {int(key) for key in prop["stats"]}
                if not is_complete:
                    closure = set(keys)
                    for key in tuple(keys):
                        meta = self._stats.get(key)
                        if meta is not None:
                            closure.update(meta["linkedKeys"])
                    missing = closure - set(donor_values)
                    # A family may still be offered when the missing members
                    # are class ids (chosen from a list in the UI) or plain
                    # numeric companions (filled from the catalog defaults).
                    # A missing skill id would write half a proc or half a
                    # skill grant, so such a preset is never exposed.
                    if any(
                        str(self._stats.get(key, {}).get("role")) != "class_id"
                        and not bool(self._stats.get(key, {}).get("safeEditable"))
                        for key in missing
                    ):
                        continue
                    family_id = tuple(sorted(closure))
                    if family_id in seen_families:
                        continue
                    seen_families.add(family_id)
                    prop["keys"] = list(family_id)
                    prop["stats"] = {}
                    for key in family_id:
                        if key in donor_values:
                            prop["stats"][str(key)] = donor_values[key]
                        elif str(self._stats.get(key, {}).get("role")) != "class_id":
                            # plain numeric companion: use the catalog default
                            default = stat_map.get(key, {}).get("recommendedValue")
                            if isinstance(default, (int, float)) and not isinstance(default, bool):
                                prop["stats"][str(key)] = default
                    prop["needsClass"] = sorted(
                        key for key in missing
                        if str(self._stats.get(key, {}).get("role")) == "class_id"
                    )
                    first_meta = self._stats.get(family_id[0], {})
                    if len(family_id) > 1:
                        prop["label"] = GROUP_LABELS.get(
                            str(first_meta.get("group") or ""),
                            str(first_meta.get("name") or prop.get("label") or "Linked property").split(":", 1)[0],
                        )
                    elif first_meta.get("name") != "unknown":
                        prop["label"] = first_meta.get("name")
                    prop["plainDescription"] = str(first_meta.get("plainDescription") or "")
                    prop["linked"] = len(family_id) > 1
                    prop["safeEditable"] = all(
                        bool(self._stats.get(key, {}).get("safeEditable"))
                        for key in family_id
                    )
                    prop["confidence"] = (
                        "code-heuristic"
                        if any(
                            self._stats.get(key, {}).get("evidence", {}).get("confidence")
                            == "code-heuristic"
                            for key in family_id
                        )
                        else "code"
                    )
                else:
                    # Remove any incomplete linked family from an all-stats
                    # donor preset rather than weakening atomicity.
                    complete_stats = {int(key): value for key, value in prop["stats"].items()}
                    invalid: set[int] = set()
                    for key in tuple(complete_stats):
                        meta = self._stats.get(key)
                        if meta and not set(meta["linkedKeys"]).issubset(complete_stats):
                            invalid.update(meta["linkedKeys"])
                    for key in invalid:
                        complete_stats.pop(key, None)
                    if not complete_stats:
                        continue
                    prop["keys"] = sorted(complete_stats)
                    prop["stats"] = {str(key): complete_stats[key] for key in sorted(complete_stats)}
                    prop["label"] = "Complete item property set"
                    prop["plainDescription"] = "Copies every complete, code-verified runtime property observed on this item."
                    prop["linked"] = True
                    prop["safeEditable"] = False
                    prop["confidence"] = "code"
                output_properties.append(prop)
            donor["properties"] = output_properties
            if output_properties:
                donors.append(donor)

        decorated["stats"] = stats
        decorated["donors"] = donors
        decorated["pickers"] = self.pickers()
        decorated["semantics"] = {
            "schemaVersion": SCHEMA_VERSION,
            "exeSha256": self.exe_sha256,
            "keyCount": self.key_count,
            "observedNamedCount": sum(
                row["observed"] and not row["advanced"] for row in stats
            ),
            "observedUnknownCount": sum(
                row["observed"] and row["advanced"] for row in stats
            ),
            "codeOnlyCount": sum(bool(row.get("codeOnly")) for row in stats),
        }
        coverage = dict(decorated.get("coverage") or {})
        coverage.update({
            "statKeyCount": len(stats),
            "uniqueDonorCount": len(donors),
            "propertyPresetCount": sum(len(row["properties"]) for row in donors),
        })
        decorated["coverage"] = coverage
        return decorated


def load_stat_semantics(
    base: str | Path,
    *,
    expected_exe_sha256: str,
) -> StatSemanticsDatabase:
    path = Path(base) / DEFAULT_FILENAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatSemanticsError(f"stat semantics could not be loaded: {exc}") from exc
    talent_path = Path(base) / TALENT_TABLE_FILENAME
    try:
        talent_document = json.loads(talent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatSemanticsError(f"talent table could not be loaded: {exc}") from exc
    return StatSemanticsDatabase(
        path,
        _validate_document(document, expected_exe_sha256),
        _validate_talent_table(talent_document, expected_exe_sha256),
    )


__all__ = [
    "DEFAULT_FILENAME",
    "SCHEMA_VERSION",
    "StatSemanticsDatabase",
    "StatSemanticsError",
    "load_stat_semantics",
]
