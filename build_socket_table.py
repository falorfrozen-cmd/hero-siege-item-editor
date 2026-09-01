#!/usr/bin/env python3
"""Turn a ForgePact socketprobe capture into per-item socket seeds.

Input:  bp_ipc/socketchain.jsonl, written by the plugin's `socketprobe` command.
        One record per created item: the item's own CPR seed, how many draws
        LoadCommonItems consumed, every draw inside CreateItemNew (call-site
        RVA, bound, result), and the serialised item.

Output: hs_socket_seeds.json -- for every item whose chain verifies, the seed
        that makes the game roll its maximum socket count while keeping the
        stat rolls as close to their endpoints as the domain allows.

The socket draw is the one taken at the if/else in CreateItemNew.  Its two call
sites return to the RVAs below; everything before it is `nStat` stat draws plus
eight draws with fixed bounds.  The socket count the game shows is draw + 1, so
an item's real capacity is `bound + 1` -- which is measured here rather than
taken from hs_perfect_roll_profiles.json, whose maxSockets values were produced
against a different executable and disagree with this build for most items.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from roll_profile_db import evaluate_seed
from socket_chain import FIXED_TAIL_BOUNDS, search_socket_seed

# Return addresses of the two socket call sites (AnkerGames build).
SOCKET_RETURN_RVAS = {0x6F12D2: "if", 0x6F13A8: "else"}


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def identity(record: dict) -> tuple[str | None, str | None]:
    """Return ``(addressKey, baseName)`` for a probed item, when derivable."""
    item = record.get("it")
    if not isinstance(item, dict):
        return None, None
    definition = item.get("itemDefinitionStruct") or {}
    info = item.get("itemInfoStruct") or {}
    base = info.get("28") or info.get("14")
    cls = item.get("itemType")
    sub, address, unique = definition.get("j"), definition.get("b"), definition.get("c")
    if None in (cls, sub, address):
        return None, base
    kind = "unique" if unique == 1 else "normal"
    return f"{kind}:{int(cls)}:{int(sub)}:{int(address)}", base


def analyse(record: dict) -> dict | None:
    """Verify one chain and return its measured shape, or None if unusable."""
    seed, rolls = record.get("seed"), record.get("rolls")
    if not isinstance(seed, int) or seed < 1 or not isinstance(rolls, list):
        return None
    if record.get("lc"):
        # LoadCommonItems advanced the stream by draws we did not record, so the
        # replay cannot line up and this sample cannot be trusted.
        return None
    index = next(
        (i for i, r in enumerate(rolls)
         if isinstance(r, list) and len(r) == 3 and int(r[0]) in SOCKET_RETURN_RVAS),
        None,
    )
    if index is None or index < len(FIXED_TAIL_BOUNDS):
        return None
    bounds = [int(r[1]) for r in rolls[:index + 1]]
    results = [int(r[2]) for r in rolls[:index + 1]]
    if any(b < 0 for b in bounds):
        return None
    if list(evaluate_seed(seed, bounds).event_rolls) != results:
        return None  # not a chain that starts from its own seed (socketable gems)
    return dict(
        seed=seed,
        nStat=index - len(FIXED_TAIL_BOUNDS),
        statBounds=bounds[:index - len(FIXED_TAIL_BOUNDS)],
        socketBound=bounds[index],
        socketDraw=results[index],
        sockets=results[index] + 1,
        maxSockets=bounds[index] + 1,
        branch=SOCKET_RETURN_RVAS[int(rolls[index][0])],
    )


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="path to socketchain.jsonl")
    parser.add_argument("--out", type=Path, default=here / "hs_socket_seeds.json")
    parser.add_argument("--stop", type=int, default=1_000_000_000,
                        help="seed domain upper bound for the search")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what was measured without searching seeds")
    args = parser.parse_args(argv)

    records = load_records(args.capture)
    print(f"records: {len(records)}")

    measured: dict[str, dict] = {}
    names: dict[str, str] = {}
    unverified = unidentified = 0
    for record in records:
        shape = analyse(record)
        if shape is None:
            unverified += 1
            continue
        key, base = identity(record)
        if key is None:
            unidentified += 1
            continue
        if base:
            names[key] = base
        # Same base item, same chain shape every time; keep the first.
        measured.setdefault(key, shape)

    print(f"verified chains: {sum(1 for _ in measured)} distinct items "
          f"({unverified} unusable, {unidentified} unidentified)")
    already_max = [k for k, v in measured.items() if v["sockets"] == v["maxSockets"]]
    print(f"already at max sockets: {len(already_max)}")

    if args.dry_run:
        for key, shape in sorted(measured.items()):
            print(f"   {names.get(key, key):<44} nStat={shape['nStat']:<3} "
                  f"bound={shape['socketBound']} max={shape['maxSockets']} "
                  f"now={shape['sockets']} ({shape['branch']})")
        return 0

    seeds: dict[str, dict] = {}
    for position, (key, shape) in enumerate(sorted(measured.items()), 1):
        name = names.get(key, key)
        if shape["maxSockets"] < 2:
            continue  # a one-socket item has nothing to search for
        best = search_socket_seed(shape["statBounds"], shape["maxSockets"],
                                  stop=args.stop)
        if best is None:
            print(f"[{position}/{len(measured)}] {name}: no seed in domain")
            continue
        seeds[key] = dict(
            name=name,
            maxSockets=shape["maxSockets"],
            statBounds=shape["statBounds"],
            seed=best["seed"],
            sockets=shape["maxSockets"],
            maxed=best["maxed"],
            total=best["total"],
            endpointDeficit=best["deficit"],
            searchedThrough=args.stop,
            measured=dict(branch=shape["branch"], nStat=shape["nStat"]),
            previous=dict(seed=shape["seed"], sockets=shape["sockets"]),
        )
        print(f"[{position}/{len(measured)}] {name}: seed {best['seed']} -> "
              f"{shape['maxSockets']} sockets, {best['maxed']}/{best['total']} maxed",
              flush=True)

    document = dict(
        schemaVersion=1,
        note=("Item seeds chosen so the game's socket draw lands on the item's "
              "maximum. The socket draw is taken from the `a` chain before the "
              "re-seed that hs_perfect_roll_profiles.json models, so only `a` "
              "can move it; see socket_chain.py and SOCKET_CHAIN_RESEARCH.md."),
        measuredAgainst=dict(
            build="Hero-Siege-AnkerGames (1) / HeroSiege / bin",
            exeSha256="2034fad4096be6de1147e4ff61b942a706673a9567b10c3013c6393ed0686486",
            exeBytes=281773056,
            method=f"ForgePact socketprobe, {len(records)} created items",
        ),
        chain=dict(
            order=("one draw per variable stat, then 8 draws with bounds "
                   "2,4,2,4,2,4,2,4, then the socket draw"),
            socketBound="maxSockets - 1 (measured, not taken from the profile database)",
            socketCount="socket draw + 1",
        ),
        seeds=seeds,
    )
    args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(seeds)} socket seeds -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
