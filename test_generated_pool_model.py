"""Tests for the production, research-independent generated-pool model."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESEARCH = ROOT / "_research"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(RESEARCH))

import generated_pool_model as subject  # noqa: E402

try:
    import generated_pool_evaluator_438b as oracle  # noqa: E402
except ModuleNotFoundError:
    oracle = None


class GeneratedPoolModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (
            RESEARCH / "item_roll_profiles_all_438b.json",
            RESEARCH / "generated_pool_spec_438b.json",
            RESEARCH / "late_unique_roll_spec_438b.json",
            RESEARCH / "item_roll_profiles_unique_runtime_438b.json",
        )
        if oracle is None or not all(path.is_file() for path in required):
            raise unittest.SkipTest(
                "independent research oracle/fixtures are not included in the "
                "standalone application repository"
            )
        source = json.loads(
            (RESEARCH / "item_roll_profiles_all_438b.json").read_text(
                encoding="utf-8"
            )
        )
        spec = json.loads(
            (RESEARCH / "generated_pool_spec_438b.json").read_text(
                encoding="utf-8"
            )
        )
        cls.profiles = source["profiles"]
        cls.affected = {row["profileId"]: row for row in spec["affectedProfiles"]}
        cls.late_spec_path = RESEARCH / "late_unique_roll_spec_438b.json"
        cls.runtime_path = (
            RESEARCH / "item_roll_profiles_unique_runtime_438b.json"
        )

    def test_bundle_is_hash_bound_and_has_exact_coverage(self) -> None:
        self.assertEqual(
            subject.computed_bundle_sha256(), subject.MODEL_BUNDLE_SHA256
        )
        self.assertEqual(
            set(subject.PROFILE_MODELS),
            set(self.affected) | {subject.OVERLOADED_PROFILE_ID},
        )
        self.assertEqual(
            sum(len(row["poolSlots"]) for row in subject.PROFILE_MODELS.values()),
            43,
        )
        self.assertEqual(subject.SOURCE_ARTIFACT_SHA256, {
            "generatedPoolSpec": hashlib.sha256(
                (ROOT / "_research" / "generated_pool_spec_438b.json").read_bytes()
            ).hexdigest().upper(),
            "lateUniqueSpec": hashlib.sha256(
                self.late_spec_path.read_bytes()
            ).hexdigest().upper(),
            "uniqueRuntimeProfiles": hashlib.sha256(
                self.runtime_path.read_bytes()
            ).hexdigest().upper(),
        })

    def test_scalar_replay_matches_independent_research_oracle(self) -> None:
        for profile_id, row in self.affected.items():
            slots = {int(key): int(value) for key, value in row["poolSlots"].items()}
            for seed in (1, 19, 113, 2_842, 999_983):
                expected = oracle.evaluate(self.profiles[profile_id], slots, seed)
                actual = subject.replay(profile_id, seed)
                self.assertEqual(actual["finalState"], expected["finalState"])
                self.assertEqual(actual["eventPath"], expected["calls"])
                self.assertEqual(
                    actual["visibleAssignments"], expected["visibleVariableStats"]
                )
                self.assertEqual(
                    (
                        actual["maxed"], actual["total"],
                        actual["endpointDeficit"],
                    ),
                    (
                        expected["objective"]["maxed"],
                        expected["objective"]["total"],
                        expected["objective"]["endpointDeficit"],
                    ),
                    (profile_id, seed),
                )

    def test_metadata_is_canonical_and_tampering_fails(self) -> None:
        profile_id = "unique:10:0:70"
        metadata = subject.metadata(profile_id)
        self.assertEqual(subject.validate_metadata(profile_id, metadata), metadata)
        for field, replacement in (
            ("kind", "other"),
            ("poolSlots", {"0": 1}),
            ("perfectRollAction", "preserve_existing_a"),
            ("theoreticalMaxVisible", 3),
            ("modelSha256", "0" * 64),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(metadata)
                changed[field] = replacement
                with self.assertRaises(ValueError):
                    subject.validate_metadata(profile_id, changed)

    def test_variable_visibility_requires_structural_upper_bound(self) -> None:
        profile_id = "unique:10:0:70"
        early = subject.replay(profile_id, 3_100)
        later = subject.replay(profile_id, 3_430)
        self.assertEqual(
            (early["maxed"], early["total"], early["endpointDeficit"]),
            (3, 3, 0),
        )
        self.assertEqual(early["theoreticalMaxVisible"], 4)
        self.assertEqual(
            (later["maxed"], later["total"], later["endpointDeficit"]),
            (4, 4, 0),
        )

    def test_special_stats_run_after_key20_and_use_last_write_wins(self) -> None:
        wraith = subject.replay("unique:1:0:98", 1)
        self.assertEqual(
            [event["phase"] for event in wraith["eventPath"][-3:]],
            [
                "late.socket_range_value",
                "special.damage_type.identity",
                "special.damage_type.value",
            ],
        )
        special = wraith["visibleAssignments"][-1]
        self.assertEqual(special["source"], "special.damage_type")
        self.assertIn(special["statKey"], {82, 83, 84, 85, 86})

        signet = subject.replay("unique:7:0:51", 1)
        self.assertEqual(
            [event["phase"] for event in signet["eventPath"][-3:]],
            [
                "late.socket_missing_hidden_advance",
                "special.damage_type.identity",
                "special.random_stat.identity",
            ],
        )
        blocked_keys = {row["statKey"] for row in signet["identityResults"]}
        self.assertTrue(blocked_keys.isdisjoint(
            row["statKey"] for row in signet["visibleAssignments"]
        ))

    def test_overloaded_identity_loop_is_audited_but_not_an_objective(self) -> None:
        replay = subject.replay(subject.OVERLOADED_PROFILE_ID, 1)
        phases = [event["phase"] for event in replay["eventPath"]]
        self.assertEqual(phases[0], "definition.definition_range")
        self.assertEqual(phases.count("generated.outer_group"), 4)
        self.assertEqual(phases.count("generated.outer_subtype"), 4)
        self.assertEqual(phases[-1], "late.socket_missing_hidden_advance")
        selected = replay["identityResults"][0]
        self.assertIn(
            selected["selectedIdentity"], subject.OVERLOADED_VALID_SUBSKILL_IDS
        )
        self.assertEqual(
            (replay["maxed"], replay["total"], replay["endpointDeficit"]),
            (0, 0, 0),
        )
        self.assertEqual(
            subject.metadata(subject.OVERLOADED_PROFILE_ID)["perfectRollAction"],
            "preserve_existing_a",
        )

    def test_native_source_seed_domain_is_one_through_one_billion(self) -> None:
        for seed in (1, 1_000_000_000):
            replay = subject.replay("unique:10:0:70", seed)
            self.assertEqual(replay["seed"], seed)
        for seed in (0, 1_000_000_001, True):
            with self.assertRaises(ValueError):
                subject.replay("unique:10:0:70", seed)


if __name__ == "__main__":
    unittest.main()
