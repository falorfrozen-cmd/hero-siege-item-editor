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
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing, contextmanager
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

    def _full_bulk_stash(self) -> dict:
        stash = self._read_stash()
        stash.setdefault("material_tab", {})
        stash.setdefault("socket_tab", {})
        stash.setdefault("unique_items", {})
        for index in range(1, 20):
            stash.setdefault(f"stash_tab_{index}", {})
        stash["stash_reset"] = 0.0
        stash["stash_tab_data"] = {
            "NH": [], "LocalNH": [], "SH": [], "BP": [],
            "LocalNS": [{"tab": -5.0, "name": "Unique"}],
            "SS": [], "NS": [], "Odyssey": [],
        }
        return stash

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

    def test_vault_payload_is_compact_and_uses_semantic_tooltip_fingerprint(self):
        self._assert_ok(self._deposit())
        stored = self._single_vault_item()
        build_status = {
            "matched": True,
            "code": "matched",
            "message": "Verified test build.",
            "expectedSha256": editor.EXPECTED_GAME_EXE_SHA256,
            "detectedSha256": editor.EXPECTED_GAME_EXE_SHA256,
        }
        named = self._vault().set_item_custom_name(stored.id, "Boss <Melter>")
        payload = editor._vault_item_payload(named, build_status)

        self.assertEqual(payload["customName"], "Boss <Melter>")
        self.assertEqual(payload["gameTooltip"]["item"]["customName"], "Boss <Melter>")
        self.assertEqual(payload["fingerprint"], payload["gameTooltip"]["fingerprint"])
        self.assertRegex(payload["fingerprint"], r"^[0-9A-F]{16}$")
        self.assertNotIn("raw", payload)
        self.assertNotIn("rollProfile", payload)
        self.assertNotIn("skillSelector", payload)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("unknown_future_field", serialized)
        self.assertNotIn("do_not_drop", serialized)

        moved_entry = json.loads(json.dumps(self.original_entry))
        moved_entry["pos"] = [12.0, 15.0]
        second = self._vault().deposit(
            "Vault",
            json.dumps(moved_entry, separators=(",", ":")),
            source_item_key=SOURCE_KEY,
            label="Loaded Dice",
        )
        second_payload = editor._vault_item_payload(second, build_status)
        self.assertNotEqual(stored.raw_sha256, second.raw_sha256)
        self.assertEqual(payload["fingerprint"], second_payload["fingerprint"])

    def test_custom_name_api_updates_and_clears_metadata_without_touching_native_item(self):
        self._assert_ok(self._deposit())
        stored = self._single_vault_item()
        raw_before = stored.raw_item_json
        sha_before = stored.raw_sha256
        named = editor.op_vault_item({
            "action": "setCustomName",
            "itemId": stored.id,
            "customName": "<img src=x onerror=alert(1)>",
        })
        self._assert_ok(named)
        self.assertEqual(
            named["item"]["customName"], "<img src=x onerror=alert(1)>"
        )
        cleared = editor.op_vault_item({
            "action": "setCustomName",
            "itemId": stored.id,
            "customName": "   ",
        })
        self._assert_ok(cleared)
        self.assertIsNone(cleared["item"]["customName"])

        final = self._vault().get_item(stored.id)
        self.assertEqual(final.raw_item_json, raw_before)
        self.assertEqual(final.raw_sha256, sha_before)

    def test_tooltip_identity_is_enriched_with_the_verified_subskill_name(self):
        # Load the immutable proven-build assets without consulting the locally
        # installed executable.  The developer machine may already be on a
        # newer patch, while this unit test deliberately exercises the old
        # verified model with an explicit matched build attestation below.
        proven_rolls = editor.load_roll_profile_database(editor.BASE)
        proven_dice = editor.load_dice_skill_database(editor.BASE)
        item = editor.resolve(
            "0-0-1700000000100-10",
            {"c": 1.0, "b": 89.0, "j": 0.0, "a": 123456789.0},
        )
        item["rollProfile"] = proven_rolls.lookup("unique", 10, 0, 89)
        item["skillSelector"] = proven_dice.selector(
            "unique:10:0:89", item["raw"]["a"]
        )
        model = editor._game_tooltip_model(item, build_status={
            "matched": True,
            "code": "matched",
            "message": "Verified test build.",
            "expectedSha256": editor.EXPECTED_GAME_EXE_SHA256,
            "detectedSha256": editor.EXPECTED_GAME_EXE_SHA256,
        })
        self.assertEqual(model["item"]["canonicalName"], "Overloaded Dice")
        self.assertEqual(len(model["identities"]), 1)
        self.assertEqual(model["identities"][0]["selectedIdentity"], 56)
        self.assertEqual(model["identities"][0]["selectedName"], "Pirate: Buckshot")

    def test_embedded_ui_has_safe_shared_tooltip_compare_and_custom_name_controls(self):
        html = editor.HTML
        for marker in (
            "function renderGameTooltip(model,options={})",
            "function tooltipDifferenceKeys(left,right)",
            "const vaultCompareItems=new Map()",
            "function openVaultCompare()",
            "async function editVaultCustomName(row)",
            "function packVaultGridPages(items,persistent=false)",
            "function vaultGridPageHTML(page,index,persistent=false,stash=null)",
            "function showVaultCtx(x,y,row,el)",
            "data-vault-stash-name",
            "vault-grid-item",
            "registerPreviewModel(row.gameTooltip)",
            "Array.isArray(model.identities)",
            "Max endpoints",
            "formattedValue:'—'",
            "function tooltipComparisonLayout(left,right)",
        ):
            self.assertIn(marker, html)
        self.assertIn(
            "e.target.closest('button,input,select,textarea')", html
        )
        self.assertIn("row.customName||row.name", html)
        self.assertIn("VAULT_GRID_COLS=17,VAULT_GRID_ROWS=18", html)
        self.assertIn("data-vault-stash-roll", html)
        self.assertIn("data-vault-stash-send", html)
        self.assertIn("function openStackAmountDialog", html)
        self.assertIn("Add stack...", html)
        self.assertIn("action:'addStack'", html)
        self.assertIn("/api/vault/layout", html)
        self.assertIn("/api/vault/selection-preview", html)
        self.assertIn("action:'moveMany'", html)
        custom_name_ui = html.split(
            "async function editVaultCustomName(row){", 1
        )[1].split("async function withdrawVaultItem", 1)[0]
        self.assertNotIn("prompt(", custom_name_ui)
        self.assertIn('maxlength="128"', custom_name_ui)
        self.assertIn("Vault Custom Name", custom_name_ui)
        self.assertIn("SAVE", custom_name_ui)
        self.assertIn("CLEAR", custom_name_ui)
        vault_renderer = html.split("function renderVaultItems(payload){", 1)[1]
        vault_renderer = vault_renderer.split("async function withdrawVaultItem", 1)[0]
        self.assertIn("packVaultGridPages(rows,true)", vault_renderer)
        self.assertIn("item.oncontextmenu", vault_renderer)
        self.assertIn("showVaultCtx", vault_renderer)
        self.assertNotIn("vault-card", vault_renderer)
        self.assertNotIn("data-raw", vault_renderer)
        self.assertNotIn("data-roll", vault_renderer)
        self.assertNotIn("data-skill", vault_renderer)

    def test_infinite_vault_keeps_the_primary_screen_simple(self):
        html = editor.HTML.split("async function openVault(reset=true){", 1)[1]
        html = html.split("function vaultBulkSessionKey", 1)[0]
        for marker in (
            'class="vault-page-head"',
            'id="vaultnew"',
            'id="vaultcategory"',
            'id="vaultnewgrid"',
            '+ CATEGORY',
            '+ STASH',
            'class="vault-tools-menu"',
        ):
            self.assertIn(marker, html)
        self.assertNotIn("Complete Stash Transfer", html)
        self.assertNotIn("TRANSFER ITEMS", html)
        self.assertNotIn("Search this vault", html)
        self.assertNotIn("All Items", html)
        self.assertNotIn('id="vaultbulkin"', html)
        self.assertNotIn('id="vaultbulkout"', html)
        self.assertNotIn("Database: ${esc(vaultMeta.databaseName", html)
        self.assertIn("}while(payload.hasMore);", editor.HTML)
        self.assertIn("function openSharedStashVaultTransfer", editor.HTML)
        self.assertIn("TO INFINITE VAULT", editor.HTML)
        self.assertIn("destinationPageIndex", editor.HTML)
        self.assertIn("data-vault-stash-source", editor.HTML)

    def test_bulk_verified_roll_preview_and_apply_are_one_safe_write(self):
        cap_key = "0-0-1700000000999-0"
        cap = {
            "pos": [0.0, 0.0],
            "data": {"a": 1.0, "j": 0.0, "b": 0.0, "c": 0.0, "o": 1.0},
        }
        stash = self._read_stash()
        stash["stash_tab_1"][cap_key] = cap
        self._write_stash(stash)
        target = {"type": "stash", "tab": "stash_tab_1"}

        preview = editor.op_bulk_roll({"action": "preview", "target": target})
        self.assertEqual(preview["changeCount"], 1)
        self.assertEqual(preview["exactCount"], 1)
        self.assertEqual(preview["skippedCount"], 1)  # Loaded Dice selector.
        self.assertEqual(preview["blockedCount"], 0)
        self.assertTrue(preview["canRun"])
        before_original = json.loads(json.dumps(self.original_entry))
        result = editor.op_bulk_roll({
            "action": "apply", "target": target,
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(result)
        self.assertEqual(result["changeCount"], 1)
        changed = self._read_stash()["stash_tab_1"]
        self.assertEqual(changed[SOURCE_KEY], before_original)
        self.assertEqual(changed[cap_key]["data"]["a"], 172693)
        self.assertTrue(result["backup"])

    def test_shared_stash_max_sets_only_proven_stackables_to_native_x999(self):
        material_key = "0-0-1700000000998-14"
        stash = self._read_stash()
        stash["material_tab"][material_key] = {
            "pos": [0.0, 0.0],
            "data": {"a": 123456.0, "j": 0.0, "b": 70.0, "c": 0.0, "o": 2.0},
        }
        self._write_stash(stash)
        target = {"type": "stash", "tab": "material_tab"}

        preview = editor.op_bulk_roll({"action": "preview", "target": target})
        self.assertEqual(preview["changeCount"], 1)
        self.assertEqual(preview["stackMaxCount"], 1)
        self.assertEqual(preview["exactCount"], 0)
        self.assertTrue(preview["canRun"])
        result = editor.op_bulk_roll({
            "action": "apply", "target": target,
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(result)
        self.assertEqual(
            self._read_stash()["material_tab"][material_key]["data"]["o"],
            999.0,
        )

    def test_add_stack_adds_delta_in_shared_stash_and_rejects_equipment(self):
        material_key = "0-0-1700000000997-14"
        stash = self._read_stash()
        stash["material_tab"][material_key] = {
            "pos": [0.0, 0.0],
            "data": {"a": 123456.0, "j": 0.0, "b": 70.0, "c": 0.0, "o": 2.0},
        }
        self._write_stash(stash)

        result = editor.op_modify({
            "action": "addstack",
            "target": {"type": "stash", "tab": "material_tab"},
            "key": material_key,
            "count": 500,
        })
        self._assert_ok(result)
        self.assertEqual(result["stack"], 502)
        self.assertEqual(
            self._read_stash()["material_tab"][material_key]["data"]["o"],
            502.0,
        )

        before = self.stash_path.read_bytes()
        rejected = editor.op_modify({
            "action": "addstack",
            "target": {"type": "stash", "tab": "stash_tab_1"},
            "key": SOURCE_KEY,
            "count": 500,
        })
        self.assertIn("err", rejected)
        self.assertEqual(self.stash_path.read_bytes(), before)

    def test_vault_add_stack_then_max_is_atomic_and_catalog_guarded(self):
        material_key = "0-0-1700000000996-14"
        raw = json.dumps({
            "pos": [0.0, 0.0],
            "data": {"a": 123456.0, "j": 0.0, "b": 70.0, "c": 0.0, "o": 2.0},
        }, separators=(",", ":"))
        vault = self._vault()
        item = vault.deposit(
            "Vault", raw, source_item_key=material_key, label="Angel's Wisdom"
        )
        vault.set_item_layouts("Vault", [{
            "itemId": item.id, "pageIndex": 0, "x": 0, "y": 0,
        }])

        added = editor.op_vault_item({
            "action": "addStack", "itemId": item.id, "count": 500,
        })
        self._assert_ok(added)
        self.assertEqual(added["stack"], 502)
        self.assertEqual(vault.get_item(item.id).decoded_item()["data"]["o"], 502.0)

        preview = editor.op_vault_roll({
            "action": "preview", "collectionId": 1, "pageIndex": 0,
        })
        self.assertEqual(preview["changeCount"], 1)
        self.assertEqual(preview["stackMaxCount"], 1)
        applied = editor.op_vault_roll({
            "action": "apply", "collectionId": 1, "pageIndex": 0,
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(applied)
        self.assertEqual(vault.get_item(item.id).decoded_item()["data"]["o"], 999.0)

        singleton = editor.resolve(
            "0-0-1700000000995-14",
            {"a": 1.0, "j": 0.0, "b": 59.0, "c": 0.0},
        )
        self.assertFalse(singleton["stackable"])

    def test_vault_stash_verified_roll_preview_and_apply_are_atomic(self):
        cap_key = "0-0-1700000000999-0"
        raw = json.dumps({
            "pos": [0.0, 0.0],
            "data": {"a": 1.0, "j": 0.0, "b": 0.0, "c": 0.0, "o": 1.0},
        }, separators=(",", ":"))
        vault = self._vault()
        item = vault.deposit("Vault", raw, source_item_key=cap_key, label="Cap")
        vault.set_item_layouts("Vault", [{
            "itemId": item.id, "pageIndex": 0, "x": 0, "y": 0,
        }])

        preview = editor.op_vault_roll({
            "action": "preview", "collectionId": 1, "pageIndex": 0,
        })
        self.assertEqual(preview["changeCount"], 1)
        self.assertEqual(preview["exactCount"], 1)
        self.assertTrue(preview["canRun"])
        result = editor.op_vault_roll({
            "action": "apply", "collectionId": 1, "pageIndex": 0,
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(result)
        self.assertEqual(vault.get_item(item.id).decoded_item()["data"]["a"], 172693)
        self.assertTrue(result["backup"])

    def test_vault_stash_roll_rejects_stale_preview_without_partial_write(self):
        cap_key = "0-0-1700000000999-0"
        raw = json.dumps({
            "pos": [0.0, 0.0],
            "data": {"a": 1.0, "j": 0.0, "b": 0.0, "c": 0.0, "o": 1.0},
        }, separators=(",", ":"))
        vault = self._vault()
        item = vault.deposit("Vault", raw, source_item_key=cap_key, label="Cap")
        vault.set_item_layouts("Vault", [{
            "itemId": item.id, "pageIndex": 0, "x": 0, "y": 0,
        }])
        preview = editor.op_vault_roll({
            "action": "preview", "collectionId": 1, "pageIndex": 0,
        })
        changed = json.dumps({
            "pos": [0.0, 0.0],
            "data": {"a": 2.0, "j": 0.0, "b": 0.0, "c": 0.0, "o": 1.0},
        }, separators=(",", ":"))
        vault.update_stash_item_payloads("Vault", 0, [{
            "itemId": item.id,
            "expectedSha256": item.raw_sha256,
            "rawItemJson": changed,
        }])
        before = self.vault_path.read_bytes()
        result = editor.op_vault_roll({
            "action": "apply", "collectionId": 1, "pageIndex": 0,
            "previewToken": preview["previewToken"],
        })
        self.assertEqual(result.get("code"), "preview_stale")
        self.assertEqual(self.vault_path.read_bytes(), before)

    def test_bulk_verified_roll_rejects_stale_preview_without_write(self):
        cap_key = "0-0-1700000000999-0"
        stash = self._read_stash()
        stash["stash_tab_1"][cap_key] = {
            "pos": [0.0, 0.0],
            "data": {"a": 1.0, "j": 0.0, "b": 0.0, "c": 0.0, "o": 1.0},
        }
        self._write_stash(stash)
        target = {"type": "stash", "tab": "stash_tab_1"}
        preview = editor.op_bulk_roll({"action": "preview", "target": target})
        changed = self._read_stash()
        changed["stash_tab_1"][cap_key]["data"]["a"] = 2.0
        self._write_stash(changed)
        before = self.stash_path.read_bytes()
        backups = set(self.saves.glob("stash.hss.guibak_*"))
        result = editor.op_bulk_roll({
            "action": "apply", "target": target,
            "previewToken": preview["previewToken"],
        })
        self.assertEqual(result.get("code"), "preview_stale")
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups)

    def test_ui_exposes_simple_mode_and_bulk_roll_preflight(self):
        html = editor.HTML
        self.assertIn('id="mode-toggle"', html)
        self.assertIn("function applyEditorMode()", html)
        self.assertIn("advancedMode&&!isEq", html)
        self.assertIn("MAX / BEST THIS TAB", html)
        self.assertIn("MAX / BEST EQUIPPED", html)
        self.assertIn("/api/roll/bulk", html)
        self.assertIn("previewToken:preview.previewToken", html)

    def test_frozen_build_bundles_exact_tooltip_module_and_model(self):
        spec_text = (MODULE_DIR / "HeroSiegeItemEditor.spec").read_text(encoding="utf-8")
        self.assertIn('"hs_tooltip_roll_models.json"', spec_text)
        self.assertIn('"exact_tooltip"', spec_text)

    def test_bulk_complete_stash_round_trip_is_exact_including_unique_duplicates_and_metadata(self):
        stash = self._full_bulk_stash()
        duplicate = json.loads(json.dumps(self.original_entry))
        duplicate["pos"] = [8.0, 7.0]
        stash["stash_tab_2"][SOURCE_KEY] = duplicate
        unique = json.loads(json.dumps(self.original_entry))
        unique.pop("pos", None)
        stash["unique_items"]["0-0-1700000000101-10"] = unique
        stash["material_tab"]["0-0-1700000000102-14"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 123.0, "j": 0.0, "b": 71.0, "c": 0.0, "o": 37.0},
        }
        stash["socket_tab"]["0-0-1700000000103-15"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 124.0, "j": 0.0, "b": 136.0, "c": 0.0, "o": 8.0},
        }
        original = json.loads(json.dumps(stash))
        self._write_stash(stash)

        preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
        })
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["itemCount"], 5)
        explicit_all_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["all"],
        })
        self.assertEqual(explicit_all_preview["previewToken"], preview["previewToken"])
        deposited = editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "all",
            "requestId": "bulk_full_deposit_0123456789",
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(deposited)
        self.assertEqual(deposited["movedCount"], 5)
        emptied = self._read_stash()
        self.assertTrue(all(not emptied[tab] for tab in editor.BULK_STASH_ITEM_TABS))
        self.assertEqual(emptied["stash_tab_data"], original["stash_tab_data"])
        self.assertEqual(self._vault().count_items(status="available"), 5)

        before_retry = self.stash_path.read_bytes()
        retry = editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "requestId": "bulk_full_deposit_0123456789",
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(retry)
        self.assertEqual(self.stash_path.read_bytes(), before_retry)
        self.assertEqual(self._vault().count_items(status="available"), 5)

        return_preview = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
        })
        self.assertEqual(return_preview["itemCount"], 5)
        returned = editor.op_vault_bulk({
            "direction": "vault-to-stash",
            "requestId": "bulk_full_withdraw_012345678",
            "previewToken": return_preview["previewToken"],
        })
        self._assert_ok(returned)
        self.assertEqual(self._read_stash(), original)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_bulk_selected_numbered_tab_moves_only_that_tab_and_retries_exactly(self):
        stash = self._full_bulk_stash()
        selected_key = "0-0-1700000000200-10"
        selected_entry = json.loads(json.dumps(self.original_entry))
        selected_entry["pos"] = [11.0, 8.0]
        selected_entry["data"]["a"] = 987001.0
        stash["stash_tab_2"][selected_key] = selected_entry
        stash["material_tab"]["0-0-1700000000201-14"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 987002.0, "j": 0.0, "b": 71.0, "c": 0.0, "o": 37.0},
        }
        original = json.loads(json.dumps(stash))
        self._write_stash(stash)

        preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_2"],
        })
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["sourceTab"], "stash_tab_2")
        self.assertEqual(preview["sourceLabel"], "Shared Stash Tab 2")
        self.assertEqual(preview["itemCount"], 1)
        self.assertEqual(
            preview["tabCounts"],
            [{"tab": "stash_tab_2", "label": "Shared Stash Tab 2", "count": 1}],
        )

        request = {
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "stash_tab_2",
            "requestId": "bulk_selected_tab_2_012345678",
            "previewToken": preview["previewToken"],
        }
        result = editor.op_vault_bulk(request)
        self._assert_ok(result)
        self.assertEqual(result["movedCount"], 1)
        after = self._read_stash()
        self.assertEqual(after["stash_tab_2"], {})
        for tab in editor.BULK_STASH_ITEM_TABS:
            if tab != "stash_tab_2":
                self.assertEqual(after[tab], original[tab], tab)
        self.assertEqual(after["stash_tab_data"], original["stash_tab_data"])
        stored = self._vault().list_items(status="available")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].source, "stash_tab_2")
        self.assertEqual(stored[0].source_item_key, selected_key)

        bytes_after = self.stash_path.read_bytes()
        backups_after = set(self.saves.glob("stash.hss.guibak_*"))
        retry = editor.op_vault_bulk(request)
        self._assert_ok(retry)
        self.assertEqual(self.stash_path.read_bytes(), bytes_after)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_after)
        self.assertEqual(self._vault().count_items(status="available"), 1)

        other_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_1"],
        })
        reused_elsewhere = editor.op_vault_bulk({
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "stash_tab_1",
            "requestId": request["requestId"],
            "previewToken": other_preview["previewToken"],
        })
        self.assertEqual(reused_elsewhere.get("code"), "preview_stale")
        self.assertEqual(self._read_stash()["stash_tab_1"], original["stash_tab_1"])
        self.assertEqual(self._vault().count_items(status="available"), 1)

        return_preview = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
        })
        returned = editor.op_vault_bulk({
            "direction": "vault-to-stash",
            "requestId": "bulk_selected_return_012345678",
            "previewToken": return_preview["previewToken"],
        })
        self._assert_ok(returned)
        self.assertEqual(self._read_stash(), original)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_shared_stash_tab_moves_into_exact_named_vault_stash(self):
        self._write_stash(self._full_bulk_stash())
        vault = self._vault()
        second = vault.add_stash_page("Vault")
        self.assertEqual(second.page_index, 1)
        preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_1"],
            "destinationPageIndex": ["1"],
        })
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["destinationPageIndex"], 1)
        self.assertEqual(preview["destinationStashName"], "Stash 2")
        request = {
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "stash_tab_1",
            "destinationPageIndex": 1,
            "requestId": "bulk_exact_vault_stash_0123456",
            "previewToken": preview["previewToken"],
        }
        result = editor.op_vault_bulk(request)
        self._assert_ok(result)
        self.assertEqual(self._read_stash()["stash_tab_1"], {})
        stored = vault.list_items(status="available")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].page_index, 1)
        self.assertEqual((stored[0].layout_x, stored[0].layout_y), (4, 7))

        retry_bytes = self.stash_path.read_bytes()
        retry = editor.op_vault_bulk(request)
        self._assert_ok(retry)
        self.assertEqual(self.stash_path.read_bytes(), retry_bytes)

    def test_bulk_selected_special_tabs_are_independent_sources(self):
        stash = self._full_bulk_stash()
        stash["material_tab"]["0-0-1700000000300-14"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 988001.0, "j": 0.0, "b": 71.0, "c": 0.0, "o": 37.0},
        }
        stash["socket_tab"]["0-0-1700000000301-15"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 988002.0, "j": 0.0, "b": 136.0, "c": 0.0, "o": 8.0},
        }
        unique = json.loads(json.dumps(self.original_entry))
        unique.pop("pos", None)
        unique["data"]["a"] = 988003.0
        stash["unique_items"]["0-0-1700000000302-10"] = unique
        metadata = json.loads(json.dumps(stash["stash_tab_data"]))
        numbered = json.loads(json.dumps(stash["stash_tab_1"]))
        self._write_stash(stash)

        for index, (tab, label) in enumerate((
            ("material_tab", "Material Tab"),
            ("socket_tab", "Socket Tab"),
            ("unique_items", "Unique Tab"),
        )):
            with self.subTest(tab=tab):
                preview = editor.vault_bulk_preview({
                    "direction": ["stash-to-vault"],
                    "collectionId": ["1"],
                    "sourceTab": [tab],
                })
                self.assertNotIn("err", preview, preview.get("err"))
                self.assertEqual(preview["sourceLabel"], label)
                self.assertEqual(preview["itemCount"], 1)
                moved = editor.op_vault_bulk({
                    "direction": "stash-to-vault",
                    "collectionId": 1,
                    "sourceTab": tab,
                    "requestId": f"bulk_special_{index}_012345678901",
                    "previewToken": preview["previewToken"],
                })
                self._assert_ok(moved)
                self.assertEqual(self._read_stash()[tab], {})
                self.assertEqual(self._read_stash()["stash_tab_1"], numbered)
                self.assertEqual(self._read_stash()["stash_tab_data"], metadata)

        self.assertEqual(self._vault().count_items(status="available"), 3)

    def test_bulk_empty_selected_tab_is_noop_even_when_other_tabs_have_items(self):
        stash = self._full_bulk_stash()
        original = json.loads(json.dumps(stash))
        self._write_stash(stash)
        preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_2"],
        })
        self.assertTrue(preview["empty"])
        self.assertFalse(preview["canRun"])
        self.assertEqual(preview["itemCount"], 0)
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        result = editor.op_vault_bulk({
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "stash_tab_2",
            "requestId": "bulk_empty_selected_012345678",
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(result)
        self.assertEqual(result["code"], "empty")
        self.assertEqual(result["movedCount"], 0)
        self.assertEqual(self._read_stash(), original)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_bulk_source_tab_is_strictly_validated_and_preview_bound(self):
        stash = self._full_bulk_stash()
        second = json.loads(json.dumps(self.original_entry))
        second["pos"] = [8.0, 8.0]
        second["data"]["a"] = 989001.0
        stash["stash_tab_2"]["0-0-1700000000400-10"] = second
        self._write_stash(stash)
        before = self.stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        invalid_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_20"],
        })
        self.assertIn("err", invalid_preview)
        invalid_return_scope = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
            "sourceTab": ["stash_tab_1"],
        })
        self.assertIn("err", invalid_return_scope)
        invalid_post = editor.op_vault_bulk({
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "../stash_tab_1",
            "requestId": "bulk_invalid_source_012345678",
            "previewToken": "0" * 64,
        })
        self.assertIn("err", invalid_post)

        preview_tab_1 = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_1"],
        })
        mismatched = editor.op_vault_bulk({
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "stash_tab_2",
            "requestId": "bulk_mismatched_source_0123456",
            "previewToken": preview_tab_1["previewToken"],
        })
        self.assertEqual(mismatched.get("code"), "preview_stale")
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_bulk_selected_preview_expires_when_an_unselected_tab_changes(self):
        stash = self._full_bulk_stash()
        selected = json.loads(json.dumps(self.original_entry))
        selected["pos"] = [9.0, 9.0]
        stash["stash_tab_2"]["0-0-1700000000500-10"] = selected
        self._write_stash(stash)
        preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"],
            "collectionId": ["1"],
            "sourceTab": ["stash_tab_2"],
        })
        changed = self._read_stash()
        changed["stash_tab_1"][SOURCE_KEY]["data"]["a"] += 1.0
        self._write_stash(changed)
        before = self.stash_path.read_bytes()

        result = editor.op_vault_bulk({
            "direction": "stash-to-vault",
            "collectionId": 1,
            "sourceTab": "stash_tab_2",
            "requestId": "bulk_unselected_stale_01234567",
            "previewToken": preview["previewToken"],
        })
        self.assertEqual(result.get("code"), "preview_stale")
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_bulk_preview_token_rejects_stale_stash_without_backup_or_vault_mutation(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
        })
        self.assertTrue(preview["empty"])

        changed = self._read_stash()
        changed["stash_tab_1"][SOURCE_KEY] = self.original_entry
        self._write_stash(changed)
        before = self.stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        result = editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "requestId": "bulk_stale_preview_012345678",
            "previewToken": preview["previewToken"],
        })
        self.assertEqual(result.get("code"), "preview_stale")
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_bulk_ui_keeps_both_previewed_transfer_directions_available(self):
        html = editor.HTML
        self.assertIn("Shared Stash → Vault", html)
        self.assertIn("Vault → Shared Stash", html)
        self.assertIn('id="vaultdeskin"', html)
        self.assertIn('id="vaultdeskout"', html)
        self.assertIn("/api/vault/bulk-preview?", html)
        self.assertIn("/api/vault/bulk", html)
        self.assertIn("every available item in every collection", html)
        self.assertIn('id="vaultbulksource"', html)
        self.assertIn('id="vaultbulkdestination"', html)
        self.assertIn("params.set('sourceTab',source.value)", html)
        self.assertIn("params.set('destinationTab',destinationTab.value)", html)
        self.assertIn("body.sourceTab=preview.sourceTab||'all'", html)
        self.assertIn("body.destinationTab=preview.destinationTab||'auto'", html)
        self.assertIn("vaultBulkSessionKey(direction,sourceTab='all',collectionId='all',destinationTab='auto')", html)
        self.assertIn("sessionStorage.setItem(stableKey,stableId)", html)
        self.assertIn("RETRY SAME TRANSFER", html)
        self.assertIn("role=\"dialog\"", html)
        self.assertIn("/api/vault/batch/resolve", html)
        self.assertIn("PRESERVE VAULT OWNERSHIP", html)
        self.assertIn("data-health-vault-resolve", html)

        options = editor._bulk_deposit_source_options()
        self.assertEqual(options[0]["tab"], "all")
        self.assertEqual(
            [row["tab"] for row in options[1:20]],
            [f"stash_tab_{index}" for index in range(1, 20)],
        )
        self.assertEqual(
            [row["tab"] for row in options[-3:]],
            ["material_tab", "socket_tab", "unique_items"],
        )
        destinations = editor._bulk_withdrawal_destination_options()
        self.assertEqual(destinations[0]["tab"], "auto")
        self.assertEqual(
            [row["tab"] for row in destinations[1:20]],
            [f"stash_tab_{index}" for index in range(1, 20)],
        )
        self.assertEqual(
            [row["tab"] for row in destinations[-3:]],
            ["material_tab", "socket_tab", "unique_items"],
        )

    def test_bulk_withdraw_exact_numbered_destination_accepts_mixed_normal_and_unique(self):
        stash = self._full_bulk_stash()
        normal_key = "0-0-1700000000500-10"
        stash["stash_tab_1"][normal_key] = {
            "pos": [8.0, 7.0],
            "data": {
                "w": 1.0, "a": 991000.0, "j": 0.0,
                "b": 0.0, "c": 0.0, "o": 1.0,
            },
        }
        original_data = sorted(
            (entry["data"] for entry in stash["stash_tab_1"].values()),
            key=lambda data: (data["c"], data["b"]),
        )
        self._write_stash(stash)
        deposit_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["stash_tab_1"],
        })
        deposited = editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "stash_tab_1",
            "requestId": "bulk_exact_target_seed_01234567",
            "previewToken": deposit_preview["previewToken"],
        })
        self._assert_ok(deposited)

        preview = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
            "destinationTab": ["stash_tab_2"],
        })
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["destinationTab"], "stash_tab_2")
        self.assertEqual(preview["destinationLabel"], "Shared Stash Tab 2")
        self.assertEqual(
            preview["tabCounts"],
            [{"tab": "stash_tab_2", "label": "Shared Stash Tab 2", "count": 2}],
        )
        automatic = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"], "destinationTab": ["auto"],
        })
        self.assertNotEqual(preview["previewToken"], automatic["previewToken"])

        request = {
            "direction": "vault-to-stash",
            "destinationTab": "stash_tab_2",
            "requestId": "bulk_exact_target_tab_2_012345",
            "previewToken": preview["previewToken"],
        }
        returned = editor.op_vault_bulk(request)
        self._assert_ok(returned)
        after = self._read_stash()
        self.assertEqual(after["stash_tab_1"], {})
        self.assertEqual(len(after["stash_tab_2"]), 2)
        returned_entries = list(after["stash_tab_2"].values())
        self.assertEqual(
            sorted(
                (entry["data"] for entry in returned_entries),
                key=lambda data: (data["c"], data["b"]),
            ),
            original_data,
        )
        self.assertTrue(all("pos" in entry for entry in returned_entries))
        self.assertEqual(self._vault().count_items(status="available"), 0)

        before_retry = self.stash_path.read_bytes()
        retry = editor.op_vault_bulk(request)
        self._assert_ok(retry)
        self.assertEqual(self.stash_path.read_bytes(), before_retry)

    def test_bulk_withdraw_exact_destination_rejects_incompatible_items_read_only(self):
        stash = self._full_bulk_stash()
        stash["material_tab"]["0-0-1700000000600-14"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 991001.0, "j": 0.0, "b": 71.0, "c": 0.0, "o": 37.0},
        }
        stash["socket_tab"]["0-0-1700000000601-15"] = {
            "pos": [0.0, 0.0],
            "data": {"a": 991002.0, "j": 0.0, "b": 136.0, "c": 0.0, "o": 8.0},
        }
        self._write_stash(stash)
        deposit_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["all"],
        })
        deposited = editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "all", "requestId": "bulk_mixed_target_seed_0123456",
            "previewToken": deposit_preview["previewToken"],
        })
        self._assert_ok(deposited)
        stash_before = self.stash_path.read_bytes()
        vault_before = self._vault().count_items(status="available")
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        for destination in ("material_tab", "socket_tab", "unique_items"):
            with self.subTest(destination=destination):
                preview = editor.vault_bulk_preview({
                    "direction": ["vault-to-stash"],
                    "destinationTab": [destination],
                })
                self.assertIn("err", preview)
                self.assertEqual(self.stash_path.read_bytes(), stash_before)
                self.assertEqual(
                    self._vault().count_items(status="available"), vault_before
                )
                self.assertEqual(
                    set(self.saves.glob("stash.hss.guibak_*")), backups_before
                )

    def test_bulk_withdraw_destination_is_strictly_validated_and_preview_bound(self):
        stash = self._full_bulk_stash()
        stash["stash_tab_1"][SOURCE_KEY]["data"]["c"] = 0.0
        self._write_stash(stash)
        deposit_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["stash_tab_1"],
        })
        self._assert_ok(editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "stash_tab_1",
            "requestId": "bulk_bound_target_seed_0123456",
            "previewToken": deposit_preview["previewToken"],
        }))
        invalid = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
            "destinationTab": ["../stash_tab_1"],
        })
        self.assertIn("err", invalid)
        preview = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
            "destinationTab": ["stash_tab_2"],
        })
        before = self.stash_path.read_bytes()
        result = editor.op_vault_bulk({
            "direction": "vault-to-stash",
            "destinationTab": "stash_tab_3",
            "requestId": "bulk_bound_target_post_0123456",
            "previewToken": preview["previewToken"],
        })
        self.assertEqual(result.get("code"), "preview_stale")
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(self._vault().count_items(status="available"), 1)

    def test_bulk_withdraw_exact_special_destinations_accept_only_native_kind(self):
        stash = self._full_bulk_stash()
        stash["stash_tab_1"] = {}
        material_key = "0-0-1700000000700-14"
        stash["material_tab"][material_key] = {
            "pos": [4.0, 5.0],
            "data": {"a": 992001.0, "j": 0.0, "b": 71.0, "c": 0.0, "o": 37.0},
        }
        self._write_stash(stash)
        material_deposit = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["material_tab"],
        })
        self._assert_ok(editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "material_tab",
            "requestId": "bulk_material_target_seed_01234",
            "previewToken": material_deposit["previewToken"],
        }))
        material_return = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
            "destinationTab": ["material_tab"],
        })
        self.assertNotIn("err", material_return, material_return.get("err"))
        self.assertEqual(material_return["tabCounts"][0]["tab"], "material_tab")
        self._assert_ok(editor.op_vault_bulk({
            "direction": "vault-to-stash", "destinationTab": "material_tab",
            "requestId": "bulk_material_target_return_012",
            "previewToken": material_return["previewToken"],
        }))
        self.assertIn(material_key, self._read_stash()["material_tab"])

        stash = self._read_stash()
        unique_key = "0-0-1700000000701-10"
        unique = json.loads(json.dumps(self.original_entry))
        unique.pop("pos", None)
        stash["unique_items"][unique_key] = unique
        self._write_stash(stash)
        unique_deposit = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["unique_items"],
        })
        self._assert_ok(editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "unique_items",
            "requestId": "bulk_unique_target_seed_0123456",
            "previewToken": unique_deposit["previewToken"],
        }))
        unique_return = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
            "destinationTab": ["unique_items"],
        })
        self.assertNotIn("err", unique_return, unique_return.get("err"))
        self.assertEqual(unique_return["tabCounts"][0]["tab"], "unique_items")
        self._assert_ok(editor.op_vault_bulk({
            "direction": "vault-to-stash", "destinationTab": "unique_items",
            "requestId": "bulk_unique_target_return_01234",
            "previewToken": unique_return["previewToken"],
        }))
        returned_unique = self._read_stash()["unique_items"][unique_key]
        self.assertNotIn("pos", returned_unique)
        self.assertEqual(returned_unique, unique)

    def test_bulk_withdraw_exact_destination_capacity_failure_is_read_only(self):
        stash = self._full_bulk_stash()
        stash["stash_tab_1"][SOURCE_KEY]["data"]["c"] = 0.0
        self._write_stash(stash)
        deposit_preview = editor.vault_bulk_preview({
            "direction": ["stash-to-vault"], "collectionId": ["1"],
            "sourceTab": ["stash_tab_1"],
        })
        self._assert_ok(editor.op_vault_bulk({
            "direction": "stash-to-vault", "collectionId": 1,
            "sourceTab": "stash_tab_1",
            "requestId": "bulk_capacity_target_seed_01234",
            "previewToken": deposit_preview["previewToken"],
        }))
        before = self.stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        with patch.object(editor, "find_free_pos", return_value=None):
            preview = editor.vault_bulk_preview({
                "direction": ["vault-to-stash"],
                "destinationTab": ["stash_tab_2"],
            })
        self.assertIn("err", preview)
        self.assertIn("No safe Shared Stash space", preview["err"])
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        self.assertEqual(self._vault().count_items(status="available"), 1)

    def test_bulk_withdraw_preview_becomes_stale_when_custom_name_changes(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        raw = json.dumps(
            {"data": {"w": 1.0, "a": 123.0, "j": 0.0,
                      "b": 31.0, "c": 1.0, "m": 1.0}},
            separators=(",", ":"),
        )
        item = self._vault().deposit(
            "Vault", raw, source_item_key="0-0-1800000000000-10",
            source="unique_items",
        )
        preview = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
        })
        self.assertEqual(preview["customNamedCount"], 0)
        self._vault().set_item_custom_name(item.id, "After Preview")
        before = self.stash_path.read_bytes()
        result = editor.op_vault_bulk({
            "direction": "vault-to-stash",
            "requestId": "bulk_alias_race_0123456789",
            "previewToken": preview["previewToken"],
        })
        self.assertEqual(result.get("code"), "preview_stale")
        self.assertEqual(self.stash_path.read_bytes(), before)
        self.assertEqual(self._vault().get_item(item.id).custom_name, "After Preview")

    def test_bulk_capacity_failure_is_read_only_and_keeps_every_vault_item(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        raw = json.dumps(
            {"pos": [0.0, 0.0],
             "data": {"a": 123.0, "j": 0.0, "b": 36.0,
                      "c": 0.0, "o": 1.0}},
            separators=(",", ":"),
        )
        item = self._vault().deposit(
            "Vault", raw, source_item_key="0-0-1800000000001-12"
        )
        before_stash = self.stash_path.read_bytes()
        before_db_count = self._vault().count_items(status="available")
        with patch.object(editor, "find_free_pos", return_value=None):
            preview = editor.vault_bulk_preview({
                "direction": ["vault-to-stash"],
            })
        self.assertIn("err", preview)
        self.assertIn("nothing was moved", preview["err"])
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(self._vault().count_items(status="available"), before_db_count)
        self.assertEqual(self._vault().get_item(item.id).status, "available")

    def test_bulk_deposit_rejects_malformed_or_unreturnable_grid_records_read_only(self):
        cases = {
            "malformed native key": (
                "not-a-native-item-key",
                {
                    "pos": [0.0, 0.0],
                    "data": {"a": 101.0, "j": 0.0, "b": 0.0, "c": 0.0},
                },
                "unsupported native key",
            ),
            "unknown native grid address": (
                "0-0-1800000000100-12",
                {
                    "pos": [0.0, 0.0],
                    "data": {
                        "a": 102.0, "j": 0.0, "b": 999999.0,
                        "c": 0.0, "o": 1.0,
                    },
                },
                "proven Season 10 catalog",
            ),
        }
        for label, (key, entry, expected_error) in cases.items():
            with self.subTest(label):
                stash = self._full_bulk_stash()
                for tab in editor.BULK_STASH_ITEM_TABS:
                    stash[tab] = {}
                stash["stash_tab_1"][key] = entry
                self._write_stash(stash)
                before_stash = self.stash_path.read_bytes()
                backups_before = set(self.saves.glob("stash.hss.guibak_*"))
                items_before = self._vault().count_items(status="all")

                preview = editor.vault_bulk_preview({
                    "direction": ["stash-to-vault"], "collectionId": ["1"],
                })

                self.assertIn("err", preview)
                self.assertIn(expected_error, preview["err"])
                self.assertEqual(self.stash_path.read_bytes(), before_stash)
                self.assertEqual(
                    set(self.saves.glob("stash.hss.guibak_*")), backups_before
                )
                self.assertEqual(
                    self._vault().count_items(status="all"), items_before
                )

    def test_bulk_withdraw_rejects_raw_json_hash_mismatch_read_only(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        raw = json.dumps(
            {"data": {"a": 201.0, "j": 0.0, "b": 0.0, "c": 0.0}},
            separators=(",", ":"),
        )
        item = self._vault().deposit(
            "Vault", raw, source_item_key="0-0-1800000000200-5",
            source="stash_tab_1",
        )
        tampered = json.loads(raw)
        tampered["data"]["a"] = 999999.0
        tampered_raw = json.dumps(tampered, separators=(",", ":"))
        with closing(sqlite3.connect(self.vault_path)) as connection:
            connection.execute(
                "UPDATE items SET raw_json=? WHERE id=?", (tampered_raw, item.id)
            )
            connection.commit()
        before_stash = self.stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        with closing(sqlite3.connect(self.vault_path)) as connection:
            row_before = connection.execute(
                "SELECT raw_json, raw_sha256, status, reserved_token "
                "FROM items WHERE id=?", (item.id,),
            ).fetchone()

        preview = editor.vault_bulk_preview({"direction": ["vault-to-stash"]})

        self.assertIn("err", preview)
        self.assertIn("integrity hash", preview["err"])
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        with closing(sqlite3.connect(self.vault_path)) as connection:
            row_after = connection.execute(
                "SELECT raw_json, raw_sha256, status, reserved_token "
                "FROM items WHERE id=?", (item.id,),
            ).fetchone()
        self.assertEqual(row_after, row_before)

    def test_bulk_withdraw_rejects_tampered_raw_sha256_read_only(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        raw = json.dumps(
            {"data": {"a": 202.0, "j": 0.0, "b": 0.0, "c": 0.0}},
            separators=(",", ":"),
        )
        item = self._vault().deposit(
            "Vault", raw, source_item_key="0-0-1800000000201-5",
            source="stash_tab_1",
        )
        with closing(sqlite3.connect(self.vault_path)) as connection:
            connection.execute(
                "UPDATE items SET raw_sha256=? WHERE id=?", ("f" * 64, item.id)
            )
            connection.commit()
        before_stash = self.stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))
        with closing(sqlite3.connect(self.vault_path)) as connection:
            row_before = connection.execute(
                "SELECT raw_json, raw_sha256, status, reserved_token "
                "FROM items WHERE id=?", (item.id,),
            ).fetchone()

        preview = editor.vault_bulk_preview({"direction": ["vault-to-stash"]})

        self.assertIn("err", preview)
        self.assertIn("integrity hash", preview["err"])
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        with closing(sqlite3.connect(self.vault_path)) as connection:
            row_after = connection.execute(
                "SELECT raw_json, raw_sha256, status, reserved_token "
                "FROM items WHERE id=?", (item.id,),
            ).fetchone()
        self.assertEqual(row_after, row_before)

    def test_bulk_routing_preserves_scarce_normal_space_for_generic_item(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        material_raw = json.dumps(
            {"data": {"a": 301.0, "j": 0.0, "b": 19.0,
                      "c": 0.0, "o": 1.0}},
            separators=(",", ":"),
        )
        generic_raw = json.dumps(
            {"data": {"a": 302.0, "j": 0.0, "b": 0.0, "c": 0.0}},
            separators=(",", ":"),
        )
        self._vault().deposit(
            "Vault", material_raw,
            source_item_key="0-0-1800000000300-13",
            source="stash_tab_1",
        )
        self._vault().deposit(
            "Vault", generic_raw,
            source_item_key="0-0-1800000000301-5",
        )
        normal_slot_taken = False

        def scarce_capacity(_items, tab, _width, _height):
            nonlocal normal_slot_taken
            if tab == "material_tab":
                return [0.0, 0.0]
            if tab.startswith("stash_tab_") and not normal_slot_taken:
                normal_slot_taken = True
                return [0.0, 0.0]
            return None

        with patch.object(editor, "find_free_pos", side_effect=scarce_capacity):
            preview = editor.vault_bulk_preview({
                "direction": ["vault-to-stash"],
            })

        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["itemCount"], 2)
        self.assertEqual(
            {row["tab"]: row["count"] for row in preview["tabCounts"]},
            {"material_tab": 1, "stash_tab_1": 1},
        )

    def test_bulk_withdraw_prefers_free_exact_origin_position(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        expected = {
            "pos": [12.0, 9.0],
            "data": {"a": 401.0, "j": 0.0, "b": 0.0, "c": 0.0},
        }
        raw = json.dumps(expected, separators=(",", ":"))
        self._vault().deposit(
            "Vault", raw, source_item_key="0-0-1800000000400-5",
            source="stash_tab_7",
        )
        preview = editor.vault_bulk_preview({"direction": ["vault-to-stash"]})
        self.assertNotIn("err", preview, preview.get("err"))
        returned = editor.op_vault_bulk({
            "direction": "vault-to-stash",
            "requestId": "bulk_exact_origin_01234567890",
            "previewToken": preview["previewToken"],
        })

        self._assert_ok(returned)
        self.assertEqual(
            self._read_stash()["stash_tab_7"],
            {"0-0-1800000000400-5": expected},
        )
        self.assertEqual(self._vault().count_items(status="all"), 0)

    def test_bulk_withdraw_rejects_decoded_hss_size_limit_read_only(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        raw = json.dumps(
            {
                "data": {"a": 501.0, "j": 0.0, "b": 0.0, "c": 0.0},
                "future_padding": "x" * 8192,
            },
            separators=(",", ":"),
        )
        item = self._vault().deposit(
            "Vault", raw, source_item_key="0-0-1800000000500-5",
            source="stash_tab_1",
        )
        before_stash = self.stash_path.read_bytes()
        backups_before = set(self.saves.glob("stash.hss.guibak_*"))

        with patch.object(editor, "MAX_HSS_DECODED_BYTES", 4096):
            preview = editor.vault_bulk_preview({
                "direction": ["vault-to-stash"],
            })

        self.assertIn("err", preview)
        self.assertIn("decoded HSS size limit", preview["err"])
        self.assertEqual(self.stash_path.read_bytes(), before_stash)
        self.assertEqual(set(self.saves.glob("stash.hss.guibak_*")), backups_before)
        with closing(sqlite3.connect(self.vault_path)) as connection:
            state = connection.execute(
                "SELECT status, reserved_token FROM items WHERE id=?", (item.id,),
            ).fetchone()
        self.assertEqual(state, ("available", None))

    def test_bulk_recovery_commits_whole_deposit_after_atomic_stash_write(self):
        stash = self._full_bulk_stash()
        self._write_stash(stash)
        plan = editor._bulk_plan_locked("stash-to-vault", 1)
        batch = self._vault().prepare_bulk_deposit(
            1, plan["entries"], request_id="bulk_powerloss_012345678901",
            request_hash=plan["intentHash"],
            stash_before_sha256=plan["stashSha256"],
            stash_after_sha256=hashlib.sha256(
                plan["encodedAfter"].encode("ascii")
            ).hexdigest(),
        )
        self.assertEqual(batch.status, "prepared")
        self.stash_path.write_text(plan["encodedAfter"], encoding="ascii")
        recovered = editor.reconcile_vault_transfers()
        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(
            self._vault().get_transfer_batch(batch.request_id).status, "committed"
        )
        self.assertEqual(self._vault().count_items(status="available"), 1)
        self.assertTrue(all(
            not self._read_stash()[tab] for tab in editor.BULK_STASH_ITEM_TABS
        ))

    def test_unreadable_stash_preserves_pending_deposit_copies_in_vault(self):
        self._write_stash(self._full_bulk_stash())
        plan = editor._bulk_plan_locked("stash-to-vault", 1)
        batch = self._vault().prepare_bulk_deposit(
            1, plan["entries"], request_id="bulk_unreadable_deposit_0001",
            request_hash=plan["intentHash"],
            stash_before_sha256=plan["stashSha256"],
            stash_after_sha256=hashlib.sha256(
                plan["encodedAfter"].encode("ascii")
            ).hexdigest(),
        )
        corrupt = b"not-a-valid-hss-payload!"
        self.stash_path.write_bytes(corrupt)

        recovered = editor.reconcile_vault_transfers()

        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(recovered["conflicts"], 0)
        self.assertEqual(len(recovered["warnings"]), 1)
        warning = recovered["warnings"][0]
        self.assertTrue(warning["possibleDuplicate"])
        self.assertEqual(
            warning["code"], "vault_ownership_preserved_possible_duplicate"
        )
        self.assertEqual(
            self._vault().get_transfer_batch(batch.request_id).status, "committed"
        )
        self.assertEqual(self._vault().count_items(status="available"), 1)
        self.assertEqual(self.stash_path.read_bytes(), corrupt)

    def test_unreadable_stash_returns_pending_withdrawal_items_to_vault(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        raw = json.dumps(self.original_entry, separators=(",", ":"))
        item = self._vault().deposit(
            "Vault", raw, source_item_key=SOURCE_KEY,
            source="stash_tab_1",
        )
        plan = editor._bulk_plan_locked("vault-to-stash", None)
        batch = self._vault().prepare_bulk_withdrawal(
            plan["targets"], request_id="bulk_unreadable_withdraw_001",
            request_hash=plan["intentHash"],
            stash_before_sha256=plan["stashSha256"],
            stash_after_sha256=hashlib.sha256(
                plan["encodedAfter"].encode("ascii")
            ).hexdigest(),
        )
        corrupt = b"not-a-valid-hss-payload!"
        self.stash_path.write_bytes(corrupt)

        recovered = editor.reconcile_vault_transfers()

        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(len(recovered["warnings"]), 1)
        self.assertTrue(recovered["warnings"][0]["possibleDuplicate"])
        self.assertEqual(
            self._vault().get_transfer_batch(batch.request_id).status, "cancelled"
        )
        self.assertEqual(self._vault().get_item(item.id).status, "available")
        self.assertEqual(self.stash_path.read_bytes(), corrupt)

    def test_unreadable_stash_preserves_legacy_single_deposit_copy_in_vault(self):
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
            1, raw_item_json, request_id=DEPOSIT_REQUEST,
            request_hash=request_hash, source_tab="stash_tab_1",
            source_key=SOURCE_KEY, stash_before_sha256=before_hash,
            stash_after_sha256=after_hash, label="Legacy pending deposit",
        )
        self.assertEqual(prepared.status, "prepared")
        corrupt = b"not-a-valid-hss-payload!"
        self.stash_path.write_bytes(corrupt)

        recovered = editor.reconcile_vault_transfers()

        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(recovered["conflicts"], 0)
        self.assertEqual(len(recovered["warnings"]), 1)
        self.assertEqual(recovered["warnings"][0]["itemCount"], 1)
        self.assertTrue(recovered["warnings"][0]["possibleDuplicate"])
        self.assertEqual(store.get_transfer(DEPOSIT_REQUEST).status, "committed")
        self.assertEqual(store.count_items(status="available"), 1)
        self.assertEqual(self.stash_path.read_bytes(), corrupt)

    def test_unreadable_stash_releases_legacy_single_withdrawal_to_vault(self):
        stash = self._read_stash()
        stash["stash_tab_1"] = {}
        self._write_stash(stash)
        store = self._vault()
        raw = json.dumps(self.original_entry, separators=(",", ":"))
        item = store.deposit(
            "Vault", raw, source_item_key=SOURCE_KEY, source="stash_tab_1"
        )
        before_hash = editor._file_sha256(self.stash_path)
        intended_after = self._read_stash()
        intended_after["stash_tab_1"][SOURCE_KEY] = self.original_entry
        encoded_after = editor._encoded_stash_document(intended_after)
        after_hash = hashlib.sha256(encoded_after.encode("ascii")).hexdigest()
        request_hash = editor.canonical_request_hash({
            "direction": "withdrawal",
            "itemId": item.id,
            "target": {"type": "stash", "tab": "stash_tab_1"},
        })
        prepared = store.prepare_withdrawal(
            item.id, request_id=WITHDRAW_REQUEST,
            request_hash=request_hash, target_tab="stash_tab_1",
            target_key=SOURCE_KEY, target_pos=(4, 7),
            stash_before_sha256=before_hash,
            stash_after_sha256=after_hash,
        )
        self.assertEqual(prepared.status, "prepared")
        corrupt = b"not-a-valid-hss-payload!"
        self.stash_path.write_bytes(corrupt)

        recovered = editor.reconcile_vault_transfers()

        self.assertEqual(recovered["pending"], 0)
        self.assertEqual(recovered["conflicts"], 0)
        self.assertEqual(len(recovered["warnings"]), 1)
        self.assertEqual(recovered["warnings"][0]["itemCount"], 1)
        self.assertTrue(recovered["warnings"][0]["possibleDuplicate"])
        self.assertEqual(store.get_transfer(WITHDRAW_REQUEST).status, "cancelled")
        self.assertEqual(store.get_item(item.id).status, "available")
        self.assertEqual(self.stash_path.read_bytes(), corrupt)

    def test_explicit_deposit_conflict_resolution_keeps_stash_and_vault_copies(self):
        stash = self._full_bulk_stash()
        second_key = "0-0-1700000000001-10"
        second = json.loads(json.dumps(self.original_entry))
        second["pos"] = [8.0, 7.0]
        second["data"]["a"] = 429566.0
        stash["stash_tab_1"][second_key] = second
        self._write_stash(stash)
        plan = editor._bulk_plan_locked("stash-to-vault", 1)
        batch = self._vault().prepare_bulk_deposit(
            1, plan["entries"], request_id="bulk_mixed_deposit_resolve_01",
            request_hash=plan["intentHash"],
            stash_before_sha256=plan["stashSha256"],
            stash_after_sha256=hashlib.sha256(
                plan["encodedAfter"].encode("ascii")
            ).hexdigest(),
        )
        mixed = self._read_stash()
        mixed["stash_tab_1"].pop(SOURCE_KEY)
        self._write_stash(mixed)
        mixed_bytes = self.stash_path.read_bytes()
        recovery = editor.reconcile_vault_transfers()
        self.assertEqual(recovery["conflicts"], 1)

        resolved = editor.op_vault_resolve_batch({
            "action": "preserve-vault-ownership",
            "requestId": batch.request_id,
        })

        self._assert_ok(resolved)
        self.assertTrue(resolved["possibleDuplicate"])
        self.assertIn("duplicates may exist", resolved["warning"])
        self.assertEqual(
            self._vault().get_transfer_batch(batch.request_id).status, "committed"
        )
        self.assertEqual(self._vault().count_items(status="available"), 2)
        self.assertEqual(self.stash_path.read_bytes(), mixed_bytes)
        self.assertIn(second_key, self._read_stash()["stash_tab_1"])

    def test_explicit_withdrawal_conflict_resolution_releases_every_reservation(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        first = json.loads(json.dumps(self.original_entry))
        second = json.loads(json.dumps(self.original_entry))
        second["pos"] = [8.0, 7.0]
        second["data"]["a"] = 429566.0
        records = [
            self._vault().deposit(
                "Vault", json.dumps(entry, separators=(",", ":")),
                source_item_key=key, source="stash_tab_1",
            )
            for entry, key in (
                (first, SOURCE_KEY),
                (second, "0-0-1700000000001-10"),
            )
        ]
        plan = editor._bulk_plan_locked("vault-to-stash", None)
        batch = self._vault().prepare_bulk_withdrawal(
            plan["targets"], request_id="bulk_mixed_withdraw_resolve_1",
            request_hash=plan["intentHash"],
            stash_before_sha256=plan["stashSha256"],
            stash_after_sha256=hashlib.sha256(
                plan["encodedAfter"].encode("ascii")
            ).hexdigest(),
        )
        mixed = json.loads(json.dumps(plan["work"]))
        omitted = plan["targets"][1]
        mixed[omitted["target_tab"]].pop(omitted["target_key"])
        self._write_stash(mixed)
        mixed_bytes = self.stash_path.read_bytes()
        recovery = editor.reconcile_vault_transfers()
        self.assertEqual(recovery["conflicts"], 1)

        resolved = editor.op_vault_resolve_batch({
            "action": "preserve-vault-ownership",
            "requestId": batch.request_id,
        })

        self._assert_ok(resolved)
        self.assertTrue(resolved["possibleDuplicate"])
        self.assertEqual(
            self._vault().get_transfer_batch(batch.request_id).status, "cancelled"
        )
        self.assertEqual(self._vault().count_items(status="available"), 2)
        self.assertTrue(all(
            self._vault().get_item(record.id).status == "available"
            for record in records
        ))
        self.assertEqual(self.stash_path.read_bytes(), mixed_bytes)

    def test_bulk_return_all_is_not_limited_to_first_five_hundred_items(self):
        stash = self._full_bulk_stash()
        for tab in editor.BULK_STASH_ITEM_TABS:
            stash[tab] = {}
        self._write_stash(stash)
        entries = []
        for index in range(501):
            raw = json.dumps(
                {"data": {"w": 1.0, "a": float(index + 1), "j": 0.0,
                          "b": 31.0, "c": 1.0, "m": 1.0}},
                separators=(",", ":"),
            )
            entries.append({
                "raw_item_json": raw,
                "source_tab": "unique_items",
                "source_key": f"0-0-{1900000000000 + index}-10",
                "label": f"Unique {index}",
                "source": "unique_items",
            })
        intent = editor.canonical_request_hash({
            "direction": "bulk_deposit", "collectionId": 1,
            "items": [{
                "sourceTab": row["source_tab"],
                "sourceKey": row["source_key"],
                "rawSha256": hashlib.sha256(
                    row["raw_item_json"].encode("utf-8")
                ).hexdigest(),
            } for row in entries],
        })
        seeded = self._vault().prepare_bulk_deposit(
            1, entries, request_id="bulk_seed_501_items_01234567",
            request_hash=intent, stash_before_sha256="a" * 64,
            stash_after_sha256="b" * 64,
        )
        self._vault().commit_transfer_batch(seeded.request_id, "b" * 64)
        preview = editor.vault_bulk_preview({
            "direction": ["vault-to-stash"],
        })
        self.assertNotIn("err", preview, preview.get("err"))
        self.assertEqual(preview["itemCount"], 501)
        returned = editor.op_vault_bulk({
            "direction": "vault-to-stash",
            "requestId": "bulk_return_501_items_012345",
            "previewToken": preview["previewToken"],
        })
        self._assert_ok(returned)
        self.assertEqual(len(self._read_stash()["unique_items"]), 501)
        self.assertEqual(self._vault().count_items(status="all"), 0)


if __name__ == "__main__":
    unittest.main()
