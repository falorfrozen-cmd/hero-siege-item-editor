"""Safety contract for the Item Editor <-> Infinite Vault boundary.

These tests deliberately exercise only temporary ``stash.hss`` and SQLite
files.  They describe the integration API expected from
``hs_item_editor_gui.py``:

* ``vault_store()`` opens ``VAULT_DB_FILE``;
* ``op_vault_deposit(body)`` and ``op_vault_withdraw(body)`` move an exact
  native item entry between a grid-backed shared-stash tab and SQLite;
* ``reconcile_vault_transfers()`` resolves interrupted two-phase transfers.

The operation bodies use a stable ``requestId``.  Repeating the same request
must be a no-op success, including after the source item has disappeared or a
withdrawn vault row has been committed. Normal numbered, Material, and Socket
Shared Stash grids are valid transfer endpoints; auto-sorted/non-grid
containers are not.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen


MODULE_DIR = Path(__file__).resolve().parent
MODULE_PATH = MODULE_DIR / "hs_item_editor_gui.py"
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
SPEC = importlib.util.spec_from_file_location(
    "hs_item_editor_gui_vault_integration_tests", MODULE_PATH
)
editor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(editor)


SOURCE_KEY = "0-0-1700000000000-10"
DEPOSIT_REQUEST = "11111111-1111-4111-8111-111111111111"
WITHDRAW_REQUEST = "22222222-2222-4222-8222-222222222222"


def native_opaque_entry() -> dict:
    """A deliberately odd but valid native record which must stay opaque."""

    socket_payload = base64.b64encode(
        json.dumps({"a": 987654.0, "b": 46.0, "n": 0.0}).encode("utf-8")
    ).decode("ascii")
    return {
        "pos": [4.0, 7.0],
        "data": {
            "w": 1.0,
            "a": 429565.0,       # Loaded Dice skill-selection seed.
            "i": 314159.0,       # Independent native dice/roll seed.
            "s": 271828.0,       # Socket-count seed/enable field.
            "j": 0.0,
            "b": 31.0,
            "c": 1.0,
            "m": 1.0,
            "o": 12345678.0,     # Stack-like opaque field, even on a charm.
            "s1": socket_payload,
            "zz": {
                "sockets": 6.0,
                "future_nested": {"flags": [1, 0, 1], "text": "keep me"},
            },
            "unknown_future_field": {"bytes": "AAECAw==", "enabled": True},
        },
        "unknown_entry_field": {"season": 10, "do_not_drop": ["x", 7]},
    }


class SimulatedPowerLoss(BaseException):
    """Bypass ordinary ``except Exception`` cleanup like a killed process."""


class InfiniteVaultIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.saves = self.directory / "saves"
        self.saves.mkdir()
        self.vault_path = self.directory / "hs_infinite_vault.sqlite3"
        self.stash_path = self.saves / "stash.hss"
        self.original_entry = native_opaque_entry()
        self.initial_stash = {
            "stash_tab_1": {SOURCE_KEY: self.original_entry},
            "stash_tab_2": {},
            "stash_tab_10": {},
            "stash_tab_19": {},
            "material_tab": {},
            "socket_tab": {},
            "unique_items": {},
            "stash_tab_data": {"future_metadata": {"must": "survive"}},
        }
        self._write_stash(self.initial_stash)

        self.old_saves = editor.SAVES
        self.had_vault_path = hasattr(editor, "VAULT_DB_FILE")
        self.old_vault_path = getattr(editor, "VAULT_DB_FILE", None)
        editor.SAVES = self.saves
        editor.VAULT_DB_FILE = self.vault_path

        # The contract intentionally requires a path-sensitive factory rather
        # than a process-global handle tied forever to the user's real DB.
        for cache_name in ("_VAULT_STORE", "_vault_instance"):
            if hasattr(editor, cache_name):
                setattr(editor, cache_name, None)

        self.game_patch = patch.object(editor, "game_running", return_value=False)
        self.game_patch.start()

    def tearDown(self):
        self.game_patch.stop()
        editor.SAVES = self.old_saves
        if self.had_vault_path:
            editor.VAULT_DB_FILE = self.old_vault_path
        else:
            delattr(editor, "VAULT_DB_FILE")
        for cache_name in ("_VAULT_STORE", "_vault_instance"):
            if hasattr(editor, cache_name):
                setattr(editor, cache_name, None)
        self.temporary.cleanup()

    def _write_stash(self, value: dict) -> None:
        self.stash_path.write_text(
            editor.encode_hss(json.dumps(value, separators=(", ", ": "))),
            encoding="ascii",
        )

    def _read_stash(self) -> dict:
        return json.loads(editor.decode_hss(self.stash_path))

    def _contract_function(self, name: str):
        function = getattr(editor, name, None)
        if not callable(function):
            self.fail(
                f"Infinite Vault integration contract is missing editor.{name}()"
            )
        return function

    def _vault(self):
        return self._contract_function("vault_store")()

    def _deposit(self, **overrides) -> dict:
        body = {
            "source": {"type": "stash", "tab": "stash_tab_1"},
            "key": SOURCE_KEY,
            "collectionId": 1,
            "requestId": DEPOSIT_REQUEST,
        }
        body.update(overrides)
        result = self._contract_function("op_vault_deposit")(body)
        self.assertIsInstance(result, dict)
        return result

    def _withdraw(self, item_id: str, **overrides) -> dict:
        body = {
            "itemId": item_id,
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "requestId": WITHDRAW_REQUEST,
        }
        body.update(overrides)
        result = self._contract_function("op_vault_withdraw")(body)
        self.assertIsInstance(result, dict)
        return result

    def _assert_ok(self, result: dict) -> None:
        self.assertNotIn("err", result, result.get("err"))
        self.assertIn("ok", result)

    @staticmethod
    def _result_item_id(result: dict) -> str | None:
        item = result.get("item")
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            return item["id"]
        for field in ("itemId", "vaultItemId"):
            if isinstance(result.get(field), str):
                return result[field]
        return None

    def _single_vault_item(self):
        items = self._vault().list_items(status="all")
        self.assertEqual(len(items), 1)
        return items[0]

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

    @staticmethod
    def _post_json(base_url: str, path: str, body: dict, timeout: float = 5.0):
        request = Request(
            base_url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                editor.EDITOR_REQUEST_HEADER: "1",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def test_shared_stash_exact_round_trip_preserves_every_opaque_field(self):
        deposited = self._deposit()
        self._assert_ok(deposited)
        self.assertEqual(self._read_stash()["stash_tab_1"], {})

        stored = self._single_vault_item()
        self.assertEqual(stored.source_item_key, SOURCE_KEY)
        self.assertEqual(json.loads(stored.raw_item_json), self.original_entry)

        withdrawn = self._withdraw(stored.id)
        self._assert_ok(withdrawn)
        restored = self._read_stash()
        self.assertEqual(restored["stash_tab_1"], {SOURCE_KEY: self.original_entry})
        self.assertEqual(
            restored["stash_tab_data"], self.initial_stash["stash_tab_data"]
        )
        self.assertEqual(self._vault().count_items(status="all"), 0)
        self.assertEqual(self._vault().list_pending_withdrawals(), [])

    def test_all_current_shared_grid_shapes_round_trip_exactly(self):
        for index, tab in enumerate((
            "stash_tab_10", "stash_tab_19", "material_tab", "socket_tab",
        )):
            with self.subTest(tab=tab):
                stash = self._read_stash()
                stash[tab][SOURCE_KEY] = json.loads(json.dumps(self.original_entry))
                self._write_stash(stash)

                deposited = self._deposit(
                    source={"type": "stash", "tab": tab},
                    requestId=f"griddeposit_{index:02d}_0123456789abcdef",
                )
                self._assert_ok(deposited)
                item_id = self._result_item_id(deposited)
                self.assertIsNotNone(item_id)
                self.assertEqual(self._read_stash()[tab], {})

                withdrawn = self._withdraw(
                    item_id,
                    target={"type": "stash", "tab": tab},
                    requestId=f"gridwithdraw_{index:02d}_0123456789abcdef",
                )
                self._assert_ok(withdrawn)
                restored = self._read_stash()
                self.assertEqual(restored[tab], {SOURCE_KEY: self.original_entry})
                self.assertEqual(self._vault().count_items(status="all"), 0)

                del restored[tab][SOURCE_KEY]
                self._write_stash(restored)

    def test_vault_meta_lists_every_existing_compatible_grid_in_native_order(self):
        payload = self._contract_function("vault_meta")()
        self.assertEqual(
            [row["tab"] for row in payload["transferTabs"]],
            [
                "stash_tab_1", "stash_tab_2", "stash_tab_10", "stash_tab_19",
                "material_tab", "socket_tab",
            ],
        )

    def test_rejects_every_non_grid_shared_stash_endpoint(self):
        before = self.stash_path.read_bytes()
        invalid_sources = [
            {"type": "bag", "slot": 1, "tab": "inventory_tab_0"},
            {"type": "stash", "tab": "unique_items"},
            {"type": "stash", "tab": "stash_tab_data"},
            {"type": "stash", "tab": "material_tab_0"},
            {"type": "stash", "tab": "socket_tab_0"},
            {"type": "stash", "tab": "stash_tab_0"},
        ]
        for index, source in enumerate(invalid_sources):
            with self.subTest(source=source):
                result = self._deposit(
                    source=source,
                    requestId=f"badsource_{index:02d}_0123456789abcdef",
                )
                self.assertIn("err", result)
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_embedded_ui_exposes_vault_action_for_every_supported_grid(self):
        self.assertIn("function isVaultTransferTab(tab)", editor.HTML)
        self.assertIn("isVaultTransferTab(target.tab)", editor.HTML)
        self.assertIn("vaultMeta.transferTabs", editor.HTML)
        self.assertNotIn("/^stash_tab_[1-9]$/", editor.HTML)

    def test_game_running_refuses_deposit_and_withdraw_without_mutation(self):
        before_stash = self.stash_path.read_bytes()
        with patch.object(editor, "game_running", return_value=True):
            blocked = self._deposit()
        self.assertIn("err", blocked)
        self.assertIn("running", blocked["err"].casefold())
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(self._vault().count_items(status="all"), 0)

        deposited = self._deposit()
        self._assert_ok(deposited)
        item = self._single_vault_item()
        before_stash = self.stash_path.read_bytes()
        before_db = self.vault_path.read_bytes()
        with patch.object(editor, "game_running", return_value=True):
            blocked = self._withdraw(item.id)
        self.assertIn("err", blocked)
        self.assertIn("running", blocked["err"].casefold())
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(self.vault_path.read_bytes(), before_db)
        self.assertEqual(self._vault().get_item(item.id).status, "available")

    def test_game_starting_during_deposit_cancels_before_stash_write(self):
        for index, sequence in enumerate((
            [False, True],
            [False, False, True],
            [False, False, False, True],
        )):
            with self.subTest(sequence=sequence):
                before = self.stash_path.read_bytes()
                with patch.object(editor, "game_running", side_effect=sequence):
                    result = self._deposit(
                        requestId=f"game_race_{index:02d}_0123456789abcdef"
                    )
                self.assertIn("err", result)
                self.assertEqual(self.stash_path.read_bytes(), before)
                self.assertIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])
                self.assertEqual(self._vault().count_items(status="all"), 0)
                self.assertEqual(self._vault().list_pending_transfers(), [])

    def test_game_starting_during_withdrawal_keeps_vault_item(self):
        self._assert_ok(self._deposit())
        item = self._single_vault_item()
        before = self.stash_path.read_bytes()
        with patch.object(editor, "game_running", side_effect=[False, False, False, True]):
            result = self._withdraw(
                item.id, requestId="withdraw_game_race_0123456789abcdef"
            )
        self.assertIn("err", result)
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(self._vault().get_item(item.id).status, "available")
        self.assertEqual(self._vault().list_pending_transfers(), [])

    def test_game_starting_during_deposit_backup_cancels_before_stash_write(self):
        before = self.stash_path.read_bytes()
        game_started = {"value": False}
        real_backup = editor.backup

        def backup_then_start_game(path):
            result = real_backup(path)
            game_started["value"] = True
            return result

        with (
            patch.object(editor, "game_running", side_effect=lambda: game_started["value"]),
            patch.object(editor, "backup", side_effect=backup_then_start_game),
        ):
            result = self._deposit(
                requestId="deposit_backup_race_0123456789abcdef"
            )

        self.assertIn("err", result)
        self.assertIn("backup", result["err"].casefold())
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])
        self.assertEqual(self._vault().count_items(status="all"), 0)
        self.assertEqual(self._vault().list_pending_transfers(), [])

    def test_game_starting_during_withdrawal_backup_restores_vault_item(self):
        self._assert_ok(self._deposit())
        item = self._single_vault_item()
        before = self.stash_path.read_bytes()
        game_started = {"value": False}
        real_backup = editor.backup

        def backup_then_start_game(path):
            result = real_backup(path)
            game_started["value"] = True
            return result

        with (
            patch.object(editor, "game_running", side_effect=lambda: game_started["value"]),
            patch.object(editor, "backup", side_effect=backup_then_start_game),
        ):
            result = self._withdraw(
                item.id,
                requestId="withdraw_backup_race_0123456789abcdef",
            )

        self.assertIn("err", result)
        self.assertIn("backup", result["err"].casefold())
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(self._vault().get_item(item.id).status, "available")
        self.assertEqual(self._vault().list_pending_transfers(), [])

    def test_full_target_tab_is_preflighted_and_leaves_vault_untouched(self):
        deposited = self._deposit()
        self._assert_ok(deposited)
        item = self._single_vault_item()

        stash = self._read_stash()
        filler_data = {"w": 1.0, "a": 100001.0, "j": 0.0,
                       "b": 61.0, "c": 1.0, "m": 1.0}
        stash["stash_tab_2"] = {
            f"0-0-{2000000 + y * 17 + x}-7": {
                "pos": [float(x), float(y)], "data": dict(filler_data)
            }
            for y in range(18)
            for x in range(17)
        }
        self._write_stash(stash)
        before_stash = self.stash_path.read_bytes()
        before_db = self.vault_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        result = self._withdraw(
            item.id,
            target={"type": "stash", "tab": "stash_tab_2"},
        )
        self.assertIn("err", result)
        self.assertIn("space", result["err"].casefold())
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(self.vault_path.read_bytes(), before_db)
        self.assertEqual(
            set(self.saves.glob("stash.hss.guibak_*")), backups_before
        )
        self.assertEqual(self._vault().get_item(item.id).status, "available")
        self.assertEqual(self._vault().list_pending_withdrawals(), [])

    def test_request_id_retries_do_not_duplicate_either_transfer(self):
        first_deposit = self._deposit()
        self._assert_ok(first_deposit)
        item = self._single_vault_item()
        first_id = self._result_item_id(first_deposit) or item.id
        after_first_deposit = self.stash_path.read_bytes()

        retry_deposit = self._deposit()
        self._assert_ok(retry_deposit)
        retry_id = self._result_item_id(retry_deposit) or self._single_vault_item().id
        self.assertEqual(retry_id, first_id)
        self.assertEqual(self.stash_path.read_bytes(), after_first_deposit)
        self.assertEqual(self._vault().count_items(status="all"), 1)

        first_withdraw = self._withdraw(item.id)
        self._assert_ok(first_withdraw)
        after_first_withdraw = self.stash_path.read_bytes()
        retry_withdraw = self._withdraw(item.id)
        self._assert_ok(retry_withdraw)
        self.assertEqual(self.stash_path.read_bytes(), after_first_withdraw)
        self.assertEqual(
            self._read_stash()["stash_tab_1"], {SOURCE_KEY: self.original_entry}
        )
        self.assertEqual(self._vault().count_items(status="all"), 0)
        self.assertEqual(self._vault().list_pending_withdrawals(), [])

    def test_same_request_lookup_cannot_cancel_an_inflight_deposit(self):
        """A retry from another process must wait for the stash-file lock.

        This deterministically models process A after its durable SQLite
        prepare and before its atomic stash replacement.  Process B can look
        up the same request at that instant, but the lookup itself must not
        reconcile the still-before stash hash.  The old implementation did,
        deleting the pending vault copy before process A removed the source.
        """

        store = self._vault()
        before_hash = editor._file_sha256(self.stash_path)
        stash_after = self._read_stash()
        raw_item_json = editor._vault_item_json(
            stash_after["stash_tab_1"][SOURCE_KEY]
        )
        del stash_after["stash_tab_1"][SOURCE_KEY]
        encoded_after = editor._encoded_stash_document(stash_after)
        after_hash = hashlib.sha256(encoded_after.encode("ascii")).hexdigest()
        request_hash = editor.canonical_request_hash({
            "direction": "deposit",
            "source": {"type": "stash", "tab": "stash_tab_1"},
            "key": SOURCE_KEY,
            "collectionId": 1,
        })

        prepared = store.prepare_deposit(
            1,
            raw_item_json,
            request_id=DEPOSIT_REQUEST,
            request_hash=request_hash,
            source_tab="stash_tab_1",
            source_key=SOURCE_KEY,
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
            label="Inflight deposit",
        )
        self.assertEqual(prepared.status, "prepared")

        observed_by_retry = editor._existing_vault_request(
            store, DEPOSIT_REQUEST, request_hash, "deposit"
        )
        self.assertEqual(observed_by_retry.status, "prepared")
        self.assertEqual(store.count_items(status="deposit_pending"), 1)
        self.assertIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])

        editor.atomic_write_text(self.stash_path, encoded_after, "ascii")
        committed = store.commit_deposit(
            DEPOSIT_REQUEST, editor._file_sha256(self.stash_path)
        )
        self.assertEqual(committed.status, "committed")
        self.assertNotIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])
        self.assertEqual(store.count_items(status="available"), 1)

    def test_same_request_lookup_cannot_cancel_an_inflight_withdrawal(self):
        """The withdrawal analogue must not duplicate an item on a retry."""

        deposited = self._deposit()
        self._assert_ok(deposited)
        store = self._vault()
        item = self._single_vault_item()
        before_hash = editor._file_sha256(self.stash_path)
        stash_after = self._read_stash()
        stash_after["stash_tab_1"][SOURCE_KEY] = item.decoded_item()
        encoded_after = editor._encoded_stash_document(stash_after)
        after_hash = hashlib.sha256(encoded_after.encode("ascii")).hexdigest()
        request_hash = editor.canonical_request_hash({
            "direction": "withdrawal",
            "itemId": item.id,
            "target": {"type": "stash", "tab": "stash_tab_1"},
        })

        prepared = store.prepare_withdrawal(
            item.id,
            request_id=WITHDRAW_REQUEST,
            request_hash=request_hash,
            target_tab="stash_tab_1",
            target_key=SOURCE_KEY,
            target_pos=(4, 7),
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
        )
        self.assertEqual(prepared.status, "prepared")

        observed_by_retry = editor._existing_vault_request(
            store, WITHDRAW_REQUEST, request_hash, "withdrawal"
        )
        self.assertEqual(observed_by_retry.status, "prepared")
        self.assertEqual(store.get_item(item.id).status, "reserved")
        self.assertEqual(self._read_stash()["stash_tab_1"], {})

        editor.atomic_write_text(self.stash_path, encoded_after, "ascii")
        committed = store.commit_withdrawal(
            WITHDRAW_REQUEST, editor._file_sha256(self.stash_path)
        )
        self.assertEqual(committed.status, "committed")
        self.assertEqual(
            self._read_stash()["stash_tab_1"], {SOURCE_KEY: self.original_entry}
        )
        self.assertEqual(store.count_items(status="all"), 0)

    def test_ordinary_save_post_holds_stash_lock_while_vault_waits(self):
        """A normal save POST and a vault transfer must observe one order.

        The delete is deliberately paused after replacing the temporary stash
        but before the HTTP handler releases its save/OS-lock scope.  A vault
        POST started at that point must not enter its operation.  Once released,
        it must read the new stash and report that the deleted source is gone.
        """

        # Make the vault journal/gate active before either request starts.
        self._vault()
        real_exclusive = editor._exclusive_save_file
        real_delete = editor.op_delete
        real_deposit = editor.op_vault_deposit
        ordinary_mutated = threading.Event()
        release_ordinary = threading.Event()
        vault_entered = threading.Event()
        vault_finished = threading.Event()
        state_guard = threading.Lock()
        lock_depth: dict[int, int] = {}
        lock_entries: list[tuple[int, Path]] = []
        mutation_had_lock: list[bool] = []

        @contextmanager
        def tracked_exclusive(path, timeout=15.0):
            with real_exclusive(path, timeout):
                identity = threading.get_ident()
                with state_guard:
                    lock_depth[identity] = lock_depth.get(identity, 0) + 1
                    lock_entries.append((identity, Path(path).resolve()))
                try:
                    yield
                finally:
                    with state_guard:
                        lock_depth[identity] -= 1

        def delayed_delete(body):
            result = real_delete(body)
            with state_guard:
                mutation_had_lock.append(lock_depth.get(threading.get_ident(), 0) > 0)
            ordinary_mutated.set()
            if not release_ordinary.wait(5):
                return {"err": "test did not release the ordinary save request"}
            return result

        def observed_deposit(body):
            vault_entered.set()
            try:
                return real_deposit(body)
            finally:
                vault_finished.set()

        ordinary_result: list[dict] = []
        vault_result: list[dict] = []
        failures: list[BaseException] = []

        def request_worker(destination, base_url, path, payload):
            try:
                destination.append(self._post_json(base_url, path, payload))
            except BaseException as exc:  # surfaced on the assertion thread
                failures.append(exc)

        delete_body = {
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "key": SOURCE_KEY,
        }
        deposit_body = {
            "source": {"type": "stash", "tab": "stash_tab_1"},
            "key": SOURCE_KEY,
            "collectionId": 1,
            "requestId": DEPOSIT_REQUEST,
        }
        with (
            patch.object(editor, "_exclusive_save_file", tracked_exclusive),
            patch.object(editor, "op_delete", delayed_delete),
            patch.object(editor, "op_vault_deposit", observed_deposit),
            self._http_server() as base_url,
        ):
            ordinary_thread = threading.Thread(
                target=request_worker,
                args=(ordinary_result, base_url, "/api/delete", delete_body),
                daemon=True,
            )
            ordinary_thread.start()
            self.assertTrue(ordinary_mutated.wait(3), "ordinary POST never mutated stash")
            self.assertNotIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])

            vault_thread = threading.Thread(
                target=request_worker,
                args=(vault_result, base_url, "/api/vault/deposit", deposit_body),
                daemon=True,
            )
            vault_thread.start()
            self.assertFalse(
                vault_entered.wait(0.25),
                "vault operation entered while ordinary save POST still held its lock",
            )
            self.assertFalse(vault_finished.is_set())

            release_ordinary.set()
            ordinary_thread.join(5)
            vault_thread.join(5)
            self.assertFalse(ordinary_thread.is_alive())
            self.assertFalse(vault_thread.is_alive())

        self.assertEqual(failures, [])
        self.assertTrue(mutation_had_lock and mutation_had_lock[0])
        self.assertGreaterEqual(len(lock_entries), 2)
        self.assertGreaterEqual(len({identity for identity, _ in lock_entries}), 2)
        self.assertTrue(
            all(path == self.stash_path.resolve() for _, path in lock_entries)
        )
        self.assertNotIn("err", ordinary_result[0])
        self.assertIn("err", vault_result[0])
        self.assertIn("not found", vault_result[0]["err"].casefold())
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_ordinary_post_reconciles_pending_journal_without_relocking(self):
        """The journal gate runs inside the already-held stash lock.

        A prepared deposit whose source still exists must be cancelled before
        an unrelated ordinary deletion is dispatched.  The guarded lock
        wrapper turns an accidental nested acquisition into an immediate test
        failure instead of allowing a 15-second lock timeout/deadlock.
        """

        other_key = "0-0-1700000000001-14"
        other_entry = {
            "pos": [12.0, 1.0],
            "data": {"a": 1234.0, "b": 44.0, "c": 0.0, "j": 0.0, "o": 7.0},
        }
        stash_before = self._read_stash()
        stash_before["stash_tab_1"][other_key] = other_entry
        self._write_stash(stash_before)

        store = self._vault()
        before_hash = editor._file_sha256(self.stash_path)
        intended_after = self._read_stash()
        raw_item_json = editor._vault_item_json(
            intended_after["stash_tab_1"][SOURCE_KEY]
        )
        del intended_after["stash_tab_1"][SOURCE_KEY]
        encoded_after = editor._encoded_stash_document(intended_after)
        after_hash = hashlib.sha256(encoded_after.encode("ascii")).hexdigest()
        request_hash = editor.canonical_request_hash({
            "direction": "deposit",
            "source": {"type": "stash", "tab": "stash_tab_1"},
            "key": SOURCE_KEY,
            "collectionId": 1,
        })
        prepared = store.prepare_deposit(
            1,
            raw_item_json,
            request_id=DEPOSIT_REQUEST,
            request_hash=request_hash,
            source_tab="stash_tab_1",
            source_key=SOURCE_KEY,
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
            label="Pending before ordinary POST",
        )
        self.assertEqual(prepared.status, "prepared")

        real_exclusive = editor._exclusive_save_file
        depth = threading.local()

        @contextmanager
        def reject_nested_exclusive(path, timeout=15.0):
            if getattr(depth, "value", 0):
                raise AssertionError("ordinary journal gate tried to re-lock stash.hss")
            with real_exclusive(path, timeout):
                depth.value = 1
                try:
                    yield
                finally:
                    depth.value = 0

        delete_body = {
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "key": other_key,
        }
        with (
            patch.object(editor, "_exclusive_save_file", reject_nested_exclusive),
            self._http_server() as base_url,
        ):
            result = self._post_json(base_url, "/api/delete", delete_body)

        self.assertNotIn("err", result, result.get("err"))
        final_stash = self._read_stash()["stash_tab_1"]
        self.assertIn(SOURCE_KEY, final_stash)
        self.assertNotIn(other_key, final_stash)
        self.assertEqual(store.get_transfer(DEPOSIT_REQUEST).status, "cancelled")
        self.assertEqual(store.count_items(status="all"), 0)
        self.assertEqual(store.list_pending_transfers(), [])

    def test_recovery_cancels_db_first_deposit_if_source_still_exists(self):
        real_atomic_write = editor.atomic_write_text

        def lose_power_before_stash_replace(path, text, encoding="utf-8"):
            if Path(path) == self.stash_path:
                raise SimulatedPowerLoss("deposit DB committed; stash not replaced")
            return real_atomic_write(path, text, encoding)

        with patch.object(editor, "atomic_write_text", lose_power_before_stash_replace):
            with self.assertRaises(SimulatedPowerLoss):
                self._deposit()

        self.assertIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])
        self.assertEqual(self._vault().count_items(status="all"), 1)

        unrelated = self._read_stash()
        unrelated["stash_tab_data"]["changed_after_crash"] = "deposit"
        self._write_stash(unrelated)

        recovered = self._contract_function("reconcile_vault_transfers")()
        self.assertIsInstance(recovered, dict)
        self.assertIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])
        self.assertEqual(self._vault().count_items(status="all"), 0)
        self.assertEqual(self._vault().list_pending_withdrawals(), [])

        # Startup recovery itself must be idempotent.
        again = self._contract_function("reconcile_vault_transfers")()
        self.assertIsInstance(again, dict)
        self.assertIn(SOURCE_KEY, self._read_stash()["stash_tab_1"])
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_recovery_commits_withdrawal_after_stash_write_won_the_race(self):
        deposited = self._deposit()
        self._assert_ok(deposited)
        item = self._single_vault_item()
        store_type = type(self._vault())

        with patch.object(
            store_type,
            "commit_withdrawal",
            side_effect=SimulatedPowerLoss(
                "stash replaced; withdrawal DB commit never ran"
            ),
        ):
            with self.assertRaises(SimulatedPowerLoss):
                self._withdraw(item.id)

        self.assertEqual(
            self._read_stash()["stash_tab_1"], {SOURCE_KEY: self.original_entry}
        )
        self.assertEqual(self._vault().count_items(status="reserved"), 1)
        pending = self._vault().list_pending_withdrawals()
        self.assertEqual(len(pending), 1)

        unrelated = self._read_stash()
        unrelated["stash_tab_data"]["changed_after_crash"] = "withdrawal"
        self._write_stash(unrelated)

        recovered = self._contract_function("reconcile_vault_transfers")()
        self.assertIsInstance(recovered, dict)
        self.assertEqual(
            self._read_stash()["stash_tab_1"], {SOURCE_KEY: self.original_entry}
        )
        self.assertEqual(self._vault().count_items(status="all"), 0)
        self.assertEqual(self._vault().list_pending_withdrawals(), [])
        self.assertEqual(
            self._vault().get_withdrawal(pending[0].token).status, "committed"
        )

    def test_deposit_recovery_does_not_mistake_an_identical_second_item_for_source(self):
        second_key = "0-0-1700000000001-10"
        stash = self._read_stash()
        stash["stash_tab_2"][second_key] = json.loads(json.dumps(self.original_entry))
        self._write_stash(stash)
        store_type = type(self._vault())

        with patch.object(
            store_type,
            "commit_deposit",
            side_effect=SimulatedPowerLoss("stash replaced before DB deposit commit"),
        ):
            with self.assertRaises(SimulatedPowerLoss):
                self._deposit(requestId="duplicate_proof_0123456789abcdef")

        changed = self._read_stash()
        self.assertNotIn(SOURCE_KEY, changed["stash_tab_1"])
        self.assertIn(second_key, changed["stash_tab_2"])
        changed["stash_tab_data"]["unrelated_after_crash"] = True
        self._write_stash(changed)

        recovered = self._contract_function("reconcile_vault_transfers")()
        self.assertEqual(recovered["conflicts"], 0)
        self.assertEqual(self._vault().count_items(), 1)
        self.assertIn(second_key, self._read_stash()["stash_tab_2"])
        self.assertEqual(
            json.loads(self._single_vault_item().raw_item_json), self.original_entry
        )


if __name__ == "__main__":
    unittest.main()
