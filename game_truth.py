"""Game truth: the items Hero Siege itself finished building, for exact tooltips.

A save keeps only an item's compact definition (seeds and ids). The game
recomputes every stat line - the rolled rarity, the magic prefix and suffix,
up to five generated affixes, socket shares - each time it builds the item,
and that computation changes with game updates. ``exact_tooltip.py`` replays
it from the 2026-08-28 build's rules, which is an estimate on any other build
(measured 2026-09-24 on the 2026-09-16 build: 199 of 755 owned items matched
line for line, and no generated affix was shown at all).

This module keeps what the game actually built instead:

* ForgePact's Item Truth journal (ForgePact 1.4.6+): one NDJSON line per
  finished item while the game runs, under ``<root>/journal``. ForgePact
  writes it only while ``<root>/capture.request`` exists, which this editor
  creates.
* AFK FARM's delivery spool, which already holds the complete struct of
  every item it delivered to the Vault (captured right after CreateItemNew).

Both are ingested into ``<root>/truth.sqlite3`` and matched to a saved item by
its itemTimeStamp and its definition fields, so a record is never shown for a
different item, or for an older seed of the same one. Nothing here reads or
writes saves, and nothing talks to the running game; the store is built only
from files the game side wrote. ``<root>`` is %LOCALAPPDATA%\\Hero_Siege\\itemtruth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

SCHEMA_VERSION = 1
JOURNAL_SCHEMA = 1

# The build exact_tooltip.py's roll models were generated from: Hero_Siege.exe
# SHA-256 438BF484..., linked 2026-08-28. Its identity in the form below.
MODEL_BUILD_ID = "pe-6a91a8b3-0caf38b8"

# itemStatStruct keys "10".."14" hold one generated affix each:
# [stat key, minimum, maximum, affix tier]. The rolled value itself sits under
# the stat key like any other stat.
AFFIX_SLOT_KEYS = ("10", "11", "12", "13", "14")
# Weapon family/range selector, stored on hundreds of weapons, never drawn.
HIDDEN_STAT_KEYS = frozenset({447})
# Definition fields that say where an item sits or are the editor's own: the
# equipment slot, the grid flag the editor writes for gear, the editor sidecar.
PLACEMENT_FIELDS = frozenset({"g", "w", "zz", "pos"})
# Fields the Vault's AFK ingest writes as 1 where the game's definition leaves
# them out (a single item): absent and 1 mean the same thing.
DEFAULT_ONE_FIELDS = frozenset({"m", "o"})

# itemInfoStruct["27"] - the rolled rarity. 1/3/5-10 were measured when the
# Custom Forge learned to set it; 2 is what normal drops with two affixes
# carry, drawn in the "superior" colour (translationsMain.csv "superior").
RARITY_NAMES = {
    1: "Common", 2: "Superior", 3: "Rare", 5: "Legendary",
    6: "Satanic", 7: "Angelic", 9: "Heroic", 10: "Unholy",
}
# itemInfoStruct["32"] - the tooltip's tier letter.
TIER_NAMES = {1: "C", 2: "B", 3: "A", 4: "S", 5: "SS"}

_SPOOL_PATTERNS = ("*_claim.ndjson", "worker_*.ndjson")
_BATCH = 2000
# Bumped when the store keeps something new from journal lines it has read.
_JOURNAL_READ_VERSION = "2"


# ---- small helpers -------------------------------------------------------------

def _norm(value: Any) -> Any:
    """JSON numbers compare by value: 1.0 == 1, floats to 9 decimals."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else round(number, 9)
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _norm(item) for key, item in value.items()}
    return str(value)


def _canonical(value: Any) -> str:
    return json.dumps(_norm(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _whole_text(value: Any) -> str | None:
    """A timestamp as the game keeps it: a whole number (float or string)."""
    if isinstance(value, str):
        return value if re.fullmatch(r"\d{1,20}", value) else None
    if _is_number(value) and float(value) >= 0 and float(value).is_integer():
        return str(int(value))
    return None


def _pack(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 6)


def _unpack(value: Any) -> Any:
    """JSON the store keeps packed, or as text from before it packed it."""
    if isinstance(value, (bytes, memoryview)):
        value = zlib.decompress(bytes(value)).decode("utf-8")
    return json.loads(value)


def _format_number(value: float | int) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _format_value(value: float | int, percent: bool) -> str:
    return _format_number(value) + ("%" if percent else "")


# ---- build identity -------------------------------------------------------------

def build_id_from_headers(data: bytes) -> str | None:
    """``pe-<link stamp>-<.text size>``, the same id ForgePact writes.

    Both fields are untouched by AuriePatcher, so a clean and a patched exe
    of one build share it, and every game update changes it."""
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe < 0x40 or pe + 24 > len(data) or data[pe:pe + 4] != b"PE\0\0":
        return None
    sections, stamp = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<I", data, pe + 8)[0]
    optional = struct.unpack_from("<H", data, pe + 20)[0]
    table = pe + 24 + optional
    for index in range(min(sections, 96)):
        at = table + index * 40
        if at + 40 > len(data):
            break
        if data[at:at + 8] != b".text\0\0\0":
            continue
        text = struct.unpack_from("<I", data, at + 8)[0]
        return f"pe-{stamp:08x}-{text:08x}" if stamp and text else None
    return None


_BUILD_CACHE: dict[tuple[str, int, int], str | None] = {}


def build_id_of_exe(path: str | os.PathLike | None) -> str | None:
    if not path:
        return None
    try:
        target = Path(path)
        stat = target.stat()
        key = (str(target.resolve()), stat.st_size, stat.st_mtime_ns)
        if key not in _BUILD_CACHE:
            with target.open("rb") as handle:
                _BUILD_CACHE[key] = build_id_from_headers(handle.read(4096))
        return _BUILD_CACHE[key]
    except OSError:
        return None


def build_date(build_id: str | None) -> str | None:
    """The link date a build id carries, e.g. '2026-09-16'."""
    match = re.fullmatch(r"pe-([0-9a-f]{8})-[0-9a-f]{8}", str(build_id or ""))
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1), 16), timezone.utc).strftime("%Y-%m-%d")


# ---- identity -------------------------------------------------------------------

_SOCKET_FIELD = re.compile(r"s\d+")


def _socket_value(value: Any) -> Any:
    """A socket payload the way the game keeps it in itemDefinitionStruct.

    The save stores each filled socket as base64 JSON text; the game's own
    definition holds the decoded struct (measured 2026-09-24:
    "eyJhIjoy..." in the save, {"a":298537379,"b":39,"n":0} in the game)."""
    if isinstance(value, str):
        try:
            decoded = json.loads(base64.b64decode(value, validate=True))
        except (ValueError, TypeError):
            return value
        if isinstance(decoded, dict):
            return decoded
    return value


def identity_fields(definition: Mapping[str, Any], ignore: Iterable[str] = ()) -> dict[str, Any]:
    """The definition fields that decide what the game builds."""
    skip = PLACEMENT_FIELDS | frozenset(ignore)
    out = {
        str(key): _norm(_socket_value(value) if _SOCKET_FIELD.fullmatch(str(key)) else value)
        for key, value in definition.items()
        if str(key) not in skip
    }
    for key in DEFAULT_ONE_FIELDS:
        if out.get(key) == 1:
            out.pop(key)
    return out


_MISSING = object()


def definition_mismatch(record_definition: Mapping[str, Any], save_data: Mapping[str, Any],
                        ignore: Iterable[str] = ()) -> str | None:
    """None when a game record describes this saved item, else the first differing field.

    ``ignore`` names fields that do not change the item's lines - the stack
    count of a native stackable, whose stack the Vault merges and splits."""
    left, right = identity_fields(record_definition, ignore), identity_fields(save_data, ignore)
    if left == right:
        return None
    for key in sorted(set(left) | set(right)):
        if left.get(key, _MISSING) != right.get(key, _MISSING):
            return key
    return "?"


