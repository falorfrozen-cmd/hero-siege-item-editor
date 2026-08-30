import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from HSItemEditor import game_build_identity as subject
except ModuleNotFoundError:
    import game_build_identity as subject


class GameBuildIdentityTests(unittest.TestCase):
    def _install(self, root: Path, payload: bytes, folder: str = "HeroSiege") -> Path:
        steamapps = root / "steamapps"
        executable = steamapps / "common" / folder / "bin" / "Hero_Siege.exe"
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(payload)
        (steamapps / "appmanifest_269210.acf").write_text(
            '"AppState"\n{\n"appid" "269210"\n'
            f'"installdir" "{folder}"\n}}\n',
            encoding="utf-8",
        )
        return executable.resolve()

    def _clean_test_pe(self) -> tuple[bytes, subject.AurieBaseProfile]:
        """Build a small PE32+ fixture with spare room for one section header."""

        pe_offset = 0x80
        optional_header_offset = pe_offset + 24
        optional_header_size = 0xF0
        section_table_offset = optional_header_offset + optional_header_size
        header_size = 0x200
        file_size = 0x400
        entry_point = 0x1000
        size_of_image = 0x2000

        image = bytearray(file_size)
        image[:2] = b"MZ"
        struct.pack_into("<I", image, 0x3C, pe_offset)
        image[pe_offset : pe_offset + 4] = b"PE\0\0"
        struct.pack_into(
            "<HHIIIHH",
            image,
            pe_offset + 4,
            0x8664,
            1,
            0x12345678,
            0,
            0,
            optional_header_size,
            0x22,
        )
        struct.pack_into("<H", image, optional_header_offset, 0x20B)
        struct.pack_into("<I", image, optional_header_offset + 16, entry_point)
        struct.pack_into("<Q", image, optional_header_offset + 24, 0x140000000)
        struct.pack_into("<I", image, optional_header_offset + 32, 0x1000)
        struct.pack_into("<I", image, optional_header_offset + 36, 0x200)
        struct.pack_into("<I", image, optional_header_offset + 56, size_of_image)
        struct.pack_into("<I", image, optional_header_offset + 60, header_size)
        struct.pack_into(
            "<8sIIIIIIHHI",
            image,
            section_table_offset,
            b".text\0\0\0",
            0x180,
            0x1000,
            0x200,
            header_size,
            0,
            0,
            0,
            0,
            0x60000020,
        )
        image[header_size:] = bytes((index * 17 + 3) & 0xFF for index in range(0x200))
        return bytes(image), subject.AurieBaseProfile(
            file_size=file_size,
            number_of_sections=1,
            address_of_entry_point=entry_point,
            size_of_image=size_of_image,
            aurie_section_size=0x200,
            aurie_entry_point_offset=0x20,
        )

    def _aurie_patch_test_pe(self, clean: bytes, marker: bytes = b"") -> bytes:
        image = bytearray(clean)
        pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
        file_header_offset = pe_offset + 4
        optional_header_offset = pe_offset + 24
        optional_header_size = struct.unpack_from("<H", image, file_header_offset + 16)[0]
        original_sections = struct.unpack_from("<H", image, file_header_offset + 2)[0]
        aurie_header_offset = (
            optional_header_offset + optional_header_size + original_sections * 40
        )
        aurie_raw_size = 0x200
        aurie_virtual_address = 0x2000

        struct.pack_into("<H", image, file_header_offset + 2, original_sections + 1)
        struct.pack_into(
            "<I", image, optional_header_offset + 16, aurie_virtual_address + 0x20
        )
        struct.pack_into("<I", image, optional_header_offset + 56, 0x3000)
        struct.pack_into(
            "<8sIIIIIIHHI",
            image,
            aurie_header_offset,
            b".aurie\0\0",
            aurie_raw_size,
            aurie_virtual_address,
            aurie_raw_size,
            len(clean),
            0,
            0,
            0,
            0,
            0xE0000000,
        )
        payload = bytearray(aurie_raw_size)
        payload[:2] = b"MZ"
        payload[16 : 16 + len(marker)] = marker
        image.extend(payload)
        return bytes(image)

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

    def test_verified_clean_base_with_structural_aurie_patch_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            clean, profile = self._clean_test_pe()
            patched = self._aurie_patch_test_pe(clean, b"C:\\Games\\AurieCore.dll")
            executable = self._install(root, patched)
            expected = hashlib.sha256(clean).hexdigest()
            guard = subject.GameBuildGuard(
                expected, [root], aurie_profile=profile
            )

            status = guard.verify()
            self.assertTrue(status.matched, status.message)
            self.assertEqual(status.code, "ready_aurie")
            self.assertEqual(status.executable_path, executable)
            self.assertEqual(
                status.detected_sha256,
                hashlib.sha256(patched).hexdigest().upper(),
            )
            self.assertIsNone(guard.error())

    def test_variable_aurie_payloads_share_the_same_verified_base(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            clean, profile = self._clean_test_pe()
            first = self._aurie_patch_test_pe(clean, b"C:\\One\\AurieCore.dll")
            second = self._aurie_patch_test_pe(clean, b"F:\\Two\\AurieCore.dll")
            self.assertNotEqual(hashlib.sha256(first).digest(), hashlib.sha256(second).digest())

            executable = self._install(root, first)
            expected = hashlib.sha256(clean).hexdigest()
            guard = subject.GameBuildGuard(expected, [root], aurie_profile=profile)
            self.assertEqual(guard.verify().code, "ready_aurie")
            executable.write_bytes(second)
            self.assertEqual(guard.verify().code, "ready_aurie")

    def test_tampered_base_or_malformed_aurie_patch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            clean, profile = self._clean_test_pe()
            expected = hashlib.sha256(clean).hexdigest()

            tampered = bytearray(self._aurie_patch_test_pe(clean))
            tampered[0x220] ^= 0xFF
            executable = self._install(root, bytes(tampered))
            guard = subject.GameBuildGuard(expected, [root], aurie_profile=profile)
            self.assertEqual(guard.verify().code, "build_mismatch")

            malformed = bytearray(self._aurie_patch_test_pe(clean))
            malformed[len(clean) : len(clean) + 2] = b"NO"
            executable.write_bytes(malformed)
            self.assertEqual(guard.verify().code, "build_mismatch")

            executable.write_bytes(self._aurie_patch_test_pe(clean) + b"overlay")
            self.assertEqual(guard.verify().code, "build_mismatch")

    def test_each_aurie_pe_shape_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            clean, profile = self._clean_test_pe()
            expected = hashlib.sha256(clean).hexdigest()
            valid = self._aurie_patch_test_pe(clean)
            pe_offset = struct.unpack_from("<I", valid, 0x3C)[0]
            file_header_offset = pe_offset + 4
            optional_header_offset = pe_offset + 24
            optional_size = struct.unpack_from("<H", valid, file_header_offset + 16)[0]
            aurie_header_offset = optional_header_offset + optional_size + 40

            def overwrite(offset: int, fmt: str, value: int) -> bytes:
                changed = bytearray(valid)
                struct.pack_into(fmt, changed, offset, value)
                return bytes(changed)

            cases = {
                "section count": overwrite(file_header_offset + 2, "<H", 3),
                "entry point": overwrite(optional_header_offset + 16, "<I", 0x2001),
                "image size": overwrite(optional_header_offset + 56, "<I", 0x4000),
                "virtual size": overwrite(aurie_header_offset + 8, "<I", 0x180),
                "virtual address": overwrite(aurie_header_offset + 12, "<I", 0x3000),
                "raw size": overwrite(aurie_header_offset + 16, "<I", 0x180),
                "raw pointer": overwrite(aurie_header_offset + 20, "<I", 0x200),
                "characteristics": overwrite(aurie_header_offset + 36, "<I", 0x60000020),
            }
            renamed = bytearray(valid)
            renamed[aurie_header_offset : aurie_header_offset + 8] = b".other\0\0"
            cases["section name"] = bytes(renamed)

            for label, payload in cases.items():
                with self.subTest(label=label):
                    executable = self._install(root, payload)
                    guard = subject.GameBuildGuard(
                        expected, [root], aurie_profile=profile
                    )
                    self.assertEqual(guard.verify().code, "build_mismatch")

    def test_change_during_aurie_attestation_is_reported_as_unstable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            clean, profile = self._clean_test_pe()
            patched = self._aurie_patch_test_pe(clean)
            executable = self._install(root, patched)
            expected = hashlib.sha256(clean).hexdigest()
            guard = subject.GameBuildGuard(expected, [root], aurie_profile=profile)
            normalize = subject._aurie_normalized_sha256

            def normalize_then_change(path, selected_profile):
                result = normalize(path, selected_profile)
                with path.open("ab") as stream:
                    stream.write(b"changed-during-verification")
                return result

            with mock.patch.object(
                subject,
                "_aurie_normalized_sha256",
                side_effect=normalize_then_change,
            ):
                self.assertEqual(guard.verify().code, "unstable")

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
