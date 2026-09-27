"""AFK FARM's Blacksmith: a Vault item reforged with a seed the game built, at most once
per request id, in place, behind a backup and an undo barrier.

Only temporary databases are touched; the game is faked (which seeds it has built) and
Hero Siege is reported as closed.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import test_vault_afk_qol as qol
from test_vault_ingest import RARITY_BELTS, belt, editor, spool_record


class FakeGame:
    """The game's Item Truth: the seeds it has built, and the requests it was sent."""

    def __init__(self):
        self.built: set[float] = set()
        self.requests: list[list] = []

    def write(self, root, entries, **_):
        entries = list(entries)
        self.requests.append(entries)
        return f"req{len(self.requests)}", [key for key, _ in entries]

    def build_all(self):
        for entries in self.requests:
            self.built.update(float(data["a"]) for _, data in entries)

    def model(self, item, custom_name=None, build_status=None):
        seed = float((item.get("raw") or {}).get("a", -1))
        if seed not in self.built:
            return {"calculation": {"coverage": "estimate"}, "item": {"name": item.get("name")}, "stats": []}
        return {"calculation": {"coverage": "game_verified"},
                "item": {"name": item.get("name"), "rarity": "Satanic", "tier": "Legendary", "requiredLevel": 60},
                "stats": [{"label": "Magic Find", "formattedValue": f"+{int(seed) % 50}%"},
                          {"label": "Defense", "formattedValue": str(100 + int(seed) % 30)}]}


