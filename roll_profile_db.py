"""Fail-closed loader for the Season 10 perfect-roll profile database.

The database is generated offline from a clean Steam executable.  This module
does not inspect a running game and never reads or writes Hero Siege saves.  It
validates the complete artifact before exposing a single profile, including an
independent scalar IEEE-754 binary64 replay of every save-field CPR segment.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from . import generated_pool_model
except ImportError:  # Direct module loading used by the standalone editor/tests.
    try:
        import generated_pool_model  # type: ignore
    except ImportError:
        from HSItemEditor import generated_pool_model  # type: ignore

SCHEMA_VERSION = 3
CATALOG_PROFILE = "Season 10"
EXPECTED_EXE_SHA256 = (
    "438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4"
)
ALGORITHM = "Hero Siege S10 CPR binary64 exhaustive"
OBJECTIVE = [
    "maximize endpoint-hit count",
    "minimize total endpoint deficit",
    "minimize seed",
]
SEED_START = 1
SEED_STOP = 1_000_000_000
DEFAULT_DATABASE_PATH = Path(__file__).with_name("hs_perfect_roll_profiles.json")
ALLOWED_SAVE_FIELDS = frozenset({"a", "i", "s"})

# Schema v1 put one ambiguous CPR seed/trajectory at profile scope.  The game
# actually has independently initialized save-field chains (notably ``a`` for
# direct/base definition stats and ``i`` for a runeword overlay), so accepting
# any of these fields at profile scope would silently revive the old bug.
_LEGACY_PROFILE_ROLL_FIELDS = frozenset({
    "seed",
    "signature",
    "eventRolls",
    "proof",
})

# Scalar constants and operation order recovered for Hero Siege's CPR.  Keep
# these local: importing the research solver would defeat the runtime's
# independent verification boundary.
_CPR_MULTIPLIER = 1_789_570_533.0
_CPR_INCREMENT = 465_707.0
_CPR_MODULUS = 2_147_483_648.0
_CPR_MASK = 1_073_741_823
_CPR_MAX = 1_073_741_823.0

_DIRECT_ID_RE = re.compile(
    r"(?P<kind>normal|unique):(?P<cls>0|[1-9][0-9]*):"
    r"(?P<sub>0|[1-9][0-9]*):(?P<base>0|[1-9][0-9]*)\Z"
)
_RUNEWORD_ID_RE = re.compile(
    r"runeword:(?P<runeword>0|[1-9][0-9]*)\|normal:"
    r"(?P<cls>0|[1-9][0-9]*):(?P<sub>0|[1-9][0-9]*):"
    r"(?P<base>0|[1-9][0-9]*)\Z"
)


class RollProfileValidationError(ValueError):
    """Raised when an artifact does not satisfy the runtime contract."""


@dataclass(frozen=True)
class RollEvaluation:
    """Result of replaying one seed through one normalized event signature."""

    event_rolls: tuple[int | None, ...]
    maxed: int
    total: int
    endpoint_deficit: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "eventRolls": list(self.event_rolls),
            "maxed": self.maxed,
            "total": self.total,
            "endpointDeficit": self.endpoint_deficit,
        }


@dataclass(frozen=True)
class RollProfileStatus:
    """A stable status object suitable for the editor overview/API."""

    available: bool
    code: str
    message: str
    path: Path
    profile_count: int = 0
    actionable_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "code": self.code,
            "message": self.message,
            "path": str(self.path),
            "profileCount": self.profile_count,
            "actionableCount": self.actionable_count,
        }


class RollProfileDatabase:
    """Validated profiles, or an empty unavailable database.

    Profile access returns defensive copies so UI code cannot accidentally
    mutate the validated in-memory artifact.
    """

    def __init__(
        self,
        path: Path,
        status: RollProfileStatus,
        document: Mapping[str, Any] | None = None,
        runtime_build_check: Callable[[], str | None] | None = None,
    ) -> None:
        self.path = path
        self.status = status
        self._document = copy.deepcopy(dict(document or {}))
        raw_profiles = self._document.get("profiles", {})
        self._profiles: dict[str, dict[str, Any]] = copy.deepcopy(dict(raw_profiles))
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

    @property
    def profile_count(self) -> int:
        return len(self._profiles) if self.available else 0

    @property
    def actionable_count(self) -> int:
        if not self.available:
            return 0
        return sum(
            profile.get("mode") in {"exact", "best"}
            for profile in self._profiles.values()
        )

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(self._profiles) if self.available else ()

    def metadata(self) -> dict[str, Any]:
        if not self.available:
            return {}
        return copy.deepcopy({
            key: value
            for key, value in self._document.items()
            if key != "profiles"
        })

    def summary(self) -> dict[str, Any]:
        """Return compact, JSON-serializable fields for ``/api/overview``."""

        summary = self.status.as_dict()
        runtime_error = self._runtime_error()
        if runtime_error is not None:
            summary.update({
                "available": False,
                "code": "game_build_unverified",
                "message": runtime_error,
                "profileCount": 0,
                "actionableCount": 0,
            })
            return summary
        if not self.status.available:
            return summary
        coverage = self._document["coverage"]
        summary.update({
            "schemaVersion": self._document["schemaVersion"],
            "catalogProfile": self._document["catalogProfile"],
            "exeSha256": self._document["exeSha256"],
            "algorithm": self._document["algorithm"],
            "seedDomain": copy.deepcopy(self._document["seedDomain"]),
            "modes": copy.deepcopy(coverage["modes"]),
            "kinds": copy.deepcopy(coverage["kinds"]),
        })
        return summary

    def lookup_id(self, profile_id: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        profile = self._profiles.get(profile_id)
        return copy.deepcopy(profile) if profile is not None else None

    def lookup(self, kind: str, cls: int, sub: int, base: int) -> dict[str, Any] | None:
        return self.lookup_id(address_key(kind, cls, sub, base))

    def lookup_runeword(
        self,
        runeword: int,
        base_cls: int,
        base_sub: int,
        base: int,
    ) -> dict[str, Any] | None:
        return self.lookup_id(
            runeword_key(runeword, base_cls, base_sub, base)
        )


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def address_key(kind: str, cls: int, sub: int, base: int) -> str:
    """Return the canonical primary ID for a normal or unique item address."""

    if kind not in {"normal", "unique"}:
        raise ValueError("kind must be 'normal' or 'unique'")
    return (
        f"{kind}:{_nonnegative_integer(cls, 'cls')}:"
        f"{_nonnegative_integer(sub, 'sub')}:"
        f"{_nonnegative_integer(base, 'base')}"
    )


def runeword_key(runeword: int, cls: int, sub: int, base: int) -> str:
    """Return the canonical ID for a runeword on its concrete normal base."""

    return (
        f"runeword:{_nonnegative_integer(runeword, 'runeword')}|"
        f"normal:{_nonnegative_integer(cls, 'cls')}:"
        f"{_nonnegative_integer(sub, 'sub')}:"
        f"{_nonnegative_integer(base, 'base')}"
    )


# Explicit aliases make the helpers discoverable without forcing editor code to
# remember whether the noun or verb comes first.
make_address_key = address_key
make_runeword_key = runeword_key
runeword_address_key = runeword_key


def evaluate_seed(seed: int, signature: Iterable[int | None]) -> RollEvaluation:
    """Replay CPR using scalar binary64 operations in the game's event order.

    ``None`` is an unscored event: it advances CPR but does not contribute to
    the optimization objective.  This is required for special stat 221 and any
    other definition path with an intermediate/null CPR call.
    """

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    state = seed
    event_rolls: list[int | None] = []
    maxed = 0
    total = 0
    endpoint_deficit = 0
    for index, delta in enumerate(signature):
        if delta is not None and (
            isinstance(delta, bool) or not isinstance(delta, int) or delta < 0
        ):
            raise ValueError(f"signature event {index} must be null or a non-negative integer")
        state = int(math.fmod(
            _CPR_MULTIPLIER * float(state) + _CPR_INCREMENT,
            _CPR_MODULUS,
        )) & _CPR_MASK
        if delta is None:
            event_rolls.append(None)
            continue
        roll = math.floor(
            (float(delta) + 0.99999) * (float(state) / _CPR_MAX)
        )
        event_rolls.append(roll)
        total += 1
        if roll == delta:
            maxed += 1
        endpoint_deficit += delta - roll
    return RollEvaluation(
        event_rolls=tuple(event_rolls),
        maxed=maxed,
        total=total,
        endpoint_deficit=endpoint_deficit,
    )


def _fail(message: str) -> None:
    raise RollProfileValidationError(message)


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _parse_profile_id(profile_id: str) -> tuple[str, dict[str, int]]:
    if not isinstance(profile_id, str):
        _fail("profile ID must be a string")
    direct = _DIRECT_ID_RE.fullmatch(profile_id)
    if direct:
        return direct.group("kind"), {
            "cls": int(direct.group("cls")),
            "sub": int(direct.group("sub")),
            "base": int(direct.group("base")),
        }
    runeword = _RUNEWORD_ID_RE.fullmatch(profile_id)
    if runeword:
        return "runeword", {
            "runeword": int(runeword.group("runeword")),
            "cls": int(runeword.group("cls")),
            "sub": int(runeword.group("sub")),
            "base": int(runeword.group("base")),
        }
    _fail(
        f"{profile_id!r}: profile ID must be kind:cls:sub:b or "
        "runeword:<rw>|normal:<cls>:<sub>:<b>"
    )
    raise AssertionError("unreachable")


def _validate_chain(
    profile_id: str,
    save_field: str,
    raw: Any,
) -> dict[str, Any]:
    """Validate one save-field seed across independently reset CPR segments."""

    label = f"{profile_id}: chains.{save_field}"
    if not isinstance(raw, dict):
        _fail(f"{label} must be an object")
    if "model" in raw:
        return _validate_generated_pool_chain(
            profile_id, save_field, raw
        )
    chain = copy.deepcopy(raw)
    if "signature" in chain:
        _fail(f"{label}: flat signature is forbidden; use reset-aware segments")
    expected_static_keys = {
        "seed", "segments", "eventRolls", "mode", "maxed", "total",
        "endpointDeficit", "proof",
    }
    if set(chain) != expected_static_keys:
        _fail(f"{label}: static chain fields do not match schema v3")
    mode = chain.get("mode")
    if mode not in {"exact", "best"}:
        _fail(f"{label}.mode must be exact or best")

    segments = chain.get("segments")
    event_rolls = chain.get("eventRolls")
    if not isinstance(segments, list) or not segments:
        _fail(f"{label}.segments must be a non-empty array of arrays")
    if not isinstance(event_rolls, list) or len(event_rolls) != len(segments):
        _fail(f"{label}.eventRolls must have one array per segment")

    for segment_index, signature in enumerate(segments):
        segment_label = f"{label}.segments[{segment_index}]"
        if not isinstance(signature, list):
            _fail(f"{segment_label} must be an array")
        rolls = event_rolls[segment_index]
        if not isinstance(rolls, list) or len(rolls) != len(signature):
            _fail(f"{label}.eventRolls[{segment_index}] must match its segment length")
        for event_index, delta in enumerate(signature):
            if delta is not None and (
                isinstance(delta, bool) or not isinstance(delta, int) or delta < 0
            ):
                _fail(f"{segment_label}: invalid event {event_index}")
        for event_index, roll in enumerate(rolls):
            if roll is not None and (
                isinstance(roll, bool) or not isinstance(roll, int) or roll < 0
            ):
                _fail(
                    f"{label}.eventRolls[{segment_index}]: "
                    f"invalid event roll {event_index}"
                )

    scored_total = sum(
        delta is not None
        for signature in segments
        for delta in signature
    )
    declared_total = _require_int(chain.get("total"), f"{label}.total")
    declared_maxed = _require_int(chain.get("maxed"), f"{label}.maxed")
    declared_deficit = _require_int(
        chain.get("endpointDeficit"),
        f"{label}.endpointDeficit",
    )
    if scored_total == 0:
        _fail(f"{label} has no scored roll events; fixed chains must be omitted")
    if declared_total != scored_total:
        _fail(f"{label}.total does not match its scored signature events")
    if declared_maxed > declared_total:
        _fail(f"{label}.maxed exceeds total")

    seed = _require_int(chain.get("seed"), f"{label}.seed", minimum=SEED_START)
    if seed > SEED_STOP:
        _fail(f"{label}.seed is outside the game domain")
    calculated_segments: list[RollEvaluation] = []
    for segment_index, signature in enumerate(segments):
        try:
            # Each call intentionally begins from the same save-field seed:
            # the game performs a fresh cpr_init(field) at every segment.
            calculated_segments.append(evaluate_seed(seed, signature))
        except ValueError as exc:
            _fail(
                f"{label}.segments[{segment_index}]: "
                f"scalar CPR replay failed: {exc}"
            )
    expected = {
        "eventRolls": [list(result.event_rolls) for result in calculated_segments],
        "maxed": sum(result.maxed for result in calculated_segments),
        "total": sum(result.total for result in calculated_segments),
        "endpointDeficit": sum(
            result.endpoint_deficit for result in calculated_segments
        ),
    }
    for field, declared in (
        ("eventRolls", event_rolls),
        ("maxed", declared_maxed),
        ("total", declared_total),
        ("endpointDeficit", declared_deficit),
    ):
        if declared != expected[field]:
            _fail(f"{label}: scalar CPR recheck failed for {field}")

    is_exact = (
        expected["maxed"] == expected["total"]
        and expected["endpointDeficit"] == 0
    )
    if (mode == "exact") != is_exact:
        _fail(f"{label}: mode/result mismatch")

    proof = chain.get("proof")
    if not isinstance(proof, dict):
        _fail(f"{label} has no proof object")
    if proof.get("seedStart") != SEED_START or proof.get("seedStop") != SEED_STOP:
        _fail(f"{label}.proof uses the wrong seed domain")
    searched_through = _require_int(
        proof.get("searchedThrough"),
        f"{label}.proof.searchedThrough",
        minimum=SEED_START,
    )
    if searched_through < seed or searched_through > SEED_STOP:
        _fail(f"{label}: invalid proof search boundary")
    if not isinstance(proof.get("intervalExhausted"), bool):
        _fail(f"{label}.proof.intervalExhausted must be boolean")
    if mode == "best" and (
        proof["intervalExhausted"] is not True
        or searched_through != SEED_STOP
    ):
        _fail(f"{label}: Best Possible requires full-domain exhaustion")
    return chain


def _validate_generated_pool_chain(
    profile_id: str,
    save_field: str,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    """Replay and validate one hash-bound dynamic generated-stat chain."""

    label = f"{profile_id}: chains.{save_field}"
    if save_field != "a":
        _fail(f"{label}: generated-pool model is valid only for field a")
    chain = copy.deepcopy(dict(raw))
    forbidden = {"segments", "eventRolls", "signature"}.intersection(chain)
    if forbidden:
        _fail(
            f"{label}: generated-pool chain cannot declare static fields: "
            f"{', '.join(sorted(forbidden))}"
        )
    expected_keys = {
        "seed", "model", "eventPath", "visibleAssignments", "finalState",
        "mode", "maxed", "total", "endpointDeficit", "proof",
    }
    if set(chain) != expected_keys:
        _fail(f"{label}: generated-pool chain fields do not match schema v3")
    try:
        model = generated_pool_model.validate_metadata(
            profile_id, chain.get("model")
        )
    except ValueError as exc:
        _fail(f"{label}: {exc}")
    seed = _require_int(chain.get("seed"), f"{label}.seed", minimum=SEED_START)
    if seed > SEED_STOP:
        _fail(f"{label}.seed is outside the game domain")
    try:
        replay = generated_pool_model.replay(profile_id, seed)
    except ValueError as exc:
        _fail(f"{label}: generated-pool scalar replay failed: {exc}")

    declared_mode = chain.get("mode")
    if declared_mode not in {"exact", "best"}:
        _fail(f"{label}.mode must be exact or best")
    for field in ("maxed", "total", "endpointDeficit", "finalState"):
        _require_int(chain.get(field), f"{label}.{field}")
    expected = {
        "eventPath": replay["eventPath"],
        "visibleAssignments": replay["visibleAssignments"],
        "finalState": replay["finalState"],
        "maxed": replay["maxed"],
        "total": replay["total"],
        "endpointDeficit": replay["endpointDeficit"],
    }
    for field, calculated in expected.items():
        if chain.get(field) != calculated:
            _fail(f"{label}: generated-pool scalar replay failed for {field}")

    structural_exact = (
        replay["maxed"] == model["theoreticalMaxVisible"]
        and replay["endpointDeficit"] == 0
    )
    if (declared_mode == "exact") != structural_exact:
        _fail(f"{label}: generated-pool mode/result mismatch")

    proof = chain.get("proof")
    if not isinstance(proof, dict) or set(proof) != {
        "seedStart", "seedStop", "searchedThrough", "intervalExhausted",
        "objectiveUpperBound",
    }:
        _fail(f"{label}: generated-pool proof fields do not match schema v3")
    if proof.get("seedStart") != SEED_START or proof.get("seedStop") != SEED_STOP:
        _fail(f"{label}.proof uses the wrong seed domain")
    if proof.get("objectiveUpperBound") != model["theoreticalMaxVisible"]:
        _fail(f"{label}.proof uses the wrong objective upper bound")
    searched_through = _require_int(
        proof.get("searchedThrough"),
        f"{label}.proof.searchedThrough",
        minimum=SEED_START,
    )
    if searched_through < seed or searched_through > SEED_STOP:
        _fail(f"{label}: invalid proof search boundary")
    if not isinstance(proof.get("intervalExhausted"), bool):
        _fail(f"{label}.proof.intervalExhausted must be boolean")
    if declared_mode == "best" and (
        proof["intervalExhausted"] is not True
        or searched_through != SEED_STOP
    ):
        _fail(f"{label}: Best Possible requires full-domain exhaustion")
    chain["model"] = model
    return chain


def _validate_identity_only_audit(
    profile_id: str,
    raw: Any,
) -> dict[str, Any]:
    label = f"audit.identityOnlyAudits.{profile_id}"
    if profile_id not in generated_pool_model.IDENTITY_ONLY_PROFILE_IDS:
        _fail(f"{label}: unsupported identity-only model")
    if not isinstance(raw, dict):
        _fail(f"{label} must be an object")
    audit = copy.deepcopy(raw)
    if set(audit) != {
        "model", "auditSeed", "eventPath", "identityResults", "finalState",
        "maxed", "total", "endpointDeficit", "perfectRollAction", "proof",
    }:
        _fail(f"{label}: fields do not match schema v3")
    try:
        model = generated_pool_model.validate_metadata(
            profile_id, audit.get("model")
        )
    except ValueError as exc:
        _fail(f"{label}: {exc}")
    audit_seed = _require_int(
        audit.get("auditSeed"), f"{label}.auditSeed", minimum=SEED_START
    )
    if audit_seed != SEED_START:
        _fail(f"{label}: canonical audit seed must be {SEED_START}")
    try:
        replay = generated_pool_model.replay(profile_id, audit_seed)
    except ValueError as exc:
        _fail(f"{label}: scalar replay failed: {exc}")
    expected = {
        "eventPath": replay["eventPath"],
        "identityResults": replay["identityResults"],
        "finalState": replay["finalState"],
        "maxed": 0,
        "total": 0,
        "endpointDeficit": 0,
        "perfectRollAction": "preserve_existing_a",
        "proof": {
            "kind": "identity-only scalar trajectory audit",
            "sourceSeedMutation": False,
        },
    }
    for field, calculated in expected.items():
        if audit.get(field) != calculated:
            _fail(f"{label}: scalar replay failed for {field}")
    audit["model"] = model
    return audit


def _validate_profile(profile_id: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail(f"{profile_id}: profile must be an object")
    profile = copy.deepcopy(raw)
    parsed_kind, _ = _parse_profile_id(profile_id)
    if profile.get("addressKey") != profile_id:
        _fail(f"{profile_id}: addressKey/primary-key mismatch")
    if profile.get("kind") != parsed_kind:
        _fail(f"{profile_id}: kind does not match its primary address ID")
    for field in ("name", "sourceKey", "detail"):
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            _fail(f"{profile_id}: {field} must be a non-empty string")

    max_sockets = profile.get("maxSockets")
    if max_sockets is not None:
        _require_int(max_sockets, f"{profile_id}: maxSockets")

    legacy_fields = _LEGACY_PROFILE_ROLL_FIELDS.intersection(profile)
    if legacy_fields:
        _fail(
            f"{profile_id}: legacy profile-scope roll fields are forbidden: "
            f"{', '.join(sorted(legacy_fields))}"
        )

    raw_chains = profile.get("chains")
    if not isinstance(raw_chains, dict):
        _fail(f"{profile_id}: chains must be an object")
    chains: dict[str, dict[str, Any]] = {}
    for save_field, raw_chain in raw_chains.items():
        if not isinstance(save_field, str) or save_field not in ALLOWED_SAVE_FIELDS:
            _fail(
                f"{profile_id}: chain key must be one of "
                f"{', '.join(sorted(ALLOWED_SAVE_FIELDS))}"
            )
        chains[save_field] = _validate_chain(profile_id, save_field, raw_chain)

    field_seeds = profile.get("fieldSeeds")
    if not isinstance(field_seeds, dict):
        _fail(f"{profile_id}: fieldSeeds must be an object")
    for save_field, seed in field_seeds.items():
        if not isinstance(save_field, str) or save_field not in ALLOWED_SAVE_FIELDS:
            _fail(
                f"{profile_id}: fieldSeeds key must be one of "
                f"{', '.join(sorted(ALLOWED_SAVE_FIELDS))}"
            )
        _require_int(seed, f"{profile_id}: fieldSeeds.{save_field}", minimum=SEED_START)
    expected_field_seeds = {
        save_field: chain["seed"]
        for save_field, chain in chains.items()
    }
    if field_seeds != expected_field_seeds:
        _fail(f"{profile_id}: fieldSeeds must exactly match chains")

    expected_mode = (
        "fixed"
        if not chains
        else "best"
        if any(chain["mode"] == "best" for chain in chains.values())
        else "exact"
    )
    if profile.get("mode") != expected_mode:
        _fail(f"{profile_id}: aggregate mode does not match chains")

    expected_maxed = sum(chain["maxed"] for chain in chains.values())
    expected_total = sum(chain["total"] for chain in chains.values())
    expected_deficit = sum(
        chain["endpointDeficit"] for chain in chains.values()
    )
    for field, expected in (
        ("maxed", expected_maxed),
        ("total", expected_total),
        ("endpointDeficit", expected_deficit),
    ):
        declared = _require_int(profile.get(field), f"{profile_id}: {field}")
        if declared != expected:
            _fail(f"{profile_id}: aggregate {field} does not match chains")

    # Replace nested values with their independently validated copies and a
    # canonical field->seed projection before exposing the document.
    profile["chains"] = chains
    profile["fieldSeeds"] = expected_field_seeds
    return profile


def validate_roll_profile_document(document: Any) -> dict[str, Any]:
    """Validate and return an isolated copy of a runtime database document."""

    if not isinstance(document, dict):
        _fail("database top level must be an object")
    if document.get("schemaVersion") != SCHEMA_VERSION:
        _fail(f"database schemaVersion must be {SCHEMA_VERSION}")
    if document.get("catalogProfile") != CATALOG_PROFILE:
        _fail(f"database catalogProfile must be {CATALOG_PROFILE!r}")
    exe_hash = document.get("exeSha256")
    if not isinstance(exe_hash, str) or exe_hash.upper() != EXPECTED_EXE_SHA256:
        _fail("database executable SHA-256 is not the clean Steam Season 10 build")
    if document.get("algorithm") != ALGORITHM:
        _fail("database algorithm is not the exhaustive scalar CPR algorithm")
    if document.get("objective") != OBJECTIVE:
        _fail("database objective does not match the Best Possible contract")
    if document.get("seedDomain") != {"start": SEED_START, "stop": SEED_STOP}:
        _fail("database seedDomain must be the complete game domain")

    audit = document.get("audit")
    if not isinstance(audit, dict) or audit.get("runtimeUsed") is not False:
        _fail("database audit must declare runtimeUsed=false")
    if audit.get("generatedPoolModelSha256") != generated_pool_model.MODEL_BUNDLE_SHA256:
        _fail("database audit has the wrong generated-pool model hash")
    if (
        audit.get("generatedPoolSourceArtifactSha256")
        != generated_pool_model.SOURCE_ARTIFACT_SHA256
    ):
        _fail("database audit has the wrong generated-pool source hashes")
    raw_identity_audits = audit.get("identityOnlyAudits")
    if (
        not isinstance(raw_identity_audits, dict)
        or not set(raw_identity_audits).issubset(
            generated_pool_model.IDENTITY_ONLY_PROFILE_IDS
        )
    ):
        _fail("database audit identity-only coverage mismatch")
    identity_audits = {
        profile_id: _validate_identity_only_audit(profile_id, raw)
        for profile_id, raw in raw_identity_audits.items()
    }
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        _fail("database contains no profiles")

    profiles: dict[str, dict[str, Any]] = {}
    for profile_id, raw_profile in raw_profiles.items():
        profiles[profile_id] = _validate_profile(profile_id, raw_profile)
    expected_identity_ids = set(profiles).intersection(
        generated_pool_model.IDENTITY_ONLY_PROFILE_IDS
    )
    if set(identity_audits) != expected_identity_ids:
        _fail("database audit identity-only/profile coverage mismatch")
    for profile_id in identity_audits:
        profile = profiles.get(profile_id)
        if profile is None or profile["chains"] or profile["fieldSeeds"]:
            _fail(
                f"{profile_id}: identity-only audit requires a fixed profile "
                "that preserves existing a"
            )

    coverage = document.get("coverage")
    if not isinstance(coverage, dict):
        _fail("database has no coverage object")
    modes = Counter(profile["mode"] for profile in profiles.values())
    kinds = Counter(profile["kind"] for profile in profiles.values())
    expected_modes = dict(sorted(modes.items()))
    expected_kinds = dict(sorted(kinds.items()))
    actionable_count = modes["exact"] + modes["best"]
    if coverage.get("profileCount") != len(profiles):
        _fail("coverage.profileCount does not match profiles")
    if coverage.get("actionableCount") != actionable_count:
        _fail("coverage.actionableCount does not match profiles")
    if coverage.get("modes") != expected_modes:
        _fail("coverage.modes does not match profiles")
    if coverage.get("kinds") != expected_kinds:
        _fail("coverage.kinds does not match profiles")
    scope = coverage.get("scope")
    if not isinstance(scope, dict) or set(scope) != {
        "directNormal", "directUnique", "equipmentRuneword",
        "excludedCodex", "generatedPoolProfiles", "identityOnlyAudited",
        "socketSeedChains",
    }:
        _fail("coverage.scope does not match schema v3")
    expected_scope = {
        "directNormal": kinds["normal"],
        "directUnique": kinds["unique"],
        "equipmentRuneword": kinds["runeword"],
        "generatedPoolProfiles": sum(
            chain.get("model", {}).get("kind") == generated_pool_model.MODEL_KIND
            for profile in profiles.values()
            for chain in profile["chains"].values()
        ),
        "identityOnlyAudited": len(identity_audits),
        "socketSeedChains": sum(
            "s" in profile["chains"] for profile in profiles.values()
        ),
    }
    for field, expected in expected_scope.items():
        if scope.get(field) != expected:
            _fail(f"coverage.scope.{field} does not match profiles")
    _require_int(scope.get("excludedCodex"), "coverage.scope.excludedCodex")

    validated = copy.deepcopy(document)
    validated["exeSha256"] = EXPECTED_EXE_SHA256
    validated["audit"]["identityOnlyAudits"] = identity_audits
    validated["profiles"] = profiles
    return validated


def load_roll_profile_db(
    path: str | Path | None = None,
    *,
    runtime_build_check: Callable[[], str | None] | None = None,
) -> RollProfileDatabase:
    """Load a database without propagating file/validation failures.

    Missing, unreadable, malformed, stale, or internally inconsistent files
    all produce the same safe behavior: an unavailable database with zero
    exposed profiles and a human-readable status message.
    """

    database_path = Path(path) if path is not None else DEFAULT_DATABASE_PATH
    try:
        raw_text = database_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        status = RollProfileStatus(
            available=False,
            code="missing",
            message="Perfect-roll profile database is not installed.",
            path=database_path,
        )
        return RollProfileDatabase(
            database_path, status, runtime_build_check=runtime_build_check
        )
    except (OSError, UnicodeError) as exc:
        status = RollProfileStatus(
            available=False,
            code="unreadable",
            message=f"Perfect-roll profile database cannot be read: {exc}",
            path=database_path,
        )
        return RollProfileDatabase(
            database_path, status, runtime_build_check=runtime_build_check
        )

    try:
        document = json.loads(raw_text)
        validated = validate_roll_profile_document(document)
    except (json.JSONDecodeError, RollProfileValidationError) as exc:
        status = RollProfileStatus(
            available=False,
            code="invalid",
            message=f"Perfect-roll profile database was rejected: {exc}",
            path=database_path,
        )
        return RollProfileDatabase(
            database_path, status, runtime_build_check=runtime_build_check
        )

    profiles = validated["profiles"]
    actionable = sum(
        profile["mode"] in {"exact", "best"}
        for profile in profiles.values()
    )
    status = RollProfileStatus(
        available=True,
        code="ready",
        message=(
            f"{len(profiles)} verified Season 10 roll profiles loaded "
            f"({actionable} actionable)."
        ),
        path=database_path,
        profile_count=len(profiles),
        actionable_count=actionable,
    )
    return RollProfileDatabase(
        database_path,
        status,
        validated,
        runtime_build_check=runtime_build_check,
    )


def load_roll_profile_database(
    base_or_path: str | Path | None = None,
    *,
    runtime_build_check: Callable[[], str | None] | None = None,
) -> RollProfileDatabase:
    """Stable editor-facing loader accepting either its base dir or JSON path."""

    if base_or_path is None:
        return load_roll_profile_db(runtime_build_check=runtime_build_check)
    candidate = Path(base_or_path)
    if candidate.is_dir():
        candidate = candidate / DEFAULT_DATABASE_PATH.name
    return load_roll_profile_db(
        candidate, runtime_build_check=runtime_build_check
    )


__all__ = [
    "ALGORITHM",
    "ALLOWED_SAVE_FIELDS",
    "CATALOG_PROFILE",
    "DEFAULT_DATABASE_PATH",
    "EXPECTED_EXE_SHA256",
    "OBJECTIVE",
    "RollEvaluation",
    "RollProfileDatabase",
    "RollProfileStatus",
    "RollProfileValidationError",
    "SCHEMA_VERSION",
    "SEED_START",
    "SEED_STOP",
    "address_key",
    "evaluate_seed",
    "load_roll_profile_db",
    "load_roll_profile_database",
    "make_address_key",
    "make_runeword_key",
    "runeword_key",
    "runeword_address_key",
    "validate_roll_profile_document",
]
