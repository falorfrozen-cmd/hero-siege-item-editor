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
        202: {"name": "Skill Grant: Skill", "valueKind": "skill_id",
              "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "grant block: INT 202"}},
        203: {"name": "Skill Grant: Levels", "valueKind": "skill_level",
              "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "grant block: INT 203"}},
        204: {"name": "Skill Grant: Class", "valueKind": "class_id",
              "evidence": {"function": "gml_Script_DrawInventoryItemV2", "location": "grant block: INT 204"}},
        1: {"name": "unknown", "valueKind": "unknown", "evidence": {"function": None}},
    }

    def get(self, key):
        return self.STATS.get(int(key))

    def talent(self, talent_id):
        return {182: {"name": "Frost Nova", "slug": "frost_nova"},
                500: {"name": "", "slug": "relicMeatHook"}}.get(talent_id)

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
        request_id, written = gt.write_eval_request(self.folder, entries)
        self.assertEqual(written, ["0-0-212409236228-8", "0-0-6-3"], "duplicates, placeholder keys and junk are left out")
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
        self.assertEqual(gt.write_eval_request(self.folder, []), (None, []))
        self.assertFalse((self.folder / "requests").exists())

    def test_a_request_names_only_the_items_that_fit(self):
        # The caller learns which items the game is asked about: the ones past the
        # cap are not, and must not count as asked when the request finishes.
        entries = [(f"0-0-{n}-3", {"a": n}) for n in (1, 2, 3)]
        with mock.patch.object(gt, "MAX_REQUEST_ITEMS", 2):
            request_id, written = gt.write_eval_request(self.folder, entries)
        self.assertEqual(written, ["0-0-1-3", "0-0-2-3"])
        lines = (self.folder / "requests" / f"{request_id}.req").read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines], written)

    def test_request_states_follow_the_file_names(self):
        folder = self.folder / "requests"
        folder.mkdir()
        for name in ("1-a.req", "2-b.working", "3-c.stopped", "4-d.tmp", "bad id.req"):
            (folder / name).write_text("x", encoding="utf-8")
        self.assertEqual(gt.request_files(self.folder), {"waiting": ["1-a"], "running": ["2-b"], "stopped": ["3-c"]})
        self.assertEqual(gt.clear_stopped_requests(self.folder), 1)
        self.assertEqual(gt.request_files(self.folder)["stopped"], [])

    def test_drawing_requests_live_in_their_own_folder(self):
        request_id, written = gt.write_eval_request(self.folder, [("0-0-6-3", {"a": 6})], kind="tipdraw")
        self.assertEqual(written, ["0-0-6-3"])
        self.assertTrue((self.folder / "tips" / f"{request_id}.req").is_file())
        self.assertFalse((self.folder / "requests").exists())
        self.assertEqual(gt.request_files(self.folder, "tipdraw")["waiting"], [request_id])
        self.assertEqual(gt.request_files(self.folder)["waiting"], [])
        (self.folder / "tips" / f"{request_id}.req").rename(self.folder / "tips" / f"{request_id}.stopped")
        self.assertEqual(gt.stopped_request_keys(self.folder, request_id), ["0-0-6-3"])
        self.assertEqual(gt.stopped_request_keys(self.folder, "../x"), [])
        self.assertEqual(gt.clear_stopped_requests(self.folder, "tipdraw"), 1)
        self.assertEqual(gt.request_files(self.folder, "tipdraw")["stopped"], [])

    def test_a_session_ending_on_a_request_names_it_and_play_after_it_does_not(self):
        store = gt.TruthStore(self.folder / "truth.sqlite3")
        journal = self.folder / "journal"
        journal.mkdir()
        progress = lambda req, kind="eval": json.dumps({"v": 1, "kind": kind, "req": req, "build": BUILD, "t": 1,
                                                        "total": 2, "done": 0, "ok": 0, "failed": 0, "rejected": 0,
                                                        "finished": False}) + "\n"
        drawing = lambda req: json.dumps({"v": 1, "kind": "tooltip", "build": BUILD, "t": 2, "ts": "9", "hash": "h",
                                          "req": req, "args": [], "rows": [], "stats": []}) + "\n"
        table = json.dumps({"v": 1, "kind": "tooltip-table", "build": BUILD, "t": 3, "stats": []}) + "\n"
        # A check that crashed on its second item: its progress, then the first item built.
        (journal / "live-a-1.ndjson").write_text(progress("1-a") + journal_line("5", src="eval"), encoding="utf-8")
        # A drawing paused, then two items of ordinary play before the quit.
        (journal / "live-b-1.ndjson").write_text(progress("2-b", "tipdraw") + drawing("2-b") + journal_line("6")
                                                 + journal_line("7"), encoding="utf-8")
        # A drawing that ended the session: its stat table counts nothing.
        (journal / "live-c-1.ndjson").write_text(progress("3-c", "tipdraw") + journal_line("8") + drawing("3-c")
                                                 + table, encoding="utf-8")
        store.ingest_journal_dir(journal)
        self.assertTrue(store.request_ended_session("1-a"))
        self.assertFalse(store.request_ended_session("2-b"))
        self.assertTrue(store.request_ended_session("3-c"))
        self.assertFalse(store.request_ended_session("4-d"))
        # An eval record carries its request: it starts the count again.
        (journal / "live-b-2.ndjson").write_text(json.dumps({**json.loads(journal_line("9", src="eval")), "req": "2-b"})
                                                 + "\n", encoding="utf-8")
        store.ingest_journal_dir(journal)
        self.assertTrue(store.request_ended_session("2-b"), "a journal's parts continue each other")
        store.close()

    def test_drawing_progress_and_strikes_are_kept(self):
        store = gt.TruthStore(self.folder / "truth.sqlite3")
        journal = self.folder / "journal"
        journal.mkdir()
        (journal / "live.ndjson").write_text(json.dumps(
            {"v": 1, "kind": "tipdraw", "req": "18-cd", "build": BUILD, "t": 4, "total": 9, "done": 6, "ok": 6,
             "failed": 0, "rejected": 0, "finished": False}) + "\n", encoding="utf-8")
        store.ingest_journal_dir(journal)
        self.assertEqual((store.evaluation("18-cd")["done"], store.evaluation("18-cd")["total"]), (6, 9))
        self.assertEqual(store.strike_request(BUILD, ["0-0-6-3"], "tipdraw"), {"0-0-6-3": 1})
        store.close()
        again = gt.TruthStore(self.folder / "truth.sqlite3")
        self.assertEqual(again.strike_request(BUILD, ["0-0-6-3", "0-0-7-3"], "tipdraw"), {"0-0-6-3": 2, "0-0-7-3": 1})
        self.assertEqual(again.request_strikes(BUILD, "eval"), {}, "checks and drawings keep their own strikes")
        self.assertEqual(again.strike_request(BUILD, ["0-0-6-3"], "eval"), {"0-0-6-3": 1})
        self.assertEqual(again.request_strikes("pe-other", "tipdraw"), {})
        self.assertEqual(again.request_strikes(BUILD, "unknown"), {})
        again.close()

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


