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
        moved = self.vault.move_item(item.id, destinations[-1].id)
        self.assertEqual(moved.collection_name, "Page 79")
        self.assertEqual(self.vault.count_items(collection="Vault"), 0)
        self.assertEqual(self.vault.count_items(collection="Page 79"), 1)

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


if __name__ == "__main__":
    unittest.main()
