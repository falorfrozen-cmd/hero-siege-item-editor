import unittest
from pathlib import Path

try:
    from HSItemEditor import torch_class_selector as subject
    from HSItemEditor import dice_skill_selector
except ModuleNotFoundError:
    import torch_class_selector as subject
    import dice_skill_selector


class TorchClassSelectorTests(unittest.TestCase):
    def test_all_24_class_seeds_replay_with_every_variable_stat_maxed(self):
        database = subject.load_torch_class_database()
        self.assertTrue(database.available, database.status.message)
        targets = database.targets(subject.TORCH_PROFILE_ID)
        self.assertEqual([row["id"] for row in targets], list(range(1, 25)))
        self.assertEqual(len({row["seed"] for row in targets}), 24)
        for target in targets:
            with self.subTest(class_id=target["id"], name=target["name"]):
                self.assertEqual(target["seed"], subject.CLASS_SEEDS[target["id"]])
                replay = subject.replay_torch_seed(target["seed"])
                self.assertEqual(replay.variable_rolls, (10, 2, 10, 2))
                self.assertTrue(replay.all_variable_stats_max)
                self.assertEqual(replay.class_id, target["id"])
                self.assertEqual(replay.socket_count, 2)

    def test_known_classes_and_current_seed_decode(self):
        database = subject.load_torch_class_database()
        pirate = database.target(subject.TORCH_PROFILE_ID, 4)
        stormweaver = database.target(subject.TORCH_PROFILE_ID, 22)
        self.assertEqual((pirate["name"], pirate["seed"]), ("Pirate", 331_867))
        self.assertEqual(
            (stormweaver["name"], stormweaver["seed"]),
            ("Stormweaver", 326_164),
        )
        selector = database.selector(subject.TORCH_PROFILE_ID, pirate["seed"])
        self.assertEqual(selector["targetKind"], "class")
        self.assertEqual(selector["maxSockets"], 2)
        self.assertEqual(selector["current"]["id"], 4)
        self.assertEqual(selector["current"]["name"], "Pirate")
        self.assertEqual(subject.CLASS_NAMES[18], "Jötunn")
        self.assertEqual(subject.CLASS_NAMES[19], "Illusionist")

    def test_exact_address_mapping_and_non_torch_rejection(self):
        self.assertEqual(
            subject.profile_id_for_address("unique", 10, 0, 23),
            subject.TORCH_PROFILE_ID,
        )
        for address in (
            ("normal", 10, 0, 23),
            ("unique", 10, 0, 31),
            ("unique", 10, 1, 23),
            ("unique", 3, 13, 23),
        ):
            with self.subTest(address=address):
                self.assertIsNone(subject.profile_id_for_address(*address))
        database = subject.load_torch_class_database()
        with self.assertRaisesRegex(
            subject.TorchClassValidationError,
            "does not support Torch class targeting",
        ):
            database.target("unique:10:0:31", 4)

    def test_only_proven_705_and_706_clean_hashes_are_accepted(self):
        self.assertTrue(
            subject.supports_executable_sha256(subject.SEASON_10_705_EXE_SHA256)
        )
        self.assertTrue(
            subject.supports_executable_sha256(
                subject.SEASON_10_COMPATIBLE_EXE_SHA256.lower()
            )
        )
        self.assertFalse(subject.supports_executable_sha256("0" * 64))
        self.assertFalse(subject.supports_executable_sha256(None))

    def test_runtime_build_mismatch_disables_every_class_target(self):
        database = subject.load_torch_class_database(
            runtime_build_check=lambda: "installed game hash changed"
        )
        self.assertFalse(database.available)
        self.assertEqual(database.targets(subject.TORCH_PROFILE_ID), [])
        selector = database.selector(subject.TORCH_PROFILE_ID)
        self.assertFalse(selector["available"])
        self.assertEqual(selector["targetKind"], "class")
        self.assertEqual(database.summary()["code"], "game_build_unverified")
        with self.assertRaisesRegex(
            subject.TorchClassValidationError, "hash changed"
        ):
            database.target(subject.TORCH_PROFILE_ID, 4)

    def test_invalid_class_ids_fail_closed(self):
        database = subject.load_torch_class_database()
        for invalid in (None, True, 0, 25, 1.5, "pirate"):
            with self.subTest(invalid=invalid), self.assertRaises(
                subject.TorchClassValidationError
            ):
                database.target(subject.TORCH_PROFILE_ID, invalid)

    def test_706_torch_support_does_not_unlock_dice_database(self):
        database = dice_skill_selector.load_dice_skill_database(
            Path(__file__).parent,
            runtime_build_check=lambda: (
                "Installed Hero_Siege.exe does not match Dice's proven build "
                f"(detected {subject.SEASON_10_COMPATIBLE_EXE_SHA256})"
            ),
        )
        self.assertFalse(database.available)
        self.assertEqual(
            database.targets(dice_skill_selector.LOADED_PROFILE_ID), []
        )
        self.assertEqual(database.summary()["code"], "game_build_unverified")


if __name__ == "__main__":
    unittest.main()
