import importlib.util
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen


MODULE_PATH = Path(__file__).with_name("hs_item_editor_gui.py")
SPEC = importlib.util.spec_from_file_location("hs_item_editor_gui_launch_tests", MODULE_PATH)
editor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(editor)


class _Response:
    def __init__(self, document):
        self.payload = json.dumps(document).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class _ForeignPanel(BaseHTTPRequestHandler):
    """Answers like ForgePact's panel: its own page at /, a JSON 404 elsewhere."""

    page = b"<!DOCTYPE html><html><head><title>ForgePact</title></head><body></body></html>"

    def do_GET(self):
        if self.path == "/":
            body, status, kind = self.page, 200, "text/html; charset=utf-8"
        else:
            body, status, kind = b'{"err": "not found"}', 404, "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class _PreInstanceEditor(_ForeignPanel):
    """An Item Editor before v2.7.2: /api/instance is a 404, its page is at /."""

    page = (
        b'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
        b"<title>Hero Siege Item Editor \xe2\x80\x94 Season 10</title>\n<style>"
    )


class _UnsharedHTTPServer(ThreadingHTTPServer):
    """A listener that never shares its port, like most programs not built on http.server."""

    allow_reuse_address = False


def _get(port, path):
    """(status, JSON body) of a GET on this loopback port."""
    try:
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _free_ports(count):
    """The first of `count` consecutive loopback ports nothing holds right now.

    Drawn from 20000-32767, outside the ephemeral ranges Windows (49152 up, or
    as low as 1024-15000 on some machines) and Linux (32768 up) hand out to
    this test's own connections, one port after another.
    """
    for first in random.sample(range(20000, 32768 - count), 50):
        held = []
        try:
            for port in range(first, first + count):
                held.append(socket.socket())
                held[-1].bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            for sock in held:
                sock.close()
        return first
    raise unittest.SkipTest(f"no {count} consecutive free loopback ports")


