"""AFK Vault quality-of-life: batch deposits, rarity clean-up, lite grid rows.

Only temporary databases are touched; Hero Siege is reported as closed unless a
test says otherwise.
"""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_vault_ingest import RARITY_BELTS, belt, editor, spool_record
from infinite_vault import InfiniteVault, VaultConflictError


def mixed_records() -> list[dict]:
    records = []
    seq = 0
    for name in ("Satanic", "Heroic", "Normal", "Angelic"):
        base, unique = RARITY_BELTS[name]
        for _ in range(4):
            seq += 1
            records.append(belt(seq, f"{name} belt {seq}", base, unique=unique))
    return records


class VaultAfkQolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (
            ("VAULT_DB_FILE", self.root / "vault.sqlite3"),
            ("SAVES", self.root / "saves"),
            ("_VAULT_STORE", None),
            ("_VAULT_STORE_PATH", None),
        ):
            patcher = patch.object(editor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        running = patch.object(editor, "game_running", return_value=False)
        self.game_running = running.start()
        self.addCleanup(running.stop)
        self.store = editor.vault_store()

    def ingest(self, records, expedition="exp_qol"):
        result = editor.op_vault_ingest({"expedition_id": expedition, "records": records})
        self.assertNotIn("err", result, result.get("err"))
        return result

    # --------------------------------------------------------- deferred layout
    def test_deferred_batches_are_laid_out_once_in_rarity_order(self):
        def batch(start, names):
            records = []
            for offset, name in enumerate(names):
                base, unique = RARITY_BELTS[name]
                records.append(belt(start + offset, f"{name} belt {start + offset}", base, unique=unique))
            return records

        first = editor.op_vault_ingest({
            "expedition_id": "exp_defer", "layout": "defer",
            "records": batch(1, ["Normal"] * 3 + ["Heroic"] * 3),
        })
        second = editor.op_vault_ingest({
            "expedition_id": "exp_defer", "layout": "defer",
            "records": batch(10, ["Heroic"] * 3 + ["Angelic"] * 3),
        })
        self.assertEqual((first["deposited"], second["deposited"]), (6, 6))
        category = first["collections"]["farm"]["id"]
        self.assertEqual(second["collections"]["farm"]["id"], category)
        self.assertTrue(all(row.page_index is None
                            for row in self.store.list_all_available_items(collection=category)))
        done = editor.op_vault_ingest({"expedition_id": "exp_defer", "records": [], "finalize": True})
        self.assertEqual((done["deposited"], done["collections"]["farm"]["pageName"]), (0, "Angelic"))
        pages = {p.page_index: p.name for p in self.store.list_stash_pages(category)}
        self.assertEqual(list(pages.values()), ["Angelic", "Heroic", "Normal"])
        for row in self.store.list_all_available_items(collection=category):
            self.assertEqual(pages[row.page_index], row.label.split(" belt ")[0])
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "x", "records": [], "layout": "later"}))
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "x", "records": [], "finalize": "yes"}))

    # ---------------------------------------------------------------- clean-up
    def test_clean_up_deletes_only_the_chosen_rarities_and_keeps_named_items(self):
        result = self.ingest(mixed_records())
        category = result["collections"]["farm"]["id"]
        rows = self.store.list_all_available_items(collection=category)
        named = next(row for row in rows if row.label.startswith("Satanic"))
        self.store.set_item_custom_name(named.id, "Keeper")
        preview = editor.op_vault_purge({
            "action": "preview", "collectionId": category, "groups": ["Satanic", "Normal"],
        })
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["itemCount"], 7)
        self.assertEqual(preview["groups"], {"Satanic": 3, "Normal": 4})
        self.assertEqual(preview["keptCustomNamed"], 1)
        self.assertNotIn("pageIndexes", preview)
        pages_before = {p.name: p.page_index for p in self.store.list_stash_pages(category)}
        done = editor.op_vault_purge({
            "action": "delete", "collectionId": category, "groups": ["Satanic", "Normal"],
            "previewToken": preview["previewToken"], "removeEmptied": True,
        })
        self.assertNotIn("err", done, done.get("err"))
        self.assertEqual(done["itemCount"], 7)
        self.assertEqual(done["backup"], done["backupName"])
        # The Normal stash emptied and went; the Satanic one keeps the named item.
        self.assertEqual(done["removedPageIndexes"], [pages_before["Normal"]])
        self.assertEqual([p.name for p in self.store.list_stash_pages(category)],
                         ["Angelic", "Heroic", "Satanic"])
        left = sorted(row.label for row in self.store.list_all_available_items(collection=category))
        self.assertEqual(left, sorted(
            [row.label for row in rows if row.label.startswith(("Heroic", "Angelic"))] + [named.label]
        ))
        backup = self.store.path.with_name(done["backupName"])
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 16)
        # Transferring the same expedition again cannot bring them back.
        again = self.ingest(mixed_records())
        self.assertEqual((again["deposited"], again["duplicate"]), (0, 16))
        self.assertEqual(self.store.count_items(collection=category), 9)
        self.assertEqual(self.store.list_events(limit=5)[0]["eventType"], "items_purged")
        self.assertIsNone(self.store.preview_metadata_undo())

    def test_clean_up_refuses_a_stale_preview_and_a_running_game(self):
        result = self.ingest(mixed_records())
        category = result["collections"]["farm"]["id"]
        body = {"action": "preview", "collectionId": category, "groups": ["Satanic"]}
        preview = editor.op_vault_purge(body)
        self.ingest([belt(90, "Satanic belt 90", 0)])
        stale = editor.op_vault_purge({**body, "action": "delete", "previewToken": preview["previewToken"]})
        self.assertIn("changed", stale["err"])
        self.assertEqual(self.store.count_items(collection=category), 17)
        self.game_running.return_value = True
        refused = editor.op_vault_purge(body)
        self.assertIn("Close Hero Siege", refused["err"])
        for bad in ({"groups": []}, {"groups": ["Legendary"]}, {"collectionId": "1"}, {"action": "wipe"}):
            self.assertIn("err", editor.op_vault_purge({**body, **bad}))
        self.assertEqual(self.store.count_items(collection=category), 17)

    # ---------------------------------------------------------- lite grid rows
    def test_lite_rows_carry_the_grid_fields_and_tooltips_come_on_demand(self):
        result = self.ingest(mixed_records())
        category = result["collections"]["farm"]["id"]
        query = {"offset": ["0"], "limit": ["5000"], "collectionId": [str(category)]}
        full = editor.vault_items(query)
        lite = editor.vault_items({**query, "lite": ["1"]})
        self.assertEqual(lite["total"], full["total"])
        self.assertEqual(lite["stashes"], full["stashes"])
        fields = ("id", "name", "rar", "cls", "clsName", "cid", "spr", "w", "h", "stack",
                  "stackable", "pageIndex", "pos", "customName", "updatedAt")
        for heavy, light in zip(full["items"], lite["items"]):
            self.assertEqual({k: heavy[k] for k in fields}, {k: light[k] for k in fields})
            self.assertNotIn("gameTooltip", light)
            self.assertIn(light["group"], editor.VAULT_RARITY_GROUPS)
        self.assertLess(len(json.dumps(lite)), len(json.dumps(full)) / 3)
        ids = [row["id"] for row in full["items"][:5]]
        tooltips = editor.vault_tooltips({"ids": [",".join(ids)]})["tooltips"]
        self.assertEqual(set(tooltips), set(ids))
        for row in full["items"][:5]:
            self.assertEqual(tooltips[row["id"]], row["gameTooltip"])
        self.assertIn("err", editor.vault_tooltips({"ids": ["not-an-id"]}))
        too_many = ",".join("%032x" % n for n in range(editor.VAULT_TOOLTIP_BATCH + 1))
        self.assertIn("err", editor.vault_tooltips({"ids": [too_many]}))

    def test_tooltip_models_do_not_share_state_with_the_database(self):
        self.ingest(mixed_records()[:1])
        record = self.store.list_items()[0]
        first = editor._vault_item_payload(record)["gameTooltip"]
        for line in first.get("stats") or []:
            line["label"] = "changed"
        first["item"]["name"] = "changed"
        second = editor._vault_item_payload(record)["gameTooltip"]
        self.assertNotEqual(second["item"]["name"], "changed")
        self.assertTrue(all(line.get("label") != "changed" for line in second.get("stats") or []))

    # ------------------------------------------------------------ layout plan
    def test_first_fit_start_hint_gives_the_same_layout_as_a_full_scan(self):
        def reference(records, sizes):
            occupied, planned = {}, {}
            pages = [0]

            def free(page, x, y, w, h):
                cells = occupied.setdefault(page, set())
                return x + w <= editor.VAULT_GRID_COLUMNS and y + h <= editor.VAULT_GRID_ROWS and all(
                    (cx, cy) not in cells for cy in range(y, y + h) for cx in range(x, x + w))

            for record in records:
                w, h = sizes[record.id]
                index = 0
                while True:
                    page = pages[index]
                    spot = next(((x, y) for y in range(editor.VAULT_GRID_ROWS - h + 1)
                                 for x in range(editor.VAULT_GRID_COLUMNS - w + 1)
                                 if free(page, x, y, w, h)), None)
                    if spot:
                        break
                    index += 1
                    if index == len(pages):
                        pages.append(pages[-1] + 1)
                occupied[page].update((cx, cy) for cy in range(spot[1], spot[1] + h)
                                      for cx in range(spot[0], spot[0] + w))
                planned[record.id] = (page, spot[0], spot[1], w, h)
            return planned

        generator = random.Random(7)
        shapes = [(1, 1), (1, 2), (2, 2), (2, 3), (2, 4), (1, 3)]
        records, sizes = [], {}
        for index in range(900):
            record = SimpleNamespace(
                id=f"{index:032x}", page_index=None, layout_x=None, layout_y=None,
                created_at=f"2026-09-23T00:00:{index:06d}",
            )
            records.append(record)
            sizes[record.id] = generator.choice(shapes)
        with patch.object(editor, "_vault_record_size", lambda record: sizes[record.id]):
            planned = editor._vault_layout_plan(records)
        self.assertEqual(planned, reference(records, sizes))

    # ------------------------------------------------------------- storage API
    def test_deposit_many_reports_each_entry_and_takes_one_backup(self):
        raw = json.dumps({"pos": [0.0, 0.0], "data": {"a": 1.0, "b": 2.0, "c": 0.0, "j": 0.0}})
        other = json.dumps({"pos": [0.0, 0.0], "data": {"a": 9.0, "b": 2.0, "c": 0.0, "j": 0.0}})
        key = "afk-test-000000000000000000000001"
        first = self.store.deposit("Vault", raw, deposit_key=key)
        with patch.object(InfiniteVault, "_backup_existing", autospec=True,
                          side_effect=InfiniteVault._backup_existing) as backups:
            results = self.store.deposit_many("Vault", [
                {"raw_item_json": raw, "deposit_key": key},
                {"raw_item_json": other, "deposit_key": key},
                {"raw_item_json": "{not json", "deposit_key": "afk-test-000000000000000000000002"},
                {"raw_item_json": raw, "deposit_key": "afk-test-000000000000000000000003", "label": "new"},
                {"raw_item_json": other},
            ])
        self.assertEqual(backups.call_count, 1)
        self.assertEqual([r["status"] for r in results],
                         ["duplicate", "conflict", "invalid", "deposited", "deposited"])
        self.assertEqual(results[0]["record"].id, first.id)
        self.assertEqual(results[3]["record"].label, "new")
        self.assertIsNone(results[2]["record"])
        self.assertEqual(self.store.count_items(), 3)
        self.assertEqual([r.id for r in self.store.get_items([results[4]["record"].id, first.id])],
                         [results[4]["record"].id, first.id])

    def test_named_layout_creates_pages_and_never_renames_a_players_stash(self):
        category = self.store.create_collection("Layout", marker={"afkExpedition": "x-1"})
        self.assertEqual(self.store.find_marked_collection("afkExpedition", "x-1").id, category.id)
        self.assertIsNone(self.store.find_marked_collection("afkExpedition", "x-"))
        raw = json.dumps({"pos": [0.0, 0.0], "data": {"a": 1.0, "b": 2.0, "c": 0.0, "j": 0.0}})
        item = self.store.deposit(category.id, raw)
        outcome = self.store.apply_named_layout(
            category.id, {0: "Heroic", 1: "Heroic"},
            [{"itemId": item.id, "pageIndex": 1, "x": 2, "y": 3}],
        )
        self.assertEqual(outcome, {"created": [1], "renamed": [0], "changed": 1})
        pages = self.store.list_stash_pages(category.id)
        self.assertEqual([p.name for p in pages], ["Heroic", "Heroic (2)"])
        self.store.rename_stash_page(category.id, 1, "Mine")
        self.store.apply_named_layout(category.id, {1: "Satanic"}, [])
        self.assertEqual(self.store.list_stash_pages(category.id)[1].name, "Mine")
        with self.assertRaises(VaultConflictError):
            self.store.apply_named_layout(category.id, {}, [{"itemId": item.id, "pageIndex": 7, "x": 0, "y": 0}])
        self.store.rename_collection(category.id, "Renamed")
        self.assertEqual(self.store.find_marked_collection("afkExpedition", "x-1").name, "Renamed")


