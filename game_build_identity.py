"""Read-only attestation of the installed Steam Hero Siege executable.

Seed proofs are build-specific.  This module deliberately discovers only the
Steam app manifest for app 269210; it never accepts a clean research copy from
the current directory as evidence about the game the user will actually run.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


STEAM_APP_ID = "269210"
EXECUTABLE_RELATIVE_PATH = Path("bin") / "Hero_Siege.exe"


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
    """Cache a matching hash while rechecking path metadata before every use."""

    def __init__(
        self,
        expected_sha256: str,
        steam_roots: Iterable[Path] | None = None,
    ) -> None:
        expected = str(expected_sha256).upper()
        if not re.fullmatch(r"[0-9A-F]{64}", expected):
            raise ValueError("expected executable SHA-256 must be 64 hex characters")
        self.expected_sha256 = expected
        self._steam_roots = (
            tuple(Path(path) for path in steam_roots)
            if steam_roots is not None else None
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
            if digest != self.expected_sha256:
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
            else:
                status = self._status(
                    True,
                    "ready",
                    "Installed Hero Siege build matches the verified seed model.",
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
    "EXECUTABLE_RELATIVE_PATH",
    "GameBuildGuard",
    "GameBuildStatus",
    "STEAM_APP_ID",
    "installed_executable_candidates",
    "registry_steam_roots",
    "steam_library_roots",
]
