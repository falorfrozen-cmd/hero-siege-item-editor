#!/usr/bin/env python3
"""Hero Siege Item Editor GUI - local web interface.

Run with:  py hs_item_editor_gui.py   ->  http://127.0.0.1:8765 in a browser
Never writes to a save while the game is RUNNING (view only). Every write takes
an automatic backup.
Data source: the game's own item repositories (itemRepoNormal/Unique/Runeword dump).
"""

import base64
import copy
import hashlib
import html as html_lib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager, nullcontext
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen

try:
    from roll_profile_db import (
        EXPECTED_EXE_SHA256 as EXPECTED_GAME_EXE_SHA256,
        load_roll_profile_database,
    )
except ModuleNotFoundError:  # package-style import used by the unit tests
    from HSItemEditor.roll_profile_db import (
        EXPECTED_EXE_SHA256 as EXPECTED_GAME_EXE_SHA256,
        load_roll_profile_database,
    )

try:
    from dice_skill_selector import (
        DiceSkillValidationError,
        EXPECTED_EXE_SHA256 as DICE_EXPECTED_GAME_EXE_SHA256,
        LOADED_PROFILE_ID,
        OVERLOADED_PROFILE_ID,
        load_dice_skill_database,
        profile_id_for_address as dice_profile_id_for_address,
    )
except ModuleNotFoundError:  # package-style import used by the unit tests
    from HSItemEditor.dice_skill_selector import (
        DiceSkillValidationError,
        EXPECTED_EXE_SHA256 as DICE_EXPECTED_GAME_EXE_SHA256,
        LOADED_PROFILE_ID,
        OVERLOADED_PROFILE_ID,
        load_dice_skill_database,
        profile_id_for_address as dice_profile_id_for_address,
    )

try:
    from torch_class_selector import (
        TORCH_PROFILE_ID,
        TorchClassValidationError,
        load_torch_class_database,
        profile_id_for_address as torch_profile_id_for_address,
    )
except ModuleNotFoundError:  # package-style import used by the unit tests
    from HSItemEditor.torch_class_selector import (
        TORCH_PROFILE_ID,
        TorchClassValidationError,
        load_torch_class_database,
        profile_id_for_address as torch_profile_id_for_address,
    )

try:
    from hss_recovery import (
        HSSRecoveryError,
        MAX_HSS_DECODED_BYTES,
        MAX_HSS_FILE_BYTES,
        RecoveryPlan,
        analyze_stash_hss,
        decode_hss_bytes_strict,
        materialize_recovery,
        validate_stash_document,
    )
except ModuleNotFoundError:
    from HSItemEditor.hss_recovery import (
        HSSRecoveryError,
        MAX_HSS_DECODED_BYTES,
        MAX_HSS_FILE_BYTES,
        RecoveryPlan,
        analyze_stash_hss,
        decode_hss_bytes_strict,
        materialize_recovery,
        validate_stash_document,
    )

try:
    from infinite_vault import (
        InfiniteVault,
        MAX_TEXT_FIELD_LENGTH,
        VaultConflictError,
        VaultError,
        VaultNotFoundError,
        VaultStateError,
        VaultValidationError,
        canonical_request_hash,
        validate_raw_item_json,
    )
except ModuleNotFoundError:
    from HSItemEditor.infinite_vault import (
        InfiniteVault,
        MAX_TEXT_FIELD_LENGTH,
        VaultConflictError,
        VaultError,
        VaultNotFoundError,
        VaultStateError,
        VaultValidationError,
        canonical_request_hash,
        validate_raw_item_json,
    )

try:
    from exact_tooltip import (
        build_tooltip_model as _build_exact_tooltip_model,
        load_tooltip_model_database as _load_tooltip_model_database,
    )
except ModuleNotFoundError:
    try:
        from HSItemEditor.exact_tooltip import (
            build_tooltip_model as _build_exact_tooltip_model,
            load_tooltip_model_database as _load_tooltip_model_database,
        )
    except ModuleNotFoundError:
        # Source checkouts made before the exact-tooltip bundle existed must
        # remain usable.  The UI will show an explicit catalog-only preview.
        _build_exact_tooltip_model = None
        _load_tooltip_model_database = None

ROOT = Path.home() / "AppData" / "Local" / "Hero_Siege"
SAVES = ROOT / "hs2saves"
VAULT_DB_FILE = ROOT / "hs_infinite_vault.sqlite3"


def _resource_base() -> Path:
    """Return the only directory from which immutable editor assets are loaded."""
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if not bundle_root:
            raise RuntimeError("frozen editor has no PyInstaller resource directory")
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parent


# PyInstaller: bundled data _MEIPASS'ta; kaynak: script klasorunde.
BASE = _resource_base()
CATALOG_FILE = BASE / "hs_full_catalog.json"
PORT = 8765
APP_VERSION = "2.11.4-s10"
APPLICATION_ID = "hero-siege-item-editor"
CATALOG_PROFILE = "Season 10"
MAX_POST_BYTES = 2 * 1024 * 1024
EDITOR_REQUEST_HEADER = "X-Hero-Siege-Item-Editor"
if DICE_EXPECTED_GAME_EXE_SHA256 != EXPECTED_GAME_EXE_SHA256:
    raise RuntimeError("roll and Dice databases target different Hero Siege builds")
def _roll_runtime_build_error() -> str | None:
    """Do not block editing because Steam or a mod changed the game EXE."""

    return None


def _torch_runtime_build_error() -> str | None:
    """Do not block Torch targets because Steam or a mod changed the game EXE."""

    return None


def _editor_runtime_build_status(build_status: object = None) -> dict:
    """Return an unlocked feature status without hashing the installed game.

    The editor's bundled profile/target artifacts still perform their own
    schema, checksum and replay validation. Only the external Hero_Siege.exe
    identity gate is disabled so Steam patches and ForgePact/Aurie do not turn
    otherwise healthy editor features off.
    """

    raw = dict(build_status) if isinstance(build_status, dict) else {}
    return {
        **raw,
        "matched": True,
        "code": "ready_unrestricted",
        "message": "Game executable matching is not enforced.",
        "expectedSha256": EXPECTED_GAME_EXE_SHA256,
        "matchingEnforced": False,
    }


ROLL_DB = load_roll_profile_database(
    BASE, runtime_build_check=_roll_runtime_build_error
)
DICE_SKILL_DB = load_dice_skill_database(
    BASE, runtime_build_check=_roll_runtime_build_error
)
TORCH_CLASS_DB = load_torch_class_database(
    runtime_build_check=_torch_runtime_build_error
)
TOOLTIP_MODEL_DB = (
    _load_tooltip_model_database(BASE)
    if _load_tooltip_model_database is not None
    else None
)

# GetItemSeed emits this inclusive save-field range in the clean S10 build.
# CPR's later masked internal-state domain is different; it must not be used
# as the source-seed search interval.  The old editor missed only the valid
# upper endpoint 1,000,000,000.
SEED_MIN = 1
SEED_MAX = 1_000_000_000
FORGE_LOCK = threading.Lock()
# Every POST mutates one or more save documents through a read/modify/write
# cycle.  ThreadingHTTPServer can execute two clicks concurrently, so serialize
# the complete operation instead of merely relying on atomic final replaces.
SAVE_WRITE_LOCK = threading.RLock()
_VAULT_STORE = None
_VAULT_STORE_PATH = None
_VAULT_STORE_LOCK = threading.RLock()
# Runtime peer checks are enabled only by ``main`` after this process has
# acquired its listening port.  Keeping the default disabled makes imported
# library/unit-test operations deterministic and side-effect free.
INSTANCE_GUARD_ACTIVE = False
INSTANCE_PORT = None
INSTANCE_RESERVED_PORTS = frozenset()

XOR_KEY = bytes([
    0xE3, 0x95, 0x3D, 0xB1, 0x01, 0x6B, 0xB6, 0x58,
    0x54, 0x38, 0x3F, 0x46, 0xA1, 0x74, 0x29, 0xCC,
    0x45, 0x45, 0x51, 0xF2, 0xA7, 0xF7, 0xAB, 0xB7,
    0x26, 0xF1, 0x37, 0xA8, 0x81, 0x91, 0xE6, 0x7E,
])

CLASS_NAMES = {0: "Helmet", 1: "Body Armor", 2: "Boots", 3: "Weapon", 4: "Gloves", 5: "Amulet",
               6: "Shield", 7: "Ring", 8: "Belt", 10: "Charm", 11: "Potion / Codex",
               12: "Key", 13: "Boss Part / Tarot", 14: "Material", 15: "Rune / Gem / Orb",
               16: "Relic", 18: "Flask", 19: "Essence Vault", -2: "Runeword"}
# Only these repository classes are equipment whose visible definition-stat
# rolls are covered by the verified profile database.  If one of these rows
# lacks its exact address profile, generation must stop instead of silently
# falling back to a random seed and creating a non-perfect item.
ROLL_PROFILE_GEAR_CLASSES = frozenset({0, 1, 2, 3, 4, 5, 6, 7, 8, 10})
SUB_NAMES = {0: "", 1: "Sword", 2: "Dagger", 3: "Mace", 4: "Axe", 5: "Claw",
             6: "Polearm", 7: "Chainsaw", 8: "Staff", 9: "Cane", 10: "Wand", 11: "Book",
             12: "Spellblade", 13: "Bow", 14: "Gun", 15: "Flask", 16: "Throwing", 17: "Universal"}
SLOT_NAMES = {0: "Helmet", 1: "Body Armor", 2: "Boots", 3: "Weapon I", 4: "Gloves", 5: "Amulet",
              6: "Offhand I", 7: "Ring I", 8: "Belt", 9: "Ring II",
              10: "Relic 1", 11: "Relic 2", 12: "Relic 3", 13: "Relic 4", 14: "Relic 5",
              16: "Weapon II", 17: "Offhand II"}
HERO_CLASSES = {1: "Viking", 2: "Pyromancer", 3: "Marksman", 4: "Pirate", 5: "Nomad",
                6: "Redneck", 7: "Necromancer", 8: "Samurai", 9: "Paladin", 10: "Amazon",
                11: "Demon Slayer", 12: "Demonspawn", 13: "Shaman", 14: "White Mage",
                15: "Marauder", 16: "Plague Doctor", 17: "Shield Lancer", 18: "Illusionist",
                19: "Jotunn", 20: "Exo", 21: "Butcher", 22: "Stormweaver", 23: "Bard", 24: "Prophet"}
GRID_DIMS = {"inventory_tab": (15, 6), "inventory_charms": (3, 11), "inventory_key_tab": (15, 6),
             "inventory_material_tab": (15, 6), "inventory_socket_tab": (15, 6),
             "inventory_relic_tab": (15, 6), "inventory_tarot_tab": (15, 6),
             "inventory_vault_tab": (15, 6), "inventory_vault_active": (15, 6),
             "stash_tab": (17, 18), "material_tab": (17, 18), "socket_tab": (17, 18),
             "potions": (5, 2), "personal_stash": (17, 18)}

# Season 10 additions whose save addresses were verified from the current build
# and/or observed S10 saves. Entries that only exist in localization but whose
# repository tuple is not proven are deliberately not generated by the editor.
S10_CATALOG_ADDITIONS = [
    # Current S10 native key repository order. IDs 36-38 were also confirmed
    # in-game: generating them yields Boreal, Parasitic and Treasure Key.
    (12, 36, "keys_boreal_key", "Boreal Key"),
    (12, 37, "keys_parasitic_key", "Parasitic Key"),
    (12, 38, "keys_treasure_key", "Treasure Key"),
    (12, 39, "keys_hive_key", "Hive Key"),
    (12, 40, "keys_aztec_key", "Aztec Key"),
    (12, 41, "keys_tablet_of_leviathan", "Tablet of Leviathan"),
    (12, 42, "keys_tablet_of_armada", "Tablet of Armada"),
    (12, 43, "keys_tablet_of_parasite", "Tablet of Parasite"),
    # New material fragments append to the old material repository.
    (14, 71, "material_blacksmiths_mallet_fragment", "Mallet Fragment"),
    (14, 72, "material_gypsys_prophecy_fragment", "Gypsy's Fragment"),
    (14, 73, "material_satanic_dice_fragment", "Dice Fragment"),
    # Observed S10 boss-gem save tuples. These fill previously empty addresses.
    (15, 136, "socketable_gem_cthulhu", "Cthulhu's Soulgem"),
    (15, 137, "socketable_gem_of_incarnation", "Gem of Incarnation"),
]

# Verified S10 access-item routes. These are native key-repository tuples,
# not generic "boss part" substitutes: the three tablets grant passage to the
# new Uber chambers. The five keys below open the new Act IX
# Lady Sonya challenges.
S10_ACCESS_GROUPS = {
    "ubers": {
        "name": "New Uber Access Kit",
        "description": "All three Season 10 tablets used to enter the new Uber chambers.",
        "items": [
            {"key": "keys_tablet_of_leviathan", "boss": "Phantom Leviathan",
             "destination": "Permafrozen Chamber"},
            {"key": "keys_tablet_of_armada", "boss": "Captain Grimtide",
             "destination": "Secret Chamber"},
            {"key": "keys_tablet_of_parasite", "boss": "Blood Maiden",
             "destination": "Heart Chamber"},
        ],
    },
    "act9": {
        "name": "Act IX Dungeon Key Kit",
        "description": "All five Season 10 Lady Sonya challenge keys.",
        "items": [
            {"key": "keys_boreal_key", "destination": "Boreal Cave"},
            {"key": "keys_parasitic_key", "destination": "Belly of the Beast"},
            {"key": "keys_treasure_key", "destination": "Treasure Cove"},
            {"key": "keys_hive_key", "destination": "Wasp's Nest"},
            {"key": "keys_aztec_key", "destination": "Aztec Pyramid"},
        ],
    },
}

# The current S10 item table adds exactly 24 unique-repository entries.  The
# address tuple is (class, weapon subtype, game/base ID); dimensions are kept
# deliberately conservative so the editor never packs items on top of one
# another.  All 24 are Heroic boss drops in the current S10 data set.
S10_UNIQUE_ADDITIONS = [
    (0, 0, 89, "helmet_parasite_queens_tiara", "Parasite Queen's Tiara", 2, 2),
    (0, 0, 90, "helmet_leviathans_crown", "Leviathan's Crown", 2, 2),
    (1, 0, 103, "armors_captains_attire", "Captain's Attire", 2, 3),
    (1, 0, 104, "armors_leviathans_ribcage", "Leviathan's Ribcage", 2, 3),
    (2, 0, 81, "boots_ghostplunderers_marchers", "Ghostplunderer's Marchers", 2, 2),
    (2, 0, 82, "boots_phantoms_step", "Phantom's Step", 2, 2),
    (3, 1, 36, "w_melee_grimtides_scimitar", "Grimtide's Scimitar", 2, 3),
    (3, 1, 37, "w_melee_phantom_scimitar", "Phantom Scimitar", 2, 4),
    (3, 9, 8, "w_spell_leviathans_spine", "Leviathan's Spine", 2, 4),
    (3, 10, 13, "w_spell_conjured_tentacle", "Conjured Tentacle", 1, 2),
    (3, 13, 20, "w_bow_phantom_strike", "Phantom Strike", 2, 3),
    (3, 14, 19, "w_gun_ethereal_musket", "Ethereal Musket", 3, 2),
    (4, 0, 65, "gloves_infected_grasp", "Infected Grasp", 2, 2),
    (5, 0, 75, "amulets_grimtides_necklace", "Grimtide's Necklace", 1, 2),
    (5, 0, 76, "amulets_blood_maggot_pendant", "Blood Maggot Pendant", 1, 2),
    (6, 0, 56, "shields_overgrowth", "Overgrowth", 2, 3),
    (7, 0, 60, "rings_skeleton_crews_band", "Skeleton Crew's Band", 1, 1),
    (7, 0, 61, "rings_parasite_loop", "Parasite Loop", 1, 1),
    (10, 0, 99, "charms_captains_anchor", "Captain's Anchor", 2, 2),
    (10, 0, 100, "charms_ghastly_skull", "Ghastly Skull", 2, 2),
    (10, 0, 101, "charms_parasitic_heart", "Parasitic Heart", 2, 2),
    (10, 0, 102, "charms_ghost_armada", "Ghost Armada", 2, 2),
    (10, 0, 103, "charms_jar_of_parasites", "Jar of Parasites", 2, 2),
    (18, 0, 22, "consumable_leviathans_blood", "Leviathan's Blood", 1, 2),
]

# Present in the S9 catalog but absent from the current S10 unique repository.
# Keep the rows for decoding old/non-season saves, but never offer them for
# generation because those addresses no longer have a current S10 identity.
S10_UNAVAILABLE_UNIQUE_KEYS = {
    "charms_almighty_nugget",
    "charms_dev_charm_small",
    "charms_divine_crack_pipe",
    "charms_forking_bolts",
    "charms_nomads_corpse",
    "charms_supreme_elemelon",
    "deprecated",
    "rings_hero_siege_enjoyer",
    "w_throwing_darkmoon_deck",
    "w_universal_cheated_item",
    "w_universal_fishing_rod",
}


def xor_bytes(b: bytes) -> bytes:
    return bytes(x ^ XOR_KEY[i % len(XOR_KEY)] for i, x in enumerate(b))


def decode_hss(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    cleaned = "".join(c for c in raw if not c.isspace() and c != "\x00")
    packed = base64.b64decode(cleaned, validate=True)
    decoded = xor_bytes(zlib.decompress(packed))
    if len(decoded) % 2 or any(decoded[i] for i in range(1, len(decoded), 2)):
        raise ValueError(f"Unsupported or corrupt HSS text payload: {path.name}")
    return decoded[::2].decode("latin-1")


def encode_hss(text: str) -> str:
    wide = bytearray()
    for ch in text.encode("latin-1"):
        wide += bytes((ch, 0))
    return base64.b64encode(zlib.compress(xor_bytes(bytes(wide)), 9)).decode("ascii")


CREATE_NO_WINDOW = 0x08000000  # subprocess'in konsol penceresi acmasini engeller


def game_running() -> bool:
    """Return True when Hero Siege is running *or detection is unavailable*.

    Save mutations must fail closed.  Treating a tasklist timeout/failure as
    "game is not running" would otherwise disable the editor's main safety
    guarantee exactly when Windows process state could not be established.
    """
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Hero_Siege.exe"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            return True
        return any(
            line.split(None, 1)[0].casefold() == "hero_siege.exe"
            for line in r.stdout.splitlines()
            if line.strip()
        )
    except Exception:
        return True


def backup(path: Path) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak = path.with_name(path.name + f".guibak_{stamp}")
    shutil.copy2(path, bak)
    old = sorted(path.parent.glob(path.name + ".guibak_*"))
    for p in old[:-20]:
        try:
            p.unlink()
        except OSError:
            pass
    return bak.name


def atomic_write_text(path: Path, text: str, encoding: str) -> None:
    """Write a save beside the original, flush it, then atomically replace it."""
    temp = path.with_name(path.name + ".itemeditor.tmp")
    try:
        with temp.open("w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def vault_store() -> InfiniteVault:
    """Return a lazy, path-sensitive vault handle.

    Laziness prevents imports and read-only editor screens from creating user
    data.  The path sensitivity is also essential for the temporary-save test
    suite and for portable copies of the editor.
    """
    global _VAULT_STORE, _VAULT_STORE_PATH
    path = Path(VAULT_DB_FILE).expanduser().resolve()
    with _VAULT_STORE_LOCK:
        if _VAULT_STORE is None or _VAULT_STORE_PATH != path:
            _VAULT_STORE = InfiniteVault(path)
            _VAULT_STORE_PATH = path
        return _VAULT_STORE


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _exclusive_save_file(path: Path, timeout: float = 15.0):
    """Serialize save work across cooperating editor processes.

    ``SAVE_WRITE_LOCK`` covers threads in this process.  This one-byte OS lock
    covers separate processes running this release.  A legacy/second-instance
    guard separately refuses uncooperative older releases.  The OS releases
    the lock automatically after a crash.
    """
    lock_path = path.with_name(path.name + ".itemeditor.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - Windows is the supported runtime.
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("another Item Editor is changing the shared stash")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _vault_request_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
        raise VaultValidationError("requestId must be a 16-128 character stable id")
    return value


VAULT_SHARED_GRID_TAB_RE = re.compile(
    r"(?:stash_tab_[1-9]\d*|material_tab(?:_[1-9]\d*)?|socket_tab(?:_[1-9]\d*)?)"
)


def _is_vault_shared_grid_tab(tab) -> bool:
    return isinstance(tab, str) and VAULT_SHARED_GRID_TAB_RE.fullmatch(tab) is not None


def _vault_shared_grid_label(tab: str) -> str:
    match = re.fullmatch(r"stash_tab_([1-9]\d*)", tab)
    if match:
        return f"Shared Stash Tab {int(match.group(1))}"
    match = re.fullmatch(r"(material|socket)_tab(?:_([1-9]\d*))?", tab)
    if match:
        base = "Material Tab" if match.group(1) == "material" else "Socket Tab"
        return f"{base} {int(match.group(2))}" if match.group(2) else base
    return tab


def _vault_stash_tab(ref, label: str) -> str:
    if not isinstance(ref, dict) or ref.get("type") != "stash":
        raise VaultValidationError(f"{label} must be a Shared Stash target")
    tab = ref.get("tab")
    if not _is_vault_shared_grid_tab(tab):
        raise VaultValidationError(
            f"{label} must be a normal, Material, or Socket Shared Stash grid"
        )
    return tab


def _vault_transfer_tabs() -> list:
    """Return only native grid containers that exist in this stash document."""
    try:
        stash = json.loads(decode_hss(SAVES / "stash.hss"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    tabs = [
        tab for tab, items in stash.items()
        if _is_vault_shared_grid_tab(tab) and isinstance(items, dict)
    ]

    def sort_key(tab: str):
        match = re.fullmatch(r"stash_tab_([1-9]\d*)", tab)
        if match:
            return 0, int(match.group(1)), tab
        if tab.startswith("material_tab"):
            return 1, 0, tab
        return 2, 0, tab

    return [
        {"tab": tab, "label": _vault_shared_grid_label(tab)}
        for tab in sorted(tabs, key=sort_key)
    ]


def _vault_item_json(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


BULK_STASH_ITEM_TABS = (
    "material_tab",
    "socket_tab",
    "unique_items",
    *(f"stash_tab_{index}" for index in range(1, 20)),
)
BULK_DEPOSIT_ALL_TABS = "all"
BULK_WITHDRAWAL_AUTO = "auto"
BULK_ROUTING_VERSION = 3


def _vault_source_display(source: str | None) -> str:
    if source == "unique_items":
        return "Unique Tab"
    if source and (_is_vault_shared_grid_tab(source)):
        return _vault_shared_grid_label(source)
    return source or "Shared Stash"


CAT = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
SETS_FILE = BASE / "hs_sets.json"
SETS = json.loads(SETS_FILE.read_text(encoding="utf-8")) if SETS_FILE.exists() else []
RW_FILE = BASE / "hs_runewords.json"
RUNEWORDS = json.loads(RW_FILE.read_text(encoding="utf-8")) if RW_FILE.exists() else []
ICONS = BASE / "item_icons"


def apply_s10_catalog_overlay() -> None:
    by_addr = {(0, int(r.get("cls", -999)), int(r.get("sub", 0)), int(r.get("b", -1))): r
               for r in CAT if r.get("kind") == "normal"}
    for cls, base_id, key, name in S10_CATALOG_ADDITIONS:
        addr = (0, cls, 0, base_id)
        row = by_addr.get(addr)
        if row is None:
            row = {"kind": "normal", "cls": cls, "sub": 0, "b": base_id,
                   "key": key, "name": name, "rar": "Normal", "w": 1, "h": 1,
                   "spr": None, "journal": False, "noUnique": False,
                   "stats": [], "id": len(CAT)}
            CAT.append(row)
            by_addr[addr] = row
        else:
            row.update({"key": key, "name": name, "rar": "Normal", "w": 1, "h": 1})
        row.update({"available": True, "s10Verified": True})
    unique_by_addr = {(1, int(r.get("cls", -999)), int(r.get("sub", 0)), int(r.get("b", -1))): r
                      for r in CAT if r.get("kind") == "unique"}
    for cls, sub, base_id, key, name, width, height in S10_UNIQUE_ADDITIONS:
        addr = (1, cls, sub, base_id)
        row = unique_by_addr.get(addr)
        values = {
            "kind": "unique", "cls": cls, "sub": sub, "b": base_id,
            "key": key, "name": name, "rar": "Heroic", "w": width,
            "h": height,
            "spr": f"s10_{key}" if (ICONS / f"s10_{key}.png").exists() else None,
            "journal": True,
            "noUnique": False, "stats": [], "available": True,
            "s10Verified": True,
        }
        if row is None:
            row = {"id": len(CAT), **values}
            CAT.append(row)
            unique_by_addr[addr] = row
        else:
            row.update(values)
    for index, row in enumerate(CAT):
        row["id"] = index
        placeholder = (
            not str(row.get("key") or "").strip()
            or str(row.get("name") or "").startswith("?")
            or str(row.get("key") or "").lower() == "deprecated"
            # Essence Vaults carry rolled v1/v2/d payloads.  They can be read
            # and moved, but a generic item seed is not a valid vault.
            or int(row.get("cls", -999)) == 19
            or (row.get("kind") == "unique"
                and row.get("key") in S10_UNAVAILABLE_UNIQUE_KEYS)
        )
        row.setdefault("available", not placeholder)
        row.setdefault("s10Verified", False)


apply_s10_catalog_overlay()


def apply_socket_choice_labels() -> None:
    """Give duplicate-name socketables an unambiguous native base ID label."""

    rows = [
        row for row in CAT
        if row.get("kind") == "normal"
        and row.get("available", True)
        and int(row.get("cls", -1)) == 15
    ]
    name_counts: dict[str, int] = {}
    for row in rows:
        folded = str(row.get("name") or "").strip().casefold()
        name_counts[folded] = name_counts.get(folded, 0) + 1
    for row in rows:
        name = str(row.get("name") or "Unknown socketable").strip()
        folded = name.casefold()
        row["socketChoiceLabel"] = (
            f"{name} [ID {int(row['b'])}]" if name_counts.get(folded, 0) > 1 else name
        )


apply_socket_choice_labels()


SMALL_CHARM_KEY = "charms_normal_small_charm"


def is_ordinary_small_charm_row(row: object) -> bool:
    """Match only the native normal Small Charm family.

    These items use a normal random ``a`` seed and do not consume any of the
    build-specific Perfect/Best or Dice seeds.  Keep this identity predicate
    separate from display metadata so a game update cannot accidentally turn
    every class-10 charm into an unverified exception.
    """

    if not isinstance(row, dict):
        return False
    try:
        return bool(
            row.get("kind") == "normal"
            and row.get("key") == SMALL_CHARM_KEY
            and not isinstance(row.get("cls"), bool)
            and not isinstance(row.get("sub", 0), bool)
            and not isinstance(row.get("b"), bool)
            and int(row.get("cls", -1)) == 10
            and int(row.get("sub", 0)) == 0
            and 0 <= int(row.get("b", -1)) <= 19
        )
    except (TypeError, ValueError):
        pass
    return False


def catalog_roll_profile(row: dict) -> dict | None:
    """Return the verified direct-address profile for a catalog row."""
    kind = row.get("kind")
    if kind not in {"normal", "unique"}:
        return None
    try:
        return ROLL_DB.lookup(
            kind,
            int(row["cls"]),
            int(row.get("sub", 0)),
            int(row["b"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def catalog_dice_profile_id(row: dict) -> str | None:
    """Return a selectable Dice profile only for its exact native address."""

    try:
        return dice_profile_id_for_address(
            str(row.get("kind")),
            int(row["cls"]),
            int(row.get("sub", 0)),
            int(row["b"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def catalog_torch_profile_id(row: dict) -> str | None:
    """Return the class selector only for Torch of Shadows' exact address."""

    try:
        return torch_profile_id_for_address(
            str(row.get("kind")),
            int(row["cls"]),
            int(row.get("sub", 0)),
            int(row["b"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def catalog_skill_profile_id(row: dict) -> str | None:
    """Return an exact-address selectable identity profile for one row."""

    return catalog_dice_profile_id(row) or catalog_torch_profile_id(row)


def skill_target_database(profile_id: str):
    """Route a selector profile without weakening either database's guard."""

    if profile_id in {LOADED_PROFILE_ID, OVERLOADED_PROFILE_ID}:
        return DICE_SKILL_DB
    if profile_id == TORCH_PROFILE_ID:
        return TORCH_CLASS_DB
    return None


def dice_target_for_row(row: dict, skill_id: object) -> dict:
    profile_id = catalog_dice_profile_id(row)
    if profile_id is None:
        raise DiceSkillValidationError("this item does not support skill targeting")
    return DICE_SKILL_DB.target(profile_id, skill_id)


def skill_target_for_row(row: dict, target_id: object) -> dict:
    profile_id = catalog_skill_profile_id(row)
    database = skill_target_database(profile_id) if profile_id is not None else None
    if database is None:
        raise TorchClassValidationError(
            "this item does not support skill or class targeting"
        )
    return database.target(profile_id, target_id)


def roll_database_message() -> str:
    """Return the live fail-closed reason, including game-build attestation."""

    summary = getattr(ROLL_DB, "summary", None)
    if callable(summary):
        try:
            message = summary().get("message")
            if message:
                return str(message)
        except Exception:
            pass
    return str(getattr(getattr(ROLL_DB, "status", None), "message", "roll profiles unavailable"))


def generation_roll_profile(row: dict) -> dict | None:
    """Return a profile, rejecting unverified equipment generation.

    Stackables and other non-equipment repositories retain their native random
    seed because the perfect-roll project does not apply to them.  Every normal
    or unique equipment address, including fixed-stat equipment, must have a
    validated database entry before the editor is allowed to create it.
    """
    if is_ordinary_small_charm_row(row):
        # Ordinary Small Charms are rebuilt by the game from a fresh native
        # random seed. They do not use a build-bound Perfect/Best/Dice seed,
        # so an unrelated executable update must not block their creation.
        return None
    is_profiled_equipment = (
        row.get("kind") in {"normal", "unique"}
        and int(row.get("cls", -1)) in ROLL_PROFILE_GEAR_CLASSES
    )
    if is_profiled_equipment and not ROLL_DB.available:
        raise ValueError(
            f"{row.get('name', 'Equipment')}: {roll_database_message()}"
        )
    profile = row.get("rollProfile") or catalog_roll_profile(row)
    if is_profiled_equipment and profile is None:
        raise ValueError(
            f"{row.get('name', 'Equipment')}: no verified roll profile for this exact equipment address"
        )
    return profile


def generation_roll_profile_for_request(
    row: dict,
    target_id: object = None,
) -> dict | None:
    """Validate generation, allowing a proven Torch class seed to own ``a``.

    The roll artifact keeps its 7.0.5 source provenance while the complete
    general numeric path is independently proven compatible with the later
    Season 10 executable variant.
    Torch still uses its dedicated selector proof and may own ``a`` only when
    an explicit class target is present and valid.
    """

    if catalog_torch_profile_id(row) is not None and target_id is not None:
        skill_target_for_row(row, target_id)
        return None
    return generation_roll_profile(row)


for _r in CAT:
    _profile = catalog_roll_profile(_r)
    if _profile:
        _r["rollProfile"] = _profile
    _skill_profile_id = catalog_skill_profile_id(_r)
    if _skill_profile_id:
        _target_database = skill_target_database(_skill_profile_id)
        if _target_database is not None:
            _r["skillSelector"] = _target_database.selector(_skill_profile_id)
BY_ADDR = {}
for _r in CAT:
    kindbit = 1 if _r["kind"] == "unique" else 0
    BY_ADDR[(kindbit, _r["cls"], _r["sub"], _r["b"])] = _r

# Identify runewords from the rune sequence in the sockets (every recipe is
# unique, 0 collisions).
RW_BY_RUNES = {}
for _rw in RUNEWORDS:
    _seq = tuple(int(_rn["b"]) for _rn in _rw.get("runes", []))
    if _seq:
        RW_BY_RUNES.setdefault(_seq, _rw)


def runeword_allowed_subtypes(recipe: dict) -> set[int]:
    """Return the exact weapon subtype set encoded by a runeword target."""
    if int(recipe.get("type", -1)) != 3:
        return {0}
    match = re.fullmatch(
        r"Weapon\s*\(([^)]+)\)",
        str(recipe.get("target") or "").strip(),
    )
    if match is None:
        raise ValueError(f"unparseable weapon runeword target: {recipe.get('target')!r}")
    by_name = {name.casefold(): subtype for subtype, name in SUB_NAMES.items() if name}
    names = [part.strip() for part in match.group(1).split("/")]
    unknown = [name for name in names if name.casefold() not in by_name]
    if unknown:
        raise ValueError(f"unknown weapon subtype(s): {', '.join(unknown)}")
    return {by_name[name.casefold()] for name in names}


def runeword_base_candidates(recipe: dict) -> list[dict]:
    """Expand a recipe to every compatible, runtime-available normal base.

    The recipe target controls class and (for weapons) subtype. Zone Codex
    recipes deliberately keep their one fixed normal base address.
    """
    item_type = int(recipe.get("type", -1))
    allowed_subtypes = runeword_allowed_subtypes(recipe)
    reference = recipe.get("base") or {}
    reference_address = (
        int(reference.get("cls", item_type)),
        int(reference.get("sub", 0)),
        int(reference.get("b", -1)),
    )
    if item_type == 11:
        rows = [
            row for row in CAT
            if row.get("kind") == "normal"
            and row.get("available", True)
            and (int(row["cls"]), int(row.get("sub", 0)), int(row["b"]))
            == reference_address
        ]
    else:
        rows = [
            row for row in CAT
            if row.get("kind") == "normal"
            and row.get("available", True)
            and int(row["cls"]) == item_type
            and int(row.get("sub", 0)) in allowed_subtypes
        ]
    rows.sort(key=lambda row: (int(row["cls"]), int(row.get("sub", 0)), int(row["b"])))
    return rows


def runeword_base_is_compatible(recipe: dict, cls: int, sub: int, base: int) -> bool:
    """Return whether an exact save address is a valid base for ``recipe``."""
    try:
        address = (int(cls), int(sub), int(base))
        return any(
            (int(row["cls"]), int(row.get("sub", 0)), int(row["b"])) == address
            for row in runeword_base_candidates(recipe)
        )
    except (KeyError, TypeError, ValueError):
        return False


def runeword_profile(recipe: dict, base: dict) -> dict | None:
    try:
        return ROLL_DB.lookup_runeword(
            int(recipe["rw"]),
            int(base["cls"]),
            int(base.get("sub", 0)),
            int(base["b"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def runeword_profile_unavailable_reason() -> str:
    if not ROLL_DB.available:
        return roll_database_message()
    return "no verified roll profile for this exact recipe/base"


def runeword_generation_blocker(recipe: dict) -> str | None:
    """Explain runeword families that cannot yet be serialized safely."""
    if int(recipe.get("type", -1)) == 11:
        return (
            "Zone Codex generation is disabled: its d/q/r/u/v payload and "
            "socket metadata are not verified for synthesis"
        )
    return None


def runeword_api_rows() -> list[dict]:
    """Return recipes with the complete validated base matrix for the UI."""
    output = []
    for recipe in RUNEWORDS:
        row = dict(recipe)
        bases = []
        blocker = runeword_generation_blocker(recipe)
        try:
            candidates = runeword_base_candidates(recipe)
        except (KeyError, TypeError, ValueError) as exc:
            row.update({
                "bases": [],
                "available": False,
                "disabled": True,
                "unavailableReason": f"invalid runeword target: {exc}",
            })
            output.append(row)
            continue
        for base in candidates:
            profile = runeword_profile(recipe, base)
            available = profile is not None and blocker is None
            bases.append({
                "cid": int(base["id"]),
                "cls": int(base["cls"]),
                "sub": int(base.get("sub", 0)),
                "b": int(base["b"]),
                "key": base.get("key"),
                "name": base.get("name"),
                "w": int(base.get("w", 1)),
                "h": int(base.get("h", 1)),
                "rollMode": profile.get("mode") if profile else None,
                "rollDetail": profile.get("detail") if profile else None,
                "available": available,
                "disabled": not available,
                "unavailableReason": (
                    None if available else (
                        blocker or runeword_profile_unavailable_reason()
                    )
                ),
            })
        row["bases"] = bases
        row["available"] = any(base["available"] for base in bases)
        row["disabled"] = not row["available"]
        if not row["available"]:
            row["unavailableReason"] = (
                blocker or runeword_profile_unavailable_reason()
                if bases else "runeword has no runtime-available compatible base"
            )
        output.append(row)
    return output


def socket_rune_seq(data: dict) -> tuple:
    """Extract the rune b-values from the item's s1..s6 sockets, in order."""
    seq = []
    for n in range(1, 7):
        v = data.get(f"s{n}")
        if not v:
            continue
        try:
            rj = json.loads(base64.b64decode(v))
            seq.append(int(rj["b"]))
        except Exception:
            pass
    return tuple(seq)


def decode_existing_socket_payload(value: object) -> dict:
    """Decode one saved socket payload or raise without normalizing it."""

    if not isinstance(value, str) or not value:
        raise ValueError("socket payload is not a non-empty string")
    decoded = json.loads(base64.b64decode(value, validate=True))
    if not isinstance(decoded, dict):
        raise ValueError("socket payload is not an object")
    base_id = decoded.get("b")
    if (
        isinstance(base_id, bool)
        or not isinstance(base_id, (int, float))
        or not math.isfinite(float(base_id))
    ):
        raise ValueError("socket payload has no valid base id")
    return decoded


def catalog_socket_limit(row: object, default: int = 0) -> int:
    """Return the catalog's advertised maximum socket count, capped by save format.

    The editor can serialize at most ``s1`` through ``s6``.  Equipment entries
    which publish a ``Sockets`` stat carry a narrower native maximum (for
    example Poison Ivy is ``1-4``); honoring that maximum prevents the socket
    editor from creating a count the item definition cannot represent.
    """

    if isinstance(row, dict):
        for stat in row.get("stats") or ():
            if (
                not isinstance(stat, (list, tuple))
                or len(stat) != 2
                or str(stat[0]).strip().casefold() != "sockets"
            ):
                continue
            values = [int(value) for value in re.findall(r"\d+", str(stat[1]))]
            if values:
                return max(0, min(6, max(values)))
    return max(0, min(6, int(default)))


def item_socket_limit(item: object) -> int:
    """Resolve one existing item to the socket editor's safe upper bound."""

    if not isinstance(item, dict):
        return 0
    if item.get("isRW"):
        return 6
    profile = item.get("rollProfile")
    verified = roll_profile_max_sockets(profile)
    if verified is not None:
        return verified
    catalog_id = item.get("cid")
    if (
        isinstance(catalog_id, int)
        and not isinstance(catalog_id, bool)
        and 0 <= catalog_id < len(CAT)
    ):
        return catalog_socket_limit(CAT[catalog_id])
    return 0


def resolve(key: str, data: dict) -> dict:
    """Save kaydini katalog girdisine cozer."""
    try:
        sfx = int(key.rsplit("-", 1)[1])
    except Exception:
        sfx = -1
    c = int(data.get("c", 0))
    j = int(data.get("j", 0))
    b = data.get("b")
    out = {"key": key, "raw": data, "stack": data.get("o"), "cls": sfx, "sub": j}
    if b is None:
        out.update(name="Special/Runeword item", rar="Runeword", w=2, h=4, cid=None)
    else:
        r = BY_ADDR.get((c, sfx, j if sfx == 3 else 0, int(b)))
        if r:
            out.update(name=r["name"], rar=r["rar"], w=r["w"], h=r["h"], cid=r["id"],
                       set=r.get("set"), clsName=CLASS_NAMES.get(r["cls"], "?"), spr=r.get("spr"))
            if r.get("rollProfile"):
                out["rollProfile"] = effective_roll_profile(r["rollProfile"])
            skill_profile_id = catalog_skill_profile_id(r)
            target_database = (
                skill_target_database(skill_profile_id)
                if skill_profile_id is not None else None
            )
            if target_database is not None:
                out["skillSelector"] = target_database.selector(
                    skill_profile_id, data.get("a")
                )
        else:
            out.update(name=f"? (c{c} s{sfx} j{j} b{int(b)})", rar="?", w=1, h=1, cid=None)
    # If the socketed runes match a recipe this is a runeword (the game's own logic).
    # The base item's cls/spr/w/h/cid are kept so equipping and dragging stay correct;
    # only the displayed name and rarity are marked as runeword, and the tooltip
    # reads from rwcid.
    rw = RW_BY_RUNES.get(socket_rune_seq(data))
    compatible_runeword = (
        rw is not None
        and b is not None
        and runeword_base_is_compatible(
            rw,
            sfx,
            j if sfx == 3 else 0,
            int(b),
        )
    )
    if compatible_runeword:
        out["name"] = rw["name"]
        out["rar"] = "Runeword"
        out["isRW"] = True
        if isinstance(rw.get("cid"), int):
            out["rwcid"] = rw["cid"]
        if b is not None:
            try:
                profile = ROLL_DB.lookup_runeword(
                    int(rw["rw"]),
                    sfx,
                    j if sfx == 3 else 0,
                    int(b),
                )
            except (KeyError, TypeError, ValueError):
                profile = None
            if profile:
                out["rollProfile"] = effective_roll_profile(profile)
            else:
                out.pop("rollProfile", None)
    out["socketLimit"] = item_socket_limit(out)
    return out


def _catalog_row_for_item(item: dict) -> dict:
    catalog_id = item.get("rwcid")
    if not isinstance(catalog_id, int):
        catalog_id = item.get("cid")
    if isinstance(catalog_id, int) and not isinstance(catalog_id, bool):
        if 0 <= catalog_id < len(CAT):
            return CAT[catalog_id]
    # Preserve the resolved native identity even when this build does not know
    # the address.  Exact calculation remains fail-closed in that case.
    return {
        "id": catalog_id,
        "name": item.get("name", "Unknown item"),
        "rar": item.get("rar", "?"),
        "tier": None,
        "lvl": None,
        "spr": item.get("spr"),
        "stats": [],
    }


def _catalog_only_tooltip_model(
    item: dict,
    catalog_row: dict,
    *,
    custom_name: str | None = None,
    warning: str = "Exact-tooltip model is unavailable.",
) -> dict:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    ordinary_small_charm = is_ordinary_small_charm_row(catalog_row)
    try:
        canonical = json.dumps(
            raw, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        canonical = b"{}"
    canonical_name = str(item.get("name") or catalog_row.get("name") or "Unknown item")
    alias = custom_name.strip() if isinstance(custom_name, str) and custom_name.strip() else None
    warnings = [warning]
    unsupported_paths = ["tooltip_model_unavailable"]
    if ordinary_small_charm:
        warnings.insert(
            0,
            "Rolled rarity and affixes are generated in-game from seed a; "
            "this build does not decode that native path.",
        )
        unsupported_paths.append("generated_affixes_unmodelled:small_charm")
    stats = []
    for index, row in enumerate(catalog_row.get("stats") or []):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        stats.append({
            "id": f"catalog:{index}",
            "statKey": None,
            "label": str(row[0]),
            "value": None,
            "formattedValue": str(row[1]),
            "minimum": None,
            "maximum": None,
            "percent": str(row[1]).strip().endswith("%"),
            "rolled": True,
            "saveField": None,
            "role": "catalog",
            "confidence": "catalog_only",
            "catalogLineIndex": index,
            "sourceRange": str(row[1]),
        })
    return {
        "schemaVersion": 1,
        "profileId": None,
        "fingerprint": hashlib.sha256(canonical).hexdigest().upper()[:16],
        "item": {
            "name": alias or canonical_name,
            "canonicalName": canonical_name,
            "customName": alias,
            "rarity": "Unresolved" if ordinary_small_charm else item.get("rar"),
            "baseRarity": item.get("rar") if ordinary_small_charm else None,
            "rolledRarityKnown": False if ordinary_small_charm else True,
            "tier": catalog_row.get("tier"),
            "requiredLevel": catalog_row.get("lvl"),
            "iconId": item.get("spr"),
            "catalogId": catalog_row.get("id"),
        },
        "seeds": {
            field: raw[field] for field in ("a", "i", "s")
            if isinstance(raw.get(field), (int, float)) and not isinstance(raw.get(field), bool)
        },
        "stats": stats,
        "sockets": [],
        "identities": [],
        "rollQuality": {"maxed": 0, "total": 0, "endpointDeficit": 0, "percent": None},
        "calculation": {
            "coverage": "catalog_only",
            "numbersExact": False,
            "textExact": False,
            "buildMatched": False,
            "unsupportedPaths": unsupported_paths,
            "warnings": warnings,
        },
        "buildGuard": {"matched": False, "message": warning},
    }


def _tooltip_runtime_build_status(
    catalog_row: dict,
    build_status: object,
) -> object:
    """Keep exact tooltip and target features available on every EXE build."""

    del catalog_row
    return _editor_runtime_build_status(build_status)


def _game_tooltip_model(
    item: dict,
    *,
    custom_name: str | None = None,
    build_status: dict | None = None,
) -> dict:
    row = _catalog_row_for_item(item)
    if _build_exact_tooltip_model is None or TOOLTIP_MODEL_DB is None:
        return _catalog_only_tooltip_model(item, row, custom_name=custom_name)
    if build_status is None:
        try:
            build_status = _editor_runtime_build_status()
        except Exception as exc:
            build_status = {
                "matched": False,
                "code": "build_check_failed",
                "message": f"Game build could not be verified: {exc}",
            }
    build_status = _tooltip_runtime_build_status(row, build_status)
    try:
        model = _build_exact_tooltip_model(
            row,
            item.get("raw") if isinstance(item.get("raw"), dict) else {},
            item.get("rollProfile") if isinstance(item.get("rollProfile"), dict) else None,
            db=TOOLTIP_MODEL_DB,
            build_status=build_status,
            custom_name=custom_name,
        )
        identities = model.get("identities") if isinstance(model, dict) else None
        selector = item.get("skillSelector") if isinstance(item.get("skillSelector"), dict) else {}
        current_target = selector.get("current") if isinstance(selector.get("current"), dict) else {}
        if isinstance(identities, list):
            for identity in identities:
                if not isinstance(identity, dict):
                    continue
                selected = identity.get("selectedIdentity")
                source = str(identity.get("source") or "")
                if (
                    "subskill" in source
                    and selected == current_target.get("id")
                    and current_target.get("name")
                ):
                    class_name = str(current_target.get("className") or "").strip()
                    identity["selectedName"] = (
                        f"{class_name}: {current_target['name']}"
                        if class_name else str(current_target["name"])
                    )
                elif isinstance(selected, int) and not isinstance(selected, bool):
                    stat_label = TOOLTIP_MODEL_DB.stat_label(selected)
                    if stat_label.get("label"):
                        identity["selectedName"] = str(stat_label["label"])
        return model
    except Exception as exc:
        return _catalog_only_tooltip_model(
            item,
            row,
            custom_name=custom_name,
            warning=f"Exact tooltip could not be calculated: {exc}",
        )


def _attach_game_tooltip(item: dict, build_status: dict | None = None) -> dict:
    model = _game_tooltip_model(item, build_status=build_status)
    item["gameTooltip"] = model
    # Keep the socket editor aligned with the exact tooltip's final stat-20
    # value, including a saved zz.sockets override when present.
    stats = model.get("stats") if isinstance(model, dict) else None
    if isinstance(stats, list):
        socket_line = next(
            (
                line for line in stats
                if isinstance(line, dict) and line.get("statKey") == 20
            ),
            None,
        )
        value = socket_line.get("value") if isinstance(socket_line, dict) else None
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and float(value).is_integer()
            and 0 <= int(value) <= 6
        ):
            item["socketCount"] = int(value)
    return item


def _vault_item_payload(record, build_status: dict | None = None) -> dict:
    entry = record.decoded_item()
    key = record.source_item_key or "0-0-0--1"
    item = resolve(key, entry.get("data", {}))
    game_tooltip = _game_tooltip_model(
        item, custom_name=record.custom_name, build_status=build_status
    )
    return {
        "id": record.id,
        "collectionId": record.collection_id,
        "collectionName": record.collection_name,
        "customName": record.custom_name,
        "name": item.get("name", "Unknown item"),
        "rar": item.get("rar", "?"),
        "cls": item.get("cls"),
        "clsName": item.get("clsName") or CLASS_NAMES.get(item.get("cls"), "Unknown"),
        "cid": item.get("cid"),
        "rwcid": item.get("rwcid"),
        "spr": item.get("spr"),
        "w": item.get("w", 1),
        "h": item.get("h", 1),
        "stack": item.get("stack"),
        "gameTooltip": game_tooltip,
        "fingerprint": game_tooltip.get("fingerprint"),
        "sourceLabel": _vault_source_display(record.source),
        "sourceItemKey": record.source_item_key,
        "createdAt": record.created_at,
    }


def list_characters() -> list:
    chars = []
    for p in sorted(SAVES.glob("herosiege*.hss")):
        m = re.fullmatch(r"herosiege(\d+)\.hss", p.name)
        if not m or p.stat().st_size < 1000:
            continue
        slot = int(m.group(1))
        try:
            txt = decode_hss(p)
            name = re.search(r'\nname="([^"]*)"', txt)
            cls = re.search(r'\nclass="?([\d.]+)', txt)
            lvl = re.search(r'\nlevel="?([\d.]+)', txt)
            hc = int(float(cls.group(1))) if cls else 0
            chars.append({"slot": slot, "name": name.group(1) if name else f"Slot {slot}",
                          "cls": HERO_CLASSES.get(hc, f"Sinif {hc}"),
                          "level": int(float(lvl.group(1))) if lvl else 0})
        except Exception:
            continue
    chars.sort(key=lambda c: c["slot"])
    return chars


def read_char(slot: int) -> dict:
    txt = decode_hss(SAVES / f"herosiege{slot}.hss")
    m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
    inv = json.loads(base64.b64decode(m.group(1))) if m else {}
    try:
        tooltip_build_status = _editor_runtime_build_status()
    except Exception:
        tooltip_build_status = {
            "matched": False,
            "code": "build_check_failed",
            "message": "Game build could not be verified.",
        }
    out = {"equipped": [], "potions": [], "personal_stash": []}
    for k, v in inv.get("equipped_items", {}).items():
        it = resolve(k, v["data"])
        it["g"] = int(v["data"].get("g", -1))
        it["slotName"] = SLOT_NAMES.get(it["g"], f"Slot {it['g']}")
        if v["data"].get("o") is not None:
            it["relicLevel"] = int(float(v["data"]["o"]))
        _attach_game_tooltip(it, tooltip_build_status)
        out["equipped"].append(it)
    for sec in ("potions", "personal_stash"):
        for k, v in inv.get(sec, {}).items():
            it = resolve(k, v["data"])
            it["pos"] = v.get("pos", [0, 0])
            _attach_game_tooltip(it, tooltip_build_status)
            out[sec].append(it)
    # bag file
    bags = {}
    bp = SAVES / f"inventory_order_{slot}.hss"
    if bp.exists() and bp.stat().st_size > 50:
        try:
            d = json.loads(decode_hss(bp))
            for tab, items in d.items():
                if not isinstance(items, dict):
                    continue
                lst = []
                for k, v in items.items():
                    it = resolve(k, v.get("data", {}))
                    it["pos"] = v.get("pos", [0, 0])
                    _attach_game_tooltip(it, tooltip_build_status)
                    lst.append(it)
                bags[tab] = lst
        except Exception:
            pass
    out["bags"] = bags
    return out


def read_stash() -> dict:
    d = json.loads(decode_hss(SAVES / "stash.hss"))
    try:
        tooltip_build_status = _editor_runtime_build_status()
    except Exception:
        tooltip_build_status = {
            "matched": False,
            "code": "build_check_failed",
            "message": "Game build could not be verified.",
        }
    out = {}
    for tab, items in d.items():
        if not isinstance(items, dict) or tab == "stash_tab_data":
            continue
        lst = []
        for k, v in items.items():
            if not isinstance(v, dict) or "data" not in v:
                continue
            it = resolve(k, v["data"])
            if "pos" in v:
                it["pos"] = v["pos"]
            _attach_game_tooltip(it, tooltip_build_status)
            lst.append(it)
        out[tab] = lst
    return out


def fresh_key(cls: int, existing) -> str:
    base = int(time.time() * 1000)
    while f"0-0-{base}-{cls}" in existing:
        base += 1
    return f"0-0-{base}-{cls}"


def grid_dims(tab: str):
    base = re.sub(r"_\d+$", "", tab)
    if base in GRID_DIMS:
        return GRID_DIMS[base]
    # New S10 character inventory tabs use the same 15x6 bag canvas. Unknown
    # stash tabs remain 17x18. This also keeps future inventory tabs visible.
    return (15, 6) if base.startswith("inventory_") else (17, 18)


def find_free_pos(items: dict, tab: str, w: int, h: int):
    cols, rows = grid_dims(tab)
    occ = [[False] * cols for _ in range(rows)]
    for k, v in items.items():
        if "pos" not in v:
            continue
        x, y = int(v["pos"][0]), int(v["pos"][1])
        it = resolve(k, v.get("data", {}))
        for dy in range(it["h"]):
            for dx in range(it["w"]):
                if 0 <= y + dy < rows and 0 <= x + dx < cols:
                    occ[y + dy][x + dx] = True
    for y in range(rows - h + 1):
        for x in range(cols - w + 1):
            if all(not occ[y + dy][x + dx] for dy in range(h) for dx in range(w)):
                return [float(x), float(y)]
    return None


def random_item_seed() -> float:
    return float(random.randint(SEED_MIN, SEED_MAX))


def roll_profile_field_seeds(profile: dict | None) -> dict[str, float]:
    """Return only the independently verified save-field seeds in a profile."""
    if not profile or profile.get("mode") not in {"exact", "best"}:
        return {}
    raw = profile.get("fieldSeeds")
    if not isinstance(raw, dict):
        return {}
    output = {}
    for field in ("a", "i", "s"):
        seed = raw.get(field)
        if isinstance(seed, bool) or not isinstance(seed, (int, float)):
            continue
        if float(seed).is_integer() and SEED_MIN <= int(seed) <= SEED_MAX:
            output[field] = float(seed)
    return output


_SOCKET_SEED_DOCUMENT: dict | None = None
_SOCKET_SEEDS: dict | None = None


def _load_socket_seed_document() -> dict:
    """Load and structurally validate the build-measured socket seed table.

    This asset changes item generation, so a missing or malformed bundled file
    must stop the editor instead of silently falling back to lower-socket seeds.
    The offline generation scripts perform the full CPR replay; this runtime
    check protects the packaged data boundary and every value consumed here.
    """

    global _SOCKET_SEED_DOCUMENT, _SOCKET_SEEDS
    if _SOCKET_SEED_DOCUMENT is not None:
        return _SOCKET_SEED_DOCUMENT

    path = BASE / "hs_socket_seeds.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"socket seed database unavailable: {exc}") from exc
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise RuntimeError("socket seed database has an unsupported schema")
    seeds = document.get("seeds")
    if not isinstance(seeds, dict) or not seeds:
        raise RuntimeError("socket seed database contains no verified entries")

    for address_key, entry in seeds.items():
        label = f"socket seed {address_key!r}"
        if not isinstance(address_key, str) or not re.fullmatch(
            r"(?:normal|unique):\d+:\d+:\d+", address_key
        ):
            raise RuntimeError(f"{label} has an invalid address")
        if not isinstance(entry, dict):
            raise RuntimeError(f"{label} must be an object")
        seed = entry.get("seed")
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not SEED_MIN <= seed <= SEED_MAX
        ):
            raise RuntimeError(f"{label} has an invalid a seed")
        capacity = entry.get("maxSockets")
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 1 <= capacity <= 6
            or entry.get("sockets") != capacity
        ):
            raise RuntimeError(f"{label} has an invalid socket maximum")
        bounds = entry.get("statBounds")
        if (
            not isinstance(bounds, list)
            or any(
                isinstance(bound, bool)
                or not isinstance(bound, int)
                or bound < 0
                for bound in bounds
            )
        ):
            raise RuntimeError(f"{label} has invalid stat bounds")
        maxed = entry.get("maxed")
        total = entry.get("total")
        deficit = entry.get("endpointDeficit")
        if (
            isinstance(maxed, bool)
            or not isinstance(maxed, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or isinstance(deficit, bool)
            or not isinstance(deficit, int)
            or total != len(bounds)
            or not 0 <= maxed <= total
            or deficit < 0
        ):
            raise RuntimeError(f"{label} has invalid roll-quality metadata")

    _SOCKET_SEED_DOCUMENT = document
    _SOCKET_SEEDS = seeds
    return document


def socket_roll_for_profile(profile: dict | None) -> dict | None:
    """Return one validated, build-measured max-socket roll overlay."""

    if not isinstance(profile, dict):
        return None
    address_key = profile.get("addressKey")
    if (
        not isinstance(address_key, str)
        or re.fullmatch(r"(?:normal|unique):\d+:\d+:\d+", address_key) is None
    ):
        return None
    entry = _load_socket_seed_document()["seeds"].get(address_key)
    return entry if isinstance(entry, dict) else None


def socket_seed_for_profile(profile: dict | None) -> int | None:
    """Return the ``a`` seed measured to roll this item's maximum sockets."""

    entry = socket_roll_for_profile(profile)
    return int(entry["seed"]) if entry is not None else None


def effective_roll_field_seeds(profile: dict | None) -> dict[str, float]:
    """Overlay a measured max-socket ``a`` seed on the canonical roll fields."""

    seeds = roll_profile_field_seeds(profile)
    socket_seed = socket_seed_for_profile(profile)
    if socket_seed is not None:
        seeds["a"] = float(socket_seed)
    return seeds


def effective_roll_mode(profile: dict | None) -> str | None:
    """Classify the combined stat + maximum-socket roll without overclaiming."""

    if not isinstance(profile, dict):
        return None
    entry = socket_roll_for_profile(profile)
    if entry is None:
        return profile.get("mode")
    socket_chain_exact = (
        entry["maxed"] == entry["total"]
        and entry["endpointDeficit"] == 0
    )
    return (
        "exact"
        if profile.get("mode") == "exact" and socket_chain_exact
        else "best"
    )


def effective_roll_detail(profile: dict | None) -> str:
    """Describe the combined roll using the current-build socket measurement."""

    if not isinstance(profile, dict):
        return ""
    entry = socket_roll_for_profile(profile)
    if entry is None:
        return str(profile.get("detail") or "")
    detail = (
        f"{entry['maxed']}/{entry['total']} current-build a-chain stats MAX; "
        f"{entry['maxSockets']} sockets MAX"
    )
    if entry["endpointDeficit"]:
        detail += f"; verified endpoint deficit {entry['endpointDeficit']}"
    return detail


def effective_roll_label(profile: dict | None) -> str:
    """Return an honest user-facing label for the combined roll evidence."""

    mode = effective_roll_mode(profile)
    if mode == "exact":
        return "EXACT MAX"
    if socket_roll_for_profile(profile) is not None:
        return "BEST VERIFIED + MAX SOCKETS"
    return "BEST POSSIBLE"


def effective_roll_profile(profile: dict | None) -> dict | None:
    """Return the client-facing roll profile after applying socket evidence."""

    if not isinstance(profile, dict):
        return None
    output = copy.deepcopy(profile)
    entry = socket_roll_for_profile(profile)
    if entry is None:
        return output
    output.update({
        "fieldSeeds": effective_roll_field_seeds(profile),
        "mode": effective_roll_mode(profile),
        "rollLabel": effective_roll_label(profile),
        "detail": effective_roll_detail(profile),
        "maxSockets": entry["maxSockets"],
        "maxed": entry["maxed"],
        "total": entry["total"],
        "endpointDeficit": entry["endpointDeficit"],
        "socketRoll": {
            "verified": True,
            "seed": entry["seed"],
            "maxSockets": entry["maxSockets"],
            "searchedThrough": entry.get("searchedThrough"),
        },
    })
    return output


def catalog_api_rows() -> list[dict]:
    """Expose effective socket-aware profiles without mutating the roll DB."""

    rows = []
    for row in CAT:
        output = dict(row)
        if isinstance(row.get("rollProfile"), dict):
            output["rollProfile"] = effective_roll_profile(row["rollProfile"])
        rows.append(output)
    return rows


# Fail at startup if a release forgot or corrupted the socket database. A
# silent fallback would create apparently Perfect items with lower sockets.
_load_socket_seed_document()


def roll_profile_max_sockets(profile: dict | None) -> int | None:
    """Return the measured capacity, falling back to the canonical profile."""

    if not isinstance(profile, dict):
        return None
    entry = socket_roll_for_profile(profile)
    if entry is not None:
        return int(entry["maxSockets"])
    value = profile.get("maxSockets")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 6 else None


def preferred_item_seed(row: dict) -> float:
    """Use a proven item profile when one exists; otherwise create a real roll."""
    profile = row.get("rollProfile") or catalog_roll_profile(row)
    verified = effective_roll_field_seeds(profile)
    return verified["a"] if "a" in verified else random_item_seed()


def item_seed_for_generation(row: dict, skill_id: object = None) -> float:
    """Return the ordinary roll seed or an exact skill/class target seed."""

    if skill_id is None:
        return preferred_item_seed(row)
    return float(skill_target_for_row(row, skill_id)["seed"])


def preferred_runeword_seeds(
    recipe: dict,
    base: dict,
    profile: dict | None = None,
) -> dict[str, float]:
    """Return the active a/i seeds for one concrete recipe+base pair.

    Equipment runewords carry an explicit ``zz.sockets`` recipe override.
    LoadCommonItems uses that value directly and bypasses the independent
    base-capacity/``s`` CPR chain, so synthesizing ``s`` would be both unused
    and misleading.
    """
    profile = profile or runeword_profile(recipe, base)
    if profile is None:
        raise ValueError(runeword_profile_unavailable_reason())
    seeds = {field: random_item_seed() for field in ("a", "i")}
    verified = roll_profile_field_seeds(profile)
    seeds.update({field: verified[field] for field in ("a", "i") if field in verified})
    return seeds


def forge_request_item_key(
    request_id: object,
    recipe: dict,
    base: dict,
    tab: str,
) -> str | None:
    """Map an optional client request ID to a stable, save-compatible item key."""
    if request_id is None or request_id == "":
        return None
    if (
        not isinstance(request_id, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id) is None
    ):
        raise ValueError("invalid forge request id")
    identity = "|".join((
        request_id,
        str(int(recipe["rw"])),
        str(int(base["cls"])),
        str(int(base.get("sub", 0))),
        str(int(base["b"])),
        tab,
    ))
    digest = hashlib.sha256(identity.encode("ascii")).digest()
    # Match the normal 13-digit millisecond-shaped key space and stay below
    # binary64's exact-integer ceiling. The validated request identity is also
    # checked against any existing entry before it is treated as a retry.
    numeric = 1_000_000_000_000 + (
        int.from_bytes(digest[:8], "big") % 8_000_000_000_000
    )
    return f"0-0-{numeric}-{int(base['cls'])}"


def forged_entry_matches(entry: object, recipe: dict, base: dict) -> bool:
    """Recognize the item previously written for an idempotent forge retry."""
    if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
        return False
    data = entry["data"]
    try:
        expected_sub = int(base.get("sub", 0)) if int(base["cls"]) == 3 else 0
        return (
            int(data.get("b", -1)) == int(base["b"])
            and int(data.get("c", -1)) == 0
            and int(data.get("j", 0)) == expected_sub
            and socket_rune_seq(data)
            == tuple(int(rune["b"]) for rune in recipe.get("runes", []))
        )
    except (TypeError, ValueError):
        return False


def make_data(r: dict, equipped_g=None, skill_id: object = None) -> dict:
    c = 1.0 if r["kind"] == "unique" else 0.0
    j = float(r["sub"] if r["cls"] == 3 else 0)
    profile = generation_roll_profile_for_request(r, skill_id)
    verified_seeds = effective_roll_field_seeds(profile)
    item_seed = (
        item_seed_for_generation(r, skill_id)
        if skill_id is not None
        else (verified_seeds["a"] if "a" in verified_seeds else random_item_seed())
    )
    d = {"w": 1.0, "a": item_seed, "j": j,
         "b": float(r["b"]), "c": c}
    for field in ("i", "s"):
        if field in verified_seeds:
            d[field] = verified_seeds[field]
    if r.get("cls") in (12, 13, 14, 15):
        # Native S10 drops use the compact a/b/c/j/o shape in dedicated bags.
        return {"a": d["a"], "j": 0.0, "b": d["b"], "c": 0.0, "o": 1.0}
    max_sockets = roll_profile_max_sockets(profile)
    if max_sockets is not None:
        # The game rolls capacity from ``a``; this explicit value keeps the
        # editor's empty-slot count and compatibility metadata aligned with
        # that measured result. It is not the source of the in-game roll.
        d["zz"] = {"sockets": float(max_sockets)}
    if equipped_g is not None:
        d.update({"g": float(equipped_g), "d": 0.0, "n": 0.0, "e": 0.0})
    elif c == 1.0:
        d["m"] = 1.0
    else:
        d["o"] = 1.0
    return d


def _encoded_stash_document(data: dict) -> str:
    return encode_hss(json.dumps(data, separators=(", ", ": ")))


def _runtime_save_barrier() -> None:
    """Fail closed if the game or a peer editor appeared before replacement."""
    if not INSTANCE_GUARD_ACTIVE:
        return
    if game_running():
        raise RuntimeError("Game started before the save write. Close it and retry.")
    peer_error = _active_peer_editor_error()
    if peer_error:
        raise RuntimeError(peer_error)


def write_stash(data: dict, *, check_runtime: bool = True) -> str:
    p = SAVES / "stash.hss"
    bk = backup(p)
    if check_runtime:
        _runtime_save_barrier()
    atomic_write_text(p, _encoded_stash_document(data), "ascii")
    return bk


def write_bags(slot: int, data: dict, *, check_runtime: bool = True) -> str:
    p = SAVES / f"inventory_order_{slot}.hss"
    bk = backup(p)
    if check_runtime:
        _runtime_save_barrier()
    atomic_write_text(p, encode_hss(json.dumps(data, separators=(", ", ": "))), "ascii")
    return bk


def _encoded_char_inventory_document(slot: int, inv: dict) -> tuple[Path, str | None]:
    p = SAVES / f"herosiege{slot}.hss"
    txt = decode_hss(p)
    blob = base64.b64encode(json.dumps(inv, separators=(", ", ": ")).encode()).decode()
    new, replacements = re.subn(
        r'inventory="[A-Za-z0-9+/=]*"',
        f'inventory="{blob}"',
        txt,
        count=1,
    )
    if replacements != 1:
        raise ValueError(f"inventory field not found in herosiege{slot}.hss")
    if new == txt:
        return p, None
    return p, encode_hss(new)


def write_char_inventory(slot: int, inv: dict, *, check_runtime: bool = True) -> str:
    p, encoded = _encoded_char_inventory_document(slot, inv)
    if encoded is None:
        return ""
    bk = backup(p)
    if check_runtime:
        _runtime_save_barrier()
    atomic_write_text(p, encoded, "ascii")
    return bk


# ---------- API operations ----------

def pos_free(items: dict, tab: str, pos, w: int, h: int, skip_key=None) -> bool:
    cols, rows = grid_dims(tab)
    x0, y0 = int(pos[0]), int(pos[1])
    if x0 < 0 or y0 < 0 or x0 + w > cols or y0 + h > rows:
        return False
    for k, v in items.items():
        if k == skip_key or "pos" not in v:
            continue
        it = resolve(k, v.get("data", {}))
        x, y = int(v["pos"][0]), int(v["pos"][1])
        if not (x0 + w <= x or x + it["w"] <= x0 or y0 + h <= y or y + it["h"] <= y0):
            return False
    return True


def _bulk_stash_tab_label(tab: str) -> str:
    return "Unique Tab" if tab == "unique_items" else _vault_shared_grid_label(tab)


def _bulk_deposit_source_tab(value: object = None) -> str:
    """Return one exact bulk-deposit source or the backwards-compatible all scope."""

    if value is None or value == BULK_DEPOSIT_ALL_TABS:
        return BULK_DEPOSIT_ALL_TABS
    if not isinstance(value, str) or value not in BULK_STASH_ITEM_TABS:
        raise VaultValidationError(
            "sourceTab must be all or an exact Season 10 Shared Stash item tab"
        )
    return value


def _bulk_deposit_source_label(source_tab: str) -> str:
    if source_tab == BULK_DEPOSIT_ALL_TABS:
        return "All item tabs"
    return _bulk_stash_tab_label(source_tab)


def _bulk_deposit_source_options() -> list[dict[str, str]]:
    """Stable UI choices; the backend still validates every submitted value."""

    ordered_tabs = (
        *(f"stash_tab_{index}" for index in range(1, 20)),
        "material_tab",
        "socket_tab",
        "unique_items",
    )
    return [
        {
            "tab": BULK_DEPOSIT_ALL_TABS,
            "label": "All item tabs (Shared 1-19 + Material + Socket + Unique)",
        },
        *[
            {"tab": tab, "label": _bulk_stash_tab_label(tab)}
            for tab in ordered_tabs
        ],
    ]


def _bulk_withdrawal_destination_tab(value: object = None) -> str:
    """Return an exact Shared Stash destination or native automatic routing."""

    if value is None or value == BULK_WITHDRAWAL_AUTO:
        return BULK_WITHDRAWAL_AUTO
    if not isinstance(value, str) or value not in BULK_STASH_ITEM_TABS:
        raise VaultValidationError(
            "destinationTab must be auto or an exact Season 10 Shared Stash item tab"
        )
    return value


def _bulk_withdrawal_destination_label(destination_tab: str) -> str:
    if destination_tab == BULK_WITHDRAWAL_AUTO:
        return "Automatic (original tabs + safe routing)"
    return _bulk_stash_tab_label(destination_tab)


def _bulk_withdrawal_destination_options() -> list[dict[str, str]]:
    """Stable, strictly whitelisted bulk-withdrawal destinations for the UI."""

    ordered_tabs = (
        *(f"stash_tab_{index}" for index in range(1, 20)),
        "material_tab",
        "socket_tab",
        "unique_items",
    )
    return [
        {
            "tab": BULK_WITHDRAWAL_AUTO,
            "label": "Automatic (original tabs + safe routing)",
        },
        *[
            {"tab": tab, "label": _bulk_stash_tab_label(tab)}
            for tab in ordered_tabs
        ],
    ]


def _bulk_validate_stash_document(document: object) -> dict:
    if not isinstance(document, dict):
        raise VaultValidationError("Shared Stash root is not an object")
    try:
        validate_stash_document(document)
    except HSSRecoveryError as exc:
        raise VaultValidationError(
            f"Shared Stash does not match the complete Season 10 schema ({exc}); "
            "run Save Health Check first"
        ) from exc
    for tab in BULK_STASH_ITEM_TABS:
        items = document.get(tab)
        if not isinstance(items, dict):
            raise VaultValidationError(
                f"{_bulk_stash_tab_label(tab)} is missing or malformed; run Save Health Check first"
            )
        if tab != "unique_items":
            grid_error = _stash_fill_grid_error(items, tab)
            if grid_error:
                raise VaultValidationError(
                    f"{_bulk_stash_tab_label(tab)} has an invalid grid ({grid_error}); "
                    "run Save Health Check first"
                )
        else:
            for key, entry in items.items():
                if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
                    raise VaultValidationError(
                        f"Unique Tab record {key!r} is malformed; run Save Health Check first"
                    )
    return document


def _bulk_existing_origin_tab(record, journal_origins: dict[str, str]) -> str | None:
    origin = journal_origins.get(record.id)
    if origin in BULK_STASH_ITEM_TABS:
        return origin
    if record.source in BULK_STASH_ITEM_TABS:
        return record.source
    source = str(record.source or "")
    match = re.fullmatch(r"Shared Stash Tab (\d+)", source)
    if match:
        candidate = f"stash_tab_{int(match.group(1))}"
        return candidate if candidate in BULK_STASH_ITEM_TABS else None
    match = re.fullmatch(r"(Material|Socket) Tab(?: (\d+))?", source)
    if match and not match.group(2):
        return "material_tab" if match.group(1) == "Material" else "socket_tab"
    if source.casefold() == "unique tab":
        return "unique_items"
    return None


def _bulk_item_class(key: str | None, data: dict, resolved: dict) -> int:
    if not isinstance(key, str):
        raise VaultValidationError("a Vault item has no original native class key")
    match = re.search(r"-(-?\d+)$", key)
    if not match:
        raise VaultValidationError("a Vault item has no original native class key")
    return int(match.group(1))


def _bulk_deterministic_key(
    record_id: str, cls: int, tab: str, existing: dict, preferred: str | None
) -> str:
    if preferred and preferred not in existing:
        return preferred
    digest = hashlib.sha256(f"{record_id}|{tab}|{cls}".encode("utf-8")).digest()
    numeric = 1_000_000_000_000 + (
        int.from_bytes(digest[:8], "big") % 8_000_000_000_000
    )
    candidate = f"0-0-{numeric}-{cls}"
    while candidate in existing:
        numeric += 1
        if numeric >= 9_000_000_000_000:
            numeric = 1_000_000_000_000
        candidate = f"0-0-{numeric}-{cls}"
    return candidate


def _bulk_clean_text_field(value: object, label: str) -> str:
    """Mirror the Vault metadata limits so a successful preview can be prepared."""

    if not isinstance(value, str):
        raise VaultValidationError(f"{label} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise VaultValidationError(f"{label} cannot be empty")
    if len(cleaned) > MAX_TEXT_FIELD_LENGTH:
        raise VaultValidationError(
            f"{label} cannot exceed {MAX_TEXT_FIELD_LENGTH} characters"
        )
    if any(char in "\x00\r\n" for char in cleaned):
        raise VaultValidationError(f"{label} contains forbidden control characters")
    return cleaned


def _bulk_native_item_info(
    source_key: str, raw_json: str, origin_tab: str
) -> tuple[dict, dict, int]:
    """Prove that one exact native record can make the round trip safely."""

    decoded = validate_raw_item_json(raw_json)
    data = decoded["data"]
    try:
        resolved = resolve(source_key, data)
        cls = _bulk_item_class(source_key, data, resolved)
    except Exception as exc:
        raise VaultValidationError(
            f"{_bulk_stash_tab_label(origin_tab)} item {source_key!r} has an "
            "unsupported native key and was left untouched"
        ) from exc
    known_address = resolved.get("cid") is not None or resolved.get("rwcid") is not None
    if origin_tab != "unique_items" and not known_address:
        raise VaultValidationError(
            f"{_bulk_stash_tab_label(origin_tab)} item {source_key!r} is not in "
            "the proven Season 10 catalog; it was left untouched"
        )
    return decoded, resolved, cls


def _bulk_encode_stash_document(data: dict) -> str:
    """Encode only a stash that both HSS recovery limits can decode later."""

    serialized = json.dumps(data, separators=(", ", ": "))
    try:
        decoded_bytes = len(serialized.encode("latin-1")) * 2
    except UnicodeEncodeError as exc:
        raise VaultValidationError(
            "The planned stash contains unsupported text; nothing was moved"
        ) from exc
    if decoded_bytes > MAX_HSS_DECODED_BYTES:
        raise VaultValidationError(
            "The planned stash would exceed the supported decoded HSS size limit; "
            "nothing was moved"
        )
    encoded = encode_hss(serialized)
    if len(encoded.encode("ascii")) > MAX_HSS_FILE_BYTES:
        raise VaultValidationError(
            "The planned stash would exceed the supported HSS file size limit; "
            "nothing was moved"
        )
    return encoded


def _bulk_deposit_plan(
    document: dict,
    collection_id: int,
    source_tab: str = BULK_DEPOSIT_ALL_TABS,
) -> dict:
    _bulk_validate_stash_document(document)
    source_tab = _bulk_deposit_source_tab(source_tab)
    selected_tabs = (
        BULK_STASH_ITEM_TABS
        if source_tab == BULK_DEPOSIT_ALL_TABS
        else (source_tab,)
    )
    work = copy.deepcopy(document)
    entries = []
    tab_counts = []
    for tab in selected_tabs:
        items = document[tab]
        tab_counts.append({
            "tab": tab,
            "label": _bulk_stash_tab_label(tab),
            "count": len(items),
        })
        for key in sorted(items):
            entry = items[key]
            raw_json = _vault_item_json(entry)
            raw_source_key = str(key)
            source_key = _bulk_clean_text_field(raw_source_key, "source key")
            if source_key != raw_source_key:
                raise VaultValidationError(
                    f"{_bulk_stash_tab_label(tab)} item key {raw_source_key!r} "
                    "contains unsupported outer whitespace; it was left untouched"
                )
            _decoded, resolved, _cls = _bulk_native_item_info(
                source_key, raw_json, tab
            )
            label = _bulk_clean_text_field(
                str(resolved.get("name") or source_key), "item label"
            )
            entries.append({
                "raw_item_json": raw_json,
                "source_tab": tab,
                "source_key": source_key,
                "label": label,
                # Keep the machine tab, not only a friendly label. This makes
                # a later full return origin-exact even across app restarts.
                "source": tab,
            })
        work[tab] = {}
    intent_hash = canonical_request_hash({
        "direction": "bulk_deposit",
        "collectionId": collection_id,
        "items": [
            {
                "sourceTab": row["source_tab"],
                "sourceKey": row["source_key"],
                "rawSha256": hashlib.sha256(
                    row["raw_item_json"].encode("utf-8")
                ).hexdigest(),
            }
            for row in entries
        ],
    })
    encoded_after = _bulk_encode_stash_document(work)
    return {
        "entries": entries,
        "work": work,
        "encodedAfter": encoded_after,
        "intentHash": intent_hash,
        "tabCounts": [row for row in tab_counts if row["count"]],
        "itemCount": len(entries),
        "customNamedCount": 0,
        "sourceTab": source_tab,
        "sourceLabel": _bulk_deposit_source_label(source_tab),
    }


def _bulk_withdrawal_candidates(origin: str | None, cls: int, unique: bool) -> list[str]:
    candidates: list[str] = []

    def add(tab: str) -> None:
        if tab in BULK_STASH_ITEM_TABS and tab not in candidates:
            candidates.append(tab)

    if unique:
        add("unique_items")
        return candidates
    if cls in {13, 14}:
        add("material_tab")
    elif cls == 15:
        add("socket_tab")
    # Exact original cells were already reserved in a separate first pass.
    # For displaced flexible records, prefer their dedicated container so a
    # material/socket never consumes the only slot a generic item can use.
    if origin:
        add(origin)
    for index in range(1, 20):
        add(f"stash_tab_{index}")
    return candidates


def _bulk_vault_metadata_hash(record) -> str:
    return canonical_request_hash({
        "id": record.id,
        "collectionId": record.collection_id,
        "collectionName": record.collection_name,
        "sourceItemKey": record.source_item_key,
        "label": record.label,
        "customName": record.custom_name,
        "source": record.source,
        "depositKey": record.deposit_key,
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
    })


def _bulk_withdrawal_plan(
    document: dict,
    records: list,
    journal_origins: dict[str, str],
    destination_tab: str = BULK_WITHDRAWAL_AUTO,
) -> dict:
    _bulk_validate_stash_document(document)
    destination_tab = _bulk_withdrawal_destination_tab(destination_tab)
    work = copy.deepcopy(document)
    prepared = []
    custom_named_count = 0
    for record in records:
        actual_sha256 = hashlib.sha256(
            record.raw_item_json.encode("utf-8")
        ).hexdigest()
        if actual_sha256 != record.raw_sha256:
            raise VaultValidationError(
                f"Vault item {record.id[:8]} failed its integrity hash; it remains safe in Infinite Vault"
            )
        try:
            entry = validate_raw_item_json(record.raw_item_json)
        except VaultValidationError as exc:
            raise VaultValidationError(
                f"Vault item {record.id[:8]} is malformed; it remains safe in Infinite Vault"
            ) from exc
        data = entry["data"]
        key = record.source_item_key
        try:
            if not isinstance(key, str):
                raise VaultValidationError(
                    f"Vault item {record.id[:8]} has no original native key"
                )
            resolved = resolve(key, data)
            width = max(1, int(resolved.get("w") or 1))
            height = max(1, int(resolved.get("h") or 1))
        except Exception as exc:
            raise VaultValidationError(
                f"Vault item {record.id[:8]} has unknown dimensions and cannot be packed safely"
            ) from exc
        cls = _bulk_item_class(key, data, resolved)
        try:
            unique = int(data.get("c", 0)) == 1
        except (TypeError, ValueError):
            unique = False
        if destination_tab != BULK_WITHDRAWAL_AUTO:
            if destination_tab == "unique_items":
                if not unique:
                    raise VaultValidationError(
                        f"{record.custom_name or record.label or record.id[:8]} is not a Unique item; "
                        "nothing was moved"
                    )
            elif destination_tab == "material_tab" and (
                unique or cls not in {13, 14}
            ):
                raise VaultValidationError(
                    f"{record.custom_name or record.label or record.id[:8]} is not a Material/Consumable; "
                    "nothing was moved"
                )
            elif destination_tab == "socket_tab" and (unique or cls != 15):
                raise VaultValidationError(
                    f"{record.custom_name or record.label or record.id[:8]} is not a Socket item; "
                    "nothing was moved"
                )
            # Numbered Shared Stash grids natively accept both normal and
            # Unique items. Their exact dimensions are verified below before
            # any placement is planned.
        origin = _bulk_existing_origin_tab(record, journal_origins)
        known_address = resolved.get("cid") is not None or resolved.get("rwcid") is not None
        exact_grid_destination = destination_tab not in {
            BULK_WITHDRAWAL_AUTO, "unique_items"
        }
        if not known_address and (
            origin != "unique_items" or exact_grid_destination
        ):
            raise VaultValidationError(
                f"Vault item {record.id[:8]} has unknown grid dimensions; it remains safe in Infinite Vault"
            )
        prepared.append({
            "record": record,
            "entry": entry,
            "cls": cls,
            "width": width,
            "height": height,
            "origin": origin,
            "unique": unique,
            "candidates": (
                _bulk_withdrawal_candidates(origin, cls, unique)
                if destination_tab == BULK_WITHDRAWAL_AUTO
                else [destination_tab]
            ),
        })
        if record.custom_name:
            custom_named_count += 1

    # Larger records first reduces fragmentation, while every record still
    # gets its exact origin position before any first-free fallback.
    prepared.sort(
        key=lambda row: (
            -(row["width"] * row["height"]),
            -max(row["width"], row["height"]),
            row["record"].created_at,
            row["record"].id,
        )
    )
    targets = []
    destination_counts: dict[str, int] = {}

    def record_target(row: dict, tab: str, target_key: str, target_pos) -> None:
        record = row["record"]
        target_entry = copy.deepcopy(row["entry"])
        if target_pos is None:
            target_entry.pop("pos", None)
        else:
            target_entry["pos"] = target_pos
        work[tab][target_key] = target_entry
        row["placed"] = True
        targets.append({
            "item_id": record.id,
            "raw_sha256": record.raw_sha256,
            "metadata_sha256": _bulk_vault_metadata_hash(record),
            "target_tab": tab,
            "target_key": target_key,
            "target_pos": (
                [int(target_pos[0]), int(target_pos[1])]
                if target_pos is not None else None
            ),
        })
        destination_counts[tab] = destination_counts.get(tab, 0) + 1

    for row in prepared:
        row["placed"] = False

    # Automatic routing reserves every still-free exact origin position first.
    # An exact destination intentionally ignores other origin tabs and packs
    # every compatible record into the selected container only.
    if destination_tab == BULK_WITHDRAWAL_AUTO:
        for row in prepared:
            origin = row["origin"]
            if origin not in BULK_STASH_ITEM_TABS:
                continue
            items = work[origin]
            target_key = _bulk_deterministic_key(
                row["record"].id, row["cls"], origin, items,
                row["record"].source_item_key,
            )
            if origin == "unique_items":
                record_target(row, origin, target_key, None)
                continue
            original_pos = row["entry"].get("pos")
            if (
                isinstance(original_pos, list)
                and len(original_pos) >= 2
                and pos_free(
                    items, origin, original_pos, row["width"], row["height"]
                )
            ):
                record_target(
                    row, origin, target_key,
                    [float(int(original_pos[0])), float(int(original_pos[1]))],
                )

    for row in prepared:
        if row["placed"]:
            continue
        record = row["record"]
        placed = False
        for tab in row["candidates"]:
            items = work[tab]
            target_key = _bulk_deterministic_key(
                record.id, row["cls"], tab, items, record.source_item_key
            )
            if tab == "unique_items":
                target_pos = None
                placed = True
            else:
                target_pos = find_free_pos(
                    items, tab, row["width"], row["height"]
                )
                if target_pos is None:
                    continue
                placed = True
            if placed:
                record_target(row, tab, target_key, target_pos)
                break
        if not placed:
            raise VaultValidationError(
                f"No safe Shared Stash space remains for {record.custom_name or record.label or record.id[:8]}; "
                "nothing was moved"
            )

    intent = {
        "direction": "bulk_withdrawal",
        "items": [
            {
                "itemId": row["item_id"],
                "rawSha256": row["raw_sha256"],
                "metadataSha256": row["metadata_sha256"],
                "targetTab": row["target_tab"],
                "targetKey": row["target_key"],
                "targetPos": row["target_pos"],
            }
            for row in targets
        ],
    }
    if destination_tab != BULK_WITHDRAWAL_AUTO:
        intent["destinationTab"] = destination_tab
    intent_hash = canonical_request_hash(intent)
    encoded_after = _bulk_encode_stash_document(work)
    return {
        "targets": targets,
        "work": work,
        "encodedAfter": encoded_after,
        "intentHash": intent_hash,
        "tabCounts": [
            {
                "tab": tab,
                "label": _bulk_stash_tab_label(tab),
                "count": destination_counts[tab],
            }
            for tab in BULK_STASH_ITEM_TABS if destination_counts.get(tab)
        ],
        "itemCount": len(targets),
        "customNamedCount": custom_named_count,
        "destinationTab": destination_tab,
        "destinationLabel": _bulk_withdrawal_destination_label(destination_tab),
    }


def _bulk_preview_token(
    direction: str,
    stash_sha256: str,
    intent_hash: str,
    item_count: int,
    collection_id: int | None,
) -> str:
    return canonical_request_hash({
        "kind": "vault_bulk_preview",
        "routingVersion": BULK_ROUTING_VERSION,
        "direction": direction,
        "collectionId": collection_id,
        "stashSha256": stash_sha256,
        "intentHash": intent_hash,
        "itemCount": item_count,
    })


def _bulk_plan_locked(
    direction: str,
    collection_id: int | None,
    source_tab: object = None,
    destination_tab: object = None,
) -> dict:
    stash_path = SAVES / "stash.hss"
    stash_sha256 = _file_sha256(stash_path)
    document = json.loads(decode_hss_bytes_strict(stash_path.read_bytes(), XOR_KEY))
    store = vault_store()
    collection_name = None
    if direction == "stash-to-vault":
        source_tab = _bulk_deposit_source_tab(source_tab)
        if collection_id is None:
            raise VaultValidationError("a destination Vault collection is required")
        collection = next(
            (row for row in store.list_collections() if row.id == collection_id), None
        )
        if collection is None:
            raise VaultValidationError("destination Vault collection was not found")
        collection_name = collection.name
        plan = _bulk_deposit_plan(document, collection_id, source_tab)
    elif direction == "vault-to-stash":
        if source_tab not in (None, BULK_DEPOSIT_ALL_TABS):
            raise VaultValidationError(
                "sourceTab can only be used when moving Shared Stash items into the Vault"
            )
        source_tab = None
        destination_tab = _bulk_withdrawal_destination_tab(destination_tab)
        records = store.list_all_available_items()
        plan = _bulk_withdrawal_plan(
            document, records, store.available_item_origin_tabs(), destination_tab
        ) if records else {
            "targets": [], "work": document,
            "encodedAfter": _bulk_encode_stash_document(document),
            "intentHash": canonical_request_hash({
                "direction": "bulk_withdrawal", "items": [],
                **({"destinationTab": destination_tab}
                   if destination_tab != BULK_WITHDRAWAL_AUTO else {}),
            }),
            "tabCounts": [], "itemCount": 0, "customNamedCount": 0,
            "destinationTab": destination_tab,
            "destinationLabel": _bulk_withdrawal_destination_label(destination_tab),
        }
    else:
        raise VaultValidationError("unknown bulk transfer direction")
    plan.update({
        "direction": direction,
        "collectionId": collection_id,
        "collectionName": collection_name,
        "sourceTab": source_tab,
        "sourceLabel": (
            _bulk_deposit_source_label(source_tab)
            if direction == "stash-to-vault" else None
        ),
        "destinationTab": (
            plan.get("destinationTab") if direction == "vault-to-stash" else None
        ),
        "destinationLabel": (
            plan.get("destinationLabel") if direction == "vault-to-stash" else None
        ),
        "stashSha256": stash_sha256,
        "previewToken": _bulk_preview_token(
            direction, stash_sha256, plan["intentHash"], plan["itemCount"],
            collection_id if direction == "stash-to-vault" else None,
        ),
    })
    return plan


def _bulk_public_preview(plan: dict, *, game_is_running: bool) -> dict:
    item_count = int(plan["itemCount"])
    return {
        "direction": plan["direction"],
        "itemCount": item_count,
        "tabCounts": plan["tabCounts"],
        "collectionId": plan.get("collectionId"),
        "collectionName": plan.get("collectionName"),
        "sourceTab": plan.get("sourceTab"),
        "sourceLabel": plan.get("sourceLabel"),
        "destinationTab": plan.get("destinationTab"),
        "destinationLabel": plan.get("destinationLabel"),
        "customNamedCount": int(plan.get("customNamedCount", 0)),
        "previewToken": plan["previewToken"],
        "routingVersion": BULK_ROUTING_VERSION,
        "gameRunning": game_is_running,
        "canRun": bool(item_count and not game_is_running),
        "empty": item_count == 0,
    }


def vault_bulk_preview(query: dict) -> dict:
    direction = query.get("direction", [""])[0]
    collection_id = None
    source_tab = None
    destination_tab = None
    if direction == "stash-to-vault":
        if "destinationTab" in query:
            return {
                "err": "destinationTab can only be used when returning Vault items to Shared Stash"
            }
        raw_collection = query.get("collectionId", [None])[0]
        try:
            collection_id = int(raw_collection)
        except (TypeError, ValueError):
            return {"err": "Select a destination Vault collection."}
        try:
            source_tab = _bulk_deposit_source_tab(
                query.get("sourceTab", [None])[0]
            )
        except VaultValidationError as exc:
            return {"err": str(exc)}
    elif direction == "vault-to-stash":
        if query.get("sourceTab", [None])[0] not in (
            None, BULK_DEPOSIT_ALL_TABS
        ):
            return {
                "err": "sourceTab can only be used when moving Shared Stash items into the Vault"
            }
        try:
            destination_tab = _bulk_withdrawal_destination_tab(
                query.get("destinationTab", [None])[0]
            )
        except VaultValidationError as exc:
            return {"err": str(exc)}
    elif "destinationTab" in query:
        return {
            "err": "destinationTab can only be used when returning Vault items to Shared Stash"
        }
    peer_error = _active_peer_editor_error()
    if peer_error:
        return {"err": peer_error}
    try:
        with SAVE_WRITE_LOCK:
            stash_path = SAVES / "stash.hss"
            with _exclusive_save_file(stash_path):
                peer_error = _active_peer_editor_error()
                if peer_error:
                    return {"err": peer_error}
                running = game_running()
                if not running:
                    recovery = _reconcile_vault_transfers_locked(stash_path)
                    if recovery.get("err"):
                        return {"err": recovery["err"]}
                    if recovery.get("pending") or recovery.get("conflicts"):
                        return {
                            "err": "An interrupted Infinite Vault transfer must be recovered first."
                        }
                plan = _bulk_plan_locked(
                    direction, collection_id, source_tab, destination_tab
                )
                return _bulk_public_preview(plan, game_is_running=running)
    except (VaultError, OSError, ValueError, json.JSONDecodeError) as exc:
        return {"err": f"Bulk transfer preview failed: {exc}"}


def _vault_transfer_result(transfer, backup_name: str = "") -> dict:
    if transfer.status == "conflict":
        return {"err": "Infinite Vault transfer needs recovery: " + (transfer.error or "state conflict")}
    if transfer.status == "cancelled":
        return {"err": "The interrupted transfer was safely cancelled; the original item was kept."}
    result = {
        "ok": "Infinite Vault transfer completed",
        "itemId": transfer.item_id,
        "transfer": transfer.as_dict(),
    }
    if backup_name:
        result["backup"] = backup_name
    return result


def _reconcile_vault_transfers_locked(stash_path: Path) -> dict:
    """Resolve pending journal rows while SAVE and stash OS locks are held."""
    if game_running():
        return {"recovered": 0, "conflicts": 0, "pending": None, "deferred": True}
    if not stash_path.exists():
        return {"recovered": 0, "conflicts": 1, "pending": None,
                "err": "stash.hss is missing; pending vault transfers were left untouched"}
    recovered = 0
    recovery_warnings = []
    current_hash = _file_sha256(stash_path)
    store = vault_store()
    stash = None

    def decoded_stash() -> dict:
        nonlocal stash
        if stash is None:
            stash = json.loads(decode_hss(stash_path))
        return stash

    # Parent batches are always resolved as a whole. Their member rows are
    # deliberately excluded from list_pending_transfers().
    for batch in store.list_pending_transfer_batches():
        previous = batch.status
        if current_hash in {batch.stash_before_sha256, batch.stash_after_sha256}:
            updated_batch = store.reconcile_transfer_batch(
                batch.request_id, current_hash
            )
        else:
            members = store.list_transfer_batch_members(batch.request_id)
            try:
                document = decoded_stash()
            except Exception:
                # When the save cannot be decoded there is no trustworthy
                # per-item evidence.  Resolve toward Vault ownership: make
                # prepared deposit copies available, or release withdrawal
                # reservations back to the Vault.  This may leave duplicates
                # in an unreadable/recovered stash, but never destroys the
                # only item copy we can still prove and inspect.
                outcome = (
                    "committed" if batch.direction == "deposit" else "cancelled"
                )
                updated_batch = store.resolve_transfer_batch_by_evidence(
                    batch.request_id, outcome, current_hash,
                    "stash payload was unreadable; Vault ownership was preserved and Shared Stash may contain duplicate copies",
                )
                recovery_warnings.append({
                    "code": "vault_ownership_preserved_possible_duplicate",
                    "requestId": batch.request_id,
                    "direction": batch.direction,
                    "itemCount": batch.item_count,
                    "outcome": outcome,
                    "possibleDuplicate": True,
                    "message": (
                        f"Vault ownership was preserved for {batch.item_count} items because "
                        "stash.hss could not be decoded. Shared Stash may contain duplicate copies after recovery."
                    ),
                })
            else:
                if batch.direction == "deposit":
                    evidence = []
                    for member in members:
                        entries = document.get(member.source_tab, {})
                        candidate = (
                            entries.get(member.source_key)
                            if isinstance(entries, dict) else None
                        )
                        evidence.append(candidate == member.decoded_item())
                    if all(evidence):
                        updated_batch = store.resolve_transfer_batch_by_evidence(
                            batch.request_id, "cancelled", current_hash,
                            "all exact journaled source entries still exist",
                        )
                    elif not any(evidence):
                        updated_batch = store.resolve_transfer_batch_by_evidence(
                            batch.request_id, "committed", current_hash,
                            "all exact journaled source entries are absent",
                        )
                    else:
                        updated_batch = store.mark_transfer_batch_conflict(
                            batch.request_id,
                            "only part of the prepared deposit batch exists in Shared Stash",
                            observed_stash_sha256=current_hash,
                        )
                else:
                    exact_targets = []
                    empty_targets = []
                    for member in members:
                        entries = document.get(member.target_tab, {})
                        candidate = (
                            entries.get(member.target_key)
                            if isinstance(entries, dict) else None
                        )
                        expected = member.decoded_item()
                        if member.target_tab == "unique_items":
                            expected.pop("pos", None)
                        elif member.target_pos is not None:
                            expected["pos"] = [
                                float(member.target_pos[0]), float(member.target_pos[1])
                            ]
                        exact_targets.append(candidate == expected)
                        empty_targets.append(candidate is None)
                    if all(exact_targets):
                        updated_batch = store.resolve_transfer_batch_by_evidence(
                            batch.request_id, "committed", current_hash,
                            "all exact prepared target entries exist",
                        )
                    elif all(empty_targets):
                        updated_batch = store.resolve_transfer_batch_by_evidence(
                            batch.request_id, "cancelled", current_hash,
                            "all prepared target keys are absent",
                        )
                    else:
                        updated_batch = store.mark_transfer_batch_conflict(
                            batch.request_id,
                            "only part of the prepared withdrawal batch exists in Shared Stash",
                            observed_stash_sha256=current_hash,
                        )
        if (
            updated_batch.status in {"committed", "cancelled"}
            and previous not in {"committed", "cancelled"}
        ):
            recovered += 1

    pending = store.list_pending_transfers()
    for transfer in pending:
        previous = transfer.status
        if current_hash in {transfer.stash_before_sha256, transfer.stash_after_sha256}:
            updated = store.reconcile_transfer(transfer.request_id, current_hash)
        else:
            try:
                document = decoded_stash()
            except Exception:
                # Legacy single-item journals need the same fail-safe as
                # parent batches.  With no readable per-item evidence, keep
                # the protected Vault-side copy/reservation and explicitly
                # warn that a recovered stash could contain a duplicate.
                outcome = (
                    "committed" if transfer.direction == "deposit" else "cancelled"
                )
                updated = store.resolve_transfer_by_evidence(
                    transfer.request_id, outcome, current_hash,
                    "stash payload was unreadable; Vault ownership was preserved and Shared Stash may contain a duplicate copy",
                )
                recovery_warnings.append({
                    "code": "vault_ownership_preserved_possible_duplicate",
                    "requestId": transfer.request_id,
                    "direction": transfer.direction,
                    "itemCount": 1,
                    "outcome": outcome,
                    "possibleDuplicate": True,
                    "message": (
                        "Vault ownership was preserved for 1 item because stash.hss "
                        "could not be decoded. Shared Stash may contain a duplicate copy after recovery."
                    ),
                })
            else:
                if transfer.direction == "deposit":
                    expected = transfer.decoded_item()
                    source_entries = document.get(transfer.source_tab, {})
                    source_entry = (
                        source_entries.get(transfer.source_key)
                        if isinstance(source_entries, dict) else None
                    )
                    exact_source_exists = source_entry == expected
                    outcome = "cancelled" if exact_source_exists else "committed"
                    updated = store.resolve_transfer_by_evidence(
                        transfer.request_id, outcome, current_hash,
                        "exact journaled source entry exists" if exact_source_exists
                        else "exact journaled source entry is absent",
                    )
                else:
                    entries = document.get(transfer.target_tab, {})
                    candidate = entries.get(transfer.target_key) if isinstance(entries, dict) else None
                    expected = transfer.decoded_item()
                    if transfer.target_pos is not None:
                        expected["pos"] = [float(transfer.target_pos[0]), float(transfer.target_pos[1])]
                    if candidate == expected:
                        updated = store.resolve_transfer_by_evidence(
                            transfer.request_id, "committed", current_hash,
                            "exact prepared target entry exists",
                        )
                    elif candidate is None:
                        updated = store.resolve_transfer_by_evidence(
                            transfer.request_id, "cancelled", current_hash,
                            "prepared target key is absent",
                        )
                    else:
                        updated = store.resolve_transfer_by_evidence(
                            transfer.request_id, "cancelled", current_hash,
                            "prepared target key contains a different item",
                        )
        if updated.status in {"committed", "cancelled"} and previous not in {"committed", "cancelled"}:
            recovered += 1
    remaining = store.list_pending_transfers()
    remaining_batches = store.list_pending_transfer_batches()
    return {
        "recovered": recovered,
        "conflicts": (
            sum(1 for row in remaining if row.status == "conflict")
            + sum(1 for row in remaining_batches if row.status == "conflict")
        ),
        "pending": len(remaining) + len(remaining_batches),
        "warnings": recovery_warnings,
    }


def reconcile_vault_transfers() -> dict:
    """Resolve transactions interrupted between SQLite and stash replacement."""
    path = Path(VAULT_DB_FILE).expanduser().resolve()
    if not path.exists():
        return {"recovered": 0, "conflicts": 0, "pending": 0}
    peer_error = _active_peer_editor_error()
    if peer_error:
        return {"recovered": 0, "conflicts": 0, "pending": None, "err": peer_error}
    stash_path = SAVES / "stash.hss"
    with SAVE_WRITE_LOCK:
        with _exclusive_save_file(stash_path):
            peer_error = _active_peer_editor_error()
            if peer_error:
                return {"recovered": 0, "conflicts": 0, "pending": None, "err": peer_error}
            return _reconcile_vault_transfers_locked(stash_path)


def _vault_save_mutation_gate(*, stash_lock_held: bool = False) -> dict | None:
    """Reconcile the cross-store journal before any ordinary save mutation."""
    if not Path(VAULT_DB_FILE).expanduser().resolve().exists():
        return None
    recovery = (
        _reconcile_vault_transfers_locked(SAVES / "stash.hss")
        if stash_lock_held else reconcile_vault_transfers()
    )
    if recovery.get("deferred"):
        return {"err": "Game is running! Close it before editing saves."}
    if recovery.get("err"):
        return {"err": recovery["err"]}
    if recovery.get("pending") or recovery.get("conflicts"):
        return {
            "err": "An interrupted Infinite Vault transfer must be recovered before another save change. Open Infinite Vault for details."
        }
    return None


def vault_meta() -> dict:
    running = game_running()
    recovery = {"conflicts": 0, "pending": 0}
    if not running:
        recovery = reconcile_vault_transfers()
    store = vault_store()
    if running:
        pending_rows = store.list_pending_transfers()
        pending_batches = store.list_pending_transfer_batches()
        recovery = {
            "pending": len(pending_rows) + len(pending_batches),
            "conflicts": (
                sum(1 for row in pending_rows if row.status == "conflict")
                + sum(1 for row in pending_batches if row.status == "conflict")
            ),
        }
    collections = store.list_collections()
    recovery_batches = [
        {
            "requestId": row.request_id,
            "direction": row.direction,
            "status": row.status,
            "itemCount": row.item_count,
            "error": row.error,
            "recommendedAction": "preserve-vault-ownership",
            "possibleDuplicate": True,
        }
        for row in store.list_pending_transfer_batches()
    ]
    default = next((row for row in collections if row.name == "Vault"),
                   collections[0] if collections else None)
    return {
        "collections": [row.as_dict() for row in collections],
        "defaultCollectionId": default.id if default else None,
        "total": store.count_items(status="available"),
        "gameRunning": running,
        "pending": recovery.get("pending"),
        "conflicts": recovery.get("conflicts", 0),
        "recoveryWarnings": recovery.get("warnings", []),
        "recoveryBatches": recovery_batches,
        "databaseName": Path(VAULT_DB_FILE).name,
        "transferTabs": _vault_transfer_tabs(),
        "bulkDepositTabs": _bulk_deposit_source_options(),
        "bulkWithdrawalTabs": _bulk_withdrawal_destination_options(),
    }


def vault_items(query: dict) -> dict:
    try:
        limit = max(1, min(200, int(query.get("limit", [120])[0])))
        offset = max(0, int(query.get("offset", [0])[0]))
    except (TypeError, ValueError):
        raise VaultValidationError("invalid vault pagination")
    search = query.get("q", [""])[0]
    collection_raw = query.get("collectionId", [None])[0]
    collection = None
    if collection_raw not in (None, "", "all"):
        try:
            collection = int(collection_raw)
        except (TypeError, ValueError):
            raise VaultValidationError("invalid collectionId")
    store = vault_store()
    rows = store.list_items(
        collection=collection, search=search, status="available",
        limit=min(500, limit + 1), offset=offset,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    if search and search.strip():
        total = offset + len(rows) + (1 if has_more else 0)
    else:
        total = store.count_items(collection=collection, status="available")
    try:
        tooltip_build_status = _editor_runtime_build_status()
    except Exception:
        tooltip_build_status = {
            "matched": False,
            "code": "build_check_failed",
            "message": "Game build could not be verified.",
        }
    return {
        "items": [_vault_item_payload(row, tooltip_build_status) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": has_more,
    }


def op_vault_resolve_batch(body: dict) -> dict:
    """Explicitly resolve an ambiguous parent batch toward Vault ownership.

    This operation never changes ``stash.hss``.  A deposit batch is committed
    so its protected Vault copies become available; a withdrawal batch is
    cancelled so its reservations become available again.  If Shared Stash
    already contains some or all members, duplicates can therefore remain and
    are reported instead of being silently deleted.
    """

    peer_error = _active_peer_editor_error()
    if peer_error:
        return {"err": peer_error, "code": "peer_editor"}
    if game_running():
        return {
            "err": "Hero Siege is running. Close it before resolving Vault ownership.",
            "code": "game_running",
        }
    try:
        if body.get("action") != "preserve-vault-ownership":
            raise VaultValidationError(
                "the explicit preserve-vault-ownership action is required"
            )
        request_id = _vault_request_id(body.get("requestId"))
        with SAVE_WRITE_LOCK:
            stash_path = SAVES / "stash.hss"
            with _exclusive_save_file(stash_path):
                peer_error = _active_peer_editor_error()
                if peer_error:
                    return {"err": peer_error, "code": "peer_editor"}
                if game_running():
                    return {
                        "err": "Hero Siege started while ownership resolution was waiting.",
                        "code": "game_running",
                    }
                if not stash_path.exists():
                    return {
                        "err": "stash.hss is missing. Vault ownership was left unchanged.",
                        "code": "stash_missing",
                    }
                current_hash = _file_sha256(stash_path)
                store = vault_store()
                batch = store.get_transfer_batch(request_id)
                outcome = "committed" if batch.direction == "deposit" else "cancelled"
                if batch.status in {"committed", "cancelled"}:
                    if batch.status != outcome:
                        return {
                            "err": "This batch already finished with the opposite ownership result.",
                            "code": "batch_already_finished",
                            "batch": batch.as_dict(),
                        }
                    resolved = batch
                    already = True
                else:
                    resolved = store.resolve_transfer_batch_by_evidence(
                        request_id, outcome, current_hash,
                        "user explicitly selected Vault ownership; Shared Stash may contain duplicate copies",
                    )
                    already = False
                warning = (
                    f"Vault ownership is preserved for {resolved.item_count} items. "
                    "Any copies already present in Shared Stash were left untouched, so duplicates may exist."
                )
                return {
                    "ok": (
                        "Vault ownership was already preserved"
                        if already else "Vault ownership preserved"
                    ),
                    "code": "vault_ownership_preserved_possible_duplicate",
                    "possibleDuplicate": True,
                    "warning": warning,
                    "batch": resolved.as_dict(),
                }
    except VaultError as exc:
        return {"err": str(exc), "code": "vault_error"}
    except (OSError, TypeError, ValueError) as exc:
        return {"err": f"Vault ownership resolution failed: {exc}", "code": "resolve_failed"}


def op_vault_collections(body: dict) -> dict:
    try:
        action = body.get("action")
        store = vault_store()
        if action == "create":
            row = store.create_collection(body.get("name"))
            return {"ok": f"Collection created: {row.name}", "collection": row.as_dict()}
        if action == "rename":
            row = store.rename_collection(int(body.get("collectionId")), body.get("name"))
            return {"ok": f"Collection renamed: {row.name}", "collection": row.as_dict()}
        if action == "delete":
            collections = store.list_collections()
            if len(collections) <= 1:
                return {"err": "The last vault collection cannot be deleted."}
            collection_id = int(body.get("collectionId"))
            name = next((row.name for row in collections if row.id == collection_id), "collection")
            store.delete_collection(collection_id)
            return {"ok": f"Empty collection deleted: {name}"}
        return {"err": "unknown vault collection action"}
    except (VaultError, TypeError, ValueError) as exc:
        return {"err": str(exc)}


def op_vault_item(body: dict) -> dict:
    try:
        action = body.get("action")
        if action == "move":
            row = vault_store().move_item(
                body.get("itemId"), int(body.get("collectionId"))
            )
            message = f"Moved to {row.collection_name}"
        elif action == "setCustomName":
            row = vault_store().set_item_custom_name(
                body.get("itemId"), body.get("customName")
            )
            message = (
                f'Custom name set to "{row.custom_name}"'
                if row.custom_name
                else "Custom name cleared"
            )
        else:
            return {"err": "unknown vault item action"}
        return {"ok": message, "item": _vault_item_payload(row)}
    except (VaultError, TypeError, ValueError) as exc:
        return {"err": str(exc)}


def _existing_vault_request(store, request_id: str, request_hash: str, direction: str):
    try:
        transfer = store.get_transfer(request_id)
    except VaultNotFoundError:
        return None
    if transfer.direction != direction or transfer.request_hash != request_hash:
        raise VaultConflictError("requestId was already used for different transfer data")
    return transfer


def op_vault_deposit(body: dict) -> dict:
    """Move one exact item from a grid-backed shared-stash tab into SQLite."""
    peer_error = _active_peer_editor_error()
    if peer_error:
        return {"err": peer_error}
    if game_running():
        return {"err": "Game is running! Close it before using Infinite Vault."}
    try:
        source = body.get("source")
        tab = _vault_stash_tab(source, "source")
        key = body.get("key")
        if not isinstance(key, str) or not key or len(key) > 512:
            raise VaultValidationError("invalid source item key")
        request_id = _vault_request_id(body.get("requestId"))
        collection_id = int(body.get("collectionId"))
        request_hash = canonical_request_hash({
            "direction": "deposit", "source": {"type": "stash", "tab": tab},
            "key": key, "collectionId": collection_id,
        })
        with SAVE_WRITE_LOCK:
            store = vault_store()
            prior = _existing_vault_request(store, request_id, request_hash, "deposit")
            if prior is not None:
                if prior.status in {"prepared", "conflict"}:
                    stash_path = SAVES / "stash.hss"
                    with _exclusive_save_file(stash_path):
                        peer_error = _active_peer_editor_error()
                        if peer_error:
                            return {"err": peer_error}
                        if game_running():
                            return {"err": "Game started while the transfer was waiting. Close it and retry."}
                        prior = store.reconcile_transfer(request_id, _file_sha256(stash_path))
                result = _vault_transfer_result(prior)
                if prior.status == "committed":
                    result["ok"] = "Item is already stored in Infinite Vault"
                    result["item"] = _vault_item_payload(store.get_item(prior.item_id))
                return result
            recovery = reconcile_vault_transfers()
            if recovery.get("deferred"):
                return {"err": "Game started while the transfer was being prepared. Close it and retry."}
            if recovery.get("err"):
                return {"err": recovery["err"]}
            if recovery.get("conflicts"):
                return {"err": "A previous Infinite Vault transfer needs recovery before a new transfer can start."}
            stash_path = SAVES / "stash.hss"
            with _exclusive_save_file(stash_path):
                peer_error = _active_peer_editor_error()
                if peer_error:
                    return {"err": peer_error}
                if game_running():
                    return {"err": "Game started while the transfer was waiting. Close it and retry."}
                before_hash = _file_sha256(stash_path)
                stash = json.loads(decode_hss(stash_path))
                items = stash.get(tab)
                if not isinstance(items, dict) or key not in items:
                    return {"err": "item to store was not found in the selected shared-stash tab"}
                entry = items[key]
                if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
                    return {"err": "the selected stash record is malformed"}
                raw_item_json = _vault_item_json(entry)
                resolved = resolve(key, entry["data"])
                del items[key]
                encoded_after = _encoded_stash_document(stash)
                after_hash = hashlib.sha256(encoded_after.encode("ascii")).hexdigest()
                transfer = store.prepare_deposit(
                    collection_id, raw_item_json,
                    request_id=request_id, request_hash=request_hash,
                    source_tab=tab, source_key=key,
                    stash_before_sha256=before_hash, stash_after_sha256=after_hash,
                    label=resolved.get("name"),
                    source=_vault_shared_grid_label(tab),
                )
                if transfer.status != "prepared":
                    return _vault_transfer_result(transfer)
                peer_error = _active_peer_editor_error()
                if peer_error:
                    store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    return {"err": peer_error}
                if game_running():
                    store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    return {"err": "Game started before the stash write. The prepared vault copy was safely cancelled."}
                if _file_sha256(stash_path) != before_hash:
                    updated = store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    return _vault_transfer_result(updated)
                try:
                    backup_name = backup(stash_path)
                    if game_running():
                        store.reconcile_transfer(request_id, _file_sha256(stash_path))
                        return {"err": "Game started during backup. The prepared vault copy was safely cancelled."}
                    peer_error = _active_peer_editor_error()
                    if peer_error:
                        store.reconcile_transfer(request_id, _file_sha256(stash_path))
                        return {"err": peer_error}
                    if _file_sha256(stash_path) != before_hash:
                        updated = store.reconcile_transfer(request_id, _file_sha256(stash_path))
                        return _vault_transfer_result(updated, backup_name)
                    atomic_write_text(stash_path, encoded_after, "ascii")
                except Exception:
                    store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    raise
                observed = _file_sha256(stash_path)
                committed = store.commit_deposit(request_id, observed)
                result = _vault_transfer_result(committed, backup_name)
                if committed.status == "committed":
                    result["ok"] = f"{resolved.get('name', 'Item')} stored in Infinite Vault"
                    result["item"] = _vault_item_payload(store.get_item(committed.item_id))
                return result
    except VaultError as exc:
        return {"err": str(exc)}
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"err": f"Infinite Vault deposit failed: {exc}"}


def op_vault_withdraw(body: dict) -> dict:
    """Return one vault item to a grid-backed shared-stash tab without loss."""
    peer_error = _active_peer_editor_error()
    if peer_error:
        return {"err": peer_error}
    if game_running():
        return {"err": "Game is running! Close it before using Infinite Vault."}
    try:
        tab = _vault_stash_tab(body.get("target"), "target")
        item_id = body.get("itemId")
        request_id = _vault_request_id(body.get("requestId"))
        request_hash = canonical_request_hash({
            "direction": "withdrawal", "itemId": item_id,
            "target": {"type": "stash", "tab": tab},
        })
        with SAVE_WRITE_LOCK:
            store = vault_store()
            prior = _existing_vault_request(store, request_id, request_hash, "withdrawal")
            if prior is not None:
                if prior.status in {"prepared", "conflict"}:
                    stash_path = SAVES / "stash.hss"
                    with _exclusive_save_file(stash_path):
                        peer_error = _active_peer_editor_error()
                        if peer_error:
                            return {"err": peer_error}
                        if game_running():
                            return {"err": "Game started while the transfer was waiting. Close it and retry."}
                        prior = store.reconcile_transfer(request_id, _file_sha256(stash_path))
                result = _vault_transfer_result(prior)
                if prior.status == "committed":
                    result["ok"] = "Item is already back in Shared Stash"
                return result
            recovery = reconcile_vault_transfers()
            if recovery.get("deferred"):
                return {"err": "Game started while the transfer was being prepared. Close it and retry."}
            if recovery.get("err"):
                return {"err": recovery["err"]}
            if recovery.get("conflicts"):
                return {"err": "A previous Infinite Vault transfer needs recovery before a new transfer can start."}
            stash_path = SAVES / "stash.hss"
            with _exclusive_save_file(stash_path):
                peer_error = _active_peer_editor_error()
                if peer_error:
                    return {"err": peer_error}
                if game_running():
                    return {"err": "Game started while the transfer was waiting. Close it and retry."}
                before_hash = _file_sha256(stash_path)
                stash = json.loads(decode_hss(stash_path))
                items = stash.get(tab)
                if not isinstance(items, dict):
                    return {"err": "selected shared-stash tab does not exist"}
                record = store.get_item(item_id)
                entry = record.decoded_item()
                if not isinstance(entry.get("data"), dict):
                    raise VaultValidationError("stored item record is malformed")
                source_key = record.source_item_key or ""
                try:
                    cls = int(source_key.rsplit("-", 1)[1])
                except (ValueError, IndexError):
                    cls = int(resolve("0-0-0--1", entry["data"]).get("cls", -1))
                target_key = source_key if source_key and source_key not in items else fresh_key(cls, items)
                resolved = resolve(target_key, entry["data"])
                original_pos = entry.get("pos")
                if (isinstance(original_pos, list) and len(original_pos) >= 2
                        and pos_free(items, tab, original_pos, resolved["w"], resolved["h"])):
                    pos = [float(int(original_pos[0])), float(int(original_pos[1]))]
                else:
                    pos = find_free_pos(items, tab, resolved["w"], resolved["h"])
                if pos is None:
                    return {"err": f"No space in {_vault_shared_grid_label(tab)}. The item is still safe in Infinite Vault."}
                entry["pos"] = pos
                items[target_key] = entry
                encoded_after = _encoded_stash_document(stash)
                after_hash = hashlib.sha256(encoded_after.encode("ascii")).hexdigest()
                transfer = store.prepare_withdrawal(
                    item_id, request_id=request_id, request_hash=request_hash,
                    target_tab=tab, target_key=target_key, target_pos=(int(pos[0]), int(pos[1])),
                    stash_before_sha256=before_hash, stash_after_sha256=after_hash,
                )
                if transfer.status != "prepared":
                    return _vault_transfer_result(transfer)
                peer_error = _active_peer_editor_error()
                if peer_error:
                    store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    return {"err": peer_error}
                if game_running():
                    store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    return {"err": "Game started before the stash write. The vault item remains safely stored."}
                if _file_sha256(stash_path) != before_hash:
                    updated = store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    return _vault_transfer_result(updated)
                try:
                    backup_name = backup(stash_path)
                    if game_running():
                        store.reconcile_transfer(request_id, _file_sha256(stash_path))
                        return {"err": "Game started during backup. The vault item remains safely stored."}
                    peer_error = _active_peer_editor_error()
                    if peer_error:
                        store.reconcile_transfer(request_id, _file_sha256(stash_path))
                        return {"err": peer_error}
                    if _file_sha256(stash_path) != before_hash:
                        updated = store.reconcile_transfer(request_id, _file_sha256(stash_path))
                        return _vault_transfer_result(updated, backup_name)
                    atomic_write_text(stash_path, encoded_after, "ascii")
                except Exception:
                    store.reconcile_transfer(request_id, _file_sha256(stash_path))
                    raise
                observed = _file_sha256(stash_path)
                committed = store.commit_withdrawal(request_id, observed)
                result = _vault_transfer_result(store.get_transfer(request_id), backup_name)
                if committed.status == "committed":
                    result["ok"] = f"{resolved.get('name', 'Item')} returned to {_vault_shared_grid_label(tab)}"
                return result
    except VaultError as exc:
        return {"err": str(exc)}
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"err": f"Infinite Vault withdrawal failed: {exc}"}


def _bulk_operation_result(batch, *, backup_name: str = "", already: bool = False) -> dict:
    base = {
        "status": batch.status,
        "terminal": batch.status in {"committed", "cancelled"},
        "retryable": batch.status == "prepared",
        "movedCount": batch.item_count if batch.status == "committed" else 0,
        "batch": batch.as_dict(),
    }
    if batch.status == "committed":
        base.update({
            "ok": (
                f"{batch.item_count} items were already moved safely"
                if already else f"{batch.item_count} items moved safely in one batch"
            ),
            "code": "batch_committed",
        })
    elif batch.status == "cancelled":
        base.update({
            "err": "The interrupted bulk transfer was safely cancelled; every original item was kept.",
            "code": "batch_cancelled",
            "retryable": False,
        })
    else:
        base.update({
            "err": "Bulk transfer needs recovery: " + (batch.error or "state conflict"),
            "code": "batch_recovery_required",
            "requiresOwnershipDecision": batch.status == "conflict",
            "recommendedAction": (
                "preserve-vault-ownership" if batch.status == "conflict" else None
            ),
        })
    if backup_name:
        base["backup"] = backup_name
    return base


def op_vault_bulk(body: dict) -> dict:
    """Move one previewed stash scope or the complete Vault atomically."""

    peer_error = _active_peer_editor_error()
    if peer_error:
        return {"err": peer_error, "code": "peer_editor", "terminal": True}
    if game_running():
        return {
            "err": "Game is running! Close it before a bulk transfer.",
            "code": "game_running", "terminal": True, "retryable": False,
        }
    try:
        direction = body.get("direction")
        if direction not in {"stash-to-vault", "vault-to-stash"}:
            raise VaultValidationError("unknown bulk transfer direction")
        request_id = _vault_request_id(body.get("requestId"))
        preview_token = body.get("previewToken")
        if not isinstance(preview_token, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", preview_token
        ):
            raise VaultValidationError("a valid bulk preview token is required")
        collection_id = None
        source_tab = None
        destination_tab = None
        if direction == "stash-to-vault":
            collection_id = int(body.get("collectionId"))
            source_tab = _bulk_deposit_source_tab(body.get("sourceTab"))
            if body.get("destinationTab") is not None:
                raise VaultValidationError(
                    "destinationTab can only be used when returning Vault items to Shared Stash"
                )
        else:
            if body.get("sourceTab") not in (None, BULK_DEPOSIT_ALL_TABS):
                raise VaultValidationError(
                    "sourceTab can only be used when moving Shared Stash items into the Vault"
                )
            destination_tab = _bulk_withdrawal_destination_tab(
                body.get("destinationTab")
            )
        expected_direction = "deposit" if direction == "stash-to-vault" else "withdrawal"

        with SAVE_WRITE_LOCK:
            store = vault_store()
            try:
                prior = store.get_transfer_batch(request_id)
            except VaultNotFoundError:
                prior = None
            if prior is not None:
                if prior.direction != expected_direction:
                    raise VaultConflictError(
                        "requestId was already used for the opposite bulk direction"
                    )
                if (
                    direction == "stash-to-vault"
                    and prior.collection_id != collection_id
                ):
                    raise VaultConflictError(
                        "requestId was already used for a different Vault collection"
                    )
                prior_preview_token = _bulk_preview_token(
                    direction, prior.stash_before_sha256, prior.request_hash,
                    prior.item_count,
                    prior.collection_id if direction == "stash-to-vault" else None,
                )
                if preview_token.lower() != prior_preview_token:
                    return {
                        "err": "This requestId belongs to a different bulk preview; nothing was changed.",
                        "code": "preview_stale", "terminal": True,
                        "retryable": False,
                    }
                stash_path = SAVES / "stash.hss"
                with _exclusive_save_file(stash_path):
                    peer_error = _active_peer_editor_error()
                    if peer_error:
                        return {"err": peer_error, "code": "peer_editor"}
                    if game_running():
                        return {
                            "err": "Game started while the bulk retry was waiting. Close it and retry.",
                            "code": "game_running", "retryable": True,
                        }
                    if prior.status in {"prepared", "conflict"}:
                        _reconcile_vault_transfers_locked(stash_path)
                        prior = store.get_transfer_batch(request_id)
                return _bulk_operation_result(
                    prior, already=prior.status == "committed"
                )

            recovery = reconcile_vault_transfers()
            if recovery.get("deferred"):
                return {"err": "Game started while preparing the bulk transfer.", "code": "game_running"}
            if recovery.get("err"):
                return {"err": recovery["err"], "code": "recovery_failed"}
            if recovery.get("pending") or recovery.get("conflicts"):
                return {
                    "err": "A previous Infinite Vault transfer needs recovery first.",
                    "code": "recovery_required",
                }

            stash_path = SAVES / "stash.hss"
            with _exclusive_save_file(stash_path):
                peer_error = _active_peer_editor_error()
                if peer_error:
                    return {"err": peer_error, "code": "peer_editor"}
                if game_running():
                    return {"err": "Game started while the bulk transfer was waiting.", "code": "game_running"}
                plan = _bulk_plan_locked(
                    direction, collection_id, source_tab, destination_tab
                )
                if plan["previewToken"] != preview_token.lower():
                    return {
                        "err": "The stash or Vault changed after preview. Review the new plan; nothing was moved.",
                        "code": "preview_stale", "terminal": True,
                        "retryable": False,
                    }
                if not plan["itemCount"]:
                    return {
                        "ok": "There are no items to move.", "code": "empty",
                        "status": "noop", "terminal": True, "retryable": False,
                        "movedCount": 0,
                    }
                before_hash = plan["stashSha256"]
                after_hash = hashlib.sha256(
                    plan["encodedAfter"].encode("ascii")
                ).hexdigest()
                if direction == "stash-to-vault":
                    batch = store.prepare_bulk_deposit(
                        collection_id, plan["entries"], request_id=request_id,
                        request_hash=plan["intentHash"],
                        stash_before_sha256=before_hash,
                        stash_after_sha256=after_hash,
                    )
                else:
                    batch = store.prepare_bulk_withdrawal(
                        plan["targets"], request_id=request_id,
                        request_hash=plan["intentHash"],
                        stash_before_sha256=before_hash,
                        stash_after_sha256=after_hash,
                        destination_tab=(
                            None
                            if plan.get("destinationTab") == BULK_WITHDRAWAL_AUTO
                            else plan.get("destinationTab")
                        ),
                    )
                if batch.status != "prepared":
                    return _bulk_operation_result(batch)

                def safely_cancel_or_commit():
                    try:
                        return store.reconcile_transfer_batch(
                            request_id, _file_sha256(stash_path)
                        )
                    except VaultError:
                        return store.get_transfer_batch(request_id)

                peer_error = _active_peer_editor_error()
                if peer_error:
                    return _bulk_operation_result(safely_cancel_or_commit())
                if game_running():
                    return _bulk_operation_result(safely_cancel_or_commit())
                if _file_sha256(stash_path) != before_hash:
                    return _bulk_operation_result(safely_cancel_or_commit())
                try:
                    backup_name = backup(stash_path)
                    if game_running():
                        return _bulk_operation_result(
                            safely_cancel_or_commit(), backup_name=backup_name
                        )
                    peer_error = _active_peer_editor_error()
                    if peer_error:
                        return _bulk_operation_result(
                            safely_cancel_or_commit(), backup_name=backup_name
                        )
                    if _file_sha256(stash_path) != before_hash:
                        return _bulk_operation_result(
                            safely_cancel_or_commit(), backup_name=backup_name
                        )
                    atomic_write_text(stash_path, plan["encodedAfter"], "ascii")
                except Exception:
                    safely_cancel_or_commit()
                    raise
                committed = store.commit_transfer_batch(
                    request_id, _file_sha256(stash_path)
                )
                result = _bulk_operation_result(
                    committed, backup_name=backup_name
                )
                result["tabCounts"] = plan["tabCounts"]
                result["customNamedCount"] = plan.get("customNamedCount", 0)
                return result
    except VaultError as exc:
        return {"err": str(exc), "code": "vault_error", "terminal": True}
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {
            "err": f"Infinite Vault bulk transfer failed: {exc}",
            "code": "bulk_failed", "terminal": True,
        }


def file_key(ref: dict):
    t = ref["type"]
    if t == "stash":
        return ("stash",)
    if t == "bag":
        return ("bag", int(ref["slot"]))
    return ("char", int(ref["slot"]))


class FileCtx:
    """Let several references to the same file share a single load."""

    def __init__(self):
        self.loaded = {}

    def items(self, ref: dict) -> dict:
        fk = file_key(ref)
        if fk not in self.loaded:
            if fk[0] == "stash":
                self.loaded[fk] = json.loads(decode_hss(SAVES / "stash.hss"))
            elif fk[0] == "bag":
                p = SAVES / f"inventory_order_{fk[1]}.hss"
                self.loaded[fk] = json.loads(decode_hss(p)) if p.exists() and p.stat().st_size > 50 else {}
            else:
                txt = decode_hss(SAVES / f"herosiege{fk[1]}.hss")
                m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
                if not m:
                    raise ValueError(f"inventory field not found in herosiege{fk[1]}.hss")
                self.loaded[fk] = json.loads(base64.b64decode(m.group(1)))
        d = self.loaded[fk]
        tab = ref.get("tab") or ref["type"]
        return d.setdefault(tab, {})

    def save_all(self, destination_first=None) -> list:
        """Commit a multi-file edit without a source-first loss window.

        All potentially slow backups complete before the final runtime safety
        check.  Destination-first replacement means a process/power failure
        between two atomic replaces can leave a duplicate, never delete the
        only active copy of a moved item.
        """
        keys = list(self.loaded)
        if destination_first in self.loaded:
            keys.remove(destination_first)
            keys.insert(0, destination_first)
        plans = []
        for fk in keys:
            d = self.loaded[fk]
            if fk[0] == "stash":
                plans.append((SAVES / "stash.hss", _encoded_stash_document(d)))
            elif fk[0] == "bag":
                plans.append((
                    SAVES / f"inventory_order_{fk[1]}.hss",
                    encode_hss(json.dumps(d, separators=(", ", ": "))),
                ))
            else:
                path, encoded = _encoded_char_inventory_document(fk[1], d)
                if encoded is not None:
                    plans.append((path, encoded))
        baks = [backup(path) for path, _encoded in plans]
        _runtime_save_barrier()
        for path, encoded in plans:
            atomic_write_text(path, encoded, "ascii")
        return baks


EQUIP_ACCEPT = {
    0: {0}, 1: {1}, 2: {2}, 3: {3}, 16: {3}, 4: {4}, 5: {5},
    6: {3, 6}, 17: {3, 6}, 7: {7}, 9: {7}, 8: {8},
    10: {16}, 11: {16}, 12: {16}, 13: {16}, 14: {16},
}


def op_move(body: dict) -> dict:
    if game_running():
        return {"err": "Game is running! Close it first."}
    frm, to, key = body["from"], body["to"], body["key"]
    pos = body.get("pos") or [0, 0]
    ctx = FileCtx()
    src = ctx.items(frm)
    if key not in src:
        return {"err": "item to move not found"}
    entry = src[key]
    it = resolve(key, entry.get("data", {}))

    # hedef: ekipman slotu (giydir)
    if to["type"] == "equip":
        g = int(to["g"])
        if it.get("cls") not in EQUIP_ACCEPT.get(g, set()):
            return {"err": f"{it['name']} does not fit {SLOT_NAMES.get(g, g)}"}
        eq = ctx.items({"type": "equipped", "slot": int(to["slot"]), "tab": "equipped_items"})
        if any(int(v.get("data", {}).get("g", -1)) == g for k, v in eq.items() if k != key):
            return {"err": f"{SLOT_NAMES.get(g, g)} slot is occupied - unequip first"}
        del src[key]
        d0 = entry.setdefault("data", {})
        d0["g"] = float(g)
        d0["w"] = 1.0
        d0.setdefault("d", 0.0)
        d0.setdefault("n", 0.0)
        d0.setdefault("e", 0.0)
        d0.pop("m", None)
        entry.pop("pos", None)
        if key in eq:
            key = fresh_key(int(key.rsplit("-", 1)[1]), eq)
        eq[key] = entry
        baks = ctx.save_all(destination_first=file_key({
            "type": "equipped", "slot": int(to["slot"])
        }))
        return {"ok": f"{it['name']} equipped -> {SLOT_NAMES.get(g, g)}", "backup": ", ".join(baks)}

    dst = ctx.items(to)
    dst_tab = to.get("tab") or to["type"]
    same = src is dst
    if not pos_free(dst, dst_tab, pos, it["w"], it["h"], skip_key=key if same else None):
        return {"err": "cell occupied or does not fit"}
    if not same:
        del src[key]
        if frm["type"] == "equipped":  # ekipmandan cikariliyor
            entry.get("data", {}).pop("g", None)
            entry.get("data", {}).pop("t", None)
        if key in dst:
            key = fresh_key(int(key.rsplit("-", 1)[1]), dst)
    entry["pos"] = [float(int(pos[0])), float(int(pos[1]))]
    dst[key] = entry
    baks = ctx.save_all(destination_first=file_key(to))
    return {"ok": f"{it['name']} moved -> [{int(pos[0])},{int(pos[1])}]", "backup": ", ".join(baks)}


def op_add(body: dict) -> dict:
    r = CAT[int(body["cid"])]
    tgt = body["target"]          # {"type":"stash_unique"|"stash"|"bag"|"char_*", ...}
    if game_running():
        return {"err": "Game is running! Close it first."}
    if r.get("kind") == "runeword" or r.get("cls", 0) < 0:
        return {"err": "Runewords can't be added directly - use the Runeword Builder."}
    if not r.get("available", True):
        return {"err": "This catalog address is not verified for Season 10."}
    skill_id = body.get("targetId", body.get("skillId"))
    profile_id = catalog_skill_profile_id(r)
    target_database = (
        skill_target_database(profile_id) if profile_id is not None else None
    )
    selector = (
        target_database.selector(profile_id)
        if target_database is not None else None
    )
    if profile_id is not None and skill_id is None:
        noun = "class" if selector and selector.get("targetKind") == "class" else "skill"
        return {"err": f"{r['name']}: choose a verified {noun} target before adding; item unchanged"}
    selected_skill = None
    try:
        generation_roll_profile_for_request(r, skill_id)
        if skill_id is not None:
            selected_skill = skill_target_for_row(r, skill_id)
    except (DiceSkillValidationError, TorchClassValidationError, ValueError) as exc:
        return {"err": f"{r['name']}: {exc}; item unchanged"}
    if selected_skill and selector and selector.get("targetKind") == "class":
        skill_suffix = (
            f" · All Skills class: {selected_skill['name']} "
            f"(Class ID {selected_skill['id']})"
        )
    elif selected_skill:
        skill_suffix = (
            f" · {selected_skill['className']}: {selected_skill['name']} "
            f"(ID {selected_skill['id']})"
        )
    else:
        skill_suffix = ""
    if tgt["type"] == "stash_unique":
        if r["kind"] != "unique":
            return {"err": "Only unique items can go to the Unique tab."}
        d = json.loads(decode_hss(SAVES / "stash.hss"))
        ui = d.setdefault("unique_items", {})
        key = fresh_key(r["cls"], ui)
        ui[key] = {"data": make_data(r, skill_id=skill_id)}
        bk = write_stash(d)
        return {"ok": f"{r['name']} -> Unique tab{skill_suffix}", "backup": bk}
    if tgt["type"] == "stash":
        d = json.loads(decode_hss(SAVES / "stash.hss"))
        tab = tgt["tab"]
        items = d.setdefault(tab, {})
        if tgt.get("pos") is not None:
            pos = [float(int(tgt["pos"][0])), float(int(tgt["pos"][1]))]
            if not pos_free(items, tab, pos, r["w"], r["h"]):
                return {"err": "cell occupied or does not fit"}
        else:
            pos = find_free_pos(items, tab, r["w"], r["h"])
            if pos is None:
                return {"err": "No free space in this tab."}
        key = fresh_key(r["cls"], items)
        items[key] = {"pos": pos, "data": make_data(r, skill_id=skill_id)}
        bk = write_stash(d)
        return {"ok": f"{r['name']} -> {tab} pos {pos}{skill_suffix}", "backup": bk}
    if tgt["type"] == "bag":
        slot, tab = int(tgt["slot"]), tgt["tab"]
        p = SAVES / f"inventory_order_{slot}.hss"
        d = json.loads(decode_hss(p)) if p.exists() and p.stat().st_size > 50 else {}
        items = d.setdefault(tab, {})
        if tgt.get("pos") is not None:
            pos = [float(int(tgt["pos"][0])), float(int(tgt["pos"][1]))]
            if not pos_free(items, tab, pos, r["w"], r["h"]):
                return {"err": "cell occupied or does not fit"}
        else:
            pos = find_free_pos(items, tab, r["w"], r["h"])
            if pos is None:
                return {"err": "No free space in the bag."}
        key = fresh_key(r["cls"], items)
        items[key] = {"pos": pos, "data": make_data(r, skill_id=skill_id)}
        bk = write_bags(slot, d)
        return {"ok": f"{r['name']} -> canta {tab}{skill_suffix}", "backup": bk}
    if tgt["type"] in ("potions", "personal_stash"):
        slot = int(tgt["slot"])
        p = SAVES / f"herosiege{slot}.hss"
        txt = decode_hss(p)
        match = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
        if not match:
            return {"err": f"inventory field not found in herosiege{slot}.hss"}
        inv = json.loads(base64.b64decode(match.group(1)))
        tab = tgt["type"]
        items = inv.setdefault(tab, {})
        if tgt.get("pos") is not None:
            pos = [float(int(tgt["pos"][0])), float(int(tgt["pos"][1]))]
            if not pos_free(items, tab, pos, r["w"], r["h"]):
                return {"err": "cell occupied or does not fit"}
        else:
            pos = find_free_pos(items, tab, r["w"], r["h"])
            if pos is None:
                return {"err": f"No free space in {tab}."}
        key = fresh_key(r["cls"], items)
        items[key] = {"pos": pos, "data": make_data(r, skill_id=skill_id)}
        bk = write_char_inventory(slot, inv)
        return {"ok": f"{r['name']} -> {tab}{skill_suffix}", "backup": bk}
    if tgt["type"] == "equip":
        slot, g = int(tgt["slot"]), int(tgt["g"])
        if r.get("cls") not in EQUIP_ACCEPT.get(g, set()):
            return {"err": f"{r['name']} does not fit {SLOT_NAMES.get(g, g)}"}
        p = SAVES / f"herosiege{slot}.hss"
        txt = decode_hss(p)
        m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
        inv = json.loads(base64.b64decode(m.group(1))) if m else {}
        eq = inv.setdefault("equipped_items", {})
        # remove whatever is already in that slot
        for k in [k for k, v in eq.items() if int(v["data"].get("g", -1)) == g]:
            del eq[k]
        key = fresh_key(r["cls"], eq)
        eq[key] = {"data": make_data(r, equipped_g=g, skill_id=skill_id)}
        bk = write_char_inventory(slot, inv)
        return {"ok": (f"{r['name']} -> {SLOT_NAMES.get(g, g)} "
                       f"(character {slot}){skill_suffix}"), "backup": bk}
    return {"err": "unknown target"}


def op_addmany(body: dict) -> dict:
    """Add several uniques to the Unique tab in a single write (for sets)."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    cids = body.get("cids") or []
    rows = []
    for cid in cids:
        r = CAT[int(cid)]
        if r["kind"] != "unique" or not r.get("available", True):
            continue
        try:
            generation_roll_profile(r)
        except ValueError as exc:
            return {"err": f"{exc}; no items added"}
        profile_id = catalog_skill_profile_id(r)
        if profile_id is not None:
            database = skill_target_database(profile_id)
            selector = database.selector(profile_id) if database is not None else None
            noun = "class" if selector and selector.get("targetKind") == "class" else "skill"
            return {
                "err": (
                    f"{r['name']}: bulk add has no explicit {noun} target; "
                    "no items added"
                )
            }
        rows.append(r)
    d = json.loads(decode_hss(SAVES / "stash.hss"))
    ui = d.setdefault("unique_items", {})
    added = []
    for r in rows:
        key = fresh_key(r["cls"], ui)
        ui[key] = {"data": make_data(r)}
        added.append(r["name"])
    if not added:
        return {"err": "nothing to add"}
    bk = write_stash(d)
    return {"ok": f"added {len(added)}: " + ", ".join(added), "backup": bk}


def op_modify(body: dict) -> dict:
    """Validated operations on one existing item."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    action = body["action"]
    tgt = body["target"]
    key = body["key"]
    ctx = FileCtx()
    items = ctx.items(tgt)
    if key not in items:
        return {"err": "item not found"}
    entry = items[key]
    it = resolve(key, entry.get("data", {}))
    if action == "selectskill":
        selector = it.get("skillSelector")
        if not isinstance(selector, dict) or not selector.get("profileId"):
            return {"err": f"{it['name']}: this item has no selectable skill or class; item unchanged"}
        profile_id = str(selector["profileId"])
        target_database = skill_target_database(profile_id)
        if target_database is None:
            return {"err": f"{it['name']}: unknown selector profile; item unchanged"}
        try:
            target = target_database.target(
                profile_id, body.get("targetId", body.get("skillId"))
            )
        except (DiceSkillValidationError, TorchClassValidationError) as exc:
            return {"err": f"{it['name']}: {exc}; item unchanged"}
        data = entry.setdefault("data", {})
        current = target_database.selector(profile_id, data.get("a"))
        current_skill = current.get("current") if isinstance(current, dict) else None
        same_target = (
            isinstance(current_skill, dict)
            and current_skill.get("id") == target["id"]
        )
        if selector.get("targetKind") == "class":
            try:
                same_target = same_target and float(data.get("a")) == float(target["seed"])
            except (TypeError, ValueError):
                same_target = False
        if same_target:
            if selector.get("targetKind") == "class":
                current_label = f"All Skills class {target['name']} (Class ID {target['id']})"
            else:
                current_label = (
                    f"{target['className']}: {target['name']} (ID {target['id']})"
                )
            return {
                "ok": f"{it['name']}: already targets {current_label}",
                "backup": "",
            }
        # The runtime recreates the selected identity from ``a``.  Never inject
        # stat 202/203/419/420 or touch i/s/zz/socket payloads.
        data["a"] = float(target["seed"])
        baks = ctx.save_all()
        if selector.get("targetKind") == "class":
            changed_label = (
                f"All Skills class -> {target['name']} "
                f"(Class ID {target['id']}; a={target['seed']}; 4/4 stats MAX)"
            )
        else:
            noun = "subskill" if selector.get("targetKind") == "subskill" else "skill"
            changed_label = (
                f"{noun} -> {target['className']}: {target['name']} "
                f"(ID {target['id']}; a={target['seed']})"
            )
        return {
            "ok": f"{it['name']}: {changed_label}",
            "backup": ", ".join(baks),
        }
    if action == "reroll":
        d0 = entry.setdefault("data", {})
        d0["a"] = random_item_seed()
        if "i" in d0: d0["i"] = random_item_seed()
        if "s" in d0: d0["s"] = random_item_seed()
        baks = ctx.save_all()
        return {"ok": f"{it['name']}: stats rerolled (new seeds)", "backup": ", ".join(baks)}
    if action == "perfect":
        if it.get("skillSelector"):
            selector_kind = it["skillSelector"].get("targetKind")
            noun = "class" if selector_kind == "class" else "skill"
            return {"err": (f"{it['name']}: its variable range selects a {noun} identity; "
                            f"use Choose {noun} instead")}
        if not ROLL_DB.available:
            return {
                "err": (
                    f"{it['name']}: {roll_database_message()}; item unchanged"
                )
            }
        profile = it.get("rollProfile")
        if not profile:
            return {"err": (f"{it['name']}: no verified profile for this exact "
                            "item address; item unchanged")}
        if profile.get("mode") == "fixed":
            return {"err": (f"{it['name']}: definition stats are fixed; "
                            "there is no variable roll to change")}
        field_seeds = effective_roll_field_seeds(profile)
        if not field_seeds:
            return {"err": f"{it['name']}: verified profile has no actionable seed fields; item unchanged"}
        # Each listed field owns an independently proven CPR chain. Filled
        # socket payloads and unrelated metadata are preserved byte-for-byte.
        data = entry.setdefault("data", {})
        # Save field s activates a separate loader chain. Preserve the native
        # shape of existing items: optimize it only when it already exists.
        applicable_field_seeds = {
            field: seed for field, seed in field_seeds.items()
            if field != "s" or "s" in data
        }
        if not applicable_field_seeds:
            return {"err": (f"{it['name']}: verified socket seed is not active "
                            "because this item has no s field; item unchanged")}
        max_sockets = roll_profile_max_sockets(profile)
        if max_sockets is not None:
            excess_socket_fields = [
                f"s{index}"
                for index in range(max_sockets + 1, 7)
                if f"s{index}" in data
            ]
            if excess_socket_fields:
                labels = ", ".join(excess_socket_fields)
                return {
                    "err": (
                        f"{it['name']}: socket payload {labels} exceeds the "
                        f"verified maximum of {max_sockets}; run Save Health "
                        "Check and repair the socket layout first; item unchanged"
                    )
                }
        existing_zz = data.get("zz")
        if max_sockets is not None and existing_zz is not None and not isinstance(existing_zz, dict):
            return {"err": (f"{it['name']}: socket metadata is malformed; "
                            "item unchanged")}
        socket_capacity_applied = (
            max_sockets is None
            or (
                isinstance(existing_zz, dict)
                and isinstance(existing_zz.get("sockets"), (int, float))
                and not isinstance(existing_zz.get("sockets"), bool)
                and float(existing_zz["sockets"]) == float(max_sockets)
            )
        )
        mode = effective_roll_label(profile)
        detail = effective_roll_detail(profile)
        already_applied = socket_capacity_applied and all(
            isinstance(data.get(field), (int, float))
            and not isinstance(data.get(field), bool)
            and float(data[field]) == seed
            for field, seed in applicable_field_seeds.items()
        )
        seed_detail = ", ".join(
            f"{field}={int(seed)}" for field, seed in applicable_field_seeds.items()
        )
        if max_sockets is not None:
            seed_detail += f", zz.sockets={max_sockets}"
        if already_applied:
            return {"ok": f"{it['name']}: already {mode} ({detail})",
                    "backup": ""}
        data.update(applicable_field_seeds)
        if max_sockets is not None:
            zz = data.setdefault("zz", {})
            zz["sockets"] = float(max_sockets)
        baks = ctx.save_all()
        return {"ok": (f"{it['name']}: {mode} applied ({detail}; "
                       f"{seed_detail})"),
                "backup": ", ".join(baks)}
    if action == "setstack":
        n = max(1, min(99_999_999, int(body.get("count", 1))))
        entry.setdefault("data", {})["o"] = float(n)
        baks = ctx.save_all()
        return {"ok": f"{it['name']}: stack = {n}", "backup": ", ".join(baks)}
    if action == "duplicate":
        import copy
        clone = copy.deepcopy(entry)
        tab = tgt.get("tab") or tgt["type"]
        if "pos" in entry:
            pos = find_free_pos(items, tab, it["w"], it["h"])
            if pos is None:
                return {"err": "no free space for the copy"}
            clone["pos"] = pos
        nk = fresh_key(int(key.rsplit("-", 1)[1]), items)
        items[nk] = clone
        baks = ctx.save_all()
        return {"ok": f"{it['name']}: duplicated", "backup": ", ".join(baks)}
    return {"err": "unknown action"}


def op_forge(body: dict) -> dict:
    """Serialize forge writes so concurrent retries observe the persisted key."""
    with FORGE_LOCK:
        return _op_forge(body)


def _op_forge(body: dict) -> dict:
    """Forge a runeword. Codex type -> forged codex; equipment type -> a normal
    base with the recipe's runes socketed (the game identifies it from the socket
    runes, D2 style)."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    raw_rwid = body.get("rw")
    if isinstance(raw_rwid, bool):
        return {"err": "invalid runeword selection"}
    try:
        rwid = int(raw_rwid)
    except (TypeError, ValueError):
        return {"err": "invalid runeword selection"}
    tab = body.get("tab") or "stash_tab_1"
    if not isinstance(tab, str) or re.fullmatch(r"stash_tab_[1-9]", tab) is None:
        return {"err": "invalid forge target; choose stash tab 1-9"}
    rec = next((x for x in RUNEWORDS if x["rw"] == rwid), None)
    if not rec or not rec.get("base"):
        return {"err": "unknown runeword / no valid base"}
    blocker = runeword_generation_blocker(rec)
    if blocker:
        return {"err": f"{rec['name']}: {blocker}; item unchanged"}
    try:
        candidates = runeword_base_candidates(rec)
    except ValueError as exc:
        return {"err": f"invalid runeword target: {exc}"}
    if not candidates:
        return {"err": "runeword has no runtime-available compatible base"}
    requested_cid = body.get("baseCid")
    if requested_cid is None:
        reference = rec["base"]
        base = next((
            row for row in candidates
            if int(row["cls"]) == int(reference.get("cls", rec["type"]))
            and int(row.get("sub", 0)) == int(reference.get("sub", 0))
            and int(row["b"]) == int(reference.get("b", -1))
        ), None)
    else:
        if isinstance(requested_cid, bool):
            return {"err": "invalid runeword base selection"}
        try:
            requested_cid = int(requested_cid)
        except (TypeError, ValueError):
            return {"err": "invalid runeword base selection"}
        base = next((row for row in candidates if int(row["id"]) == requested_cid), None)
    if base is None:
        return {"err": "selected base is not valid for this runeword"}
    profile = runeword_profile(rec, base)
    if profile is None:
        return {"err": (
            f"{rec['name']}: {runeword_profile_unavailable_reason()}; "
            "item unchanged"
        )}
    try:
        request_key = forge_request_item_key(body.get("requestId"), rec, base, tab)
    except (KeyError, TypeError, ValueError) as exc:
        return {"err": str(exc)}
    d = json.loads(decode_hss(SAVES / "stash.hss"))
    items = d.get(tab)
    if items is None:
        items = {}
        d[tab] = items
    elif not isinstance(items, dict):
        return {"err": f"invalid stash section: {tab}; item unchanged"}
    if request_key is not None and request_key in items:
        if forged_entry_matches(items[request_key], rec, base):
            return {
                "ok": f"ALREADY FORGED: {rec['name']} -> {tab}",
                "backup": "",
            }
        return {"err": "forge request id collision; item unchanged"}
    pos = find_free_pos(items, tab, base.get("w", 2), base.get("h", 2))
    if pos is None:
        return {"err": f"no free space in {tab}"}

    def rune_b64(rb):
        rj = json.dumps({"a": int(random_item_seed()), "b": int(rb), "n": 0},
                        separators=(",", ":"))
        return base64.b64encode(rj.encode()).decode()

    # Equipment: normal base + the recipe's runes (real Scholar/Skysong/
    # Grimwalkers examples). Zone Codex recipes returned fail-closed above.
    roll_seeds = preferred_runeword_seeds(rec, base, profile)
    data = {"a": roll_seeds["a"],
            "i": roll_seeds["i"],
            "b": float(base["b"]), "c": 0.0, "w": 1.0,
            "j": float(base["sub"] if base["cls"] == 3 else 0),
            "d": 0.0, "e": 0.0, "n": 0.0,
            "zz": {"sockets": float(len(rec["runes"]))}}
    for n, rn in enumerate(rec["runes"], 1):
        data[f"s{n}"] = rune_b64(rn["b"])

    key = request_key or fresh_key(base["cls"], items)
    items[key] = {"pos": pos, "data": data}
    bk = write_stash(d)
    return {"ok": f"FORGED: {rec['name']} ({rec['target']}) -> {tab} | runes: " +
                  ", ".join(r["name"] for r in rec["runes"]),
            "backup": bk}


LOADOUTS_FILE = ROOT / "hs_loadouts.json"
BUILD_EXPORT_DIR = Path.home() / "Downloads" / "HeroSiegeBuilds"


def load_loadouts() -> dict:
    if LOADOUTS_FILE.exists():
        try:
            return json.loads(LOADOUTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_loadouts(d: dict):
    LOADOUTS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _safe_build_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._")
    return cleaned[:80] or "HeroSiegeBuild"


def _loadout_item_view(item: dict, index: int) -> dict:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    cls = int(item.get("cls", 0))
    resolved = resolve(f"0-0-{index}-{cls}", data)
    cid = resolved.get("rwcid") if resolved.get("isRW") else resolved.get("cid")
    row = CAT[cid] if isinstance(cid, int) and 0 <= cid < len(CAT) else {}
    stats = []
    for stat in row.get("stats", []):
        if isinstance(stat, (list, tuple)) and stat:
            label = str(stat[0])
            value = str(stat[1]) if len(stat) > 1 and stat[1] not in (None, "") else ""
            stats.append({"label": label, "value": value})
    base_id = data.get("b")
    base_text = "-" if base_id is None else str(int(base_id))
    return {
        "name": resolved.get("name") or item.get("name") or "Unknown item",
        "rarity": resolved.get("rar") or row.get("rar") or "Unknown",
        "slot": SLOT_NAMES.get(int(item.get("g", data.get("g", -1))), "Equipment"),
        "class": resolved.get("clsName") or CLASS_NAMES.get(cls, f"Class {cls}"),
        "spr": resolved.get("spr") or item.get("spr"),
        "address": f"c{int(data.get('c', 0))} / class {cls} / sub {int(data.get('j', 0))} / b{base_text}",
        "stats": stats,
    }


def _icon_data_uri(sprite) -> str:
    if not sprite:
        return ""
    path = ICONS / f"{sprite}.png"
    if not path.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def export_loadout(name: str, loadout: dict) -> dict:
    """Write a portable editor JSON and a self-contained human-readable HTML."""
    if not (isinstance(loadout, dict) and isinstance(loadout.get("items"), list)):
        return {"err": "invalid loadout"}
    BUILD_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base_name = f"{_safe_build_filename(name)}_{stamp}"
    json_path = BUILD_EXPORT_DIR / f"{base_name}.hsbuild.json"
    html_path = BUILD_EXPORT_DIR / f"{base_name}.html"
    portable = {
        "format": "hero-siege-item-editor-build",
        "formatVersion": 1,
        "appVersion": APP_VERSION,
        "name": name,
        "created": loadout.get("created", "unknown"),
        "exported": datetime.now().isoformat(timespec="seconds"),
        "items": loadout["items"],
    }
    atomic_write_text(json_path, json.dumps(portable, ensure_ascii=False, indent=2), "utf-8")

    cards = []
    for index, item in enumerate(loadout["items"]):
        view = _loadout_item_view(item, index)
        icon = _icon_data_uri(view["spr"])
        stat_html = "".join(
            f'<li><span>{html_lib.escape(stat["label"])}</span><b>{html_lib.escape(stat["value"])}</b></li>'
            for stat in view["stats"]
        ) or '<li class="empty">No catalog stats recorded</li>'
        icon_html = (f'<img src="{icon}" alt="">' if icon else '<div class="noicon">HS</div>')
        cards.append(
            f'<article class="item"><header>{icon_html}<div><small>{html_lib.escape(view["slot"])}</small>'
            f'<h2>{html_lib.escape(view["name"])}</h2><em class="r-{html_lib.escape(view["rarity"])}">'
            f'{html_lib.escape(view["rarity"])} · {html_lib.escape(view["class"])}</em></div></header>'
            f'<ul>{stat_html}</ul><footer>{html_lib.escape(view["address"])}</footer></article>'
        )
    safe_name = html_lib.escape(name)
    safe_created = html_lib.escape(str(loadout.get("created", "unknown")))
    html_doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_name} — Hero Siege Build</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#080d15;color:#e7edf5;font:14px/1.45 Inter,Segoe UI,sans-serif}}main{{max-width:1180px;margin:auto;padding:38px 24px 70px}}.hero{{padding:25px;border:1px solid #8a642d;border-radius:16px;background:linear-gradient(135deg,#1a2636,#101720);box-shadow:0 18px 45px #0007}}h1{{margin:0;color:#ffd98d;font-size:30px}}.hero p{{margin:6px 0 0;color:#8392a7}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin-top:20px}}.item{{overflow:hidden;border:1px solid #304159;border-radius:12px;background:linear-gradient(145deg,#151f2c,#0d141e)}}header{{display:flex;gap:13px;align-items:center;padding:15px;border-bottom:1px solid #29384c}}header img,.noicon{{width:50px;height:50px;object-fit:contain;border:1px solid #3a4d66;border-radius:9px;background:#0a1019;image-rendering:pixelated}}.noicon{{display:grid;place-items:center;color:#607087;font-weight:900}}small{{color:#75859b;text-transform:uppercase;letter-spacing:1px}}h2{{margin:2px 0;font-size:17px}}em{{font-style:normal;font-size:11px;font-weight:800}}.r-Heroic{{color:#50ddba}}.r-Satanic{{color:#ff6268}}.r-Angelic{{color:#ffe18d}}.r-Unholy{{color:#c28cff}}.r-Runeword{{color:#9daeff}}ul{{list-style:none;margin:0;padding:10px 15px}}li{{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid #ffffff0b;color:#9eabbc}}li b{{color:#dce6f1;text-align:right}}li.empty{{color:#65758a}}footer{{padding:9px 15px;background:#090f18;color:#526176;font:10px Consolas,monospace}}.credit{{margin-top:20px;color:#5f6e82;font-size:11px}}</style></head>
<body><main><section class="hero"><h1>{safe_name}</h1><p>Hero Siege build · {len(cards)} equipped items · saved {safe_created} · exported by Item Editor {APP_VERSION}</p></section><section class="grid">{''.join(cards)}</section><p class="credit">Offline build report. The accompanying .hsbuild.json file can be imported back into Hero Siege Item Editor.</p></main></body></html>"""
    atomic_write_text(html_path, html_doc, "utf-8")
    return {
        "ok": f"Build exported to Downloads/HeroSiegeBuilds: {json_path.name} + {html_path.name}",
        "json": str(json_path),
        "html": str(html_path),
    }


def op_loadout(body: dict) -> dict:
    act = body["action"]
    store = load_loadouts()
    if act == "save":
        slot = int(body["slot"])
        name = (body.get("name") or "").strip()
        if not name:
            return {"err": "loadout needs a name"}
        txt = decode_hss(SAVES / f"herosiege{slot}.hss")
        m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
        inv = json.loads(base64.b64decode(m.group(1))) if m else {}
        eq = inv.get("equipped_items", {})
        if not eq:
            return {"err": "character has no equipped items"}
        items = []
        for k, v in eq.items():
            it = resolve(k, v.get("data", {}))
            items.append({"cls": int(k.rsplit("-", 1)[1]), "data": v.get("data", {}),
                          "name": it["name"], "spr": it.get("spr"), "g": int(v.get("data", {}).get("g", -1))})
        store[name] = {"created": time.strftime("%Y-%m-%d %H:%M"), "items": items}
        save_loadouts(store)
        return {"ok": f"loadout '{name}' saved ({len(items)} items)"}
    if act == "apply":
        if game_running():
            return {"err": "Game is running! Close it first."}
        slot = int(body["slot"])
        name = body.get("name")
        lo = store.get(name)
        if not lo:
            return {"err": "loadout not found"}
        pth = SAVES / f"herosiege{slot}.hss"
        txt = decode_hss(pth)
        m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
        inv = json.loads(base64.b64decode(m.group(1))) if m else {}
        import copy
        neweq = {}
        for it in lo["items"]:
            key = fresh_key(int(it.get("cls", 0)), neweq)
            neweq[key] = {"data": copy.deepcopy(it["data"])}
        inv["equipped_items"] = neweq
        bk = write_char_inventory(slot, inv)
        return {"ok": f"loadout '{name}' applied to slot {slot} ({len(neweq)} items)", "backup": bk}
    if act == "delete":
        name = body.get("name")
        if name in store:
            del store[name]
            save_loadouts(store)
            return {"ok": f"loadout '{name}' deleted"}
        return {"err": "loadout not found"}
    if act == "export":
        name = body.get("name")
        loadout = store.get(name)
        if not loadout:
            return {"err": "loadout not found"}
        return export_loadout(name, loadout)
    if act == "import":
        lo = body.get("loadout")
        name = (body.get("name") or "").strip()
        if not (isinstance(lo, dict) and isinstance(lo.get("items"), list) and name):
            return {"err": "invalid loadout file"}
        for it in lo["items"]:
            if not isinstance(it.get("data"), dict):
                return {"err": "invalid loadout items"}
        store[name] = {"created": lo.get("created", "imported"), "items": lo["items"]}
        save_loadouts(store)
        return {"ok": f"loadout '{name}' imported ({len(lo['items'])} items)"}
    return {"err": "unknown loadout action"}


BAK_PAT = re.compile(
    r"^(?P<target>(?:stash|herosiege\d+|inventory_order_\d+)\.hss)\."
    r"(?P<kind>guibak|itemed_bak|pre_recovery)_"
    r"(?P<ts>\d{8}_\d{6})(?:_(?P<micro>\d{6}))?$"
)


def _backup_name_match(file_name: object):
    if not isinstance(file_name, str) or Path(file_name).name != file_name:
        return None
    match = BAK_PAT.fullmatch(file_name)
    if match and match.group("kind") == "pre_recovery" and match.group("target") != "stash.hss":
        return None
    return match


def list_backups() -> list:
    out = []
    for f in SAVES.iterdir():
        m = _backup_name_match(f.name)
        if m and f.is_file() and not f.is_symlink():
            out.append({"file": f.name, "target": m.group("target"), "ts": m.group("ts"),
                        "micro": m.group("micro") or "", "kind": m.group("kind"),
                        "size": f.stat().st_size})
    out.sort(key=lambda x: (x["ts"], x["micro"]), reverse=True)
    return out[:200]


def op_restore_backup(body: dict) -> dict:
    if game_running():
        return {"err": "Game is running! Close it first."}
    if not isinstance(body, dict):
        return {"err": "backup not found"}
    fn = body.get("file", "")
    m = _backup_name_match(fn)
    if not m:
        return {"err": "backup not found"}
    src = SAVES / fn
    if not src.is_file() or src.is_symlink():
        return {"err": "backup not found"}
    target = SAVES / m.group("target")
    pre = backup(target) if target.exists() else "(none)"
    _runtime_save_barrier()
    shutil.copy2(src, target)
    return {"ok": f"restored {m.group('target')} from {m.group('ts')}", "backup": f"pre-restore: {pre}"}


def op_sockets(body: dict) -> dict:
    """Socket editing: rewrite the s1..s6 contents.

    Each socket entry is one of these forms:
      - None / ""              -> empty socket (skipped)
      - {"keepEncoded": <str>} -> an existing socket payload, moved or unchanged;
                                  the server verifies it belongs to this item
      - {"keep": {a,b,n}}      -> legacy unchanged-socket marker
      - {"b": <int>}           -> the user changed/added it -> new seed, n=0
      - <int> (legacy format)  -> new gem/rune; new seed, n=0
    This keeps the a (seed) and n (variant) values of untouched gems/jewels intact.
    """
    if game_running():
        return {"err": "Game is running! Close it first."}
    if not isinstance(body, dict):
        return {"err": "invalid socket request; item unchanged"}
    tgt = body.get("target")
    key = body.get("key")
    if "sockets" not in body or body.get("sockets") is None:
        return {
            "err": (
                "sockets must be an explicit list; use [] only to clear all "
                "sockets; item unchanged"
            )
        }
    sockets = body.get("sockets")
    if not isinstance(tgt, dict) or not isinstance(key, str) or not isinstance(sockets, list):
        return {"err": "invalid socket request; item unchanged"}
    ctx = FileCtx()
    try:
        items = ctx.items(tgt)
    except (KeyError, TypeError, ValueError):
        return {"err": "invalid socket target; item unchanged"}
    if key not in items:
        return {"err": "item not found"}
    entry = items[key]
    d0 = entry.setdefault("data", {})
    if not isinstance(d0, dict):
        return {"err": "item data is malformed; item unchanged"}
    it = resolve(key, d0)
    malformed_sockets = []
    for index in range(1, 7):
        field = f"s{index}"
        if field not in d0:
            continue
        try:
            decode_existing_socket_payload(d0[field])
        except Exception:
            malformed_sockets.append(index)
    if malformed_sockets:
        labels = ", ".join(f"s{index}" for index in malformed_sockets)
        return {
            "err": (
                f"{it['name']}: existing socket payload {labels} is malformed; "
                "run Save Health Check; item unchanged"
            )
        }
    max_sockets = item_socket_limit(it)
    if len(sockets) > max_sockets:
        return {
            "err": (
                f"{it['name']}: maximum {max_sockets} sockets; item unchanged"
            )
        }

    original_payloads = {
        index: d0.get(f"s{index}")
        for index in range(1, 7)
        if f"s{index}" in d0
    }
    remaining_payload_counts: dict[str, int] = {}
    for value in original_payloads.values():
        if isinstance(value, str):
            remaining_payload_counts[value] = remaining_payload_counts.get(value, 0) + 1

    def legacy_keep_payload(marker: object, target_index: int) -> str | None:
        if not isinstance(marker, dict):
            return None
        current = original_payloads.get(target_index)
        candidates = ([current] if isinstance(current, str) else []) + [
            original_payloads[index]
            for index in sorted(original_payloads)
            if isinstance(original_payloads[index], str)
            and original_payloads[index] != current
        ]
        for encoded in candidates:
            if remaining_payload_counts.get(encoded, 0) <= 0:
                continue
            try:
                decoded = json.loads(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(decoded, dict):
                continue
            if all(decoded.get(field) == marker.get(field) for field in ("a", "b", "n")):
                return encoded
        return None

    socketable_ids = {
        int(row["b"])
        for row in CAT
        if row.get("kind") == "normal"
        and row.get("available", True)
        and int(row.get("cls", -1)) == 15
    }
    prepared: list[str | None] = []
    for n, e in enumerate(sockets, 1):
        if e is None or e == "":
            prepared.append(None)
            continue
        encoded = None
        if isinstance(e, dict) and "keepEncoded" in e:
            candidate = e.get("keepEncoded")
            if isinstance(candidate, str) and remaining_payload_counts.get(candidate, 0) > 0:
                encoded = candidate
        elif isinstance(e, dict) and e.get("keep") is True:
            candidate = original_payloads.get(n)
            if (
                isinstance(candidate, str)
                and remaining_payload_counts.get(candidate, 0) > 0
            ):
                encoded = candidate
        elif isinstance(e, dict) and isinstance(e.get("keep"), dict):
            encoded = legacy_keep_payload(e.get("keep"), n)
        if encoded is not None:
            remaining_payload_counts[encoded] -= 1
            prepared.append(encoded)
            continue

        raw_base = e.get("b") if isinstance(e, dict) and "b" in e else e
        if isinstance(raw_base, bool) or not isinstance(raw_base, (int, float)):
            return {"err": f"Socket {n} is invalid; item unchanged"}
        try:
            base_id = int(raw_base)
            integral = float(raw_base) == float(base_id)
        except (OverflowError, TypeError, ValueError):
            integral = False
            base_id = -1
        if not integral or base_id not in socketable_ids:
            return {"err": f"Socket {n} is not a known rune or gem; item unchanged"}
        payload = {"a": int(random_item_seed()), "b": base_id, "n": 0}
        prepared.append(base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode())

    import copy
    updated = copy.deepcopy(d0)
    for n in range(1, 7):
        updated.pop(f"s{n}", None)
    updated.pop("unset", None)  # editing resets the forged state
    for n, encoded in enumerate(prepared, 1):
        if encoded is not None:
            updated[f"s{n}"] = encoded

    # The payload fields s1..s6 hold contents, not slot capacity. ``zz.sockets``
    # records the editor-visible empty-slot count; native in-game capacity is
    # still generated from the item's measured ``a`` seed.
    if "zz" in updated or len(sockets) > 0:
        if "zz" in updated and not isinstance(updated.get("zz"), dict):
            return {"err": f"{it['name']}: socket metadata is malformed; item unchanged"}
        zz = updated.setdefault("zz", {})
        zz["sockets"] = float(len(sockets))
    if updated == d0:
        return {
            "ok": f"{it['name']}: sockets already unchanged",
            "backup": "",
            "socketCount": len(sockets),
            "maxSockets": max_sockets,
        }
    d0.clear()
    d0.update(updated)
    baks = ctx.save_all()
    filled = sum(encoded is not None for encoded in prepared)
    return {
        "ok": (
            f"{it['name']}: sockets updated "
            f"({filled}/{len(sockets)} filled)"
        ),
        "backup": ", ".join(baks),
        "socketCount": len(sockets),
        "maxSockets": max_sockets,
    }


def op_delete(body: dict) -> dict:
    if game_running():
        return {"err": "Game is running! Close it first."}
    tgt = body["target"]
    key = body["key"]
    if tgt["type"] == "stash":
        d = json.loads(decode_hss(SAVES / "stash.hss"))
        if key in d.get(tgt["tab"], {}):
            del d[tgt["tab"]][key]
            bk = write_stash(d)
            return {"ok": "deleted", "backup": bk}
    elif tgt["type"] == "bag":
        slot = int(tgt["slot"])
        p = SAVES / f"inventory_order_{slot}.hss"
        d = json.loads(decode_hss(p))
        if key in d.get(tgt["tab"], {}):
            del d[tgt["tab"]][key]
            bk = write_bags(slot, d)
            return {"ok": "deleted", "backup": bk}
    elif tgt["type"] in ("equipped", "potions", "personal_stash"):
        slot = int(tgt["slot"])
        p = SAVES / f"herosiege{slot}.hss"
        txt = decode_hss(p)
        m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
        inv = json.loads(base64.b64decode(m.group(1)))
        sec = "equipped_items" if tgt["type"] == "equipped" else tgt["type"]
        if key in inv.get(sec, {}):
            del inv[sec][key]
            bk = write_char_inventory(slot, inv)
            return {"ok": "deleted", "backup": bk}
    return {"err": "item not found"}


# ---------- Relic Lab ----------
# Relics go ONLY into the 5 relic slots (g=10..14); they cannot sit in the bag or
# the stash.  They are stored in the character save's equipped_items:
#   {"g":<slot 10-14>, "o":<level 1-10>, "a":<seed>, "j":0, "b":<relic base>, "c":0}
# "o" = level (Globe lvl10 = +5 element skill). If all 5 slots are full, the
# selected slot is replaced.
RELIC_SLOTS = {10: "Relic 1", 11: "Relic 2", 12: "Relic 3", 13: "Relic 4", 14: "Relic 5"}


def relic_disp(key: str) -> str:
    """relic_lightningGlobe -> 'Lightning Globe'."""
    s = re.sub(r"^relic_", "", str(key or ""))
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return s[:1].upper() + s[1:] if s else "?"


def relic_list() -> list:
    """Katalogdaki tum relic'ler (cls 16), eleman-skill etiketiyle."""
    out = []
    for i, r in enumerate(CAT):
        if isinstance(r, dict) and r.get("cls") == 16:
            tag = ""
            for s in (r.get("stats") or []):
                s0 = str(s[0]) if isinstance(s, list) and len(s) >= 1 else ""
                if "Skills" in s0 and "Damage" not in s0:
                    tag = "+" + s0.replace("to ", "").replace(" Skills", " skills")
                    break
            out.append({"cid": i, "b": r.get("b"), "key": r.get("key"),
                        "name": relic_disp(r.get("key")), "tag": tag})
    out.sort(key=lambda x: x["name"])
    return out


def op_make_relic(body: dict) -> dict:
    """Write the relic into the character's equipped relic slot (g=10..13),
    replacing whatever is in that slot.
    body: {slot:<char>, cid:<catalog>, level:1-10, g:10-13}"""
    if game_running():
        return {"err": "Game is running! Close it first."}
    try:
        slot = int(body["slot"]); g = int(body["g"])
        level = max(1, min(10, int(body.get("level", 10))))
        r = CAT[int(body["cid"])]
    except Exception:
        return {"err": "bad request"}
    if r.get("cls") != 16:
        return {"err": "That catalog entry is not a relic."}
    if g not in RELIC_SLOTS:
        return {"err": "Relic slot must be Relic 1-4 (g=10-13)."}
    p = SAVES / f"herosiege{slot}.hss"
    if not p.exists():
        return {"err": f"character {slot} not found"}
    txt = decode_hss(p)
    m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
    inv = json.loads(base64.b64decode(m.group(1))) if m else {}
    eq = inv.setdefault("equipped_items", {})
    replaced = None
    for k in [k for k, v in eq.items() if int(v.get("data", {}).get("g", -1)) == g]:
        replaced = eq[k]["data"].get("b"); del eq[k]
    key = fresh_key(16, eq)
    eq[key] = {"data": {"g": float(g), "o": float(level),
                        "a": random_item_seed(),
                        "j": 0.0, "b": float(int(r["b"])), "c": 0.0}}
    bk = write_char_inventory(slot, inv)
    name = relic_disp(r.get("key"))
    extra = f" (replaced {relic_disp(_relic_key_by_b(replaced))})" if replaced is not None else ""
    return {"ok": f"{name} lvl {level} -> {RELIC_SLOTS[g]} on character {slot}{extra}", "backup": bk}


def _relic_key_by_b(b) -> str:
    if b is None:
        return ""
    try:
        bi = int(float(b))
    except Exception:
        return ""
    for r in CAT:
        if isinstance(r, dict) and r.get("cls") == 16 and int(r.get("b", -1)) == bi:
            return r.get("key", "")
    return f"b={bi}"


# ---------- Bulk Stackable Lab ----------
# Key (12) / Boss Part-Tarot (13) / Material (14) / Rune-Gem-Orb (15):
# in a single slot `o` = quantity (stack).
#   Key/Gem:   {"n":0,"w":1,"o":<qty>,"a":0,"e":0,"d":0,"b":<base>,"c":0}
#   Material:  {"o":<qty>,"a":<seed>,"e":0,"d":0,"b":<base>,"c":0}
# (Potion 11 = codex/potion-belt mix; Consumable 18 = m:1 singleton -> not included.)
STACKABLE_CLS = {
    12: "Key",
    13: "Boss Part / Tarot",
    14: "Material",
    15: "Rune / Gem / Orb",
}

# The native Season 10 stash produced by the game/editor uses 999 as a full
# stack.  The generic right-click editor intentionally accepts larger manual
# values, but the one-click fill operation stays on this conservative native
# amount instead of treating a numeric save-field limit as a game stack limit.
FULL_STACK_AMOUNT = 999

# These addresses are stored as individual records in native Season 10 stash
# data.  Adding an ``o`` quantity turns them into an unproven shape, so the fill
# operation ensures one record exists but never invents a stack for them.
MATERIAL_SINGLETON_ADDRESSES = frozenset({
    *((13, base) for base in range(58, 65)),
    (14, 59),  # Reflection of Tarethiel
    (14, 67),  # Blessed Dice
})


def stackable_list() -> list:
    out = []
    for i, r in enumerate(CAT):
        if (isinstance(r, dict) and r.get("cls") in STACKABLE_CLS
                and r.get("available", True)):
            out.append({"cid": i, "cls": r["cls"], "b": r.get("b"),
                        "name": r.get("name") or r.get("key")})
    out.sort(key=lambda x: (x["cls"], str(x["name"])))
    return out


def s10_access_list() -> list:
    """Return UI metadata only for access items proven in the S10 repository."""
    by_key = {r.get("key"): r for r in CAT if isinstance(r, dict)}
    groups = []
    for group_id, spec in S10_ACCESS_GROUPS.items():
        items = []
        for item_spec in spec["items"]:
            row = by_key.get(item_spec["key"])
            if not row or row.get("cls") != 12 or not row.get("s10Verified"):
                continue
            items.append({
                "name": row.get("name") or row.get("key"),
                "cls": 12,
                "b": int(row["b"]),
                **{k: v for k, v in item_spec.items() if k != "key"},
            })
        groups.append({"id": group_id, "name": spec["name"],
                       "description": spec["description"], "items": items})
    return groups


def _make_one_stackable(
    items: dict,
    tab: str,
    cls: int,
    base: int,
    amount: int,
    width: int = 1,
    height: int = 1,
):
    pos = find_free_pos(items, tab, width, height)
    if pos is None:
        return None
    key = fresh_key(cls, items)
    d = {"o": float(amount), "a": random_item_seed(),
         "j": 0.0, "b": float(int(base)), "c": 0.0}
    items[key] = {"pos": pos, "data": d}
    return key


def _native_catalog_address(row: dict) -> tuple[str, int, int, int]:
    cls = int(row["cls"])
    return (
        str(row["kind"]),
        cls,
        int(row.get("sub", 0)) if cls == 3 else 0,
        int(row["b"]),
    )


def _native_entry_address(key: object, entry: object) -> tuple[str, int, int, int] | None:
    """Read an item's native identity without trusting its display name."""

    if not isinstance(key, str) or not isinstance(entry, dict):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    try:
        cls = int(key.rsplit("-", 1)[1])
        kind_bit = int(data.get("c", 0))
        if kind_bit not in (0, 1):
            return None
        sub = int(data.get("j", 0)) if cls == 3 else 0
        base = int(data["b"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    return ("unique" if kind_bit == 1 else "normal", cls, sub, base)


def stash_fill_catalog(tab: object) -> tuple[str, list[dict]]:
    """Return the exact allowlisted catalog scope for one shared-stash tab."""

    if tab == "unique_items":
        label = "Unique tab"
        rows = [
            row for row in CAT
            if isinstance(row, dict)
            and row.get("kind") == "unique"
            and row.get("available", True)
        ]
    elif isinstance(tab, str) and re.fullmatch(r"material_tab(?:_[1-9]\d*)?", tab):
        label = _vault_shared_grid_label(tab)
        rows = [
            row for row in CAT
            if isinstance(row, dict)
            and row.get("kind") == "normal"
            and int(row.get("cls", -1)) in (13, 14)
            and row.get("available", True)
        ]
    elif isinstance(tab, str) and re.fullmatch(r"socket_tab(?:_[1-9]\d*)?", tab):
        label = _vault_shared_grid_label(tab)
        rows = [
            row for row in CAT
            if isinstance(row, dict)
            and row.get("kind") == "normal"
            and int(row.get("cls", -1)) == 15
            and row.get("available", True)
        ]
    else:
        raise ValueError("Only the Unique, Material, or Socket stash tab can be filled.")

    # Fail closed if a future catalog accidentally repeats one native address.
    by_address = {}
    for row in rows:
        address = _native_catalog_address(row)
        if address in by_address:
            raise ValueError(f"duplicate catalog address detected: {address}")
        by_address[address] = row
    return label, sorted(rows, key=_native_catalog_address)


def _stack_is_full(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and float(value) == FULL_STACK_AMOUNT
    except (TypeError, ValueError, OverflowError):
        return False


def _new_stash_fill_entry(row: dict, tab: str, items: dict, *, stack: bool) -> str | None:
    width = max(1, int(row.get("w", 1)))
    height = max(1, int(row.get("h", 1)))
    pos = find_free_pos(items, tab, width, height)
    if pos is None:
        return None
    cls = int(row["cls"])
    key = fresh_key(cls, items)
    data = {
        "a": random_item_seed(),
        "j": 0.0,
        "b": float(int(row["b"])),
        "c": 0.0,
    }
    if stack:
        data["o"] = float(FULL_STACK_AMOUNT)
    items[key] = {"pos": pos, "data": data}
    return key


def _stash_fill_grid_error(items: dict, tab: str) -> str | None:
    """Reject a damaged target grid instead of treating hidden items as owned."""

    cols, rows = grid_dims(tab)
    occupied: dict[tuple[int, int], str] = {}
    for key, entry in items.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
            return f"record {key!r} has no valid item data"
        pos = entry.get("pos")
        if not isinstance(pos, (list, tuple)) or len(pos) != 2:
            return f"record {key!r} has no valid grid position"
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not float(value).is_integer()
            for value in pos
        ):
            return f"record {key!r} has a malformed grid position"
        try:
            item = resolve(str(key), entry["data"])
            width = max(1, int(item["w"]))
            height = max(1, int(item["h"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            return f"record {key!r} has an invalid native item address"
        x, y = int(pos[0]), int(pos[1])
        if x < 0 or y < 0 or x + width > cols or y + height > rows:
            return f"record {key!r} is outside the {cols}x{rows} grid"
        for dy in range(height):
            for dx in range(width):
                cell = (x + dx, y + dy)
                if cell in occupied:
                    return (
                        f"records {occupied[cell]!r} and {key!r} overlap at "
                        f"cell {cell[0]},{cell[1]}"
                    )
                occupied[cell] = str(key)
    return None


def op_fill_stash(body: dict) -> dict:
    """Idempotently complete one native Shared Stash repository tab.

    The full plan is built in memory first.  Unknown records, duplicate native
    records, positions and future fields are preserved.  A single backup/write
    happens only after every missing entry has passed validation and placement.
    """

    if game_running():
        return {"err": "Game is running! Close it first."}
    try:
        tab = body.get("tab")
        label, rows = stash_fill_catalog(tab)
    except (AttributeError, TypeError, ValueError) as exc:
        return {"err": str(exc)}

    stash_path = SAVES / "stash.hss"
    try:
        document = json.loads(decode_hss(stash_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"err": f"Shared Stash could not be read: {exc}"}
    if tab not in document or not isinstance(document.get(tab), dict):
        return {"err": f"{label} does not exist in this Shared Stash; nothing changed."}

    work = copy.deepcopy(document[tab])
    if tab != "unique_items":
        grid_error = _stash_fill_grid_error(work, str(tab))
        if grid_error:
            return {
                "err": (
                    f"{label} has an invalid grid ({grid_error}). Run Save Health "
                    "Check first; nothing changed."
                )
            }
    existing: dict[tuple[str, int, int, int], list[str]] = {}
    for key, entry in work.items():
        address = _native_entry_address(key, entry)
        if address is not None:
            existing.setdefault(address, []).append(key)

    added = 0
    updated = 0
    already_present = 0
    singleton_count = 0
    dice_targets = []
    missing_rows = [row for row in rows if _native_catalog_address(row) not in existing]

    if tab == "unique_items":
        # Every equipment profile and explicit selectable identity is validated before
        # the first in-memory insertion, keeping failure strictly all-or-nothing.
        prepared = []
        requested_targets = body.get("identityTargetIds", body.get("diceSkillIds"))
        if not isinstance(requested_targets, dict):
            requested_targets = {}
        for row in missing_rows:
            try:
                profile_id = catalog_skill_profile_id(row)
                skill_id = None
                selected = None
                if profile_id is not None:
                    database = skill_target_database(profile_id)
                    is_class_target = profile_id == TORCH_PROFILE_ID
                    noun = "class" if is_class_target else "skill"
                    if profile_id not in requested_targets:
                        return {
                            "err": (
                                f"{row['name']} needs an explicit verified {noun} target; "
                                "no items were added."
                            )
                        }
                    if database is None:
                        raise ValueError("unknown selectable identity profile")
                    selected = database.target(profile_id, requested_targets[profile_id])
                    skill_id = int(selected["id"])
                generation_roll_profile_for_request(row, skill_id)
                data = make_data(row, skill_id=skill_id)
            except (
                DiceSkillValidationError,
                TorchClassValidationError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                return {"err": f"{row.get('name', 'Unique item')}: {exc}; nothing changed"}
            prepared.append((row, data))
            if selected is not None:
                is_class = profile_id == TORCH_PROFILE_ID
                dice_targets.append({
                    "item": row["name"],
                    "profileId": profile_id,
                    "targetId": int(selected["id"]),
                    "targetKind": "class" if is_class else "skill",
                    "target": (
                        selected["name"] if is_class
                        else f"{selected['className']}: {selected['name']}"
                    ),
                })
        already_present = len(rows) - len(prepared)
        for row, data in prepared:
            key = fresh_key(int(row["cls"]), work)
            work[key] = {"data": data}
            added += 1
    else:
        row_by_address = {_native_catalog_address(row): row for row in rows}
        already_present = len(rows) - len(missing_rows)
        # Preserve every duplicate record, while honoring the user's request
        # that every recognized stack in this repository be full.
        for address, keys in existing.items():
            row = row_by_address.get(address)
            if row is None:
                continue
            is_singleton = (
                int(row["cls"]), int(row["b"])
            ) in MATERIAL_SINGLETON_ADDRESSES
            if is_singleton:
                singleton_count += 1
                continue
            for key in sorted(keys):
                data = work[key].get("data")
                if isinstance(data, dict) and not _stack_is_full(data.get("o")):
                    data["o"] = float(FULL_STACK_AMOUNT)
                    updated += 1

        # Place tall Tarot cards before 1x1 entries so a valid packing is not
        # lost merely because catalog order fragmented an otherwise empty grid.
        missing_rows.sort(key=lambda row: (
            -int(row.get("w", 1)) * int(row.get("h", 1)),
            _native_catalog_address(row),
        ))
        for row in missing_rows:
            is_singleton = (
                int(row["cls"]), int(row["b"])
            ) in MATERIAL_SINGLETON_ADDRESSES
            if _new_stash_fill_entry(row, str(tab), work, stack=not is_singleton) is None:
                return {
                    "err": (
                        f"{label} does not have enough valid grid space for all "
                        f"{len(rows)} catalog items; nothing changed."
                    )
                }
            added += 1
            if is_singleton:
                singleton_count += 1

    if added == 0 and updated == 0:
        return {
            "ok": f"{label} is already complete.",
            "backup": "",
            "tab": tab,
            "total": len(rows),
            "added": 0,
            "updated": 0,
            "existing": already_present,
            "fullStack": FULL_STACK_AMOUNT if tab != "unique_items" else None,
            "singletons": singleton_count,
            "diceTargets": dice_targets,
        }

    document[tab] = work
    backup_name = write_stash(document)
    if tab == "unique_items":
        summary = f"{label}: {added} added, {already_present} already present"
    else:
        summary = (
            f"{label}: {added} added, {updated} set to x{FULL_STACK_AMOUNT}, "
            f"{already_present} already present"
        )
    return {
        "ok": summary,
        "backup": backup_name,
        "tab": tab,
        "total": len(rows),
        "added": added,
        "updated": updated,
        "existing": already_present,
        "fullStack": FULL_STACK_AMOUNT if tab != "unique_items" else None,
        "singletons": singleton_count,
        "diceTargets": dice_targets,
    }


def op_make_stackable(body: dict) -> dict:
    """Key/Material/Gem yigini uret. body: {target, cid, amount, count}."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    try:
        r = CAT[int(body["cid"])]
        amount = max(1, min(99999, int(body.get("amount", 999))))
        count = max(1, min(200, int(body.get("count", 1))))
        tgt = body["target"]
    except Exception:
        return {"err": "bad request"}
    cls = r.get("cls")
    if cls not in STACKABLE_CLS:
        return {"err": "That item is not a Season 10 stackable."}
    ctx = FileCtx()
    items = ctx.items(tgt)
    tab = tgt.get("tab") or tgt["type"]
    made = 0
    for _ in range(count):
        if _make_one_stackable(
            items,
            tab,
            cls,
            int(r["b"]),
            amount,
            max(1, int(r.get("w", 1))),
            max(1, int(r.get("h", 1))),
        ) is None:
            break
        made += 1
    if made == 0:
        return {"err": "no free space in target tab"}
    baks = ctx.save_all()
    nm = r.get("name") or r.get("key")
    return {"ok": f"{made}x {nm} (stack {amount}) -> {tab}", "backup": ", ".join(baks)}


def op_make_s10_access(body: dict) -> dict:
    """Create a complete verified S10 access kit in a character key bag."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    try:
        group_id = str(body["group"])
        spec = S10_ACCESS_GROUPS[group_id]
        slot = int(body["slot"])
        amount = max(1, min(99999, int(body.get("amount", 10))))
    except (KeyError, TypeError, ValueError):
        return {"err": "bad request"}

    by_key = {r.get("key"): r for r in CAT if isinstance(r, dict)}
    rows = []
    for item_spec in spec["items"]:
        row = by_key.get(item_spec["key"])
        if (not row or row.get("cls") != 12 or not row.get("s10Verified")
                or not row.get("available", True)):
            return {"err": f"Unverified Season 10 address: {item_spec['key']}"}
        rows.append(row)

    ctx = FileCtx()
    target = {"type": "bag", "slot": slot, "tab": "inventory_key_tab"}
    items = ctx.items(target)
    created = []
    for row in rows:
        key = _make_one_stackable(items, "inventory_key_tab", 12,
                                  int(row["b"]), amount)
        if key is None:
            return {"err": (f"Not enough free slots in character {slot}'s Key bag. "
                            f"The kit needs {len(rows)} free slots; nothing was written.")}
        created.append(row.get("name") or row.get("key"))

    backups = ctx.save_all()
    return {
        "ok": (f"{spec['name']} -> character {slot} Key bag: "
               f"{', '.join(created)} (stack {amount} each)"),
        "backup": ", ".join(backups),
        "created": created,
    }


# ---------- Save health and owned-item search ----------

TAB_LABELS = {
    "unique_items": "Unique collection",
    "material_tab": "Material tab",
    "socket_tab": "Socket tab",
    "inventory_tab_0": "Main bag",
    "inventory_tab_1": "Extra bag 1",
    "inventory_tab_2": "Extra bag 2",
    "inventory_tab_3": "Extra bag 3",
    "inventory_tab_4": "Extra bag 4",
    "inventory_charms": "Charms",
    "inventory_key_tab": "Keys",
    "inventory_material_tab": "Materials",
    "inventory_socket_tab": "Runes & Gems",
    "inventory_relic_tab": "Relics",
    "inventory_tarot_tab": "Tarot",
    "inventory_vault_tab": "Essence Vaults",
    "personal_stash": "Personal Stash",
    "potions": "Potion belt",
    "equipped_items": "Equipped",
}


def _tab_label(tab: str) -> str:
    if tab in TAB_LABELS:
        return TAB_LABELS[tab]
    m = re.fullmatch(r"stash_tab_(\d+)", tab)
    if m:
        return f"Stash tab {m.group(1)}"
    m = re.fullmatch(r"inventory_vault_active_(\d+)", tab)
    if m:
        return f"Active Vault {int(m.group(1)) + 1}"
    return tab.replace("_", " ").title()


def _character_paths():
    for path in sorted(SAVES.glob("herosiege*.hss")):
        match = re.fullmatch(r"herosiege(\d+)\.hss", path.name)
        if match and path.stat().st_size > 50:
            yield int(match.group(1)), path


def _decode_character_inventory(path: Path) -> tuple[str, dict]:
    text = decode_hss(path)
    match = re.search(r'inventory="([A-Za-z0-9+/=]+)"', text)
    if not match:
        raise ValueError("inventory field not found")
    payload = base64.b64decode(match.group(1), validate=True)
    inventory = json.loads(payload)
    if not isinstance(inventory, dict):
        raise ValueError("inventory payload is not an object")
    return text, inventory


def _character_name(text: str, slot: int) -> str:
    match = re.search(r'\nname="([^"]*)"', text)
    return match.group(1) if match and match.group(1) else f"Slot {slot}"


def _valid_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _health_issue(issues: list, severity: str, code: str, file_name: str,
                  location: str, message: str, *, item: str = "", fixable: bool = False):
    issues.append({
        "id": f"issue-{len(issues) + 1}", "severity": severity, "code": code,
        "file": file_name, "location": location, "item": item,
        "message": message, "fixable": fixable,
    })


def _first_free_from_occupied(occupied: set, cols: int, rows: int, width: int, height: int):
    if width < 1 or height < 1 or width > cols or height > rows:
        return None
    for y in range(rows - height + 1):
        for x in range(cols - width + 1):
            cells = {(x + dx, y + dy) for dy in range(height) for dx in range(width)}
            if not cells.intersection(occupied):
                return [x, y], cells
    return None


def _scan_item_container(items, tab: str, file_name: str, location: str,
                         issues: list, *, positioned: bool, equipment: bool = False,
                         apply: bool = False, state: dict | None = None) -> bool:
    """Validate one native item dictionary and apply only deterministic repairs."""
    if not isinstance(items, dict):
        _health_issue(issues, "error", "container_type", file_name, location,
                      "Item container is not an object and cannot be read safely.")
        return False

    changed = False
    occupied = set()
    equipped_slots = {}
    cols, rows = grid_dims(tab)

    for key, entry in list(items.items()):
        item_label = str(key)
        if not isinstance(entry, dict):
            _health_issue(issues, "error", "entry_type", file_name, location,
                          "Item record is not an object.", item=item_label)
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            _health_issue(issues, "error", "item_data", file_name, location,
                          "Item data is missing or malformed.", item=item_label)
            continue

        key_match = re.search(r"-(-?\d+)$", str(key))
        if not key_match:
            _health_issue(issues, "error", "item_key", file_name, location,
                          "Item key has no valid class suffix.", item=item_label)
            continue
        item_class = int(key_match.group(1))
        try:
            resolved = resolve(str(key), data)
        except Exception:
            _health_issue(issues, "error", "item_address", file_name, location,
                          "Item address fields are malformed; no automatic change was made.",
                          item=item_label)
            continue
        item_label = resolved.get("name") or str(key)
        width = max(1, int(resolved.get("w") or 1))
        height = max(1, int(resolved.get("h") or 1))

        if resolved.get("cid") is None and data.get("b") is not None:
            _health_issue(issues, "warning", "unknown_item", file_name, location,
                          "Item address is not present in the verified Season 10 catalog.",
                          item=item_label)
        elif resolved.get("cid") is not None and CAT[resolved["cid"]].get("available") is False:
            _health_issue(issues, "warning", "unavailable_item", file_name, location,
                          "Item is readable but is not approved for Season 10 generation.",
                          item=item_label)

        for socket_index in range(1, 7):
            socket_field = f"s{socket_index}"
            if socket_field not in data:
                continue
            try:
                decode_existing_socket_payload(data[socket_field])
            except Exception:
                _health_issue(issues, "error", "socket_payload", file_name, location,
                              f"Socket {socket_index} payload is malformed; it was not changed.",
                              item=item_label)

        if item_class in (12, 13, 14, 15) and "o" in data:
            amount = data.get("o")
            if not _valid_number(amount) or float(amount) < 1:
                _health_issue(issues, "warning", "stack_value", file_name, location,
                              "Stack amount is invalid and can be reset to 1.",
                              item=item_label, fixable=True)
                if apply:
                    data["o"] = 1.0
                    changed = True
                    if state is not None:
                        state["fixed"] += 1

        if equipment:
            g = data.get("g")
            if not _valid_number(g) or float(g) != int(float(g)):
                _health_issue(issues, "error", "equip_slot", file_name, location,
                              "Equipped item has an invalid slot number.", item=item_label)
                continue
            g = int(float(g))
            if g not in EQUIP_ACCEPT or item_class not in EQUIP_ACCEPT[g]:
                _health_issue(issues, "error", "equip_mismatch", file_name, location,
                              f"Item class does not fit {SLOT_NAMES.get(g, f'slot {g}')}; no automatic change was made.",
                              item=item_label)
            if g in equipped_slots:
                _health_issue(issues, "error", "duplicate_equip", file_name, location,
                              f"Multiple items occupy {SLOT_NAMES.get(g, f'slot {g}')}; no automatic change was made.",
                              item=item_label)
            else:
                equipped_slots[g] = key
            continue

        if not positioned:
            continue

        pos = entry.get("pos")
        valid_pos = (isinstance(pos, (list, tuple)) and len(pos) >= 2
                     and _valid_number(pos[0]) and _valid_number(pos[1])
                     and float(pos[0]) == int(float(pos[0]))
                     and float(pos[1]) == int(float(pos[1])))
        reason = "position_missing"
        candidate_cells = None
        if valid_pos:
            x, y = int(float(pos[0])), int(float(pos[1]))
            candidate_cells = {(x + dx, y + dy) for dy in range(height) for dx in range(width)}
            if x < 0 or y < 0 or x + width > cols or y + height > rows:
                valid_pos = False
                reason = "position_bounds"
            elif candidate_cells.intersection(occupied):
                valid_pos = False
                reason = "position_overlap"

        if valid_pos:
            occupied.update(candidate_cells)
            continue

        free = _first_free_from_occupied(occupied, cols, rows, width, height)
        can_fix = free is not None
        messages = {
            "position_missing": "Grid position is missing or malformed.",
            "position_bounds": "Item extends beyond this grid.",
            "position_overlap": "Item overlaps another item in this grid.",
        }
        _health_issue(issues, "error", reason, file_name, location,
                      messages[reason] + (" It can be moved to the first free cell." if can_fix else " No free cell is available."),
                      item=item_label, fixable=can_fix)
        if free is not None:
            new_pos, cells = free
            occupied.update(cells)
            if apply:
                entry["pos"] = [float(new_pos[0]), float(new_pos[1])]
                changed = True
                if state is not None:
                    state["fixed"] += 1
    return changed


def _validate_recovery_document(plan: RecoveryPlan) -> tuple[dict, list]:
    """Run the regular read-only item scanner against a recovered preview."""
    if plan.status != "recoverable" or plan.recovered_text is None:
        raise HSSRecoveryError("A recoverable stash plan is required")
    document = json.loads(plan.recovered_text)
    if not isinstance(document, dict):
        raise HSSRecoveryError("Recovered stash root is not an object")
    issues = []
    item_count = 0
    for tab, items in document.items():
        if tab == "stash_tab_data" or not isinstance(items, dict):
            continue
        item_count += len(items)
        _scan_item_container(
            items, tab, "stash.hss", f"Shared Stash · {_tab_label(tab)}", issues,
            positioned=tab != "unique_items", apply=False, state={"fixed": 0},
        )
    if item_count != plan.item_count:
        raise HSSRecoveryError(
            f"Recovered item count changed ({item_count} != {plan.item_count})"
        )
    return document, issues


def inspect_stash_hss_recovery(path: Path | None = None) -> tuple[RecoveryPlan, list]:
    """Return a read-only recovery plan plus ordinary health findings."""
    stash_path = path or (SAVES / "stash.hss")
    raw = stash_path.read_bytes()
    plan = analyze_stash_hss(raw, XOR_KEY)
    issues = []
    if plan.status == "recoverable":
        try:
            _, issues = _validate_recovery_document(plan)
        except Exception as exc:
            return RecoveryPlan(
                status="unsupported",
                source_sha256=plan.source_sha256,
                source_size=plan.source_size,
                decoded_size=plan.decoded_size,
                nonzero_high_bytes=plan.nonzero_high_bytes,
                trailing_codepoints=plan.trailing_codepoints,
                diagnostics=(f"Recovered preview failed the item health gate: {exc}",),
            ), []
        if any(issue["severity"] == "error" for issue in issues):
            return RecoveryPlan(
                status="unsupported",
                source_sha256=plan.source_sha256,
                source_size=plan.source_size,
                decoded_size=plan.decoded_size,
                nonzero_high_bytes=plan.nonzero_high_bytes,
                trailing_codepoints=plan.trailing_codepoints,
                diagnostics=("Recovered preview contains structural item errors.",),
            ), issues
    return plan, issues


def _hss_recovery_mutation_gate() -> dict | None:
    """Protect recovery without decoding a stash needed by a pending Vault row."""
    if game_running():
        return {"err": "Hero Siege is running. Close it before HSS recovery."}
    vault_path = Path(VAULT_DB_FILE).expanduser().resolve()
    if not vault_path.exists():
        return None
    try:
        store = vault_store()
        pending = store.list_pending_transfers()
        pending_batches = store.list_pending_transfer_batches()
    except Exception as exc:
        return {"err": f"Infinite Vault state could not be verified: {exc}"}
    if pending or pending_batches:
        return {
            "err": "An Infinite Vault transfer is pending. HSS recovery was blocked to protect item ownership. Nothing was changed."
        }
    return None


def _write_recovery_temp(stash_path: Path, payload: bytes) -> Path:
    handle = tempfile.NamedTemporaryFile(
        mode="wb", dir=stash_path.parent, prefix=".stash.hss.recovery-",
        suffix=".tmp", delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temp_path
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _verify_recovery_output(raw: bytes, source_plan: RecoveryPlan) -> RecoveryPlan:
    if hashlib.sha256(raw).hexdigest().upper() != source_plan.output_sha256:
        raise HSSRecoveryError("Recovered output hash differs from the preview")
    verified = analyze_stash_hss(raw, XOR_KEY)
    if verified.status != "healthy":
        raise HSSRecoveryError("Recovered output does not pass strict HSS validation")
    if (
        verified.root_key_count != source_plan.root_key_count
        or verified.item_count != source_plan.item_count
        or verified.items_by_container != source_plan.items_by_container
        or verified.item_manifest_sha256 != source_plan.item_manifest_sha256
    ):
        raise HSSRecoveryError("Recovered output changed the validated item manifest")
    return verified


def _create_recovery_backup(stash_path: Path, raw: bytes, source_sha256: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_path = stash_path.with_name(stash_path.name + f".pre_recovery_{stamp}")
    try:
        with backup_path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _file_sha256(backup_path).upper() != source_sha256 or backup_path.read_bytes() != raw:
            raise HSSRecoveryError("Recovery backup verification failed")
        return backup_path
    except Exception:
        try:
            backup_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _restore_recovery_backup(stash_path: Path, backup_path: Path,
                             expected_sha256: str,
                             expected_current_sha256: str) -> None:
    backup_raw = backup_path.read_bytes()
    if hashlib.sha256(backup_raw).hexdigest().upper() != expected_sha256:
        raise HSSRecoveryError("Recovery rollback backup no longer matches the source")
    current_raw = stash_path.read_bytes()
    if hashlib.sha256(current_raw).hexdigest().upper() != expected_current_sha256:
        raise HSSRecoveryError(
            "Active stash changed after recovery; rollback was refused to avoid overwriting a newer external write"
        )
    rollback_path = _write_recovery_temp(stash_path, backup_raw)
    try:
        _runtime_save_barrier()
        current_raw = stash_path.read_bytes()
        if hashlib.sha256(current_raw).hexdigest().upper() != expected_current_sha256:
            raise HSSRecoveryError(
                "Active stash changed before rollback; rollback was refused to avoid overwriting a newer external write"
            )
        os.replace(rollback_path, stash_path)
        rollback_path = None
        if _file_sha256(stash_path).upper() != expected_sha256:
            raise HSSRecoveryError("Recovery rollback verification failed")
    finally:
        if rollback_path is not None:
            try:
                rollback_path.unlink()
            except FileNotFoundError:
                pass


def op_recover_stash_hss(body: dict) -> dict:
    """Apply one previewed recovery while the caller holds save/stash locks."""
    if not isinstance(body, dict):
        return {"err": "HSS recovery request must be a JSON object."}
    if body.get("file") != "stash.hss":
        return {"err": "Only the active stash.hss can be recovered."}
    expected_sha256 = body.get("expectedSha256")
    if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[0-9A-Fa-f]{64}", expected_sha256):
        return {"err": "A valid recovery preview SHA-256 is required."}
    expected_sha256 = expected_sha256.upper()
    blocked = _hss_recovery_mutation_gate()
    if blocked:
        return blocked

    stash_path = SAVES / "stash.hss"
    if not stash_path.exists():
        return {"err": "stash.hss is missing. Nothing was changed."}
    source_raw = stash_path.read_bytes()
    source_sha256 = hashlib.sha256(source_raw).hexdigest().upper()
    if source_sha256 != expected_sha256:
        return {"err": "stash.hss changed after the recovery preview. Nothing was written; scan again."}

    plan, preview_issues = inspect_stash_hss_recovery(stash_path)
    if plan.status != "recoverable":
        if plan.status == "healthy":
            return {"err": "stash.hss is already healthy. Nothing was changed."}
        return {"err": "This file does not match a proven HSS recovery profile. Nothing was changed."}
    if plan.source_sha256 != expected_sha256:
        return {"err": "stash.hss changed after the recovery preview. Nothing was written; scan again."}
    if any(issue["severity"] == "error" for issue in preview_issues):
        return {"err": "The recovery preview contains structural item errors. Nothing was changed."}

    try:
        output_raw = materialize_recovery(source_raw, plan, XOR_KEY)
        _verify_recovery_output(output_raw, plan)
    except Exception as exc:
        return {"err": f"Recovery preview verification failed: {exc}. Nothing was changed."}

    candidate_path = None
    backup_path = None
    replaced = False
    try:
        candidate_path = _write_recovery_temp(stash_path, output_raw)
        _verify_recovery_output(candidate_path.read_bytes(), plan)
        candidate_document = json.loads(decode_hss(candidate_path))
        if candidate_document != json.loads(plan.recovered_text):
            raise HSSRecoveryError("Candidate JSON differs from the validated preview")

        blocked = _hss_recovery_mutation_gate()
        if blocked:
            return blocked
        current_raw = stash_path.read_bytes()
        if current_raw != source_raw or _file_sha256(stash_path).upper() != expected_sha256:
            return {"err": "stash.hss changed during recovery. Nothing was written; scan again."}

        backup_path = _create_recovery_backup(stash_path, source_raw, expected_sha256)
        blocked = _hss_recovery_mutation_gate()
        if blocked:
            return {**blocked, "backup": backup_path.name}
        if stash_path.read_bytes() != source_raw:
            return {"err": "stash.hss changed before replacement. Nothing was written; scan again.",
                    "backup": backup_path.name}

        _runtime_save_barrier()
        if stash_path.read_bytes() != source_raw:
            return {"err": "stash.hss changed at the replacement barrier. Nothing was written; scan again.",
                    "backup": backup_path.name}
        os.replace(candidate_path, stash_path)
        candidate_path = None
        replaced = True
        final_raw = stash_path.read_bytes()
        verified = _verify_recovery_output(final_raw, plan)
        final_document = json.loads(decode_hss(stash_path))
        _, final_health = _validate_recovery_document(
            RecoveryPlan(
                status="recoverable",
                source_sha256=verified.source_sha256,
                source_size=verified.source_size,
                decoded_size=verified.decoded_size,
                root_key_count=verified.root_key_count,
                item_count=verified.item_count,
                items_by_container=verified.items_by_container,
                item_manifest_sha256=verified.item_manifest_sha256,
                recovered_text=decode_hss(stash_path),
            )
        )
        if final_document != json.loads(plan.recovered_text) or any(
                issue["severity"] == "error" for issue in final_health):
            raise HSSRecoveryError("Final on-disk health verification failed")

        return {
            "ok": "stash.hss recovered and verified.",
            "file": "stash.hss",
            "profile": plan.profile,
            "backup": backup_path.name,
            "sourceSha256": expected_sha256,
            "writtenSha256": verified.source_sha256,
            "itemRecordsPreserved": plan.item_count,
            "itemManifestSha256": plan.item_manifest_sha256,
            "changesApplied": len(plan.changes),
            "warnings": sum(1 for issue in final_health if issue["severity"] == "warning"),
        }
    except Exception as exc:
        if replaced and backup_path is not None:
            try:
                _restore_recovery_backup(
                    stash_path, backup_path, expected_sha256, plan.output_sha256
                )
                return {
                    "err": f"Recovery verification failed: {exc}. The original stash.hss was restored from backup.",
                    "backup": backup_path.name,
                    "rolledBack": True,
                }
            except Exception as rollback_exc:
                return {
                    "err": f"Recovery verification failed ({exc}) and automatic rollback also failed ({rollback_exc}). Use the untouched backup immediately.",
                    "backup": backup_path.name,
                    "rolledBack": False,
                }
        return {"err": f"Recovery failed before replacement: {exc}. The active stash was not changed.",
                **({"backup": backup_path.name} if backup_path is not None else {})}
    finally:
        if candidate_path is not None:
            try:
                candidate_path.unlink()
            except FileNotFoundError:
                pass


def _stash_recovery_available() -> bool:
    try:
        plan, _ = inspect_stash_hss_recovery(SAVES / "stash.hss")
        return plan.status == "recoverable"
    except Exception:
        return False


def scan_save_health(apply: bool = False) -> dict:
    running = game_running()
    if apply and running:
        return {"err": "Game is running. Close Hero Siege before applying repairs."}

    issues = []
    backups = []
    recoveries = []
    state = {"fixed": 0, "files": 0, "items": 0}

    stash_path = SAVES / "stash.hss"
    if not stash_path.exists():
        _health_issue(issues, "error", "missing_file", "stash.hss", "Shared Stash",
                      "Shared stash file is missing.")
    else:
        try:
            stash = json.loads(decode_hss(stash_path))
            if not isinstance(stash, dict):
                raise ValueError("root payload is not an object")
            state["files"] += 1
            stash_changed = False
            for tab, items in stash.items():
                if tab == "stash_tab_data" or not isinstance(items, dict):
                    continue
                if isinstance(items, dict):
                    state["items"] += len(items)
                stash_changed |= _scan_item_container(
                    items, tab, "stash.hss", f"Shared Stash · {_tab_label(tab)}", issues,
                    positioned=tab != "unique_items", apply=apply, state=state)
            if apply and stash_changed:
                backups.append(write_stash(stash))
        except Exception as exc:
            try:
                plan, recovery_issues = inspect_stash_hss_recovery(stash_path)
            except Exception:
                plan, recovery_issues = None, []
            if plan is not None and plan.status == "recoverable":
                state["files"] += 1
                state["items"] += plan.item_count
                _health_issue(
                    issues, "error", "file_decode_recoverable", "stash.hss", "Shared Stash",
                    "Strict HSS decoding failed, but the file matches a proven recovery profile. Review and apply the explicit HSS Recovery preview below.",
                )
                preview = plan.as_dict(file_name="stash.hss", can_apply=not running)
                preview["healthWarnings"] = sum(
                    1 for issue in recovery_issues if issue["severity"] == "warning"
                )
                recoveries.append(preview)
            else:
                _health_issue(issues, "error", "file_decode", "stash.hss", "Shared Stash",
                              f"File could not be decoded: {exc}")

    for slot, char_path in _character_paths():
        try:
            char_text, inventory = _decode_character_inventory(char_path)
            char_name = _character_name(char_text, slot)
            state["files"] += 1
            char_changed = False
            for section, positioned, equipment in (
                    ("equipped_items", False, True), ("potions", True, False),
                    ("personal_stash", True, False)):
                items = inventory.get(section, {})
                if isinstance(items, dict):
                    state["items"] += len(items)
                char_changed |= _scan_item_container(
                    items, section, char_path.name,
                    f"{char_name} · {_tab_label(section)}", issues,
                    positioned=positioned, equipment=equipment, apply=apply, state=state)
            if apply and char_changed:
                backups.append(write_char_inventory(slot, inventory))
        except Exception as exc:
            _health_issue(issues, "error", "file_decode", char_path.name,
                          f"Character slot {slot}", f"File could not be decoded: {exc}")
            char_name = f"Slot {slot}"

        bag_path = SAVES / f"inventory_order_{slot}.hss"
        if not bag_path.exists() or bag_path.stat().st_size <= 50:
            continue
        try:
            bags = json.loads(decode_hss(bag_path))
            if not isinstance(bags, dict):
                raise ValueError("root payload is not an object")
            state["files"] += 1
            bags_changed = False
            for tab, items in bags.items():
                if not isinstance(items, dict):
                    _health_issue(issues, "error", "container_type", bag_path.name,
                                  f"{char_name} · {_tab_label(tab)}",
                                  "Item container is not an object and cannot be read safely.")
                    continue
                state["items"] += len(items)
                bags_changed |= _scan_item_container(
                    items, tab, bag_path.name, f"{char_name} · {_tab_label(tab)}", issues,
                    positioned=True, apply=apply, state=state)
            if apply and bags_changed:
                backups.append(write_bags(slot, bags))
        except Exception as exc:
            _health_issue(issues, "error", "file_decode", bag_path.name,
                          f"{char_name} · Bags", f"File could not be decoded: {exc}")

    errors = sum(1 for issue in issues if issue["severity"] == "error")
    warnings = sum(1 for issue in issues if issue["severity"] == "warning")
    fixable = sum(1 for issue in issues if issue["fixable"])
    vault_recovery_batches = []
    if Path(VAULT_DB_FILE).expanduser().resolve().exists():
        try:
            vault_recovery_batches = [
                {
                    "requestId": row.request_id,
                    "direction": row.direction,
                    "status": row.status,
                    "itemCount": row.item_count,
                    "error": row.error,
                    "recommendedAction": "preserve-vault-ownership",
                    "possibleDuplicate": True,
                }
                for row in vault_store().list_pending_transfer_batches()
            ]
        except Exception as exc:
            _health_issue(
                issues, "error", "vault_journal_unreadable",
                Path(VAULT_DB_FILE).name, "Infinite Vault",
                f"Vault transfer journal could not be inspected: {exc}",
            )
            errors += 1
    return {
        "summary": {"files": state["files"], "items": state["items"],
                    "errors": errors, "warnings": warnings, "fixable": fixable},
        "issues": issues, "fixed": state["fixed"], "backups": backups,
        "recoveries": recoveries, "gameRunning": running,
        "vaultRecoveryBatches": vault_recovery_batches,
    }


def op_fix_save_health() -> dict:
    repaired = scan_save_health(apply=True)
    if repaired.get("err"):
        return repaired
    after = scan_save_health(apply=False)
    after["fixed"] = repaired.get("fixed", 0)
    after["backups"] = repaired.get("backups", [])
    after["ok"] = (f"Applied {after['fixed']} safe repair(s)."
                   if after["fixed"] else "No safe repairs were needed.")
    return after


def find_owned_items(query: str) -> dict:
    def search_text(value) -> str:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        return re.sub(r"[_-]+", " ", text).casefold()

    needle = search_text((query or "").strip())
    if len(needle) < 2:
        return {"items": [], "total": 0, "limited": False}
    results = []
    char_names = {}

    def add(key, entry, location, target):
        if not isinstance(entry, dict) or not isinstance(entry.get("data"), dict):
            return
        try:
            item = resolve(str(key), entry["data"])
        except Exception:
            return
        display_name = (relic_disp(item.get("name")) if item.get("cls") == 16
                        else item.get("name", "Unknown item"))
        haystack = " ".join(search_text(value) for value in (
            item.get("name", ""), display_name, item.get("rar", ""),
            item.get("clsName", ""), location))
        if needle not in haystack:
            return
        results.append({
            "name": display_name, "rar": item.get("rar", "?"),
            "clsName": item.get("clsName", CLASS_NAMES.get(item.get("cls"), "")),
            "stack": item.get("stack") if item.get("cls") in (12, 13, 14, 15) else None,
            "spr": item.get("spr"), "cid": item.get("cid"),
            "key": str(key), "location": location, "target": target,
        })

    stash_path = SAVES / "stash.hss"
    if stash_path.exists():
        try:
            stash = json.loads(decode_hss(stash_path))
            for tab, items in stash.items():
                if tab == "stash_tab_data" or not isinstance(items, dict):
                    continue
                for key, entry in items.items():
                    add(key, entry, f"Shared Stash · {_tab_label(tab)}",
                        {"view": "stash", "tab": tab, "key": str(key)})
        except Exception:
            pass

    for slot, char_path in _character_paths():
        try:
            char_text, inventory = _decode_character_inventory(char_path)
            char_name = _character_name(char_text, slot)
            char_names[slot] = char_name
            for section in ("equipped_items", "potions", "personal_stash"):
                items = inventory.get(section, {})
                if not isinstance(items, dict):
                    continue
                for key, entry in items.items():
                    section_label = _tab_label(section)
                    if section == "equipped_items" and isinstance(entry, dict):
                        data = entry.get("data", {})
                        if _valid_number(data.get("g")):
                            section_label = SLOT_NAMES.get(int(float(data["g"])), section_label)
                    add(key, entry, f"{char_name} · {section_label}",
                        {"view": "char", "slot": slot, "section": section,
                         "tab": section, "key": str(key)})
        except Exception:
            continue

        bag_path = SAVES / f"inventory_order_{slot}.hss"
        if not bag_path.exists() or bag_path.stat().st_size <= 50:
            continue
        try:
            bags = json.loads(decode_hss(bag_path))
            for tab, items in bags.items():
                if not isinstance(items, dict):
                    continue
                for key, entry in items.items():
                    add(key, entry, f"{char_names.get(slot, f'Slot {slot}')} · {_tab_label(tab)}",
                        {"view": "char", "slot": slot, "section": "bag",
                         "tab": tab, "key": str(key)})
        except Exception:
            pass

    vault_path = Path(VAULT_DB_FILE).expanduser().resolve()
    if vault_path.exists():
        try:
            for record in vault_store().search_items(query, status="available", limit=500):
                item = _vault_item_payload(record)
                location = f"Infinite Vault · {record.collection_name}"
                results.append({
                    "id": record.id,
                    "name": item["name"], "rar": item["rar"],
                    "clsName": item["clsName"], "stack": item["stack"],
                    "spr": item["spr"], "cid": item["cid"],
                    "key": record.source_item_key or "", "location": location,
                    "target": {"view": "vault", "collectionId": record.collection_id,
                               "itemId": record.id},
                })
        except VaultError:
            # A damaged/newer vault must not break save-file search. The Vault
            # screen reports its precise fail-closed error separately.
            pass

    results.sort(key=lambda row: (row["name"].casefold(), row["location"].casefold()))
    total = len(results)
    return {"items": results[:500], "total": total, "limited": total > 500}


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _allowed_authority(self) -> tuple[str, str]:
        port = int(self.server.server_address[1])
        return (f"127.0.0.1:{port}", f"localhost:{port}")

    def _require_local_host(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._allowed_authority():
            # Consume a small rejected POST body before closing the socket.
            # Otherwise Windows may send a TCP reset before the browser sees
            # the 403 response because unread request bytes remain queued.
            if getattr(self, "command", None) == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= MAX_POST_BYTES:
                        self.rfile.read(length)
                except (TypeError, ValueError, OSError):
                    pass
            self._json({"err": "request rejected: invalid local Host"}, 403)
            return False
        return True

    def _read_json_post(self):
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._json({"err": "valid Content-Length required"}, 411)
            return None
        if length < 0:
            self._json({"err": "invalid Content-Length"}, 400)
            return None
        if length > MAX_POST_BYTES:
            self._json({"err": "request body is too large"}, 413)
            return None
        raw_body = self.rfile.read(length)
        media_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self._json({"err": "Content-Type must be application/json"}, 415)
            return None
        if self.headers.get(EDITOR_REQUEST_HEADER) != "1":
            self._json({"err": "request rejected: missing editor request header"}, 403)
            return None
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").strip().lower()
            if origin.strip().lower() != f"http://{host}":
                self._json({"err": "request rejected: invalid Origin"}, 403)
                return None
        try:
            body = json.loads(raw_body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json({"err": "malformed JSON request"}, 400)
            return None
        if not isinstance(body, dict):
            self._json({"err": "JSON request body must be an object"}, 400)
            return None
        return body

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._require_local_host():
            return
        u = urlparse(self.path)
        if u.path == "/":
            b = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/instance":
            # Lightweight startup handshake: do not enumerate saves or invoke
            # tasklist merely to decide whether a second launcher can reuse us.
            self._json({
                "application": APPLICATION_ID,
                "version": APP_VERSION,
                "pid": os.getpid(),
            })
        elif u.path == "/api/overview":
            self._json({
                "application": APPLICATION_ID,
                "chars": list_characters(),
                "gameRunning": game_running(),
                "version": APP_VERSION,
                "profile": CATALOG_PROFILE,
                "catalogItems": sum(1 for row in CAT if row.get("available", True)),
                "s10VerifiedAdditions": sum(1 for row in CAT if row.get("s10Verified")),
                "gameBuild": _editor_runtime_build_status(),
                "rollProfiles": ROLL_DB.summary(),
                "diceSkillTargets": DICE_SKILL_DB.summary(),
                "torchClassTargets": TORCH_CLASS_DB.summary(),
                "exactTooltips": (
                    TOOLTIP_MODEL_DB.summary()
                    if TOOLTIP_MODEL_DB is not None
                    else {
                        "available": False,
                        "code": "missing",
                        "message": "Exact-tooltip module is unavailable.",
                    }
                ),
            })
        elif u.path == "/api/catalog":
            self._json(catalog_api_rows())
        elif u.path == "/api/dice-skills":
            profile_id = parse_qs(u.query).get("profile", [""])[0]
            selector = DICE_SKILL_DB.selector(profile_id)
            if selector is None:
                self._json({"err": "unknown dice skill profile"}, 404)
            elif not DICE_SKILL_DB.available:
                self._json({"err": selector["message"], "selector": selector})
            else:
                self._json({
                    "selector": selector,
                    "targets": DICE_SKILL_DB.targets(profile_id),
                })
        elif u.path == "/api/skill-targets":
            profile_id = parse_qs(u.query).get("profile", [""])[0]
            database = skill_target_database(profile_id)
            selector = database.selector(profile_id) if database is not None else None
            if selector is None:
                self._json({"err": "unknown skill/class target profile"}, 404)
            elif not database.available:
                self._json({"err": selector["message"], "selector": selector})
            else:
                self._json({
                    "selector": selector,
                    "targets": database.targets(profile_id),
                })
        elif u.path.startswith("/api/char/"):
            self._json(read_char(int(u.path.rsplit("/", 1)[1])))
        elif u.path == "/api/stash":
            try:
                self._json(read_stash())
            except Exception as exc:
                self._json({
                    "err": f"Shared Stash unavailable: {exc}",
                    "code": "stash_hss_unreadable",
                    "recoveryAvailable": _stash_recovery_available(),
                }, 500)
        elif u.path == "/api/vault/meta":
            try:
                self._json(vault_meta())
            except Exception as exc:
                self._json({
                    "err": f"Infinite Vault unavailable: {exc}",
                    "code": "stash_hss_unreadable",
                    "recoveryAvailable": _stash_recovery_available(),
                }, 500)
        elif u.path == "/api/vault/bulk-preview":
            self._json(vault_bulk_preview(parse_qs(u.query, keep_blank_values=True)))
        elif u.path == "/api/vault/items":
            try:
                self._json(vault_items(parse_qs(u.query, keep_blank_values=True)))
            except Exception as exc:
                self._json({"err": f"Infinite Vault query failed: {exc}"}, 500)
        elif u.path == "/api/sets":
            self._json(SETS)
        elif u.path == "/api/runewords":
            self._json(runeword_api_rows())
        elif u.path == "/api/relics":
            self._json(relic_list())
        elif u.path == "/api/stackables":
            self._json(stackable_list())
        elif u.path == "/api/s10access":
            self._json(s10_access_list())
        elif u.path == "/api/loadouts":
            self._json(load_loadouts())
        elif u.path == "/api/backups":
            self._json(list_backups())
        elif u.path == "/api/health":
            self._json(scan_save_health())
        elif u.path == "/api/find":
            query = parse_qs(u.query).get("q", [""])[0]
            self._json(find_owned_items(query))
        elif u.path.startswith("/icons/"):
            name = u.path.rsplit("/", 1)[1]
            p = ICONS / name
            if re.fullmatch(r"[A-Za-z0-9_.-]+\.png", name) and p.exists():
                b = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self._json({"err": "not found"}, 404)

    def _dispatch_post(self, path: str, body: dict) -> None:
        if path == "/api/add":
            self._json(op_add(body))
        elif path == "/api/move":
            self._json(op_move(body))
        elif path == "/api/addmany":
            self._json(op_addmany(body))
        elif path == "/api/stash/fill":
            self._json(op_fill_stash(body))
        elif path == "/api/forge":
            self._json(op_forge(body))
        elif path == "/api/loadout":
            self._json(op_loadout(body))
        elif path == "/api/restorebak":
            self._json(op_restore_backup(body))
        elif path == "/api/sockets":
            self._json(op_sockets(body))
        elif path == "/api/makerelic":
            self._json(op_make_relic(body))
        elif path == "/api/makestackable":
            self._json(op_make_stackable(body))
        elif path == "/api/makes10access":
            self._json(op_make_s10_access(body))
        elif path == "/api/modify":
            self._json(op_modify(body))
        elif path == "/api/delete":
            self._json(op_delete(body))
        elif path == "/api/health/fix":
            self._json(op_fix_save_health())
        elif path == "/api/health/recover":
            self._json(op_recover_stash_hss(body))
        elif path == "/api/vault/deposit":
            self._json(op_vault_deposit(body))
        elif path == "/api/vault/withdraw":
            self._json(op_vault_withdraw(body))
        elif path == "/api/vault/bulk":
            self._json(op_vault_bulk(body))
        elif path == "/api/vault/batch/resolve":
            self._json(op_vault_resolve_batch(body))
        elif path == "/api/vault/collections":
            self._json(op_vault_collections(body))
        elif path == "/api/vault/item":
            self._json(op_vault_item(body))
        else:
            self._json({"err": "not found"}, 404)

    def do_POST(self):
        if not self._require_local_host():
            return
        body = self._read_json_post()
        if body is None:
            return
        path = urlparse(self.path).path
        ordinary_save_routes = {
            "/api/add", "/api/move", "/api/addmany", "/api/stash/fill", "/api/forge",
            "/api/restorebak", "/api/sockets",
            "/api/makerelic", "/api/makestackable", "/api/makes10access",
            "/api/modify", "/api/delete", "/api/health/fix",
        }
        hss_recovery_route = path == "/api/health/recover"
        mutates_save = path in ordinary_save_routes or hss_recovery_route or (
            path == "/api/loadout" and body.get("action") == "apply"
        )
        try:
            with SAVE_WRITE_LOCK:
                peer_error = _active_peer_editor_error()
                if peer_error:
                    self._json({"err": peer_error})
                    return
                lock = _exclusive_save_file(SAVES / "stash.hss") if mutates_save else nullcontext()
                with lock:
                    peer_error = _active_peer_editor_error()
                    if peer_error:
                        self._json({"err": peer_error})
                        return
                    if hss_recovery_route:
                        blocked = _hss_recovery_mutation_gate()
                        if blocked:
                            self._json(blocked)
                            return
                    elif mutates_save:
                        blocked = _vault_save_mutation_gate(stash_lock_held=True)
                        if blocked:
                            self._json(blocked)
                            return
                    self._dispatch_post(path, body)
        except Exception as exc:
            self._json({"err": f"error: {exc}"}, 500)


HTML = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Hero Siege Item Editor — Season 10</title>
<style>
:root{--bg:#16090b;--panel:#1f1416;--card:#2a1b1e;--gold:#c9a227;--tx:#e8d9c0;--line:#3a2326}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);display:flex;height:100vh;overflow:hidden}
#left{width:230px;background:var(--panel);border-right:1px solid var(--line);padding:12px;overflow-y:auto}
#mid{flex:1;padding:14px;overflow-y:auto}
#right{width:380px;background:var(--panel);border-left:1px solid var(--line);padding:12px;display:flex;flex-direction:column}
h1{font-size:17px;color:var(--gold);margin:0 0 10px;letter-spacing:1px}
h2{font-size:14px;color:var(--gold);margin:14px 0 6px}
.charbtn,.tabbtn{display:block;width:100%;text-align:left;background:var(--card);border:1px solid var(--line);color:var(--tx);padding:7px 9px;margin:3px 0;cursor:pointer;border-radius:4px}
.charbtn:hover,.tabbtn:hover{border-color:var(--gold)}
.charbtn.sel,.tabbtn.sel{border-color:var(--gold);background:#33211c}
.muted{color:#937f6a;font-size:12px}
#status{padding:6px 9px;border-radius:4px;background:#241317;margin-bottom:8px;font-size:12px;border:1px solid var(--line)}
#status.warn{color:#ff9c5b;border-color:#7a4a22}
.version{font-size:10px;color:#7ddcff;border:1px solid #28516a;background:#10202b;border-radius:999px;padding:2px 7px;display:inline-block;margin-bottom:8px}
.grid{position:relative;background:#120a0c;border:1px solid var(--line);border-radius:4px;margin:6px 0 14px}
.cell{position:absolute;border:1px solid #221317}
.item{position:absolute;border-radius:3px;padding:1px;font-size:9px;overflow:hidden;cursor:pointer;border:1px solid;display:flex;align-items:center;justify-content:center;text-align:center}
.item:hover{filter:brightness(1.35);z-index:5}
.item img{max-width:100%;max-height:100%;image-rendering:pixelated;pointer-events:none}
.res img{width:24px;height:24px;object-fit:contain;image-rendering:pixelated;vertical-align:middle;margin-right:5px}
.slot img{width:30px;height:30px;object-fit:contain;image-rendering:pixelated;float:right}
.stk{position:absolute;right:2px;bottom:1px;color:#fff;text-shadow:0 0 3px #000,0 0 3px #000;font-size:10px;font-weight:bold}
.slotrow{display:flex;flex-wrap:wrap;gap:6px}
.slot{width:118px;min-height:58px;background:var(--card);border:1px solid var(--line);border-radius:4px;padding:5px;font-size:11px;cursor:pointer}
.slot:hover{border-color:var(--gold)}
.slot .sn{color:#937f6a;font-size:10px}
input,select{background:#140c0e;color:var(--tx);border:1px solid var(--line);border-radius:4px;padding:6px}
#q{width:100%}
#results{flex:1;overflow-y:auto;margin-top:8px}
.res{padding:5px 7px;border:1px solid var(--line);border-radius:4px;margin:3px 0;cursor:pointer;font-size:12px}
.res:hover{border-color:var(--gold)}
.res.sel{background:#33211c;border-color:var(--gold)}
.r-Satanic{color:#ff5050}.r-Heroic{color:#54e87a}.r-Angelic{color:#ffe080}.r-Unholy{color:#c77dff}
.r-Normal{color:#cfcfcf}.r-Superior{color:#7db5ff}.r-Rare{color:#ffd84d}.r-Legendary{color:#ff9c40}
.r-Mythic{color:#5bd6d6}.r-Runeword{color:#b0a8ff}
.b-Satanic{background:#3a1414;border-color:#ff5050}.b-Heroic{background:#11331c;border-color:#54e87a}
.b-Angelic{background:#3a3416;border-color:#ffe080}.b-Unholy{background:#2c1840;border-color:#c77dff}
.b-Normal{background:#26211f;border-color:#777}.b-Superior{background:#16263a;border-color:#7db5ff}
.b-Rare{background:#383011;border-color:#ffd84d}.b-Legendary{background:#3a2410;border-color:#ff9c40}
.b-Mythic{background:#0f3030;border-color:#5bd6d6}.b-Runeword{background:#1d1a38;border-color:#b0a8ff}.b-_{background:#222;border-color:#555}
button.act{background:#5a3413;color:#ffd9a0;border:1px solid #8a5a26;border-radius:4px;padding:8px;margin-top:8px;cursor:pointer;font-size:13px}
button.act:hover{background:#6f421a}
#msg{font-size:12px;margin-top:6px;min-height:30px}
.flex{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
/* ---- paper doll (oyundaki Inventory ekrani) ---- */
#doll{display:flex;gap:14px;align-items:flex-start;background:linear-gradient(180deg,#241114,#1a0c0e);border:2px solid #4a262b;border-radius:8px;padding:16px;width:fit-content}
.relcol{display:flex;flex-direction:column;gap:8px}
.dmain{display:flex;flex-direction:column;gap:10px;align-items:center}
.drow{display:flex;gap:10px;align-items:flex-start;justify-content:center}
.dslot{position:relative;background:#160b0d;border:2px solid #4a262b;border-radius:5px;display:flex;align-items:center;justify-content:center;cursor:pointer}
.dslot:hover{border-color:var(--gold)}
.dslot .lbl{position:absolute;top:-9px;left:4px;font-size:9px;color:#937f6a;background:#1a0c0e;padding:0 4px;border-radius:3px;white-space:nowrap;z-index:2}
.dslot img{max-width:88%;max-height:88%;image-rendering:pixelated}
.dslot.drophl-ok{border-color:#54e87a;background:rgba(84,232,122,.12)}
.dslot.drophl-no{border-color:#ff5050;background:rgba(255,80,80,.12)}
.wpanel{display:flex;flex-direction:column;align-items:center}
.wtabs{display:flex;gap:2px;margin-bottom:3px}
.wtabs button{background:#2a1014;color:#937f6a;border:1px solid #4a262b;border-bottom:none;padding:1px 12px;font-size:11px;cursor:pointer;border-radius:4px 4px 0 0}
.wtabs button.on{background:#5a1c22;color:var(--gold)}
.dcharms h3,.dbag h3{font-size:12px;color:var(--gold);margin:0 0 5px}
.bagtabs{display:flex;gap:4px;margin:16px 0 6px;flex-wrap:wrap}
.bagtabs button{background:#2a1518;color:#b9a58c;border:1px solid var(--line);padding:6px 16px;cursor:pointer;border-radius:4px 4px 0 0;font-size:12px;letter-spacing:.5px}
.bagtabs button.on{background:#4a1c22;color:var(--gold);border-color:var(--gold)}
#filters{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.fitem label{display:block;font-size:10px;color:#937f6a;margin-bottom:2px;letter-spacing:.5px;text-transform:uppercase}
.fitem select{width:100%}
.setcard{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin:8px 0;max-width:760px}
.setcard h3{margin:0 0 6px;font-size:14px;color:#54e87a}
.setcard h3 .muted{font-size:11px}
.spiece{display:inline-flex;align-items:center;gap:6px;background:#1a0e10;border:1px solid var(--line);border-radius:4px;padding:4px 8px;margin:3px 4px 3px 0;font-size:12px}
.spiece img{width:22px;height:22px;object-fit:contain;image-rendering:pixelated}
.spiece.own{border-color:#3da55e}
.spiece.miss{opacity:.45}
.setadd{background:#234a2a;color:#9fe8b0;border:1px solid #3da55e;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:12px;margin-left:8px}
.setadd:hover{background:#2d5e36}
.setadd[disabled]{opacity:.4;cursor:default}
.perfect-pill{display:inline-flex;align-items:center;gap:5px;margin-top:7px;padding:4px 8px;border:1px solid #3da55e;border-radius:999px;background:#122a1a;color:#74ee98;font-size:11px;font-weight:700;letter-spacing:.4px}
#ctxmenu{position:fixed;z-index:120;display:none;background:#1c1013;border:1px solid #6a3a40;border-radius:6px;box-shadow:0 4px 16px rgba(0,0,0,.7);min-width:170px}
#ctxmenu div{padding:8px 14px;font-size:13px;cursor:pointer}
#ctxmenu div:hover{background:#3a1c22;color:var(--gold)}
#ctxmenu div.danger:hover{background:#4a1414;color:#ff7060}
#sockmodal{position:fixed;inset:0;z-index:150;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.6)}
#sockbox{background:#1c1013;border:1px solid #6a3a40;border-radius:8px;padding:18px 22px;min-width:420px;max-height:80vh;overflow-y:auto}
#sockbox h3{margin:0 0 4px;color:var(--gold);font-size:15px}
.sockrow{display:flex;align-items:center;gap:8px;margin:6px 0}
.sockrow img{width:24px;height:24px;image-rendering:pixelated}
.sockrow input{flex:1}
.sockrow button{background:#3a1c22;color:#c9a;border:1px solid var(--line);border-radius:4px;cursor:pointer;padding:4px 9px}
.skill-search{width:100%;margin:10px 0 8px;padding:8px 10px}
.skill-select{width:100%;min-height:310px;padding:5px}
.skill-current{margin:8px 0;padding:8px 10px;border:1px solid #30445c;border-radius:7px;background:#0b121c;color:#9ddff0;font-size:12px}
.skill-proof{margin:8px 0;color:#7f8da1;font-size:11px;line-height:1.5}
.rwcard{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 14px;margin:6px 0;max-width:1120px;display:grid;grid-template-columns:220px minmax(180px,1fr) minmax(250px,340px) 92px;align-items:center;gap:12px}
.tipbar{background:#241a2e;border:1px solid #4a3a6a;border-radius:6px;padding:7px 12px;margin:0 0 12px;font-size:12px;color:#bfb3d6;max-width:920px}
.tipbar b{color:#d8c9ff}
.jlrow{display:flex;align-items:center;gap:10px;margin:7px 0;max-width:760px}
.jlrow label{min-width:90px;color:#bfb3d6;font-size:13px}
.jlrow select,.jlrow input{background:#120a0c;color:var(--tx);border:1px solid var(--line);border-radius:4px;padding:5px 8px}
.rwhead{display:flex;flex-direction:column;gap:2px;min-width:0}
.rwcard .rwname{font-weight:bold;cursor:default;line-height:1.15}
.rwtarget{font-size:11px;color:var(--mut);line-height:1.25;white-space:normal}
.rwrunes{display:flex;gap:5px;flex-wrap:wrap}
.rwrune{display:inline-flex;align-items:center;gap:3px;background:#1a0e10;border:1px solid #4a3a26;border-radius:4px;padding:2px 6px;font-size:11px;color:#d8c9a0}
.rwrune img{width:18px;height:18px;image-rendering:pixelated}
.rwbase{width:100%;min-width:0;background:#120a0c;color:var(--tx);border:1px solid var(--line);border-radius:4px;padding:6px 8px;font-size:11px}
.forgebtn{background:#4a2a13;color:#ffd9a0;border:1px solid #8a5a26;border-radius:4px;padding:6px 0;cursor:pointer;font-size:12px;width:100%}
.forgebtn:hover{background:#6f421a}
/* ---- item tooltip ---- */
#tip{position:fixed;z-index:99;display:none;background:rgba(12,5,7,.97);border:1px solid #6a3a40;border-radius:6px;padding:10px 14px;max-width:330px;pointer-events:none;box-shadow:0 4px 18px rgba(0,0,0,.7)}
#tip .tname{font-size:14px;font-weight:bold;margin-bottom:2px}
#tip .ttype{font-size:11px;color:#937f6a;margin-bottom:6px}
#tip .tstat{font-size:12px;color:#8fb7ff;line-height:1.5}
#tip .tstat b{color:#fff;font-weight:600}
#tip .tset{font-size:11px;color:#54e87a;margin-top:5px}
.game-tooltip{min-width:270px;max-width:380px;color:#dce6f2}
.gtt-alias{margin:0 0 3px;color:#f4c86c;font-size:10px;font-weight:800;letter-spacing:.7px;text-transform:uppercase}
.gtt-title{font-size:15px;font-weight:850;line-height:1.2;text-align:center;text-shadow:0 2px 8px rgba(0,0,0,.75)}
.gtt-type,.gtt-requirement{text-align:center;color:#91a0b4;font-size:10px;line-height:1.4}.gtt-requirement{color:#c4ccd7;margin-top:2px}
.gtt-rule{height:1px;margin:8px 0;background:linear-gradient(90deg,transparent,#526177,transparent)}
.gtt-stat{display:grid;grid-template-columns:minmax(44px,auto) minmax(0,1fr);gap:6px;padding:2px 5px;border-radius:4px;color:#9fc0ff;font-size:11px;line-height:1.35}
.gtt-stat b{color:#f2f6fb;font-weight:750;text-align:right}.gtt-stat.unresolved b{color:#8896a9}.gtt-stat.missing{color:#708096;font-style:italic}.gtt-stat.missing b{color:#ffab72}.gtt-stat.gtt-diff{background:rgba(255,174,76,.13);box-shadow:inset 2px 0 #ffa64f}
.gtt-identity,.gtt-socket{padding:2px 5px;color:#c7a4ff;font-size:10px}.gtt-identity b{color:#e0d0ff}.gtt-empty{padding:7px;color:#7f8da0;text-align:center;font-size:10px}
.gtt-editor-meta{margin-top:8px;padding-top:7px;border-top:1px solid #344156;color:#8291a5;font-size:9px;line-height:1.5}
.gtt-editor-meta b{color:#b8c7d8}.gtt-exact{color:#69e0ad!important}.gtt-partial{color:#ffb36f!important}.gtt-fingerprint{font-family:Consolas,monospace;letter-spacing:.6px}
.vault-compare-modal{position:fixed;z-index:110;inset:0;display:grid;place-items:center;padding:24px;background:rgba(3,7,12,.84);backdrop-filter:blur(7px)}
.vault-compare-dialog{width:min(920px,calc(100vw - 48px));max-height:calc(100vh - 48px);overflow:auto;padding:17px;border:1px solid #3a4d67;border-radius:14px;background:#0d141f;box-shadow:0 28px 75px rgba(0,0,0,.72)}
.vault-compare-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:13px}.vault-compare-head h3{margin:0;font-size:16px}.vault-compare-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px}
.vault-compare-column{min-width:0;padding:13px;border:1px solid #2d3d52;border-radius:11px;background:linear-gradient(145deg,#131d2a,#090f18)}.vault-compare-column .game-tooltip{max-width:none}.vault-compare-close{min-width:74px}

/* ========================================================================
   S10 MODERN VAULT UI — visual layer only; inventory/save logic is unchanged
   ======================================================================== */
:root{
  --bg:#080c13;--panel:#101722;--card:#151e2b;--gold:#f1b84b;--tx:#ecf1f7;
  --line:#283548;--mut:#8390a3;--cyan:#36c8e8;--danger:#ff5f65;
  --shadow:0 18px 45px rgba(0,0,0,.34);--soft:rgba(255,255,255,.035)
}
*{scrollbar-width:thin;scrollbar-color:#40516a #0a1019}
body{
  display:grid;grid-template-columns:264px minmax(560px,1fr) 410px;
  grid-template-rows:76px minmax(0,1fr);height:100vh;min-width:1040px;
  font:14px/1.48 Inter,Bahnschrift,'Segoe UI',sans-serif;color:var(--tx);
  background:
    radial-gradient(circle at 42% -15%,rgba(54,200,232,.12),transparent 35%),
    radial-gradient(circle at 95% 8%,rgba(241,184,75,.09),transparent 28%),
    linear-gradient(145deg,#070b12 0%,#0a111b 55%,#080c13 100%);
  overflow:hidden;transition:grid-template-columns .26s cubic-bezier(.2,.8,.2,1)
}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.13;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.02) 1px,transparent 1px);background-size:32px 32px}
#topbar{grid-column:1/-1;grid-row:1;position:relative;z-index:30;display:flex;align-items:center;gap:22px;padding:0 20px;background:linear-gradient(90deg,rgba(12,18,28,.97),rgba(17,25,37,.96));border-bottom:1px solid #2d3b50;box-shadow:0 10px 30px rgba(0,0,0,.3)}
#topbar:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;background:linear-gradient(90deg,transparent,var(--cyan),var(--gold),transparent);opacity:.55}
.brand{display:flex;align-items:center;gap:12px;min-width:222px}
.brand-rune{width:42px;height:42px;display:grid;place-items:center;position:relative;border:1px solid rgba(241,184,75,.62);border-radius:11px;background:radial-gradient(circle at 35% 25%,#2b3a4f,#111923 68%);color:#ffd57b;font-weight:900;letter-spacing:-1px;box-shadow:inset 0 0 18px rgba(241,184,75,.1),0 0 22px rgba(241,184,75,.08);transform:rotate(45deg)}
.brand-rune span{transform:rotate(-45deg)}
.brand-title{font-size:15px;font-weight:800;letter-spacing:1.5px;color:#f6f8fb}.brand-title small{display:block;margin-top:2px;color:#7e8da2;font-size:9px;font-weight:600;letter-spacing:2.7px}
.top-health{flex:1;display:flex;justify-content:center}
#status{margin:0;display:flex;align-items:center;gap:9px;min-width:265px;justify-content:center;padding:8px 14px;border-radius:999px;background:rgba(32,167,119,.09);border:1px solid rgba(55,216,158,.28);color:#79e2ba;text-transform:uppercase;letter-spacing:.8px;font-size:10px;font-weight:700;white-space:nowrap}
#status:before{content:"";width:7px;height:7px;border-radius:50%;background:#52dda9;box-shadow:0 0 0 4px rgba(82,221,169,.1),0 0 13px rgba(82,221,169,.65)}
#status.warn{color:#ffb46e;border-color:rgba(255,153,76,.4);background:rgba(255,132,45,.09)}#status.warn:before{background:#ff9454;box-shadow:0 0 0 4px rgba(255,148,84,.11),0 0 13px rgba(255,148,84,.65)}
.top-actions{display:flex;align-items:center;justify-content:flex-end;gap:10px;min-width:330px}
.version{margin:0;padding:6px 10px;color:#8bdff1;background:rgba(54,200,232,.08);border-color:rgba(54,200,232,.28);font-size:9px;letter-spacing:.65px;max-width:245px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.iconbtn{height:34px;padding:0 12px;border-radius:8px;border:1px solid #334258;background:#151e2b;color:#cad4df;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.45px}.iconbtn:hover{border-color:#667b98;background:#1b2737;color:#fff}
#left{grid-column:1;grid-row:2;width:auto;padding:18px 14px 14px;background:linear-gradient(180deg,rgba(15,22,33,.98),rgba(10,15,24,.98));border-right:1px solid #263449;overflow-y:auto;box-shadow:12px 0 35px rgba(0,0,0,.15)}
#left h1{display:none}.side-caption{padding:0 8px 8px;color:#65738a;font-size:9px;font-weight:800;letter-spacing:2px}.side-caption.characters{margin-top:22px}
.tabbtn,.charbtn{position:relative;margin:4px 0;padding:10px 11px 10px 13px;border:1px solid transparent;border-radius:8px;background:transparent;color:#aab6c5;transition:background .16s,border-color .16s,color .16s,transform .16s}
.tabbtn{font-size:12px;font-weight:700;letter-spacing:.2px}.tabbtn:before,.charbtn:before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:2px;border-radius:2px;background:transparent}
.tabbtn:hover,.charbtn:hover{background:rgba(255,255,255,.045);border-color:#2c3a4e;color:#eef4fb;transform:translateX(2px)}
.tabbtn.sel,.charbtn.sel{background:linear-gradient(90deg,rgba(54,200,232,.13),rgba(54,200,232,.025));border-color:rgba(54,200,232,.25);color:#eafaff}.tabbtn.sel:before,.charbtn.sel:before{background:var(--cyan);box-shadow:0 0 10px rgba(54,200,232,.65)}
.charbtn{padding:9px 11px 9px 13px}.charbtn b{font-size:12px;color:#eef4fb}.charbtn .muted{color:#69788d;font-size:10px}
.side-foot{margin:22px 6px 2px;padding:11px;border:1px solid #263449;border-radius:9px;background:rgba(0,0,0,.14);font-size:10px;color:#67758a}.side-foot b{color:#96a5b9}
#mid{grid-column:2;grid-row:2;padding:22px 24px 40px;overflow:auto;min-width:0;position:relative}
#mid>h2,#mid>div>h2{font-size:20px;letter-spacing:.2px;color:#f4f7fa;margin:0 0 14px}
.welcome{min-height:calc(100vh - 124px);display:grid;place-items:center}.welcome-card{max-width:520px;text-align:center;padding:36px;border:1px solid #2b3a4f;border-radius:18px;background:linear-gradient(145deg,rgba(22,31,44,.86),rgba(12,18,28,.92));box-shadow:var(--shadow)}.welcome-mark{font-size:36px;color:var(--gold);filter:drop-shadow(0 0 14px rgba(241,184,75,.2))}.welcome-card h2{margin:8px 0 5px;font-size:22px}.welcome-card p{margin:0;color:var(--mut);font-size:12px}
#right{grid-column:3;grid-row:2;width:auto;padding:18px 16px 14px;background:linear-gradient(180deg,rgba(16,23,34,.98),rgba(10,16,25,.98));border-left:1px solid #263449;box-shadow:-12px 0 35px rgba(0,0,0,.14);min-width:0;overflow:hidden;transition:opacity .2s,transform .25s,padding .25s,border-color .25s}
body.catalog-collapsed{grid-template-columns:264px minmax(560px,1fr) 0}body.catalog-collapsed #right{opacity:0;transform:translateX(35px);padding-left:0;padding-right:0;border-color:transparent;pointer-events:none}
.catalog-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}.catalog-head h2{margin:0;color:#f4f7fa;font-size:17px}.catalog-head p{margin:2px 0 0;color:#68768a;font-size:10px}.catalog-badge{padding:4px 7px;border:1px solid rgba(241,184,75,.28);border-radius:999px;color:#e9bc68;background:rgba(241,184,75,.06);font-size:9px;font-weight:800;letter-spacing:.7px}
.searchbox{position:relative}.searchbox:before{content:"⌕";position:absolute;left:11px;top:5px;color:#617087;font-size:19px;z-index:1}.searchbox #q{height:38px;padding-left:34px;border-color:#2d3b50;background:#0b111a;border-radius:9px;font-size:12px}.searchbox #q:focus{outline:none;border-color:rgba(54,200,232,.6);box-shadow:0 0 0 3px rgba(54,200,232,.08)}
input,select{background:#0b111a;color:#dfe7f0;border-color:#2d3b50;border-radius:7px}input:focus,select:focus{outline:none;border-color:#54708f}
#filters{gap:8px;margin-top:11px}.fitem label{color:#69788e;font-size:8px;font-weight:800;letter-spacing:1.2px}.fitem select,.fitem input{height:33px;font-size:11px}
#results{margin:13px -4px 0;padding:0 4px 10px}.res{min-height:39px;display:flex;align-items:center;padding:7px 9px;margin:4px 0;border-color:#253246;border-radius:8px;background:rgba(255,255,255,.018);transition:transform .14s,border-color .14s,background .14s}.res:hover{transform:translateX(-2px);background:rgba(255,255,255,.045);border-color:#4a5c74}.res.sel{background:linear-gradient(90deg,rgba(241,184,75,.13),rgba(241,184,75,.025));border-color:rgba(241,184,75,.58);box-shadow:inset 3px 0 var(--gold)}.res img{width:28px;height:28px;margin-right:9px;filter:drop-shadow(0 3px 5px rgba(0,0,0,.5))}
#addzone{margin:0 -4px;padding:12px 4px 0!important;border-color:#29364a!important;background:linear-gradient(180deg,transparent,rgba(8,13,21,.7))}#selinfo{min-height:18px}#targetrow select{max-width:100%;flex:1}button.act{min-height:36px;padding:8px 14px;border-radius:8px;border-color:#8b6430;background:linear-gradient(180deg,#6f4a1f,#4e3218);color:#ffe2a7;font-weight:800;letter-spacing:.25px;box-shadow:0 5px 14px rgba(0,0,0,.2)}button.act:hover{background:linear-gradient(180deg,#855c28,#60401d);transform:translateY(-1px)}button.act:disabled{opacity:.38;transform:none;cursor:not-allowed}
.muted{color:var(--mut)}.tipbar{max-width:none;margin-bottom:17px;padding:10px 13px;border-color:#2f4058;background:linear-gradient(90deg,rgba(54,200,232,.07),rgba(143,92,220,.05));color:#91a0b4;border-radius:9px}.tipbar b{color:#c8e8f2}
#doll{border:1px solid #33445c;border-radius:15px;padding:22px;background:radial-gradient(circle at 50% 35%,rgba(54,200,232,.075),transparent 38%),linear-gradient(160deg,#151d29,#0c121c);box-shadow:var(--shadow),inset 0 0 35px rgba(0,0,0,.25)}
.dslot{background:linear-gradient(145deg,#0c121b,#111a26);border:1px solid #35465e;border-radius:8px;box-shadow:inset 0 0 12px rgba(0,0,0,.35)}.dslot:hover{border-color:#cf9f4b;box-shadow:inset 0 0 12px rgba(241,184,75,.07),0 0 12px rgba(241,184,75,.07)}.dslot .lbl{top:-8px;color:#78879b;background:#101722;border-radius:4px}.wtabs button{background:#0f1621;border-color:#334258;color:#77869a;border-radius:6px 6px 0 0}.wtabs button.on{background:#223146;color:#d8e8f5;border-color:#4a637f}
.grid{border-color:#35455c;border-radius:8px;background:linear-gradient(145deg,#090e16,#0d141e);box-shadow:inset 0 0 25px rgba(0,0,0,.35),0 8px 25px rgba(0,0,0,.16)}.cell{border-color:rgba(111,132,158,.105)}.item{border-radius:5px;transition:filter .12s,transform .12s;box-shadow:inset 0 0 9px rgba(255,255,255,.035),0 2px 5px rgba(0,0,0,.32)}.item:hover{filter:brightness(1.28);transform:translateY(-1px);z-index:5}
.bagtabs{gap:6px;margin-top:20px}.bagtabs button{padding:7px 13px;border-color:#2c3a4e;background:#0d141e;color:#7f8ea2;border-radius:7px;font-weight:700}.bagtabs button:hover{border-color:#536984;color:#cbd6e1}.bagtabs button.on{background:linear-gradient(180deg,#26374a,#1b2939);border-color:#55718f;color:#eef7ff;box-shadow:inset 0 -2px var(--cyan)}
.slot{width:126px;min-height:62px;padding:7px;border-color:#2e3d52;border-radius:8px;background:#111925}.slot:hover{border-color:#b88b41}.slot .sn{color:#68778c}
.setcard,.rwcard{max-width:none;border-color:#2b394d;border-radius:10px;background:linear-gradient(145deg,#141d29,#0e151f);box-shadow:0 7px 20px rgba(0,0,0,.13)}.setcard{padding:13px 15px}.setcard h3{color:#78e6b8}.spiece{border-color:#29374a;background:#0c121c;border-radius:7px}.rwcard{padding:11px 14px}.rwrune{background:#0c121c;border-color:#3e4d61;color:#c7d0dd;border-radius:6px}.forgebtn{border-radius:7px;background:#21374a;border-color:#42637c;color:#aee9f4}.forgebtn:hover{background:#2a485e}
.jlrow{max-width:900px;margin:10px 0}.jlrow label{color:#8694a8;font-size:12px;font-weight:700}.jlrow select,.jlrow input{min-height:36px;background:#0b111a;border-color:#2e3d52;border-radius:7px}
.access-hero{max-width:960px;margin-bottom:22px;padding:18px;border:1px solid rgba(241,184,75,.34);border-radius:14px;background:radial-gradient(circle at 90% 0,rgba(241,184,75,.11),transparent 38%),linear-gradient(145deg,rgba(27,37,51,.94),rgba(13,20,30,.96));box-shadow:0 14px 34px rgba(0,0,0,.2)}
.access-title{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:4px}.access-title h3{margin:0;color:#f7d894;font-size:16px}.access-title span{padding:4px 8px;border:1px solid rgba(82,221,169,.3);border-radius:999px;color:#73dfb5;background:rgba(82,221,169,.07);font-size:9px;font-weight:800;letter-spacing:.8px}
.access-grid{display:grid;grid-template-columns:repeat(2,minmax(260px,1fr));gap:12px;margin-top:14px}.access-card{padding:14px;border:1px solid #304158;border-radius:10px;background:linear-gradient(145deg,rgba(20,30,43,.96),rgba(11,17,26,.96))}.access-card h4{margin:0 0 3px;color:#f2f6fa;font-size:14px}.access-card p{margin:0 0 10px;color:#7f8da1;font-size:10px}.access-items{min-height:70px;margin-bottom:11px}.access-item{display:flex;justify-content:space-between;gap:12px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.045);font-size:11px}.access-item b{color:#c7eaf2}.access-item span{color:#718096;text-align:right}.access-card button{width:100%;margin:0}.section-rule{max-width:960px;margin:24px 0 18px;border:0;border-top:1px solid #29384d}
#ctxmenu,#sockbox{background:#101722;border-color:#3b4d65;box-shadow:0 22px 55px rgba(0,0,0,.55)}#ctxmenu{border-radius:9px;padding:5px}#ctxmenu div{border-radius:5px}#ctxmenu div:hover{background:#1d2a3a;color:#f5c96f}#sockmodal{backdrop-filter:blur(5px)}#sockbox{border-radius:13px}
#tip{background:rgba(7,11,18,.98);border-color:#3a4c64;border-radius:10px;box-shadow:0 20px 50px rgba(0,0,0,.58);backdrop-filter:blur(8px)}
.r-Satanic{color:#ff6268}.r-Heroic{color:#50ddba}.r-Angelic{color:#ffe18d}.r-Unholy{color:#c28cff}.r-Runeword{color:#9daeff}
.b-Satanic{background:#35171c;border-color:#ff6268}.b-Heroic{background:#0f302c;border-color:#50ddba}.b-Angelic{background:#37301a;border-color:#ffe18d}.b-Unholy{background:#2a1d3d;border-color:#c28cff}
.tool-intro{max-width:920px;margin-bottom:16px;padding:13px 15px;border:1px solid #2f4058;border-radius:10px;background:linear-gradient(110deg,rgba(54,200,232,.07),rgba(241,184,75,.035));color:#96a5b9;font-size:12px}.tool-intro b{color:#dce7f2}
.health-summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;max-width:920px;margin:0 0 15px}.health-metric{min-width:0;padding:12px 10px;border:1px solid #2d3b50;border-radius:10px;background:linear-gradient(145deg,#141d29,#0d141e)}.health-metric span{display:block;color:#718097;font-size:8px;font-weight:800;letter-spacing:.8px;text-transform:uppercase;white-space:nowrap}.health-metric b{display:block;margin-top:3px;font-size:22px;color:#eff5fb}.health-metric.error b{color:#ff7378}.health-metric.warn b{color:#ffc16e}.health-metric.fix b{color:#63dfb1}
.health-actions{display:flex;gap:9px;align-items:center;margin-bottom:15px}.health-state{font-size:11px;color:#7f8da1}.health-list{display:flex;flex-direction:column;gap:7px;max-width:920px}.health-issue{display:grid;grid-template-columns:72px minmax(0,1fr);gap:11px;padding:11px 13px;border:1px solid #2c3a4e;border-radius:9px;background:#101824}.health-issue.error{border-left:3px solid #ff6268}.health-issue.warning{border-left:3px solid #f1b84b}.health-sev{font-size:9px;font-weight:900;letter-spacing:1px;color:#8290a4}.health-issue.error .health-sev{color:#ff7d82}.health-issue.warning .health-sev{color:#f4c66f}.health-msg{color:#dce5ef;font-size:12px}.health-loc{margin-top:3px;color:#6f7e93;font-size:10px}.health-fixable{color:#63dfb1;font-weight:800}.health-clean{max-width:920px;padding:30px;text-align:center;border:1px solid rgba(82,221,169,.28);border-radius:14px;background:rgba(37,172,125,.06);color:#72dfb7}.health-clean b{display:block;font-size:18px;margin-bottom:4px}
.recovery-card{max-width:920px;margin:0 0 15px;padding:15px;border:1px solid rgba(255,161,79,.5);border-left:4px solid #ff9f4d;border-radius:11px;background:linear-gradient(145deg,rgba(84,43,17,.35),rgba(25,22,24,.8))}.recovery-card h3{margin:0 0 6px;color:#ffb36f;font-size:15px}.recovery-card p{margin:5px 0;color:#d9c7ba;font-size:12px}.recovery-facts{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.recovery-facts span{padding:5px 8px;border:1px solid #59412e;border-radius:7px;background:#191416;color:#e8d9c0;font-size:10px}.recovery-repairs{margin:8px 0 12px;padding-left:18px;color:#bfae9f;font-size:11px}.recovery-button{background:#6c351b!important;border-color:#c66c35!important;color:#fff1e7!important}.recovery-button:disabled{opacity:.45;cursor:not-allowed}
.finder-bar{display:grid;grid-template-columns:minmax(190px,1fr) minmax(105px,135px) minmax(105px,135px) auto;gap:8px;max-width:980px;margin-bottom:14px}.finder-bar input,.finder-bar select{height:39px;min-width:0}.finder-count{margin:4px 0 11px;color:#77869a;font-size:11px}.finder-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:8px;max-width:980px}.found-card{display:grid;grid-template-columns:38px minmax(0,1fr) auto;gap:10px;align-items:center;min-height:61px;padding:9px 10px;border:1px solid #2b394d;border-radius:10px;background:linear-gradient(145deg,#141d29,#0d141e)}.found-card img{width:34px;height:34px;object-fit:contain;image-rendering:pixelated;filter:drop-shadow(0 4px 7px rgba(0,0,0,.5))}.found-icon{width:34px;height:34px;display:grid;place-items:center;border:1px solid #34445a;border-radius:7px;color:#66768b}.found-name{font-weight:750;font-size:12px}.found-loc{margin-top:2px;color:#718096;font-size:10px}.locate-btn{padding:6px 9px;border:1px solid #3b526c;border-radius:7px;background:#162436;color:#a8ddec;cursor:pointer;font-size:10px;font-weight:800}.locate-btn:hover{border-color:#55bad0;background:#1d3348;color:#e2f9ff}.found-empty{max-width:920px;padding:28px;border:1px dashed #334258;border-radius:12px;text-align:center;color:#718096}
.found-pulse{position:relative!important;z-index:15!important;animation:foundPulse 1.2s ease-in-out 3;box-shadow:0 0 0 2px #53d7ef,0 0 24px rgba(83,215,239,.65)!important}@keyframes foundPulse{50%{filter:brightness(1.65);transform:scale(1.04)}}
.vault-toolbar{display:grid;grid-template-columns:minmax(180px,1fr) minmax(150px,220px) minmax(130px,180px) auto;gap:9px;align-items:center;max-width:1120px;margin:0 0 13px}.vault-toolbar input,.vault-toolbar select{height:39px;min-width:0}.vault-manage{display:flex;gap:7px;flex-wrap:wrap;max-width:1120px;margin-bottom:13px}.vault-mini{min-height:33px;padding:6px 10px;border:1px solid #33465e;border-radius:7px;background:#142033;color:#b9d8e4;cursor:pointer;font-size:10px;font-weight:800}.vault-mini:hover{border-color:#55bad0;color:#effcff}.vault-mini.danger{color:#ff999d;border-color:#643b45}.vault-summary{display:flex;align-items:center;justify-content:space-between;gap:12px;max-width:1120px;margin:8px 0 12px;color:#8190a5;font-size:11px}.vault-compare-bar{position:sticky;top:-12px;z-index:12;display:flex;align-items:center;gap:8px;max-width:1120px;margin:0 0 12px;padding:9px 10px;border:1px solid rgba(241,184,75,.32);border-radius:9px;background:rgba(15,23,34,.96);box-shadow:0 8px 22px rgba(0,0,0,.28)}.vault-compare-bar[hidden]{display:none}.vault-compare-slots{flex:1;min-width:0;overflow:hidden;color:#9caabd;font-size:10px;white-space:nowrap;text-overflow:ellipsis}.vault-compare-slots b{color:#f1c86f}.vault-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(265px,1fr));gap:10px;max-width:1120px}.vault-card{position:relative;display:grid;grid-template-columns:54px minmax(0,1fr);gap:11px;min-height:92px;padding:12px;border:1px solid #2e3f55;border-radius:11px;background:radial-gradient(circle at 0 0,rgba(54,200,232,.055),transparent 38%),linear-gradient(145deg,#151f2d,#0d151f);box-shadow:0 8px 22px rgba(0,0,0,.16);transition:border-color .14s,transform .14s}.vault-card:hover{border-color:#536f8d;transform:translateY(-1px)}.vault-card.compare-selected{border-color:#e0ad52;box-shadow:0 0 0 1px rgba(241,184,75,.32),0 9px 25px rgba(0,0,0,.3)}.vault-card img,.vault-card-icon{width:52px;height:52px;object-fit:contain;image-rendering:pixelated;filter:drop-shadow(0 5px 8px rgba(0,0,0,.55))}.vault-card-icon{display:grid;place-items:center;border:1px solid #354860;border-radius:9px;color:#6c7f96;font-size:20px}.vault-card-copy{min-width:0;padding-right:52px}.vault-alias{margin-bottom:2px;color:#f2c76b;font-size:12px;font-weight:850;line-height:1.2}.vault-name{font-weight:800;font-size:13px;line-height:1.2}.vault-alias+.vault-name{font-size:10px;font-weight:700}.vault-fingerprint{font-family:Consolas,monospace;color:#617086}.vault-card-tool{position:absolute;top:7px;width:25px;height:25px;padding:0;border:1px solid #3a4d65;border-radius:6px;background:#111b28;color:#90a5bb;cursor:pointer;font-size:11px;font-weight:850}.vault-card-tool:hover{border-color:#efbd60;color:#ffe1a0}.vault-card-tool.name{right:38px}.vault-card-tool.compare{right:7px}.vault-card-tool.compare.on{border-color:#efbd60;background:#503a18;color:#ffe3a5}.vault-meta{margin-top:4px;color:#718198;font-size:10px;line-height:1.45}.vault-actions{grid-column:1/-1;display:flex;gap:6px;justify-content:flex-end;margin-top:2px}.vault-return{padding:6px 9px;border:1px solid rgba(82,221,169,.38);border-radius:7px;background:rgba(37,172,125,.09);color:#85e7bf;cursor:pointer;font-size:10px;font-weight:800}.vault-return:hover{background:rgba(37,172,125,.16);border-color:#52dda9}.vault-return:disabled{opacity:.4;cursor:not-allowed}.vault-pager{display:flex;justify-content:center;gap:8px;max-width:1120px;margin:16px 0}.vault-empty{max-width:1120px;padding:38px 24px;border:1px dashed #34465d;border-radius:13px;text-align:center;color:#75869c}.vault-warning{max-width:1120px;margin-bottom:13px;padding:10px 13px;border:1px solid rgba(255,153,76,.35);border-radius:9px;background:rgba(255,132,45,.07);color:#ffb47d;font-size:11px}.unique-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:7px;max-width:1120px;margin-bottom:16px}.unique-card{display:grid;grid-template-columns:34px minmax(0,1fr);gap:8px;align-items:center;min-height:54px;padding:8px;border:1px solid #2b3b50;border-radius:8px;background:#101923;cursor:pointer}.unique-card:hover{border-color:#526b88}.unique-card img{width:32px;height:32px;object-fit:contain;image-rendering:pixelated}.unique-card .muted{font-size:9px}
.vault-bulk-panel{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:12px;align-items:center;max-width:1120px;margin:0 0 13px;padding:12px 13px;border:1px solid rgba(54,200,232,.28);border-radius:11px;background:linear-gradient(145deg,rgba(24,55,75,.32),rgba(16,24,36,.88))}.vault-bulk-copy b{display:block;color:#dff8ff;font-size:12px}.vault-bulk-copy span{display:block;margin-top:2px;color:#7f91a7;font-size:10px}.vault-bulk-button{min-height:37px;padding:7px 11px;border:1px solid rgba(82,221,169,.45);border-radius:8px;background:rgba(37,172,125,.1);color:#8ce6bf;cursor:pointer;font-size:9px;font-weight:900;letter-spacing:.25px}.vault-bulk-button.out{border-color:rgba(241,184,75,.5);background:rgba(163,111,30,.1);color:#f3ce83}.vault-bulk-button:hover{filter:brightness(1.18)}.vault-bulk-button:disabled{opacity:.36;cursor:not-allowed;filter:none}.vault-bulk-preview{margin:12px 0;padding:11px;border:1px solid #33465c;border-radius:9px;background:#0c141f}.vault-bulk-preview strong{display:block;color:#f0f5fb;font-size:14px}.vault-bulk-tabs{display:flex;flex-wrap:wrap;gap:5px;margin-top:9px}.vault-bulk-tabs span{padding:4px 7px;border:1px solid #32445a;border-radius:999px;color:#9eb0c5;font-size:9px}.vault-bulk-note{margin-top:9px;color:#8c9caf;font-size:10px;line-height:1.5}.vault-bulk-note.warn{color:#ffb47d}.vault-bulk-actions{display:flex;gap:7px;margin-top:14px}.vault-bulk-confirm{background:#24543e!important;border-color:#4aa77c!important;color:#c7ffe8!important}.vault-bulk-confirm.out{background:#62461e!important;border-color:#b88337!important;color:#ffe2a5!important}.vault-bulk-confirm:disabled{opacity:.4;cursor:not-allowed}
.stash-tab-head{display:flex;align-items:end;justify-content:space-between;gap:12px;max-width:1120px;margin-top:14px}.stash-tab-head h2{margin:0 0 6px}.stash-fill{min-height:31px;margin:0 0 6px;padding:6px 11px;border:1px solid rgba(82,221,169,.44);border-radius:8px;background:linear-gradient(180deg,rgba(38,126,91,.28),rgba(23,73,55,.3));color:#91ebc4;cursor:pointer;font-size:10px;font-weight:850;letter-spacing:.35px}.stash-fill:hover{border-color:#62e3b3;background:rgba(38,126,91,.38)}.stash-fill:disabled{opacity:.38;cursor:not-allowed}
@media(max-width:1260px){body{grid-template-columns:232px minmax(500px,1fr) 350px}.top-actions{min-width:280px}.brand{min-width:195px}.version{max-width:175px}#mid{padding-left:17px;padding-right:17px}.finder-bar{grid-template-columns:1fr 1fr}.finder-bar #ofq,.finder-bar #ofgo{grid-column:1/-1}.access-grid{grid-template-columns:1fr}.vault-compare-grid{grid-template-columns:1fr}}
</style></head><body>
<header id="topbar">
  <div class="brand">
    <div class="brand-rune"><span>HS</span></div>
    <div class="brand-title">HERO SIEGE VAULT<small>OFFLINE ITEM EDITOR</small></div>
  </div>
  <div class="top-health"><div id="status" role="status" aria-live="polite">CHECKING GAME STATE...</div></div>
  <div class="top-actions">
    <div class="version" id="version">SEASON 10</div>
    <button type="button" class="iconbtn" id="catalog-toggle" title="Show or hide the item catalog" aria-controls="right" aria-expanded="true">CATALOG ◫</button>
  </div>
</header>
<aside id="left">
  <h1>HERO SIEGE ITEM EDITOR</h1>
  <div class="side-caption">WORKSPACE</div>
  <button class="tabbtn" data-view="finder">&#128269;&nbsp; Global Item Finder</button>
  <button class="tabbtn" data-view="stash">&#128451;&nbsp; Shared Stash</button>
  <button class="tabbtn" data-view="vault">&#8734;&nbsp; Infinite Vault</button>
  <button class="tabbtn" data-view="sets">&#9876;&nbsp; Set Collection</button>
  <button class="tabbtn" data-view="runewords">&#10038;&nbsp; Runeword Forge</button>
  <button class="tabbtn" data-view="relics">&#128302;&nbsp; Relic Lab</button>
  <button class="tabbtn" data-view="stackables">&#128230;&nbsp; Season 10 Access &amp; Materials</button>
  <button class="tabbtn" data-view="health">&#128737;&nbsp; Save Health Check</button>
  <button class="tabbtn" data-view="backups">&#128190;&nbsp; Recovery Vault</button>
  <div class="side-caption characters">CHARACTERS</div>
  <div id="chars"></div>
  <div class="side-foot"><b>Offline safety</b><br>Every write creates a recoverable backup. Editing locks automatically while Hero Siege is running.</div>
</aside>
<main id="mid"><div class="welcome"><div class="welcome-card"><div class="welcome-mark">&#10022;</div><h2>Vault ready</h2><p>Select a character or workspace tool to begin.</p></div></div></main>
<aside id="right">
  <div class="catalog-head"><div><h2>Item Catalog</h2><p>Verified Season 10 repository</p></div><span class="catalog-badge">S10</span></div>
  <div class="searchbox"><input id="q" placeholder="Search by item name..." aria-label="Search item catalog"></div>
  <div id="filters">
    <div class="fitem"><label>Type</label><select id="fkind"><option value="">All</option><option value="unique">Unique / Set</option><option value="normal">Normal</option><option value="runeword">Runeword</option></select></div>
    <div class="fitem"><label>Slot</label><select id="fcls"><option value="">All</option></select></div>
    <div class="fitem"><label>Rarity</label><select id="frar"><option value="">All</option><option>Angelic</option><option>Unholy</option><option>Heroic</option><option>Satanic</option><option>Normal</option></select></div>
    <div class="fitem"><label>Set</label><select id="fset"><option value="">All items</option><option value="any">Any set piece</option></select></div>
    <div class="fitem" style="grid-column:1/3"><label>Has Stat</label><input id="fstat" list="statlist" placeholder="e.g. magic find, attack speed..."><datalist id="statlist"></datalist></div>
  </div>
  <div id="results"></div>
  <div id="addzone" style="border-top:1px solid var(--line);padding-top:8px">
    <div id="selinfo" class="muted">No item selected</div>
    <div class="perfect-pill" id="rollstatus">ROLL PROFILE DATABASE CHECKING...</div>
    <div class="flex" id="targetrow" style="margin-top:6px"></div>
    <button class="act" id="addbtn" disabled>Add</button>
    <div id="msg"></div>
  </div>
</aside>
<script>
let CAT=[], SETS_DB=[], RW_DB=[], chars=[], view=null, sel=null, curChar=null, charData=null, stashData=null, GAME_RUNNING=false;
let vaultState={collectionId:'all',q:'',offset:0,limit:120,withdrawTab:'stash_tab_1',queryToken:0,highlightItem:null};
let vaultBulkBusy=false;
let previewModels=new Map(),previewSequence=0;
const vaultCompareItems=new Map();
function resetPreviewModels(){previewModels.clear();previewSequence=0}
function registerPreviewModel(model){
  if(!model||typeof model!=='object')return '';
  const id='preview_'+(++previewSequence);previewModels.set(id,model);return id;
}
const DICE_TARGET_CACHE={};
const DICE_ADD_SELECTION={};
const STASH_FILL_IDENTITY_TARGETS={"unique:10:0:23":1,"unique:10:0:31":2,"unique:10:0:89":7};
function isOrdinarySmallCharm(row){
  const base=Number(row&&row.b);
  return Boolean(row)&&row.kind==='normal'&&Number(row.cls)===10&&Number(row.sub||0)===0&&row.key==='charms_normal_small_charm'&&Number.isInteger(base)&&base>=0&&base<=19;
}
const CLS={0:"Helmet",1:"Body Armor",2:"Boots",3:"Weapon",4:"Gloves",5:"Amulet",6:"Shield",7:"Ring",8:"Belt",10:"Charm",11:"Potion / Codex",12:"Key",13:"Boss Part / Tarot",14:"Material",15:"Rune / Gem / Orb",16:"Relic",18:"Flask",19:"Essence Vault","-2":"Runeword"};
const SLOTS={0:"Helmet",1:"Body Armor",2:"Boots",3:"Weapon I",4:"Gloves",5:"Amulet",6:"Offhand I",7:"Ring I",8:"Belt",9:"Ring II",10:"Relic 1",11:"Relic 2",12:"Relic 3",13:"Relic 4",14:"Relic 5",16:"Weapon II",17:"Offhand II"};
const DIMS={inventory_tab:[15,6],inventory_charms:[3,11],inventory_key_tab:[15,6],inventory_material_tab:[15,6],inventory_socket_tab:[15,6],inventory_relic_tab:[15,6],inventory_tarot_tab:[15,6],inventory_vault_tab:[15,6],inventory_vault_active:[15,6],stash_tab:[17,18],material_tab:[17,18],socket_tab:[17,18],potions:[5,2],personal_stash:[17,18]};
const BAG_LABELS={inventory_tab_0:"Main",inventory_tab_1:"Extra 1",inventory_tab_2:"Extra 2",inventory_tab_3:"Extra 3",inventory_tab_4:"Extra 4",inventory_socket_tab:"Runes & Gems",inventory_material_tab:"Materials",inventory_key_tab:"Keys",inventory_relic_tab:"Relics",inventory_tarot_tab:"Tarot",inventory_vault_tab:"Essence Vaults",inventory_vault_active_0:"Active Vault",inventory_charms:"Charms",personal_stash:"Personal Stash"};
const CELL=26;
const TIPBAR=`<div class="tipbar">&#128161; <b>Right-click any item</b> for: Store in Infinite Vault, Verified MAX / Best Roll, Dice skill target, compatible-item sockets, Random reroll, Duplicate, Edit stack, Delete &nbsp;&middot;&nbsp; <b>Verified item profiles are applied automatically when available</b> &nbsp;&middot;&nbsp; <b>Drag</b> items to move them or drop onto an equipment slot</div>`;
const STASH_DRAG_TIP=`<div class="tipbar">&#8597; <b>Moving between stash tabs:</b> while holding an item, use the mouse wheel or keep the pointer near the top/bottom edge to auto-scroll. The item is saved only when dropped on a valid cell.</div>`;
async function j(u,opt={}){
  const cfg={...opt};
  if((cfg.method||'GET').toUpperCase()==='POST'){
    cfg.headers={'Content-Type':'application/json','X-Hero-Siege-Item-Editor':'1',...(cfg.headers||{})};
  }
  const r=await fetch(u,cfg);return r.json()
}
async function boot(){
  CAT=await j('/api/catalog'); SETS_DB=await j('/api/sets'); RW_DB=await j('/api/runewords');
  const fsetEl=document.getElementById('fset');
  SETS_DB.forEach(s=>{const o=document.createElement('option');o.value=s.set;o.textContent=s.name;fsetEl.appendChild(o)});
  const labels=new Set();
  CAT.forEach(r=>(r.stats||[]).forEach(([l,v])=>labels.add(l)));
  const dl=document.getElementById('statlist');
  [...labels].sort().forEach(l=>{const o=document.createElement('option');o.value=l;dl.appendChild(o)});
  const ov=await j('/api/overview'); chars=ov.chars;
  GAME_RUNNING=!!ov.gameRunning;
  document.getElementById('version').textContent=`${ov.profile||'Season 10'} · v${ov.version||''} · ${ov.catalogItems||0} items`;
  const rollStatus=document.getElementById('rollstatus'), rpdb=ov.rollProfiles||{};
  const diceDb=ov.diceSkillTargets||{}, torchDb=ov.torchClassTargets||{};
  const capabilityBits=[
    rpdb.available
      ?`&#10003; MAX/BEST READY · ${rpdb.profileCount||0} VERIFIED PROFILES`
      :'&#9888; MAX/BEST UNAVAILABLE',
    torchDb.available?'TORCH TARGETS READY':'TORCH TARGETS UNAVAILABLE',
    diceDb.available?'DICE TARGETS READY':'DICE TARGETS UNAVAILABLE',
  ];
  rollStatus.innerHTML=capabilityBits.join(' · ');
  rollStatus.title=[
    rpdb.available?'':`MAX/BEST: ${rpdb.message||'database unavailable'}`,
    torchDb.available?'':`Torch targets: ${torchDb.message||'database unavailable'}`,
    diceDb.available?'':`Dice targets: ${diceDb.message||'database unavailable'}`,
  ].filter(Boolean).join('\n');
  const capabilityReady=[!!rpdb.available,!!torchDb.available,!!diceDb.available];
  const allCapabilitiesReady=capabilityReady.every(Boolean);
  const anyCapabilityReady=capabilityReady.some(Boolean);
  rollStatus.style.borderColor=allCapabilitiesReady?'#3da55e':(anyCapabilityReady?'#a87329':'#b45a43');
  rollStatus.style.color=allCapabilitiesReady?'#74ee98':(anyCapabilityReady?'#ffd080':'#ff9b83');
  document.getElementById('status').textContent=ov.gameRunning?'GAME RUNNING - VIEW ONLY, WRITING LOCKED':'GAME CLOSED - EDITING ENABLED';
  document.getElementById('status').className=ov.gameRunning?'warn':'';
  const cd=document.getElementById('chars'); cd.innerHTML='';
  chars.forEach(c=>{const b=document.createElement('button');b.className='charbtn';
    b.dataset.slot=c.slot;
    b.innerHTML=`<b>${c.name}</b><br><span class="muted">${c.cls} - Lv. ${c.level} (slot ${c.slot})</span>`;
    b.onclick=()=>{document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('sel'));openChar(c.slot,b)}; cd.appendChild(b)});
  const fc=document.getElementById('fcls');
  Object.entries(CLS).forEach(([k,v])=>{const o=document.createElement('option');o.value=k;o.textContent=v;fc.appendChild(o)});
  search();
  setInterval(async()=>{const o=await j('/api/overview');GAME_RUNNING=!!o.gameRunning;
    document.getElementById('status').textContent=o.gameRunning?'GAME RUNNING - VIEW ONLY, WRITING LOCKED':'GAME CLOSED - EDITING ENABLED';
    document.getElementById('status').className=o.gameRunning?'warn':'';
    document.querySelectorAll('.stash-fill').forEach(button=>button.disabled=GAME_RUNNING);
    document.querySelectorAll('.vault-bulk-button').forEach(button=>button.disabled=GAME_RUNNING||button.dataset.staticDisabled==='1'||vaultBulkBusy);
    document.querySelectorAll('.vault-bulk-confirm').forEach(button=>button.disabled=GAME_RUNNING||vaultBulkBusy||button.dataset.ready==='0');},5000);
  document.querySelector('[data-view=stash]').onclick=openStash;
  document.querySelector('[data-view=vault]').onclick=()=>openVault(true);
  document.querySelector('[data-view=finder]').onclick=openFinder;
  document.querySelector('[data-view=sets]').onclick=openSets;
  document.querySelector('[data-view=runewords]').onclick=openRunewords;
  document.querySelector('[data-view=relics]').onclick=openRelics;
  document.querySelector('[data-view=stackables]').onclick=openStackables;
  document.querySelector('[data-view=health]').onclick=openHealth;
  document.querySelector('[data-view=backups]').onclick=openBackups;
  document.querySelectorAll('.tabbtn').forEach(b=>b.addEventListener('click',()=>{
    document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('sel'));
    b.classList.add('sel');
  }));
  document.getElementById('catalog-toggle').onclick=()=>{
    const closed=document.body.classList.toggle('catalog-collapsed');
    document.getElementById('catalog-toggle').textContent=closed?'CATALOG ▣':'CATALOG ◫';
    document.getElementById('catalog-toggle').setAttribute('aria-expanded',String(!closed));
  };
  setupDnD(); setupTip();
}
function dims(tab){const b=tab.replace(/_\d+$/,'');return DIMS[b]||(b.startsWith('inventory_')?[15,6]:[17,18])}
let gridReg={}, gridSeq=0;
function gridHTML(tab,items,delTarget){
  const [c,r]=dims(tab);
  const gid='g'+(gridSeq++);
  gridReg[gid]={tab,target:delTarget,items,cols:c,rows:r};
  let h=`<div class="grid" id="${gid}" data-gid="${gid}" style="width:${c*CELL+2}px;height:${r*CELL+2}px">`;
  for(let y=0;y<r;y++)for(let x=0;x<c;x++)h+=`<div class="cell" style="left:${x*CELL}px;top:${y*CELL}px;width:${CELL}px;height:${CELL}px"></div>`;
  items.forEach((it,i)=>{
    const p=it.pos||[0,0];
    const rr=it.rar&&it.rar!=='?'?it.rar:'_';
    const previewId=registerPreviewModel(it.gameTooltip);
    const inner=it.spr?`<img src="/icons/${attr(it.spr)}.png?v=2" loading="lazy">`:esc(short(it.name));
    h+=`<div class="item b-${attr(rr)}" draggable="true" title="" data-i="${i}" data-preview-id="${attr(previewId)}" data-del='${attr(JSON.stringify(delTarget))}' data-key="${attr(it.key)}" data-w="${it.w||1}" data-h="${it.h||1}" data-cid="${it.cid??''}" data-rwcid="${it.rwcid??''}" data-socket-limit="${it.socketLimit??0}" data-socket-count="${it.socketCount??''}" data-roll="${attr(JSON.stringify(it.rollProfile||null))}" data-skill="${attr(JSON.stringify(it.skillSelector||null))}" data-raw='${attr(JSON.stringify(it.raw||{}))}'
      style="left:${p[0]*CELL}px;top:${p[1]*CELL}px;width:${(it.w||1)*CELL-2}px;height:${(it.h||1)*CELL-2}px">${inner}${it.stack?`<span class="stk">x${it.stack}</span>`:''}</div>`;
  });
  return h+'</div>';
}
function uniqueListHTML(items,delTarget){
  if(!items.length)return '<div class="muted">This auto-sorted tab is empty.</div>';
  return `<div class="unique-list">${items.map(it=>`<div class="unique-card" data-item-preview data-preview-id="${attr(registerPreviewModel(it.gameTooltip))}" data-del='${attr(JSON.stringify(delTarget))}' data-key="${attr(it.key)}" data-cid="${it.cid??''}" data-rwcid="${it.rwcid??''}" data-socket-limit="${it.socketLimit??0}" data-socket-count="${it.socketCount??''}" data-roll="${attr(JSON.stringify(it.rollProfile||null))}" data-skill="${attr(JSON.stringify(it.skillSelector||null))}" data-raw='${attr(JSON.stringify(it.raw||{}))}'>${it.spr?`<img src="/icons/${attr(it.spr)}.png?v=2" loading="lazy">`:'<div class="found-icon">&#9671;</div>'}<div><div class="r-${attr(it.rar||'_')}">${esc(it.name)}</div><div class="muted">right-click for actions</div></div></div>`).join('')}</div>`;
}
function occFree(g,x,y,w,h,skipKey){
  if(x<0||y<0||x+w>g.cols||y+h>g.rows)return false;
  for(const it of g.items){
    if(it.key===skipKey||!it.pos)continue;
    const ix=it.pos[0],iy=it.pos[1],iw=it.w||1,ih=it.h||1;
    if(!(x+w<=ix||ix+iw<=x||y+h<=iy||iy+ih<=y))return false;
  }
  return true;
}
let dragInfo=null, dragScrollFrame=0, dragScrollSpeed=0;
const DRAG_SCROLL_MAX=24;
function dragScrollVelocity(y,top,height){const edge=Math.min(88,Math.max(48,height*.15));if(y<top||y>top+height)return 0;if(y<top+edge)return -Math.max(2,Math.ceil(DRAG_SCROLL_MAX*(top+edge-y)/edge));if(y>top+height-edge)return Math.max(2,Math.ceil(DRAG_SCROLL_MAX*(y-(top+height-edge))/edge));return 0}
function stopDragScroll(){
  dragScrollSpeed=0;
  if(dragScrollFrame){cancelAnimationFrame(dragScrollFrame);dragScrollFrame=0}
}
function dragScrollTick(){
  dragScrollFrame=0;
  if(!dragInfo||view!=='stash'||!dragScrollSpeed)return;
  const mid=document.getElementById('mid');
  const before=mid.scrollTop, limit=Math.max(0,mid.scrollHeight-mid.clientHeight);
  mid.scrollTop=Math.max(0,Math.min(limit,before+dragScrollSpeed));
  clearGhost();
  if(mid.scrollTop===before){dragScrollSpeed=0;return}
  dragScrollFrame=requestAnimationFrame(dragScrollTick);
}
function updateDragScroll(e){
  if(!dragInfo||view!=='stash'){stopDragScroll();return}
  const mid=document.getElementById('mid'), rect=mid.getBoundingClientRect();
  const insideX=e.clientX>=rect.left&&e.clientX<=rect.right;
  const speed=insideX?dragScrollVelocity(e.clientY,rect.top,rect.height):0;
  if(speed===dragScrollSpeed&&(!speed||dragScrollFrame))return;
  dragScrollSpeed=speed;
  if(!speed){stopDragScroll();return}
  if(!dragScrollFrame)dragScrollFrame=requestAnimationFrame(dragScrollTick);
}
function wheelDragScroll(e){
  if(!dragInfo||view!=='stash')return;
  const mid=document.getElementById('mid');
  const unit=e.deltaMode===1?36:(e.deltaMode===2?Math.max(1,mid.clientHeight*.85):1);
  const before=mid.scrollTop, limit=Math.max(0,mid.scrollHeight-mid.clientHeight);
  mid.scrollTop=Math.max(0,Math.min(limit,before+e.deltaY*unit));
  if(mid.scrollTop!==before){e.preventDefault();clearGhost()}
}
function finishDrag(){stopDragScroll();clearGhost();dragInfo=null}
function slotAccepts(g){
  if(!dragInfo)return false;
  if(dollEq[g])return false;  // slot dolu
  let cls=null;
  if(dragInfo.mode==='add')cls=CAT[dragInfo.cid].cls;
  else if(dragInfo.cid!==''&&dragInfo.cid!=null)cls=CAT[+dragInfo.cid].cls;
  if(cls==null)return false;
  return (ACCEPT[g]||[]).includes(cls);
}
function setupDnD(){
  const mid=document.getElementById('mid');
  document.addEventListener('wheel',wheelDragScroll,{capture:true,passive:false});
  document.addEventListener('dragend',finishDrag,true);
  mid.addEventListener('dragstart',e=>{
    const el=e.target.closest('.item,.dslot[draggable]'); if(!el)return;
    dragInfo={mode:'move',from:JSON.parse(el.dataset.del),key:el.dataset.key,w:+el.dataset.w,h:+el.dataset.h,cid:el.dataset.cid};
    e.dataTransfer.effectAllowed='move';
  });
  mid.addEventListener('dragover',e=>{
    clearGhost();
    if(!dragInfo){stopDragScroll();return}
    updateDragScroll(e);
    const sEl=e.target.closest('.dslot');
    if(sEl){
      e.preventDefault();
      const g=+sEl.dataset.g;
      sEl.classList.add(slotAccepts(g)?'drophl-ok':'drophl-no');
      return;
    }
    const gEl=e.target.closest('.grid');
    if(!gEl)return;
    e.preventDefault();
    const g=gridReg[gEl.dataset.gid];
    const rc=gEl.getBoundingClientRect();
    let x=Math.floor((e.clientX-rc.left)/CELL), y=Math.floor((e.clientY-rc.top)/CELL);
    x=Math.max(0,Math.min(x,g.cols-dragInfo.w)); y=Math.max(0,Math.min(y,g.rows-dragInfo.h));
    const same=dragInfo.mode==='move'&&JSON.stringify(dragInfo.from)===JSON.stringify(g.target);
    const free=occFree(g,x,y,dragInfo.w,dragInfo.h,same?dragInfo.key:null);
    const gh=document.createElement('div'); gh.className='ghost';
    gh.style.cssText=`position:absolute;left:${x*CELL}px;top:${y*CELL}px;width:${dragInfo.w*CELL-2}px;height:${dragInfo.h*CELL-2}px;border:2px solid ${free?'#54e87a':'#ff5050'};background:${free?'rgba(84,232,122,.18)':'rgba(255,80,80,.18)'};pointer-events:none;z-index:9`;
    gh.dataset.x=x; gh.dataset.y=y; gh.dataset.free=free?'1':'';
    gEl.appendChild(gh);
  });
  mid.addEventListener('dragleave',e=>{
    if(!mid.contains(e.relatedTarget))stopDragScroll();
    if(!e.target.closest('.grid')&&!e.target.closest('.dslot'))clearGhost();
  });
  mid.addEventListener('drop',async e=>{
    if(!dragInfo)return;
    stopDragScroll();
    const sEl=e.target.closest('.dslot');
    if(sEl){
      e.preventDefault();
      const g=+sEl.dataset.g;
      clearGhost();
      if(!slotAccepts(g)){flash({err:dollEq[g]?'slot occupied - unequip first':'this item does not fit that slot'});finishDrag();return}
      let r;
      if(dragInfo.mode==='add'){
        r=await j('/api/add',{method:'POST',body:JSON.stringify({cid:dragInfo.cid,target:{type:'equip',slot:curChar,g},targetId:dragInfo.targetId})});
      }else{
        r=await j('/api/move',{method:'POST',body:JSON.stringify({from:dragInfo.from,to:{type:'equip',slot:curChar,g},key:dragInfo.key})});
      }
      flash(r); finishDrag(); refresh();
      return;
    }
    const gEl=e.target.closest('.grid'); if(!gEl)return;
    e.preventDefault();
    const gh=gEl.querySelector('.ghost');
    const g=gridReg[gEl.dataset.gid];
    if(!gh||!gh.dataset.free){finishDrag();return}
    const pos=[+gh.dataset.x,+gh.dataset.y];
    clearGhost();
    let r;
    if(dragInfo.mode==='move'){
      r=await j('/api/move',{method:'POST',body:JSON.stringify({from:dragInfo.from,to:g.target,key:dragInfo.key,pos})});
    }else{
      r=await j('/api/add',{method:'POST',body:JSON.stringify({cid:dragInfo.cid,target:{...g.target,pos},targetId:dragInfo.targetId})});
    }
    flash(r); finishDrag(); refresh();
  });
}
function clearGhost(){
  document.querySelectorAll('.ghost').forEach(g=>g.remove());
  document.querySelectorAll('.drophl-ok,.drophl-no').forEach(s=>s.classList.remove('drophl-ok','drophl-no'));
}
// ---- socket editor ----
function openSocketEditor(target,key,el){
  const old=document.getElementById('sockmodal'); if(old)old.remove();
  let raw={};
  try{raw=JSON.parse(el.dataset.raw||'{}')}catch(e){}
  const parsedSocketLimit=Number.parseInt(el.dataset.socketLimit||'0',10);
  const socketLimit=Number.isFinite(parsedSocketLimit)?Math.max(0,Math.min(6,parsedSocketLimit)):0;
  if(socketLimit<=0){flash({err:'This item has no verified socket capacity. Run Save Health Check if it already contains socket data.'});return}
  const cur=new Array(6).fill(null);
  let highestPayload=0;
  for(let n=1;n<=6;n++){
    const s=raw['s'+n];
    if(s===undefined)continue;
    highestPayload=n;
    let o=null; try{o=JSON.parse(atob(s))}catch(e){}
    const originalB=(o&&o.b!==undefined)?Number(o.b):null;
    cur[n-1]={originalEncoded:typeof s==='string'?s:null,originalB,b:originalB,inputValue:null,invalid:false};
  }
  const declared=Number(raw&&raw.zz&&raw.zz.sockets);
  const declaredCount=Number.isFinite(declared)?Math.max(0,Math.min(6,Math.trunc(declared))):0;
  const native=Number(el.dataset.socketCount);
  const nativeCount=Number.isFinite(native)?Math.max(0,Math.min(6,Math.trunc(native))):0;
  const currentCount=Math.max(nativeCount,declaredCount,highestPayload);
  const RUNES=CAT.filter(r=>r.available!==false&&r.kind==='normal'&&r.cls===15);
  const choiceLabel=r=>String(r.socketChoiceLabel||r.name||'').trim();
  const byLabel=new Map();RUNES.forEach(r=>byLabel.set(choiceLabel(r).toLowerCase(),Number(r.b)));
  const modal=document.createElement('div'); modal.id='sockmodal';
  const rows=cur.slice(0,currentCount).map(row=>row||{originalEncoded:null,originalB:null,b:null,inputValue:null,invalid:false});
  function socketRowPayload(row){
    if(row.originalEncoded&&row.originalB===row.b)return {keepEncoded:row.originalEncoded};
    return row.b==null?null:{b:row.b};
  }
  function render(){
    let h=`<div id="sockbox"><h3>Edit Sockets</h3>
    <div class="muted" style="margin-bottom:8px">Add or remove empty socket slots, then optionally choose a rune/gem. Press <b>Save</b> to write the displayed count.<br>Existing payloads stay byte-for-byte unchanged unless you replace or remove them. Maximum for this item: <b>${socketLimit}</b>.</div>
    <datalist id="runedl">${RUNES.map(r=>`<option value="${esc(choiceLabel(r))}">`).join('')}</datalist>`;
    if(!rows.length)h+=`<div class="muted" style="margin:10px 0">No sockets. Use Add socket to create the first empty slot.</div>`;
    rows.forEach((row,i)=>{
      const r=RUNES.find(x=>x.b===row.b);
      const inputValue=row.inputValue!=null?row.inputValue:(r?choiceLabel(r):'');
      h+=`<div class="sockrow"><b style="width:18px">${i+1}</b>
        ${r&&r.spr?`<img src="/icons/${r.spr}.png?v=2">`:'<span style="width:24px"></span>'}
        <input list="runedl" data-i="${i}" value="${esc(inputValue)}" placeholder="${row.originalEncoded&&!r?'existing unknown payload':'empty socket'}">
        <button type="button" data-rm="${i}" title="remove socket">&#10006;</button></div>`;
    });
    h+=`<div class="flex" style="margin-top:10px">
      <button type="button" class="act" style="margin:0" id="sockadd" ${rows.length>=socketLimit?'disabled':''}>+ Add socket (${rows.length}/${socketLimit})</button>
      <button type="button" class="act" style="margin:0;background:#234a2a;border-color:#3da55e" id="socksave">Save</button>
      <button type="button" class="act" style="margin:0" id="sockcancel">Cancel</button></div></div>`;
    modal.innerHTML=h;
    modal.querySelectorAll('input[data-i]').forEach(inp=>{
      // Do not rebuild the modal from a change/blur event. Replacing the DOM
      // before the following click event used to swallow Add/Save clicks.
      inp.oninput=()=>{
        const i=+inp.dataset.i;
        const value=inp.value.trim();
        const b=byLabel.get(value.toLowerCase());
        const nb=(b===undefined?null:b);
        rows[i].b=nb;
        rows[i].inputValue=value;
        rows[i].invalid=value!==''&&b===undefined;
      };
    });
    modal.querySelectorAll('button[data-rm]').forEach(btn=>{
      btn.onclick=()=>{rows.splice(+btn.dataset.rm,1);render()};
    });
    modal.querySelector('#sockadd').onclick=()=>{if(rows.length<socketLimit){rows.push({originalEncoded:null,originalB:null,b:null,inputValue:null,invalid:false});render()}};
    modal.querySelector('#sockcancel').onclick=()=>modal.remove();
    modal.querySelector('#socksave').onclick=async()=>{
      const save=modal.querySelector('#socksave');save.disabled=true;save.textContent='Saving...';
      if(rows.some(row=>row.invalid)){
        flash({err:'Choose an exact rune/gem option. Duplicate names include a required [ID ...] suffix.'});
        save.disabled=false;save.textContent='Save';return;
      }
      const payload=rows.map(socketRowPayload);
      try{
        const r=await j('/api/sockets',{method:'POST',body:JSON.stringify({target,key,sockets:payload})});
        flash(r);
        if(!r.err){modal.remove();refresh();return}
      }catch(e){flash({err:'Socket update was interrupted; the item was not confirmed as changed.'})}
      save.disabled=false;save.textContent='Save';
    };
  }
  render();
  modal.onclick=(e)=>{if(e.target===modal)modal.remove()};
  document.body.appendChild(modal);
}
// ---- Exact skill/class target editor ----
async function openSkillTargetEditor(target,key,selector){
  const old=document.getElementById('sockmodal'); if(old)old.remove();
  if(!selector||!selector.profileId){flash({err:'This item has no selectable skill or class profile.'});return}
  const isClass=selector.targetKind==='class';
  if(selector.available===false){flash({err:selector.message||(isClass?'Class targets are unavailable.':'Skill targets are unavailable.')});return}
  let payload=DICE_TARGET_CACHE[selector.profileId];
  if(!payload){
    payload=await j('/api/skill-targets?profile='+encodeURIComponent(selector.profileId));
    if(payload.err){flash(payload);return}
    DICE_TARGET_CACHE[selector.profileId]=payload;
  }
  const targets=payload.targets||[];
  if(!targets.length){flash({err:'No verified targets are available for this item.'});return}
  const modal=document.createElement('div'); modal.id='sockmodal';
  let chosenId=selector.current&&selector.current.id!=null?Number(selector.current.id):Number(targets[0].id);
  const currentText=selector.current
    ?(isClass?`${selector.current.name} (Class ID ${selector.current.id})`:`${selector.current.className}: ${selector.current.name} (ID ${selector.current.id})`)
    :'Current target could not be decoded from the saved seed.';
  const noun=isClass?'class':selector.targetKind==='subskill'?'subskill-capable skill':'skill';
  const proof=isClass
    ?'Every class option below keeps all four variable Torch stats at MAX. Saving replaces only the item\'s <b>a</b> seed; metadata, sockets, position, and every other field stay untouched.'
    :'Every option below has a clean-build replay proof. Saving changes only the item\'s <b>a</b> seed; +12/+1 fixed values, sockets, and every other field stay untouched.';
  modal.innerHTML=`<div id="sockbox" style="width:min(620px,90vw)"><h3>${esc(selector.name||'Dice')} · Choose ${esc(noun)}</h3>
    <div class="skill-current"><b>Current:</b> ${esc(currentText)}</div>
    <div class="skill-proof">${proof}</div>
    <input class="skill-search" id="skillsearch" placeholder="${isClass?'Search by class or Class ID...':'Search by skill, class, key, or ID...'}" autocomplete="off">
    <select class="skill-select" id="skillchoice" size="14"></select>
    <div class="flex" style="margin-top:12px">
      <button class="act" style="margin:0;background:#234a2a;border-color:#3da55e" id="skillsave">Apply chosen ${isClass?'class':'skill'}</button>
      <button class="act" style="margin:0" id="skillcancel">Cancel</button>
    </div></div>`;
  const search=modal.querySelector('#skillsearch');
  const choice=modal.querySelector('#skillchoice');
  function renderChoices(){
    const q=search.value.trim().toLowerCase();
    const matches=targets.filter(row=>!q||String(row.id)===q||row.name.toLowerCase().includes(q)||row.className.toLowerCase().includes(q)||(row.key||'').toLowerCase().includes(q));
    if(matches.length&&!matches.some(row=>Number(row.id)===chosenId))chosenId=Number(matches[0].id);
    if(isClass){
      choice.innerHTML=[...matches].sort((a,b)=>Number(a.id)-Number(b.id)).map(row=>`<option value="${row.id}" ${Number(row.id)===chosenId?'selected':''}>${esc(row.name)} · Class ID ${row.id}</option>`).join('');
    }else{
      const groups=new Map();
      [...matches].sort((a,b)=>Number(a.classId)-Number(b.classId)||a.name.localeCompare(b.name)).forEach(row=>{
        if(!groups.has(row.className))groups.set(row.className,[]);
        groups.get(row.className).push(row);
      });
      choice.innerHTML=[...groups.entries()].map(([className,rows])=>`<optgroup label="${esc(className)}">${rows.map(row=>`<option value="${row.id}" ${Number(row.id)===chosenId?'selected':''}>${esc(row.name)} · ID ${row.id}</option>`).join('')}</optgroup>`).join('');
    }
    choice.disabled=matches.length===0;
    modal.querySelector('#skillsave').disabled=matches.length===0;
  }
  search.oninput=renderChoices;
  choice.onchange=()=>{chosenId=Number(choice.value)};
  modal.querySelector('#skillcancel').onclick=()=>modal.remove();
  const skillSave=modal.querySelector('#skillsave');
  const skillCancel=modal.querySelector('#skillcancel');
  skillSave.onclick=async()=>{
    if(skillSave.disabled||choice.disabled||!Number.isInteger(Number(choice.value)))return;
    const selectedSkillId=Number(choice.value);
    skillSave.disabled=true; skillCancel.disabled=true; choice.disabled=true; search.disabled=true;
    try{
      const result=await j('/api/modify',{method:'POST',body:JSON.stringify({action:'selectskill',target,key,targetId:selectedSkillId})});
      modal.remove(); flash(result); refresh();
    }finally{
      if(modal.isConnected){skillSave.disabled=false;skillCancel.disabled=false;choice.disabled=false;search.disabled=false}
    }
  };
  renderChoices();
  modal.onclick=(e)=>{if(e.target===modal)modal.remove()};
  document.body.appendChild(modal);
  search.focus();
}
// ---- item tooltip ----
const TOOLTIP_RARITIES=new Set(['Satanic','Heroic','Angelic','Unholy','Runeword','Normal']);
function tooltipRarityClass(value){return TOOLTIP_RARITIES.has(value)?value:'_'}
function tooltipLineKey(line,index){
  if(line&&line._comparisonKey)return String(line._comparisonKey);
  if(line&&Number.isInteger(line.statKey))return 'stat:'+line.statKey;
  return line&&line.id?String(line.id):`line:${index}:${String(line&&line.label||'')}`;
}
function tooltipDifferenceKeys(left,right){
  const keyed=model=>new Map((model&&Array.isArray(model.stats)?model.stats:[]).map((line,index)=>[
    tooltipLineKey(line,index),JSON.stringify([line&&line.value,line&&line.formattedValue,line&&line.label])
  ]));
  const a=keyed(left),b=keyed(right),different=new Set();
  new Set([...a.keys(),...b.keys()]).forEach(key=>{if(a.get(key)!==b.get(key))different.add(key)});
  return different;
}
function tooltipComparisonLayout(left,right){
  const leftStats=left&&Array.isArray(left.stats)?left.stats:[],rightStats=right&&Array.isArray(right.stats)?right.stats:[];
  const keys=[],labels=new Map();
  [...leftStats,...rightStats].forEach((line,index)=>{
    const key=tooltipLineKey(line,index);if(!keys.includes(key))keys.push(key);
    if(!labels.has(key))labels.set(key,String(line&&line.label||'Unknown stat'));
  });
  return {keys,labels,differences:tooltipDifferenceKeys(left,right)};
}
function renderGameTooltip(model,options={}){
  if(!model||typeof model!=='object')return '';
  const item=model.item&&typeof model.item==='object'?model.item:{};
  const calc=model.calculation&&typeof model.calculation==='object'?model.calculation:{};
  const rarity=String(item.rarity||'?'),canonical=item.canonicalName||item.name||'Unknown item';
  const custom=item.customName&&String(item.customName).trim()?String(item.customName).trim():'';
  let h='<div class="game-tooltip">';
  if(custom)h+=`<div class="gtt-alias">${esc(custom)}</div>`;
  h+=`<div class="gtt-title r-${tooltipRarityClass(rarity)}">${esc(canonical)}</div>`;
  const meta=[rarity,item.tier?`Tier ${item.tier}`:'',options.extra||''].filter(Boolean).map(esc).join(' &middot; ');
  if(meta)h+=`<div class="gtt-type">${meta}</div>`;
  if(item.requiredLevel)h+=`<div class="gtt-requirement">Requires Level ${esc(item.requiredLevel)}</div>`;
  h+='<div class="gtt-rule"></div>';
  const nativeStats=Array.isArray(model.stats)?model.stats:[];
  let stats=nativeStats;
  if(options.comparison&&Array.isArray(options.comparison.keys)){
    const own=new Map(nativeStats.map((line,index)=>[tooltipLineKey(line,index),line]));
    stats=options.comparison.keys.map(key=>own.get(key)||{
      id:key,_comparisonKey:key,label:options.comparison.labels.get(key)||'Unknown stat',
      formattedValue:'—',value:null,confidence:'missing',missing:true,
    });
  }
  const differences=options.differences instanceof Set?options.differences:new Set();
  if(stats.length){
    stats.forEach((line,index)=>{
      if(!line||typeof line!=='object')return;
      const key=tooltipLineKey(line,index),value=line.formattedValue??line.sourceRange??'?';
      const classes=['gtt-stat'];
      if(line.confidence==='unresolved'||value==='?')classes.push('unresolved');
      if(line.missing||line.confidence==='missing')classes.push('missing');
      if(differences.has(key))classes.push('gtt-diff');
      h+=`<div class="${classes.join(' ')}"><b>${esc(value)}</b><span>${esc(line.label||'Unknown stat')}</span></div>`;
    });
  }else h+='<div class="gtt-empty">No displayed stat lines were resolved.</div>';
  const identities=Array.isArray(model.identities)?model.identities:[];
  identities.forEach(identity=>{
    if(!identity||typeof identity!=='object')return;
    const source=String(identity.source||''),label=source.includes('subskill')?'Subskill target':source.includes('damage_type')?'Damage type':source.includes('random_stat')?'Random stat identity':`Identity stat #${identity.statKey??'?'}`;
    const selected=identity.selectedName?String(identity.selectedName):(identity.selectedIdentity!=null?`#${identity.selectedIdentity}`:null);
    const details=[selected||'unresolved',identity.fixedValue!=null?`value ${identity.fixedValue}`:'',identity.attempts>1?`${identity.attempts} attempts`:''].filter(Boolean);
    h+=`<div class="gtt-identity"><b>${esc(label)}</b> &middot; ${details.map(esc).join(' &middot; ')}</div>`;
  });
  const sockets=Array.isArray(model.sockets)?model.sockets:[];
  sockets.forEach(socket=>{
    if(!socket||typeof socket!=='object')return;
    const details=socket.status==='decoded'?[`ID ${socket.baseId??'?'}`,socket.quantity!=null?`n=${socket.quantity}`:''].filter(Boolean):['unreadable payload'];
    h+=`<div class="gtt-socket">Socket ${esc(socket.index??'?')} &middot; ${details.map(esc).join(' &middot; ')}</div>`;
  });
  const exact=calc.numbersExact===true,quality=model.rollQuality&&typeof model.rollQuality==='object'?model.rollQuality:null;
  const seeds=model.seeds&&typeof model.seeds==='object'?Object.entries(model.seeds).map(([k,v])=>`${k}=${v}`).join(', '):'';
  h+=`<div class="gtt-editor-meta ${exact?'gtt-exact':'gtt-partial'}"><b>${exact?'EXACT NUMBERS':'SAFE PREVIEW'}</b>`;
  if(quality&&Number(quality.total)>0)h+=` &middot; Max endpoints ${esc(quality.maxed)}/${esc(quality.total)}`;
  if(model.fingerprint)h+=` &middot; <span class="gtt-fingerprint">#${esc(model.fingerprint)}</span>`;
  if(seeds)h+=`<br>${esc(seeds)}`;
  const warnings=Array.isArray(calc.warnings)?calc.warnings.filter(Boolean):[];
  if(warnings.length)h+=`<br>${esc(warnings[0])}`;
  h+='</div></div>';
  return h;
}
function renderCatalogTooltip(cid,extra,raw,profileOverride,skillSelector){
    const r=CAT[cid]; if(!r)return '';
    let h=`<div class="tname r-${r.rar}">${esc(r.name)}</div>`;
    const meta=r.kind==='runeword'?'Runeword':`${CLS[r.cls]||''}${r.cls===3?' / '+(SUBN[r.sub]||r.sub):''} &middot; ${r.rar} &middot; ${r.kind}`;
    h+=`<div class="ttype">${meta}${r.tier?` &middot; Tier ${esc(r.tier)}`:''}${extra||''}</div>`;
    if(r.lvl)h+=`<div class="ttype">Requires Level ${r.lvl}</div>`;
    const profile=profileOverride||r.rollProfile||null;
    const fieldSeeds=profile&&profile.fieldSeeds&&typeof profile.fieldSeeds==='object'?{...profile.fieldSeeds}:{};
    const seedFields=['a','i','s'].filter(field=>Object.prototype.hasOwnProperty.call(fieldSeeds,field));
    const hasSeed=raw&&raw.a!==undefined&&Number.isFinite(Number(raw.a));
    if(isOrdinarySmallCharm(r)){
      h+=`<div class="ttype" style="color:#ffb46e">ROLLED RARITY &amp; AFFIXES: NATIVE RANDOM &middot; generated in-game from seed a</div>`;
    }else if(skillSelector){
      if(skillSelector.current){
        const isClass=skillSelector.targetKind==='class';
        const kind=isClass?'CLASS TARGET':skillSelector.targetKind==='subskill'?'SUBSKILL TARGET':'SKILL TARGET';
        const target=isClass?`${esc(skillSelector.current.name)} &middot; Class ID ${skillSelector.current.id}`:`${esc(skillSelector.current.className)}: ${esc(skillSelector.current.name)} &middot; ID ${skillSelector.current.id}`;
        h+=`<div class="ttype" style="color:#72dfdf">&#10003; ${kind} &middot; ${target} &middot; a=${hasSeed?Number(raw.a):'missing'}${isClass?' &middot; chosen targets use 4/4 MAX stats &middot; 2/2 sockets':''}</div>`;
      }else if(!raw&&skillSelector.available){
        const kind=skillSelector.targetKind==='class'?'CLASS':skillSelector.targetKind==='subskill'?'SUBSKILL':'SKILL';
        h+=`<div class="ttype" style="color:#72dfdf">&#10003; VERIFIED ${kind} SELECTOR &middot; choose a target before adding${kind==='CLASS'?' &middot; 4/4 stats MAX &middot; 2/2 sockets':''}</div>`;
      }else{
        h+=`<div class="ttype" style="color:#ffb46e">${skillSelector.targetKind==='class'?'Class':'Skill'} target unavailable &middot; ${esc(skillSelector.message||'saved seed could not be decoded')}</div>`;
      }
    }else if(profile&&profile.mode==='fixed'){
      h+=`<div class="ttype" style="color:#74ee98">&#10003; FIXED DEFINITION STATS &middot; no variable roll</div>`;
    }else if(profile&&seedFields.length&&raw){
      const applied=seedFields.every(field=>Number.isFinite(Number(raw[field]))&&Number(raw[field])===Number(fieldSeeds[field]));
      const rollLabel=profile.rollLabel||(profile.mode==='exact'?'EXACT MAX':'BEST POSSIBLE');
      const label=applied?`${profile.mode==='exact'?'&#10003;':'&#9733;'} ${rollLabel}`:'Current roll seed';
      const values=seedFields.map(field=>`${field}=${raw[field]===undefined?'missing':Number(raw[field])}`).join(', ');
      h+=`<div class="ttype" style="color:${applied?'#74ee98':'#a9bdd2'}">${label} &middot; ${esc(values)}${applied?` &middot; ${esc(profile.detail)}`:''}</div>`;
    }else if(hasSeed){
      h+=`<div class="ttype" style="color:#a9bdd2">Current roll seed &middot; a=${Number(raw.a)}</div>`;
    }else if(profile){
      h+=`<div class="ttype" style="color:#74ee98">Verified ${esc(profile.rollLabel||(profile.mode==='exact'?'EXACT MAX':'BEST POSSIBLE'))} profile available &middot; ${esc(profile.detail)}</div>`;
    }
    for(const [lbl,val] of (r.stats||[])){
      h+=`<div class="tstat"><b>${esc(val)}</b> ${esc(lbl)}</div>`;
    }
    if(r.set!==undefined){const s=SETS_DB.find(x=>x.set===r.set);
      h+=`<div class="tset">${esc(r.setName||(s&&s.name)||('Set #'+r.set))}</div>`;
      if(s)for(const pc of s.pieces)h+=`<div class="tset" style="color:#3da55e">&nbsp;&nbsp;${esc(pc.name)}</div>`;
      h+=`<div class="tset" style="color:#937f6a">(full-set bonus values not extracted yet)</div>`;}
    return h;
}
function setupTip(){
  const tip=document.createElement('div'); tip.id='tip'; document.body.appendChild(tip);
  let active=null;
  function hide(){tip.style.display='none';active=null}
  function position(x,y){
    const tw=tip.offsetWidth,th=tip.offsetHeight;
    tip.style.left=Math.max(8,Math.min(x+18,innerWidth-tw-8))+'px';
    tip.style.top=Math.max(8,Math.min(y+12,innerHeight-th-8))+'px';
  }
  document.addEventListener('mousemove',e=>{
    if(dragInfo||document.querySelector('.vault-compare-modal')||e.target.closest('button,input,select,textarea')){hide();return}
    const el=e.target.closest('.item,.dslot[draggable],.res,.rwname,[data-item-preview]');
    if(!el){hide();return}
    if(active!==el){
      const stk=el.querySelector('.stk'),preview=previewModels.get(el.dataset.previewId||'');
      if(preview)tip.innerHTML=renderGameTooltip(preview,{extra:stk?stk.textContent:''});
      else{
        const rwcid=el.dataset.rwcid;
        const cid=(rwcid!==''&&rwcid!=null)?rwcid:el.dataset.cid;
        if(cid===''||cid==null){hide();return}
        let raw=null;try{raw=JSON.parse(el.dataset.raw||'null')}catch(err){}
        let rollProfile=null;try{rollProfile=JSON.parse(el.dataset.roll||'null')}catch(err){}
        let skillSelector=null;try{skillSelector=JSON.parse(el.dataset.skill||'null')}catch(err){}
        tip.innerHTML=renderCatalogTooltip(+cid,stk?` &middot; ${stk.textContent}`:'',raw,rollProfile,skillSelector);
      }
      if(!tip.innerHTML){hide();return}
      active=el;tip.style.display='block';
    }
    position(e.clientX,e.clientY);
  });
}
const SUBN={1:"Sword",2:"Dagger",3:"Mace",4:"Axe",5:"Claw",6:"Polearm",7:"Chainsaw",8:"Staff",9:"Cane",10:"Wand",11:"Book",12:"Spellblade",13:"Bow",14:"Gun",15:"Flask",16:"Throwing",17:"Universal"}
function short(n){return n&&n.length>22?n.slice(0,20)+'..':(n||'?')}
function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function attr(s){return esc(s).replace(/'/g,'&#39;')}
function requestId(){return (globalThis.crypto&&globalThis.crypto.randomUUID)?globalThis.crypto.randomUUID():('req_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2))}
function isVaultTransferTab(tab){return /^(?:stash_tab_[1-9]\d*|material_tab(?:_[1-9]\d*)?|socket_tab(?:_[1-9]\d*)?)$/.test(tab||'')}
function vaultTabLabel(tab){
  const listed=vaultMeta&&(vaultMeta.transferTabs||[]).find(row=>row.tab===tab);
  if(listed)return listed.label;
  const normal=/^stash_tab_([1-9]\d*)$/.exec(tab||'');if(normal)return `Shared Stash Tab ${+normal[1]}`;
  const special=/^(material|socket)_tab(?:_([1-9]\d*))?$/.exec(tab||'');
  if(special)return `${special[1]==='material'?'Material Tab':'Socket Tab'}${special[2]?' '+(+special[2]):''}`;
  return tab||'Shared Stash';
}
let vaultMeta=null;
function vaultCollectionOptions(collections,selected){
  return (collections||[]).map(c=>`<option value="${c.id}" ${String(c.id)===String(selected)?'selected':''}>${esc(c.name)} (${c.itemCount||0})</option>`).join('');
}
async function preserveVaultBatchOwnership(requestId,itemCount,direction,returnView){
  const source=direction==='deposit'?'the protected deposit copies':'the reserved withdrawal items';
  if(!confirm(`Preserve Vault ownership for all ${itemCount} items?\n\nThis makes ${source} available in Infinite Vault. Shared Stash is not changed or cleaned. If some copies are already there, duplicates may remain.\n\nHero Siege must remain closed.`))return;
  const result=await j('/api/vault/batch/resolve',{method:'POST',body:JSON.stringify({action:'preserve-vault-ownership',requestId})});
  flash(result);
  if(result.err){alert(result.err);return}
  alert(`${result.ok}\n\n${result.warning||'Shared Stash was left untouched; check for possible duplicates.'}`);
  if(returnView==='health')await openHealth();else await openVault(false);
}
async function openVault(reset=true){
  view='vault';curChar=null;gridReg={};gridSeq=0;
  resetPreviewModels();
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  if(reset)vaultState.offset=0;
  try{vaultState.withdrawTab=localStorage.getItem('hsVaultWithdrawTab')||vaultState.withdrawTab}catch(e){}
  const md=document.getElementById('mid');
  md.innerHTML='<h2>Infinite Vault</h2><div class="tool-intro"><b>Opening your local vault...</b> Items are stored in a separate SQLite database beside your Hero Siege data.</div>';
  vaultMeta=await j('/api/vault/meta');
  if(vaultMeta.err){
    md.innerHTML=`<h2>Infinite Vault</h2><div class="vault-warning">${esc(vaultMeta.err)}</div>${vaultMeta.recoveryAvailable?'<button class="act recovery-button" id="vaulthealth" style="margin-top:12px">OPEN SAVE HEALTH CHECK</button>':''}`;
    const health=document.getElementById('vaulthealth');if(health)health.onclick=openHealth;return
  }
  if(vaultState.collectionId!=='all'&&!(vaultMeta.collections||[]).some(c=>String(c.id)===String(vaultState.collectionId)))vaultState.collectionId='all';
  const transferTabs=vaultMeta.transferTabs||[];
  if(!transferTabs.some(row=>row.tab===vaultState.withdrawTab))vaultState.withdrawTab=transferTabs.length?transferTabs[0].tab:'';
  const returnOptions=transferTabs.map(row=>`<option value="${attr(row.tab)}" ${vaultState.withdrawTab===row.tab?'selected':''}>Return to ${esc(row.label)}</option>`).join('');
  const bulkStaticLocked=!!(vaultMeta.pending||vaultMeta.conflicts);
  const bulkLocked=!!(vaultMeta.gameRunning||bulkStaticLocked);
  const vaultRecoveryBatches=vaultMeta.recoveryBatches||[];
  const vaultRecoveryWarnings=vaultMeta.recoveryWarnings||[];
  const vaultRecoveryMarkup=vaultRecoveryBatches.map(row=>`<div class="recovery-card"><h3>&#9888; Ambiguous ${row.direction==='deposit'?'stash-to-Vault deposit':'Vault-to-stash return'} (${row.itemCount} items)</h3><p>${esc(row.error||'The journal could not prove which side completed.')}</p><p><b>Ownership-safe choice:</b> keep every protected record available in Infinite Vault. Shared Stash will not be changed; duplicates may remain.</p><button class="act recovery-button" data-vault-batch-resolve="${attr(row.requestId)}" data-count="${row.itemCount}" data-direction="${attr(row.direction)}" ${vaultMeta.gameRunning?'disabled':''}>PRESERVE VAULT OWNERSHIP</button></div>`).join('');
  const vaultWarningMarkup=vaultRecoveryWarnings.map(row=>`<div class="vault-warning"><b>Vault ownership preserved for ${row.itemCount} items.</b> ${esc(row.message||'Shared Stash may contain duplicate copies.')}</div>`).join('');
  let h=`<h2>Infinite Vault <span class="muted">(${vaultMeta.total||0} items)</span></h2>
    <div class="tool-intro"><b>Unlimited named collections, connected to Shared Stash.</b> Individual actions use normal, Material and Socket grids. Bulk Transfer additionally preserves the complete Unique tab and all 19 numbered tabs.</div>
    ${vaultMeta.gameRunning?'<div class="vault-warning">Hero Siege is running. Your vault is viewable, but transfers are locked until the game is closed.</div>':''}
    ${vaultWarningMarkup}
    ${(vaultMeta.pending||vaultMeta.conflicts)?`<div class="vault-warning"><b>${vaultMeta.pending||vaultMeta.conflicts} transfer operation needs attention.</b> No item was discarded. Review the ownership-safe recovery below.</div>${vaultRecoveryMarkup}`:''}
    <div class="vault-bulk-panel">
      <div class="vault-bulk-copy"><b>Complete Stash Transfer</b><span>Choose every item tab or one exact Shared Stash tab. One preview, one backup, one atomic journal.</span></div>
      <button type="button" class="vault-bulk-button" id="vaultbulkin" data-static-disabled="${bulkStaticLocked?'1':'0'}" ${bulkLocked?'disabled':''}>MOVE SHARED STASH → VAULT</button>
      <button type="button" class="vault-bulk-button out" id="vaultbulkout" data-static-disabled="${bulkStaticLocked||!(vaultMeta.total||0)?'1':'0'}" ${bulkLocked||!(vaultMeta.total||0)?'disabled':''}>RETURN ALL VAULT → SHARED STASH</button>
    </div>
    <div class="vault-toolbar">
      <input id="vaultq" value="${attr(vaultState.q)}" placeholder="Search this vault..." aria-label="Search Infinite Vault">
      <select id="vaultcollection"><option value="all">All collections (${vaultMeta.total||0})</option>${vaultCollectionOptions(vaultMeta.collections,vaultState.collectionId)}</select>
      <select id="vaultreturn" title="Return items to this shared-stash tab" ${returnOptions?'':'disabled'}>${returnOptions||'<option>No compatible stash grid found</option>'}</select>
      <button class="vault-mini" id="vaultrefresh">REFRESH</button>
    </div>
    <div class="vault-manage"><button class="vault-mini" id="vaultnew">+ NEW COLLECTION</button><button class="vault-mini" id="vaultrename" ${vaultState.collectionId==='all'?'disabled':''}>RENAME</button><button class="vault-mini danger" id="vaultdelete" ${vaultState.collectionId==='all'?'disabled':''}>DELETE EMPTY</button></div>
    <div class="vault-summary"><span id="vaultcount">Loading items...</span><span>Database: ${esc(vaultMeta.databaseName||'hs_infinite_vault.sqlite3')}</span></div>
    <div class="vault-compare-bar" id="vaultcomparebar" hidden><div class="vault-compare-slots" id="vaultcompareslots"></div><button class="vault-mini" id="vaultcompareopen" disabled>COMPARE</button><button class="vault-mini" id="vaultcompareclear">CLEAR</button></div>
    <div id="vaultitems"><div class="vault-empty">Loading...</div></div>`;
  md.innerHTML=h;
  md.querySelectorAll('[data-vault-batch-resolve]').forEach(button=>button.onclick=()=>preserveVaultBatchOwnership(button.dataset.vaultBatchResolve,+button.dataset.count,button.dataset.direction,'vault'));
  document.getElementById('vaultcollection').value=String(vaultState.collectionId);
  let timer=null;
  document.getElementById('vaultq').oninput=e=>{vaultState.q=e.target.value;vaultState.offset=0;clearTimeout(timer);timer=setTimeout(loadVaultItems,220)};
  document.getElementById('vaultcollection').onchange=e=>{vaultState.collectionId=e.target.value==='all'?'all':+e.target.value;vaultState.offset=0;openVault(false)};
  document.getElementById('vaultreturn').onchange=e=>{vaultState.withdrawTab=e.target.value;try{localStorage.setItem('hsVaultWithdrawTab',vaultState.withdrawTab)}catch(err){}};
  document.getElementById('vaultrefresh').onclick=()=>openVault(false);
  document.getElementById('vaultbulkin').onclick=()=>openVaultBulk('stash-to-vault');
  document.getElementById('vaultbulkout').onclick=()=>openVaultBulk('vault-to-stash');
  document.getElementById('vaultnew').onclick=()=>manageVaultCollection('create');
  document.getElementById('vaultrename').onclick=()=>manageVaultCollection('rename');
  document.getElementById('vaultdelete').onclick=()=>manageVaultCollection('delete');
  document.getElementById('vaultcompareopen').onclick=openVaultCompare;
  document.getElementById('vaultcompareclear').onclick=()=>{vaultCompareItems.clear();renderVaultCompareBar();document.querySelectorAll('.vault-card.compare-selected').forEach(card=>card.classList.remove('compare-selected'))};
  renderVaultCompareBar();
  await loadVaultItems();
}
function vaultBulkSessionKey(direction,sourceTab='all',collectionId='all',destinationTab='auto'){return `hsVaultBulk:${direction}:${sourceTab}:${collectionId}:${destinationTab}`}
async function openVaultBulk(direction){
  if(vaultBulkBusy)return;
  if(GAME_RUNNING){flash({err:'Hero Siege is running. Close it before a bulk transfer.'});return}
  const previous=document.getElementById('sockmodal');if(previous)previous.remove();
  const returnFocus=document.activeElement;
  const intoVault=direction==='stash-to-vault';
  const selectedCollection=vaultState.collectionId!=='all'?vaultState.collectionId:vaultMeta.defaultCollectionId;
  const bulkSourceTabs=(vaultMeta.bulkDepositTabs||[]).length?vaultMeta.bulkDepositTabs:[{tab:'all',label:'All item tabs'}];
  const bulkDestinationTabs=(vaultMeta.bulkWithdrawalTabs||[]).length?vaultMeta.bulkWithdrawalTabs:[{tab:'auto',label:'Automatic (original tabs + safe routing)'}];
  let selectedSource='all';try{const savedSource=localStorage.getItem('hsVaultBulkSourceTab');if(bulkSourceTabs.some(row=>row.tab===savedSource))selectedSource=savedSource}catch(e){}
  let selectedDestination='auto';try{const savedDestination=localStorage.getItem('hsVaultBulkDestinationTab');if(bulkDestinationTabs.some(row=>row.tab===savedDestination))selectedDestination=savedDestination}catch(e){}
  const sourceOptions=bulkSourceTabs.map(row=>`<option value="${attr(row.tab)}" ${row.tab===selectedSource?'selected':''}>${esc(row.label)}</option>`).join('');
  const destinationOptions=bulkDestinationTabs.map(row=>`<option value="${attr(row.tab)}" ${row.tab===selectedDestination?'selected':''}>${esc(row.label)}</option>`).join('');
  const modal=document.createElement('div');modal.id='sockmodal';
  modal.innerHTML=`<div id="sockbox" role="dialog" aria-modal="true" aria-labelledby="vaultbulktitle">
    <h3 id="vaultbulktitle">${intoVault?'Move Shared Stash items to Infinite Vault':'Return complete Infinite Vault to Shared Stash'}</h3>
    <div class="muted" style="margin:6px 0 12px">${intoVault?'Choose all item tabs or one exact numbered, Material, Socket, or Unique tab.':'Includes every available item in every collection. Choose automatic routing or one exact compatible Shared Stash tab.'}</div>
    ${intoVault?`<div class="jlrow"><label for="vaultbulksource">Source</label><select id="vaultbulksource">${sourceOptions}</select></div>`:''}
    ${!intoVault?`<div class="jlrow"><label for="vaultbulkdestination">Destination</label><select id="vaultbulkdestination">${destinationOptions}</select></div>`:''}
    ${intoVault?`<div class="jlrow"><label for="vaultbulkcollection">Collection</label><select id="vaultbulkcollection">${vaultCollectionOptions(vaultMeta.collections,selectedCollection)}</select></div>`:''}
    <div id="vaultbulkpreview" class="vault-bulk-preview" role="status" aria-live="polite"><strong>Building exact preview…</strong><div class="vault-bulk-note">No data is being changed.</div></div>
    <div class="vault-bulk-actions"><button class="act vault-bulk-confirm${intoVault?'':' out'}" id="vaultbulkgo" type="button" data-ready="0" disabled>CONTINUE</button><button class="act" id="vaultbulkcancel" type="button">CANCEL</button></div>
  </div>`;
  document.body.appendChild(modal);
  const inertRoots=['topbar','left','mid','right'].map(id=>document.getElementById(id)).filter(Boolean);inertRoots.forEach(node=>node.inert=true);
  const previewHost=document.getElementById('vaultbulkpreview'),go=document.getElementById('vaultbulkgo'),cancel=document.getElementById('vaultbulkcancel'),collection=document.getElementById('vaultbulkcollection'),source=document.getElementById('vaultbulksource'),destinationTab=document.getElementById('vaultbulkdestination');
  let preview=null,loading=false;
  const dismiss=()=>{modal.remove();inertRoots.forEach(node=>node.inert=false);if(returnFocus&&returnFocus.focus)returnFocus.focus()};
  const close=()=>{if(!vaultBulkBusy)dismiss()};
  cancel.onclick=close;modal.onclick=e=>{if(e.target===modal)close()};
  modal.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();close();return}if(e.key==='Tab'){const focusable=[...modal.querySelectorAll('button:not([disabled]),select:not([disabled])')];if(!focusable.length)return;const first=focusable[0],last=focusable[focusable.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}}};
  const loadPreview=async()=>{
    if(loading||vaultBulkBusy)return;loading=true;preview=null;go.dataset.ready='0';go.disabled=true;go.textContent='CHECKING…';if(collection)collection.disabled=true;if(source)source.disabled=true;if(destinationTab)destinationTab.disabled=true;previewHost.setAttribute('aria-busy','true');
    previewHost.innerHTML='<strong>Building exact preview…</strong><div class="vault-bulk-note">Checking every item, destination and grid cell. No data is being changed.</div>';
    const params=new URLSearchParams({direction});if(intoVault){params.set('collectionId',String(collection.value));params.set('sourceTab',source.value)}else{params.set('destinationTab',destinationTab.value)}
    try{
      const result=await j('/api/vault/bulk-preview?'+params.toString());
      if(result.err){previewHost.innerHTML=`<strong>Transfer unavailable</strong><div class="vault-bulk-note warn">${esc(result.err)}</div>`;go.textContent='RETRY PREVIEW';go.dataset.ready='preview';go.disabled=false;return}
      preview=result;
      const tabs=(result.tabCounts||[]).map(row=>`<span>${esc(row.label)} · ${row.count}</span>`).join('');
      const destination=intoVault?` from <b>${esc(result.sourceLabel)}</b> into collection <b>${esc(result.collectionName)}</b>`:` from <b>all Vault collections</b> to <b>${esc(result.destinationLabel)}</b>`;
      const aliasWarning=(!intoVault&&result.customNamedCount)?`<div class="vault-bulk-note warn">${result.customNamedCount} Vault-only custom name${result.customNamedCount===1?'':'s'} will be removed with the returned Vault record; Hero Siege stash data has no field for these aliases.</div>`:'';
      const scopeNote=intoVault&&result.sourceTab!=='all'?'<div class="vault-bulk-note">Only this selected tab will be emptied. Every other item tab and all Shared Stash tab-name metadata remain untouched.</div>':'';
      previewHost.innerHTML=`<strong>${result.itemCount} exact item record${result.itemCount===1?'':'s'}${destination}</strong><div class="vault-bulk-tabs">${tabs||'<span>No item records</span>'}</div><div class="vault-bulk-note">All records are preflighted first. If one item cannot fit, nothing moves. Success creates one stash backup and one atomic batch.</div>${scopeNote}${aliasWarning}${result.gameRunning?'<div class="vault-bulk-note warn">Hero Siege is running; close it before continuing.</div>':''}`;
      go.dataset.ready=result.canRun?'1':'0';go.disabled=!result.canRun;go.textContent=result.empty?'NOTHING TO MOVE':(intoVault?`MOVE ${result.itemCount} ITEM${result.itemCount===1?'':'S'}`:`RETURN ALL ${result.itemCount} ITEMS`);
    }catch(error){previewHost.innerHTML='<strong>Preview interrupted</strong><div class="vault-bulk-note warn">Could not verify the complete transfer plan. Try again.</div>';go.textContent='RETRY PREVIEW';go.disabled=false;go.dataset.ready='preview'}
    finally{loading=false;previewHost.setAttribute('aria-busy','false');if(collection)collection.disabled=false;if(source)source.disabled=false;if(destinationTab)destinationTab.disabled=false}
  };
  if(collection)collection.onchange=loadPreview;
  if(source)source.onchange=()=>{try{localStorage.setItem('hsVaultBulkSourceTab',source.value)}catch(e){}loadPreview()};
  if(destinationTab)destinationTab.onchange=()=>{try{localStorage.setItem('hsVaultBulkDestinationTab',destinationTab.value)}catch(e){}loadPreview()};
  go.onclick=async()=>{
    if(go.dataset.ready==='preview'){loadPreview();return}
    if(!preview||go.dataset.ready!=='1'||vaultBulkBusy)return;
    vaultBulkBusy=true;go.disabled=true;cancel.disabled=true;if(collection)collection.disabled=true;if(source)source.disabled=true;if(destinationTab)destinationTab.disabled=true;
    go.textContent=`MOVING ${preview.itemCount} ITEMS ATOMICALLY…`;
    previewHost.setAttribute('aria-busy','true');previewHost.innerHTML=`<strong>Moving ${preview.itemCount} items atomically…</strong><div class="vault-bulk-note warn">Do not start Hero Siege or close the Item Editor. The batch journal is protecting every item.</div>`;
    const stableKey=vaultBulkSessionKey(direction,intoVault?(preview.sourceTab||'all'):'all',intoVault?String(preview.collectionId):'all',intoVault?'auto':(preview.destinationTab||'auto'));
    let stableId='';try{stableId=sessionStorage.getItem(stableKey)||requestId();sessionStorage.setItem(stableKey,stableId)}catch(e){stableId=requestId()}
    const body={direction,requestId:stableId,previewToken:preview.previewToken};if(intoVault){body.collectionId=+preview.collectionId;body.sourceTab=preview.sourceTab||'all'}else{body.destinationTab=preview.destinationTab||'auto'}
    try{
      const result=await j('/api/vault/bulk',{method:'POST',body:JSON.stringify(body)});flash(result);
      if(result.terminal){try{sessionStorage.removeItem(stableKey)}catch(e){}}
      if(!result.err){dismiss();vaultCompareItems.clear();vaultState.offset=0;await openVault(true);return}
      if(result.code==='preview_stale'){previewHost.innerHTML=`<strong>Preview expired safely</strong><div class="vault-bulk-note warn">${esc(result.err)}</div>`;preview=null;go.dataset.ready='preview';go.textContent='BUILD NEW PREVIEW'}
      else{go.textContent=result.retryable?'RETRY SAME TRANSFER':'REVIEW AGAIN';go.dataset.ready=result.retryable?'1':'preview'}
    }catch(error){flash({err:'Connection interrupted. The batch journal protects every item; retry with the same transfer ID.'});go.textContent='RETRY SAME TRANSFER';go.dataset.ready='1'}
    finally{vaultBulkBusy=false;previewHost.setAttribute('aria-busy','false');cancel.disabled=false;if(collection)collection.disabled=false;if(source)source.disabled=false;if(destinationTab)destinationTab.disabled=false;go.disabled=GAME_RUNNING||go.dataset.ready==='0'}
  };
  cancel.focus();
  loadPreview();
}
async function loadVaultItems(){
  const token=++vaultState.queryToken;
  const params=new URLSearchParams({q:vaultState.q,offset:String(vaultState.offset),limit:String(vaultState.limit)});
  if(vaultState.collectionId!=='all')params.set('collectionId',String(vaultState.collectionId));
  const payload=await j('/api/vault/items?'+params.toString());
  if(token!==vaultState.queryToken||view!=='vault')return;
  renderVaultItems(payload);
}
function renderVaultItems(payload){
  const host=document.getElementById('vaultitems'),count=document.getElementById('vaultcount');if(!host||!count)return;
  resetPreviewModels();
  if(payload.err){count.textContent='Vault query failed';host.innerHTML=`<div class="vault-warning">${esc(payload.err)}</div>`;renderVaultCompareBar();return}
  const rows=payload.items||[],total=payload.total||0;
  const start=total?payload.offset+1:0,end=Math.min(total,payload.offset+rows.length);
  count.textContent=`Showing ${start}-${end} of ${total}`;
  if(!rows.length){host.innerHTML='<div class="vault-empty">No items match this collection or search.</div>';renderVaultCompareBar();return}
  const cards=rows.map(row=>{
    const selected=vaultCompareItems.has(String(row.id)),previewId=registerPreviewModel(row.gameTooltip);
    return `<article class="vault-card${selected?' compare-selected':''}" data-item-preview data-preview-id="${attr(previewId)}" data-vault-id="${attr(row.id)}" data-cid="${row.cid??''}" data-rwcid="${row.rwcid??''}">
      ${row.spr?`<img src="/icons/${attr(row.spr)}.png?v=2" loading="lazy">`:'<div class="vault-card-icon">&#9671;</div>'}
      <div class="vault-card-copy">${row.customName?`<div class="vault-alias">${esc(row.customName)}</div>`:''}<div class="vault-name r-${attr(row.rar||'_')}">${esc(row.name)}</div><div class="vault-meta">${esc(row.collectionName)} &middot; ${esc(row.clsName||'Unknown type')}<br>${esc(row.sourceLabel||'Shared Stash')}${row.fingerprint?` &middot; <span class="vault-fingerprint">#${esc(row.fingerprint)}</span>`:''}</div></div>
      <button type="button" class="vault-card-tool name" data-vault-name="${attr(row.id)}" title="Set or clear custom name" aria-label="Set custom name">&#9998;</button>
      <button type="button" class="vault-card-tool compare${selected?' on':''}" data-vault-compare="${attr(row.id)}" title="${selected?'Remove from comparison':'Select for comparison'}" aria-label="${selected?'Remove from comparison':'Select for comparison'}">${selected?'&#10003;':'&#8644;'}</button>
      <div class="vault-actions"><button class="vault-mini" data-vault-move="${attr(row.id)}" data-current-collection="${row.collectionId}">MOVE</button><button class="vault-return" data-vault-return="${attr(row.id)}" ${vaultMeta&&vaultMeta.gameRunning?'disabled':''}>RETURN TO STASH</button></div>
    </article>`;
  }).join('');
  host.innerHTML=`<div class="vault-list">${cards}</div>
    <div class="vault-pager"><button class="vault-mini" id="vaultprev" ${payload.offset<=0?'disabled':''}>PREVIOUS</button><button class="vault-mini" id="vaultnext" ${payload.offset+rows.length>=total?'disabled':''}>NEXT</button></div>`;
  const byId=new Map(rows.map(row=>[String(row.id),row]));
  host.querySelectorAll('[data-vault-return]').forEach(btn=>btn.onclick=()=>withdrawVaultItem(btn.dataset.vaultReturn,btn));
  host.querySelectorAll('[data-vault-move]').forEach(btn=>btn.onclick=()=>moveVaultItem(btn.dataset.vaultMove,+btn.dataset.currentCollection));
  host.querySelectorAll('[data-vault-name]').forEach(btn=>btn.onclick=()=>editVaultCustomName(byId.get(btn.dataset.vaultName)));
  host.querySelectorAll('[data-vault-compare]').forEach(btn=>btn.onclick=()=>toggleVaultCompare(byId.get(btn.dataset.vaultCompare),btn));
  document.getElementById('vaultprev').onclick=()=>{vaultState.offset=Math.max(0,vaultState.offset-vaultState.limit);loadVaultItems()};
  document.getElementById('vaultnext').onclick=()=>{vaultState.offset+=vaultState.limit;loadVaultItems()};
  renderVaultCompareBar();
  if(vaultState.highlightItem){const card=[...host.querySelectorAll('[data-vault-id]')].find(el=>el.dataset.vaultId===vaultState.highlightItem);if(card){card.scrollIntoView({behavior:'smooth',block:'center'});card.classList.add('found-pulse');setTimeout(()=>card.classList.remove('found-pulse'),3800);vaultState.highlightItem=null}}
}
function vaultCompareLabel(row){return row&&String(row.customName||row.name||'Unknown item')}
function renderVaultCompareBar(){
  const bar=document.getElementById('vaultcomparebar'),slots=document.getElementById('vaultcompareslots'),open=document.getElementById('vaultcompareopen');
  if(!bar||!slots||!open)return;
  const rows=[...vaultCompareItems.values()];bar.hidden=!rows.length;open.disabled=rows.length!==2;
  slots.innerHTML=rows.length?`<b>${rows.length}/2 selected:</b> ${rows.map(vaultCompareLabel).map(esc).join(' &middot; ')}`:'';
}
function toggleVaultCompare(row,btn){
  if(!row)return;
  const id=String(row.id),selected=vaultCompareItems.has(id);
  if(selected)vaultCompareItems.delete(id);
  else{
    if(vaultCompareItems.size>=2){flash({err:'Two items are already selected. Remove one before adding another.'});return}
    vaultCompareItems.set(id,row);
  }
  const on=vaultCompareItems.has(id),card=btn&&btn.closest('.vault-card');
  if(card)card.classList.toggle('compare-selected',on);
  if(btn){btn.classList.toggle('on',on);btn.innerHTML=on?'&#10003;':'&#8644;';btn.title=on?'Remove from comparison':'Select for comparison';btn.setAttribute('aria-label',btn.title)}
  renderVaultCompareBar();
}
function openVaultCompare(){
  const rows=[...vaultCompareItems.values()];if(rows.length!==2){flash({err:'Select exactly two Vault items first.'});return}
  const previous=document.querySelector('.vault-compare-modal .vault-compare-close');if(previous)previous.click();
  const comparison=tooltipComparisonLayout(rows[0].gameTooltip,rows[1].gameTooltip),differences=comparison.differences,modal=document.createElement('div');
  modal.className='vault-compare-modal';modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');modal.setAttribute('aria-label','Compare Vault items');
  modal.innerHTML=`<div class="vault-compare-dialog"><div class="vault-compare-head"><div><h3>Item Comparison</h3><div class="muted">Highlighted rows differ; — means that stat is absent on that item. Max endpoints counts rolled stat lines currently at their upper endpoint.</div></div><button class="vault-mini vault-compare-close" type="button">CLOSE</button></div><div class="vault-compare-grid"><section class="vault-compare-column">${renderGameTooltip(rows[0].gameTooltip,{differences,comparison})}</section><section class="vault-compare-column">${renderGameTooltip(rows[1].gameTooltip,{differences,comparison})}</section></div></div>`;
  const close=()=>{document.removeEventListener('keydown',onKey);modal.remove()},onKey=e=>{if(e.key==='Escape')close()};
  modal.querySelector('.vault-compare-close').onclick=close;modal.onclick=e=>{if(e.target===modal)close()};document.addEventListener('keydown',onKey);document.body.appendChild(modal);modal.querySelector('.vault-compare-close').focus();
}
async function editVaultCustomName(row){
  if(!row)return;
  const previous=document.getElementById('sockmodal');if(previous)previous.remove();
  const modal=document.createElement('div');modal.id='sockmodal';
  modal.innerHTML=`<div id="sockbox" role="dialog" aria-modal="true" aria-labelledby="vaultnametitle"><h3 id="vaultnametitle">Vault Custom Name</h3><div class="muted" style="margin:6px 0 12px">Shown only inside Infinite Vault. The native ${esc(row.name)} item data is never changed.</div><div class="jlrow"><label for="vaultnameinput">Name</label><input id="vaultnameinput" maxlength="128" autocomplete="off" aria-describedby="vaultnamehelp" style="flex:1;min-width:0"></div><div class="muted" id="vaultnamehelp" style="margin-top:7px">Up to 128 characters. Clear removes the Vault-only name.</div><div class="flex" style="margin-top:14px"><button class="act" id="vaultnamesave" type="button" style="margin:0;background:#234a2a;border-color:#3da55e">SAVE</button><button class="act" id="vaultnameclear" type="button" style="margin:0">CLEAR</button><button class="act" id="vaultnamecancel" type="button" style="margin:0">CANCEL</button></div></div>`;
  document.body.appendChild(modal);
  const input=document.getElementById('vaultnameinput'),save=document.getElementById('vaultnamesave'),clear=document.getElementById('vaultnameclear'),cancel=document.getElementById('vaultnamecancel');
  input.value=row.customName||'';
  const close=()=>modal.remove();
  const submit=async value=>{
    save.disabled=true;clear.disabled=true;cancel.disabled=true;input.disabled=true;
    try{
      const r=await j('/api/vault/item',{method:'POST',body:JSON.stringify({action:'setCustomName',itemId:row.id,customName:value})});flash(r);
      if(r.err){save.disabled=false;clear.disabled=false;cancel.disabled=false;input.disabled=false;input.focus();return}
      if(r.item&&vaultCompareItems.has(String(row.id)))vaultCompareItems.set(String(row.id),r.item);
      close();await openVault(false);
    }catch(error){flash({err:'Custom name could not be saved.'});save.disabled=false;clear.disabled=false;cancel.disabled=false;input.disabled=false;input.focus()}
  };
  save.onclick=()=>submit(input.value);clear.onclick=()=>submit('');cancel.onclick=close;
  input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();save.click()}};
  modal.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();close()}};
  modal.onclick=e=>{if(e.target===modal)close()};
  input.focus();input.select();
}
async function withdrawVaultItem(itemId,btn){
  if(!vaultState.withdrawTab){flash({err:'No compatible Shared Stash grid was found.'});return}
  if(!confirm(`Return this item to ${vaultTabLabel(vaultState.withdrawTab)}?`))return;
  btn.disabled=true;btn.textContent='RETURNING...';
  btn.dataset.requestId=btn.dataset.requestId||requestId();
  try{
    const r=await j('/api/vault/withdraw',{method:'POST',body:JSON.stringify({itemId,target:{type:'stash',tab:vaultState.withdrawTab},requestId:btn.dataset.requestId})});
    flash(r);if(!r.err){delete btn.dataset.requestId;vaultCompareItems.delete(String(itemId));await openVault(false);return}
  }catch(e){flash({err:'Transfer interrupted. The item is still protected; press Return again to resume.'})}
  btn.disabled=false;btn.textContent='RETURN TO STASH';
}
async function moveVaultItem(itemId,currentId){
  const choices=(vaultMeta.collections||[]).filter(c=>c.id!==currentId);
  if(!choices.length){flash({err:'Create another collection first.'});return}
  const name=prompt('Move to collection:\n'+choices.map(c=>c.name).join('\n'),choices[0].name);if(name==null)return;
  const target=choices.find(c=>c.name.toLocaleLowerCase()===name.trim().toLocaleLowerCase());if(!target){flash({err:'Collection not found.'});return}
  const r=await j('/api/vault/item',{method:'POST',body:JSON.stringify({action:'move',itemId,collectionId:target.id})});flash(r);if(!r.err){if(r.item&&vaultCompareItems.has(String(itemId)))vaultCompareItems.set(String(itemId),r.item);await openVault(false)}
}
async function manageVaultCollection(action){
  let body={action};
  const current=(vaultMeta.collections||[]).find(c=>String(c.id)===String(vaultState.collectionId));
  if(action==='create'){const name=prompt('New collection name:');if(name==null)return;body.name=name}
  else if(action==='rename'){if(!current)return;const name=prompt('Rename collection:',current.name);if(name==null)return;body.collectionId=current.id;body.name=name}
  else{if(!current||!confirm(`Delete empty collection "${current.name}"?`))return;body.collectionId=current.id}
  const r=await j('/api/vault/collections',{method:'POST',body:JSON.stringify(body)});flash(r);if(!r.err){if(action==='delete')vaultState.collectionId='all';openVault(false)}
}
async function openVaultDepositDialog(target,key,el){
  const old=document.getElementById('sockmodal');if(old)old.remove();
  const meta=await j('/api/vault/meta');if(meta.err){flash(meta);return}
  if(meta.gameRunning){flash({err:'Hero Siege is running. Close it before transferring items.'});return}
  const modal=document.createElement('div');modal.id='sockmodal';
  const cid=el&&el.dataset?el.dataset.cid:null,row=(cid!==''&&cid!=null)?CAT[+cid]:null;
  modal.innerHTML=`<div id="sockbox"><h3>Store in Infinite Vault</h3><div class="muted" style="margin:6px 0 12px">${esc(row?row.name:'This exact item')} will be removed from Shared Stash only after its full record is safely stored.</div><div class="jlrow"><label>Collection</label><select id="vaultdepositcollection">${vaultCollectionOptions(meta.collections,meta.defaultCollectionId)}</select></div><div class="flex" style="margin-top:14px"><button class="act" id="vaultdepositgo" style="margin:0;background:#234a2a;border-color:#3da55e">STORE ITEM</button><button class="act" id="vaultdepositcancel" style="margin:0">CANCEL</button></div></div>`;
  document.body.appendChild(modal);document.getElementById('vaultdepositcancel').onclick=()=>modal.remove();
  const go=document.getElementById('vaultdepositgo');
  go.onclick=async()=>{go.disabled=true;go.textContent='STORING...';go.dataset.requestId=go.dataset.requestId||requestId();
    try{const r=await j('/api/vault/deposit',{method:'POST',body:JSON.stringify({source:target,key,collectionId:+document.getElementById('vaultdepositcollection').value,requestId:go.dataset.requestId})});flash(r);if(!r.err){modal.remove();refresh();return}}
    catch(e){flash({err:'Transfer interrupted. Your item remains protected; press Store Item again to resume.'})}
    go.disabled=false;go.textContent='STORE ITEM';};
}
function stashFillSpec(tab){
  let rows=[],button='',detail='';
  if(tab==='unique_items'){
    rows=CAT.filter(row=>row.kind==='unique'&&row.available!==false);
    button=`FILL ALL UNIQUES (${rows.length})`;
    detail=`Ensure all ${rows.length} available Unique items are present. Existing records remain unchanged. Torch of Shadows uses All Skills: Viking with 4/4 variable stats MAX; Loaded Dice targets Viking: Weapon Master; Overloaded Dice targets Viking: Odin's Fury. All three can be retargeted later.`;
  }else if(/^material_tab(?:_[1-9]\d*)?$/.test(tab)){
    rows=CAT.filter(row=>row.kind==='normal'&&(row.cls===13||row.cls===14)&&row.available!==false);
    button=`FILL MATERIALS (${rows.length}) · FULL x999`;
    detail=`Ensure all ${rows.length} Boss Part, Tarot and Material entries are present. Native stackables become x999; nine native singleton items remain single records.`;
  }else if(/^socket_tab(?:_[1-9]\d*)?$/.test(tab)){
    rows=CAT.filter(row=>row.kind==='normal'&&row.cls===15&&row.available!==false);
    button=`FILL SOCKETS (${rows.length}) · FULL x999`;
    detail=`Ensure all ${rows.length} verified Rune, Gem and Orb entries are present at a full x999 stack.`;
  }else return null;
  return {count:rows.length,button,detail};
}
async function fillStashTab(tab,button,spec){
  if(GAME_RUNNING){flash({err:'Hero Siege is running. Close it before filling a stash tab.'});return}
  const warning=`${spec.detail}\n\nNothing is deleted or rebuilt. A backup is created only if the stash changes. Continue?`;
  if(!confirm(warning))return;
  const oldText=button.textContent,mid=document.getElementById('mid'),scrollTop=mid.scrollTop;
  button.disabled=true;button.textContent='FILLING...';
  const body={tab};if(tab==='unique_items')body.identityTargetIds=STASH_FILL_IDENTITY_TARGETS;
  try{
    const result=await j('/api/stash/fill',{method:'POST',body:JSON.stringify(body)});
    flash(result);
    if(!result.err){await openStash();document.getElementById('mid').scrollTop=scrollTop;return}
  }catch(error){flash({err:'The fill request was interrupted; the original stash remains protected.'})}
  button.disabled=GAME_RUNNING;button.textContent=oldText;
}
async function openStash(){
  resetPreviewModels();
  view='stash'; curChar=null; stashData=await j('/api/stash');
  gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const md=document.getElementById('mid');
  if(stashData.err){
    md.innerHTML=`<h2>Stash (shared)</h2><div class="vault-warning">${esc(stashData.err)}</div>${stashData.recoveryAvailable?'<button class="act recovery-button" id="stashhealth" style="margin-top:12px">OPEN SAVE HEALTH CHECK</button>':''}`;
    const health=document.getElementById('stashhealth');if(health)health.onclick=openHealth;return
  }
  const order=Object.keys(stashData).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true,sensitivity:'base'}));
  let h='<h2>Stash (shared)</h2>'+TIPBAR+STASH_DRAG_TIP;
  for(const tab of order){
    const items=stashData[tab];
    const fill=stashFillSpec(tab);
    h+=`<div class="stash-tab-head"><h2 data-find-tab="${attr(tab)}">${esc(tab)} <span class="muted">(${items.length})</span></h2>${fill?`<button class="stash-fill" data-fill-stash="${attr(tab)}" title="${attr(fill.detail)}" ${GAME_RUNNING?'disabled':''}>${esc(fill.button)}</button>`:''}</div>`;
    if(tab==='unique_items'){
      h+=`<div class="muted" style="margin-bottom:7px">auto-sorted tab &middot; ${items.length} records (no grid positions)</div>`;
      h+=uniqueListHTML(items,{type:'stash',tab});
      continue;
    }
    h+=gridHTML(tab,items,{type:'stash',tab});
  }
  md.innerHTML=h;
  md.querySelectorAll('.stash-fill').forEach(button=>{
    const tab=button.dataset.fillStash,spec=stashFillSpec(tab);
    button.onclick=()=>fillStashTab(tab,button,spec);
  });
  bindDelete(); renderTargets();
}
// slot -> kabul edilen sinif idleri
const ACCEPT={0:[0],1:[1],2:[2],3:[3],16:[3],6:[3,6],17:[3,6],4:[4],5:[5],7:[7],9:[7],8:[8],10:[16],11:[16],12:[16],13:[16],14:[16]};
let wTabL=1, wTabR=1, bagTab='inventory_tab_0', dollEq={};
const DC=32;
function dslot(g,w,h,label){
  const e=dollEq[g];
  const rr=e&&e.rar&&e.rar!=='?'?e.rar:null;
  const del=attr(JSON.stringify({type:"equipped",slot:curChar,tab:"equipped_items"}));
  const previewId=e?registerPreviewModel(e.gameTooltip):'';
  return `<div class="dslot${rr?' b-'+rr:''}" data-g="${g}" style="width:${w*DC}px;height:${h*DC}px"
    ${e?`draggable="true" data-preview-id="${attr(previewId)}" data-del='${del}' data-key="${attr(e.key)}" data-w="${e.w||1}" data-h="${e.h||1}" data-cid="${e.cid??''}" data-rwcid="${e.rwcid??''}" data-socket-limit="${e.socketLimit??0}" data-socket-count="${e.socketCount??''}" data-roll="${attr(JSON.stringify(e.rollProfile||null))}" data-skill="${attr(JSON.stringify(e.skillSelector||null))}" data-raw='${attr(JSON.stringify(e.raw||{}))}'`:`title="${attr(label)} (empty)"`}>
    <span class="lbl">${esc(label)}</span>${e&&e.spr?`<img src="/icons/${attr(e.spr)}.png?v=2">`:(e?esc(short(e.name)):'')}</div>`;
}
function wpanel(side){
  const tabs=side==='L'?[3,16]:[6,17];
  const cur=side==='L'?(wTabL===1?3:16):(wTabR===1?6:17);
  const tsel=side==='L'?wTabL:wTabR;
  return `<div class="wpanel"><div class="wtabs">
    <button class="${tsel===1?'on':''}" onclick="wswap('${side}',1)">1</button>
    <button class="${tsel===2?'on':''}" onclick="wswap('${side}',2)">2</button></div>
    ${dslot(cur,2,4,SLOTS[cur])}</div>`;
}
function wswap(side,n){ if(side==='L')wTabL=n; else wTabR=n; renderChar(); }
function renderChar(){
  const slot=curChar, md=document.getElementById('mid');
  gridReg={}; gridSeq=0;
  dollEq={}; charData.equipped.forEach(e=>dollEq[e.g]=e);
  let h=`<div class="flex" id="lobar" style="margin-bottom:10px">
    <b style="color:var(--gold)">Loadouts:</b>
    <select id="losel"><option value="">select...</option></select>
    <button class="act" style="margin:0;padding:5px 12px" id="loapply">Apply</button>
    <button class="act" style="margin:0;padding:5px 12px" id="losave">Save current as...</button>
    <button class="act" style="margin:0;padding:5px 12px" id="loexport">Export Build</button>
    <button class="act" style="margin:0;padding:5px 12px" id="loimport">Import</button>
    <button class="act" style="margin:0;padding:5px 12px;border-color:#7a3030" id="lodelete">Delete</button>
    <input type="file" id="lofile" accept=".json" style="display:none">
  </div>`;
  h+=TIPBAR;
  h+=`<div id="doll">`;
  // relic sutunu
  h+=`<div class="relcol">${[10,11,12,13,14].map(g=>dslot(g,1,1.6,SLOTS[g])).join('')}</div>`;
  // orta: paper doll
  h+=`<div class="dmain">
    <div class="drow">${dslot(0,2,2,'Helmet')}${dslot(5,1,1,'Amulet')}</div>
    <div class="drow">${wpanel('L')}${dslot(1,2,3,'Body Armor')}${wpanel('R')}</div>
    <div class="drow">${dslot(7,1,1,'Ring I')}${dslot(8,2,1,'Belt')}${dslot(9,1,1,'Ring II')}</div>
    <div class="drow">${dslot(4,2,2,'Gloves')}<div><div class="lbl muted" style="font-size:9px;text-align:center">Potions</div>${gridHTML('potions',charData.potions,{type:'potions',slot})}</div>${dslot(2,2,2,'Boots')}</div>
  </div>`;
  // charm cantasi
  const charms=(charData.bags||{})['inventory_charms']||[];
  h+=`<div class="dcharms"><h3>CHARMS</h3>${gridHTML('inventory_charms',charms,{type:'bag',slot,tab:'inventory_charms'})}</div>`;
  h+=`</div>`;
  // Season 10 bag tabs. Future inventory tabs found in the save are appended
  // automatically instead of being silently hidden by a hard-coded list.
  const fixedTabs=["inventory_tab_0","inventory_tab_1","inventory_tab_2","inventory_tab_3","inventory_tab_4",
    "inventory_socket_tab","inventory_material_tab","inventory_key_tab","inventory_relic_tab","inventory_tarot_tab","inventory_vault_tab"];
  const discovered=Object.keys(charData.bags||{}).filter(t=>t!=="inventory_charms"&&!t.startsWith("inventory_vault_active_"));
  const tabNames=[...fixedTabs,...discovered.filter(t=>!fixedTabs.includes(t)),"personal_stash"];
  const BT=tabNames.map(t=>[t,BAG_LABELS[t]||t.replace(/^inventory_/,"").replace(/_/g," ")]);
  h+=`<div class="bagtabs">${BT.map(([t,l])=>{
    const n=t==='personal_stash'?charData.personal_stash.length:((charData.bags||{})[t]||[]).length;
    return `<button class="${bagTab===t?'on':''}" onclick="bagSwap('${t}')">${l}${n?` (${n})`:''}</button>`}).join('')}</div>`;
  if(bagTab==='personal_stash'){
    h+=gridHTML('personal_stash',charData.personal_stash,{type:'personal_stash',slot});
  }else{
    h+=gridHTML(bagTab,(charData.bags||{})[bagTab]||[],{type:'bag',slot,tab:bagTab});
  }
  md.innerHTML=h; bindDelete(); bindLoadouts();
}
function bagSwap(t){ bagTab=t; renderChar(); }
async function openSets(){
  view='sets'; curChar=null;
  gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const stash=await j('/api/stash');
  const owned=new Set((stash.unique_items||[]).map(x=>x.cid).filter(x=>x!=null));
  const md=document.getElementById('mid');
  let h='<h2>Item Sets <span class="muted">('+SETS_DB.length+' sets)</span></h2>';
  for(const s of SETS_DB){
    const own=s.pieces.filter(pc=>owned.has(pc.id)).length;
    const missing=s.pieces.filter(pc=>!owned.has(pc.id)).map(pc=>pc.id);
    h+=`<div class="setcard"><h3>${esc(s.name)} <span class="muted">${own}/${s.pieces.length} owned</span>`+
       (missing.length?`<button class="setadd" data-cids="${missing.join(',')}">Add missing (${missing.length}) to Unique tab</button>`:` <span class="muted" style="color:#3da55e">&#10004; complete</span>`)+`</h3>`;
    for(const pc of s.pieces){
      const r=CAT[pc.id];
      h+=`<span class="spiece ${owned.has(pc.id)?'own':'miss'}" data-cid="${pc.id}">${r&&r.spr?`<img src="/icons/${r.spr}.png?v=2" loading="lazy">`:''}<span class="r-${r?r.rar:'_'}">${esc(pc.name)}</span></span>`;
    }
    h+='</div>';
  }
  md.innerHTML=h;
  md.querySelectorAll('.setadd').forEach(b=>{
    b.onclick=async()=>{
      const cids=b.dataset.cids.split(',').map(Number);
      b.disabled=true;
      const r=await j('/api/addmany',{method:'POST',body:JSON.stringify({cids})});
      flash(r); openSets();
    };
  });
}
async function bindLoadouts(){
  const los=await j('/api/loadouts');
  const sel=document.getElementById('losel');
  Object.keys(los).sort().forEach(n=>{const o=document.createElement('option');o.value=n;
    o.textContent=`${n} (${los[n].items.length} items, ${los[n].created})`;sel.appendChild(o)});
  document.getElementById('losave').onclick=async()=>{
    const n=prompt('Loadout name:'); if(!n)return;
    flash(await j('/api/loadout',{method:'POST',body:JSON.stringify({action:'save',slot:curChar,name:n})}));
    renderChar();
  };
  document.getElementById('loapply').onclick=async()=>{
    const n=sel.value; if(!n){flash({err:'select a loadout first'});return}
    if(!confirm(`Replace ALL equipped items on this character with loadout "${n}"?`))return;
    flash(await j('/api/loadout',{method:'POST',body:JSON.stringify({action:'apply',slot:curChar,name:n})}));
    refresh();
  };
  document.getElementById('lodelete').onclick=async()=>{
    const n=sel.value; if(!n){flash({err:'select a loadout first'});return}
    if(!confirm(`Delete loadout "${n}"?`))return;
    flash(await j('/api/loadout',{method:'POST',body:JSON.stringify({action:'delete',name:n})}));
    renderChar();
  };
  document.getElementById('loexport').onclick=async()=>{
    const n=sel.value; if(!n){flash({err:'select a loadout first'});return}
    flash(await j('/api/loadout',{method:'POST',body:JSON.stringify({action:'export',name:n})}));
  };
  document.getElementById('loimport').onclick=()=>document.getElementById('lofile').click();
  document.getElementById('lofile').onchange=async(e)=>{
    const f=e.target.files[0]; if(!f)return;
    const txt=await f.text();
    let lo; try{lo=JSON.parse(txt)}catch(err){flash({err:'invalid file'});return}
    const n=prompt('Import as name:',lo.name||f.name.replace('.loadout.json',''));
    if(!n)return;
    flash(await j('/api/loadout',{method:'POST',body:JSON.stringify({action:'import',name:n,loadout:lo})}));
    renderChar();
  };
}
let finderRows=[];
async function openFinder(){
  view='finder'; curChar=null; gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const md=document.getElementById('mid');
  md.innerHTML=`<h2>Global Item Finder</h2>
    <div class="tool-intro"><b>Search items you already own</b> across every character, equipment slot, bag, shared stash and Infinite Vault. This tool is read-only.</div>
    <div class="finder-bar">
      <input id="ofq" placeholder="Type at least 2 characters..." aria-label="Search owned items" autofocus>
      <select id="ofrar"><option value="">All rarities</option><option>Angelic</option><option>Unholy</option><option>Heroic</option><option>Satanic</option><option>Runeword</option><option>Normal</option></select>
      <select id="ofscope"><option value="">Everywhere</option><option value="stash">Shared stash</option><option value="vault">Infinite Vault</option><option value="char">Characters</option></select>
      <button class="act" id="ofgo" style="margin:0">Search</button>
    </div>
    <div id="ofcount" class="finder-count">Enter an item name, rarity, type or location.</div>
    <div id="ofresults" class="finder-list"><div class="found-empty">No search has been run yet.</div></div>`;
  let timer=null;
  const run=async()=>{
    const q=document.getElementById('ofq').value.trim();
    if(q.length<2){finderRows=[];document.getElementById('ofcount').textContent='Enter at least 2 characters.';document.getElementById('ofresults').innerHTML='<div class="found-empty">Search is read-only and includes all local Season 10 characters and stash tabs.</div>';return;}
    document.getElementById('ofcount').textContent='Searching local saves...';
    const result=await j('/api/find?q='+encodeURIComponent(q));
    finderRows=result.items||[];
    renderFinderRows(result.total||0,result.limited);
  };
  document.getElementById('ofgo').onclick=run;
  document.getElementById('ofq').onkeydown=e=>{if(e.key==='Enter')run()};
  document.getElementById('ofq').oninput=()=>{clearTimeout(timer);timer=setTimeout(run,260)};
  document.getElementById('ofrar').onchange=()=>renderFinderRows(finderRows.length,false);
  document.getElementById('ofscope').onchange=()=>renderFinderRows(finderRows.length,false);
}
function renderFinderRows(total,limited){
  const host=document.getElementById('ofresults'); if(!host)return;
  const rarity=document.getElementById('ofrar').value;
  const scope=document.getElementById('ofscope').value;
  const rows=finderRows.map((row,index)=>({row,index})).filter(({row})=>(!rarity||row.rar===rarity)&&(!scope||row.target.view===scope));
  document.getElementById('ofcount').textContent=`${rows.length}${limited?' of '+total:''} matching location${rows.length===1?'':'s'}`;
  if(!rows.length){host.innerHTML='<div class="found-empty">No owned items match these filters.</div>';return;}
  host.innerHTML=rows.map(({row,index})=>`<div class="found-card">
    ${row.spr?`<img src="/icons/${row.spr}.png?v=2" loading="lazy">`:'<div class="found-icon">&#9671;</div>'}
    <div><div class="found-name r-${row.rar}">${esc(row.name)}${row.stack?` <span class="muted">x${row.stack}</span>`:''}</div><div class="found-loc">${esc(row.location)}${row.clsName?` &middot; ${esc(row.clsName)}`:''}</div></div>
    <button class="locate-btn" data-find-index="${index}">LOCATE</button></div>`).join('');
  host.querySelectorAll('[data-find-index]').forEach(btn=>btn.onclick=()=>locateOwnedItem(+btn.dataset.findIndex));
}
async function locateOwnedItem(index){
  const row=finderRows[index]; if(!row)return;
  const target=row.target||{};
  if(target.view==='stash'){
    document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('sel'));
    document.querySelector('[data-view=stash]').classList.add('sel');
    await openStash();
  }else if(target.view==='vault'){
    document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('sel'));
    document.querySelector('[data-view=vault]').classList.add('sel');
    vaultState.collectionId=target.collectionId||'all';vaultState.q=row.id||'';vaultState.offset=0;vaultState.highlightItem=target.itemId||row.id;
    await openVault(false);
  }else if(target.view==='char'){
    document.querySelectorAll('.tabbtn').forEach(x=>x.classList.remove('sel'));
    if(target.section==='bag'||target.section==='personal_stash')bagTab=target.tab;
    const charButton=[...document.querySelectorAll('.charbtn')].find(b=>+b.dataset.slot===+target.slot);
    await openChar(+target.slot,charButton||null);
  }
  await new Promise(resolve=>setTimeout(resolve,30));
  const item=target.view==='vault'?[...document.querySelectorAll('[data-vault-id]')].find(el=>el.dataset.vaultId===(target.itemId||row.id||'')):[...document.querySelectorAll('[data-key]')].find(el=>el.dataset.key===target.key);
  if(item){item.scrollIntoView({behavior:'smooth',block:'center',inline:'center'});item.classList.add('found-pulse');setTimeout(()=>item.classList.remove('found-pulse'),3800);return;}
  const heading=[...document.querySelectorAll('[data-find-tab]')].find(el=>el.dataset.findTab===target.tab);
  if(heading){heading.scrollIntoView({behavior:'smooth',block:'center'});heading.classList.add('found-pulse');setTimeout(()=>heading.classList.remove('found-pulse'),3800);}
}

async function openHealth(){
  view='health'; curChar=null; gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const md=document.getElementById('mid');
  md.innerHTML='<h2>Save Health Check</h2><div class="tool-intro"><b>Read-only scan in progress...</b> No save file is changed during a scan.</div>';
  renderHealth(await j('/api/health'));
}
function renderHealth(report){
  const md=document.getElementById('mid');
  if(report.err){md.innerHTML=`<h2>Save Health Check</h2><div class="health-issue error"><div class="health-sev">ERROR</div><div class="health-msg">${esc(report.err)}</div></div>`;return;}
  const s=report.summary||{files:0,items:0,errors:0,warnings:0,fixable:0};
  const recoveries=report.recoveries||[];
  const vaultRecoveryBatches=report.vaultRecoveryBatches||[];
  let h=`<h2>Save Health Check</h2>
    <div class="tool-intro"><b>Season 10 preflight validation.</b> Scans save encoding, item addresses, grid placement, equipment slots, sockets and stack values. A normal scan never writes to disk.</div>
    <div class="health-summary">
      <div class="health-metric"><span>Files scanned</span><b>${s.files}</b></div>
      <div class="health-metric"><span>Items scanned</span><b>${s.items}</b></div>
      <div class="health-metric error"><span>Errors</span><b>${s.errors}</b></div>
      <div class="health-metric warn"><span>Warnings</span><b>${s.warnings}</b></div>
      <div class="health-metric fix"><span>Safe fixes</span><b>${s.fixable}</b></div>
    </div>
    <div class="health-actions"><button class="act" id="healthscan" style="margin:0">Scan again</button><button class="act" id="healthfix" style="margin:0" ${(s.fixable===0||report.gameRunning)?'disabled':''}>Fix safe issues</button><span class="health-state">${report.gameRunning?'Hero Siege is running — repairs are locked.':'Repairs create a backup before every changed file.'}</span></div>`;
  recoveries.forEach((recovery,index)=>{
    const repairs=(recovery.repairs||[]).map(row=>`<li>${esc(row.message||row.code)} <span class="muted">(${row.count||1})</span></li>`).join('');
    h+=`<div class="recovery-card"><h3>&#9888; ${esc(recovery.file)} can be recovered safely</h3>
      <p>The editor matched the proven <b>${esc(recovery.profile)}</b> profile. The validated preview preserves <b>${recovery.itemRecords||0}/${recovery.itemRecords||0}</b> item records.</p>
      <div class="recovery-facts"><span>${recovery.topLevelFields||0} top-level fields</span><span>${recovery.nonzeroHighByteCount||0} encoding anomalies</span><span>SHA-256 ${esc((recovery.sourceSha256||'').slice(0,12))}...</span></div>
      <ul class="recovery-repairs">${repairs}</ul>
      <button class="act recovery-button" id="hssrecover${index}" style="margin:0" ${recovery.canApply?'':'disabled'}>RECOVER ${esc((recovery.file||'stash.hss').toUpperCase())}</button>
      <span class="health-state" style="margin-left:9px">${recovery.canApply?'Creates an exact timestamped backup, then verifies the replacement.':'Close Hero Siege before recovery.'}</span></div>`;
  });
  vaultRecoveryBatches.forEach(row=>{
    h+=`<div class="recovery-card"><h3>&#9888; Infinite Vault ownership needs a decision</h3>
      <p>The ${row.direction==='deposit'?'stash-to-Vault deposit':'Vault-to-stash return'} journal contains <b>${row.itemCount}</b> protected item records, but the current stash only provides mixed evidence.</p>
      <p><b>Ownership-safe choice:</b> make every protected record available in Infinite Vault. This never deletes or rewrites Shared Stash; copies already there may remain as duplicates.</p>
      <button class="act recovery-button" data-health-vault-resolve="${attr(row.requestId)}" data-count="${row.itemCount}" data-direction="${attr(row.direction)}" ${report.gameRunning?'disabled':''}>PRESERVE VAULT OWNERSHIP</button>
      <span class="health-state" style="margin-left:9px">${report.gameRunning?'Close Hero Siege before resolving ownership.':'Shared Stash remains untouched.'}</span></div>`;
  });
  if(report.fixed)h+=`<div class="tool-intro" style="border-color:rgba(82,221,169,.35);color:#72dfb7"><b>${report.fixed} safe repair(s) applied.</b> Backups: ${esc((report.backups||[]).join(', ')||'none')}</div>`;
  if(!(report.issues||[]).length)h+='<div class="health-clean"><b>&#10003; Save structure looks healthy</b>No structural Season 10 issues were detected.</div>';
  else h+='<div class="health-list">'+report.issues.map(issue=>`<div class="health-issue ${issue.severity}"><div class="health-sev">${esc(issue.severity.toUpperCase())}</div><div><div class="health-msg">${esc(issue.message)} ${issue.fixable?'<span class="health-fixable">SAFE FIX</span>':''}</div><div class="health-loc">${esc(issue.location)} &middot; ${esc(issue.file)}${issue.item?' &middot; '+esc(issue.item):''}</div></div></div>`).join('')+'</div>';
  md.innerHTML=h;
  document.getElementById('healthscan').onclick=openHealth;
  const fix=document.getElementById('healthfix');
  if(fix)fix.onclick=async()=>{
    if(!confirm(`Apply ${s.fixable} safe repair(s)?\nA backup will be created before each changed file.`))return;
    fix.disabled=true;fix.textContent='Repairing...';
    renderHealth(await j('/api/health/fix',{method:'POST',body:'{}'}));
  };
  recoveries.forEach((recovery,index)=>{
    const button=document.getElementById(`hssrecover${index}`);if(!button)return;
    button.onclick=async()=>{
      if(!confirm(`Recover ${recovery.file}?\n\nThe validated preview preserves ${recovery.itemRecords||0}/${recovery.itemRecords||0} item records.\nThe current file will be copied to a timestamped backup before replacement.\n\nHero Siege must remain closed.`))return;
      button.disabled=true;button.textContent='RECOVERING...';
      const result=await j('/api/health/recover',{method:'POST',body:JSON.stringify({file:recovery.file,expectedSha256:recovery.sourceSha256})});
      if(result.err)alert(result.err);else alert(`${result.ok}\n${result.itemRecordsPreserved||0} item records preserved.\nBackup: ${result.backup||'created'}`);
      await openHealth();
    };
  });
  md.querySelectorAll('[data-health-vault-resolve]').forEach(button=>button.onclick=()=>preserveVaultBatchOwnership(button.dataset.healthVaultResolve,+button.dataset.count,button.dataset.direction,'health'));
}

async function openBackups(){
  view='backups'; curChar=null;
  gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const baks=await j('/api/backups');
  const md=document.getElementById('mid');
  let h=`<h2>Backups <span class="muted">(${baks.length} latest)</span></h2>
  <div class="muted" style="margin-bottom:8px">Every change made by this editor creates one of these automatically. Restoring also backs up the current state first.</div>`;
  h+='<table style="border-collapse:collapse;font-size:12px">';
  h+='<tr style="color:#937f6a;text-align:left"><th style="padding:4px 14px 4px 0">Time</th><th style="padding:4px 14px 4px 0">File</th><th style="padding:4px 14px 4px 0">Type</th><th style="padding:4px 14px 4px 0">Size</th><th></th></tr>';
  for(const b of baks){
    const ts=`${b.ts.slice(6,8)}.${b.ts.slice(4,6)}.${b.ts.slice(0,4)} ${b.ts.slice(9,11)}:${b.ts.slice(11,13)}:${b.ts.slice(13,15)}`;
    const kind=b.kind==='pre_recovery'?'Recovery source':'Automatic Backup';
    h+=`<tr style="border-top:1px solid #2a1518"><td style="padding:4px 14px 4px 0">${ts}</td><td style="padding:4px 14px 4px 0">${esc(b.target)}</td><td style="padding:4px 14px 4px 0" class="muted">${kind}</td><td style="padding:4px 14px 4px 0" class="muted">${(b.size/1024).toFixed(1)} KB</td>
    <td><button class="act" style="margin:0;padding:3px 10px;font-size:11px" data-bak="${esc(b.file)}">Restore</button></td></tr>`;
  }
  h+='</table>';
  md.innerHTML=h;
  md.querySelectorAll('[data-bak]').forEach(btn=>{
    btn.onclick=async()=>{
      if(!confirm(`Restore ${btn.dataset.bak}?\nCurrent state will be backed up first.`))return;
      flash(await j('/api/restorebak',{method:'POST',body:JSON.stringify({file:btn.dataset.bak})}));
      openBackups();
    };
  });
}
function openRunewords(){
  view='runewords'; curChar=null;
  gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const md=document.getElementById('mid');
  let h=`<h2>Runeword Builder <span class="muted">(${RW_DB.length} runewords)</span></h2>
  <div class="muted" style="margin-bottom:8px">Forges a completed runeword on the compatible base you choose, with the correct runes socketed. Hover a name for its stats.</div>
  <div class="flex" style="margin-bottom:10px">Target: <select id="rwtab">${[1,2,3,4,5,6,7,8,9].map(i=>`<option value="stash_tab_${i}">Stash tab ${i}</option>`).join('')}</select>
  <input id="rwq" placeholder="filter runewords..." style="flex:1;max-width:240px"></div>
  <div id="rwlist"></div>`;
  md.innerHTML=h;
  const render=()=>{
    const q=(document.getElementById('rwq').value||'').toLowerCase();
    document.getElementById('rwlist').innerHTML=RW_DB.filter(r=>!q||r.name.toLowerCase().includes(q)).map(r=>{
      const bases=r.bases||[];
      const availableBases=bases.filter(base=>base.available!==false&&!base.disabled);
      const firstAvailable=availableBases.length?availableBases[0].cid:null;
      const baseOptions=bases.map(base=>{
        const subtype=base.cls===3?`${SUBN[base.sub]||base.sub} · `:'';
        const available=base.available!==false&&!base.disabled;
        const roll=base.rollMode==='exact'?'EXACT MAX':base.rollMode==='best'?'BEST POSSIBLE':base.rollMode==='fixed'?'FIXED':'UNAVAILABLE';
        const reason=!available&&base.unavailableReason?` · ${base.unavailableReason}`:'';
        return `<option value="${base.cid}" ${available?'':'disabled'} ${available&&base.cid===firstAvailable?'selected':''}>${esc(subtype+(base.name||base.key||'Base')+` · b${base.b} · ${roll}${reason}`)}</option>`;
      }).join('');
      const unavailable=!availableBases.length&&r.unavailableReason?` · ${r.unavailableReason}`:'';
      return `<div class="rwcard"><div class="rwhead"><span class="rwname r-Runeword" data-cid="${r.cid??''}">${esc(r.name)}</span><span class="rwtarget">${esc(r.target||'')} · ${availableBases.length}/${bases.length} verified base${bases.length===1?'':'s'}${esc(unavailable)}</span></div><span class="rwrunes">`+
      r.runes.map(rn=>`<span class="rwrune">${rn.spr?`<img src="/icons/${rn.spr}.png?v=2" loading="lazy">`:''}${esc(rn.name)}</span>`).join('')+
      `</span><select class="rwbase" data-rwbase="${r.rw}" ${availableBases.length?'':'disabled'}>${baseOptions||'<option>No valid base</option>'}</select><button class="forgebtn" data-rw="${r.rw}" ${availableBases.length?'':'disabled'}>Forge</button></div>`;
    }).join('');
    document.querySelectorAll('.forgebtn').forEach(b=>{
      b.onclick=async()=>{
        b.disabled=true;
        const base=document.querySelector(`[data-rwbase="${b.dataset.rw}"]`);
        const requestId=(globalThis.crypto&&crypto.randomUUID)?crypto.randomUUID():`${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const r=await j('/api/forge',{method:'POST',body:JSON.stringify({rw:+b.dataset.rw,baseCid:+base.value,tab:document.getElementById('rwtab').value,requestId})});
        flash(r); b.disabled=false;
      };
    });
  };
  document.getElementById('rwq').addEventListener('input',render);
  render();
}
let STACKABLES=[],S10_ACCESS=[];
async function openStackables(){
  view='stackables'; curChar=null;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  if(!STACKABLES.length) STACKABLES=await j('/api/stackables');
  if(!S10_ACCESS.length) S10_ACCESS=await j('/api/s10access');
  const md=document.getElementById('mid');
  const CATS=[{cls:12,label:'Keys'},{cls:13,label:'Boss Parts / Tarot'},{cls:14,label:'Materials'},{cls:15,label:'Runes / Gems / Orbs'}];
  const charOpts=chars.map(c=>`<option value="${c.slot}">${esc(c.name)} (slot ${c.slot})</option>`).join('');
  const accessCards=S10_ACCESS.map(g=>`<div class="access-card"><h4>${esc(g.name)}</h4><p>${esc(g.description)}</p><div class="access-items">${g.items.map(x=>`<div class="access-item"><b>${esc(x.name)}</b><span>${esc(x.boss||x.destination||'')}</span></div>`).join('')}</div><button class="act" data-access="${esc(g.id)}" ${chars.length?'':'disabled'}>Generate Complete Kit</button></div>`).join('');
  let h=`<h2>Season 10 Access &amp; Materials <span class="muted">(${STACKABLES.length} items)</span></h2>
  <div class="access-hero"><div class="access-title"><h3>&#9760; Season 10 Boss &amp; Dungeon Access</h3><span>GAME DATA VERIFIED</span></div>
  <div class="muted">The three tablets are the actual access items for Phantom Leviathan, Captain Grimtide and Blood Maiden. Generate a complete kit directly into a character's Key bag.</div>
  <div class="jlrow"><label>Character</label><select id="akchar" ${chars.length?'':'disabled'}>${charOpts||'<option>No character found</option>'}</select><label style="min-width:auto;margin-left:8px">Each stack</label><input id="akamount" type="number" value="10" min="1" max="99999" style="width:100px"></div>
  <div class="access-grid">${accessCards}</div><div id="akmsg" class="muted" style="margin-top:12px"></div></div>
  <hr class="section-rule"><h3 style="margin:0 0 8px">Individual Keys &amp; Materials</h3>
  <div class="tipbar">&#128230; Season 10 routing: <b>Keys</b>, <b>Boss Parts/Tarot</b>, <b>Materials</b> and <b>Runes/Gems/Orbs</b>. One slot holds the whole stack. Items go to their matching dedicated bag.</div>
  <div class="jlrow"><label>Category</label><select id="scat">${CATS.map(c=>`<option value="${c.cls}">${c.label}</option>`).join('')}</select></div>
  <div class="jlrow"><label>Item</label><select id="sitem" style="min-width:300px"></select></div>
  <div class="jlrow"><label>Target</label><select id="stgt"></select></div>
  <div class="jlrow"><label>Amount</label><input id="samt" type="number" value="999" min="1" max="99999" style="width:120px"> <span class="muted">how many in the stack (e.g. 999)</span></div>
  <div class="jlrow"><button class="act" id="sgen" style="margin:0">Generate</button></div>
  <div id="smsg" class="muted" style="margin-top:12px;max-width:760px"></div>`;
  md.innerHTML=h;
  md.querySelectorAll('[data-access]').forEach(btn=>{
    btn.onclick=async()=>{
      btn.disabled=true;
      const body={group:btn.dataset.access,slot:+document.getElementById('akchar').value,
        amount:Math.max(1,Math.min(99999,+document.getElementById('akamount').value||10))};
      const r=await j('/api/makes10access',{method:'POST',body:JSON.stringify(body)});
      document.getElementById('akmsg').innerHTML=r.err?`<span style="color:#ff7060">${esc(r.err)}</span>`:`<span style="color:#54e87a">${esc(r.ok)}</span>`;
      btn.disabled=false;
    };
  });
  let stgts=[];
  function fillItems(){
    const cls=+document.getElementById('scat').value;
    const items=STACKABLES.filter(x=>x.cls===cls);
    document.getElementById('sitem').innerHTML=items.map(x=>`<option value="${x.cid}">${esc(x.name)}</option>`).join('');
    const tab = cls===12?'inventory_key_tab':(cls===15?'inventory_socket_tab':'inventory_material_tab');
    stgts=[];
    if(cls===13||cls===14) stgts.push({label:'Stash — Material tab', t:{type:'stash',tab:'material_tab'}});
    if(cls===15) stgts.push({label:'Stash — Socket tab', t:{type:'stash',tab:'socket_tab'}});
    chars.forEach(c=>stgts.push({label:`${c.name} (slot ${c.slot}) — ${BAG_LABELS[tab]} bag`, t:{type:'bag',slot:c.slot,tab:tab}}));
    document.getElementById('stgt').innerHTML=stgts.map((o,i)=>`<option value="${i}">${esc(o.label)}</option>`).join('');
  }
  document.getElementById('scat').addEventListener('change',fillItems); fillItems();
  function msg(r){document.getElementById('smsg').innerHTML=r.err?`<span style="color:#ff7060">${esc(r.err)}</span>`:`<span style="color:#54e87a">${esc(r.ok)}</span>`;}
  document.getElementById('sgen').onclick=async()=>{
    const tgt=stgts[+document.getElementById('stgt').value].t;
    const body={target:tgt, cid:+document.getElementById('sitem').value,
      amount:Math.max(1,Math.min(99999,+document.getElementById('samt').value||999)), count:1};
    msg(await j('/api/makestackable',{method:'POST',body:JSON.stringify(body)}));
  };
}
let RELICS=[];
async function openRelics(){
  view='relics'; curChar=null;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  if(!RELICS.length) RELICS=await j('/api/relics');
  const md=document.getElementById('mid');
  if(!chars.length){md.innerHTML='<h2>Relic Lab</h2><div class="muted">No characters found.</div>';return;}
  const charOpts=chars.map(c=>`<option value="${c.slot}">${esc(c.name)} (slot ${c.slot})</option>`).join('');
  const relOpts=RELICS.map(r=>`<option value="${r.cid}">${esc(r.name)}${r.tag?'  —  '+esc(r.tag):''}</option>`).join('');
  let h=`<h2>Relic Lab <span class="muted">(${RELICS.length} relics)</span></h2>
  <div class="tipbar">&#128302; Relics can ONLY live in the 5 Relic slots (never in bag/stash). This writes the relic straight into a Relic slot, <b>replacing</b> whatever is there. <b>Level</b> 1-10 = power (e.g. element Globe at lvl 10 = <b>+5 to that element's skills</b> &amp; +20% skill damage).</div>
  <div class="jlrow"><label>Character</label><select id="rchar">${charOpts}</select></div>
  <div id="rcur" class="muted" style="margin:2px 0 12px;max-width:820px">…</div>
  <div class="jlrow"><label>Relic</label><select id="rsel" style="min-width:340px">${relOpts}</select></div>
  <div class="jlrow"><label>Level</label><input id="rlvl" type="number" value="10" min="1" max="10" style="width:80px"> <span class="muted">1-10</span></div>
  <div class="jlrow"><label>Slot</label><select id="rslot"><option value="10">Relic 1</option><option value="11">Relic 2</option><option value="12">Relic 3</option><option value="13">Relic 4</option><option value="14">Relic 5</option></select></div>
  <div class="jlrow"><button class="act" id="rgen" style="margin:0">Place Relic</button></div>
  <div id="rmsg" class="muted" style="margin-top:12px;max-width:820px"></div>`;
  md.innerHTML=h;
  async function showCur(){
    const slot=+document.getElementById('rchar').value;
    const cd=await j('/api/char/'+slot);
    const rel=(cd.equipped||[]).filter(e=>e.g>=10&&e.g<=14).sort((a,b)=>a.g-b.g);
    const map={}; rel.forEach(e=>map[e.g]=e);
    let parts=[];
    for(let g=10;g<=14;g++){const e=map[g];
      parts.push(`<b>Relic ${g-9}</b>: ${e?esc(e.name)+(e.relicLevel!=null?` (lvl ${e.relicLevel})`:''):'<span class="muted">empty</span>'}`);}
    document.getElementById('rcur').innerHTML='Current relic slots — '+parts.join(' &nbsp;·&nbsp; ');
  }
  document.getElementById('rchar').addEventListener('change',showCur); showCur();
  function msg(r){document.getElementById('rmsg').innerHTML=r.err?`<span style="color:#ff7060">${esc(r.err)}</span>`:`<span style="color:#54e87a">${esc(r.ok)}</span>`;}
  document.getElementById('rgen').onclick=async()=>{
    const body={slot:+document.getElementById('rchar').value, cid:+document.getElementById('rsel').value,
      level:Math.max(1,Math.min(10,+document.getElementById('rlvl').value||10)), g:+document.getElementById('rslot').value};
    const r=await j('/api/makerelic',{method:'POST',body:JSON.stringify(body)});
    msg(r); if(!r.err) showCur();
  };
}
async function openChar(slot,btn){
  resetPreviewModels();
  view='char'; curChar=slot; charData=await j('/api/char/'+slot);
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  if(btn)btn.classList.add('sel');
  renderChar(); renderTargets();
}
function bindDelete(){
  document.querySelectorAll('[data-del]').forEach(el=>{
    el.oncontextmenu=(e)=>{
      e.preventDefault();
      showCtx(e.clientX,e.clientY,JSON.parse(el.dataset.del),el.dataset.key,el);
    };
  });
}
let ctxEl=null;
function showCtx(x,y,target,key,el){
  let m=document.getElementById('ctxmenu');
  if(!m){m=document.createElement('div');m.id='ctxmenu';document.body.appendChild(m);
    document.addEventListener('click',()=>{m.style.display='none'});}
  const isEq=target.type==='equipped';
  const acts=[];
  if(target.type==='stash'&&isVaultTransferTab(target.tab))acts.push(['Store in Infinite Vault...','VAULT','']);
  if(!isEq)acts.push(['Duplicate','duplicate','']);
  let rollProfile=null;
  try{rollProfile=JSON.parse((el&&el.dataset&&el.dataset.roll)||'null')}catch(e){}
  let skillSelector=null;
  try{skillSelector=JSON.parse((el&&el.dataset&&el.dataset.skill)||'null')}catch(e){}
  if(skillSelector)acts.push([skillSelector.targetKind==='class'?'Choose class...':skillSelector.targetKind==='subskill'?'Choose subskill target...':'Choose skill target...','SKILL','']);
  else if(rollProfile&&['exact','best'].includes(rollProfile.mode))acts.push([`Apply ${rollProfile.rollLabel||(rollProfile.mode==='exact'?'EXACT MAX':'BEST POSSIBLE')}`,'perfect','']);
  acts.push(['Random reroll','reroll','']);
  const socketLimit=Number.parseInt((el&&el.dataset&&el.dataset.socketLimit)||'0',10);
  if(Number.isFinite(socketLimit)&&socketLimit>0)acts.push(['Edit sockets...','SOCKETS','']);
  if(!isEq)acts.push(['Edit stack...','setstack','']);
  acts.push(['Delete','DELETE','danger']);
  m.innerHTML=acts.map(([lbl,act,cls])=>`<div class="${cls}" data-act="${act}">${lbl}</div>`).join('');
  m.querySelectorAll('div').forEach(d=>{
    d.onclick=async()=>{
      m.style.display='none';
      const act=d.dataset.act;
      let r;
      if(act==='DELETE'){
        if(!confirm('DELETE this item?'))return;
        r=await j('/api/delete',{method:'POST',body:JSON.stringify({target,key})});
      }else if(act==='VAULT'){
        openVaultDepositDialog(target,key,el);return;
      }else if(act==='SOCKETS'){
        openSocketEditor(target,key,el);return;
      }else if(act==='SKILL'){
        openSkillTargetEditor(target,key,skillSelector);return;
      }else if(act==='setstack'){
        const n=prompt('New stack count:','999');
        if(n==null)return;
        r=await j('/api/modify',{method:'POST',body:JSON.stringify({action:'setstack',target,key,count:+n})});
      }else{
        r=await j('/api/modify',{method:'POST',body:JSON.stringify({action:act,target,key})});
      }
      flash(r); refresh();
    };
  });
  m.style.display='block';
  m.style.left=Math.max(6,Math.min(x,innerWidth-m.offsetWidth-8))+'px';
  m.style.top=Math.max(6,Math.min(y,innerHeight-m.offsetHeight-8))+'px';
}
function refresh(){ if(view==='stash')openStash(); else if(view==='vault')openVault(false); else if(view==='sets')openSets(); else if(view==='char')openChar(curChar,document.querySelector('.charbtn.sel')) }
function search(){
  const q=document.getElementById('q').value.toLowerCase();
  const fk=document.getElementById('fkind').value, fc=document.getElementById('fcls').value;
  const fr=document.getElementById('frar').value, fs=document.getElementById('fset').value;
  const fst=document.getElementById('fstat').value.toLowerCase();
  // runeword (cls<0) girdileri buradan eklenmez -> Runeword Builder kullanilir
  const out=CAT.filter(r=>r.available!==false&&r.kind!=='runeword'&&(!q||r.name.toLowerCase().includes(q)||(r.key||'').includes(q))&&(!fk||r.kind===fk)&&(fc===''||String(r.cls)===fc)&&(!fr||r.rar===fr)&&(fs===''||(fs==='any'?r.set!==undefined:r.set===+fs))&&(!fst||(r.stats||[]).some(([l,v])=>l.toLowerCase().includes(fst)))).slice(0,100);
  const rd=document.getElementById('results'); rd.innerHTML='';
  out.forEach(r=>{const d=document.createElement('div');d.className='res'+(sel&&sel.id===r.id?' sel':'');
    d.draggable=true; d.dataset.cid=r.id; d.dataset.skill=JSON.stringify(r.skillSelector||null);
    let statHint='';
    if(fst){const hit=(r.stats||[]).find(([l,v])=>l.toLowerCase().includes(fst));
      if(hit)statHint=` <span class="muted" style="color:#8fb7ff">${esc(hit[1])} ${esc(hit[0])}</span>`;}
    d.innerHTML=`${r.spr?`<img src="/icons/${r.spr}.png?v=2" loading="lazy">`:''}<span class="r-${r.rar}">${esc(r.name)}</span>${statHint}`;
    d.onclick=()=>{sel=r;document.querySelectorAll('.res').forEach(x=>x.classList.remove('sel'));d.classList.add('sel');renderTargets()};
    d.addEventListener('dragstart',e=>{
      dragInfo={mode:'add',cid:r.id,w:r.w||1,h:r.h||1,targetId:DICE_ADD_SELECTION[r.id]};
      e.dataTransfer.effectAllowed='copy';
    });
    rd.appendChild(d)});
}
async function renderTargets(){
  const si=document.getElementById('selinfo'), tr=document.getElementById('targetrow'), btn=document.getElementById('addbtn');
  if(!sel){si.textContent='No item selected';tr.innerHTML='';btn.disabled=true;return}
  const selectedRow=sel, selectedId=selectedRow.id;
  tr.innerHTML=''; btn.disabled=true; btn.onclick=null;
  const rp=selectedRow.rollProfile||null;
  const diceSelector=selectedRow.skillSelector||null;
  const ordinarySmallCharm=isOrdinarySmallCharm(selectedRow);
  const actionable=!ordinarySmallCharm&&rp&&['exact','best'].includes(rp.mode);
  const rollText=ordinarySmallCharm
    ?'Random rarity & affixes · native seed'
    :diceSelector
    ?(diceSelector.available
      ?(diceSelector.targetKind==='class'?'Choose exact class target · 4/4 stats MAX · 2/2 sockets':`Choose exact ${diceSelector.targetKind==='subskill'?'subskill':'skill'} target`)
      :`${diceSelector.targetKind==='class'?'Class':'Skill'} targets disabled · ${diceSelector.message||'database unavailable'}`)
    :(!rp?'Random roll · no verified profile':rp.mode==='fixed'?'Fixed definition stats · no roll needed':`${rp.rollLabel||(rp.mode==='exact'?'EXACT MAX':'BEST POSSIBLE')} · ${rp.maxed}/${rp.total} MAX`);
  si.innerHTML=`Selected: <span class="r-${selectedRow.rar}">${esc(selectedRow.name)}</span> <span class="muted">${selectedRow.w}x${selectedRow.h}</span> <span style="color:${(rp||diceSelector&&diceSelector.available)?'#74ee98':'#e0a05b'}">&middot; ${esc(rollText)}</span>`;
  let dicePayload=null;
  if(diceSelector&&diceSelector.available){
    dicePayload=DICE_TARGET_CACHE[diceSelector.profileId];
    if(!dicePayload){
      dicePayload=await j('/api/skill-targets?profile='+encodeURIComponent(diceSelector.profileId));
      if(dicePayload.err){si.innerHTML+=`<br><span style="color:#ff7060">${esc(dicePayload.err)}</span>`;dicePayload=null}
      else DICE_TARGET_CACHE[diceSelector.profileId]=dicePayload;
    }
    if(!sel||sel.id!==selectedId)return;
  }
  const options=[], seen=new Set();
  const addOpt=(target,label)=>{const value=JSON.stringify(target);if(!seen.has(value)){seen.add(value);options.push({value,label})}};
  if(selectedRow.kind==='unique')addOpt({type:'stash_unique'},'Stash > Unique tab');
  if(view==='char'&&curChar!==null){
    let preferred=null;
    if(selectedRow.cls===10)preferred='inventory_charms';
    else if(selectedRow.cls===12)preferred='inventory_key_tab';
    else if(selectedRow.cls===13)preferred=((selectedRow.b>=19&&selectedRow.b<=40)||(selectedRow.b>=54&&selectedRow.b<=57))?'inventory_tarot_tab':'inventory_material_tab';
    else if(selectedRow.cls===14)preferred='inventory_material_tab';
    else if(selectedRow.cls===15)preferred='inventory_socket_tab';
    else if(selectedRow.cls===16)preferred='inventory_relic_tab';
    else if(selectedRow.cls===19)preferred='inventory_vault_tab';
    if(preferred)addOpt({type:'bag',slot:curChar,tab:preferred},`Recommended: ${BAG_LABELS[preferred]||preferred}`);
    if(selectedRow.cls===18)addOpt({type:'potions',slot:curChar},'Recommended: Potion belt');
    for(let i=0;i<5;i++)addOpt({type:'bag',slot:curChar,tab:`inventory_tab_${i}`},`Bag: ${i===0?'Main':'Extra '+i}`);
    addOpt({type:'personal_stash',slot:curChar},'Personal Stash');
    for(const g in SLOTS)if((ACCEPT[g]||[]).includes(selectedRow.cls))addOpt({type:'equip',slot:curChar,g:+g},`EQUIP: ${SLOTS[g]}`);
  }
  for(let i=1;i<=9;i++)addOpt({type:'stash',tab:`stash_tab_${i}`},`Stash tab ${i}`);
  let skillHtml='';
  if(dicePayload&&dicePayload.targets&&dicePayload.targets.length){
    const rows=[...dicePayload.targets].sort((a,b)=>Number(a.classId)-Number(b.classId)||a.name.localeCompare(b.name));
    let chosen=DICE_ADD_SELECTION[selectedId];
    if(!rows.some(row=>Number(row.id)===Number(chosen)))chosen=Number(rows[0].id);
    DICE_ADD_SELECTION[selectedId]=chosen;
    if(diceSelector.targetKind==='class'){
      skillHtml=`<select id="skilladdselect" title="Chosen class target">${rows.sort((a,b)=>Number(a.id)-Number(b.id)).map(row=>`<option value="${row.id}" ${Number(row.id)===chosen?'selected':''}>${esc(row.name)} · Class ID ${row.id}</option>`).join('')}</select>`;
    }else{
      const groups=new Map();
      rows.forEach(row=>{if(!groups.has(row.className))groups.set(row.className,[]);groups.get(row.className).push(row)});
      skillHtml=`<select id="skilladdselect" title="Chosen skill target">${[...groups.entries()].map(([className,classRows])=>`<optgroup label="${esc(className)}">${classRows.map(row=>`<option value="${row.id}" ${Number(row.id)===chosen?'selected':''}>${esc(row.name)} · ID ${row.id}</option>`).join('')}</optgroup>`).join('')}</select>`;
    }
  }
  tr.innerHTML=skillHtml+`<select id="tsel">${options.map(o=>`<option value='${esc(o.value)}'>${esc(o.label)}</option>`).join('')}</select>`;
  const skillSelect=document.getElementById('skilladdselect');
  if(skillSelect)skillSelect.onchange=()=>{DICE_ADD_SELECTION[selectedId]=Number(skillSelect.value)};
  btn.disabled=options.length===0||Boolean(diceSelector&&!skillSelect);
  btn.textContent=ordinarySmallCharm?'Add · RANDOM ROLL':diceSelector?(diceSelector.targetKind==='class'?'Add · CHOSEN CLASS':'Add · CHOSEN SKILL'):(actionable?`Add · ${rp.rollLabel||(rp.mode==='exact'?'EXACT MAX':'BEST POSSIBLE')}`:(rp&&rp.mode==='fixed'?'Add · FIXED STATS':'Add · RANDOM ROLL'));
  btn.onclick=async()=>{
    if(btn.disabled)return;
    btn.disabled=true;
    const t=JSON.parse(document.getElementById('tsel').value);
    const body={cid:selectedId,target:t};
    const chosen=document.getElementById('skilladdselect');
    if(chosen)body.targetId=Number(chosen.value);
    try{
      const r=await j('/api/add',{method:'POST',body:JSON.stringify(body)});
      flash(r); refresh();
    }finally{
      if(sel&&sel.id===selectedId)btn.disabled=false;
    }
  };
}
function flash(r){const m=document.getElementById('msg');
  m.innerHTML=r.err?`<span style="color:#ff7060">${esc(r.err)}</span>`:`<span style="color:#54e87a">${esc(r.ok)}</span> <span class="muted">backup: ${esc(r.backup||'')}</span>`}
['q','fkind','fcls','frar','fset','fstat'].forEach(id=>document.getElementById(id).addEventListener('input',search));
boot();
</script></body></html>"""


def _open_window(port: int) -> bool:
    """Open the UI and report whether a blocking native window was used.

    The distinction matters because ``webview.start()`` owns the process until
    its window closes, while ``webbrowser.open()`` returns immediately.  The
    caller must keep the HTTP server alive for the browser fallback.
    """
    url = f"http://127.0.0.1:{port}"
    try:
        import webview
        webview.create_window(f"Hero Siege Item Editor {APP_VERSION}", url,
                              width=1480, height=920, min_size=(1100, 680))
        webview.start()
        return True
    except Exception:
        import webbrowser
        webbrowser.open(url)
        return False


def _editor_identity(port: int, timeout: float = 1.0):
    """Return a validated local Item Editor identity, including legacy peers."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/instance", timeout=timeout) as response:
            identity = json.loads(response.read())
            if not isinstance(identity, dict) or identity.get("application") != APPLICATION_ID:
                return None
            version = identity.get("version")
            if not isinstance(version, str) or not version:
                return None
            pid = identity.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                pid = None
            return {"version": version, "pid": pid, "port": int(port)}
    except Exception:
        return None


def _served_version(port: int):
    """Return the version of an existing editor server, if the port is ours."""
    identity = _editor_identity(port)
    return identity["version"] if identity else None


def _peer_editor_error(
    exclude_port: int | None = None, *, exclude_ports=()
) -> str | None:
    """Find another editor process, treating PID-less legacy builds as peers."""
    own_pid = os.getpid()
    excluded = set(exclude_ports)
    if exclude_port is not None:
        excluded.add(exclude_port)
    for candidate in range(PORT, PORT + 10):
        if candidate in excluded:
            continue
        identity = _editor_identity(candidate, timeout=0.2)
        if identity is None or identity.get("pid") == own_pid:
            continue
        pid_text = f", PID {identity['pid']}" if identity.get("pid") else ""
        return (
            f"Another Hero Siege Item Editor (v{identity['version']}{pid_text}) "
            f"is running on port {candidate}. Close it before editing saves."
        )
    return None


def _active_peer_editor_error() -> str | None:
    if not INSTANCE_GUARD_ACTIVE:
        return None
    if INSTANCE_RESERVED_PORTS:
        return _peer_editor_error(exclude_ports=INSTANCE_RESERVED_PORTS)
    return _peer_editor_error(exclude_port=INSTANCE_PORT)


def _show_startup_error(message: str) -> None:
    """Display a visible startup failure without requiring a console window."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, message, "Hero Siege Item Editor", 0x10
            )
            return
        except Exception:
            pass
    print(f"Hero Siege Item Editor: {message}", file=sys.stderr)


def main():
    global INSTANCE_GUARD_ACTIVE, INSTANCE_PORT, INSTANCE_RESERVED_PORTS
    servers = []
    server_threads = []
    reserved_ports = []
    port = PORT
    reuse_port = None
    startup_error = None
    try:
        with _exclusive_save_file(ROOT / "editor-startup", timeout=5.0):
            for candidate in range(PORT, PORT + 10):
                identity = _editor_identity(candidate, timeout=0.2)
                if identity is None:
                    continue
                if identity["version"] == APP_VERSION and identity.get("pid") is not None:
                    reuse_port = candidate
                else:
                    startup_error = (
                        f"Item Editor v{identity['version']} is already running. "
                        f"Close it before starting v{APP_VERSION}."
                    )
                break

            if reuse_port is None and startup_error is None:
                for candidate in range(PORT, PORT + 10):
                    try:
                        candidate_server = ThreadingHTTPServer(("127.0.0.1", candidate), H)
                        if not servers:
                            port = candidate
                        servers.append(candidate_server)
                        reserved_ports.append(candidate)
                    except OSError:
                        identity = _editor_identity(candidate, timeout=0.2)
                        if identity is None:
                            startup_error = (
                                f"Local editor port {candidate} is occupied by an "
                                "unidentified or legacy process. Close it before "
                                f"starting Item Editor v{APP_VERSION}."
                            )
                        elif identity["version"] == APP_VERSION and identity.get("pid") is not None:
                            reuse_port = candidate
                        else:
                            startup_error = (
                                f"Item Editor v{identity['version']} is already running. "
                                f"Close it before starting v{APP_VERSION}."
                            )
                        break

            if not servers and reuse_port is None and startup_error is None:
                startup_error = f"No free editor port in {PORT}..{PORT + 9}."

            if (reuse_port is not None or startup_error is not None) and servers:
                for candidate_server in servers:
                    candidate_server.server_close()
                servers.clear()
                reserved_ports.clear()

            if servers:
                INSTANCE_PORT = port
                INSTANCE_RESERVED_PORTS = frozenset(reserved_ports)
                INSTANCE_GUARD_ACTIVE = True
                for candidate_server in servers:
                    thread = threading.Thread(
                        target=candidate_server.serve_forever, daemon=True
                    )
                    server_threads.append(thread)
                    thread.start()
                peer_error = _peer_editor_error(
                    exclude_ports=INSTANCE_RESERVED_PORTS
                )
                if peer_error:
                    startup_error = peer_error
                    INSTANCE_GUARD_ACTIVE = False
                    INSTANCE_PORT = None
                    INSTANCE_RESERVED_PORTS = frozenset()
                    for candidate_server in servers:
                        candidate_server.shutdown()
                        candidate_server.server_close()
                    servers.clear()
                    server_threads.clear()
    except (OSError, TimeoutError) as exc:
        startup_error = f"Could not acquire the editor startup lock: {exc}"

    if reuse_port is not None:
        _open_window(reuse_port)
        return
    if startup_error is not None:
        _show_startup_error(startup_error)
        return
    if not servers or not server_threads:
        _show_startup_error("The local editor server could not be started.")
        return
    try:
        native_window = _open_window(port)
        if not native_window:
            # A browser launch is non-blocking.  Keep the source/fallback
            # process (and therefore the daemon HTTP thread) alive until the
            # user interrupts it instead of immediately serving a dead URL.
            server_threads[0].join()
    except KeyboardInterrupt:
        pass
    finally:
        INSTANCE_GUARD_ACTIVE = False
        INSTANCE_PORT = None
        INSTANCE_RESERVED_PORTS = frozenset()
        for candidate_server in servers:
            candidate_server.shutdown()
            candidate_server.server_close()


if __name__ == "__main__":
    main()
