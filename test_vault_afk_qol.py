"""AFK Vault quality-of-life: batch deposits, rarity clean-up, lite grid rows.

Only temporary databases are touched; Hero Siege is reported as closed unless a
test says otherwise.
"""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
