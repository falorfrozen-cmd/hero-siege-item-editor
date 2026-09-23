"""Contract for ``POST /api/vault/ingest``.

HS AFK Expedition spool records land in Infinite Vault exactly once, in the
game's compact stash shape: gear in the expedition's own category on stashes
named after its rarity group, native stackables in ``AFK Materials``.  An
expedition that started in the older shared ``AFK Farm`` category continues
there.  Only temporary files are touched.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MODULE_DIR = Path(__file__).resolve().parent
MODULE_PATH = MODULE_DIR / "hs_item_editor_gui.py"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location(
    "hs_item_editor_gui_vault_ingest_tests", MODULE_PATH
)
editor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(editor)


EXPEDITION = "exp_test"
PAGE_NAME = "exp_test · 2026-09-17"  # legacy AFK Farm page of EXPEDITION
CATEGORY = "AFK · 2026-09-17 · exp_test"


def spool_record(seq, cls, name, definition, *, kind="item", stamp=None):
    """One record shaped like ``%LOCALAPPDATA%\\Hero_Siege\\afk\\spool\\<id>.ndjson``."""

    return {
        "expedition_id": EXPEDITION,
        "seq": seq,
        "kind": kind,
        "t": "2026-09-17T09:31:31Z",
        "packet": "0" * 64,
        "type": cls,
        "name": name,
        "item": {
            "itemInfoStruct": {"28": name, "14": "item_type_x"},
            "itemDataHash": "329325263d07e2dfb1ae48c97584a8acf20e5c40",
            "itemDefinitionStruct": definition,
            "itemStatStruct": {"39": 4.0, "10": [39, 1.0, 5.0, 0]},
            "itemTimeStamp": float(211825890000 + seq) if stamp is None else stamp,
            "itemRegion": 0.0,
            "itemType": float(cls),
            "itemAccount": 0.0,
            "itemOnlinePending": False,
            "fp_recorded": True,
        },
    }


GEAR = spool_record(
    1, 3, "Brute's Hand Cannon",
    {"n": 2.0, "b": 0.0, "a": 273552506.0, "j": 14.0, "c": 0.0},
)
MATERIAL = spool_record(
    2, 14, "Crystal", {"n": 2.0, "b": 29, "a": 412516488.0, "j": 0, "c": 0.0},
)
SUMMARY = {
    "expedition_id": EXPEDITION, "seq": 3, "kind": "summary",
    "t": "2026-09-17T09:31:31Z", "gold": 0, "calls": 20, "items": 2,
}


def belt(seq, name, base, *, unique=True):
    """A 2x1 belt; ``base`` picks its catalog rarity (see ``RARITY_BELTS``)."""

    return spool_record(
        seq, 8, name,
        {"n": 2.0, "b": float(base), "a": float(1000 + seq), "j": 0, "c": 1.0 if unique else 0.0},
    )


# Catalog belts of known rarity: Heroic, Satanic, Angelic unique belts and a
# normal Sash.
RARITY_BELTS = {
    "Heroic": (12, True), "Satanic": (0, True), "Angelic": (44, True), "Normal": (0, False),
}


class VaultIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.old_vault_path = editor.VAULT_DB_FILE
        editor.VAULT_DB_FILE = self.directory / "hs_infinite_vault.sqlite3"
        editor._VAULT_STORE = None

    def tearDown(self):
        editor.VAULT_DB_FILE = self.old_vault_path
        editor._VAULT_STORE = None
        self.temporary.cleanup()

    def _ingest(self, records, **extra) -> dict:
        body = {"expedition_id": EXPEDITION, "records": records}
        body.update(extra)
        result = editor.op_vault_ingest(body)
        self.assertNotIn("err", result, result.get("err"))
        return result

    def _rows_by_label(self) -> dict:
        return {row.label: row for row in editor.vault_store().list_items(status="all")}

    def test_deleted_categories_stay_deleted_after_restart_and_reingest(self):
        first = self._ingest([GEAR, MATERIAL])
        store = editor.vault_store()
        for kind in ("farm", "materials"):
            category = first["collections"][kind]["id"]
            preview = store.preview_storage_deletion(category)
            store.delete_storage(category, preview_token=preview["previewToken"])
        editor._VAULT_STORE = None
        again = self._ingest([GEAR, MATERIAL])
        self.assertEqual((again["deposited"], again["duplicate"]), (0, 2))
        self.assertEqual(editor.vault_store().count_items(), 0)
        self.assertEqual([c.name for c in editor.vault_store().list_collections()], ["Vault"])
        self.assertEqual(editor.vault_ingest_status({"expedition_id": [EXPEDITION]})["deposited"], 2)
        # A genuinely new expedition is still allowed to create fresh storage.
        new = editor.op_vault_ingest({"expedition_id": "brand_new_run", "records": [GEAR]})
        self.assertEqual(new["deposited"], 1)

    def test_deleted_stash_is_not_recreated_by_retry_or_new_expedition_layout(self):
        # 60 normal body armors fill two Normal stashes of one expedition.
        armors = [
            spool_record(seq, 1, f"Armor {seq}", {"n": 2.0, "b": 6.0, "a": float(seq), "j": 0, "c": 0.0})
            for seq in range(1, 61)
        ]
        crystal = spool_record(200, 14, "Crystal", {"n": 2.0, "b": 29, "a": 412516488.0, "j": 0, "c": 0.0})
        first = self._ingest(armors + [crystal])
        other = editor.op_vault_ingest({"expedition_id": "exp_two", "records": [GEAR]})
        farm_id = first["collections"]["farm"]["id"]
        store = editor.vault_store()
        self.assertEqual([p.page_index for p in store.list_stash_pages(farm_id)], [0, 1])
        survivors = [row for row in store.list_all_available_items(collection=farm_id) if row.page_index == 0]
        preview = store.preview_storage_deletion(farm_id, page_index=1)
        store.delete_storage(farm_id, page_index=1, preview_token=preview["previewToken"])
        retry = self._ingest(armors + [crystal])
        self.assertEqual((retry["deposited"], retry["duplicate"]), (0, 61))
        ensure = editor.op_vault_layout({"action": "ensure", "collectionId": farm_id})
        self.assertEqual([p["pageIndex"] for p in ensure["stashes"]], [0])
        for row in survivors:
            self.assertEqual(store.get_item(row.id), row)
        # Another expedition keeps its own, untouched category.
        other_id = other["collections"]["farm"]["id"]
        self.assertNotEqual(other_id, farm_id)
        self.assertEqual(store.count_items(collection=other_id), 1)
        third = editor.op_vault_ingest({"expedition_id": "exp_three", "records": [GEAR]})
        self.assertNotIn(third["collections"]["farm"]["id"], {farm_id, other_id})
        self.assertEqual(store.count_items(collection=first["collections"]["materials"]["id"]), 1)

    def test_deleted_deposit_key_cannot_be_reused_by_direct_deposit(self):
        first = self._ingest([GEAR])
        store = editor.vault_store()
        item = store.list_items()[0]
        category = first["collections"]["farm"]["id"]
        preview = store.preview_storage_deletion(category)
        store.delete_storage(category, preview_token=preview["previewToken"])
        with self.assertRaisesRegex(editor.VaultStateError, "deleted"):
            store.deposit("Vault", item.raw_item_json, deposit_key=item.deposit_key)
        self.assertEqual(store.count_items(), 0)

    def test_failed_deletion_does_not_leave_a_consumed_key(self):
        first = self._ingest([GEAR])
        store = editor.vault_store()
        category = first["collections"]["farm"]["id"]
        preview = store.preview_storage_deletion(category)
        with patch.object(store, "_event", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                store.delete_storage(category, preview_token=preview["previewToken"])
        self.assertEqual(store.count_items(), 1)
        with closing(sqlite3.connect(store.path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM deleted_deposit_keys").fetchone()[0], 0)

    def test_v6_upgrade_preserves_items_and_keeps_a_pre_migration_backup(self):
        self._ingest([GEAR, MATERIAL])
        store = editor.vault_store()
        before = store.list_items()
        with closing(sqlite3.connect(store.path)) as connection:
            connection.execute("DROP TABLE deleted_deposit_keys")
            connection.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
            connection.execute("PRAGMA user_version=6")
            connection.commit()
        editor._VAULT_STORE = None
        upgraded = editor.vault_store()
        self.assertEqual(upgraded.schema_version, 7)
        self.assertEqual(upgraded.list_items(), before)
        with closing(sqlite3.connect(upgraded.backup_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)

    def test_two_records_deposit_once_and_repeat_as_duplicates(self):
        first = self._ingest([GEAR, MATERIAL, SUMMARY])
        self.assertEqual(
            (first["deposited"], first["duplicate"], first["ignored"], first["skipped"]),
            (2, 0, 1, []),
        )
        second = self._ingest([GEAR, MATERIAL, SUMMARY])
        self.assertEqual(
            (second["deposited"], second["duplicate"], second["ignored"], second["skipped"]),
            (0, 2, 1, []),
        )
        self.assertEqual(second["collections"], first["collections"])

        rows = self._rows_by_label()
        self.assertEqual(sorted(rows), ["Brute's Hand Cannon", "Crystal"])
        gear, material = rows["Brute's Hand Cannon"], rows["Crystal"]

        # Gear: the game's compact definition plus ``w`` and a one-stack ``o``
        # (``m`` for uniques), written as floats like the game does.
        self.assertEqual(
            gear.raw_item_json,
            '{"pos":[0.0,0.0],"data":{"w":1.0,"a":273552506.0,"j":14.0,"b":0.0,'
            '"c":0.0,"n":2.0,"o":1.0}}',
        )
        # Material: no ``w``, an ``o`` stack of one, ``b`` promoted to a float.
        self.assertEqual(
            material.raw_item_json,
            '{"pos":[0.0,0.0],"data":{"a":412516488.0,"j":0.0,"b":29.0,"c":0.0,'
            '"n":2.0,"o":1.0}}',
        )
        for row in (gear, material):
            data = json.loads(row.raw_item_json)["data"]
            self.assertLessEqual({"a", "b", "c", "j", "n"}, set(data))
            self.assertEqual(row.source, "afk-expedition")
            self.assertEqual(row.status, "available")
        self.assertEqual(gear.collection_name, CATEGORY)
        self.assertEqual(material.collection_name, "AFK Materials")
        self.assertEqual(gear.source_item_key, "0-0-211825890001-3")
        self.assertEqual(material.source_item_key, "0-0-211825890002-14")
        self.assertRegex(gear.deposit_key, r"^afk-exp_test-[0-9a-f]{16}-0000000001$")
        self.assertRegex(material.deposit_key, r"^afk-exp_test-[0-9a-f]{16}-0000000002$")

        # The expedition's own category; the gear's stash is named after its
        # rarity group; layout assigned.
        store = editor.vault_store()
        group = editor._vault_item_derived(gear)["group"]
        self.assertEqual(
            [page.name for page in store.list_stash_pages(gear.collection_id)],
            [group],
        )
        self.assertEqual((gear.page_index, gear.layout_x, gear.layout_y), (0, 0, 0))
        self.assertEqual((material.page_index, material.layout_x, material.layout_y), (0, 0, 0))
        self.assertEqual(
            first["collections"],
            {
                "farm": {
                    "id": gear.collection_id, "name": CATEGORY,
                    "pageIndex": 0, "pageName": group,
                },
                "materials": {"id": material.collection_id, "name": "AFK Materials"},
            },
        )
        # The editor's own catalog resolution and display payload work on them.
        payload = editor._vault_item_payload(gear)
        self.assertEqual((payload["name"], payload["clsName"], payload["pos"]), ("Hand Cannon", "Weapon", [0, 0]))
        self.assertEqual((payload["w"], payload["h"]), (2, 2))
        material_payload = editor._vault_item_payload(material)
        self.assertEqual((material_payload["clsName"], material_payload["stack"]), ("Material", 1))

        status = editor.vault_ingest_status({"expedition_id": [EXPEDITION]})
        self.assertEqual(status, {"expedition_id": EXPEDITION, "deposited": 2})
        self.assertEqual(
            editor.vault_ingest_status({"expedition_id": ["exp_test_other"]})["deposited"], 0
        )

    def test_unique_gear_gets_m_and_each_expedition_gets_its_own_category(self):
        first = self._ingest([GEAR])
        unique = spool_record(
            7, 0, "Crown", {"n": 2.0, "b": 41.0, "a": 107917.0, "j": 0, "c": 1.0}
        )
        result = editor.op_vault_ingest({
            "expedition_id": "exp_two", "label": "Act 3 · 2h", "records": [unique],
        })
        self.assertNotIn("err", result, result.get("err"))
        self.assertEqual(result["deposited"], 1)
        farm = result["collections"]["farm"]
        rows = self._rows_by_label()
        crown = rows["Crown"]
        group = editor._vault_item_derived(crown)["group"]
        self.assertEqual(
            (farm["name"], farm["pageIndex"], farm["pageName"]),
            ("AFK · 2026-09-17 · Act 3 · 2h", 0, group),
        )
        self.assertNotEqual(farm["id"], first["collections"]["farm"]["id"])
        self.assertEqual(
            json.loads(crown.raw_item_json)["data"],
            {"w": 1.0, "a": 107917.0, "j": 0.0, "b": 41.0, "c": 1.0, "n": 2.0, "m": 1.0},
        )
        self.assertEqual((crown.page_index, crown.layout_x, crown.layout_y), (0, 0, 0))
        cannon = rows["Brute's Hand Cannon"]
        self.assertEqual((cannon.page_index, cannon.layout_x, cannon.layout_y), (0, 0, 0))
        # The UI's own layout pass keeps those placements untouched.
        ensure = editor.op_vault_layout({"action": "ensure", "collectionId": farm["id"]})
        self.assertNotIn("err", ensure, ensure.get("err"))
        self.assertEqual(ensure["changed"], 0)
        # A later batch of the first expedition lands back in its own category,
        # even after the player renamed it.
        store = editor.vault_store()
        store.rename_collection(first["collections"]["farm"]["id"], "Monday run")
        more = self._ingest([spool_record(9, 8, "Belt", {"n": 2.0, "b": 2.0, "a": 5.0, "j": 0, "c": 0.0})])
        self.assertEqual(more["deposited"], 1)
        self.assertEqual(more["collections"]["farm"]["id"], first["collections"]["farm"]["id"])
        self.assertEqual(more["collections"]["farm"]["name"], "Monday run")
        belt_row = self._rows_by_label()["Belt"]
        self.assertEqual(belt_row.collection_id, first["collections"]["farm"]["id"])

    def test_gear_is_grouped_on_stashes_named_after_its_rarity(self):
        records = []
        seq = 0
        for name in ("Normal", "Satanic", "Heroic", "Angelic"):
            base, unique = RARITY_BELTS[name]
            for _ in range(3):
                seq += 1
                records.append(belt(seq, f"{name} belt {seq}", base, unique=unique))
        result = self._ingest(records)
        self.assertEqual(result["deposited"], 12)
        store = editor.vault_store()
        farm_id = result["collections"]["farm"]["id"]
        pages = store.list_stash_pages(farm_id)
        # VAULT_RARITY_GROUPS order: the best items get the first stashes.
        self.assertEqual([page.name for page in pages], ["Angelic", "Heroic", "Satanic", "Normal"])
        by_page = {page.page_index: page.name for page in pages}
        for row in store.list_all_available_items(collection=farm_id):
            self.assertEqual(by_page[row.page_index], row.label.split(" belt ")[0])
        self.assertEqual(result["collections"]["farm"]["pageName"], "Angelic")
        # A later batch adds to its group's stash instead of opening a new one.
        more = self._ingest([belt(20, "Heroic belt 20", 12)])
        self.assertEqual(more["deposited"], 1)
        self.assertEqual(len(store.list_stash_pages(farm_id)), 4)
        self.assertEqual(by_page[self._rows_by_label()["Heroic belt 20"].page_index], "Heroic")

    def test_legacy_expedition_in_afk_farm_continues_on_its_page(self):
        store = editor.vault_store()
        farm = store.create_collection("AFK Farm")
        store.rename_stash_page(farm.id, 0, PAGE_NAME)
        result = self._ingest([GEAR, MATERIAL])
        self.assertEqual(result["deposited"], 2)
        self.assertEqual(
            result["collections"]["farm"],
            {"id": farm.id, "name": "AFK Farm", "pageIndex": 0, "pageName": PAGE_NAME},
        )
        cannon = self._rows_by_label()["Brute's Hand Cannon"]
        self.assertEqual((cannon.collection_id, cannon.page_index), (farm.id, 0))
        self.assertIsNone(store.find_marked_collection(editor.AFK_INGEST_MARKER, EXPEDITION))

    def test_each_batch_is_one_backed_up_transaction_per_category(self):
        records = [belt(seq, f"Belt {seq}", 12) for seq in range(1, 41)]
        records += [
            spool_record(100 + seq, 14, f"Crystal {seq}", {"n": 2.0, "b": 29, "a": float(seq), "j": 0, "c": 0.0})
            for seq in range(1, 11)
        ]
        store = editor.vault_store()
        with patch.object(
            type(store), "_backup_existing", autospec=True,
            side_effect=editor.InfiniteVault._backup_existing,
        ) as backups:
            result = self._ingest(records)
        self.assertEqual(result["deposited"], 50)
        # category + deposit + layout for gear, collection + deposit + layout
        # for materials: a handful of writes, not one backup per item.
        self.assertLessEqual(backups.call_count, 8)

    def test_gear_overflowing_a_rarity_stash_continues_on_a_numbered_one(self):
        # A 2x3 body armor fills a 17x18 stash after 51 pieces.
        armors = [
            spool_record(seq, 1, f"Armor {seq}", {"n": 2.0, "b": 6.0, "a": float(seq), "j": 0, "c": 0.0})
            for seq in range(1, 61)
        ]
        result = self._ingest(armors)
        self.assertEqual((result["deposited"], result["duplicate"]), (60, 0))
        store = editor.vault_store()
        farm_id = result["collections"]["farm"]["id"]
        self.assertEqual(
            [page.name for page in store.list_stash_pages(farm_id)],
            ["Normal", "Normal (2)"],
        )
        rows = store.list_all_available_items(collection=farm_id)
        self.assertEqual(sorted({row.page_index for row in rows}), [0, 1])
        self.assertEqual(len(rows), 60)
        # Re-posting is still a no-op and creates no further page.
        again = self._ingest(armors)
        self.assertEqual((again["deposited"], again["duplicate"]), (0, 60))
        self.assertEqual(len(store.list_stash_pages(farm_id)), 2)
        # The next expedition gets its own category.
        other = editor.op_vault_ingest({"expedition_id": "exp_two", "records": [
            spool_record(1, 3, "Cannon", {"n": 2.0, "b": 0.0, "a": 1.0, "j": 14.0, "c": 0.0}),
        ]})
        self.assertNotEqual(other["collections"]["farm"]["id"], farm_id)
        self.assertEqual(other["collections"]["farm"]["name"], "AFK · 2026-09-17 · exp_two")
        self.assertEqual(len(store.list_stash_pages(farm_id)), 2)

    def test_malformed_records_are_skipped_with_reasons_not_aborted(self):
        result = self._ingest([
            spool_record(4, 99, "Mystery", {"b": 1.0, "a": 2.0}),
            {"kind": "item", "seq": 5, "name": "no item"},
            spool_record("x", 3, "Bad seq", {"b": 1.0, "a": 2.0}, stamp=1.0),
            GEAR,
            "not a record",
            spool_record(8, 3, "No base", {"a": 2.0}),
            spool_record(10, 3, "Bad seed", {"b": 1.0, "a": "seed"}),
            spool_record(11, 3, "No definition", None),
            {"kind": "gold", "seq": 12, "amount": 5},
        ])
        self.assertEqual((result["deposited"], result["duplicate"], result["ignored"]), (1, 0, 1))
        self.assertEqual(
            [(entry["seq"], entry["reason"]) for entry in result["skipped"]],
            [
                (4, "unknown item class 99.0"),
                (5, "item must be an object"),
                ("x", "seq must be a non-negative integer"),
                (None, "record is not an object"),
                (8, "itemDefinitionStruct.b is missing"),
                (10, "itemDefinitionStruct.a must be a number"),
                (11, "item.itemDefinitionStruct must be an object"),
            ],
        )
        self.assertEqual(editor.vault_store().count_items(), 1)

    def test_request_validation_and_empty_batches(self):
        self.assertEqual(
            editor.op_vault_ingest({"records": []}), {"err": "expedition_id must be text"}
        )
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "  ", "records": []}))
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "a\x00b", "records": []}))
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "x", "records": "nope"}))
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "x", "records": [], "label": 3}))
        self.assertIn("err", editor.op_vault_ingest({"expedition_id": "x", "records": [{}] * 501}))
        empty = self._ingest([SUMMARY])
        self.assertEqual(
            empty,
            {
                "expedition_id": EXPEDITION, "deposited": 0, "duplicate": 0,
                "ignored": 1, "skipped": [], "collections": {},
            },
        )
        self.assertEqual(editor.vault_store().list_collections()[0].name, "Vault")
        self.assertIn("err", editor.vault_ingest_status({}))

    def test_ingested_gear_withdraws_to_the_shared_stash_and_stays_counted(self):
        saves = self.directory / "saves"
        saves.mkdir()
        stash_path = saves / "stash.hss"
        stash = {"stash_tab_1": {}, "material_tab": {}, "socket_tab": {}, "unique_items": {}}
        stash_path.write_text(
            editor.encode_hss(json.dumps(stash, separators=(", ", ": "))), encoding="ascii"
        )
        old_saves = editor.SAVES
        editor.SAVES = saves
        try:
            with patch.object(editor, "game_running", return_value=False):
                self._ingest([GEAR, MATERIAL])
                gear = self._rows_by_label()["Brute's Hand Cannon"]
                result = editor.op_vault_withdraw({
                    "itemId": gear.id,
                    "target": {"type": "stash", "tab": "stash_tab_1"},
                    "requestId": "33333333-3333-4333-8333-333333333333",
                })
                self.assertNotIn("err", result, result.get("err"))
                written = json.loads(editor.decode_hss(stash_path))["stash_tab_1"]
                self.assertEqual(list(written), ["0-0-211825890001-3"])
                entry = written["0-0-211825890001-3"]
                self.assertEqual(entry["data"], json.loads(gear.raw_item_json)["data"])
                self.assertEqual(entry["pos"], [0.0, 0.0])
                again = self._ingest([GEAR, MATERIAL])
                self.assertEqual((again["deposited"], again["duplicate"]), (0, 2))
                self.assertEqual(
                    editor.vault_ingest_status({"expedition_id": [EXPEDITION]})["deposited"], 2
                )
                self.assertEqual(editor.vault_store().count_items(), 1)
        finally:
            editor.SAVES = old_saves

    @contextmanager
    def _http_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), editor.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_http_routes_require_the_editor_header_and_report_status(self):
        body = json.dumps({"expedition_id": EXPEDITION, "records": [GEAR, MATERIAL]}).encode("utf-8")
        with self._http_server() as base:
            anonymous = Request(
                base + "/api/vault/ingest", data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as refused:
                urlopen(anonymous, timeout=5)
            self.assertEqual(refused.exception.code, 403)
            self.assertEqual(editor.vault_store().count_items(), 0)

            request = Request(
                base + "/api/vault/ingest", data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    editor.EDITOR_REQUEST_HEADER: "1",
                },
            )
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
            self.assertEqual((result["deposited"], result["duplicate"]), (2, 0))
            query = urlencode({"expedition_id": EXPEDITION})
            with urlopen(base + "/api/vault/ingest/status?" + query, timeout=5) as response:
                self.assertEqual(
                    json.load(response), {"expedition_id": EXPEDITION, "deposited": 2}
                )


if __name__ == "__main__":
    unittest.main()