def timestamp_of_key(item_key: str | None) -> str | None:
    """The itemTimeStamp inside an editor item key ``<x>-<y>-<timestamp>-<class>``
    (None for the Vault's placeholder key ``0-0-0--1``)."""
    parts = str(item_key or "").split("-")
    if len(parts) < 4 or not re.fullmatch(r"\d{1,20}", parts[2]) or int(parts[2]) == 0:
        return None
    return parts[2]


def filter_info(info: Mapping[str, Any] | None) -> dict[str, Any]:
    """itemInfoStruct without sound and sprite references: the text and numbers."""
    out: dict[str, Any] = {}
    for key, value in (info or {}).items():
        if isinstance(value, str):
            if not value.startswith("@ref "):
                out[str(key)] = value
        elif _is_number(value):
            out[str(key)] = _norm(value)
    return out


# ---- store ----------------------------------------------------------------------

@dataclass
class IngestResult:
    files: int = 0
    lines: int = 0
    added: int = 0
    skipped: int = 0

    def merge(self, other: "IngestResult") -> "IngestResult":
        self.files += other.files
        self.lines += other.lines
        self.added += other.added
        self.skipped += other.skipped
        return self


@dataclass
class TruthMatch:
    """One game record that describes a saved item."""
    record: dict[str, Any]
    build_status: str            # current | other_build | unknown_build
    current_build: str | None
    candidates: int = 1
    rejected: dict[str, int] = field(default_factory=dict)


