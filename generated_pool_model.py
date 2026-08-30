"""Self-contained clean-438B late unique-item CPR replay model.

The 21 endpoint-actionable generated-pool profiles and Overloaded Dice's
identity-only rejection trajectory are embedded here.  Runtime data can only
refer to these hash-bound models; it cannot supply event lists, pool tables,
or valid identity sets.  The loader therefore remains independent from every
``_research`` file while still replaying the complete winning/audit path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Mapping


EXPECTED_EXE_SHA256 = (
    "438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4"
)
MODEL_KIND = "late_unique_dynamic_438b_v2"
MODEL_BUNDLE_SHA256 = "6688D0BBAA39AF6E3DBB9988212DD43027BC935A981BB17973A66DD28561A0E7"

# These hashes bind the embedded tables to the final static research merger.
# They are data, not runtime file dependencies.
SOURCE_ARTIFACT_SHA256 = {
    "generatedPoolSpec": "D4DE4126CDDC80662A977BEA7679AECF8F65A4C22F7DED95317F2E82902DBFCB",
    "lateUniqueSpec": "EFFE9C05E51F23403C3A9660390190653CFA347FAA81B5A6408D96042E45BF8A",
    "uniqueRuntimeProfiles": "DF019EB4E914512F712A8B3E5C40DE8EFB92557AA5D5D196E2B5F30759D2FF27",
}

_CPR_MULTIPLIER = 1_789_570_533.0
_CPR_INCREMENT = 465_707.0
_CPR_MODULUS = 2_147_483_648.0
_CPR_MASK = 1_073_741_823
_CPR_MAX = 1_073_741_823.0


def _entry(key: int, minimum: int, maximum: int) -> dict[str, int]:
    return {"key": key, "minimum": minimum, "maximum": maximum}


POOL_TABLES: dict[int, list[dict[str, Any]]] = {
    1: [
        _entry(33, 20, 40), _entry(36, 20, 40), _entry(25, 10, 20),
        _entry(57, 6, 12), _entry(59, 15, 30), _entry(64, 6, 12),
        _entry(66, 15, 30), _entry(68, 10, 20), _entry(72, 4, 10),
        _entry(73, 10, 30), _entry(74, 250, 400), _entry(75, 15, 25),
        _entry(76, 3, 5), _entry(95, 10, 20), _entry(97, 10, 20),
        _entry(99, 10, 20), _entry(129, 15, 50), _entry(282, 1, 5),
        _entry(80, 8, 20), _entry(79, 8, 20), _entry(51, 10, 25),
        _entry(128, 18, 38), _entry(222, 1, 3),
    ],
    2: [
        _entry(39, 20, 40), _entry(42, 20, 40), _entry(25, 10, 20),
        _entry(58, 6, 12), _entry(59, 15, 30), _entry(65, 6, 12),
        _entry(66, 15, 30), _entry(196, 10, 20), _entry(198, 5, 15),
        _entry(200, 5, 15),
        {"keysBySubtype": [224, 223, 225, 226, 227], "minimum": 2, "maximum": 4},
        {"keysBySubtype": [143, 139, 135, 147, 151], "minimum": 15, "maximum": 25},
        _entry(437, 5, 10),
        {"keysBySubtype": [141, 137, 133, 145, 149], "minimum": 22, "maximum": 50},
        _entry(167, 15, 25),
        {"keysBySubtype": [142, 138, 134, 146, 150], "minimum": 15, "maximum": 40},
        _entry(282, 1, 5),
    ],
    3: [
        _entry(45, 20, 40), _entry(48, 20, 40), _entry(25, 10, 20),
        _entry(55, 20, 50), _entry(54, 4, 8), _entry(63, 20, 50),
        _entry(62, 4, 8), _entry(155, 10, 20), _entry(160, 3, 6),
        _entry(159, 3, 6), _entry(162, 50, 100), _entry(169, 125, 200),
        _entry(171, 40, 60), _entry(173, 10, 20),
        {"keysBySubtype": [195, 192, 191, 194, 193], "minimum": 4, "maximum": 8},
        {"keysBySubtype": [181, 177, 175, 179, 183], "minimum": 50, "maximum": 75},
        _entry(282, 1, 5),
    ],
}


# Compact canonical definition path: events are the audited CreateItemInit
# events; ``minimums`` contains only scored definition values.  All numbers
# are integral in the clean export even though GameMaker stores them as reals.
PROFILE_MODELS: dict[str, dict[str, Any]] = json.loads(r'''{
  "unique:0:0:64":{"events":[{"delta":30,"score":true,"key":29},{"delta":10,"score":true,"key":42},{"delta":50,"score":true,"key":63},{"delta":30,"score":true,"key":101},{"delta":20,"score":true,"key":154},{"delta":1,"score":true,"key":201},{"delta":5,"score":true,"key":250}],"minimums":{"29":110,"42":15,"63":100,"101":20,"154":60,"201":2,"250":15},"poolSlots":{"0":4}},
  "unique:10:0:70":{"events":[{"delta":1,"score":true,"key":201}],"minimums":{"201":1},"poolSlots":{"0":4,"1":4,"2":4}},
  "unique:1:0:81":{"events":[{"delta":100,"score":true,"key":29},{"delta":40,"score":true,"key":154},{"delta":4,"score":true,"key":161},{"delta":10,"score":true,"key":173},{"delta":1,"score":true,"key":201}],"minimums":{"29":350,"154":280,"161":8,"173":30,"201":2},"poolSlots":{"0":4,"1":4,"2":4}},
  "unique:1:0:98":{"events":[{"delta":130,"score":true,"key":29},{"delta":30,"score":true,"key":154}],"minimums":{"29":270,"154":110},"poolSlots":{"0":2,"1":2}},
  "unique:2:0:60":{"events":[{"delta":30,"score":true,"key":29},{"delta":12,"score":true,"key":154},{"delta":1,"score":true,"key":201}],"minimums":{"29":120,"154":52,"201":1},"poolSlots":{"0":4,"1":4,"2":4}},
  "unique:2:0:62":{"events":[{"delta":25,"score":true,"key":25},{"delta":45,"score":true,"key":29},{"delta":50,"score":true,"key":96},{"delta":10,"score":true,"key":97},{"delta":18,"score":true,"key":154},{"delta":1,"score":true,"key":201}],"minimums":{"25":75,"29":180,"96":60,"97":25,"154":72,"201":3},"poolSlots":{"0":4,"1":4}},
  "unique:3:11:8":{"events":[{"delta":5,"score":true,"key":22},{"delta":15,"score":true,"key":152},{"delta":10,"score":true,"key":196},{"delta":1,"score":true,"key":201},{"delta":10,"score":true,"key":250},{"delta":35,"score":true,"key":258}],"minimums":{"22":30,"152":10,"196":30,"201":3,"250":20,"258":15},"poolSlots":{"0":2,"1":2,"2":2}},
  "unique:3:13:18":{"events":[{"delta":10,"score":true,"key":22},{"delta":50,"score":true,"key":28},{"delta":1,"score":true,"key":70},{"delta":50,"score":true,"key":73},{"delta":5,"score":true,"key":437}],"minimums":{"22":130,"28":900,"70":3,"73":150,"437":10},"poolSlots":{"0":1}},
  "unique:3:1:31":{"events":[{"delta":9,"score":true,"key":22},{"delta":85,"score":true,"key":28},{"delta":15,"score":true,"key":51},{"delta":5,"score":true,"key":57},{"delta":15,"score":true,"key":66},{"delta":2,"score":true,"key":114}],"minimums":{"22":73,"28":890,"51":25,"57":10,"66":25,"114":1},"poolSlots":{"0":1,"1":1}},
  "unique:3:3:17":{"events":[{"delta":10,"score":true,"key":22},{"delta":115,"score":true,"key":28},{"delta":100,"score":true,"key":33},{"delta":50,"score":true,"key":48},{"delta":40,"score":true,"key":79},{"delta":7,"score":true,"key":114},{"delta":7,"score":true,"key":115},{"delta":2,"score":true,"key":201}],"minimums":{"22":165,"28":850,"33":150,"48":75,"79":40,"114":3,"115":6,"201":3},"poolSlots":{"0":1,"1":1}},
  "unique:3:4:10":{"events":[{"delta":8,"score":true,"key":22},{"delta":140,"score":true,"key":28},{"delta":15,"score":true,"key":67},{"delta":65,"score":true,"key":68},{"delta":2,"score":true,"key":118},{"delta":20,"score":true,"key":123},{"delta":7,"score":true,"key":124},{"delta":5,"score":true,"key":222}],"minimums":{"22":82,"28":780,"67":25,"68":35,"118":1,"123":40,"124":8,"222":5},"poolSlots":{"0":1}},
  "unique:3:4:9":{"events":[{"delta":7,"score":true,"key":22},{"delta":250,"score":true,"key":28},{"delta":25,"score":true,"key":48},{"delta":6,"score":true,"key":64},{"delta":50,"score":true,"key":96},{"delta":10,"score":true,"key":173},{"delta":3,"score":true,"key":201},{"delta":3,"score":true,"key":316}],"minimums":{"22":158,"28":925,"48":40,"64":7,"96":100,"173":30,"201":5,"316":12},"poolSlots":{"0":1,"1":1,"2":1}},
  "unique:4:0:49":{"events":[{"delta":15,"score":true,"key":28},{"delta":15,"score":true,"key":51},{"delta":6,"score":true,"key":65},{"delta":10,"score":true,"key":68},{"delta":2,"score":true,"key":203},{"delta":4,"score":true,"key":274}],"minimums":{"28":35,"51":15,"65":6,"68":15,"203":1,"274":4},"poolSlots":{"0":4}},
  "unique:4:0:57":{"events":[{"delta":10,"score":true,"key":39},{"delta":10,"score":true,"key":61},{"delta":15,"score":true,"key":101},{"delta":12,"score":true,"key":154},{"delta":1,"score":false,"key":221,"phase":"placeholder_range_roll"},{"advance":1,"key":221,"phase":"generated_target_key_222_227"},{"delta":1,"score":true,"key":221,"phase":"generated_stat_value"}],"minimums":{"39":45,"61":30,"101":5,"154":26,"221":2},"target221":{"minimum":222,"maximum":227},"poolSlots":{"0":4}},
  "unique:4:0:63":{"events":[{"delta":15,"score":true,"key":28},{"delta":15,"score":true,"key":51},{"delta":6,"score":true,"key":65},{"delta":60,"score":true,"key":71},{"delta":10,"score":true,"key":198},{"delta":2,"score":true,"key":201},{"delta":4,"score":true,"key":274}],"minimums":{"28":35,"51":15,"65":6,"71":-150,"198":25,"201":3,"274":4},"poolSlots":{"0":4,"1":4}},
  "unique:5:0:60":{"events":[{"delta":20,"score":true,"key":51},{"delta":2,"score":true,"key":201}],"minimums":{"51":20,"201":1},"poolSlots":{"0":4,"1":4,"2":4}},
  "unique:5:0:7":{"events":[{"delta":15,"score":true,"key":101}],"minimums":{"101":10},"poolSlots":{"0":4}},
  "unique:5:0:71":{"events":[{"delta":25,"score":true,"key":25},{"delta":1,"score":true,"key":337},{"delta":20,"score":true,"key":338}],"minimums":{"25":15,"337":1,"338":10},"poolSlots":{"0":4}},
  "unique:6:0:54":{"events":[{"delta":20,"score":true,"key":198},{"delta":1,"score":true,"key":201},{"delta":2,"score":true,"key":335},{"delta":25,"score":true,"key":336},{"delta":2,"score":true,"key":337}],"minimums":{"198":15,"201":2,"335":3,"336":25,"337":1},"poolSlots":{"0":4,"1":4}},
  "unique:7:0:51":{"events":[],"minimums":{},"poolSlots":{"0":4,"1":4,"2":4}},
  "unique:8:0:42":{"events":[{"delta":30,"score":true,"key":29},{"delta":15,"score":true,"key":48},{"delta":16,"score":true,"key":154}],"minimums":{"29":45,"48":15,"154":52},"poolSlots":{"0":4,"1":4,"2":4}}
}''')


SOCKET_TAILS: dict[str, dict[str, Any]] = {
    "unique:0:0:64": {"kind": "range_assignment", "statKey": 20, "minimum": 1, "maximum": 4, "delta": 3},
    "unique:10:0:70": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:1:0:81": {"kind": "fixed_no_draw", "statKey": 20, "value": 5},
    "unique:1:0:98": {"kind": "range_assignment", "statKey": 20, "minimum": 4, "maximum": 6, "delta": 2},
    "unique:2:0:60": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:2:0:62": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:3:11:8": {"kind": "range_assignment", "statKey": 20, "minimum": 4, "maximum": 5, "delta": 1},
    "unique:3:13:18": {"kind": "fixed_no_draw", "statKey": 20, "value": 4, "resolvedReference": True},
    "unique:3:1:31": {"kind": "range_assignment", "statKey": 20, "minimum": 3, "maximum": 5, "delta": 2},
    "unique:3:3:17": {"kind": "range_assignment", "statKey": 20, "minimum": 3, "maximum": 6, "delta": 3},
    "unique:3:4:10": {"kind": "fixed_no_draw", "statKey": 20, "value": 4},
    "unique:3:4:9": {"kind": "range_assignment", "statKey": 20, "minimum": 4, "maximum": 6, "delta": 2},
    "unique:4:0:49": {"kind": "fixed_no_draw", "statKey": 20, "value": 1},
    "unique:4:0:57": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:4:0:63": {"kind": "range_assignment", "statKey": 20, "minimum": 1, "maximum": 3, "delta": 2},
    "unique:5:0:60": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:5:0:7": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:5:0:71": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:6:0:54": {"kind": "range_assignment", "statKey": 20, "minimum": 3, "maximum": 4, "delta": 1},
    "unique:7:0:51": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "unique:8:0:42": {"kind": "missing_hidden_advance", "upperInclusive": 1},
}

SPECIAL_TAILS: dict[str, list[dict[str, Any]]] = {
    "unique:1:0:98": [{
        "kind": "variable_damage_type",
        "candidateStatKeys": [85, 86, 83, 84, 82],
        "minimum": 20,
        "maximum": 35,
    }],
    "unique:7:0:51": [{
        "kind": "fixed_damage_type",
        "candidateStatKeys": [128, 141, 137, 133, 145, 149],
        "fixedValue": 33,
    }, {
        "kind": "fixed_random_stat_identity",
        "candidateStatKeys": [
            26, 69, 70, 87, 101, 110, 111, 161, 171, 173, 197, 198,
            228, 234, 235, 241, 250, 336, 338, 340, 342,
        ],
    }],
}

OVERLOADED_PROFILE_ID = "unique:10:0:89"
OVERLOADED_VALID_SUBSKILL_IDS = (
    7, 10, 11, 12, 13, 14, 16, 17, 18, 20, 22, 23, 29, 30, 32, 33,
    34, 35, 36, 37, 38, 39, 40, 41, 44, 46, 47, 48, 50, 52, 53, 55,
    56, 58, 59, 60, 61, 63, 65, 66, 68, 71, 73, 74, 75, 76, 79, 82,
    83, 84, 85, 87, 89, 90, 92, 93, 95, 100, 101, 103, 108, 109, 110,
    112, 113, 114, 116, 117, 118, 120, 122, 125, 127, 128, 129, 131,
    132, 135, 137, 139, 140, 143, 145, 146, 148, 149, 150, 152, 155,
    158, 161, 162, 164, 165, 166, 167, 172, 173, 175, 176, 177, 180,
    182, 185, 186, 187, 190, 191, 192, 198, 200, 201, 204, 207, 208,
    209, 215, 218, 219, 220, 221, 222, 224, 226, 227, 229, 230, 232,
    235, 236, 237, 239, 240, 242, 244, 245, 246, 250, 253, 254, 255,
    257, 259, 261, 263, 264, 265, 269, 271, 272, 276, 281, 282, 283,
    287, 288, 289, 290, 292, 293, 296, 299, 301, 304, 305, 308, 315,
    317, 318, 325, 326, 327, 329, 332, 334, 336, 337, 340, 341, 344,
    346, 348, 352, 355, 356, 358, 361, 362, 363, 371, 372, 375, 376,
    377, 379, 380, 383, 386, 387, 390, 396, 398, 400, 402, 406, 407,
    409, 412, 415, 416, 417, 418, 420, 421, 422, 424, 425, 426, 428,
    429, 430, 433,
)

if set(SOCKET_TAILS) != set(PROFILE_MODELS):
    raise RuntimeError("generated-pool socket-tail coverage mismatch")
for _profile_id, _socket_tail in SOCKET_TAILS.items():
    PROFILE_MODELS[_profile_id]["socketTail"] = copy.deepcopy(_socket_tail)
    PROFILE_MODELS[_profile_id]["specialTail"] = copy.deepcopy(
        SPECIAL_TAILS.get(_profile_id, [])
    )
    PROFILE_MODELS[_profile_id]["subskills"] = None
    PROFILE_MODELS[_profile_id]["perfectRollAction"] = "solve_runtime_objective"

PROFILE_MODELS[OVERLOADED_PROFILE_ID] = {
    "events": [{
        "delta": 431,
        "score": False,
        "key": 419,
        "phase": "definition_range",
    }],
    "minimums": {},
    "poolSlots": {},
    "socketTail": {"kind": "missing_hidden_advance", "upperInclusive": 1},
    "specialTail": [],
    "subskills": {
        "kind": "rejection_loop",
        "statKey": 419,
        "upperInclusive": 431,
        "candidateOffset": 2,
        "validCandidateIds": list(OVERLOADED_VALID_SUBSKILL_IDS),
    },
    "perfectRollAction": "preserve_existing_a",
}

ACTIONABLE_PROFILE_IDS = frozenset(
    profile_id for profile_id, model in PROFILE_MODELS.items()
    if model["perfectRollAction"] == "solve_runtime_objective"
)
IDENTITY_ONLY_PROFILE_IDS = frozenset(
    profile_id for profile_id, model in PROFILE_MODELS.items()
    if model["perfectRollAction"] == "preserve_existing_a"
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def _bundle_payload() -> dict[str, Any]:
    return {
        "exeSha256": EXPECTED_EXE_SHA256,
        "kind": MODEL_KIND,
        "sourceArtifactSha256": SOURCE_ARTIFACT_SHA256,
        "poolTables": POOL_TABLES,
        "profiles": PROFILE_MODELS,
    }


def computed_bundle_sha256() -> str:
    return hashlib.sha256(_canonical_json(_bundle_payload())).hexdigest().upper()


def _candidate_keys(configured_pool: int) -> set[int]:
    groups = (1, 2, 3) if configured_pool == 4 else (configured_pool,)
    keys: set[int] = set()
    for group in groups:
        for entry in POOL_TABLES[group]:
            if "keysBySubtype" in entry:
                keys.update(int(key) for key in entry["keysBySubtype"])
            else:
                keys.add(int(entry["key"]))
    return keys


def _maximum_distinct(
    constant_keys: set[int], dynamic_sets: list[set[int]]
) -> int:
    candidates = [keys - constant_keys for keys in dynamic_sets]
    matched: dict[int, int] = {}

    def augment(source: int, seen: set[int]) -> bool:
        for key in sorted(candidates[source]):
            if key in seen:
                continue
            seen.add(key)
            prior = matched.get(key)
            if prior is None or augment(prior, seen):
                matched[key] = source
                return True
        return False

    return len(constant_keys) + sum(
        augment(source, set()) for source in range(len(candidates))
    )


def theoretical_max_visible(profile_id: str) -> int:
    model = PROFILE_MODELS.get(profile_id)
    if model is None:
        raise ValueError(f"unsupported generated-pool profile {profile_id!r}")
    constant: set[int] = set()
    dynamic: list[set[int]] = []
    for event in model["events"]:
        if bool(event.get("score", True)) and "delta" in event:
            key = int(event["key"])
            if key == 221:
                target = model["target221"]
                dynamic.append(set(range(int(target["minimum"]), int(target["maximum"]) + 1)))
            else:
                constant.add(key)
    for pool in model["poolSlots"].values():
        dynamic.append(_candidate_keys(int(pool)))
    if model["socketTail"]["kind"] == "range_assignment":
        constant.add(20)
    for special in model["specialTail"]:
        if special["kind"] == "variable_damage_type":
            dynamic.append(set(map(int, special["candidateStatKeys"])))
    return _maximum_distinct(constant, dynamic)


def profile_model_sha256(profile_id: str) -> str:
    model = PROFILE_MODELS.get(profile_id)
    if model is None:
        raise ValueError(f"unsupported generated-pool profile {profile_id!r}")
    return hashlib.sha256(_canonical_json({
        "bundleSha256": MODEL_BUNDLE_SHA256,
        "profileId": profile_id,
        "profile": model,
    })).hexdigest().upper()


def metadata(profile_id: str) -> dict[str, Any]:
    model = PROFILE_MODELS.get(profile_id)
    if model is None:
        raise ValueError(f"unsupported generated-pool profile {profile_id!r}")
    return {
        "kind": MODEL_KIND,
        "profileId": profile_id,
        "poolSlots": copy.deepcopy(model["poolSlots"]),
        "perfectRollAction": model["perfectRollAction"],
        "theoreticalMaxVisible": theoretical_max_visible(profile_id),
        "modelSha256": profile_model_sha256(profile_id),
    }


def validate_metadata(profile_id: str, declared: Any) -> dict[str, Any]:
    expected = metadata(profile_id)
    if not isinstance(declared, dict) or declared != expected:
        raise ValueError(f"{profile_id}: generated-pool model metadata mismatch")
    return copy.deepcopy(expected)


def _next(state: int | float) -> int:
    return int(math.fmod(
        _CPR_MULTIPLIER * float(state) + _CPR_INCREMENT,
        _CPR_MODULUS,
    )) & _CPR_MASK


def _draw(state: int, upper: int) -> tuple[int, int]:
    state = _next(state)
    roll = math.floor((float(upper) + 0.99999) * (float(state) / _CPR_MAX))
    return state, roll


def _pool_entry(group: int, selector: int, subtype: int) -> dict[str, int]:
    raw = POOL_TABLES[group][selector]
    key = (
        int(raw["keysBySubtype"][subtype - 1])
        if "keysBySubtype" in raw
        else int(raw["key"])
    )
    return {
        "key": key,
        "minimum": int(raw["minimum"]),
        "maximum": int(raw["maximum"]),
    }


def replay(profile_id: str, seed: int) -> dict[str, Any]:
    """Replay one canonical profile; return its complete auditable path."""
    if isinstance(seed, bool) or not isinstance(seed, int) or not 1 <= seed <= 1_000_000_000:
        raise ValueError("generated-pool seed must be in 1..1000000000")
    model = PROFILE_MODELS.get(profile_id)
    if model is None:
        raise ValueError(f"unsupported generated-pool profile {profile_id!r}")
    state = seed
    calls: list[dict[str, Any]] = []
    assignments: dict[int, dict[str, Any]] = {}
    identity_results: list[dict[str, Any]] = []
    pending_target: int | None = None

    def draw(upper: int, phase: str, **extra: Any) -> tuple[int, int]:
        nonlocal state
        state, roll = _draw(state, upper)
        call = {
            "index": len(calls), "phase": phase, "upperInclusive": upper,
            "roll": roll, "state": state, "scored": False,
        }
        call.update(extra)
        calls.append(call)
        return len(calls) - 1, roll

    for event in model["events"]:
        key = int(event["key"])
        phase = str(event.get("phase", "definition_range"))
        if "advance" in event:
            for _ in range(int(event["advance"])):
                if key == 221 and phase == "generated_target_key_222_227":
                    target = model["target221"]
                    _, roll = draw(
                        int(target["maximum"]) - int(target["minimum"]),
                        "definition.221.target_identity", statKey=221,
                    )
                    pending_target = int(target["minimum"]) + roll
                else:
                    state = _next(state)
                    calls.append({
                        "index": len(calls), "phase": f"definition.{phase}",
                        "upperInclusive": None, "roll": None, "state": state,
                        "scored": False, "statKey": key,
                    })
            continue
        delta = int(event["delta"])
        call_index, roll = draw(delta, f"definition.{phase}", statKey=key)
        if not bool(event.get("score", True)):
            continue
        if key == 221:
            if pending_target is None:
                raise ValueError("key-221 value has no target identity")
            target_key = pending_target
            calls[call_index]["resolvedStatKey"] = target_key
        else:
            target_key = key
        minimum = int(model["minimums"][str(key)])
        assignments[target_key] = {
            "statKey": target_key, "source": "definition", "callIndex": call_index,
            "minimum": minimum, "maximum": minimum + delta,
            "value": minimum + roll, "delta": delta, "roll": roll,
        }

    slots = {int(slot): int(pool) for slot, pool in model["poolSlots"].items()}
    for slot in range(4):
        _, group_roll = draw(2, "generated.outer_group", slot=slot)
        _, subtype_roll = draw(4, "generated.outer_subtype", slot=slot)
        configured_pool = slots.get(slot, 0)
        if configured_pool == 0:
            continue
        group = group_roll + 1 if configured_pool == 4 else configured_pool
        subtype = subtype_roll + 1
        _, selector = draw(
            len(POOL_TABLES[group]) - 1,
            "generated.helper_stat_identity", slot=slot,
            configuredPool=configured_pool, effectivePool=group, subtype=subtype,
        )
        selected = _pool_entry(group, selector, subtype)
        delta = selected["maximum"] - selected["minimum"]
        call_index, roll = draw(
            delta, "generated.helper_numeric_value", slot=slot,
            configuredPool=configured_pool, effectivePool=group,
            subtype=subtype, selector=selector, statKey=selected["key"],
            minimum=selected["minimum"], maximum=selected["maximum"],
        )
        assignments[selected["key"]] = {
            "statKey": selected["key"], "source": f"generated.slot{slot}",
            "callIndex": call_index, "minimum": selected["minimum"],
            "maximum": selected["maximum"],
            "value": selected["minimum"] + roll, "delta": delta, "roll": roll,
        }

    subskills = model["subskills"]
    if subskills is not None:
        if subskills.get("kind") != "rejection_loop":
            raise ValueError(f"unsupported subskill model {subskills!r}")
        valid_candidates = set(map(int, subskills["validCandidateIds"]))
        upper = int(subskills["upperInclusive"])
        offset = int(subskills["candidateOffset"])
        visited_states: set[int] = set()
        attempt = 0
        while True:
            if state in visited_states:
                raise ValueError("subskill rejection loop entered a state cycle")
            visited_states.add(state)
            call_index, roll = draw(
                upper,
                "subskills.overloaded.identity",
                statKey=int(subskills["statKey"]),
                attempt=attempt,
            )
            candidate = roll + offset
            accepted = candidate in valid_candidates
            calls[call_index]["candidateSubskillId"] = candidate
            calls[call_index]["accepted"] = accepted
            attempt += 1
            if accepted:
                identity_results.append({
                    "statKey": int(subskills["statKey"]),
                    "source": "subskills.overloaded",
                    "callIndex": call_index,
                    "selectedIdentity": candidate,
                    "attempts": attempt,
                })
                break

    socket_tail = model["socketTail"]
    if socket_tail["kind"] == "missing_hidden_advance":
        draw(
            int(socket_tail["upperInclusive"]),
            "late.socket_missing_hidden_advance", statKey=20,
        )
    elif socket_tail["kind"] == "range_assignment":
        delta = int(socket_tail["delta"])
        call_index, roll = draw(
            delta, "late.socket_range_value", statKey=20,
            minimum=int(socket_tail["minimum"]),
            maximum=int(socket_tail["maximum"]),
        )
        assignments[20] = {
            "statKey": 20, "source": "late.socket", "callIndex": call_index,
            "minimum": int(socket_tail["minimum"]),
            "maximum": int(socket_tail["maximum"]),
            "value": int(socket_tail["minimum"]) + roll,
            "delta": delta, "roll": roll,
        }
    elif socket_tail["kind"] != "fixed_no_draw":
        raise ValueError(f"unsupported socket tail {socket_tail!r}")

    for special in model["specialTail"]:
        candidates = tuple(map(int, special["candidateStatKeys"]))
        phase = (
            "special.damage_type.identity"
            if "damage_type" in special["kind"]
            else "special.random_stat.identity"
        )
        selector_call, selector = draw(
            len(candidates) - 1,
            phase,
            candidateStatKeys=list(candidates),
        )
        selected_key = candidates[selector]
        calls[selector_call]["resolvedStatKey"] = selected_key
        if special["kind"] == "variable_damage_type":
            minimum = int(special["minimum"])
            maximum = int(special["maximum"])
            delta = maximum - minimum
            call_index, roll = draw(
                delta,
                "special.damage_type.value",
                statKey=selected_key,
                minimum=minimum,
                maximum=maximum,
            )
            assignments[selected_key] = {
                "statKey": selected_key,
                "source": "special.damage_type",
                "callIndex": call_index,
                "minimum": minimum,
                "maximum": maximum,
                "value": minimum + roll,
                "delta": delta,
                "roll": roll,
            }
        elif special["kind"] in {
            "fixed_damage_type", "fixed_random_stat_identity",
        }:
            # The fixed AddStat remains visible in-game, but it is not a
            # numeric roll objective.  It hides an earlier variable write to
            # the same key under SetItemStat's last-write-wins rule.
            assignments.pop(selected_key, None)
            identity_results.append({
                "statKey": selected_key,
                "source": special["kind"],
                "callIndex": selector_call,
                "selectedIdentity": selected_key,
                **(
                    {"fixedValue": int(special["fixedValue"])}
                    if "fixedValue" in special else {}
                ),
            })
        else:
            raise ValueError(f"unsupported special-tail model {special!r}")

    visible = sorted(assignments.values(), key=lambda row: (row["callIndex"], row["statKey"]))
    for assignment in visible:
        calls[assignment["callIndex"]]["scored"] = True
        calls[assignment["callIndex"]]["visibleAtEnd"] = True
    hits = sum(row["roll"] == row["delta"] for row in visible)
    deficit = sum(row["delta"] - row["roll"] for row in visible)
    return {
        "profileId": profile_id,
        "seed": seed,
        "finalState": state,
        "eventPath": calls,
        "visibleAssignments": visible,
        "identityResults": identity_results,
        "maxed": int(hits),
        "total": len(visible),
        "endpointDeficit": int(deficit),
        "theoreticalMaxVisible": theoretical_max_visible(profile_id),
    }


def _validate_bundle() -> None:
    if len(PROFILE_MODELS) != 22:
        raise RuntimeError("late unique model must contain exactly 22 profiles")
    if sum(len(model["poolSlots"]) for model in PROFILE_MODELS.values()) != 43:
        raise RuntimeError("generated-pool model must contain exactly 43 active slots")
    actions = {
        action: sum(
            model["perfectRollAction"] == action
            for model in PROFILE_MODELS.values()
        )
        for action in {model["perfectRollAction"] for model in PROFILE_MODELS.values()}
    }
    if actions != {"solve_runtime_objective": 21, "preserve_existing_a": 1}:
        raise RuntimeError("late unique action counts mismatch")
    tail_kinds: dict[str, int] = {}
    for model in PROFILE_MODELS.values():
        kind = str(model["socketTail"]["kind"])
        tail_kinds[kind] = tail_kinds.get(kind, 0) + 1
    if tail_kinds != {
        "range_assignment": 8,
        "missing_hidden_advance": 10,
        "fixed_no_draw": 4,
    }:
        raise RuntimeError("generated-pool socket-tail kind counts mismatch")
    if len(OVERLOADED_VALID_SUBSKILL_IDS) != 222:
        raise RuntimeError("Overloaded Dice valid identity set mismatch")
    if sum(bool(model["specialTail"]) for model in PROFILE_MODELS.values()) != 2:
        raise RuntimeError("special-tail profile count mismatch")
    if computed_bundle_sha256() != MODEL_BUNDLE_SHA256:
        raise RuntimeError("generated-pool model bundle hash mismatch")


_validate_bundle()
