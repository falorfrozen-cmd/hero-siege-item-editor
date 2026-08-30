"""Crash-safe, SQLite-backed storage for the Item Editor's Infinite Vault.

This module deliberately knows nothing about game save locations or formats.  It
stores one already-extracted item record as exact JSON text and exposes a small
transactional API that the editor can compose with its existing save writer.

Cross-store deposits and withdrawals use a two-step state machine.  A caller
persists the complete intended before/after state, changes the external store,
then commits only after its whole-file hash matches the prepared result.  An
interrupted operation remains recoverable on the next launch; an ambiguous
hash becomes an explicit conflict and the vault never silently discards data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar


SCHEMA_VERSION = 2
DEFAULT_COLLECTION_NAME = "Vault"
MAX_COLLECTION_NAME_LENGTH = 128
MAX_ITEM_JSON_BYTES = 4 * 1024 * 1024
MAX_TEXT_FIELD_LENGTH = 512
MAX_SEARCH_LENGTH = 256
MAX_PAGE_SIZE = 500
SQLITE_MAX_INTEGER = (1 << 63) - 1
PROCESS_LOCK_TIMEOUT_SECONDS = 30.0

_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_REQUEST_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()
_T = TypeVar("_T")


class VaultError(Exception):
    """Base class for Infinite Vault errors."""


class VaultValidationError(VaultError, ValueError):
    """Raised before unsafe or malformed input reaches SQLite."""


class VaultSchemaError(VaultError):
    """Raised when a database is corrupt, foreign, or too new to open."""


class VaultNotFoundError(VaultError, LookupError):
    """Raised when a requested collection, item, or withdrawal is absent."""


class VaultConflictError(VaultError):
    """Raised when a unique name or idempotency key conflicts."""


class VaultStateError(VaultConflictError):
    """Raised when an operation is invalid for the record's current state."""


@dataclass(frozen=True)
class CollectionRecord:
    id: int
    name: str
    available_count: int
    reserved_count: int
    created_at: str
    updated_at: str

    @property
    def item_count(self) -> int:
        return self.available_count + self.reserved_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "itemCount": self.item_count,
            "availableCount": self.available_count,
            "reservedCount": self.reserved_count,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class VaultItemRecord:
    id: str
    collection_id: int
    collection_name: str
    raw_item_json: str
    raw_sha256: str
    source_item_key: str | None
    label: str | None
    source: str | None
    deposit_key: str | None
    status: str
    reserved_token: str | None
    created_at: str
    updated_at: str

    def decoded_item(self) -> dict[str, Any]:
        """Return a fresh decoded copy while keeping the stored text untouched."""

        value = json.loads(self.raw_item_json)
        if not isinstance(value, dict):  # Protected by deposit validation.
            raise VaultSchemaError("stored item JSON is not an object")
        return value

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "collectionId": self.collection_id,
            "collectionName": self.collection_name,
            "rawItemJson": self.raw_item_json,
            "rawSha256": self.raw_sha256,
            "sourceItemKey": self.source_item_key,
            "label": self.label,
            "source": self.source,
            "depositKey": self.deposit_key,
            "status": self.status,
            "reservedToken": self.reserved_token,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True)
class WithdrawalRecord:
    token: str
    item_id: str
    source_collection_id: int | None
    source_collection_name: str
    raw_item_json: str
    raw_sha256: str
    source_item_key: str | None
    label: str | None
    source: str | None
    deposit_key: str | None
    status: str
    reserved_at: str
    finished_at: str | None

    def decoded_item(self) -> dict[str, Any]:
        value = json.loads(self.raw_item_json)
        if not isinstance(value, dict):
            raise VaultSchemaError("stored withdrawal JSON is not an object")
        return value

    def as_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "itemId": self.item_id,
            "sourceCollectionId": self.source_collection_id,
            "sourceCollectionName": self.source_collection_name,
            "rawItemJson": self.raw_item_json,
            "rawSha256": self.raw_sha256,
            "sourceItemKey": self.source_item_key,
            "label": self.label,
            "source": self.source,
            "depositKey": self.deposit_key,
            "status": self.status,
            "reservedAt": self.reserved_at,
            "finishedAt": self.finished_at,
        }


@dataclass(frozen=True)
class TransferRecord:
    request_id: str
    request_hash: str
    direction: str
    status: str
    item_id: str
    collection_id: int | None
    collection_name: str | None
    raw_item_json: str
    raw_sha256: str
    source_tab: str | None
    source_key: str | None
    target_tab: str | None
    target_key: str | None
    target_pos: tuple[int, int] | None
    stash_before_sha256: str | None
    stash_after_sha256: str | None
    observed_stash_sha256: str | None
    error: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    def decoded_item(self) -> dict[str, Any]:
        value = json.loads(self.raw_item_json)
        if not isinstance(value, dict):
            raise VaultSchemaError("stored transfer JSON is not an object")
        return value

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "requestHash": self.request_hash,
            "direction": self.direction,
            "status": self.status,
            "itemId": self.item_id,
            "collectionId": self.collection_id,
            "collectionName": self.collection_name,
            "rawItemJson": self.raw_item_json,
            "rawSha256": self.raw_sha256,
            "sourceTab": self.source_tab,
            "sourceKey": self.source_key,
            "targetTab": self.target_tab,
            "targetKey": self.target_key,
            "targetPos": list(self.target_pos) if self.target_pos is not None else None,
            "stashBeforeSha256": self.stash_before_sha256,
            "stashAfterSha256": self.stash_after_sha256,
            "observedStashSha256": self.observed_stash_sha256,
            "error": self.error,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "finishedAt": self.finished_at,
        }


_SCHEMA_SQL = """
BEGIN IMMEDIATE;
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE RESTRICT,
    raw_json TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    search_text TEXT NOT NULL,
    source_item_key TEXT,
    label TEXT,
    source TEXT,
    deposit_key TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('deposit_pending', 'available', 'reserved')),
    reserved_token TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'deposit_pending' AND reserved_token IS NULL) OR
        (status = 'available' AND reserved_token IS NULL) OR
        (status = 'reserved' AND reserved_token IS NOT NULL)
    )
);

CREATE TABLE transfers (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('deposit', 'withdrawal')),
    status TEXT NOT NULL CHECK (status IN ('prepared', 'committed', 'conflict', 'cancelled')),
    item_id TEXT NOT NULL,
    collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    collection_name TEXT,
    raw_json TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    deposit_key TEXT,
    source_tab TEXT,
    source_key TEXT,
    target_tab TEXT,
    target_key TEXT,
    target_pos_json TEXT,
    stash_before_sha256 TEXT,
    stash_after_sha256 TEXT,
    observed_stash_sha256 TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    item_id TEXT,
    collection_name TEXT,
    withdrawal_token TEXT,
    created_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX items_collection_status_idx
    ON items(collection_id, status, created_at, id);
CREATE INDEX items_search_idx ON items(search_text);
CREATE INDEX transfers_status_idx ON transfers(status, created_at, request_id);
CREATE INDEX transfers_item_idx ON transfers(item_id, created_at, request_id);
CREATE INDEX events_created_idx ON events(created_at, id);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2');
INSERT INTO collections(name, name_key, created_at, updated_at)
VALUES ('Vault', 'vault', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 2;
COMMIT;
"""

