import base64
import json
import math
import unittest
import zlib
from unittest import mock

try:
    from HSItemEditor import hss_recovery as subject
except ModuleNotFoundError:
    import hss_recovery as subject


XOR_KEY = bytes([
    0xE3, 0x95, 0x3D, 0xB1, 0x01, 0x6B, 0xB6, 0x58,
    0x54, 0x38, 0x3F, 0x46, 0xA1, 0x74, 0x29, 0xCC,
    0x45, 0x45, 0x51, 0xF2, 0xA7, 0xF7, 0xAB, 0xB7,
    0x26, 0xF1, 0x37, 0xA8, 0x81, 0x91, 0xE6, 0x7E,
])
UNIQUE = '{"tab":-5.0,"name":"Unique"}'
EMPTY = '{"tab":-5.0,"name":""}'
GARBLED = '{"tab":-5.0,"name":"\\u00076\xa2}'


def fixture_document(item_count=1):
    document = {f"stash_tab_{index}": {} for index in range(1, 20)}
    tab_data = {
        namespace: [{"tab": 0.0, "name": "Personal"}]
        for namespace in ("NH", "LocalNH", "SH", "BP", "LocalNS", "SS", "NS", "Odyssey")
    }
    tab_data["LocalNS"].append({"tab": -5.0, "name": "Unique"})
    document.update({
        "material_tab": {},
        "socket_tab": {},
        "unique_items": {},
        "stash_reset": 0.0,
        "stash_tab_data": tab_data,
    })
    for index in range(item_count):
        document["unique_items"][f"0-0-{1000 + index}-7"] = {
            "data": {"b": float(index + 1), "a": float(index + 10)},
        }
    return document


def fixture_text(item_count=1):
    return json.dumps(fixture_document(item_count), separators=(",", ":"))


def wide_text(text):
    encoded = text.encode("latin-1")
    wide = bytearray(len(encoded) * 2)
    wide[::2] = encoded
    return wide


def pack_decoded(decoded, *, level=6, outer_nul=False, compressed_tail=b""):
    xored = bytes(value ^ XOR_KEY[index % len(XOR_KEY)] for index, value in enumerate(decoded))
    result = base64.b64encode(zlib.compress(xored, level) + compressed_tail)
    return result + (b"\x00" if outer_nul else b"")


def healthy_hss(item_count=1):
    return pack_decoded(wide_text(fixture_text(item_count)), level=9)


def blank_profile_hss(item_count=1, *, extra_high=None):
    return blank_profile_document_hss(fixture_document(item_count), extra_high=extra_high)


def blank_profile_document_hss(document, *, extra_high=None):
    text = json.dumps(document, separators=(",", ":")).replace(UNIQUE, EMPTY, 1)
    wide = wide_text(text)
    fragment_start = text.index(EMPTY)
    closing_quote = fragment_start + EMPTY.rfind('"')
    wide[closing_quote * 2 + 1] = 0x08
    if extra_high is not None:
        wide[extra_high * 2 + 1] = 0x01
    wide += b"\x00\x00\xff\xff"
    return pack_decoded(wide, outer_nul=True)


def garbled_profile_hss(item_count=1):
    text = fixture_text(item_count).replace(UNIQUE, GARBLED, 1)
    wide = wide_text(text)
    fragment_start = text.index(GARBLED)
    wide[(fragment_start + GARBLED.index("\\")) * 2 + 1] = 0x01
    wide[(fragment_start + GARBLED.index("\xa2")) * 2 + 1] = 0x01
    wide += b"\x00\x00\xff\xff"
    return pack_decoded(wide, outer_nul=True)


