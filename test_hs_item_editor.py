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


class ItemEditorSeason10Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saves = Path(self.temp.name)
        self.old_saves = editor.SAVES
        self.old_loadouts_file = editor.LOADOUTS_FILE
        self.old_build_export_dir = editor.BUILD_EXPORT_DIR
        editor.SAVES = self.saves
        editor.LOADOUTS_FILE = self.saves / "hs_loadouts.json"
        editor.BUILD_EXPORT_DIR = self.saves / "exports"

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
