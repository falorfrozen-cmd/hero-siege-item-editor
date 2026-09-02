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


SCHEMA_VERSION = 6
DEFAULT_COLLECTION_NAME = "Vault"
MAX_COLLECTION_NAME_LENGTH = 128
MAX_STASH_NAME_LENGTH = 128
MAX_CUSTOM_NAME_LENGTH = 128
MAX_ITEM_JSON_BYTES = 4 * 1024 * 1024
MAX_TEXT_FIELD_LENGTH = 512
MAX_SEARCH_LENGTH = 256
MAX_PAGE_SIZE = 500
VAULT_GRID_COLUMNS = 17
VAULT_GRID_ROWS = 18
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
    stash_count: int = 0

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
            "stashCount": self.stash_count,
        }


@dataclass(frozen=True)
class StashPageRecord:
    id: int
    collection_id: int
    page_index: int
    name: str
    item_count: int
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "collectionId": self.collection_id,
            "pageIndex": self.page_index,
            "name": self.name,
            "itemCount": self.item_count,
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
    custom_name: str | None
    source: str | None
    deposit_key: str | None
    status: str
    reserved_token: str | None
    page_index: int | None
    layout_x: int | None
    layout_y: int | None
    created_at: str
    updated_at: str

    def decoded_item(self) -> dict[str, Any]:
        """Return a validated copy without trusting mutable database metadata."""

        return validate_item_record_integrity(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "collectionId": self.collection_id,
            "collectionName": self.collection_name,
            "rawItemJson": self.raw_item_json,
            "rawSha256": self.raw_sha256,
            "sourceItemKey": self.source_item_key,
            "label": self.label,
            "customName": self.custom_name,
            "source": self.source,
            "depositKey": self.deposit_key,
            "status": self.status,
            "reservedToken": self.reserved_token,
            "pageIndex": self.page_index,
            "layoutX": self.layout_x,
            "layoutY": self.layout_y,
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


@dataclass(frozen=True)
class TransferBatchRecord:
    request_id: str
    request_hash: str
    direction: str
    status: str
    item_count: int
    collection_id: int | None
    collection_name: str | None
    stash_before_sha256: str
    stash_after_sha256: str
    observed_stash_sha256: str | None
    error: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "requestHash": self.request_hash,
            "direction": self.direction,
            "status": self.status,
            "itemCount": self.item_count,
            "collectionId": self.collection_id,
            "collectionName": self.collection_name,
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

CREATE TABLE stash_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL CHECK (page_index >= 0),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(collection_id, page_index),
    UNIQUE(collection_id, name_key)
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE RESTRICT,
    raw_json TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    search_text TEXT NOT NULL,
    source_item_key TEXT,
    label TEXT,
    custom_name TEXT,
    source TEXT,
    deposit_key TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('deposit_pending', 'available', 'reserved')),
    reserved_token TEXT UNIQUE,
    page_index INTEGER CHECK (page_index IS NULL OR page_index >= 0),
    layout_x INTEGER CHECK (layout_x IS NULL OR (layout_x >= 0 AND layout_x < 17)),
    layout_y INTEGER CHECK (layout_y IS NULL OR (layout_y >= 0 AND layout_y < 18)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'deposit_pending' AND reserved_token IS NULL) OR
        (status = 'available' AND reserved_token IS NULL) OR
        (status = 'reserved' AND reserved_token IS NOT NULL)
    ),
    CHECK (
        (page_index IS NULL AND layout_x IS NULL AND layout_y IS NULL) OR
        (page_index IS NOT NULL AND layout_x IS NOT NULL AND layout_y IS NOT NULL)
    )
);

CREATE TABLE transfer_batches (
    request_id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('deposit', 'withdrawal')),
    status TEXT NOT NULL CHECK (status IN ('prepared', 'committed', 'conflict', 'cancelled')),
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
    collection_name TEXT,
    stash_before_sha256 TEXT NOT NULL,
    stash_after_sha256 TEXT NOT NULL,
    observed_stash_sha256 TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
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
    finished_at TEXT,
    batch_id TEXT REFERENCES transfer_batches(request_id) ON DELETE RESTRICT,
    batch_ordinal INTEGER,
    UNIQUE(batch_id, batch_ordinal),
    CHECK (
        (batch_id IS NULL AND batch_ordinal IS NULL) OR
        (batch_id IS NOT NULL AND batch_ordinal IS NOT NULL AND batch_ordinal >= 0)
    )
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
CREATE INDEX items_collection_layout_idx
    ON items(collection_id, page_index, layout_y, layout_x, id);
CREATE INDEX stash_pages_collection_idx
    ON stash_pages(collection_id, page_index);
CREATE INDEX transfers_status_idx ON transfers(status, created_at, request_id);
CREATE INDEX transfers_item_idx ON transfers(item_id, created_at, request_id);
CREATE INDEX transfer_batches_status_idx
    ON transfer_batches(status, created_at, request_id);
CREATE INDEX transfers_batch_idx ON transfers(batch_id, batch_ordinal);
CREATE INDEX events_created_idx ON events(created_at, id);

