import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("roll_profile_db.py")
SPEC = importlib.util.spec_from_file_location("roll_profile_db", MODULE_PATH)
roll_db = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = roll_db
SPEC.loader.exec_module(roll_db)


def _chain(segments, seed, mode):
    evaluations = [roll_db.evaluate_seed(seed, segment) for segment in segments]
    return {
        "seed": seed,
        "segments": segments,
        "mode": mode,
        "eventRolls": [list(evaluation.event_rolls) for evaluation in evaluations],
        "maxed": sum(evaluation.maxed for evaluation in evaluations),
        "total": sum(evaluation.total for evaluation in evaluations),
        "endpointDeficit": sum(
            evaluation.endpoint_deficit for evaluation in evaluations
        ),
        "proof": {
            "seedStart": roll_db.SEED_START,
            "seedStop": roll_db.SEED_STOP,
            "searchedThrough": (
                roll_db.SEED_STOP if mode == "best" else seed
            ),
            "intervalExhausted": mode == "best",
        },
    }


def _profile(profile_id, kind, chains, *, max_sockets=3, name="Fixture item"):
    mode = (
        "fixed"
        if not chains
        else "best"
        if any(chain["mode"] == "best" for chain in chains.values())
        else "exact"
    )
    return {
        "addressKey": profile_id,
        "kind": kind,
        "name": name,
        "sourceKey": "fixture_item",
        "maxSockets": max_sockets,
        "mode": mode,
        "maxed": sum(chain["maxed"] for chain in chains.values()),
        "total": sum(chain["total"] for chain in chains.values()),
        "endpointDeficit": sum(
            chain["endpointDeficit"] for chain in chains.values()
        ),
        "fieldSeeds": {
            save_field: chain["seed"] for save_field, chain in chains.items()
        },
        "chains": chains,
        "detail": "Verified fixture roll",
    }


def _dynamic_chain(profile_id, seed, mode, *, exhausted=False):
    replay = roll_db.generated_pool_model.replay(profile_id, seed)
    return {
        "seed": seed,
        "model": roll_db.generated_pool_model.metadata(profile_id),
        "eventPath": replay["eventPath"],
        "visibleAssignments": replay["visibleAssignments"],
        "finalState": replay["finalState"],
        "mode": mode,
        "maxed": replay["maxed"],
        "total": replay["total"],
        "endpointDeficit": replay["endpointDeficit"],
        "proof": {
            "seedStart": roll_db.SEED_START,
            "seedStop": roll_db.SEED_STOP,
            "searchedThrough": roll_db.SEED_STOP if exhausted else seed,
            "intervalExhausted": exhausted,
            "objectiveUpperBound": replay["theoreticalMaxVisible"],
        },
    }