class AfkStackCountTests(unittest.TestCase):
    """A native stack keeps its count in the Vault (Prospector fragments come as
    stacks of up to 999); a stackable drop without a count stays a single."""

    setUp = VaultAfkQolTests.setUp
    ingest = VaultAfkQolTests.ingest

    def test_stack_counts_are_kept_and_bad_counts_are_skipped(self):
        stacks = [
            spool_record(1, 14, "Satanic Crystal Fragment", {"o": 999.0, "b": 60.0, "a": 11.0, "j": 0, "c": 0.0}),
            spool_record(2, 14, "Satanic Crystal Fragment", {"o": 192.0, "b": 60.0, "a": 12.0, "j": 0, "c": 0.0}),
            spool_record(3, 14, "Crystal", {"n": 2.0, "b": 29, "a": 13.0, "j": 0, "c": 0.0}),
            spool_record(4, 14, "Too many", {"o": 1000.0, "b": 60.0, "a": 14.0, "j": 0, "c": 0.0}),
            spool_record(5, 14, "Half", {"o": 2.5, "b": 60.0, "a": 15.0, "j": 0, "c": 0.0}),
        ]
        result = self.ingest(stacks, "exp_stacks")
        self.assertEqual(result["deposited"], 3)
        self.assertEqual(sorted(entry["seq"] for entry in result["skipped"]), [4, 5])
        counts = {}
        for record in self.store.list_all_available_items():
            counts[record.label] = counts.get(record.label, []) + [record.decoded_item()["data"]["o"]]
        self.assertEqual(sorted(counts["Satanic Crystal Fragment"]), [192.0, 999.0])
        self.assertEqual(counts["Crystal"], [1.0])


