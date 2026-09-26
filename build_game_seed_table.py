#!/usr/bin/env python3
"""Build hs_game_seeds.json by having the running game build the candidates.

Run it with Hero Siege at the main menu and ForgePact's Item Truth capture on
(the editor's Game truth switch); nothing touches a save - every item is an
evaluation request the game builds in memory through its own save loader.

    python build_game_seed_table.py [--out hs_game_seeds.json]

One run builds the whole table: the game keeps none of the items it evaluates
(measured 2026-09-26 with ForgePact's tools/itemtruth_memrun.py: 20,000 at the
main menu moved its memory by about 10 MB, and it stayed there). What the game
built goes into a work file, so a run that stops early - at --max-per-run, or
because the game closed - carries on where it left off when it is run again.
Until everything is measured it stops with exit code 2.

What it measures (GAME_TRUTH_DESIGN.md, step 4):

1. White equipment bases. For each base the CPR model ranks seeds by how many of
   the base's variable stats land on their top (the roll profiles' own scoring);
   the game builds the best --white-candidates, and the seeds that come out
   Common with no socket are kept, best first. It also builds --natural-samples
   random seeds per base: the most sockets any of them rolls is the base's
   natural maximum, the socket editor's limit. Amulets and rings never come out
   Common, so they get no entry.
2. Uniques the editor claims sockets on, and every unique of the socket table:
   the game builds each with the editor's CPR-solved seed. Where it gives fewer
   sockets than claimed, it builds --unique-candidates stat-ranked seeds and the
   best one with the most sockets replaces it.
3. Runewords. Every recipe x base the editor offers, forged on the new white seed
   with zz.sockets = the rune count: a pair the game does not form (the
   runeword's name missing from the built item's title) goes into
   ``runewordBlocked``.

The socket rule the table relies on, measured the same way: a non-unique item
(c 0) shows the larger of zz.sockets and its seed's count, a unique (c 1) its
seed's count only, and filled socket payloads never add a socket.
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import game_truth  # noqa: E402

MULT, INC, MOD, MASK, TOP = 1_789_570_533.0, 465_707.0, 2_147_483_648.0, 0x3FFFFFFF, 1_073_741_823.0
EQUIPMENT = range(0, 9)
CHUNK = 20_000
KEEP = 4
PHASE_TS = {"measure": 1_795_000_000_000, "short": 1_795_300_000_000, "runewords": 1_795_600_000_000}


def _load_editor():
    os.chdir(HERE)
    argv, sys.argv = sys.argv, ["build_game_seed_table"]
    try:
        import hs_item_editor_gui as editor  # the editor's own catalog and rules
    finally:
        sys.argv = argv
    return editor


def a_chain_bounds(profile: dict | None) -> list | None:
    chain = ((profile or {}).get("chains") or {}).get("a") or {}
    segments = chain.get("segments")
    if not isinstance(segments, list):
        return None
    return [None if bound is None else int(bound) for segment in segments for bound in segment]


def ranked_seeds(signatures: dict[tuple, int], stop: int, block: int = 1_000_000) -> dict[tuple, list]:
    """Per stat signature, the best seeds by (stats at top, deficit, seed)."""

    best = {signature: [] for signature in signatures}
    scored = {signature: sum(1 for bound in signature if bound is not None) for signature in signatures}
    depth = max((len(signature) for signature in signatures), default=0)
    shift = np.int64(1 << 32)
    for start in range(1, stop, block):
        open_signatures = [
            signature for signature in signatures
            if sum(1 for e in best[signature] if e[2] == scored[signature] and e[3] == 0) < signatures[signature]
        ]
        if not open_signatures:
            break
        seeds = np.arange(start, min(start + block, stop), dtype=np.int64)
        state = seeds.astype(np.float64)
        draws = []
        for _ in range(depth):
            state = np.fmod(MULT * state + INC, MOD)
            state = np.bitwise_and(state.astype(np.int64), MASK).astype(np.float64)
            draws.append(state / TOP)
        for signature in open_signatures:
            maxed = np.zeros(len(seeds), dtype=np.int64)
            deficit = np.zeros(len(seeds), dtype=np.int64)
            for index, bound in enumerate(signature):
                if bound is None:
                    continue
                roll = np.floor((bound + 0.99999) * draws[index]).astype(np.int64)
                maxed += roll == bound
                deficit += bound - roll
            key = maxed * shift - deficit
            need = signatures[signature]
            kept = best[signature]
            idx = np.flatnonzero(key > kept[-1][0]) if len(kept) >= need else np.arange(len(seeds))
            if len(idx) > 4 * need:
                idx = idx[np.argpartition(-key[idx], 4 * need)[: 4 * need]]
            merged = kept + [(int(key[j]), int(seeds[j]), int(maxed[j]), int(deficit[j])) for j in idx]
            merged.sort(key=lambda entry: (-entry[0], entry[1]))
            best[signature] = merged[:need]
        print(f"  CPR scan {min(start + block - 1, stop):,} seeds, {len(open_signatures)} signatures open", flush=True)
    return {signature: [(seed, maxed, deficit) for _key, seed, maxed, deficit in entries]
            for signature, entries in best.items()}


class Work:
    """The plan and what the game built so far, kept between runs."""

    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "phases": {}, "records": {}, "requests": [],
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state), encoding="utf-8")
        tmp.replace(self.path)

    def phase(self, name: str) -> list | None:
        return self.state["phases"].get(name)

    def plan(self, name: str, items: list) -> None:
        base = PHASE_TS[name]
        self.state["phases"][name] = [
            {"ts": str(base + index), "cls": int(cls), "data": data, "label": label}
            for index, (cls, data, label) in enumerate(items)
        ]
        self.save()

    def missing(self, name: str) -> list:
        return [item for item in self.phase(name) or () if item["ts"] not in self.state["records"]]

    def record(self, ts: str) -> dict:
        return self.state["records"][ts]


def spend(budget: int | None, used: int) -> int | None:
    """What is left of --max-per-run after a phase built ``used`` items (None: no limit)."""
    return None if budget is None else budget - used


def measure(work: Work, root: Path, items: list, budget: int | None) -> int:
    """Have the game build ``items``, at most ``budget`` of them (None: all);
    returns how many it built."""

    batch = items if budget is None else items[:max(budget, 0)]
    if not batch:
        return 0
    requests = []
    for start in range(0, len(batch), CHUNK):
        entries = [(f"0-0-{item['ts']}-{item['cls']}", item["data"]) for item in batch[start:start + CHUNK]]
        request_id, _keys = game_truth.write_eval_request(root, entries)
        requests.append(request_id)
    work.state["requests"].extend(requests)
    work.save()
    wanted = {item["ts"] for item in batch}
    print(f"  the game builds {len(batch):,} items ({len(requests)} request(s))", flush=True)
    last, still, got = -1, 0, 0
    while True:
        for path in sorted((root / "journal").glob("live-*.ndjson")):
            with path.open("rb") as handle:
                for raw in handle:
                    if b'"src":"eval"' not in raw:
                        continue
                    record = json.loads(raw)
                    ts = str(record.get("ts"))
                    if record.get("req") in requests and ts in wanted and ts not in work.state["records"]:
                        info = record.get("info") or {}
                        stat = (record.get("stats") or {}).get("20")
                        work.state["records"][ts] = {
                            "rarity": info.get("27"),
                            "sockets": int(stat) if isinstance(stat, (int, float)) else 0,
                            "title": info.get("28"),
                            "build": record.get("build"),
                        }
        got = sum(1 for ts in wanted if ts in work.state["records"])
        if got >= len(wanted):
            break
        still = still + 1 if got == last else 0
        last = got
        if still >= 18:
            print(f"  the game stopped after {got:,} of {len(wanted):,}; the rest waits for the next run", flush=True)
            break
        print(f"  {got:,}/{len(wanted):,}", flush=True)
        time.sleep(10)
    work.save()
    return got


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=HERE / "hs_game_seeds.json")
    parser.add_argument("--root", type=Path,
                        default=Path(os.environ.get("LOCALAPPDATA", "")) / "Hero_Siege" / "itemtruth")
    parser.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "hs_game_seed_table" / "work.json")
    parser.add_argument("--max-per-run", type=int, default=0,
                        help="stop after this many items (0, the default: no limit - the game keeps none of them)")
    parser.add_argument("--white-candidates", type=int, default=100)
    parser.add_argument("--unique-candidates", type=int, default=200)
    parser.add_argument("--natural-samples", type=int, default=150)
    parser.add_argument("--scan", type=int, default=30_000_000, help="CPR seeds to rank")
    args = parser.parse_args()
    status = game_truth.capture_status(args.root)
    if not status.get("requested") or not status.get("reporting"):
        raise SystemExit("Item Truth is not running: start Hero Siege with ForgePact 1.4.5 or later "
                         "and turn on Game truth in the editor")

    editor = _load_editor()
    work = Work(args.work)
    budget = args.max_per_run if args.max_per_run > 0 else None
    rng = random.Random(20260926)

    def white_data(row: dict, seed: int) -> dict:
        cls = int(row["cls"])
        return {"w": 1.0, "a": float(seed), "j": float(int(row.get("sub", 0)) if cls == 3 else 0),
                "b": float(row["b"]), "c": 0.0, "o": 1.0}

    def unique_data(row: dict, seed: float) -> dict:
        return {"w": 1.0, "a": float(seed), "j": float(int(row.get("sub", 0)) if int(row["cls"]) == 3 else 0),
                "b": float(row["b"]), "c": 1.0, "m": 1.0}

    white_rows = [row for row in editor.CAT if row.get("kind") == "normal" and row.get("available", True)
                  and int(row["cls"]) in EQUIPMENT]
    unique_rows = {}
    for row in editor.CAT:
        if row.get("kind") != "unique":
            continue
        profile = row.get("rollProfile") or editor.catalog_roll_profile(row)
        if editor.socket_roll_for_profile(profile) is not None or (
            int(row["cls"]) in EQUIPMENT and (profile or {}).get("maxSockets")
        ):
            unique_rows[editor.game_seed_address(row)] = (row, profile)

    # phase 1: white candidates, natural samples, uniques with their CPR-solved seed
    if work.phase("measure") is None:
        signatures: dict[tuple, int] = {}
        for row in white_rows:
            bounds = a_chain_bounds(row.get("rollProfile") or editor.catalog_roll_profile(row))
            if bounds:
                signatures[tuple(bounds)] = args.white_candidates
        ranked = ranked_seeds(signatures, args.scan)
        items = []
        for row in white_rows:
            address = editor.game_seed_address(row)
            bounds = a_chain_bounds(row.get("rollProfile") or editor.catalog_roll_profile(row))
            total = sum(1 for bound in bounds if bound is not None) if bounds else 0
            cands = ranked.get(tuple(bounds), []) if bounds else [
                (rng.randint(1, 2_000_000_000), 0, 0) for _ in range(args.white_candidates)
            ]
            for seed, maxed, deficit in cands[: args.white_candidates]:
                items.append((row["cls"], white_data(row, seed), ["white", address, row["name"], seed, maxed, total, deficit]))
            for _ in range(args.natural_samples):
                items.append((row["cls"], white_data(row, rng.randint(1, 2_000_000_000)), ["natural", address]))
        for address, (row, profile) in unique_rows.items():
            seeds = editor.roll_profile_field_seeds(profile)
            socket_seed = editor.socket_seed_for_profile(profile)
            if socket_seed is not None:
                seeds["a"] = float(socket_seed)
            if "a" not in seeds:
                continue
            claim = editor.socket_roll_for_profile(profile)
            claim = int(claim["maxSockets"]) if claim else int(profile.get("maxSockets") or 0)
            items.append((row["cls"], unique_data(row, seeds["a"]), ["unique", address, row["name"], int(seeds["a"]), claim]))
        work.plan("measure", items)
    budget = spend(budget, measure(work, args.root, work.missing("measure"), budget))
    if work.missing("measure"):
        return pause(work)

    white = collections.defaultdict(list)
    natural = collections.Counter()
    unique = {}
    build_id = None
    for item in work.phase("measure"):
        record, label = work.record(item["ts"]), item["label"]
        build_id = build_id or record.get("build")
        if label[0] == "natural":
            natural[label[1]] = max(natural[label[1]], record["sockets"])
        elif label[0] == "white":
            _kind, address, name, seed, maxed, total, deficit = label
            natural[address] = max(natural[address], record["sockets"])
            if record["rarity"] == 1 and record["sockets"] == 0:
                white[address].append((seed, maxed, total, deficit))
        else:
            _kind, address, name, seed, claim = label
            unique[address] = {"name": name, "seed": seed, "sockets": record["sockets"], "claim": claim}
    table_white = {}
    for row in white_rows:
        address = editor.game_seed_address(row)
        kept = sorted(white.get(address, []), key=lambda e: (-e[1], e[3], e[0]))[:KEEP]
        if kept:
            table_white[address] = {
                "name": row["name"],
                "seeds": [{"seed": int(s), "maxed": int(m), "total": int(t), "deficit": int(d)} for s, m, t, d in kept],
                "maxSockets": int(natural[address]),
            }

    # phase 2: uniques the game under-sockets - stat-ranked candidates
    if work.phase("short") is None:
        signatures, bounds_of = {}, {}
        for address, entry in unique.items():
            if entry["sockets"] >= entry["claim"]:
                continue
            _row, profile = unique_rows[address]
            socket_entry = editor.socket_roll_for_profile(profile)
            bounds = socket_entry["statBounds"] if socket_entry else a_chain_bounds(profile)
            if bounds:
                bounds_of[address] = tuple(bounds)
                signatures[tuple(bounds)] = args.unique_candidates
        ranked = ranked_seeds(signatures, args.scan) if signatures else {}
        items = []
        for address, bounds in bounds_of.items():
            row, _profile = unique_rows[address]
            total = sum(1 for bound in bounds if bound is not None)
            for seed, maxed, deficit in ranked[bounds]:
                items.append((row["cls"], unique_data(row, seed), [address, seed, maxed, total, deficit]))
        work.plan("short", items)
    budget = spend(budget, measure(work, args.root, work.missing("short"), budget))
    if work.missing("short"):
        return pause(work)
    found = collections.defaultdict(list)
    for item in work.phase("short"):
        address, seed, maxed, total, deficit = item["label"]
        found[address].append((work.record(item["ts"])["sockets"], maxed, total, deficit, seed))
    for address, options in found.items():
        count, maxed, total, deficit, seed = sorted(options, key=lambda o: (-o[0], -o[1], o[3], o[4]))[0]
        current = unique[address]
        if count > current["sockets"]:
            unique[address] = {"name": current["name"], "seed": int(seed), "sockets": int(count),
                               "maxed": int(maxed), "total": int(total), "deficit": int(deficit),
                               "previous": {"seed": current["seed"], "sockets": current["sockets"]},
                               "claim": current["claim"]}

    # phase 3: runewords on the new white seeds
    if work.phase("runewords") is None:
        items = []
        for recipe in editor.RUNEWORDS:
            if editor.runeword_generation_blocker(recipe):
                continue
            for base in editor.runeword_base_candidates(recipe):
                profile = editor.runeword_profile(recipe, base)
                address = editor.game_seed_address(base)
                if profile is None or address not in table_white:
                    continue
                data = white_data(base, table_white[address]["seeds"][0]["seed"])
                data.pop("o", None)
                data.update({"i": editor.roll_profile_field_seeds(profile).get("i", float(rng.randint(1, 10 ** 9))),
                             "d": 0.0, "e": 0.0, "n": 0.0, "zz": {"sockets": float(len(recipe["runes"]))}})
                for index, rune in enumerate(recipe["runes"], 1):
                    payload = json.dumps({"a": rng.randint(1, 10 ** 9), "b": int(rune["b"]), "n": 0}, separators=(",", ":"))
                    data[f"s{index}"] = base64.b64encode(payload.encode()).decode()
                items.append((base["cls"], data, [str(int(recipe["rw"])), recipe["name"], address, len(recipe["runes"])]))
        work.plan("runewords", items)
    budget = spend(budget, measure(work, args.root, work.missing("runewords"), budget))
    if work.missing("runewords"):
        return pause(work)
    blocked = collections.defaultdict(set)
    for item in work.phase("runewords"):
        recipe_id, name, address, runes = item["label"]
        record = work.record(item["ts"])
        if record["rarity"] == 1 and record["sockets"] == runes and name.casefold() not in str(record["title"]).casefold():
            blocked[recipe_id].add(address)

    def ordered(entries: dict) -> dict:
        return dict(sorted(entries.items(), key=lambda x: tuple(int(v) for v in x[0].split(":"))))

    document = {
        "schemaVersion": 1,
        "note": ("Seeds the running game itself built, through its own save loader (ForgePact Item Truth "
                 "evaluation requests): per white equipment base, `a` seeds that come out Common and roll no "
                 "socket, best CPR stat score first, and the most sockets the game rolled for the base; per "
                 "unique the editor claims sockets on, the `a` seed to use and the socket count the game gives "
                 "it; runeword x base pairs the game did not form. A non-unique item (c 0) shows the larger of "
                 "zz.sockets and its seed's count; a unique (c 1) shows its seed's count and never reads "
                 "zz.sockets; filled socket payloads never add a socket. Built by build_game_seed_table.py."),
        "build": build_id,
        "generated": time.strftime("%Y-%m-%d"),
        "white": ordered(table_white),
        "unique": ordered({address: {k: v for k, v in entry.items() if k != "claim"} for address, entry in unique.items()}),
        "runewordBlocked": {recipe: sorted(addresses) for recipe, addresses in sorted(blocked.items(), key=lambda x: int(x[0]))},
    }
    args.out.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(table_white)} white bases, {len(unique)} uniques, "
          f"{sum(len(v) for v in blocked.values())} blocked runeword bases, game build {build_id}")
    return 0


def pause(work: Work) -> int:
    left = sum(len(work.missing(name)) for name in PHASE_TS if work.phase(name) is not None)
    print(f"{left:,} items still to measure: run this again, with Hero Siege at the main menu and Game truth on "
          f"(the work is kept in {work.path}).", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
