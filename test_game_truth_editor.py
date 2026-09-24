"""The editor wired to game truth: tooltips, Item Forge base stats, the capture switch.

Game truth is switched on per test against a temporary folder and a fixed game
build; nothing reads the player's store, saves or game folder."""

import base64
import json
import os
import shutil
import tempfile
import threading
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


class SavesFixture(unittest.TestCase):
    """A character wearing the belt the game verified, and a stash item it has not."""

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
            {"schema": 1, "forgepact": "1.4.5", "build": BUILD,
             "updated": updated_ms if updated_ms is not None else int(time.time() * 1000)}), encoding="utf-8")

    def request_lines(self, request_id):
        return (self.truth / "requests" / f"{request_id}.req").read_text(encoding="utf-8").splitlines()

    def second_stash_item(self):
        """A second item the game has not verified, "0-0-6-3", after the first."""
        stash = {"stash_tab_1": {"0-0-5-3": {"pos": [0, 0], "data": {"a": 6, "b": 1, "c": 1, "j": 7}},
                                 "0-0-6-3": {"pos": [1, 0], "data": {"a": 8, "b": 1, "c": 1, "j": 7}}},
                 "stash_tab_data": {}}
        (self.saves / "stash.hss").write_text(editor.encode_hss(json.dumps(stash)), encoding="ascii")

    def append_journal(self, *records):
        with (self.truth / "journal" / "live.ndjson").open("a", encoding="utf-8") as handle:
            handle.write("".join((record if isinstance(record, str) else json.dumps(record) + "\n")
                                 for record in records))
        editor._truth_store().ingest_journal_dir(self.truth / "journal")
        editor._TRUTH_COVERAGE.update(at=-1e9, value=None)

    @staticmethod
    def progress(request_id, kind="eval", done=0, finished=False):
        return {"v": 1, "kind": kind, "req": request_id, "build": BUILD, "t": 5, "total": 1, "done": done,
                "ok": done, "failed": 0, "rejected": 0, "finished": finished}

    def stop(self, request_id, folder="requests", ended_on_it=True):
        """The game took the request; the session then ended on it (a crash or a
        quit while it ran), or after the player went on playing."""
        records = [self.progress(request_id, "eval" if folder == "requests" else "tipdraw")]
        if not ended_on_it:
            records += [belt_record(ts="1", hash="play-1"), belt_record(ts="2", hash="play-2")]
        self.append_journal(*records)
        (self.truth / folder / f"{request_id}.req").rename(self.truth / folder / f"{request_id}.stopped")


