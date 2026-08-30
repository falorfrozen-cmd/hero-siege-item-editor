"""Fail-closed target-skill database for Loaded Dice and Overloaded Dice.

Both charms serialize only a CPR seed in save field ``a``.  The selected skill
is reconstructed by the game while the item definition is loaded; it is not an
independent value that can safely be written into the save.  This module loads
the offline-generated seed table, proves every table entry again with an
independent binary64 replay, and exposes no target when any invariant fails.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

try:
    from . import generated_pool_model
except ImportError:  # Standalone editor and direct-module unit tests.
    try:
        import generated_pool_model  # type: ignore
    except ImportError:
        from HSItemEditor import generated_pool_model  # type: ignore


SCHEMA = "hero-siege-dice-skill-targets-v1"
EXPECTED_EXE_SHA256 = (
    "438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4"
)
# Filled from the deterministic generated artifact.  A mismatched or edited
# table is rejected in full rather than partially exposing unverified seeds.
EXPECTED_ASSET_SHA256 = (
    "4A7688CD988123E5DC01225DD7E67A961DA7E425D3690E986C5FB2A172868F70"
)
DEFAULT_DATABASE_PATH = Path(__file__).with_name("hs_dice_skill_targets.json")

SEED_START = 1
SEED_STOP = 1_000_000_000
LOADED_PROFILE_ID = "unique:10:0:31"
OVERLOADED_PROFILE_ID = "unique:10:0:89"
PROFILE_ADDRESSES = {
    LOADED_PROFILE_ID: ("unique", 10, 0, 31),
    OVERLOADED_PROFILE_ID: ("unique", 10, 0, 89),
}
ADDRESS_PROFILES = {address: profile_id for profile_id, address in PROFILE_ADDRESSES.items()}

_CPR_MULTIPLIER = 1_789_570_533.0
_CPR_INCREMENT = 465_707.0
_CPR_MODULUS = 2_147_483_648.0
_CPR_MASK = 1_073_741_823
_CPR_MAX = 1_073_741_823.0


class DiceSkillValidationError(ValueError):
    """Raised when the generated target table does not satisfy its contract."""


@dataclass(frozen=True)
class DiceSkillStatus:
    available: bool
    code: str
    message: str
    path: Path
    skill_count: int = 0
    loaded_target_count: int = 0
    overloaded_target_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "code": self.code,
            "message": self.message,
            "path": str(self.path),
            "skillCount": self.skill_count,
            "loadedTargetCount": self.loaded_target_count,
            "overloadedTargetCount": self.overloaded_target_count,
        }


def _next_state(state: int | float) -> int:
    return int(math.fmod(
        _CPR_MULTIPLIER * float(state) + _CPR_INCREMENT,
        _CPR_MODULUS,
    )) & _CPR_MASK


def _draw(state: int, upper_inclusive: int) -> tuple[int, int]:
    state = _next_state(state)
    roll = math.floor(
        (float(upper_inclusive) + 0.99999) * (float(state) / _CPR_MAX)
    )
    return state, roll


def _seed_integer(value: Any, label: str = "seed") -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiceSkillValidationError(f"{label} must be an integer save seed")
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise DiceSkillValidationError(f"{label} must be an integer save seed")
    seed = int(numeric)
    if not SEED_START <= seed <= SEED_STOP:
        raise DiceSkillValidationError(
            f"{label} must be inside {SEED_START}..{SEED_STOP}"
        )
    return seed


def loaded_selected_skill_id(seed: int | float) -> int:
    """Replay Loaded Dice's first definition draw (stat 202, range 2..433)."""

    seed_int = _seed_integer(seed)
    _state, roll = _draw(seed_int, 431)
    return roll + 2


def overloaded_selected_skill_id(seed: int | float) -> int:
    """Replay Overloaded Dice through the native rejection-loop result."""

    seed_int = _seed_integer(seed)
    result = generated_pool_model.replay(OVERLOADED_PROFILE_ID, seed_int)
    identities = result.get("identityResults")
    if not isinstance(identities, list) or len(identities) != 1:
        raise DiceSkillValidationError("Overloaded Dice replay has no unique identity")
    selected = identities[0].get("selectedIdentity")
    if isinstance(selected, bool) or not isinstance(selected, int):
        raise DiceSkillValidationError("Overloaded Dice replay returned an invalid identity")
    return selected