def _identity_audit(profile_id, seed=roll_db.SEED_START):
    replay = roll_db.generated_pool_model.replay(profile_id, seed)
    return {
        "model": roll_db.generated_pool_model.metadata(profile_id),
        "auditSeed": seed,
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


def valid_document():
    exact_id = "unique:1:0:52"
    runeword_id = "runeword:7|normal:3:3:18"
    fixed_id = "normal:0:0:9"
    profiles = {
        exact_id: _profile(
            exact_id,
            "unique",
            {"a": _chain([[None, 10]], seed=1, mode="exact")},
        ),
        runeword_id: _profile(
            runeword_id,
            "runeword",
            {
                "a": _chain([[None, 10]], seed=1, mode="exact"),
                "i": _chain([[10]], seed=2, mode="best"),
            },
        ),
        fixed_id: _profile(
            fixed_id,
            "normal",
            {},
            max_sockets=None,
            name="Fixed fixture",
        ),
    }
    profiles[fixed_id]["sourceKey"] = "fixed_fixture"
    profiles[fixed_id]["detail"] = "No variable definition-stat rolls"
    return {
        "schemaVersion": roll_db.SCHEMA_VERSION,
        "catalogProfile": roll_db.CATALOG_PROFILE,
        "exeSha256": roll_db.EXPECTED_EXE_SHA256,
        "algorithm": roll_db.ALGORITHM,
        "objective": list(roll_db.OBJECTIVE),
        "seedDomain": {
            "start": roll_db.SEED_START,
            "stop": roll_db.SEED_STOP,
        },
        "coverage": {
            "profileCount": 3,
            "actionableCount": 2,
            "solutionSignatureReuses": 0,
            "modes": {"best": 1, "exact": 1, "fixed": 1},
            "kinds": {"normal": 1, "runeword": 1, "unique": 1},
            "scope": {
                "directNormal": 1,
                "directUnique": 1,
                "equipmentRuneword": 1,
                "excludedCodex": 0,
                "generatedPoolProfiles": 0,
                "identityOnlyAudited": 0,
                "socketSeedChains": 0,
            },
        },
        "audit": {
            "definitionFile": "fixture.json",
            "generatedPoolModelSha256": roll_db.generated_pool_model.MODEL_BUNDLE_SHA256,
            "generatedPoolSourceArtifactSha256": (
                roll_db.generated_pool_model.SOURCE_ARTIFACT_SHA256
            ),
            "identityOnlyAudits": {},
            "runtimeUsed": False,
        },
        "profiles": profiles,
    }


class RollProfileDatabaseTests(unittest.TestCase):
    def test_runtime_build_compatibility_is_exact_hash_bound_and_audited(self):
        self.assertTrue(
            roll_db.supports_executable_sha256(roll_db.EXPECTED_EXE_SHA256)
        )
        self.assertTrue(
            roll_db.supports_executable_sha256(
                roll_db.SEASON_10_706_EXE_SHA256.lower()
            )
        )
        self.assertFalse(roll_db.supports_executable_sha256("0" * 64))
        self.assertFalse(roll_db.supports_executable_sha256(None))

        proof = roll_db.ROLL_BUILD_EQUIVALENCE_PROOFS[
            roll_db.SEASON_10_706_EXE_SHA256
        ]
        self.assertEqual(proof["sourceExeSha256"], roll_db.EXPECTED_EXE_SHA256)
        self.assertEqual(proof["definitionProfileCount"], 1_444)
        self.assertEqual(proof["generatedPoolProfileCount"], 21)
        self.assertEqual(proof["generatedPoolActiveSlotCount"], 43)
        self.assertEqual(proof["runtimeProfileCount"], 5_059)

    def test_runtime_game_build_mismatch_hides_all_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps(valid_document()), encoding="utf-8")
            database = roll_db.load_roll_profile_db(
                path, runtime_build_check=lambda: "installed game hash changed"
            )
            self.assertFalse(database.available)
            self.assertEqual(database.profile_count, 0)
            self.assertEqual(database.profile_ids(), ())
            self.assertIsNone(database.lookup_id("unique:1:0:52"))
            self.assertEqual(
                database.summary()["code"], "game_build_unverified"
            )

    def test_address_helpers_and_both_lookup_paths(self):
        self.assertEqual(
            roll_db.address_key("unique", 1, 0, 52),
            "unique:1:0:52",
        )
        self.assertEqual(
            roll_db.runeword_key(7, 3, 3, 18),
            "runeword:7|normal:3:3:18",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / roll_db.DEFAULT_DATABASE_PATH.name
            path.write_text(json.dumps(valid_document()), encoding="utf-8")
            database = roll_db.load_roll_profile_database(Path(directory))

        self.assertTrue(database.available, database.status.message)
        self.assertEqual(database.status.code, "ready")
        self.assertEqual(database.profile_count, 3)
        self.assertEqual(database.actionable_count, 2)
        self.assertEqual(database.summary()["schemaVersion"], 3)
        self.assertEqual(database.summary()["modes"], {
            "best": 1, "exact": 1, "fixed": 1,
        })
        self.assertEqual(database.summary()["profileCount"], 3)
        self.assertEqual(
            set(database.summary()["supportedRuntimeExeSha256"]),
            set(roll_db.SUPPORTED_RUNTIME_EXE_SHA256),
        )
        self.assertEqual(database.lookup("unique", 1, 0, 52)["mode"], "exact")
        runeword = database.lookup_runeword(7, 3, 3, 18)
        self.assertEqual(runeword["mode"], "best")
        self.assertEqual(runeword["fieldSeeds"], {"a": 1, "i": 2})
        self.assertEqual(set(runeword["chains"]), {"a", "i"})
        self.assertIsNone(database.lookup("normal", 8, 8, 8))
        self.assertEqual(
            roll_db.runeword_address_key(7, 3, 3, 18),
            "runeword:7|normal:3:3:18",
        )

    def test_profile_results_and_constructor_are_defensive(self):
        document = roll_db.validate_roll_profile_document(valid_document())
        status = roll_db.RollProfileStatus(
            True, "ready", "fixture", Path("fixture.json"), 3, 2
        )
        database = roll_db.RollProfileDatabase(
            Path("fixture.json"), status, document
        )
        first = database.lookup("unique", 1, 0, 52)
        first["fieldSeeds"]["a"] = 999
        first["chains"]["a"]["seed"] = 999
        document["profiles"]["unique:1:0:52"]["chains"]["a"]["seed"] = 888
        stored = database.lookup("unique", 1, 0, 52)
        self.assertEqual(stored["fieldSeeds"]["a"], 1)
        self.assertEqual(stored["chains"]["a"]["seed"], 1)

    def test_missing_database_is_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "not-created.json"
            database = roll_db.load_roll_profile_db(path)
        self.assertFalse(database.available)
        self.assertEqual(database.status.code, "missing")
        self.assertEqual(database.profile_count, 0)
        self.assertIsNone(database.lookup("unique", 1, 0, 52))
        self.assertIn("not installed", database.status.message)

    def test_bad_json_is_rejected_without_partial_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text('{"profiles":', encoding="utf-8")
            database = roll_db.load_roll_profile_db(path)
        self.assertFalse(database.available)
        self.assertEqual(database.status.code, "invalid")
        self.assertEqual(database.profile_count, 0)
        self.assertIn("rejected", database.status.message)

    def test_metadata_contract_is_strict(self):
        mutations = {
            "schema": lambda doc: doc.update(schemaVersion=1),
            "catalog": lambda doc: doc.update(catalogProfile="Season 9"),
            "exe": lambda doc: doc.update(exeSha256="0" * 64),
            "algorithm": lambda doc: doc.update(algorithm="heuristic"),
            "objective": lambda doc: doc.update(objective=["maximize seed"]),
            "domain": lambda doc: doc.update(
                seedDomain={"start": 0, "stop": roll_db.SEED_STOP}
            ),
            "runtime": lambda doc: doc["audit"].update(runtimeUsed=True),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                document = valid_document()
                mutate(document)
                with self.assertRaises(roll_db.RollProfileValidationError):
                    roll_db.validate_roll_profile_document(document)

    def test_null_event_advances_cpr_before_scored_event_per_chain(self):
        evaluation = roll_db.evaluate_seed(1, [None, 10])
        self.assertEqual(evaluation.event_rolls, (None, 10))
        self.assertEqual((evaluation.maxed, evaluation.total), (1, 1))

        document = valid_document()
        document["profiles"]["unique:1:0:52"]["chains"]["a"][
            "eventRolls"
        ] = [[None, 7]]
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "scalar CPR recheck failed",
        ):
            roll_db.validate_roll_profile_document(document)

    def test_same_field_segments_restart_from_the_same_seed(self):
        document = valid_document()
        profile_id = "unique:1:0:52"
        document["profiles"][profile_id] = _profile(
            profile_id,
            "unique",
            {
                "a": _chain(
                    [[None, 10], [None, 10]],
                    seed=1,
                    mode="exact",
                )
            },
        )
        validated = roll_db.validate_roll_profile_document(document)
        chain = validated["profiles"][profile_id]["chains"]["a"]
        self.assertEqual(chain["eventRolls"], [[None, 10], [None, 10]])
        self.assertEqual(
            (chain["maxed"], chain["total"], chain["endpointDeficit"]),
            (2, 2, 0),
        )

        # A continuous replay would end in 1, proving that segment two must
        # independently restart from save field ``a`` rather than continue.
        self.assertEqual(
            roll_db.evaluate_seed(1, [None, 10, None, 10]).event_rolls,
            (None, 10, None, 1),
        )
        document["profiles"][profile_id]["chains"]["a"]["eventRolls"][1][-1] = 1
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "scalar CPR recheck failed",
        ):
            roll_db.validate_roll_profile_document(document)

    def test_schema_v3_generated_pool_chain_is_scalar_replayed(self):
        document = valid_document()
        profile_id = "unique:10:0:70"
        chain = _dynamic_chain(profile_id, 2_842, "exact")
        document["profiles"][profile_id] = _profile(
            profile_id, "unique", {"a": chain}, max_sockets=None,
            name="Generated pool fixture",
        )
        document["coverage"]["profileCount"] += 1
        document["coverage"]["actionableCount"] += 1
        document["coverage"]["modes"]["exact"] += 1
        document["coverage"]["kinds"]["unique"] += 1
        document["coverage"]["scope"]["directUnique"] += 1
        document["coverage"]["scope"]["generatedPoolProfiles"] += 1
        validated = roll_db.validate_roll_profile_document(document)
        actual = validated["profiles"][profile_id]["chains"]["a"]
        self.assertEqual(
            actual["model"]["kind"], roll_db.generated_pool_model.MODEL_KIND
        )
        self.assertEqual((actual["maxed"], actual["total"]), (4, 4))

        mutations = {
            "model": lambda row: row["model"].update(modelSha256="0" * 64),
            "event": lambda row: row["eventPath"][0].update(roll=0),
            "assignment": lambda row: row["visibleAssignments"][0].update(value=-1),
            "state": lambda row: row.update(finalState=row["finalState"] + 1),
            "upper": lambda row: row["proof"].update(objectiveUpperBound=3),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(document))
                mutate(changed["profiles"][profile_id]["chains"]["a"])
                with self.assertRaises(roll_db.RollProfileValidationError):
                    roll_db.validate_roll_profile_document(changed)

    def test_generated_pool_visible_exact_below_upper_bound_is_best(self):
        profile_id = "unique:10:0:70"
        chain = _dynamic_chain(
            profile_id, 3_100, "best", exhausted=True
        )
        self.assertEqual((chain["maxed"], chain["total"]), (3, 3))
        self.assertEqual(chain["endpointDeficit"], 0)
        document = valid_document()
        document["profiles"][profile_id] = _profile(
            profile_id, "unique", {"a": chain}, max_sockets=None,
        )
        document["coverage"]["profileCount"] += 1
        document["coverage"]["actionableCount"] += 1
        document["coverage"]["modes"]["best"] += 1
        document["coverage"]["kinds"]["unique"] += 1
        document["coverage"]["scope"]["directUnique"] += 1
        document["coverage"]["scope"]["generatedPoolProfiles"] += 1
        validated = roll_db.validate_roll_profile_document(document)
        self.assertEqual(validated["profiles"][profile_id]["mode"], "best")

        wrong = json.loads(json.dumps(document))
        dynamic = wrong["profiles"][profile_id]["chains"]["a"]
        dynamic["mode"] = "exact"
        wrong["profiles"][profile_id]["mode"] = "exact"
        wrong["coverage"]["modes"]["best"] -= 1
        wrong["coverage"]["modes"]["exact"] += 1
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError, "mode/result mismatch"
        ):
            roll_db.validate_roll_profile_document(wrong)

    def test_identity_only_profile_is_replayed_but_keeps_no_seed_chain(self):
        profile_id = roll_db.generated_pool_model.OVERLOADED_PROFILE_ID
        document = valid_document()
        document["profiles"][profile_id] = _profile(
            profile_id, "unique", {}, max_sockets=None, name="Overloaded Dice"
        )
        document["profiles"][profile_id]["detail"] = (
            "No ordered numeric roll objective; existing a is preserved"
        )
        document["coverage"]["profileCount"] += 1
        document["coverage"]["modes"]["fixed"] += 1
        document["coverage"]["kinds"]["unique"] += 1
        document["coverage"]["scope"]["directUnique"] += 1
        document["coverage"]["scope"]["identityOnlyAudited"] = 1
        document["audit"]["identityOnlyAudits"] = {
            profile_id: _identity_audit(profile_id)
        }

        validated = roll_db.validate_roll_profile_document(document)
        self.assertEqual(validated["profiles"][profile_id]["chains"], {})
        self.assertEqual(validated["profiles"][profile_id]["fieldSeeds"], {})

        mutations = {
            "event": lambda audit: audit["eventPath"][0].update(roll=-1),
            "identity": lambda audit: audit["identityResults"][0].update(
                selectedIdentity=-1
            ),
            "mutation": lambda audit: audit["proof"].update(
                sourceSeedMutation=True
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(document))
                mutate(changed["audit"]["identityOnlyAudits"][profile_id])
                with self.assertRaisesRegex(
                    roll_db.RollProfileValidationError, "scalar replay failed"
                ):
                    roll_db.validate_roll_profile_document(changed)

    def test_known_s10_zephy_and_dawn_regressions(self):
        zephy = roll_db.evaluate_seed(
            3_888_156,
            [50, 8, 9, 30, 15, 17],
        )
        self.assertEqual(zephy.event_rolls, (50, 8, 9, 30, 15, 17))
        self.assertEqual(
            (zephy.maxed, zephy.total, zephy.endpoint_deficit),
            (6, 6, 0),
        )

        dawn = roll_db.evaluate_seed(
            17_234_404,
            [6, 50, 10, 20, 10, 10, 20, 10],
        )
        self.assertEqual(
            dawn.event_rolls,
            (6, 41, 10, 20, 10, 10, 20, 10),
        )
        self.assertEqual(
            (dawn.maxed, dawn.total, dawn.endpoint_deficit),
            (7, 8, 9),
        )

    def test_primary_address_ids_are_canonical_and_match_profile(self):
        bad_ids = [
            "armors_zephys_gown",
            "unique:01:0:52",
            "runeword:7|unique:3:3:18",
        ]
        for bad_id in bad_ids:
            with self.subTest(profile_id=bad_id):
                document = valid_document()
                profile = document["profiles"].pop("unique:1:0:52")
                profile["addressKey"] = bad_id
                document["profiles"][bad_id] = profile
                with self.assertRaises(roll_db.RollProfileValidationError):
                    roll_db.validate_roll_profile_document(document)

        document = valid_document()
        document["profiles"]["unique:1:0:52"]["kind"] = "normal"
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "kind does not match",
        ):
            roll_db.validate_roll_profile_document(document)

    def test_chain_modes_and_proofs_fail_closed_on_inconsistency(self):
        cases = []

        wrong_aggregate_mode = valid_document()
        wrong_aggregate_mode["profiles"]["unique:1:0:52"]["mode"] = "best"
        cases.append(("aggregate mode", wrong_aggregate_mode))

        incomplete_best = valid_document()
        incomplete_best["profiles"][
            "runeword:7|normal:3:3:18"
        ]["chains"]["i"]["proof"]["intervalExhausted"] = False
        cases.append(("full-domain", incomplete_best))

        chain_marked_fixed = valid_document()
        chain_marked_fixed["profiles"]["unique:1:0:52"]["chains"]["a"][
            "mode"
        ] = "fixed"
        cases.append(("mode must be exact or best", chain_marked_fixed))

        exact_result_marked_best = valid_document()
        exact_result_marked_best["profiles"]["unique:1:0:52"]["chains"]["a"][
            "mode"
        ] = "best"
        cases.append(("mode/result mismatch", exact_result_marked_best))

        for expected, document in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(
                    roll_db.RollProfileValidationError,
                    expected,
                ):
                    roll_db.validate_roll_profile_document(document)

    def test_fixed_profiles_omit_chains_and_have_no_field_seeds(self):
        validated = roll_db.validate_roll_profile_document(valid_document())
        fixed = validated["profiles"]["normal:0:0:9"]
        self.assertEqual(fixed["mode"], "fixed")
        self.assertEqual(fixed["chains"], {})
        self.assertEqual(fixed["fieldSeeds"], {})
        self.assertEqual((fixed["maxed"], fixed["total"]), (0, 0))

        seeded_fixed = valid_document()
        seeded_fixed["profiles"]["normal:0:0:9"]["fieldSeeds"] = {"a": 1}
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "fieldSeeds must exactly match chains",
        ):
            roll_db.validate_roll_profile_document(seeded_fixed)

    def test_unknown_chain_fields_and_legacy_v1_fields_are_rejected(self):
        unknown = valid_document()
        unknown["profiles"]["unique:1:0:52"]["chains"]["x"] = _chain(
            [[None, 10]], 1, "exact"
        )
        unknown["profiles"]["unique:1:0:52"]["fieldSeeds"]["x"] = 1
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "chain key must be one of",
        ):
            roll_db.validate_roll_profile_document(unknown)

        for legacy_field, value in (
            ("seed", 1),
            ("signature", [10]),
            ("eventRolls", [10]),
            ("proof", {}),
        ):
            with self.subTest(legacy_field=legacy_field):
                document = valid_document()
                document["profiles"]["unique:1:0:52"][legacy_field] = value
                with self.assertRaisesRegex(
                    roll_db.RollProfileValidationError,
                    "legacy profile-scope roll fields",
                ):
                    roll_db.validate_roll_profile_document(document)

        flat_chain = valid_document()
        chain = flat_chain["profiles"]["unique:1:0:52"]["chains"]["a"]
        chain["signature"] = chain.pop("segments")[0]
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "flat signature is forbidden",
        ):
            roll_db.validate_roll_profile_document(flat_chain)

    def test_each_chain_seed_and_objective_values_are_recomputed(self):
        fields = ("eventRolls", "maxed", "total", "endpointDeficit")
        for field in fields:
            with self.subTest(field=field):
                document = valid_document()
                chain = document["profiles"]["unique:1:0:52"]["chains"]["a"]
                if field == "eventRolls":
                    chain[field] = [[None, 9]]
                else:
                    chain[field] += 1
                    if field == "total":
                        # Preserve the cheap structural total invariant so this
                        # still exercises the independent scalar replay.
                        chain["segments"][0].append(0)
                        chain["eventRolls"][0].append(0)
                with self.assertRaises(roll_db.RollProfileValidationError):
                    roll_db.validate_roll_profile_document(document)

    def test_field_seeds_are_an_exact_typed_projection_of_chains(self):
        mutations = {
            "missing": lambda seeds: seeds.pop("i"),
            "wrong": lambda seeds: seeds.update(i=3),
            "extra": lambda seeds: seeds.update(s=1),
            "bool": lambda seeds: seeds.update(i=True),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                document = valid_document()
                seeds = document["profiles"][
                    "runeword:7|normal:3:3:18"
                ]["fieldSeeds"]
                mutate(seeds)
                with self.assertRaises(roll_db.RollProfileValidationError):
                    roll_db.validate_roll_profile_document(document)

    def test_aggregate_objective_values_are_sums_of_all_chains(self):
        for field in ("maxed", "total", "endpointDeficit"):
            with self.subTest(field=field):
                document = valid_document()
                document["profiles"]["runeword:7|normal:3:3:18"][field] += 1
                with self.assertRaisesRegex(
                    roll_db.RollProfileValidationError,
                    f"aggregate {field}",
                ):
                    roll_db.validate_roll_profile_document(document)

    def test_all_allowed_save_field_chains_validate_independently(self):
        document = valid_document()
        profile_id = "unique:1:0:52"
        profile = _profile(
            profile_id,
            "unique",
            {
                "a": _chain([[None, 10]], 1, "exact"),
                "i": _chain([[None, 10]], 1, "exact"),
                "s": _chain([[None, 10]], 1, "exact"),
            },
        )
        document["profiles"][profile_id] = profile
        document["coverage"]["scope"]["socketSeedChains"] = 1
        validated = roll_db.validate_roll_profile_document(document)
        self.assertEqual(
            validated["profiles"][profile_id]["fieldSeeds"],
            {"a": 1, "i": 1, "s": 1},
        )
        self.assertEqual(validated["profiles"][profile_id]["total"], 3)

    def test_required_profile_text_fields_are_nonempty(self):
        for field in ("name", "sourceKey", "detail"):
            with self.subTest(field=field):
                document = valid_document()
                document["profiles"]["unique:1:0:52"][field] = " "
                with self.assertRaisesRegex(
                    roll_db.RollProfileValidationError,
                    f"{field} must be a non-empty string",
                ):
                    roll_db.validate_roll_profile_document(document)

    def test_coverage_counters_must_match_validated_profiles(self):
        document = valid_document()
        document["coverage"]["actionableCount"] = 921
        with self.assertRaisesRegex(
            roll_db.RollProfileValidationError,
            "actionableCount",
        ):
            roll_db.validate_roll_profile_document(document)

    def test_helper_inputs_reject_bool_negative_and_wrong_kind(self):
        for call in (
            lambda: roll_db.address_key("set", 1, 2, 3),
            lambda: roll_db.address_key("unique", True, 2, 3),
            lambda: roll_db.address_key("unique", -1, 2, 3),
            lambda: roll_db.runeword_key(False, 1, 2, 3),
        ):
            with self.assertRaises(ValueError):
                call()


if __name__ == "__main__":
    unittest.main()
