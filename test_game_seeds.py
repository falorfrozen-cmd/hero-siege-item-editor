"""Game-built seeds (hs_game_seeds.json): what Item Editor 2.16.3 writes for white
bases, runewords and uniques, and how the socket editor follows the game's rule.

The table itself was measured in the running game through ForgePact's Item Truth
evaluation requests (GAME_TRUTH_DESIGN.md, step 4); these tests pin how the editor
uses it, without a game.
"""
import base64
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_game_seed_tests", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)

TABLE = json.loads(MODULE_PATH.with_name("hs_game_seeds.json").read_text(encoding="utf-8"))


def catalog(**match):
    return next(
        row for row in editor.CAT
        if all(row.get(key) == value for key, value in match.items())
    )


class GameSeedTableTests(unittest.TestCase):
    def test_table_is_measured_on_a_named_build_and_covers_every_white_base_but_jewelry(self):
        self.assertEqual(TABLE["schemaVersion"], 1)
        self.assertTrue(TABLE["build"].startswith("pe-"))
        covered = {address for address in TABLE["white"]}
        for row in editor.CAT:
            if row.get("kind") != "normal" or not row.get("available", True):
                continue
            if int(row["cls"]) not in range(0, 9):
                continue
            address = editor.game_seed_address(row)
            with self.subTest(address=address, name=row["name"]):
                if int(row["cls"]) in (5, 7):
                    # amulets and rings never came out Common in the game
                    self.assertNotIn(address, covered)
                else:
                    self.assertIn(address, covered)
                    self.assertTrue(TABLE["white"][address]["seeds"])

    def test_every_socket_table_unique_has_a_game_count(self):
        for row in editor.CAT:
            if editor.socket_roll_for_profile(row.get("rollProfile")) is None:
                continue
            with self.subTest(name=row["name"]):
                self.assertIsNotNone(editor.unique_seed_entry(row))

    def test_missing_or_damaged_table_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            old = (editor.BASE, editor._GAME_SEED_DOCUMENT)
            try:
                editor.BASE = Path(folder)
                editor._GAME_SEED_DOCUMENT = None
                with self.assertRaisesRegex(RuntimeError, "game seed table unavailable"):
                    editor._load_game_seed_document()
                damaged = copy.deepcopy(TABLE)
                first = next(iter(damaged["white"]))
                damaged["white"][first]["seeds"][0]["seed"] = 0
                Path(folder, "hs_game_seeds.json").write_text(json.dumps(damaged), encoding="utf-8")
                editor._GAME_SEED_DOCUMENT = None
                with self.assertRaisesRegex(RuntimeError, "invalid seed choice"):
                    editor._load_game_seed_document()
                damaged = copy.deepcopy(TABLE)
                damaged["runewordBlocked"] = {"x": ["3:1:2"]}
                Path(folder, "hs_game_seeds.json").write_text(json.dumps(damaged), encoding="utf-8")
                editor._GAME_SEED_DOCUMENT = None
                with self.assertRaisesRegex(RuntimeError, "runeword block list"):
                    editor._load_game_seed_document()
            finally:
                editor.BASE, editor._GAME_SEED_DOCUMENT = old

    def test_release_bundles_the_table(self):
        spec = MODULE_PATH.with_name("HeroSiegeItemEditor.spec").read_text(encoding="utf-8")
        self.assertIn('"hs_game_seeds.json"', spec)


class GameSeedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saves = Path(self.temp.name)
        self.old_saves = editor.SAVES
        editor.SAVES = self.saves
        self.patches = [
            patch.object(editor, "game_running", return_value=False),
            # the local Custom Forge store lives here, never in the user's folder
            patch.object(editor, "ROOT", self.saves),
        ]
        for item in self.patches:
            item.start()
        self.royal = catalog(kind="normal", name="Royal Shield")
        self.royal_seeds = [choice["seed"] for choice in editor.white_seed_entry(self.royal)["seeds"]]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        editor.SAVES = self.old_saves
        self.temp.cleanup()

    def test_white_base_takes_the_best_common_seed_and_no_socket_claim(self):
        data = editor.make_data(self.royal)
        # the profile's own seed 370696 builds a Rare shield on this game build
        self.assertEqual(data["a"], float(self.royal_seeds[0]))
        self.assertNotEqual(data["a"], 370696.0)
        self.assertNotIn("zz", data)
        profile = editor.effective_roll_profile(self.royal["rollProfile"])
        self.assertEqual(profile["gameSeed"]["seed"], self.royal_seeds[0])
        self.assertIn("Common, game-verified", profile["detail"])
        self.assertEqual(profile["maxSockets"], TABLE["white"]["6:0:15"]["maxSockets"])

    def test_a_seed_a_custom_forge_item_owns_is_skipped(self):
        owned = {(6.0, float(self.royal_seeds[0]))}

        def claims(cls, data):
            return (float(cls), float(data["a"])) in owned

        with patch.object(editor, "_custom_forge_claims", side_effect=claims):
            self.assertEqual(editor.make_data(self.royal)["a"], float(self.royal_seeds[1]))
        with patch.object(editor, "_custom_forge_claims", return_value=True):
            with self.assertRaisesRegex(ValueError, "taken by a Custom Forge item"):
                editor.make_data(self.royal)

    def test_great_helm_no_longer_shares_the_miners_helmet_seed(self):
        helm = catalog(kind="normal", cls=0, b=7)
        self.assertNotEqual(editor.make_data(helm)["a"], 332620.0)

    def test_runeword_takes_the_bases_common_seed_and_the_profiles_i(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        base = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) == (3, 1, 17)
        )
        profile = editor.runeword_profile(recipe, base)
        seeds = editor.preferred_runeword_seeds(recipe, base, profile)
        self.assertEqual(seeds["a"], float(editor.white_seed_choice(dict(base, kind="normal"))["seed"]))
        self.assertEqual(seeds["i"], editor.roll_profile_field_seeds(profile)["i"])

    def test_a_base_the_game_never_forms_the_runeword_on_is_closed(self):
        recipe_id, addresses = next(iter(TABLE["runewordBlocked"].items()))
        recipe = next(row for row in editor.RUNEWORDS if str(row["rw"]) == recipe_id)
        row = next(
            row for row in editor.runeword_api_rows() if row["rw"] == recipe["rw"]
        )
        blocked = [base for base in row["bases"]
                   if f"{base['cls']}:{base['sub'] if base['cls'] == 3 else 0}:{base['b']}" in addresses]
        self.assertTrue(blocked)
        for base in blocked:
            self.assertFalse(base["available"])
            self.assertIn("does not form this runeword", base["unavailableReason"])
        (self.saves / "stash.hss").write_text(
            editor.encode_hss(json.dumps({"stash_tab_1": {}})), encoding="ascii"
        )
        result = editor.op_forge({"rw": recipe["rw"], "baseCid": blocked[0]["cid"], "tab": "stash_tab_1"})
        self.assertIn("does not form this runeword", result["err"])

    def test_unique_writes_the_count_the_game_gives(self):
        gown = catalog(key="armors_zephys_gown")
        data = editor.make_data(gown)
        entry = editor.unique_seed_entry(gown)
        self.assertEqual(data["a"], float(entry["seed"]))
        # the older build's socket table claimed 4; the game gives 3
        self.assertEqual(data["zz"], {"sockets": 3.0})


class GameSeedEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saves = Path(self.temp.name)
        self.old_saves = editor.SAVES
        editor.SAVES = self.saves
        self.patches = [
            patch.object(editor, "game_running", return_value=False),
            patch.object(editor, "ROOT", self.saves),
            # no Item Truth record unless a test gives one
            patch.object(editor, "_game_socket_count", return_value=None),
        ]
        for item in self.patches:
            item.start()
        self.royal = catalog(kind="normal", name="Royal Shield")
        self.seeds = [choice["seed"] for choice in editor.white_seed_entry(self.royal)["seeds"]]
        self.target = {"type": "stash", "tab": "stash_tab_1"}

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        editor.SAVES = self.old_saves
        self.temp.cleanup()

    def _store(self, data, cls=6, key="0-0-1234567890123-6"):
        self.key = key
        stash = {"stash_tab_1": {key: {"pos": [0.0, 0.0], "data": data}}}
        (self.saves / "stash.hss").write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")

    def _data(self):
        stash = json.loads(editor.decode_hss(self.saves / "stash.hss"))
        return stash["stash_tab_1"][self.key]["data"]

    def _modify(self, action):
        return editor.op_modify({"action": action, "target": self.target, "key": self.key})

    def _sockets(self, sockets):
        return editor.op_sockets({"target": self.target, "key": self.key, "sockets": sockets})

    def test_perfect_on_a_white_base_writes_the_best_common_seed_and_keeps_sockets(self):
        self._store({"w": 1.0, "a": 370696.0, "j": 0.0, "b": 15.0, "c": 0.0, "o": 1.0, "zz": {"sockets": 3.0}})
        result = self._modify("perfect")
        self.assertIn("best game-verified Common roll applied", result["ok"])
        data = self._data()
        self.assertEqual(data["a"], float(self.seeds[0]))
        self.assertEqual(data["zz"], {"sockets": 3.0})
        again = self._modify("perfect")
        self.assertIn("already the best game-verified Common roll", again["ok"])

    def test_reroll_on_an_editor_white_base_stays_on_game_built_seeds(self):
        self._store({"w": 1.0, "a": float(self.seeds[0]), "j": 0.0, "b": 15.0, "c": 0.0, "o": 1.0})
        result = self._modify("reroll")
        self.assertIn("rerolled to another game-verified Common seed", result["ok"])
        self.assertIn(self._data()["a"], [float(seed) for seed in self.seeds[1:]])

    def test_reroll_on_a_dropped_item_keeps_random_seeds(self):
        self._store({"w": 1.0, "a": 123456789.0, "j": 0.0, "b": 15.0, "c": 0.0, "o": 1.0})
        with patch.object(editor, "random_item_seed", return_value=987654.0):
            result = self._modify("reroll")
        self.assertIn("stats rerolled", result["ok"])
        self.assertEqual(self._data()["a"], 987654.0)

    def test_white_base_socket_count_is_zz_up_to_the_natural_maximum(self):
        self._store({"w": 1.0, "a": float(self.seeds[0]), "j": 0.0, "b": 15.0, "c": 0.0, "o": 1.0})
        limit = TABLE["white"]["6:0:15"]["maxSockets"]
        result = self._sockets([None] * limit)
        self.assertIn("ok", result)
        data = self._data()
        self.assertEqual(data["zz"], {"sockets": float(limit)})
        self.assertEqual(data["a"], float(self.seeds[0]))
        too_many = self._sockets([None] * (limit + 1))
        self.assertIn(f"maximum {limit} sockets", too_many["err"])

    def test_unique_keeps_the_count_its_seed_rolls(self):
        gown = catalog(key="armors_zephys_gown")
        data = editor.make_data(gown)
        self._store(data, cls=1, key="0-0-1234567890123-1")
        fewer = self._sockets([None, None])
        self.assertIn("never reads a saved socket count on a unique", fewer["err"])
        gem = next(int(row["b"]) for row in editor.CAT
                   if row.get("kind") == "normal" and int(row["cls"]) == 15 and row.get("available", True))
        kept = self._sockets([{"b": gem}, None, None])
        self.assertIn("ok", kept)
        self.assertEqual(self._data()["zz"], {"sockets": 3.0})

    def test_a_non_unique_item_never_shows_fewer_than_its_seed_rolls(self):
        # a dropped item on the base, recorded by the game with 4 sockets and
        # no zz.sockets: its seed rolled 4
        self._store({"w": 1.0, "a": 123456789.0, "j": 0.0, "b": 15.0, "c": 0.0, "o": 1.0})
        with patch.object(editor, "_game_socket_count", return_value=4):
            result = self._sockets([None, None])
            self.assertIn("so the game shows at least 4", result["err"])
            self.assertIn("ok", self._sockets([None] * 4))


if __name__ == "__main__":
    unittest.main()
