import json
import tempfile
import unittest
from pathlib import Path

try:
    from HSItemEditor import dice_skill_selector as subject
except ModuleNotFoundError:
    import dice_skill_selector as subject


class DiceSkillSelectorTests(unittest.TestCase):
    def test_installed_database_is_complete_and_verified(self):
        database = subject.load_dice_skill_database(Path(__file__).parent)
        self.assertTrue(database.available, database.status.message)
        self.assertEqual(database.status.skill_count, 432)
        self.assertEqual(database.status.loaded_target_count, 432)
        self.assertEqual(database.status.overloaded_target_count, 222)
        self.assertEqual(
            {row["id"] for row in database.targets(subject.LOADED_PROFILE_ID)},
            set(range(2, 434)),
        )
        self.assertEqual(
            {row["id"] for row in database.targets(subject.OVERLOADED_PROFILE_ID)},
            set(subject.generated_pool_model.OVERLOADED_VALID_SUBSKILL_IDS),
        )

    def test_known_loaded_targets_use_global_smallest_first_hit_seeds(self):
        database = subject.load_dice_skill_database(Path(__file__).parent)
        expected = {
            2: 3,
            31: 86_667,
            55: 158_856,
            433: 429_565,
        }
        for skill_id, seed in expected.items():
            with self.subTest(skill_id=skill_id):
                target = database.target(subject.LOADED_PROFILE_ID, skill_id)
                self.assertEqual(target["seed"], seed)
                self.assertEqual(subject.loaded_selected_skill_id(seed), skill_id)

    def test_known_overloaded_targets_follow_rejection_loop(self):
        database = subject.load_dice_skill_database(Path(__file__).parent)
        expected = {7: 558, 55: 59, 148: 1, 433: 93}
        for skill_id, seed in expected.items():
            with self.subTest(skill_id=skill_id):
                target = database.target(subject.OVERLOADED_PROFILE_ID, skill_id)
                self.assertEqual(target["seed"], seed)
                self.assertEqual(subject.overloaded_selected_skill_id(seed), skill_id)

    def test_selectors_decode_current_saved_seed(self):
        database = subject.load_dice_skill_database(Path(__file__).parent)
        loaded = database.selector(subject.LOADED_PROFILE_ID, 86_667.0)
        overloaded = database.selector(subject.OVERLOADED_PROFILE_ID, 59.0)
        self.assertEqual(loaded["current"]["name"], "Meteor")
        self.assertEqual(overloaded["current"]["name"], "Gunner Drone")

    def test_overloaded_rejects_a_non_subskill_identity(self):
        database = subject.load_dice_skill_database(Path(__file__).parent)
        with self.assertRaisesRegex(subject.DiceSkillValidationError, "not a valid subskill"):
            database.target(subject.OVERLOADED_PROFILE_ID, 31)

    def test_profile_address_mapping_does_not_confuse_chaos_gemstone(self):
        self.assertEqual(
            subject.profile_id_for_address("unique", 10, 0, 31),
            subject.LOADED_PROFILE_ID,
        )
        self.assertEqual(
            subject.profile_id_for_address("unique", 10, 0, 89),
            subject.OVERLOADED_PROFILE_ID,
        )
        self.assertIsNone(subject.profile_id_for_address("unique", 10, 0, 70))

    def test_missing_and_modified_assets_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = subject.load_dice_skill_database(directory)
            self.assertFalse(missing.available)
            self.assertEqual(missing.status.code, "missing")

            source = Path(__file__).with_name("hs_dice_skill_targets.json")
            document = json.loads(source.read_text(encoding="utf-8"))
            document["profiles"][subject.LOADED_PROFILE_ID]["targets"]["31"] += 1
            target = Path(directory) / source.name
            target.write_text(json.dumps(document), encoding="utf-8")
            modified = subject.load_dice_skill_database(directory)
            self.assertFalse(modified.available)
            self.assertEqual(modified.status.code, "invalid")
            self.assertIn("SHA-256 mismatch", modified.status.message)
            self.assertEqual(modified.targets(subject.LOADED_PROFILE_ID), [])

    def test_runtime_game_build_mismatch_disables_every_target(self):
        database = subject.load_dice_skill_database(
            Path(__file__).parent,
            runtime_build_check=lambda: "installed game hash changed",
        )
        self.assertFalse(database.available)
        self.assertEqual(database.targets(subject.LOADED_PROFILE_ID), [])
        self.assertEqual(database.summary()["code"], "game_build_unverified")
        selector = database.selector(subject.LOADED_PROFILE_ID)
        self.assertFalse(selector["available"])
        self.assertIn("hash changed", selector["message"])
        with self.assertRaisesRegex(
            subject.DiceSkillValidationError, "hash changed"
        ):
            database.target(subject.LOADED_PROFILE_ID, 31)


if __name__ == "__main__":
    unittest.main()
