"""Fail-closed recovery for narrowly proven Hero Siege stash corruption.

The normal HSS decoder must remain strict.  This module never treats arbitrary
non-zero UTF-16 high bytes as harmless; it recognizes only fully validated
serializer fingerprints observed in affected Season 10 ``stash.hss`` files.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import zlib
from dataclasses import dataclass, field
from typing import Any, Literal


MAX_HSS_FILE_BYTES = 32 * 1024 * 1024
MAX_HSS_DECODED_BYTES = 128 * 1024 * 1024
MAX_JSON_NODES = 2_000_000
MAX_JSON_DEPTH = 256
_ASCII_WHITESPACE = " \t\r\n\v\f"
_ITEM_CONTAINERS = (
    "material_tab",
    "socket_tab",
    "unique_items",
    *(f"stash_tab_{index}" for index in range(1, 20)),
)
_EXPECTED_ROOT_KEYS = frozenset(
    (*_ITEM_CONTAINERS, "stash_reset", "stash_tab_data")
)
_EXPECTED_TAB_NAMESPACES = frozenset({
    "NH", "LocalNH", "SH", "BP", "LocalNS", "SS", "NS", "Odyssey",
})
_ALLOWED_TAB_IDS = frozenset({-5, -4, -2, *range(20)})
_BAD_LOCALNS_FRAGMENT = '{"tab":-5.0,"name":"\\u00076\xa2}'
_EMPTY_LOCALNS_FRAGMENT = '{"tab":-5.0,"name":""}'
_UNIQUE_LOCALNS_FRAGMENT = '{"tab":-5.0,"name":"Unique"}'
_RECOVERY_SENTINEL = "__HSS_RECOVERY_LOCALNS_UNIQUE_SENTINEL__"


class HSSRecoveryError(ValueError):
    """Raised when an HSS envelope or recovery invariant is invalid."""


@dataclass(frozen=True)
class RecoveryChange:
    code: str
    message: str
    count: int
    decoded_byte_start: int
    decoded_byte_end: int
    original_text: str
    replacement_text: str
    json_pointer: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "count": self.count,
            "decodedByteStart": self.decoded_byte_start,
            "decodedByteEnd": self.decoded_byte_end,
            "originalText": self.original_text,
            "replacementText": self.replacement_text,
            "jsonPointer": self.json_pointer,
        }


@dataclass(frozen=True)
class RecoveryPlan:
    status: Literal["healthy", "recoverable", "unsupported"]
    source_sha256: str
    source_size: int
    decoded_size: int | None
    profile: str | None = None
    output_sha256: str | None = None
    changes: tuple[RecoveryChange, ...] = ()
    root_key_count: int = 0
    item_count: int = 0
    items_by_container: tuple[tuple[str, int], ...] = ()
    item_manifest_sha256: str | None = None
    nonzero_high_bytes: tuple[tuple[int, int, int], ...] = ()
    trailing_codepoints: tuple[int, ...] = ()
    diagnostics: tuple[str, ...] = ()
    recovered_text: str | None = field(default=None, repr=False)

    def as_dict(self, *, file_name: str = "stash.hss", can_apply: bool = True) -> dict[str, Any]:
        return {
            "file": file_name,
            "status": self.status,
            "profile": self.profile,
            "sourceSha256": self.source_sha256,
            "sourceBytes": self.source_size,
            "decodedBytes": self.decoded_size,
            "outputSha256": self.output_sha256,
            "canApply": bool(can_apply and self.status == "recoverable"),
            "topLevelFields": self.root_key_count,
            "itemRecords": self.item_count,
            "itemRecordsPreserved": self.status in {"healthy", "recoverable"},
            "itemsByContainer": dict(self.items_by_container),
            "itemManifestSha256": self.item_manifest_sha256,
            "nonzeroHighByteCount": len(self.nonzero_high_bytes),
            "nonzeroHighBytes": [
                {"offset": offset, "low": low, "high": high}
                for offset, low, high in self.nonzero_high_bytes
            ],
            "trailingCodepoints": list(self.trailing_codepoints),
            "repairs": [change.as_dict() for change in self.changes],
            "diagnostics": list(self.diagnostics),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _unsupported(
    raw: bytes,
    message: str,
    *,
    decoded_size: int | None = None,
    anomalies: tuple[tuple[int, int, int], ...] = (),
    trailing: tuple[int, ...] = (),
) -> RecoveryPlan:
    return RecoveryPlan(
        status="unsupported",
        source_sha256=_sha256(raw),
        source_size=len(raw),
        decoded_size=decoded_size,
        nonzero_high_bytes=anomalies,
        trailing_codepoints=trailing,
        diagnostics=(message,),
    )


def _strict_envelope(raw: bytes) -> bytes:
    if not isinstance(raw, bytes):
        raise HSSRecoveryError("HSS input must be bytes")
    if not raw:
        raise HSSRecoveryError("HSS file is empty")
    if len(raw) > MAX_HSS_FILE_BYTES:
        raise HSSRecoveryError("HSS file exceeds the recovery size limit")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise HSSRecoveryError("HSS envelope is not ASCII") from exc

    envelope = text.rstrip(_ASCII_WHITESPACE)
    if envelope.endswith("\x00"):
        envelope = envelope[:-1]
    if "\x00" in envelope:
        raise HSSRecoveryError("HSS envelope contains an embedded or repeated NUL")
    cleaned = "".join(char for char in envelope if char not in _ASCII_WHITESPACE)
    if not cleaned:
        raise HSSRecoveryError("HSS Base64 payload is empty")
    try:
        packed = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HSSRecoveryError("HSS Base64 payload is invalid") from exc
    if base64.b64encode(packed).decode("ascii") != cleaned:
        raise HSSRecoveryError("HSS Base64 payload is not canonical")
    return packed


def _strict_inflate(packed: bytes) -> bytes:
    inflater = zlib.decompressobj()
    try:
        inflated = inflater.decompress(packed, MAX_HSS_DECODED_BYTES + 1)
    except zlib.error as exc:
        raise HSSRecoveryError("HSS zlib payload is invalid") from exc
    if len(inflated) > MAX_HSS_DECODED_BYTES or inflater.unconsumed_tail:
        raise HSSRecoveryError("HSS decoded payload exceeds the recovery size limit")
    try:
        inflated += inflater.flush()
    except zlib.error as exc:
        raise HSSRecoveryError("HSS zlib payload could not be finalized") from exc
    if len(inflated) > MAX_HSS_DECODED_BYTES:
        raise HSSRecoveryError("HSS decoded payload exceeds the recovery size limit")
    if not inflater.eof:
        raise HSSRecoveryError("HSS zlib stream is truncated")
    if inflater.unused_data or inflater.unconsumed_tail:
        raise HSSRecoveryError("HSS zlib stream has trailing or unconsumed data")
    return inflated


def _xor_payload(payload: bytes, xor_key: bytes) -> bytes:
    if not isinstance(xor_key, bytes) or not xor_key:
        raise HSSRecoveryError("HSS XOR key is invalid")
    return bytes(value ^ xor_key[index % len(xor_key)] for index, value in enumerate(payload))


def decode_hss_bytes_strict(raw: bytes, xor_key: bytes) -> str:
    """Decode a canonical HSS document without applying recovery."""

    decoded = _xor_payload(_strict_inflate(_strict_envelope(raw)), xor_key)
    if len(decoded) % 2:
        raise HSSRecoveryError("HSS decoded text has an odd byte length")
    if any(decoded[index] for index in range(1, len(decoded), 2)):
        raise HSSRecoveryError("HSS decoded text has non-zero UTF-16 high bytes")
    return decoded[::2].decode("latin-1")


def encode_hss_text(text: str, xor_key: bytes, *, level: int = 9) -> bytes:
    if not isinstance(text, str):
        raise HSSRecoveryError("HSS text must be a string")
    try:
        narrow = text.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise HSSRecoveryError("HSS text is outside the supported Latin-1 range") from exc
    wide = bytearray(len(narrow) * 2)
    wide[::2] = narrow
    packed = zlib.compress(_xor_payload(bytes(wide), xor_key), level)
    return base64.b64encode(packed)


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HSSRecoveryError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise HSSRecoveryError(f"JSON contains non-finite constant {value}")


def _parse_json_strict(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_constant,
        )
    except HSSRecoveryError:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise HSSRecoveryError(f"stash JSON is invalid: {exc}") from exc


def _validate_json_resources(value: Any) -> None:
    """Bound validation work without recursively walking attacker-controlled JSON."""

    stack = [(value, 0)]
    visited = 0
    while stack:
        current, depth = stack.pop()
        visited += 1
        if visited > MAX_JSON_NODES:
            raise HSSRecoveryError("stash JSON exceeds the recovery node limit")
        if depth > MAX_JSON_DEPTH:
            raise HSSRecoveryError("stash JSON exceeds the recovery nesting limit")
        if isinstance(current, float) and not math.isfinite(current):
            raise HSSRecoveryError("stash JSON contains a non-finite number")
        if isinstance(current, dict):
            stack.extend((nested, depth + 1) for nested in current.values())
        elif isinstance(current, list):
            stack.extend((nested, depth + 1) for nested in current)


def _validate_stash_document(
    document: Any,
    *,
    expected_unique_name: str | None = "Unique",
) -> tuple[int, tuple[tuple[str, int], ...], str]:
    if not isinstance(document, dict):
        raise HSSRecoveryError("stash JSON root is not an object")
    if frozenset(document) != _EXPECTED_ROOT_KEYS:
        missing = sorted(_EXPECTED_ROOT_KEYS.difference(document))
        extra = sorted(set(document).difference(_EXPECTED_ROOT_KEYS))
        raise HSSRecoveryError(f"stash root schema differs (missing={missing}, extra={extra})")
    _validate_json_resources(document)

    stash_reset = document.get("stash_reset")
    if (
        isinstance(stash_reset, bool)
        or not isinstance(stash_reset, (int, float))
        or (isinstance(stash_reset, float) and not math.isfinite(stash_reset))
        or stash_reset < -(2 ** 53)
        or stash_reset > (2 ** 53)
    ):
        raise HSSRecoveryError("stash_reset is not a finite numeric value")

    tab_data = document.get("stash_tab_data")
    if not isinstance(tab_data, dict):
        raise HSSRecoveryError("stash_tab_data is not an object")
    if frozenset(tab_data) != _EXPECTED_TAB_NAMESPACES:
        raise HSSRecoveryError("stash_tab_data namespace schema differs")

    unique_rows: list[dict[str, Any]] = []
    for namespace, rows in tab_data.items():
        if not isinstance(rows, list):
            raise HSSRecoveryError(f"stash_tab_data.{namespace} is not an array")
        seen_tabs: set[int] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"tab", "name"}:
                raise HSSRecoveryError(
                    f"stash_tab_data.{namespace} contains a malformed row"
                )
            tab = row["tab"]
            name = row["name"]
            if (
                isinstance(tab, bool)
                or not isinstance(tab, (int, float))
                or (isinstance(tab, float) and not math.isfinite(tab))
                or (isinstance(tab, float) and not tab.is_integer())
                or tab not in _ALLOWED_TAB_IDS
            ):
                raise HSSRecoveryError(
                    f"stash_tab_data.{namespace} contains an invalid tab number"
                )
            tab_id = int(tab)
            if tab_id in seen_tabs:
                raise HSSRecoveryError(
                    f"stash_tab_data.{namespace} contains a duplicate tab number"
                )
            seen_tabs.add(tab_id)
            if not isinstance(name, str):
                raise HSSRecoveryError(
                    f"stash_tab_data.{namespace} contains an invalid tab name"
                )
            if namespace == "LocalNS" and tab_id == -5:
                unique_rows.append(row)
    if len(unique_rows) != 1:
        raise HSSRecoveryError("LocalNS must contain exactly one tab=-5 row")
    if expected_unique_name is not None and unique_rows[0].get("name") != expected_unique_name:
        raise HSSRecoveryError("LocalNS tab=-5 does not have the expected Unique name")

    item_rows: list[tuple[str, str, str]] = []
    counts: list[tuple[str, int]] = []
    for container_name in _ITEM_CONTAINERS:
        container = document.get(container_name)
        if not isinstance(container, dict):
            raise HSSRecoveryError(f"{container_name} is not an item object")
        counts.append((container_name, len(container)))
        for item_key in sorted(container):
            item = container[item_key]
            if not isinstance(item, dict) or not isinstance(item.get("data"), dict):
                raise HSSRecoveryError(f"{container_name}/{item_key} is not a native item record")
            try:
                canonical = json.dumps(
                    item,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError, RecursionError) as exc:
                raise HSSRecoveryError(f"{container_name}/{item_key} cannot be canonicalized") from exc
            item_rows.append((container_name, item_key, canonical))
    manifest = hashlib.sha256(
        json.dumps(item_rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest().upper()
    return len(item_rows), tuple(counts), manifest


def _count_sentinel(value: Any) -> int:
    if value == _RECOVERY_SENTINEL:
        return 1
    if isinstance(value, dict):
        return sum(_count_sentinel(nested) for nested in value.values())
    if isinstance(value, list):
        return sum(_count_sentinel(nested) for nested in value)
    return 0


def _candidate_for_garbled_localns(
    core_text: str,
    anomalies: tuple[tuple[int, int, int], ...],
) -> tuple[str, str, RecoveryChange] | None:
    if core_text.count(_BAD_LOCALNS_FRAGMENT) != 1 or len(anomalies) != 3:
        return None
    start = core_text.index(_BAD_LOCALNS_FRAGMENT)
    backslash_unit = start + _BAD_LOCALNS_FRAGMENT.index("\\")
    cent_unit = start + _BAD_LOCALNS_FRAGMENT.index("\xa2")
    expected = (
        (backslash_unit * 2 + 1, 0x5C, 0x01),
        (cent_unit * 2 + 1, 0xA2, 0x01),
        anomalies[-1],
    )
    if anomalies != expected:
        return None

    sentinel_fragment = f'{{"tab":-5.0,"name":"{_RECOVERY_SENTINEL}"}}'
    sentinel_text = core_text.replace(_BAD_LOCALNS_FRAGMENT, sentinel_fragment, 1)
    try:
        sentinel_document = _parse_json_strict(sentinel_text)
        _validate_stash_document(sentinel_document, expected_unique_name=_RECOVERY_SENTINEL)
    except HSSRecoveryError:
        return None
    if _count_sentinel(sentinel_document) != 1:
        return None

    recovered = core_text.replace(_BAD_LOCALNS_FRAGMENT, _UNIQUE_LOCALNS_FRAGMENT, 1)
    change = RecoveryChange(
        code="localns_unique_name",
        message="Restore the corrupted LocalNS Unique-tab name.",
        count=1,
        decoded_byte_start=start * 2,
        decoded_byte_end=(start + len(_BAD_LOCALNS_FRAGMENT)) * 2,
        original_text=repr(_BAD_LOCALNS_FRAGMENT),
        replacement_text=_UNIQUE_LOCALNS_FRAGMENT,
        json_pointer="/stash_tab_data/LocalNS/*/name",
    )
    return "stash_localns_unique_garbled_v1", recovered, change


def _candidate_for_blank_localns(
    core_text: str,
    anomalies: tuple[tuple[int, int, int], ...],
) -> tuple[str, str, RecoveryChange] | None:
    if core_text.count(_EMPTY_LOCALNS_FRAGMENT) != 1 or len(anomalies) != 2:
        return None
    try:
        source_document = _parse_json_strict(core_text)
        _validate_stash_document(source_document, expected_unique_name="")
    except HSSRecoveryError:
        return None

    start = core_text.index(_EMPTY_LOCALNS_FRAGMENT)
    quote_unit = start + _EMPTY_LOCALNS_FRAGMENT.rfind('"')
    if anomalies[0] != (quote_unit * 2 + 1, 0x22, 0x08):
        return None
    recovered = core_text.replace(_EMPTY_LOCALNS_FRAGMENT, _UNIQUE_LOCALNS_FRAGMENT, 1)
    change = RecoveryChange(
        code="localns_unique_name",
        message="Restore the corrupted LocalNS Unique-tab name.",
        count=1,
        decoded_byte_start=start * 2,
        decoded_byte_end=(start + len(_EMPTY_LOCALNS_FRAGMENT)) * 2,
        original_text=_EMPTY_LOCALNS_FRAGMENT,
        replacement_text=_UNIQUE_LOCALNS_FRAGMENT,
        json_pointer="/stash_tab_data/LocalNS/*/name",
    )
    return "stash_localns_unique_blank_v1", recovered, change


def analyze_stash_hss(raw: bytes, xor_key: bytes) -> RecoveryPlan:
    """Inspect one stash without modifying it and return a deterministic plan."""

    try:
        decoded = _xor_payload(_strict_inflate(_strict_envelope(raw)), xor_key)
    except HSSRecoveryError as exc:
        return _unsupported(raw, str(exc))
    if len(decoded) % 2:
        return _unsupported(raw, "HSS decoded text has an odd byte length", decoded_size=len(decoded))

    anomalies = tuple(
        (index, decoded[index - 1], decoded[index])
        for index in range(1, len(decoded), 2)
        if decoded[index]
    )
    low_text = decoded[::2].decode("latin-1")
    if not anomalies:
        try:
            document = _parse_json_strict(low_text)
            item_count, counts, manifest = _validate_stash_document(document)
        except HSSRecoveryError as exc:
            return _unsupported(raw, str(exc), decoded_size=len(decoded))
        return RecoveryPlan(
            status="healthy",
            source_sha256=_sha256(raw),
            source_size=len(raw),
            decoded_size=len(decoded),
            root_key_count=len(document),
            item_count=item_count,
            items_by_container=counts,
            item_manifest_sha256=manifest,
            diagnostics=("Strict HSS decoding and stash validation passed.",),
        )

    trailing = tuple(ord(char) for char in low_text[-2:]) if len(low_text) >= 2 else ()
    terminal = (len(decoded) - 1, 0xFF, 0xFF)
    if (
        len(decoded) < 4
        or decoded[-4:] != b"\x00\x00\xff\xff"
        or trailing != (0, 255)
        or anomalies[-1] != terminal
    ):
        return _unsupported(
            raw,
            "Non-zero UTF-16 high bytes do not match a proven terminal sentinel.",
            decoded_size=len(decoded),
            anomalies=anomalies,
            trailing=trailing,
        )

    core_text = low_text[:-2]
    candidate = _candidate_for_blank_localns(core_text, anomalies)
    if candidate is None:
        candidate = _candidate_for_garbled_localns(core_text, anomalies)
    if candidate is None and anomalies == (terminal,):
        try:
            document = _parse_json_strict(core_text)
            _validate_stash_document(document)
            candidate = (
                "stash_terminal_sentinel_v1",
                core_text,
                RecoveryChange(
                    code="terminal_sentinel",
                    message="Remove the exact invalid terminal NUL/U+FFFF code units.",
                    count=2,
                    decoded_byte_start=len(decoded) - 4,
                    decoded_byte_end=len(decoded),
                    original_text="U+0000,U+FFFF",
                    replacement_text="",
                ),
            )
        except HSSRecoveryError:
            candidate = None
    if candidate is None:
        return _unsupported(
            raw,
            "The HSS anomalies do not match a proven recovery profile.",
            decoded_size=len(decoded),
            anomalies=anomalies,
            trailing=trailing,
        )

    profile, recovered_text, metadata_change = candidate
    try:
        document = _parse_json_strict(recovered_text)
        item_count, counts, manifest = _validate_stash_document(document)
        encoded = encode_hss_text(recovered_text, xor_key)
        round_trip_text = decode_hss_bytes_strict(encoded, xor_key)
        if round_trip_text != recovered_text:
            raise HSSRecoveryError("Recovered HSS text failed exact round-trip validation")
        round_trip_document = _parse_json_strict(round_trip_text)
        round_count, round_counts, round_manifest = _validate_stash_document(round_trip_document)
        if (round_count, round_counts, round_manifest) != (item_count, counts, manifest):
            raise HSSRecoveryError("Recovered HSS item manifest changed during round-trip")
    except HSSRecoveryError as exc:
        return _unsupported(
            raw,
            str(exc),
            decoded_size=len(decoded),
            anomalies=anomalies,
            trailing=trailing,
        )

    terminal_change = RecoveryChange(
        code="terminal_sentinel",
        message="Remove the exact invalid terminal NUL/U+FFFF code units.",
        count=2,
        decoded_byte_start=len(decoded) - 4,
        decoded_byte_end=len(decoded),
        original_text="U+0000,U+FFFF",
        replacement_text="",
    )
    changes = (
        (terminal_change,)
        if profile == "stash_terminal_sentinel_v1"
        else (metadata_change, terminal_change)
    )
    return RecoveryPlan(
        status="recoverable",
        source_sha256=_sha256(raw),
        source_size=len(raw),
        decoded_size=len(decoded),
        profile=profile,
        output_sha256=_sha256(encoded),
        changes=changes,
        root_key_count=len(document),
        item_count=item_count,
        items_by_container=counts,
        item_manifest_sha256=manifest,
        nonzero_high_bytes=anomalies,
        trailing_codepoints=trailing,
        diagnostics=(
            "The file matches a proven Season 10 LocalNS serializer corruption profile.",
            "Only the reported metadata and exact terminal sentinel will change.",
        ),
        recovered_text=recovered_text,
    )


def materialize_recovery(raw: bytes, plan: RecoveryPlan, xor_key: bytes) -> bytes:
    """Create recovered HSS bytes only when ``raw`` still matches ``plan``."""

    if not isinstance(plan, RecoveryPlan) or plan.status != "recoverable" or plan.recovered_text is None:
        raise HSSRecoveryError("A recoverable HSS plan is required")
    if _sha256(raw) != plan.source_sha256:
        raise HSSRecoveryError("HSS source changed after the recovery preview")
    refreshed = analyze_stash_hss(raw, xor_key)
    if refreshed != plan:
        raise HSSRecoveryError("HSS recovery plan no longer matches the source")
    encoded = encode_hss_text(plan.recovered_text, xor_key)
    if _sha256(encoded) != plan.output_sha256:
        raise HSSRecoveryError("HSS recovery output hash does not match the plan")
    return encoded
