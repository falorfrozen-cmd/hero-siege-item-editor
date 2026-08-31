import base64
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_gui", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


def catalog_row(key: str) -> dict:
    return next(row for row in editor.CAT if row.get("key") == key)


def roll_profile(
    profile_id,
    kind,
    name,
    mode,
    field_seeds,
    maxed,
    total,
    deficit,
    *,
    max_sockets=None,
):
    return {
        "addressKey": profile_id,
        "kind": kind,
        "name": name,
        "sourceKey": "test_fixture",
        "maxSockets": max_sockets,
        "mode": mode,
        "maxed": maxed,
        "total": total,
        "endpointDeficit": deficit,
        "fieldSeeds": dict(field_seeds),
        "chains": {
            field: {"seed": int(seed)} for field, seed in field_seeds.items()
        },
        "detail": (
            "No variable definition-stat rolls"
            if mode == "fixed"
            else f"{maxed}/{total} variable stats MAX"
            + (f"; minimum total deficit {deficit}" if deficit else "")
        ),
    }


class FixtureRollDatabase:
    def __init__(self):
        direct = [
            roll_profile(
                "unique:0:0:90", "unique", "Leviathan's Crown", "exact",
                {"a": 271_828}, 1, 1, 0,
            ),
            roll_profile(
                "unique:1:0:52", "unique", "Zephy's Gown", "exact",
                {"a": 3_888_156}, 6, 6, 0,
            ),
            roll_profile(
                "unique:3:3:18", "unique", "The Dawn Bringer", "best",
                {"a": 17_234_404}, 8, 9, 9,
            ),
            roll_profile(
                "unique:3:13:2", "unique", "Poison Ivy", "best",
                {"a": 4_677_950}, 6, 7, 1, max_sockets=4,
            ),
            roll_profile(
                "unique:7:0:61", "unique", "Parasite Loop", "exact",
                {"a": 314_560}, 3, 3, 0,
            ),
            roll_profile(
                "unique:10:0:31", "unique", "Loaded Dice", "exact",
                {"a": 429_565}, 1, 1, 0,
            ),
            roll_profile(
                "unique:10:0:89", "unique", "Overloaded Dice", "fixed",
                {}, 0, 0, 0,
            ),
        ]
        runeword = roll_profile(
            "runeword:1|normal:3:1:17", "runeword",
            "Breath of the Damned", "exact",
            {"a": 356_137, "i": 424_123}, 4, 4, 0,
        )
        self.profiles = {
            profile["addressKey"]: profile for profile in [*direct, runeword]
        }
        self.available = True
        self.status = type("Status", (), {"message": "fixture database ready"})()

    def lookup(self, kind, cls, sub, base):
        return self.profiles.get(f"{kind}:{int(cls)}:{int(sub)}:{int(base)}")

    def lookup_runeword(self, runeword, cls, sub, base):
        return self.profiles.get(
            f"runeword:{int(runeword)}|normal:{int(cls)}:{int(sub)}:{int(base)}"
        )

    def summary(self):
        return {"available": True, "profileCount": len(self.profiles)}