def material(seq, base, amount=None, name="Crystal"):
    definition = {"b": float(base), "a": float(1000 + seq), "j": 0, "c": 0.0}
    if amount is not None:
        definition["o"] = float(amount)
    return spool_record(seq, 14, name, definition)


class VaultStackTests(unittest.TestCase):
    """Stackable items of one kind merge into native stacks of up to 999."""

    setUp = VaultAfkQolTests.setUp
    ingest = VaultAfkQolTests.ingest

    def amounts(self, collection):
        return sorted((r.label, r.decoded_item()["data"].get("o", 1.0))
                      for r in self.store.list_all_available_items(collection=collection))

    def test_afk_materials_arrive_stacked_and_a_repeat_brings_nothing_back(self):
        records = [material(1, 29), material(2, 29), material(3, 29, 4), material(4, 30, name="Other")]
        self.ingest(records, "exp_stack")
        materials = editor._afk_find_collection(self.store, editor.AFK_INGEST_MATERIALS_COLLECTION)
        self.assertEqual(self.amounts(materials.id), [("Crystal", 6.0), ("Other", 1.0)])
        again = self.ingest(records, "exp_stack")
        self.assertEqual((again["deposited"], again["duplicate"]), (0, 4))
        self.assertEqual(self.amounts(materials.id), [("Crystal", 6.0), ("Other", 1.0)])

    def test_compact_merges_up_to_999_and_leaves_named_singletons_and_large_stacks(self):
        category = self.store.create_collection("Mats")
        entries = [material(i, 29, 400) for i in range(1, 5)] + [
            material(10, 59, name="Reflection"), material(11, 59, name="Reflection"),
            material(12, 29, 999, name="Huge"), material(13, 29, name="Named")]
        prepared = [editor._afk_prepare_record("mats", entry) for entry in entries]
        # A manual stack larger than the native maximum (the editor allows it) is left alone.
        huge = next(p for p in prepared if p["label"] == "Huge")
        huge["raw"] = huge["raw"].replace('"o":999.0', '"o":5000.0')
        results = self.store.deposit_many(category.id, [{
            "raw_item_json": p["raw"], "source_item_key": p["key"], "label": p["label"], "source": "test",
            "deposit_key": p["depositKey"]} for p in prepared])
        self.assertTrue(all(r["status"] == "deposited" for r in results))
        named = next(r["record"] for r in results if r["record"].label == "Named")
        self.store.set_item_custom_name(named.id, "Keep me")
        done = editor.op_vault_layout({"action": "compact", "collectionId": category.id})
        self.assertNotIn("err", done, done.get("err"))
        self.assertIn("merged into existing stacks", done["ok"])
        self.assertEqual(self.amounts(category.id), [
            ("Crystal", 601.0), ("Crystal", 999.0), ("Huge", 5000.0), ("Named", 1.0),
            ("Reflection", 1.0), ("Reflection", 1.0)])
        self.assertTrue(list(self.root.glob("vault.sqlite3.before-items-stacked-*.bak")))
        undo = self.store.preview_metadata_undo()
        self.assertTrue(undo is None or undo["eventType"] == "collection_layout_updated")


