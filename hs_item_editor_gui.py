#!/usr/bin/env python3
"""Hero Siege Item Editor GUI - yerel web arayuzu.

Calistir:  py hs_item_editor_gui.py   ->  tarayicida http://127.0.0.1:8765
Oyun ACIKKEN kayit yazmaz (sadece goruntuler). Her yazimda otomatik yedek.
Veri kaynagi: oyunun kendi item depolari (itemRepoNormal/Unique/Runeword dokumu).
"""

import base64
import json
import random
import re
import shutil
import subprocess
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path.home() / "AppData" / "Local" / "Hero_Siege"
SAVES = ROOT / "hs2saves"
# PyInstaller: bundled data _MEIPASS'ta; kaynak: script klasorunde
BASE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
CATALOG_FILE = BASE / "hs_full_catalog.json"
PORT = 8765

XOR_KEY = bytes([
    0xE3, 0x95, 0x3D, 0xB1, 0x01, 0x6B, 0xB6, 0x58,
    0x54, 0x38, 0x3F, 0x46, 0xA1, 0x74, 0x29, 0xCC,
    0x45, 0x45, 0x51, 0xF2, 0xA7, 0xF7, 0xAB, 0xB7,
    0x26, 0xF1, 0x37, 0xA8, 0x81, 0x91, 0xE6, 0x7E,
])

CLASS_NAMES = {0: "Helmet", 1: "Body Armor", 2: "Boots", 3: "Weapon", 4: "Gloves", 5: "Amulet",
               6: "Shield", 7: "Ring", 8: "Belt", 10: "Charm", 11: "Potion",
               12: "Key", 13: "Boss Material", 14: "Socketable", 15: "Rune",
               16: "Relic", 18: "Consumable", 19: "Essence Vault", -2: "Runeword"}
SUB_NAMES = {0: "", 1: "Sword", 2: "Dagger", 3: "Mace", 4: "Axe", 5: "Claw",
             6: "Polearm", 7: "Chainsaw", 8: "Staff", 9: "Cane", 10: "Wand", 11: "Book",
             12: "Spellblade", 13: "Bow", 14: "Gun", 15: "Flask", 16: "Throwing", 17: "Universal"}
SLOT_NAMES = {0: "Helmet", 1: "Body Armor", 2: "Boots", 3: "Weapon I", 4: "Gloves", 5: "Amulet",
              6: "Offhand I", 7: "Ring I", 8: "Belt", 9: "Ring II",
              10: "Relic 1", 11: "Relic 2", 12: "Relic 3", 13: "Relic 4",
              16: "Weapon II", 17: "Offhand II"}
HERO_CLASSES = {1: "Viking", 2: "Pyromancer", 3: "Marksman", 4: "Pirate", 5: "Nomad",
                6: "Redneck", 7: "Necromancer", 8: "Samurai", 9: "Paladin", 10: "Amazon",
                11: "Demon Slayer", 12: "Demonspawn", 13: "Shaman", 14: "White Mage",
                15: "Marauder", 16: "Plague Doctor", 17: "Shield Lancer", 18: "Illusionist",
                19: "Jotunn", 20: "Exo", 21: "Butcher", 22: "Stormweaver", 23: "Bard", 24: "Prophet"}
GRID_DIMS = {"inventory_tab": (15, 6), "inventory_charms": (3, 11), "inventory_key_tab": (15, 6),
             "inventory_material_tab": (15, 6), "inventory_socket_tab": (15, 6),
             "stash_tab": (17, 18), "material_tab": (17, 18), "socket_tab": (17, 18),
             "potions": (5, 2), "personal_stash": (17, 18)}


def xor_bytes(b: bytes) -> bytes:
    return bytes(x ^ XOR_KEY[i % len(XOR_KEY)] for i, x in enumerate(b))