def selected_skill_id(profile_id: str, seed: int | float) -> int:
    if profile_id == LOADED_PROFILE_ID:
        return loaded_selected_skill_id(seed)
    if profile_id == OVERLOADED_PROFILE_ID:
        return overloaded_selected_skill_id(seed)
    raise DiceSkillValidationError(f"unsupported dice profile: {profile_id!r}")


def profile_id_for_address(kind: str, cls: int, sub: int, base: int) -> str | None:
    try:
        address = (str(kind), int(cls), int(sub), int(base))
    except (TypeError, ValueError):
        return None
    return ADDRESS_PROFILES.get(address)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise DiceSkillValidationError(
            f"{label} keys mismatch (missing={missing}, extra={extra})"
        )


def _validate_skill_rows(raw: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) != 432:
        raise DiceSkillValidationError("skills must contain all 432 IDs from 2 through 433")
    rows: dict[int, dict[str, Any]] = {}
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            raise DiceSkillValidationError(f"skills[{index}] must be an object")
        _require_exact_keys(
            value,
            {"id", "key", "name", "classId", "className", "hasSubskills"},
            f"skills[{index}]",
        )
        skill_id = value["id"]
        class_id = value["classId"]
        if isinstance(skill_id, bool) or not isinstance(skill_id, int):
            raise DiceSkillValidationError(f"skills[{index}].id must be an integer")
        if isinstance(class_id, bool) or not isinstance(class_id, int) or not 1 <= class_id <= 24:
            raise DiceSkillValidationError(f"skills[{index}].classId is invalid")
        if skill_id in rows:
            raise DiceSkillValidationError(f"duplicate skill ID {skill_id}")
        if not isinstance(value["hasSubskills"], bool):
            raise DiceSkillValidationError(f"skills[{index}].hasSubskills must be boolean")
        for key in ("key", "name", "className"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise DiceSkillValidationError(f"skills[{index}].{key} must be non-empty")
        rows[skill_id] = copy.deepcopy(value)
    if set(rows) != set(range(2, 434)):
        raise DiceSkillValidationError("skill ID coverage must be exactly 2..433")
    return rows


def _validate_seed_map(
    raw: Any,
    expected_ids: set[int],
    profile_id: str,
) -> dict[int, int]:
    if not isinstance(raw, dict):
        raise DiceSkillValidationError(f"{profile_id} targets must be an object")
    try:
        ids = {int(key) for key in raw}
    except (TypeError, ValueError) as exc:
        raise DiceSkillValidationError(f"{profile_id} target keys must be integer strings") from exc
    if ids != expected_ids or len(raw) != len(expected_ids):
        raise DiceSkillValidationError(f"{profile_id} target coverage mismatch")
    seeds: dict[int, int] = {}
    for key, value in raw.items():
        skill_id = int(key)
        seed = _seed_integer(value, f"{profile_id}.targets[{skill_id}]")
        if selected_skill_id(profile_id, seed) != skill_id:
            raise DiceSkillValidationError(
                f"{profile_id} seed {seed} does not select skill ID {skill_id}"
            )
        seeds[skill_id] = seed
    return seeds


def _prove_smallest_loaded_seeds(expected: Mapping[int, int], searched_through: int) -> None:
    first: dict[int, int] = {}
    for seed in range(SEED_START, searched_through + 1):
        first.setdefault(loaded_selected_skill_id(seed), seed)
    if first != dict(expected):
        raise DiceSkillValidationError("Loaded Dice smallest-seed proof does not replay")


def _prove_smallest_overloaded_seeds(expected: Mapping[int, int], searched_through: int) -> None:
    first: dict[int, int] = {}
    for seed in range(SEED_START, searched_through + 1):
        first.setdefault(overloaded_selected_skill_id(seed), seed)
    if first != dict(expected):
        raise DiceSkillValidationError("Overloaded Dice smallest-seed proof does not replay")


def validate_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise DiceSkillValidationError("target database root must be an object")
    _require_exact_keys(
        document,
        {"schema", "catalogProfile", "exeSha256", "seedDomain", "rng", "sources", "skills", "profiles"},
        "root",
    )
    if document["schema"] != SCHEMA:
        raise DiceSkillValidationError(f"unsupported schema: {document['schema']!r}")
    if document["catalogProfile"] != "Season 10 clean-438B":
        raise DiceSkillValidationError("catalog profile mismatch")
    if document["exeSha256"] != EXPECTED_EXE_SHA256:
        raise DiceSkillValidationError("clean executable fingerprint mismatch")
    if document["seedDomain"] != {"start": SEED_START, "stop": SEED_STOP}:
        raise DiceSkillValidationError("save-seed domain mismatch")
    if not isinstance(document["rng"], dict) or not isinstance(document["sources"], dict):
        raise DiceSkillValidationError("rng and sources metadata must be objects")

    skills = _validate_skill_rows(document["skills"])
    profiles = document["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != {
        LOADED_PROFILE_ID, OVERLOADED_PROFILE_ID,
    }:
        raise DiceSkillValidationError("dice profile coverage mismatch")

    expected_by_profile = {
        LOADED_PROFILE_ID: set(range(2, 434)),
        OVERLOADED_PROFILE_ID: set(map(int, generated_pool_model.OVERLOADED_VALID_SUBSKILL_IDS)),
    }
    seed_maps: dict[str, dict[int, int]] = {}
    for profile_id, expected_ids in expected_by_profile.items():
        profile = profiles[profile_id]
        if not isinstance(profile, dict):
            raise DiceSkillValidationError(f"{profile_id} must be an object")
        _require_exact_keys(
            profile,
            {"name", "address", "statKey", "targetKind", "algorithm", "searchedThrough", "targets"},
            profile_id,
        )
        if profile["address"] != {
            "kind": PROFILE_ADDRESSES[profile_id][0],
            "cls": PROFILE_ADDRESSES[profile_id][1],
            "sub": PROFILE_ADDRESSES[profile_id][2],
            "b": PROFILE_ADDRESSES[profile_id][3],
        }:
            raise DiceSkillValidationError(f"{profile_id} address mismatch")
        searched_through = profile["searchedThrough"]
        if (
            isinstance(searched_through, bool)
            or not isinstance(searched_through, int)
            or not SEED_START <= searched_through <= SEED_STOP
        ):
            raise DiceSkillValidationError(f"{profile_id}.searchedThrough is invalid")
        seeds = _validate_seed_map(profile["targets"], expected_ids, profile_id)
        if max(seeds.values()) != searched_through:
            raise DiceSkillValidationError(
                f"{profile_id}.searchedThrough must equal the last first-hit seed"
            )
        seed_maps[profile_id] = seeds

    has_subskills = {skill_id for skill_id, row in skills.items() if row["hasSubskills"]}
    if has_subskills != expected_by_profile[OVERLOADED_PROFILE_ID]:
        raise DiceSkillValidationError("skill hasSubskills flags disagree with native predicate")

    _prove_smallest_loaded_seeds(
        seed_maps[LOADED_PROFILE_ID],
        profiles[LOADED_PROFILE_ID]["searchedThrough"],
    )
    _prove_smallest_overloaded_seeds(
        seed_maps[OVERLOADED_PROFILE_ID],
        profiles[OVERLOADED_PROFILE_ID]["searchedThrough"],
    )
    return copy.deepcopy(document)


class DiceSkillDatabase:
    """Validated immutable target table, or a completely unavailable database."""

    def __init__(
        self,
        path: Path,
        status: DiceSkillStatus,
        document: Mapping[str, Any] | None = None,
        runtime_build_check: Callable[[], str | None] | None = None,
    ) -> None:
        self.path = path
        self.status = status
        self._document = copy.deepcopy(dict(document or {}))
        self._skills = {
            int(row["id"]): copy.deepcopy(row)
            for row in self._document.get("skills", [])
        }
        self._profiles = copy.deepcopy(dict(self._document.get("profiles", {})))
        self._runtime_build_check = runtime_build_check

    def _runtime_error(self) -> str | None:
        if not self.status.available or self._runtime_build_check is None:
            return None
        try:
            return self._runtime_build_check()
        except Exception as exc:
            return f"Installed Hero Siege build could not be verified: {exc}"

    @property
    def available(self) -> bool:
        return self.status.available and self._runtime_error() is None

    def summary(self) -> dict[str, Any]:
        result = self.status.as_dict()
        runtime_error = self._runtime_error()
        if runtime_error is not None:
            result.update({
                "available": False,
                "code": "game_build_unverified",
                "message": runtime_error,
            })
        if self.status.available and runtime_error is None:
            result.update({
                "schema": self._document["schema"],
                "exeSha256": self._document["exeSha256"],
                "seedDomain": copy.deepcopy(self._document["seedDomain"]),
            })
        return result

    def selector(self, profile_id: str, current_seed: Any = None) -> dict[str, Any] | None:
        if profile_id not in PROFILE_ADDRESSES:
            return None
        profile = self._profiles.get(profile_id)
        runtime_error = self._runtime_error()
        available = self.status.available and runtime_error is None
        result = {
            "profileId": profile_id,
            "name": (
                profile.get("name") if isinstance(profile, dict)
                else ("Loaded Dice" if profile_id == LOADED_PROFILE_ID else "Overloaded Dice")
            ),
            "targetKind": (
                profile.get("targetKind") if isinstance(profile, dict)
                else ("skill" if profile_id == LOADED_PROFILE_ID else "subskill")
            ),
            "available": available,
            "message": runtime_error or self.status.message,
        }
        if available and current_seed is not None:
            try:
                skill_id = selected_skill_id(profile_id, current_seed)
            except DiceSkillValidationError:
                skill_id = None
            skill = self._skills.get(skill_id) if skill_id is not None else None
            if skill is not None:
                result["current"] = copy.deepcopy(skill)
        return result

    def targets(self, profile_id: str) -> list[dict[str, Any]]:
        if not self.status.available or self._runtime_error() is not None or profile_id not in self._profiles:
            return []
        seeds = self._profiles[profile_id]["targets"]
        rows = []
        for skill_id_text, seed in seeds.items():
            skill_id = int(skill_id_text)
            row = copy.deepcopy(self._skills[skill_id])
            row["seed"] = int(seed)
            rows.append(row)
        rows.sort(key=lambda row: (
            str(row["className"]).casefold(), str(row["name"]).casefold(), int(row["id"])
        ))
        return rows

    def target(self, profile_id: str, skill_id: Any) -> dict[str, Any]:
        runtime_error = self._runtime_error()
        if not self.status.available or runtime_error is not None:
            raise DiceSkillValidationError(runtime_error or self.status.message)
        if isinstance(skill_id, bool):
            raise DiceSkillValidationError("skill ID must be an integer")
        try:
            target_id = int(skill_id)
        except (TypeError, ValueError) as exc:
            raise DiceSkillValidationError("skill ID must be an integer") from exc
        if isinstance(skill_id, float) and not skill_id.is_integer():
            raise DiceSkillValidationError("skill ID must be an integer")
        profile = self._profiles.get(profile_id)
        if not isinstance(profile, dict):
            raise DiceSkillValidationError("this item does not support skill targeting")
        raw_seed = profile["targets"].get(str(target_id))
        if raw_seed is None:
            noun = "subskill" if profile_id == OVERLOADED_PROFILE_ID else "skill"
            raise DiceSkillValidationError(
                f"skill ID {target_id} is not a valid {noun} target for this item"
            )
        row = copy.deepcopy(self._skills[target_id])
        row.update({"seed": int(raw_seed), "profileId": profile_id})
        return row


def load_dice_skill_database(
    base: str | Path | None = None,
    *,
    runtime_build_check: Callable[[], str | None] | None = None,
) -> DiceSkillDatabase:
    path = DEFAULT_DATABASE_PATH if base is None else Path(base) / DEFAULT_DATABASE_PATH.name
    if not path.is_file():
        status = DiceSkillStatus(
            False, "missing", f"Dice skill target database is not installed: {path}", path,
        )
        return DiceSkillDatabase(path, status, runtime_build_check=runtime_build_check)
    try:
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest().upper()
        if digest != EXPECTED_ASSET_SHA256:
            raise DiceSkillValidationError(
                f"target database SHA-256 mismatch ({digest})"
            )
        document = validate_document(json.loads(payload.decode("utf-8")))
        loaded_count = len(document["profiles"][LOADED_PROFILE_ID]["targets"])
        overloaded_count = len(document["profiles"][OVERLOADED_PROFILE_ID]["targets"])
        status = DiceSkillStatus(
            True,
            "ready",
            "Verified dice skill targets ready",
            path,
            len(document["skills"]),
            loaded_count,
            overloaded_count,
        )
        return DiceSkillDatabase(
            path, status, document, runtime_build_check=runtime_build_check
        )
    except Exception as exc:
        status = DiceSkillStatus(
            False, "invalid", f"Dice skill targets disabled: {exc}", path,
        )
        return DiceSkillDatabase(path, status, runtime_build_check=runtime_build_check)


__all__ = [
    "DiceSkillDatabase",
    "DiceSkillStatus",
    "DiceSkillValidationError",
    "LOADED_PROFILE_ID",
    "OVERLOADED_PROFILE_ID",
    "load_dice_skill_database",
    "loaded_selected_skill_id",
    "overloaded_selected_skill_id",
    "profile_id_for_address",
    "selected_skill_id",
    "validate_document",
]
