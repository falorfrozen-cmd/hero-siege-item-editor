#!/usr/bin/env python3
"""Build the Custom Item Forge catalog from verified S10 runtime models."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parent
MODEL_PATH = BASE / "hs_tooltip_roll_models.json"
CATALOG_PATH = BASE / "hs_full_catalog.json"
OUTPUT_PATH = BASE / "hs_custom_forge_catalog.json"

BUNDLE_FAMILIES = (
    ((113, 114, 115), "Chance When Attacking"),
    ((116, 117, 118), "Chance When Striking"),
    ((122, 123, 124), "Chance After Each Kill"),
    ((125, 126, 127), "Chance When Casting"),
    ((185, 186, 187), "Chance When Struck"),
    ((188, 189, 190), "Chance After Blocking"),
)


def _numbers(value: Any) -> list[float]:
    if isinstance(value, list):
        return [float(number) for number in value if isinstance(number, (int, float))]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    if not isinstance(value, str):
        return []
    return [float(token) for token in re.findall(r"-?\d+(?:\.\d+)?", value)]


SOCKET_KEY = 20


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _clean_number(value: float) -> int | float:
    return int(value) if math.isfinite(value) and value.is_integer() else value


def _default_value(stat: dict[str, Any], catalog_row: dict[str, Any] | None) -> int | float:
    values = _numbers(stat.get("values"))
    line_index = stat.get("catalogLineIndex")
    if not values and catalog_row and isinstance(line_index, int):
        lines = catalog_row.get("stats", [])
        if 0 <= line_index < len(lines) and isinstance(lines[line_index], list):
            values = _numbers(lines[line_index][1] if len(lines[line_index]) > 1 else "")
    if not values:
        values = _numbers(stat.get("catalogTemplate"))
    return _clean_number(max(values) if values else 1.0)


def _label(stat_key: int, stat: dict[str, Any], labels: dict[str, Any]) -> str:
    value = str(stat.get("label") or "").strip()
    if not value:
        value = str(labels.get(str(stat_key), {}).get("label") or "").strip()
    return value or f"Stat #{stat_key}"


def _normalized_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _property_rows(catalog_id: int, by_key: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    properties = []
    bundled_keys: set[int] = set()
    for family, family_label in BUNDLE_FAMILIES:
        if all(key in by_key for key in family):
            stats = {str(key): by_key[key]["value"] for key in family}
            properties.append({
                "id": f"u:{catalog_id}:{'-'.join(map(str, family))}",
                "label": family_label,
                "kind": "linked_unique",
                "keys": list(family),
                "stats": stats,
            })
            bundled_keys.update(family)
    for key, row in sorted(by_key.items()):
        if key in bundled_keys:
            continue
        properties.append({
            "id": f"u:{catalog_id}:{key}",
            "label": row["label"],
            "kind": "observed_property",
            "keys": [key],
            "stats": {str(key): row["value"]},
        })
    properties.append({
        "id": f"u:{catalog_id}:all",
        "label": "Complete observed property set",
        "kind": "complete_donor",
        "keys": sorted(by_key),
        "stats": {str(key): by_key[key]["value"] for key in sorted(by_key)},
    })
    return properties


def main() -> None:
    model = json.loads(MODEL_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog_by_id = {int(row["id"]): row for row in catalog}
    labels = model.get("statLabels", {})

    observations: dict[int, dict[str, Any]] = {}
    donor_rows: dict[int, dict[str, Any]] = {}
    label_to_keys: dict[str, set[int]] = {}

    def remember_label(label: Any, key: int) -> None:
        normalized = _normalized_label(label)
        if normalized and not re.fullmatch(r"stat #\d+", normalized):
            label_to_keys.setdefault(normalized, set()).add(key)

    def observe(key: int, label: str, value: int | float, name: str) -> None:
        observed = observations.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "percent": bool(labels.get(str(key), {}).get("percent", False)),
                "values": [],
                "examples": [],
            },
        )
        observed["values"].append(float(value))
        if name not in observed["examples"] and len(observed["examples"]) < 4:
            observed["examples"].append(name)

    for raw_key, meta in labels.items():
        try:
            remember_label(meta.get("label"), int(raw_key))
        except (AttributeError, TypeError, ValueError):
            continue

    for definition in model.get("definitions", {}).values():
        if not isinstance(definition, dict):
            continue
        catalog_id = definition.get("catalogId")
        catalog_row = catalog_by_id.get(int(catalog_id)) if isinstance(catalog_id, int) else None
        by_key: dict[int, dict[str, Any]] = {}
        for raw_stat in definition.get("stats", []):
            if not isinstance(raw_stat, dict) or not isinstance(raw_stat.get("statKey"), int):
                continue
            key = int(raw_stat["statKey"])
            value = _default_value(raw_stat, catalog_row)
            label = _label(key, raw_stat, labels)
            by_key[key] = {"key": key, "label": label, "value": value}
            name = str(definition.get("name") or "Unknown item")
            observe(key, label, value, name)
            remember_label(raw_stat.get("label"), key)
            line_index = raw_stat.get("catalogLineIndex")
            if catalog_row and isinstance(line_index, int):
                lines = catalog_row.get("stats", [])
                if 0 <= line_index < len(lines) and isinstance(lines[line_index], list):
                    remember_label(lines[line_index][0], key)

        # A few boolean mechanics are deliberately fixed and therefore absent
        # from the roll-event table. Their catalog labels still preserve the
        # exact numeric key ("Stat #292", etc.); include those too.
        if catalog_row:
            for line in catalog_row.get("stats", []):
                if not isinstance(line, list) or len(line) < 2:
                    continue
                match = re.fullmatch(r"Stat #(\d+)", str(line[0]).strip())
                if not match:
                    continue
                key = int(match.group(1))
                values = _numbers(line[1])
                value = _clean_number(max(values) if values else 1.0)
                by_key.setdefault(key, {"key": key, "label": f"Stat #{key}", "value": value})
                name = str(definition.get("name") or "Unknown item")
                observe(key, f"Stat #{key}", value, name)

        if not catalog_row or catalog_row.get("kind") != "unique" or not by_key:
            continue

        donor_rows[int(catalog_id)] = {
            "catalogId": catalog_id,
            "name": definition.get("name"),
            "itemKey": catalog_row.get("key"),
            "properties": _property_rows(int(catalog_id), by_key),
        }

    # Some valid unique records (notably special charms and consumables) are
    # absent from the roll-profile definitions but retain exact catalog lines.
    # Recover direct Stat #N fields and named fields only when the observed
    # runtime evidence maps that label to one unambiguous numeric key.
    for catalog_row in catalog:
        if catalog_row.get("kind") != "unique":
            continue
        catalog_id = int(catalog_row["id"])
        if catalog_id in donor_rows:
            continue
        by_key: dict[int, dict[str, Any]] = {}
        for line in catalog_row.get("stats", []):
            if not isinstance(line, list) or len(line) < 2:
                continue
            raw_label = str(line[0]).strip()
            direct = re.fullmatch(r"Stat #(\d+)", raw_label)
            if direct:
                key = int(direct.group(1))
            else:
                candidates = label_to_keys.get(_normalized_label(raw_label), set())
                if len(candidates) != 1:
                    continue
                key = next(iter(candidates))
            values = _numbers(line[1])
            value = _clean_number(max(values) if values else 1.0)
            label = str(labels.get(str(key), {}).get("label") or raw_label or f"Stat #{key}")
            by_key[key] = {"key": key, "label": label, "value": value}
            observe(key, label, value, str(catalog_row.get("name") or "Unknown item"))
        if by_key:
            donor_rows[catalog_id] = {
                "catalogId": catalog_id,
                "name": catalog_row.get("name"),
                "itemKey": catalog_row.get("key"),
                "properties": _property_rows(catalog_id, by_key),
            }

    # Labels can contain valid observed keys that happened not to occur in one
    # of the selected definition profiles. They remain available in Advanced.
    for raw_key, meta in labels.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            continue
        observations.setdefault(
            key,
            {
                "key": key,
                "label": str(meta.get("label") or f"Stat #{key}"),
                "percent": bool(meta.get("percent", False)),
                "values": [1.0],
                "examples": [],
            },
        )

    stats = []
    for key in sorted(observations):
        row = observations[key]
        values = row.pop("values")
        row["minimumObserved"] = _clean_number(min(values)) if values else 1
        row["maximumObserved"] = _clean_number(max(values)) if values else 1
        # The default a click adds. The observed maximum is dominated by a
        # few outlier uniques (one item carries +70 to All Skills), so the
        # median of the observed unique definitions is the starting value.
        # Sockets are the exception: the ceiling is what a forge is for.
        row["recommendedValue"] = (
            row["maximumObserved"]
            if key == SOCKET_KEY
            else _clean_number(_median(values)) if values else 1
        )
        row["advanced"] = row["label"].startswith("Stat #")
        stats.append(row)

    sorted_donors = sorted(
        donor_rows.values(),
        key=lambda row: (str(row["name"]).casefold(), int(row["catalogId"])),
    )
    payload = {
        "schemaVersion": 1,
        "catalogProfile": model.get("catalogProfile"),
        "exeSha256": model.get("exeSha256"),
        "sourcePayloadSha256": model.get("payloadSha256"),
        "stats": stats,
        "donors": sorted_donors,
        "coverage": {
            "statKeyCount": len(stats),
            "uniqueDonorCount": len(sorted_donors),
            "propertyPresetCount": sum(len(row["properties"]) for row in sorted_donors),
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload["payloadSha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    OUTPUT_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload["coverage"], sort_keys=True))


if __name__ == "__main__":
    main()