def decode_hss(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    cleaned = "".join(c for c in raw if not c.isspace() and c != "\x00")
    return xor_bytes(zlib.decompress(base64.b64decode(cleaned)))[::2].decode("latin-1")


def encode_hss(text: str) -> str:
    wide = bytearray()
    for ch in text.encode("latin-1"):
        wide += bytes((ch, 0))
    return base64.b64encode(zlib.compress(xor_bytes(bytes(wide)), 9)).decode("ascii")


CREATE_NO_WINDOW = 0x08000000  # subprocess'in konsol penceresi acmasini engeller


def game_running() -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Hero_Siege.exe"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        return "Hero_Siege.exe" in r.stdout
    except Exception:
        return False


def backup(path: Path) -> str:
    bak = path.with_name(path.name + f".guibak_{time.strftime('%Y%m%d_%H%M%S')}")
    if not bak.exists():  # ayni saniyede ikinci yazim ezilmesin
        shutil.copy2(path, bak)
    old = sorted(path.parent.glob(path.name + ".guibak_*"))
    for p in old[:-20]:
        try:
            p.unlink()
        except OSError:
            pass
    return bak.name


CAT = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
SETS_FILE = BASE / "hs_sets.json"
SETS = json.loads(SETS_FILE.read_text(encoding="utf-8")) if SETS_FILE.exists() else []
RW_FILE = BASE / "hs_runewords.json"
RUNEWORDS = json.loads(RW_FILE.read_text(encoding="utf-8")) if RW_FILE.exists() else []
ICONS = BASE / "item_icons"
BY_ADDR = {}
for _r in CAT:
    kindbit = 1 if _r["kind"] == "unique" else 0
    BY_ADDR[(kindbit, _r["cls"], _r["sub"], _r["b"])] = _r

# Runeword'leri soketteki run dizisinden tani (her tarif benzersiz, 0 cakisma).
RW_BY_RUNES = {}
for _rw in RUNEWORDS:
    _seq = tuple(int(_rn["b"]) for _rn in _rw.get("runes", []))
    if _seq:
        RW_BY_RUNES.setdefault(_seq, _rw)


def socket_rune_seq(data: dict) -> tuple:
    """Itemin s1..s6 soketlerindeki run b-degerlerini sirayla cikar."""
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


def resolve(key: str, data: dict) -> dict:
    """Save kaydini katalog girdisine cozer."""
    try:
        sfx = int(key.rsplit("-", 1)[1])
    except Exception:
        sfx = -1
    c = int(data.get("c", 0))
    j = int(data.get("j", 0))
    b = data.get("b")
    out = {"key": key, "raw": data, "stack": data.get("o")}
    if b is None:
        out.update(name="Special/Runeword item", rar="Runeword", w=2, h=4, cid=None)
    else:
        r = BY_ADDR.get((c, sfx, j if sfx == 3 else 0, int(b)))
        if r:
            out.update(name=r["name"], rar=r["rar"], w=r["w"], h=r["h"], cid=r["id"],
                       set=r.get("set"), clsName=CLASS_NAMES.get(r["cls"], "?"), spr=r.get("spr"))
        else:
            out.update(name=f"? (c{c} s{sfx} j{j} b{int(b)})", rar="?", w=1, h=1, cid=None)
    # Soketlenmis runler bir tarifle eslesiyorsa bu bir runeword'dur (oyunun mantigi).
    # Taban itemin cls/spr/w/h/cid'i korunur (giydirme/surukleme dogru kalsin),
    # sadece gorunen ad + rarity runeword olarak isaretlenir; tooltip rwcid'den okur.
    rw = RW_BY_RUNES.get(socket_rune_seq(data))
    if rw:
        out["name"] = rw["name"]
        out["rar"] = "Runeword"
        out["isRW"] = True
        if isinstance(rw.get("cid"), int):
            out["rwcid"] = rw["cid"]
    return out


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
    return chars


def read_char(slot: int) -> dict:
    txt = decode_hss(SAVES / f"herosiege{slot}.hss")
    m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
    inv = json.loads(base64.b64decode(m.group(1))) if m else {}
    out = {"equipped": [], "potions": [], "personal_stash": []}
    for k, v in inv.get("equipped_items", {}).items():
        it = resolve(k, v["data"])
        it["g"] = int(v["data"].get("g", -1))
        it["slotName"] = SLOT_NAMES.get(it["g"], f"Slot {it['g']}")
        out["equipped"].append(it)
    for sec in ("potions", "personal_stash"):
        for k, v in inv.get(sec, {}).items():
            it = resolve(k, v["data"])
            it["pos"] = v.get("pos", [0, 0])
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
                    lst.append(it)
                bags[tab] = lst
        except Exception:
            pass
    out["bags"] = bags
    return out


def read_stash() -> dict:
    d = json.loads(decode_hss(SAVES / "stash.hss"))
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
            lst.append(it)
        out[tab] = lst
    return out


def fresh_key(cls: int, existing) -> str:
    base = int(time.time() * 1000) % 10**12
    while f"0-0-{base}-{cls}" in existing:
        base += 1
    return f"0-0-{base}-{cls}"


def grid_dims(tab: str):
    base = re.sub(r"_\d+$", "", tab)
    return GRID_DIMS.get(base, (17, 18))


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


def make_data(r: dict, equipped_g=None) -> dict:
    c = 1.0 if r["kind"] == "unique" else 0.0
    j = float(r["sub"] if r["cls"] == 3 else 0)
    d = {"w": 1.0, "a": float(random.randint(1, 999_999_999)), "j": j,
         "b": float(r["b"]), "c": c}
    if equipped_g is not None:
        d.update({"g": float(equipped_g), "d": 0.0, "n": 0.0, "e": 0.0})
    elif c == 1.0:
        d["m"] = 1.0
    else:
        d["o"] = 1.0
    return d


def write_stash(data: dict) -> str:
    p = SAVES / "stash.hss"
    bk = backup(p)
    p.write_text(encode_hss(json.dumps(data, separators=(", ", ": "))), encoding="ascii")
    return bk


def write_bags(slot: int, data: dict) -> str:
    p = SAVES / f"inventory_order_{slot}.hss"
    bk = backup(p)
    p.write_text(encode_hss(json.dumps(data, separators=(", ", ": "))), encoding="ascii")
    return bk


def write_char_inventory(slot: int, inv: dict) -> str:
    p = SAVES / f"herosiege{slot}.hss"
    txt = decode_hss(p)
    blob = base64.b64encode(json.dumps(inv, separators=(", ", ": ")).encode()).decode()
    new = re.sub(r'inventory="[A-Za-z0-9+/=]*"', f'inventory="{blob}"', txt, count=1)
    bk = backup(p)
    p.write_text(encode_hss(new), encoding="ascii")
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


def file_key(ref: dict):
    t = ref["type"]
    if t == "stash":
        return ("stash",)
    if t == "bag":
        return ("bag", int(ref["slot"]))
    return ("char", int(ref["slot"]))


class FileCtx:
    """Ayni dosyaya birden fazla referans tek yuklemeyle calissin."""

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
                self.loaded[fk] = json.loads(base64.b64decode(m.group(1))) if m else {}
        d = self.loaded[fk]
        tab = ref.get("tab") or ref["type"]
        return d.setdefault(tab, {})

    def save_all(self) -> list:
        baks = []
        for fk, d in self.loaded.items():
            if fk[0] == "stash":
                baks.append(write_stash(d))
            elif fk[0] == "bag":
                baks.append(write_bags(fk[1], d))
            else:
                baks.append(write_char_inventory(fk[1], d))
        return baks


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
        baks = ctx.save_all()
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
    baks = ctx.save_all()
    return {"ok": f"{it['name']} moved -> [{int(pos[0])},{int(pos[1])}]", "backup": ", ".join(baks)}


def op_add(body: dict) -> dict:
    r = CAT[int(body["cid"])]
    tgt = body["target"]          # {"type":"stash_unique"|"stash"|"bag"|"char_*", ...}
    if game_running():
        return {"err": "Game is running! Close it first."}
    if r.get("kind") == "runeword" or r.get("cls", 0) < 0:
        return {"err": "Runewords can't be added directly - use the Runeword Builder."}
    if tgt["type"] == "stash_unique":
        if r["kind"] != "unique":
            return {"err": "Only unique items can go to the Unique tab."}
        d = json.loads(decode_hss(SAVES / "stash.hss"))
        ui = d.setdefault("unique_items", {})
        key = fresh_key(r["cls"], ui)
        ui[key] = {"data": make_data(r)}
        bk = write_stash(d)
        return {"ok": f"{r['name']} -> Unique sekmesi", "backup": bk}
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
        items[key] = {"pos": pos, "data": make_data(r)}
        bk = write_stash(d)
        return {"ok": f"{r['name']} -> {tab} pos {pos}", "backup": bk}
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
        items[key] = {"pos": pos, "data": make_data(r)}
        bk = write_bags(slot, d)
        return {"ok": f"{r['name']} -> canta {tab}", "backup": bk}
    if tgt["type"] == "equip":
        slot, g = int(tgt["slot"]), int(tgt["g"])
        p = SAVES / f"herosiege{slot}.hss"
        txt = decode_hss(p)
        m = re.search(r'inventory="([A-Za-z0-9+/=]+)"', txt)
        inv = json.loads(base64.b64decode(m.group(1))) if m else {}
        eq = inv.setdefault("equipped_items", {})
        # ayni slottakini cikar
        for k in [k for k, v in eq.items() if int(v["data"].get("g", -1)) == g]:
            del eq[k]
        key = fresh_key(r["cls"], eq)
        eq[key] = {"data": make_data(r, equipped_g=g)}
        bk = write_char_inventory(slot, inv)
        return {"ok": f"{r['name']} -> {SLOT_NAMES.get(g, g)} (karakter {slot})", "backup": bk}
    return {"err": "unknown target"}


def op_addmany(body: dict) -> dict:
    """Birden fazla unique'i tek yazimda Unique sekmesine ekle (set icin)."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    cids = body.get("cids") or []
    d = json.loads(decode_hss(SAVES / "stash.hss"))
    ui = d.setdefault("unique_items", {})
    added = []
    for cid in cids:
        r = CAT[int(cid)]
        if r["kind"] != "unique":
            continue
        key = fresh_key(r["cls"], ui)
        ui[key] = {"data": make_data(r)}
        added.append(r["name"])
    if not added:
        return {"err": "nothing to add"}
    bk = write_stash(d)
    return {"ok": f"added {len(added)}: " + ", ".join(added), "backup": bk}


def op_modify(body: dict) -> dict:
    """duplicate / setstack / reroll - tek item uzerinde kucuk islemler."""
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
    if action == "reroll":
        d0 = entry.setdefault("data", {})
        d0["a"] = float(random.randint(1, 999_999_999))
        if "i" in d0: d0["i"] = float(random.randint(1, 999_999_999))
        if "s" in d0: d0["s"] = float(random.randint(1, 999_999_999))
        baks = ctx.save_all()
        return {"ok": f"{it['name']}: stats rerolled (new seeds)", "backup": ", ".join(baks)}
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
    """Runeword uret. Codex tipi -> forged codex; ekipman tipi -> tarif runleri
    soketlenmis normal base (oyun kimligi soket runlerinden tanir, D2 usulu)."""
    if game_running():
        return {"err": "Game is running! Close it first."}
    rwid = int(body["rw"])
    tab = body.get("tab") or "stash_tab_1"
    rec = next((x for x in RUNEWORDS if x["rw"] == rwid), None)
    if not rec or not rec.get("base"):
        return {"err": "unknown runeword / no valid base"}
    base = rec["base"]
    d = json.loads(decode_hss(SAVES / "stash.hss"))
    items = d.setdefault(tab, {})
    pos = find_free_pos(items, tab, base.get("w", 2), base.get("h", 2))
    if pos is None:
        return {"err": f"no free space in {tab}"}

    def rune_b64(rb):
        rj = json.dumps({"a": random.randint(1, 999_999_999), "b": int(rb), "n": 0},
                        separators=(",", ":"))
        return base64.b64encode(rj.encode()).decode()

    if rec["type"] == 11:
        # zone codex: soket sayisi SEED'lerden zar atilir -> bilinen-iyi seed setleri klonlanir
        # (3 soket: kullanicinin gercek forged Codex of Experience; 5 soket: gercek 5-soketli codex)
        CODEX_SEEDS = {
            3: {"a": 765653088.0, "i": 918025265.0, "s": 801771351.0,
                "d": 4.0, "q": 1.0, "r": 0.0, "u": 4.0, "v": 6.0},
            5: {"a": 402084546.0, "i": 146361242.0, "s": 763442575.0,
                "d": 4.0, "q": 1.0, "u": 9.0, "v": 5.0},
        }
        n_runes = len(rec["runes"])
        tmpl = CODEX_SEEDS.get(n_runes)
        note = ""
        if tmpl:
            data = {"b": 23.0, "c": 0.0, "e": 0.0, **tmpl}
        else:
            data = {"b": 23.0, "c": 0.0, "d": 4.0, "e": 0.0, "q": 1.0, "r": 0.0,
                    "u": 4.0, "v": 6.0,
                    "a": float(random.randint(1, 999_999_999)),
                    "i": float(random.randint(1, 999_999_999)),
                    "s": float(random.randint(1, 999_999_999))}
            note = f" | NOTE: random seeds - if in-game socket count != {n_runes}, right-click the codex -> Reroll stats until it matches"
        unset = []
        for n, rn in enumerate(rec["runes"], 1):
            data[f"s{n}"] = rune_b64(rn["b"])
            unset.append(f"s{n}")
        data["unset"] = unset
    else:
        # ekipman: normal base + tarif runleri (gercek Scholar/Skysong/Grimwalkers ornekleri)
        data = {"a": float(random.randint(1, 999_999_999)),
                "i": float(random.randint(1, 999_999_999)),
                "s": float(random.randint(1, 999_999_999)),
                "b": float(base["b"]), "c": 0.0, "w": 1.0,
                "j": float(base["sub"] if base["cls"] == 3 else 0),
                "d": 0.0, "e": 0.0, "n": 0.0,
                "zz": {"sockets": float(len(rec["runes"]))}}
        for n, rn in enumerate(rec["runes"], 1):
            data[f"s{n}"] = rune_b64(rn["b"])

    key = fresh_key(base["cls"], items)
    items[key] = {"pos": pos, "data": data}
    bk = write_stash(d)
    return {"ok": f"FORGED: {rec['name']} ({rec['target']}) -> {tab} | runes: " +
                  ", ".join(r["name"] for r in rec["runes"]) + (note if rec["type"] == 11 else ""),
            "backup": bk}


LOADOUTS_FILE = ROOT / "hs_loadouts.json"


def load_loadouts() -> dict:
    if LOADOUTS_FILE.exists():
        try:
            return json.loads(LOADOUTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_loadouts(d: dict):
    LOADOUTS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


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


BAK_PAT = re.compile(r"^(?P<target>.+?\.hss)\.(?:guibak|itemed_bak)_(?P<ts>\d{8}_\d{6})$")


def list_backups() -> list:
    out = []
    for f in SAVES.iterdir():
        m = BAK_PAT.match(f.name)
        if m:
            out.append({"file": f.name, "target": m.group("target"), "ts": m.group("ts"),
                        "size": f.stat().st_size})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:200]


def op_restore_backup(body: dict) -> dict:
    if game_running():
        return {"err": "Game is running! Close it first."}
    fn = body.get("file", "")
    m = BAK_PAT.match(fn)
    src = SAVES / fn
    if not m or not src.exists():
        return {"err": "backup not found"}
    target = SAVES / m.group("target")
    pre = backup(target) if target.exists() else "(none)"
    shutil.copy2(src, target)
    return {"ok": f"restored {m.group('target')} from {m.group('ts')}", "backup": f"pre-restore: {pre}"}


def op_sockets(body: dict) -> dict:
    """Soket duzenleme: s1..s6 iceriklerini yeniden yaz.

    Her soket girdisi su formlardan biri:
      - None / ""              -> bos soket (atla)
      - {"keep": {a,b,n}}      -> DEGISMEYEN soket; iceригi (tohum/varyant) AYNEN korunur
      - {"b": <int>}           -> kullanici degistirdi/ekledi -> yeni tohum, n=0
      - <int> (eski format)    -> yeni gem/run; yeni tohum, n=0
    Boylece dokunulmayan gem/jewel'lerin a (tohum) ve n (varyant) degeri bozulmaz.
    """
    if game_running():
        return {"err": "Game is running! Close it first."}
    tgt = body["target"]
    key = body["key"]
    sockets = body.get("sockets") or []
    if len(sockets) > 6:
        return {"err": "max 6 sockets"}
    ctx = FileCtx()
    items = ctx.items(tgt)
    if key not in items:
        return {"err": "item not found"}
    entry = items[key]
    d0 = entry.setdefault("data", {})
    it = resolve(key, d0)
    for n in range(1, 7):
        d0.pop(f"s{n}", None)
    d0.pop("unset", None)  # duzenleme forged durumunu sifirlar
    filled = 0
    for n, e in enumerate(sockets, 1):
        if e is None or e == "":
            continue
        if isinstance(e, dict) and isinstance(e.get("keep"), dict):
            o = e["keep"]                       # dokunulmadi -> aynen koru
            sj = {"a": o.get("a", 0), "b": int(o.get("b", 0)), "n": o.get("n", 0)}
        elif isinstance(e, dict) and "b" in e:
            sj = {"a": random.randint(1, 999_999_999), "b": int(e["b"]), "n": 0}
        elif isinstance(e, (int, float)):
            sj = {"a": random.randint(1, 999_999_999), "b": int(e), "n": 0}
        else:
            continue
        d0[f"s{n}"] = base64.b64encode(
            json.dumps(sj, separators=(",", ":")).encode()).decode()
        filled += 1
    # zz.sockets SADECE zaten varsa guncellenir (runeword item); normal gemli
    # itemlerde zz YOKTUR, eklemek hatali olurdu.
    if isinstance(d0.get("zz"), dict):
        d0["zz"]["sockets"] = float(len(sockets))
    baks = ctx.save_all()
    return {"ok": f"{it['name']}: sockets updated ({filled}/{len(sockets)} filled)",
            "backup": ", ".join(baks)}


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


# ---------- HTTP ----------

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            b = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/overview":
            self._json({"chars": list_characters(), "gameRunning": game_running()})
        elif u.path == "/api/catalog":
            self._json(CAT)
        elif u.path.startswith("/api/char/"):
            self._json(read_char(int(u.path.rsplit("/", 1)[1])))
        elif u.path == "/api/stash":
            self._json(read_stash())
        elif u.path == "/api/sets":
            self._json(SETS)
        elif u.path == "/api/runewords":
            self._json(RUNEWORDS)
        elif u.path == "/api/loadouts":
            self._json(load_loadouts())
        elif u.path == "/api/backups":
            self._json(list_backups())
        elif u.path.startswith("/icons/"):
            name = u.path.rsplit("/", 1)[1]
            p = ICONS / name
            if re.fullmatch(r"\d+\.png", name) and p.exists():
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
            self._json({"err": "yok"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        u = urlparse(self.path)
        try:
            if u.path == "/api/add":
                self._json(op_add(body))
            elif u.path == "/api/move":
                self._json(op_move(body))
            elif u.path == "/api/addmany":
                self._json(op_addmany(body))
            elif u.path == "/api/forge":
                self._json(op_forge(body))
            elif u.path == "/api/loadout":
                self._json(op_loadout(body))
            elif u.path == "/api/restorebak":
                self._json(op_restore_backup(body))
            elif u.path == "/api/sockets":
                self._json(op_sockets(body))
            elif u.path == "/api/modify":
                self._json(op_modify(body))
            elif u.path == "/api/delete":
                self._json(op_delete(body))
            else:
                self._json({"err": "yok"}, 404)
        except Exception as e:
            self._json({"err": f"hata: {e}"}, 500)


HTML = r"""<!DOCTYPE html>
<html lang="tr"><head><meta charset="utf-8"><title>Hero Siege Item Editor</title>
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
.rwcard{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 14px;margin:6px 0;max-width:920px;display:grid;grid-template-columns:240px 1fr 92px;align-items:center;gap:16px}
.tipbar{background:#241a2e;border:1px solid #4a3a6a;border-radius:6px;padding:7px 12px;margin:0 0 12px;font-size:12px;color:#bfb3d6;max-width:920px}
.tipbar b{color:#d8c9ff}
.rwhead{display:flex;flex-direction:column;gap:2px;min-width:0}
.rwcard .rwname{font-weight:bold;cursor:default;line-height:1.15}
.rwtarget{font-size:11px;color:var(--mut);line-height:1.25;white-space:normal}
.rwrunes{display:flex;gap:5px;flex-wrap:wrap}
.rwrune{display:inline-flex;align-items:center;gap:3px;background:#1a0e10;border:1px solid #4a3a26;border-radius:4px;padding:2px 6px;font-size:11px;color:#d8c9a0}
.rwrune img{width:18px;height:18px;image-rendering:pixelated}
.forgebtn{background:#4a2a13;color:#ffd9a0;border:1px solid #8a5a26;border-radius:4px;padding:6px 0;cursor:pointer;font-size:12px;width:100%}
.forgebtn:hover{background:#6f421a}
/* ---- item tooltip ---- */
#tip{position:fixed;z-index:99;display:none;background:rgba(12,5,7,.97);border:1px solid #6a3a40;border-radius:6px;padding:10px 14px;max-width:330px;pointer-events:none;box-shadow:0 4px 18px rgba(0,0,0,.7)}
#tip .tname{font-size:14px;font-weight:bold;margin-bottom:2px}
#tip .ttype{font-size:11px;color:#937f6a;margin-bottom:6px}
#tip .tstat{font-size:12px;color:#8fb7ff;line-height:1.5}
#tip .tstat b{color:#fff;font-weight:600}
#tip .tset{font-size:11px;color:#54e87a;margin-top:5px}
</style></head><body>
<div id="left">
  <h1>HERO SIEGE<br>ITEM EDITOR</h1>
  <div id="status">loading...</div>
  <button class="tabbtn" data-view="stash">&#128451; Stash (shared)</button>
  <button class="tabbtn" data-view="sets">&#9876; Sets</button>
  <button class="tabbtn" data-view="runewords">&#10038; Runeword Builder</button>
  <button class="tabbtn" data-view="backups">&#128190; Backups</button>
  <h2>Characters</h2>
  <div id="chars"></div>
</div>
<div id="mid"><div class="muted">Select the stash or a character on the left.</div></div>
<div id="right">
  <h2 style="margin-top:0">Item Catalog</h2>
  <input id="q" placeholder="search items... (e.g. buriza)">
  <div id="filters">
    <div class="fitem"><label>Type</label><select id="fkind"><option value="">All</option><option value="unique">Unique / Set</option><option value="normal">Normal</option><option value="runeword">Runeword</option></select></div>
    <div class="fitem"><label>Slot</label><select id="fcls"><option value="">All</option></select></div>
    <div class="fitem"><label>Rarity</label><select id="frar"><option value="">All</option><option>Angelic</option><option>Unholy</option><option>Heroic</option><option>Satanic</option><option>Mythic</option><option>Legendary</option><option>Rare</option><option>Superior</option><option>Normal</option></select></div>
    <div class="fitem"><label>Set</label><select id="fset"><option value="">All items</option><option value="any">Any set piece</option></select></div>
    <div class="fitem" style="grid-column:1/3"><label>Has Stat</label><input id="fstat" list="statlist" placeholder="e.g. magic find, attack speed..."><datalist id="statlist"></datalist></div>
  </div>
  <div id="results"></div>
  <div id="addzone" style="border-top:1px solid var(--line);padding-top:8px">
    <div id="selinfo" class="muted">no item selected</div>
    <div class="flex" id="targetrow" style="margin-top:6px"></div>
    <button class="act" id="addbtn" disabled>Add</button>
    <div id="msg"></div>
  </div>
</div>
<script>
let CAT=[], SETS_DB=[], RW_DB=[], chars=[], view=null, sel=null, curChar=null, charData=null, stashData=null;
const CLS={0:"Helmet",1:"Body Armor",2:"Boots",3:"Weapon",4:"Gloves",5:"Amulet",6:"Shield",7:"Ring",8:"Belt",10:"Charm",11:"Potion",12:"Key",13:"Boss Material",14:"Socketable",15:"Rune",16:"Relic",18:"Consumable",19:"Essence Vault","-2":"Runeword"};
const SLOTS={0:"Helmet",1:"Body Armor",2:"Boots",3:"Weapon I",4:"Gloves",5:"Amulet",6:"Offhand I",7:"Ring I",8:"Belt",9:"Ring II",10:"Relic 1",11:"Relic 2",12:"Relic 3",13:"Relic 4",16:"Weapon II",17:"Offhand II"};
const DIMS={inventory_tab:[15,6],inventory_charms:[3,11],inventory_key_tab:[15,6],inventory_material_tab:[15,6],inventory_socket_tab:[15,6],stash_tab:[17,18],material_tab:[17,18],socket_tab:[17,18],potions:[5,2],personal_stash:[17,18]};
const CELL=26;
const TIPBAR=`<div class="tipbar">&#128161; <b>Right-click any item</b> for: Edit sockets, Reroll stats, Duplicate, Edit stack, Delete &nbsp;&middot;&nbsp; <b>Drag</b> items to move them or drop onto an equipment slot &nbsp;&middot;&nbsp; drag from the <b>Item Catalog</b> (right) to add a new item</div>`;
async function j(u,opt){const r=await fetch(u,opt);return r.json()}
async function boot(){
  CAT=await j('/api/catalog'); SETS_DB=await j('/api/sets'); RW_DB=await j('/api/runewords');
  const fsetEl=document.getElementById('fset');
  SETS_DB.forEach(s=>{const o=document.createElement('option');o.value=s.set;o.textContent=s.name;fsetEl.appendChild(o)});
  const labels=new Set();
  CAT.forEach(r=>(r.stats||[]).forEach(([l,v])=>labels.add(l)));
  const dl=document.getElementById('statlist');
  [...labels].sort().forEach(l=>{const o=document.createElement('option');o.value=l;dl.appendChild(o)});
  const ov=await j('/api/overview'); chars=ov.chars;
  document.getElementById('status').textContent=ov.gameRunning?'GAME RUNNING - view only, writing locked':'game closed - editing enabled';
  document.getElementById('status').className=ov.gameRunning?'warn':'';
  const cd=document.getElementById('chars'); cd.innerHTML='';
  chars.forEach(c=>{const b=document.createElement('button');b.className='charbtn';
    b.innerHTML=`<b>${c.name}</b><br><span class="muted">${c.cls} - lv ${c.level} (slot ${c.slot})</span>`;
    b.onclick=()=>openChar(c.slot,b); cd.appendChild(b)});
  const fc=document.getElementById('fcls');
  Object.entries(CLS).forEach(([k,v])=>{const o=document.createElement('option');o.value=k;o.textContent=v;fc.appendChild(o)});
  search();
  setInterval(async()=>{const o=await j('/api/overview');
    document.getElementById('status').textContent=o.gameRunning?'GAME RUNNING - view only, writing locked':'game closed - editing enabled';
    document.getElementById('status').className=o.gameRunning?'warn':'';},5000);
  document.querySelector('[data-view=stash]').onclick=openStash;
  document.querySelector('[data-view=sets]').onclick=openSets;
  document.querySelector('[data-view=runewords]').onclick=openRunewords;
  document.querySelector('[data-view=backups]').onclick=openBackups;
  setupDnD(); setupTip();
}
function dims(tab){const b=tab.replace(/_\d+$/,'');return DIMS[b]||[17,18]}
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
    const inner=it.spr?`<img src="/icons/${it.spr}.png?v=2" loading="lazy">`:esc(short(it.name));
    h+=`<div class="item b-${rr}" draggable="true" title="" data-i="${i}" data-del='${JSON.stringify(delTarget)}' data-key="${it.key}" data-w="${it.w||1}" data-h="${it.h||1}" data-cid="${it.cid??''}" data-rwcid="${it.rwcid??''}" data-raw='${esc(JSON.stringify(it.raw||{}))}'
      style="left:${p[0]*CELL}px;top:${p[1]*CELL}px;width:${(it.w||1)*CELL-2}px;height:${(it.h||1)*CELL-2}px">${inner}${it.stack?`<span class="stk">x${it.stack}</span>`:''}</div>`;
  });
  return h+'</div>';
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
let dragInfo=null;
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
  mid.addEventListener('dragstart',e=>{
    const el=e.target.closest('.item,.dslot[draggable]'); if(!el)return;
    dragInfo={mode:'move',from:JSON.parse(el.dataset.del),key:el.dataset.key,w:+el.dataset.w,h:+el.dataset.h,cid:el.dataset.cid};
    e.dataTransfer.effectAllowed='move';
  });
  mid.addEventListener('dragover',e=>{
    clearGhost();
    if(!dragInfo)return;
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
  mid.addEventListener('dragleave',e=>{ if(!e.target.closest('.grid')&&!e.target.closest('.dslot'))clearGhost() });
  mid.addEventListener('drop',async e=>{
    if(!dragInfo)return;
    const sEl=e.target.closest('.dslot');
    if(sEl){
      e.preventDefault();
      const g=+sEl.dataset.g;
      clearGhost();
      if(!slotAccepts(g)){flash({err:dollEq[g]?'slot occupied - unequip first':'this item does not fit that slot'});dragInfo=null;return}
      let r;
      if(dragInfo.mode==='add'){
        r=await j('/api/add',{method:'POST',body:JSON.stringify({cid:dragInfo.cid,target:{type:'equip',slot:curChar,g}})});
      }else{
        r=await j('/api/move',{method:'POST',body:JSON.stringify({from:dragInfo.from,to:{type:'equip',slot:curChar,g},key:dragInfo.key})});
      }
      flash(r); dragInfo=null; refresh();
      return;
    }
    const gEl=e.target.closest('.grid'); if(!gEl)return;
    e.preventDefault();
    const gh=gEl.querySelector('.ghost');
    const g=gridReg[gEl.dataset.gid];
    if(!gh||!gh.dataset.free){clearGhost();dragInfo=null;return}
    const pos=[+gh.dataset.x,+gh.dataset.y];
    clearGhost();
    let r;
    if(dragInfo.mode==='move'){
      r=await j('/api/move',{method:'POST',body:JSON.stringify({from:dragInfo.from,to:g.target,key:dragInfo.key,pos})});
    }else{
      r=await j('/api/add',{method:'POST',body:JSON.stringify({cid:dragInfo.cid,target:{...g.target,pos}})});
    }
    flash(r); dragInfo=null; refresh();
  });
  mid.addEventListener('dragend',()=>{clearGhost();dragInfo=null});
}
function clearGhost(){
  document.querySelectorAll('.ghost').forEach(g=>g.remove());
  document.querySelectorAll('.drophl-ok,.drophl-no').forEach(s=>s.classList.remove('drophl-ok','drophl-no'));
}
// ---- soket editoru ----
function openSocketEditor(target,key,el){
  const old=document.getElementById('sockmodal'); if(old)old.remove();
  // mevcut soketleri raw'dan al -- her soket: {orig:{a,b,n}|null, b:<secili>|null}
  // orig = save'deki tam icerik; dokunulmadiginda AYNEN geri yazilir (tohum/varyant korunur)
  let raw={};
  try{raw=JSON.parse(el.dataset.raw||'{}')}catch(e){}
  const cur=[];
  for(let n=1;n<=6;n++){
    const s=raw['s'+n];
    if(s===undefined)continue;
    let o=null; try{o=JSON.parse(atob(s))}catch(e){}
    cur.push({orig:o, b:(o&&o.b!==undefined)?o.b:null});
  }
  const RUNES=CAT.filter(r=>r.kind==='normal'&&r.cls===15);
  const byName={}; RUNES.forEach(r=>byName[r.name.toLowerCase()]=r.b);
  const modal=document.createElement('div'); modal.id='sockmodal';
  const rows=cur.length?[...cur]:[{orig:null,b:null}];
  function render(){
    let h=`<div id="sockbox"><h3>Edit Sockets</h3>
    <div class="muted" style="margin-bottom:8px">Pick a rune/gem for each socket (type to search). Empty = empty socket.<br>Sockets you don't change keep their exact gem (seed &amp; variant preserved). Editing resets a codex's forged state.</div>
    <datalist id="runedl">${RUNES.map(r=>`<option value="${esc(r.name)}">`).join('')}</datalist>`;
    rows.forEach((row,i)=>{
      const r=RUNES.find(x=>x.b===row.b);
      h+=`<div class="sockrow"><b style="width:18px">${i+1}</b>
        ${r&&r.spr?`<img src="/icons/${r.spr}.png?v=2">`:'<span style="width:24px"></span>'}
        <input list="runedl" data-i="${i}" value="${r?esc(r.name):''}" placeholder="empty socket">
        <button data-rm="${i}" title="remove socket">&#10006;</button></div>`;
    });
    h+=`<div class="flex" style="margin-top:10px">
      <button class="act" style="margin:0" id="sockadd" ${rows.length>=6?'disabled':''}>+ Add socket</button>
      <button class="act" style="margin:0;background:#234a2a;border-color:#3da55e" id="socksave">Save</button>
      <button class="act" style="margin:0" id="sockcancel">Cancel</button></div></div>`;
    modal.innerHTML=h;
    modal.querySelectorAll('input[data-i]').forEach(inp=>{
      inp.onchange=()=>{
        const i=+inp.dataset.i;
        const b=byName[inp.value.toLowerCase()];
        const nb=(b===undefined?null:b);
        // ayni gem yeniden secildiyse orijinali (tohum/varyant) koru; aksi halde yeni
        if(rows[i].orig&&rows[i].orig.b===nb)rows[i]={orig:rows[i].orig,b:nb};
        else rows[i]={orig:null,b:nb};
        render();
      };
    });
    modal.querySelectorAll('button[data-rm]').forEach(btn=>{
      btn.onclick=()=>{rows.splice(+btn.dataset.rm,1);render()};
    });
    modal.querySelector('#sockadd').onclick=()=>{if(rows.length<6){rows.push({orig:null,b:null});render()}};
    modal.querySelector('#sockcancel').onclick=()=>modal.remove();
    modal.querySelector('#socksave').onclick=async()=>{
      // dokunulmayan soket -> {keep:orig}; degisen/yeni -> {b}; bos -> null
      const payload=rows.map(row=>row.b==null?null:(row.orig&&row.orig.b===row.b?{keep:row.orig}:{b:row.b}));
      const r=await j('/api/sockets',{method:'POST',body:JSON.stringify({target,key,sockets:payload})});
      modal.remove(); flash(r); refresh();
    };
  }
  render();
  modal.onclick=(e)=>{if(e.target===modal)modal.remove()};
  document.body.appendChild(modal);
}
// ---- item tooltip ----
function setupTip(){
  const tip=document.createElement('div'); tip.id='tip'; document.body.appendChild(tip);
  function show(cid,extra,x,y){
    const r=CAT[cid]; if(!r){tip.style.display='none';return}
    let h=`<div class="tname r-${r.rar}">${esc(r.name)}</div>`;
    const meta=r.kind==='runeword'?'Runeword':`${CLS[r.cls]||''}${r.cls===3?' / '+(SUBN[r.sub]||r.sub):''} &middot; ${r.rar} &middot; ${r.kind}`;
    h+=`<div class="ttype">${meta}${r.tier?` &middot; Tier ${r.tier}`:''}${extra||''}</div>`;
    if(r.lvl)h+=`<div class="ttype">Requires Level ${r.lvl}</div>`;
    for(const [lbl,val] of (r.stats||[]))h+=`<div class="tstat"><b>${esc(val)}</b> ${esc(lbl)}</div>`;
    if(r.set!==undefined){const s=SETS_DB.find(x=>x.set===r.set);
      h+=`<div class="tset">${esc(r.setName||(s&&s.name)||('Set #'+r.set))}</div>`;
      if(s)for(const pc of s.pieces)h+=`<div class="tset" style="color:#3da55e">&nbsp;&nbsp;${esc(pc.name)}</div>`;
      h+=`<div class="tset" style="color:#937f6a">(full-set bonus values not extracted yet)</div>`;}
    tip.innerHTML=h; tip.style.display='block';
    const tw=tip.offsetWidth, th=tip.offsetHeight;
    tip.style.left=Math.min(x+18,innerWidth-tw-8)+'px';
    tip.style.top=Math.min(y+12,innerHeight-th-8)+'px';
  }
  document.addEventListener('mousemove',e=>{
    if(dragInfo){tip.style.display='none';return}
    const el=e.target.closest('.item,.dslot[draggable],.res,.rwname');
    if(!el){tip.style.display='none';return}
    const rwcid=el.dataset.rwcid;
    let cid=(rwcid!==''&&rwcid!=null)?rwcid:el.dataset.cid;
    if(cid===''||cid==null){
      if(el.classList.contains('res'))return;
      tip.style.display='none';return;
    }
    const stk=el.querySelector('.stk');
    show(+cid,stk?` &middot; ${stk.textContent}`:'',e.clientX,e.clientY);
  });
}
const SUBN={1:"Sword",2:"Dagger",3:"Mace",4:"Axe",5:"Claw",6:"Polearm",7:"Chainsaw",8:"Staff",9:"Cane",10:"Wand",11:"Book",12:"Spellblade",13:"Bow",14:"Gun",15:"Flask",16:"Throwing",17:"Universal"}
function short(n){return n&&n.length>22?n.slice(0,20)+'..':(n||'?')}
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
async function openStash(){
  view='stash'; curChar=null; stashData=await j('/api/stash');
  gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const md=document.getElementById('mid');
  const order=Object.keys(stashData).sort();
  let h='<h2>Stash (shared)</h2>'+TIPBAR;
  for(const tab of order){
    const items=stashData[tab];
    h+=`<h2>${tab} <span class="muted">(${items.length})</span></h2>`;
    if(tab==='unique_items'){
      h+=`<div class="muted">auto-sorted tab &middot; ${items.length} records (no grid positions)</div>`;
      continue;
    }
    h+=gridHTML(tab,items,{type:'stash',tab});
  }
  md.innerHTML=h; bindDelete(); renderTargets();
}
// slot -> kabul edilen sinif idleri
const ACCEPT={0:[0],1:[1],2:[2],3:[3],16:[3],6:[3,6],17:[3,6],4:[4],5:[5],7:[7],9:[7],8:[8],10:[16],11:[16],12:[16],13:[16]};
let wTabL=1, wTabR=1, bagTab='inventory_tab_0', dollEq={};
const DC=32;
function dslot(g,w,h,label){
  const e=dollEq[g];
  const rr=e&&e.rar&&e.rar!=='?'?e.rar:null;
  const del=JSON.stringify({type:"equipped",slot:curChar,tab:"equipped_items"});
  return `<div class="dslot${rr?' b-'+rr:''}" data-g="${g}" style="width:${w*DC}px;height:${h*DC}px"
    ${e?`draggable="true" data-del='${del}' data-key="${e.key}" data-w="${e.w||1}" data-h="${e.h||1}" data-cid="${e.cid??''}" data-rwcid="${e.rwcid??''}" data-raw='${esc(JSON.stringify(e.raw||{}))}'`:`title="${label} (empty)"`}>
    <span class="lbl">${label}</span>${e&&e.spr?`<img src="/icons/${e.spr}.png?v=2">`:(e?esc(short(e.name)):'')}</div>`;
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
    <button class="act" style="margin:0;padding:5px 12px" id="loexport">Export</button>
    <button class="act" style="margin:0;padding:5px 12px" id="loimport">Import</button>
    <button class="act" style="margin:0;padding:5px 12px;border-color:#7a3030" id="lodelete">Delete</button>
    <input type="file" id="lofile" accept=".json" style="display:none">
  </div>`;
  h+=TIPBAR;
  h+=`<div id="doll">`;
  // relic sutunu
  h+=`<div class="relcol">${[10,11,12,13].map(g=>dslot(g,1,1.6,SLOTS[g])).join('')}</div>`;
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
  // canta sekmeleri (oyundaki Main/Extra)
  const BT=[["inventory_tab_0","Main"],["inventory_tab_1","Extra 1"],["inventory_tab_2","Extra 2"],["inventory_tab_3","Extra 3"],["inventory_tab_4","Extra 4"],
            ["inventory_socket_tab","Socket"],["inventory_material_tab","Materials"],["inventory_key_tab","Keys"],["personal_stash","Personal Stash"]];
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
    const all=await j('/api/loadouts');
    const blob=new Blob([JSON.stringify({name:n,...all[n]},null,1)],{type:'application/json'});
    const aEl=document.createElement('a');aEl.href=URL.createObjectURL(blob);
    aEl.download=n.replace(/[^\w-]+/g,'_')+'.loadout.json';aEl.click();
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
async function openBackups(){
  view='backups'; curChar=null;
  gridReg={}; gridSeq=0;
  document.querySelectorAll('.charbtn').forEach(b=>b.classList.remove('sel'));
  const baks=await j('/api/backups');
  const md=document.getElementById('mid');
  let h=`<h2>Backups <span class="muted">(${baks.length} latest)</span></h2>
  <div class="muted" style="margin-bottom:8px">Every change made by this editor creates one of these automatically. Restoring also backs up the current state first.</div>`;
  h+='<table style="border-collapse:collapse;font-size:12px">';
  h+='<tr style="color:#937f6a;text-align:left"><th style="padding:4px 14px 4px 0">Time</th><th style="padding:4px 14px 4px 0">File</th><th style="padding:4px 14px 4px 0">Size</th><th></th></tr>';
  for(const b of baks){
    const ts=`${b.ts.slice(6,8)}.${b.ts.slice(4,6)}.${b.ts.slice(0,4)} ${b.ts.slice(9,11)}:${b.ts.slice(11,13)}:${b.ts.slice(13,15)}`;
    h+=`<tr style="border-top:1px solid #2a1518"><td style="padding:4px 14px 4px 0">${ts}</td><td style="padding:4px 14px 4px 0">${esc(b.target)}</td><td style="padding:4px 14px 4px 0" class="muted">${(b.size/1024).toFixed(1)} KB</td>
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
  <div class="muted" style="margin-bottom:8px">Forges a completed runeword codex (with the correct runes socketed) into the stash tab below. Hover a name for its stats.</div>
  <div class="flex" style="margin-bottom:10px">Target: <select id="rwtab">${[1,2,3,4,5,6,7,8,9].map(i=>`<option value="stash_tab_${i}">Stash tab ${i}</option>`).join('')}</select>
  <input id="rwq" placeholder="filter runewords..." style="flex:1;max-width:240px"></div>
  <div id="rwlist"></div>`;
  md.innerHTML=h;
  const render=()=>{
    const q=(document.getElementById('rwq').value||'').toLowerCase();
    document.getElementById('rwlist').innerHTML=RW_DB.filter(r=>!q||r.name.toLowerCase().includes(q)).map(r=>
      `<div class="rwcard"><div class="rwhead"><span class="rwname r-Runeword" data-cid="${r.cid??''}">${esc(r.name)}</span><span class="rwtarget">${esc(r.target||'')}</span></div><span class="rwrunes">`+
      r.runes.map(rn=>`<span class="rwrune">${rn.spr?`<img src="/icons/${rn.spr}.png?v=2" loading="lazy">`:''}${esc(rn.name)}</span>`).join('')+
      `</span><button class="forgebtn" data-rw="${r.rw}">Forge</button></div>`).join('');
    document.querySelectorAll('.forgebtn').forEach(b=>{
      b.onclick=async()=>{
        b.disabled=true;
        const r=await j('/api/forge',{method:'POST',body:JSON.stringify({rw:+b.dataset.rw,tab:document.getElementById('rwtab').value})});
        flash(r); b.disabled=false;
      };
    });
  };
  document.getElementById('rwq').addEventListener('input',render);
  render();
}
async function openChar(slot,btn){
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
  if(!isEq)acts.push(['Duplicate','duplicate','']);
  acts.push(['Reroll stats','reroll','']);
  acts.push(['Edit sockets...','SOCKETS','']);
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
      }else if(act==='SOCKETS'){
        openSocketEditor(target,key,el);return;
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
  m.style.left=Math.min(x,innerWidth-190)+'px';
  m.style.top=Math.min(y,innerHeight-160)+'px';
}
function refresh(){ if(view==='stash')openStash(); else if(view==='sets')openSets(); else if(view==='char')openChar(curChar,document.querySelector('.charbtn.sel')) }
function search(){
  const q=document.getElementById('q').value.toLowerCase();
  const fk=document.getElementById('fkind').value, fc=document.getElementById('fcls').value;
  const fr=document.getElementById('frar').value, fs=document.getElementById('fset').value;
  const fst=document.getElementById('fstat').value.toLowerCase();
  // runeword (cls<0) girdileri buradan eklenmez -> Runeword Builder kullanilir
  const out=CAT.filter(r=>r.kind!=='runeword'&&(!q||r.name.toLowerCase().includes(q)||r.key.includes(q))&&(!fk||r.kind===fk)&&(fc===''||String(r.cls)===fc)&&(!fr||r.rar===fr)&&(fs===''||(fs==='any'?r.set!==undefined:r.set===+fs))&&(!fst||(r.stats||[]).some(([l,v])=>l.toLowerCase().includes(fst)))).slice(0,100);
  const rd=document.getElementById('results'); rd.innerHTML='';
  out.forEach(r=>{const d=document.createElement('div');d.className='res'+(sel&&sel.id===r.id?' sel':'');
    d.draggable=true; d.dataset.cid=r.id;
    let statHint='';
    if(fst){const hit=(r.stats||[]).find(([l,v])=>l.toLowerCase().includes(fst));
      if(hit)statHint=` <span class="muted" style="color:#8fb7ff">${esc(hit[1])} ${esc(hit[0])}</span>`;}
    d.innerHTML=`${r.spr?`<img src="/icons/${r.spr}.png?v=2" loading="lazy">`:''}<span class="r-${r.rar}">${esc(r.name)}</span>${statHint}`;
    d.onclick=()=>{sel=r;document.querySelectorAll('.res').forEach(x=>x.classList.remove('sel'));d.classList.add('sel');renderTargets()};
    d.addEventListener('dragstart',e=>{
      dragInfo={mode:'add',cid:r.id,w:r.w||1,h:r.h||1};
      e.dataTransfer.effectAllowed='copy';
    });
    rd.appendChild(d)});
}
function renderTargets(){
  const si=document.getElementById('selinfo'), tr=document.getElementById('targetrow'), btn=document.getElementById('addbtn');
  if(!sel){si.textContent='item secilmedi';tr.innerHTML='';btn.disabled=true;return}
  si.innerHTML=`selected: <span class="r-${sel.rar}">${esc(sel.name)}</span> <span class="muted">${sel.w}x${sel.h}</span>`;
  let h='<select id="tsel">';
  if(sel.kind==='unique')h+=`<option value='{"type":"stash_unique"}'>Stash &gt; Unique tab</option>`;
  for(let i=1;i<=9;i++)h+=`<option value='{"type":"stash","tab":"stash_tab_${i}"}'>Stash tab ${i}</option>`;
  if(view==='char'&&curChar!==null){
    for(let i=0;i<5;i++)h+=`<option value='{"type":"bag","slot":${curChar},"tab":"inventory_tab_${i}"}'>Bag: ${i===0?'Main':'Extra '+i}</option>`;
    h+=`<option value='{"type":"bag","slot":${curChar},"tab":"inventory_charms"}'>Charm bag</option>`;
    for(const g in SLOTS)h+=`<option value='{"type":"equip","slot":${curChar},"g":${g}}'>EQUIP: ${SLOTS[g]}</option>`;
  }
  h+='</select>';
  tr.innerHTML=h; btn.disabled=false;
  btn.onclick=async()=>{
    const t=JSON.parse(document.getElementById('tsel').value);
    const r=await j('/api/add',{method:'POST',body:JSON.stringify({cid:sel.id,target:t})});
    flash(r); refresh();
  };
}
function flash(r){const m=document.getElementById('msg');
  m.innerHTML=r.err?`<span style="color:#ff7060">${esc(r.err)}</span>`:`<span style="color:#54e87a">${esc(r.ok)}</span> <span class="muted">backup: ${esc(r.backup||'')}</span>`}
['q','fkind','fcls','frar','fset','fstat'].forEach(id=>document.getElementById(id).addEventListener('input',search));
boot();
</script></body></html>"""


def _open_window():
    """Native desktop window via pywebview; falls back to the browser if unavailable."""
    url = f"http://127.0.0.1:{PORT}"
    try:
        import webview
        webview.create_window("Hero Siege Item Editor", url, width=1320, height=880, min_size=(900, 600))
        webview.start()
    except Exception:
        import webbrowser
        webbrowser.open(url)


def main():
    import threading
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        # Another instance already serves the port -> just open a window onto it.
        _open_window()
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _open_window()
    # window closed -> exit the whole app (server thread is daemon)


if __name__ == "__main__":
    main()


