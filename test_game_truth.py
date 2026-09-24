"""Game truth: the game's own finished items behind the editor's tooltips.

Everything runs on temporary folders; nothing reads the player's saves, Vault,
AFK spool or ForgePact journal."""

import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import game_truth as gt

BUILD = "pe-6aaa6779-0cad4fc8"
OLD_BUILD = "pe-6a9ed3ee-0cadcc08"


def pe_header(stamp: int, sections: list[tuple[bytes, int]]) -> bytes:
    data = bytearray(0x1000)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x86, len(sections))
    struct.pack_into("<I", data, 0x88, stamp)
    struct.pack_into("<H", data, 0x94, 0xF0)
    at = 0x80 + 24 + 0xF0
    for name, size in sections:
        data[at:at + 8] = name.ljust(8, b"\0")
        struct.pack_into("<I", data, at + 8, size)
        at += 40
    return bytes(data)


def journal_line(ts="212409236228", *, build=BUILD, t=1790000000000, definition=None, stats=None,
                 info=None, native=None, item_type=8, src="live"):
    record = {
        "v": 1, "src": src, "build": build, "t": t, "ts": ts, "type": item_type, "hash": "h" + ts,
        "def": definition if definition is not None else {"a": 107725.0, "b": 2.0, "c": 0.0, "j": 0.0},
        "stats": stats if stats is not None else {"154": 4.0, "10": [51, 1.0, 3.0, 0], "51": 3.0},
        "info": info if info is not None else {"27": 1, "28": "Sturdy Heavy Belt", "5": "Sturdy ", "4": "",
                                              "32": 2, "1": 12.0, "13": "@ref sound(x)"},
    }
    if native is not None:
        record["native"] = native
    return json.dumps(record) + "\n"


