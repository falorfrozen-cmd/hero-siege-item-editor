import hashlib
import importlib.util
import json
import multiprocessing
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("infinite_vault.py")
SPEC = importlib.util.spec_from_file_location("infinite_vault", MODULE_PATH)
vault_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vault_module
SPEC.loader.exec_module(vault_module)


RAW_SWORD = '{ "pos": [1.0, 2.0], "data": {"b": 52.0, "a": 123.0, "name": "Kılıç"} }\n'
RAW_HELM = '{"data":{"b":91.0,"a":456.0,"name":"Storm Helm","note":"100%"}}'


def _process_deposit_worker(module_directory, database_path, index, queue):
    try:
        sys.path.insert(0, module_directory)
        import infinite_vault as child_vault

        handle = child_vault.InfiniteVault(database_path)
        raw = json.dumps({"data": {"b": index + 1, "name": f"Process {index}"}})
        handle.deposit(
            "Vault", raw, deposit_key=f"process_{index:04d}_0123456789"
        )
        queue.put(None)
    except Exception as exc:  # pragma: no cover - parent asserts serialized text.
        queue.put(repr(exc))


class InfiniteVaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "vault.sqlite3"
        self.vault = vault_module.InfiniteVault(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_database_has_versioned_schema_and_default_collection(self):
        self.assertEqual(self.vault.schema_version, vault_module.SCHEMA_VERSION)
        collections = self.vault.list_collections()
        self.assertEqual([record.name for record in collections], ["Vault"])
        self.assertEqual(collections[0].item_count, 0)
        self.assertEqual(collections[0].stash_count, 1)
        pages = self.vault.list_stash_pages("Vault")
        self.assertEqual([(page.page_index, page.name) for page in pages], [(0, "Stash 1")])
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                vault_module.SCHEMA_VERSION,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                str(vault_module.SCHEMA_VERSION),
            )
        finally:
            connection.close()

    def test_category_creation_adds_exactly_one_named_stash(self):
        category = self.vault.create_collection("Sets")
        self.assertEqual(category.stash_count, 1)
        self.assertEqual(
            [(page.page_index, page.name) for page in self.vault.list_stash_pages(category.id)],
            [(0, "Stash 1")],
        )
        added = self.vault.add_stash_page(category.id)
        self.assertEqual((added.page_index, added.name), (1, "Stash 2"))
        self.assertEqual(len(self.vault.list_stash_pages(category.id)), 2)

    def test_stash_names_are_quickly_renamed_and_unique_per_category(self):
        second = self.vault.add_stash_page("Vault")
        renamed = self.vault.rename_stash_page("Vault", 0, "  Boss Sets  ")
        self.assertEqual(renamed.name, "Boss Sets")
        reopened = vault_module.InfiniteVault(self.path)
        self.assertEqual(reopened.list_stash_pages("Vault")[0].name, "Boss Sets")
        with self.assertRaises(vault_module.VaultConflictError):
            reopened.rename_stash_page("Vault", second.page_index, "boss sets")

    def test_stash_payload_updates_are_atomic_and_reject_stale_preview(self):
        first = self.vault.deposit("Vault", RAW_SWORD)
        second = self.vault.deposit("Vault", RAW_HELM)
        self.vault.set_item_layouts("Vault", [
            {"itemId": first.id, "pageIndex": 0, "x": 0, "y": 0},
            {"itemId": second.id, "pageIndex": 0, "x": 2, "y": 0},
        ])
        updated_first = json.dumps(
            {"pos": [1.0, 2.0], "data": {"b": 52.0, "a": 999.0, "name": "Kılıç"}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        updated_second = json.dumps(
            {"data": {"b": 91.0, "a": 999.0, "name": "Storm Helm", "note": "100%"}},
            separators=(",", ":"),
        )
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.update_stash_item_payloads("Vault", 0, [
                {
                    "itemId": first.id,
                    "expectedSha256": first.raw_sha256,
                    "rawItemJson": updated_first,
                },
                {
                    "itemId": second.id,
                    "expectedSha256": "0" * 64,
                    "rawItemJson": updated_second,
                },
            ])
        self.assertEqual(self.vault.get_item(first.id).raw_item_json, RAW_SWORD)
        changed = self.vault.update_stash_item_payloads("Vault", 0, [{
            "itemId": first.id,
            "expectedSha256": first.raw_sha256,
            "rawItemJson": updated_first,
        }])
        self.assertEqual(changed[0].decoded_item()["data"]["a"], 999.0)

    def test_deposit_preserves_raw_json_exactly(self):
        record = self.vault.deposit(
            "Vault",
            RAW_SWORD,
            source_item_key="0-0-123-3",
            label="Night's Edge",
            source="Shared Stash 1",
        )
        reopened = vault_module.InfiniteVault(self.path).get_item(record.id)
        self.assertEqual(reopened.raw_item_json, RAW_SWORD)
        self.assertEqual(reopened.decoded_item()["data"]["a"], 123.0)
        self.assertEqual(len(reopened.raw_sha256), 64)

    def test_custom_name_crud_search_audit_and_raw_preservation(self):
        item = self.vault.deposit(
            "Vault", RAW_SWORD, label="Night's Edge", source="Shared Stash 1"
        )
        original_json = item.raw_item_json
        original_sha256 = item.raw_sha256

        named = self.vault.set_item_custom_name(item.id, "  Bo\u0301ss Melter  ")
        self.assertEqual(named.custom_name, "B\u00f3ss Melter")
        self.assertEqual(named.as_dict()["customName"], "B\u00f3ss Melter")
        self.assertEqual(named.raw_item_json, original_json)
        self.assertEqual(named.raw_sha256, original_sha256)
        self.assertEqual(self.vault.search_items("B\u00d3SS MELTER")[0].id, item.id)

        # Updating replaces the indexed alias; repeating the same value is
        # idempotent and does not create another audit event.
        renamed = self.vault.set_item_custom_name(item.id, "Arena Loadout")
        same = self.vault.set_item_custom_name(item.id, "Arena Loadout")
        self.assertEqual(same, renamed)
        self.assertEqual(self.vault.search_items("B\u00f3ss Melter"), [])
        self.assertEqual(self.vault.search_items("arena loadout")[0].id, item.id)

        reopened = vault_module.InfiniteVault(self.path)
        persisted = reopened.get_item(item.id)
        self.assertEqual(persisted.custom_name, "Arena Loadout")
        self.assertEqual(persisted.raw_item_json, original_json)
        self.assertEqual(persisted.raw_sha256, original_sha256)

        cleared = reopened.clear_item_custom_name(item.id)
        self.assertIsNone(cleared.custom_name)
        self.assertEqual(reopened.search_items("arena loadout"), [])
        self.assertEqual(reopened.search_items("Night's Edge")[0].id, item.id)
        self.assertEqual(cleared.raw_item_json, original_json)
        self.assertEqual(cleared.raw_sha256, original_sha256)

        connection = sqlite3.connect(self.path)
        try:
            events = connection.execute(
                """SELECT details_json FROM events
                   WHERE event_type='item_custom_name_updated'
                   ORDER BY id"""
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(events), 3)
        self.assertEqual(
            json.loads(events[0][0]),
            {"customName": "B\u00f3ss Melter", "previousCustomName": None},
        )
        self.assertEqual(
            json.loads(events[-1][0]),
            {"customName": None, "previousCustomName": "Arena Loadout"},
        )

    def test_custom_name_validation_fails_without_mutating_item(self):
        item = self.vault.deposit("Vault", RAW_HELM)
        invalid_values = [
            42,
            "x" * (vault_module.MAX_CUSTOM_NAME_LENGTH + 1),
            "hidden\tcontrol",
            "broken\ud800unicode",
        ]
        for value in invalid_values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(vault_module.VaultValidationError):
                    self.vault.set_item_custom_name(item.id, value)
        unchanged = self.vault.get_item(item.id)
        self.assertIsNone(unchanged.custom_name)
        self.assertEqual(unchanged.raw_item_json, RAW_HELM)
        self.assertEqual(unchanged.raw_sha256, item.raw_sha256)

    def test_custom_name_rejects_every_non_available_item(self):
        reserved = self.vault.deposit("Vault", RAW_SWORD)
        self.vault.set_item_custom_name(reserved.id, "Keep Me")
        withdrawal = self.vault.reserve_withdrawal(reserved.id)
        for attempted_name in ("Keep Me", "Changed", None):
            with self.subTest(status="reserved", name=attempted_name):
                with self.assertRaises(vault_module.VaultStateError):
                    self.vault.set_item_custom_name(reserved.id, attempted_name)
        unchanged = self.vault.get_item(reserved.id)
        self.assertEqual(unchanged.status, "reserved")
        self.assertEqual(unchanged.custom_name, "Keep Me")

        pending = self.vault.prepare_deposit(
            "Vault",
            RAW_HELM,
            request_id="pending_alias_0123456789",
            request_hash=vault_module.canonical_request_hash({
                "direction": "deposit",
                "source": {"type": "stash", "tab": "stash_tab_1"},
                "key": "0-0-999-1",
                "collectionId": 1,
            }),
            source_tab="stash_tab_1",
            source_key="0-0-999-1",
            stash_before_sha256="a" * 64,
            stash_after_sha256="b" * 64,
        )
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.set_item_custom_name(pending.item_id, "Too Early")
        self.assertEqual(
            self.vault.get_item(pending.item_id).status, "deposit_pending"
        )
        self.vault.cancel_withdrawal(withdrawal.token)

    def test_public_item_integrity_validation_rejects_tampered_hash_and_raw(self):
        item = self.vault.deposit("Vault", RAW_SWORD)
        self.assertEqual(
            vault_module.validate_item_record_integrity(item)["data"]["a"], 123.0
        )

        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE items SET raw_sha256=? WHERE id=?", ("0" * 64, item.id)
            )
            connection.commit()
        finally:
            connection.close()
        tampered_hash = self.vault.get_item(item.id)
        with self.assertRaises(vault_module.VaultSchemaError):
            vault_module.validate_item_record_integrity(tampered_hash)
        with self.assertRaises(vault_module.VaultSchemaError):
            tampered_hash.decoded_item()

        malformed_raw = '{"data":[]}'
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE items SET raw_json=?, raw_sha256=? WHERE id=?",
                (
                    malformed_raw,
                    hashlib.sha256(malformed_raw.encode("utf-8")).hexdigest(),
                    item.id,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(vault_module.VaultSchemaError):
            vault_module.validate_item_record_integrity(self.vault.get_item(item.id))

    def test_v2_database_migrates_with_backup_and_preserves_native_payload(self):
        legacy_path = self.directory / "legacy.sqlite3"
        legacy = vault_module.InfiniteVault(legacy_path)
        item = legacy.deposit(
            "Vault",
            RAW_SWORD,
            source_item_key="0-0-123-3",
            label="Legacy Blade",
            source="Shared Stash 2",
        )

        # Recreate the exact v2 shape: v3 only added this nullable column.
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute("ALTER TABLE items DROP COLUMN custom_name")
            connection.execute(
                "UPDATE schema_meta SET value='2' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        finally:
            connection.close()

        migrated = vault_module.InfiniteVault(legacy_path)
        reopened = migrated.get_item(item.id)
        self.assertEqual(migrated.schema_version, vault_module.SCHEMA_VERSION)
        self.assertIsNone(reopened.custom_name)
        self.assertEqual(reopened.raw_item_json, RAW_SWORD)
        self.assertEqual(reopened.raw_sha256, item.raw_sha256)
        self.assertEqual(migrated.search_items("Legacy Blade")[0].id, item.id)

        backup_path = Path(str(legacy_path) + ".bak")
        backup = sqlite3.connect(backup_path)
        try:
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
            columns = {
                row[1] for row in backup.execute("PRAGMA table_info(items)").fetchall()
            }
            backed_up_item = backup.execute(
                "SELECT raw_json, raw_sha256 FROM items WHERE id=?", (item.id,)
            ).fetchone()
        finally:
            backup.close()
        self.assertNotIn("custom_name", columns)
        self.assertEqual(backed_up_item, (RAW_SWORD, item.raw_sha256))

        named = migrated.set_item_custom_name(item.id, "Migrated Favorite")
        self.assertEqual(named.custom_name, "Migrated Favorite")
        self.assertEqual(named.raw_item_json, RAW_SWORD)
        self.assertEqual(named.raw_sha256, item.raw_sha256)

    def test_v4_database_migrates_layout_columns_with_backup(self):
        legacy_path = self.directory / "legacy-v4.sqlite3"
        legacy = vault_module.InfiniteVault(legacy_path)
        item = legacy.deposit("Vault", RAW_HELM, label="Legacy Helm")
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP INDEX items_collection_layout_idx")
            connection.execute("DROP INDEX items_collection_status_idx")
            connection.execute("DROP INDEX items_search_idx")
            connection.execute(
                """CREATE TABLE items_v4 (
                       id TEXT PRIMARY KEY,
                       collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE RESTRICT,
                       raw_json TEXT NOT NULL, raw_sha256 TEXT NOT NULL,
                       search_text TEXT NOT NULL, source_item_key TEXT, label TEXT,
                       custom_name TEXT, source TEXT, deposit_key TEXT UNIQUE,
                       status TEXT NOT NULL CHECK (status IN ('deposit_pending', 'available', 'reserved')),
                       reserved_token TEXT UNIQUE, created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       CHECK (
                           (status = 'deposit_pending' AND reserved_token IS NULL) OR
                           (status = 'available' AND reserved_token IS NULL) OR
                           (status = 'reserved' AND reserved_token IS NOT NULL)
                       )
                   )"""
            )
            connection.execute(
                """INSERT INTO items_v4(
                       id, collection_id, raw_json, raw_sha256, search_text,
                       source_item_key, label, custom_name, source, deposit_key,
                       status, reserved_token, created_at, updated_at
                   ) SELECT id, collection_id, raw_json, raw_sha256, search_text,
                            source_item_key, label, custom_name, source, deposit_key,
                            status, reserved_token, created_at, updated_at FROM items"""
            )
            connection.execute("DROP TABLE items")
            connection.execute("ALTER TABLE items_v4 RENAME TO items")
            connection.execute(
                "CREATE INDEX items_collection_status_idx ON items(collection_id, status, created_at, id)"
            )
            connection.execute("CREATE INDEX items_search_idx ON items(search_text)")
            connection.execute(
                "UPDATE schema_meta SET value='4' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
        finally:
            connection.close()

        migrated = vault_module.InfiniteVault(legacy_path)
        reopened = migrated.get_item(item.id)
        self.assertEqual(migrated.schema_version, 6)
        self.assertIsNone(reopened.page_index)
        self.assertIsNone(reopened.layout_x)
        self.assertIsNone(reopened.layout_y)
        self.assertEqual(reopened.raw_item_json, RAW_HELM)
        self.assertEqual(reopened.raw_sha256, item.raw_sha256)
        self.assertEqual(
            [(page.page_index, page.name) for page in migrated.list_stash_pages("Vault")],
            [(0, "Stash 1")],
        )

        backup = sqlite3.connect(Path(str(legacy_path) + ".bak"))
        try:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 4)
            columns = {
                row[1] for row in backup.execute("PRAGMA table_info(items)").fetchall()
            }
        finally:
            backup.close()
        self.assertNotIn("page_index", columns)
        self.assertNotIn("layout_x", columns)
        self.assertNotIn("layout_y", columns)

    def test_v5_database_migrates_existing_grid_pages_to_named_stashes(self):
        legacy_path = self.directory / "legacy-v5.sqlite3"
        legacy = vault_module.InfiniteVault(legacy_path)
        item = legacy.deposit("Vault", RAW_HELM)
        legacy.ensure_stash_page_count("Vault", 4)
        legacy.set_item_layouts("Vault", [{
            "itemId": item.id, "pageIndex": 3, "x": 4, "y": 5,
        }])
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute("DROP TABLE stash_pages")
            connection.execute(
                "UPDATE schema_meta SET value='5' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 5")
            connection.commit()
        finally:
            connection.close()

        migrated = vault_module.InfiniteVault(legacy_path)
        self.assertEqual(migrated.schema_version, 6)
        self.assertEqual(
            [(page.page_index, page.name) for page in migrated.list_stash_pages("Vault")],
            [(0, "Stash 1"), (1, "Stash 2"), (2, "Stash 3"), (3, "Stash 4")],
        )
        reopened = migrated.get_item(item.id)
        self.assertEqual(
            (reopened.page_index, reopened.layout_x, reopened.layout_y),
            (3, 4, 5),
        )
        backup = sqlite3.connect(Path(str(legacy_path) + ".bak"))
        try:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 5)
            tables = {
                row[0] for row in backup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            backup.close()
        self.assertNotIn("stash_pages", tables)

    def test_special_item_without_base_identity_is_preserved_opaquely(self):
        raw = '{"pos":[0,0],"data":{"a":123,"future":{"kind":"special"}}}'
        record = self.vault.deposit("Vault", raw)
        self.assertEqual(self.vault.get_item(record.id).raw_item_json, raw)

    def test_rejects_malformed_or_non_item_json(self):
        bad_values = [
            None,
            "",
            "not-json",
            "[]",
            "{}",
            '{"data":[]}',
            '{"data":{"b":true}}',
            '{"data":{"b":NaN}}',
            '{"data":{"b":1e999}}',
            '{"data":{"b":1},"data":{"b":2}}',
            '{"pos":[0],"data":{"b":1}}',
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(vault_module.VaultValidationError):
                    self.vault.deposit("Vault", value)
        self.assertEqual(self.vault.count_items(), 0)

    def test_collection_crud_is_unicode_normalized_and_case_insensitive(self):
        builds = self.vault.create_collection("  Élite Builds  ")
        self.assertEqual(builds.name, "Élite Builds")
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.create_collection("e\u0301LITE BUILDS")
        renamed = self.vault.rename_collection(builds.id, "Season Ten")
        self.assertEqual(renamed.name, "Season Ten")
        self.vault.delete_collection("season ten")
        self.assertEqual([entry.name for entry in self.vault.list_collections()], ["Vault"])

    def test_delete_requires_collection_to_be_empty(self):
        target = self.vault.create_collection("Keep")
        item = self.vault.deposit(target.id, RAW_SWORD)
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.delete_collection(target.id)
        withdrawal = self.vault.reserve_withdrawal(item.id)
        self.vault.commit_withdrawal(withdrawal.token)
        self.vault.delete_collection(target.id)

    def test_list_search_count_and_pagination(self):
        second = self.vault.create_collection("Sets")
        sword = self.vault.deposit("Vault", RAW_SWORD, label="Ancient Blade")
        helm = self.vault.deposit(second.id, RAW_HELM, label="Tempest Crown")
        self.assertEqual(self.vault.count_items(), 2)
        self.assertEqual(self.vault.count_items(collection="sets"), 1)
        self.assertEqual(self.vault.search_items("kılıç")[0].id, sword.id)
        self.assertEqual(self.vault.search_items("TEMPEST")[0].id, helm.id)
        self.assertEqual(self.vault.search_items("100%")[0].id, helm.id)
        self.assertEqual(self.vault.search_items("100_") , [])
        page_one = self.vault.list_items(limit=1, offset=0)
        page_two = self.vault.list_items(limit=1, offset=1)
        self.assertEqual(len(page_one), 1)
        self.assertEqual(len(page_two), 1)
        self.assertNotEqual(page_one[0].id, page_two[0].id)

    def test_move_item_between_unlimited_named_collections(self):
        item = self.vault.deposit("Vault", RAW_SWORD)
        destinations = [self.vault.create_collection(f"Page {index}") for index in range(80)]
        self.vault.ensure_stash_page_count("Vault", 4)
        positioned = self.vault.set_item_layouts("Vault", [{
            "itemId": item.id, "pageIndex": 3, "x": 4, "y": 5,
        }])[0]
        self.assertEqual(
            (positioned.page_index, positioned.layout_x, positioned.layout_y),
            (3, 4, 5),
        )
        moved = self.vault.move_item(item.id, destinations[-1].id)
        self.assertEqual(moved.collection_name, "Page 79")
        self.assertIsNone(moved.page_index)
        self.assertIsNone(moved.layout_x)
        self.assertIsNone(moved.layout_y)
        self.assertEqual(self.vault.count_items(collection="Vault"), 0)
        self.assertEqual(self.vault.count_items(collection="Page 79"), 1)

    def test_persistent_layout_preserves_payload_and_audits_changes(self):
        sword = self.vault.deposit("Vault", RAW_SWORD)
        helm = self.vault.deposit("Vault", RAW_HELM)
        self.vault.ensure_stash_page_count("Vault", 2)
        original = {
            row.id: (row.raw_item_json, row.raw_sha256)
            for row in (sword, helm)
        }
        placed = self.vault.set_item_layouts("Vault", [
            {"itemId": sword.id, "pageIndex": 0, "x": 2, "y": 3},
            {"itemId": helm.id, "pageIndex": 1, "x": 0, "y": 0},
        ])
        self.assertEqual(
            [(row.page_index, row.layout_x, row.layout_y) for row in placed],
            [(0, 2, 3), (1, 0, 0)],
        )
        moved = self.vault.set_item_layouts("Vault", [{
            "itemId": sword.id, "pageIndex": 0, "x": 8, "y": 9,
        }])[0]
        self.assertEqual((moved.page_index, moved.layout_x, moved.layout_y), (0, 8, 9))

        reopened = vault_module.InfiniteVault(self.path)
        for item_id, expected in original.items():
            record = reopened.get_item(item_id)
            self.assertEqual((record.raw_item_json, record.raw_sha256), expected)
        self.assertEqual(
            (
                reopened.get_item(sword.id).page_index,
                reopened.get_item(sword.id).layout_x,
                reopened.get_item(sword.id).layout_y,
            ),
            (0, 8, 9),
        )

        connection = sqlite3.connect(self.path)
        try:
            events = connection.execute(
                """SELECT details_json FROM events
                   WHERE event_type='collection_layout_updated' ORDER BY id"""
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(events), 2)
        first = json.loads(events[0][0])
        second = json.loads(events[1][0])
        self.assertEqual(first["collectionId"], sword.collection_id)
        self.assertEqual(first["changes"][0]["previous"], None)
        self.assertEqual(second["changes"][0]["previous"], {
            "pageIndex": 0, "x": 2, "y": 3,
        })
        self.assertEqual(second["changes"][0]["current"], {
            "pageIndex": 0, "x": 8, "y": 9,
        })

    def test_layout_validation_rejects_unsafe_or_stale_targets(self):
        item = self.vault.deposit("Vault", RAW_SWORD)
        other = self.vault.create_collection("Other")
        foreign = self.vault.deposit(other.id, RAW_HELM)
        invalid = [
            [{"itemId": item.id, "pageIndex": -1, "x": 0, "y": 0}],
            [{"itemId": item.id, "pageIndex": 0, "x": 17, "y": 0}],
            [{"itemId": item.id, "pageIndex": 0, "x": 0, "y": 18}],
            [{"itemId": item.id, "pageIndex": True, "x": 0, "y": 0}],
            [
                {"itemId": item.id, "pageIndex": 0, "x": 0, "y": 0},
                {"itemId": item.id, "pageIndex": 0, "x": 1, "y": 0},
            ],
        ]
        for placements in invalid:
            with self.subTest(placements=placements):
                with self.assertRaises(vault_module.VaultValidationError):
                    self.vault.set_item_layouts("Vault", placements)
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.set_item_layouts("Vault", [{
                "itemId": foreign.id, "pageIndex": 0, "x": 0, "y": 0,
            }])
        withdrawal = self.vault.reserve_withdrawal(item.id)
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.set_item_layouts("Vault", [{
                "itemId": item.id, "pageIndex": 0, "x": 0, "y": 0,
            }])
        self.vault.cancel_withdrawal(withdrawal.token)
        unchanged = self.vault.get_item(item.id)
        self.assertIsNone(unchanged.page_index)

    def test_move_selected_items_is_atomic_and_preserves_payloads(self):
        target = self.vault.create_collection("Loadout")
        first = self.vault.deposit("Vault", RAW_SWORD)
        second = self.vault.deposit("Vault", RAW_HELM)
        self.vault.set_item_layouts("Vault", [
            {"itemId": first.id, "pageIndex": 0, "x": 0, "y": 0},
            {"itemId": second.id, "pageIndex": 0, "x": 4, "y": 0},
        ])
        originals = {
            first.id: (first.raw_item_json, first.raw_sha256),
            second.id: (second.raw_item_json, second.raw_sha256),
        }
        moved = self.vault.move_items([first.id, second.id], target.id)
        self.assertEqual([row.id for row in moved], [first.id, second.id])
        for row in moved:
            self.assertEqual(row.collection_id, target.id)
            self.assertIsNone(row.page_index)
            self.assertEqual((row.raw_item_json, row.raw_sha256), originals[row.id])

        withdrawal = self.vault.reserve_withdrawal(first.id)
        before = self.vault.get_item(second.id)
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.move_items([first.id, second.id], "Vault")
        self.assertEqual(self.vault.get_item(second.id), before)
        self.vault.cancel_withdrawal(withdrawal.token)

    def test_history_preview_and_custom_name_undo_are_state_checked(self):
        item = self.vault.deposit("Vault", RAW_SWORD, label="Blade")
        named = self.vault.set_item_custom_name(item.id, "Boss Loadout")
        preview = self.vault.preview_metadata_undo()
        self.assertEqual(preview["eventType"], "item_custom_name_updated")
        self.assertEqual(preview["itemCount"], 1)
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.undo_metadata_event(preview["eventId"] + 1)
        result = self.vault.undo_metadata_event(preview["eventId"])
        self.assertEqual(result["eventType"], "item_custom_name_updated")
        restored = self.vault.get_item(item.id)
        self.assertIsNone(restored.custom_name)
        self.assertEqual(restored.raw_item_json, named.raw_item_json)
        self.assertEqual(restored.raw_sha256, named.raw_sha256)
        self.assertIsNone(self.vault.preview_metadata_undo())
        history = self.vault.list_events(limit=10)
        self.assertEqual(history[0]["eventType"], "metadata_undo_applied")
        self.assertEqual(history[0]["details"]["eventId"], preview["eventId"])

    def test_move_and_layout_undo_restore_previous_collection_grid(self):
        destination = self.vault.create_collection("Build")
        item = self.vault.deposit("Vault", RAW_HELM)
        self.vault.ensure_stash_page_count("Vault", 3)
        self.vault.set_item_layouts("Vault", [{
            "itemId": item.id, "pageIndex": 2, "x": 4, "y": 5,
        }])
        self.vault.move_items([item.id], destination.id)
        move_preview = self.vault.preview_metadata_undo()
        self.assertEqual(move_preview["eventType"], "items_moved")
        self.vault.undo_metadata_event(move_preview["eventId"])
        restored = self.vault.get_item(item.id)
        self.assertEqual(restored.collection_name, "Vault")
        self.assertEqual(
            (restored.page_index, restored.layout_x, restored.layout_y),
            (2, 4, 5),
        )

        layout_preview = self.vault.preview_metadata_undo()
        self.assertEqual(layout_preview["eventType"], "collection_layout_updated")
        self.vault.undo_metadata_event(layout_preview["eventId"])
        cleared = self.vault.get_item(item.id)
        self.assertIsNone(cleared.page_index)
        self.assertIsNone(cleared.layout_x)
        self.assertIsNone(cleared.layout_y)

    def test_reserve_and_cancel_restores_item(self):
        item = self.vault.deposit("Vault", RAW_SWORD)
        withdrawal = self.vault.reserve_withdrawal(item.id)
        self.assertEqual(withdrawal.raw_item_json, RAW_SWORD)
        self.assertEqual(self.vault.count_items(), 0)
        self.assertEqual(self.vault.count_items(status="reserved"), 1)
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.move_item(item.id, "Vault")
        cancelled = self.vault.cancel_withdrawal(withdrawal.token)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.vault.get_item(item.id).status, "available")
        self.assertEqual(self.vault.count_items(), 1)
        self.assertEqual(self.vault.cancel_withdrawal(withdrawal.token).status, "cancelled")

    def test_commit_removes_item_but_retains_exact_audit_record(self):
        item = self.vault.deposit("Vault", RAW_HELM)
        withdrawal = self.vault.reserve_withdrawal(item.id)
        committed = self.vault.commit_withdrawal(withdrawal.token)
        self.assertEqual(committed.status, "committed")
        self.assertEqual(committed.raw_item_json, RAW_HELM)
        with self.assertRaises(vault_module.VaultNotFoundError):
            self.vault.get_item(item.id)
        self.assertEqual(self.vault.commit_withdrawal(withdrawal.token).status, "committed")
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.cancel_withdrawal(withdrawal.token)

    def test_pending_reservation_survives_reopen(self):
        item = self.vault.deposit("Vault", RAW_SWORD)
        withdrawal = self.vault.reserve_withdrawal(item.id)
        reopened = vault_module.InfiniteVault(self.path)
        self.assertEqual(
            [entry.token for entry in reopened.list_pending_withdrawals()],
            [withdrawal.token],
        )
        reopened.cancel_withdrawal(withdrawal.token)
        self.assertEqual(reopened.get_item(item.id).status, "available")

    def test_deposit_key_makes_retries_idempotent(self):
        key = "sharedstash_0123456789abcdef"
        first = self.vault.deposit("Vault", RAW_SWORD, deposit_key=key)
        retry = self.vault.deposit("Vault", RAW_SWORD, deposit_key=key)
        self.assertEqual(first.id, retry.id)
        self.assertEqual(self.vault.count_items(), 1)
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.deposit("Vault", RAW_HELM, deposit_key=key)
        token = self.vault.reserve_withdrawal(first.id).token
        self.vault.commit_withdrawal(token)
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.deposit("Vault", RAW_SWORD, deposit_key=key)

    def test_backup_is_valid_pre_mutation_snapshot(self):
        backup = self.vault.backup_path
        self.assertFalse(backup.exists())
        self.vault.create_collection("After Backup")
        self.assertTrue(backup.exists())
        connection = sqlite3.connect(backup)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            names = [row[0] for row in connection.execute("SELECT name FROM collections")]
        finally:
            connection.close()
        self.assertEqual(names, ["Vault"])
        self.vault.deposit("Vault", RAW_SWORD)
        connection = sqlite3.connect(backup)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
            self.assertEqual(
                sorted(row[0] for row in connection.execute("SELECT name FROM collections")),
                ["After Backup", "Vault"],
            )
        finally:
            connection.close()

    def test_mutation_aborts_when_backup_cannot_be_written(self):
        impossible_backup = self.directory / "backup-directory"
        impossible_backup.mkdir()
        guarded = vault_module.InfiniteVault(self.path, backup_path=impossible_backup)
        with self.assertRaises(OSError):
            guarded.create_collection("Must Not Exist")
        self.assertNotIn(
            "Must Not Exist", [entry.name for entry in self.vault.list_collections()]
        )

    def test_multiple_threads_and_handles_do_not_lose_deposits(self):
        errors = []

        def worker(index):
            try:
                handle = vault_module.InfiniteVault(self.path)
                raw = json.dumps({"data": {"b": index + 1, "name": f"Item {index}"}})
                handle.deposit("Vault", raw, deposit_key=f"thread_{index:04d}_0123456789")
            except Exception as exc:  # pragma: no cover - asserted below.
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.vault.count_items(), 24)

    def test_foreign_and_newer_databases_fail_closed(self):
        foreign = self.directory / "foreign.sqlite3"
        connection = sqlite3.connect(foreign)
        try:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(vault_module.VaultSchemaError):
            vault_module.InfiniteVault(foreign)

        newer = self.directory / "newer.sqlite3"
        connection = sqlite3.connect(newer)
        try:
            connection.execute("CREATE TABLE placeholder(value TEXT)")
            connection.execute("PRAGMA user_version = 999")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(vault_module.VaultSchemaError):
            vault_module.InfiniteVault(newer)

    def test_invalid_identifiers_and_pagination_fail_before_query(self):
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.get_item("../save")
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.get_withdrawal("ABC")
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.list_items(limit=0)
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.list_items(offset=-1)
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.count_items(status="deleted")

    def test_prepare_deposit_is_hidden_idempotent_and_commits_by_hash(self):
        request_id = "deposit_0123456789abcdef"
        request_hash = vault_module.canonical_request_hash({
            "direction": "deposit",
            "source": {"type": "stash", "tab": "stash_tab_2"},
            "key": "0-0-123-3",
            "collectionId": 1,
        })
        before_hash = "a" * 64
        after_hash = "b" * 64
        prepared = self.vault.prepare_deposit(
            "Vault",
            RAW_SWORD,
            request_id=request_id,
            request_hash=request_hash,
            source_tab="stash_tab_2",
            source_key="0-0-123-3",
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
            label="Exact Sword",
        )
        self.assertEqual(prepared.status, "prepared")
        self.assertEqual(prepared.direction, "deposit")
        self.assertEqual(prepared.raw_item_json, RAW_SWORD)
        self.assertEqual(self.vault.count_items(), 0)
        self.assertEqual(self.vault.count_items(status="deposit_pending"), 1)
        self.assertEqual(self.vault.count_items(status="all"), 1)
        self.assertEqual(self.vault.list_items(), [])

        reopened = vault_module.InfiniteVault(self.path)
        self.assertEqual(reopened.list_pending_transfers()[0].request_id, request_id)
        retry = reopened.prepare_deposit(
            "Vault",
            RAW_SWORD,
            request_id=request_id,
            request_hash=request_hash,
            source_tab="stash_tab_2",
            source_key="0-0-123-3",
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
            label="Exact Sword",
        )
        self.assertEqual(retry.item_id, prepared.item_id)
        with self.assertRaises(vault_module.VaultConflictError):
            reopened.prepare_deposit(
                "Vault", RAW_SWORD,
                request_id="deposit_other_0123456789",
                request_hash=request_hash,
                source_tab="stash_tab_2", source_key="0-0-123-3",
                stash_before_sha256=before_hash,
                stash_after_sha256=after_hash,
            )
        with self.assertRaises(vault_module.VaultConflictError):
            reopened.prepare_deposit(
                "Vault",
                RAW_SWORD,
                request_id=request_id,
                request_hash="d" * 64,
                source_tab="stash_tab_2",
                source_key="0-0-123-3",
                stash_before_sha256=before_hash,
                stash_after_sha256=after_hash,
            )

        conflict = reopened.commit_deposit(request_id, "e" * 64)
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(reopened.count_items(), 0)
        committed = reopened.commit_deposit(request_id, after_hash)
        self.assertEqual(committed.status, "committed")
        self.assertEqual(reopened.count_items(), 1)
        self.assertEqual(reopened.get_item(prepared.item_id).raw_item_json, RAW_SWORD)

    def test_transfer_intent_hash_and_derived_metadata_are_revalidated(self):
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.prepare_deposit(
                "Vault", RAW_SWORD,
                request_id="bad_hash_deposit_012345",
                request_hash="0" * 64,
                source_tab="stash_tab_1", source_key="0-0-123-3",
                stash_before_sha256="1" * 64,
                stash_after_sha256="2" * 64,
            )

        item = self.vault.deposit("Vault", RAW_HELM)
        request_id = "withdraw_metadata_012345"
        request_hash = vault_module.canonical_request_hash({
            "direction": "withdrawal", "itemId": item.id,
            "target": {"type": "stash", "tab": "stash_tab_1"},
        })
        self.vault.prepare_withdrawal(
            item.id, request_id=request_id, request_hash=request_hash,
            target_tab="stash_tab_1", target_key="0-0-500-1", target_pos=[1, 2],
            stash_before_sha256="3" * 64, stash_after_sha256="4" * 64,
        )
        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.prepare_withdrawal(
                item.id, request_id=request_id, request_hash=request_hash,
                target_tab="stash_tab_1", target_key="0-0-501-1", target_pos=[1, 2],
                stash_before_sha256="3" * 64, stash_after_sha256="4" * 64,
            )

    def test_cancel_and_reconcile_deposit_preserve_only_safe_copy(self):
        before_hash = "1" * 64
        after_hash = "2" * 64
        cancelled = self.vault.prepare_deposit(
            "Vault",
            RAW_HELM,
            request_id="deposit_cancel_01234567",
            request_hash=vault_module.canonical_request_hash({
                "direction": "deposit",
                "source": {"type": "stash", "tab": "stash_tab_1"},
                "key": "0-0-222-1",
                "collectionId": 1,
            }),
            source_tab="stash_tab_1",
            source_key="0-0-222-1",
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
        )
        resolved = self.vault.reconcile_transfer(cancelled.request_id, before_hash)
        self.assertEqual(resolved.status, "cancelled")
        self.assertEqual(resolved.raw_item_json, RAW_HELM)
        self.assertEqual(self.vault.count_items(status="all"), 0)

        committed = self.vault.prepare_deposit(
            "Vault",
            RAW_HELM,
            request_id="deposit_commit_01234567",
            request_hash=vault_module.canonical_request_hash({
                "direction": "deposit",
                "source": {"type": "stash", "tab": "stash_tab_1"},
                "key": "0-0-223-1",
                "collectionId": 1,
            }),
            source_tab="stash_tab_1",
            source_key="0-0-223-1",
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
        )
        resolved = self.vault.reconcile_transfer(committed.request_id, after_hash)
        self.assertEqual(resolved.status, "committed")
        self.assertEqual(self.vault.count_items(), 1)

    def test_prepare_withdrawal_persists_destination_and_reconciles(self):
        item = self.vault.deposit("Vault", RAW_SWORD)
        before_hash = "5" * 64
        after_hash = "6" * 64
        prepared = self.vault.prepare_withdrawal(
            item.id,
            request_id="withdraw_0123456789abcdef",
            request_hash=vault_module.canonical_request_hash({
                "direction": "withdrawal", "itemId": item.id,
                "target": {"type": "stash", "tab": "stash_tab_4"},
            }),
            target_tab="stash_tab_4",
            target_key="0-0-999-3",
            target_pos=[8, 9],
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
        )
        self.assertEqual(prepared.target_tab, "stash_tab_4")
        self.assertEqual(prepared.target_key, "0-0-999-3")
        self.assertEqual(prepared.target_pos, (8, 9))
        self.assertEqual(self.vault.count_items(), 0)
        self.assertEqual(self.vault.count_items(status="reserved"), 1)

        reopened = vault_module.InfiniteVault(self.path)
        resolved = reopened.reconcile_transfer(prepared.request_id, after_hash)
        self.assertEqual(resolved.status, "committed")
        self.assertEqual(resolved.raw_item_json, RAW_SWORD)
        with self.assertRaises(vault_module.VaultNotFoundError):
            reopened.get_item(item.id)

        second = reopened.deposit("Vault", RAW_HELM)
        cancelled = reopened.prepare_withdrawal(
            second.id,
            request_id="withdraw_cancel_01234567",
            request_hash=vault_module.canonical_request_hash({
                "direction": "withdrawal", "itemId": second.id,
                "target": {"type": "stash", "tab": "stash_tab_3"},
            }),
            target_tab="stash_tab_3",
            target_key="0-0-1000-1",
            target_pos=[0, 0],
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
        )
        resolved = reopened.reconcile_transfer(cancelled.request_id, before_hash)
        self.assertEqual(resolved.status, "cancelled")
        self.assertEqual(reopened.get_item(second.id).status, "available")

    def test_unknown_reconcile_state_retains_item_and_marks_conflict(self):
        prepared = self.vault.prepare_deposit(
            "Vault",
            RAW_SWORD,
            request_id="deposit_conflict_012345",
            request_hash=vault_module.canonical_request_hash({
                "direction": "deposit",
                "source": {"type": "stash", "tab": "stash_tab_1"},
                "key": "0-0-321-3",
                "collectionId": 1,
            }),
            source_tab="stash_tab_1",
            source_key="0-0-321-3",
            stash_before_sha256="a" * 64,
            stash_after_sha256="b" * 64,
        )
        conflict = self.vault.reconcile_transfer(prepared.request_id, "c" * 64)
        self.assertEqual(conflict.status, "conflict")
        self.assertIn("neither", conflict.error)
        self.assertEqual(self.vault.count_items(status="deposit_pending"), 1)
        self.assertEqual(self.vault.list_pending_transfers()[0].request_id, prepared.request_id)

    def test_surrogates_and_sqlite_integer_overflow_are_validation_errors(self):
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.deposit("Vault", '{"data":{"b":1},"x":"\ud800"}')
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.list_items(offset=10**100)
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.count_items(collection=10**100)
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.list_items(search="\ud800")

        item = self.vault.deposit("Vault", RAW_SWORD)
        with self.assertRaises(vault_module.VaultValidationError):
            self.vault.prepare_withdrawal(
                item.id,
                request_id="withdraw_hugepos_012345",
                request_hash="f" * 64,
                target_tab="stash_tab_1",
                target_key="0-0-1-3",
                target_pos=[10**10000, 0],
                stash_before_sha256="1" * 64,
                stash_after_sha256="2" * 64,
            )

    def test_cross_process_mutations_serialize_backup_and_write(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        workers = [
            context.Process(
                target=_process_deposit_worker,
                args=(str(MODULE_PATH.parent), str(self.path), index, queue),
            )
            for index in range(6)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        errors = [queue.get(timeout=2) for _ in workers]
        queue.close()
        queue.join_thread()
        self.assertEqual(errors, [None] * len(workers))
        self.assertEqual(self.vault.count_items(), len(workers))
        connection = sqlite3.connect(self.vault.backup_path)
        try:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                len(workers) - 1,
            )
        finally:
            connection.close()

    def test_bulk_deposit_is_one_hidden_idempotent_parent_transaction(self):
        entries = [
            {
                "raw_item_json": RAW_SWORD,
                "source_tab": "stash_tab_1",
                "source_key": "0-0-100-3",
                "label": "Sword",
                "source": "stash_tab_1",
            },
            {
                "raw_item_json": RAW_HELM,
                "source_tab": "unique_items",
                "source_key": "0-0-101-0",
                "label": "Helm",
                "source": "unique_items",
            },
        ]
        intent = vault_module.canonical_request_hash({
            "direction": "bulk_deposit",
            "collectionId": 1,
            "items": [
                {
                    "sourceTab": row["source_tab"],
                    "sourceKey": row["source_key"],
                    "rawSha256": __import__("hashlib").sha256(
                        row["raw_item_json"].encode("utf-8")
                    ).hexdigest(),
                }
                for row in entries
            ],
        })
        batch = self.vault.prepare_bulk_deposit(
            1, entries, request_id="bulk_deposit_parent_012345",
            request_hash=intent, stash_before_sha256="1" * 64,
            stash_after_sha256="2" * 64,
        )
        self.assertEqual(batch.status, "prepared")
        self.assertEqual(batch.item_count, 2)
        self.assertEqual(self.vault.count_items(status="available"), 0)
        self.assertEqual(self.vault.count_items(status="deposit_pending"), 2)
        self.assertEqual(self.vault.list_pending_transfers(), [])
        self.assertEqual(len(self.vault.list_pending_transfer_batches()), 1)
        members = self.vault.list_transfer_batch_members(batch.request_id)
        with self.assertRaises(vault_module.VaultStateError):
            self.vault.commit_deposit(members[0].request_id, "2" * 64)

        retry = self.vault.prepare_bulk_deposit(
            1, entries, request_id=batch.request_id, request_hash=intent,
            stash_before_sha256="1" * 64, stash_after_sha256="2" * 64,
        )
        self.assertEqual(retry, batch)
        committed = self.vault.commit_transfer_batch(batch.request_id, "2" * 64)
        self.assertEqual(committed.status, "committed")
        self.assertEqual(self.vault.count_items(status="available"), 2)
        self.assertEqual(self.vault.list_pending_transfer_batches(), [])
        self.assertEqual(
            self.vault.commit_transfer_batch(batch.request_id, "2" * 64), committed
        )

    def test_bulk_withdrawal_requires_and_removes_complete_available_snapshot(self):
        rows = [
            self.vault.deposit(
                "Vault", json.dumps({"data": {"a": index, "b": index + 1}}),
                source_item_key=f"0-0-{index}-3",
            )
            for index in range(3)
        ]
        targets = [
            {
                "item_id": row.id,
                "raw_sha256": row.raw_sha256,
                "metadata_sha256": vault_module.canonical_request_hash({
                    "id": row.id,
                    "collectionId": row.collection_id,
                    "collectionName": row.collection_name,
                    "sourceItemKey": row.source_item_key,
                    "label": row.label,
                    "customName": row.custom_name,
                    "source": row.source,
                    "depositKey": row.deposit_key,
                    "createdAt": row.created_at,
                    "updatedAt": row.updated_at,
                }),
                "target_tab": "stash_tab_1",
                "target_key": f"0-0-{100 + index}-3",
                "target_pos": [index, 0],
            }
            for index, row in enumerate(rows)
        ]

        def intent(values):
            return vault_module.canonical_request_hash({
                "direction": "bulk_withdrawal",
                "items": [
                    {
                        "itemId": row["item_id"],
                        "rawSha256": row["raw_sha256"],
                        "metadataSha256": row["metadata_sha256"],
                        "targetTab": row["target_tab"],
                        "targetKey": row["target_key"],
                        "targetPos": row["target_pos"],
                    }
                    for row in values
                ],
            })

        with self.assertRaises(vault_module.VaultConflictError):
            self.vault.prepare_bulk_withdrawal(
                targets[:-1], request_id="bulk_incomplete_01234567",
                request_hash=intent(targets[:-1]), stash_before_sha256="3" * 64,
                stash_after_sha256="4" * 64,
            )
        self.assertEqual(self.vault.count_items(status="available"), 3)
        batch = self.vault.prepare_bulk_withdrawal(
            targets, request_id="bulk_withdraw_parent_012345",
            request_hash=intent(targets), stash_before_sha256="3" * 64,
            stash_after_sha256="4" * 64,
        )
        self.assertEqual(self.vault.count_items(status="reserved"), 3)
        self.assertEqual(len(set(
            member.request_id
            for member in self.vault.list_transfer_batch_members(batch.request_id)
        )), 3)
        committed = self.vault.reconcile_transfer_batch(batch.request_id, "4" * 64)
        self.assertEqual(committed.status, "committed")
        self.assertEqual(self.vault.count_items(status="all"), 0)
        self.assertEqual(len(self.vault.list_transfer_batch_members(batch.request_id)), 3)

    def test_bulk_withdrawal_revalidates_raw_integrity_inside_reservation(self):
        item = self.vault.deposit(
            "Vault", RAW_SWORD, source_item_key="0-0-123-3"
        )
        metadata_sha256 = vault_module.canonical_request_hash({
            "id": item.id,
            "collectionId": item.collection_id,
            "collectionName": item.collection_name,
            "sourceItemKey": item.source_item_key,
            "label": item.label,
            "customName": item.custom_name,
            "source": item.source,
            "depositKey": item.deposit_key,
            "createdAt": item.created_at,
            "updatedAt": item.updated_at,
        })
        target = {
            "item_id": item.id,
            "raw_sha256": item.raw_sha256,
            "metadata_sha256": metadata_sha256,
            "target_tab": "stash_tab_1",
            "target_key": "0-0-123-3",
            "target_pos": [0, 0],
        }
        request_hash = vault_module.canonical_request_hash({
            "direction": "bulk_withdrawal",
            "items": [{
                "itemId": target["item_id"],
                "rawSha256": target["raw_sha256"],
                "metadataSha256": target["metadata_sha256"],
                "targetTab": target["target_tab"],
                "targetKey": target["target_key"],
                "targetPos": target["target_pos"],
            }],
        })

        # Simulate corruption after a caller built its preview but before the
        # reservation transaction. The stale stored digest still matches the
        # caller's target, so only recomputing the payload catches this change.
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE items SET raw_json=? WHERE id=?", (RAW_HELM, item.id)
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(vault_module.VaultSchemaError):
            self.vault.prepare_bulk_withdrawal(
                [target],
                request_id="bulk_raw_recheck_01234567",
                request_hash=request_hash,
                stash_before_sha256="7" * 64,
                stash_after_sha256="8" * 64,
            )
        self.assertEqual(self.vault.count_items(status="available"), 1)
        self.assertEqual(self.vault.list_pending_transfer_batches(), [])

    def test_selected_bulk_withdrawal_leaves_unselected_items_available(self):
        rows = [
            self.vault.deposit(
                "Vault", json.dumps({"data": {"a": index, "b": index + 1}}),
                source_item_key=f"0-0-{index}-3",
            )
            for index in range(3)
        ]
        selected = rows[:2]
        targets = []
        for index, row in enumerate(selected):
            targets.append({
                "item_id": row.id,
                "raw_sha256": row.raw_sha256,
                "metadata_sha256": vault_module.canonical_request_hash({
                    "id": row.id,
                    "collectionId": row.collection_id,
                    "collectionName": row.collection_name,
                    "sourceItemKey": row.source_item_key,
                    "label": row.label,
                    "customName": row.custom_name,
                    "source": row.source,
                    "depositKey": row.deposit_key,
                    "createdAt": row.created_at,
                    "updatedAt": row.updated_at,
                }),
                "target_tab": "stash_tab_1",
                "target_key": f"0-0-{200 + index}-3",
                "target_pos": [index, 0],
            })
        request_hash = vault_module.canonical_request_hash({
            "direction": "bulk_withdrawal",
            "items": [
                {
                    "itemId": row["item_id"],
                    "rawSha256": row["raw_sha256"],
                    "metadataSha256": row["metadata_sha256"],
                    "targetTab": row["target_tab"],
                    "targetKey": row["target_key"],
                    "targetPos": row["target_pos"],
                }
                for row in targets
            ],
            "scope": "selection",
        })
        batch = self.vault.prepare_bulk_withdrawal(
            targets,
            request_id="bulk_selected_parent_012345",
            request_hash=request_hash,
            stash_before_sha256="9" * 64,
            stash_after_sha256="a" * 64,
            selection=True,
        )
        self.assertEqual(batch.item_count, 2)
        self.assertEqual(self.vault.count_items(status="reserved"), 2)
        self.assertEqual(self.vault.count_items(status="available"), 1)
        self.assertEqual(self.vault.get_item(rows[2].id).status, "available")
        self.vault.commit_transfer_batch(batch.request_id, "a" * 64)
        self.assertEqual(self.vault.count_items(status="all"), 1)
        self.assertEqual(self.vault.get_item(rows[2].id).raw_item_json, rows[2].raw_item_json)

    def test_bulk_hash_conflict_retains_every_pending_or_reserved_item(self):
        entry = {
            "raw_item_json": RAW_SWORD,
            "source_tab": "stash_tab_1",
            "source_key": "0-0-777-3",
            "label": "Sword",
            "source": "stash_tab_1",
        }
        intent = vault_module.canonical_request_hash({
            "direction": "bulk_deposit", "collectionId": 1,
            "items": [{
                "sourceTab": entry["source_tab"],
                "sourceKey": entry["source_key"],
                "rawSha256": __import__("hashlib").sha256(
                    entry["raw_item_json"].encode("utf-8")
                ).hexdigest(),
            }],
        })
        batch = self.vault.prepare_bulk_deposit(
            1, [entry], request_id="bulk_conflict_parent_012345",
            request_hash=intent, stash_before_sha256="5" * 64,
            stash_after_sha256="6" * 64,
        )
        conflicted = self.vault.reconcile_transfer_batch(batch.request_id, "7" * 64)
        self.assertEqual(conflicted.status, "conflict")
        self.assertEqual(self.vault.count_items(status="deposit_pending"), 1)
        self.assertEqual(len(self.vault.list_pending_transfer_batches()), 1)
        cancelled = self.vault.resolve_transfer_batch_by_evidence(
            batch.request_id, "cancelled", "7" * 64,
            "source item was verified in Shared Stash",
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.vault.count_items(status="all"), 0)


if __name__ == "__main__":
    unittest.main()
