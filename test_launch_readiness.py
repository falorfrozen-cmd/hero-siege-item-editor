import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


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
            patch.object(editor, "ThreadingHTTPServer", side_effect=servers) as server_factory,
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
            patch.object(editor, "ThreadingHTTPServer") as server_factory,
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
            patch.object(editor, "ThreadingHTTPServer") as server_factory,
            patch.object(editor, "_open_window") as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        server_factory.assert_not_called()
        open_window.assert_not_called()
        message = show_error.call_args.args[0]
        self.assertIn("v2.7.2", message)
        self.assertIn(editor.APP_VERSION, message)

    def test_main_fails_closed_if_any_reserved_port_is_unidentified(self):
        first_server = SimpleNamespace(
            serve_forever=Mock(), shutdown=Mock(), server_close=Mock(),
        )
        with (
            patch.object(editor, "_exclusive_save_file", return_value=editor.nullcontext()),
            patch.object(editor, "_editor_identity", return_value=None),
            patch.object(
                editor, "ThreadingHTTPServer",
                side_effect=[first_server, OSError("occupied")],
            ) as server_factory,
            patch.object(editor.threading, "Thread") as thread_factory,
            patch.object(editor, "_open_window") as open_window,
            patch.object(editor, "_show_startup_error") as show_error,
        ):
            editor.main()

        self.assertEqual(server_factory.call_count, 2)
        first_server.server_close.assert_called_once_with()
        first_server.shutdown.assert_not_called()
        thread_factory.assert_not_called()
        open_window.assert_not_called()
        message = show_error.call_args.args[0]
        self.assertIn(str(editor.PORT + 1), message)
        self.assertIn("unidentified or legacy", message)

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
            patch.object(editor, "ThreadingHTTPServer", side_effect=servers),
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
            "hs_dice_skill_targets.json",
            "item_icons",
            "roll_profile_db",
            "generated_pool_model",
            "dice_skill_selector",
            "torch_class_selector",
            "game_build_identity",
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


if __name__ == "__main__":
    unittest.main()