class AfkReforgeTests(unittest.TestCase):
    setUp_vault = qol.VaultAfkQolTests.setUp
    ingest = qol.VaultAfkQolTests.ingest

    def setUp(self):
        self.setUp_vault()
        self.game = FakeGame()
        self.truth = Path(self.temp.name) / "itemtruth"
        for target, value in (("GAME_TRUTH_ACTIVE", True), ("ITEM_TRUTH_DIR", self.truth),
                              ("_game_tooltip_model", self.game.model)):
            patcher = patch.object(editor, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (("write_eval_request", self.game.write), ("capture_status", lambda root: {"requested": True})):
            patcher = patch.object(editor.game_truth, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        base, unique = RARITY_BELTS["Satanic"]
        self.ingest([belt(1, "Belt one", base, unique=unique), belt(2, "Belt two", base, unique=unique),
                     belt(3, "Socketed belt", base, unique=unique),
                     spool_record(4, 12, "Basic Key", {"n": 2.0, "b": 0.0, "a": 7.0, "j": 0, "c": 0.0, "o": 5.0})], "exp_forge")
        items = {r.label: r for r in self.store.list_all_available_items()}
        self.belt, self.other, socketed = items["Belt one"], items["Belt two"], items["Socketed belt"]
        value = socketed.decoded_item()           # a rune in its first socket (a spool import keeps no sockets)
        value["data"] = dict(value["data"], s1="eyJiIjogMX0=")
        token = self.store.preview_item_rework([socketed.id])
        self.store.rework_items(update=[(socketed.id, json.dumps(value, separators=(",", ":")))], preview_token=token,
                                event_type="items_stacked")
        self.socketed = self.store.get_item(socketed.id)

    def forge(self, action, **body):
        return editor.op_vault_afk_reforge({"action": action, **body})

    def offer(self, request="forge-0001-abcdef", tries=4, item=None):
        return self.forge("offer", requestId=request, itemId=(item or self.belt).id, tries=tries)

    def test_items_lists_equipment_and_what_cannot_be_reforged(self):
        by_id = {r["id"]: r for r in self.forge("items")["items"]}
        self.assertTrue(by_id[self.belt.id]["eligible"])
        self.assertEqual(by_id[self.belt.id]["itemSha"], self.belt.raw_sha256)
        self.assertFalse(by_id[self.socketed.id]["eligible"])
        self.assertIn("sockets", by_id[self.socketed.id]["reason"])
        self.assertEqual(len(by_id), 3, "the key is no equipment")
        self.assertIn("err", self.forge("items", limit=1000))

    def test_why_an_item_cannot_be_reforged(self):
        why = editor._afk_reforge_why_not
        gear, unique = {"cls": 8, "stackable": False}, {"a": 5.0, "c": 1.0}
        self.assertIsNone(why(gear, unique))
        self.assertIn("equipment", why(dict(gear, stackable=True), unique))
        self.assertIn("equipment", why(dict(gear, cls=14), unique))
        self.assertIn("unique", why(gear, dict(unique, c=0.0)), "any other item's seed rolls its rarity too")
        self.assertIn("unique", why(gear, {"a": 5.0}))
        self.assertIn("runeword", why(dict(gear, rar="Runeword"), unique))
        self.assertIn("sockets", why(gear, dict(unique, s2="x")))
        self.assertIn("skill", why(dict(gear, skillSelector={"current": {}}), unique))
        self.assertIn("Custom Forge", why(dict(gear, customForge={"active": True}), unique))
        self.assertIn("seed", why(gear, dict(unique, a=True)))

    def test_an_offer_asks_the_game_and_the_same_request_is_the_same_offer(self):
        first = self.offer()
        self.assertNotIn("err", first, first.get("err"))
        self.assertEqual((first["state"], first["tries"], len(first["candidates"])), ("building", 4, 4))
        seeds = [c["seed"] for c in first["candidates"]]
        self.assertEqual(len(set(seeds)), 4)
        self.assertNotIn(self.belt.decoded_item()["data"]["a"], seeds, "never the seed it has")
        self.assertEqual([float(d["a"]) for _, d in self.game.requests[0]], seeds)
        self.assertEqual({k for k, _ in self.game.requests[0]}, {self.belt.source_item_key})
        again = self.offer()
        self.assertEqual([c["seed"] for c in again["candidates"]], seeds, "drawn from the request id")
        self.game.build_all()
        ready = self.forge("status", requestId="forge-0001-abcdef", itemId=self.belt.id, tries=4, itemSha=self.belt.raw_sha256)
        self.assertEqual(ready["state"], "ready")
        self.assertTrue(all(c["verified"] and c["lines"] and c["rarity"] == "Satanic" for c in ready["candidates"]))
        self.assertEqual(len(self.game.requests), 2)
        self.offer()
        self.assertEqual(len(self.game.requests), 2, "nothing more to build")
        other = self.offer(request="forge-0002-abcdef")
        self.assertNotEqual([c["seed"] for c in other["candidates"]], seeds)

    def test_an_offer_needs_the_games_truth_and_a_fit_item(self):
        with patch.object(editor, "GAME_TRUTH_ACTIVE", False):
            self.assertIn("Game truth", self.offer()["err"])
        with patch.object(editor.game_truth, "capture_status", lambda root: {"requested": False}):
            self.assertIn("capture is off", self.offer()["err"])
        self.assertIn("sockets", self.offer(item=self.socketed)["err"])
        self.assertIn("err", self.offer(tries=3))
        self.assertIn("err", self.forge("offer", requestId="short", itemId=self.belt.id, tries=4))
        self.assertIn("err", self.forge("offer", requestId="forge-0003-abcdef", itemId="nope", tries=4))
        self.assertEqual(self.game.requests, [])

    def test_choose_replaces_the_item_in_place_once_behind_a_backup(self):
        offer = self.offer()
        request, sha = "forge-0001-abcdef", self.belt.raw_sha256
        choose = dict(requestId=request, itemId=self.belt.id, tries=4, itemSha=sha)
        self.assertIn("not built", self.forge("choose", index=1, **choose)["err"])
        self.game.build_all()
        self.assertIn("err", self.forge("choose", index=4, **choose))
        self.assertIn("changed", self.forge("choose", index=1, **dict(choose, itemSha="0" * 64))["err"])
        before = self.store.get_item(self.belt.id)
        done = self.forge("choose", index=1, **choose)
        self.assertNotIn("err", done, done.get("err"))
        self.assertEqual((done["state"], done["replayed"], done["candidate"]), ("done", False, 1))
        self.assertIn("ok", done)
        after = self.store.get_item(self.belt.id)
        self.assertEqual(after.decoded_item()["data"]["a"], offer["candidates"][1]["seed"])
        self.assertEqual({k: v for k, v in after.decoded_item()["data"].items() if k != "a"},
                         {k: v for k, v in before.decoded_item()["data"].items() if k != "a"}, "only the seed changes")
        self.assertEqual((after.collection_id, after.page_index, after.layout_x, after.layout_y, after.source_item_key),
                         (before.collection_id, before.page_index, before.layout_x, before.layout_y, before.source_item_key))
        self.assertTrue(list(Path(self.temp.name).glob("vault.sqlite3.before-afk-item-reforged-*.bak")))
        event = next(e for e in self.store.list_events(limit=5) if e["eventType"] == "afk_item_reforged")
        self.assertEqual((event["itemId"], event["details"]["requestId"], event["details"]["oldSeed"], event["details"]["newSeed"]),
                         (self.belt.id, request, before.decoded_item()["data"]["a"], offer["candidates"][1]["seed"]))
        again = self.forge("choose", index=2, **choose)
        self.assertEqual((again["state"], again["replayed"], again["candidate"]), ("done", True, 1))
        self.assertEqual(self.store.get_item(self.belt.id).decoded_item()["data"]["a"], offer["candidates"][1]["seed"])
        self.assertEqual(self.forge("status", **choose)["state"], "done")
        report = self.forge("cancel", requestId=request)
        self.assertEqual((report["state"], report["replayed"]), ("done", True))

    def test_a_cancelled_request_never_reforges(self):
        self.offer()
        self.game.build_all()
        request = "forge-0001-abcdef"
        cancelled = self.forge("cancel", requestId=request)
        self.assertEqual((cancelled["state"], cancelled["replayed"]), ("cancelled", False))
        late = self.forge("choose", requestId=request, itemId=self.belt.id, tries=4, itemSha=self.belt.raw_sha256, index=0)
        self.assertEqual(late["state"], "cancelled")
        self.assertEqual(self.store.get_item(self.belt.id).raw_sha256, self.belt.raw_sha256)
        self.assertEqual(self.forge("offer", requestId=request, itemId=self.belt.id, tries=4)["state"], "cancelled")

    def test_a_reforge_is_an_undo_barrier(self):
        elsewhere = self.store.create_collection("Elsewhere")
        self.store.move_item(self.other.id, elsewhere.id)
        self.assertEqual(self.store.preview_metadata_undo()["eventType"], "item_moved")
        self.offer()
        self.game.build_all()
        done = self.forge("choose", requestId="forge-0001-abcdef", itemId=self.belt.id, tries=4, itemSha=self.belt.raw_sha256, index=0)
        self.assertEqual(done["state"], "done")
        self.assertIsNone(self.store.preview_metadata_undo())

    def test_the_route_answers_with_the_editor_header(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), editor.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/vault/afk-reforge"
            request = Request(url, data=json.dumps({"action": "items"}).encode("utf-8"), method="POST",
                              headers={"Content-Type": "application/json", editor.EDITOR_REQUEST_HEADER: "1"})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(len(json.load(response)["items"]), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
