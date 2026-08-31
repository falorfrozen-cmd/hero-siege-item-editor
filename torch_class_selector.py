"""Fail-closed class selector for the Season 10 Torch of Shadows charm.

Torch of Shadows serializes its definition CPR seed in save field ``a``.  The
game consumes four variable-stat rolls, eight inactive generated-pool draws,
then derives stat 21 (the class used by ``All Talents``) from the thirteenth
draw.  The table below contains one independently replayed seed for every
native class.  Every seed also places all four variable definition stats at
their maximum endpoint.

This selector is deliberately separate from the Loaded/Overloaded Dice
database.  Its instruction path has been matched only on the two executable
hashes listed in :data:`SUPPORTED_EXE_SHA256`; an unknown build disables every
target instead of guessing.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable


TORCH_PROFILE_ID = "unique:10:0:23"
TORCH_ADDRESS = ("unique", 10, 0, 23)

SEASON_10_705_EXE_SHA256 = (
    "438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4"
)
SEASON_10_706_EXE_SHA256 = (
    "2034FAD4096BE6DE1147E4FF61B942A706673A9567B10C3013C6393ED0686486"
)
SUPPORTED_EXE_SHA256 = frozenset({
    SEASON_10_705_EXE_SHA256,
    SEASON_10_706_EXE_SHA256,
})

SEED_START = 1
SEED_STOP = 1_000_000_000

_CPR_MULTIPLIER = 1_789_570_533.0
_CPR_INCREMENT = 465_707.0
_CPR_MODULUS = 2_147_483_648.0
_CPR_MASK = 1_073_741_823
_CPR_MAX = 1_073_741_823.0

# Native stat-21 IDs.  Keep this table local: the editor's general display
# table historically swapped Jotunn and Illusionist and must not influence the
# binary-proven selector identity.
CLASS_NAMES = {
    1: "Viking",
    2: "Pyromancer",
    3: "Marksman",
    4: "Pirate",
    5: "Nomad",
    6: "Redneck",
    7: "Necromancer",
    8: "Samurai",
    9: "Paladin",
    10: "Amazon",
    11: "Demon Slayer",
    12: "Demonspawn",
    13: "Shaman",
    14: "White Mage",
    15: "Marauder",
    16: "Plague Doctor",
    17: "Shield Lancer",
    18: "Jötunn",
    19: "Illusionist",
    20: "Exo",
    21: "Butcher",
    22: "Stormweaver",
    23: "Bard",
    24: "Prophet",
}

# First seeds found in 1..353944 that satisfy both invariants:
#   core roll deltas == [10, 2, 10, 2]
#   stat 21 == requested class ID
CLASS_SEEDS = {
    1: 332_503,
    2: 315_484,
    3: 314_878,
    4: 331_867,
    5: 320_779,
    6: 315_529,
    7: 315_892,
    8: 320_482,
    9: 314_824,
    10: 324_193,
    11: 321_187,
    12: 323_512,
    13: 315_757,
    14: 325_558,
    15: 316_825,
    16: 353_944,
    17: 325_504,
    18: 319_195,
    19: 348_151,
    20: 316_189,
    21: 317_476,
    22: 314_470,
    23: 316_771,
    24: 319_423,
}


class TorchClassValidationError(ValueError):
    """Raised when a Torch class target cannot be proven or applied."""


@dataclass(frozen=True)
class TorchRollReplay:
    seed: int
    variable_rolls: tuple[int, int, int, int]
    class_id: int

    @property
    def all_variable_stats_max(self) -> bool:
        return self.variable_rolls == (10, 2, 10, 2)


@dataclass(frozen=True)
class TorchClassStatus:
    available: bool
    code: str
    message: str
    target_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "code": self.code,
            "message": self.message,
            "targetCount": self.target_count,
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


def _seed_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TorchClassValidationError("Torch seed must be an integer save seed")
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise TorchClassValidationError("Torch seed must be an integer save seed")
    seed = int(numeric)
    if not SEED_START <= seed <= SEED_STOP:
        raise TorchClassValidationError(
            f"Torch seed must be inside {SEED_START}..{SEED_STOP}"
        )
    return seed


def replay_torch_seed(seed: int | float) -> TorchRollReplay:
    """Replay the exact Torch definition chain through native stat 21."""

    seed_int = _seed_integer(seed)
    state = seed_int
    values: list[int] = []
    for upper_inclusive in (10, 2, 10, 2):
        state, value = _draw(state, upper_inclusive)
        values.append(value)
    # GenerateItemRandomStats advances four inactive slot group/subtype pairs.
    # Their range arguments do not affect CPR state, so only eight advances are
    # relevant to the later class draw.
    for _ in range(8):
        state = _next_state(state)
    state, class_roll = _draw(state, 23)
    return TorchRollReplay(
        seed=seed_int,
        variable_rolls=(values[0], values[1], values[2], values[3]),
        class_id=class_roll + 1,
    )


def torch_selected_class_id(seed: int | float) -> int:
    return replay_torch_seed(seed).class_id


def supports_executable_sha256(digest: Any) -> bool:
    """Return true only for an exact, statically matched clean executable."""

    return isinstance(digest, str) and digest.upper() in SUPPORTED_EXE_SHA256


def profile_id_for_address(kind: str, cls: int, sub: int, base: int) -> str | None:
    try:
        address = (str(kind), int(cls), int(sub), int(base))
    except (TypeError, ValueError):
        return None
    return TORCH_PROFILE_ID if address == TORCH_ADDRESS else None


def _target_row(class_id: int, *, include_seed: bool) -> dict[str, Any]:
    name = CLASS_NAMES[class_id]
    row: dict[str, Any] = {
        "id": class_id,
        "key": name.casefold().replace(" ", "_").replace("ö", "o"),
        "name": name,
        "classId": class_id,
        "className": name,
    }
    if include_seed:
        row.update({"seed": CLASS_SEEDS[class_id], "profileId": TORCH_PROFILE_ID})
    return row


def _validate_embedded_targets() -> None:
    if set(CLASS_NAMES) != set(range(1, 25)):
        raise TorchClassValidationError("Torch native class coverage must be 1..24")
    if set(CLASS_SEEDS) != set(CLASS_NAMES):
        raise TorchClassValidationError("Torch class seed coverage mismatch")
    if len(set(CLASS_SEEDS.values())) != 24:
        raise TorchClassValidationError("Torch class seeds must be unique")
    for class_id, seed in CLASS_SEEDS.items():
        replay = replay_torch_seed(seed)
        if replay.class_id != class_id:
            raise TorchClassValidationError(
                f"Torch seed {seed} selects class {replay.class_id}, not {class_id}"
            )
        if not replay.all_variable_stats_max:
            raise TorchClassValidationError(
                f"Torch seed {seed} does not keep all four variable stats MAX"
            )


class TorchClassDatabase:
    """Immutable embedded target table with a runtime executable guard."""

    def __init__(
        self,
        *,
        runtime_build_check: Callable[[], str | None] | None = None,
    ) -> None:
        try:
            _validate_embedded_targets()
        except Exception as exc:
            self.status = TorchClassStatus(
                False, "invalid", f"Torch class targets disabled: {exc}", 0
            )
        else:
            self.status = TorchClassStatus(
                True,
                "ready",
                "Verified Torch of Shadows class targets ready",
                len(CLASS_SEEDS),
            )
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
                "profileId": TORCH_PROFILE_ID,
                "supportedExeSha256": sorted(SUPPORTED_EXE_SHA256),
                "maxedVariableStats": 4,
                "variableStatCount": 4,
            })
        return result

    def selector(self, profile_id: str, current_seed: Any = None) -> dict[str, Any] | None:
        if profile_id != TORCH_PROFILE_ID:
            return None
        runtime_error = self._runtime_error()
        available = self.status.available and runtime_error is None
        result: dict[str, Any] = {
            "profileId": TORCH_PROFILE_ID,
            "name": "Torch of Shadows",
            "targetKind": "class",
            "available": available,
            "message": runtime_error or self.status.message,
        }
        if available and current_seed is not None:
            try:
                class_id = torch_selected_class_id(current_seed)
            except TorchClassValidationError:
                class_id = None
            if class_id in CLASS_NAMES:
                result["current"] = _target_row(int(class_id), include_seed=False)
        return result

    def targets(self, profile_id: str) -> list[dict[str, Any]]:
        if profile_id != TORCH_PROFILE_ID or not self.available:
            return []
        return [_target_row(class_id, include_seed=True) for class_id in range(1, 25)]

    def target(self, profile_id: str, class_id: Any) -> dict[str, Any]:
        runtime_error = self._runtime_error()
        if not self.status.available or runtime_error is not None:
            raise TorchClassValidationError(runtime_error or self.status.message)
        if profile_id != TORCH_PROFILE_ID:
            raise TorchClassValidationError(
                "this item does not support Torch class targeting"
            )
        if isinstance(class_id, bool):
            raise TorchClassValidationError("class ID must be an integer")
        try:
            target_id = int(class_id)
        except (TypeError, ValueError) as exc:
            raise TorchClassValidationError("class ID must be an integer") from exc
        if isinstance(class_id, float) and not class_id.is_integer():
            raise TorchClassValidationError("class ID must be an integer")
        if target_id not in CLASS_SEEDS:
            raise TorchClassValidationError(
                f"class ID {target_id} is not a valid Torch target"
            )
        return copy.deepcopy(_target_row(target_id, include_seed=True))


def load_torch_class_database(
    *,
    runtime_build_check: Callable[[], str | None] | None = None,
) -> TorchClassDatabase:
    return TorchClassDatabase(runtime_build_check=runtime_build_check)


__all__ = [
    "CLASS_NAMES",
    "CLASS_SEEDS",
    "SEASON_10_705_EXE_SHA256",
    "SEASON_10_706_EXE_SHA256",
    "SUPPORTED_EXE_SHA256",
    "TORCH_ADDRESS",
    "TORCH_PROFILE_ID",
    "TorchClassDatabase",
    "TorchClassStatus",
    "TorchClassValidationError",
    "TorchRollReplay",
    "load_torch_class_database",
    "profile_id_for_address",
    "replay_torch_seed",
    "supports_executable_sha256",
    "torch_selected_class_id",
]
