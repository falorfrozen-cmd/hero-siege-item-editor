import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_gui_small_charm", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


def small_charm_rows() -> list[dict]:
    return sorted(
        (
            row for row in editor.CAT
            if row.get("kind") == "normal"
            and row.get("cls") == 10
            and row.get("sub", 0) == 0
            and row.get("key") == "charms_normal_small_charm"
            and 0 <= int(row.get("b", -1)) <= 19
        ),
        key=lambda row: int(row["b"]),
    )


class UnavailableRollDatabase:
    available = False

    @staticmethod
    def summary() -> dict:
        return {"message": "test build mismatch"}


class SmallCharmNativeRandomTests(unittest.TestCase):
    def test_catalog_keeps_native_addresses_without_invented_grade_metadata(self):
        rows = small_charm_rows()
        self.assertEqual(len(rows), 20)
        self.assertEqual([int(row["b"]) for row in rows], list(range(20)))
        self.assertTrue(all(row["rar"] == "Normal" for row in rows))
        self.assertTrue(all("skillSelector" not in row for row in rows))
        for row in rows:
            with self.subTest(base=row["b"]):
                for removed in ("tier", "appearance", "variantLabel", "grade"):
                    self.assertNotIn(removed, row)

    def test_identity_predicate_is_fail_closed_to_exact_normal_small_charm(self):
        valid = small_charm_rows()[0]
        self.assertTrue(editor.is_ordinary_small_charm_row(valid))
        for changed in (
            {"key": "charms_loaded_dice"},
            {"kind": "unique"},
            {"cls": 9},
            {"sub": 1},
            {"b": -1},
            {"b": 20},
            {"b": True},
            {"cls": True},
            {"sub": True},
        ):
            row = dict(valid)
            row.update(changed)
            with self.subTest(changed=changed):
                self.assertFalse(editor.is_ordinary_small_charm_row(row))

    def test_all_twenty_addresses_generate_native_random_hss_shape_during_mismatch(self):
        with patch.object(editor, "ROLL_DB", UnavailableRollDatabase()):
            for row in small_charm_rows():
                with self.subTest(base=row["b"]):
                    self.assertIsNone(editor.generation_roll_profile(row))
                    generated = editor.make_data(row)
                    self.assertEqual(set(generated), {"w", "a", "j", "b", "c", "o"})
                    self.assertEqual(generated["b"], float(row["b"]))
                    self.assertEqual(generated["c"], 0.0)
                    self.assertEqual(generated["j"], 0.0)
                    self.assertEqual(generated["o"], 1.0)
                    self.assertGreaterEqual(generated["a"], editor.SEED_MIN)
                    self.assertLessEqual(generated["a"], editor.SEED_MAX)
                    for invented in (
                        "tier", "grade", "appearance", "rarity", "affix", "skill"
                    ):
                        self.assertNotIn(invented, generated)

    def test_build_specific_unique_charm_and_both_dice_stay_blocked(self):
        rows = [
            next(
                row for row in editor.CAT
                if row.get("kind") == "unique"
                and row.get("cls") == 10
                and row.get("available", True)
                and row.get("key") not in {
                    "charms_loaded_dice", "charms_overloaded_dice"
                }
            ),
            next(row for row in editor.CAT if row.get("key") == "charms_loaded_dice"),
            next(row for row in editor.CAT if row.get("key") == "charms_overloaded_dice"),
        ]
        with patch.object(editor, "ROLL_DB", UnavailableRollDatabase()):
            for row in rows:
                with self.subTest(item=row["name"]):
                    with self.assertRaisesRegex(ValueError, "test build mismatch"):
                        editor.generation_roll_profile(row)

    def test_skill_target_cannot_be_injected_into_ordinary_small_charm(self):
        row = small_charm_rows()[0]
        with self.assertRaisesRegex(
            editor.DiceSkillValidationError,
            "does not support skill targeting",
        ):
            editor.dice_target_for_row(row, 1)

    def test_resolve_and_safe_tooltip_do_not_claim_grade_or_rolled_rarity(self):
        row = small_charm_rows()[18]
        raw = {
            "w": 1.0, "a": 361_225_666.0, "j": 0.0,
            "b": 18.0, "c": 0.0, "o": 1.0,
        }
        before = copy.deepcopy(raw)
        resolved = editor.resolve("0-0-1234567890123-10", raw)
        self.assertEqual(raw, before)
        self.assertEqual(resolved["name"], "Small Charm")
        self.assertEqual(resolved["rar"], "Normal")
        for removed in ("tier", "appearance", "variantLabel", "grade"):
            self.assertNotIn(removed, resolved)

        tooltip = editor._catalog_only_tooltip_model(resolved, row)
        self.assertEqual(tooltip["item"]["rarity"], "Unresolved")
        self.assertFalse(tooltip["item"]["rolledRarityKnown"])
        self.assertIsNone(tooltip["item"]["tier"])
        self.assertNotIn("appearance", tooltip["item"])
        self.assertNotIn("variantLabel", tooltip["item"])
        self.assertIn(
            "generated_affixes_unmodelled:small_charm",
            tooltip["calculation"]["unsupportedPaths"],
        )

    def test_add_endpoint_writes_small_charm_even_when_roll_database_is_blocked(self):
        row = small_charm_rows()[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stash_path = root / "stash.hss"
            stash_path.write_text(
                editor.encode_hss(json.dumps({"stash_tab_1": {}, "stash_tab_data": {}})),
                encoding="ascii",
            )
            with (
                patch.object(editor, "SAVES", root),
                patch.object(editor, "ROLL_DB", UnavailableRollDatabase()),
                patch.object(editor, "game_running", return_value=False),
                patch.object(editor, "INSTANCE_GUARD_ACTIVE", False),
            ):
                result = editor.op_add({
                    "cid": row["id"],
                    "target": {"type": "stash", "tab": "stash_tab_1"},
                })
                self.assertIn("ok", result)
                saved = json.loads(editor.decode_hss(stash_path))

        self.assertEqual(len(saved["stash_tab_1"]), 1)
        entry = next(iter(saved["stash_tab_1"].values()))
        self.assertEqual(set(entry["data"]), {"w", "a", "j", "b", "c", "o"})
        self.assertEqual(entry["data"]["b"], float(row["b"]))

    def test_embedded_ui_has_random_roll_but_no_grade_or_appearance_controls(self):
        html = editor.HTML
        for removed in (
            "SMALL_CHARM_TIERS",
            "smallCharmCatalogRow",
            'id="charmgradeselect"',
            'id="charmappearanceselect"',
            "NATIVE GRADE",
            "Add · NATIVE RANDOM ROLL",
            "variantLabel",
        ):
            self.assertNotIn(removed, html)
        self.assertIn("Add · RANDOM ROLL", html)
        self.assertIn("ROLLED RARITY &amp; AFFIXES: NATIVE RANDOM", html)


if __name__ == "__main__":
    unittest.main()