class TooltipRecordTests(unittest.TestCase):
    def test_tooltip_rows_and_the_stat_table_are_kept_per_build(self):
        folder = Path(tempfile.mkdtemp())
        try:
            store = gt.TruthStore(folder / "truth.sqlite3")
            journal = folder / "journal"
            journal.mkdir()
            row = {"fn": "o", "s": 28, "c": 16777215, "ha": 1, "a": [10, 20, "+773% Enhanced Damage"]}
            tip = lambda t, text: json.dumps({"v": 1, "kind": "tooltip", "build": BUILD, "t": t, "ts": "212409236228",
                                               "hash": "h", "args": [1, 2, 1, None],
                                               "rows": [dict(row, a=[10, 20, text])],
                                               "stats": [{"id": 28, "h": 30, "a": [10, 20, None, 28, "Enhanced Damage", 2, 8]}]}) + "\n"
            table = json.dumps({"v": 1, "kind": "tooltip-table", "build": BUILD, "t": 3,
                                "stats": [{"id": 28, "h": 0, "a": [0, 0, None, 28, "Enhanced Damage", 2, 8]}]}) + "\n"
            (journal / "live.ndjson").write_text(tip(5, "+773% Enhanced Damage") + tip(4, "older") + table
                                                 + journal_line(), encoding="utf-8")
            result = store.ingest_journal_dir(journal)
            self.assertEqual((result.added, result.skipped), (1, 0), "tooltip lines are not item records")
            kept = store.tooltip("212409236228", "h", BUILD)
            self.assertEqual(kept["rows"][0]["a"][2], "+773% Enhanced Damage", "the newest pass wins")
            self.assertIsNone(store.tooltip("212409236228", "h", "pe-other"))
            self.assertIsNone(store.tooltip("212409236228", "other hash", BUILD))
            self.assertEqual(store.tooltip_table(BUILD)[0]["id"], 28)
            self.assertIsNone(store.tooltip_table(None))
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def bgr(rgb: str) -> int:
    """#rrggbb as the game keeps a colour (0xBBGGRR)."""
    value = int(rgb.lstrip("#"), 16)
    return ((value & 0xFF) << 16) | (value & 0xFF00) | (value >> 16)