class GameTruthCheckTests(SavesFixture):
    """Step 2: the editor asks the game to build the items it has not verified."""

    def test_every_owned_item_is_seen(self):
        found = [(place, key) for place, key, _ in editor._owned_item_payloads()]
        self.assertEqual(found, [("character 0", BELT_KEY), ("Shared Stash", "0-0-5-3")])

    def test_coverage_counts_what_the_game_verified(self):
        coverage = editor._truth_coverage(force=True)
        self.assertEqual(coverage["places"]["Characters"], {"items": 1, "verified": 1, "drawn": 0, "unmatchable": 0})
        self.assertEqual(coverage["places"]["Shared Stash"], {"items": 1, "verified": 0, "drawn": 0, "unmatchable": 0})
        self.assertEqual([key for key, _ in coverage["missing"]], ["0-0-5-3"])
        self.assertEqual([key for key, _ in coverage["undrawn"]], [BELT_KEY], "verified, not drawn yet")

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

    def test_clearing_a_stopped_check_strikes_the_item_it_stopped_on(self):
        # The pill promises: clearing lets checks run again, and an item that stops
        # the game again is skipped next time.
        gt.request_capture(self.truth, editor_version="t")
        self.live()
        first = editor._truth_auto_check_once()
        self.stop(first)
        self.assertIsNone(editor._truth_auto_check_once(), "a stopped check waits for the player")
        self.assertEqual(editor.game_truth_status()["evaluation"]["state"], "stopped")
        editor.op_truth_clear_stopped({})
        self.assertIsNone(editor.game_truth_status()["evaluation"], "the status line lets go of a cleared check")
        self.assertEqual(editor._truth_store().request_strikes(BUILD, "eval"), {"0-0-5-3": 1})
        second = editor._truth_auto_check_once()
        self.assertEqual([line.split("\t")[0] for line in self.request_lines(second)], ["0-0-5-3"],
                         "one strike: asked about again")
        self.stop(second)
        editor.op_truth_clear_stopped({})
        self.assertEqual(editor._truth_store().request_strikes(BUILD, "eval"), {"0-0-5-3": 2})
        editor._truth_auto_check_once()
        self.assertEqual(gt.request_files(self.truth)["waiting"], [], "two strikes: not asked about again")
        manual = editor.op_truth_verify({"scope": "missing"})
        self.assertIn("stopped the game twice", manual["ok"])

    def test_a_check_the_player_played_on_after_suspects_nothing(self):
        gt.request_capture(self.truth, editor_version="t")
        self.live()
        first = editor._truth_auto_check_once()
        self.stop(first, ended_on_it=False)
        editor.op_truth_clear_stopped({})
        self.assertEqual(editor._truth_store().request_strikes(BUILD, "eval"), {}, "an ordinary quit is no strike")
        second = editor._truth_auto_check_once()
        self.assertEqual([line.split("\t")[0] for line in self.request_lines(second)], ["0-0-5-3"])

    def test_a_click_and_the_automatic_check_never_queue_two_checks(self):
        gt.request_capture(self.truth, editor_version="t")
        self.live()
        original = editor._truth_coverage

        def slow(force=False):
            time.sleep(0.2)   # the 1-2 s a large Vault takes, shortened
            return original(force=force)

        def run(target, *args):
            try:
                target(*args)
            finally:
                editor._truth_store().close()   # this thread's own connection

        with mock.patch.object(editor, "_truth_coverage", slow):
            threads = [threading.Thread(target=run, args=(editor._truth_auto_check_once,)),
                       threading.Thread(target=run, args=(editor.op_truth_verify, {"scope": "missing"}))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        self.assertEqual(len(gt.request_files(self.truth)["waiting"]), 1)

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
        drawing = editor._truth_auto_check_once()
        self.assertIn("0-0-5-3", editor._TRUTH_GIVEN_UP)
        self.assertEqual(gt.request_files(self.truth)["waiting"], [], "no second check")
        self.assertEqual(gt.request_files(self.truth, "tipdraw")["waiting"], [drawing],
                         "with nothing left to check, the verified items go to be drawn")

    def test_items_past_the_size_cap_are_asked_about_next(self):
        # A Vault too big for one request: a finished check gives up only on the
        # items it asked about, and the rest go in the next request.
        self.second_stash_item()
        gt.request_capture(self.truth, editor_version="t")
        self.live()
        with mock.patch.object(gt, "MAX_REQUEST_ITEMS", 1):
            first = editor._truth_auto_check_once()
            self.assertEqual([line.split("\t")[0] for line in self.request_lines(first)], ["0-0-5-3"])
            (self.truth / "requests" / f"{first}.req").unlink()   # the game took it and could not build it
            self.append_journal({**self.progress(first, done=1, finished=True), "ok": 0, "failed": 1})
            second = editor._truth_auto_check_once()
        self.assertEqual(editor._TRUTH_GIVEN_UP, {"0-0-5-3"})
        self.assertEqual([line.split("\t")[0] for line in self.request_lines(second)], ["0-0-6-3"])

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


class GameTextDrawingTests(SavesFixture):
    """Step 3: the game draws the tooltips of verified items the player never hovers."""

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(editor, "_TRUTH_LAST_DRAWING", {"id": None, "items": 0, "keys": frozenset()}),
            mock.patch.object(editor, "_TRUTH_DRAW_GIVEN_UP", set()),
            mock.patch.object(editor, "_TRUTH_GIVEN_UP", {"0-0-5-3"}),   # the stash item never builds
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        gt.request_capture(self.truth, editor_version="t")
        self.live()

    def drawing_lines(self, request_id, suffix=".req"):
        return (self.truth / "tips" / f"{request_id}{suffix}").read_text(encoding="utf-8").splitlines()

    def test_verified_items_are_sent_to_be_drawn(self):
        request_id = editor._truth_auto_check_once()
        self.assertEqual([line.split("\t")[0] for line in self.drawing_lines(request_id)], [BELT_KEY])
        self.assertEqual(json.loads(self.drawing_lines(request_id)[0].split("\t", 1)[1]), BELT_DATA)
        status = editor.game_truth_status()
        self.assertEqual((status["drawing"]["state"], status["drawing"]["request"]), ("waiting", request_id))
        self.assertEqual(status["coverage"]["undrawn"], 1)
        self.assertIsNone(editor._truth_auto_check_once(), "one drawing request at a time")

    def test_a_drawn_item_is_not_asked_for_again(self):
        self.append_journal({"v": 1, "kind": "tooltip", "build": BUILD, "t": 7, "ts": "212409236228", "hash": "abc",
                             "req": "1-a", "args": [1, 2, 1, None],
                             "rows": [{"fn": "draw_text", "s": -1, "c": 16777215, "ha": 1, "a": [0, 8, "Heavy Belt"]}],
                             "stats": []})
        coverage = editor._truth_coverage(force=True)
        self.assertEqual(coverage["places"]["Characters"]["drawn"], 1)
        self.assertIsNone(editor._truth_auto_check_once())
        model = editor._game_tooltip_model(editor.resolve(BELT_KEY, dict(BELT_DATA)))
        self.assertEqual(model["gameText"]["rows"][0]["parts"][0]["text"], "Heavy Belt")
        self.assertEqual(model["calculation"]["textSource"], "game")

    def test_a_drawing_cut_short_strikes_the_item_it_stopped_on(self):
        first = editor._truth_auto_check_once()
        self.stop(first, "tips")
        second = editor._truth_auto_check_once()
        self.assertNotEqual(first, second)
        self.assertFalse((self.truth / "tips" / f"{first}.stopped").exists(), "a stopped drawing is cleared on its own")
        self.assertEqual(editor._truth_store().request_strikes(BUILD, "tipdraw"), {BELT_KEY: 1})
        self.assertEqual([line.split("\t")[0] for line in self.drawing_lines(second)], [BELT_KEY],
                         "one strike: asked for again")
        self.stop(second, "tips")
        self.assertIsNone(editor._truth_auto_check_once(), "two strikes: not asked for again on this build")
        self.assertEqual(editor._truth_store().request_strikes(BUILD, "tipdraw"), {BELT_KEY: 2})

    def test_a_drawing_paused_before_an_ordinary_quit_strikes_nothing(self):
        # Drawing runs only while a tooltip is open: the player closes it, plays
        # on and quits, and the request comes back stopped with no one to blame.
        first = editor._truth_auto_check_once()
        self.stop(first, "tips", ended_on_it=False)
        second = editor._truth_auto_check_once()
        self.assertEqual(editor._truth_store().request_strikes(BUILD, "tipdraw"), {})
        self.assertEqual([line.split("\t")[0] for line in self.drawing_lines(second)], [BELT_KEY])

    def test_a_drawing_the_editor_cannot_tie_is_not_asked_for_again(self):
        request_id = editor._truth_auto_check_once()
        (self.truth / "tips" / f"{request_id}.req").unlink()   # the game claimed it and drew it
        self.append_journal(
            {"v": 1, "kind": "tipdraw", "req": request_id, "build": BUILD, "t": 6, "total": 1, "done": 0, "ok": 0,
             "failed": 0, "rejected": 0, "finished": False},   # not right after the item's record: no tie
            {"v": 1, "kind": "tooltip", "build": BUILD, "t": 7, "ts": "212409236228", "hash": "another",
             "req": request_id, "args": [], "rows": [{"fn": "draw_text", "s": -1, "c": 0, "ha": 1, "a": [0, 8, "x"]}],
             "stats": []},
            {"v": 1, "kind": "tipdraw", "req": request_id, "build": BUILD, "t": 8, "total": 1, "done": 1, "ok": 1,
             "failed": 0, "rejected": 0, "finished": True})
        self.assertIsNone(editor._truth_auto_check_once(), "drawn but not tied to its record: once is enough")
        self.assertIn(BELT_KEY, editor._TRUTH_DRAW_GIVEN_UP)
        self.assertEqual(gt.request_files(self.truth, "tipdraw")["waiting"], [])

    def test_items_past_the_size_cap_are_drawn_next(self):
        # The stash item is verified too; a drawing request has room for one item.
        self.append_journal(belt_record(**{"ts": "5", "type": 3, "hash": "stash",
                                           "def": {"a": 6.0, "b": 1.0, "c": 1.0, "j": 7.0}}))
        with mock.patch.object(gt, "MAX_REQUEST_ITEMS", 1):
            first = editor._truth_auto_check_once()
            self.assertEqual([line.split("\t")[0] for line in self.drawing_lines(first)], [BELT_KEY])
            (self.truth / "tips" / f"{first}.req").unlink()   # the game took it; nothing drawn could be tied
            self.append_journal(self.progress(first, "tipdraw", done=1, finished=True))
            second = editor._truth_auto_check_once()
        self.assertEqual(editor._TRUTH_DRAW_GIVEN_UP, {BELT_KEY})
        self.assertEqual([line.split("\t")[0] for line in self.drawing_lines(second)], ["0-0-5-3"])

    def test_a_drawing_is_tied_to_its_item_even_under_another_hash(self):
        request_id = editor._truth_auto_check_once()
        (self.truth / "tips" / f"{request_id}.req").unlink()
        rebuilt = json.loads(belt_record(hash="rebuilt"))   # the same content, a new hash
        self.append_journal(
            rebuilt,
            {"v": 1, "kind": "tooltip", "build": BUILD, "t": 7, "ts": "212409236228", "hash": "rebuilt",
             "req": request_id, "args": [],
             "rows": [{"fn": "draw_text", "s": -1, "c": 0, "ha": 1, "a": [0, 8, "Heavy Belt of Recovery"]}],
             "stats": []})
        self.assertEqual(editor._truth_coverage(force=True)["places"]["Characters"]["drawn"], 1)
        model = editor._game_tooltip_model(editor.resolve(BELT_KEY, dict(BELT_DATA)))
        self.assertEqual(model["gameText"]["rows"][0]["parts"][0]["text"], "Heavy Belt of Recovery")

    def test_status_follows_a_drawing(self):
        request_id = editor._truth_auto_check_once()
        (self.truth / "tips" / f"{request_id}.req").rename(self.truth / "tips" / f"{request_id}.working")
        self.append_journal({"v": 1, "kind": "tipdraw", "req": request_id, "build": BUILD, "t": 5, "total": 1,
                             "done": 0, "ok": 0, "failed": 0, "rejected": 0, "finished": False})
        drawing = editor.game_truth_status()["drawing"]
        self.assertEqual((drawing["state"], drawing["progress"]["total"]), ("running", 1))
        (self.truth / "tips" / f"{request_id}.working").unlink()
        self.append_journal({"v": 1, "kind": "tipdraw", "req": request_id, "build": BUILD, "t": 6, "total": 1,
                             "done": 1, "ok": 1, "failed": 0, "rejected": 0, "finished": True})
        self.assertEqual(editor.game_truth_status()["drawing"]["state"], "done")


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
