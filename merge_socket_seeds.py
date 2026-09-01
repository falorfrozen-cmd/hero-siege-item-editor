#!/usr/bin/env python3
"""Merge searched socket seeds into hs_socket_seeds.json.

Existing entries are kept unless the search found a strictly better one, so
seeds that are already in use never silently revert. Every entry is re-verified
against the CPR model before it is written: an entry that does not actually
produce the item's maximum socket count is dropped rather than shipped.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

from socket_chain import predict_sockets


def verify(entry: dict) -> bool:
    try:
        got = predict_sockets(entry["seed"], entry["statBounds"], entry["maxSockets"])
    except Exception:
        return False
    return got == entry["maxSockets"] == entry.get("sockets")


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("searched", type=Path)
    parser.add_argument("--table", type=Path, default=here / "hs_socket_seeds.json")
    parser.add_argument("--capture-count", type=int, default=0)
    args = parser.parse_args()

    document = json.loads(args.table.read_text(encoding="utf-8"))
    seeds: dict = document.setdefault("seeds", {})
    searched: dict = json.loads(args.searched.read_text(encoding="utf-8"))

    kept = added = replaced = rejected = 0
    for key, entry in searched.items():
        if not verify(entry):
            rejected += 1
            continue
        current = seeds.get(key)
        if current is None:
            seeds[key] = entry
            added += 1
            continue
        # Prefer whichever keeps more stats on their endpoint.
        better = (entry.get("maxed", -1), -entry.get("endpointDeficit", 10**6)) > (
            current.get("maxed", -1), -current.get("endpointDeficit", 10**6))
        if better:
            seeds[key] = entry
            replaced += 1
        else:
            kept += 1

    stale = [key for key, entry in seeds.items() if not verify(entry)]
    for key in stale:
        del seeds[key]

    if args.capture_count:
        document.setdefault("measuredAgainst", {})["method"] = (
            f"ForgePact socketprobe, {args.capture_count} created items")
    document["generated"] = datetime.now().strftime("%Y-%m-%d")

    backup = args.table.with_suffix(
        f".json.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copyfile(args.table, backup)
    args.table.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    print(f"added {added}, replaced {replaced}, kept {kept}, "
          f"rejected {rejected}, dropped stale {len(stale)}")
    print(f"table now holds {len(seeds)} verified socket seeds")
    print(f"backup: {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
