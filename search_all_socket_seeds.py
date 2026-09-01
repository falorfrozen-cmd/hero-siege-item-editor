#!/usr/bin/env python3
"""Search socket-maximising seeds for every measured item at once.

Every item replays the same CPR stream -- only the bounds differ -- so the
per-index states are computed once per block of seeds and reused across all
items.  That turns a per-item full scan into a shared one and makes searching
hundreds of items practical.

A candidate is only accepted when it does not cost stat quality: it must land
on at least as many endpoints, with no more total deficit, than the seed the
item is generated with today.  An item that cannot get more sockets without
giving up stats is reported and left alone.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

MULT, INC, MOD, MASK, TOP = (
    1_789_570_533.0, 465_707.0, 2_147_483_648.0, 0x3FFFFFFF, 1_073_741_823.0,
)
FIXED_TAIL = 8


def baseline(seed: int, stat_bounds: list[int]) -> tuple[int, int]:
    """Stat score of the seed the item is generated with today."""
    state = float(seed)
    maxed = deficit = 0
    for bound in stat_bounds:
        state = float(int(math.fmod(MULT * state + INC, MOD)) & MASK)
        roll = math.floor((bound + 0.99999) * (state / TOP))
        maxed += roll == bound
        deficit += bound - roll
    return maxed, deficit


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measured", type=Path)
    parser.add_argument("--out", type=Path, default=here / "_socket_search_all.json")
    parser.add_argument("--stop", type=int, default=50_000_000)
    parser.add_argument("--block", type=int, default=1_000_000)
    args = parser.parse_args()

    measured = json.loads(args.measured.read_text(encoding="utf-8"))
    items = []
    for key, shape in measured.items():
        if shape["sockets"] >= shape["maxSockets"]:
            continue  # already rolls its maximum
        if shape["maxSockets"] < 2:
            continue
        base_maxed, base_deficit = baseline(shape["seed"], shape["statBounds"])
        items.append((key, shape, base_maxed, base_deficit))
    depth = max(s["nStat"] + FIXED_TAIL for _, s, _, _ in items)
    print(f"items to search: {len(items)}   chain depth: {depth}   "
          f"domain: 1..{args.stop:,}", flush=True)

    best: dict[str, tuple] = {}
    started = time.time()
    for block_start in range(1, args.stop, args.block):
        seeds = np.arange(block_start, min(block_start + args.block, args.stop),
                          dtype=np.int64)
        state = seeds.astype(np.float64)
        states = []
        for _ in range(depth + 1):
            state = np.fmod(MULT * state + INC, MOD)
            state = np.bitwise_and(state.astype(np.int64), MASK).astype(np.float64)
            states.append(state / TOP)

        for key, shape, base_maxed, base_deficit in items:
            socket_bound = shape["socketBound"]
            hit = np.flatnonzero(
                np.floor((socket_bound + 0.99999) * states[shape["nStat"] + FIXED_TAIL])
                == socket_bound
            )
            if not len(hit):
                continue
            maxed = np.zeros(len(hit), dtype=np.int16)
            deficit = np.zeros(len(hit), dtype=np.int32)
            for index, bound in enumerate(shape["statBounds"]):
                roll = np.floor((bound + 0.99999) * states[index][hit])
                maxed += roll == bound
                deficit += (bound - roll).astype(np.int32)
            keep = np.flatnonzero((maxed >= base_maxed) & (deficit <= base_deficit))
            if not len(keep):
                continue
            hit = hit[keep]
            order = np.lexsort((seeds[hit], deficit[keep], -maxed[keep]))
            pick = order[0]
            rank = (int(maxed[keep][pick]), -int(deficit[keep][pick]),
                    -int(seeds[hit][pick]))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, int(seeds[hit][pick]),
                             int(maxed[keep][pick]), int(deficit[keep][pick]))

        done = block_start + args.block - 1
        print(f"  {min(done, args.stop):,}/{args.stop:,} seeds  "
              f"{len(best)}/{len(items)} solved  {time.time() - started:.0f}s",
              flush=True)

    result = {}
    for key, shape, base_maxed, base_deficit in items:
        if key not in best:
            continue
        _, seed, maxed, deficit = best[key]
        result[key] = dict(
            name=shape.get("name", key),
            maxSockets=shape["maxSockets"],
            statBounds=shape["statBounds"],
            seed=seed,
            sockets=shape["maxSockets"],
            maxed=maxed,
            total=len(shape["statBounds"]),
            endpointDeficit=deficit,
            searchedThrough=args.stop,
            measured=dict(branch=shape["branch"], nStat=shape["nStat"]),
            previous=dict(seed=shape["seed"], sockets=shape["sockets"],
                          maxed=base_maxed, endpointDeficit=base_deficit),
        )
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"solved {len(result)}/{len(items)} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
