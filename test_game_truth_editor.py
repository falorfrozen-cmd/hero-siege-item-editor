"""The editor wired to game truth: tooltips, Item Forge base stats, the capture switch.

Game truth is switched on per test against a temporary folder and a fixed game
build; nothing reads the player's store, saves or game folder."""

import base64
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import game_truth as gt
import hs_item_editor_gui as editor

BUILD = "pe-6aaa6779-0cad4fc8"
BELT_KEY = "0-0-212409236228-8"
BELT_DATA = {"w": 1, "o": 1, "a": 107725, "b": 2, "j": 0, "c": 0}


def belt_record(**changes):
    record = {
        "v": 1, "src": "live", "build": BUILD, "t": 1790000000000, "ts": "212409236228", "type": 8,
        "hash": "abc", "def": {"a": 107725.0, "b": 2.0, "c": 0.0, "j": 0.0},
        "stats": {"154": 4.0, "10": [170, 10.0, 25.0, 0], "170": 18.0},
        "info": {"27": 1, "28": "Heavy Belt of Recovery", "5": "", "4": " of Recovery", "32": 1, "1": 9.0},
    }
    record.update(changes)
    return json.dumps(record) + "\n"


class GameTruthEditorTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.truth = self.folder / "itemtruth"
        (self.truth / "journal").mkdir(parents=True)
        patches = [
            mock.patch.object(editor, "GAME_TRUTH_ACTIVE", True),
            mock.patch.object(editor, "ITEM_TRUTH_DIR", self.truth),
            mock.patch.object(editor, "_TRUTH_STORE", None),
            mock.patch.object(editor, "_TRUTH_INGESTOR", None),
            mock.patch.object(editor, "_current_game_build", lambda: (BUILD, None)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        store = editor._TRUTH_STORE
        if store is not None:
            store.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def ingest(self, *lines):
        (self.truth / "journal" / "live.ndjson").write_text("".join(lines), encoding="utf-8")
        editor._truth_store().ingest_journal_dir(self.truth / "journal")

    def belt(self, data=None):
        return editor.resolve(BELT_KEY, dict(data or BELT_DATA))

    def test_the_game_record_replaces_the_replay(self):
        self.ingest(belt_record())
        model = editor._game_tooltip_model(self.belt())
        self.assertEqual(model["calculation"]["coverage"], "game_verified")
        self.assertTrue(model["calculation"]["numbersExact"])
        lines = {line["statKey"]: line for line in model["stats"]}
        self.assertEqual(lines[154]["value"], 4)
        self.assertEqual((lines[170]["value"], lines[170]["role"], lines[170]["minimum"], lines[170]["maximum"]),
                         (18, "affix", 10, 25))
        self.assertEqual((model["item"]["name"], model["item"]["rarity"], model["item"]["tier"]),
                         ("Heavy Belt of Recovery", "Common", "C"))

    def test_without_a_record_the_replay_is_an_estimate(self):
        model = editor._game_tooltip_model(self.belt())
        self.assertNotEqual(model["calculation"]["coverage"], "game_verified")
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertEqual(model["verification"]["status"], "estimate")

    def test_the_replay_keeps_its_promise_on_the_build_it_was_made_for(self):
        with mock.patch.object(editor, "_current_game_build", lambda: (gt.MODEL_BUILD_ID, None)):
            model = editor._game_tooltip_model(self.belt())
        self.assertTrue(model["calculation"]["numbersExact"])
        self.assertNotIn("verification", model)

    def test_a_reseeded_item_does_not_borrow_the_old_record(self):
        self.ingest(belt_record())
        model = editor._game_tooltip_model(self.belt({**BELT_DATA, "a": 107726}))
        self.assertNotEqual(model["calculation"]["coverage"], "game_verified")

    def test_game_truth_off_leaves_the_replay_untouched(self):
        self.ingest(belt_record())
        with mock.patch.object(editor, "GAME_TRUTH_ACTIVE", False):
            model = editor._game_tooltip_model(self.belt())
        self.assertNotIn("verification", model)
        self.assertNotEqual(model["calculation"]["coverage"], "game_verified")

    def test_item_forge_base_stats_are_the_games_values_before_any_dressing(self):
        self.ingest(belt_record(native={"154": 4.0, "170": 18.0}, stats={"154": 99.0, "170": 18.0}))
        stats, labels, source = editor._forge_base_stats(self.belt(), {"154", "170"}, BELT_KEY)
        self.assertEqual((stats, source), ({"154": 4, "170": 18}, "runtime"))

    def test_the_capture_switch_survives_a_restart(self):
        self.assertIn("err", editor.op_truth_capture({"on": "yes"}))
        off = editor.op_truth_capture({"on": False})
        self.assertIn("ok", off)
        self.assertFalse((self.truth / "capture.request").exists())
        self.assertTrue((self.truth / "capture.off").exists())
        with mock.patch.object(editor.game_truth.TruthIngestor, "start", lambda self: None):
            editor._start_game_truth()
        self.assertFalse((self.truth / "capture.request").exists(), "turned off stays off")
        on = editor.op_truth_capture({"on": True})
        self.assertIn("ok", on)
        self.assertTrue((self.truth / "capture.request").exists())
        self.assertFalse((self.truth / "capture.off").exists())
        self.assertTrue(on["status"]["capture"]["requested"])

    def test_status_reports_builds_and_counts(self):
        self.ingest(belt_record())
        status = editor.game_truth_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["store"]["records"], 1)
        self.assertEqual((status["currentBuildDate"], status["modelBuildDate"]), ("2026-09-16", "2026-08-28"))

    def test_the_status_route_is_served(self):
        self.assertIn('elif u.path == "/api/truth/status":', Path(editor.__file__).read_text(encoding="utf-8"))
        self.assertIn('elif path == "/api/truth/capture":', Path(editor.__file__).read_text(encoding="utf-8"))


class GameTruthCheckTests(unittest.TestCase):
    """Step 2: the editor asks the game to build the items it has not verified."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.truth = self.folder / "itemtruth"
        (self.truth / "journal").mkdir(parents=True)
        self.saves = self.folder / "hs2saves"
        self.saves.mkdir()
        inventory = {"equipped_items": {BELT_KEY: {"data": BELT_DATA}}}
        text = ('\nname="Tester"\nclass="1"\nlevel="10"\ninventory="'
                + base64.b64encode(json.dumps(inventory).encode("utf-8")).decode("ascii") + '"\n'
                + os.urandom(1500).hex())
        (self.saves / "herosiege0.hss").write_text(editor.encode_hss(text), encoding="ascii")
        stash = {"stash_tab_1": {"0-0-5-3": {"pos": [0, 0], "data": {"a": 6, "b": 1, "c": 1, "j": 7}}},
                 "stash_tab_data": {}}
        (self.saves / "stash.hss").write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")
        patches = [
            mock.patch.object(editor, "GAME_TRUTH_ACTIVE", True),
            mock.patch.object(editor, "ITEM_TRUTH_DIR", self.truth),
            mock.patch.object(editor, "SAVES", self.saves),
            mock.patch.object(editor, "VAULT_DB_FILE", self.folder / "no-vault.sqlite3"),
            mock.patch.object(editor, "_TRUTH_STORE", None),
            mock.patch.object(editor, "_TRUTH_COVERAGE", {"at": -1e9, "value": None}),
            mock.patch.object(editor, "_TRUTH_LAST_REQUEST", {"id": None, "keys": frozenset(), "scope": None, "items": 0}),
            mock.patch.object(editor, "_TRUTH_GIVEN_UP", set()),
            mock.patch.object(editor, "_current_game_build", lambda: (BUILD, None)),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        (self.truth / "journal" / "live.ndjson").write_text(belt_record(), encoding="utf-8")
        editor._truth_store().ingest_journal_dir(self.truth / "journal")

    def tearDown(self):
        store = editor._TRUTH_STORE
        if store is not None:
            store.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def live(self, updated_ms=None):
        (self.truth / "status.json").write_text(json.dumps(
            {"schema": 1, "forgepact": "1.4.6", "build": BUILD,
             "updated": updated_ms if updated_ms is not None else int(time.time() * 1000)}), encoding="utf-8")

    def request_lines(self, request_id):
        return (self.truth / "requests" / f"{request_id}.req").read_text(encoding="utf-8").splitlines()

    def test_every_owned_item_is_seen(self):
        found = [(place, key) for place, key, _ in editor._owned_item_payloads()]
        self.assertEqual(found, [("character 0", BELT_KEY), ("Shared Stash", "0-0-5-3")])

    def test_coverage_counts_what_the_game_verified(self):
        coverage = editor._truth_coverage(force=True)
        self.assertEqual(coverage["places"]["Characters"], {"items": 1, "verified": 1, "unmatchable": 0})
        self.assertEqual(coverage["places"]["Shared Stash"], {"items": 1, "verified": 0, "unmatchable": 0})
        self.assertEqual([key for key, _ in coverage["missing"]], ["0-0-5-3"])

    def test_a_check_asks_only_for_unverified_items(self):
        self.assertIn("err", editor.op_truth_verify({"scope": "missing"}), "capture off: refused")
        gt.request_capture(self.truth, editor_version="t")
        result = editor.op_truth_verify({"scope": "missing"})
        self.assertEqual(result["items"], 1)
        self.assertIn("as soon as Hero Siege runs", result["ok"])
        self.assertEqual([line.split("\t")[0] for line in self.request_lines(result["request"])], ["0-0-5-3"])
        self.assertIn("err", editor.op_truth_verify({"scope": "missing"}), "one check at a time")
        self.assertIn("err", editor.op_truth_verify({"scope": "everything"}))

    def test_checking_everything_includes_verified_items(self):
        gt.request_capture(self.truth, editor_version="t")
        self.live()
        result = editor.op_truth_verify({"scope": "all"})
        self.assertIn("now", result["ok"])
        self.assertEqual(sorted(line.split("\t")[0] for line in self.request_lines(result["request"])),
                         sorted([BELT_KEY, "0-0-5-3"]))

    def test_the_automatic_check_needs_the_game_and_never_repeats_a_stopped_one(self):
        gt.request_capture(self.truth, editor_version="t")
        self.assertIsNone(editor._truth_auto_check_once(), "no ForgePact status: the game is not running")
        self.live(updated_ms=int(time.time() * 1000) - 10 * 60 * 1000)
        self.assertIsNone(editor._truth_auto_check_once(), "a stale status is not a running game")
        self.live()
        stopped = self.truth / "requests"
        stopped.mkdir(exist_ok=True)
        (stopped / "1-x.stopped").write_text("x", encoding="utf-8")
        self.assertIsNone(editor._truth_auto_check_once(), "a stopped check waits for the player")
        editor.op_truth_clear_stopped({})
        request_id = editor._truth_auto_check_once()
        self.assertIsNotNone(request_id)
        self.assertEqual([line.split("\t")[0] for line in self.request_lines(request_id)], ["0-0-5-3"])

    def test_an_item_the_game_could_not_build_is_not_asked_about_again(self):
        gt.request_capture(self.truth, editor_version="t")
        self.live()
        request_id = editor._truth_auto_check_once()
        (self.truth / "requests" / f"{request_id}.req").unlink()   # the game claimed and finished it
        finished = json.dumps({"v": 1, "kind": "eval", "req": request_id, "build": BUILD, "t": 9, "total": 1,
                               "done": 1, "ok": 0, "failed": 1, "rejected": 0, "finished": True}) + "\n"
        with (self.truth / "journal" / "live.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(finished)
        editor._truth_store().ingest_journal_dir(self.truth / "journal")
        self.assertEqual(editor.game_truth_status()["evaluation"]["state"], "done")
        self.assertIsNone(editor._truth_auto_check_once())
        self.assertIn("0-0-5-3", editor._TRUTH_GIVEN_UP)

    def test_status_follows_a_check_from_queued_to_done(self):
        gt.request_capture(self.truth, editor_version="t")
        request_id = editor.op_truth_verify({"scope": "missing"})["request"]
        status = editor.game_truth_status()
        self.assertEqual(status["evaluation"]["state"], "waiting")
        self.assertEqual(status["coverage"]["missing"], 1)
        (self.truth / "requests" / f"{request_id}.req").rename(self.truth / "requests" / f"{request_id}.working")
        running = json.dumps({"v": 1, "kind": "eval", "req": request_id, "build": BUILD, "t": 5, "total": 1,
                              "done": 0, "ok": 0, "failed": 0, "rejected": 0, "finished": False}) + "\n"
        with (self.truth / "journal" / "live.ndjson").open("a", encoding="utf-8") as handle:
            handle.write(running)
        editor._truth_store().ingest_journal_dir(self.truth / "journal")
        self.assertEqual(editor.game_truth_status()["evaluation"]["state"], "running")


class GameTruthDefaultsTests(unittest.TestCase):
    def test_off_at_import_so_tests_and_scripts_never_read_the_players_store(self):
        self.assertFalse(editor.GAME_TRUTH_ACTIVE)
        self.assertIsNone(editor._truth_store())

    def test_main_turns_it_on_before_the_window_opens(self):
        source = Path(editor.__file__).read_text(encoding="utf-8")
        main = source[source.index("def main():"):]
        self.assertLess(main.index("_start_game_truth()"), main.index("native_window = _open_window(port)"))


if __name__ == "__main__":
    unittest.main()
