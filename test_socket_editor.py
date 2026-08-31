import base64
import importlib.util
import json
import math
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_socket_tests", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


class SocketEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.saves = Path(self.temp.name)
        self.old_saves = editor.SAVES
        editor.SAVES = self.saves
        self.game_patch = patch.object(editor, "game_running", return_value=False)
        self.game_patch.start()

        self.key = "0-0-1234567890123-3"
        self.data = {
            "w": 1.0,
            "a": 4677950.0,
            "j": 13.0,
            "b": 2.0,
            "c": 1.0,
            "m": 1.0,
        }
        self.stash = {
            "stash_tab_1": {},
            "unique_items": {self.key: {"data": self.data}},
            "stash_tab_data": {},
        }
        self._write_stash()
        self.target = {"type": "stash", "tab": "unique_items"}

    def tearDown(self):
        self.game_patch.stop()
        editor.SAVES = self.old_saves
        self.temp.cleanup()

    def _write_stash(self):
        (self.saves / "stash.hss").write_text(
            editor.encode_hss(json.dumps(self.stash)), encoding="ascii"
        )

    def _read_data(self):
        stash = json.loads(editor.decode_hss(self.saves / "stash.hss"))
        return stash["unique_items"][self.key]["data"]

    def _socket(self, payload):
        return editor.op_sockets({
            "target": self.target,
            "key": self.key,
            "sockets": payload,
        })

    def test_poison_ivy_uses_catalog_maximum_of_four(self):
        row = next(row for row in editor.CAT if row.get("key") == "w_bow_poison_ivy")
        self.assertEqual(editor.catalog_socket_limit(row), 4)
        self.assertEqual(editor.resolve(self.key, self.data)["socketLimit"], 4)

        result = self._socket([None, None, None, None])

        self.assertIn("ok", result)
        self.assertEqual(result["socketCount"], 4)
        self.assertEqual(result["maxSockets"], 4)
        self.assertEqual(self._read_data()["zz"]["sockets"], 4.0)

    def test_too_many_sockets_is_atomic_and_creates_no_backup(self):
        before = (self.saves / "stash.hss").read_bytes()

        result = self._socket([None] * 5)

        self.assertIn("maximum 4 sockets", result["err"])
        self.assertEqual((self.saves / "stash.hss").read_bytes(), before)
        self.assertEqual(list(self.saves.glob("stash.hss.guibak_*")), [])

    def test_missing_or_null_socket_list_cannot_clear_an_item(self):
        payload = base64.b64encode(json.dumps({
            "a": 456789,
            "b": 17,
            "n": 1,
        }, separators=(",", ":")).encode()).decode()
        self.data.update({"s1": payload, "zz": {"sockets": 1.0}})
        self._write_stash()
        before = (self.saves / "stash.hss").read_bytes()
        base_request = {"target": self.target, "key": self.key}

        for request in (base_request, {**base_request, "sockets": None}):
            with self.subTest(request=request):
                result = editor.op_sockets(request)
                self.assertIn("explicit list", result["err"])
                self.assertEqual((self.saves / "stash.hss").read_bytes(), before)
        self.assertEqual(list(self.saves.glob("stash.hss.guibak_*")), [])

        cleared = self._socket([])
        self.assertIn("ok", cleared)
        updated = self._read_data()
        self.assertNotIn("s1", updated)
        self.assertEqual(updated["zz"]["sockets"], 0.0)

    def test_known_item_without_socket_capacity_is_not_editable(self):
        # Leviathan's Crown has a binary-proven native 2-4 socket range even
        # though the compact catalog omits its display line.  Use a genuinely
        # socketless verified definition for this negative regression.
        row = next(row for row in editor.CAT if row.get("key") == "helmet_colossal_avenger")
        key = "0-0-1234567890999-0"
        data = {
            "w": 1.0,
            "a": 271828.0,
            "j": 0.0,
            "b": 1.0,
            "c": 1.0,
            "m": 1.0,
        }
        self.stash["unique_items"] = {key: {"data": data}}
        self._write_stash()
        before = (self.saves / "stash.hss").read_bytes()

        self.assertEqual(editor.catalog_socket_limit(row), 0)
        self.assertEqual(editor.resolve(key, data)["socketLimit"], 0)
        result = editor.op_sockets({
            "target": self.target,
            "key": key,
            "sockets": [None],
        })

        self.assertIn("maximum 0 sockets", result["err"])
        self.assertEqual((self.saves / "stash.hss").read_bytes(), before)
        self.assertEqual(list(self.saves.glob("stash.hss.guibak_*")), [])

    def test_existing_socket_payload_is_preserved_byte_for_byte(self):
        payload = base64.b64encode(json.dumps({
            "a": 123456,
            "b": 17,
            "n": 3,
            "future": {"opaque": True},
        }, separators=(",", ":")).encode()).decode()
        self.data.update({
            "s1": payload,
            "zz": {"sockets": 2.0, "future": "keep"},
            "unset": 1.0,
        })
        self._write_stash()

        result = self._socket([{"keepEncoded": payload}, None])

        self.assertIn("ok", result)
        updated = self._read_data()
        self.assertEqual(updated["s1"], payload)
        self.assertEqual(updated["zz"], {"sockets": 2.0, "future": "keep"})
        self.assertNotIn("unset", updated)

    def test_existing_payload_cannot_be_copied_more_times_than_it_exists(self):
        payload = base64.b64encode(json.dumps({
            "a": 654321,
            "b": 17,
            "n": 2,
        }, separators=(",", ":")).encode()).decode()
        self.data.update({"s1": payload, "zz": {"sockets": 1.0}})
        self._write_stash()
        before = (self.saves / "stash.hss").read_bytes()

        result = self._socket([
            {"keepEncoded": payload},
            {"keepEncoded": payload},
        ])

        self.assertIn("err", result)
        self.assertEqual((self.saves / "stash.hss").read_bytes(), before)
        self.assertEqual(list(self.saves.glob("stash.hss.guibak_*")), [])

    def test_unknown_keep_marker_and_nonfinite_id_fail_closed(self):
        before = (self.saves / "stash.hss").read_bytes()
        for payload in (
            [{"keepEncoded": "not-from-this-item"}],
            [{"b": math.nan}],
            [{"b": math.inf}],
            [{"b": True}],
            [{"b": 999999}],
        ):
            with self.subTest(payload=payload):
                result = self._socket(payload)
                self.assertIn("err", result)
                self.assertEqual((self.saves / "stash.hss").read_bytes(), before)
        self.assertEqual(list(self.saves.glob("stash.hss.guibak_*")), [])

    def test_malformed_existing_socket_requires_health_check_and_never_writes(self):
        malformed_values = (
            None,
            "",
            123,
            "not-base64",
            base64.b64encode(b"[]").decode(),
            base64.b64encode(b'{"a":1}').decode(),
        )
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                self.data["s1"] = malformed
                self.data["zz"] = {"sockets": 1.0}
                self._write_stash()
                before = (self.saves / "stash.hss").read_bytes()

                result = self._socket([])

                self.assertIn("Save Health Check", result["err"])
                self.assertEqual((self.saves / "stash.hss").read_bytes(), before)
                report = editor.scan_save_health()
                self.assertTrue(any(
                    issue.get("code") == "socket_payload"
                    for issue in report.get("issues", [])
                ))
        self.assertEqual(list(self.saves.glob("stash.hss.guibak_*")), [])

    def test_new_rune_payload_and_declared_empty_count_are_saved(self):
        rune = next(
            row for row in editor.CAT
            if row.get("kind") == "normal"
            and row.get("available", True)
            and row.get("cls") == 15
        )

        result = self._socket([None, {"b": rune["b"]}, None, None])

        self.assertIn("ok", result)
        updated = self._read_data()
        self.assertEqual(updated["zz"]["sockets"], 4.0)
        decoded = json.loads(base64.b64decode(updated["s2"]))
        self.assertEqual(decoded["b"], rune["b"])
        self.assertEqual(decoded["n"], 0)
        self.assertGreaterEqual(decoded["a"], editor.SEED_MIN)
        self.assertLessEqual(decoded["a"], editor.SEED_MAX)

    def test_frontend_restores_declared_empty_slots_without_swallowing_clicks(self):
        html = editor.HTML
        self.assertIn("raw&&raw.zz&&raw.zz.sockets", html)
        self.assertIn("Math.max(declaredCount,highestPayload)", html)
        self.assertIn("inp.oninput=", html)
        self.assertNotIn("inp.onchange=", html)
        self.assertIn("row.originalEncoded&&row.originalB===row.b", html)
        self.assertIn("keepEncoded:row.originalEncoded", html)
        self.assertIn("rows.length>=socketLimit", html)
        self.assertIn("data-socket-limit=", html)
        self.assertIn("socketLimit>0)acts.push(['Edit sockets...','SOCKETS',''])", html)

    def test_duplicate_socketable_names_have_unique_id_labels(self):
        jewels = [
            row for row in editor.CAT
            if row.get("kind") == "normal"
            and row.get("cls") == 15
            and row.get("name") == "Uncut Jewel"
            and row.get("available", True)
        ]
        self.assertEqual([row["b"] for row in jewels], list(range(97, 112)))
        labels = [row.get("socketChoiceLabel") for row in jewels]
        self.assertEqual(len(set(labels)), 15)
        self.assertEqual(labels[0], "Uncut Jewel [ID 97]")
        self.assertEqual(labels[-1], "Uncut Jewel [ID 111]")
        self.assertIn("r.socketChoiceLabel||r.name", editor.HTML)
        self.assertIn("Duplicate names include a required [ID ...] suffix", editor.HTML)

        result = self._socket([{"b": 97}, {"b": 111}])
        self.assertIn("ok", result)
        updated = self._read_data()
        self.assertEqual(json.loads(base64.b64decode(updated["s1"]))["b"], 97)
        self.assertEqual(json.loads(base64.b64decode(updated["s2"]))["b"], 111)

    def test_frontend_change_away_then_back_restores_original_payload(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed")
        start = editor.HTML.index("function socketRowPayload(row)")
        end = editor.HTML.index("\n  function render()", start)
        function_source = editor.HTML[start:end]
        harness = function_source + """
const row={originalEncoded:'opaque-original',originalB:97,b:98};
const changed=socketRowPayload(row);
row.b=97;
const restored=socketRowPayload(row);
process.stdout.write(JSON.stringify({changed,restored}));
"""
        result = subprocess.run(
            [node, "-e", harness],
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payloads = json.loads(result.stdout)
        self.assertEqual(payloads["changed"], {"b": 98})
        self.assertEqual(
            payloads["restored"], {"keepEncoded": "opaque-original"}
        )


if __name__ == "__main__":
    unittest.main()
