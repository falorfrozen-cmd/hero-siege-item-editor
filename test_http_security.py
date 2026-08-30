import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_gui_http_tests", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        saves = root / "hs2saves"
        saves.mkdir()
        self.db_path = root / "vault.sqlite3"
        self.patches = (
            patch.object(editor, "ROOT", root),
            patch.object(editor, "SAVES", saves),
            patch.object(editor, "VAULT_DB_FILE", self.db_path),
            patch.object(editor, "INSTANCE_GUARD_ACTIVE", False),
            patch.object(editor, "INSTANCE_PORT", None),
        )
        for active_patch in self.patches:
            active_patch.start()
        self.server = editor.ThreadingHTTPServer(("127.0.0.1", 0), editor.H)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp.cleanup()

    def request(self, method, path, body=b"", *, host=None, content_type=None,
                origin=None, editor_header=True, content_length=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest(method, path, skip_host=True)
        connection.putheader("Host", host or f"127.0.0.1:{self.port}")
        if content_type is not None:
            connection.putheader("Content-Type", content_type)
        if origin is not None:
            connection.putheader("Origin", origin)
        if editor_header:
            connection.putheader(editor.EDITOR_REQUEST_HEADER, "1")
        if method == "POST":
            size = len(body) if content_length is None else content_length
            connection.putheader("Content-Length", str(size))
        connection.endheaders(body if content_length is None or content_length == len(body) else b"")
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload)

    def valid_collection_post(self, name, *, origin_marker="same"):
        body = json.dumps({"action": "create", "name": name}).encode()
        origin = (
            f"http://127.0.0.1:{self.port}"
            if origin_marker == "same" else origin_marker
        )
        return self.request(
            "POST", "/api/vault/collections", body,
            content_type="application/json; charset=UTF-8", origin=origin,
        )

    def test_instance_endpoint_requires_exact_loopback_host_and_reports_pid(self):
        status, payload = self.request("GET", "/api/instance", editor_header=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["application"], editor.APPLICATION_ID)
        self.assertEqual(payload["version"], editor.APP_VERSION)
        self.assertEqual(payload["pid"], os.getpid())

        status, _ = self.request(
            "GET", "/api/instance", host=f"evil.example:{self.port}",
            editor_header=False,
        )
        self.assertEqual(status, 403)

    def test_cross_site_or_non_json_posts_are_rejected_without_mutation(self):
        body = json.dumps({"action": "create", "name": "Must Not Exist"}).encode()
        attempts = (
            {"host": f"evil.example:{self.port}", "content_type": "application/json",
             "origin": f"http://127.0.0.1:{self.port}"},
            {"content_type": "application/json", "origin": "https://evil.example"},
            {"content_type": "application/json", "origin": "null"},
            {"content_type": "application/json", "origin": f"http://localhost:{self.port}"},
            {"content_type": "text/plain", "origin": f"http://127.0.0.1:{self.port}"},
            {"content_type": None, "origin": f"http://127.0.0.1:{self.port}"},
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                status, _ = self.request(
                    "POST", "/api/vault/collections", body, **attempt
                )
                self.assertIn(status, {403, 415})
                self.assertFalse(self.db_path.exists())

    def test_custom_editor_header_is_required(self):
        body = json.dumps({"action": "create", "name": "Must Not Exist"}).encode()
        status, _ = self.request(
            "POST", "/api/vault/collections", body,
            content_type="application/json",
            origin=f"http://127.0.0.1:{self.port}",
            editor_header=False,
        )
        self.assertEqual(status, 403)
        self.assertFalse(self.db_path.exists())

    def test_same_origin_and_originless_local_json_posts_are_accepted(self):
        status, payload = self.valid_collection_post("Same Origin")
        self.assertEqual(status, 200)
        self.assertIn("ok", payload)

        status, payload = self.valid_collection_post("Native Client", origin_marker=None)
        self.assertEqual(status, 200)
        self.assertIn("ok", payload)
        names = {row.name for row in editor.vault_store().list_collections()}
        self.assertTrue({"Same Origin", "Native Client"}.issubset(names))

    def test_bad_lengths_and_malformed_json_fail_cleanly_then_server_stays_alive(self):
        malformed = b"{not-json"
        status, _ = self.request(
            "POST", "/api/vault/collections", malformed,
            content_type="application/json",
            origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(status, 400)

        status, _ = self.request(
            "POST", "/api/vault/collections", b"",
            content_type="application/json",
            origin=f"http://127.0.0.1:{self.port}", content_length=-1,
        )
        self.assertEqual(status, 400)

        status, _ = self.request(
            "POST", "/api/vault/collections", b"",
            content_type="application/json",
            origin=f"http://127.0.0.1:{self.port}",
            content_length=editor.MAX_POST_BYTES + 1,
        )
        self.assertEqual(status, 413)

        status, payload = self.request("GET", "/api/instance", editor_header=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["application"], editor.APPLICATION_ID)

    def test_embedded_client_marks_every_post_as_local_json(self):
        self.assertIn("'Content-Type':'application/json'", editor.HTML)
        self.assertIn("'X-Hero-Siege-Item-Editor':'1'", editor.HTML)

    def test_html_cannot_be_framed_and_responses_disable_sniffing(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        raw_response = connection.getresponse()
        raw_response.read()
        self.assertEqual(raw_response.status, 200)
        self.assertEqual(raw_response.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(raw_response.getheader("Content-Security-Policy"),
                         "frame-ancestors 'none'")
        self.assertEqual(raw_response.getheader("X-Content-Type-Options"), "nosniff")
        connection.close()


if __name__ == "__main__":
    unittest.main()
