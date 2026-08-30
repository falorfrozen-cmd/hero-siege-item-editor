import base64
import hashlib
import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_gui_recovery_tests", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


UNIQUE = '{"tab":-5.0,"name":"Unique"}'
EMPTY = '{"tab":-5.0,"name":""}'


def fixture_document(item_count: int = 2) -> dict:
    document = {f"stash_tab_{index}": {} for index in range(1, 20)}
    tab_data = {
        namespace: [{"tab": 0.0, "name": "Personal"}]
        for namespace in ("NH", "LocalNH", "SH", "BP", "LocalNS", "SS", "NS", "Odyssey")
    }
    tab_data["LocalNS"].append({"tab": -5.0, "name": "Unique"})
    document.update({
        "material_tab": {},
        "socket_tab": {},
        "unique_items": {},
        "stash_reset": 0.0,
        "stash_tab_data": tab_data,
    })
    for index in range(item_count):
        document["unique_items"][f"0-0-{1000 + index}-7"] = {
            "data": {"b": float(index + 1), "a": float(index + 10)},
        }
    return document


def fixture_text(item_count: int = 2) -> str:
    return json.dumps(fixture_document(item_count), separators=(",", ":"))


def _pack_decoded(decoded: bytes, *, outer_nul: bool = False) -> bytes:
    xored = bytes(
        value ^ editor.XOR_KEY[index % len(editor.XOR_KEY)]
        for index, value in enumerate(decoded)
    )
    raw = base64.b64encode(zlib.compress(xored, 6))
    return raw + (b"\x00" if outer_nul else b"")


def corrupt_blank_profile_hss(item_count: int = 2) -> bytes:
    text = fixture_text(item_count).replace(UNIQUE, EMPTY, 1)
    narrow = text.encode("latin-1")
    wide = bytearray(len(narrow) * 2)
    wide[::2] = narrow
    fragment_start = text.index(EMPTY)
    closing_quote = fragment_start + EMPTY.rfind('"')
    wide[closing_quote * 2 + 1] = 0x08
    wide += b"\x00\x00\xff\xff"
    return _pack_decoded(bytes(wide), outer_nul=True)


class _PendingVault:
    def list_pending_transfers(self):
        return [object()]


class HSSRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saves = Path(self.temp.name)
        self.old_saves = editor.SAVES
        self.old_vault_db_file = editor.VAULT_DB_FILE
        self.old_vault_store = editor._VAULT_STORE
        self.old_vault_store_path = editor._VAULT_STORE_PATH
        editor.SAVES = self.saves
        editor.VAULT_DB_FILE = self.saves / "hs_infinite_vault.sqlite3"
        editor._VAULT_STORE = None
        editor._VAULT_STORE_PATH = None
        self.game_patch = patch.object(editor, "game_running", return_value=False)
        self.game_patch.start()

    def tearDown(self):
        self.game_patch.stop()
        editor.SAVES = self.old_saves
        editor.VAULT_DB_FILE = self.old_vault_db_file
        editor._VAULT_STORE = self.old_vault_store
        editor._VAULT_STORE_PATH = self.old_vault_store_path
        self.temp.cleanup()

    def _write_corrupt_stash(self, item_count: int = 2) -> tuple[Path, bytes, str]:
        path = self.saves / "stash.hss"
        raw = corrupt_blank_profile_hss(item_count)
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest().upper()
        return path, raw, digest

    def _backup_paths(self) -> list[Path]:
        return list(self.saves.glob("stash.hss.pre_recovery_*"))

    def test_recovery_happy_path_creates_exact_backup_and_preserves_manifest(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash(item_count=3)
        preview, preview_issues = editor.inspect_stash_hss_recovery(path)

        self.assertEqual(preview.status, "recoverable")
        self.assertFalse(any(issue["severity"] == "error" for issue in preview_issues))
        result = editor.op_recover_stash_hss({
            "file": "stash.hss", "expectedSha256": source_sha256,
        })

        self.assertNotIn("err", result)
        self.assertEqual(result["sourceSha256"], source_sha256)
        self.assertEqual(result["itemRecordsPreserved"], 3)
        self.assertEqual(result["itemManifestSha256"], preview.item_manifest_sha256)
        backup_path = self.saves / result["backup"]
        self.assertEqual(backup_path.read_bytes(), source_raw)
        self.assertEqual(hashlib.sha256(backup_path.read_bytes()).hexdigest().upper(), source_sha256)
        self.assertEqual(self._backup_paths(), [backup_path])

        written = path.read_bytes()
        self.assertNotEqual(written, source_raw)
        verified = editor.analyze_stash_hss(written, editor.XOR_KEY)
        self.assertEqual(verified.status, "healthy")
        self.assertEqual(verified.item_count, 3)
        self.assertEqual(verified.item_manifest_sha256, preview.item_manifest_sha256)

    def test_stale_preview_sha_refuses_without_backup_or_mutation(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash()
        stale_sha256 = ("0" if source_sha256[0] != "0" else "1") + source_sha256[1:]

        result = editor.op_recover_stash_hss({
            "file": "stash.hss", "expectedSha256": stale_sha256,
        })

        self.assertIn("changed after the recovery preview", result["err"])
        self.assertEqual(path.read_bytes(), source_raw)
        self.assertEqual(self._backup_paths(), [])

    def test_running_game_refuses_without_backup_or_mutation(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash()

        with patch.object(editor, "game_running", return_value=True):
            result = editor.op_recover_stash_hss({
                "file": "stash.hss", "expectedSha256": source_sha256,
            })

        self.assertIn("Hero Siege is running", result["err"])
        self.assertEqual(path.read_bytes(), source_raw)
        self.assertEqual(self._backup_paths(), [])

    def test_pending_vault_transfer_refuses_without_backup_or_mutation(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash()
        Path(editor.VAULT_DB_FILE).touch()

        with patch.object(editor, "vault_store", return_value=_PendingVault()):
            result = editor.op_recover_stash_hss({
                "file": "stash.hss", "expectedSha256": source_sha256,
            })

        self.assertIn("Infinite Vault transfer is pending", result["err"])
        self.assertEqual(path.read_bytes(), source_raw)
        self.assertEqual(self._backup_paths(), [])

    def test_post_replace_verification_failure_restores_exact_source(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash(item_count=3)
        original_verify = editor._verify_recovery_output
        calls = 0

        def fail_only_after_replace(raw, plan):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise editor.HSSRecoveryError("injected final verification failure")
            return original_verify(raw, plan)

        with patch.object(editor, "_verify_recovery_output", side_effect=fail_only_after_replace):
            result = editor.op_recover_stash_hss({
                "file": "stash.hss", "expectedSha256": source_sha256,
            })

        self.assertTrue(result.get("rolledBack"), result)
        self.assertIn("original stash.hss was restored", result["err"])
        self.assertEqual(path.read_bytes(), source_raw)
        backup_path = self.saves / result["backup"]
        self.assertEqual(backup_path.read_bytes(), source_raw)
        self.assertEqual(self._backup_paths(), [backup_path])

    def test_rollback_refuses_to_overwrite_a_newer_external_write(self):
        path, _, source_sha256 = self._write_corrupt_stash(item_count=3)
        original_verify = editor._verify_recovery_output
        external_raw = b"external-writer-state"
        calls = 0

        def replace_then_fail(raw, plan):
            nonlocal calls
            calls += 1
            if calls == 3:
                path.write_bytes(external_raw)
                raise editor.HSSRecoveryError("injected external writer race")
            return original_verify(raw, plan)

        with patch.object(editor, "_verify_recovery_output", side_effect=replace_then_fail):
            result = editor.op_recover_stash_hss({
                "file": "stash.hss", "expectedSha256": source_sha256,
            })

        self.assertFalse(result.get("rolledBack"), result)
        self.assertIn("rollback also failed", result["err"])
        self.assertIn("avoid overwriting a newer external write", result["err"])
        self.assertEqual(path.read_bytes(), external_raw)
        self.assertEqual(len(self._backup_paths()), 1)

    def test_recovery_source_backup_is_listed_and_can_be_restored_explicitly(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash(item_count=2)
        recovered = editor.op_recover_stash_hss({
            "file": "stash.hss", "expectedSha256": source_sha256,
        })
        self.assertNotIn("err", recovered)

        listed = editor.list_backups()
        source_rows = [row for row in listed if row["file"] == recovered["backup"]]
        self.assertEqual(len(source_rows), 1)
        self.assertEqual(source_rows[0]["kind"], "pre_recovery")

        restored = editor.op_restore_backup({"file": recovered["backup"]})
        self.assertNotIn("err", restored)
        self.assertEqual(path.read_bytes(), source_raw)

    def test_backup_restore_rejects_path_traversal_and_fake_recovery_target(self):
        outside = self.saves.parent / "outside.hss.guibak_20260831_010101"
        outside.write_bytes(b"outside")
        fake = self.saves / "herosiege1.hss.pre_recovery_20260831_010101_123456"
        fake.write_bytes(b"fake")

        traversal = editor.op_restore_backup({"file": "../" + outside.name})
        fake_result = editor.op_restore_backup({"file": fake.name})

        self.assertEqual(traversal, {"err": "backup not found"})
        self.assertEqual(fake_result, {"err": "backup not found"})
        self.assertEqual(editor.list_backups(), [])

    def test_health_preview_is_read_only_and_reports_exact_recovery(self):
        path, source_raw, source_sha256 = self._write_corrupt_stash(item_count=4)

        report = editor.scan_save_health(apply=False)

        self.assertEqual(path.read_bytes(), source_raw)
        self.assertEqual(self._backup_paths(), [])
        self.assertEqual(report["summary"]["items"], 4)
        self.assertEqual(len(report["recoveries"]), 1)
        recovery = report["recoveries"][0]
        self.assertEqual(recovery["file"], "stash.hss")
        self.assertEqual(recovery["status"], "recoverable")
        self.assertEqual(recovery["sourceSha256"], source_sha256)
        self.assertEqual(recovery["itemRecords"], 4)
        self.assertTrue(recovery["canApply"])
        matching = [
            issue for issue in report["issues"]
            if issue["code"] == "file_decode_recoverable"
        ]
        self.assertEqual(len(matching), 1)
        self.assertFalse(matching[0]["fixable"])

    def test_successful_request_retry_is_idempotent_and_makes_no_second_backup(self):
        path, _, source_sha256 = self._write_corrupt_stash()
        request = {"file": "stash.hss", "expectedSha256": source_sha256}

        first = editor.op_recover_stash_hss(request)
        self.assertNotIn("err", first)
        recovered_raw = path.read_bytes()
        backups_after_first = self._backup_paths()
        second = editor.op_recover_stash_hss(request)

        self.assertIn("changed after the recovery preview", second["err"])
        self.assertEqual(path.read_bytes(), recovered_raw)
        self.assertEqual(self._backup_paths(), backups_after_first)
        plan, issues = editor.inspect_stash_hss_recovery(path)
        self.assertEqual(plan.status, "healthy")
        self.assertEqual(issues, [])

    def test_http_recovery_route_applies_the_hash_bound_preview(self):
        path, _, source_sha256 = self._write_corrupt_stash(item_count=1)
        server = editor.ThreadingHTTPServer(("127.0.0.1", 0), editor.H)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({
                "file": "stash.hss", "expectedSha256": source_sha256,
            }).encode()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request(
                "POST", "/api/health/recover", body=body,
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Content-Type": "application/json",
                    editor.EDITOR_REQUEST_HEADER: "1",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertNotIn("err", payload)
        self.assertEqual(payload["sourceSha256"], source_sha256)
        self.assertEqual(editor.analyze_stash_hss(path.read_bytes(), editor.XOR_KEY).status,
                         "healthy")

    def test_embedded_ui_exposes_preview_confirmation_and_recovery_endpoint(self):
        self.assertIn(".recovery-card", editor.HTML)
        self.assertIn("RECOVER ${esc((recovery.file||'stash.hss').toUpperCase())}",
                      editor.HTML)
        self.assertIn("itemRecords||0", editor.HTML)
        self.assertIn("expectedSha256:recovery.sourceSha256", editor.HTML)
        self.assertIn("/api/health/recover", editor.HTML)
        self.assertIn("OPEN SAVE HEALTH CHECK", editor.HTML)


if __name__ == "__main__":
    unittest.main()