def drawn(x, y, text, colour, *, ha=0, fn="draw_text", s=-1, extra=()):
    return {"fn": fn, "s": s, "c": bgr(colour), "ha": ha, "va": 0, "a": [x, y, text, *extra]}


def stat_call(y, stat, label, fmt, style):
    return {"id": stat, "h": 30,
            "a": [2048, y, None, stat if stat >= 0 else None, label, fmt, style, None, False, False, 13461874, False]}


# One tooltip the way ForgePact records it: every draw call of the pass and every
# stat call that drew a line (the shapes of a captured set item, made-up values).
CAPTURED = {
    "recordedAt": 1790000000000,
    "rows": [
        drawn(2048, 8, "Belt of Tests", "#0ce11c", ha=1),
        drawn(2048, 48, "Satanic Set Belt", "#808080", ha=1),
        drawn(1945, 68, "(", "#808080"), drawn(1952, 68, "Gem", "#808080"), drawn(1992, 68, ", ", "#808080"),
        drawn(2002, 68, "Gem", "#808080"), drawn(2042, 68, ")", "#808080"),
        drawn(1962, 118, "Defense: ", "#ffffff"), drawn(2074, 118, "11194", "#7269cd"),
        drawn(1899, 178, "+264%", "#000000", s=29), drawn(1903, 178, "+264%", "#000000", s=29),
        drawn(1901, 178, "+264%", "#7269cd", s=29), drawn(1976, 178, "Enhanced Defense", "#7269cd", s=29),
        drawn(1851, 208, "Ailment damage increased by", "#7269cd", s=250), drawn(2201, 208, "35%", "#7269cd", s=250),
        drawn(1959, 268, "Sockets (4)", "#7269cd"), drawn(2083, 268, " [2-4]", "#808080"),
        drawn(1894, 328, "+30", "#404040", s=30), drawn(1937, 328, "to Magic Skill Damage", "#404040", s=30),
        drawn(2048, 388, "Forged in tests.", "#0ce11c", ha=1, fn="draw_text_ext", extra=(-1, 560)),
        drawn(1908, 470, "Tier ", "#808080"), drawn(1962, 470, "S", "#f5c832"),
        drawn(1974, 470, ", Requires Level ", "#808080"), drawn(2163, 470, "38", "#ffffff"),
        drawn(2048, 515, "ALT - Show Information", "#808080", ha=1),
    ],
    "stats": [stat_call(118, -1, "Defense: ", 1, 1), stat_call(178, 29, "Enhanced Defense", 2, 8),
              stat_call(208, 250, "Ailment damage increased by", 2, 9), stat_call(328, 30, "to Magic Skill Damage", 6, 8)],
}


def table_call(stat, label, fmt, style, *, per_level=False, negated=False, colour=13461874):
    return {"id": stat, "h": 0, "a": [0, 0, None, stat, label, fmt, style, None, per_level, negated, colour, False]}


# The stat calls of one tooltip pass: the game's order, labels and formats.
TABLE = [
    {"id": -1, "h": 30, "a": [0, 0, None, None, "Attack Damage: ", 1, 9, None, False, False, 13461874, False]},
    table_call(28, "Enhanced Damage", 2, 8),
    table_call(203, "to ", 3, 8, colour=18687),
    table_call(51, "to All Attributes", 3, 8),
    table_call(152, "to All Enemy Resistances", 2, 8, negated=True),
    table_call(250, "Ailment damage increased by", 2, 9),
    table_call(34, "to Strength (Based on Level)", 3, 8, per_level=True),
    table_call(28, "a later call of the same stat", 3, 8),
]