class AfkDismantleTests(unittest.TestCase):
    """DISMANTLE on an AFK stash: the Prospector's break-down for Satanic and
    above, below Satanic deleted, fragments stacked into AFK Materials."""

    setUp = VaultAfkQolTests.setUp
    ingest = VaultAfkQolTests.ingest

    def spool(self, expedition, facts):
        folder = self.root / "afk" / "spool"
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / f"{expedition}.ndjson").open("w", encoding="utf-8") as stream:
            for seq, (cls, rarity, tier) in sorted(facts.items()):
                stream.write(json.dumps({"expedition_id": expedition, "seq": seq, "kind": "item", "type": cls, "item": {
                    "itemType": float(cls), "itemInfoStruct": {"27": rarity, "32": tier},
                    "itemDefinitionStruct": {"b": 0.0, "a": 1.0}}}, separators=(",", ":")) + "\n")

    def page(self, category, name):
        return next(p.page_index for p in self.store.list_stash_pages(category) if p.name == name)

    def run_dismantle(self, category, page):
        preview = editor.op_vault_dismantle({"action": "preview", "collectionId": category, "pageIndex": page})
        self.assertNotIn("err", preview, preview.get("err"))
        done = editor.op_vault_dismantle({"action": "dismantle", "collectionId": category, "pageIndex": page,
                                          "previewToken": preview["previewToken"]})
        self.assertNotIn("err", done, done.get("err"))
        return preview, done

    def test_satanic_and_above_become_fragments_and_lower_items_are_deleted(self):
        self.ingest([material(1, 60, 990, name="Satanic Crystal Fragment")], "exp_mats")
        gear = [belt(1, "Satanic belt 1", 0), belt(2, "Satanic belt 2", 0), belt(3, "Normal belt 3", 0, unique=False),
                belt(4, "Heroic belt 4", 12), belt(5, "Satanic belt 5", 0)]
        result = self.ingest(gear, "exp_d")
        self.spool("exp_d", {1: (8, 6, 1), 2: (8, 6, 3), 3: (8, 2, 0), 4: (8, 9, 4), 5: (8, 6, 2)})
        category = result["collections"]["farm"]["id"]
        named = next(r for r in self.store.list_all_available_items(collection=category) if r.label == "Satanic belt 5")
        self.store.set_item_custom_name(named.id, "Keeper")

        preview, done = self.run_dismantle(category, self.page(category, "Satanic"))
        self.assertEqual((preview["dismantle"], preview["delete"], preview["keptNamed"], preview["fragments"], preview["fromRecords"]),
                         (2, 0, 1, 38, 2))
        self.assertTrue(list(self.root.glob("vault.sqlite3.before-items-dismantled-*.bak")))
        materials = editor._afk_find_collection(self.store, editor.AFK_INGEST_MATERIALS_COLLECTION)
        stacks = sorted(r.decoded_item()["data"]["o"] for r in self.store.list_all_available_items(collection=materials.id))
        self.assertEqual(stacks, [29.0, 999.0], "the partial stack is topped up to 999, the rest starts a new one")

        self.run_dismantle(category, self.page(category, "Normal"))
        preview, _ = self.run_dismantle(category, self.page(category, "Heroic"))
        self.assertEqual((preview["dismantle"], preview["random"]), (1, 1))
        names = sorted(r.label for r in self.store.list_all_available_items(collection=materials.id))
        self.assertTrue(set(names) - {"Satanic Crystal Fragment"} <= {"Gypsy's Fragment", "Mallet Fragment"})
        self.assertEqual(len(names), 3)
        left = [r.label for r in self.store.list_all_available_items(collection=category)]
        self.assertEqual(left, ["Satanic belt 5"], "only the custom-named item stays")
        again = self.ingest(gear, "exp_d")
        self.assertEqual(again["deposited"], 0, "a repeated transfer cannot bring dismantled items back")
        undo = self.store.preview_metadata_undo()
        self.assertTrue(undo is None or undo["eventType"] == "collection_layout_updated")

    def test_refusals_and_the_catalog_fallback(self):
        result = self.ingest([belt(1, "Normal belt 1", 0, unique=False), belt(2, "Satanic belt 2", 0)], "exp_c")
        category = result["collections"]["farm"]["id"]
        other = self.store.create_collection("Not AFK")
        self.assertIn("err", editor.op_vault_dismantle({"action": "preview", "collectionId": other.id, "pageIndex": 0}))
        self.assertIn("err", editor.op_vault_dismantle({"action": "boom", "collectionId": category, "pageIndex": 0}))
        page = self.page(category, "Normal")
        preview = editor.op_vault_dismantle({"action": "preview", "collectionId": category, "pageIndex": page})
        self.assertEqual((preview["delete"], preview["fromRecords"]), (1, 0), "without AFK records the catalog decides")
        self.ingest([material(7, 60, 3, name="Satanic Crystal Fragment")], "exp_m2")
        stale = editor.op_vault_dismantle({"action": "dismantle", "collectionId": category, "pageIndex": page,
                                           "previewToken": "0" * 64})
        self.assertIn("err", stale)
        self.assertEqual(len(self.store.list_all_available_items(collection=category)), 2)
        self.assertTrue(editor.vault_meta()["collections"][[c["id"] for c in editor.vault_meta()["collections"]].index(category)]["afk"])

    def test_a_whole_category_by_rarity(self):
        self.ingest([material(1, 60, 990, name="Satanic Crystal Fragment")], "exp_mats")
        gear = [belt(1, "Satanic belt 1", 0), belt(2, "Satanic belt 2", 0), belt(3, "Normal belt 3", 0, unique=False),
                belt(4, "Heroic belt 4", 12), belt(5, "Satanic belt 5", 0), belt(6, "Angelic belt 6", 44)]
        result = self.ingest(gear, "exp_all")
        self.spool("exp_all", {1: (8, 6, 1), 2: (8, 6, 3), 3: (8, 2, 0), 4: (8, 9, 4), 5: (8, 6, 2), 6: (8, 7, 0)})
        category = result["collections"]["farm"]["id"]
        named = next(r for r in self.store.list_all_available_items(collection=category) if r.label == "Satanic belt 5")
        self.store.set_item_custom_name(named.id, "Keeper")
        spare = self.store.add_stash_page(category)
        self.store.rename_stash_page(category, spare.page_index, "Already empty")
        stashes_before = {p.name for p in self.store.list_stash_pages(category)}
        self.assertTrue({"Satanic", "Normal", "Heroic", "Angelic", "Already empty"} <= stashes_before)

        body = {"collectionId": category, "groups": ["Satanic", "Normal", "Heroic"], "removeEmptied": True}
        preview = editor.op_vault_dismantle({**body, "action": "preview"})
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(
            (preview["dismantle"], preview["delete"], preview["keptNamed"], preview["fragments"], preview["random"]),
            (3, 1, 1, 38, 1),
        )
        self.assertEqual(preview["groups"]["Satanic"], {"dismantle": 2, "delete": 0, "keep": 0, "keptNamed": 1})
        self.assertEqual(preview["emptyStashes"], 3, "Normal and Heroic empty out; one stash was empty already")
        self.assertNotIn("Angelic", preview["groups"], "an unticked rarity is not touched")
        changed = editor.op_vault_dismantle({**body, "groups": ["Satanic", "Heroic"], "action": "dismantle",
                                             "previewToken": preview["previewToken"]})
        self.assertIn("err", changed, "a different choice needs its own review")

        done = editor.op_vault_dismantle({**body, "action": "dismantle", "previewToken": preview["previewToken"]})
        self.assertNotIn("err", done, done.get("err"))
        self.assertEqual(done["removedStashes"], 3)
        self.assertIn("removed 3 empty stashes", done["ok"])
        self.assertIn("Satanic Crystal Fragment", done["ok"], "fragment names keep their capitals")
        left = sorted(r.label for r in self.store.list_all_available_items(collection=category))
        self.assertEqual(left, ["Angelic belt 6", "Satanic belt 5"], "the unticked Angelic and the named item stay")
        stashes_after = {p.name for p in self.store.list_stash_pages(category)}
        self.assertEqual(stashes_before - stashes_after, {"Normal", "Heroic", "Already empty"},
                         "the stash with the kept item stays")
        materials = editor._afk_find_collection(self.store, editor.AFK_INGEST_MATERIALS_COLLECTION)
        crystal = sorted(r.decoded_item()["data"]["o"] for r in self.store.list_all_available_items(collection=materials.id)
                         if r.label == "Satanic Crystal Fragment")
        self.assertEqual(crystal, [29.0, 999.0])
        self.assertTrue(list(self.root.glob("vault.sqlite3.before-items-dismantled-*.bak")))
        self.assertEqual(self.ingest(gear, "exp_all")["deposited"], 0, "a repeated transfer cannot bring them back")
        dismantled = next(e for e in self.store.list_events(limit=5) if e["eventType"] == "items_dismantled")
        self.assertEqual(dismantled["details"]["rarities"], ["Heroic", "Normal", "Satanic"])
        self.assertEqual(sum(len(pages) for pages in dismantled["details"]["removedPages"].values()), 3)

    def test_the_last_stash_of_a_category_stays(self):
        result = self.ingest([belt(1, "Satanic belt 1", 0), belt(2, "Satanic belt 2", 0)], "exp_one")
        self.spool("exp_one", {1: (8, 6, 0), 2: (8, 6, 1)})
        category = result["collections"]["farm"]["id"]
        body = {"collectionId": category, "groups": ["Satanic"], "removeEmptied": True}
        self.store.add_stash_page(category)
        preview = editor.op_vault_dismantle({**body, "action": "preview"})
        self.assertEqual(preview["emptyStashes"], 1, "two stashes end up empty; one of them stays")
        done = editor.op_vault_dismantle({**body, "action": "dismantle", "previewToken": preview["previewToken"]})
        self.assertNotIn("err", done, done.get("err"))
        self.assertEqual(self.store.list_all_available_items(collection=category), [])
        self.assertEqual([p.page_index for p in self.store.list_stash_pages(category)], [0], "the first stash stays")
        for bad in ({"groups": []}, {"groups": ["Bogus"]}, {"groups": ["Satanic"], "pageIndex": 0}, {}):
            self.assertIn("err", editor.op_vault_dismantle({"action": "preview", "collectionId": category, **bad}), bad)


