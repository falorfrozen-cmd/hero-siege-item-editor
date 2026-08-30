import hashlib
import tempfile
import unittest
from pathlib import Path

try:
    from HSItemEditor import game_build_identity as subject
except ModuleNotFoundError:
    import game_build_identity as subject


class GameBuildIdentityTests(unittest.TestCase):
    def _install(self, root: Path, payload: bytes, folder: str = "HeroSiege") -> Path:
        steamapps = root / "steamapps"
        executable = steamapps / "common" / folder / "bin" / "Hero_Siege.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(payload)
        (steamapps / "appmanifest_269210.acf").write_text(
            '"AppState"\n{\n"appid" "269210"\n'
            f'"installdir" "{folder}"\n}}\n',
            encoding="utf-8",
        )
        return executable.resolve()

    def test_exact_manifest_install_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            payload = b"verified-game-build"
            executable = self._install(root, payload)
            expected = hashlib.sha256(payload).hexdigest()
            guard = subject.GameBuildGuard(expected, [root])

            status = guard.verify()
            self.assertTrue(status.matched, status.message)
            self.assertEqual(status.code, "ready")
            self.assertEqual(status.executable_path, executable)
            self.assertIsNone(guard.error())

    def test_missing_mismatch_and_changed_after_startup_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            expected_payload = b"expected"
            expected = hashlib.sha256(expected_payload).hexdigest()
            guard = subject.GameBuildGuard(expected, [root])
            self.assertEqual(guard.verify().code, "not_found")

            executable = self._install(root, expected_payload)
            self.assertTrue(guard.verify().matched)
            executable.write_bytes(b"updated-game-build-with-different-size")
            changed = guard.verify()
            self.assertFalse(changed.matched)
            self.assertEqual(changed.code, "build_mismatch")
            self.assertIsNotNone(guard.error())

    def test_multiple_manifest_installs_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first = base / "SteamA"
            second = base / "SteamB"
            payload = b"same-build"
            self._install(first, payload)
            self._install(second, payload)
            guard = subject.GameBuildGuard(
                hashlib.sha256(payload).hexdigest(), [first, second]
            )
            self.assertEqual(guard.verify().code, "ambiguous")

    def test_configured_library_vdf_is_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            steam = base / "Steam"
            library = base / "Library"
            payload = b"library-build"
            executable = self._install(library, payload)
            vdf = steam / "steamapps" / "libraryfolders.vdf"
            vdf.parent.mkdir(parents=True)
            escaped = str(library).replace("\\", "\\\\")
            vdf.write_text(
                f'"libraryfolders"\n{{\n"1"\n{{\n"path" "{escaped}"\n}}\n}}',
                encoding="utf-8",
            )

            candidates = subject.installed_executable_candidates([steam])
            self.assertEqual(candidates, [executable])


if __name__ == "__main__":
    unittest.main()