class TruthStore:
    """SQLite store of game-built items; safe to share between threads."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            connection = self._connection()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records(
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    type INTEGER,
                    build TEXT NOT NULL,
                    source TEXT NOT NULL,
                    recorded_at INTEGER NOT NULL,
                    hash TEXT,
                    def_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    native_json TEXT,
                    info_json TEXT,
                    content_key TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS records_by_ts ON records(ts, recorded_at);
                CREATE TABLE IF NOT EXISTS sources(
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    offset INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tooltips(
                    ts TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    build TEXT NOT NULL,
                    recorded_at INTEGER NOT NULL,
                    args_json TEXT NOT NULL,
                    rows_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    record_hash TEXT,
                    PRIMARY KEY(ts, hash, build)
                );
                CREATE TABLE IF NOT EXISTS tooltip_tables(
                    build TEXT PRIMARY KEY,
                    recorded_at INTEGER NOT NULL,
                    stats_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hash_aliases(
                    ts TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    content_key TEXT NOT NULL,
                    PRIMARY KEY(ts, hash)
                );
                CREATE TABLE IF NOT EXISTS evals(
                    req TEXT PRIMARY KEY,
                    build TEXT,
                    total INTEGER NOT NULL,
                    done INTEGER NOT NULL,
                    ok INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    rejected INTEGER NOT NULL,
                    finished INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),)
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tooltips)")}
            if "record_hash" in columns:   # a development version's link column
                try:
                    connection.execute("ALTER TABLE tooltips DROP COLUMN record_hash")
                except sqlite3.OperationalError:
                    pass
            if "record_key" not in columns:
                connection.execute("ALTER TABLE tooltips ADD COLUMN record_key TEXT")
            read = connection.execute("SELECT value FROM meta WHERE key='journals-read'").fetchone()
            if read is None or read[0] != _JOURNAL_READ_VERSION:
                # Journals read before drawings and hash aliases were kept are read
                # again once; records and tooltips are kept only once.
                connection.execute("DELETE FROM sources WHERE path LIKE ? OR path LIKE ?",
                                   ("%\\journal\\%", "%/journal/%"))
                connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('journals-read', ?)",
                                   (_JOURNAL_READ_VERSION,))
        # Per journal, the item record on the line just read, if that line was one:
        # (timestamp, content key, build). A tooltip the game drew for a request
        # comes right after the record of the item it built (see _link).
        self._last_item: dict[str, tuple[str, str, str]] = {}

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            # Autocommit; _ingest_file opens its own explicit transactions.
            connection = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False,
                                         isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    # ---- ingest -------------------------------------------------------------
    def _insert(self, connection: sqlite3.Connection, *, ts: str, item_type: Any, build: str,
                source: str, recorded_at: int, hash_text: Any, definition: Mapping,
                stats: Mapping, native: Mapping | None, info: Mapping | None) -> tuple[bool, str]:
        """Stores a record unless its content is already kept; (added, content key)."""
        type_value = int(item_type) if _is_number(item_type) and float(item_type).is_integer() else None
        def_json = _canonical(definition)
        stats_json = _canonical(stats)
        native_json = _canonical(native) if isinstance(native, Mapping) and native else None
        if native_json == stats_json:
            native_json = None
        info_json = _canonical(filter_info(info)) if isinstance(info, Mapping) else None
        content_key = hashlib.sha256(
            "|".join([ts, str(type_value), build, def_json, stats_json, native_json or ""]).encode("utf-8")
        ).hexdigest()
        cursor = connection.execute(
            """INSERT OR IGNORE INTO records
               (ts, type, build, source, recorded_at, hash, def_json, stats_json, native_json, info_json, content_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, type_value, build, source, int(recorded_at), str(hash_text) if isinstance(hash_text, str) else None,
             def_json, stats_json, native_json, info_json, content_key),
        )
        added = cursor.rowcount > 0
        if not added and isinstance(hash_text, str) and hash_text:
            # The game gives some items a new itemDataHash each time it builds them
            # (potions, essence vaults, forged gear, a few uniques): the content is
            # kept once, and every other hash it came with is an alias of it, so a
            # tooltip drawn under any of them finds its record.
            connection.execute(
                """INSERT OR IGNORE INTO hash_aliases(ts, hash, content_key) SELECT ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM records WHERE content_key=? AND hash=?)""",
                (ts, hash_text, content_key, content_key, hash_text),
            )
        return added, content_key

    def _ingest_file(self, path: Path, parse: Callable[[dict], dict | None]) -> IngestResult:
        result = IngestResult()
        try:
            stat = path.stat()
        except OSError:
            return result
        key = str(path.resolve())
        connection = self._connection()
        row = connection.execute("SELECT size, mtime_ns, offset FROM sources WHERE path=?", (key,)).fetchone()
        offset = 0
        if row is not None:
            if row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                return result
            offset = row["offset"] if row["offset"] <= stat.st_size else 0   # replaced by a shorter file
        result.files = 1
        pending = 0
        session = re.sub(r"-\d+\.ndjson$", "", path.name)   # a journal's parts continue each other
        with self._write_lock, path.open("rb") as handle:
            handle.seek(offset)
            try:
                connection.execute("BEGIN")
                for raw in handle:
                    if not raw.endswith(b"\n"):
                        break   # still being written; next time
                    offset += len(raw)
                    result.lines += 1
                    try:
                        document = json.loads(raw)
                        progress = _parse_eval_progress(document)
                        tooltip = None if progress is not None else _parse_tooltip(document)
                        parsed = None if progress is not None or tooltip is not None else parse(document)
                    except (ValueError, TypeError, KeyError):
                        progress = tooltip = parsed = None
                    if progress is not None:
                        self._last_item.pop(session, None)
                        self._record_progress(connection, progress)
                        continue
                    if tooltip is not None:
                        self._record_tooltip(connection, self._link(session, tooltip))
                        self._last_item.pop(session, None)
                        continue
                    if parsed is None:
                        self._last_item.pop(session, None)
                        result.skipped += 1
                        continue
                    added, content_key = self._insert(connection, **parsed)
                    if added:
                        result.added += 1
                    self._last_item[session] = (parsed["ts"], content_key, parsed["build"])
                    pending += 1
                    if pending >= _BATCH:
                        connection.execute(
                            "INSERT OR REPLACE INTO sources(path, size, mtime_ns, offset) VALUES (?, ?, ?, ?)",
                            (key, -1, -1, offset),
                        )
                        connection.execute("COMMIT")
                        connection.execute("BEGIN")
                        pending = 0
                done = offset >= stat.st_size
                connection.execute(
                    "INSERT OR REPLACE INTO sources(path, size, mtime_ns, offset) VALUES (?, ?, ?, ?)",
                    (key, stat.st_size if done else -1, stat.st_mtime_ns if done else -1, offset),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return result

    @staticmethod
    def _record_progress(connection: sqlite3.Connection, progress: dict[str, Any]) -> None:
        connection.execute(
            """INSERT INTO evals(req, build, total, done, ok, failed, rejected, finished, updated_at)
               VALUES (:req, :build, :total, :done, :ok, :failed, :rejected, :finished, :updated_at)
               ON CONFLICT(req) DO UPDATE SET build=excluded.build, total=excluded.total,
                   done=MAX(evals.done, excluded.done), ok=MAX(evals.ok, excluded.ok),
                   failed=MAX(evals.failed, excluded.failed), rejected=excluded.rejected,
                   finished=MAX(evals.finished, excluded.finished),
                   updated_at=MAX(evals.updated_at, excluded.updated_at)""",
            progress,
        )

    def _link(self, session: str, tooltip: dict[str, Any]) -> dict[str, Any]:
        """A tooltip the game drew for an editor request, tied to the content of
        the item it drew. The game builds the item (its record is the line right
        before) and draws it at once. The itemDataHash cannot tie them: the game
        gives some items (potions, essence vaults, forged gear, a few uniques) a
        new hash each time it builds them, identical content or not, and the store
        keeps the content once - under the hash it first saw. Only the line right
        before counts: items can share a timestamp."""
        if tooltip["kind"] != "tooltip" or not tooltip.get("request"):
            return tooltip
        last = self._last_item.get(session)
        if last is not None and last[0] == tooltip["ts"] and last[2] == tooltip["build"]:
            return {**tooltip, "record_key": last[1]}
        return tooltip

    @staticmethod
    def _record_tooltip(connection: sqlite3.Connection, tooltip: dict[str, Any]) -> None:
        if tooltip["kind"] == "tooltip-table":
            connection.execute(
                """INSERT INTO tooltip_tables(build, recorded_at, stats_json) VALUES (?, ?, ?)
                   ON CONFLICT(build) DO UPDATE SET recorded_at=excluded.recorded_at, stats_json=excluded.stats_json
                   WHERE excluded.recorded_at >= tooltip_tables.recorded_at""",
                (tooltip["build"], tooltip["recorded_at"], tooltip["stats_json"]),
            )
            return
        # Packed: a drawn tooltip is a few kilobytes of JSON, and the game draws one
        # for every item the player owns.
        connection.execute(
            """INSERT INTO tooltips(ts, hash, build, recorded_at, args_json, rows_json, stats_json, record_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ts, hash, build) DO UPDATE SET recorded_at=excluded.recorded_at,
                   args_json=excluded.args_json, rows_json=excluded.rows_json, stats_json=excluded.stats_json,
                   record_key=COALESCE(excluded.record_key, tooltips.record_key)
               WHERE excluded.recorded_at >= tooltips.recorded_at""",
            (tooltip["ts"], tooltip["hash"], tooltip["build"], tooltip["recorded_at"], _pack(tooltip["args_json"]),
             _pack(tooltip["rows_json"]), _pack(tooltip["stats_json"]), tooltip.get("record_key")),
        )

    def tooltip(self, timestamp: str, hash_text: str | None, build: str | None,
                record_key: str | None = None) -> dict[str, Any] | None:
        """The rows the game drew for this item's tooltip, if it has drawn it: a
        drawing tied to this record (``record_key``, its content key), or drawn
        under its hash or any hash the same content came with - the newest."""
        if not timestamp or not build or (hash_text is None and not record_key):
            return None
        row = self._connection().execute(
            """SELECT * FROM tooltips WHERE ts=? AND build=? AND (
                   hash=? OR record_key=?
                   OR hash IN (SELECT hash FROM hash_aliases WHERE ts=? AND content_key=?))
               ORDER BY record_key IS ? DESC, recorded_at DESC LIMIT 1""",
            (timestamp, build, hash_text, record_key, timestamp, record_key, record_key),
        ).fetchone()
        return None if row is None else {
            "recordedAt": row["recorded_at"], "args": _unpack(row["args_json"]),
            "rows": _unpack(row["rows_json"]), "stats": _unpack(row["stats_json"]),
        }

    def tooltip_keys(self, build: str | None) -> set[tuple[str, str]]:
        """What the game drew on this build: (itemTimeStamp, itemDataHash) of each
        drawn item, and (itemTimeStamp, "#" + content key) of each record a
        drawing is tied to or was drawn under an alias of."""
        if not build:
            return set()
        connection = self._connection()
        keys = set()
        for row in connection.execute("SELECT ts, hash, record_key FROM tooltips WHERE build=?", (build,)):
            keys.add((row[0], row[1]))
            if row[2]:
                keys.add((row[0], "#" + row[2]))
        for row in connection.execute(
                """SELECT a.ts, a.content_key FROM hash_aliases a
                   JOIN tooltips t ON t.ts = a.ts AND t.hash = a.hash WHERE t.build=?""", (build,)):
            keys.add((row[0], "#" + row[1]))
        return keys

    # A drawing request cut short leaves the item it was drawing under suspicion:
    # the game may have closed on it. An item suspected twice is not asked for
    # again on that build (hovering it in the game still records it).
    def drawing_strikes(self, build: str | None) -> dict[str, int]:
        if not build:
            return {}
        row = self._connection().execute("SELECT value FROM meta WHERE key=?", ("tipdraw-strikes:" + build,)).fetchone()
        try:
            strikes = json.loads(row["value"]) if row is not None else {}
        except ValueError:
            strikes = {}
        return {str(key): int(count) for key, count in strikes.items()} if isinstance(strikes, dict) else {}

    def strike_drawing(self, build: str | None, keys: Iterable[str]) -> dict[str, int]:
        strikes = self.drawing_strikes(build)
        if not build:
            return strikes
        for key in keys:
            strikes[str(key)] = strikes.get(str(key), 0) + 1
        with self._write_lock:
            self._connection().execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                       ("tipdraw-strikes:" + build, json.dumps(strikes, sort_keys=True)))
        return strikes

    def tooltip_table(self, build: str | None) -> list | None:
        """Every stat call of one tooltip on this build: the game's order, labels and formats."""
        if not build:
            return None
        row = self._connection().execute(
            "SELECT stats_json FROM tooltip_tables WHERE build=?", (build,)
        ).fetchone()
        return None if row is None else json.loads(row["stats_json"])

    def table_entries(self, build: str | None) -> dict[int, dict[str, Any]]:
        """``tooltip_table_entries`` of this build's table, parsed once per recording."""
        if not build:
            return {}
        row = self._connection().execute(
            "SELECT recorded_at FROM tooltip_tables WHERE build=?", (build,)
        ).fetchone()
        if row is None:
            return {}
        cache = self.__dict__.setdefault("_table_cache", {})
        cached = cache.get(build)
        if cached is None or cached[0] != row["recorded_at"]:
            cached = (row["recorded_at"], tooltip_table_entries(self.tooltip_table(build)))
            cache[build] = cached
        return cached[1]

    def evaluation(self, request_id: str) -> dict[str, Any] | None:
        """What ForgePact last reported about one evaluation request."""
        row = self._connection().execute("SELECT * FROM evals WHERE req=?", (request_id,)).fetchone()
        return None if row is None else {
            "request": row["req"], "build": row["build"], "total": row["total"], "done": row["done"],
            "ok": row["ok"], "failed": row["failed"], "rejected": row["rejected"],
            "finished": bool(row["finished"]), "updatedAt": row["updated_at"],
        }

    def ingest_journal_dir(self, folder: str | os.PathLike) -> IngestResult:
        """ForgePact's Item Truth journal files, from where the last call stopped."""
        total = IngestResult()
        directory = Path(folder)
        if not directory.is_dir():
            return total
        for path in sorted(directory.glob("*.ndjson")):
            total.merge(self._ingest_file(path, _parse_journal_record))
        return total

    def ingest_spool_dir(self, folder: str | os.PathLike, *, current_build: str | None,
                         build_since: float | None) -> IngestResult:
        """AFK FARM's delivery spool: the complete struct of every delivered item.

        The spool does not name the game build. A record made after the game
        exe on disk last changed (``build_since``, its mtime) was made by that
        exe's build; an older one keeps an unknown build."""
        total = IngestResult()
        directory = Path(folder)
        if not directory.is_dir():
            return total
        paths: set[Path] = set()
        for pattern in _SPOOL_PATTERNS:
            paths.update(directory.glob(pattern))
        for path in sorted(paths):
            total.merge(self._ingest_file(
                path, lambda record: _parse_spool_record(record, current_build, build_since)
            ))
        return total

    def prune_journal_dir(self, folder: str | os.PathLike, *, keep_seconds: float = 3 * 86400) -> int:
        """Delete journal files that are fully ingested and older than ``keep_seconds``."""
        directory = Path(folder)
        if not directory.is_dir():
            return 0
        removed = 0
        now = time.time()
        connection = self._connection()
        for path in directory.glob("*.ndjson"):
            try:
                stat = path.stat()
            except OSError:
                continue
            row = connection.execute(
                "SELECT size, mtime_ns FROM sources WHERE path=?", (str(path.resolve()),)
            ).fetchone()
            if row is None or row["size"] != stat.st_size or row["mtime_ns"] != stat.st_mtime_ns:
                continue
            if now - stat.st_mtime < keep_seconds:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    # ---- lookup -------------------------------------------------------------
    def lookup(self, item_key: str | None, data: Mapping[str, Any] | None, *,
               current_build: str | None, ignore: Iterable[str] = ()) -> TruthMatch | None:
        """The newest game record of this saved item, preferring the running build."""
        ignore = tuple(ignore)
        timestamp = timestamp_of_key(item_key)
        if timestamp is None or not isinstance(data, Mapping):
            return None
        try:
            item_type = int(str(item_key).rsplit("-", 1)[1])
        except (ValueError, IndexError):
            item_type = None
        rows = self._connection().execute(
            "SELECT * FROM records WHERE ts=? ORDER BY recorded_at DESC, id DESC", (timestamp,)
        ).fetchall()
        if not rows:
            return None
        rejected: dict[str, int] = {}
        best_other = None
        matches = 0
        for row in rows:
            if item_type is not None and row["type"] is not None and row["type"] != item_type:
                rejected["type"] = rejected.get("type", 0) + 1
                continue
            reason = definition_mismatch(json.loads(row["def_json"]), data, ignore)
            if reason is not None:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            matches += 1
            if current_build and row["build"] == current_build:
                return TruthMatch(_row_record(row), "current", current_build, matches, rejected)
            if best_other is None:
                best_other = row
        if best_other is None:
            return None
        status = "other_build" if current_build and best_other["build"] else "unknown_build"
        return TruthMatch(_row_record(best_other), status, current_build, matches, rejected)

    def explain(self, item_key: str | None, data: Mapping[str, Any] | None,
                ignore: Iterable[str] = ()) -> list[dict[str, Any]]:
        """Every record with this item's timestamp and why it does or does not match."""
        ignore = tuple(ignore)
        timestamp = timestamp_of_key(item_key)
        if timestamp is None or not isinstance(data, Mapping):
            return []
        rows = self._connection().execute(
            "SELECT * FROM records WHERE ts=? ORDER BY recorded_at DESC, id DESC", (timestamp,)
        ).fetchall()
        return [
            {"build": row["build"], "source": row["source"], "recordedAt": row["recorded_at"],
             "type": row["type"], "mismatch": definition_mismatch(json.loads(row["def_json"]), data, ignore),
             "def": json.loads(row["def_json"])}
            for row in rows
        ]

    def counts(self) -> dict[str, Any]:
        connection = self._connection()
        total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        builds = {
            (row[0] or "unknown"): row[1]
            for row in connection.execute("SELECT build, COUNT(*) FROM records GROUP BY build")
        }
        sources = {
            row[0]: row[1] for row in connection.execute("SELECT source, COUNT(*) FROM records GROUP BY source")
        }
        newest = connection.execute("SELECT MAX(recorded_at) FROM records").fetchone()[0]
        return {"records": total, "builds": builds, "sources": sources, "newestRecordedAt": newest}


