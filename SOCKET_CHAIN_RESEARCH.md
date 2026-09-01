# Socket count (stat 20) — static analysis

Build analysed: `Hero-Siege Tracker/.../Hero_Siege.exe`, 281,882,112 bytes,
SHA-256 starts `759CE2F2…`, image base `0x140000000`. All addresses are RVAs.

## Why nothing we wrote into the save ever worked

Socket count is **stat 20** and it is *generated*, never loaded.

- The constant `20` used for this stat lives at `0x1065D380`. It has exactly
  **11 references in the whole executable**: 10 in `gml_Script_GenerateItemCodex`
  and 1 in `gml_Script_GenerateItemSpecialStats`. Both are generation code.
  Nothing on the load path touches it.
- `gml_Script_InitItemFromJson` (`0x3D6AAC0`) — the save loader — calls
  `GetSocketKey` only to read the `s1…s6` socket *contents*. It never reads a
  socket *count* field.
- Every reader of the `sockets` variable (9 sites: `LoadCommonItems`,
  `CreateItemNew`, `UiACraftButton`, `DefineCraftingCombos`,
  `ConvertItemFromOldFormat`) takes it from the **item definition** via
  `GetItemDef` → `struct_get_from_hash(def, "sockets")`, not from save data.
- `socket_roll_seed` is referenced only inside `ConvertItemFromOldFormat`
  (legacy format conversion). It is dead for current saves.

So `zz.sockets`, a synthesized `s` field, and hand-written `s1…s6` are all
overwritten: the game recomputes stat 20 from the item seed on every load.

## Where the socket value actually comes from

In `gml_Script_CreateItemNew` (`0x6E97F0`, size `0x8C40`):

| address | what |
| --- | --- |
| `0x6ECFEF` | `GetItemDef` |
| `0x6ED067` | reads `def.sockets` (`struct_get_from_hash` at `0x6ED087`) |
| `0x6ED16E` | `is_undefined` guard |
| `0x6ED2A4` | **`SetItemStat(item, 20, …)`** — the writer |
| `0x6ED2E2` | `GetItemDef` again |
| `0x6ED35A` | reads `def.sockets` again, with `GMLINT 20` at `0x6ED377` |
| `0x6ED4A8` | **`GetBaseItemStat(base, 20)`** (`GMLINT 20` at `0x6ED4B7`) — the range |
| `0x6ED610` | `is_undefined` |
| `0x6ED6D3` | `is_array` |
| **`0x6ED88D`** | **`cpr_irandom`** — socket roll #1 |
| **`0x6ED963`** | **`cpr_irandom`** — socket roll #2 (a `DBL 1.0` is staged at `0x6ED92C`) |

`gml_Script_GenerateItemSpecialStats` (`0x701490`) contains **no `cpr_` call at
all** between its two `itemBaseSocketStatStruct` references
(`0x7031D7`…`0x703FE3`). Its socket block only *reads* the stat
(`GetItemStat` slot `0x10809498`, used at `0x703B41` with `GMLINT 20` at
`0x703B4C`) and resolves `s1…s6` keys via `GetSocketKey` (`0x703C94`). It is
validation, not generation.

## Chain position — the number the solver needs

`cpr_*` call order inside `CreateItemNew`:

| # | address | routine |
| --- | --- | --- |
| 0 | `0x6EC4CC` | `cpr_irandom` |
| 1 | `0x6EC94E` | `cpr_irandom` |
| **2** | **`0x6ED88D`** | **`cpr_irandom` — SOCKET** |
| **3** | **`0x6ED963`** | **`cpr_irandom` — SOCKET** |
| 4 | `0x6EE889` | `cpr_init` |
| 5 | `0x6EE8F9` | `cpr_irandom` |
| 6 | `0x6EE995` | `cpr_irandom` |
| 7 | `0x6EEB27` | `cpr_irandom` |
| 8 | `0x6EEBA4` | `cpr_irandom` |
| 9 | `0x6EEFA7` | `cpr_init` |

The two socket rolls are **chain slots 2 and 3**, before the first `cpr_init`.

## `cpr_irandom`

`gml_Script_cpr_irandom` @ `0x71CBC0`, size `0x9B0`:

