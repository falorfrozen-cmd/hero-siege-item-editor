"""Read-only attestation of the installed Steam Hero Siege executable.

Seed proofs are build-specific.  This module deliberately discovers only the
Steam app manifest for app 269210; it never accepts a clean research copy or a
sidecar backup as evidence about the game the user will actually run.  A known
ForgePact/Aurie patch is normalized in memory and must reconstruct the same
complete clean executable hash.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


STEAM_APP_ID = "269210"
EXECUTABLE_RELATIVE_PATH = Path("bin") / "Hero_Siege.exe"


@dataclass(frozen=True)
class AurieBaseProfile:
    """PE fields AuriePatcher replaces when it appends its loader section.

    The values describe the proven clean executable, not the patched file.
    Reconstructing only these documented changes and hashing the original file
    span lets us attest the complete clean base without trusting the variable
    ``.aurie`` payload or a potentially stale sidecar backup.
    """

    file_size: int
    number_of_sections: int
    address_of_entry_point: int
    size_of_image: int
    aurie_section_size: int
    aurie_entry_point_offset: int


# Clean Season 10 build targeted by the bundled roll/Dice proof databases.
# AuriePatcher adds one final section at this file's EOF and changes only the
# three PE fields recorded here plus the new 40-byte section-table entry.
KNOWN_AURIE_BASE_PROFILES: dict[str, AurieBaseProfile] = {
    "438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4": AurieBaseProfile(
        file_size=281_599_488,
        number_of_sections=8,
        address_of_entry_point=0x0B8D5AB4,
        size_of_image=0x11193000,
        aurie_section_size=0x45000,
        aurie_entry_point_offset=0x1E20,
    ),
}

_PE32_PLUS_MAGIC = 0x20B
_AMD64_MACHINE = 0x8664
_AURIE_SECTION_NAME = b".aurie"
_AURIE_SECTION_CHARACTERISTICS = 0xE0000000
_MAX_PE_HEADER_OFFSET = 1024 * 1024


def _align_up(value: int, alignment: int) -> int:
    if alignment <= 0 or alignment & (alignment - 1):
        raise ValueError("invalid PE section alignment")
    return (value + alignment - 1) & ~(alignment - 1)


def _read_exact(stream, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ValueError("truncated executable")
    return data


def _aurie_normalized_sha256(path: Path, profile: AurieBaseProfile) -> str | None:
    """Return the clean-base digest for a structurally valid Aurie patch.

    Aurie's loader bytes contain an absolute DLL path and process-relocated
    values, so their raw hash is intentionally not allowlisted.  Instead this
    accepts exactly one final RWX ``.aurie`` section, restores the documented
    clean PE header fields in memory, removes that section logically, and
    hashes every byte of the reconstructed clean executable.
    """

    try:
        file_size = path.stat().st_size
        with path.open("rb") as stream:
            dos = _read_exact(stream, 64)
            if dos[:2] != b"MZ":
                return None
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            if pe_offset < 64 or pe_offset > _MAX_PE_HEADER_OFFSET:
                return None

            stream.seek(pe_offset)
            signature = _read_exact(stream, 4)
            if signature != b"PE\0\0":
                return None
            file_header = _read_exact(stream, 20)
            (
                machine,
                number_of_sections,
                _timestamp,
                _symbol_table,
                _symbol_count,
                optional_header_size,
                _file_characteristics,
            ) = struct.unpack("<HHIIIHH", file_header)
            if machine != _AMD64_MACHINE:
                return None
            if number_of_sections != profile.number_of_sections + 1:
                return None

            optional_header_offset = pe_offset + 24
            optional_header = _read_exact(stream, optional_header_size)
            if optional_header_size < 68:
                return None
            if struct.unpack_from("<H", optional_header, 0)[0] != _PE32_PLUS_MAGIC:
                return None
            patched_entry_point = struct.unpack_from("<I", optional_header, 16)[0]
            section_alignment = struct.unpack_from("<I", optional_header, 32)[0]
            patched_size_of_image = struct.unpack_from("<I", optional_header, 56)[0]
            size_of_headers = struct.unpack_from("<I", optional_header, 60)[0]

            section_table_offset = optional_header_offset + optional_header_size
            aurie_header_offset = (
                section_table_offset + profile.number_of_sections * 40
            )
            header_end = aurie_header_offset + 40
            if (
                header_end > size_of_headers
                or header_end > profile.file_size
                or header_end > _MAX_PE_HEADER_OFFSET
            ):
                return None

            stream.seek(aurie_header_offset)
            aurie_header = _read_exact(stream, 40)
            (
                raw_name,
                virtual_size,
                virtual_address,
                raw_size,
                raw_pointer,
                _relocations_pointer,
                _line_numbers_pointer,
                relocation_count,
                line_number_count,
                characteristics,
            ) = struct.unpack("<8sIIIIIIHHI", aurie_header)
            if raw_name.rstrip(b"\0") != _AURIE_SECTION_NAME:
                return None
            if characteristics != _AURIE_SECTION_CHARACTERISTICS:
                return None
            if relocation_count != 0 or line_number_count != 0:
                return None
            if (
                virtual_size != profile.aurie_section_size
                or raw_size != profile.aurie_section_size
            ):
                return None
            if raw_pointer != profile.file_size:
                return None
            if file_size != profile.file_size + raw_size:
                return None
            if virtual_address != profile.size_of_image:
                return None
            expected_image_size = _align_up(
                virtual_address + virtual_size, section_alignment
            )
            if patched_size_of_image != expected_image_size:
                return None
            if patched_entry_point != (
                virtual_address + profile.aurie_entry_point_offset
            ):
                return None

            # The copied AuriePatcher image is a mapped PE image.  Requiring its
            # DOS signature rejects a merely renamed arbitrary appended blob.
            stream.seek(raw_pointer)
            if _read_exact(stream, 2) != b"MZ":
                return None

            stream.seek(0)
            header = bytearray(_read_exact(stream, header_end))
            file_header_offset = pe_offset + 4
            struct.pack_into(
                "<H", header, file_header_offset + 2, profile.number_of_sections
            )
            struct.pack_into(
                "<I",
                header,
                optional_header_offset + 16,
                profile.address_of_entry_point,
            )
            struct.pack_into(
                "<I", header, optional_header_offset + 56, profile.size_of_image
            )
            header[aurie_header_offset:header_end] = b"\0" * 40

            digest = hashlib.sha256()
            digest.update(header)
            remaining = profile.file_size - header_end
            while remaining:
                block = stream.read(min(4 * 1024 * 1024, remaining))
                if not block:
                    raise ValueError("truncated executable")
                digest.update(block)
                remaining -= len(block)
            return digest.hexdigest().upper()
    except (ValueError, struct.error):
        return None


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        try:
            normalized = path.resolve(strict=False)
        except OSError:
            normalized = path
        key = str(normalized).replace("/", "\\").casefold()
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def registry_steam_roots() -> list[Path]:
    """Return Steam roots from the Windows registry and standard locations."""

    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            for hive, key_name in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        for value_name in ("SteamPath", "InstallPath"):
                            try:
                                value, _kind = winreg.QueryValueEx(key, value_name)
                            except OSError:
                                continue
                            if isinstance(value, str) and value.strip():
                                roots.append(Path(value.strip()))
                except OSError:
                    continue
        except ImportError:
            pass
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / "Steam")
    roots.append(Path(r"C:\Program Files (x86)\Steam"))
    return _unique_paths(roots)


def steam_library_roots(steam_roots: Iterable[Path] | None = None) -> list[Path]:
    """Read configured library paths without searching arbitrary drives."""

    roots = list(registry_steam_roots() if steam_roots is None else steam_roots)
    libraries: list[Path] = []
    for root in roots:
        libraries.append(Path(root))
        vdf = Path(root) / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        for raw in re.findall(r'"path"\s+"([^"]+)"', text, flags=re.IGNORECASE):
            libraries.append(Path(raw.replace("\\\\", "\\")))
    return _unique_paths(libraries)


def installed_executable_candidates(
    steam_roots: Iterable[Path] | None = None,
) -> list[Path]:
    """Resolve actual installs only through Hero Siege's Steam manifest."""

    candidates: list[Path] = []
    for library in steam_library_roots(steam_roots):
        steamapps = library / "steamapps"
        manifest = steamapps / f"appmanifest_{STEAM_APP_ID}.acf"
        try:
            text = manifest.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        app_match = re.search(r'"appid"\s+"([^"]+)"', text, flags=re.IGNORECASE)
        install_match = re.search(
            r'"installdir"\s+"([^"]+)"', text, flags=re.IGNORECASE
        )
        if app_match and app_match.group(1) != STEAM_APP_ID:
            continue
        if not install_match:
            continue
        executable = (
            steamapps / "common" / install_match.group(1) / EXECUTABLE_RELATIVE_PATH
        )
        if executable.is_file():
            candidates.append(executable)
    return _unique_paths(candidates)