class GameTextTests(unittest.TestCase):
    def texts(self, rows):
        return ["".join(part["text"] for part in row["parts"]) for row in rows]

    def test_a_forged_row_above_the_games_row_leaves_the_stat_to_the_games_row(self):
        # ForgePact draws a forged item's own rows (centred, gold) inside the stat
        # call, above the game's row, which the game then draws 30 px lower.
        tooltip = {"rows": [
            drawn(2048, 178, "Steals the affixes of slain rare monsters", "#f2c462", ha=1, s=172),
            drawn(1800, 208, "Every point in resistances increases your damage by", "#7269cd", s=172),
            drawn(2300, 208, "1%", "#7269cd", s=172),
        ], "stats": [stat_call(178, 172, "Every point in resistances increases your damage by", 2, 9)]}
        rows = gt.captured_tooltip_rows(tooltip)
        self.assertEqual(self.texts(rows), ["Steals the affixes of slain rare monsters",
                                            "Every point in resistances increases your damage by 1%"])
        self.assertEqual([row["stat"] for row in rows], [None, 172])

    def test_the_alt_view_marks_the_lines_that_show_their_own_range(self):
        tooltip = {"rows": [
            drawn(1901, 178, "+56%", "#7269cd", s=29), drawn(1976, 178, "Enhanced Defense", "#7269cd", s=29),
            drawn(2200, 178, " [45-75]", "#808080", s=29),
            drawn(1959, 238, "Sockets (4)", "#7269cd"), drawn(2083, 238, " [2-4]", "#808080"),
        ], "stats": [stat_call(178, 29, "Enhanced Defense", 2, 8)]}
        rows = gt.captured_tooltip_rows(tooltip)
        self.assertEqual(self.texts(rows), ["+56% Enhanced Defense [45-75]", "Sockets (4) [2-4]"])
        self.assertEqual([row["ranged"] for row in rows], [True, False], "only a stat line's range is the ALT view's")

    def test_a_star_row_repeating_a_value_keeps_its_own_line(self):
        tooltip = {"rows": [
            drawn(1984, 658, "+8%", "#404040", s=8), drawn(2035, 658, "to Life", "#404040", s=8),
            drawn(1827, 688, "+8%", "#404040", s=8), drawn(1878, 688, "Increased Total Movement Speed", "#404040", s=8),
        ], "stats": [stat_call(658, 8, "to Life", 5, 8), stat_call(688, 8, "Increased Total Movement Speed", 5, 8)]}
        self.assertEqual(self.texts(gt.captured_tooltip_rows(tooltip)),
                         ["+8% to Life", "+8% Increased Total Movement Speed"])

    def test_captured_rows_read_as_the_player_saw_them(self):
        rows = gt.captured_tooltip_rows(CAPTURED)
        self.assertEqual(self.texts(rows), [
            "Belt of Tests", "Satanic Set Belt", "(Gem, Gem)", "Defense: 11194", "+264% Enhanced Defense",
            "Ailment damage increased by 35%", "Sockets (4) [2-4]", "+30 to Magic Skill Damage",
            "Forged in tests.", "Tier S, Requires Level 38",
        ], "pieces join per line, a stat's value and label with a space, the key hint is left out")
        self.assertEqual([row["gap"] for row in rows],
                         [False, True, False, True, True, False, True, True, True, True])
        self.assertEqual([row["stat"] for row in rows],
                         [None, None, None, None, 29, 250, None, None, None, None],
                         "only a stat line names its stat; the star row draws a value it was given")
        self.assertEqual([part["color"] for part in rows[3]["parts"]], ["#ffffff", "#7269cd"])
        self.assertEqual([part["color"] for part in rows[4]["parts"]], ["#7269cd", "#7269cd"],
                         "an outline's dark copies give way to the coloured draw")
        self.assertEqual(rows[0]["parts"][0]["color"], "#0ce11c")
        self.assertTrue(rows[8]["block"])
        self.assertEqual([part["color"] for part in rows[9]["parts"]], ["#808080", "#f5c832", "#808080", "#ffffff"])

    def test_a_colour_builtin_brings_its_own_colour(self):
        tooltip = {"rows": [{"fn": "draw_text_colour", "s": -1, "c": 0, "ha": 1,
                             "a": [0, 8, "Name", bgr("#d61616"), bgr("#d61616"), 0, 0, 1]}], "stats": []}
        self.assertEqual(gt.captured_tooltip_rows(tooltip)[0]["parts"][0]["color"], "#d61616")

    def test_game_colours_are_bgr(self):
        self.assertEqual(gt.gm_colour(13461874), "#7269cd")
        self.assertEqual(gt.gm_colour(18687), "#ff4800")
        self.assertIsNone(gt.gm_colour(None))

    def test_the_table_says_how_each_stat_line_reads(self):
        entries = gt.tooltip_table_entries(TABLE)
        self.assertEqual(sorted(entries), [28, 34, 51, 152, 203, 250], "header lines are not stat lines")
        self.assertEqual(entries[28]["label"], "Enhanced Damage", "the first call of a stat is its line")
        text = lambda key, value: (lambda g: (g["value"], g["label"], g["valueFirst"]))(
            gt.table_line_text(entries[key], value))
        self.assertEqual(text(28, 449), ("+449%", "Enhanced Damage", True))
        self.assertEqual(text(28, 2.5), ("+2.50%", "Enhanced Damage", True), "two decimals, as the game prints")
        self.assertEqual(text(51, -3), ("-3", "to All Attributes", True))
        self.assertEqual(text(152, 25), ("-25%", "to All Enemy Resistances", True), "a negated line")
        self.assertEqual(text(250, 35), ("35%", "Ailment damage increased by", False), "label first: no sign")
        self.assertEqual(text(34, 0.4), ("+40", "to Strength (Based on Level)", True), "per level, at level 100")
        self.assertEqual(gt.table_line_text(entries[34], 0.4, level=50)["value"], "+20")
        self.assertEqual(entries[203]["color"], "#ff4800")

    def model(self, stats, **kwargs):
        offline = VerifiedModelTests().offline()
        offline["stats"].append({"statKey": 29, "label": "Enhanced Defense", "value": 250, "minimum": 200,
                                 "maximum": 300, "percent": True})
        return gt.build_verified_model(offline, VerifiedModelTests().match(stats), semantics=FakeSemantics(),
                                       **kwargs)

    def test_without_a_drawing_the_table_gives_every_line_its_game_text_and_place(self):
        model = self.model({"154": 36.0, "34": 0.4, "250": 35.0, "51": 3.0, "203": 12.0, "202": 182.0,
                            "28": 449.0}, table=gt.tooltip_table_entries(TABLE))
        order = [line["statKey"] for line in model["stats"]]
        self.assertEqual(order, [154, 28, 203, 51, 250, 34], "header first, then the table's order")
        lines = {line["statKey"]: line for line in model["stats"]}
        grant = lines[203]["game"]
        self.assertEqual((grant["value"], grant["label"], grant["color"]), ("+12", "to Frost Nova", "#ff4800"),
                         "a skill grant is one line named after its skill")
        self.assertIn(202, {line["statKey"] for line in model["internalStats"]})
        self.assertNotIn("game", lines[154])
        self.assertEqual(model["calculation"]["textSource"], "table")
        self.assertFalse(model["calculation"]["textExact"])
        self.assertIsNone(model["gameText"])

    def test_zero_lines_are_not_drawn_and_class_skills_name_their_class(self):
        # Measured on 7,628 tooltips the game drew: "+3 to All Skills (Illusionist)",
        # "+16 to Omnislash (Samurai)", and no line for a 0.
        table = gt.tooltip_table_entries(TABLE + [table_call(201, "to All Skills", 3, 8),
                                                  table_call(225, "to Fire Skills", 3, 8)])
        model = self.model({"201": 3.0, "21": 3.0, "202": 182.0, "203": 12.0, "204": 3.0, "225": 2.0, "51": 0.0},
                           table=table)
        lines = {line["statKey"]: line for line in model["stats"]}
        self.assertEqual(lines[201]["game"]["label"], "to All Skills (Viking)")
        self.assertEqual(lines[225]["game"]["label"], "to Fire Skills (Viking)")
        self.assertEqual(lines[203]["game"]["label"], "to Frost Nova (Viking)")
        self.assertEqual(set(lines), {201, 203, 225}, "the skill, the classes and the 0 do not stand alone")
        self.assertTrue({21, 202, 204, 51} <= {line["statKey"] for line in model["internalStats"]})

    def test_relic_skills_take_their_names_from_the_games_translations(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "translationsRelic.csv").write_text(
                "[Relics]|en|fi\ntalent_name_relicMeatHook|Meat Hook|Lihakoukku\ndesc_x|y|z\n", encoding="utf-8")
            names = gt.talent_names(folder)
            self.assertEqual(names, {"relicMeatHook": "Meat Hook"})
            self.assertEqual(gt.talent_names(None), {})
            table = gt.tooltip_table_entries(TABLE)
            named = self.model({"202": 500.0, "203": 2.0}, table=table, talent_names=names)
            self.assertEqual({line["statKey"]: line for line in named["stats"]}[203]["game"]["label"], "to Meat Hook")
            bare = self.model({"202": 500.0, "203": 2.0}, table=table)
            self.assertEqual({line["statKey"]: line for line in bare["stats"]}[203]["game"]["label"], "to relicMeatHook")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_games_own_drawing_is_the_tooltip(self):
        model = self.model({"154": 36.0, "29": 264.0}, tooltip=CAPTURED, table=gt.tooltip_table_entries(TABLE))
        self.assertEqual(self.texts(model["gameText"]["rows"])[4], "+264% Enhanced Defense")
        self.assertEqual(model["gameText"]["recordedAt"], 1790000000000)
        self.assertTrue(model["calculation"]["textExact"])
        self.assertEqual(model["calculation"]["textSource"], "game")

    def test_drawn_tooltips_are_packed_and_listed_per_build(self):
        folder = Path(tempfile.mkdtemp())
        try:
            store = gt.TruthStore(folder / "truth.sqlite3")
            journal = folder / "journal"
            journal.mkdir()
            record = {"v": 1, "kind": "tooltip", "build": BUILD, "t": 5, "ts": "212409236228", "hash": "h",
                      "req": "1-a", "args": [1, 2, 1, None], "rows": CAPTURED["rows"], "stats": CAPTURED["stats"]}
            (journal / "live.ndjson").write_text(json.dumps(record) + "\n", encoding="utf-8")
            store.ingest_journal_dir(journal)
            raw = store._connection().execute("SELECT rows_json FROM tooltips").fetchone()[0]
            self.assertIsInstance(raw, bytes, "rows are kept packed")
            self.assertLess(len(raw), len(json.dumps(CAPTURED["rows"])))
            self.assertEqual(store.tooltip("212409236228", "h", BUILD)["rows"], CAPTURED["rows"])
            store._connection().execute(
                "INSERT INTO tooltips(ts, hash, build, recorded_at, args_json, rows_json, stats_json) "
                "VALUES ('5', 'x', ?, 1, '[]', '[{\"fn\":\"draw_text\"}]', '[]')", (BUILD,))
            self.assertEqual(store.tooltip("5", "x", BUILD)["rows"], [{"fn": "draw_text"}], "text from before packing")
            self.assertEqual(store.tooltip_keys(BUILD), {("212409236228", "h"), ("5", "x")})
            self.assertEqual(store.tooltip_keys("pe-other"), set())
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_drawing_is_tied_to_the_content_of_the_item_the_game_built_for_it(self):
        # The game gives some items a new itemDataHash each time it builds them:
        # the store keeps the content once, under the hash it saw first.
        folder = Path(tempfile.mkdtemp())
        try:
            store = gt.TruthStore(folder / "truth.sqlite3")
            journal = folder / "journal"
            journal.mkdir()
            drawing = lambda ts, hash_text, req="1-a": json.dumps(
                {"v": 1, "kind": "tooltip", "build": BUILD, "t": 6, "ts": ts, "hash": hash_text, "req": req,
                 "args": [], "rows": [{"fn": "draw_text", "s": -1, "c": 0, "ha": 1, "a": [0, 8, ts + hash_text]}],
                 "stats": []}) + "\n"
            progress = json.dumps({"v": 1, "kind": "tipdraw", "req": "1-a", "build": BUILD, "t": 7, "total": 3,
                                   "done": 1, "ok": 1, "failed": 0, "rejected": 0, "finished": False}) + "\n"
            item = lambda ts, hash_text: json.dumps({**json.loads(journal_line(ts, src="live")), "hash": hash_text}) + "\n"
            (journal / "live-x-1.ndjson").write_text(
                item("11", "first") + item("11", "second") + drawing("11", "second")    # tied, though kept as "first"
                + item("22", "first") + progress + drawing("22", "second")             # a line between: not tied
                + item("33", "first") + drawing("33", "second", req=None)              # the player's own hover
                + item("44", "first"), encoding="utf-8")
            (journal / "live-x-2.ndjson").write_text(drawing("44", "second"), encoding="utf-8")   # the next part
            store.ingest_journal_dir(journal)
            key = lambda ts: store.lookup(f"0-0-{ts}-8", {"a": 107725, "b": 2, "c": 0, "j": 0},
                                          current_build=BUILD).record
            first = key("11")
            self.assertEqual(first["hash"], "first", "the content is kept once")
            self.assertEqual(store.tooltip("11", first["hash"], BUILD, first["contentKey"])["rows"][0]["a"][2], "11second")
            self.assertIsNone(store.tooltip("11", "first", BUILD), "the hash alone does not reach it")
            self.assertIsNone(store.tooltip("22", "first", BUILD, key("22")["contentKey"]))
            self.assertIsNone(store.tooltip("33", "first", BUILD, key("33")["contentKey"]))
            self.assertEqual(store.tooltip("33", "second", BUILD)["rows"][0]["a"][2], "33second",
                             "a hover is found by the hash it was drawn with")
            self.assertEqual(store.tooltip("44", "first", BUILD, key("44")["contentKey"])["rows"][0]["a"][2], "44second",
                             "a journal's parts continue each other")
            keys = store.tooltip_keys(BUILD)
            self.assertIn(("11", "#" + first["contentKey"]), keys)
            self.assertNotIn(("22", "#" + key("22")["contentKey"]), keys)
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_any_hash_the_same_content_came_with_reaches_its_drawing(self):
        # The player loads the item (a new hash, the same content: kept as an
        # alias), then hovers it: that drawing belongs to the record kept earlier.
        folder = Path(tempfile.mkdtemp())
        try:
            store = gt.TruthStore(folder / "truth.sqlite3")
            journal = folder / "journal"
            journal.mkdir()
            item = lambda hash_text, **changes: json.dumps(
                {**json.loads(journal_line("55", src="live", **changes)), "hash": hash_text}) + "\n"
            hover = json.dumps({"v": 1, "kind": "tooltip", "build": BUILD, "t": 9, "ts": "55", "hash": "later",
                                "args": [], "rows": [{"fn": "draw_text", "s": -1, "c": 0, "ha": 1, "a": [0, 8, "x"]}],
                                "stats": []}) + "\n"
            (journal / "live-a-1.ndjson").write_text(item("first"), encoding="utf-8")
            another_item = {"a": 107726.0, "b": 2.0, "c": 0.0, "j": 0.0}   # the same timestamp, another item
            (journal / "live-b-1.ndjson").write_text(
                item("later") + item("other", definition=another_item) + hover, encoding="utf-8")
            store.ingest_journal_dir(journal)
            record = store.lookup("0-0-55-8", {"a": 107725, "b": 2, "c": 0, "j": 0}, current_build=BUILD).record
            self.assertEqual(record["hash"], "first")
            self.assertEqual(store.tooltip("55", "first", BUILD, record["contentKey"])["rows"][0]["a"][2], "x")
            self.assertIn(("55", "#" + record["contentKey"]), store.tooltip_keys(BUILD))
            aliases = store._connection().execute("SELECT hash FROM hash_aliases").fetchall()
            self.assertEqual([row[0] for row in aliases], ["later"], "a new content is a record, not an alias")
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_journals_read_before_drawings_were_tied_are_read_again_once(self):
        folder = Path(tempfile.mkdtemp())
        try:
            path = folder / "truth.sqlite3"
            old = sqlite3.connect(str(path))
            old.executescript(
                """CREATE TABLE tooltips(ts TEXT NOT NULL, hash TEXT NOT NULL, build TEXT NOT NULL,
                       recorded_at INTEGER NOT NULL, args_json TEXT NOT NULL, rows_json TEXT NOT NULL,
                       stats_json TEXT NOT NULL, record_hash TEXT, PRIMARY KEY(ts, hash, build));
                   CREATE TABLE sources(path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                       offset INTEGER NOT NULL);
                   INSERT INTO sources VALUES('C:\\x\\itemtruth\\journal\\live-1.ndjson', 5, 5, 5);
                   INSERT INTO sources VALUES('C:\\x\\afk\\spool\\a_claim.ndjson', 5, 5, 5);""")
            old.commit()
            old.close()
            store = gt.TruthStore(path)
            self.assertEqual([row[0] for row in store._connection().execute("SELECT path FROM sources")],
                             ["C:\\x\\afk\\spool\\a_claim.ndjson"])
            columns = {row[1] for row in store._connection().execute("PRAGMA table_info(tooltips)")}
            self.assertIn("record_key", columns)
            self.assertNotIn("record_hash", columns, "a development version's column is dropped")
            store.close()
            again = gt.TruthStore(path)
            again._connection().execute("INSERT INTO sources VALUES('C:\\x\\itemtruth\\journal\\live-2.ndjson', 1, 1, 1)")
            again.close()
            third = gt.TruthStore(path)
            self.assertEqual(len(third._connection().execute("SELECT * FROM sources").fetchall()), 2, "only once")
            third.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_store_parses_a_table_once_per_recording(self):
        folder = Path(tempfile.mkdtemp())
        try:
            store = gt.TruthStore(folder / "truth.sqlite3")
            journal = folder / "journal"
            journal.mkdir()
            line = lambda t, label: json.dumps({"v": 1, "kind": "tooltip-table", "build": BUILD, "t": t,
                                                "stats": [table_call(28, label, 2, 8)]}) + "\n"
            (journal / "a.ndjson").write_text(line(3, "Enhanced Damage"), encoding="utf-8")
            store.ingest_journal_dir(journal)
            first = store.table_entries(BUILD)
            self.assertEqual(first[28]["label"], "Enhanced Damage")
            self.assertIs(store.table_entries(BUILD), first)
            (journal / "b.ndjson").write_text(line(9, "Enhanced Damage (new)"), encoding="utf-8")
            store.ingest_journal_dir(journal)
            self.assertEqual(store.table_entries(BUILD)[28]["label"], "Enhanced Damage (new)")
            self.assertEqual(store.table_entries("pe-other"), {})
            store.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)


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
                {"schema": 1, "forgepact": "1.4.5", "build": BUILD, "updated": int(now * 1000) - 5000,
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
                        r"function gameColor\([^\n]+", r"function renderGameTextRows\(model\)\{.*?\n\}",
                        r"function renderTooltipLines\(model,options\)\{.*?\n\}",
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

    def test_the_games_drawing_is_shown_row_by_row_in_its_colours(self):
        model = GameTextTests().model({"154": 36.0, "29": 264.0}, tooltip=CAPTURED)
        html = self.render(model)
        self.assertIn('<div class="gtt-row gtt-name"><span style="color:#0ce11c">Belt of Tests</span></div>', html)
        self.assertIn('<span style="color:#7269cd">+264%</span><span style="color:#7269cd"> Enhanced Defense</span>'
                      '<small class="gtt-range">(200–300)</small>', html, "a rolled stat keeps its range hint")
        self.assertIn('<div class="gtt-row gap block"><span style="color:#0ce11c">Forged in tests.</span></div>', html)
        self.assertIn("Game verified &middot; game text", html)
        self.assertNotIn("gtt-title", html, "the game's rows replace the editor's header")
        self.assertNotIn("Show Information", html)

    def test_a_line_drawn_with_its_own_range_gets_no_second_one(self):
        model = GameTextTests().model({"29": 264.0}, tooltip={"rows": [
            drawn(1901, 178, "+264%", "#7269cd", s=29), drawn(1976, 178, "Enhanced Defense", "#7269cd", s=29),
            drawn(2200, 178, " [200-300]", "#808080", s=29)], "stats": [stat_call(178, 29, "Enhanced Defense", 2, 8)]})
        html = self.render(model)
        self.assertIn('<span style="color:#808080"> [200-300]</span></div>', html)
        self.assertNotIn("gtt-range", html)

    def test_the_table_gives_lines_the_games_text_before_the_game_draws_them(self):
        model = GameTextTests().model({"28": 449.0, "250": 35.0}, table=gt.tooltip_table_entries(TABLE))
        html = self.render(model)
        self.assertIn('style="color:#7269cd">+449% Enhanced Damage<', html)
        self.assertIn('style="color:#7269cd">Ailment damage increased by 35%<', html)
        self.assertIn("Game verified &middot; game labels", html)
        self.assertIn("gtt-title", html, "the header stays the editor's until the game draws the item")

    def test_mythic_has_the_games_purple_and_no_second_rule(self):
        # The game's rarity 5 is "Mythic", drawn in #b115eb; a later rule for the
        # same class would silently win over it.
        self.assertEqual(self.html.count(".r-Mythic{"), 1)
        self.assertEqual(self.html.count(".b-Mythic{"), 1)
        self.assertIn(".r-Mythic{color:#c56cf0}", self.html)
        self.assertIn(".b-Mythic{background:#2a1336;border-color:#b115eb}", self.html)
        self.assertEqual(gt.RARITY_NAMES[5], "Mythic")

    def test_a_colour_from_a_record_is_never_markup(self):
        model = GameTextTests().model({"29": 264.0}, tooltip={"rows": [
            {"fn": "draw_text", "s": -1, "c": 0, "ha": 1, "a": [0, 8, "<b>x</b>"]}], "stats": []})
        model["gameText"]["rows"][0]["parts"][0]["color"] = "red;background:url(x)"
        html = self.render(model)
        self.assertIn('<span style="color:inherit">&lt;b&gt;x&lt;/b&gt;</span>', html)


if __name__ == "__main__":
    unittest.main()