- calls `gml_Script_cpr_rand32` (`0x71DF40`) at `0x71CCB1`
- reads the global `cpr_rand_max` (`0x71CC5C`)
- reads `gDataProtected` (`0x71CDBE`)
- applies `floor` (`0x71CE64`) against `DBL 0.99999` (`0x71CEB8`)

i.e. the usual `floor(rand32 / rand_max * (bound + 0.99999))` scaling.

## What this means for the profile database

`hs_perfect_roll_profiles.json` reports `coverage.socketSeedChains: 0`, and its
audit trace marks the socket phase `'scored': False`. The solver walks past
slots 2 and 3 without scoring them, so the winning `a` seed maximises the other
stats and takes whatever socket count falls out.

Proof from the data itself: several **`exact`-mode** profiles (all variable
stats maxed, `endpointDeficit: 0`) still sit below their own `maxSockets` —
Death Lord's Crown `maxSockets 3`, Surgical Mask `maxSockets 2`, Underworld's
Skullbreaker `maxSockets 3`. If sockets were scored, that could not happen.

## The fix

Score chain slots 2 and 3 in the solver and re-search seeds with sockets as a
second objective. Expect trade-offs: sockets and the stats come out of one seed,
so a socket-max seed will usually cost a point or two elsewhere.


## Verified with the existing simulator

`roll_profile_db.evaluate_seed()` reproduces both stored profiles exactly
(Poison Ivy seed 4677950 and St. Ahto seed 17303869 → stored `eventRolls`
byte-for-byte). So the CPR model in the editor is correct and reusable.

Tested and **rejected**: "the last scored signature entry is the socket roll".
Across the 383 profiles that carry `maxSockets`, `last_scored + 1 == maxSockets`
holds for only 72 and fails for 311. The socket events are genuinely absent from
the recorded signatures, matching `coverage.socketSeedChains: 0`.

## What is still missing

`gml_Script_SetBaseItemSocketStat@anon@53834@s_ItemBaseDefinition@DefineStructs`
(`0x12F3170`) is only a *setter*: it writes into `itemBaseSocketStatStruct` via
`variable_struct_get`/`variable_struct_set`. The per-base socket **range**
(min/max) is supplied by its callers — the hundreds of `DefineItemNormal*` /
`DefineItemUnique*` scripts — as literal arguments.

To score the socket rolls the solver needs, per base item:
1. the socket range (min and max) — extractable, but it means decoding those
   Define* call sites;
2. how the two `cpr_irandom` results at chain slots 2 and 3 combine into the
   final count.

Only then can seeds be re-searched with sockets as a scored objective. Expect
trade-offs: the stats and the sockets come from one seed.

---

# Correction and root cause (AnkerGames "Latest" build)

Build analysed: `Latest/Hero-Siege-AnkerGames (1)/HeroSiege/bin/Hero_Siege.exe.aurie_backup`
(281,773,056 bytes). `CreateItemNew` @ `0x6ED230`, size `0x8C40` — the same
in-function offsets as the Tracker build, so both sections describe the same code.

## There is only ONE socket roll, not two

The earlier note listed chain slots 2 and 3 as "socket roll #1 and #2". They are
not two rolls: they are the two sides of a single `if/else`.

- `0x6F10BE` computes the condition, `0x6F10C5  je 0x6F132F` takes the else side.
- **if-branch**: `0x6F12CD  call cpr_irandom` with a *dynamic* bound built at
  `0x6F1287`, then `0x6F1305  jmp 0x6F13B7` — which jumps **past** the second call.
- **else-branch**: `0x6F136C` stages a literal `DBL 1.0` and `0x6F13A3` calls
  `cpr_irandom(1)` — a 0-or-1 result.

So exactly one draw decides the socket count. Which branch runs is what decides
whether an item can exceed one socket at all.

The condition is fed from `0x6F0EE8` (`VAR tier`) plus `GMLINT 20` at `0x6F0EF7`,
i.e. the per-base socket entry for that tier; when it is absent the else-branch's
fixed `cpr_irandom(1)` is used.

## Why the profile seed never controlled sockets

`CreateItemNew` seeds the RNG itself, and it does so *twice*:

