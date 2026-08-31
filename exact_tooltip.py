"""Fail-closed Hero Siege Season 10 item-tooltip calculation.

The public catalog contains definition ranges while a save contains only CPR
seeds.  This module joins those two pieces through a hash-bound, generated
definition map and replays the clean Season 10 build's scalar arithmetic.  It
does not read or write saves, inspect a running process, or guess values for an
unsupported path.

``numbersExact`` is deliberately stricter than "a number was calculated": it
is true only when the executable build is attested, every displayed numeric
definition stat is resolved, and no filled socket payload can add an
unmodelled modifier.  Text localization is a separate concern and is never
claimed exact by this backend.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from . import generated_pool_model
    from . import roll_profile_db
except ImportError:  # Standalone editor/tests import modules from this folder.
    import generated_pool_model  # type: ignore
    import roll_profile_db  # type: ignore


SCHEMA_VERSION = 1
MODEL_SCHEMA_VERSION = 1
EXPECTED_EXE_SHA256 = roll_profile_db.EXPECTED_EXE_SHA256
DEFAULT_MODEL_PATH = Path(__file__).with_name("hs_tooltip_roll_models.json")
DEFAULT_CATALOG_PATH = Path(__file__).with_name("hs_full_catalog.json")
DEFAULT_ROLL_PROFILE_PATH = Path(__file__).with_name("hs_perfect_roll_profiles.json")
_NUMBER = r"-?\d+(?:\.\d+)?"
_RANGE_RE = re.compile(rf"^(?P<lo>{_NUMBER})-(?P<hi>{_NUMBER})$")
_SCALAR_RE = re.compile(rf"^(?P<value>{_NUMBER})$")

# Native weapon definitions store an implementation-only family/range selector
# in stat 447.  It is present on hundreds of otherwise catalog-backed weapons
# but is not a visible tooltip line.  A definition with no catalog evidence is
# handled conservatively below; this one audited helper remains intentionally
# hidden instead of being exposed as a fake ``Stat #447`` property.
_HIDDEN_NATIVE_HELPER_STAT_KEYS = frozenset({447})

# Their saved ``a`` values select identities through separate target tables,
# not merely the numeric definition/CPR paths covered by the general 7.0.6
# roll-equivalence proof.  Keep them fail-closed for a compatible-build status
# until those complete identity tables receive their own audit.
_COMPATIBILITY_EXCLUDED_PROFILE_IDS = frozenset({
    "unique:10:0:31",  # Loaded Dice
    "unique:10:0:89",  # Overloaded Dice
})


def _is_ordinary_small_charm(row: Mapping[str, Any]) -> bool:
    """Identify only the five-by-four native normal Small Charm matrix.

    These items have no serialized rarity or affix list.  ``LoadCommonItems``
    rebuilds both from save seed ``a`` through the generated prefix/suffix
    path, which is deliberately outside the current exact-tooltip model.
    """

    cls = row.get("cls")
    sub = row.get("sub", 0)
    base = row.get("b")
    if any(isinstance(value, bool) for value in (cls, sub, base)):
        return False
    try:
        return (
            str(row.get("kind")) == "normal"
            and int(cls) == 10
            and int(sub) == 0
            and 0 <= int(base) <= 19
            and str(row.get("key") or "") == "charms_normal_small_charm"
        )
    except (TypeError, ValueError):
        return False


class TooltipModelValidationError(ValueError):
    """Raised when the generated model asset does not satisfy its contract."""


@dataclass(frozen=True)
class TooltipModelStatus:
    available: bool
    code: str
    message: str
    path: Path
    profile_count: int = 0
    definition_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "code": self.code,
            "message": self.message,
            "path": str(self.path),
            "profileCount": self.profile_count,
            "definitionCount": self.definition_count,
        }


class TooltipModelDatabase:
    """Validated immutable tooltip definitions with defensive-copy lookup."""

    def __init__(
        self,
        path: Path,
        status: TooltipModelStatus,
        document: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.status = status
        self._document = copy.deepcopy(dict(document or {}))
        self._profiles = self._document.get("profiles", {})
        self._definitions = self._document.get("definitions", {})
        self._stat_labels = self._document.get("statLabels", {})

    @property
    def available(self) -> bool:
        return self.status.available

    @property
    def profile_count(self) -> int:
        return len(self._profiles) if self.available else 0

    @property
    def definition_count(self) -> int:
        return len(self._definitions) if self.available else 0

    def profile(self, profile_id: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        value = self._profiles.get(profile_id)
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def definition(self, definition_id: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        value = self._definitions.get(definition_id)
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def stat_label(self, stat_key: int) -> dict[str, Any]:
        if not self.available:
            return {}
        value = self._stat_labels.get(str(int(stat_key)))
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def summary(self) -> dict[str, Any]:
        output = self.status.as_dict()
        if self.available:
            output.update({
                "schemaVersion": self._document["schemaVersion"],
                "catalogProfile": self._document["catalogProfile"],
                "exeSha256": self._document["exeSha256"],
                "payloadSha256": self._document["payloadSha256"],
            })
        return output


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


def _fail(message: str) -> None:
    raise TooltipModelValidationError(message)


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _validate_definition(definition_id: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("addressKey") != definition_id:
        _fail(f"{definition_id}: malformed definition")
    definition = copy.deepcopy(raw)
    events = definition.get("events")
    stats = definition.get("stats")
    if not isinstance(events, list) or not isinstance(stats, list):
        _fail(f"{definition_id}: events/stats must be arrays")
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"delta", "statKey", "scored"}:
            _fail(f"{definition_id}: malformed event {index}")
        if not isinstance(event["scored"], bool):
            _fail(f"{definition_id}: event {index} scored must be boolean")
        delta = event["delta"]
        if event["scored"]:
            _require_int(delta, f"{definition_id}: event {index} delta")
        elif delta is not None:
            _fail(f"{definition_id}: hidden event {index} must have null delta")
        key = event["statKey"]
        if key is not None:
            _require_int(key, f"{definition_id}: event {index} statKey")
    for index, stat in enumerate(stats):
        if not isinstance(stat, dict):
            _fail(f"{definition_id}: malformed stat {index}")
        key = _require_int(stat.get("statKey"), f"{definition_id}: stat {index} key")
        representation = stat.get("representation")
        if representation not in {"scalar", "range", "dynamic_or_reference"}:
            _fail(f"{definition_id}: unsupported stat representation {representation!r}")
        if not isinstance(stat.get("label"), str) or not stat["label"].strip():
            _fail(f"{definition_id}: stat {index} has no label")
        if not isinstance(stat.get("percent"), bool):
            _fail(f"{definition_id}: stat {index} percent must be boolean")
        line_index = stat.get("catalogLineIndex")
        if line_index is not None:
            _require_int(line_index, f"{definition_id}: stat {index} catalogLineIndex")
        if representation == "range":
            minimum = stat.get("minimum")
            maximum = stat.get("maximum")
            delta = _require_int(stat.get("delta"), f"{definition_id}: stat {index} delta")
            if (
                isinstance(minimum, bool)
                or isinstance(maximum, bool)
                or not isinstance(minimum, (int, float))
                or not isinstance(maximum, (int, float))
                or not all(math.isfinite(float(value)) for value in (minimum, maximum))
                or float(maximum) - float(minimum) != delta
            ):
                _fail(f"{definition_id}: stat {index} has an invalid range")
            event_index = stat.get("eventIndex")
            if event_index is not None:
                event_index = _require_int(
                    event_index, f"{definition_id}: stat {index} eventIndex"
                )
                if event_index >= len(events):
                    _fail(f"{definition_id}: stat {index} eventIndex is out of bounds")
                event = events[event_index]
                if (
                    event["scored"] is not True
                    or event["statKey"] != key
                    or event["delta"] != delta
                ):
                    _fail(f"{definition_id}: stat {index}/event mismatch")
    return definition


def validate_tooltip_model_document(
    document: Any,
    *,
    catalog_path: Path,
    roll_profiles_path: Path,
) -> dict[str, Any]:
    """Validate asset integrity, its bound runtime files, and every signature."""

    if not isinstance(document, dict):
        _fail("tooltip model top level must be an object")
    if document.get("schemaVersion") != MODEL_SCHEMA_VERSION:
        _fail(f"tooltip model schemaVersion must be {MODEL_SCHEMA_VERSION}")
    if document.get("catalogProfile") != roll_profile_db.CATALOG_PROFILE:
        _fail("tooltip model catalog profile mismatch")
    if str(document.get("exeSha256", "")).upper() != EXPECTED_EXE_SHA256:
        _fail("tooltip model belongs to another executable")
    declared_payload_hash = document.get("payloadSha256")
    if not isinstance(declared_payload_hash, str):
        _fail("tooltip model has no payload hash")
    payload = copy.deepcopy(document)
    payload.pop("payloadSha256", None)
    calculated_payload_hash = hashlib.sha256(_canonical_bytes(payload)).hexdigest().upper()
    if declared_payload_hash.upper() != calculated_payload_hash:
        _fail("tooltip model payload hash mismatch")

    source = document.get("source")
    if not isinstance(source, dict):
        _fail("tooltip model has no source manifest")
    if source.get("catalogSha256") != _sha256(catalog_path):
        _fail("tooltip model/catalog hash mismatch")
    if source.get("rollProfilesSha256") != _sha256(roll_profiles_path):
        _fail("tooltip model/roll-profile hash mismatch")
    if source.get("generatedPoolModelSha256") != generated_pool_model.MODEL_BUNDLE_SHA256:
        _fail("tooltip model/generated-pool hash mismatch")

    try:
        roll_document = roll_profile_db.validate_roll_profile_document(
            json.loads(roll_profiles_path.read_text(encoding="utf-8"))
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        _fail(f"bound roll-profile database was rejected: {exc}")
    runtime_profiles = roll_document["profiles"]

    raw_definitions = document.get("definitions")
    raw_profiles = document.get("profiles")
    if not isinstance(raw_definitions, dict) or not isinstance(raw_profiles, dict):
        _fail("tooltip definitions/profiles must be objects")
    definitions = {
        definition_id: _validate_definition(definition_id, raw)
        for definition_id, raw in raw_definitions.items()
    }
    if set(raw_profiles) != set(runtime_profiles):
        _fail("tooltip/runtime profile coverage mismatch")
    dynamic_ids = document.get("dynamicProfileIds")
    if not isinstance(dynamic_ids, list) or len(dynamic_ids) != len(set(dynamic_ids)):
        _fail("tooltip dynamic profile list is malformed")
    dynamic_set = set(dynamic_ids)
    expected_dynamic = {
        profile_id for profile_id, profile in runtime_profiles.items()
        if any("model" in chain for chain in profile["chains"].values())
    } | ({"unique:10:0:89"} & set(runtime_profiles))
    if dynamic_set != expected_dynamic:
        _fail("tooltip dynamic profile coverage mismatch")

    profiles: dict[str, dict[str, Any]] = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, dict) or raw.get("kind") != runtime_profiles[profile_id]["kind"]:
            _fail(f"{profile_id}: malformed tooltip profile")
        components = raw.get("components")
        if not isinstance(components, list) or not components:
            _fail(f"{profile_id}: tooltip profile has no components")
        seen_fields: set[str] = set()
        for component in components:
            if not isinstance(component, dict) or set(component) != {
                "saveField", "role", "definitionId"
            }:
                _fail(f"{profile_id}: malformed tooltip component")
            save_field = component["saveField"]
            role = component["role"]
            definition_id = component["definitionId"]
            if save_field not in roll_profile_db.ALLOWED_SAVE_FIELDS or save_field in seen_fields:
                _fail(f"{profile_id}: invalid/duplicate component save field")
            if role not in {"item", "base", "runeword"}:
                _fail(f"{profile_id}: invalid tooltip component role")
            if definition_id not in definitions:
                _fail(f"{profile_id}: missing tooltip definition {definition_id}")
            seen_fields.add(save_field)
            if profile_id in dynamic_set:
                continue
            signature = [event["delta"] for event in definitions[definition_id]["events"]]
            scored = any(delta is not None for delta in signature)
            chain = runtime_profiles[profile_id]["chains"].get(save_field)
            if scored and (chain is None or chain.get("segments") != [signature]):
                _fail(f"{profile_id}: {save_field} definition/signature mismatch")
            if not scored and chain is not None:
                _fail(f"{profile_id}: {save_field} unexpectedly has a seed chain")
        profiles[profile_id] = copy.deepcopy(raw)

    coverage = document.get("coverage")
    if not isinstance(coverage, dict) or coverage != {
        "definitionCount": len(definitions),
        "profileCount": len(profiles),
        "dynamicProfileCount": len(dynamic_set),
    }:
        _fail("tooltip model coverage counters mismatch")
    validated = copy.deepcopy(document)
    validated["exeSha256"] = EXPECTED_EXE_SHA256
    validated["payloadSha256"] = calculated_payload_hash
    validated["definitions"] = definitions
    validated["profiles"] = profiles
    return validated


def load_tooltip_model_database(
    base_or_path: str | Path | None = None,
    *,
    catalog_path: str | Path | None = None,
    roll_profiles_path: str | Path | None = None,
) -> TooltipModelDatabase:
    """Load the generated model or return a safe unavailable database."""

    model_path = Path(base_or_path) if base_or_path is not None else DEFAULT_MODEL_PATH
    if model_path.is_dir():
        model_path = model_path / DEFAULT_MODEL_PATH.name
    catalog = Path(catalog_path) if catalog_path is not None else model_path.with_name(
        DEFAULT_CATALOG_PATH.name
    )
    profiles = (
        Path(roll_profiles_path) if roll_profiles_path is not None
        else model_path.with_name(DEFAULT_ROLL_PROFILE_PATH.name)
    )
    try:
        document = json.loads(model_path.read_text(encoding="utf-8"))
        validated = validate_tooltip_model_document(
            document, catalog_path=catalog, roll_profiles_path=profiles
        )
    except FileNotFoundError:
        status = TooltipModelStatus(
            False, "missing", "Exact-tooltip model asset is not installed.", model_path
        )
        return TooltipModelDatabase(model_path, status)
    except (OSError, UnicodeError) as exc:
        status = TooltipModelStatus(
            False, "unreadable", f"Exact-tooltip model cannot be read: {exc}", model_path
        )
        return TooltipModelDatabase(model_path, status)
    except (json.JSONDecodeError, TooltipModelValidationError) as exc:
        status = TooltipModelStatus(
            False, "invalid", f"Exact-tooltip model was rejected: {exc}", model_path
        )
        return TooltipModelDatabase(model_path, status)
    status = TooltipModelStatus(
        True,
        "ready",
        f"{len(validated['profiles'])} build-bound tooltip profiles loaded.",
        model_path,
        len(validated["profiles"]),
        len(validated["definitions"]),
    )
    return TooltipModelDatabase(model_path, status, validated)


def _raw_data(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    nested = value.get("data")
    return dict(nested) if isinstance(nested, Mapping) else dict(value)


def _seed(value: Any) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not float(value).is_integer()
    ):
        return None
    integer = int(value)
    return integer if roll_profile_db.SEED_START <= integer <= roll_profile_db.SEED_STOP else None


def _number(value: float | int) -> int | float:
    number = float(value)
    return int(number) if number.is_integer() else number


def _format_number(value: int | float) -> str:
    value = _number(value)
    return str(value)


def _format_value(value: int | float | None, percent: bool) -> str | None:
    if value is None:
        return None
    return f"{_format_number(value)}{'%' if percent else ''}"


def _guard_summary(build_status: Any) -> dict[str, Any]:
    if hasattr(build_status, "as_dict") and callable(build_status.as_dict):
        try:
            build_status = build_status.as_dict()
        except Exception:
            build_status = None
    raw = dict(build_status) if isinstance(build_status, Mapping) else {}
    expected = str(raw.get("expectedSha256") or raw.get("expected_sha256") or "").upper()
    code = str(raw.get("code") or "unverified")
    detected_value = raw.get("detectedSha256") or raw.get("detected_sha256")
    detected = str(detected_value or "").upper()
    direct_match = (
        raw.get("matched") is True
        and expected == EXPECTED_EXE_SHA256
        and code in {"ready", "ready_aurie"}
    )
    # The generated tooltip asset keeps its 7.0.5 source provenance.  A newer
    # executable may use it only after the complete numeric roll path has been
    # independently proven semantically equivalent and pinned by exact hash in
    # roll_profile_db.  This feature-local decision must not change the global
    # build guard or unlock Dice's separately audited identity tables.
    compatible_match = (
        raw.get("matched") is True
        and expected == EXPECTED_EXE_SHA256
        and code == "ready_compatible"
        and detected != EXPECTED_EXE_SHA256
        and roll_profile_db.supports_executable_sha256(detected)
    )
    matched = direct_match or compatible_match
    return {
        "matched": matched,
        "code": code,
        "message": str(raw.get("message") or "Installed build was not attested."),
        "expectedExeSha256": EXPECTED_EXE_SHA256,
        "detectedExeSha256": detected_value,
    }


def _catalog_value(raw: Any) -> tuple[int | float | None, int | float | None, bool]:
    text = str(raw).strip()
    if text.endswith(" (roll)"):
        text = text[:-7].rstrip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].rstrip()
    match = _RANGE_RE.fullmatch(text)
    if match:
        return _number(float(match.group("lo"))), _number(float(match.group("hi"))), percent
    match = _SCALAR_RE.fullmatch(text)
    if match:
        number = _number(float(match.group("value")))
        return number, number, percent
    return None, None, percent


def _fallback_catalog_stats(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    output = []
    for index, raw in enumerate(row.get("stats") or []):
        if not isinstance(raw, list) or len(raw) != 2:
            continue
        minimum, maximum, percent = _catalog_value(raw[1])
        fixed = minimum is not None and minimum == maximum
        output.append({
            "id": f"catalog:{index}",
            "statKey": None,
            "label": str(raw[0]),
            "value": minimum if fixed else None,
            "formattedValue": _format_value(minimum, percent) if fixed else str(raw[1]),
            "minimum": minimum,
            "maximum": maximum,
            "percent": percent,
            "rolled": not fixed,
            "saveField": None,
            "role": "catalog",
            "confidence": "catalog_only",
            "catalogLineIndex": index,
            "sourceRange": str(raw[1]),
            "_order": (0, index, index),
        })
    return output


def _unmapped_catalog_line(
    raw: Mapping[str, Any],
    *,
    definition_id: str,
    role: str,
    component_index: int,
) -> dict[str, Any]:
    index = int(raw["catalogLineIndex"])
    minimum, maximum, percent = _catalog_value(raw.get("template"))
    fixed = minimum is not None and minimum == maximum
    return {
        "id": f"{role}:catalog:{definition_id}:{index}",
        "statKey": None,
        "label": str(raw.get("label") or "Unknown stat"),
        "value": minimum if fixed else None,
        "formattedValue": (
            _format_value(minimum, percent) if fixed else str(raw.get("template") or "")
        ),
        "minimum": minimum,
        "maximum": maximum,
        "percent": percent,
        "rolled": not fixed,
        "saveField": None,
        "role": role,
        "confidence": "catalog_only",
        "catalogLineIndex": index,
        "sourceRange": raw.get("template"),
        "_order": (component_index, index, 1_000_000 + index),
    }


def _socket_payloads(data: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    output: list[dict[str, Any]] = []
    has_filled = False
    for socket_index in range(1, 7):
        encoded = data.get(f"s{socket_index}")
        if encoded in {None, ""}:
            continue
        has_filled = True
        row: dict[str, Any] = {"index": socket_index, "status": "invalid"}
        if isinstance(encoded, str):
            try:
                decoded = json.loads(base64.b64decode(encoded, validate=True))
                if isinstance(decoded, dict):
                    row.update({
                        "status": "decoded",
                        "baseId": decoded.get("b"),
                        "seed": decoded.get("a"),
                        "quantity": decoded.get("n"),
                    })
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        output.append(row)
    return output, has_filled


def _native_socket_count(data: Mapping[str, Any]) -> tuple[bool, int | None]:
    """Return the game's explicit ``zz.sockets`` override when one is present.

    The save format permits JSON numbers, so an integral binary64 value is
    accepted just like the other native numeric fields.  Presence and validity
    are separate: callers must fail closed when a present override is malformed
    instead of silently falling back to definition capacity.
    """

    if "zz" not in data:
        return False, None
    zz = data.get("zz")
    if not isinstance(zz, Mapping):
        return True, None
    if "sockets" not in zz:
        return False, None
    raw = zz.get("sockets")
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
        or not float(raw).is_integer()
    ):
        return True, None
    count = int(raw)
    return (True, count) if 0 <= count <= 6 else (True, None)


def _stat_line(
    stat: Mapping[str, Any],
    *,
    value: int | float | None,
    save_field: str,
    role: str,
    confidence: str,
    component_index: int,
) -> dict[str, Any]:
    minimum = stat.get("minimum")
    maximum = stat.get("maximum")
    if stat.get("representation") == "scalar":
        values = stat.get("values") or []
        minimum = maximum = values[0] if len(values) == 1 else None
    line_index = stat.get("catalogLineIndex")
    return {
        "id": f"{role}:{int(stat['statKey'])}",
        "statKey": int(stat["statKey"]),
        "label": str(stat["label"]),
        "value": _number(value) if value is not None else None,
        "formattedValue": _format_value(value, bool(stat.get("percent"))),
        "minimum": _number(minimum) if isinstance(minimum, (int, float)) else None,
        "maximum": _number(maximum) if isinstance(maximum, (int, float)) else None,
        "percent": bool(stat.get("percent")),
        "rolled": stat.get("representation") == "range",
        "saveField": save_field,
        "role": role,
        "confidence": confidence,
        "catalogLineIndex": line_index,
        "sourceRange": stat.get("catalogTemplate"),
        "_order": (
            component_index,
            int(line_index) if isinstance(line_index, int) else 100_000,
            int(stat["statKey"]),
        ),
    }


def _dynamic_line(
    assignment: Mapping[str, Any],
    *,
    definition: Mapping[str, Any],
    db: TooltipModelDatabase,
    confidence: str,
    component_index: int,
) -> dict[str, Any]:
    key = int(assignment["statKey"])
    local = next(
        (row for row in definition["stats"] if int(row["statKey"]) == key), None
    )
    global_label = db.stat_label(key)
    stat = {
        "statKey": key,
        "label": (
            local.get("label") if local is not None
            else global_label.get("label") or f"Stat #{key}"
        ),
        "percent": (
            bool(local.get("percent")) if local is not None
            else bool(global_label.get("percent", False))
        ),
        "minimum": assignment.get("minimum"),
        "maximum": assignment.get("maximum"),
        "representation": "range",
        "catalogLineIndex": local.get("catalogLineIndex") if local is not None else None,
        "catalogTemplate": local.get("catalogTemplate") if local is not None else None,
    }
    return _stat_line(
        stat,
        value=assignment.get("value"),
        save_field="a",
        role="item",
        confidence=confidence,
        component_index=component_index,
    )


def build_tooltip_model(
    catalog_row: Mapping[str, Any] | None,
    raw_data: Mapping[str, Any] | None,
    roll_profile: Mapping[str, Any] | None,
    *,
    db: TooltipModelDatabase | None = None,
    build_status: Any = None,
    custom_name: str | None = None,
) -> dict[str, Any]:
    """Return a normalized, JSON-safe tooltip model for one saved item.

    Values may still be returned while a build is unverified so callers can
    show a useful preview, but their per-line confidence and ``numbersExact``
    remain fail-closed.
    """

    row = dict(catalog_row or {})
    data = _raw_data(raw_data)
    profile = dict(roll_profile or {})
    database = db or load_tooltip_model_database()
    guard = _guard_summary(build_status)
    profile_id = profile.get("addressKey") if isinstance(profile.get("addressKey"), str) else None
    if (
        guard["matched"]
        and guard["code"] == "ready_compatible"
        and profile_id in _COMPATIBILITY_EXCLUDED_PROFILE_IDS
    ):
        # Defense in depth: even a direct caller which constructs a valid
        # feature-compatible status cannot promote unaudited Dice identities
        # to exact.  The GUI applies the same per-item split before this call.
        guard = {
            **guard,
            "matched": False,
            "code": "dice_build_unverified",
            "message": (
                "Dice identity targets are not yet verified for this compatible build."
            ),
        }
    profile_model = database.profile(profile_id) if profile_id else None
    canonical_name = str(profile.get("name") or row.get("name") or "Unknown item")
    alias = custom_name.strip() if isinstance(custom_name, str) and custom_name.strip() else None
    seed_values = {field: _seed(data.get(field)) for field in ("a", "i", "s")}
    seeds = {field: seed for field, seed in seed_values.items() if seed is not None}
    sockets, has_filled_sockets = _socket_payloads(data)
    socket_count_present, native_socket_count = _native_socket_count(data)
    fingerprint = hashlib.sha256(_canonical_bytes(data)).hexdigest().upper()[:16]
    unsupported: list[str] = []
    warnings: list[str] = []
    lines_by_key: dict[int, dict[str, Any]] = {}
    extra_lines: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    objective_maxed = 0
    objective_total = 0
    objective_deficit = 0
    calculated_any = False
    ordinary_small_charm = _is_ordinary_small_charm(row)

    if ordinary_small_charm:
        # The definition itself is empty, but the resulting item is not a
        # fixed-stat item: native code generates its rarity and affixes from
        # the continuous ``a`` CPR chain.  Never let an empty definition turn
        # into a false EXACT/FIXED claim.
        unsupported.append("generated_affixes_unmodelled:small_charm")
        warnings.append(
            "Rolled rarity and affixes are generated in-game from seed a; "
            "this build does not decode that native path."
        )

    if not database.available:
        unsupported.append("tooltip_model_unavailable")
        warnings.append(database.status.message)
    if not guard["matched"]:
        unsupported.append("build_unverified")
        warnings.append(guard["message"])
    if profile_id is None:
        unsupported.append("roll_profile_missing")
    elif profile_model is None:
        unsupported.append("tooltip_profile_missing")

    if profile_model is None:
        extra_lines = _fallback_catalog_stats(row)
    else:
        is_dynamic = profile_id in set(
            database._document.get("dynamicProfileIds", [])  # validated private data
        )
        confidence = "exact" if guard["matched"] else "modelled_unverified"
        for component_index, component in enumerate(profile_model["components"]):
            save_field = str(component["saveField"])
            role = str(component["role"])
            definition = database.definition(str(component["definitionId"]))
            if definition is None:  # Defensive; validated assets cannot reach this.
                unsupported.append(f"definition_missing:{component['definitionId']}")
                continue

            unmapped_catalog = definition.get("unmappedCatalogLines") or []
            if unmapped_catalog:
                unsupported.append(f"catalog_line_mapping:{component['definitionId']}")
                extra_lines.extend(
                    _unmapped_catalog_line(
                        raw,
                        definition_id=str(component["definitionId"]),
                        role=role,
                        component_index=component_index,
                    )
                    for raw in unmapped_catalog
                )

            # Most fallback-labelled native fields are proven implementation
            # helpers because another field in the same definition is bound to
            # the public tooltip catalog.  Some new Season 10 definitions have
            # no catalog row at all, though.  In that case silently dropping a
            # fallback field can make an incomplete tooltip claim exactness.
            # Preserve those fields under a neutral Stat # label and block the
            # exact badge until their visible identity is independently mapped.
            definition_has_catalog_evidence = bool(unmapped_catalog) or any(
                isinstance(stat.get("catalogLineIndex"), int)
                for stat in definition["stats"]
            )
            exposed_fallback_keys = {
                int(stat["statKey"])
                for stat in definition["stats"]
                if stat.get("labelSource") == "fallback"
                and not definition_has_catalog_evidence
                and int(stat["statKey"]) not in _HIDDEN_NATIVE_HELPER_STAT_KEYS
            }
            unsupported.extend(
                f"unmapped_definition_stat:{key}"
                for key in sorted(exposed_fallback_keys)
            )

            if is_dynamic:
                # Fixed definition writes remain visible unless a dynamic
                # assignment to the same key replaces them below.
                pool_slot_keys = {
                    int(key)
                    for key in generated_pool_model.PROFILE_MODELS[profile_id][
                        "poolSlots"
                    ]
                }
                for stat in definition["stats"]:
                    if int(stat["statKey"]) in pool_slot_keys:
                        # These scalars configure generated-stat slots; they
                        # are not visible item properties.
                        continue
                    stat_key = int(stat["statKey"])
                    if (
                        stat.get("labelSource") == "fallback"
                        and stat_key not in exposed_fallback_keys
                    ):
                        continue
                    if stat["representation"] == "scalar" and len(stat.get("values") or []) == 1:
                        value = stat["values"][0]
                        lines_by_key[stat_key] = _stat_line(
                            stat,
                            value=value,
                            save_field=save_field,
                            role=role,
                            confidence=(
                                "unmapped" if stat_key in exposed_fallback_keys
                                else confidence
                            ),
                            component_index=component_index,
                        )
                        calculated_any = True
                    elif stat["representation"] == "dynamic_or_reference":
                        unsupported.append(f"dynamic_reference:{stat_key}")
                seed = seed_values.get("a")
                if seed is None:
                    unsupported.append("missing_or_invalid_seed:a")
                    for stat in definition["stats"]:
                        if stat["representation"] == "range":
                            lines_by_key[int(stat["statKey"])] = _stat_line(
                                stat,
                                value=None,
                                save_field="a",
                                role="item",
                                confidence="unresolved",
                                component_index=component_index,
                            )
                    continue
                try:
                    replay = generated_pool_model.replay(profile_id, seed)
                except ValueError as exc:
                    unsupported.append("dynamic_replay_failed")
                    warnings.append(str(exc))
                    continue
                for assignment in replay["visibleAssignments"]:
                    assignment_key = int(assignment["statKey"])
                    dynamic_line = _dynamic_line(
                        assignment,
                        definition=definition,
                        db=database,
                        confidence=(
                            "unmapped" if assignment_key in exposed_fallback_keys
                            else confidence
                        ),
                        component_index=component_index,
                    )
                    lines_by_key[assignment_key] = dynamic_line
                    calculated_any = True
                identities.extend(copy.deepcopy(replay.get("identityResults") or []))
                objective_maxed += int(replay["maxed"])
                objective_total += int(replay["total"])
                objective_deficit += int(replay["endpointDeficit"])
                continue

            signature = [event["delta"] for event in definition["events"]]
            chain = (profile.get("chains") or {}).get(save_field)
            chain_valid = (
                not any(delta is not None for delta in signature)
                or isinstance(chain, Mapping) and chain.get("segments") == [signature]
            )
            seed = seed_values.get(save_field)
            evaluation = None
            if any(delta is not None for delta in signature):
                if not chain_valid:
                    unsupported.append(f"profile_signature_mismatch:{save_field}")
                elif seed is None:
                    unsupported.append(f"missing_or_invalid_seed:{save_field}")
                else:
                    evaluation = roll_profile_db.evaluate_seed(seed, signature)
                    objective_maxed += evaluation.maxed
                    objective_total += evaluation.total
                    objective_deficit += evaluation.endpoint_deficit
            for stat in definition["stats"]:
                stat_key = int(stat["statKey"])
                if (
                    stat.get("labelSource") == "fallback"
                    and stat_key not in exposed_fallback_keys
                ):
                    # A native definition can contain internal helper values
                    # absent from the public visible-stat catalog.  Do not
                    # leak those implementation details into the tooltip.
                    continue
                representation = stat["representation"]
                value: int | float | None = None
                line_confidence = (
                    "unmapped" if stat_key in exposed_fallback_keys else confidence
                )
                if representation == "scalar" and len(stat.get("values") or []) == 1:
                    value = stat["values"][0]
                    calculated_any = True
                elif representation == "range":
                    event_index = stat.get("eventIndex")
                    if (
                        evaluation is not None
                        and isinstance(event_index, int)
                        and evaluation.event_rolls[event_index] is not None
                    ):
                        value = _number(
                            float(stat["minimum"])
                            + int(evaluation.event_rolls[event_index])
                        )
                        calculated_any = True
                    else:
                        line_confidence = "unresolved"
                        unsupported.append(f"unresolved_range:{stat_key}")
                else:
                    line_confidence = "unresolved"
                    unsupported.append(f"dynamic_reference:{stat_key}")
                lines_by_key[stat_key] = _stat_line(
                    stat,
                    value=value,
                    save_field=save_field,
                    role=role,
                    confidence=line_confidence,
                    component_index=component_index,
                )

    if socket_count_present and native_socket_count is None:
        unsupported.append("invalid_socket_count")
        socket_line = lines_by_key.get(20)
        if socket_line is not None:
            socket_line["confidence"] = "unresolved"
    elif native_socket_count is not None:
        # ``zz.sockets`` is the saved item's current socket count and is the
        # value consumed by the game.  It intentionally overrides a definition
        # key-20 capacity/range without making an otherwise supported model
        # inexact.  A missing definition line is still representable because
        # the native metadata supplies the complete current value.
        socket_line = lines_by_key.get(20)
        if socket_line is None:
            socket_label = database.stat_label(20).get("label") or "Sockets"
            socket_line = _stat_line(
                {
                    "statKey": 20,
                    "representation": "scalar",
                    "values": [native_socket_count],
                    "label": socket_label,
                    "percent": False,
                    "catalogLineIndex": None,
                    "catalogTemplate": None,
                },
                value=native_socket_count,
                save_field="zz.sockets",
                role="item",
                confidence=("exact" if guard["matched"] else "modelled_unverified"),
                component_index=len(profile_model.get("components") or []) if profile_model else 0,
            )
            lines_by_key[20] = socket_line
        else:
            socket_line["value"] = native_socket_count
            socket_line["formattedValue"] = _format_value(native_socket_count, False)
            socket_line["saveField"] = "zz.sockets"

    if has_filled_sockets:
        unsupported.append("socket_payload_effects_unmodelled")
    unsupported = list(dict.fromkeys(unsupported))
    warnings = list(dict.fromkeys(warnings))
    stats = list(lines_by_key.values()) + extra_lines
    stats.sort(key=lambda line: line["_order"])
    for line in stats:
        line.pop("_order", None)
    all_numeric_resolved = all(
        line["confidence"] == "exact"
        for line in stats
        if line.get("minimum") is not None or line.get("maximum") is not None
    )
    exact_blockers = {
        blocker for blocker in unsupported
        if blocker.startswith((
            "tooltip_model_", "roll_profile_", "tooltip_profile_",
            "build_", "missing_or_invalid_seed", "profile_signature_",
            "unresolved_range", "dynamic_reference", "dynamic_replay_",
            "socket_payload_", "definition_missing", "catalog_line_mapping",
            "unmapped_definition_stat", "invalid_socket_count",
            "generated_affixes_",
        ))
    }
    numbers_exact = (
        database.available
        and guard["matched"]
        and profile_model is not None
        and all_numeric_resolved
        and not exact_blockers
    )
    coverage = (
        "exact_numbers" if numbers_exact
        else "partial" if calculated_any
        else "catalog_only"
    )
    roll_quality = {
        "maxed": objective_maxed,
        "total": objective_total,
        "endpointDeficit": objective_deficit,
        "percent": (
            round(100.0 * objective_maxed / objective_total, 2)
            if objective_total else None
        ),
    }
    return {
        "schemaVersion": SCHEMA_VERSION,
        "profileId": profile_id,
        "fingerprint": fingerprint,
        "item": {
            "name": alias or canonical_name,
            "canonicalName": canonical_name,
            "customName": alias,
            "rarity": (
                "Unresolved"
                if ordinary_small_charm
                else profile.get("kind") == "runeword" and "Runeword" or row.get("rar")
            ),
            "baseRarity": row.get("rar") if ordinary_small_charm else None,
            "rolledRarityKnown": False if ordinary_small_charm else True,
            "tier": row.get("tier"),
            "requiredLevel": row.get("lvl"),
            "iconId": row.get("spr"),
            "catalogId": row.get("id"),
        },
        "seeds": seeds,
        "stats": stats,
        "sockets": sockets,
        "identities": identities,
        "rollQuality": roll_quality,
        "calculation": {
            "coverage": coverage,
            "numbersExact": numbers_exact,
            "textExact": False,
            "buildMatched": guard["matched"],
            "unsupportedPaths": unsupported,
            "warnings": warnings,
        },
        "buildGuard": guard,
    }


def compare_tooltip_models(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare normalized models without inferring whether higher is better."""

    def keyed(model: Mapping[str, Any]) -> dict[tuple[Any, ...], Mapping[str, Any]]:
        output = {}
        for line in model.get("stats") or []:
            if not isinstance(line, Mapping):
                continue
            key = (
                "key", int(line["statKey"])
            ) if isinstance(line.get("statKey"), int) else (
                "label", str(line.get("label")), str(line.get("role"))
            )
            output[key] = line
        return output

    left_stats, right_stats = keyed(left), keyed(right)
    rows = []
    for key in sorted(set(left_stats) | set(right_stats), key=str):
        left_line, right_line = left_stats.get(key), right_stats.get(key)
        left_value = left_line.get("value") if left_line else None
        right_value = right_line.get("value") if right_line else None
        delta = (
            _number(float(right_value) - float(left_value))
            if isinstance(left_value, (int, float))
            and not isinstance(left_value, bool)
            and isinstance(right_value, (int, float))
            and not isinstance(right_value, bool)
            else None
        )
        rows.append({
            "statKey": (left_line or right_line).get("statKey"),
            "label": (left_line or right_line).get("label"),
            "left": copy.deepcopy(left_line),
            "right": copy.deepcopy(right_line),
            "delta": delta,
            "different": left_value != right_value or (left_line is None) != (right_line is None),
        })
    return {
        "leftFingerprint": left.get("fingerprint"),
        "rightFingerprint": right.get("fingerprint"),
        "different": any(row["different"] for row in rows),
        "rows": rows,
    }


__all__ = [
    "DEFAULT_MODEL_PATH",
    "EXPECTED_EXE_SHA256",
    "MODEL_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "TooltipModelDatabase",
    "TooltipModelStatus",
    "TooltipModelValidationError",
    "build_tooltip_model",
    "compare_tooltip_models",
    "load_tooltip_model_database",
    "validate_tooltip_model_document",
]
