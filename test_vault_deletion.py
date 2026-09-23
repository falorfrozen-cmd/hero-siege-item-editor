"""Destructive Vault actions: only temporary databases and save directories."""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_vault_integration import editor, native_opaque_entry
from infinite_vault import InfiniteVault, VaultError, canonical_request_hash


class VaultDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "vault.sqlite3"
        self.saves = self.root / "saves"
        self.saves.mkdir()
        self.stash = self.saves / "stash.hss"
        self.stash.write_bytes(b"untouched game save")
        for name, value in (("VAULT_DB_FILE", self.path), ("SAVES", self.saves),
                            ("_VAULT_STORE", None), ("_VAULT_STORE_PATH", None)):
            patcher = patch.object(editor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        running = patch.object(editor, "game_running", return_value=False)
        running.start()
        self.addCleanup(running.stop)
        self.store = editor.vault_store()
        self.category = self.store.create_collection('Delete "Test"')
        self.page = self.store.add_stash_page(self.category.id)
        self.raw = json.dumps(native_opaque_entry(), ensure_ascii=False)
        self.first = self.store.deposit(self.category.id, self.raw)
        self.second = self.store.deposit(self.category.id, self.raw)
        self.keeper = self.store.deposit("Vault", self.raw)
        self.store.set_item_layouts(self.category.id, [
            {"itemId": self.first.id, "pageIndex": 0, "x": 0, "y": 0},
            {"itemId": self.second.id, "pageIndex": 1, "x": 3, "y": 4},
        ])

    def preview(self, page=None):
        return self.store.preview_storage_deletion(self.category.id, page_index=page)

    def delete(self, preview):
        return self.store.delete_storage(
            preview["collectionId"], page_index=preview["pageIndex"],
            preview_token=preview["previewToken"],
        )

    def test_category_deletes_all_pages_and_items_and_retains_exact_backup(self):
        preview = self.preview()
        self.assertEqual((preview["stashCount"], preview["itemCount"]), (2, 2))
        result = self.delete(preview)
        self.assertEqual([c.name for c in self.store.list_collections()], ["Vault"])
        self.assertEqual([i.id for i in self.store.list_items()], [self.keeper.id])
        backup = self.path.with_name(result["backupName"])
        before = backup.read_bytes()
        self.store.add_stash_page("Vault")
        self.assertEqual(backup.read_bytes(), before)
        with closing(sqlite3.connect(backup)) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stash_pages").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT raw_json FROM items WHERE id=?", (self.first.id,)).fetchone()[0], self.raw)
        self.assertEqual(self.stash.read_bytes(), b"untouched game save")
        self.assertEqual(self.store.list_events()[1]["eventType"], "category_contents_deleted")

    def test_delete_first_stash_keeps_remaining_id_name_layout_across_refresh_and_compact(self):
        self.delete(self.preview(0))
        for action in ("ensure", "compact", "ensure"):
            response = editor.op_vault_layout({"action": action, "collectionId": self.category.id})
            self.assertNotIn("err", response)
            self.assertEqual([p["pageIndex"] for p in response["stashes"]], [1])
        pages = InfiniteVault(self.path).list_stash_pages(self.category.id)
        self.assertEqual([(p.id, p.name) for p in pages], [(self.page.id, "Stash 2")])
        self.assertEqual([i.id for i in self.store.list_items(collection=self.category.id)], [self.second.id])
        self.assertEqual(self.store.get_item(self.second.id).page_index, 1)

    def test_middle_and_empty_stashes_stay_deleted_and_new_items_use_existing_pages(self):
        third = self.store.add_stash_page(self.category.id)
        self.delete(self.preview(1))
        extra = self.store.deposit(self.category.id, self.raw)
        response = editor.op_vault_layout({"action": "ensure", "collectionId": self.category.id})
        self.assertNotIn("err", response)
        self.assertEqual([p["pageIndex"] for p in response["stashes"]], [0, 2])
        self.assertEqual(self.store.get_item(extra.id).page_index, 0)
        self.delete(self.preview(third.page_index))
        self.assertEqual([p.page_index for p in self.store.list_stash_pages(self.category.id)], [0])

    def test_stash_delete_preserves_other_page_item_coordinates(self):
        before = self.store.get_item(self.second.id)
        self.delete(self.preview(0))
        response = editor.op_vault_layout({"action": "ensure", "collectionId": self.category.id})
        self.assertNotIn("err", response)
        self.assertEqual(self.store.get_item(self.second.id), before)

    def test_deletion_blocks_undo_into_removed_pages_and_fallback_counts_are_current(self):
        self.assertIsNotNone(self.store.preview_metadata_undo())
        self.delete(self.preview())
        self.assertIsNone(self.store.preview_metadata_undo())
        response = editor.op_vault_layout({"action": "ensure", "collectionId": self.keeper.collection_id})
        self.assertEqual(response["stashes"][0]["itemCount"], 1)

    def test_content_change_invalidates_confirmation_without_deleting_anything(self):
        preview = self.preview()
        self.store.deposit(self.category.id, self.raw)
        with self.assertRaisesRegex(VaultError, "changed"):
            self.delete(preview)
        self.assertEqual(self.store.count_items(collection=self.category.id), 3)
        self.assertEqual(list(self.root.glob("*.before-delete-*.bak")), [])

    def test_moved_item_invalidates_stash_confirmation(self):
        preview = self.preview(0)
        self.store.set_item_layouts(self.category.id, [
            {"itemId": self.second.id, "pageIndex": 0, "x": 3, "y": 4},
        ])
        with self.assertRaisesRegex(VaultError, "changed"):
            self.delete(preview)
        self.assertEqual(self.store.count_items(), 3)

    def test_repeated_delete_cannot_delete_another_stash(self):
        preview = self.preview(1)
        self.delete(preview)
        replacement = self.store.add_stash_page(self.category.id)
        self.assertEqual(replacement.page_index, 1)
        with self.assertRaisesRegex(VaultError, "changed"):
            self.delete(preview)
        self.assertEqual(len(self.store.list_stash_pages(self.category.id)), 2)

    def test_reserved_item_blocks_category_and_stash_deletion(self):
        previews = [self.preview(), self.preview(1)]
        self.store.reserve_withdrawal(self.first.id)
        for preview in previews:
            with self.subTest(page=preview["pageIndex"]):
                with self.assertRaisesRegex(VaultError, "reserved|pending"):
                    self.delete(preview)
        self.assertEqual(self.store.count_items(status="reserved"), 1)

    def test_pending_deposit_then_conflict_blocks_deletion_and_committed_journal_survives(self):
        intent = {"direction": "deposit", "source": {"type": "stash", "tab": "stash_tab_1"},
                  "key": "test-source", "collectionId": self.category.id}
        request_id = "delete_test_deposit_012345"
        self.store.prepare_deposit(
            self.category.id, self.raw, request_id=request_id,
            request_hash=canonical_request_hash(intent), source_tab="stash_tab_1",
            source_key="test-source", stash_before_sha256="1" * 64, stash_after_sha256="2" * 64,
        )
        for observed in (None, "3" * 64):
            if observed:
                self.store.commit_deposit(request_id, observed)
            for page in (None, 0):
                with self.assertRaisesRegex(VaultError, "pending transfers"):
                    self.preview(page)
        self.store.commit_deposit(request_id, "2" * 64)
        result = self.delete(self.preview())
        self.assertEqual(result["itemCount"], 3)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("SELECT status,collection_id FROM transfers WHERE request_id=?", (request_id,)).fetchone(), ("committed", None))

    def test_last_category_and_last_stash_are_protected(self):
        with self.assertRaisesRegex(VaultError, "last stash"):
            self.store.preview_storage_deletion("Vault", page_index=0)
        self.delete(self.preview())
        with self.assertRaisesRegex(VaultError, "last Vault category"):
            self.store.preview_storage_deletion("Vault")
        self.assertEqual(self.store.count_items(), 1)

    def test_backup_failure_prevents_deletion(self):
        original = self.store._backup_existing
        def backup(destination=None):
            if destination is not None:
                raise OSError("disk full")
            return original()
        with patch.object(self.store, "_backup_existing", side_effect=backup):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.delete(self.preview())
        self.assertEqual(self.store.count_items(), 3)
        self.assertEqual(len(self.store.list_stash_pages(self.category.id)), 2)

    def test_failure_after_item_delete_rolls_back_category_pages_and_items(self):
        preview = self.preview()
        with patch.object(self.store, "_event", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.delete(preview)
        self.assertEqual(self.store.count_items(), 3)
        self.assertEqual(len(self.store.list_stash_pages(self.category.id)), 2)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_api_requires_preview_and_valid_integer_target(self):
        for value in (True, "2", None, -1):
            result = editor.op_vault_collections({"action": "delete", "collectionId": value})
            self.assertIn("err", result)
        for value in (True, "0", None, -1, 999):
            result = editor.op_vault_stashes({"action": "previewDelete", "collectionId": self.category.id, "pageIndex": value})
            self.assertIn("err", result)
        result = editor.op_vault_collections({"action": "delete", "collectionId": self.category.id})
        self.assertIn("preview", result["err"])
        self.assertEqual(self.store.count_items(), 3)

    def test_game_starting_after_preview_blocks_deletion(self):
        preview = self.preview()
        with patch.object(editor, "game_running", return_value=True):
            result = editor.op_vault_collections({"action": "delete", **preview})
        self.assertIn("Close Hero Siege", result["err"])
        self.assertEqual(self.store.count_items(), 3)

    def test_http_preview_confirm_and_csrf_guard(self):
        server = editor.ThreadingHTTPServer(("127.0.0.1", 0), editor.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        endpoint = f"http://127.0.0.1:{server.server_port}/api/vault/stashes"
        def request(body, *, authorized=True):
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["X-Hero-Siege-Item-Editor"] = "1"
            with urlopen(Request(endpoint, data=json.dumps(body).encode(), headers=headers), timeout=10) as response:
                return json.load(response)
        body = {"action": "previewDelete", "collectionId": self.category.id, "pageIndex": 0}
        with patch.object(editor, "_active_peer_editor_error", return_value=None):
            preview = request(body)
            self.assertEqual(preview["itemCount"], 1)
            self.assertEqual(self.store.count_items(), 3)
            confirmation = {**body, "action": "delete", "previewToken": preview["previewToken"]}
            with self.assertRaises(HTTPError) as denied:
                request(confirmation, authorized=False)
            self.assertEqual(denied.exception.code, 403)
            self.assertEqual(self.store.count_items(), 3)
            result = request(confirmation)
            self.assertNotIn("err", result)
            self.assertEqual(result["itemCount"], 1)
        self.assertEqual(self.store.count_items(), 2)
        self.assertEqual(self.stash.read_bytes(), b"untouched game save")


if __name__ == "__main__":
    unittest.main()