class AfkFarmSplitTests(unittest.TestCase):
    """The shared legacy AFK Farm category is split into one category per
    expedition, each laid out on rarity stashes like a new import."""

    setUp = VaultAfkQolTests.setUp
    ingest = VaultAfkQolTests.ingest

    def legacy_farm(self):
        farm = self.store.create_collection("AFK Farm")
        self.store.rename_stash_page(farm.id, 0, "exp_a · 2026-09-17 · Test A")
        self.ingest([belt(1, "Heroic belt 1", 12), belt(2, "Satanic belt 2", 0), belt(3, "Angelic belt 3", 44)], "exp_a")
        page = self.store.add_stash_page(farm.id)
        self.store.rename_stash_page(farm.id, page.page_index, "exp_b · 2026-09-18")
        self.ingest([belt(1, "Heroic belt b1", 12), belt(2, "Normal belt b2", 0, unique=False)], "exp_b")
        self.assertIsNone(self.store.find_marked_collection(editor.AFK_INGEST_MARKER, "exp_a"), "legacy fixture")
        return farm

    def keep_manual_item(self, farm):
        entry = editor._afk_prepare_record("manual", belt(9, "Kept belt", 12))
        [result] = self.store.deposit_many(farm.id, [{
            "raw_item_json": entry["raw"], "source_item_key": entry["key"], "label": "Kept belt",
            "source": "manual", "deposit_key": None,
        }])
        self.assertEqual(result["status"], "deposited")

    def placements(self):
        return sorted((r.id, r.collection_id, r.page_index, r.layout_x, r.layout_y)
                      for r in self.store.list_all_available_items())

    def test_preview_describes_each_expedition_and_changes_nothing(self):
        farm = self.legacy_farm()
        self.keep_manual_item(farm)
        before = self.placements()
        preview = editor.op_vault_afk_split({"action": "preview"})
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual((preview["collectionName"], preview["itemCount"], preview["unassigned"]), ("AFK Farm", 5, 1))
        self.assertEqual(
            [(g["expeditionId"], g["categoryName"], g["itemCount"]) for g in preview["groups"]],
            [("exp_a", "AFK · 2026-09-17 · Test A", 3), ("exp_b", "AFK · 2026-09-18 · exp_b", 2)],
        )
        self.assertEqual(self.placements(), before)
        self.assertEqual([c.name for c in self.store.list_collections()].count("AFK · 2026-09-17 · Test A"), 0)

    def test_split_moves_each_expedition_to_rarity_stashes_and_keeps_other_items(self):
        farm = self.legacy_farm()
        self.keep_manual_item(farm)
        preview = editor.op_vault_afk_split({"action": "preview"})
        result = editor.op_vault_afk_split({"action": "split", "previewToken": preview["previewToken"]})
        self.assertNotIn("err", result, result.get("err"))
        self.assertEqual((result["itemCount"], result["unassigned"], result["removedFarm"]), (5, 1, False))
        self.assertTrue(list(self.root.glob("vault.sqlite3.before-split-*.bak")), "a dedicated backup is kept")
        by_name = {}
        for expedition, stashes in (("exp_a", ["Angelic", "Heroic", "Satanic"]), ("exp_b", ["Heroic", "Normal"])):
            category = self.store.find_marked_collection(editor.AFK_INGEST_MARKER, expedition)
            pages = {p.page_index: p.name for p in self.store.list_stash_pages(category.id)}
            rows = self.store.list_all_available_items(collection=category.id)
            self.assertEqual(sorted(pages[r.page_index] for r in rows), stashes, expedition)
            self.assertTrue(all(r.layout_x is not None for r in rows))
            by_name[expedition] = category
        self.assertEqual(by_name["exp_a"].name, "AFK · 2026-09-17 · Test A")
        self.assertEqual([r.label for r in self.store.list_all_available_items(collection=farm.id)], ["Kept belt"])
        self.assertEqual(len(self.store.list_stash_pages(farm.id)), 1, "emptied AFK Farm stashes are removed, one is kept")
        more = self.ingest([belt(7, "Heroic belt 7", 12)], "exp_a")
        self.assertEqual(more["deposited"], 1)
        [late] = [r for r in self.store.list_all_available_items() if r.label == "Heroic belt 7"]
        self.assertEqual(late.collection_id, by_name["exp_a"].id, "a later transfer continues in the expedition's category")

    def test_a_stale_preview_or_a_running_game_moves_nothing(self):
        self.legacy_farm()
        preview = editor.op_vault_afk_split({"action": "preview"})
        self.ingest([belt(5, "Heroic belt 5", 12)], "exp_a")
        before = self.placements()
        stale = editor.op_vault_afk_split({"action": "split", "previewToken": preview["previewToken"]})
        self.assertIn("err", stale)
        self.assertEqual(self.placements(), before)
        self.game_running.return_value = True
        self.assertIn("err", editor.op_vault_afk_split({"action": "preview"}))
        self.assertIn("err", editor.op_vault_afk_split({"action": "unknown"}))

    def test_an_emptied_afk_farm_is_removed_and_undo_stops_at_the_split(self):
        self.legacy_farm()
        preview = editor.op_vault_afk_split({"action": "preview"})
        result = editor.op_vault_afk_split({"action": "split", "previewToken": preview["previewToken"]})
        self.assertTrue(result["removedFarm"], result)
        self.assertNotIn("AFK Farm", [c.name for c in self.store.list_collections()])
        with closing(sqlite3.connect(self.root / "vault.sqlite3")) as connection:
            [(split_id,)] = connection.execute("SELECT id FROM events WHERE event_type='items_split'").fetchall()
        undo = self.store.preview_metadata_undo()
        self.assertTrue(undo is None or undo["eventId"] > split_id, "nothing older than the split can be undone")

    def test_nothing_to_split_without_afk_farm(self):
        self.assertIn("err", editor.op_vault_afk_split({"action": "preview"}))