def _row_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ts": row["ts"],
        "type": row["type"],
        "build": row["build"],
        "source": row["source"],
        "recordedAt": row["recorded_at"],
        "hash": row["hash"],
        "contentKey": row["content_key"],
        "def": json.loads(row["def_json"]),
        "stats": json.loads(row["stats_json"]),
        "native": json.loads(row["native_json"]) if row["native_json"] else None,
        "info": json.loads(row["info_json"]) if row["info_json"] else {},
    }


def _parse_eval_progress(record: Any) -> dict | None:
    """A ForgePact progress line of an evaluation or drawing request, or None.
    Request ids are unique across both kinds, so they share one table."""
    if not isinstance(record, dict) or record.get("v") != JOURNAL_SCHEMA or record.get("kind") not in REQUEST_FOLDERS:
        return None
    request = record.get("req")
    if not isinstance(request, str) or not REQUEST_ID.fullmatch(request):
        return None
    counts = {}
    for name in ("total", "done", "ok", "failed", "rejected"):
        value = record.get(name)
        counts[name] = int(value) if _is_number(value) and float(value) >= 0 else 0
    updated = record.get("t")
    return {"req": request, "build": str(record.get("build") or ""), **counts,
            "finished": 1 if record.get("finished") is True else 0,
            "updated_at": int(updated) if _is_number(updated) else 0}


def _parse_tooltip(record: Any) -> dict | None:
    """A ForgePact tooltip or tooltip-table line, or None."""
    if not isinstance(record, dict) or record.get("v") != JOURNAL_SCHEMA:
        return None
    kind = record.get("kind")
    if kind not in {"tooltip", "tooltip-table"}:
        return None
    stats = record.get("stats")
    recorded = record.get("t")
    common = {"kind": kind, "build": str(record.get("build") or ""),
              "recorded_at": int(recorded) if _is_number(recorded) else 0}
    if not isinstance(stats, list) or not common["build"]:
        return None
    if kind == "tooltip-table":
        return {**common, "stats_json": json.dumps(stats, ensure_ascii=False, separators=(",", ":"))}
    ts = _whole_text(record.get("ts"))
    rows, args = record.get("rows"), record.get("args")
    if ts is None or not isinstance(rows, list) or not isinstance(args, list):
        return None
    request = record.get("req")
    return {**common, "ts": ts, "hash": str(record.get("hash") or ""),
            "request": request if isinstance(request, str) and REQUEST_ID.fullmatch(request) else None,
            "args_json": json.dumps(args, ensure_ascii=False, separators=(",", ":")),
            "rows_json": json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
            "stats_json": json.dumps(stats, ensure_ascii=False, separators=(",", ":"))}


def _parse_journal_record(record: dict) -> dict | None:
    if not isinstance(record, dict) or record.get("v") != JOURNAL_SCHEMA or record.get("kind") is not None:
        return None
    ts = _whole_text(record.get("ts"))
    definition, stats = record.get("def"), record.get("stats")
    if ts is None or not isinstance(definition, dict) or not isinstance(stats, dict):
        return None
    recorded = record.get("t")
    return {
        "ts": ts,
        "item_type": record.get("type"),
        "build": str(record.get("build") or ""),
        "source": str(record.get("src") or "live")[:16],
        "recorded_at": int(recorded) if _is_number(recorded) else 0,
        "hash_text": record.get("hash"),
        "definition": definition,
        "stats": stats,
        "native": record.get("native") if isinstance(record.get("native"), dict) else None,
        "info": record.get("info") if isinstance(record.get("info"), dict) else None,
    }