class FakeSemantics:
    """The two StatSemanticsDatabase methods game truth uses."""

    STATS = {
        154: {"name": "Defense", "valueKind": "flat",
              "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "0x1564EC1: INT 154"}},
        51: {"name": "to All Attributes", "valueKind": "flat",
             "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "stat block at 0x1641305"}},
        28: {"name": "Enhanced Damage", "valueKind": "percent",
             "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "stat block at 0x15CDCA7"}},
        292: {"name": "Attacks can hit multiple enemies", "valueKind": "boolean",
              "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "flag line at 0x16809A6"}},
        116: {"name": "Chance when Striking: Skill", "valueKind": "skill_id",
              "evidence": {"function": "gml_Script_GetItemTooltipString", "location": "block at 0x3EA9496"}},
        21: {"name": "All Skills: Class", "valueKind": "class_id",
             "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "0x15C2FB6: INT 21"}},
        1: {"name": "unknown", "valueKind": "unknown", "evidence": {"function": None}},
    }

    def get(self, key):
        return self.STATS.get(int(key))

    def talent(self, talent_id):
        return {"name": "Frost Nova", "slug": "frost_nova"} if talent_id == 182 else None

    def pickers(self):
        return {"talents": [], "classes": [{"id": 3, "label": "Viking"}]}


class BuildIdTests(unittest.TestCase):
    def test_link_stamp_and_text_size_survive_the_aurie_section(self):
        patched = pe_header(0x6AAA6779, [(b".text", 0x0CAD4FC8), (b".rdata", 0x10), (b".aurie", 0x45000)])
        clean = pe_header(0x6AAA6779, [(b".text", 0x0CAD4FC8), (b".rdata", 0x10)])
        self.assertEqual(gt.build_id_from_headers(patched), BUILD)
        self.assertEqual(gt.build_id_from_headers(clean), BUILD)

    def test_broken_headers_have_no_id(self):
        good = pe_header(0x6AAA6779, [(b".text", 0x0CAD4FC8)])
        self.assertIsNone(gt.build_id_from_headers(b"XY" + good[2:]))
        self.assertIsNone(gt.build_id_from_headers(good[:0x90]))
        self.assertIsNone(gt.build_id_from_headers(pe_header(0x6AAA6779, [(b".data", 1)])))

    def test_exe_file_is_read_once_per_version(self):
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "Hero_Siege.exe"
            exe.write_bytes(pe_header(0x6A91A8B3, [(b".text", 0x0CAF38B8)]) + b"\0" * 100)
            self.assertEqual(gt.build_id_of_exe(exe), gt.MODEL_BUILD_ID)
            self.assertIsNone(gt.build_id_of_exe(Path(folder) / "missing.exe"))
            self.assertIsNone(gt.build_id_of_exe(None))

    def test_build_date_reads_the_link_stamp(self):
        self.assertEqual(gt.build_date(BUILD), "2026-09-16")
        self.assertEqual(gt.build_date(gt.MODEL_BUILD_ID), "2026-08-28")
        self.assertIsNone(gt.build_date("unknown"))


class IdentityTests(unittest.TestCase):
    def test_placement_fields_and_number_spelling_do_not_matter(self):
        record = {"a": 107725.0, "b": 2.0, "c": 0.0, "j": 0.0}
        saved = {"w": 1, "g": 8.0, "a": 107725, "b": 2, "c": 0, "j": 0, "zz": {"sockets": 2}}
        self.assertIsNone(gt.definition_mismatch(record, saved))

    def test_a_single_item_is_the_same_with_or_without_m_and_o(self):
        self.assertIsNone(gt.definition_mismatch({"a": 1, "b": 2, "c": 1}, {"a": 1, "b": 2, "c": 1, "m": 1.0}))
        self.assertIsNone(gt.definition_mismatch({"a": 1, "b": 2, "c": 0}, {"a": 1, "b": 2, "c": 0, "o": 1.0}))
        self.assertEqual(gt.definition_mismatch({"a": 1, "b": 16, "c": 0, "o": 3}, {"a": 1, "b": 16, "c": 0, "o": 4}), "o")

    def test_a_new_seed_or_socket_is_another_item(self):
        base = {"a": 107725, "b": 2, "c": 0, "j": 0}
        self.assertEqual(gt.definition_mismatch(base, {**base, "a": 107726}), "a")
        self.assertEqual(gt.definition_mismatch(base, {**base, "s1": "eyJhIjoxfQ=="}), "s1")
        self.assertEqual(gt.definition_mismatch({**base, "p": 5}, base), "p")

    def test_a_socket_is_the_same_as_text_in_the_save_and_as_a_struct_in_the_game(self):
        # Measured on a live record: the save keeps base64 JSON, the game the decoded struct.
        saved = {"a": 424666.0, "b": 34.0, "c": 1.0, "j": 0.0, "m": 1.0, "p": 5.0, "r": 0.0, "i": 200500036.0,
                 "s1": "eyJhIjoyOTg1MzczNzksImIiOjM5LCJuIjowfQ==", "unset": ["s2"], "w": 1.0}
        game = {"a": 424666, "b": 34, "c": 1, "g": 1, "i": 200500036, "j": 0, "m": 1, "p": 5, "r": 0,
                "s1": {"a": 298537379, "b": 39, "n": 0}, "unset": ["s2"], "w": 1, "zz": {"sockets": 4}}
        self.assertIsNone(gt.definition_mismatch(game, saved))
        other_gem = dict(game, s1={"a": 298537379, "b": 40, "n": 0})
        self.assertEqual(gt.definition_mismatch(other_gem, saved), "s1")
        self.assertEqual(gt.definition_mismatch(game, dict(saved, s1="not base64")), "s1")

    def test_ignored_fields(self):
        self.assertIsNone(gt.definition_mismatch({"a": 1, "o": 30}, {"a": 1, "o": 999}, ignore=("o",)))

    def test_timestamp_of_key(self):
        self.assertEqual(gt.timestamp_of_key("0-0-212409236228-8"), "212409236228")
        self.assertIsNone(gt.timestamp_of_key("0-0-0--1"), "the Vault's placeholder key has no timestamp")
        self.assertIsNone(gt.timestamp_of_key("bad"))
        self.assertIsNone(gt.timestamp_of_key(None))

    def test_info_keeps_text_and_numbers_only(self):
        info = {"28": "Name", "27": 1, "13": "@ref sound(x)", "52": ["@ref sound(y)"], "2": 2.0}
        self.assertEqual(gt.filter_info(info), {"28": "Name", "27": 1, "2": 2})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.store = gt.TruthStore(self.folder / "truth.sqlite3")
        self.journal = self.folder / "journal"
        self.journal.mkdir()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_journal_is_read_once_and_a_half_written_line_waits(self):
        path = self.journal / "live-a.ndjson"
        path.write_text(journal_line() + journal_line(ts="2")[:40], encoding="utf-8")
        first = self.store.ingest_journal_dir(self.journal)
        self.assertEqual((first.lines, first.added), (1, 1))
        with path.open("w", encoding="utf-8") as handle:
            handle.write(journal_line() + journal_line(ts="2"))
        second = self.store.ingest_journal_dir(self.journal)
        self.assertEqual((second.lines, second.added), (1, 1))
        self.assertEqual(self.store.ingest_journal_dir(self.journal).files, 0)
        self.assertEqual(self.store.counts()["records"], 2)

    def test_the_same_record_twice_is_stored_once(self):
        (self.journal / "a.ndjson").write_text(journal_line(), encoding="utf-8")
        (self.journal / "b.ndjson").write_text(journal_line(), encoding="utf-8")
        self.assertEqual(self.store.ingest_journal_dir(self.journal).added, 1)

    def test_bad_lines_are_skipped(self):
        (self.journal / "a.ndjson").write_text(
            "not json\n" + json.dumps({"v": 2}) + "\n" + json.dumps({"v": 1, "ts": "x"}) + "\n" + journal_line(),
            encoding="utf-8",
        )
        result = self.store.ingest_journal_dir(self.journal)
        self.assertEqual((result.added, result.skipped), (1, 3))

    def test_a_replaced_shorter_file_is_read_from_the_start(self):
        path = self.journal / "a.ndjson"
        path.write_text(journal_line() + journal_line(ts="2"), encoding="utf-8")
        self.store.ingest_journal_dir(self.journal)
        path.write_text(journal_line(ts="3"), encoding="utf-8")
        self.assertEqual(self.store.ingest_journal_dir(self.journal).added, 1)

    def test_lookup_prefers_the_running_build_and_the_newest_record(self):
        (self.journal / "a.ndjson").write_text(
            journal_line(build=OLD_BUILD, t=3, stats={"154": 1.0})
            + journal_line(build=BUILD, t=1, stats={"154": 2.0})
            + journal_line(build=BUILD, t=2, stats={"154": 3.0}),
            encoding="utf-8",
        )
        self.store.ingest_journal_dir(self.journal)
        data = {"a": 107725.0, "b": 2.0, "c": 0.0, "j": 0.0, "w": 1.0}
        match = self.store.lookup("0-0-212409236228-8", data, current_build=BUILD)
        self.assertEqual((match.build_status, match.record["stats"]), ("current", {"154": 3}))
        older = self.store.lookup("0-0-212409236228-8", data, current_build="pe-00000001-00000001")
        self.assertEqual((older.build_status, older.record["stats"]), ("other_build", {"154": 1}))
        unknown = self.store.lookup("0-0-212409236228-8", data, current_build=None)
        self.assertEqual(unknown.build_status, "unknown_build")

    def test_lookup_never_answers_for_another_item(self):
        (self.journal / "a.ndjson").write_text(journal_line(), encoding="utf-8")
        self.store.ingest_journal_dir(self.journal)
        reseeded = {"a": 999.0, "b": 2.0, "c": 0.0, "j": 0.0}
        self.assertIsNone(self.store.lookup("0-0-212409236228-8", reseeded, current_build=BUILD))
        self.assertIsNone(self.store.lookup("0-0-212409236228-3", {"a": 107725, "b": 2, "c": 0, "j": 0},
                                            current_build=BUILD), "another item class")
        self.assertIsNone(self.store.lookup("0-0-1-8", {"a": 107725}, current_build=BUILD))
        explained = self.store.explain("0-0-212409236228-8", reseeded)
        self.assertEqual([row["mismatch"] for row in explained], ["a"])

    def test_a_stack_matches_whatever_its_count(self):
        (self.journal / "a.ndjson").write_text(
            journal_line(definition={"a": 5, "b": 27, "c": 0, "j": 0, "o": 12}, item_type=14), encoding="utf-8")
        self.store.ingest_journal_dir(self.journal)
        data = {"a": 5, "b": 27, "c": 0, "j": 0, "o": 999}
        self.assertIsNone(self.store.lookup("0-0-212409236228-14", data, current_build=BUILD))
        self.assertIsNotNone(self.store.lookup("0-0-212409236228-14", data, current_build=BUILD, ignore=("o",)))

    def test_spool_records_take_the_build_of_the_exe_they_were_made_after(self):
        spool = self.folder / "spool"
        spool.mkdir()
        item = {"itemTimeStamp": 212408973073.0, "itemType": 3.0, "itemDataHash": "abc",
                "itemDefinitionStruct": {"m": 1.0, "b": 11.0, "a": 154989853.0, "j": 6.0, "c": 1.0},
                "itemStatStruct": {"23": 1.5, "20": 2.0}, "itemInfoStruct": {"28": "Lance", "27": 6}}
        lines = [
            {"expedition_id": "e", "seq": 1, "kind": "item", "t": "2026-09-19T10:00:00Z", "item": item},
            {"expedition_id": "e", "seq": 2, "kind": "item", "t": "2026-09-22T10:00:00Z",
             "item": {**item, "itemTimeStamp": 212408973074.0}},
            {"expedition_id": "e", "seq": 3, "kind": "sale", "t": "2026-09-22T10:00:00Z"},
        ]
        (spool / "farm_x_claim.ndjson").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        (spool / "codex_audit_ignored.ndjson").write_text(json.dumps(lines[0]) + "\n", encoding="utf-8")
        since = time.mktime(time.strptime("2026-09-20 02:27", "%Y-%m-%d %H:%M"))
        result = self.store.ingest_spool_dir(spool, current_build=BUILD, build_since=since)
        self.assertEqual((result.files, result.added, result.skipped), (1, 2, 1))
        self.assertEqual(self.store.counts()["builds"], {"unknown": 1, BUILD: 1})
        vault = {"w": 1.0, "a": 154989853.0, "j": 6.0, "b": 11.0, "c": 1.0, "m": 1.0}
        newer = self.store.lookup("0-0-212408973074-3", vault, current_build=BUILD)
        self.assertEqual((newer.build_status, newer.record["source"]), ("current", "spool"))
        older = self.store.lookup("0-0-212408973073-3", vault, current_build=BUILD)
        self.assertEqual(older.build_status, "unknown_build")

    def test_prune_removes_only_old_fully_read_journals(self):
        old = time.time() - 10 * 86400
        done = self.journal / "done.ndjson"
        done.write_text(journal_line(), encoding="utf-8")
        os.utime(done, (old, old))
        fresh = self.journal / "fresh.ndjson"
        fresh.write_text(journal_line(ts="5"), encoding="utf-8")
        self.store.ingest_journal_dir(self.journal)
        unread = self.journal / "unread.ndjson"
        unread.write_text(journal_line(ts="6"), encoding="utf-8")
        os.utime(unread, (old, old))
        self.assertEqual(self.store.prune_journal_dir(self.journal), 1)
        self.assertFalse(done.exists())
        self.assertTrue(fresh.exists() and unread.exists())


class VerifiedModelTests(unittest.TestCase):
    def match(self, stats, info=None, status="current", native=None):
        record = {"ts": "1", "type": 8, "build": BUILD if status == "current" else OLD_BUILD, "source": "live",
                  "recordedAt": 1790000000000, "hash": "h", "def": {}, "stats": stats,
                  "native": native, "info": info or {}}
        return gt.TruthMatch(record, status, BUILD)

    def offline(self):
        return {
            "schemaVersion": 1, "profileId": "normal:8:0:2", "fingerprint": "F",
            "item": {"name": "Heavy Belt", "canonicalName": "Heavy Belt", "customName": None, "rarity": "Normal",
                     "tier": None, "requiredLevel": 10},
            "seeds": {"a": 107725}, "sockets": [], "identities": [],
            "stats": [{"statKey": 154, "label": "Defense", "value": 3, "minimum": 1, "maximum": 4,
                       "percent": False, "catalogLineIndex": 0, "sourceRange": "1-4"},
                      {"statKey": 28, "label": "Enhanced Damage", "value": 765, "minimum": 720, "maximum": 830,
                       "percent": True}],
            "calculation": {"coverage": "exact_numbers", "numbersExact": True, "warnings": []},
        }

    def test_every_number_comes_from_the_game_with_its_affixes(self):
        model = gt.build_verified_model(
            self.offline(),
            self.match({"154": 4.0, "10": [51, 1.0, 3.0, 2], "51": 3.0, "28": 773.0, "447": 40.0, "1": 4.0,
                        "292": 1.0},
                       info={"27": 2, "28": "Sturdy Heavy Belt of Fox", "5": "Sturdy ", "4": " of Fox", "32": 3,
                             "1": 12.0}),
            semantics=FakeSemantics(),
        )
        lines = {line["statKey"]: line for line in model["stats"]}
        self.assertEqual(lines[154]["formattedValue"], "4")
        self.assertEqual((lines[154]["minimum"], lines[154]["maximum"]), (1, 4))
        self.assertEqual(lines[28]["formattedValue"], "773%")
        self.assertEqual(lines[51]["role"], "affix")
        self.assertEqual((lines[51]["minimum"], lines[51]["maximum"], lines[51]["affixTier"]), (1, 3, 2))
        self.assertEqual(lines[292]["formattedValue"], "")
        self.assertNotIn(447, lines)
        self.assertNotIn(1, lines)
        self.assertEqual({line["statKey"] for line in model["internalStats"]}, {447, 1})
        self.assertEqual([line["statKey"] for line in model["stats"]], [154, 28, 51, 292], "the game's draw order")
        item = model["item"]
        self.assertEqual((item["name"], item["rarity"], item["tier"], item["requiredLevel"]),
                         ("Sturdy Heavy Belt of Fox", "Superior", "A", 12))
        self.assertEqual((item["prefix"], item["suffix"], item["baseName"]), ("Sturdy ", " of Fox", "Heavy Belt"))
        self.assertEqual(model["calculation"]["coverage"], "game_verified")
        self.assertTrue(model["calculation"]["numbersExact"])
        self.assertEqual(model["rollQuality"]["maxed"], 2, "Defense 4/4 and the affix 3/3; 773 of 830 is not")
        self.assertEqual(model["rollQuality"]["total"], 3)
        differences = {d["statKey"]: d for d in model["verification"]["estimateDifferences"]}
        self.assertEqual((differences[154]["estimate"], differences[154]["game"]), (3, 4))
        self.assertEqual((differences[28]["estimate"], differences[28]["game"]), (765, 773))
        self.assertEqual(model["verification"]["affixCount"], 1)

    def test_a_value_beyond_its_range_shows_no_range(self):
        # A 5-star item: the game scales Defense past the definition's 1-4.
        model = gt.build_verified_model(self.offline(), self.match({"154": 5.0, "28": 800.0}),
                                        semantics=FakeSemantics())
        lines = {line["statKey"]: line for line in model["stats"]}
        self.assertEqual((lines[154]["minimum"], lines[154]["maximum"], lines[154]["rolled"]), (None, None, False))
        self.assertTrue(lines[154]["beyondRange"])
        self.assertEqual((lines[28]["minimum"], lines[28]["maximum"]), (720, 830))
        self.assertNotIn("beyondRange", lines[28])
        self.assertEqual(model["rollQuality"]["total"], 1, "only in-range rolls count")

    def test_skill_and_class_ids_read_as_names(self):
        model = gt.build_verified_model(self.offline(), self.match({"116": 182.0, "21": 3.0}),
                                        semantics=FakeSemantics())
        values = {line["statKey"]: line["formattedValue"] for line in model["stats"]}
        self.assertEqual(values, {116: "Frost Nova", 21: "Viking"})

    def test_a_record_from_another_build_is_shown_but_not_promised(self):
        model = gt.build_verified_model(self.offline(), self.match({"154": 4.0}, status="other_build"))
        self.assertFalse(model["calculation"]["numbersExact"])
        self.assertIn("2026-09-07", model["calculation"]["warnings"][0])
        self.assertEqual(model["verification"]["status"], "other_build")

    def test_a_custom_name_stays_on_top(self):
        model = gt.build_verified_model(self.offline(), self.match({"154": 4.0}, info={"28": "Belt"}),
                                        custom_name="My belt")
        self.assertEqual((model["item"]["name"], model["item"]["customName"], model["item"]["canonicalName"]),
                         ("My belt", "My belt", "Belt"))

    def test_the_replay_is_an_estimate_on_any_other_build(self):
        offline = self.offline()
        same = gt.mark_estimate(offline, current_build=gt.MODEL_BUILD_ID)
        self.assertIs(same, offline)
        estimate = gt.mark_estimate(offline, current_build=BUILD)
        self.assertEqual(estimate["calculation"]["coverage"], "estimate")
        self.assertFalse(estimate["calculation"]["numbersExact"])
        self.assertIn("2026-08-28", estimate["calculation"]["warnings"][0])
        self.assertIn("2026-09-16", estimate["calculation"]["warnings"][0])
        self.assertTrue(offline["calculation"]["numbersExact"], "the input model is not changed")
        unknown = gt.mark_estimate(offline, current_build=None)
        self.assertIn("could not identify", unknown["calculation"]["warnings"][0])
        catalog = {"calculation": {"coverage": "catalog_only", "numbersExact": False}}
        self.assertIs(gt.mark_estimate(catalog, current_build=BUILD), catalog)


class EvaluationRequestTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_request_holds_one_key_and_save_payload_per_line(self):
        entries = [
            ("0-0-212409236228-8", {"a": 107725, "b": 2, "c": 0, "j": 0, "name": "Kılıç"}),
            ("0-0-212409236228-8", {"a": 107725, "b": 2, "c": 0, "j": 0, "name": "Kılıç"}),
            ("0-0-0--1", {"a": 1}),
            ("0-0-5-3", "not a dict"),
            ("0-0-6-3", {"a": 6, "b": 1, "c": 1, "j": 7, "s1": "eyJhIjoxfQ=="}),
        ]
        request_id, count = gt.write_eval_request(self.folder, entries)
        self.assertEqual(count, 2, "duplicates, placeholder keys and junk are left out")
        self.assertRegex(request_id, r"^\d{13}-[0-9a-f]{6}$")
        path = self.folder / "requests" / f"{request_id}.req"
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split("\t")[0], "0-0-212409236228-8")
        self.assertEqual(json.loads(lines[0].split("\t", 1)[1])["name"], "Kılıç")
        self.assertTrue(lines[0].isascii(), "the game's json_parse reads \\u escapes")
        self.assertEqual(lines[1].split("\t")[0], "0-0-6-3")
        self.assertFalse(list((self.folder / "requests").glob("*.tmp")))
        self.assertEqual(gt.request_files(self.folder)["waiting"], [request_id])

    def test_nothing_to_ask_writes_nothing(self):
        self.assertEqual(gt.write_eval_request(self.folder, []), (None, 0))
        self.assertFalse((self.folder / "requests").exists())

    def test_request_states_follow_the_file_names(self):
        folder = self.folder / "requests"
        folder.mkdir()
        for name in ("1-a.req", "2-b.working", "3-c.stopped", "4-d.tmp", "bad id.req"):
            (folder / name).write_text("x", encoding="utf-8")
        self.assertEqual(gt.request_files(self.folder), {"waiting": ["1-a"], "running": ["2-b"], "stopped": ["3-c"]})
        self.assertEqual(gt.clear_stopped_requests(self.folder), 1)
        self.assertEqual(gt.request_files(self.folder)["stopped"], [])

    def test_progress_lines_are_kept_per_request_and_never_go_back(self):
        store = gt.TruthStore(self.folder / "truth.sqlite3")
        journal = self.folder / "journal"
        journal.mkdir()
        progress = lambda **kw: json.dumps({"v": 1, "kind": "eval", "req": "17-ab", "build": BUILD, "t": 1,
                                            "total": 3, "done": 0, "ok": 0, "failed": 0, "rejected": 1,
                                            "finished": False, **kw}) + "\n"
        (journal / "live.ndjson").write_text(
            progress() + journal_line(src="eval") + progress(done=3, ok=2, failed=1, finished=True, t=5)
            + progress(done=1, t=3), encoding="utf-8")
        result = store.ingest_journal_dir(journal)
        self.assertEqual(result.added, 1, "the evaluated item is an ordinary record")
        self.assertEqual(result.skipped, 0, "progress lines are not skipped records")
        evaluation = store.evaluation("17-ab")
        self.assertEqual((evaluation["done"], evaluation["ok"], evaluation["failed"], evaluation["finished"]),
                         (3, 2, 1, True))
        self.assertIsNone(store.evaluation("unknown"))
        self.assertEqual(store.counts()["sources"], {"eval": 1})
        store.close()


class CaptureTests(unittest.TestCase):
    def test_request_is_idempotent_and_withdrawable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "itemtruth"
            self.assertTrue(gt.request_capture(root, editor_version="9"))
            first = (root / "capture.request").read_text(encoding="utf-8")
            self.assertTrue(gt.request_capture(root, editor_version="10"))
            self.assertEqual((root / "capture.request").read_text(encoding="utf-8"), first)
            self.assertTrue(gt.capture_status(root)["requested"])
            self.assertTrue(gt.withdraw_capture(root))
            self.assertTrue(gt.withdraw_capture(root))
            self.assertFalse(gt.capture_status(root)["requested"])

    def test_forgepact_status_counts_as_live_for_two_minutes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = time.time()
            (root / "status.json").write_text(json.dumps(
                {"schema": 1, "forgepact": "1.4.6", "build": BUILD, "updated": int(now * 1000) - 5000,
                 "written": 12}), encoding="utf-8")
            status = gt.capture_status(root, now=now)
            self.assertTrue(status["reporting"])
            self.assertEqual(status["forgepact"]["written"], 12)
            self.assertFalse(gt.capture_status(root, now=now + 600)["reporting"])


class ForgePactRecordFormatTests(unittest.TestCase):
    """The journal ForgePact writes (tests/item_truth_harness.cpp builds the same
    shape) is what this module reads."""

    def test_harness_record_shape_parses(self):
        line = ('{"v":1,"src":"live","build":"pe-6aaa6779-0cad4fc8","t":1790000000000,"ts":"212409236228",'
                '"type":8,"hash":"d2af\\"x\\\\","def":{"a":107725.0,"b":2.0,"c":0.0,"j":0.0,"o":1.0},'
                '"stats":{"154":4.0,"10":[51,1.0,3.0,0],"51":3.0},"info":{"27":1,"28":"Heavy Belt","5":"","4":""}}')
        parsed = gt._parse_journal_record(json.loads(line))
        self.assertEqual((parsed["ts"], parsed["build"], parsed["recorded_at"]), ("212409236228", BUILD, 1790000000000))
        self.assertEqual(parsed["hash_text"], 'd2af"x\\')


class EmbeddedTooltipTests(unittest.TestCase):
    """The tooltip renderer in the editor page shows where its numbers come from."""

    @classmethod
    def setUpClass(cls):
        import hs_item_editor_gui as editor
        cls.html = editor.HTML

    def render(self, model):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is not installed; embedded JS harness skipped")
        parts = []
        for pattern in (r"function esc\(s\)\{[^\n]+", r"const TOOLTIP_RARITIES=[^\n]+",
                        r"function tooltipRarityClass\([^\n]+", r"function tooltipLineKey\(line,index\)\{.*?\n\}",
                        r"function renderGameTooltip\(model,options=\{\}\)\{.*?\n\}"):
            found = re.search(pattern, self.html, re.DOTALL)
            self.assertIsNotNone(found, pattern)
            parts.append(found.group(0))
        script = "\n".join(parts) + "\nconsole.log(renderGameTooltip(JSON.parse(require('fs').readFileSync(0,'utf8'))));"
        result = subprocess.run([node, "-e", script], input=json.dumps(model), encoding="utf-8", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_verified_tooltip(self):
        model = gt.build_verified_model(
            VerifiedModelTests().offline(),
            VerifiedModelTests().match({"154": 4.0, "10": [51, 1.0, 3.0, 2], "51": 3.0},
                                       info={"27": 2, "28": "Sturdy Heavy Belt"}),
            semantics=FakeSemantics(),
        )
        html = self.render(model)
        self.assertIn("Game verified", html)
        self.assertIn('class="gtt-title r-Superior">Sturdy Heavy Belt<', html)
        self.assertIn('class="gtt-stat gtt-affix"><b>3</b><span>to All Attributes<small class="gtt-range">(1–3)</small>', html)
        self.assertIn("GAME VERIFIED", html)

    def test_estimate_tooltip(self):
        model = gt.mark_estimate(VerifiedModelTests().offline(), current_build=BUILD)
        html = self.render(model)
        self.assertIn("Estimate &middot; not yet seen in the game", html)
        self.assertIn("<b>ESTIMATE</b>", html)
        self.assertNotIn("Game verified", html)


if __name__ == "__main__":
    unittest.main()
