"""Socket count prediction and seed search for Hero Siege S10 items.

Measured live against the AnkerGames build
(`Hero_Siege.exe.aurie_backup`, sha256 2034fad4..., 281,773,056 bytes) with the
ForgePact plugin's `socketprobe` command, over 200 created items.

How the game decides an item's socket count
-------------------------------------------
`CreateItemNew` seeds CPR itself, at `0x6EDE9A`, by calling `CreateItemInit`
(a 0x180-byte wrapper whose entire body is `cpr_init(arg0)`, and whose only call
site in the whole executable is that one).  The seed it receives is the item's
own ``a`` field.  From that point the draws run in a fixed order:

    <one draw per variable stat>        the rolls the profile database models
    2, 4, 2, 4, 2, 4, 2, 4              eight draws with constant bounds
    <the socket draw>                   ONE draw, bound = maxSockets - 1

and the socket count the game finally shows is ``socket_draw + 1``.

Only one socket draw is taken.  `0x6F10C5` is an if/else: the if-branch draws
with the item's own bound at `0x6F12CD` and then jumps past the else-branch,
which is a fixed `cpr_irandom(1)` at `0x6F13A3` (0 or 1, i.e. 1 or 2 sockets)
used when the base has no per-tier socket entry.

Immediately after the socket draw, `0x6F22C9` calls `cpr_init` again and
re-seeds.  That is why nothing written into the save ever changed the socket
count: the socket draw is taken *before* the seed the editor controls is
applied, so `zz.sockets`, a synthesized ``s`` field and hand-written ``s1..s6``
are all overwritten by a number that was already decided.

Verified: replaying this chain with :func:`roll_profile_db.evaluate_seed`
reproduces the game's draws exactly, through and including the socket draw, for
110 of the 126 probed items that start their own CPR stream.  The 16 that do not
are socketable gems, which are seeded from their parent's socket slot rather
than from their own ``a``.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from roll_profile_db import evaluate_seed

__all__ = [
    "FIXED_TAIL_BOUNDS",
    "socket_bound_for",
    "socket_signature",
    "predict_sockets",
    "search_socket_seed",
]

# The eight draws that always sit between the stat rolls and the socket draw.
# Same bounds, same order, in every probed item.
FIXED_TAIL_BOUNDS = (2, 4, 2, 4, 2, 4, 2, 4)


def socket_bound_for(max_sockets: int) -> int:
    """Return the bound the game draws with for an item of this capacity."""
    if not isinstance(max_sockets, int) or isinstance(max_sockets, bool):
        raise ValueError("max_sockets must be an integer")
    if max_sockets < 1:
        raise ValueError("max_sockets must be >= 1")
    return max_sockets - 1


def socket_signature(
    stat_events: Sequence[int | None] | int,
    max_sockets: int,
) -> list[int | None]:
    """Build a signature for :func:`roll_profile_db.evaluate_seed`.

    ``stat_events`` is either the item's stat bounds in the order the game draws
    them, or simply how many stat draws it takes.  Only the count matters for
    the socket result -- unscored ``None`` events advance CPR just the same --
    so passing an integer is enough when the bounds are not known.
    """
    if isinstance(stat_events, int) and not isinstance(stat_events, bool):
        if stat_events < 0:
            raise ValueError("stat draw count must be >= 0")
        head: list[int | None] = [None] * stat_events
    else:
        head = list(stat_events)
    return head + [None] * len(FIXED_TAIL_BOUNDS) + [socket_bound_for(max_sockets)]


def predict_sockets(seed: int, stat_events: Sequence[int | None] | int,
                    max_sockets: int) -> int:
    """Return the socket count the game will generate for this item seed."""
    signature = socket_signature(stat_events, max_sockets)
    return int(evaluate_seed(seed, signature).event_rolls[-1]) + 1


def search_socket_seed(
    stat_bounds: Sequence[int],
    max_sockets: int,
    *,
    start: int = 1,
    stop: int = 1_000_000_000,
    require_max: bool = True,
) -> dict | None:
    """Find the seed that maxes the socket draw and keeps the stats best.

    Ranked the same way the profile database ranks: most stats landing on their
    endpoint, then least total deficit, then smallest seed.  Returns ``None``
    when nothing in the range satisfies ``require_max``.

    ``stat_bounds`` must be in the order the game draws them, which is not
    necessarily the order stored in ``hs_perfect_roll_profiles.json`` -- that
    file was generated against a different executable (its ``exeSha256`` matches
    neither the AnkerGames nor the Tracker build) and its ordering has since
    shifted.
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy ships with the editor
        return _search_socket_seed_slow(stat_bounds, max_sockets, start, stop,
                                        require_max)

    mult, inc, mod, mask, top = (
        1_789_570_533.0, 465_707.0, 2_147_483_648.0, 0x3FFFFFFF, 1_073_741_823.0,
    )
    want = socket_bound_for(max_sockets)
    bounds = np.asarray(stat_bounds, dtype=np.float64)
    best = None

    def advance(state: "np.ndarray") -> "np.ndarray":
        state = np.fmod(mult * state + inc, mod)
        return np.bitwise_and(state.astype(np.int64), mask).astype(np.float64)

    for chunk_start in range(start, stop, 4_000_000):
        chunk_stop = min(chunk_start + 4_000_000, stop)
        seeds = np.arange(chunk_start, chunk_stop, dtype=np.int64)
        state = seeds.astype(np.float64)
        maxed = np.zeros(len(seeds), dtype=np.int16)
        deficit = np.zeros(len(seeds), dtype=np.int32)
        for bound in bounds:
            state = advance(state)
            roll = np.floor((bound + 0.99999) * (state / top))
            maxed += roll == bound
            deficit += (bound - roll).astype(np.int32)
        for _ in FIXED_TAIL_BOUNDS:
            state = advance(state)
        state = advance(state)
        draw = np.floor((want + 0.99999) * (state / top))

        keep = np.flatnonzero(draw == want) if require_max else np.arange(len(seeds))
        if not len(keep):
            continue
        order = np.lexsort((seeds[keep], deficit[keep], -maxed[keep]))
        i = keep[order[0]]
        rank = (int(maxed[i]), -int(deficit[i]), -int(seeds[i]))
        if best is None or rank > best[0]:
            best = (rank, dict(seed=int(seeds[i]), maxed=int(maxed[i]),
                               total=len(bounds), deficit=int(deficit[i]),
                               sockets=max_sockets))
    return best[1] if best else None


def _search_socket_seed_slow(stat_bounds, max_sockets, start, stop, require_max):
    mult, inc, mod, mask, top = (
        1_789_570_533.0, 465_707.0, 2_147_483_648.0, 0x3FFFFFFF, 1_073_741_823.0,
    )
    want = socket_bound_for(max_sockets)
    best = None
    for seed in range(start, stop):
        state = float(seed)
        maxed = deficit = 0
        for bound in stat_bounds:
            state = float(int(math.fmod(mult * state + inc, mod)) & mask)
            roll = math.floor((bound + 0.99999) * (state / top))
            maxed += roll == bound
            deficit += bound - roll
        for _ in range(len(FIXED_TAIL_BOUNDS) + 1):
            state = float(int(math.fmod(mult * state + inc, mod)) & mask)
        draw = math.floor((want + 0.99999) * (state / top))
        if require_max and draw != want:
            continue
        rank = (maxed, -deficit, -seed)
        if best is None or rank > best[0]:
            best = (rank, dict(seed=seed, maxed=maxed, total=len(stat_bounds),
                               deficit=deficit, sockets=max_sockets))
    return best[1] if best else None
