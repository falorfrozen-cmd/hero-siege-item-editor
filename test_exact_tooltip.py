"""Regression tests for the build-bound exact-tooltip backend."""

from __future__ import annotations

import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import exact_tooltip as subject  # noqa: E402
import roll_profile_db as roll_db  # noqa: E402


class ExactTooltipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(
            (HERE / "hs_full_catalog.json").read_text(encoding="utf-8")
        )
        cls.roll_document = json.loads(
            (HERE / "hs_perfect_roll_profiles.json").read_text(encoding="utf-8")
        )
        cls.profiles = cls.roll_document["profiles"]
        cls.db = subject.load_tooltip_model_database(HERE)
        if not cls.db.available:
            raise AssertionError(cls.db.status.message)
        cls.ready_build = {
            "matched": True,
            "code": "ready",
            "message": "test build matched",
            "expectedSha256": subject.EXPECTED_EXE_SHA256,
            "detectedSha256": subject.EXPECTED_EXE_SHA256,
        }

    def model(self, profile_id: str, catalog_id: int, data: dict, **kwargs):
        return subject.build_tooltip_model(
            self.catalog[catalog_id],
            data,
            self.profiles[profile_id],
            db=self.db,
            build_status=kwargs.pop("build_status", self.ready_build),
            **kwargs,
        )

    @staticmethod
    def by_key(model: dict) -> dict[int, dict]:
        return {
            row["statKey"]: row
            for row in model["stats"]
            if isinstance(row.get("statKey"), int)
        }

    def test_asset_is_hash_bound_and_covers_every_runtime_profile(self):
        self.assertEqual(self.db.profile_count, 5_059)
        self.assertEqual(self.db.definition_count, 1_444)
        self.assertEqual(set(self.db._profiles), set(self.profiles))
        summary = self.db.summary()
        self.assertEqual(summary["exeSha256"], subject.EXPECTED_EXE_SHA256)
        self.assertRegex(summary["payloadSha256"], r"^[0-9A-F]{64}$")

    def test_harlequinn_socket_and_all_talents_labels_do_not_cross(self):
        definition = self.db.definition("unique:0:0:0")
        labels = {row["statKey"]: row["label"] for row in definition["stats"]}
        self.assertEqual(labels[20], "Sockets")
        self.assertEqual(labels[201], "All Talents")
        self.assertEqual(definition["unmappedCatalogLines"], [])

        # Any catalog-backed name must agree with the independently learned
        # global identity.  Numeric collisions used to create 23 wrong names.
        mismatches = []
        for definition_id, raw in self.db._definitions.items():
            for stat in raw["stats"]:
                global_label = self.db._stat_labels.get(str(stat["statKey"]))
                if (
                    stat["labelSource"] == "item_catalog"
                    and global_label
                    and stat["label"] != global_label["label"]
                ):
                    mismatches.append((definition_id, stat["statKey"]))
        self.assertEqual(mismatches, [])

    def test_zephy_perfect_seed_replays_each_named_value(self):
        model = self.model(
            "unique:1:0:52", 1149,
            {"a": 3_888_156.0},
        )
        values = {key: row["value"] for key, row in self.by_key(model).items()}
        self.assertEqual(values, {
            20: 3,
            25: 50,
            29: 150,
            53: 15,
            66: 15,
            154: 160,
            173: 50,
            284: 25,
        })
        self.assertEqual(model["rollQuality"], {
            "maxed": 6,
            "total": 6,
            "endpointDeficit": 0,
            "percent": 100.0,
        })
        self.assertTrue(model["calculation"]["numbersExact"])
        self.assertFalse(model["calculation"]["textExact"])
        self.assertEqual(model["calculation"]["unsupportedPaths"], [])

    def test_arbitrary_nonperfect_seed_uses_native_cpr_order(self):
        model = self.model(
            "unique:1:0:52", 1149,
            {"a": 1.0},
        )
        values = {key: row["value"] for key, row in self.by_key(model).items()}
        self.assertEqual(values[29], 134)   # 100 + CPR(50) -> 34
        self.assertEqual(values[53], 15)    # 7 + CPR(8) -> 8
        self.assertEqual(values[66], 7)     # 6 + CPR(9) -> 1
        self.assertEqual(values[154], 132)  # 130 + CPR(30) -> 2
        self.assertEqual(values[173], 39)   # 35 + CPR(15) -> 4
        self.assertEqual(values[284], 23)   # 8 + CPR(17) -> 15
        self.assertEqual(model["rollQuality"]["endpointDeficit"], 65)
        self.assertTrue(model["calculation"]["numbersExact"])

    def test_ordinary_small_charm_never_claims_fixed_stats_or_rarity(self):
        model = self.model("normal:10:0:19", 382, {"a": 42_202_695.0})
        self.assertEqual(model["item"]["rarity"], "Unresolved")
        self.assertEqual(model["item"]["baseRarity"], "Normal")
        self.assertFalse(model["item"]["rolledRarityKnown"])
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "generated_affixes_unmodelled:small_charm",
            model["calculation"]["unsupportedPaths"],
        )
        self.assertTrue(any(
            "generated in-game from seed a" in warning
            for warning in model["calculation"]["warnings"]
        ))

    def test_ordinary_small_charm_identity_rejects_boolean_addresses(self):
        row = self.catalog[382]
        self.assertTrue(subject._is_ordinary_small_charm(row))
        for field in ("cls", "sub", "b"):
            changed = dict(row)
            changed[field] = True
            with self.subTest(field=field):
                self.assertFalse(subject._is_ordinary_small_charm(changed))

    def test_build_mismatch_never_claims_exact_values(self):
        mismatch = {
            "matched": False,
            "code": "build_mismatch",
            "message": "wrong build",
            "expectedSha256": subject.EXPECTED_EXE_SHA256,
            "detectedSha256": "0" * 64,
        }
        model = self.model(
            "unique:1:0:52", 1149,
            {"a": 3_888_156},
            build_status=mismatch,
        )
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn("build_unverified", model["calculation"]["unsupportedPaths"])
        self.assertTrue(all(
            row["confidence"] == "modelled_unverified" for row in model["stats"]
        ))

    def test_audited_compatible_build_claims_exact_values_without_weakening_unknowns(self):
        compatible = {
            "matched": True,
            "code": "ready_compatible",
            "message": "numeric path independently audited",
            "expectedSha256": subject.EXPECTED_EXE_SHA256,
            "detectedSha256": roll_db.SEASON_10_COMPATIBLE_EXE_SHA256,
        }
        model = self.model(
            "unique:1:0:52", 1149,
            {"a": 3_888_156},
            build_status=compatible,
        )
        self.assertTrue(model["calculation"]["numbersExact"])
        self.assertTrue(model["calculation"]["buildMatched"])
        self.assertEqual(model["buildGuard"]["code"], "ready_compatible")
        self.assertEqual(
            model["buildGuard"]["detectedExeSha256"],
            roll_db.SEASON_10_COMPATIBLE_EXE_SHA256,
        )

        rejected_statuses = (
            dict(compatible, code="unstable", matched=False),
            dict(compatible, code="unreadable", matched=False),
            dict(compatible, code="build_mismatch", matched=False),
            dict(compatible, detectedSha256="0" * 64),
        )
        for rejected in rejected_statuses:
            with self.subTest(status=rejected):
                guarded = self.model(
                    "unique:1:0:52", 1149,
                    {"a": 3_888_156},
                    build_status=rejected,
                )
                self.assertFalse(guarded["calculation"]["numbersExact"])
                self.assertIn(
                    "build_unverified", guarded["calculation"]["unsupportedPaths"]
                )

        for profile_id, catalog_id in (
            ("unique:10:0:31", 1850),
            ("unique:10:0:89", 1908),
        ):
            with self.subTest(profile_id=profile_id):
                dice = self.model(
                    profile_id, catalog_id, {"a": 1}, build_status=compatible
                )
                self.assertFalse(dice["calculation"]["numbersExact"])
                self.assertFalse(dice["calculation"]["buildMatched"])
                self.assertEqual(dice["buildGuard"]["code"], "dice_build_unverified")
                self.assertIn(
                    "build_unverified", dice["calculation"]["unsupportedPaths"]
                )

    def test_missing_seed_keeps_ranges_visible_but_unresolved(self):
        model = self.model("unique:1:0:52", 1149, {})
        by_key = self.by_key(model)
        self.assertEqual(by_key[25]["value"], 50)  # fixed definition stat
        self.assertIsNone(by_key[29]["value"])
        self.assertEqual(by_key[29]["minimum"], 100)
        self.assertEqual(by_key[29]["maximum"], 150)
        self.assertEqual(by_key[29]["confidence"], "unresolved")
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "missing_or_invalid_seed:a", model["calculation"]["unsupportedPaths"]
        )

    def test_caller_profile_signature_tampering_fails_closed(self):
        profile = copy.deepcopy(self.profiles["normal:0:0:9"])
        profile["chains"]["a"]["segments"] = [[11]]
        model = subject.build_tooltip_model(
            self.catalog[9],
            {"a": 332_620},
            profile,
            db=self.db,
            build_status=self.ready_build,
        )
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "profile_signature_mismatch:a",
            model["calculation"]["unsupportedPaths"],
        )
        self.assertIsNone(self.by_key(model)[154]["value"])

    def test_generated_pool_returns_only_visible_assignments(self):
        model = self.model("unique:10:0:70", 1889, {"a": 2_842})
        by_key = self.by_key(model)
        self.assertEqual(set(by_key), {58, 97, 196, 201})
        self.assertEqual({key: row["value"] for key, row in by_key.items()}, {
            58: 12,
            97: 20,
            196: 20,
            201: 2,
        })
        # Pool selector configuration keys 0/1/2 are not item properties.
        self.assertNotIn(0, by_key)
        self.assertTrue(model["calculation"]["numbersExact"])

    def test_runeword_combines_independent_base_and_overlay_seeds(self):
        model = self.model(
            "runeword:1|normal:3:1:0",
            50,
            {"a": 1, "i": 424_123},
        )
        by_key = self.by_key(model)
        self.assertEqual(by_key[22]["value"], 7)
        self.assertEqual(by_key[22]["role"], "base")
        self.assertEqual(by_key[28]["value"], 880)
        self.assertEqual(by_key[28]["formattedValue"], "880%")
        self.assertEqual(by_key[28]["role"], "runeword")
        self.assertEqual(model["rollQuality"]["total"], 4)
        self.assertTrue(model["calculation"]["numbersExact"])

    def test_last_definition_write_wins_for_duplicate_stat_key(self):
        status = subject.TooltipModelStatus(
            True, "ready", "synthetic", HERE, 1, 2
        )

        def fixed(definition_id: str, value: int) -> dict:
            return {
                "addressKey": definition_id,
                "events": [],
                "stats": [{
                    "statKey": 7,
                    "representation": "scalar",
                    "values": [value],
                    "label": "Shared stat",
                    "percent": False,
                    "catalogLineIndex": 0,
                    "catalogTemplate": str(value),
                    "labelSource": "item_catalog",
                }],
                "unmappedCatalogLines": [],
            }

        document = {
            "dynamicProfileIds": [],
            "statLabels": {},
            "definitions": {"base": fixed("base", 10), "overlay": fixed("overlay", 25)},
            "profiles": {"synthetic": {
                "kind": "runeword",
                "components": [
                    {"saveField": "a", "role": "base", "definitionId": "base"},
                    {"saveField": "i", "role": "runeword", "definitionId": "overlay"},
                ],
            }},
        }
        database = subject.TooltipModelDatabase(HERE, status, document)
        model = subject.build_tooltip_model(
            {"name": "Synthetic", "rar": "Runeword", "stats": []},
            {},
            {"addressKey": "synthetic", "name": "Synthetic", "kind": "runeword", "chains": {}},
            db=database,
            build_status=self.ready_build,
        )
        self.assertEqual(len(model["stats"]), 1)
        self.assertEqual(model["stats"][0]["value"], 25)
        self.assertEqual(model["stats"][0]["role"], "runeword")

    def test_unmapped_catalog_line_is_retained_and_blocks_exact(self):
        profile = self.profiles["normal:3:13:0"]
        model = self.model(
            "normal:3:13:0",
            197,
            {"a": profile["fieldSeeds"]["a"]},
        )
        old_catalog_line = next(
            row for row in model["stats"]
            if row["statKey"] is None and row["label"] == "Attack Range"
        )
        self.assertEqual(old_catalog_line["formattedValue"], "440")
        self.assertEqual(old_catalog_line["confidence"], "catalog_only")
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "catalog_line_mapping:normal:3:13:0",
            model["calculation"]["unsupportedPaths"],
        )

    def test_unbound_fallback_range_is_visible_and_blocks_exact(self):
        profile = self.profiles["unique:0:0:90"]
        model = subject.build_tooltip_model(
            {
                "name": "Leviathan's Crown",
                "rar": "Heroic",
                "stats": [],
            },
            profile["fieldSeeds"],
            profile,
            db=self.db,
            build_status=self.ready_build,
        )
        stat = self.by_key(model)[471]
        self.assertEqual(stat["label"], "Stat #471")
        self.assertEqual((stat["minimum"], stat["maximum"]), (25, 35))
        self.assertIsInstance(stat["value"], int)
        self.assertEqual(stat["confidence"], "unmapped")
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "unmapped_definition_stat:471",
            model["calculation"]["unsupportedPaths"],
        )

    def test_unbound_fallback_scalar_is_visible_but_native_helper_stays_hidden(self):
        profile = self.profiles["unique:3:10:13"]
        model = subject.build_tooltip_model(
            {"name": "Conjured Tentacle", "rar": "Heroic", "stats": []},
            profile["fieldSeeds"],
            profile,
            db=self.db,
            build_status=self.ready_build,
        )
        by_key = self.by_key(model)
        self.assertEqual(by_key[467]["value"], 15)
        self.assertEqual(by_key[467]["confidence"], "unmapped")
        self.assertNotIn(447, by_key)
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "unmapped_definition_stat:467",
            model["calculation"]["unsupportedPaths"],
        )

    def test_unbound_dynamic_reference_is_visible_and_fails_closed(self):
        profile = self.profiles["unique:3:14:19"]
        model = subject.build_tooltip_model(
            {"name": "Ethereal Musket", "rar": "Heroic", "stats": []},
            profile["fieldSeeds"],
            profile,
            db=self.db,
            build_status=self.ready_build,
        )
        stat = self.by_key(model)[77]
        self.assertEqual(stat["label"], "Stat #77")
        self.assertIsNone(stat["value"])
        self.assertEqual(stat["confidence"], "unresolved")
        self.assertNotIn(447, self.by_key(model))
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "unmapped_definition_stat:77",
            model["calculation"]["unsupportedPaths"],
        )
        self.assertIn(
            "dynamic_reference:77",
            model["calculation"]["unsupportedPaths"],
        )

    def test_filled_socket_is_decoded_and_blocks_full_exactness(self):
        socket = base64.b64encode(
            json.dumps({"a": 123, "b": 42, "n": 0}).encode("ascii")
        ).decode("ascii")
        model = self.model(
            "unique:1:0:52", 1149,
            {
                "a": 3_888_156,
                "s1": socket,
            },
        )
        self.assertEqual(model["sockets"], [{
            "index": 1,
            "status": "decoded",
            "baseId": 42,
            "seed": 123,
            "quantity": 0,
        }])
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn(
            "socket_payload_effects_unmodelled",
            model["calculation"]["unsupportedPaths"],
        )

    def test_ordinary_unique_uses_explicit_saved_socket_override(self):
        model = self.model(
            "unique:1:0:52",
            1149,
            {
                "a": 3_888_156,
                "c": 1.0,
                "s": 1.0,
                "zz": {"sockets": 1.0},
            },
        )
        sockets = self.by_key(model)[20]
        self.assertEqual(sockets["value"], 1)
        self.assertEqual(sockets["formattedValue"], "1")
        self.assertEqual(sockets["saveField"], "zz.sockets")
        self.assertEqual(sockets["confidence"], "exact")
        self.assertTrue(model["calculation"]["numbersExact"])
        self.assertEqual(model["calculation"]["unsupportedPaths"], [])
        self.assertEqual(model["calculation"]["warnings"], [])

    def test_invalid_stale_s_field_does_not_replace_direct_a_socket_roll(self):
        for invalid in (0, -1, 1.5, True, "2", None, float("nan")):
            with self.subTest(invalid=invalid):
                model = self.model(
                    "unique:1:0:52",
                    1149,
                    {"a": 3_888_156, "s": invalid},
                )
                self.assertTrue(model["calculation"]["numbersExact"])
                self.assertEqual(self.by_key(model)[20]["value"], 3)
                self.assertEqual(self.by_key(model)[20]["saveField"], "a")

    def test_real_runeword_socket_payloads_remain_partial(self):
        rune_ids = (26, 15, 27, 2, 33, 28)  # Breath of the Damned recipe #1
        data = {"a": 1, "i": 424_123, "zz": {"sockets": len(rune_ids)}}
        for index, rune_id in enumerate(rune_ids, 1):
            data[f"s{index}"] = base64.b64encode(
                json.dumps(
                    {"a": 100 + index, "b": rune_id, "n": 0},
                    separators=(",", ":"),
                ).encode("ascii")
            ).decode("ascii")
        model = self.model(
            "runeword:1|normal:3:1:0",
            50,
            data,
        )
        self.assertEqual(self.by_key(model)[20]["value"], 6)
        self.assertEqual(self.by_key(model)[20]["saveField"], "zz.sockets")
        self.assertEqual(len(model["sockets"]), 6)
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertEqual(model["calculation"]["coverage"], "partial")
        self.assertIn(
            "socket_payload_effects_unmodelled",
            model["calculation"]["unsupportedPaths"],
        )

    def test_custom_name_is_metadata_and_fingerprint_uses_native_data_only(self):
        first = self.model(
            "unique:1:0:52", 1149, {"a": 3_888_156}, custom_name="Boss Killer"
        )
        second = self.model(
            "unique:1:0:52", 1149, {"a": 3_888_156}, custom_name="Other Alias"
        )
        self.assertEqual(first["item"]["name"], "Boss Killer")
        self.assertEqual(first["item"]["canonicalName"], "Zephy's Gown")
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_compare_reports_numeric_deltas_without_better_worse_guess(self):
        low = self.model("unique:1:0:52", 1149, {"a": 1})
        high = self.model("unique:1:0:52", 1149, {"a": 3_888_156})
        comparison = subject.compare_tooltip_models(low, high)
        by_key = {
            row["statKey"]: row for row in comparison["rows"]
            if isinstance(row.get("statKey"), int)
        }
        self.assertTrue(comparison["different"])
        self.assertEqual(by_key[29]["delta"], 16)
        self.assertTrue(by_key[29]["different"])
        self.assertFalse(by_key[20]["different"])

    def test_payload_or_bound_catalog_tampering_is_rejected(self):
        document = json.loads(
            (HERE / "hs_tooltip_roll_models.json").read_text(encoding="utf-8")
        )
        changed = copy.deepcopy(document)
        changed["definitions"]["normal:0:0:0"]["name"] = "tampered"
        with self.assertRaisesRegex(
            subject.TooltipModelValidationError, "payload hash mismatch"
        ):
            subject.validate_tooltip_model_document(
                changed,
                catalog_path=HERE / "hs_full_catalog.json",
                roll_profiles_path=HERE / "hs_perfect_roll_profiles.json",
            )

        with tempfile.TemporaryDirectory() as temp:
            altered_catalog = Path(temp) / "hs_full_catalog.json"
            altered_catalog.write_bytes(
                (HERE / "hs_full_catalog.json").read_bytes() + b" "
            )
            with self.assertRaisesRegex(
                subject.TooltipModelValidationError, "catalog hash mismatch"
            ):
                subject.validate_tooltip_model_document(
                    document,
                    catalog_path=altered_catalog,
                    roll_profiles_path=HERE / "hs_perfect_roll_profiles.json",
                )


if __name__ == "__main__":
    unittest.main()