def _parse_iso_ms(text: Any) -> int | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _parse_spool_record(record: dict, current_build: str | None, build_since: float | None) -> dict | None:
    if not isinstance(record, dict) or record.get("kind") != "item":
        return None
    item = record.get("item")
    if not isinstance(item, dict):
        return None
    ts = _whole_text(item.get("itemTimeStamp"))
    definition, stats = item.get("itemDefinitionStruct"), item.get("itemStatStruct")
    if ts is None or not isinstance(definition, dict) or not isinstance(stats, dict):
        return None
    recorded = _parse_iso_ms(record.get("t")) or 0
    build = ""
    if current_build and build_since is not None and recorded and recorded >= build_since * 1000:
        build = current_build
    return {
        "ts": ts,
        "item_type": item.get("itemType", record.get("type")),
        "build": build,
        "source": "spool",
        "recorded_at": recorded,
        "hash_text": item.get("itemDataHash"),
        "definition": definition,
        "stats": stats,
        "native": None,
        "info": item.get("itemInfoStruct") if isinstance(item.get("itemInfoStruct"), dict) else None,
    }


# ---- evaluation requests -------------------------------------------------------
# The game builds items it has not built yet when the editor asks: one line per
# item, "<item key>\t<save data json>", in <root>/requests/<id>.req. ForgePact
# claims it (<id>.working), builds each item through the game's own save loader,
# journals it with src "eval", and deletes the file when done; a check it could
# not finish is left as <id>.stopped.
# Drawing requests have the same lines in <root>/tips: while the player has an
# item tooltip open, the game draws the requested items' tooltips off screen and
# journals each like a tooltip the player saw, with "req":"<id>".
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_REQUEST_ITEMS = 50000
REQUEST_FOLDERS = {"eval": "requests", "tipdraw": "tips"}


def write_eval_request(root: str | os.PathLike, entries: Iterable[tuple[str, Mapping[str, Any]]],
                       kind: str = "eval") -> tuple[str | None, int]:
    """Queue items for the game to build (``kind`` "eval") or to draw ("tipdraw");
    returns (request id, items written)."""
    lines = []
    seen = set()
    for key, data in entries:
        if timestamp_of_key(key) is None or not re.fullmatch(r"\d{1,20}-\d{1,20}-\d{1,20}-\d{1,4}", str(key)):
            continue
        if not isinstance(data, Mapping):
            continue
        text = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        marker = (key, text)
        if marker in seen:
            continue
        seen.add(marker)
        lines.append(f"{key}\t{text}\n")
        if len(lines) >= MAX_REQUEST_ITEMS:
            break
    if not lines:
        return None, 0
    folder = Path(root) / REQUEST_FOLDERS[kind]
    folder.mkdir(parents=True, exist_ok=True)
    request_id = f"{int(time.time() * 1000)}-{os.urandom(3).hex()}"
    temp = folder / f"{request_id}.tmp"
    temp.write_text("".join(lines), encoding="utf-8", newline="\n")
    os.replace(temp, folder / f"{request_id}.req")
    return request_id, len(lines)


def request_files(root: str | os.PathLike, kind: str = "eval") -> dict[str, list[str]]:
    """Request ids by state on disk: waiting (.req), running (.working), stopped."""
    folder = Path(root) / REQUEST_FOLDERS[kind]
    states = {"waiting": [], "running": [], "stopped": []}
    suffixes = {".req": "waiting", ".working": "running", ".stopped": "stopped"}
    if folder.is_dir():
        for path in sorted(folder.iterdir()):
            state = suffixes.get(path.suffix)
            if state and REQUEST_ID.fullmatch(path.stem):
                states[state].append(path.stem)
    return states


def clear_stopped_requests(root: str | os.PathLike, kind: str = "eval") -> int:
    removed = 0
    folder = Path(root) / REQUEST_FOLDERS[kind]
    for request_id in request_files(root, kind)["stopped"]:
        try:
            (folder / f"{request_id}.stopped").unlink()
            removed += 1
        except OSError:
            continue
    return removed


def stopped_request_keys(root: str | os.PathLike, request_id: str, kind: str = "tipdraw") -> list[str]:
    """The item keys of a stopped request, in the order the game took them."""
    if not REQUEST_ID.fullmatch(str(request_id)):
        return []
    path = Path(root) / REQUEST_FOLDERS[kind] / f"{request_id}.stopped"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line.split("\t", 1)[0] for line in text.splitlines() if "\t" in line]


# ---- capture request and ForgePact status --------------------------------------

def request_capture(root: str | os.PathLike, *, editor_version: str) -> bool:
    """Ask ForgePact to journal finished items (idempotent)."""
    target = Path(root) / "capture.request"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            payload = {"schema": 1, "requestedBy": "hero-siege-item-editor", "version": editor_version,
                       "since": int(time.time() * 1000)}
            temp = target.with_suffix(".tmp")
            temp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temp, target)
        return True
    except OSError:
        return False