def stackable(seq, cls, base, amount, name):
    return spool_record(seq, cls, name, {"b": float(base), "a": float(5000 + seq), "j": 0, "c": 0.0, "o": float(amount)})


class AfkCampTakeTests(unittest.TestCase):
    """AFK FARM's camp takes keys and jeweler materials out of AFK Materials:
    all or nothing, smaller stacks first, at most once per request id."""

    setUp = VaultAfkQolTests.setUp
    ingest = VaultAfkQolTests.ingest

    def fill(self):
        self.records = [
            stackable(1, 12, 0, 999, "Basic Key"), stackable(2, 12, 0, 5, "Basic Key"),
            stackable(3, 12, 1, 27, "Crystal Key"), stackable(4, 14, 5, 40, "Jewel material"),
            stackable(5, 14, 60, 300, "Satanic Crystal Fragment"), stackable(6, 15, 1, 3, "Rune"),
        ]
        self.ingest(self.records, "exp_keys")
        self.materials = editor._afk_find_collection(self.store, editor.AFK_INGEST_MATERIALS_COLLECTION)

    def camp(self, action, **body):
        return editor.op_vault_afk_take({"action": action, **body})

    def take(self, request_id, items, **extra):
        return self.camp("take", requestId=request_id, items=items, **extra)

    def counts(self):
        return {(row["cls"], row["base"]): row["count"] for row in self.camp("stock")["stock"]}

    def basic_key_stacks(self):
        return sorted(r.decoded_item()["data"]["o"] for r in self.store.list_all_available_items(collection=self.materials.id)
                      if r.source_item_key.endswith("-12") and r.decoded_item()["data"]["b"] == 0.0)

    def test_stock_lists_only_keys_and_jeweler_materials(self):
        self.assertEqual(self.camp("stock"), {"category": None, "stock": []})
        self.fill()
        stock = self.camp("stock")
        self.assertEqual(stock["category"], "AFK Materials")
        self.assertEqual({(row["cls"], row["base"]): (row["count"], row["stacks"]) for row in stock["stock"]},
                         {(12, 0): (1004, 2), (12, 1): (27, 1), (14, 5): (40, 1)})
        self.assertTrue(all(row["name"] for row in stock["stock"]))

    def test_a_take_uses_up_the_smaller_stack_first_and_happens_once(self):
        self.fill()
        request = "camp-take-0001-abcdef"
        first = self.take(request, [{"cls": 12, "base": 0, "count": 7}, {"cls": 12, "base": 1, "count": 2}], purpose="Key rack")
        self.assertNotIn("err", first, first.get("err"))
        self.assertEqual((first["state"], first["replayed"]), ("done", False))
        self.assertIn("ok", first)
        self.assertEqual([(t["cls"], t["base"], t["count"]) for t in first["taken"]], [(12, 0, 7), (12, 1, 2)])
        self.assertEqual(self.basic_key_stacks(), [997.0])
        self.assertEqual(self.counts(), {(12, 0): 997, (12, 1): 25, (14, 5): 40})
        self.assertEqual({(row["cls"], row["base"]): row["count"] for row in first["stock"]}, self.counts())

        again = self.take(request, [{"cls": 12, "base": 0, "count": 7}])
        self.assertEqual((again["state"], again["replayed"], again["taken"], again["eventId"]),
                         ("done", True, first["taken"], first["eventId"]))
        self.assertNotIn("ok", again)
        status = self.camp("status", requestId=request)
        self.assertEqual((status["state"], status["taken"]), ("done", first["taken"]))
        self.assertEqual(self.counts()[(12, 0)], 997)
        # The used-up stack's AFK import cannot come back with a repeated transfer.
        self.assertEqual(self.ingest(self.records, "exp_keys")["deposited"], 0)
        self.assertEqual(self.counts()[(12, 0)], 997)
        with patch.object(editor, "_afk_take_stock", side_effect=sqlite3.OperationalError("busy")):
            late = self.take("camp-take-0008-abcdef", [{"cls": 12, "base": 1, "count": 1}])
        self.assertEqual((late["state"], late.get("err")), ("done", None))  # committed: never an error reply
        self.assertNotIn("stock", late)
        event = next(e for e in self.store.list_events(limit=5)
                     if e["eventType"] == "afk_items_taken" and e["details"]["requestId"] == request)
        self.assertEqual((event["details"]["requestId"], event["details"]["purpose"], event["details"]["removed"]),
                         (request, "Key rack", 1))

    def test_nothing_is_taken_when_any_part_is_missing_or_not_allowed(self):
        self.fill()
        before = self.counts()
        short = self.take("camp-take-0002-abcdef", [{"cls": 12, "base": 0, "count": 3}, {"cls": 12, "base": 1, "count": 28}])
        self.assertIn("27", short.get("err", ""))
        for items in ([{"cls": 14, "base": 60, "count": 1}], [{"cls": 15, "base": 1, "count": 1}], [],
                      [{"cls": 12, "base": 0, "count": 0}], [{"cls": 12, "base": 0, "count": 1.5}],
                      [{"cls": True, "base": 0, "count": 1}], None):
            self.assertIn("err", self.take("camp-take-0003-abcdef", items), items)
        self.assertIn("err", self.take("short", [{"cls": 12, "base": 0, "count": 1}]))
        self.assertIn("err", self.camp("drop"))
        self.assertEqual(self.counts(), before)
        # A refused request id is not used up.
        self.assertEqual(self.take("camp-take-0002-abcdef", [{"cls": 12, "base": 0, "count": 3}])["state"], "done")

    def test_a_cancelled_request_never_takes_and_a_cancel_reports_an_earlier_take(self):
        self.fill()
        request = "camp-take-0004-abcdef"
        self.assertEqual(self.camp("status", requestId=request)["state"], "unknown")
        cancelled = self.camp("cancel", requestId=request)
        self.assertEqual((cancelled["state"], cancelled["replayed"]), ("cancelled", False))
        late = self.take(request, [{"cls": 12, "base": 0, "count": 1}])
        self.assertEqual((late["state"], late["taken"]), ("cancelled", []))
        self.assertEqual(self.camp("cancel", requestId=request)["state"], "cancelled")
        self.assertEqual(self.counts()[(12, 0)], 1004)
        done = self.take("camp-take-0005-abcdef", [{"cls": 14, "base": 5, "count": 40}])
        self.assertNotIn((14, 5), self.counts())
        report = self.camp("cancel", requestId="camp-take-0005-abcdef")
        self.assertEqual((report["state"], report["replayed"], report["taken"]), ("done", True, done["taken"]))

    def test_a_take_is_an_undo_barrier_and_a_stale_review_changes_nothing(self):
        self.fill()
        elsewhere = self.store.create_collection("Elsewhere")
        rune = next(r for r in self.store.list_all_available_items(collection=self.materials.id)
                    if r.source_item_key.endswith("-15"))
        self.store.move_item(rune.id, elsewhere.id)
        self.assertEqual(self.store.preview_metadata_undo()["eventType"], "item_moved")
        crystal = next(r for r in self.store.list_all_available_items(collection=self.materials.id)
                       if r.source_item_key.endswith("-12") and r.decoded_item()["data"]["b"] == 1.0)
        token = self.store.preview_item_rework([crystal.id])
        self.store.set_item_custom_name(crystal.id, "Mine")
        with self.assertRaises(VaultConflictError):
            self.store.take_items("camp-take-0006-abcdef", remove=[crystal.id], preview_token=token)
        self.assertIsNone(self.store.take_request("camp-take-0006-abcdef"))
        self.assertNotIn((12, 1), self.counts())  # a custom-named stack is not the camp's to take
        self.assertEqual(self.take("camp-take-0007-abcdef", [{"cls": 12, "base": 0, "count": 1}])["state"], "done")
        self.assertIsNone(self.store.preview_metadata_undo())

    def test_the_route_needs_the_editor_header(self):
        self.fill()
        server = ThreadingHTTPServer(("127.0.0.1", 0), editor.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/vault/afk-take"
            body = json.dumps({"action": "stock"}).encode("utf-8")
            with self.assertRaises(HTTPError) as refused:
                urlopen(Request(url, data=body, method="POST", headers={"Content-Type": "application/json"}), timeout=5)
            self.assertEqual(refused.exception.code, 403)
            request = Request(url, data=body, method="POST",
                              headers={"Content-Type": "application/json", editor.EDITOR_REQUEST_HEADER: "1"})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(json.load(response)["category"], "AFK Materials")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