INSERT INTO schema_meta(key, value) VALUES ('schema_version', '6');
INSERT INTO collections(name, name_key, created_at, updated_at)
VALUES ('Vault', 'vault', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
INSERT INTO stash_pages(collection_id, page_index, name, name_key, created_at, updated_at)
SELECT id, 0, 'Stash 1', 'stash 1',
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
       strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
FROM collections WHERE name_key='vault';
PRAGMA user_version = 6;
COMMIT;
"""

_REQUIRED_TABLES = frozenset({
    "schema_meta", "collections", "stash_pages", "items", "transfer_batches", "transfers", "events"
})

_REQUIRED_COLUMNS = {
    "schema_meta": frozenset({"key", "value"}),
    "collections": frozenset({"id", "name", "name_key", "created_at", "updated_at"}),
    "stash_pages": frozenset({
        "id", "collection_id", "page_index", "name", "name_key",
        "created_at", "updated_at",
    }),
    "items": frozenset({
        "id", "collection_id", "raw_json", "raw_sha256", "search_text",
        "source_item_key", "label", "custom_name", "source", "deposit_key", "status",
        "reserved_token", "page_index", "layout_x", "layout_y", "created_at", "updated_at",
    }),
    "transfer_batches": frozenset({
        "request_id", "request_hash", "direction", "status", "item_count",
        "collection_id", "collection_name", "stash_before_sha256",
        "stash_after_sha256", "observed_stash_sha256", "error", "created_at",
        "updated_at", "finished_at",
    }),
    "transfers": frozenset({
        "request_id", "request_hash", "direction", "status", "item_id",
        "collection_id", "collection_name", "raw_json", "raw_sha256", "deposit_key",
        "source_tab", "source_key", "target_tab", "target_key",
        "target_pos_json", "stash_before_sha256", "stash_after_sha256",
        "observed_stash_sha256", "error", "created_at", "updated_at",
        "finished_at", "batch_id", "batch_ordinal",
    }),
    "events": frozenset({
        "id", "event_type", "item_id", "collection_name",
        "withdrawal_token", "created_at", "details_json",
    }),
}

_REQUIRED_COLUMNS_V5 = {
    table: columns
    for table, columns in _REQUIRED_COLUMNS.items()
    if table != "stash_pages"
}

_REQUIRED_COLUMNS_V4 = {
    table: (
        columns - {"page_index", "layout_x", "layout_y"}
        if table == "items" else columns
    )
    for table, columns in _REQUIRED_COLUMNS_V5.items()
}

_REQUIRED_COLUMNS_V3 = {
    table: (
        columns - {"batch_id", "batch_ordinal"}
        if table == "transfers" else columns
    )
    for table, columns in _REQUIRED_COLUMNS_V4.items()
    if table != "transfer_batches"
}

_REQUIRED_COLUMNS_V2 = {
    table: (columns - {"custom_name"} if table == "items" else columns)
    for table, columns in _REQUIRED_COLUMNS_V3.items()
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


def _clean_stash_name(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise VaultValidationError("stash name must be text")
    name = unicodedata.normalize("NFC", value.strip())
    if not name:
        raise VaultValidationError("stash name cannot be empty")
    if len(name) > MAX_STASH_NAME_LENGTH:
        raise VaultValidationError(
            f"stash name cannot exceed {MAX_STASH_NAME_LENGTH} characters"
        )
    if any(unicodedata.category(char).startswith("C") for char in name):
        raise VaultValidationError("stash name cannot contain control characters")
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


def _clean_custom_name(value: Any) -> str | None:
    """Normalize one Vault-only alias; blank text deliberately clears it."""

    cleaned = _clean_optional_text(value, "custom name")
    if cleaned is None:
        return None
    cleaned = unicodedata.normalize("NFC", cleaned)
    if len(cleaned) > MAX_CUSTOM_NAME_LENGTH:
        raise VaultValidationError(
            f"custom name cannot exceed {MAX_CUSTOM_NAME_LENGTH} characters"
        )
    if any(unicodedata.category(char).startswith("C") for char in cleaned):
        raise VaultValidationError("custom name cannot contain control characters")
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


def _validate_stored_raw_item_integrity(
    raw_item_json: Any, raw_sha256: Any
) -> dict[str, Any]:
    """Validate one payload read from SQLite and its persisted digest."""

    if not isinstance(raw_item_json, str):
        raise VaultSchemaError("stored item JSON is not text")
    try:
        actual_sha256 = hashlib.sha256(raw_item_json.encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise VaultSchemaError("stored item JSON is not valid Unicode") from exc
    if (
        not isinstance(raw_sha256, str)
        or _SHA256_RE.fullmatch(raw_sha256) is None
        or actual_sha256 != raw_sha256.lower()
    ):
        raise VaultSchemaError("stored item JSON hash does not match its metadata")
    try:
        return validate_raw_item_json(raw_item_json)
    except VaultValidationError as exc:
        raise VaultSchemaError("stored item JSON is malformed") from exc


def validate_item_record_integrity(record: VaultItemRecord) -> dict[str, Any]:
    """Recompute and validate one :class:`VaultItemRecord` payload.

    Callers that plan a batch operation can use this before trusting the
    decoded item. The database transaction repeats the same check when it
    reserves the batch, so this public preflight is not a TOCTOU boundary.
    """

    if not isinstance(record, VaultItemRecord):
        raise VaultValidationError("Vault item record is required")
    return _validate_stored_raw_item_integrity(
        record.raw_item_json, record.raw_sha256
    )


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

    @staticmethod
    def _validate_schema_shape(
        connection: sqlite3.Connection,
        tables: set[str],
        required_columns: Mapping[str, frozenset[str]],
        expected_version: int,
    ) -> None:
        missing = set(required_columns).difference(tables)
        if missing:
            raise VaultSchemaError(
                "vault database is missing tables: " + ", ".join(sorted(missing))
            )
        for table, columns in required_columns.items():
            actual_columns = {
                row[1]
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }
            missing_columns = columns.difference(actual_columns)
            if missing_columns:
                raise VaultSchemaError(
                    f"vault table {table} is missing columns: "
                    + ", ".join(sorted(missing_columns))
                )
        meta = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if meta is None or meta[0] != str(expected_version):
            raise VaultSchemaError("vault schema metadata does not match user_version")

    @staticmethod
    def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
        """Add Vault-only aliases without rewriting any native item payload."""

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE items ADD COLUMN custom_name TEXT")
            connection.execute(
                "UPDATE schema_meta SET value='3' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise VaultSchemaError(
                f"vault schema migration from 2 to 3 failed: {exc}"
            ) from exc

    @staticmethod
    def _migrate_v3_to_v4(connection: sqlite3.Connection) -> None:
        """Add an atomic parent journal for multi-item stash transfers."""

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS transfer_batches (
                       request_id TEXT PRIMARY KEY,
                       request_hash TEXT NOT NULL,
                       direction TEXT NOT NULL CHECK (direction IN ('deposit', 'withdrawal')),
                       status TEXT NOT NULL CHECK (status IN ('prepared', 'committed', 'conflict', 'cancelled')),
                       item_count INTEGER NOT NULL CHECK (item_count > 0),
                       collection_id INTEGER REFERENCES collections(id) ON DELETE SET NULL,
                       collection_name TEXT,
                       stash_before_sha256 TEXT NOT NULL,
                       stash_after_sha256 TEXT NOT NULL,
                       observed_stash_sha256 TEXT,
                       error TEXT,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       finished_at TEXT
                   )"""
            )
            transfer_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(transfers)"
                ).fetchall()
            }
            if "batch_id" not in transfer_columns:
                connection.execute(
                    "ALTER TABLE transfers ADD COLUMN batch_id TEXT REFERENCES transfer_batches(request_id) ON DELETE RESTRICT"
                )
            if "batch_ordinal" not in transfer_columns:
                connection.execute(
                    "ALTER TABLE transfers ADD COLUMN batch_ordinal INTEGER"
                )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS transfers_batch_ordinal_unique
                   ON transfers(batch_id, batch_ordinal)
                   WHERE batch_id IS NOT NULL"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS transfer_batches_status_idx
                   ON transfer_batches(status, created_at, request_id)"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS transfers_batch_idx ON transfers(batch_id, batch_ordinal)"
            )
            connection.execute(
                "UPDATE schema_meta SET value='4' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 4")
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise VaultSchemaError(
                f"vault schema migration from 3 to 4 failed: {exc}"
            ) from exc

    @staticmethod
    def _migrate_v4_to_v5(connection: sqlite3.Connection) -> None:
        """Add persistent per-collection Vault page coordinates.

        Existing items deliberately start unplaced. The editor resolves their
        real dimensions and assigns non-overlapping cells on first use without
        touching the native item JSON.
        """

        try:
            connection.execute("BEGIN IMMEDIATE")
            item_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(items)"
                ).fetchall()
            }
            if "page_index" not in item_columns:
                connection.execute(
                    "ALTER TABLE items ADD COLUMN page_index INTEGER CHECK (page_index IS NULL OR page_index >= 0)"
                )
            if "layout_x" not in item_columns:
                connection.execute(
                    "ALTER TABLE items ADD COLUMN layout_x INTEGER CHECK (layout_x IS NULL OR (layout_x >= 0 AND layout_x < 17))"
                )
            if "layout_y" not in item_columns:
                connection.execute(
                    "ALTER TABLE items ADD COLUMN layout_y INTEGER CHECK (layout_y IS NULL OR (layout_y >= 0 AND layout_y < 18))"
                )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS items_collection_layout_idx
                   ON items(collection_id, page_index, layout_y, layout_x, id)"""
            )
            connection.execute(
                "UPDATE schema_meta SET value='5' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 5")
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise VaultSchemaError(
                f"vault schema migration from 4 to 5 failed: {exc}"
            ) from exc

    @staticmethod
    def _migrate_v5_to_v6(connection: sqlite3.Connection) -> None:
        """Turn implicit grid indexes into named stash records.

        Every existing collection receives at least one stash. Existing page
        indexes keep their identity and native item JSON is never rewritten.
        """

        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS stash_pages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                       page_index INTEGER NOT NULL CHECK (page_index >= 0),
                       name TEXT NOT NULL,
                       name_key TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       UNIQUE(collection_id, page_index),
                       UNIQUE(collection_id, name_key)
                   )"""
            )
            now = _utc_now()
            collections = connection.execute(
                "SELECT id FROM collections ORDER BY id"
            ).fetchall()
            for collection in collections:
                collection_id = int(collection["id"])
                maximum = connection.execute(
                    "SELECT MAX(page_index) FROM items WHERE collection_id=?",
                    (collection_id,),
                ).fetchone()[0]
                page_count = max(1, int(maximum) + 1 if maximum is not None else 1)
                for page_index in range(page_count):
                    name = f"Stash {page_index + 1}"
                    connection.execute(
                        """INSERT OR IGNORE INTO stash_pages(
                               collection_id, page_index, name, name_key,
                               created_at, updated_at
                           ) VALUES (?, ?, ?, ?, ?, ?)""",
                        (collection_id, page_index, name, name.casefold(), now, now),
                    )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS stash_pages_collection_idx
                   ON stash_pages(collection_id, page_index)"""
            )
            connection.execute(
                "UPDATE schema_meta SET value='6' WHERE key='schema_version'"
            )
            connection.execute("PRAGMA user_version = 6")
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise VaultSchemaError(
                f"vault schema migration from 5 to 6 failed: {exc}"
            ) from exc

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
                if version not in {2, 3, 4, 5}:
                    raise VaultSchemaError(
                        f"vault schema {version} has no supported migration to {SCHEMA_VERSION}"
                    )
                self._validate_schema_shape(
                    connection,
                    tables,
                    (
                        _REQUIRED_COLUMNS_V2
                        if version == 2
                        else _REQUIRED_COLUMNS_V3
                        if version == 3
                        else _REQUIRED_COLUMNS_V4
                        if version == 4
                        else _REQUIRED_COLUMNS_V5
                    ),
                    version,
                )
                # A migration is a mutation. Preserve the complete old database
                # once before applying the supported sequential upgrades.
                connection.close()
                self._backup_existing()
                connection = self._connect()
                if version == 2:
                    self._migrate_v2_to_v3(connection)
                    version = 3
                if version == 3:
                    self._migrate_v3_to_v4(connection)
                    version = 4
                if version == 4:
                    self._migrate_v4_to_v5(connection)
                    version = 5
                if version == 5:
                    self._migrate_v5_to_v6(connection)
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise VaultSchemaError(
                        f"vault database integrity check failed after migration: {integrity}"
                    )
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self._validate_schema_shape(
                connection, tables, _REQUIRED_COLUMNS, SCHEMA_VERSION
            )
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
        keys = set(row.keys())
        return CollectionRecord(
            id=int(row["id"]),
            name=str(row["name"]),
            available_count=int(row["available_count"]),
            reserved_count=int(row["reserved_count"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            stash_count=(int(row["stash_count"]) if "stash_count" in keys else 0),
        )

    @staticmethod
    def _stash_page_from_row(row: sqlite3.Row) -> StashPageRecord:
        return StashPageRecord(
            id=int(row["id"]),
            collection_id=int(row["collection_id"]),
            page_index=int(row["page_index"]),
            name=str(row["name"]),
            item_count=int(row["item_count"]),
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
            custom_name=row["custom_name"],
            source=row["source"],
            deposit_key=row["deposit_key"],
            status=str(row["status"]),
            reserved_token=row["reserved_token"],
            page_index=(
                int(row["page_index"]) if row["page_index"] is not None else None
            ),
            layout_x=(int(row["layout_x"]) if row["layout_x"] is not None else None),
            layout_y=(int(row["layout_y"]) if row["layout_y"] is not None else None),
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
    def _batch_from_row(row: sqlite3.Row) -> TransferBatchRecord:
        return TransferBatchRecord(
            request_id=str(row["request_id"]),
            request_hash=str(row["request_hash"]),
            direction=str(row["direction"]),
            status=str(row["status"]),
            item_count=int(row["item_count"]),
            collection_id=(
                int(row["collection_id"])
                if row["collection_id"] is not None else None
            ),
            collection_name=row["collection_name"],
            stash_before_sha256=str(row["stash_before_sha256"]),
            stash_after_sha256=str(row["stash_after_sha256"]),
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

    @staticmethod
    def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            details = json.loads(str(row["details_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise VaultSchemaError("stored Vault history details are malformed") from exc
        if not isinstance(details, dict):
            raise VaultSchemaError("stored Vault history details are not an object")
        return {
            "id": int(row["id"]),
            "eventType": str(row["event_type"]),
            "itemId": row["item_id"],
            "collectionName": row["collection_name"],
            "withdrawalToken": row["withdrawal_token"],
            "createdAt": str(row["created_at"]),
            "details": details,
        }

    def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise VaultValidationError("history limit must be between 1 and 200")
        return self._read(
            lambda connection: [
                self._event_payload(row)
                for row in connection.execute(
                    """SELECT * FROM events ORDER BY id DESC LIMIT ?""", (limit,)
                ).fetchall()
            ]
        )

    @staticmethod
    def _latest_reversible_event(
        connection: sqlite3.Connection,
    ) -> tuple[sqlite3.Row, dict[str, Any]] | None:
        reversible = {
            "item_custom_name_updated",
            "item_moved",
            "items_moved",
            "collection_layout_updated",
        }
        rows = connection.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 2000"
        ).fetchall()
        undone: set[int] = set()
        for row in rows:
            if row["event_type"] != "metadata_undo_applied":
                continue
            payload = InfiniteVault._event_payload(row)
            event_id = payload["details"].get("eventId")
            if isinstance(event_id, int):
                undone.add(event_id)
        for row in rows:
            if row["event_type"] in reversible and int(row["id"]) not in undone:
                return row, InfiniteVault._event_payload(row)
        return None

    def preview_metadata_undo(self) -> dict[str, Any] | None:
        def operation(connection: sqlite3.Connection):
            candidate = self._latest_reversible_event(connection)
            if candidate is None:
                return None
            _row, event = candidate
            details = event["details"]
            event_type = event["eventType"]
            count = (
                len(details.get("changes") or [])
                if event_type in {"items_moved", "collection_layout_updated"}
                else 1
            )
            labels = {
                "item_custom_name_updated": "Restore previous custom name",
                "item_moved": "Move item back to its previous collection",
                "items_moved": "Move selected items back",
                "collection_layout_updated": "Restore previous Vault grid positions",
            }
            return {
                "eventId": event["id"],
                "eventType": event_type,
                "createdAt": event["createdAt"],
                "itemCount": count,
                "label": labels[event_type],
            }

        return self._read(operation)

    def undo_metadata_event(self, expected_event_id: int) -> dict[str, Any]:
        """Undo the latest reversible metadata event after strict state checks."""

        if (
            isinstance(expected_event_id, bool)
            or not isinstance(expected_event_id, int)
            or expected_event_id <= 0
        ):
            raise VaultValidationError("a valid Vault history event id is required")

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            candidate = self._latest_reversible_event(connection)
            if candidate is None:
                raise VaultStateError("there is no reversible Vault metadata action")
            row, event = candidate
            if int(row["id"]) != expected_event_id:
                raise VaultConflictError(
                    "Vault history changed after the undo preview; review it again"
                )
            details = event["details"]
            event_type = event["eventType"]
            now = _utc_now()
            affected: list[str] = []

            if event_type == "item_custom_name_updated":
                item_id = _clean_id(event["itemId"], "item id")
                item = connection.execute(
                    """SELECT i.*, c.name AS collection_name FROM items AS i
                       JOIN collections AS c ON c.id=i.collection_id WHERE i.id=?""",
                    (item_id,),
                ).fetchone()
                if item is None or item["status"] != "available":
                    raise VaultConflictError("the renamed Vault item is no longer available")
                if item["custom_name"] != details.get("customName"):
                    raise VaultConflictError("the custom name changed after this history entry")
                previous_name = details.get("previousCustomName")
                decoded = validate_item_record_integrity(self._item_from_row(item))
                search_text = _search_document(
                    decoded,
                    (
                        item["source_item_key"], item["label"], item["source"],
                        previous_name,
                    ),
                )
                connection.execute(
                    """UPDATE items SET custom_name=?, search_text=?, updated_at=?
                       WHERE id=?""",
                    (previous_name, search_text, now, item_id),
                )
                affected.append(item_id)

            elif event_type == "item_moved":
                changes = [{
                    "itemId": event["itemId"],
                    "previousCollectionId": details.get("previousCollectionId"),
                    "previousLayout": details.get("previousLayout"),
                }]
                destination_name = event["collectionName"]
                affected.extend(self._restore_move_changes(
                    connection, changes, destination_name, now
                ))

            elif event_type == "items_moved":
                changes = details.get("changes")
                if not isinstance(changes, list) or not changes:
                    raise VaultSchemaError("batch move history has no item changes")
                affected.extend(self._restore_move_changes(
                    connection, changes, event["collectionName"], now
                ))

            elif event_type == "collection_layout_updated":
                changes = details.get("changes")
                collection_id = details.get("collectionId")
                if not isinstance(changes, list) or not changes:
                    raise VaultSchemaError("layout history has no item changes")
                if isinstance(collection_id, bool) or not isinstance(collection_id, int):
                    raise VaultSchemaError("layout history collection is invalid")
                for change in changes:
                    if not isinstance(change, dict):
                        raise VaultSchemaError("layout history change is malformed")
                    item_id = _clean_id(change.get("itemId"), "item id")
                    current = change.get("current")
                    previous = change.get("previous")
                    if not isinstance(current, dict) or (
                        previous is not None and not isinstance(previous, dict)
                    ):
                        raise VaultSchemaError("layout history coordinates are malformed")
                    item = connection.execute(
                        "SELECT * FROM items WHERE id=?", (item_id,)
                    ).fetchone()
                    if item is None or item["status"] != "available" or int(item["collection_id"]) != collection_id:
                        raise VaultConflictError("a layout item moved after this history entry")
                    observed = {
                        "pageIndex": item["page_index"],
                        "x": item["layout_x"],
                        "y": item["layout_y"],
                    }
                    if observed != current:
                        raise VaultConflictError("Vault positions changed after this history entry")
                    values = (
                        (None, None, None)
                        if previous is None else (
                            previous.get("pageIndex"), previous.get("x"), previous.get("y")
                        )
                    )
                    connection.execute(
                        """UPDATE items SET page_index=?, layout_x=?, layout_y=?,
                                  updated_at=? WHERE id=?""",
                        (*values, now, item_id),
                    )
                    affected.append(item_id)
            else:  # pragma: no cover - candidate filter is exhaustive.
                raise VaultStateError("this Vault history event cannot be undone")

            self._event(
                connection,
                "metadata_undo_applied",
                details={
                    "eventId": int(row["id"]),
                    "eventType": event_type,
                    "itemCount": len(affected),
                },
            )
            return {
                "eventId": int(row["id"]),
                "eventType": event_type,
                "itemCount": len(affected),
            }

        return self._write(operation)

    @staticmethod
    def _restore_move_changes(
        connection: sqlite3.Connection,
        changes: list[dict[str, Any]],
        destination_name: str | None,
        now: str,
    ) -> list[str]:
        destination = connection.execute(
            "SELECT id FROM collections WHERE name=?", (destination_name,)
        ).fetchone()
        if destination is None:
            raise VaultConflictError("the move destination collection no longer exists")
        prepared: list[tuple[str, int, tuple[Any, Any, Any]]] = []
        for change in changes:
            if not isinstance(change, dict):
                raise VaultSchemaError("move history change is malformed")
            item_id = _clean_id(change.get("itemId"), "item id")
            previous_collection = change.get("previousCollectionId")
            if (
                isinstance(previous_collection, bool)
                or not isinstance(previous_collection, int)
            ):
                raise VaultSchemaError("move history collection is invalid")
            if connection.execute(
                "SELECT 1 FROM collections WHERE id=?", (previous_collection,)
            ).fetchone() is None:
                raise VaultConflictError("a previous collection no longer exists")
            item = connection.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None or item["status"] != "available":
                raise VaultConflictError("a moved Vault item is no longer available")
            if int(item["collection_id"]) != int(destination["id"]):
                raise VaultConflictError("a moved Vault item changed collection again")
            layout = change.get("previousLayout")
            if layout is None:
                values = (None, None, None)
            elif isinstance(layout, dict):
                values = (
                    layout.get("pageIndex"), layout.get("x"), layout.get("y")
                )
                if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
                    raise VaultSchemaError("move history layout is invalid")
            else:
                raise VaultSchemaError("move history layout is malformed")
            prepared.append((item_id, previous_collection, values))
        for item_id, previous_collection, values in prepared:
            connection.execute(
                """UPDATE items SET collection_id=?, page_index=?, layout_x=?,
                          layout_y=?, updated_at=? WHERE id=?""",
                (previous_collection, *values, now, item_id),
            )
        return [row[0] for row in prepared]

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
            collection_id = int(cursor.lastrowid)
            connection.execute(
                """INSERT INTO stash_pages(
                       collection_id, page_index, name, name_key, created_at, updated_at
                   ) VALUES (?, 0, 'Stash 1', 'stash 1', ?, ?)""",
                (collection_id, now, now),
            )
            self._event(connection, "collection_created", collection_name=clean_name)
            row = connection.execute(
                """SELECT id, name, 0 AS available_count, 0 AS reserved_count,
                          1 AS stash_count, created_at, updated_at
                   FROM collections WHERE id = ?""",
                (collection_id,),
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
                              AS reserved_count,
                          (SELECT COUNT(*) FROM stash_pages AS sp
                           WHERE sp.collection_id=c.id) AS stash_count
                   FROM collections AS c
                   LEFT JOIN items AS i ON i.collection_id = c.id
                   GROUP BY c.id
                   ORDER BY c.name_key, c.id"""
            ).fetchall()
            return [self._collection_from_row(row) for row in rows]

        return self._read(operation)

    def list_stash_pages(self, collection: int | str) -> list[StashPageRecord]:
        def operation(connection: sqlite3.Connection) -> list[StashPageRecord]:
            target = self._resolve_collection(connection, collection)
            rows = connection.execute(
                """SELECT sp.*,
                          COALESCE(SUM(CASE WHEN i.status='available' THEN 1 ELSE 0 END), 0)
                              AS item_count
                   FROM stash_pages AS sp
                   LEFT JOIN items AS i
                     ON i.collection_id=sp.collection_id
                    AND i.page_index=sp.page_index
                   WHERE sp.collection_id=?
                   GROUP BY sp.id
                   ORDER BY sp.page_index""",
                (target["id"],),
            ).fetchall()
            return [self._stash_page_from_row(row) for row in rows]

        return self._read(operation)

    @staticmethod
    def _next_stash_name(
        connection: sqlite3.Connection, collection_id: int, page_index: int
    ) -> tuple[str, str]:
        base = f"Stash {page_index + 1}"
        existing = {
            str(row[0])
            for row in connection.execute(
                "SELECT name_key FROM stash_pages WHERE collection_id=?",
                (collection_id,),
            ).fetchall()
        }
        candidate = base
        suffix = 2
        while candidate.casefold() in existing:
            candidate = f"{base} ({suffix})"
            suffix += 1
        return candidate, candidate.casefold()

    def add_stash_page(self, collection: int | str) -> StashPageRecord:
        """Append exactly one named stash to a category."""

        def operation(connection: sqlite3.Connection) -> StashPageRecord:
            target = self._resolve_collection(connection, collection)
            maximum = connection.execute(
                "SELECT MAX(page_index) FROM stash_pages WHERE collection_id=?",
                (target["id"],),
            ).fetchone()[0]
            page_index = int(maximum) + 1 if maximum is not None else 0
            name, name_key = self._next_stash_name(
                connection, int(target["id"]), page_index
            )
            now = _utc_now()
            cursor = connection.execute(
                """INSERT INTO stash_pages(
                       collection_id, page_index, name, name_key, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (target["id"], page_index, name, name_key, now, now),
            )
            connection.execute(
                "UPDATE collections SET updated_at=? WHERE id=?",
                (now, target["id"]),
            )
            self._event(
                connection, "stash_page_created", collection_name=target["name"],
                details={"pageIndex": page_index, "name": name},
            )
            row = connection.execute(
                """SELECT sp.*, 0 AS item_count FROM stash_pages AS sp
                   WHERE sp.id=?""",
                (cursor.lastrowid,),
            ).fetchone()
            return self._stash_page_from_row(row)

        return self._write(operation)

    def ensure_stash_page_count(
        self, collection: int | str, page_count: int
    ) -> list[StashPageRecord]:
        """Create missing legacy pages up to ``page_count`` in one transaction."""

        if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
            raise VaultValidationError("stash page count must be a positive integer")
        if page_count > MAX_PAGE_SIZE:
            raise VaultValidationError(
                f"a category cannot initialize more than {MAX_PAGE_SIZE} stashes at once"
            )

        def operation(connection: sqlite3.Connection) -> list[StashPageRecord]:
            target = self._resolve_collection(connection, collection)
            existing = {
                int(row[0])
                for row in connection.execute(
                    "SELECT page_index FROM stash_pages WHERE collection_id=?",
                    (target["id"],),
                ).fetchall()
            }
            now = _utc_now()
            created: list[int] = []
            for page_index in range(page_count):
                if page_index in existing:
                    continue
                name, name_key = self._next_stash_name(
                    connection, int(target["id"]), page_index
                )
                connection.execute(
                    """INSERT INTO stash_pages(
                           collection_id, page_index, name, name_key,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (target["id"], page_index, name, name_key, now, now),
                )
                created.append(page_index)
            if created:
                connection.execute(
                    "UPDATE collections SET updated_at=? WHERE id=?",
                    (now, target["id"]),
                )
                self._event(
                    connection, "stash_pages_initialized",
                    collection_name=target["name"],
                    details={"pageIndexes": created},
                )
            rows = connection.execute(
                """SELECT sp.*,
                          COALESCE(SUM(CASE WHEN i.status='available' THEN 1 ELSE 0 END), 0)
                              AS item_count
                   FROM stash_pages AS sp
                   LEFT JOIN items AS i
                     ON i.collection_id=sp.collection_id
                    AND i.page_index=sp.page_index
                   WHERE sp.collection_id=?
                   GROUP BY sp.id ORDER BY sp.page_index""",
                (target["id"],),
            ).fetchall()
            return [self._stash_page_from_row(row) for row in rows]

        return self._write(operation)

    def rename_stash_page(
        self, collection: int | str, page_index: int, new_name: str
    ) -> StashPageRecord:
        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
            raise VaultValidationError("stash page index must be a non-negative integer")
        clean_name, name_key = _clean_stash_name(new_name)

        def operation(connection: sqlite3.Connection) -> StashPageRecord:
            target = self._resolve_collection(connection, collection)
            current = connection.execute(
                """SELECT * FROM stash_pages
                   WHERE collection_id=? AND page_index=?""",
                (target["id"], page_index),
            ).fetchone()
            if current is None:
                raise VaultNotFoundError("stash was not found")
            if current["name"] == clean_name:
                item_count = connection.execute(
                    """SELECT COUNT(*) FROM items
                       WHERE collection_id=? AND page_index=? AND status='available'""",
                    (target["id"], page_index),
                ).fetchone()[0]
                return self._stash_page_from_row(dict(current) | {"item_count": item_count})
            now = _utc_now()
            try:
                connection.execute(
                    """UPDATE stash_pages SET name=?, name_key=?, updated_at=?
                       WHERE id=?""",
                    (clean_name, name_key, now, current["id"]),
                )
            except sqlite3.IntegrityError as exc:
                raise VaultConflictError(
                    "a stash with that name already exists in this category"
                ) from exc
            self._event(
                connection, "stash_page_renamed", collection_name=target["name"],
                details={
                    "pageIndex": page_index,
                    "previousName": current["name"],
                    "name": clean_name,
                },
            )
            row = connection.execute(
                """SELECT sp.*,
                          (SELECT COUNT(*) FROM items AS i
                           WHERE i.collection_id=sp.collection_id
                             AND i.page_index=sp.page_index
                             AND i.status='available') AS item_count
                   FROM stash_pages AS sp WHERE sp.id=?""",
                (current["id"],),
            ).fetchone()
            return self._stash_page_from_row(row)

        return self._write(operation)

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
                              AS reserved_count,
                          (SELECT COUNT(*) FROM stash_pages AS sp
                           WHERE sp.collection_id=c.id) AS stash_count
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

    def set_item_custom_name(
        self, item_id: str, custom_name: str | None
    ) -> VaultItemRecord:
        """Create, replace, or clear a Vault-only item alias.

        ``None`` and blank text clear the alias. The native JSON text and its
        digest are never rewritten; only metadata and the derived search index
        change.
        """

        clean_item_id = _clean_id(item_id, "item id")
        clean_custom_name = _clean_custom_name(custom_name)

        def operation(connection: sqlite3.Connection) -> VaultItemRecord:
            item = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                   WHERE i.id = ?""",
                (clean_item_id,),
            ).fetchone()
            if item is None:
                raise VaultNotFoundError("item was not found")
            if item["status"] != "available":
                raise VaultStateError("only an available item can be renamed")
            if item["custom_name"] == clean_custom_name:
                return self._item_from_row(item)

            decoded = validate_item_record_integrity(self._item_from_row(item))
            search_text = _search_document(
                decoded,
                (
                    item["source_item_key"],
                    item["label"],
                    item["source"],
                    clean_custom_name,
                ),
            )
            now = _utc_now()
            connection.execute(
                """UPDATE items
                   SET custom_name = ?, search_text = ?, updated_at = ?
                   WHERE id = ?""",
                (clean_custom_name, search_text, now, clean_item_id),
            )
            self._event(
                connection,
                "item_custom_name_updated",
                item_id=clean_item_id,
                collection_name=item["collection_name"],
                details={
                    "previousCustomName": item["custom_name"],
                    "customName": clean_custom_name,
                },
            )
            updated = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                   WHERE i.id = ?""",
                (clean_item_id,),
            ).fetchone()
            return self._item_from_row(updated)

        return self._write(operation)

    def clear_item_custom_name(self, item_id: str) -> VaultItemRecord:
        """Clear a Vault-only alias without touching the native item payload."""

        return self.set_item_custom_name(item_id, None)

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
                    ORDER BY
                        CASE WHEN i.page_index IS NULL THEN 1 ELSE 0 END,
                        i.page_index, i.layout_y, i.layout_x,
                        i.created_at DESC, i.id
                    LIMIT ? OFFSET ?""",
                tuple(arguments),
            ).fetchall()
            return [self._item_from_row(row) for row in rows]

        return self._read(operation)

    def list_all_available_items(
        self, *, collection: int | str | None = None
    ) -> list[VaultItemRecord]:
        """Return one transactionally consistent unpaged snapshot for bulk work."""

        def operation(connection: sqlite3.Connection) -> list[VaultItemRecord]:
            arguments: tuple[Any, ...] = ()
            where = "i.status='available'"
            if collection is not None:
                target = self._resolve_collection(connection, collection)
                where += " AND i.collection_id=?"
                arguments = (target["id"],)
            return [
                self._item_from_row(row)
                for row in connection.execute(
                    f"""SELECT i.*, c.name AS collection_name
                        FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                        WHERE {where}
                        ORDER BY
                            CASE WHEN i.page_index IS NULL THEN 1 ELSE 0 END,
                            i.page_index, i.layout_y, i.layout_x,
                            i.created_at, i.id""",
                    arguments,
                ).fetchall()
            ]

        return self._read(operation)

    def available_item_origin_tabs(self) -> dict[str, str]:
        """Return the machine source tab retained by each available deposit journal."""

        def operation(connection: sqlite3.Connection) -> dict[str, str]:
            rows = connection.execute(
                """SELECT i.id,
                          (SELECT t.source_tab FROM transfers AS t
                           WHERE t.item_id=i.id AND t.direction='deposit'
                             AND t.status='committed' AND t.source_tab IS NOT NULL
                           ORDER BY t.finished_at DESC, t.created_at DESC LIMIT 1)
                              AS origin_tab
                   FROM items AS i WHERE i.status='available'"""
            ).fetchall()
            return {
                str(row["id"]): str(row["origin_tab"])
                for row in rows if row["origin_tab"] is not None
            }

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

    def set_item_layouts(
        self,
        collection: int | str,
        placements: Iterable[Mapping[str, Any]],
    ) -> list[VaultItemRecord]:
        """Persist validated grid coordinates without rewriting item payloads.

        Dimension and overlap validation belongs to the editor integration,
        which owns the game catalog. This storage boundary still validates the
        collection, item state, coordinate range, duplicate ids, and performs
        the complete metadata update in one backed-up SQLite transaction.
        """

        prepared: list[tuple[str, int, int, int]] = []
        seen: set[str] = set()
        for raw in placements:
            if not isinstance(raw, Mapping):
                raise VaultValidationError("Vault placements must be objects")
            item_id = _clean_id(raw.get("itemId"), "item id")
            if item_id in seen:
                raise VaultValidationError("Vault placements contain a duplicate item")
            seen.add(item_id)
            values: list[int] = []
            for field, upper in (
                ("pageIndex", SQLITE_MAX_INTEGER),
                ("x", VAULT_GRID_COLUMNS - 1),
                ("y", VAULT_GRID_ROWS - 1),
            ):
                value = raw.get(field)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise VaultValidationError(f"Vault layout {field} must be an integer")
                if not 0 <= value <= upper:
                    raise VaultValidationError(f"Vault layout {field} is out of range")
                values.append(value)
            prepared.append((item_id, values[0], values[1], values[2]))
            if len(prepared) > MAX_PAGE_SIZE:
                raise VaultValidationError(
                    f"at most {MAX_PAGE_SIZE} Vault placements can be saved at once"
                )
        if not prepared:
            return []

        def operation(connection: sqlite3.Connection) -> list[VaultItemRecord]:
            target = self._resolve_collection(connection, collection)
            valid_pages = {
                int(row[0])
                for row in connection.execute(
                    "SELECT page_index FROM stash_pages WHERE collection_id=?",
                    (target["id"],),
                ).fetchall()
            }
            if any(page_index not in valid_pages for _, page_index, _, _ in prepared):
                raise VaultConflictError(
                    "the target stash no longer exists in this category"
                )
            rows = connection.execute(
                """SELECT * FROM items
                   WHERE collection_id=? AND status='available'""",
                (target["id"],),
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            missing = [item_id for item_id, *_ in prepared if item_id not in by_id]
            if missing:
                raise VaultConflictError(
                    "a Vault item moved, disappeared, or became reserved before layout save"
                )
            now = _utc_now()
            changes: list[dict[str, Any]] = []
            for item_id, page_index, x, y in prepared:
                row = by_id[item_id]
                previous = (
                    int(row["page_index"]),
                    int(row["layout_x"]),
                    int(row["layout_y"]),
                ) if (
                    row["page_index"] is not None
                    and row["layout_x"] is not None
                    and row["layout_y"] is not None
                ) else None
                current = (page_index, x, y)
                if previous == current:
                    continue
                connection.execute(
                    """UPDATE items SET page_index=?, layout_x=?, layout_y=?,
                              updated_at=? WHERE id=?""",
                    (page_index, x, y, now, item_id),
                )
                changes.append({
                    "itemId": item_id,
                    "previous": (
                        {"pageIndex": previous[0], "x": previous[1], "y": previous[2]}
                        if previous is not None else None
                    ),
                    "current": {"pageIndex": page_index, "x": x, "y": y},
                })
            if changes:
                self._event(
                    connection,
                    "collection_layout_updated",
                    collection_name=target["name"],
                    details={"collectionId": int(target["id"]), "changes": changes},
                )
            result: list[VaultItemRecord] = []
            for item_id, *_ in prepared:
                row = connection.execute(
                    """SELECT i.*, c.name AS collection_name
                       FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                       WHERE i.id=?""",
                    (item_id,),
                ).fetchone()
                result.append(self._item_from_row(row))
            return result

        return self._write(operation)

    def update_stash_item_payloads(
        self,
        collection: int | str,
        page_index: int,
        updates: Iterable[Mapping[str, Any]],
    ) -> list[VaultItemRecord]:
        """Atomically replace verified item payloads in one named stash.

        This is intentionally separate from layout metadata: callers must
        provide each currently observed SHA-256, and every item is validated
        again inside the backed-up transaction before any payload changes.
        """

        if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
            raise VaultValidationError("stash page index must be a non-negative integer")
        prepared: list[tuple[str, str, str, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for raw in updates:
            if not isinstance(raw, Mapping):
                raise VaultValidationError("Vault item updates must be objects")
            item_id = _clean_id(raw.get("itemId"), "item id")
            if item_id in seen:
                raise VaultValidationError("Vault item updates contain a duplicate item")
            seen.add(item_id)
            expected = raw.get("expectedSha256")
            if not isinstance(expected, str) or _SHA256_RE.fullmatch(expected) is None:
                raise VaultValidationError("expected item SHA-256 is invalid")
            raw_json = raw.get("rawItemJson")
            decoded = validate_raw_item_json(raw_json)
            digest = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            prepared.append((item_id, expected.lower(), raw_json, digest, decoded))
            if len(prepared) > MAX_PAGE_SIZE:
                raise VaultValidationError(
                    f"at most {MAX_PAGE_SIZE} Vault items can be updated at once"
                )
        if not prepared:
            return []

        def operation(connection: sqlite3.Connection) -> list[VaultItemRecord]:
            target = self._resolve_collection(connection, collection)
            page = connection.execute(
                """SELECT 1 FROM stash_pages
                   WHERE collection_id=? AND page_index=?""",
                (target["id"], page_index),
            ).fetchone()
            if page is None:
                raise VaultNotFoundError("stash was not found")
            placeholders = ",".join("?" for _ in prepared)
            rows = connection.execute(
                f"""SELECT * FROM items
                    WHERE id IN ({placeholders}) AND collection_id=?
                      AND page_index=? AND status='available'""",
                tuple(item_id for item_id, *_ in prepared)
                + (target["id"], page_index),
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if len(by_id) != len(prepared):
                raise VaultConflictError(
                    "a Vault item moved, disappeared, or became reserved before the write"
                )
            now = _utc_now()
            changed_ids: list[str] = []
            for item_id, expected, raw_json, digest, decoded in prepared:
                row = by_id[item_id]
                _validate_stored_raw_item_integrity(row["raw_json"], row["raw_sha256"])
                if str(row["raw_sha256"]).lower() != expected:
                    raise VaultConflictError(
                        "a Vault item changed after preview; nothing was written"
                    )
                if row["raw_json"] == raw_json and row["raw_sha256"] == digest:
                    continue
                search_text = _search_document(
                    decoded,
                    (
                        row["source_item_key"], row["label"], row["source"],
                        row["custom_name"],
                    ),
                )
                connection.execute(
                    """UPDATE items SET raw_json=?, raw_sha256=?, search_text=?,
                              updated_at=? WHERE id=?""",
                    (raw_json, digest, search_text, now, item_id),
                )
                changed_ids.append(item_id)
            if changed_ids:
                self._event(
                    connection, "stash_items_optimized",
                    collection_name=target["name"],
                    details={"pageIndex": page_index, "itemIds": changed_ids},
                )
            result: list[VaultItemRecord] = []
            for item_id, *_ in prepared:
                row = connection.execute(
                    """SELECT i.*, c.name AS collection_name
                       FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                       WHERE i.id=?""",
                    (item_id,),
                ).fetchone()
                result.append(self._item_from_row(row))
            return result

        return self._write(operation)

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
            if int(item["collection_id"]) == int(target["id"]):
                row = connection.execute(
                    """SELECT i.*, c.name AS collection_name
                       FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                       WHERE i.id=?""",
                    (clean_item_id,),
                ).fetchone()
                return self._item_from_row(row)
            previous = connection.execute(
                "SELECT name FROM collections WHERE id = ?", (item["collection_id"],)
            ).fetchone()[0]
            now = _utc_now()
            connection.execute(
                """UPDATE items SET collection_id = ?, page_index = NULL,
                          layout_x = NULL, layout_y = NULL, updated_at = ?
                   WHERE id = ?""",
                (target["id"], now, clean_item_id),
            )
            self._event(
                connection,
                "item_moved",
                item_id=clean_item_id,
                collection_name=target["name"],
                details={
                    "previousCollection": previous,
                    "previousCollectionId": int(item["collection_id"]),
                    "previousLayout": (
                        {
                            "pageIndex": int(item["page_index"]),
                            "x": int(item["layout_x"]),
                            "y": int(item["layout_y"]),
                        }
                        if item["page_index"] is not None
                        and item["layout_x"] is not None
                        and item["layout_y"] is not None
                        else None
                    ),
                },
            )
            row = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id = i.collection_id
                   WHERE i.id = ?""",
                (clean_item_id,),
            ).fetchone()
            return self._item_from_row(row)

        return self._write(operation)

    def move_items(
        self, item_ids: Iterable[str], destination: int | str
    ) -> list[VaultItemRecord]:
        """Move a selected set atomically and clear its old page positions."""

        cleaned: list[str] = []
        seen: set[str] = set()
        for value in item_ids:
            item_id = _clean_id(value, "item id")
            if item_id in seen:
                raise VaultValidationError("selected Vault items contain a duplicate")
            seen.add(item_id)
            cleaned.append(item_id)
            if len(cleaned) > MAX_PAGE_SIZE:
                raise VaultValidationError(
                    f"at most {MAX_PAGE_SIZE} Vault items can be moved at once"
                )
        if not cleaned:
            raise VaultValidationError("select at least one Vault item")

        def operation(connection: sqlite3.Connection) -> list[VaultItemRecord]:
            target = self._resolve_collection(connection, destination)
            rows = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                   WHERE i.id IN ({})""".format(",".join("?" for _ in cleaned)),
                tuple(cleaned),
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if set(by_id) != set(cleaned):
                raise VaultConflictError(
                    "a selected Vault item moved or disappeared before the batch"
                )
            if any(row["status"] != "available" for row in rows):
                raise VaultStateError("a reserved Vault item cannot be moved")
            now = _utc_now()
            changes: list[dict[str, Any]] = []
            for item_id in cleaned:
                row = by_id[item_id]
                if int(row["collection_id"]) == int(target["id"]):
                    continue
                previous_layout = (
                    {
                        "pageIndex": int(row["page_index"]),
                        "x": int(row["layout_x"]),
                        "y": int(row["layout_y"]),
                    }
                    if row["page_index"] is not None
                    and row["layout_x"] is not None
                    and row["layout_y"] is not None
                    else None
                )
                connection.execute(
                    """UPDATE items SET collection_id=?, page_index=NULL,
                              layout_x=NULL, layout_y=NULL, updated_at=?
                       WHERE id=?""",
                    (target["id"], now, item_id),
                )
                changes.append({
                    "itemId": item_id,
                    "previousCollectionId": int(row["collection_id"]),
                    "previousCollection": str(row["collection_name"]),
                    "previousLayout": previous_layout,
                })
            if changes:
                self._event(
                    connection,
                    "items_moved",
                    collection_name=target["name"],
                    details={
                        "destinationCollectionId": int(target["id"]),
                        "itemCount": len(changes),
                        "changes": changes,
                    },
                )
            result = []
            for item_id in cleaned:
                row = connection.execute(
                    """SELECT i.*, c.name AS collection_name
                       FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                       WHERE i.id=?""",
                    (item_id,),
                ).fetchone()
                result.append(self._item_from_row(row))
            return result

        return self._write(operation)

    @staticmethod
    def _batch_member_request_id(batch_request_id: str, ordinal: int) -> str:
        return hashlib.sha256(
            f"{batch_request_id}:{ordinal}".encode("ascii")
        ).hexdigest()

    @staticmethod
    def _batch_members(
        connection: sqlite3.Connection, batch_request_id: str
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT * FROM transfers WHERE batch_id=?
               ORDER BY batch_ordinal, request_id""",
            (batch_request_id,),
        ).fetchall()

    def get_transfer_batch(self, request_id: str) -> TransferBatchRecord:
        clean_request_id = _clean_required_request_id(request_id)

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            row = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if row is None:
                raise VaultNotFoundError("transfer batch was not found")
            return self._batch_from_row(row)

        return self._read(operation)

    def list_transfer_batch_members(self, request_id: str) -> list[TransferRecord]:
        clean_request_id = _clean_required_request_id(request_id)

        def operation(connection: sqlite3.Connection) -> list[TransferRecord]:
            parent = connection.execute(
                "SELECT item_count FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if parent is None:
                raise VaultNotFoundError("transfer batch was not found")
            rows = self._batch_members(connection, clean_request_id)
            if len(rows) != int(parent["item_count"]):
                raise VaultSchemaError("transfer batch member count is inconsistent")
            return [self._transfer_from_row(row) for row in rows]

        return self._read(operation)

    def list_pending_transfer_batches(self) -> list[TransferBatchRecord]:
        return self._read(
            lambda connection: [
                self._batch_from_row(row)
                for row in connection.execute(
                    """SELECT * FROM transfer_batches
                       WHERE status IN ('prepared', 'conflict')
                       ORDER BY created_at, request_id"""
                ).fetchall()
            ]
        )

    def prepare_bulk_deposit(
        self,
        collection: int | str,
        entries: Iterable[Mapping[str, Any]],
        *,
        request_id: str,
        request_hash: str,
        stash_before_sha256: str,
        stash_after_sha256: str,
        destination_page_index: int | None = None,
    ) -> TransferBatchRecord:
        """Persist every stash item as one indivisible pending deposit batch."""

        clean_request_id = _clean_required_request_id(request_id)
        clean_request_hash = _clean_sha256(request_hash, "request hash")
        before_hash = _clean_sha256(stash_before_sha256, "stash before hash")
        after_hash = _clean_sha256(stash_after_sha256, "stash after hash")
        if before_hash == after_hash:
            raise VaultValidationError("batch before and after stash hashes must differ")
        if destination_page_index is not None and (
            isinstance(destination_page_index, bool)
            or not isinstance(destination_page_index, int)
            or destination_page_index < 0
        ):
            raise VaultValidationError("destination stash index must be a non-negative integer")
        prepared: list[dict[str, Any]] = []
        sources: set[tuple[str, str]] = set()
        planned_cells: set[tuple[int, int]] = set()
        for raw_spec in entries:
            if not isinstance(raw_spec, Mapping):
                raise VaultValidationError("bulk deposit entries must be objects")
            raw_json = raw_spec.get("raw_item_json")
            decoded = validate_raw_item_json(raw_json)
            source_tab = _clean_optional_text(raw_spec.get("source_tab"), "source tab")
            source_key = _clean_optional_text(raw_spec.get("source_key"), "source key")
            if source_tab is None or source_key is None:
                raise VaultValidationError("bulk deposit source tab and key are required")
            identity = (source_tab, source_key)
            if identity in sources:
                raise VaultValidationError("bulk deposit contains a duplicate stash source")
            sources.add(identity)
            label = _clean_optional_text(raw_spec.get("label"), "label")
            source = _clean_optional_text(raw_spec.get("source"), "source") or source_tab
            raw_sha256 = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            target_pos = None
            target_size = None
            if destination_page_index is not None:
                raw_pos = raw_spec.get("target_pos")
                raw_size = raw_spec.get("target_size")
                if (
                    not isinstance(raw_pos, (list, tuple)) or len(raw_pos) != 2
                    or not isinstance(raw_size, (list, tuple)) or len(raw_size) != 2
                    or any(isinstance(value, bool) or not isinstance(value, int)
                           for value in (*raw_pos, *raw_size))
                ):
                    raise VaultValidationError(
                        "a destination stash transfer requires integer position and size"
                    )
                x, y = int(raw_pos[0]), int(raw_pos[1])
                width, height = int(raw_size[0]), int(raw_size[1])
                if (
                    x < 0 or y < 0 or width < 1 or height < 1
                    or x + width > VAULT_GRID_COLUMNS
                    or y + height > VAULT_GRID_ROWS
                ):
                    raise VaultValidationError("a deposited item does not fit the destination stash")
                cells = {
                    (column, row)
                    for row in range(y, y + height)
                    for column in range(x, x + width)
                }
                if cells & planned_cells:
                    raise VaultValidationError("bulk deposit destination positions overlap")
                planned_cells.update(cells)
                target_pos = (x, y)
                target_size = (width, height)
            elif raw_spec.get("target_pos") is not None or raw_spec.get("target_size") is not None:
                raise VaultValidationError(
                    "destination positions require a concrete destination stash"
                )
            prepared.append({
                "raw_json": raw_json,
                "raw_sha256": raw_sha256,
                "search_text": _search_document(decoded, (source_key, label, source)),
                "source_tab": source_tab,
                "source_key": source_key,
                "label": label,
                "source": source,
                "target_pos": target_pos,
                "target_size": target_size,
            })
        if not prepared:
            raise VaultValidationError("bulk deposit requires at least one item")

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            target = self._resolve_collection(connection, collection)
            if destination_page_index is not None:
                page = connection.execute(
                    """SELECT 1 FROM stash_pages
                       WHERE collection_id=? AND page_index=?""",
                    (target["id"], destination_page_index),
                ).fetchone()
                if page is None:
                    raise VaultNotFoundError("destination stash was not found")
            expected_intent = {
                "direction": "bulk_deposit",
                "collectionId": int(target["id"]),
                "items": [
                    {
                        "sourceTab": row["source_tab"],
                        "sourceKey": row["source_key"],
                        "rawSha256": row["raw_sha256"],
                        **(
                            {
                                "targetPos": list(row["target_pos"]),
                                "targetSize": list(row["target_size"]),
                            }
                            if row["target_pos"] is not None else {}
                        ),
                    }
                    for row in prepared
                ],
            }
            if destination_page_index is not None:
                expected_intent["destinationPageIndex"] = destination_page_index
            expected_hash = canonical_request_hash(expected_intent)
            prior = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["direction"] == "deposit"
                    and prior["request_hash"] == clean_request_hash == expected_hash
                    and int(prior["item_count"]) == len(prepared)
                    and int(prior["collection_id"]) == int(target["id"])
                    and prior["stash_before_sha256"] == before_hash
                    and prior["stash_after_sha256"] == after_hash
                ):
                    return self._batch_from_row(prior)
                raise VaultConflictError("request id was reused with different bulk deposit data")
            if clean_request_hash != expected_hash:
                raise VaultValidationError("request hash does not match bulk deposit intent")
            for row in prepared:
                active = connection.execute(
                    """SELECT request_id FROM transfers
                       WHERE direction='deposit' AND status IN ('prepared', 'conflict')
                         AND source_tab=? AND source_key=? AND stash_before_sha256=?
                         AND raw_sha256=? LIMIT 1""",
                    (
                        row["source_tab"], row["source_key"], before_hash,
                        row["raw_sha256"],
                    ),
                ).fetchone()
                if active is not None:
                    raise VaultConflictError(
                        "a stash source already has an active vault transfer"
                    )
            now = _utc_now()
            connection.execute(
                """INSERT INTO transfer_batches(
                       request_id, request_hash, direction, status, item_count,
                       collection_id, collection_name, stash_before_sha256,
                       stash_after_sha256, observed_stash_sha256, error,
                       created_at, updated_at, finished_at
                   ) VALUES (?, ?, 'deposit', 'prepared', ?, ?, ?, ?, ?, NULL,
                             NULL, ?, ?, NULL)""",
                (
                    clean_request_id, clean_request_hash, len(prepared),
                    target["id"], target["name"], before_hash, after_hash, now, now,
                ),
            )
            for ordinal, row in enumerate(prepared):
                child_id = self._batch_member_request_id(clean_request_id, ordinal)
                item_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO items(
                           id, collection_id, raw_json, raw_sha256, search_text,
                           source_item_key, label, source, deposit_key, status,
                           reserved_token, page_index, layout_x, layout_y,
                           created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'deposit_pending',
                                 NULL, ?, ?, ?, ?, ?)""",
                    (
                        item_id, target["id"], row["raw_json"], row["raw_sha256"],
                        row["search_text"], row["source_key"], row["label"],
                        row["source"], child_id, destination_page_index,
                        row["target_pos"][0] if row["target_pos"] is not None else None,
                        row["target_pos"][1] if row["target_pos"] is not None else None,
                        now, now,
                    ),
                )
                connection.execute(
                    """INSERT INTO transfers(
                           request_id, request_hash, direction, status, item_id,
                           collection_id, collection_name, raw_json, raw_sha256,
                           deposit_key, source_tab, source_key, target_tab, target_key,
                           target_pos_json, stash_before_sha256, stash_after_sha256,
                           observed_stash_sha256, error, created_at, updated_at,
                           finished_at, batch_id, batch_ordinal
                       ) VALUES (?, ?, 'deposit', 'prepared', ?, ?, ?, ?, ?, ?, ?, ?,
                                 NULL, NULL, NULL, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)""",
                    (
                        child_id, clean_request_hash, item_id, target["id"],
                        target["name"], row["raw_json"], row["raw_sha256"],
                        child_id, row["source_tab"], row["source_key"], before_hash,
                        after_hash, now, now, clean_request_id, ordinal,
                    ),
                )
            self._event(
                connection, "bulk_deposit_prepared",
                collection_name=target["name"], withdrawal_token=clean_request_id,
                details={
                    "itemCount": len(prepared),
                    "destinationPageIndex": destination_page_index,
                },
            )
            return self._batch_from_row(
                connection.execute(
                    "SELECT * FROM transfer_batches WHERE request_id=?",
                    (clean_request_id,),
                ).fetchone()
            )

        return self._write(operation)

    def prepare_bulk_withdrawal(
        self,
        targets: Iterable[Mapping[str, Any]],
        *,
        request_id: str,
        request_hash: str,
        stash_before_sha256: str,
        stash_after_sha256: str,
        destination_tab: str | None = None,
        selection: bool = False,
    ) -> TransferBatchRecord:
        """Reserve a previewed complete snapshot or selected subset atomically."""

        clean_request_id = _clean_required_request_id(request_id)
        clean_request_hash = _clean_sha256(request_hash, "request hash")
        before_hash = _clean_sha256(stash_before_sha256, "stash before hash")
        after_hash = _clean_sha256(stash_after_sha256, "stash after hash")
        clean_destination_tab = _clean_optional_text(
            destination_tab, "bulk destination tab"
        )
        if before_hash == after_hash:
            raise VaultValidationError("batch before and after stash hashes must differ")
        if not isinstance(selection, bool):
            raise VaultValidationError("bulk withdrawal selection flag must be boolean")
        prepared: list[dict[str, Any]] = []
        item_ids: set[str] = set()
        destinations: set[tuple[str, str]] = set()
        for raw_spec in targets:
            if not isinstance(raw_spec, Mapping):
                raise VaultValidationError("bulk withdrawal targets must be objects")
            item_id = _clean_id(raw_spec.get("item_id"), "item id")
            if item_id in item_ids:
                raise VaultValidationError("bulk withdrawal contains a duplicate item")
            item_ids.add(item_id)
            target_tab = _clean_optional_text(raw_spec.get("target_tab"), "target tab")
            target_key = _clean_optional_text(raw_spec.get("target_key"), "target key")
            if target_tab is None or target_key is None:
                raise VaultValidationError("bulk withdrawal target tab and key are required")
            destination = (target_tab, target_key)
            if destination in destinations:
                raise VaultValidationError("bulk withdrawal contains a duplicate destination")
            destinations.add(destination)
            raw_pos = raw_spec.get("target_pos")
            target_pos = None if raw_pos is None else _clean_target_pos(raw_pos)
            prepared.append({
                "item_id": item_id,
                "raw_sha256": _clean_sha256(raw_spec.get("raw_sha256"), "raw item hash"),
                "metadata_sha256": _clean_sha256(
                    raw_spec.get("metadata_sha256"), "Vault metadata hash"
                ),
                "target_tab": target_tab,
                "target_key": target_key,
                "target_pos": target_pos,
            })
        if not prepared:
            raise VaultValidationError("bulk withdrawal requires at least one item")

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            prior = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            expected_intent = {
                "direction": "bulk_withdrawal",
                "items": [
                    {
                        "itemId": row["item_id"],
                        "rawSha256": row["raw_sha256"],
                        "metadataSha256": row["metadata_sha256"],
                        "targetTab": row["target_tab"],
                        "targetKey": row["target_key"],
                        "targetPos": (
                            list(row["target_pos"])
                            if row["target_pos"] is not None else None
                        ),
                    }
                    for row in prepared
                ],
            }
            if clean_destination_tab is not None:
                expected_intent["destinationTab"] = clean_destination_tab
            if selection:
                expected_intent["scope"] = "selection"
            expected_hash = canonical_request_hash(expected_intent)
            if prior is not None:
                if (
                    prior["direction"] == "withdrawal"
                    and prior["request_hash"] == clean_request_hash == expected_hash
                    and int(prior["item_count"]) == len(prepared)
                    and prior["stash_before_sha256"] == before_hash
                    and prior["stash_after_sha256"] == after_hash
                ):
                    return self._batch_from_row(prior)
                raise VaultConflictError(
                    "request id was reused with different bulk withdrawal data"
                )
            if clean_request_hash != expected_hash:
                raise VaultValidationError("request hash does not match bulk withdrawal intent")
            available_rows = connection.execute(
                """SELECT i.*, c.name AS collection_name
                   FROM items AS i JOIN collections AS c ON c.id=i.collection_id
                   WHERE i.status='available' ORDER BY i.id"""
            ).fetchall()
            available_ids = {str(row["id"]) for row in available_rows}
            snapshot_matches = (
                item_ids.issubset(available_ids)
                if selection else available_ids == item_ids
            )
            if not snapshot_matches:
                raise VaultConflictError(
                    "the available Vault changed after the bulk transfer was previewed"
                )
            by_id = {str(row["id"]): row for row in available_rows}
            for target in prepared:
                item = by_id[target["item_id"]]
                _validate_stored_raw_item_integrity(
                    item["raw_json"], item["raw_sha256"]
                )
                if item["raw_sha256"] != target["raw_sha256"]:
                    raise VaultConflictError("a Vault item changed after preview")
                metadata_sha256 = canonical_request_hash({
                    "id": str(item["id"]),
                    "collectionId": int(item["collection_id"]),
                    "collectionName": str(item["collection_name"]),
                    "sourceItemKey": item["source_item_key"],
                    "label": item["label"],
                    "customName": item["custom_name"],
                    "source": item["source"],
                    "depositKey": item["deposit_key"],
                    "createdAt": str(item["created_at"]),
                    "updatedAt": str(item["updated_at"]),
                })
                if metadata_sha256 != target["metadata_sha256"]:
                    raise VaultConflictError(
                        "Vault item metadata changed after preview"
                    )
            now = _utc_now()
            connection.execute(
                """INSERT INTO transfer_batches(
                       request_id, request_hash, direction, status, item_count,
                       collection_id, collection_name, stash_before_sha256,
                       stash_after_sha256, observed_stash_sha256, error,
                       created_at, updated_at, finished_at
                   ) VALUES (?, ?, 'withdrawal', 'prepared', ?, NULL, NULL, ?, ?,
                             NULL, NULL, ?, ?, NULL)""",
                (
                    clean_request_id, clean_request_hash, len(prepared),
                    before_hash, after_hash, now, now,
                ),
            )
            for ordinal, target in enumerate(prepared):
                item = by_id[target["item_id"]]
                child_id = self._batch_member_request_id(clean_request_id, ordinal)
                target_pos_json = (
                    json.dumps(list(target["target_pos"]), separators=(",", ":"))
                    if target["target_pos"] is not None else None
                )
                connection.execute(
                    """INSERT INTO transfers(
                           request_id, request_hash, direction, status, item_id,
                           collection_id, collection_name, raw_json, raw_sha256,
                           deposit_key, source_tab, source_key, target_tab, target_key,
                           target_pos_json, stash_before_sha256, stash_after_sha256,
                           observed_stash_sha256, error, created_at, updated_at,
                           finished_at, batch_id, batch_ordinal
                       ) VALUES (?, ?, 'withdrawal', 'prepared', ?, ?, ?, ?, ?, ?, ?, ?,
                                 ?, ?, ?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?)""",
                    (
                        child_id, clean_request_hash, item["id"], item["collection_id"],
                        item["collection_name"], item["raw_json"], item["raw_sha256"],
                        item["deposit_key"], item["source"], item["source_item_key"],
                        target["target_tab"], target["target_key"], target_pos_json,
                        before_hash, after_hash, now, now, clean_request_id, ordinal,
                    ),
                )
                connection.execute(
                    """UPDATE items SET status='reserved', reserved_token=?, updated_at=?
                       WHERE id=? AND status='available'""",
                    (child_id, now, item["id"]),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise VaultConflictError("a Vault item could not be reserved")
            self._event(
                connection, "bulk_withdrawal_prepared",
                withdrawal_token=clean_request_id,
                details={"itemCount": len(prepared)},
            )
            return self._batch_from_row(
                connection.execute(
                    "SELECT * FROM transfer_batches WHERE request_id=?",
                    (clean_request_id,),
                ).fetchone()
            )

        return self._write(operation)

    @staticmethod
    def _validate_batch_members(
        connection: sqlite3.Connection, batch: sqlite3.Row
    ) -> list[sqlite3.Row]:
        members = InfiniteVault._batch_members(connection, str(batch["request_id"]))
        if len(members) != int(batch["item_count"]):
            raise VaultSchemaError("transfer batch member count is inconsistent")
        for ordinal, member in enumerate(members):
            if int(member["batch_ordinal"]) != ordinal:
                raise VaultSchemaError("transfer batch ordinals are inconsistent")
            if member["direction"] != batch["direction"]:
                raise VaultSchemaError("transfer batch direction is inconsistent")
            if member["request_hash"] != batch["request_hash"]:
                raise VaultSchemaError("transfer batch intent hash is inconsistent")
            if member["stash_before_sha256"] != batch["stash_before_sha256"]:
                raise VaultSchemaError("transfer batch before hash is inconsistent")
            if member["stash_after_sha256"] != batch["stash_after_sha256"]:
                raise VaultSchemaError("transfer batch after hash is inconsistent")
        return members

    @staticmethod
    def _finish_batch_locked(
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
        outcome: str,
        observed: str,
        *,
        evidence: str | None = None,
    ) -> TransferBatchRecord:
        if outcome not in {"committed", "cancelled"}:
            raise VaultValidationError("batch outcome must be committed or cancelled")
        if batch["status"] in {"committed", "cancelled"}:
            if batch["status"] != outcome:
                raise VaultStateError("finished transfer batch has the opposite outcome")
            return InfiniteVault._batch_from_row(batch)
        members = InfiniteVault._validate_batch_members(connection, batch)
        now = _utc_now()
        for member in members:
            item = connection.execute(
                "SELECT * FROM items WHERE id=?", (member["item_id"],)
            ).fetchone()
            if batch["direction"] == "deposit":
                if (
                    item is None
                    or item["status"] != "deposit_pending"
                    or item["raw_sha256"] != member["raw_sha256"]
                    or item["deposit_key"] != member["request_id"]
                ):
                    raise VaultSchemaError("pending batch deposit item is inconsistent")
                if outcome == "committed":
                    connection.execute(
                        "UPDATE items SET status='available', updated_at=? WHERE id=?",
                        (now, item["id"]),
                    )
                else:
                    connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
            else:
                if (
                    item is None
                    or item["status"] != "reserved"
                    or item["reserved_token"] != member["request_id"]
                    or item["raw_sha256"] != member["raw_sha256"]
                ):
                    raise VaultSchemaError("reserved batch withdrawal item is inconsistent")
                if outcome == "committed":
                    connection.execute("DELETE FROM items WHERE id=?", (item["id"],))
                else:
                    connection.execute(
                        """UPDATE items SET status='available', reserved_token=NULL,
                                  updated_at=? WHERE id=?""",
                        (now, item["id"]),
                    )
        connection.execute(
            """UPDATE transfers SET status=?, observed_stash_sha256=?, error=NULL,
                      updated_at=?, finished_at=? WHERE batch_id=?""",
            (outcome, observed, now, now, batch["request_id"]),
        )
        connection.execute(
            """UPDATE transfer_batches
               SET status=?, observed_stash_sha256=?, error=NULL,
                   updated_at=?, finished_at=? WHERE request_id=?""",
            (outcome, observed, now, now, batch["request_id"]),
        )
        InfiniteVault._event(
            connection,
            f"bulk_{batch['direction']}_{outcome}",
            collection_name=batch["collection_name"],
            withdrawal_token=batch["request_id"],
            details={
                "itemCount": int(batch["item_count"]),
                **({"evidence": evidence} if evidence else {}),
            },
        )
        return InfiniteVault._batch_from_row(
            connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (batch["request_id"],),
            ).fetchone()
        )

    def commit_transfer_batch(
        self, request_id: str, observed_stash_sha256: str
    ) -> TransferBatchRecord:
        clean_request_id = _clean_required_request_id(request_id)
        observed = _clean_sha256(observed_stash_sha256, "observed stash hash")

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            batch = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if batch is None:
                raise VaultNotFoundError("transfer batch was not found")
            if batch["status"] == "committed":
                return self._batch_from_row(batch)
            if batch["status"] == "cancelled":
                raise VaultStateError("a cancelled transfer batch cannot be committed")
            if observed != batch["stash_after_sha256"]:
                return self._mark_batch_conflict_locked(
                    connection, batch,
                    "observed stash hash does not match prepared batch after hash",
                    observed,
                )
            return self._finish_batch_locked(connection, batch, "committed", observed)

        return self._write(operation)

    def cancel_transfer_batch(
        self, request_id: str, observed_stash_sha256: str
    ) -> TransferBatchRecord:
        clean_request_id = _clean_required_request_id(request_id)
        observed = _clean_sha256(observed_stash_sha256, "observed stash hash")

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            batch = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if batch is None:
                raise VaultNotFoundError("transfer batch was not found")
            if batch["status"] == "cancelled":
                return self._batch_from_row(batch)
            if batch["status"] == "committed":
                raise VaultStateError("a committed transfer batch cannot be cancelled")
            if observed != batch["stash_before_sha256"]:
                return self._mark_batch_conflict_locked(
                    connection, batch,
                    "observed stash hash does not match prepared batch before hash",
                    observed,
                )
            return self._finish_batch_locked(connection, batch, "cancelled", observed)

        return self._write(operation)

    @staticmethod
    def _mark_batch_conflict_locked(
        connection: sqlite3.Connection,
        batch: sqlite3.Row,
        error: str,
        observed: str | None,
    ) -> TransferBatchRecord:
        if batch["status"] in {"committed", "cancelled"}:
            raise VaultStateError("a finished transfer batch cannot be marked conflicted")
        now = _utc_now()
        connection.execute(
            """UPDATE transfers SET status='conflict', observed_stash_sha256=?,
                      error=?, updated_at=? WHERE batch_id=?""",
            (observed, error, now, batch["request_id"]),
        )
        connection.execute(
            """UPDATE transfer_batches SET status='conflict',
                      observed_stash_sha256=?, error=?, updated_at=?
               WHERE request_id=?""",
            (observed, error, now, batch["request_id"]),
        )
        InfiniteVault._event(
            connection, "bulk_transfer_conflict",
            collection_name=batch["collection_name"],
            withdrawal_token=batch["request_id"],
            details={"error": error, "itemCount": int(batch["item_count"])},
        )
        return InfiniteVault._batch_from_row(
            connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (batch["request_id"],),
            ).fetchone()
        )

    def mark_transfer_batch_conflict(
        self,
        request_id: str,
        error: str,
        *,
        observed_stash_sha256: str | None = None,
    ) -> TransferBatchRecord:
        clean_request_id = _clean_required_request_id(request_id)
        clean_error = _clean_optional_text(error, "transfer batch error")
        if clean_error is None:
            raise VaultValidationError("transfer batch error is required")
        observed = _clean_sha256(
            observed_stash_sha256, "observed stash hash", optional=True
        )

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            batch = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if batch is None:
                raise VaultNotFoundError("transfer batch was not found")
            return self._mark_batch_conflict_locked(
                connection, batch, clean_error, observed
            )

        return self._write(operation)

    def resolve_transfer_batch_by_evidence(
        self,
        request_id: str,
        outcome: str,
        observed_stash_sha256: str,
        evidence: str,
    ) -> TransferBatchRecord:
        clean_request_id = _clean_required_request_id(request_id)
        if outcome not in {"committed", "cancelled"}:
            raise VaultValidationError("batch evidence outcome is invalid")
        observed = _clean_sha256(observed_stash_sha256, "observed stash hash")
        clean_evidence = _clean_optional_text(evidence, "batch evidence")
        if clean_evidence is None:
            raise VaultValidationError("batch evidence is required")

        def operation(connection: sqlite3.Connection) -> TransferBatchRecord:
            batch = connection.execute(
                "SELECT * FROM transfer_batches WHERE request_id=?",
                (clean_request_id,),
            ).fetchone()
            if batch is None:
                raise VaultNotFoundError("transfer batch was not found")
            return self._finish_batch_locked(
                connection, batch, outcome, observed, evidence=clean_evidence
            )

        return self._write(operation)

    def reconcile_transfer_batch(
        self, request_id: str, current_stash_sha256: str
    ) -> TransferBatchRecord:
        current = _clean_sha256(current_stash_sha256, "current stash hash")
        batch = self.get_transfer_batch(request_id)
        if batch.status in {"committed", "cancelled"}:
            return batch
        if current == batch.stash_after_sha256:
            return self.commit_transfer_batch(request_id, current)
        if current == batch.stash_before_sha256:
            return self.cancel_transfer_batch(request_id, current)
        return self.mark_transfer_batch_conflict(
            request_id,
            "current stash matches neither side of the prepared batch",
            observed_stash_sha256=current,
        )

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
            if transfer["batch_id"] is not None:
                raise VaultStateError("batch members must be committed through their parent batch")
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
            if transfer["batch_id"] is not None:
                raise VaultStateError("batch members must be cancelled through their parent batch")
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
                    """SELECT * FROM transfers
                       WHERE batch_id IS NULL AND status IN ('prepared', 'conflict')
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
                       WHERE batch_id IS NULL AND direction='withdrawal'
                         AND status IN ('prepared', 'conflict')
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
            if transfer["batch_id"] is not None:
                raise VaultStateError("batch members must be committed through their parent batch")
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
            if transfer["batch_id"] is not None:
                raise VaultStateError("batch members must be cancelled through their parent batch")
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
            if transfer["batch_id"] is not None:
                raise VaultStateError("batch members must be resolved through their parent batch")
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
            if transfer["batch_id"] is not None:
                raise VaultStateError("batch members must be resolved through their parent batch")
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
    "MAX_CUSTOM_NAME_LENGTH",
    "MAX_STASH_NAME_LENGTH",
    "VAULT_GRID_COLUMNS",
    "VAULT_GRID_ROWS",
    "CollectionRecord",
    "StashPageRecord",
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
    "validate_item_record_integrity",
    "canonical_request_hash",
]