class HSSRecoveryTests(unittest.TestCase):
    def test_healthy_stash_is_read_only_and_idempotent(self):
        raw = healthy_hss(2)
        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        self.assertEqual(plan.status, "healthy")
        self.assertEqual(plan.item_count, 2)
        self.assertIsNone(plan.output_sha256)
        with self.assertRaises(subject.HSSRecoveryError):
            subject.materialize_recovery(raw, plan, XOR_KEY)

    def test_blank_localns_profile_recovers_only_metadata_and_trailer(self):
        raw = blank_profile_hss(3)
        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        self.assertEqual(plan.status, "recoverable")
        self.assertEqual(plan.profile, "stash_localns_unique_blank_v1")
        self.assertEqual(plan.item_count, 3)
        self.assertEqual([change.code for change in plan.changes], [
            "localns_unique_name", "terminal_sentinel",
        ])

        recovered = subject.materialize_recovery(raw, plan, XOR_KEY)
        recovered_text = subject.decode_hss_bytes_strict(recovered, XOR_KEY)
        self.assertEqual(recovered_text, fixture_text(3))
        self.assertEqual(subject.analyze_stash_hss(recovered, XOR_KEY).status, "healthy")

    def test_garbled_localns_profile_recovers_known_7_and_8_shape(self):
        raw = garbled_profile_hss(1)
        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        self.assertEqual(plan.status, "recoverable")
        self.assertEqual(plan.profile, "stash_localns_unique_garbled_v1")
        recovered = subject.materialize_recovery(raw, plan, XOR_KEY)
        self.assertEqual(subject.decode_hss_bytes_strict(recovered, XOR_KEY), fixture_text(1))

    def test_exact_terminal_sentinel_alone_is_recoverable(self):
        decoded = wide_text(fixture_text(0)) + b"\x00\x00\xff\xff"
        raw = pack_decoded(decoded, outer_nul=True)
        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        self.assertEqual(plan.status, "recoverable")
        self.assertEqual(plan.profile, "stash_terminal_sentinel_v1")
        self.assertEqual(len(plan.changes), 1)
        recovered = subject.materialize_recovery(raw, plan, XOR_KEY)
        self.assertEqual(subject.decode_hss_bytes_strict(recovered, XOR_KEY), fixture_text(0))

    def test_manifest_and_item_counts_are_preserved(self):
        expected = subject.analyze_stash_hss(healthy_hss(4), XOR_KEY)
        plan = subject.analyze_stash_hss(blank_profile_hss(4), XOR_KEY)
        recovered = subject.analyze_stash_hss(
            subject.materialize_recovery(blank_profile_hss(4), plan, XOR_KEY), XOR_KEY
        )

        self.assertEqual(plan.item_count, expected.item_count)
        self.assertEqual(plan.item_manifest_sha256, expected.item_manifest_sha256)
        self.assertEqual(recovered.item_manifest_sha256, expected.item_manifest_sha256)

    def test_source_hash_change_is_rejected(self):
        raw = blank_profile_hss()
        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        changed = raw[:-1]
        with self.assertRaisesRegex(subject.HSSRecoveryError, "changed"):
            subject.materialize_recovery(changed, plan, XOR_KEY)

    def test_unknown_high_byte_is_not_silently_zeroed(self):
        raw = blank_profile_hss(extra_high=2)
        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        self.assertEqual(plan.status, "unsupported")
        self.assertIsNone(plan.recovered_text)

    def test_missing_or_changed_terminal_fingerprint_is_unsupported(self):
        text = fixture_text().replace(UNIQUE, EMPTY, 1)
        wide = wide_text(text)
        closing_quote = text.index(EMPTY) + EMPTY.rfind('"')
        wide[closing_quote * 2 + 1] = 0x08

        missing = subject.analyze_stash_hss(pack_decoded(wide), XOR_KEY)
        changed = subject.analyze_stash_hss(
            pack_decoded(wide + b"\x00\x00\xfe\xff"), XOR_KEY
        )
        self.assertEqual(missing.status, "unsupported")
        self.assertEqual(changed.status, "unsupported")

    def test_odd_decoded_length_is_unsupported(self):
        plan = subject.analyze_stash_hss(pack_decoded(b"abc"), XOR_KEY)
        self.assertEqual(plan.status, "unsupported")
        self.assertIn("odd", plan.diagnostics[0])

    def test_invalid_base64_zlib_and_multistream_are_unsupported(self):
        self.assertEqual(subject.analyze_stash_hss(b"!!", XOR_KEY).status, "unsupported")
        invalid_zlib = base64.b64encode(b"not-zlib")
        self.assertEqual(subject.analyze_stash_hss(invalid_zlib, XOR_KEY).status, "unsupported")

        xored = bytes(
            value ^ XOR_KEY[index % len(XOR_KEY)]
            for index, value in enumerate(wide_text(fixture_text()))
        )
        multistream = base64.b64encode(zlib.compress(xored) + zlib.compress(b"extra"))
        self.assertEqual(subject.analyze_stash_hss(multistream, XOR_KEY).status, "unsupported")

    def test_embedded_or_repeated_outer_nul_is_unsupported(self):
        healthy = healthy_hss()
        embedded = healthy[:8] + b"\x00" + healthy[8:]
        repeated = healthy + b"\x00\x00"
        self.assertEqual(subject.analyze_stash_hss(embedded, XOR_KEY).status, "unsupported")
        self.assertEqual(subject.analyze_stash_hss(repeated, XOR_KEY).status, "unsupported")

    def test_duplicate_json_key_and_nonfinite_number_are_unsupported(self):
        text = fixture_text()
        duplicate = text[:-1] + ',"stash_reset":0.0}'
        self.assertEqual(
            subject.analyze_stash_hss(pack_decoded(wide_text(duplicate)), XOR_KEY).status,
            "unsupported",
        )

        document = fixture_document()
        document["stash_reset"] = math.nan
        nonfinite = json.dumps(document, separators=(",", ":"))
        self.assertEqual(
            subject.analyze_stash_hss(pack_decoded(wide_text(nonfinite)), XOR_KEY).status,
            "unsupported",
        )

    def test_wrong_root_or_duplicate_unique_row_is_unsupported(self):
        wrong = fixture_document()
        wrong.pop("stash_tab_19")
        wrong_raw = pack_decoded(wide_text(json.dumps(wrong, separators=(",", ":"))))
        self.assertEqual(subject.analyze_stash_hss(wrong_raw, XOR_KEY).status, "unsupported")

        duplicate = fixture_document()
        duplicate["stash_tab_data"]["LocalNS"].append({"tab": -5.0, "name": "Unique"})
        duplicate_raw = pack_decoded(wide_text(json.dumps(duplicate, separators=(",", ":"))))
        self.assertEqual(subject.analyze_stash_hss(duplicate_raw, XOR_KEY).status, "unsupported")

    def test_extreme_localns_number_fails_closed_without_overflow(self):
        document = fixture_document()
        document["stash_tab_data"]["LocalNS"].append({
            "tab": 10 ** 400,
            "name": "Invalid",
        })
        raw = pack_decoded(wide_text(json.dumps(document, separators=(",", ":"))))

        plan = subject.analyze_stash_hss(raw, XOR_KEY)

        self.assertEqual(plan.status, "unsupported")
        self.assertIn("invalid tab number", plan.diagnostics[0])

    def test_independent_stash_metadata_corruption_blocks_known_profile(self):
        bad_reset = fixture_document()
        bad_reset["stash_reset"] = "garbage"
        reset_plan = subject.analyze_stash_hss(
            blank_profile_document_hss(bad_reset), XOR_KEY
        )

        bad_namespace = fixture_document()
        bad_namespace["stash_tab_data"]["NH"].append({
            "tab": 0.0,
            "name": "Duplicate",
        })
        namespace_plan = subject.analyze_stash_hss(
            blank_profile_document_hss(bad_namespace), XOR_KEY
        )

        self.assertEqual(reset_plan.status, "unsupported")
        self.assertIsNone(reset_plan.recovered_text)
        self.assertEqual(namespace_plan.status, "unsupported")
        self.assertIsNone(namespace_plan.recovered_text)

    def test_inflate_limit_fails_closed(self):
        raw = healthy_hss(2)
        with mock.patch.object(subject, "MAX_HSS_DECODED_BYTES", 32):
            plan = subject.analyze_stash_hss(raw, XOR_KEY)
        self.assertEqual(plan.status, "unsupported")
        self.assertIn("size limit", plan.diagnostics[0])

    def test_random_extra_high_bytes_never_match_a_recovery_profile(self):
        text = fixture_text(1)
        for unit in (0, 1, 2, 5, 10, 20, 40, 80, len(text) // 2, len(text) - 2):
            wide = wide_text(text)
            wide[unit * 2 + 1] = 1
            wide += b"\x00\x00\xff\xff"
            with self.subTest(unit=unit):
                self.assertEqual(
                    subject.analyze_stash_hss(pack_decoded(wide), XOR_KEY).status,
                    "unsupported",
                )


if __name__ == "__main__":
    unittest.main()
