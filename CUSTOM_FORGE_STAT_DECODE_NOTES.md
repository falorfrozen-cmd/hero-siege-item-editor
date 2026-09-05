# Custom Item Forge — stat key decode notes (Season 10, exe 438BF484…)

Working notes for `hs_stat_semantics_s10.json`. Everything here was read from
the executable the editor is bound to
(`Hero-Siege Tracker/Hero-Siege-AnkerGames (1)/HeroSiege/bin/Hero_Siege.exe`,
281,599,488 bytes, SHA-256 `438BF484…`) and from the game's own localization
files. No guessing from value ranges or item themes.

## Where the names live

- Player-facing text is **not** in the exe or in `data.win`. It is in
  `bin/translations*.csv` (16 files, `|`-separated, columns
  `[section]|en|fi|pt|ru|zh|ja|ko|de|fr|sp|pl`).
- `translationsAttributes.csv` → `[Item Stats]` (344 `stat_*` keys),
  `[Global Stats]` (388), `[Stat Explanations]` (89 `stat_desc_*`).
- `translationsMain.csv` → the proc suffixes `when_attacking`, `when_strike`,
  `when_spellhit`, `when_kill`, `when_casting`, `when_struck`, `when_blocking`.
- `translationsEther.csv` → `inc_*` (`_f` flat, `_p` percent);
  `translationsItem.csv` → `prefix_*` / `suffix_*` affix names.

## Why a plain string search finds nothing

YYC does not embed GML string literals where they are used. Each literal gets a
tiny initializer thunk at the start of `.text`:

```
sub rsp,28h ; lea rdx,[C-string] ; lea rcx,[16-byte RValue slot] ; call 0xB49BB40 ; ...
```

The code references the **slot**, which is zero in the image (a scanner sees
`DBL 0`). Parsing the 33,088 thunks gives slot → text, after which every
string constant in every function becomes readable (`yyscan.py`).

Variable and script names are reached through the variable table: the id
dword the code loads sits **8 bytes before** the name entry (`name_ptr − 8`).
That is also how `SetItemStat`, `GetBaseItemStat`, `GPV` are called — via the
table, not with a direct `call` — which is why direct-call scans found nothing.

## How a stat line is rendered (the mapping evidence)

`gml_Script_DrawInventoryItemV2` (inventory tooltip, 1.69 MB) draws one block
per stat:

```
STR  stat_enhanced_damage        ← localization key
INT  28                          ← itemStatStruct key
CALL GetLocalized
INT  2   INT  8                  ← format codes (2 = percent style, 3 = flat "+N to")
CALL DrawInventoryStatsNew
```

`gml_Script_GetItemTooltipString` (chat/link tooltip, 770 KB) has the same
`STR → INT → GetLocalized` structure. 302 keys were extracted from the first,
220 from the second; on the 205 keys both contain, they agree 205/205.

Flag-style lines use the reverse order `INT key → STR stat_* → GetLocalized`
(e.g. `102 → stat_monsters_rest_in_peace`, `288 → stat_cannot_be_frozen`,
`291 → stat_double_jump`).

## Multi-key families (read from the same code)

- **Trigger procs** (`GetItemTooltipString`): block order is
  `INT chance → "chance" "% " → "cast" → when_X → "level" INT level → INT skill → GetItemSkillDescriptionString`.
  Families: 113/114/115 attacking, 116/117/118 strike, **119/120/121 spellhit
  (not in the original request)**, 122/123/124 kill, 125/126/127 casting,
  185/186/187 struck, 188/189/190 blocking — roles skill / level / chance.
- **Skill grants** (`DrawInventoryItemV2`): `INT skill → GetTalentInfo(skill, 29) → DrawSkillDescription → INT level (fmt 3,8) → DrawInventoryStatsNew`,
  and a third key passed to `GetClassLocalizationKey` = class restriction.
  Triples: (202,203,204) (205,206,207) (208,209,210) (211,212,213)
  (214,215,216) (217,218,219); also (444,445,·) and (462,463,464).
- **Sub-skill grants**: (315,316) (319,320) (321,322) (323,324) (325,326) —
  `INT id → "level" → GetTalentDescription`, `GetTalentInfo(id, 3)`.
- **Damage over time taken**: 279 (% of incoming damage) + 280 (seconds).
- **Projectiles**: 413 extra projectiles, 414 chaining (times), 415 forking,
  416 projectile return, 418 projectile direction, 77 piercing.
- **Base weapon damage**: 447 with 22 (`stat_base_damage`), 448/449 min (flat/%),
  450/451 max (flat/%) — all handled inside `DrawInventoryStatsNew`.

## Corrections to the existing catalog labels

The runtime-harvested labels in `hs_custom_forge_catalog.json` are shifted or
paraphrased for 43 keys (e.g. 52 is `to Life`, 53 is `Life Increased by`,
72 is `Increased Critical Strike Chance`, 79–81 are the "Extra Damage to …"
lines). The code-derived names replace them. The catalog `percent` flag
disagrees with the code's format code for 88 keys; the format code is what the
game uses to draw the `%`, so it wins.

## Still unknown

Keys never referenced by either renderer (e.g. 108, 110) are internal; they are
marked `unknown` with the functions that read them listed as hints.
