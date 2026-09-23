"""Move the item editor's version, and check that it agrees with a tag.

Run it:

    py tools/cut_release.py 2.15.5
    py tools/cut_release.py --check
    py tools/cut_release.py --check --expect 2.15.5

The version lives in one place: `APP_VERSION` in `hs_item_editor_gui.py`,
written as `<X.Y.Z>-<season>` (`2.15.4-s10`). The release tag is `vX.Y.Z` and
the release asset is `HeroSiegeItemEditor-v<APP_VERSION>.exe`, so this script
moves only the three numbers and keeps the season suffix as it is. A new
season is a deliberate edit to that line, not a side effect of a release.

`--check` also requires `RELEASE_NOTES_v<X.Y.Z>.md` to exist, because the
README links every version's notes file and a release without one leaves a
gap in that history. `--allow-missing-notes` relaxes that for the tag
workflow only, which composes the draft body itself and falls back to
generated notes under a banner.

Modelled on ForgePact's `tools/cut_release.py`. This one deliberately does not
touch git, does not build and does not package anything.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent

VERSION_FILE = "hs_item_editor_gui.py"

# Each component is `0` or an ASCII number that does not start with one. `\d`
# would accept non-ASCII digits that no downstream tool parses.
VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")

# The whole assignment, anchored at line start: the file is 10,000 lines long
# and full of other version-looking strings. `\r?` because the worktree is CRLF.
APP_VERSION_LINE = re.compile(
    rb'(?m)^(APP_VERSION = ")'
    rb"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    rb'(?P<suffix>-[a-z0-9]+)?(")(?=\r?\n)'
)

RELEASE_NOTES = "RELEASE_NOTES_v{version}.md"


def _read(root: Path) -> Tuple[str, str, int]:
    """(version, suffix, number of matching lines) from `APP_VERSION`."""
    blob = (root / VERSION_FILE).read_bytes()
    found = list(APP_VERSION_LINE.finditer(blob))
    if not found:
        raise SystemExit(f"{VERSION_FILE} has no APP_VERSION line this can read")
    first = found[0]
    return (
        first.group("version").decode(),
        (first.group("suffix") or b"").decode(),
        len(found),
    )


def current(root: Path) -> str:
    """The X.Y.Z part of `APP_VERSION`."""
    return _read(root)[0]


def app_version(root: Path) -> str:
    """The whole `APP_VERSION` string, suffix included (`2.15.4-s10`)."""
    version, suffix, _ = _read(root)
    return version + suffix


def check(
    root: Path, expect: str | None, notes_required: bool = True
) -> Tuple[bool, List[str]]:
    version, suffix, hits = _read(root)
    ok = True
    lines = []

    if hits == 1:
        lines.append(f"  ok      {version}  {VERSION_FILE} -- APP_VERSION = \"{version}{suffix}\"")
    else:
        ok = False
        lines.append(
            f"  MISSING {version}  {VERSION_FILE} -- {hits} APP_VERSION lines; "
            "expected exactly one"
        )

    if not VERSION.match(version):
        ok = False
        lines.append(f"  INVALID {version}  not three plain numbers")

    notes = root / RELEASE_NOTES.format(version=version)
    if notes.is_file():
        lines.append(f"  ok      {version}  {notes.name} -- the release notes exist")
    elif notes_required:
        ok = False
        lines.append(f"  MISSING {version}  {notes.name} -- write the release notes")
    else:
        lines.append(
            f"  NOTE    {version}  {notes.name} -- missing; the tag workflow falls "
            "back to generated notes"
        )

    if expect is not None and expect != version:
        ok = False
        lines.append(f"  MISMATCH the tree says {version}, expected {expect}")

    return ok, lines


def cut(root: Path, new: str) -> List[str]:
    """Rewrite `APP_VERSION`, in binary so the CRLF endings survive."""
    if not VERSION.match(new):
        raise SystemExit(f"{new!r} is not a three-part version like 1.2.3")

    here, suffix, hits = _read(root)
    if hits != 1:
        raise SystemExit(
            f"{VERSION_FILE} has {hits} APP_VERSION lines, so it cannot be bumped safely"
        )
    if here == new:
        raise SystemExit(f"already at {new}")

    path = root / VERSION_FILE
    blob = path.read_bytes()
    blob, count = APP_VERSION_LINE.subn(
        lambda m: m.group(1) + new.encode() + (m.group("suffix") or b"") + m.group(4),
        blob,
    )
    if count != 1:
        raise SystemExit(f"{VERSION_FILE}: expected one match, found {count}")
    path.write_bytes(blob)

    done = [f"  {here}{suffix} -> {new}{suffix}  {VERSION_FILE}"]
    notes = root / RELEASE_NOTES.format(version=new)
    if not notes.is_file():
        done.append(f"  NOTE  {notes.name} does not exist yet - write it before releasing")
    return done


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="move the item editor's APP_VERSION, or check it. Does not "
                    "touch git, build or package anything.",
    )
    parser.add_argument("version", nargs="?", help="the new version, e.g. 2.15.5")
    parser.add_argument("--check", action="store_true", help="report the version and fail on a problem")
    parser.add_argument("--expect", default=None, help="with --check, also require this version")
    parser.add_argument(
        "--allow-missing-notes",
        action="store_true",
        help="with --check, don't fail when the release notes file is missing (tag workflow only)",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to act on")
    args = parser.parse_args(argv)

    if args.allow_missing_notes and not args.check:
        parser.error("--allow-missing-notes only makes sense with --check")

    if args.check:
        ok, lines = check(args.root, args.expect, notes_required=not args.allow_missing_notes)
        print(f"Item editor version: {app_version(args.root)}")
        print("\n".join(lines))
        if not ok:
            print("ERROR: the item editor's version is not releasable as asked", file=sys.stderr)
            return 1
        return 0

    if not args.version:
        parser.error("give a version to cut, or --check")

    for line in cut(args.root, args.version):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