| order | address | what |
| --- | --- | --- |
| 1 | `0x6EDE9A` | `CreateItemInit(seed)` — a 0x180-byte wrapper whose only body is `cpr_init(arg0)` (`0x6F608D`). Its **only call site in the whole executable** is this one. |
| 2 | `0x6EF903` | `LoadCommonItems` — consumes a large, item-dependent number of draws (it holds 964 `cpr_irandom` sites; it is one 0xFC2E0-byte function containing every item definition inline, selected by a switch) |
| 3 | `0x6EFF0C`, `0x6F038E` | two draws |
| 4 | **`0x6F12CD` / `0x6F13A3`** | **the socket draw** |
| 5 | `0x6F22C9` | `cpr_init` — **re-seeds**, and the stat rolls follow |
| 6 | `0x6F29E7` | `cpr_init` again |

The editor's `roll_profile_db.evaluate_seed()` models the stream that begins at
step 5, which is why it reproduces `eventRolls` byte-for-byte yet has no
influence on sockets: **the socket draw happens before that re-seed**, on the
stream established at step 1 and already advanced by an unknown number of draws
in step 2.

This, not the save format, is why `zz.sockets`, a synthesized `s`, and
hand-written `s1…s6` all failed. Nothing written into the save can move a draw
that is taken before the seed the editor controls is even applied.

## What is still needed

Two runtime measurements, both of which the ForgePact plugin's `socketprobe`
command now records per created item:

1. `seed=` — the value `CreateItemInit` passes to `cpr_init`, so the saved field
   that drives the socket stream can be identified.
2. `lc=` — how many draws `LoadCommonItems` consumes for that item, which is the
   offset the simulator has to skip to land on the socket draw.

Plus, from the roll line itself, whether the if-branch (`6F12CD`) or the
else-branch (`6F13A3`) ran, and the if-branch's bound.

Rejected along the way, so they are not retried:
- `itemInfoStruct["21"]` is not the socket count. It is constant per base item
  across all 5,755 drop records, and joining 386 items against the profile
  database's `maxSockets` shows no fixed relation (differences spread -1..5).
  St. Ahto 1 / Poison Ivy 2 are that field's values, matching the inventory
  footprint of a glove and a bow.
- No `itemInfoStruct` key equals `maxSockets`; the best candidate matched
  106/386.

---

# Final editor integration (2026-09-01)

The ForgePact runtime capture resolved the missing measurements above. For the
verified chains, the saved field passed to `CreateItemInit` is `a`. Replaying
the measured draws showed this stable order:

1. one draw per captured current-build stat bound;
2. eight draws with bounds `2,4,2,4,2,4,2,4`;
3. one socket draw with bound `maxSockets - 1`.

The native socket count is the final draw plus one. `socket_chain.py` implements
that replay, and `hs_socket_seeds.json` contains the per-address seeds selected
from the measured chains. Every shipped entry is checked in both directions:
its new seed replays to `maxSockets`, and its recorded previous seed replays to
the observed previous count.

Current verified table:

- 267 address-specific seeds;
- all 267 replay to their measured native maximum;
- 217 also land every captured `a`-chain stat on its maximum endpoint;
- the other 50 are labelled `BEST VERIFIED + MAX SOCKETS` rather than
  `EXACT MAX`;
- none has fewer maximum endpoints or a larger endpoint deficit than its
  recorded previous seed.

Editor rules that must not regress:

- Both new-item generation and right-click Perfect use the same effective
  socket-aware `a` seed. Applying Perfect must never restore the old profile
  seed and reduce sockets.
- The measured table's `maxSockets` overrides the older profile/catalog value.
  Those older values disagree for 256 of the 267 measured addresses.
- `zz.sockets` is editor compatibility/empty-slot metadata. It is kept aligned
  with the measured capacity, but it is not the source of the in-game roll.
- `hs_socket_seeds.json` is a required PyInstaller data asset. Missing or
  malformed data fails at startup instead of silently falling back.
- Torch of Shadows is a combined identity case. Its 24 class seeds must each
  satisfy the selected class, 4/4 variable stats MAX, and 2/2 sockets. The
  class table in `torch_class_selector.py` enforces all three invariants.

Coverage caveat: `_search_all.log` records 265 solved candidates out of 270.
St. Ahto and Poison Ivy were solved separately over the full seed domain and
merged, bringing the shipped table to 267. The complete measured-input capture
was not retained in this repository, so the identities of the remaining three
candidates cannot be reconstructed from the result files alone. Do not claim
universal item coverage until those measurements are recovered or recaptured.
