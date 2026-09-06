import json
import importlib.util
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from custom_item_forge import (
    CustomForgeError,
    CustomForgeStore,
    item_selector,
    load_custom_forge_catalog,
)
from roll_profile_db import EXPECTED_EXE_SHA256
from stat_semantics import load_stat_semantics


BASE = Path(__file__).resolve().parent
EDITOR_SPEC = importlib.util.spec_from_file_location(
    "hs_item_editor_gui_custom_forge_tests", BASE / "hs_item_editor_gui.py"
)
editor = importlib.util.module_from_spec(EDITOR_SPEC)
EDITOR_SPEC.loader.exec_module(editor)


class CustomForgeMechanicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.semantics = load_stat_semantics(BASE, expected_exe_sha256=EXPECTED_EXE_SHA256)
        cls.catalog = cls.semantics.decorate_catalog(load_custom_forge_catalog(BASE))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CustomForgeStore(self.root, self.catalog, self.semantics)
        self.selector = {"t": 8, "a": 351817, "b": 19, "c": 1, "j": 0}
        self.stat_key = next(iter(sorted(self.store.allowed_stats)))

    def tearDown(self):
        self.temp.cleanup()

    def test_mechanic_round_trips_to_json_and_runtime(self):
        result = self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                                  keep_native=True, mechanic="Headhunter")
        self.assertEqual(result["entry"]["mechanic"], "headhunter")
        runtime = (self.root / "hs_custom_item_forge.runtime").read_text(encoding="utf-8")
        line = [l for l in runtime.splitlines() if l.startswith("item|")][0]
        self.assertEqual(line.count("|"), 4, line)
        self.assertIn("mechanic=headhunter", line.split("|")[4])
        self.assertEqual(self.store.get(self.selector)["mechanic"], "headhunter")

    def test_no_mechanic_keeps_the_old_line_shape(self):
        self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1}, keep_native=True)
        runtime = (self.root / "hs_custom_item_forge.runtime").read_text(encoding="utf-8")
        line = [l for l in runtime.splitlines() if l.startswith("item|")][0]
        self.assertNotIn("mechanic=", line)
        self.assertIsNone(self.store.get(self.selector)["mechanic"])

    def test_unknown_mechanic_is_rejected(self):
        with self.assertRaises(CustomForgeError):
            self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                             keep_native=True, mechanic="teleport")

    def test_name_round_trips_to_json_and_runtime(self):
        result = self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                                  keep_native=True, name="  Head   hunter ")
        self.assertEqual(result["entry"]["name"], "Head hunter")
        runtime = (self.root / "hs_custom_item_forge.runtime").read_text(encoding="utf-8")
        line = [l for l in runtime.splitlines() if l.startswith("item|")][0]
        self.assertIn("name=Head%20hunter", line.split("|")[4])
        self.assertEqual(self.store.get(self.selector)["name"], "Head hunter")

    def test_name_limits_are_enforced(self):
        with self.assertRaises(CustomForgeError):
            self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                             keep_native=True, name="x" * 49)
        with self.assertRaises(CustomForgeError):
            self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                             keep_native=True, name="bad|name")
        self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                         keep_native=True, name="   ")
        self.assertIsNone(self.store.get(self.selector)["name"])

    def test_affix_round_trips_to_json_and_runtime(self):
        result = self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                                  keep_native=True, affix=" Steals  affixes \r\n\n for 20s ")
        self.assertEqual(result["entry"]["affix"], "Steals affixes\nfor 20s")
        runtime = (self.root / "hs_custom_item_forge.runtime").read_text(encoding="utf-8")
        line = [l for l in runtime.splitlines() if l.startswith("item|")][0]
        self.assertIn("affix=Steals%20affixes%0Afor%2020s", line.split("|")[4])
        self.assertEqual(self.store.get(self.selector)["affix"], "Steals affixes\nfor 20s")

    def test_affix_limits_are_enforced(self):
        with self.assertRaises(CustomForgeError):
            self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                             keep_native=True, affix="a\nb\nc\nd")
        with self.assertRaises(CustomForgeError):
            self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                             keep_native=True, affix="x" * 241)
        with self.assertRaises(CustomForgeError):
            self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                             keep_native=True, affix="bad;text")
        self.store.apply(self.selector, label="Belt", stats={self.stat_key: 1},
                         keep_native=True, affix="\n \n")
        self.assertIsNone(self.store.get(self.selector)["affix"])

    def test_ui_offers_the_affix_field(self):
        self.assertIn('id="ifaffix"', editor.HTML)
        self.assertIn("affix:affix||null", editor.HTML)

    def test_base_stats_come_from_the_game_tooltip(self):
        allowed = {str(k) for k in self.store.allowed_stats}
        key = next(iter(sorted(allowed)))
        item = {"gameTooltip": {"stats": [
            {"statKey": int(key), "value": 9.0, "label": "Strength"},
            {"statKey": 999999, "value": 5, "label": "not writable"},
            {"statKey": None, "value": 3, "label": "header"},
            {"statKey": int(key) + 0, "value": float("nan"), "label": "nan"},
        ]}}
        stats, labels, source = editor._forge_base_stats(item, allowed)
        self.assertEqual(stats, {key: 9})
        self.assertEqual(labels, {key: "Strength"})
        self.assertEqual(source, "model")

    def test_ui_lists_base_stats(self):
        self.assertIn("setBase(obj,source)", (BASE / "item_forge_ui.js").read_text(encoding="utf-8"))
        self.assertIn("f-base-tag", (BASE / "item_forge_ui.js").read_text(encoding="utf-8"))

    def test_forge_target_tab_is_derived(self):
        self.assertEqual(editor._normalize_forge_target({"type": "equipped", "slot": 1}),
                         {"type": "equipped", "slot": 1, "tab": "equipped_items"})
        self.assertEqual(editor._normalize_forge_target({"type": "potions", "slot": 2}),
                         {"type": "potions", "slot": 2, "tab": "potions"})
        self.assertEqual(editor._normalize_forge_target({"type": "bag", "slot": 1, "tab": "main"}),
                         {"type": "bag", "slot": 1, "tab": "main"})
        self.assertEqual(editor._normalize_forge_target({"type": "stash", "tab": "stash_tab_2"}),
                         {"type": "stash", "tab": "stash_tab_2"})

    def test_owned_picker_sends_section_tabs(self):
        source = (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        self.assertIn("{type:'equipped',slot,tab:'equipped_items'}", source)
        self.assertIn("{type:'personal_stash',slot,tab:'personal_stash'}", source)
        self.assertIn("customForge?.name", source)

    def test_signature_items_are_valid_forge_configs(self):
        rows = editor.load_signature_items()
        self.assertTrue(rows, "hs_signature_items.json should offer at least one item")
        ids = {row["id"] for row in rows}
        self.assertTrue({"headhunter", "tyrant"} <= ids)
        for row in rows:
            cfg = row["config"]
            result = self.store.apply(self.selector, label=row["name"], stats=cfg["stats"], keep_native=cfg["keepNative"],
                                      lore=cfg["lore"], rarity=cfg["rarity"], mechanic=cfg["mechanic"], name=cfg["name"], affix=cfg["affix"])
            self.assertEqual(result["entry"]["name"], row["name"])
            self.assertEqual(result["entry"]["mechanic"], cfg["mechanic"])

    def test_signature_picker_is_wired(self):
        source = (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        self.assertIn("data-if-tab=\"signature\"", source)
        self.assertIn("/api/item-forge/signatures", source)

    def test_all_skills_class_needs_all_skills(self):
        with self.assertRaises(CustomForgeError):
            self.store.merge_presets([], {"21": 3}, keep_native=False)
        merged = self.store.merge_presets([], {"21": 3, "201": 2}, keep_native=False)
        self.assertEqual(merged["21"], 3)
        # a keep-native item may own 'to All Skills' natively, so the tag alone is allowed there
        merged = self.store.merge_presets([], {"21": 3}, keep_native=True)
        self.assertEqual(merged, {"21": 3})

    def test_ui_couples_all_skills_class_with_all_skills(self):
        source = (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        self.assertIn("family.includes(21)&&!selected.has('201')", source)
        self.assertIn("members.includes('201')&&selected.has('21')", source)

    def test_catalog_lines_use_verified_stat_names(self):
        labels = {line[0] for row in editor.CAT for line in (row.get("stats") or [])}
        self.assertNotIn("Stat #313", labels)
        self.assertNotIn("All Talents", labels)
        self.assertIn("to All Skills", labels)
        self.assertIn("Additional Casts with Skill Relics", labels)
        self.assertIn("Attack Damage", labels)  # ambiguous legacy label keeps its text
        self.assertGreater(editor.CATALOG_LINES_RENAMED, 1000)

    def test_ui_warns_about_socketables(self):
        # Socket bonuses are rebuilt by the game from the jewel's type and seed
        # (GenerateItemSpecialStats -> itemBaseSocketStatStruct), so forged stats on a rune,
        # gem or jewel never reach the host item; the forge must say so.
        source = (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        self.assertIn("data-socketable-note", source)
        self.assertIn("Number(baseItem.cls)===15", source)

    def test_ui_offers_the_name_field(self):
        self.assertIn('id="ifname" maxlength="48"', editor.HTML)
        self.assertNotIn('id="ifname" disabled', editor.HTML)
        self.assertIn("name:name||null", editor.HTML)

    def test_tier_travels_in_the_runtime_sidecar(self):
        merged = self.store.merge_presets([], {28: 60})
        result = self.store.apply(self.selector, label="crown", stats=merged, keep_native=True, rarity=10, tier=5)
        self.assertEqual(result["entry"]["tier"], 5)
        runtime = (self.root / "hs_custom_item_forge.runtime").read_text(encoding="utf-8")
        line = [row for row in runtime.splitlines() if row.startswith("item|")][0]
        self.assertIn("rarity=10;tier=5", line)
        for bad in ({"tier": 0}, {"tier": 6}, {"tier": "5"}):
            with self.subTest(bad=bad), self.assertRaises(CustomForgeError):
                self.store.apply(self.selector, label="x", stats=merged, keep_native=True, **bad)

    def test_ui_offers_the_tier_selector(self):
        source = (BASE / "item_forge_ui.js").read_text(encoding="utf-8")
        self.assertIn('id="iftier"', source)
        self.assertIn("tier:tier===''?null:+tier", source)
        rows = editor.load_signature_items()
        self.assertTrue(all(row["config"]["tier"] == 5 for row in rows))

    def test_ui_offers_the_mechanic_selector(self):
        self.assertIn('id="ifmech"', editor.HTML)
        self.assertIn("mechanic:q('ifmech').value", editor.HTML)


class CustomItemForgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.semantics = load_stat_semantics(
            BASE, expected_exe_sha256=EXPECTED_EXE_SHA256
        )
        cls.catalog = cls.semantics.decorate_catalog(
            load_custom_forge_catalog(BASE)
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CustomForgeStore(self.root, self.catalog, self.semantics)
        self.selector = item_selector(
            3, {"a": 4677950.0, "b": 2.0, "c": 1.0, "j": 13.0, "m": 1.0}
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_covers_advanced_keys_and_linked_unique_bundles(self):
        keys = {row["key"] for row in self.catalog["stats"]}
        self.assertGreaterEqual(len(keys), 300)
        self.assertIn(464, keys)
        striking = [
            prop
            for donor in self.catalog["donors"]
            for prop in donor["properties"]
            if prop["keys"] == [116, 117, 118]
        ]
        self.assertTrue(striking)
        self.assertTrue(all(prop["keys"] == [116, 117, 118] for prop in striking))
        self.assertTrue(all(set(prop["stats"]) == {"116", "117", "118"} for prop in striking))

    def test_skill_grant_donors_missing_a_class_key_are_offered_with_a_class_picker(self):
        full_catalog = json.loads((BASE / "hs_full_catalog.json").read_text(encoding="utf-8"))
        active_unique_ids = {
            int(row["id"])
            for row in full_catalog
            if row.get("kind") == "unique" and row.get("stats")
        }
        donor_ids = {int(row["catalogId"]) for row in self.catalog["donors"]}
        self.assertEqual(active_unique_ids - donor_ids, set())
        needs_class = [
            prop
            for donor in self.catalog["donors"]
            for prop in donor["properties"]
            if prop.get("needsClass")
        ]
        self.assertTrue(needs_class)
        for prop in needs_class:
            for key in prop["needsClass"]:
                self.assertEqual(self.semantics.get(key)["role"], "class_id")
                self.assertNotIn(str(key), prop["stats"])
        donor_names = {row["name"] for row in self.catalog["donors"]}
        self.assertIn("Blood of Spartan", donor_names)
        self.assertIn("Nomad's Rotten Corpse", donor_names)

    def test_undecoded_ids_are_hidden_from_the_normal_player_list(self):
        source = editor.HTML
        self.assertIn('type="checkbox" data-fe-unknown', source)
        self.assertIn("!row.advanced||showUnknown.checked", source)
        self.assertNotIn('data-fe-unknown checked', source)
        self.assertIn("row.safeEditable?'':'readonly'", source)
        self.assertTrue((BASE / "CLAUDE_CUSTOM_FORGE_STAT_DECODE_REQUEST.md").is_file())

    def test_atomic_source_and_runtime_files_round_trip(self):
        result = self.store.apply(
            self.selector,
            label="Poison Ivy",
            stats={20: 4, 116: 167, 117: 25, 118: 15},
            keep_native=True,
        )
        self.assertEqual(result["backup"], "")
        source = json.loads(self.store.json_path.read_text(encoding="utf-8"))
        self.assertEqual(len(source["items"]), 1)
        entry = next(iter(source["items"].values()))
        self.assertEqual(entry["stats"]["20"], 4)
        runtime = self.store.runtime_path.read_text(encoding="utf-8")
        self.assertTrue(runtime.startswith("HS_CUSTOM_ITEM_FORGE_V1\n"))
        self.assertIn("item|t=3;a=4677950;b=2;c=1;j=13|keep=1|", runtime)
        self.assertIn("20=4;116=167;117=25;118=15", runtime)

        second = self.store.apply(
            self.selector, label="Poison Ivy", stats={292: 1}, keep_native=False
        )
        self.assertIn("hs_custom_item_forge.json", second["backup"])
        self.assertIn("|keep=0|292=1", self.store.runtime_path.read_text(encoding="utf-8"))
        self.assertEqual(len(list(self.store.backup_dir.glob("*.bak"))), 2)

        removed = self.store.remove(self.selector)
        self.assertTrue(removed["removed"])
        self.assertEqual(
            self.store.runtime_path.read_text(encoding="utf-8"),
            "HS_CUSTOM_ITEM_FORGE_V1\n",
        )

    def test_preset_merges_every_linked_key_and_explicit_value_wins(self):
        preset = next(
            prop
            for donor in self.catalog["donors"]
            for prop in donor["properties"]
            if prop["keys"] == [116, 117, 118]
        )
        merged = self.store.merge_presets([preset["id"]], {118: 99})
        self.assertEqual(set(merged), {"116", "117", "118"})
        self.assertEqual(merged["118"], 99)

    def test_backup_retention_is_bounded(self):
        self.store.apply(
            self.selector, label="Poison Ivy", stats={28: 4}, keep_native=True
        )
        for index in range(43):
            self.store.apply(
                self.selector,
                label="Poison Ivy",
                stats={28: index + 5},
                keep_native=True,
            )
        self.assertLessEqual(len(list(self.store.backup_dir.glob("*.bak"))), 80)

    def test_selector_ignores_placement_and_retarget_follows_seed_changes(self):
        moved = item_selector(
            3,
            {
                "a": 4677950.0, "b": 2.0, "c": 1.0, "j": 13.0,
                "m": 999.0, "g": 3.0, "w": 0.0,
            },
        )
        self.assertEqual(moved, self.selector)
        self.store.apply(
            self.selector, label="Poison Ivy", stats={292: 1}, keep_native=True
        )
        changed = item_selector(
            3, {"a": 123456.0, "b": 2.0, "c": 1.0, "j": 13.0}
        )
        result = self.store.retarget(self.selector, changed)
        self.assertTrue(result["moved"])
        self.assertIsNone(self.store.get(self.selector))
        self.assertEqual(self.store.get(changed)["stats"], {"292": 1})

    def test_rejects_unobserved_nonfinite_or_unstable_data(self):
        with self.assertRaises(CustomForgeError):
            self.store.apply(
                self.selector, label="bad", stats={9999: 1}, keep_native=True
            )
        with self.assertRaises(CustomForgeError):
            self.store.apply(
                self.selector, label="bad", stats={20: math.nan}, keep_native=True
            )
        with self.assertRaises(CustomForgeError):
            item_selector(3, {"b": 2})

    def test_editor_api_rejects_arbitrary_read_only_stat_value(self):
        old_root = editor.ROOT
        old_store, old_store_path = (
            editor._CUSTOM_FORGE_STORE,
            editor._CUSTOM_FORGE_STORE_PATH,
        )
        try:
            editor.ROOT = self.root
            editor._CUSTOM_FORGE_STORE = None
            editor._CUSTOM_FORGE_STORE_PATH = None
            with patch.object(
                editor,
                "_custom_forge_target",
                return_value=(self.selector, {"name": "Poison Ivy"}, "save"),
            ), patch.object(editor, "game_running", return_value=False), patch.object(
                editor, "_runtime_save_barrier", return_value=None
            ):
                result = editor.op_custom_forge({
                    "action": "apply",
                    "stats": {"116": 9999},
                })
            self.assertIn("err", result)
            self.assertIn("read-only", result["err"])
            self.assertFalse((self.root / "hs_custom_item_forge.runtime").exists())
        finally:
            editor.ROOT = old_root
            editor._CUSTOM_FORGE_STORE = old_store
            editor._CUSTOM_FORGE_STORE_PATH = old_store_path

    def test_editor_operation_marks_shared_stash_item_without_rewriting_save(self):
        saves = self.root / "hs2saves"
        saves.mkdir()
        key = "0-0-1700000000000-3"
        document = {
            "stash_tab_1": {
                key: {
                    "pos": [0.0, 0.0],
                    "data": {
                        "a": 4677950.0, "b": 2.0, "c": 1.0,
                        "j": 13.0, "m": 1.0, "w": 1.0,
                    },
                }
            }
        }
        stash_path = saves / "stash.hss"
        stash_path.write_text(
            editor.encode_hss(json.dumps(document, separators=(", ", ": "))),
            encoding="ascii",
        )
        before = stash_path.read_bytes()
        old_root, old_saves = editor.ROOT, editor.SAVES
        old_store, old_store_path = (
            editor._CUSTOM_FORGE_STORE,
            editor._CUSTOM_FORGE_STORE_PATH,
        )
        try:
            editor.ROOT, editor.SAVES = self.root, saves
            editor._CUSTOM_FORGE_STORE = None
            editor._CUSTOM_FORGE_STORE_PATH = None
            with patch.object(editor, "game_running", return_value=False), patch.object(
                editor, "_runtime_save_barrier", return_value=None
            ):
                preset = next(
                    prop
                    for donor in editor.CUSTOM_FORGE_CATALOG["donors"]
                    for prop in donor["properties"]
                    if prop["keys"] == [116, 117, 118]
                )
                result = editor.op_custom_forge({
                    "action": "apply",
                    "target": {"type": "stash", "tab": "stash_tab_1"},
                    "key": key,
                    "presetIds": [preset["id"]],
                    "stats": preset["stats"],
                    "keepNative": True,
                })
                self.assertNotIn("err", result)
                self.assertEqual(stash_path.read_bytes(), before)
                payload = editor.read_stash()["stash_tab_1"][0]
                self.assertTrue(payload["customForge"]["active"])
                self.assertEqual(payload["customForge"]["statCount"], 3)
                self.assertEqual(payload["gameTooltip"]["customForge"]["statCount"], 3)
        finally:
            editor.ROOT, editor.SAVES = old_root, old_saves
            editor._CUSTOM_FORGE_STORE = old_store
            editor._CUSTOM_FORGE_STORE_PATH = old_store_path

    def test_item_forge_page_is_wired_and_create_route_validates(self):
        source = editor.HTML + (BASE / "hs_item_editor_gui.py").read_text(encoding="utf-8")
        self.assertIn('data-view="itemforge"', source)
        self.assertIn("async function openItemForge(preset)", source)
        # right-click "Custom Item Forge..." lands on the Item Forge view with the item chosen
        self.assertIn("openItemForge({ref:{target,key},label:where})", source)
        self.assertIn("openItemForge({ref:{vaultItemId:row.id}", source)
        self.assertNotIn("openCustomForge(target,key);return;", source)
        self.assertIn("function mountForgeEditor(", source)
        self.assertIn('"/api/item-forge/create"', source)
        self.assertIn('id="iflore"', source)
        self.assertIn('id="ifrarity"', source)
        self.assertEqual(editor.op_item_forge_create({"cid": 999999})["err"], "unknown catalog item")
        with patch.object(editor, "game_running", return_value=True):
            unique = next(i for i, row in enumerate(editor.CAT) if row.get("kind") == "unique")
            self.assertIn("running", editor.op_item_forge_create({"cid": unique})["err"])

    def test_lore_and_rarity_travel_in_the_runtime_sidecar(self):
        merged = self.store.merge_presets([], {28: 60})
        result = self.store.apply(
            self.selector, label="Lisa's Silence", stats=merged, keep_native=True,
            lore="Speak once more; and you will not speak again.", rarity=6,
        )
        self.assertEqual(result["entry"]["lore"], "Speak once more; and you will not speak again.")
        self.assertEqual(result["entry"]["rarity"], 6)
        runtime = (self.root / "hs_custom_item_forge.runtime").read_text(encoding="utf-8")
        line = [row for row in runtime.splitlines() if row.startswith("item|")][0]
        self.assertEqual(line.count("|"), 4)
        self.assertIn("|lore=Speak%20once%20more%3B%20and%20you%20will%20not%20speak%20again.;rarity=6", line)
        for bad in ({"rarity": 4}, {"rarity": "6"}, {"lore": "x" * 1001}, {"lore": "bad" + chr(0)}):
            with self.subTest(bad=bad), self.assertRaises(CustomForgeError):
                self.store.apply(self.selector, label="x", stats=merged, keep_native=True, **bad)


if __name__ == "__main__":
    unittest.main()