class ItemEditorSeason10Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saves = Path(self.temp.name)
        self.old_saves = editor.SAVES
        self.old_loadouts_file = editor.LOADOUTS_FILE
        self.old_build_export_dir = editor.BUILD_EXPORT_DIR
        self.old_roll_db = editor.ROLL_DB
        self.old_dice_db = editor.DICE_SKILL_DB
        self.old_torch_db = editor.TORCH_CLASS_DB
        self.old_catalog_profiles = [row.get("rollProfile") for row in editor.CAT]
        self.old_catalog_selectors = [row.get("skillSelector") for row in editor.CAT]
        editor.SAVES = self.saves
        editor.LOADOUTS_FILE = self.saves / "hs_loadouts.json"
        editor.BUILD_EXPORT_DIR = self.saves / "exports"
        editor.ROLL_DB = FixtureRollDatabase()
        # Unit fixtures exercise the immutable proven-build Dice database and
        # must not depend on whichever Hero Siege patch is installed locally.
        # Runtime mismatch behavior is covered explicitly in its own test.
        editor.DICE_SKILL_DB = editor.load_dice_skill_database(editor.BASE)
        editor.TORCH_CLASS_DB = editor.load_torch_class_database()
        for row in editor.CAT:
            row.pop("rollProfile", None)
            profile = editor.catalog_roll_profile(row)
            if profile:
                row["rollProfile"] = profile
            row.pop("skillSelector", None)
            target_profile_id = editor.catalog_skill_profile_id(row)
            if target_profile_id:
                database = editor.skill_target_database(target_profile_id)
                row["skillSelector"] = database.selector(target_profile_id)

        stash = {
            "stash_tab_1": {},
            "material_tab_1": {},
            "socket_tab_1": {},
            "unique_items": {},
            "stash_tab_data": {},
        }
        bags = {
            "inventory_tab_0": {},
            "inventory_key_tab": {},
            "inventory_material_tab": {},
            "inventory_socket_tab": {},
            "inventory_relic_tab": {},
            "inventory_tarot_tab": {},
            "inventory_vault_tab": {},
            "inventory_vault_active_0": {},
        }
        inventory = {"equipped_items": {}, "potions": {}, "personal_stash": {}}
        blob = base64.b64encode(json.dumps(inventory).encode()).decode()
        char_text = (
            '[character]\nname="S10 Test"\nclass="1"\nlevel="100"\n'
            f'inventory="{blob}"\n' + ('padding=1\n' * 150)
        )
        (self.saves / "stash.hss").write_text(
            editor.encode_hss(json.dumps(stash)), encoding="ascii"
        )
        (self.saves / "inventory_order_1.hss").write_text(
            editor.encode_hss(json.dumps(bags)), encoding="ascii"
        )
        (self.saves / "herosiege1.hss").write_text(
            editor.encode_hss(char_text), encoding="ascii"
        )

        self.game_patch = patch.object(editor, "game_running", return_value=False)
        self.game_patch.start()

    def tearDown(self):
        self.game_patch.stop()
        editor.SAVES = self.old_saves
        editor.LOADOUTS_FILE = self.old_loadouts_file
        editor.BUILD_EXPORT_DIR = self.old_build_export_dir
        editor.ROLL_DB = self.old_roll_db
        editor.DICE_SKILL_DB = self.old_dice_db
        editor.TORCH_CLASS_DB = self.old_torch_db
        for row, profile, selector in zip(
            editor.CAT, self.old_catalog_profiles, self.old_catalog_selectors
        ):
            if profile is None:
                row.pop("rollProfile", None)
            else:
                row["rollProfile"] = profile
            if selector is None:
                row.pop("skillSelector", None)
            else:
                row["skillSelector"] = selector
        self.temp.cleanup()

    def test_codec_round_trip_and_corruption_rejection(self):
        source = 'name="Tarethiel"\ninventory="e30="\n'
        encoded = editor.encode_hss(source)
        path = self.saves / "roundtrip.hss"
        path.write_text(encoded, encoding="ascii")
        self.assertEqual(editor.decode_hss(path), source)
        path.write_text(encoded[:-2] + "!!", encoding="ascii")
        with self.assertRaises(Exception):
            editor.decode_hss(path)

    def test_s10_unique_overlay_is_complete_and_old_rows_are_locked(self):
        rows = [r for r in editor.CAT if r.get("kind") == "unique" and r.get("s10Verified")]
        self.assertEqual(len(rows), 24)
        self.assertEqual(
            (catalog_row("w_bow_phantom_strike")["cls"],
             catalog_row("w_bow_phantom_strike")["sub"],
             catalog_row("w_bow_phantom_strike")["b"]),
            (3, 13, 20),
        )
        self.assertFalse(catalog_row("w_throwing_darkmoon_deck")["available"])
        self.assertFalse(catalog_row("w_universal_cheated_item")["available"])
        self.assertFalse(catalog_row("vault_superior_essence_vault")["available"])

    def test_s10_normal_overlay_addresses_match_current_repository(self):
        expected = {
            "keys_boreal_key": (12, 36),
            "keys_parasitic_key": (12, 37),
            "keys_treasure_key": (12, 38),
            "keys_hive_key": (12, 39),
            "keys_aztec_key": (12, 40),
            "keys_tablet_of_leviathan": (12, 41),
            "keys_tablet_of_armada": (12, 42),
            "keys_tablet_of_parasite": (12, 43),
            "material_blacksmiths_mallet_fragment": (14, 71),
            "material_gypsys_prophecy_fragment": (14, 72),
            "material_satanic_dice_fragment": (14, 73),
            "socketable_gem_cthulhu": (15, 136),
            "socketable_gem_of_incarnation": (15, 137),
        }
        actual = {
            row["key"]: (row["cls"], row["b"])
            for row in editor.CAT
            if row.get("kind") == "normal" and row.get("s10Verified")
        }
        self.assertEqual(actual, expected)

    def test_s10_inventory_tabs_have_expected_dimensions(self):
        for tab in ("inventory_relic_tab", "inventory_tarot_tab",
                    "inventory_vault_tab", "inventory_future_tab"):
            self.assertEqual(editor.grid_dims(tab), (15, 6))
        self.assertEqual(editor.grid_dims("stash_tab_20"), (17, 18))

    def test_add_new_unique_to_stash_and_potion_belt(self):
        crown = catalog_row("helmet_leviathans_crown")
        result = editor.op_add({"cid": crown["id"], "target": {"type": "stash_unique"}})
        self.assertIn("ok", result)
        stash = json.loads(editor.decode_hss(self.saves / "stash.hss"))
        created = next(iter(stash["unique_items"].values()))["data"]
        self.assertEqual((created["c"], created["b"]), (1.0, 90.0))
        self.assertGreaterEqual(created["a"], editor.SEED_MIN)
        self.assertLessEqual(created["a"], editor.SEED_MAX)

        flask = catalog_row("consumable_leviathans_blood")
        result = editor.op_add({"cid": flask["id"],
                                "target": {"type": "potions", "slot": 1}})
        self.assertIn("ok", result)
        char_text = editor.decode_hss(self.saves / "herosiege1.hss")
        match = re.search(r'inventory="([A-Za-z0-9+/=]+)"', char_text)
        inventory = json.loads(base64.b64decode(match.group(1)))
        created = next(iter(inventory["potions"].values()))["data"]
        self.assertEqual((created["c"], created["b"], created["m"]),
                         (1.0, 22.0, 1.0))

    def test_verified_exact_roll_changes_only_main_seed(self):
        gown = catalog_row("armors_zephys_gown")
        editor.op_add({"cid": gown["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        bags_path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(bags_path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        entry["data"].update({
            "a": 1.0,
            "i": 222.0,
            "s": 333.0,
            "zz": {"sockets": 4.0},
        })
        bags_path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })

        self.assertIn("ok", result)
        updated = json.loads(editor.decode_hss(bags_path))
        data = updated["inventory_tab_0"][key]["data"]
        profile = gown["rollProfile"]
        self.assertEqual(data["a"], float(profile["fieldSeeds"]["a"]))
        self.assertIn("EXACT MAX", result["ok"])
        self.assertEqual(data["i"], 222.0)
        self.assertEqual(data["s"], 333.0)
        self.assertEqual(data["zz"], {"sockets": 4.0})

    def test_equipped_perfect_roll_is_an_idempotent_noop(self):
        gown = catalog_row("armors_zephys_gown")
        target = {"type": "equip", "slot": 1, "g": 1}
        result = editor.op_add({"cid": gown["id"], "target": target})
        self.assertIn("ok", result)

        char_path = self.saves / "herosiege1.hss"
        char_before = char_path.read_bytes()
        backups_before = set(self.saves.glob("herosiege1.hss.guibak_*"))
        inventory = editor._decode_character_inventory(char_path)[1]
        key = next(iter(inventory["equipped_items"]))

        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "equipped", "slot": 1,
                       "tab": "equipped_items"},
            "key": key,
        })

        self.assertIn("already EXACT MAX", result["ok"])
        self.assertEqual(result["backup"], "")
        self.assertEqual(char_path.read_bytes(), char_before)
        self.assertEqual(
            set(self.saves.glob("herosiege1.hss.guibak_*")), backups_before
        )

    def test_dawn_bringer_uses_verified_best_possible_seed(self):
        dawn = catalog_row("w_melee_dawn_bringer")
        editor.op_add({"cid": dawn["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        bags_path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(bags_path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        profile = dawn["rollProfile"]
        self.assertEqual(entry["data"]["a"], float(profile["fieldSeeds"]["a"]))

        entry["data"]["a"] = 1.0
        bags_path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })
        updated = json.loads(editor.decode_hss(bags_path))
        self.assertEqual(updated["inventory_tab_0"][key]["data"]["a"],
                         float(profile["fieldSeeds"]["a"]))
        self.assertIn("BEST POSSIBLE", result["ok"])
        self.assertIn("8/9 variable stats MAX", result["ok"])
        self.assertIn("minimum total deficit 9", result["ok"])

    def test_poison_ivy_generation_persists_verified_four_socket_capacity(self):
        ivy = catalog_row("w_bow_poison_ivy")

        result = editor.op_add({"cid": ivy["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})

        self.assertIn("ok", result)
        bags = json.loads(editor.decode_hss(self.saves / "inventory_order_1.hss"))
        data = next(iter(bags["inventory_tab_0"].values()))["data"]
        self.assertEqual(data["a"], 4_677_950.0)
        self.assertEqual(data["zz"], {"sockets": 4.0})

    def test_poison_ivy_perfect_repairs_stale_one_socket_override(self):
        ivy = catalog_row("w_bow_poison_ivy")
        editor.op_add({"cid": ivy["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        socket_payload = base64.b64encode(
            json.dumps({"a": 919_191, "b": 17, "n": 4}).encode()
        ).decode()
        entry["data"].update({
            "a": 4_677_950.0,
            "s1": socket_payload,
            "zz": {"sockets": 1.0, "opaque": {"keep": True}},
        })
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })

        self.assertIn("BEST POSSIBLE", result["ok"])
        self.assertIn("zz.sockets=4", result["ok"])
        data = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]["data"]
        self.assertEqual(data["a"], 4_677_950.0)
        self.assertEqual(data["zz"], {"sockets": 4.0, "opaque": {"keep": True}})
        self.assertEqual(data["s1"], socket_payload)

    def test_poison_ivy_perfect_rejects_hidden_payload_beyond_verified_capacity(self):
        ivy = catalog_row("w_bow_poison_ivy")
        editor.op_add({"cid": ivy["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        hidden_payload = base64.b64encode(
            json.dumps({"a": 818_181, "b": 17, "n": 2}).encode()
        ).decode()
        entry["data"].update({
            "a": 1.0,
            "s5": hidden_payload,
            "zz": {"sockets": 1.0},
        })
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")
        before = path.read_bytes()
        backups_before = set(self.saves.glob("inventory_order_1.hss.guibak_*"))

        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })

        self.assertIn("s5", result["err"])
        self.assertIn("verified maximum of 4", result["err"])
        self.assertIn("Save Health Check", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("inventory_order_1.hss.guibak_*")),
            backups_before,
        )

    def test_unavailable_roll_database_leaves_item_unchanged(self):
        ring = catalog_row("rings_parasite_loop")
        editor.op_add({"cid": ring["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        bags_path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(bags_path))
        key = next(iter(bags["inventory_tab_0"]))
        before = bags_path.read_bytes()
        ring.pop("rollProfile", None)
        editor.ROLL_DB = type("UnavailableDB", (), {
            "available": False,
            "status": type("Status", (), {
                "message": "Perfect-roll profile database is not installed."
            })(),
            "lookup": lambda *args: None,
            "lookup_runeword": lambda *args: None,
        })()
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })
        self.assertIn("not installed", result["err"])
        self.assertEqual(bags_path.read_bytes(), before)

    def test_unprofiled_equipment_generation_fails_closed_and_set_add_is_atomic(self):
        supported = catalog_row("helmet_leviathans_crown")
        unsupported = catalog_row("helmet_parasite_queens_tiara")
        self.assertIn("rollProfile", supported)
        self.assertNotIn("rollProfile", unsupported)

        stash_path = self.saves / "stash.hss"
        before = stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        single = editor.op_add({
            "cid": unsupported["id"], "target": {"type": "stash_unique"},
        })
        batch = editor.op_addmany({
            "cids": [supported["id"], unsupported["id"]],
        })

        self.assertIn("no verified roll profile", single["err"])
        self.assertIn("no verified roll profile", batch["err"])
        with self.assertRaisesRegex(ValueError, "no verified roll profile"):
            editor.make_data(unsupported)
        self.assertEqual(stash_path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("stash.hss.guibak_*")), backups_before,
        )

    def test_non_regression_item_uses_its_own_exact_profile(self):
        ring = catalog_row("rings_parasite_loop")
        result = editor.op_add({"cid": ring["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        self.assertIn("ok", result)
        bags = json.loads(editor.decode_hss(self.saves / "inventory_order_1.hss"))
        data = next(iter(bags["inventory_tab_0"].values()))["data"]
        self.assertEqual(data["a"], 314_560.0)

    def test_identity_only_profile_has_no_perfect_write_or_backup(self):
        charm = catalog_row("charms_overloaded_dice")
        editor.op_add({"cid": charm["id"], "skillId": 55, "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        path = self.saves / "inventory_order_1.hss"
        before = path.read_bytes()
        backups_before = set(self.saves.glob("inventory_order_1.hss.guibak_*"))
        bags = json.loads(editor.decode_hss(path))
        key = next(iter(bags["inventory_tab_0"]))
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })
        self.assertIn("skill identity", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("inventory_order_1.hss.guibak_*")),
            backups_before,
        )

    def test_dice_catalog_uses_the_two_exact_native_addresses(self):
        loaded = catalog_row("charms_loaded_dice")
        overloaded = catalog_row("charms_overloaded_dice")
        chaos = catalog_row("charms_chaos_gemstone")

        self.assertEqual(
            loaded["skillSelector"]["profileId"], "unique:10:0:31"
        )
        self.assertEqual(
            overloaded["skillSelector"]["profileId"], "unique:10:0:89"
        )
        self.assertNotIn("skillSelector", chaos)
        self.assertTrue(loaded["skillSelector"]["available"])
        self.assertTrue(overloaded["skillSelector"]["available"])

    def test_torch_generation_requires_class_and_uses_4_of_4_max_seed(self):
        torch = catalog_row("charms_torch_of_shadows")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        path = self.saves / "inventory_order_1.hss"
        before = path.read_bytes()

        rejected = editor.op_add({"cid": torch["id"], "target": target_ref})
        self.assertIn("choose a verified class target", rejected["err"])
        self.assertEqual(path.read_bytes(), before)

        added = editor.op_add({
            "cid": torch["id"], "targetId": 4, "target": target_ref,
        })
        self.assertIn("All Skills class: Pirate", added["ok"])
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        self.assertEqual(entry["data"]["a"], 331_867.0)
        replay = editor.TORCH_CLASS_DB.target("unique:10:0:23", 4)
        self.assertEqual(replay["seed"], 331_867)
        resolved = editor.resolve(key, entry["data"])
        self.assertEqual(resolved["skillSelector"]["targetKind"], "class")
        self.assertEqual(resolved["skillSelector"]["current"]["name"], "Pirate")

    def test_existing_torch_class_change_only_replaces_a_and_keeps_metadata(self):
        torch = catalog_row("charms_torch_of_shadows")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        editor.op_add({"cid": torch["id"], "targetId": 4, "target": target_ref})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        entry["data"].update({
            "i": 202_002.0,
            "s": 303_003.0,
            "s1": "opaque-socket",
            "zz": {"sockets": 4.0, "opaque": True},
            "future": {"keep": [1, 2, 3]},
        })
        entry["pos"] = [7.0, 8.0]
        untouched_data = {
            field: value for field, value in entry["data"].items() if field != "a"
        }
        untouched_pos = list(entry["pos"])
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        changed = editor.op_modify({
            "action": "selectskill",
            "target": target_ref,
            "key": key,
            "targetId": 1,
        })
        self.assertIn("All Skills class -> Viking", changed["ok"])
        self.assertIn("4/4 stats MAX", changed["ok"])
        updated_entry = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]
        self.assertEqual(updated_entry["data"]["a"], 332_503.0)
        self.assertEqual(
            {field: value for field, value in updated_entry["data"].items() if field != "a"},
            untouched_data,
        )
        self.assertEqual(updated_entry["pos"], untouched_pos)
        self.assertEqual(
            editor.resolve(key, updated_entry["data"])["skillSelector"]["current"]["id"],
            1,
        )

    def test_existing_torch_same_class_nonmax_seed_is_replaced_by_max_seed(self):
        torch = catalog_row("charms_torch_of_shadows")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        editor.op_add({"cid": torch["id"], "targetId": 4, "target": target_ref})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        # Seed 3 also selects Pirate, but replays variable rolls (0,1,8,0),
        # so identity equality alone must not suppress the MAX-seed repair.
        entry["data"]["a"] = 3.0
        entry["data"]["future"] = {"keep": True}
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        result = editor.op_modify({
            "action": "selectskill",
            "target": target_ref,
            "key": key,
            "targetId": 4,
        })
        self.assertIn("All Skills class -> Pirate", result["ok"])
        updated = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]["data"]
        self.assertEqual(updated["a"], 331_867.0)
        self.assertEqual(updated["future"], {"keep": True})

    def test_non_torch_target_and_unique_bulk_without_torch_class_fail_closed(self):
        crown = catalog_row("helmet_leviathans_crown")
        torch = catalog_row("charms_torch_of_shadows")
        bag_path = self.saves / "inventory_order_1.hss"
        stash_path = self.saves / "stash.hss"
        bag_before = bag_path.read_bytes()
        stash_before = stash_path.read_bytes()

        non_torch = editor.op_add({
            "cid": crown["id"],
            "targetId": 4,
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
        })
        self.assertIn("does not support skill or class targeting", non_torch["err"])
        self.assertEqual(bag_path.read_bytes(), bag_before)

        with patch.object(editor, "CAT", [torch]):
            bulk = editor.op_fill_stash({"tab": "unique_items"})
        self.assertIn("explicit verified class target", bulk["err"])
        self.assertEqual(stash_path.read_bytes(), stash_before)

        with patch.object(editor, "CAT", [torch]):
            filled = editor.op_fill_stash({
                "tab": "unique_items",
                "identityTargetIds": {"unique:10:0:23": 1},
            })
        self.assertEqual((filled["added"], filled["existing"]), (1, 0))
        unique_items = json.loads(editor.decode_hss(stash_path))["unique_items"]
        self.assertEqual(len(unique_items), 1)
        self.assertEqual(next(iter(unique_items.values()))["data"]["a"], 332_503.0)

    def test_unique_fill_frontend_supplies_and_explains_torch_class_default(self):
        self.assertIn(
            'const STASH_FILL_IDENTITY_TARGETS={"unique:10:0:23":1,',
            editor.HTML,
        )
        self.assertIn(
            "body.identityTargetIds=STASH_FILL_IDENTITY_TARGETS",
            editor.HTML,
        )
        self.assertIn(
            "Torch of Shadows uses All Skills: Viking with 4/4 variable stats MAX",
            editor.HTML,
        )

    def test_torch_706_hash_override_requires_stable_build_mismatch_status(self):
        # Select the new hash independent of summary ordering.
        digest = next(value for value in editor.TORCH_CLASS_DB.summary()["supportedExeSha256"]
                      if value.startswith("2034FAD4"))

        class StubGuard:
            def __init__(self, summary):
                self._summary = summary

            def summary(self):
                return dict(self._summary)

        with patch.object(editor, "GAME_BUILD_GUARD", StubGuard({
            "matched": False,
            "code": "unstable",
            "detectedSha256": digest,
            "message": "executable changed while hashing",
        })):
            self.assertEqual(
                editor._torch_runtime_build_error(),
                "executable changed while hashing",
            )
        with patch.object(editor, "GAME_BUILD_GUARD", StubGuard({
            "matched": False,
            "code": "build_mismatch",
            "detectedSha256": digest,
            "message": "expected old hash",
        })):
            self.assertIsNone(editor._torch_runtime_build_error())

    def test_roll_706_compatibility_is_stable_and_does_not_unlock_dice(self):
        digest = next(
            value
            for value in editor.TORCH_CLASS_DB.summary()["supportedExeSha256"]
            if value.startswith("2034FAD4")
        )
        self.assertTrue(editor.roll_supports_executable_sha256(digest))

        class StubGuard:
            def __init__(self, summary):
                self._summary = summary

            def summary(self):
                return dict(self._summary)

            def error(self):
                return (
                    None
                    if self._summary.get("matched")
                    else str(self._summary.get("message") or "unverified")
                )

        with patch.object(editor, "GAME_BUILD_GUARD", StubGuard({
            "matched": False,
            "code": "unstable",
            "detectedSha256": digest,
            "message": "executable changed while hashing",
        })):
            self.assertEqual(
                editor._roll_runtime_build_error(),
                "executable changed while hashing",
            )

        with patch.object(editor, "GAME_BUILD_GUARD", StubGuard({
            "matched": False,
            "code": "build_mismatch",
            "detectedSha256": digest,
            "message": "Dice still requires its original build proof",
        })):
            self.assertIsNone(editor._roll_runtime_build_error())
            self.assertEqual(
                editor.GAME_BUILD_GUARD.error(),
                "Dice still requires its original build proof",
            )

        with patch.object(editor, "GAME_BUILD_GUARD", StubGuard({
            "matched": False,
            "code": "build_mismatch",
            "detectedSha256": "0" * 64,
            "message": "unknown executable",
        })):
            self.assertEqual(
                editor._roll_runtime_build_error(),
                "unknown executable",
            )

        compatible_status = {
            "matched": False,
            "code": "build_mismatch",
            "expectedSha256": editor.EXPECTED_GAME_EXE_SHA256,
            "detectedSha256": digest,
            "message": "source build differs",
        }
        zephy = catalog_row("armors_zephys_gown")
        promoted = editor._tooltip_runtime_build_status(zephy, compatible_status)
        self.assertTrue(promoted["matched"])
        self.assertEqual(promoted["code"], "ready_compatible")
        self.assertFalse(compatible_status["matched"])  # caller data is immutable

        for key in ("charms_loaded_dice", "charms_overloaded_dice"):
            with self.subTest(key=key):
                dice_status = editor._tooltip_runtime_build_status(
                    catalog_row(key), compatible_status
                )
                self.assertFalse(dice_status["matched"])
                self.assertEqual(dice_status["code"], "build_mismatch")

        for rejected in (
            dict(compatible_status, code="unstable"),
            dict(compatible_status, detectedSha256="0" * 64),
        ):
            with self.subTest(rejected=rejected):
                self.assertFalse(
                    editor._tooltip_runtime_build_status(zephy, rejected)["matched"]
                )

        zephy_item = editor.resolve("0-0-1-1", editor.make_data(zephy))
        zephy_item["rollProfile"] = self.old_roll_db.lookup("unique", 1, 0, 52)
        zephy_tooltip = editor._game_tooltip_model(
            zephy_item, build_status=compatible_status
        )
        self.assertTrue(zephy_tooltip["calculation"]["numbersExact"])
        self.assertEqual(zephy_tooltip["buildGuard"]["code"], "ready_compatible")

        loaded = catalog_row("charms_loaded_dice")
        loaded_item = editor.resolve(
            "0-0-2-10", {"a": 1.0, "j": 0.0, "b": 31.0, "c": 1.0}
        )
        loaded_item["rollProfile"] = self.old_roll_db.lookup("unique", 10, 0, 31)
        loaded_tooltip = editor._game_tooltip_model(
            loaded_item, build_status=compatible_status
        )
        self.assertFalse(loaded_tooltip["calculation"]["numbersExact"])
        self.assertFalse(loaded_tooltip["calculation"]["buildMatched"])
        self.assertEqual(loaded_tooltip["buildGuard"]["code"], "build_mismatch")

    def test_capability_banner_distinguishes_roll_torch_and_dice(self):
        self.assertIn("MAX/BEST READY", editor.HTML)
        self.assertIn("TORCH TARGETS READY", editor.HTML)
        self.assertIn("DICE TARGETS UNAVAILABLE", editor.HTML)
        self.assertIn("Dice targets: ${diceDb.message", editor.HTML)
        self.assertNotIn("ROLL PROFILES DISABLED", editor.HTML)

    def test_bulk_add_rejects_dice_without_an_explicit_target(self):
        loaded = catalog_row("charms_loaded_dice")
        path = self.saves / "stash.hss"
        before = path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        result = editor.op_addmany({"cids": [loaded["id"]]})

        self.assertIn("bulk add has no explicit skill target", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("stash.hss.guibak_*")), backups_before
        )

    def test_loaded_dice_add_and_retarget_changes_only_a(self):
        loaded = catalog_row("charms_loaded_dice")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        added = editor.op_add({
            "cid": loaded["id"], "skillId": 31, "target": target_ref,
        })
        self.assertIn("Pyromancer: Meteor", added["ok"])

        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        self.assertEqual(entry["data"]["a"], 86_667.0)
        entry["data"].update({
            "i": 202_002.0,
            "s": 303_003.0,
            "s1": "opaque-socket",
            "zz": {"sockets": 1.0, "opaque": True},
            "future": {"keep": [1, 2, 3]},
        })
        untouched = {
            field: value for field, value in entry["data"].items() if field != "a"
        }
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        result = editor.op_modify({
            "action": "selectskill", "target": target_ref,
            "key": key, "skillId": 55,
        })
        self.assertIn("Marksman: Gunner Drone", result["ok"])
        updated = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]["data"]
        self.assertEqual(updated["a"], 158_856.0)
        self.assertEqual(
            {field: value for field, value in updated.items() if field != "a"},
            untouched,
        )
        resolved = editor.resolve(key, updated)
        self.assertEqual(resolved["skillSelector"]["current"]["id"], 55)

        before = path.read_bytes()
        backups_before = set(self.saves.glob("inventory_order_1.hss.guibak_*"))
        again = editor.op_modify({
            "action": "selectskill", "target": target_ref,
            "key": key, "skillId": 55,
        })
        self.assertIn("already targets", again["ok"])
        self.assertEqual(again["backup"], "")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("inventory_order_1.hss.guibak_*")), backups_before
        )

    def test_loaded_dice_perfect_is_blocked_as_identity_not_quality(self):
        loaded = catalog_row("charms_loaded_dice")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        editor.op_add({"cid": loaded["id"], "skillId": 31, "target": target_ref})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key = next(iter(bags["inventory_tab_0"]))
        before = path.read_bytes()
        backups_before = set(self.saves.glob("inventory_order_1.hss.guibak_*"))

        result = editor.op_modify({
            "action": "perfect", "target": target_ref, "key": key,
        })
        self.assertIn("skill identity", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("inventory_order_1.hss.guibak_*")), backups_before
        )

    def test_runtime_game_build_change_blocks_dice_add_and_retarget(self):
        loaded = catalog_row("charms_loaded_dice")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        editor.op_add({"cid": loaded["id"], "skillId": 31, "target": target_ref})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key = next(iter(bags["inventory_tab_0"]))
        before = path.read_bytes()
        backups_before = set(self.saves.glob("inventory_order_1.hss.guibak_*"))
        original_database = editor.DICE_SKILL_DB
        editor.DICE_SKILL_DB = editor.load_dice_skill_database(
            editor.BASE,
            runtime_build_check=lambda: "installed game hash changed",
        )
        try:
            modified = editor.op_modify({
                "action": "selectskill", "target": target_ref,
                "key": key, "skillId": 55,
            })
            added = editor.op_add({
                "cid": loaded["id"], "skillId": 55, "target": target_ref,
            })
        finally:
            editor.DICE_SKILL_DB = original_database

        self.assertIn("hash changed", modified["err"])
        self.assertIn("hash changed", added["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("inventory_order_1.hss.guibak_*")), backups_before
        )

    def test_overloaded_dice_accepts_only_verified_subskill_targets(self):
        overloaded = catalog_row("charms_overloaded_dice")
        target_ref = {"type": "bag", "slot": 1, "tab": "inventory_tab_0"}
        added = editor.op_add({
            "cid": overloaded["id"], "skillId": 55, "target": target_ref,
        })
        self.assertIn("Gunner Drone", added["ok"])
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        self.assertEqual(entry["data"]["a"], 59.0)

        before = path.read_bytes()
        invalid = editor.op_modify({
            "action": "selectskill", "target": target_ref,
            "key": key, "skillId": 31,
        })
        self.assertIn("not a valid subskill target", invalid["err"])
        self.assertEqual(path.read_bytes(), before)

        valid = editor.op_modify({
            "action": "selectskill", "target": target_ref,
            "key": key, "skillId": 7,
        })
        self.assertIn("Odin's Fury", valid["ok"])
        updated = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]["data"]
        self.assertEqual(updated["a"], 558.0)
        self.assertEqual(editor.resolve(key, updated)["skillSelector"]["current"]["id"], 7)

    def test_dice_generation_requires_an_explicit_target(self):
        loaded = catalog_row("charms_loaded_dice")
        path = self.saves / "inventory_order_1.hss"
        before = path.read_bytes()
        result = editor.op_add({
            "cid": loaded["id"],
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
        })
        self.assertIn("choose a verified skill target", result["err"])
        self.assertEqual(path.read_bytes(), before)

    def test_runeword_perfect_updates_a_and_i_but_preserves_s_and_sockets(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        base = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) == (3, 1, 17)
        )
        forged = editor.op_forge({
            "rw": 1, "baseCid": base["id"], "tab": "stash_tab_1"
        })
        self.assertIn("ok", forged)
        stash_path = self.saves / "stash.hss"
        stash = json.loads(editor.decode_hss(stash_path))
        key, entry = next(iter(stash["stash_tab_1"].items()))
        self.assertEqual(entry["data"]["a"], 356_137.0)
        self.assertEqual(entry["data"]["i"], 424_123.0)
        self.assertNotIn("s", entry["data"])

        entry["data"]["a"] = 1.0
        entry["data"]["i"] = 2.0
        entry["data"]["s"] = 777.0
        sockets_before = {
            field: value for field, value in entry["data"].items()
            if field.startswith("s") and field != "s"
        }
        zz_before = dict(entry["data"]["zz"])
        stash_path.write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "key": key,
        })
        self.assertIn("EXACT MAX", result["ok"])
        updated = json.loads(editor.decode_hss(stash_path))["stash_tab_1"][key]["data"]
        self.assertEqual((updated["a"], updated["i"]), (356_137.0, 424_123.0))
        self.assertEqual(updated["s"], 777.0)
        self.assertEqual(updated["zz"], zz_before)
        self.assertEqual(
            {field: value for field, value in updated.items()
             if field.startswith("s") and field != "s"},
            sockets_before,
        )

    def test_perfect_updates_socket_seed_only_when_s_already_exists(self):
        gown = catalog_row("armors_zephys_gown")
        editor.op_add({"cid": gown["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        entry["data"]["a"] = 1.0
        self.assertNotIn("s", entry["data"])
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        # Synthetic future profile: creation/forge may opt into s explicitly,
        # but Perfect on an existing item must preserve an absent enable field.
        profile = gown["rollProfile"]
        profile["fieldSeeds"]["s"] = 987_654
        profile["chains"]["s"] = {"seed": 987_654}
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })
        self.assertIn("ok", result)
        updated = json.loads(editor.decode_hss(path))
        data = updated["inventory_tab_0"][key]["data"]
        self.assertEqual(data["a"], 3_888_156.0)
        self.assertNotIn("s", data)

        # Once s is already present, the independently verified s chain is
        # active and Perfect may update that field without touching payloads.
        data["s"] = 2.0
        data["s1"] = "opaque-socket-payload"
        path.write_text(editor.encode_hss(json.dumps(updated)), encoding="ascii")
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })
        self.assertIn("ok", result)
        final = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]["data"]
        self.assertEqual(final["s"], 987_654.0)
        self.assertEqual(final["s1"], "opaque-socket-payload")

    def test_perfect_applies_all_profile_seed_fields_and_only_those_fields(self):
        gown = catalog_row("armors_zephys_gown")
        gown["rollProfile"] = roll_profile(
            "unique:1:0:52", "unique", "Zephy's Gown", "exact",
            {"a": 101_001, "i": 202_002, "s": 303_003}, 3, 3, 0,
        )
        result = editor.op_add({"cid": gown["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        self.assertIn("ok", result)

        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key, entry = next(iter(bags["inventory_tab_0"].items()))
        socket_payload = base64.b64encode(
            json.dumps({"a": 919_191, "b": 17, "n": 4}).encode()
        ).decode()
        entry["data"].update({
            "a": 1.0,
            "i": 2.0,
            "s": 3.0,
            "s1": socket_payload,
            "zz": {"sockets": 1.0, "opaque": {"keep": True}},
            "futurePayload": {"nested": [1, 2, 3]},
        })
        untouched_before = {
            field: value for field, value in entry["data"].items()
            if field not in {"a", "i", "s"}
        }
        path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })

        self.assertIn("EXACT MAX", result["ok"])
        data = json.loads(editor.decode_hss(path))["inventory_tab_0"][key]["data"]
        self.assertEqual(
            (data["a"], data["i"], data["s"]),
            (101_001.0, 202_002.0, 303_003.0),
        )
        self.assertEqual(
            {field: value for field, value in data.items()
             if field not in {"a", "i", "s"}},
            untouched_before,
        )

    def test_socket_only_profile_does_not_create_missing_s_or_write(self):
        gown = catalog_row("armors_zephys_gown")
        result = editor.op_add({"cid": gown["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})
        self.assertIn("ok", result)
        path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(path))
        key = next(iter(bags["inventory_tab_0"]))
        self.assertNotIn("s", bags["inventory_tab_0"][key]["data"])

        gown["rollProfile"] = roll_profile(
            "unique:1:0:52", "unique", "Zephy's Gown", "exact",
            {"s": 404_004}, 1, 1, 0,
        )
        before = path.read_bytes()
        backups_before = set(self.saves.glob("inventory_order_1.hss.guibak_*"))
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            "key": key,
        })

        self.assertIn("socket seed is not active", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            set(self.saves.glob("inventory_order_1.hss.guibak_*")),
            backups_before,
        )

    def test_every_build_specific_equipment_address_fails_closed_without_a_profile(self):
        class EmptyRollDatabase:
            available = True
            status = type("Status", (), {"message": "fixture database ready"})()

            @staticmethod
            def lookup(*_args):
                return None

        equipment = [
            row for row in editor.CAT
            if row.get("kind") in {"normal", "unique"}
            and int(row.get("cls", -1)) in editor.ROLL_PROFILE_GEAR_CLASSES
            and row.get("available", True)
            and not editor.is_ordinary_small_charm_row(row)
        ]
        self.assertTrue(any(row["kind"] == "normal" for row in equipment))
        self.assertTrue(any(row["kind"] == "unique" for row in equipment))
        for row in equipment:
            row.pop("rollProfile", None)
        editor.ROLL_DB = EmptyRollDatabase()

        for row in equipment:
            with self.subTest(address=(
                row["kind"], row["cls"], row.get("sub", 0), row["b"],
            )):
                with self.assertRaisesRegex(ValueError, "no verified roll profile"):
                    editor.generation_roll_profile(row)
                with self.assertRaisesRegex(ValueError, "no verified roll profile"):
                    editor.make_data(row)

    def test_runeword_matrix_is_complete_and_invalid_base_is_rejected(self):
        counts = [editor.runeword_base_candidates(row) for row in editor.RUNEWORDS]
        self.assertEqual(len(counts), 100)
        self.assertEqual(sum(map(len, counts)), 3_722)
        self.assertEqual(
            sum(len(bases) for recipe, bases in zip(editor.RUNEWORDS, counts)
                if int(recipe["type"]) != 11),
            3_715,
        )
        invalid = catalog_row("rings_parasite_loop")
        path = self.saves / "stash.hss"
        before = path.read_bytes()
        result = editor.op_forge({
            "rw": 1, "baseCid": invalid["id"], "tab": "stash_tab_1"
        })
        self.assertIn("not valid", result["err"])
        self.assertEqual(path.read_bytes(), before)

    def test_forge_rejects_invalid_tab_without_write_or_backup(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        base = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) == (3, 1, 17)
        )
        path = self.saves / "stash.hss"
        before = path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        result = editor.op_forge({
            "rw": 1,
            "baseCid": base["id"],
            "tab": "not_a_stash",
        })
        self.assertIn("stash tab 1-9", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_missing_runeword_profile_is_disabled_and_fails_closed(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        unsupported = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) != (3, 1, 17)
        )
        api_recipe = next(row for row in editor.runeword_api_rows() if row["rw"] == 1)
        api_base = next(row for row in api_recipe["bases"] if row["cid"] == unsupported["id"])
        self.assertFalse(api_base["available"])
        self.assertTrue(api_base["disabled"])
        self.assertIn("no verified roll profile", api_base["unavailableReason"])

        path = self.saves / "stash.hss"
        before = path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        result = editor.op_forge({
            "rw": 1,
            "baseCid": unsupported["id"],
            "tab": "stash_tab_1",
        })
        self.assertIn("no verified roll profile", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_existing_runeword_never_falls_back_to_its_normal_base_profile(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        base = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) == (3, 1, 17)
        )
        base["rollProfile"] = roll_profile(
            "normal:3:1:17", "normal", base["name"], "exact",
            {"a": 111_111}, 1, 1, 0,
        )
        editor.ROLL_DB.profiles.pop("runeword:1|normal:3:1:17")

        data = {
            "a": 1.0,
            "i": 2.0,
            "b": float(base["b"]),
            "c": 0.0,
            "w": 1.0,
            "j": float(base["sub"]),
            "zz": {"sockets": float(len(recipe["runes"]))},
        }
        for index, rune in enumerate(recipe["runes"], 1):
            payload = json.dumps({"a": index, "b": rune["b"], "n": 0})
            data[f"s{index}"] = base64.b64encode(payload.encode()).decode()
        key = f"0-0-123456-{base['cls']}"
        stash = json.loads(editor.decode_hss(self.saves / "stash.hss"))
        stash["stash_tab_1"][key] = {"pos": [0.0, 0.0], "data": data}
        path = self.saves / "stash.hss"
        path.write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")

        resolved = editor.resolve(key, data)
        self.assertTrue(resolved["isRW"])
        self.assertNotIn("rollProfile", resolved)
        before = path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "key": key,
        })
        self.assertIn("no verified profile", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_fixed_equipment_runeword_has_no_perfect_write(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        base = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) == (3, 1, 17)
        )
        profile_id = "runeword:1|normal:3:1:17"
        editor.ROLL_DB.profiles[profile_id] = roll_profile(
            profile_id, "runeword", recipe["name"], "fixed", {}, 0, 0, 0,
        )
        forged = editor.op_forge({
            "rw": 1, "baseCid": base["id"], "tab": "stash_tab_1",
        })
        self.assertIn("ok", forged)

        path = self.saves / "stash.hss"
        stash = json.loads(editor.decode_hss(path))
        key = next(iter(stash["stash_tab_1"]))
        before = path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        result = editor.op_modify({
            "action": "perfect",
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "key": key,
        })
        self.assertIn("fixed", result["err"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_runeword_api_contains_malformed_recipe_as_disabled_row(self):
        original = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        malformed = {**original, "rw": 999, "target": "Weapon ???"}
        with patch.object(editor, "RUNEWORDS", [malformed]):
            rows = editor.runeword_api_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bases"], [])
        self.assertFalse(rows[0]["available"])
        self.assertTrue(rows[0]["disabled"])
        self.assertIn("invalid runeword target", rows[0]["unavailableReason"])

    def test_forge_codex_fails_closed_even_if_a_roll_profile_is_present(self):
        original = next(row for row in editor.RUNEWORDS if row["rw"] == 93)
        recipe = {**original, "rw": 999, "base": {
            "cls": 11, "sub": 0, "b": 77, "w": 2, "h": 2,
        }}
        base = {
            "id": 123_456, "kind": "normal", "cls": 11, "sub": 0,
            "b": 77, "w": 2, "h": 2, "name": "Future Zone Codex",
        }
        fixed = roll_profile(
            "runeword:999|normal:11:0:77", "runeword", recipe["name"],
            "fixed", {}, 0, 0, 0,
        )
        with (
            patch.object(editor, "RUNEWORDS", [recipe]),
            patch.object(editor, "runeword_base_candidates", return_value=[base]),
            patch.object(editor, "runeword_profile", return_value=fixed),
        ):
            api = editor.runeword_api_rows()[0]
            result = editor.op_forge({
                "rw": 999, "baseCid": base["id"], "tab": "stash_tab_1",
            })
        self.assertFalse(api["available"])
        self.assertTrue(api["disabled"])
        self.assertIn("payload", api["unavailableReason"])
        self.assertIn("disabled", result["err"])
        stash = json.loads(editor.decode_hss(self.saves / "stash.hss"))
        self.assertEqual(stash["stash_tab_1"], {})

    def test_runeword_display_requires_recipe_compatible_base(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        invalid_base = next(
            row for row in editor.CAT
            if row.get("kind") == "normal" and row.get("cls") == 7
            and row.get("available", True)
        )
        data = {
            "w": 1.0,
            "a": 1.0,
            "j": float(invalid_base.get("sub", 0)),
            "b": float(invalid_base["b"]),
            "c": 0.0,
            "o": 1.0,
        }
        for index, rune in enumerate(recipe["runes"], 1):
            payload = json.dumps({"a": 1, "b": rune["b"], "n": 0})
            data[f"s{index}"] = base64.b64encode(payload.encode()).decode()
        resolved = editor.resolve(f"0-0-1-{invalid_base['cls']}", data)
        self.assertNotEqual(resolved["name"], recipe["name"])
        self.assertNotIn("isRW", resolved)

    def test_forge_request_id_makes_retry_idempotent(self):
        recipe = next(row for row in editor.RUNEWORDS if row["rw"] == 1)
        base = next(
            row for row in editor.runeword_base_candidates(recipe)
            if (row["cls"], row.get("sub", 0), row["b"]) == (3, 1, 17)
        )
        body = {
            "rw": 1,
            "baseCid": base["id"],
            "tab": "stash_tab_1",
            "requestId": "12345678-1234-1234-1234-123456789abc",
        }
        first = editor.op_forge(body)
        backups_after_first = set(self.saves.glob("stash.hss.guibak_*"))
        second = editor.op_forge(body)
        self.assertIn("FORGED", first["ok"])
        self.assertIn("ALREADY FORGED", second["ok"])
        self.assertEqual(second["backup"], "")
        self.assertEqual(
            set(self.saves.glob("stash.hss.guibak_*")),
            backups_after_first,
        )
        stash = json.loads(editor.decode_hss(self.saves / "stash.hss"))
        self.assertEqual(len(stash["stash_tab_1"]), 1)

    def test_write_char_inventory_distinguishes_missing_field_from_noop(self):
        char_path = self.saves / "herosiege1.hss"
        inventory = editor._decode_character_inventory(char_path)[1]
        self.assertEqual(editor.write_char_inventory(1, inventory), "")

        char_path.write_text(
            editor.encode_hss('[character]\nname="Missing Inventory"\n'),
            encoding="ascii",
        )
        with self.assertRaisesRegex(ValueError, "inventory field not found"):
            editor.write_char_inventory(1, inventory)

    def test_cross_file_move_game_start_during_backups_leaves_both_files_unchanged(self):
        stash_path = self.saves / "stash.hss"
        bags_path = self.saves / "inventory_order_1.hss"
        stash = json.loads(editor.decode_hss(stash_path))
        key = "0-0-424242-7"
        stash["stash_tab_1"][key] = {
            "pos": [0.0, 0.0],
            "data": editor.make_data(catalog_row("rings_parasite_loop")),
        }
        stash_path.write_text(
            editor.encode_hss(json.dumps(stash)), encoding="ascii"
        )
        stash_before = stash_path.read_bytes()
        bags_before = bags_path.read_bytes()

        running = {"value": False, "backup_count": 0}
        real_backup = editor.backup

        def backup_then_start_game(path):
            backup_name = real_backup(path)
            running["backup_count"] += 1
            running["value"] = True
            return backup_name

        with (
            patch.object(editor, "INSTANCE_GUARD_ACTIVE", True),
            patch.object(editor, "_active_peer_editor_error", return_value=None),
            patch.object(editor, "game_running", side_effect=lambda: running["value"]),
            patch.object(editor, "backup", side_effect=backup_then_start_game),
        ):
            with self.assertRaisesRegex(RuntimeError, "Game started before the save write"):
                editor.op_move({
                    "from": {"type": "stash", "tab": "stash_tab_1"},
                    "to": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
                    "key": key,
                    "pos": [0, 0],
                })

        self.assertEqual(running["backup_count"], 2)
        self.assertEqual(stash_path.read_bytes(), stash_before)
        self.assertEqual(bags_path.read_bytes(), bags_before)
        stash_after = json.loads(editor.decode_hss(stash_path))
        bags_after = json.loads(editor.decode_hss(bags_path))
        self.assertIn(key, stash_after["stash_tab_1"])
        self.assertEqual(bags_after["inventory_tab_0"], {})

    def test_cross_file_move_second_replace_failure_keeps_destination_copy(self):
        stash_path = self.saves / "stash.hss"
        bags_path = self.saves / "inventory_order_1.hss"
        stash = json.loads(editor.decode_hss(stash_path))
        key = "0-0-424243-7"
        stash["stash_tab_1"][key] = {
            "pos": [0.0, 0.0],
            "data": editor.make_data(catalog_row("rings_parasite_loop")),
        }
        stash_path.write_text(
            editor.encode_hss(json.dumps(stash)), encoding="ascii"
        )

        real_atomic_write = editor.atomic_write_text
        writes = {"count": 0}

        def fail_second_replace(path, text, encoding):
            writes["count"] += 1
            if writes["count"] == 2:
                raise OSError("injected second replace failure")
            return real_atomic_write(path, text, encoding)

        with patch.object(editor, "atomic_write_text", side_effect=fail_second_replace):
            with self.assertRaisesRegex(OSError, "injected second replace failure"):
                editor.op_move({
                    "from": {"type": "stash", "tab": "stash_tab_1"},
                    "to": {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
                    "key": key,
                    "pos": [0, 0],
                })

        self.assertEqual(writes["count"], 2)
        stash_after = json.loads(editor.decode_hss(stash_path))
        bags_after = json.loads(editor.decode_hss(bags_path))
        self.assertIn(key, stash_after["stash_tab_1"])
        self.assertIn(key, bags_after["inventory_tab_0"])

    def test_stackables_use_native_s10_shape_in_dedicated_bags(self):
        cases = [
            ("keys_aztec_key", "inventory_key_tab", 12, 40),
            ("material_blacksmiths_mallet_fragment", "inventory_material_tab", 14, 71),
            ("socketable_gem_cthulhu", "inventory_socket_tab", 15, 136),
        ]
        for key, tab, cls, base_id in cases:
            row = catalog_row(key)
            result = editor.op_add({"cid": row["id"],
                                    "target": {"type": "bag", "slot": 1, "tab": tab}})
            self.assertIn("ok", result)
            bags = json.loads(editor.decode_hss(self.saves / "inventory_order_1.hss"))
            entry_key, entry = next(reversed(bags[tab].items()))
            self.assertTrue(entry_key.endswith(f"-{cls}"))
            self.assertEqual(set(entry["data"]), {"a", "j", "b", "c", "o"})
            self.assertEqual((entry["data"]["b"], entry["data"]["c"]),
                             (float(base_id), 0.0))

    def test_stash_fill_catalog_contract(self):
        unique_label, unique_rows = editor.stash_fill_catalog("unique_items")
        material_label, material_rows = editor.stash_fill_catalog("material_tab_1")
        socket_label, socket_rows = editor.stash_fill_catalog("socket_tab_1")

        self.assertEqual(unique_label, "Unique tab")
        self.assertEqual(len(unique_rows), 944)
        self.assertEqual(len(material_rows), 139)
        self.assertEqual(len(socket_rows), 144)
        self.assertEqual({row["cls"] for row in material_rows}, {13, 14})
        self.assertEqual({row["cls"] for row in socket_rows}, {15})
        self.assertEqual(
            len({editor._native_catalog_address(row) for row in unique_rows}),
            len(unique_rows),
        )
        self.assertFalse(any(row.get("available") is False for row in unique_rows))
        with self.assertRaisesRegex(ValueError, "Only the Unique"):
            editor.stash_fill_catalog("stash_tab_1")

    def test_material_and_socket_fill_are_complete_native_and_idempotent(self):
        stash_path = self.saves / "stash.hss"
        material = editor.op_fill_stash({"tab": "material_tab_1"})
        socket = editor.op_fill_stash({"tab": "socket_tab_1"})

        self.assertEqual((material["total"], material["added"]), (139, 139))
        self.assertEqual((socket["total"], socket["added"]), (144, 144))
        stash = json.loads(editor.decode_hss(stash_path))
        material_items = stash["material_tab_1"]
        socket_items = stash["socket_tab_1"]
        self.assertEqual(len(material_items), 139)
        self.assertEqual(len(socket_items), 144)

        singleton_addresses = set()
        for key, entry in material_items.items():
            address = editor._native_entry_address(key, entry)
            short_address = (address[1], address[3])
            if short_address in editor.MATERIAL_SINGLETON_ADDRESSES:
                singleton_addresses.add(short_address)
                self.assertNotIn("o", entry["data"])
            else:
                self.assertEqual(entry["data"]["o"], float(editor.FULL_STACK_AMOUNT))
        self.assertEqual(singleton_addresses, set(editor.MATERIAL_SINGLETON_ADDRESSES))
        self.assertTrue(all(
            entry["data"]["o"] == float(editor.FULL_STACK_AMOUNT)
            for entry in socket_items.values()
        ))

        # Catalog dimensions, including the 1x2 Tarot cards, must not overlap.
        for tab, items in (("material_tab_1", material_items),
                           ("socket_tab_1", socket_items)):
            occupied = set()
            for key, entry in items.items():
                item = editor.resolve(key, entry["data"])
                x, y = map(int, entry["pos"])
                cells = {
                    (x + dx, y + dy)
                    for dx in range(item["w"])
                    for dy in range(item["h"])
                }
                self.assertTrue(occupied.isdisjoint(cells))
                occupied.update(cells)

        before = stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        material_again = editor.op_fill_stash({"tab": "material_tab_1"})
        socket_again = editor.op_fill_stash({"tab": "socket_tab_1"})
        self.assertEqual(material_again["backup"], "")
        self.assertEqual(socket_again["backup"], "")
        self.assertEqual(stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_unique_fill_requires_explicit_dice_targets_and_preserves_owned_data(self):
        crown = catalog_row("helmet_leviathans_crown")
        loaded = catalog_row("charms_loaded_dice")
        overloaded = catalog_row("charms_overloaded_dice")
        rows = [crown, loaded, overloaded]
        stash_path = self.saves / "stash.hss"
        stash = json.loads(editor.decode_hss(stash_path))
        owned_data = editor.make_data(crown)
        owned_data.update({"future": {"keep": True}, "s1": "opaque"})
        stash["unique_items"]["0-0-777-0"] = {"data": owned_data}
        stash_path.write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")
        before = stash_path.read_bytes()

        class StubDiceDatabase:
            seeds = {2: 202.0, 7: 707.0}

            def target(self, profile_id, skill_id):
                skill_id = int(skill_id)
                if skill_id not in self.seeds:
                    raise editor.DiceSkillValidationError("invalid fixture target")
                return {
                    "id": skill_id,
                    "seed": self.seeds[skill_id],
                    "className": "Fixture",
                    "name": f"Skill {skill_id}",
                    "profileId": profile_id,
                }

        with (
            patch.object(editor, "CAT", rows),
            patch.object(editor, "DICE_SKILL_DB", StubDiceDatabase()),
        ):
            rejected = editor.op_fill_stash({"tab": "unique_items"})
            self.assertIn("explicit verified skill target", rejected["err"])
            self.assertEqual(stash_path.read_bytes(), before)

            filled = editor.op_fill_stash({
                "tab": "unique_items",
                "diceSkillIds": {
                    "unique:10:0:31": 2,
                    "unique:10:0:89": 7,
                },
            })
            self.assertEqual((filled["total"], filled["added"], filled["existing"]),
                             (3, 2, 1))
            self.assertEqual(len(filled["diceTargets"]), 2)
            updated = json.loads(editor.decode_hss(stash_path))["unique_items"]
            self.assertEqual(updated["0-0-777-0"]["data"], owned_data)
            self.assertTrue(all("pos" not in entry for entry in updated.values()))
            dice_seeds = {
                int(entry["data"]["b"]): entry["data"]["a"]
                for entry in updated.values()
                if int(entry["data"]["b"]) in (31, 89)
            }
            self.assertEqual(dice_seeds, {31: 202.0, 89: 707.0})

            complete = editor.op_fill_stash({"tab": "unique_items"})
            self.assertEqual(complete["backup"], "")
            self.assertEqual(complete["added"], 0)

    def test_stash_fill_capacity_failure_is_atomic(self):
        material_rows = [
            row for row in editor.CAT
            if row.get("kind") == "normal"
            and row.get("available", True)
            and row.get("cls") == 14
        ][:2]
        stash_path = self.saves / "stash.hss"
        stash = json.loads(editor.decode_hss(stash_path))
        stash["material_tab_1"] = {
            f"0-0-{index}-12": {
                "pos": [float(index % 17), float(index // 17)],
                "data": {"a": 1.0, "j": 0.0, "b": 9999.0,
                         "c": 0.0, "o": 1.0},
            }
            for index in range(305)
        }
        stash_path.write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")
        before = stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        with patch.object(editor, "CAT", material_rows):
            result = editor.op_fill_stash({"tab": "material_tab_1"})

        self.assertIn("enough valid grid space", result["err"])
        self.assertEqual(stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_stash_fill_maxes_preserved_duplicates_and_rejects_hidden_grid_items(self):
        row = next(
            item for item in editor.CAT
            if item.get("kind") == "normal"
            and item.get("available", True)
            and item.get("cls") == 14
            and (item["cls"], item["b"]) not in editor.MATERIAL_SINGLETON_ADDRESSES
        )
        stash_path = self.saves / "stash.hss"
        stash = json.loads(editor.decode_hss(stash_path))
        stash["material_tab_1"] = {
            "0-0-501-14": {
                "pos": [0.0, 0.0],
                "data": {"a": 11.0, "j": 0.0, "b": float(row["b"]),
                         "c": 0.0, "o": 2.0, "future": {"keep": 1}},
            },
            "0-0-502-14": {
                "pos": [1.0, 0.0],
                "data": {"a": 22.0, "j": 0.0, "b": float(row["b"]),
                         "c": 0.0, "o": 3.0, "future": {"keep": 2}},
            },
        }
        stash_path.write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")

        with patch.object(editor, "CAT", [row]):
            result = editor.op_fill_stash({"tab": "material_tab_1"})
        self.assertEqual((result["added"], result["updated"], result["existing"]),
                         (0, 2, 1))
        updated = json.loads(editor.decode_hss(stash_path))["material_tab_1"]
        self.assertEqual(len(updated), 2)
        self.assertTrue(all(
            entry["data"]["o"] == float(editor.FULL_STACK_AMOUNT)
            for entry in updated.values()
        ))
        self.assertEqual(updated["0-0-501-14"]["data"]["future"], {"keep": 1})
        self.assertEqual(updated["0-0-502-14"]["data"]["future"], {"keep": 2})

        del updated["0-0-501-14"]["pos"]
        stash = json.loads(editor.decode_hss(stash_path))
        stash["material_tab_1"] = updated
        stash_path.write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")
        before = stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        with patch.object(editor, "CAT", [row]):
            rejected = editor.op_fill_stash({"tab": "material_tab_1"})
        self.assertIn("invalid grid", rejected["err"])
        self.assertIn("Save Health Check", rejected["err"])
        self.assertEqual(stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_stash_fill_game_gate_and_missing_tab_do_not_write(self):
        stash_path = self.saves / "stash.hss"
        before = stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        with patch.object(editor, "game_running", return_value=True):
            running = editor.op_fill_stash({"tab": "socket_tab_1"})
        missing = editor.op_fill_stash({"tab": "material_tab_2"})
        invalid = editor.op_fill_stash({"tab": "stash_tab_1"})

        self.assertIn("Game is running", running["err"])
        self.assertIn("does not exist", missing["err"])
        self.assertIn("Only the Unique", invalid["err"])
        self.assertEqual(stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)

    def test_s10_access_kits_use_verified_key_routes(self):
        groups = {group["id"]: group for group in editor.s10_access_list()}
        self.assertEqual(
            [(item["name"], item["b"]) for item in groups["ubers"]["items"]],
            [("Tablet of Leviathan", 41),
             ("Tablet of Armada", 42),
             ("Tablet of Parasite", 43)],
        )
        self.assertEqual(
            [(item["name"], item["b"]) for item in groups["act9"]["items"]],
            [("Boreal Key", 36),
             ("Parasitic Key", 37),
             ("Treasure Key", 38),
             ("Hive Key", 39),
             ("Aztec Key", 40)],
        )
        for group in groups.values():
            for item in group["items"]:
                row = next(r for r in editor.CAT
                           if r.get("cls") == 12 and r.get("b") == item["b"])
                self.assertEqual(row["name"], item["name"])

        result = editor.op_make_s10_access(
            {"group": "ubers", "slot": 1, "amount": 25}
        )
        self.assertIn("ok", result)
        self.assertEqual(len(result["created"]), 3)
        bags = json.loads(editor.decode_hss(self.saves / "inventory_order_1.hss"))
        tablets = list(bags["inventory_key_tab"].values())
        self.assertEqual([entry["data"]["b"] for entry in tablets],
                         [41.0, 42.0, 43.0])
        self.assertTrue(all(entry["data"]["o"] == 25.0 for entry in tablets))

    def test_s10_access_kit_is_atomic_when_key_bag_is_full(self):
        bags_path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(bags_path))
        bags["inventory_key_tab"] = {
            f"0-0-{index}-12": {
                "pos": [float(index % 15), float(index // 15)],
                "data": {"o": 1.0, "a": float(index + 1), "j": 0.0,
                         "b": 0.0, "c": 0.0},
            }
            for index in range(89)
        }
        bags_path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")
        before = bags_path.read_bytes()
        result = editor.op_make_s10_access(
            {"group": "ubers", "slot": 1, "amount": 10}
        )
        self.assertIn("err", result)
        self.assertEqual(bags_path.read_bytes(), before)

    def test_writes_are_backed_up_and_decodable(self):
        row = catalog_row("rings_parasite_loop")
        result = editor.op_add({"cid": row["id"],
                                "target": {"type": "bag", "slot": 1,
                                           "tab": "inventory_tab_0"}})
        self.assertIn("backup", result)
        backup_path = self.saves / result["backup"]
        self.assertTrue(backup_path.exists())
        json.loads(editor.decode_hss(backup_path))
        current = json.loads(editor.decode_hss(self.saves / "inventory_order_1.hss"))
        self.assertEqual(len(current["inventory_tab_0"]), 1)

    def test_save_health_detects_and_repairs_only_safe_grid_and_stack_issues(self):
        ring = catalog_row("rings_parasite_loop")
        key_item = catalog_row("keys_aztec_key")
        bags_path = self.saves / "inventory_order_1.hss"
        bags = json.loads(editor.decode_hss(bags_path))
        bags["inventory_tab_0"] = {
            "0-0-100-7": {"pos": [0.0, 0.0], "data": editor.make_data(ring)},
            "0-0-101-7": {"pos": [0.0, 0.0], "data": editor.make_data(ring)},
        }
        bad_stack = editor.make_data(key_item)
        bad_stack["o"] = 0.0
        bags["inventory_key_tab"] = {
            "0-0-102-12": {"pos": [0.0, 0.0], "data": bad_stack},
        }
        bags_path.write_text(editor.encode_hss(json.dumps(bags)), encoding="ascii")

        before = bags_path.read_bytes()
        report = editor.scan_save_health()
        self.assertEqual(bags_path.read_bytes(), before, "read-only scan changed the save")
        codes = {issue["code"] for issue in report["issues"] if issue["fixable"]}
        self.assertIn("position_overlap", codes)
        self.assertIn("stack_value", codes)

        repaired = editor.op_fix_save_health()
        self.assertEqual(repaired["fixed"], 2)
        self.assertTrue(repaired["backups"])
        fixed_bags = json.loads(editor.decode_hss(bags_path))
        positions = [tuple(entry["pos"]) for entry in fixed_bags["inventory_tab_0"].values()]
        self.assertEqual(len(set(positions)), 2)
        self.assertEqual(fixed_bags["inventory_key_tab"]["0-0-102-12"]["data"]["o"], 1.0)

    def test_global_item_finder_searches_stash_and_character_bags(self):
        crown = catalog_row("helmet_leviathans_crown")
        ring = catalog_row("rings_parasite_loop")
        editor.op_add({"cid": crown["id"], "target": {"type": "stash_unique"}})
        editor.op_add({"cid": ring["id"], "target": {
            "type": "bag", "slot": 1, "tab": "inventory_tab_0"}})

        stash_hits = editor.find_owned_items("leviathan")
        self.assertEqual(stash_hits["total"], 1)
        self.assertEqual(stash_hits["items"][0]["target"]["tab"], "unique_items")

        bag_hits = editor.find_owned_items("parasite")
        self.assertEqual(bag_hits["total"], 1)
        self.assertEqual(bag_hits["items"][0]["target"]["slot"], 1)
        self.assertEqual(bag_hits["items"][0]["target"]["tab"], "inventory_tab_0")

    def test_loadout_export_writes_portable_json_and_readable_html(self):
        ring = catalog_row("rings_parasite_loop")
        data = editor.make_data(ring)
        data["g"] = 7.0
        editor.save_loadouts({
            "Parasite Build": {
                "created": "2026-08-24 04:00",
                "items": [{"cls": 7, "data": data, "name": ring["name"],
                           "spr": ring.get("spr"), "g": 7}],
            }
        })
        result = editor.op_loadout({"action": "export", "name": "Parasite Build"})
        self.assertIn("ok", result)
        json_path = Path(result["json"])
        html_path = Path(result["html"])
        self.assertTrue(json_path.is_file())
        self.assertTrue(html_path.is_file())
        portable = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(portable["format"], "hero-siege-item-editor-build")
        self.assertEqual(portable["formatVersion"], 1)
        self.assertEqual(portable["items"][0]["data"]["b"], 61.0)
        report = html_path.read_text(encoding="utf-8")
        self.assertIn("Parasite Build", report)
        self.assertIn("Parasite Loop", report)
        self.assertIn("Ring I", report)


if __name__ == "__main__":
    unittest.main()