def withdraw_capture(root: str | os.PathLike) -> bool:
    try:
        (Path(root) / "capture.request").unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def capture_status(root: str | os.PathLike, *, now: float | None = None) -> dict[str, Any]:
    """What ForgePact last said about its capture (status.json), if anything."""
    base = Path(root)
    status: dict[str, Any] = {
        "requested": (base / "capture.request").is_file(),
        "reporting": False,
        "forgepact": None,
    }
    try:
        document = json.loads((base / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return status
    if isinstance(document, dict):
        updated = document.get("updated")
        moment = time.time() if now is None else now
        status["forgepact"] = {
            key: document.get(key)
            for key in ("forgepact", "build", "pid", "started", "updated", "written", "dropped", "file")
        }
        status["reporting"] = _is_number(updated) and moment * 1000 - float(updated) < 120_000
    return status


# ---- the game's own tooltip text --------------------------------------------------
# ForgePact records the text rows the game draws for an item's tooltip (the first
# time in a session it draws that item) and, once per session, every stat call of
# one tooltip: the table of the lines a tooltip can draw, in the game's order, each
# with its label, format and colour.

# "(Based on Level)" lines are multiplied by the viewing character's level. The
# editor shows them at the level cap, where the captured tooltips of a level-100
# character read +300 for 3 per level.
TABLE_LEVEL = 100
# The key hint under every tooltip ("ALT - Show Information") does nothing in the editor.
_KEY_HINT = re.compile(r"[A-Za-z0-9]{1,12} - Show Information")
# The roll range the game's ALT view adds after a stat line (" [45-75]").
_RANGE_PIECE = re.compile(r" \[-?\d+(?:\.\d+)?-(?:-)?\d+(?:\.\d+)?\]")
# DrawInventoryStatsNew formats: 2 percent and 3 flat stat lines; 5 and 6 the same for
# a value passed in (the star level rows); 0 and 1 the header lines (Defense: 36).
_VALUE_FORMATS = {2, 3, 5, 6}
_STAT_FORMATS = {2, 3}
# Header values the tooltip draws above its stat lines: damage, speed, defense, block.
_HEADER_KEYS = (22, 23, 154, 157)
# Lines that name the class of stat 21 after them: to All Skills, to <element> Skills.
_CLASS_SKILL_KEYS = frozenset({201, 222, 223, 224, 225, 226, 227})
# Where a *_colour draw builtin takes its first colour.
_COLOUR_ARG = {"draw_text_colour": 3, "draw_text_ext_colour": 5,
               "draw_text_transformed_colour": 6, "draw_text_ext_transformed_colour": 8}


def gm_colour(value: Any) -> str | None:
    """A GameMaker colour (0xBBGGRR) as #rrggbb."""
    if not _is_number(value):
        return None
    number = int(value) & 0xFFFFFF
    return "#%02x%02x%02x" % (number & 0xFF, (number >> 8) & 0xFF, (number >> 16) & 0xFF)


def captured_tooltip_rows(tooltip: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The lines of a tooltip the game drew, top to bottom, as the player saw them.

    Every draw call is one piece: its text, colour and position. Outline copies
    (the same text drawn again within 3 px) keep only the last draw. Pieces on one
    line join left to right; a stat line's value and label get the space the game
    leaves between them. ``stat`` names the stat a line draws, ``gap`` marks the
    space the game leaves above a new section, ``block`` a wrapped paragraph.

    A stat call's line is the first one at or below the call's position that the
    call drew with the game's left-aligned pieces: ForgePact's own centred rows
    for a forged item are drawn inside the same call, above the game's row."""
    stat_calls = []
    for entry in tooltip.get("stats") or ():
        args = entry.get("a") if isinstance(entry, Mapping) else None
        if isinstance(args, list) and len(args) > 6 and _is_number(args[1]):
            stat_calls.append((float(args[1]), entry.get("id"), args[5]))
    pieces: list[dict[str, Any] | None] = []
    for row in tooltip.get("rows") or ():
        if not isinstance(row, Mapping):
            continue
        function = str(row.get("fn") or "")
        args = row.get("a") if isinstance(row.get("a"), list) else []
        if len(args) < 3 or not _is_number(args[0]) or not _is_number(args[1]):
            continue
        text = args[2] if isinstance(args[2], str) else next((v for v in args if isinstance(v, str)), None)
        if not text:
            continue
        x, y = float(args[0]), float(args[1])
        colour_at = _COLOUR_ARG.get(function)
        colour = gm_colour(args[colour_at] if colour_at is not None and len(args) > colour_at else row.get("c"))
        for index, other in enumerate(pieces):
            if other is not None and other["text"] == text and abs(other["x"] - x) <= 3 and abs(other["y"] - y) <= 3:
                pieces[index] = None
        pieces.append({"text": text, "color": colour, "x": x, "y": y, "block": "_ext" in function,
                       "call": row.get("s"), "centred": row.get("ha") == 1})
    lines: list[dict[str, Any]] = []
    for piece in sorted((p for p in pieces if p is not None), key=lambda p: (p["y"], p["x"])):
        last = lines[-1] if lines else None
        if last is not None and not last["block"] and not piece["block"] and abs(last["y"] - piece["y"]) <= 2:
            last["pieces"].append(piece)
        else:
            lines.append({"y": piece["y"], "block": piece["block"], "pieces": [piece]})
    calls_by_line: dict[int, tuple] = {}
    for call in stat_calls:
        candidates = [
            index for index, line in enumerate(lines)
            if call[0] - 2 <= line["y"] <= call[0] + 150 and index not in calls_by_line
            and not all(piece["centred"] for piece in line["pieces"])
            and any(piece["call"] == call[1] for piece in line["pieces"])
        ]
        if candidates:
            calls_by_line[min(candidates, key=lambda index: lines[index]["y"])] = call
    rows: list[dict[str, Any]] = []
    previous = None
    for index, line in enumerate(lines):
        call = calls_by_line.get(index)
        spaced = call is not None and call[2] in _VALUE_FORMATS
        parts: list[dict[str, Any]] = []
        for piece in line["pieces"]:
            text = piece["text"]
            if parts and spaced and not parts[-1]["text"].endswith(" ") and not text.startswith(" "):
                text = " " + text
            parts.append({"text": text, "color": piece["color"]})
        if _KEY_HINT.fullmatch("".join(part["text"] for part in parts).strip()):
            continue
        stat = None
        if call is not None and call[2] in _STAT_FORMATS and _is_number(call[1]) and float(call[1]) >= 0:
            stat = int(call[1])
        rows.append({
            "parts": parts,
            "stat": stat,
            # Drawn in the game's ALT view: the line shows its own roll range.
            "ranged": stat is not None and len(parts) > 1 and bool(_RANGE_PIECE.fullmatch(parts[-1]["text"])),
            "gap": previous is not None and (previous["block"] or line["y"] - previous["y"] > 36),
            "block": line["block"],
        })
        previous = line
    return rows


def tooltip_table_entries(table: Iterable[Any] | None) -> dict[int, dict[str, Any]]:
    """Stat key -> how the game draws that stat line: its place, label, format, colour."""
    out: dict[int, dict[str, Any]] = {}
    for index, entry in enumerate(table or ()):
        if not isinstance(entry, Mapping):
            continue
        args, key = entry.get("a"), entry.get("id")
        if not isinstance(args, list) or len(args) < 11 or not _is_number(key) or float(key) < 0:
            continue
        if args[5] not in _STAT_FORMATS or args[6] not in (8, 9):
            continue
        out.setdefault(int(key), {
            "index": index,
            "label": args[4] if isinstance(args[4], str) else "",
            "percent": args[5] == 2,
            "valueFirst": args[6] == 8,
            "perLevel": _flag(args[8]),
            "negated": _flag(args[9]),
            "color": gm_colour(args[10]),
        })
    return out


def _flag(value: Any) -> bool:
    return value is True or (_is_number(value) and float(value) != 0)


_TALENT_NAME_CACHE: dict[tuple, dict[str, str]] = {}


def talent_names(folder: str | os.PathLike | None) -> dict[str, str]:
    """English talent names by slug from the game's own translation files
    (``bin/translations*.csv``: ``talent_name_<slug>|<English>|...``); read again
    only when a file changes."""
    if not folder:
        return {}
    try:
        files = sorted(Path(folder).glob("translations*.csv"))
        key = (str(folder), tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in files))
    except OSError:
        return {}
    if key in _TALENT_NAME_CACHE:
        return _TALENT_NAME_CACHE[key]
    names: dict[str, str] = {}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        for line in text.splitlines():
            if line.startswith("talent_name_"):
                parts = line.split("|")
                if len(parts) > 1 and parts[1].strip():
                    names.setdefault(parts[0][len("talent_name_"):], parts[1].strip())
    _TALENT_NAME_CACHE.clear()
    _TALENT_NAME_CACHE[key] = names
    return names


def _game_number(value: float) -> str:
    """Whole numbers plain, anything else with two decimals, as the game prints
    them (+1.50 to Projectile Speed)."""
    rounded = round(float(value), 2)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.2f}"


def table_line_text(entry: Mapping[str, Any], value: float, *, level: int = TABLE_LEVEL,
                    label: str | None = None) -> dict[str, Any]:
    """One stat line as the game's table draws it: a value-first line signs its value
    (+449% Enhanced Damage, -25% to All Enemy Resistances), a label-first line does
    not (Ailment damage increased by 35%); per-level lines are multiplied by the
    level and negated lines drawn below zero."""
    number = float(value) * (level if entry.get("perLevel") else 1)
    if entry.get("negated"):
        number = -number
    text = _game_number(number) + ("%" if entry.get("percent") else "")
    if entry.get("valueFirst") and number >= 0:
        text = "+" + text
    return {
        "value": text,
        "label": entry.get("label", "") if label is None else label,
        "valueFirst": bool(entry.get("valueFirst")),
        "color": entry.get("color"),
        "index": entry.get("index"),
        "perLevel": bool(entry.get("perLevel")),
    }


def _apply_game_table(lines: list[dict[str, Any]], internal: list[dict[str, Any]],
                      entries: Mapping[int, Mapping[str, Any]], semantics: Any, level: int) -> list[dict[str, Any]]:
    """The game's label, value text, colour and order on every line its table draws.
    Checked against 7,628 tooltips the game drew (2026-09-24): all 41,920 lines
    they share read the same.

    * a line whose value is 0 is not drawn;
    * a skill grant draws as one line, "+16 to Omnislash (Samurai)": its level stat
      carries the label "to ", the skill stat before it names the skill and the
      class stat after it the class; "+3 to All Skills (Illusionist)" and the
      element lines, "+2 to Fire Skills (Exo)", take their class from stat 21.
      Those skill and class lines do not stand alone."""
    by_key = {line["statKey"]: line for line in lines}
    folded = set()

    def value_of(key: int, kinds: set[str]) -> str | None:
        other = by_key.get(key)
        kind = str((_semantic(semantics, key) or {}).get("valueKind") or "")
        if other is None or kind not in kinds or not other.get("formattedValue"):
            return None
        folded.add(key)
        return str(other["formattedValue"])

    for line in lines:
        key = line["statKey"]
        entry = entries.get(key)
        if entry is None or not _is_number(line.get("value")):
            continue
        if float(line["value"]) == 0:
            folded.add(key)
            continue
        label = entry.get("label", "")
        if label.endswith(" "):
            skill = value_of(key - 1, {"skill_id", "talent_id"})
            if skill:
                label += skill
                role = value_of(key + 1, {"class_id"})
                if role:
                    label += f" ({role})"
        elif key in _CLASS_SKILL_KEYS:
            role = value_of(21, {"class_id"})
            if role:
                label += f" ({role})"
        line["game"] = table_line_text(entry, float(line["value"]), level=level, label=label or None)
    internal.extend(line for line in lines if line["statKey"] in folded)
    drawn = [line for line in lines if line["statKey"] not in folded]

    def place(pair: tuple[int, dict[str, Any]]) -> tuple:
        order, line = pair
        if line["statKey"] in _HEADER_KEYS:
            return (0, _HEADER_KEYS.index(line["statKey"]), 0)
        if "game" in line:
            return (1, line["game"]["index"], 0)
        return (2, 0, order)

    return [line for _, line in sorted(enumerate(drawn), key=place)]


# ---- the verified tooltip -------------------------------------------------------

def _semantic(semantics: Any, key: int) -> dict[str, Any] | None:
    if semantics is None:
        return None
    try:
        meta = semantics.get(key)
    except Exception:  # noqa: BLE001 - a broken database must not break tooltips
        return None
    return meta if isinstance(meta, dict) else None


def _draw_order(meta: Mapping[str, Any] | None, key: int) -> tuple:
    """DrawInventoryItemV2 draws its stat blocks in code order; the address of
    each block (stat semantics evidence) is that order. Proc lines come from
    GetItemTooltipString, then everything the renderers do not draw."""
    evidence = (meta or {}).get("evidence") or {}
    function = evidence.get("function")
    found = re.search(r"0x([0-9A-Fa-f]+)", str(evidence.get("location") or ""))
    address = int(found.group(1), 16) if found else 1 << 40
    rank = {"gml_Script_DrawInventoryItemV2": 0, "gml_Script_DrawInventoryStatsNew": 0,
            "gml_Script_GetItemTooltipString": 1}.get(function, 2)
    return (rank, address, key)


def _drawn(meta: Mapping[str, Any] | None, key: int) -> bool:
    if key in HIDDEN_STAT_KEYS:
        return False
    if meta is None:
        return True   # not in the database: show it rather than hide a real line
    if meta.get("name") == "unknown":
        return False
    return True


_CLASS_NAME_CACHE: dict[int, dict[int, str]] = {}


def _class_name(semantics: Any, class_id: int) -> str | None:
    names = _CLASS_NAME_CACHE.get(id(semantics))
    if names is None:
        try:
            names = {int(row["id"]): str(row["label"]) for row in semantics.pickers()["classes"]}
        except Exception:  # noqa: BLE001
            names = {}
        _CLASS_NAME_CACHE[id(semantics)] = names
    return names.get(class_id)


def _line_value(meta: Mapping[str, Any] | None, value: float, semantics: Any,
                talent_names: Mapping[str, str] | None = None) -> tuple[str, bool]:
    kind = str((meta or {}).get("valueKind") or "")
    percent = kind == "percent"
    if kind == "boolean":
        return "", False
    if semantics is not None and float(value).is_integer():
        if kind in {"skill_id", "talent_id"}:
            try:
                talent = semantics.talent(int(value))
            except Exception:  # noqa: BLE001
                talent = None
            if talent and (talent.get("name") or talent.get("slug")):
                # Relic skills carry only a slug; the game names them from its
                # translation files (talent_name_relicMeatHook -> Meat Hook).
                slug = str(talent.get("slug") or "")
                return str(talent.get("name") or (talent_names or {}).get(slug) or slug), False
        elif kind == "class_id":
            name = _class_name(semantics, int(value))
            if name:
                return name, False
    return _format_value(value, percent), percent


def build_verified_model(offline: Mapping[str, Any], match: TruthMatch, *,
                         semantics: Any = None, custom_name: str | None = None,
                         tooltip: Mapping[str, Any] | None = None,
                         table: Mapping[int, Mapping[str, Any]] | None = None,
                         level: int = TABLE_LEVEL,
                         talent_names: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The tooltip model with every number taken from the game's own record.

    ``offline`` (exact_tooltip's model) contributes only what the record
    cannot know: the definition ranges of fixed stats, seeds and socket
    payloads for the details view, and skill-selector identities.

    ``tooltip`` is the game's own drawing of this item (``TruthStore.tooltip``):
    its rows become ``gameText``, the tooltip exactly as the game showed it.
    Without one, ``table`` (``TruthStore.table_entries`` of the record's build)
    gives each stat line the game's label, value text, colour and order."""
    record = match.record
    stats = record.get("stats") or {}
    info = record.get("info") or {}
    offline_lines = {
        line["statKey"]: line
        for line in (offline.get("stats") or [])
        if isinstance(line, Mapping) and isinstance(line.get("statKey"), int)
    }
    affixes: dict[int, tuple[Any, Any, Any, int]] = {}
    for slot in AFFIX_SLOT_KEYS:
        entry = stats.get(slot)
        if isinstance(entry, list) and len(entry) >= 3 and all(_is_number(v) for v in entry[:3]):
            affixes[int(entry[0])] = (entry[1], entry[2], entry[3] if len(entry) > 3 else None, int(slot))
    lines: list[tuple[tuple, dict[str, Any]]] = []
    internal: list[dict[str, Any]] = []
    maxed = total = 0
    deficit = 0.0
    differences = []
    for key_text, value in stats.items():
        if key_text in AFFIX_SLOT_KEYS or not _is_number(value) or not re.fullmatch(r"-?\d+", str(key_text)):
            continue
        key = int(key_text)
        meta = _semantic(semantics, key)
        offline_line = offline_lines.get(key)
        label = None
        if meta and meta.get("name") and meta.get("name") != "unknown":
            label = str(meta["name"])
        elif offline_line and offline_line.get("label"):
            label = str(offline_line["label"])
        else:
            label = f"Stat #{key}"
        formatted, percent = _line_value(meta, float(value), semantics, talent_names)
        if meta is None and offline_line is not None:
            percent = bool(offline_line.get("percent"))
            formatted = _format_value(float(value), percent)
        minimum = maximum = None
        affix = affixes.get(key)
        if affix is not None:
            minimum, maximum = affix[0], affix[1]
        elif offline_line is not None:
            minimum, maximum = offline_line.get("minimum"), offline_line.get("maximum")
        # A value outside the definition range did not come from that range: the
        # item's star level scales it (a 5-star jacket: Defense 108 from 70-90) or a
        # Custom Forge entry set it. Showing the range beside it would mislead.
        beyond = (
            _is_number(minimum) and _is_number(maximum)
            and not float(minimum) - 1e-9 <= float(value) <= float(maximum) + 1e-9
        )
        if beyond:
            minimum = maximum = None
        line = {
            "id": f"game:{key}",
            "statKey": key,
            "label": label,
            "value": _norm(value),
            "formattedValue": formatted,
            "minimum": _norm(minimum) if _is_number(minimum) else None,
            "maximum": _norm(maximum) if _is_number(maximum) else None,
            "percent": percent,
            "rolled": _is_number(minimum) and _is_number(maximum) and float(minimum) < float(maximum),
            "saveField": None,
            "role": "affix" if affix is not None else "item",
            "confidence": "game",
            "catalogLineIndex": offline_line.get("catalogLineIndex") if offline_line else None,
            "sourceRange": offline_line.get("sourceRange") if offline_line else None,
        }
        if affix is not None:
            line["affixTier"] = _norm(affix[2])
            line["affixSlot"] = affix[3]
        if beyond:
            line["beyondRange"] = True
        if line["rolled"]:
            total += 1
            if float(value) >= float(maximum) - 1e-9:
                maxed += 1
            else:
                deficit += float(maximum) - float(value)
        if offline_line is not None:
            estimate = offline_line.get("value")
            if _is_number(estimate) and not math.isclose(float(estimate), float(value), abs_tol=1e-6):
                differences.append({"statKey": key, "label": label, "estimate": _norm(estimate), "game": _norm(value)})
        if _drawn(meta, key):
            lines.append((_draw_order(meta, key), line))
        else:
            internal.append(line)
    lines.sort(key=lambda pair: pair[0])
    rolled_lines = [line for _, line in lines]
    if table:
        rolled_lines = _apply_game_table(rolled_lines, internal, table, semantics, level)
    for key, offline_line in offline_lines.items():
        if str(key) not in stats:
            differences.append({"statKey": key, "label": offline_line.get("label"),
                                "estimate": offline_line.get("value"), "game": None})

    base = dict(offline.get("item") or {})
    display = info.get("28") if isinstance(info.get("28"), str) and info.get("28") else None
    rarity_code = info.get("27")
    tier_code = info.get("32")
    level = info.get("1")
    alias = custom_name.strip() if isinstance(custom_name, str) and custom_name.strip() else base.get("customName")
    item = {
        **base,
        "name": alias or display or base.get("name"),
        "canonicalName": display or base.get("canonicalName") or base.get("name"),
        "customName": alias,
        "baseName": base.get("canonicalName") or base.get("name"),
        "prefix": info.get("5") or None,
        "suffix": info.get("4") or None,
        "rarity": RARITY_NAMES.get(int(rarity_code)) if _is_number(rarity_code) and int(rarity_code) in RARITY_NAMES else base.get("rarity"),
        "rarityCode": _norm(rarity_code) if _is_number(rarity_code) else None,
        "rolledRarityKnown": True,
        "tier": TIER_NAMES.get(int(tier_code)) if _is_number(tier_code) and int(tier_code) in TIER_NAMES else base.get("tier"),
        "requiredLevel": _norm(level) if _is_number(level) and float(level) > 0 else base.get("requiredLevel"),
    }
    current = match.build_status == "current"
    warnings = []
    if not current:
        warnings.append(
            "Recorded by the game on build " + (build_date(record.get("build")) or "unknown")
            + "; the running build may roll this item differently."
        )
    game_text = None
    if isinstance(tooltip, Mapping):
        rows = captured_tooltip_rows(tooltip)
        if rows:
            game_text = {"rows": rows, "recordedAt": tooltip.get("recordedAt"),
                         "build": record.get("build") or None}
    text_source = "game" if game_text else "table" if any("game" in line for line in rolled_lines) else None
    return {
        "schemaVersion": offline.get("schemaVersion", 1),
        "profileId": offline.get("profileId"),
        "fingerprint": offline.get("fingerprint"),
        "item": item,
        "seeds": offline.get("seeds") or {},
        "stats": rolled_lines,
        "internalStats": internal,
        "sockets": offline.get("sockets") or [],
        "identities": offline.get("identities") or [],
        "rollQuality": {
            "maxed": maxed, "total": total, "endpointDeficit": _norm(deficit),
            "percent": round(100.0 * maxed / total, 2) if total else None,
        },
        "gameText": game_text,
        "calculation": {
            "coverage": "game_verified",
            "numbersExact": current,
            "textExact": game_text is not None,
            "textSource": text_source,
            "buildMatched": current,
            "unsupportedPaths": [] if current else ["game_record_other_build"],
            "warnings": warnings,
        },
        "verification": {
            "status": match.build_status,
            "source": record.get("source"),
            "build": record.get("build") or None,
            "buildDate": build_date(record.get("build")),
            "currentBuild": match.current_build,
            "currentBuildDate": build_date(match.current_build),
            "recordedAt": record.get("recordedAt"),
            "affixCount": len(affixes),
            "estimateDifferences": differences,
        },
        "buildGuard": offline.get("buildGuard"),
    }


def mark_estimate(model: dict[str, Any], *, current_build: str | None) -> dict[str, Any]:
    """An offline model shown on a build its rules were not made for.

    exact_tooltip keeps its own per-line detail; only the promise changes:
    the numbers are the 2026-08-28 build's rules, not the running game's.
    A catalog-only preview replays nothing and is left as it is."""
    calculation = dict(model.get("calculation") or {})
    if current_build == MODEL_BUILD_ID or calculation.get("coverage") not in {"exact_numbers", "partial"}:
        return model
    if calculation.get("coverage") == "exact_numbers":
        calculation["coverage"] = "estimate"
    calculation["numbersExact"] = False
    warnings = list(calculation.get("warnings") or [])
    running = build_date(current_build)
    warnings.insert(0, (
        f"Estimated with the {build_date(MODEL_BUILD_ID)} build's rules; the game runs "
        + (f"the {running} build." if running else "a build the editor could not identify.")
        + " Load the item in the game once to see its real values."
    ))
    calculation["warnings"] = warnings
    out = dict(model)
    out["calculation"] = calculation
    out["verification"] = {"status": "estimate", "currentBuild": current_build,
                           "currentBuildDate": running, "modelBuild": MODEL_BUILD_ID,
                           "modelBuildDate": build_date(MODEL_BUILD_ID)}
    return out


# ---- background ingest --------------------------------------------------------

class TruthIngestor:
    """Keeps the store up to date while the editor runs.

    Journals every ``journal_interval`` seconds (cheap when nothing changed),
    the AFK spool every ``spool_interval`` seconds."""

    def __init__(self, store: TruthStore, *, journal_dir: Path, spool_dir: Path | None,
                 build_info: Callable[[], tuple[str | None, float | None]],
                 journal_interval: float = 3.0, spool_interval: float = 60.0) -> None:
        self.store = store
        self.journal_dir = Path(journal_dir)
        self.spool_dir = Path(spool_dir) if spool_dir else None
        self.build_info = build_info
        self.journal_interval = journal_interval
        self.spool_interval = spool_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.last_result = IngestResult()

    def run_once(self, *, spool: bool = True) -> IngestResult:
        result = IngestResult()
        result.merge(self.store.ingest_journal_dir(self.journal_dir))
        if spool and self.spool_dir is not None:
            build, since = self.build_info()
            result.merge(self.store.ingest_spool_dir(self.spool_dir, current_build=build, build_since=since))
        self.store.prune_journal_dir(self.journal_dir)
        self.last_result = result
        return result

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="game-truth-ingest", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        next_spool = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            try:
                self.run_once(spool=now >= next_spool)
                if now >= next_spool:
                    next_spool = now + self.spool_interval
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - keep ingesting on the next tick
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.journal_interval)


__all__ = [
    "AFFIX_SLOT_KEYS", "MODEL_BUILD_ID", "RARITY_NAMES", "TABLE_LEVEL", "TIER_NAMES",
    "IngestResult", "TruthIngestor", "TruthMatch", "TruthStore",
    "build_date", "build_id_from_headers", "build_id_of_exe", "build_verified_model",
    "capture_status", "captured_tooltip_rows", "definition_mismatch", "filter_info", "gm_colour",
    "identity_fields", "mark_estimate", "request_capture", "table_line_text", "timestamp_of_key",
    "tooltip_table_entries", "withdraw_capture",
]