_REQUIRED_TABLES = frozenset({
    "schema_meta", "collections", "items", "transfers", "events"
})

_REQUIRED_COLUMNS = {
    "schema_meta": frozenset({"key", "value"}),
    "collections": frozenset({"id", "name", "name_key", "created_at", "updated_at"}),
    "items": frozenset({
        "id", "collection_id", "raw_json", "raw_sha256", "search_text",
        "source_item_key", "label", "source", "deposit_key", "status",
        "reserved_token", "created_at", "updated_at",
    }),
    "transfers": frozenset({
        "request_id", "request_hash", "direction", "status", "item_id",
        "collection_id", "collection_name", "raw_json", "raw_sha256", "deposit_key",
        "source_tab", "source_key", "target_tab", "target_key",
        "target_pos_json", "stash_before_sha256", "stash_after_sha256",
        "observed_stash_sha256", "error", "created_at", "updated_at",
        "finished_at",
    }),
    "events": frozenset({
        "id", "event_type", "item_id", "collection_name",
        "withdrawal_token", "created_at", "details_json",
    }),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_process_lock(path: Path):
    """Hold a one-byte OS lock; abandoned locks are released by the kernel."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    acquired = False
    deadline = time.monotonic() + PROCESS_LOCK_TIMEOUT_SECONDS
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise VaultConflictError(
                        "timed out waiting for another Infinite Vault process"
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        else:
            handle.close()


def _clean_collection_name(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise VaultValidationError("collection name must be text")
    name = unicodedata.normalize("NFC", value.strip())
    if not name:
        raise VaultValidationError("collection name cannot be empty")
    if len(name) > MAX_COLLECTION_NAME_LENGTH:
        raise VaultValidationError(
            f"collection name cannot exceed {MAX_COLLECTION_NAME_LENGTH} characters"
        )
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise VaultValidationError("collection name cannot contain control characters")
    return name, name.casefold()


def _clean_optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VaultValidationError(f"{label} must be text or null")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_TEXT_FIELD_LENGTH:
        raise VaultValidationError(
            f"{label} cannot exceed {MAX_TEXT_FIELD_LENGTH} characters"
        )
    try:
        cleaned.encode("utf-8")
    except UnicodeError as exc:
        raise VaultValidationError(f"{label} is not valid Unicode") from exc
    if any(char in "\x00\r\n" for char in cleaned):
        raise VaultValidationError(f"{label} contains forbidden control characters")
    return cleaned


def _clean_request_key(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _REQUEST_KEY_RE.fullmatch(value) is None:
        raise VaultValidationError(
            "deposit key must be 16-128 ASCII letters, digits, underscores, or hyphens"
        )
    return value


def _clean_required_request_id(value: Any) -> str:
    cleaned = _clean_request_key(value)
    if cleaned is None:
        raise VaultValidationError("request id is required")
    return cleaned


def _clean_sha256(value: Any, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise VaultValidationError(f"{label} must be a 64-character SHA-256 hex digest")
    return value.lower()


def _clean_target_pos(value: Any) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise VaultValidationError("target position must contain exactly two coordinates")
    output: list[int] = []
    for coordinate in value:
        try:
            numeric = float(coordinate)
            valid = (
                not isinstance(coordinate, bool)
                and isinstance(coordinate, (int, float))
                and math.isfinite(numeric)
                and numeric == int(numeric)
                and 0 <= int(coordinate) <= SQLITE_MAX_INTEGER
            )
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            raise VaultValidationError(
                "target position coordinates must be non-negative integers"
            )
        output.append(int(coordinate))
    return output[0], output[1]


def canonical_request_hash(request_body: Mapping[str, Any]) -> str:
    """Hash one validated API request deterministically for retry conflict checks."""

    if not isinstance(request_body, Mapping):
        raise VaultValidationError("request body must be an object")
    try:
        payload = json.dumps(
            dict(request_body),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise VaultValidationError(f"request body cannot be hashed safely: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def _clean_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise VaultValidationError(f"{label} must be a 32-character lowercase hex id")
    return value


def _clean_page(limit: Any, offset: Any) -> tuple[int, int]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise VaultValidationError(f"limit must be an integer from 1 to {MAX_PAGE_SIZE}")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= SQLITE_MAX_INTEGER
    ):
        raise VaultValidationError("offset must be a non-negative integer")
    return limit, offset


def _strict_object_pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VaultValidationError(f"item JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise VaultValidationError(f"item JSON contains non-finite number {value}")


def _validate_finite_tree(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise VaultValidationError("item JSON contains a non-finite number")
    if isinstance(value, dict):
        for nested in value.values():
            _validate_finite_tree(nested)
    elif isinstance(value, list):
        for nested in value:
            _validate_finite_tree(nested)


def validate_raw_item_json(raw_item_json: Any) -> dict[str, Any]:
    """Validate one native item record without rewriting its JSON text."""

    if not isinstance(raw_item_json, str):
        raise VaultValidationError("raw item JSON must be text")
    try:
        encoded_size = len(raw_item_json.encode("utf-8"))
    except UnicodeError as exc:
        raise VaultValidationError(f"raw item JSON is not valid Unicode: {exc}") from exc
    if encoded_size == 0:
        raise VaultValidationError("raw item JSON cannot be empty")
    if encoded_size > MAX_ITEM_JSON_BYTES:
        raise VaultValidationError(
            f"raw item JSON cannot exceed {MAX_ITEM_JSON_BYTES} UTF-8 bytes"
        )
    try:
        decoded = json.loads(
            raw_item_json,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
        _validate_finite_tree(decoded)
    except VaultValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise VaultValidationError(f"raw item JSON is malformed: {exc}") from exc
    if not isinstance(decoded, dict):
        raise VaultValidationError("raw item JSON root must be an object")
    data = decoded.get("data")
    if not isinstance(data, dict):
        raise VaultValidationError("raw item JSON must contain a data object")
    # Most native items have numeric ``b`` identity, but the editor already
    # supports opaque/special records without it.  Infinite Vault must preserve
    # those records rather than pretending to understand or reconstruct them.
    if "b" in data:
        base = data["b"]
        try:
            finite_base = (
                not isinstance(base, bool)
                and isinstance(base, (int, float))
                and math.isfinite(float(base))
            )
        except (OverflowError, ValueError):
            finite_base = False
        if not finite_base:
            raise VaultValidationError("item data.b must be a finite number when present")
    pos = decoded.get("pos")
    if pos is not None:
        if not isinstance(pos, list) or len(pos) < 2:
            raise VaultValidationError("item pos must contain at least two coordinates")
        for coordinate in pos[:2]:
            try:
                finite_coordinate = (
                    not isinstance(coordinate, bool)
                    and isinstance(coordinate, (int, float))
                    and math.isfinite(float(coordinate))
                )
            except (OverflowError, ValueError):
                finite_coordinate = False
            if not finite_coordinate:
                raise VaultValidationError("item pos coordinates must be finite numbers")
    return decoded


def _search_document(decoded: Any, extras: Iterable[str | None]) -> str:
    values: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                values.append(str(key))
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif value is not None:
            values.append(str(value))

    visit(decoded)
    values.extend(value for value in extras if value)
    return "\n".join(values).casefold()


class InfiniteVault:
    """One thread-safe handle to an on-disk Infinite Vault database."""

    def __init__(self, path: str | os.PathLike[str], *, backup_path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.backup_path = (
            Path(backup_path).expanduser().resolve()
            if backup_path is not None
            else Path(str(self.path) + ".bak")
        )
        if self.path == self.backup_path:
            raise VaultValidationError("backup path must differ from database path")
        self.lock_path = Path(str(self.path) + ".lock")
        self._lock = _lock_for(self.path)
        with self._lock:
            with _exclusive_process_lock(self.lock_path):
                self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _initialize(self) -> None:
        if self.path.exists() and self.path.is_dir():
            raise VaultValidationError("vault database path points to a directory")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        if not existed or self.path.stat().st_size == 0:
            if existed:
                self._backup_existing()
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA_SQL)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            return

        connection = self._connect()
        try:
            try:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                raise VaultSchemaError(f"vault database is not readable: {exc}") from exc
            if integrity != "ok":
                raise VaultSchemaError(f"vault database integrity check failed: {integrity}")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if version == 0 and not tables:
                connection.close()
                self._backup_existing()
                connection = self._connect()
                connection.executescript(_SCHEMA_SQL)
                return
            if version > SCHEMA_VERSION:
                raise VaultSchemaError(
                    f"vault schema {version} is newer than supported schema {SCHEMA_VERSION}"
                )
            if version < SCHEMA_VERSION:
                raise VaultSchemaError(
                    f"vault schema {version} has no supported migration to {SCHEMA_VERSION}"
                )
            missing = _REQUIRED_TABLES.difference(tables)
            if missing:
                raise VaultSchemaError(
                    "vault database is missing tables: " + ", ".join(sorted(missing))
                )
            for table, required_columns in _REQUIRED_COLUMNS.items():
                actual_columns = {
                    row[1]
                    for row in connection.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                }
                missing_columns = required_columns.difference(actual_columns)
                if missing_columns:
                    raise VaultSchemaError(
                        f"vault table {table} is missing columns: "
                        + ", ".join(sorted(missing_columns))
                    )
            meta = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if meta is None or meta[0] != str(SCHEMA_VERSION):
                raise VaultSchemaError("vault schema metadata does not match user_version")
        finally:
            connection.close()

    def _backup_existing(self) -> None:
        """Atomically replace the sidecar with a consistent pre-mutation copy."""

        if not self.path.exists():
            return
        self.backup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.backup_path.with_name(
            f".{self.backup_path.name}.{uuid.uuid4().hex}.tmp"
        )
        if self.path.stat().st_size == 0:
            temporary.write_bytes(b"")
        else:
            source: sqlite3.Connection | None = None
            destination: sqlite3.Connection | None = None
            backup_error: Exception | None = None
            try:
                source = sqlite3.connect(self.path, timeout=15.0)
                destination = sqlite3.connect(temporary)
                source.execute("PRAGMA busy_timeout = 15000")
                source.backup(destination)
                destination.commit()
            except Exception as exc:
                backup_error = exc
            finally:
                if destination is not None:
                    destination.close()
                if source is not None:
                    source.close()
            if backup_error is not None:
                temporary.unlink(missing_ok=True)
                raise backup_error
        try:
            os.replace(temporary, self.backup_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            connection = self._connect()
            try:
                return operation(connection)
            finally:
                connection.close()

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        with self._lock:
            with _exclusive_process_lock(self.lock_path):
                self._backup_existing()
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    result = operation(connection)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

    @property
    def schema_version(self) -> int:
        return self._read(
            lambda connection: int(connection.execute("PRAGMA user_version").fetchone()[0])
        )

    @staticmethod
    def _collection_from_row(row: sqlite3.Row) -> CollectionRecord:
        return CollectionRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            available_count=int(row["available_count"]),
            reserved_count=int(row["reserved_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> VaultItemRecord:
        return VaultItemRecord(
            id=str(row["id"]),
            collection_id=int(row["collection_id"]),
            collection_name=str(row["collection_name"]),
            raw_item_json=str(row["raw_json"]),
            raw_sha256=str(row["raw_sha256"]),
            source_item_key=row["source_item_key"],
            label=row["label"],
            source=row["source"],
            deposit_key=row["deposit_key"],
            status=str(row["status"]),
            reserved_token=row["reserved_token"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _withdrawal_from_row(row: sqlite3.Row) -> WithdrawalRecord:
        return WithdrawalRecord(
            token=str(row["request_id"]),
            item_id=str(row["item_id"]),
            source_collection_id=(
                int(row["collection_id"])
                if row["collection_id"] is not None
                else None
            ),
            source_collection_name=str(row["collection_name"] or ""),
            raw_item_json=str(row["raw_json"]),
            raw_sha256=str(row["raw_sha256"]),
            source_item_key=row["source_key"],
            label=None,
            source=row["source_tab"],
            deposit_key=row["deposit_key"],
            status=("reserved" if row["status"] == "prepared" else str(row["status"])),
            reserved_at=str(row["created_at"]),
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _transfer_from_row(row: sqlite3.Row) -> TransferRecord:
        raw_position = row["target_pos_json"]
        target_pos: tuple[int, int] | None = None
        if raw_position is not None:
            try:
                decoded = json.loads(raw_position)
                target_pos = _clean_target_pos(decoded)
            except (json.JSONDecodeError, VaultValidationError) as exc:
                raise VaultSchemaError("stored transfer target position is malformed") from exc
        return TransferRecord(
            request_id=str(row["request_id"]),
            request_hash=str(row["request_hash"]),
            direction=str(row["direction"]),
            status=str(row["status"]),
            item_id=str(row["item_id"]),
            collection_id=(int(row["collection_id"]) if row["collection_id"] is not None else None),
            collection_name=row["collection_name"],
            raw_item_json=str(row["raw_json"]),
            raw_sha256=str(row["raw_sha256"]),
            source_tab=row["source_tab"],
            source_key=row["source_key"],
            target_tab=row["target_tab"],
            target_key=row["target_key"],
            target_pos=target_pos,
            stash_before_sha256=row["stash_before_sha256"],
            stash_after_sha256=row["stash_after_sha256"],
            observed_stash_sha256=row["observed_stash_sha256"],
            error=row["error"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _resolve_collection(connection: sqlite3.Connection, reference: int | str) -> sqlite3.Row:
        if isinstance(reference, bool):
            raise VaultValidationError("collection reference must be an id or name")
        if isinstance(reference, int):
            if not 1 <= reference <= SQLITE_MAX_INTEGER:
                raise VaultValidationError("collection id must be positive")
            row = connection.execute(
                "SELECT id, name FROM collections WHERE id = ?", (reference,)
            ).fetchone()
        elif isinstance(reference, str):
            _, name_key = _clean_collection_name(reference)
            row = connection.execute(
                "SELECT id, name FROM collections WHERE name_key = ?", (name_key,)
            ).fetchone()
        else:
            raise VaultValidationError("collection reference must be an id or name")
        if row is None:
            raise VaultNotFoundError("collection was not found")
        return row

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        *,
        item_id: str | None = None,
        collection_name: str | None = None,
        withdrawal_token: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO events(
                   event_type, item_id, collection_name, withdrawal_token,
                   created_at, details_json
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_type,
                item_id,
                collection_name,
                withdrawal_token,
                _utc_now(),
                json.dumps(dict(details or {}), separators=(",", ":"), sort_keys=True),
            ),
        )

    def create_collection(self, name: str) -> CollectionRecord:
        clean_name, name_key = _clean_collection_name(name)

        def operation(connection: sqlite3.Connection) -> CollectionRecord:
            now = _utc_now()
            try:
                cursor = connection.execute(
                    "INSERT INTO collections(name, name_key, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (clean_name, name_key, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise VaultConflictError("a collection with that name already exists") from exc
            self._event(connection, "collection_created", collection_name=clean_name)
            row = connection.execute(
                """SELECT id, name, 0 AS available_count, 0 AS reserved_count,
                          created_at, updated_at
                   FROM collections WHERE id = ?""",
                (cursor.lastrowid,),
            ).fetchone()
            return self._collection_from_row(row)

        return self._write(operation)

    def list_collections(self) -> list[CollectionRecord]:
        def operation(connection: sqlite3.Connection) -> list[CollectionRecord]:
            rows = connection.execute(
                """SELECT c.id, c.name, c.created_at, c.updated_at,
                          COALESCE(SUM(CASE WHEN i.status = 'available' THEN 1 ELSE 0 END), 0)
                              AS available_count,
                          COALESCE(SUM(CASE WHEN i.status = 'reserved' THEN 1 ELSE 0 END), 0)
                              AS reserved_count
                   FROM collections AS c
                   LEFT JOIN items AS i ON i.collection_id = c.id
                   GROUP BY c.id
                   ORDER BY c.name_key, c.id"""
            ).fetchall()
            return [self._collection_from_row(row) for row in rows]

        return self._read(operation)

    def rename_collection(self, collection: int | str, new_name: str) -> CollectionRecord:
        clean_name, name_key = _clean_collection_name(new_name)

        def operation(connection: sqlite3.Connection) -> CollectionRecord:
            current = self._resolve_collection(connection, collection)
            now = _utc_now()
            try:
                connection.execute(
                    "UPDATE collections SET name = ?, name_key = ?, updated_at = ? WHERE id = ?",
                    (clean_name, name_key, now, current["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise VaultConflictError("a collection with that name already exists") from exc
            self._event(
                connection,
                "collection_renamed",
                collection_name=clean_name,
                details={"previousName": current["name"]},
            )
            row = connection.execute(
                """SELECT c.id, c.name, c.created_at, c.updated_at,
                          COALESCE(SUM(CASE WHEN i.status = 'available' THEN 1 ELSE 0 END), 0)
                              AS available_count,
                          COALESCE(SUM(CASE WHEN i.status = 'reserved' THEN 1 ELSE 0 END), 0)
                              AS reserved_count
                   FROM collections AS c
                   LEFT JOIN items AS i ON i.collection_id = c.id
                   WHERE c.id = ? GROUP BY c.id""",
                (current["id"],),
            ).fetchone()
            return self._collection_from_row(row)

        return self._write(operation)

    def delete_collection(self, collection: int | str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            current = self._resolve_collection(connection, collection)
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM items WHERE collection_id = ?",
                    (current["id"],),
                ).fetchone()[0]
            )
            if count:
                raise VaultStateError("only an empty collection can be deleted")
            self._event(
                connection, "collection_deleted", collection_name=current["name"]
            )
            connection.execute("DELETE FROM collections WHERE id = ?", (current["id"],))

        self._write(operation)

    def deposit(
        self,
        collection: int | str,
        raw_item_json: str,
        *,
        source_item_key: str | None = None,
        label: str | None = None,
        source: str | None = None,
        deposit_key: str | None = None,
    ) -> VaultItemRecord:
        decoded = validate_raw_item_json(raw_item_json)
        clean_source_key = _clean_optional_text(source_item_key, "source item key")
        clean_label = _clean_optional_text(label, "label")
        clean_source = _clean_optional_text(source, "source")
        clean_deposit_key = _clean_request_key(deposit_key)
        raw_sha256 = hashlib.sha256(raw_item_json.encode("utf-8")).hexdigest()
        search_text = _search_document(
            decoded, (clean_source_key, clean_label, clean_source)
        )

        def operation(connection: sqlite3.Connection) -> VaultItemRecord:
            target = self._resolve_collection(connection, collection)
            if clean_deposit_key is not None:
                prior = connection.execute(
                    """SELECT i.*, c.name AS collection_name
                       FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                       WHERE i.deposit_key = ?""",
                    (clean_deposit_key,),
                ).fetchone()
                if prior is not None:
                    if (
                        prior["raw_sha256"] == raw_sha256
                        and prior["raw_json"] == raw_item_json
                        and int(prior["collection_id"]) == int(target["id"])
                        and prior["source_item_key"] == clean_source_key
                        and prior["label"] == clean_label
                        and prior["source"] == clean_source
                    ):
                        return self._item_from_row(prior)
                    raise VaultConflictError("deposit key is already used by another item")
                historical = connection.execute(
                    """SELECT status FROM transfers
                       WHERE deposit_key = ? AND direction = 'withdrawal'
                       ORDER BY created_at DESC LIMIT 1""",
                    (clean_deposit_key,),
                ).fetchone()
                if historical is not None and historical["status"] == "committed":
                    raise VaultStateError("deposit key belongs to an item already withdrawn")

            item_id = uuid.uuid4().hex
            now = _utc_now()
            try:
                connection.execute(
                    """INSERT INTO items(
                           id, collection_id, raw_json, raw_sha256, search_text,
                           source_item_key, label, source, deposit_key, status,
                           reserved_token, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'available', NULL, ?, ?)""",
                    (
                        item_id,
                        target["id"],
                        raw_item_json,
                        raw_sha256,
                        search_text,
                        clean_source_key,
                        clean_label,
                        clean_source,
                        clean_deposit_key,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise VaultConflictError("item could not be deposited uniquely") from exc
            self._event(
                connection,
                "item_deposited",
                item_id=item_id,
                collection_name=target["name"],
                details={"rawSha256": raw_sha256, "source": clean_source},
            )
            row = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                   WHERE i.id = ?""",
                (item_id,),
            ).fetchone()
            return self._item_from_row(row)

        return self._write(operation)

    def get_item(self, item_id: str) -> VaultItemRecord:
        clean_item_id = _clean_id(item_id, "item id")

        def operation(connection: sqlite3.Connection) -> VaultItemRecord:
            row = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                   WHERE i.id = ?""",
                (clean_item_id,),
            ).fetchone()
            if row is None:
                raise VaultNotFoundError("item was not found")
            return self._item_from_row(row)

        return self._read(operation)

    @staticmethod
    def _status_clause(status: str) -> tuple[str, tuple[str, ...]]:
        if status == "available":
            return "i.status = ?", ("available",)
        if status == "reserved":
            return "i.status = ?", ("reserved",)
        if status == "deposit_pending":
            return "i.status = ?", ("deposit_pending",)
        if status == "all":
            return "i.status IN (?, ?, ?)", ("deposit_pending", "available", "reserved")
        raise VaultValidationError(
            "status must be 'deposit_pending', 'available', 'reserved', or 'all'"
        )

    def list_items(
        self,
        *,
        collection: int | str | None = None,
        search: str | None = None,
        status: str = "available",
        limit: int = 100,
        offset: int = 0,
    ) -> list[VaultItemRecord]:
        limit, offset = _clean_page(limit, offset)
        status_sql, status_args = self._status_clause(status)
        if search is not None:
            if not isinstance(search, str):
                raise VaultValidationError("search must be text or null")
            search = search.strip()
            if len(search) > MAX_SEARCH_LENGTH:
                raise VaultValidationError(
                    f"search cannot exceed {MAX_SEARCH_LENGTH} characters"
                )
            try:
                search.encode("utf-8")
            except UnicodeError as exc:
                raise VaultValidationError("search is not valid Unicode") from exc
            if not search:
                search = None

        def operation(connection: sqlite3.Connection) -> list[VaultItemRecord]:
            conditions = [status_sql]
            arguments: list[Any] = list(status_args)
            if collection is not None:
                target = self._resolve_collection(connection, collection)
                conditions.append("i.collection_id = ?")
                arguments.append(target["id"])
            if search is not None:
                escaped = (
                    search.casefold()
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                conditions.append("(i.search_text LIKE ? ESCAPE '\\' OR i.id = ?)")
                arguments.extend((f"%{escaped}%", search.casefold()))
            arguments.extend((limit, offset))
            rows = connection.execute(
                f"""SELECT i.*, c.name AS collection_name
                    FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                    WHERE {' AND '.join(conditions)}
                    ORDER BY i.created_at DESC, i.id
                    LIMIT ? OFFSET ?""",
                tuple(arguments),
            ).fetchall()
            return [self._item_from_row(row) for row in rows]

        return self._read(operation)

    def search_items(
        self,
        query: str,
        *,
        collection: int | str | None = None,
        status: str = "available",
        limit: int = 100,
        offset: int = 0,
    ) -> list[VaultItemRecord]:
        return self.list_items(
            collection=collection,
            search=query,
            status=status,
            limit=limit,
            offset=offset,
        )

    def count_items(
        self,
        *,
        collection: int | str | None = None,
        status: str = "available",
    ) -> int:
        status_sql, status_args = self._status_clause(status)

        def operation(connection: sqlite3.Connection) -> int:
            conditions = [status_sql]
            arguments: list[Any] = list(status_args)
            if collection is not None:
                target = self._resolve_collection(connection, collection)
                conditions.append("i.collection_id = ?")
                arguments.append(target["id"])
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM items AS i WHERE {' AND '.join(conditions)}",
                    tuple(arguments),
                ).fetchone()[0]
            )

        return self._read(operation)

    def move_item(self, item_id: str, destination: int | str) -> VaultItemRecord:
        clean_item_id = _clean_id(item_id, "item id")

        def operation(connection: sqlite3.Connection) -> VaultItemRecord:
            item = connection.execute(
                "SELECT * FROM items WHERE id = ?", (clean_item_id,)
            ).fetchone()
            if item is None:
                raise VaultNotFoundError("item was not found")
            if item["status"] != "available":
                raise VaultStateError("a reserved item cannot be moved")
            target = self._resolve_collection(connection, destination)
            previous = connection.execute(
                "SELECT name FROM collections WHERE id = ?", (item["collection_id"],)
            ).fetchone()[0]
            now = _utc_now()
            connection.execute(
                "UPDATE items SET collection_id = ?, updated_at = ? WHERE id = ?",
                (target["id"], now, clean_item_id),
            )
            self._event(
                connection,
                "item_moved",
                item_id=clean_item_id,
                collection_name=target["name"],
                details={"previousCollection": previous},
            )
            row = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                   WHERE i.id = ?""",
                (clean_item_id,),
            ).fetchone()
            return self._item_from_row(row)

        return self._write(operation)

    def prepare_deposit(
        self,
        collection: int | str,
        raw_item_json: str,
        *,
        request_id: str,
        request_hash: str,
        source_tab: str,
        source_key: str,
        stash_before_sha256: str,
        stash_after_sha256: str,
        label: str | None = None,
        source: str | None = None,
    ) -> TransferRecord:
        """Persist a hidden vault copy before the caller removes the save copy."""

        decoded = validate_raw_item_json(raw_item_json)
        clean_request_id = _clean_required_request_id(request_id)
        clean_request_hash = _clean_sha256(request_hash, "request hash")
        clean_source_tab = _clean_optional_text(source_tab, "source tab")
        clean_source_key = _clean_optional_text(source_key, "source key")
        if clean_source_tab is None or clean_source_key is None:
            raise VaultValidationError("source tab and source key are required")
        before_hash = _clean_sha256(stash_before_sha256, "stash before hash")
        after_hash = _clean_sha256(stash_after_sha256, "stash after hash")
        if before_hash == after_hash:
            raise VaultValidationError("deposit before and after stash hashes must differ")
        clean_label = _clean_optional_text(label, "label")
        clean_source = _clean_optional_text(source, "source") or clean_source_tab
        raw_sha256 = hashlib.sha256(raw_item_json.encode("utf-8")).hexdigest()
        search_text = _search_document(
            decoded, (clean_source_key, clean_label, clean_source)
        )

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            target = self._resolve_collection(connection, collection)
            expected_request_hash = canonical_request_hash({
                "direction": "deposit",
                "source": {"type": "stash", "tab": clean_source_tab},
                "key": clean_source_key,
                "collectionId": int(target["id"]),
            })
            prior = connection.execute(
                "SELECT * FROM transfers WHERE request_id = ?", (clean_request_id,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["direction"] == "deposit"
                    and prior["request_hash"] == clean_request_hash
                    and prior["raw_json"] == raw_item_json
                    and int(prior["collection_id"]) == int(target["id"])
                    and prior["source_tab"] == clean_source_tab
                    and prior["source_key"] == clean_source_key
                    and prior["stash_before_sha256"] == before_hash
                    and prior["stash_after_sha256"] == after_hash
                ):
                    return self._transfer_from_row(prior)
                raise VaultConflictError("request id was reused with different deposit data")
            if clean_request_hash != expected_request_hash:
                raise VaultValidationError("request hash does not match deposit intent")
            active_source = connection.execute(
                """SELECT request_id FROM transfers
                   WHERE direction='deposit' AND status IN ('prepared', 'conflict')
                     AND source_tab=? AND source_key=? AND stash_before_sha256=?
                     AND raw_sha256=? LIMIT 1""",
                (clean_source_tab, clean_source_key, before_hash, raw_sha256),
            ).fetchone()
            if active_source is not None:
                raise VaultConflictError(
                    "that exact stash source already has an active deposit transfer"
                )
            item_id = uuid.uuid4().hex
            now = _utc_now()
            connection.execute(
                """INSERT INTO items(
                       id, collection_id, raw_json, raw_sha256, search_text,
                       source_item_key, label, source, deposit_key, status,
                       reserved_token, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'deposit_pending', NULL, ?, ?)""",
                (
                    item_id, target["id"], raw_item_json, raw_sha256, search_text,
                    clean_source_key, clean_label, clean_source, clean_request_id,
                    now, now,
                ),
            )
            connection.execute(
                """INSERT INTO transfers(
                       request_id, request_hash, direction, status, item_id,
                       collection_id, collection_name, raw_json, raw_sha256,
                       deposit_key, source_tab, source_key, target_tab, target_key,
                       target_pos_json, stash_before_sha256, stash_after_sha256,
                       observed_stash_sha256, error, created_at, updated_at, finished_at
                   ) VALUES (?, ?, 'deposit', 'prepared', ?, ?, ?, ?, ?, ?, ?, ?,
                             NULL, NULL, NULL, ?, ?, NULL, NULL, ?, ?, NULL)""",
                (
                    clean_request_id, clean_request_hash, item_id, target["id"],
                    target["name"], raw_item_json, raw_sha256, clean_request_id,
                    clean_source_tab, clean_source_key, before_hash, after_hash, now, now,
                ),
            )
            self._event(
                connection, "deposit_prepared", item_id=item_id,
                collection_name=target["name"], withdrawal_token=clean_request_id,
                details={"stashBeforeSha256": before_hash, "stashAfterSha256": after_hash},
            )
            return self._transfer_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id = ?", (clean_request_id,)
                ).fetchone()
            )

        return self._write(operation)

    def commit_deposit(self, request_id: str, observed_stash_sha256: str) -> TransferRecord:
        clean_request_id = _clean_required_request_id(request_id)
        observed = _clean_sha256(observed_stash_sha256, "observed stash hash")

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            transfer = connection.execute(
                "SELECT * FROM transfers WHERE request_id = ?", (clean_request_id,)
            ).fetchone()
            if transfer is None or transfer["direction"] != "deposit":
                raise VaultNotFoundError("deposit transfer was not found")
            if transfer["status"] == "committed":
                return self._transfer_from_row(transfer)
            if transfer["status"] == "cancelled":
                raise VaultStateError("a cancelled deposit cannot be committed")
            now = _utc_now()
            if observed != transfer["stash_after_sha256"]:
                connection.execute(
                    """UPDATE transfers SET status='conflict', observed_stash_sha256=?,
                              error=?, updated_at=? WHERE request_id=?""",
                    (observed, "observed stash hash does not match prepared after hash", now, clean_request_id),
                )
                return self._transfer_from_row(
                    connection.execute(
                        "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                    ).fetchone()
                )
            item = connection.execute(
                "SELECT * FROM items WHERE id = ?", (transfer["item_id"],)
            ).fetchone()
            if item is None or item["status"] != "deposit_pending" or item["raw_sha256"] != transfer["raw_sha256"]:
                raise VaultSchemaError("pending item and deposit transfer disagree")
            connection.execute(
                "UPDATE items SET status='available', updated_at=? WHERE id=?",
                (now, item["id"]),
            )
            connection.execute(
                """UPDATE transfers SET status='committed', observed_stash_sha256=?,
                          error=NULL, updated_at=?, finished_at=? WHERE request_id=?""",
                (observed, now, now, clean_request_id),
            )
            self._event(
                connection, "deposit_committed", item_id=item["id"],
                collection_name=transfer["collection_name"],
                withdrawal_token=clean_request_id,
            )
            return self._transfer_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                ).fetchone()
            )

        return self._write(operation)

    def cancel_deposit(self, request_id: str, observed_stash_sha256: str) -> TransferRecord:
        clean_request_id = _clean_required_request_id(request_id)
        observed = _clean_sha256(observed_stash_sha256, "observed stash hash")

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            transfer = connection.execute(
                "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
            ).fetchone()
            if transfer is None or transfer["direction"] != "deposit":
                raise VaultNotFoundError("deposit transfer was not found")
            if transfer["status"] == "cancelled":
                return self._transfer_from_row(transfer)
            if transfer["status"] == "committed":
                raise VaultStateError("a committed deposit cannot be cancelled")
            now = _utc_now()
            if observed != transfer["stash_before_sha256"]:
                connection.execute(
                    """UPDATE transfers SET status='conflict', observed_stash_sha256=?,
                              error=?, updated_at=? WHERE request_id=?""",
                    (observed, "observed stash hash does not match prepared before hash", now, clean_request_id),
                )
                return self._transfer_from_row(
                    connection.execute(
                        "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                    ).fetchone()
                )
            item = connection.execute(
                "SELECT * FROM items WHERE id=?", (transfer["item_id"],)
            ).fetchone()
            if item is None or item["status"] != "deposit_pending":
                raise VaultSchemaError("pending item and deposit transfer disagree")
            connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
            connection.execute(
                """UPDATE transfers SET status='cancelled', observed_stash_sha256=?,
                          error=NULL, updated_at=?, finished_at=? WHERE request_id=?""",
                (observed, now, now, clean_request_id),
            )
            self._event(
                connection, "deposit_cancelled", item_id=transfer["item_id"],
                collection_name=transfer["collection_name"],
                withdrawal_token=clean_request_id,
            )
            return self._transfer_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                ).fetchone()
            )

        return self._write(operation)

    def prepare_withdrawal(
        self,
        item_id: str,
        *,
        request_id: str,
        request_hash: str,
        target_tab: str,
        target_key: str,
        target_pos: tuple[int, int] | list[int],
        stash_before_sha256: str,
        stash_after_sha256: str,
    ) -> TransferRecord:
        """Reserve one visible item before the caller writes it to a save."""

        clean_item_id = _clean_id(item_id, "item id")
        clean_request_id = _clean_required_request_id(request_id)
        clean_request_hash = _clean_sha256(request_hash, "request hash")
        clean_target_tab = _clean_optional_text(target_tab, "target tab")
        clean_target_key = _clean_optional_text(target_key, "target key")
        if clean_target_tab is None or clean_target_key is None:
            raise VaultValidationError("target tab and target key are required")
        clean_target_pos = _clean_target_pos(target_pos)
        before_hash = _clean_sha256(stash_before_sha256, "stash before hash")
        after_hash = _clean_sha256(stash_after_sha256, "stash after hash")
        if before_hash == after_hash:
            raise VaultValidationError("withdrawal before and after stash hashes must differ")

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            expected_request_hash = canonical_request_hash({
                "direction": "withdrawal",
                "itemId": clean_item_id,
                "target": {"type": "stash", "tab": clean_target_tab},
            })
            prior = connection.execute(
                "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["direction"] == "withdrawal"
                    and prior["request_hash"] == clean_request_hash
                    and prior["item_id"] == clean_item_id
                    and prior["target_tab"] == clean_target_tab
                    and prior["target_key"] == clean_target_key
                    and prior["target_pos_json"] == json.dumps(
                        list(clean_target_pos), separators=(",", ":")
                    )
                    and prior["stash_before_sha256"] == before_hash
                    and prior["stash_after_sha256"] == after_hash
                ):
                    return self._transfer_from_row(prior)
                raise VaultConflictError("request id was reused with different withdrawal data")
            if clean_request_hash != expected_request_hash:
                raise VaultValidationError("request hash does not match withdrawal intent")
            item = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                   WHERE i.id=?""",
                (clean_item_id,),
            ).fetchone()
            if item is None:
                raise VaultNotFoundError("item was not found")
            if item["status"] != "available":
                raise VaultStateError("item is already pending or reserved")
            now = _utc_now()
            connection.execute(
                """INSERT INTO transfers(
                       request_id, request_hash, direction, status, item_id,
                       collection_id, collection_name, raw_json, raw_sha256,
                       deposit_key, source_tab, source_key, target_tab, target_key,
                       target_pos_json, stash_before_sha256, stash_after_sha256,
                       observed_stash_sha256, error, created_at, updated_at, finished_at
                   ) VALUES (?, ?, 'withdrawal', 'prepared', ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL)""",
                (
                    clean_request_id, clean_request_hash, clean_item_id,
                    item["collection_id"], item["collection_name"], item["raw_json"],
                    item["raw_sha256"], item["deposit_key"], item["source"],
                    item["source_item_key"], clean_target_tab, clean_target_key,
                    json.dumps(list(clean_target_pos), separators=(",", ":")),
                    before_hash, after_hash, now, now,
                ),
            )
            connection.execute(
                "UPDATE items SET status='reserved', reserved_token=?, updated_at=? WHERE id=?",
                (clean_request_id, now, clean_item_id),
            )
            self._event(
                connection, "withdrawal_prepared", item_id=clean_item_id,
                collection_name=item["collection_name"],
                withdrawal_token=clean_request_id,
                details={"targetTab": clean_target_tab, "targetKey": clean_target_key},
            )
            return self._transfer_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                ).fetchone()
            )

        return self._write(operation)

    def reserve_withdrawal(self, item_id: str) -> WithdrawalRecord:
        """Reserve without external save metadata (useful for local/manual callers)."""

        clean_item_id = _clean_id(item_id, "item id")
        token = uuid.uuid4().hex
        request_hash = canonical_request_hash(
            {"kind": "local_withdrawal", "itemId": clean_item_id, "token": token}
        )

        def operation(connection: sqlite3.Connection) -> WithdrawalRecord:
            item = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                   WHERE i.id=?""",
                (clean_item_id,),
            ).fetchone()
            if item is None:
                raise VaultNotFoundError("item was not found")
            if item["status"] != "available":
                raise VaultStateError("item is already pending or reserved")
            now = _utc_now()
            connection.execute(
                """INSERT INTO transfers(
                       request_id, request_hash, direction, status, item_id,
                       collection_id, collection_name, raw_json, raw_sha256,
                       deposit_key, source_tab, source_key, target_tab, target_key,
                       target_pos_json, stash_before_sha256, stash_after_sha256,
                       observed_stash_sha256, error, created_at, updated_at, finished_at
                   ) VALUES (?, ?, 'withdrawal', 'prepared', ?, ?, ?, ?, ?, ?, ?, ?,
                             NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL)""",
                (
                    token, request_hash, clean_item_id, item["collection_id"],
                    item["collection_name"], item["raw_json"], item["raw_sha256"],
                    item["deposit_key"], item["source"], item["source_item_key"], now, now,
                ),
            )
            connection.execute(
                "UPDATE items SET status='reserved', reserved_token=?, updated_at=? WHERE id=?",
                (token, now, clean_item_id),
            )
            self._event(
                connection, "withdrawal_reserved", item_id=clean_item_id,
                collection_name=item["collection_name"], withdrawal_token=token,
            )
            return self._withdrawal_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (token,)
                ).fetchone()
            )

        return self._write(operation)

    def get_transfer(self, request_id: str) -> TransferRecord:
        clean_request_id = _clean_required_request_id(request_id)

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            row = connection.execute(
                "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
            ).fetchone()
            if row is None:
                raise VaultNotFoundError("transfer was not found")
            return self._transfer_from_row(row)

        return self._read(operation)

    def list_pending_transfers(self) -> list[TransferRecord]:
        return self._read(
            lambda connection: [
                self._transfer_from_row(row)
                for row in connection.execute(
                    """SELECT * FROM transfers WHERE status IN ('prepared', 'conflict')
                       ORDER BY created_at, request_id"""
                ).fetchall()
            ]
        )

    def get_withdrawal(self, token: str) -> WithdrawalRecord:
        clean_token = _clean_required_request_id(token)

        def operation(connection: sqlite3.Connection) -> WithdrawalRecord:
            row = connection.execute(
                "SELECT * FROM transfers WHERE request_id=? AND direction='withdrawal'",
                (clean_token,),
            ).fetchone()
            if row is None:
                raise VaultNotFoundError("withdrawal was not found")
            return self._withdrawal_from_row(row)

        return self._read(operation)

    def list_pending_withdrawals(self) -> list[WithdrawalRecord]:
        return self._read(
            lambda connection: [
                self._withdrawal_from_row(row)
                for row in connection.execute(
                    """SELECT * FROM transfers
                       WHERE direction='withdrawal' AND status IN ('prepared', 'conflict')
                       ORDER BY created_at, request_id"""
                ).fetchall()
            ]
        )

    def commit_withdrawal(
        self, token: str, observed_stash_sha256: str | None = None
    ) -> WithdrawalRecord:
        clean_token = _clean_required_request_id(token)
        observed = _clean_sha256(
            observed_stash_sha256, "observed stash hash", optional=True
        )

        def operation(connection: sqlite3.Connection) -> WithdrawalRecord:
            transfer = connection.execute(
                "SELECT * FROM transfers WHERE request_id=? AND direction='withdrawal'",
                (clean_token,),
            ).fetchone()
            if transfer is None:
                raise VaultNotFoundError("withdrawal was not found")
            if transfer["status"] == "committed":
                return self._withdrawal_from_row(transfer)
            if transfer["status"] == "cancelled":
                raise VaultStateError("a cancelled withdrawal cannot be committed")
            now = _utc_now()
            if transfer["stash_after_sha256"] is not None:
                if observed is None:
                    raise VaultValidationError("observed stash hash is required")
                if observed != transfer["stash_after_sha256"]:
                    connection.execute(
                        """UPDATE transfers SET status='conflict', observed_stash_sha256=?,
                                  error=?, updated_at=? WHERE request_id=?""",
                        (observed, "observed stash hash does not match prepared after hash", now, clean_token),
                    )
                    return self._withdrawal_from_row(
                        connection.execute(
                            "SELECT * FROM transfers WHERE request_id=?", (clean_token,)
                        ).fetchone()
                    )
            item = connection.execute(
                "SELECT * FROM items WHERE id=?", (transfer["item_id"],)
            ).fetchone()
            if (
                item is None or item["status"] != "reserved"
                or item["reserved_token"] != clean_token
                or item["raw_sha256"] != transfer["raw_sha256"]
            ):
                raise VaultSchemaError("reserved item and withdrawal transfer disagree")
            connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
            connection.execute(
                """UPDATE transfers SET status='committed', observed_stash_sha256=?,
                          error=NULL, updated_at=?, finished_at=? WHERE request_id=?""",
                (observed, now, now, clean_token),
            )
            self._event(
                connection, "withdrawal_committed", item_id=transfer["item_id"],
                collection_name=transfer["collection_name"], withdrawal_token=clean_token,
            )
            return self._withdrawal_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_token,)
                ).fetchone()
            )

        return self._write(operation)

    def cancel_withdrawal(
        self, token: str, observed_stash_sha256: str | None = None
    ) -> WithdrawalRecord:
        clean_token = _clean_required_request_id(token)
        observed = _clean_sha256(
            observed_stash_sha256, "observed stash hash", optional=True
        )

        def operation(connection: sqlite3.Connection) -> WithdrawalRecord:
            transfer = connection.execute(
                "SELECT * FROM transfers WHERE request_id=? AND direction='withdrawal'",
                (clean_token,),
            ).fetchone()
            if transfer is None:
                raise VaultNotFoundError("withdrawal was not found")
            if transfer["status"] == "cancelled":
                return self._withdrawal_from_row(transfer)
            if transfer["status"] == "committed":
                raise VaultStateError("a committed withdrawal cannot be cancelled")
            now = _utc_now()
            if transfer["stash_before_sha256"] is not None:
                if observed is None:
                    raise VaultValidationError("observed stash hash is required")
                if observed != transfer["stash_before_sha256"]:
                    connection.execute(
                        """UPDATE transfers SET status='conflict', observed_stash_sha256=?,
                                  error=?, updated_at=? WHERE request_id=?""",
                        (observed, "observed stash hash does not match prepared before hash", now, clean_token),
                    )
                    return self._withdrawal_from_row(
                        connection.execute(
                            "SELECT * FROM transfers WHERE request_id=?", (clean_token,)
                        ).fetchone()
                    )
            item = connection.execute(
                "SELECT * FROM items WHERE id=?", (transfer["item_id"],)
            ).fetchone()
            if (
                item is None or item["status"] != "reserved"
                or item["reserved_token"] != clean_token
                or item["raw_sha256"] != transfer["raw_sha256"]
            ):
                raise VaultSchemaError("reserved item and withdrawal transfer disagree")
            connection.execute(
                "UPDATE items SET status='available', reserved_token=NULL, updated_at=? WHERE id=?",
                (now, item["id"]),
            )
            connection.execute(
                """UPDATE transfers SET status='cancelled', observed_stash_sha256=?,
                          error=NULL, updated_at=?, finished_at=? WHERE request_id=?""",
                (observed, now, now, clean_token),
            )
            self._event(
                connection, "withdrawal_cancelled", item_id=transfer["item_id"],
                collection_name=transfer["collection_name"], withdrawal_token=clean_token,
            )
            return self._withdrawal_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_token,)
                ).fetchone()
            )

        return self._write(operation)

    def mark_transfer_conflict(
        self,
        request_id: str,
        error: str,
        *,
        observed_stash_sha256: str | None = None,
    ) -> TransferRecord:
        clean_request_id = _clean_required_request_id(request_id)
        clean_error = _clean_optional_text(error, "transfer error")
        if clean_error is None:
            raise VaultValidationError("transfer error is required")
        observed = _clean_sha256(
            observed_stash_sha256, "observed stash hash", optional=True
        )

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            transfer = connection.execute(
                "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
            ).fetchone()
            if transfer is None:
                raise VaultNotFoundError("transfer was not found")
            if transfer["status"] in {"committed", "cancelled"}:
                raise VaultStateError("a finished transfer cannot be marked conflicted")
            now = _utc_now()
            connection.execute(
                """UPDATE transfers SET status='conflict', error=?,
                          observed_stash_sha256=?, updated_at=? WHERE request_id=?""",
                (clean_error, observed, now, clean_request_id),
            )
            self._event(
                connection, "transfer_conflict", item_id=transfer["item_id"],
                collection_name=transfer["collection_name"],
                withdrawal_token=clean_request_id, details={"error": clean_error},
            )
            return self._transfer_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                ).fetchone()
            )

        return self._write(operation)

    def resolve_transfer_by_evidence(
        self,
        request_id: str,
        outcome: str,
        observed_stash_sha256: str,
        evidence: str,
    ) -> TransferRecord:
        """Finish an ambiguous transfer after the caller verifies item content.

        Whole-file hashes are the normal proof. This path exists for the case
        where a later, unrelated stash edit changed the whole-file hash while
        the journal's exact source/target entry still proves which copy exists.
        The integration layer must inspect that entry under the stash OS lock
        before calling this method; the evidence text is retained in the audit
        event.
        """

        clean_request_id = _clean_required_request_id(request_id)
        if outcome not in {"committed", "cancelled"}:
            raise VaultValidationError("evidence outcome must be committed or cancelled")
        observed = _clean_sha256(observed_stash_sha256, "observed stash hash")
        clean_evidence = _clean_optional_text(evidence, "transfer evidence")
        if clean_evidence is None:
            raise VaultValidationError("transfer evidence is required")

        def operation(connection: sqlite3.Connection) -> TransferRecord:
            transfer = connection.execute(
                "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
            ).fetchone()
            if transfer is None:
                raise VaultNotFoundError("transfer was not found")
            if transfer["status"] in {"committed", "cancelled"}:
                if transfer["status"] != outcome:
                    raise VaultStateError("finished transfer has the opposite outcome")
                return self._transfer_from_row(transfer)
            item = connection.execute(
                "SELECT * FROM items WHERE id=?", (transfer["item_id"],)
            ).fetchone()
            expected_status = (
                "deposit_pending" if transfer["direction"] == "deposit" else "reserved"
            )
            if (
                item is None or item["status"] != expected_status
                or item["raw_sha256"] != transfer["raw_sha256"]
            ):
                raise VaultSchemaError("pending item and transfer evidence disagree")
            if transfer["direction"] == "withdrawal" and item["reserved_token"] != clean_request_id:
                raise VaultSchemaError("reserved item token and transfer evidence disagree")
            now = _utc_now()
            if transfer["direction"] == "deposit":
                if outcome == "committed":
                    connection.execute(
                        "UPDATE items SET status='available', updated_at=? WHERE id=?",
                        (now, item["id"]),
                    )
                else:
                    connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
            elif outcome == "committed":
                connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
            else:
                connection.execute(
                    "UPDATE items SET status='available', reserved_token=NULL, updated_at=? WHERE id=?",
                    (now, item["id"]),
                )
            connection.execute(
                """UPDATE transfers SET status=?, observed_stash_sha256=?, error=NULL,
                          updated_at=?, finished_at=? WHERE request_id=?""",
                (outcome, observed, now, now, clean_request_id),
            )
            self._event(
                connection,
                "transfer_evidence_" + outcome,
                item_id=transfer["item_id"],
                collection_name=transfer["collection_name"],
                withdrawal_token=clean_request_id,
                details={"evidence": clean_evidence, "observedStashSha256": observed},
            )
            return self._transfer_from_row(
                connection.execute(
                    "SELECT * FROM transfers WHERE request_id=?", (clean_request_id,)
                ).fetchone()
            )

        return self._write(operation)

    def reconcile_transfer(self, request_id: str, current_stash_sha256: str) -> TransferRecord:
        """Resolve a prepared operation from the current whole-stash hash."""

        transfer = self.get_transfer(request_id)
        current = _clean_sha256(current_stash_sha256, "current stash hash")
        if transfer.status in {"committed", "cancelled"}:
            return transfer
        if current == transfer.stash_after_sha256:
            if transfer.direction == "deposit":
                return self.commit_deposit(request_id, current)
            self.commit_withdrawal(request_id, current)
            return self.get_transfer(request_id)
        if current == transfer.stash_before_sha256:
            if transfer.direction == "deposit":
                return self.cancel_deposit(request_id, current)
            self.cancel_withdrawal(request_id, current)
            return self.get_transfer(request_id)
        return self.mark_transfer_conflict(
            request_id,
            "current stash hash matches neither prepared state",
            observed_stash_sha256=current,
        )


__all__ = [
    "SCHEMA_VERSION",
    "DEFAULT_COLLECTION_NAME",
    "CollectionRecord",
    "VaultItemRecord",
    "WithdrawalRecord",
    "TransferRecord",
    "VaultError",
    "VaultValidationError",
    "VaultSchemaError",
    "VaultNotFoundError",
    "VaultConflictError",
    "VaultStateError",
    "InfiniteVault",
    "validate_raw_item_json",
    "canonical_request_hash",
]
