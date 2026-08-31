#!/usr/bin/env python3
"""Build the hash-bound runtime data used by :mod:`exact_tooltip`.

The perfect-roll database intentionally contains only CPR signatures.  That is
enough to choose a seed, but not enough to turn an arbitrary saved seed back
into named values.  This developer-only builder joins the audited definition
export to the public catalog and emits the small definition/event map required
at runtime.  The resulting application has no dependency on ``_research``.

This script never opens or changes a Hero Siege save.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    import generated_pool_model
except ImportError:  # pragma: no cover - package-style developer invocation.
    from . import generated_pool_model  # type: ignore


EXPECTED_EXE_SHA256 = (
    "438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4"
)
SCHEMA_VERSION = 1
DIRECT_PROFILE_RE = re.compile(r"(?:normal|unique):\d+:\d+:\d+\Z")
RUNEWORD_PROFILE_RE = re.compile(
    r"runeword:(?P<rw>\d+)\|normal:(?P<cls>\d+):(?P<sub>\d+):(?P<base>\d+)\Z"
)
NUMBER_RE = r"-?\d+(?:\.\d+)?"
RANGE_RE = re.compile(rf"^(?P<lo>{NUMBER_RE})-(?P<hi>{NUMBER_RE})$")
SCALAR_RE = re.compile(rf"^(?P<value>{NUMBER_RE})$")
FIXED_REFERENCE_SOCKET_VALUES = {
    "unique:3:13:13": 5,
    "unique:3:13:18": 4,
    "unique:5:0:67": 1,
    "unique:8:0:51": 2,
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def _number(value: Any) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _catalog_signature(raw: Any) -> tuple[Any, ...] | None:
    text = str(raw).strip()
    roll_suffix = text.endswith(" (roll)")
    if roll_suffix:
        text = text[:-7].rstrip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].rstrip()
    match = RANGE_RE.fullmatch(text)
    if match:
        return (
            "range",
            _number(match.group("lo")),
            _number(match.group("hi")),
        )
    match = SCALAR_RE.fullmatch(text)
    if match:
        return ("scalar", _number(match.group("value")))
    return None


def _stat_signature(stat: Mapping[str, Any]) -> tuple[Any, ...] | None:
    representation = stat.get("representation")
    if representation == "range":
        return (
            "range",
            _number(stat["minimum"]),
            _number(stat["maximum"]),
        )
    if representation == "scalar":
        values = stat.get("values")
        if isinstance(values, list) and len(values) == 1:
            return ("scalar", _number(values[0]))
    return None


def _catalog_row_for_definition(
    definition: Mapping[str, Any], catalog: list[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    catalog_id = definition.get("catalogId")
    if isinstance(catalog_id, bool) or not isinstance(catalog_id, int):
        return None
    if not 0 <= catalog_id < len(catalog):
        return None
    row = catalog[catalog_id]
    # IDs were rebuilt by the editor overlay in some releases.  Refuse a
    # coincidental numeric match when the native address no longer agrees.
    address = definition.get("address") or {}
    try:
        if (
            str(row.get("kind")) != str(address.get("kind"))
            or int(row.get("cls")) != int(address.get("cls"))
            or int(row.get("sub", 0)) != int(address.get("sub", 0))
            or int(row.get("b")) != int(address.get("b"))
        ):
            return None
    except (TypeError, ValueError):
        return None
    return row


def _catalog_lines(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if row is None:
        return output
    raw_stats = row.get("stats")
    if not isinstance(raw_stats, list):
        return output
    for index, raw in enumerate(raw_stats):
        if not isinstance(raw, list) or len(raw) != 2:
            continue
        label, value = str(raw[0]).strip(), str(raw[1]).strip()
        output.append({
            "index": index,
            "label": label,
            "template": value,
            "signature": _catalog_signature(value),
            "percent": "%" in value,
        })
    return output


def _unique_value_votes(
    definitions: Mapping[str, Mapping[str, Any]],
    catalog: list[Mapping[str, Any]],
) -> tuple[dict[int, Counter], dict[int, Counter]]:
    label_votes: dict[int, Counter] = defaultdict(Counter)
    format_votes: dict[int, Counter] = defaultdict(Counter)
    for definition in definitions.values():
        lines = _catalog_lines(_catalog_row_for_definition(definition, catalog))
        stats = definition.get("stats") or []
        for stat in stats:
            signature = _stat_signature(stat)
            if signature is None:
                continue
            matching_stats = [
                candidate for candidate in stats
                if _stat_signature(candidate) == signature
            ]
            matching_lines = [line for line in lines if line["signature"] == signature]
            if len(matching_stats) == len(matching_lines) == 1:
                key = int(stat["key"])
                line = matching_lines[0]
                label_votes[key][line["label"]] += 1
                format_votes[key][bool(line["percent"])] += 1
    return label_votes, format_votes


def _winner(counter: Counter) -> Any | None:
    if not counter:
        return None
    common = counter.most_common(2)
    if len(common) > 1 and common[0][1] == common[1][1]:
        return None
    return common[0][0]


def _normalized_events(profile_id: str, definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in definition.get("events") or []:
        if "advance" in raw:
            for _ in range(int(raw["advance"])):
                output.append({"delta": None, "statKey": None, "scored": False})
            continue
        key = int(raw["key"]) if raw.get("key") is not None else None
        scored = bool(raw.get("score", True))
        if profile_id == "unique:10:0:89" and key == 419:
            scored = False
        output.append({
            "delta": int(raw["delta"]) if scored else None,
            "statKey": key,
            "scored": scored,
        })

    if (definition.get("address") or {}).get("kind") == "unique":
        output.extend(
            {"delta": None, "statKey": None, "scored": False}
            for _ in range(8)
        )
        stats = definition.get("stats") or []
        if any(int(row.get("key", -1)) == 444 for row in stats):
            output.append({"delta": None, "statKey": 444, "scored": False})
        sockets = [row for row in stats if int(row.get("key", -1)) == 20]
        if not sockets:
            output.append({"delta": None, "statKey": 20, "scored": False})
        elif len(sockets) != 1:
            raise ValueError(f"{profile_id}: duplicate socket stat")
        else:
            socket = sockets[0]
            representation = socket.get("representation")
            if representation == "range":
                output.append({
                    "delta": int(socket["delta"]),
                    "statKey": 20,
                    "scored": True,
                })
            elif representation == "scalar":
                pass
            elif (
                representation == "dynamic_or_reference"
                and profile_id in FIXED_REFERENCE_SOCKET_VALUES
            ):
                pass
            else:
                # This path is not a numeric seed objective.  Keep the model
                # honest rather than guessing a draw or value.
                pass
    return output


def _line_for_stat(
    stat: Mapping[str, Any],
    lines: list[dict[str, Any]],
    used: set[int],
    label_votes: Mapping[int, Counter],
) -> dict[str, Any] | None:
    signature = _stat_signature(stat)
    candidates = [
        line for line in lines
        if line["index"] not in used and line["signature"] == signature
    ]
    preferred = _winner(label_votes.get(int(stat["key"]), Counter()))
    preferred_candidates = [line for line in candidates if line["label"] == preferred]
    if len(preferred_candidates) == 1:
        return preferred_candidates[0]
    # A global key/name identity is stronger evidence than a coincidentally
    # unique numeric value inside one item.  Harlequinn, for example, has both
    # fixed 2 All Talents and fixed socket capacity 2; blindly consuming the
    # scalar line assigns the wrong English name to both keys.
    if len(candidates) == 1 and preferred is None:
        return candidates[0]

    # The public catalog describes socket occupancy as ``0-capacity``; the
    # native definition stores only the fixed capacity.  This is the same
    # visible Sockets line, not an unresolved value mapping.
    if int(stat["key"]) == 20 and stat.get("representation") == "scalar":
        values = stat.get("values") or []
        socket_candidates = [
            line for line in lines
            if line["index"] not in used
            and line["label"].casefold() == "sockets"
            and isinstance(line["signature"], tuple)
            and len(line["signature"]) == 3
            and line["signature"][0] == "range"
            and len(values) == 1
            and line["signature"][2] == _number(values[0])
        ]
        if len(socket_candidates) == 1:
            return socket_candidates[0]
    return None


def _definition_model(
    profile_id: str,
    definition: Mapping[str, Any],
    catalog: list[Mapping[str, Any]],
    label_votes: Mapping[int, Counter],
    global_labels: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    lines = _catalog_lines(_catalog_row_for_definition(definition, catalog))
    events = _normalized_events(profile_id, definition)
    event_by_key: dict[int, list[int]] = defaultdict(list)
    for index, event in enumerate(events):
        if event["scored"] and event["statKey"] is not None:
            event_by_key[int(event["statKey"])].append(index)

    used_lines: set[int] = set()
    output_stats: list[dict[str, Any]] = []
    # Resolve the most constrained definitions first; final display order is
    # restored through catalogLineIndex by the runtime.
    source_stats = list(definition.get("stats") or [])
    source_stats.sort(key=lambda row: (
        sum(line["signature"] == _stat_signature(row) for line in lines),
        int(row.get("key", -1)),
    ))
    for raw_stat in source_stats:
        key = int(raw_stat["key"])
        representation = str(raw_stat.get("representation"))
        line = _line_for_stat(raw_stat, lines, used_lines, label_votes)
        if line is not None:
            used_lines.add(int(line["index"]))
        global_label = global_labels.get(str(key), {})
        label = (
            line["label"] if line is not None
            else global_label.get("label") or f"Stat #{key}"
        )
        percent = (
            bool(line["percent"]) if line is not None
            else bool(global_label.get("percent", False))
        )
        values = [_number(value) for value in raw_stat.get("values") or []]
        model: dict[str, Any] = {
            "statKey": key,
            "representation": representation,
            "values": values,
            "label": label,
            "percent": percent,
            "catalogLineIndex": line["index"] if line is not None else None,
            "catalogTemplate": line["template"] if line is not None else None,
            "labelSource": (
                "item_catalog" if line is not None
                else "global_catalog" if global_label.get("label")
                else "fallback"
            ),
        }
        if representation == "range":
            model.update({
                "minimum": _number(raw_stat["minimum"]),
                "maximum": _number(raw_stat["maximum"]),
                "delta": int(raw_stat["delta"]),
            })
            candidates = event_by_key.get(key, [])
            if candidates:
                # SetItemStat is last-write-wins.  The late-unique dynamic
                # definitions are replayed by generated_pool_model instead.
                model["eventIndex"] = candidates[-1]
        output_stats.append(model)

    pool_slot_keys = {
        int(key)
        for key in generated_pool_model.PROFILE_MODELS.get(profile_id, {}).get(
            "poolSlots", {}
        )
    }

    def visible_unmapped(line: Mapping[str, Any]) -> bool:
        match = re.fullmatch(r"Stat #(\d+)", str(line["label"]))
        return match is None or int(match.group(1)) not in pool_slot_keys

    return {
        "addressKey": profile_id,
        "catalogId": definition.get("catalogId"),
        "name": str(definition.get("name") or profile_id),
        "events": events,
        "stats": output_stats,
        "unmappedCatalogLines": [
            {
                "catalogLineIndex": line["index"],
                "label": line["label"],
                "template": line["template"],
            }
            for line in lines
            if line["index"] not in used_lines and visible_unmapped(line)
        ],
    }


def _runeword_component_index(bridge: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    output: dict[int, dict[str, Any]] = {}
    for recipe in bridge.get("runeword_base_matrix") or []:
        recipe_id = int(recipe["rw"])
        if recipe_id in output:
            raise ValueError(f"duplicate runeword recipe {recipe_id}")
        output[recipe_id] = dict(recipe)
    return output


def build_document(
    definitions_path: Path,
    bridge_path: Path,
    catalog_path: Path,
    roll_profiles_path: Path,
) -> dict[str, Any]:
    definitions_doc = _load_json(definitions_path)
    bridge = _load_json(bridge_path)
    catalog = _load_json(catalog_path)
    roll_profiles = _load_json(roll_profiles_path)
    definitions = definitions_doc.get("profiles") or {}
    runtime_profiles = roll_profiles.get("profiles") or {}
    if str((definitions_doc.get("meta") or {}).get("exeSha256", "")).upper() != EXPECTED_EXE_SHA256:
        raise ValueError("definition export belongs to another executable")
    if str((bridge.get("meta") or {}).get("exe_sha256", "")).upper() != EXPECTED_EXE_SHA256:
        raise ValueError("definition bridge belongs to another executable")
    if str(roll_profiles.get("exeSha256", "")).upper() != EXPECTED_EXE_SHA256:
        raise ValueError("roll-profile database belongs to another executable")
    if not isinstance(catalog, list) or not isinstance(definitions, dict):
        raise ValueError("malformed catalog or definition export")

    label_votes, format_votes = _unique_value_votes(definitions, catalog)
    global_labels: dict[str, dict[str, Any]] = {}
    for key in sorted(set(label_votes) | set(format_votes)):
        label = _winner(label_votes.get(key, Counter()))
        percent = _winner(format_votes.get(key, Counter()))
        if label is not None:
            global_labels[str(key)] = {
                "label": label,
                "percent": bool(percent) if percent is not None else False,
                "evidenceCount": int(label_votes[key][label]),
            }

    definition_models = {
        profile_id: _definition_model(
            profile_id, definition, catalog, label_votes, global_labels
        )
        for profile_id, definition in definitions.items()
    }
    mismatches = [
        (definition_id, stat["statKey"], stat["label"], global_labels[str(stat["statKey"])]["label"])
        for definition_id, definition in definition_models.items()
        for stat in definition["stats"]
        if stat["labelSource"] == "item_catalog"
        and str(stat["statKey"]) in global_labels
        and stat["label"] != global_labels[str(stat["statKey"])]["label"]
    ]
    if mismatches:
        raise ValueError(
            "catalog/global stat-label identity mismatch: "
            + ", ".join(map(str, mismatches[:10]))
        )

    runewords = _runeword_component_index(bridge)
    profile_models: dict[str, dict[str, Any]] = {}
    for profile_id, runtime_profile in runtime_profiles.items():
        components: list[dict[str, str]] = []
        if DIRECT_PROFILE_RE.fullmatch(profile_id):
            if profile_id not in definition_models:
                raise ValueError(f"{profile_id}: missing direct definition")
            components.append({
                "saveField": "a",
                "role": "item",
                "definitionId": profile_id,
            })
        else:
            match = RUNEWORD_PROFILE_RE.fullmatch(profile_id)
            if match is None:
                raise ValueError(f"{profile_id}: unsupported runtime profile ID")
            recipe_id = int(match.group("rw"))
            recipe = runewords.get(recipe_id)
            if recipe is None:
                raise ValueError(f"{profile_id}: missing recipe bridge")
            base_id = (
                f"normal:{int(match.group('cls'))}:{int(match.group('sub'))}:"
                f"{int(match.group('base'))}"
            )
            overlay_id = str(recipe["definition_address_key"])
            for definition_id in (base_id, overlay_id):
                if definition_id not in definition_models:
                    raise ValueError(f"{profile_id}: missing definition {definition_id}")
            components.extend((
                {"saveField": "a", "role": "base", "definitionId": base_id},
                {"saveField": "i", "role": "runeword", "definitionId": overlay_id},
            ))
        profile_models[profile_id] = {
            "kind": str(runtime_profile.get("kind")),
            "components": components,
        }

    dynamic_ids = sorted(
        profile_id for profile_id, profile in runtime_profiles.items()
        if any("model" in chain for chain in (profile.get("chains") or {}).values())
        or profile_id == "unique:10:0:89"
    )
    document: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "catalogProfile": "Season 10",
        "exeSha256": EXPECTED_EXE_SHA256,
        "source": {
            "definitionsFile": definitions_path.name,
            "definitionsSha256": _sha256(definitions_path),
            "bridgeFile": bridge_path.name,
            "bridgeSha256": _sha256(bridge_path),
            "catalogFile": catalog_path.name,
            "catalogSha256": _sha256(catalog_path),
            "rollProfilesFile": roll_profiles_path.name,
            "rollProfilesSha256": _sha256(roll_profiles_path),
            "generatedPoolModelSha256": generated_pool_model.MODEL_BUNDLE_SHA256,
        },
        "coverage": {
            "definitionCount": len(definition_models),
            "profileCount": len(profile_models),
            "dynamicProfileCount": len(dynamic_ids),
        },
        "dynamicProfileIds": dynamic_ids,
        "statLabels": global_labels,
        "definitions": definition_models,
        "profiles": profile_models,
    }
    document["payloadSha256"] = hashlib.sha256(_canonical_bytes(document)).hexdigest().upper()
    return document


def _default_paths() -> dict[str, Path]:
    here = Path(__file__).resolve().parent
    source_root = here.parent.parent
    research = source_root / "_research"
    return {
        "definitions": research / "item_roll_profiles_all_438b.json",
        "bridge": research / "item_definition_bridge_438b.json",
        "catalog": here / "hs_full_catalog.json",
        "roll_profiles": here / "hs_perfect_roll_profiles.json",
        "output": here / "hs_tooltip_roll_models.json",
    }


def main(argv: Iterable[str] | None = None) -> int:
    defaults = _default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--definitions", type=Path, default=defaults["definitions"])
    parser.add_argument("--bridge", type=Path, default=defaults["bridge"])
    parser.add_argument("--catalog", type=Path, default=defaults["catalog"])
    parser.add_argument("--roll-profiles", type=Path, default=defaults["roll_profiles"])
    parser.add_argument("--output", type=Path, default=defaults["output"])
    args = parser.parse_args(list(argv) if argv is not None else None)
    document = build_document(
        args.definitions, args.bridge, args.catalog, args.roll_profiles
    )
    args.output.write_text(
        json.dumps(document, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        f"wrote {args.output} ({document['coverage']['profileCount']} profiles, "
        f"{document['coverage']['definitionCount']} definitions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
