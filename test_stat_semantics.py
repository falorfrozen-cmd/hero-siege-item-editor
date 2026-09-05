import json
import tempfile
import unittest
from pathlib import Path

from custom_item_forge import (
    CustomForgeError,
    CustomForgeStore,
    load_custom_forge_catalog,
)
from roll_profile_db import EXPECTED_EXE_SHA256
from stat_semantics import StatSemanticsError, load_stat_semantics


BASE = Path(__file__).resolve().parent


class StatSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = load_stat_semantics(
            BASE, expected_exe_sha256=EXPECTED_EXE_SHA256
        )
        cls.raw_catalog = load_custom_forge_catalog(BASE)
        cls.catalog = cls.database.decorate_catalog(cls.raw_catalog)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CustomForgeStore(
            Path(self.temp.name), self.catalog, self.database
        )

    def tearDown(self):
        self.temp.cleanup()

    def _preset_for_family(self, family):
        wanted = list(family)
        return next(
            prop
            for donor in self.catalog["donors"]
            for prop in donor["properties"]
            if prop.get("keys") == wanted
            and not prop.get("needsClass")
            and set(prop.get("stats", {})) == {str(key) for key in wanted}
        )

    def test_document_is_bound_to_the_expected_build_and_has_392_keys(self):
        self.assertEqual(self.database.key_count, 392)
        self.assertEqual(self.database.exe_sha256, EXPECTED_EXE_SHA256)
        self.assertEqual(self.catalog["semantics"]["observedNamedCount"], 325)
        self.assertEqual(self.catalog["semantics"]["observedUnknownCount"], 5)
        self.assertEqual(self.catalog["semantics"]["codeOnlyCount"], 62)

    def test_every_observed_named_stat_renders_the_json_name(self):
        original_keys = {row["key"] for row in self.raw_catalog["stats"]}
        decorated_keys = {row["key"] for row in self.catalog["stats"]}
        self.assertTrue(original_keys <= decorated_keys)
        for row in self.catalog["stats"]:
            if row.get("codeOnly"):
                self.assertNotIn(row["key"], original_keys)
                self.assertFalse(row["observed"])
                self.assertFalse(row["advanced"])
            metadata = self.database.get(row["key"])
            self.assertIsNotNone(metadata)
            if metadata["name"] != "unknown":
                self.assertEqual(row["label"], metadata["name"])
                self.assertNotEqual(row["label"], f"Stat #{row['key']}")
            self.assertEqual(row["valueKind"], metadata["valueKind"])
            self.assertEqual(row["unit"], metadata["unit"])
            self.assertEqual(row["linkedKeys"], metadata["linkedKeys"])
            self.assertEqual(row["safeEditable"], metadata["safeEditable"])

    def test_verified_presets_write_complete_linked_families(self):
        for first_key in (116, 202, 315):
            family = self.database.linked_keys(first_key)
            preset = self._preset_for_family(family)
            merged = self.store.merge_presets([preset["id"]], {})
            self.assertEqual(set(map(int, merged)), set(family))

        family = self.database.linked_keys(279)
        merged = self.store.merge_presets([], {279: 7})
        self.assertEqual(set(map(int, merged)), set(family))
        self.assertEqual(merged["279"], 7)

    def test_removing_one_member_removes_the_whole_linked_family(self):
        safe_anchor = {23: 2}
        for first_key in (116, 202, 315):
            family = self.database.linked_keys(first_key)
            preset = self._preset_for_family(family)
            merged = self.store.merge_presets(
                [preset["id"]], safe_anchor, excluded_keys=[family[-1]]
            )
            self.assertTrue(set(family).isdisjoint(map(int, merged)))
            self.assertIn("23", merged)

        family = self.database.linked_keys(279)
        merged = self.store.merge_presets(
            [], {23: 2, 279: 7, 280: 4}, excluded_keys=[280]
        )
        self.assertTrue(set(family).isdisjoint(map(int, merged)))

    def test_unsafe_values_reject_arbitrary_api_numbers(self):
        for key, value in ((20, 7), (20, 2.5), (116, 9999), (202, 9999), (315, 9999)):
            with self.subTest(key=key), self.assertRaises(CustomForgeError):
                self.store.merge_presets([], {key: value})

    def test_preset_identity_may_only_change_to_a_listed_talent(self):
        preset = self._preset_for_family(self.database.linked_keys(116))
        changed = dict(preset["stats"])
        changed["116"] = 558  # Shadowflames, a real talent id
        merged = self.store.merge_presets([preset["id"]], changed)
        self.assertEqual(merged["116"], 558)
        wrong = dict(preset["stats"])
        wrong["116"] = 9999
        with self.assertRaises(CustomForgeError):
            self.store.merge_presets([preset["id"]], wrong)

    def test_fail_closed_validation_uses_json_itself(self):
        document = json.loads(
            (BASE / "hs_stat_semantics_s10.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad_schema = dict(document)
            bad_schema["schemaVersion"] = 2
            (root / "hs_stat_semantics_s10.json").write_text(
                json.dumps(bad_schema), encoding="utf-8"
            )
            with self.assertRaises(StatSemanticsError):
                load_stat_semantics(root, expected_exe_sha256=EXPECTED_EXE_SHA256)

            broken = json.loads(json.dumps(document))
            broken["stats"]["116"]["linkedKeys"].append(9999)
            (root / "hs_stat_semantics_s10.json").write_text(
                json.dumps(broken), encoding="utf-8"
            )
            with self.assertRaises(StatSemanticsError):
                load_stat_semantics(root, expected_exe_sha256=EXPECTED_EXE_SHA256)

    def test_ui_and_pyinstaller_bundle_the_semantics_contract(self):
        source = (BASE / "hs_item_editor_gui.py").read_text(encoding="utf-8") + (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        spec = (BASE / "HeroSiegeItemEditor.spec").read_text(encoding="utf-8")
        self.assertIn("STAT_SEMANTICS.decorate_catalog", source)
        self.assertIn("row.safeEditable?'':'readonly'", source)
        self.assertIn("excludedKeys:[...excludedKeys]", source)
        self.assertIn('"hs_stat_semantics_s10.json"', spec)
        self.assertIn('"stat_semantics"', spec)
        self.assertIn('"item_forge_ui.js"', spec)
        self.assertIn('"item_forge_ui.css"', spec)

    def test_pickers_come_from_the_runtime_talent_table_and_class_list(self):
        pickers = self.catalog["pickers"]
        self.assertEqual([row["id"] for row in pickers["classes"]], list(range(1, 25)))
        self.assertGreaterEqual(len(pickers["talents"]), 800)
        talent = self.database.talent(558)
        self.assertEqual(talent["slug"], "shadowFlames")
        self.assertEqual(self.database.talent(2)["slug"], "weaponMaster")
        self.assertEqual(self.database.picker_kind(116), "talent")
        self.assertEqual(self.database.picker_kind(204), "class")
        self.assertIsNone(self.database.picker_kind(20))
        for row in self.catalog["stats"]:
            self.assertEqual(row["pickerKind"], self.database.picker_kind(row["key"]))

    def test_identity_keys_accept_listed_ids_and_refuse_everything_else(self):
        merged = self.store.merge_presets([], {116: 558})
        self.assertEqual(set(map(int, merged)), {116, 117, 118})
        self.assertEqual(merged["116"], 558)
        merged = self.store.merge_presets([], {202: 2, 204: 1, 203: 3})
        self.assertEqual(merged["204"], 1)
        for key, value in ((116, 9999), (116, 558.5), (204, 25), (204, 0)):
            with self.subTest(key=key, value=value), self.assertRaises(CustomForgeError):
                self.store.merge_presets([], {key: value})
        # Sockets are a plain 0-6 number now: ForgePact writes them after the
        # game's own socket roll, so the value holds.
        self.assertEqual(self.store.merge_presets([], {20: 6}), {"20": 6})

    def test_code_verified_keys_are_usable_even_when_never_observed(self):
        # The Spellhit proc family and the min/max damage keys exist only in
        # game code; they must be writable with explicit values.
        merged = self.store.merge_presets([], {119: 558, 120: 1, 121: 10})
        self.assertEqual(set(map(int, merged)), {119, 120, 121})
        merged = self.store.merge_presets([], {22: 40})
        self.assertEqual(merged, {"22": 40})
        merged = self.store.merge_presets([], {449: 15, 451: 15})
        self.assertEqual(set(merged), {"449", "451"})

    def test_code_only_keys_are_listed_with_usable_defaults(self):
        # Keys with code evidence but no drop (Spellhit family, min/max weapon
        # damage) get UI rows; their defaults follow the observed keys of the
        # same kind, and one click on the skill fills the whole family.
        rows = {row["key"]: row for row in self.catalog["stats"] if row.get("codeOnly")}
        self.assertEqual(len(rows), 62)
        for key in (119, 120, 121, 449, 451):
            self.assertIn(key, rows)
        self.assertEqual(rows[119]["pickerKind"], "talent")
        self.assertIsNone(rows[119]["recommendedValue"])
        for key in (120, 121, 449, 451):
            self.assertTrue(rows[key]["safeEditable"])
            self.assertIsInstance(rows[key]["recommendedValue"], (int, float))
            self.assertGreater(rows[key]["recommendedValue"], 1)
        merged = self.store.merge_presets([], {119: 558})
        self.assertEqual(set(map(int, merged)), {119, 120, 121})
        self.assertEqual(merged["120"], rows[120]["recommendedValue"])
        self.assertEqual(merged["121"], rows[121]["recommendedValue"])

    def test_ui_offers_every_named_stat_and_bundles_the_talent_table(self):
        source = (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        spec = (BASE / "HeroSiegeItemEditor.spec").read_text(encoding="utf-8")
        self.assertNotIn("if(++shown>=120)break", source)
        self.assertIn("data-picker=", source)
        self.assertIn("pickerKind", source)
        self.assertIn('"hs_talent_table_s10.json"', spec)
        self.assertIn("forgePageControls(page,hits.length,size)", source)
        self.assertNotIn("pickerOptions(meta)[0]", source)


if __name__ == "__main__":
    unittest.main()