def _stat_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


@dataclass(frozen=True)
class GameBuildStatus:
    matched: bool
    code: str
    message: str
    expected_sha256: str
    executable_path: Path | None = None
    detected_sha256: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "code": self.code,
            "message": self.message,
            "expectedSha256": self.expected_sha256,
            "executablePath": (
                str(self.executable_path) if self.executable_path is not None else None
            ),
            "detectedSha256": self.detected_sha256,
        }


class GameBuildGuard:
    """Cache a matching attestation while rechecking metadata before every use."""

    def __init__(
        self,
        expected_sha256: str,
        steam_roots: Iterable[Path] | None = None,
        aurie_profile: AurieBaseProfile | None = None,
    ) -> None:
        expected = str(expected_sha256).upper()
        if not re.fullmatch(r"[0-9A-F]{64}", expected):
            raise ValueError("expected executable SHA-256 must be 64 hex characters")
        self.expected_sha256 = expected
        self._steam_roots = (
            tuple(Path(path) for path in steam_roots)
            if steam_roots is not None else None
        )
        self._aurie_profile = (
            aurie_profile
            if aurie_profile is not None
            else KNOWN_AURIE_BASE_PROFILES.get(expected)
        )
        self._lock = threading.RLock()
        self._cached_path: Path | None = None
        self._cached_signature: tuple[int, int, int, int] | None = None
        self._cached_status: GameBuildStatus | None = None

    def _status(
        self,
        matched: bool,
        code: str,
        message: str,
        path: Path | None = None,
        digest: str | None = None,
    ) -> GameBuildStatus:
        return GameBuildStatus(
            matched, code, message, self.expected_sha256, path, digest
        )

    def verify(self) -> GameBuildStatus:
        with self._lock:
            candidates = installed_executable_candidates(self._steam_roots)
            if not candidates:
                return self._status(
                    False,
                    "not_found",
                    "Installed Steam Hero_Siege.exe was not found; build-specific seeds are disabled.",
                )
            if len(candidates) != 1:
                joined = "; ".join(str(path) for path in candidates)
                return self._status(
                    False,
                    "ambiguous",
                    f"Multiple Steam Hero Siege installs were found ({joined}); build-specific seeds are disabled.",
                )
            path = candidates[0]
            try:
                before = _stat_signature(path)
            except OSError as exc:
                return self._status(
                    False, "unreadable", f"Cannot stat installed Hero_Siege.exe: {exc}", path
                )
            if (
                self._cached_status is not None
                and self._cached_path == path
                and self._cached_signature == before
            ):
                return self._cached_status
            try:
                digest = _sha256_file(path)
                normalized_aurie_digest = (
                    _aurie_normalized_sha256(path, self._aurie_profile)
                    if digest != self.expected_sha256
                    and self._aurie_profile is not None
                    else None
                )
                after = _stat_signature(path)
            except OSError as exc:
                return self._status(
                    False, "unreadable", f"Cannot hash installed Hero_Siege.exe: {exc}", path
                )
            if before != after:
                return self._status(
                    False,
                    "unstable",
                    "Installed Hero_Siege.exe changed while it was being verified; build-specific seeds are disabled.",
                    path,
                    digest,
                )
            if digest == self.expected_sha256:
                status = self._status(
                    True,
                    "ready",
                    "Installed Hero Siege build matches the verified seed model.",
                    path,
                    digest,
                )
            elif normalized_aurie_digest == self.expected_sha256:
                status = self._status(
                    True,
                    "ready_aurie",
                    (
                        "Installed Hero Siege base build matches the verified seed model; "
                        "the ForgePact/Aurie loader patch was recognized safely."
                    ),
                    path,
                    digest,
                )
            else:
                status = self._status(
                    False,
                    "build_mismatch",
                    (
                        "Installed Hero_Siege.exe does not match the proven Season 10 build "
                        f"(detected {digest}); build-specific seeds are disabled."
                    ),
                    path,
                    digest,
                )
            self._cached_path = path
            self._cached_signature = after
            self._cached_status = status
            return status

    def error(self) -> str | None:
        status = self.verify()
        return None if status.matched else status.message

    def summary(self) -> dict[str, object]:
        return self.verify().as_dict()


__all__ = [
    "AurieBaseProfile",
    "EXECUTABLE_RELATIVE_PATH",
    "GameBuildGuard",
    "GameBuildStatus",
    "KNOWN_AURIE_BASE_PROFILES",
    "STEAM_APP_ID",
    "installed_executable_candidates",
    "registry_steam_roots",
    "steam_library_roots",
]
