import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from custom_forge_runtime import (
    RUNTIME_MARKER,
    forgepact_game_exe,
    inspect_game_dir,
    plugin_supports_forge,
    runtime_status,
)


BASE = Path(__file__).resolve().parent
EDITOR_SPEC = importlib.util.spec_from_file_location(
    "hs_item_editor_gui_runtime_tests", BASE / "hs_item_editor_gui.py"
)
editor = importlib.util.module_from_spec(EDITOR_SPEC)
EDITOR_SPEC.loader.exec_module(editor)


def _no_running_game():
    return None


class CustomForgeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "Hero_Siege"
        self.root.mkdir()
        self.game = Path(self.temp.name) / "Game" / "bin"
        (self.game / "mods" / "aurie").mkdir(parents=True)
        (self.game / "bp_ipc").mkdir()
        self.exe = self.game / "Hero_Siege.exe"
        self.exe.write_bytes(b"MZ")
        self.missing_steam = Path(self.temp.name) / "nowhere" / "Hero_Siege.exe"

    def tearDown(self):
        self.temp.cleanup()

    # --- helpers ----------------------------------------------------------
    def _config(self, exe: Path) -> None:
        (self.root / "forgepact.json").write_text(
            json.dumps({"game_exe": str(exe)}), encoding="utf-8"
        )

    def _plugin(self, capable: bool) -> Path:
        dll = self.game / "mods" / "aurie" / "BloodPactPlugin.dll"
        body = b"\x00" * 64 + (RUNTIME_MARKER if capable else b"OLD_BUILD") + b"\x00" * 64
        dll.write_bytes(body)
        (self.game / "AurieCore.dll").write_bytes(b"MZ")
        return dll

    def _sidecar(self, entries: int, when: float | None = None) -> Path:
        path = self.root / "hs_custom_item_forge.runtime"
        lines = ["HS_CUSTOM_ITEM_FORGE_V1"] + [
            f"item|t=0;a={i};b=1|keep=1|0=4" for i in range(entries)
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if when is not None:
            os.utime(path, (when, when))
        return path

    def _status(self, detail: str, *, entries: int = 1, hooks: bool = True, when: float | None = None) -> Path:
        path = self.game / "bp_ipc" / "customforge_status.json"
        path.write_text(json.dumps({
            "schemaVersion": 1, "entries": entries, "hooksActive": hooks,
            "applications": 1 if detail == "runtime stats applied" else 0,
            "detail": detail,
        }), encoding="utf-8")
        if when is not None:
            os.utime(path, (when, when))
        return path

    def _run(self):
        return runtime_status(
            self.root, running_exe=_no_running_game, steam_default=self.missing_steam
        )

    # --- building blocks ---------------------------------------------------
    def test_forgepact_config_supplies_the_game_exe(self):
        self._config(self.exe)
        self.assertEqual(forgepact_game_exe(self.root), self.exe)
        (self.root / "forgepact.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(forgepact_game_exe(self.root))

    def test_marker_decides_plugin_capability(self):
        self.assertTrue(plugin_supports_forge(self._plugin(True)))
        self.assertFalse(plugin_supports_forge(self._plugin(False)))
        self.assertFalse(plugin_supports_forge(self.game / "absent.dll"))

    def test_inspect_reports_missing_pieces_without_raising(self):
        facts = inspect_game_dir(self.game, "forgepact")
        self.assertTrue(facts["exists"])
        self.assertFalse(facts["pluginFound"])
        self.assertFalse(facts["aurieCore"])
        self.assertIsNone(facts["status"])

    # --- verdicts ------------------------------------------------------------
    def test_no_installation_found(self):
        result = self._run()
        self.assertEqual(result["code"], "game_not_found")
        self.assertEqual(result["level"], "danger")
        self.assertIsNone(result["gameDir"])

    def test_forgepact_not_installed(self):
        self._config(self.exe)
        result = self._run()
        self.assertEqual(result["code"], "forgepact_missing")
        self.assertEqual(result["level"], "danger")
        self.assertEqual(result["gameDir"], str(self.game))
        self.assertEqual(result["source"], "forgepact")

    def test_plugin_missing_but_aurie_present(self):
        self._config(self.exe)
        (self.game / "AurieCore.dll").write_bytes(b"MZ")
        self.assertEqual(self._run()["code"], "plugin_missing")

    def test_outdated_plugin_is_flagged_red(self):
        self._config(self.exe)
        self._plugin(capable=False)
        result = self._run()
        self.assertEqual(result["code"], "plugin_outdated")
        self.assertEqual(result["level"], "danger")
        self.assertTrue(result["pluginFound"])
        self.assertFalse(result["pluginSupportsForge"])

    def test_capable_plugin_without_status_asks_for_a_game_start(self):
        self._config(self.exe)
        self._plugin(capable=True)
        result = self._run()
        self.assertEqual(result["code"], "awaiting_game_start")
        self.assertEqual(result["level"], "warn")

    def test_applied_status_newer_than_sidecar_is_green(self):
        self._config(self.exe)
        self._plugin(capable=True)
        now = time.time()
        self._sidecar(2, when=now - 600)
        self._status("runtime stats applied", entries=2, when=now - 10)
        result = self._run()
        self.assertEqual(result["code"], "applied")
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["sidecarEntries"], 2)

    def test_sidecar_newer_than_status_requires_restart(self):
        self._config(self.exe)
        self._plugin(capable=True)
        now = time.time()
        self._status("runtime stats applied", entries=2, when=now - 600)
        self._sidecar(2, when=now - 10)
        result = self._run()
        self.assertEqual(result["code"], "restart_required")
        self.assertEqual(result["level"], "warn")

    def test_entry_count_mismatch_requires_restart(self):
        self._config(self.exe)
        self._plugin(capable=True)
        now = time.time()
        self._sidecar(3, when=now - 600)
        self._status("runtime stats applied", entries=2, when=now - 10)
        self.assertEqual(self._run()["code"], "restart_required")

    def test_plugin_that_found_no_sidecar_is_red(self):
        self._config(self.exe)
        self._plugin(capable=True)
        self._sidecar(1, when=time.time() - 600)
        self._status("no runtime file", entries=0)
        result = self._run()
        self.assertEqual(result["code"], "sidecar_not_found")
        self.assertEqual(result["level"], "danger")

    def test_hook_failure_is_red(self):
        self._config(self.exe)
        self._plugin(capable=True)
        self._status("runtime hook installation failed", hooks=False)
        self.assertEqual(self._run()["code"], "hooks_failed")

    def test_running_game_wins_over_configured_path(self):
        other = Path(self.temp.name) / "Other" / "bin"
        (other / "mods" / "aurie").mkdir(parents=True)
        other_exe = other / "Hero_Siege.exe"
        other_exe.write_bytes(b"MZ")
        self._config(other_exe)
        self._plugin(capable=True)
        result = runtime_status(
            self.root, running_exe=lambda: self.exe, steam_default=self.missing_steam
        )
        self.assertEqual(result["source"], "running")
        self.assertEqual(result["gameDir"], str(self.game))
        # every probed path is listed for the UI, existing or not: running, forgepact, steam
        self.assertEqual([c["source"] for c in result["candidates"]], ["running", "forgepact", "steam"])
        self.assertEqual(sum(1 for c in result["candidates"] if c["exists"]), 2)

    def test_lookup_exception_never_propagates(self):
        def boom():
            raise RuntimeError("powershell exploded")
        self._config(self.exe)
        self._plugin(capable=True)
        result = runtime_status(self.root, running_exe=boom, steam_default=self.missing_steam)
        self.assertEqual(result["code"], "awaiting_game_start")


class EditorIntegrationTests(unittest.TestCase):
    def test_editor_wrapper_uses_root_and_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(editor, "ROOT", Path(directory)):
                with patch.object(editor, "_custom_forge_runtime_status",
                                  side_effect=RuntimeError("boom")):
                    result = editor.custom_forge_runtime_status()
                    self.assertEqual(result["code"], "unknown")
                    self.assertEqual(result["level"], "warn")
                result = editor.custom_forge_runtime_status()
                self.assertIn(result["code"], {"game_not_found", "forgepact_missing",
                                               "plugin_missing", "plugin_outdated",
                                               "awaiting_game_start", "applied",
                                               "hooks_installed", "restart_required",
                                               "sidecar_not_found", "schema_mismatch",
                                               "hooks_failed", "unknown_detail"})

    def test_ui_wires_the_runtime_endpoint_and_banner(self):
        self.assertIn("/api/custom-forge/runtime", editor.HTML)
        self.assertIn("forgeRuntimeBanner(", editor.HTML)
        self.assertIn(".f-runtime.danger", editor.HTML)
        self.assertIn('id="ifruntime"', editor.HTML)


if __name__ == "__main__":
    unittest.main()
