"""Tests for the release tooling in tools/ and the workflows that call it."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "tools"))

import cut_release  # noqa: E402
import editor_tag  # noqa: E402
import release_ci  # noqa: E402

WORKFLOWS = BASE / ".github" / "workflows"


def _tree(root: Path, version_line: str, notes=()) -> Path:
    (root / cut_release.VERSION_FILE).write_bytes(
        b'PORT = 8765\r\n' + version_line.encode() + b'\r\nAPPLICATION_ID = "x"\r\n'
    )
    for version in notes:
        (root / cut_release.RELEASE_NOTES.format(version=version)).write_text(
            f"notes for {version}\n", encoding="utf-8"
        )
    return root


class CutReleaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_real_tree_reads_as_three_numbers_plus_season(self):
        self.assertRegex(cut_release.current(BASE), cut_release.VERSION)
        self.assertTrue(cut_release.app_version(BASE).startswith(cut_release.current(BASE)))
        ok, lines = cut_release.check(BASE, None, notes_required=False)
        self.assertTrue(ok, "\n".join(lines))

    def test_cut_keeps_the_season_suffix_and_crlf(self):
        _tree(self.root, 'APP_VERSION = "2.15.4-s10"')
        cut_release.cut(self.root, "2.15.5")
        blob = (self.root / cut_release.VERSION_FILE).read_bytes()
        self.assertIn(b'APP_VERSION = "2.15.5-s10"\r\n', blob)
        self.assertNotIn(b"\n\n", blob.replace(b"\r\n", b"|"))
        self.assertEqual(cut_release.app_version(self.root), "2.15.5-s10")

    def test_cut_refuses_a_malformed_or_unchanged_version(self):
        _tree(self.root, 'APP_VERSION = "2.15.4-s10"')
        for bad in ("2.15", "02.15.5", "2.15.5-s11", "2.15.٥"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                cut_release.cut(self.root, bad)
        with self.assertRaises(SystemExit):
            cut_release.cut(self.root, "2.15.4")

    def test_check_requires_notes_unless_allowed(self):
        _tree(self.root, 'APP_VERSION = "2.15.4-s10"')
        self.assertFalse(cut_release.check(self.root, "2.15.4")[0])
        self.assertTrue(cut_release.check(self.root, "2.15.4", notes_required=False)[0])
        _tree(self.root, 'APP_VERSION = "2.15.4-s10"', notes=["2.15.4"])
        self.assertTrue(cut_release.check(self.root, "2.15.4")[0])

    def test_check_refuses_a_version_other_than_expected(self):
        _tree(self.root, 'APP_VERSION = "2.15.4-s10"', notes=["2.15.4"])
        self.assertFalse(cut_release.check(self.root, "2.15.5")[0])


class PlanTests(unittest.TestCase):
    EXISTING = ["refs/tags/v2.8.2", "refs/tags/v2.15.4", "refs/tags/v2.15.4^{}"]

    def test_next_version_bumps_and_names_previous_numerically(self):
        plan = editor_tag.plan("v2.15.5", self.EXISTING, "2.15.4")
        self.assertEqual(plan, editor_tag.Plan("2.15.5", "v2.15.5", True, "v2.15.4"))

    def test_tree_already_at_the_version_needs_no_bump(self):
        self.assertFalse(editor_tag.plan("2.15.5", self.EXISTING, "2.15.5").bump)

    def test_refusals(self):
        cases = {
            "taken": ("v2.15.4", "2.15.4"),
            "below highest tag": ("v2.9.0", "2.8.2"),
            "below the tree": ("v2.15.5", "2.15.6"),
            "shape": ("V2.15.5", "2.15.4"),
            "double v": ("vv2.15.5", "2.15.4"),
            "suffix": ("v2.15.5-s10", "2.15.4"),
        }
        for label, (tag, tree) in cases.items():
            with self.subTest(label), self.assertRaises(SystemExit):
                editor_tag.plan(tag, self.EXISTING, tree)

    def test_refusal_prints_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            editor_tag.main(["--tag", "v2.15.4", "--tree", "2.15.4", "--existing", *self.EXISTING])
        self.assertEqual(out.getvalue(), "")

    def test_foreign_tags_are_ignored(self):
        plan = editor_tag.plan("v1.0.0", ["refs/tags/hub-v9.0.0", "refs/tags/v3.0.0-rc1"], "1.0.0")
        self.assertEqual(plan.previous, "")


class NotesTests(unittest.TestCase):
    NAMES = [
        "RELEASE_NOTES_v2.11.4.md", "RELEASE_NOTES_v2.12.0.md", "RELEASE_NOTES_v2.13.0.md",
        "RELEASE_NOTES_v2.13.2.md", "RELEASE_NOTES_v2.15.0.md", "release-notes-v2.14.0.md",
        "RELEASE_NOTES_v2.14.0-rc1.md", "README.md",
    ]

    def test_only_accepted_file_names_count(self):
        self.assertEqual(
            sorted(editor_tag.note_versions(self.NAMES), key=editor_tag.as_numbers),
            ["2.11.4", "2.12.0", "2.13.0", "2.13.2", "2.15.0"],
        )

    def test_skipped_versions_are_between_previous_and_top_newest_first(self):
        plan = editor_tag.notes_plan(editor_tag.note_versions(self.NAMES), "2.15.0", "v2.12.0")
        self.assertEqual(plan, editor_tag.NotesPlan(True, ["2.13.2", "2.13.0"]))

    def test_no_previous_tag_means_nothing_skipped(self):
        self.assertEqual(editor_tag.notes_plan(["2.1.0", "2.2.0"], "2.2.0", "").skipped, [])

    def test_body_from_files(self):
        body, source = editor_tag.compose_body(
            "2.15.0", "# Hero Siege Item Editor 2.15.0\r\n\r\nNew.", "gen", [("2.13.2", "Old.")]
        )
        self.assertEqual(source, "files")
        self.assertLess(body.index("2.15.0"), body.index("# Hero Siege Item Editor 2.13.2"))
        self.assertNotIn("gen", body)
        self.assertNotIn("\r", body)

    def test_body_generated_carries_the_banner(self):
        body, source = editor_tag.compose_body("2.15.5", None, "* PR title", [])
        self.assertEqual(source, "generated")
        self.assertTrue(body.startswith("> **Draft notes"))
        self.assertIn("# Hero Siege Item Editor 2.15.5\n\n* PR title", body)

    def test_compose_notes_cli_writes_the_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp), 'APP_VERSION = "2.15.5-s10"', notes=["2.15.5"])
            generated = root / "generated.md"
            generated.write_text("gen", encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                editor_tag.main([
                    "--compose-notes", "--version", "v2.15.5", "--previous", "v2.15.4",
                    "--generated", str(generated), "--out", str(root / "body.md"), "--root", str(root),
                ])
            self.assertEqual(out.getvalue(), "source=file\nversions=2.15.5\n")
            self.assertIn("notes for 2.15.5", (root / "body.md").read_text(encoding="utf-8"))


class PackageTests(unittest.TestCase):
    def test_asset_name_and_checksum_match_published_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp), 'APP_VERSION = "2.15.4-s10"')
            (root / "dist").mkdir()
            (root / "dist" / "HeroSiegeItemEditor.exe").write_bytes(b"MZ fake")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(release_ci.cmd_package(root, root / "out"), 0)
            name = "HeroSiegeItemEditor-v2.15.4-s10.exe"
            # The hub's catalog/sources.toml asset_pattern.
            self.assertRegex(name, r"^HeroSiegeItemEditor-v[0-9][^/]*\.exe$")
            digest = release_ci.sha256_file(root / "out" / name)
            self.assertEqual((root / "out" / f"{name}.sha256").read_bytes(), f"{digest} *{name}".encode())
            self.assertEqual(out.getvalue().splitlines(), [f"asset={name}", "size=7", digest])

    def test_missing_exe_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp), 'APP_VERSION = "2.15.4-s10"')
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(release_ci.cmd_package(root, root / "out"), 1)

    def test_tag_normalises_like_the_tagger(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(release_ci.cmd_tag(" v2.15.5 "), 0)
        self.assertEqual(out.getvalue(), "tag=v2.15.5\nversion=2.15.5\n")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(release_ci.cmd_tag("2.15"), 1)


class WorkflowShapeTests(unittest.TestCase):
    """Text-level checks: no YAML dependency, so the suite stays stdlib-only."""

    def read(self, name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_release_workflows_run_only_from_master(self):
        # This repository's default branch is master, not main.
        for name in ("editor-tag.yml", "editor-release.yml"):
            text = self.read(name)
            with self.subTest(name):
                self.assertIn('if [ "$BRANCH" != "master" ]', text)
                self.assertNotRegex(text, r"HEAD:main|--ref main|ref: main")

    def test_typed_input_reaches_the_shell_only_through_env(self):
        for name in ("editor-tag.yml", "editor-release.yml"):
            for line in self.read(name).splitlines():
                if "${{ inputs.tag }}" in line and "group:" not in line:
                    with self.subTest(name=name, line=line):
                        self.assertRegex(line.strip(), r"^[A-Z_]+: \$\{\{ inputs\.tag \}\}$")

    def test_nothing_publishes(self):
        for name in ("editor-tag.yml", "editor-release.yml"):
            text = self.read(name)
            with self.subTest(name):
                self.assertNotIn("--draft=false", text)
                self.assertNotIn("--latest", text)
                self.assertNotIn("gh release edit", text)
        release = self.read("editor-release.yml")
        self.assertNotIn("gh release create", release)
        self.assertNotIn("gh workflow run", release)
        self.assertNotIn("actions: write", release)

    def test_release_build_checks_out_bytes_before_any_checkout(self):
        text = self.read("editor-release.yml")
        self.assertLess(text.index("core.autocrlf false"), text.index("actions/checkout"))

    def test_release_uploads_after_the_second_draft_guard(self):
        text = self.read("editor-release.yml")
        self.assertLess(text.index("(guard 2)"), text.index("gh release upload"))

    def test_tag_workflow_bumps_before_tagging(self):
        text = self.read("editor-tag.yml")
        self.assertLess(text.index("git push origin HEAD:master"), text.index('git tag -a "$TAG"'))
        self.assertIn("gh workflow run editor-release.yml --ref master", text)

    def test_ai_review_is_scoped_to_this_repository(self):
        text = self.read("ai-review.yml")
        self.assertEqual(text.count("github.repository == 'falorfrozen-cmd/hero-siege-item-editor'"), 2)
        self.assertNotIn("ForgePact'", text)
        self.assertNotRegex(text, re.compile(r"^on:\s*\n\s*pull_request:\s*\n\s*types: \[(?!labeled)", re.M))


if __name__ == "__main__":
    unittest.main()