class LaunchReadinessTests(unittest.TestCase):
    def setUp(self):
        editor.INSTANCE_GUARD_ACTIVE = False
        editor.INSTANCE_PORT = None

    def test_resource_base_uses_source_directory(self):
        with patch.object(editor.sys, "frozen", False, create=True):
            self.assertEqual(editor._resource_base(), MODULE_PATH.resolve().parent)

    def test_resource_base_uses_frozen_bundle_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(editor.sys, "frozen", True, create=True),
                patch.object(editor.sys, "_MEIPASS", directory, create=True),
            ):
                self.assertEqual(editor._resource_base(), Path(directory).resolve())

    def test_resource_base_rejects_broken_frozen_environment(self):
        with (
            patch.object(editor.sys, "frozen", True, create=True),
            patch.object(editor.sys, "_MEIPASS", None, create=True),
        ):
            with self.assertRaisesRegex(RuntimeError, "resource directory"):
                editor._resource_base()

    def test_game_detection_matches_process_name_case_insensitively(self):
        result = subprocess.CompletedProcess([], 0, "hero_siege.EXE  123 Console\n", "")
        with patch.object(editor.subprocess, "run", return_value=result):
            self.assertTrue(editor.game_running())

    def test_game_detection_reports_closed_when_tasklist_succeeds_without_game(self):
        result = subprocess.CompletedProcess([], 0, "INFO: No tasks match.\n", "")
        with patch.object(editor.subprocess, "run", return_value=result):
            self.assertFalse(editor.game_running())

    def test_game_detection_fails_closed_on_error_or_nonzero_exit(self):
        failed = subprocess.CompletedProcess([], 1, "", "tasklist failed")
        with patch.object(editor.subprocess, "run", return_value=failed):
            self.assertTrue(editor.game_running())
        with patch.object(editor.subprocess, "run", side_effect=subprocess.TimeoutExpired("tasklist", 10)):
            self.assertTrue(editor.game_running())

    def test_existing_server_reuse_requires_application_identity_and_version(self):
        with patch.object(
            editor,
            "urlopen",
            return_value=_Response({"application": "some-other-app", "version": editor.APP_VERSION}),
        ) as foreign_request:
            self.assertIsNone(editor._served_version(editor.PORT))
        foreign_request.assert_called_once_with(
            f"http://127.0.0.1:{editor.PORT}/api/instance", timeout=1
        )
        with patch.object(
            editor,
            "urlopen",
            return_value=_Response({
                "application": editor.APPLICATION_ID,
                "version": editor.APP_VERSION,
            }),
        ):
            self.assertEqual(editor._served_version(editor.PORT), editor.APP_VERSION)

    def test_editor_identity_preserves_pid_and_recognizes_pidless_legacy_peer(self):
        with patch.object(
            editor,
            "urlopen",
            return_value=_Response({
                "application": editor.APPLICATION_ID,
                "version": editor.APP_VERSION,
                "pid": 4321,
            }),
        ):
            self.assertEqual(
                editor._editor_identity(editor.PORT),
                {"version": editor.APP_VERSION, "pid": 4321, "port": editor.PORT},
            )

        with patch.object(
            editor,
            "urlopen",
            return_value=_Response({
                "application": editor.APPLICATION_ID,
                "version": "2.7.2",
            }),
        ):
            self.assertEqual(
                editor._editor_identity(editor.PORT + 1),
                {"version": "2.7.2", "pid": None, "port": editor.PORT + 1},
            )

    def test_peer_scan_ignores_own_process_and_excluded_port(self):
        own_identity = {
            "version": editor.APP_VERSION,
            "pid": 1234,
            "port": editor.PORT,
        }

        def identity(candidate, timeout=1.0):
            return own_identity if candidate == editor.PORT else None

        with (
            patch.object(editor.os, "getpid", return_value=1234),
            patch.object(editor, "_editor_identity", side_effect=identity) as lookup,
        ):
            self.assertIsNone(editor._peer_editor_error())
            self.assertIsNone(editor._peer_editor_error(exclude_port=editor.PORT))

        self.assertEqual(lookup.call_count, 19)
        self.assertNotIn(
            editor.PORT,
            [call.args[0] for call in lookup.call_args_list[10:]],
        )

    def test_peer_scan_blocks_other_process_and_pidless_legacy_editor(self):
        cases = (
            (editor.APP_VERSION, 9876, "PID 9876"),
            ("2.7.2", None, "v2.7.2"),
        )
        for version, pid, expected in cases:
            with self.subTest(version=version, pid=pid):
                peer_port = editor.PORT + 3

                def identity(candidate, timeout=1.0):
                    if candidate == peer_port:
                        return {"version": version, "pid": pid, "port": candidate}
                    return None

                with (
                    patch.object(editor.os, "getpid", return_value=1234),
                    patch.object(editor, "_editor_identity", side_effect=identity),
                ):
                    error = editor._peer_editor_error()

                self.assertIn(expected, error)
                self.assertIn(f"port {peer_port}", error)

    def test_browser_fallback_is_nonblocking_and_never_opens_a_real_browser(self):
        with (
            patch.dict(sys.modules, {"webview": None}),
            patch.object(webbrowser, "open", return_value=True) as open_browser,
        ):
            self.assertFalse(editor._open_window(9876))
        open_browser.assert_called_once_with("http://127.0.0.1:9876")

    def test_main_keeps_new_server_alive_for_browser_fallback_then_closes_it(self):
        servers = [SimpleNamespace(
            serve_forever=Mock(), shutdown=Mock(), server_close=Mock(),
        ) for _ in range(10)]
        server_threads = [SimpleNamespace(start=Mock(), join=Mock()) for _ in range(10)]
        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", return_value=None),
            patch.object(editor, "_peer_editor_error", return_value=None),
            patch.object(editor, "EditorHTTPServer", side_effect=servers) as server_factory,
            patch.object(editor.threading, "Thread", side_effect=server_threads),
            patch.object(editor, "_open_window", return_value=False),
        ):
            editor.main()
        self.assertEqual(server_factory.call_count, 10)
        self.assertEqual(
            {call.args[0][1] for call in server_factory.call_args_list},
            set(range(editor.PORT, editor.PORT + 10)),
        )
        for server_thread in server_threads:
            server_thread.start.assert_called_once_with()
        server_threads[0].join.assert_called_once_with()
        for server_thread in server_threads[1:]:
            server_thread.join.assert_not_called()
        for server in servers:
            server.shutdown.assert_called_once_with()
            server.server_close.assert_called_once_with()

    def test_main_reuses_same_version_found_on_any_reserved_port(self):
        peer_port = editor.PORT + 4

        def identity(candidate, timeout=1.0):
            if candidate == peer_port:
                return {
                    "version": editor.APP_VERSION,
                    "pid": 5678,
                    "port": candidate,
                }
            return None

        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", side_effect=identity),
            patch.object(editor, "EditorHTTPServer") as server_factory,
            patch.object(editor, "_open_window", return_value=True) as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        open_window.assert_called_once_with(peer_port)
        server_factory.assert_not_called()
        show_error.assert_not_called()

    def test_main_rejects_different_version_instead_of_starting_on_next_port(self):
        peer_port = editor.PORT + 2

        def identity(candidate, timeout=1.0):
            if candidate == peer_port:
                return {"version": "2.7.2", "pid": 5678, "port": candidate}
            return None

        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", side_effect=identity),
            patch.object(editor, "EditorHTTPServer") as server_factory,
            patch.object(editor, "_open_window") as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        server_factory.assert_not_called()
        open_window.assert_not_called()
        message = show_error.call_args.args[0]
        self.assertIn("v2.7.2", message)
        self.assertIn(editor.APP_VERSION, message)

    def test_main_refuses_an_editor_older_than_v2_7_2_on_its_first_port(self):
        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", return_value=None),
            patch.object(editor, "_legacy_editor_page", return_value=True) as legacy_page,
            patch.object(
                editor, "EditorHTTPServer", side_effect=OSError("occupied"),
            ) as server_factory,
            patch.object(editor.threading, "Thread") as thread_factory,
            patch.object(editor, "_open_window") as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        server_factory.assert_called_once()
        legacy_page.assert_called_once_with(editor.PORT)
        thread_factory.assert_not_called()
        open_window.assert_not_called()
        message = show_error.call_args.args[0]
        self.assertIn("older than v2.7.2", message)
        self.assertIn(f"port {editor.PORT}", message)

    def test_main_leaves_ports_other_programs_hold_and_says_so_when_none_is_left(self):
        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", return_value=None),
            patch.object(editor, "_legacy_editor_page", return_value=False) as legacy_page,
            patch.object(
                editor, "EditorHTTPServer", side_effect=OSError("occupied"),
            ) as server_factory,
            patch.object(editor.threading, "Thread") as thread_factory,
            patch.object(editor, "_open_window") as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        # Every port is tried; only the first one ever hosted a pre-2.7.2 editor.
        self.assertEqual(server_factory.call_count, 10)
        legacy_page.assert_called_once_with(editor.PORT)
        thread_factory.assert_not_called()
        open_window.assert_not_called()
        show_error.assert_called_once_with(
            f"No free editor port in {editor.PORT}..{editor.PORT + 9}."
        )

    def test_legacy_editor_page_recognizes_only_an_item_editor_page(self):
        pages = (
            (b'<!DOCTYPE html>\n<html lang="tr"><head><meta charset="utf-8">'
             b"<title>Hero Siege Item Editor</title>\n<style>", True),
            (b'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
             b"<title>Hero Siege Item Editor \xe2\x80\x94 Season 10</title>", True),
            (b"<!DOCTYPE html><html><head><title>ForgePact</title></head>", False),
            (b'{"err": "not found"}', False),
        )
        for page, expected in pages:
            with self.subTest(page=page[:60]):
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.read = Mock(return_value=page)
                with patch.object(editor, "urlopen", return_value=response) as request:
                    self.assertIs(editor._legacy_editor_page(editor.PORT), expected)
                request.assert_called_once_with(f"http://127.0.0.1:{editor.PORT}/", timeout=1.0)
                response.read.assert_called_once_with(4096)
        with patch.object(editor, "urlopen", side_effect=OSError("refused")):
            self.assertFalse(editor._legacy_editor_page(editor.PORT))

    def test_peer_guard_still_checks_a_port_startup_left_to_another_program(self):
        left = editor.PORT + 1
        reserved = frozenset(range(editor.PORT, editor.PORT + 10)) - {left}

        def identity(candidate, timeout=1.0):
            if candidate == left:
                return {"version": "2.7.2", "pid": None, "port": candidate}
            return None

        with (
            patch.object(editor, "INSTANCE_GUARD_ACTIVE", True),
            patch.object(editor, "INSTANCE_RESERVED_PORTS", reserved),
            patch.object(editor, "_editor_identity", return_value=None) as lookup,
        ):
            self.assertIsNone(editor._active_peer_editor_error())
        self.assertEqual([call.args[0] for call in lookup.call_args_list], [left])

        # An editor that later starts on that port is still caught before a write.
        with (
            patch.object(editor, "INSTANCE_GUARD_ACTIVE", True),
            patch.object(editor, "INSTANCE_RESERVED_PORTS", reserved),
            patch.object(editor, "_editor_identity", side_effect=identity),
        ):
            error = editor._active_peer_editor_error()
        self.assertIn("v2.7.2", error)
        self.assertIn(f"port {left}", error)

    def test_main_closes_new_server_if_peer_appears_after_bind(self):
        servers = [SimpleNamespace(
            serve_forever=Mock(), shutdown=Mock(), server_close=Mock(),
        ) for _ in range(10)]
        server_threads = [SimpleNamespace(start=Mock(), join=Mock()) for _ in range(10)]
        peer_error = "Another Hero Siege Item Editor appeared."
        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", return_value=None),
            patch.object(editor, "_peer_editor_error", return_value=peer_error),
            patch.object(editor, "EditorHTTPServer", side_effect=servers),
            patch.object(editor.threading, "Thread", side_effect=server_threads),
            patch.object(editor, "_open_window") as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        for server_thread in server_threads:
            server_thread.start.assert_called_once_with()
            server_thread.join.assert_not_called()
        for server in servers:
            server.shutdown.assert_called_once_with()
            server.server_close.assert_called_once_with()
        open_window.assert_not_called()
        show_error.assert_called_once_with(peer_error)
        self.assertFalse(editor.INSTANCE_GUARD_ACTIVE)
        self.assertIsNone(editor.INSTANCE_PORT)

    def test_instance_endpoint_reports_process_identity(self):
        handler = object.__new__(editor.H)
        handler.path = "/api/instance"
        handler._require_local_host = Mock(return_value=True)
        handler._json = Mock()
        with patch.object(editor.os, "getpid", return_value=2468):
            handler.do_GET()

        handler._json.assert_called_once_with({
            "application": editor.APPLICATION_ID,
            "version": editor.APP_VERSION,
            "pid": 2468,
        })

    def test_runtime_peer_guard_rejects_save_and_vault_transfer_before_dispatch(self):
        for path, body in (
            ("/api/add", {"cid": 1}),
            ("/api/vault/deposit", {"tab": "stash_tab_1", "key": "item"}),
        ):
            with self.subTest(path=path):
                handler = object.__new__(editor.H)
                handler.path = path
                handler._require_local_host = Mock(return_value=True)
                handler._read_json_post = Mock(return_value=body)
                handler._json = Mock()
                handler._dispatch_post = Mock()
                peer_error = "Another Hero Siege Item Editor is running."
                with (
                    patch.object(editor, "INSTANCE_GUARD_ACTIVE", True),
                    patch.object(editor, "INSTANCE_PORT", editor.PORT),
                    patch.object(editor, "_peer_editor_error", return_value=peer_error) as scan,
                    patch.object(editor, "_exclusive_save_file") as save_lock,
                ):
                    handler.do_POST()

                handler._json.assert_called_once_with({"err": peer_error})
                handler._dispatch_post.assert_not_called()
                save_lock.assert_not_called()
                scan.assert_called_once_with(exclude_port=editor.PORT)

    def test_roll_database_path_fails_closed_until_generated_asset_is_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = editor.load_roll_profile_database(directory)
            self.assertFalse(database.available)
            self.assertEqual(database.status.code, "missing")
            self.assertEqual(database.profile_count, 0)
            self.assertEqual(
                database.path,
                Path(directory) / "hs_perfect_roll_profiles.json",
            )

        installed_path = editor.BASE / "hs_perfect_roll_profiles.json"
        installed = editor.load_roll_profile_database(editor.BASE)
        self.assertEqual(installed.path, installed_path)
        if installed_path.exists():
            self.assertTrue(installed.available, installed.status.message)
            self.assertGreater(installed.profile_count, 0)
        else:
            self.assertFalse(installed.available)
            self.assertEqual(installed.status.code, "missing")

    def test_release_spec_bundles_every_runtime_roll_dependency(self):
        spec_text = MODULE_PATH.with_name("HeroSiegeItemEditor.spec").read_text(
            encoding="utf-8"
        )
        for required in (
            "hs_full_catalog.json",
            "hs_runewords.json",
            "hs_sets.json",
            "hs_perfect_roll_profiles.json",
            "hs_socket_seeds.json",
            "hs_dice_skill_targets.json",
            "item_icons",
            "roll_profile_db",
            "generated_pool_model",
            "dice_skill_selector",
            "torch_class_selector",
            "game_build_identity",
            "game_truth",
            "webview",
        ):
            self.assertIn(required, spec_text)

    def test_shared_stash_drag_has_wheel_and_edge_scroll_fallbacks(self):
        html = editor.HTML
        for required in (
            "function dragScrollVelocity",
            "requestAnimationFrame(dragScrollTick)",
            "document.addEventListener('wheel',wheelDragScroll,{capture:true,passive:false})",
            "document.addEventListener('dragend',finishDrag,true)",
            "updateDragScroll(e);",
            "Moving between stash tabs:",
            "saved only when dropped on a valid cell",
            "localeCompare(b,undefined,{numeric:true,sensitivity:'base'})",
        ):
            self.assertIn(required, html)
        self.assertLess(
            html.index("updateDragScroll(e);"),
            html.index("const sEl=e.target.closest('.dslot');"),
        )

    def test_embedded_javascript_parses_and_drag_velocity_is_directional(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed; embedded JS harness skipped")
        script_match = re.search(r"<script>(.*)</script>", editor.HTML, re.DOTALL)
        self.assertIsNotNone(script_match)
        parsed = subprocess.run(
            [node, "-e", "new Function(require('fs').readFileSync(0,'utf8'));"],
            input=script_match.group(1), encoding="utf-8", capture_output=True,
        )
        self.assertEqual(parsed.returncode, 0, parsed.stderr)

        helper_match = re.search(
            r"function dragScrollVelocity\([^\r\n]+", editor.HTML
        )
        self.assertIsNotNone(helper_match)
        harness = (
            "const DRAG_SCROLL_MAX=24;\n" + helper_match.group(0) + "\n"
            "console.log(JSON.stringify(["
            "dragScrollVelocity(100,100,600),"
            "dragScrollVelocity(187,100,600),"
            "dragScrollVelocity(400,100,600),"
            "dragScrollVelocity(613,100,600),"
            "dragScrollVelocity(700,100,600),"
            "dragScrollVelocity(50,100,600)]));"
        )
        checked = subprocess.run(
            [node, "-e", harness], encoding="utf-8", capture_output=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout), [-24, -2, 0, 2, 24, 0])

    def test_embedded_drag_scroll_handlers_are_scoped_and_bounded(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed; embedded JS harness skipped")

        velocity = re.search(
            r"function dragScrollVelocity\([^\r\n]+", editor.HTML
        )
        handlers = re.search(
            r"function stopDragScroll\(\)\{.*?(?=function finishDrag\(\))",
            editor.HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(velocity)
        self.assertIsNotNone(handlers)

        harness = f"""
const DRAG_SCROLL_MAX=24;
let dragInfo=null, dragScrollFrame=0, dragScrollSpeed=0, view='stash';
let cleared=0, scheduled=null, nextFrame=1;
const mid={{
  scrollTop:0, scrollHeight:1000, clientHeight:100,
  getBoundingClientRect(){{return {{left:0,right:500,top:100,height:600}};}}
}};
const document={{getElementById:()=>mid}};
function clearGhost(){{cleared++;}}
function requestAnimationFrame(cb){{scheduled=cb;return nextFrame++;}}
function cancelAnimationFrame(){{scheduled=null;}}
{velocity.group(0)}
{handlers.group(0)}
function wheel(deltaY,deltaMode=0){{
  let prevented=false;
  wheelDragScroll({{deltaY,deltaMode,preventDefault(){{prevented=true;}}}});
  return {{top:mid.scrollTop,prevented}};
}}
const out={{}};
out.noDrag=wheel(200);
dragInfo={{}}; view='character'; out.wrongView=wheel(200);
view='stash'; out.pixel=wheel(420);
mid.scrollTop=880; out.clamped=wheel(50);
out.atLimit=wheel(50);
mid.scrollTop=0; out.line=wheel(2,1);
mid.scrollTop=0; out.page=wheel(1,2);
mid.scrollTop=0; dragScrollSpeed=0; dragScrollFrame=0;
updateDragScroll({{clientX:250,clientY:698}});
out.edgeScheduled={{speed:dragScrollSpeed,frame:dragScrollFrame,scheduled:!!scheduled}};
scheduled();
out.edgeTick={{top:mid.scrollTop,speed:dragScrollSpeed,frame:dragScrollFrame,cleared}};
updateDragScroll({{clientX:501,clientY:698}});
out.outside={{speed:dragScrollSpeed,frame:dragScrollFrame,scheduled:!!scheduled}};
console.log(JSON.stringify(out));
"""
        checked = subprocess.run(
            [node, "-e", harness], encoding="utf-8", capture_output=True,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(
            json.loads(checked.stdout),
            {
                "noDrag": {"top": 0, "prevented": False},
                "wrongView": {"top": 0, "prevented": False},
                "pixel": {"top": 420, "prevented": True},
                "clamped": {"top": 900, "prevented": True},
                "atLimit": {"top": 900, "prevented": False},
                "line": {"top": 72, "prevented": True},
                "page": {"top": 85, "prevented": True},
                "edgeScheduled": {"speed": 24, "frame": 1, "scheduled": True},
                "edgeTick": {"top": 24, "speed": 24, "frame": 2, "cleared": 5},
                "outside": {"speed": 0, "frame": 0, "scheduled": False},
            },
        )


class ForeignPortTests(unittest.TestCase):
    """main() against real loopback sockets, on a free run of ten ports.

    ForgePact's panel prefers 8766, the editor's second port. Only the window,
    the startup lock and game truth are replaced; binding, identifying and
    serving are real.
    """

    def setUp(self):
        editor.INSTANCE_GUARD_ACTIVE = False
        editor.INSTANCE_PORT = None
        editor.INSTANCE_RESERVED_PORTS = frozenset()

    def _serve(self, server_class, port, handler):
        server = server_class(("127.0.0.1", port), handler)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        ).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def _start_editor(self, first, other):
        """Run main() on first..first+9; the window records what it saw."""
        seen = {}

        def open_window(port):
            seen["port"] = port
            seen["reserved"] = editor.INSTANCE_RESERVED_PORTS
            seen["editor"] = _get(port, "/api/instance")
            seen["other"] = _get(other, "/api/instance")
            seen["peer_error"] = editor._active_peer_editor_error()
            return True  # the native window closed, so main() shuts down

        serve_forever = editor.EditorHTTPServer.serve_forever
        errors = []
        with (
            patch.object(editor, "PORT", first),
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_start_game_truth"),
            patch.object(editor, "_open_window", side_effect=open_window),
            patch.object(editor, "_show_startup_error", side_effect=errors.append),
            # main() stops its servers one after another, and each notices
            # only at its next poll: 0.5 s by default, seconds per run here.
            patch.object(
                editor.EditorHTTPServer, "serve_forever",
                lambda server: serve_forever(server, poll_interval=0.01),
            ),
        ):
            editor.main()
        return errors, seen

    def test_editor_starts_beside_another_program_and_leaves_its_port_alone(self):
        cases = (
            # ForgePact's panel sets SO_REUSEADDR. On Windows the editor's
            # bind used to succeed on top of it, silently.
            ("shares its port, on the second one", ThreadingHTTPServer, 1),
            # A listener that does not share: the bind failed, and startup
            # refused with "occupied by an unidentified or legacy process".
            ("keeps its port, on the second one", _UnsharedHTTPServer, 1),
            # Not an editor on the first port either: the editor moves past it.
            ("shares its port, on the first one", ThreadingHTTPServer, 0),
        )
        for label, server_class, offset in cases:
            with self.subTest(label):
                first = _free_ports(10)
                other = first + offset
                self._serve(server_class, other, _ForeignPanel)

                errors, seen = self._start_editor(first, other)

                self.assertEqual(errors, [])
                self.assertEqual(seen["port"], first + 1 if other == first else first)
                self.assertEqual(
                    seen["reserved"], frozenset(range(first, first + 10)) - {other}
                )
                status, identity = seen["editor"]
                self.assertEqual(status, 200)
                self.assertEqual(identity["application"], editor.APPLICATION_ID)
                self.assertEqual(identity["pid"], os.getpid())
                self.assertEqual(seen["other"], (404, {"err": "not found"}))
                self.assertIsNone(seen["peer_error"])
                # The other program kept its port throughout and still has it.
                self.assertEqual(_get(other, "/api/instance"), (404, {"err": "not found"}))

    def test_an_item_editor_older_than_v2_7_2_on_the_first_port_is_refused(self):
        first = _free_ports(10)
        # Those builds served PORT from ThreadingHTTPServer, SO_REUSEADDR and all.
        self._serve(ThreadingHTTPServer, first, _PreInstanceEditor)

        errors, seen = self._start_editor(first, first)

        self.assertEqual(seen, {})
        self.assertEqual(len(errors), 1)
        self.assertIn("older than v2.7.2", errors[0])
        self.assertIn(f"port {first}", errors[0])

    def test_a_port_the_editor_holds_refuses_address_reuse_and_rebinds_at_once(self):
        port = _free_ports(1)
        server = editor.EditorHTTPServer(("127.0.0.1", port), editor.H)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        ).start()
        try:
            for _ in range(5):
                self.assertEqual(_get(port, "/api/instance")[0], 200)
            # v2.7.2's launcher and ForgePact bind with SO_REUSEADDR, which on
            # Windows shares a port with a listener that set it too.
            with self.assertRaises(OSError):
                ThreadingHTTPServer(("127.0.0.1", port), _ForeignPanel).server_close()
        finally:
            server.shutdown()
            server.server_close()
        # The connections just served sit in TIME_WAIT; a restart still binds.
        editor.EditorHTTPServer(("127.0.0.1", port), editor.H).server_close()


if __name__ == "__main__":
    unittest.main()
