r"""Check the tag an item editor release is being cut as, and compose its notes.

Run it:

    py tools/editor_tag.py --tag v2.15.5 --existing v2.15.4
    py tools/editor_tag.py --compose-notes --version 2.15.5 --previous v2.15.4 \
        --generated generated.md --out release-notes.md

A port of ForgePact's `tools/forgepact_tag.py`, which carries the full
reasoning; only what differs is written down here.

**Plan mode** (the default) prints four `key=value` lines for `$GITHUB_OUTPUT`
and nothing else:

    version=2.15.5
    tag=v2.15.5
    bump=true
    previous=v2.15.4

and refuses, exiting non-zero having printed nothing, a tag that is not three
plain numbers (one optional lowercase `v`), a tag that already exists, a
version below the highest existing `v*` tag (it would point `releases/latest`
backwards), and a version below what `master`'s `APP_VERSION` already holds
(it would relabel newer code as an older version). Tags are compared
numerically, never as text: `v2.8.2` is older than `v2.15.0`.

**Notes composition** (`--compose-notes`) builds the draft release body. The
tagged version's own `RELEASE_NOTES_vX.Y.Z.md` comes first, or, when nobody has
written it, GitHub's generated notes under a banner demanding a rewrite,
because players read this text in the Toolkit Hub. Any version between the
previous tag and this one that has a notes file but was never released on its
own (v2.11.2, 2.11.3, 2.13.x were) follows, newest first, so its notes are not
silently dropped.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cut_release  # noqa: E402  (sibling module)

ROOT = Path(__file__).resolve().parent.parent

PREFIX = "v"
PRODUCT = "Hero Siege Item Editor"

SHAPE = cut_release.VERSION

NOTES_NAME = re.compile(r"^RELEASE_NOTES_v(?P<version>.+)\.md$")

BANNER = (
    "> **Draft notes, generated from pull request titles.** Rewrite the "
    "{product} {version} section for players before publishing: the Toolkit "
    "Hub shows this text to players."
)


class Plan(NamedTuple):
    version: str
    tag: str
    bump: bool
    previous: str


class NotesPlan(NamedTuple):
    top_from_file: bool
    skipped: List[str]


def normalise(raw: str) -> str:
    """The bare version out of a typed tag, or SystemExit."""
    version = raw.strip()
    if version.startswith(PREFIX):
        version = version[len(PREFIX):]
    if not SHAPE.match(version):
        raise SystemExit(
            f"{raw.strip()!r} is not a version this can tag. "
            f"Give three numbers, as 1.2.3 or {PREFIX}1.2.3."
        )
    return version


def tag_names(refs: Iterable[str]) -> set:
    """Tag names out of `git ls-remote --tags` refs or bare `git tag` names."""
    names = set()
    for ref in refs:
        name = ref.strip()
        if not name:
            continue
        name = name.rsplit("refs/tags/", 1)[-1]
        if name.endswith("^{}"):
            name = name[: -len("^{}")]
        names.add(name)
    return names


def as_numbers(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def plan(raw: str, refs: Iterable[str], tree: str) -> Plan:
    version = normalise(raw)
    tag = PREFIX + version
    taken = tag_names(refs)
    if tag in taken:
        raise SystemExit(
            f"{tag} already exists. Pick a higher version, or delete that tag "
            f"and its release first."
        )

    released = [
        name for name in taken if name.startswith(PREFIX) and SHAPE.match(name[len(PREFIX):])
    ]
    highest = None
    if released:
        highest = max(released, key=lambda name: as_numbers(name[len(PREFIX):]))
        if as_numbers(version) < as_numbers(highest[len(PREFIX):]):
            raise SystemExit(
                f"{tag} is behind {highest}, which is already tagged. Publishing "
                f"it would point releases/latest at an older version."
            )

    if as_numbers(version) < as_numbers(tree):
        raise SystemExit(
            f"{tag} is behind the tree, which is already at {tree}. Tagging it "
            f"would relabel {tree}'s code as {version}."
        )

    return Plan(version, tag, bump=tree != version, previous=highest or "")


def note_versions(names: Iterable[str]) -> List[str]:
    versions = []
    for name in names:
        match = NOTES_NAME.match(name)
        if match and SHAPE.match(match.group("version")):
            versions.append(match.group("version"))
    return versions


def notes_plan(available: Iterable[str], version: str, previous: str) -> NotesPlan:
    available = list(available)
    top_from_file = version in available
    skipped = []
    if previous:
        bound = as_numbers(previous[len(PREFIX):] if previous.startswith(PREFIX) else previous)
        top = as_numbers(version)
        skipped = [v for v in available if v != version and bound < as_numbers(v) < top]
    # With no previous tag there is no lower bound, and walking the whole
    # history back would bury the release meant to be at the top.
    skipped.sort(key=as_numbers, reverse=True)
    return NotesPlan(top_from_file, skipped)


def _normalise_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def _english_list(items: List[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _labelled(text: str, version: str) -> str:
    """Prepend a `# <product> <v>` heading unless the section starts with one."""
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            return text
        break
    return f"# {PRODUCT} {version}\n\n{text}"


def compose_body(
    version: str,
    top_text: Optional[str],
    generated: str,
    skipped: List[Tuple[str, str]],
) -> Tuple[str, str]:
    preamble: List[str] = []
    if top_text is not None:
        sections = [_labelled(_normalise_text(top_text), version)]
    else:
        sections = [f"# {PRODUCT} {version}\n\n{_normalise_text(generated)}"]
        preamble.append(BANNER.format(product=PRODUCT, version=version))

    if skipped:
        preamble.append(
            f"This release also carries the notes for "
            f"{_english_list([v for v, _ in skipped])}, which were never released "
            f"on their own."
        )
        sections += [_labelled(_normalise_text(text), v) for v, text in skipped]

    body = "\n\n---\n\n".join(sections)
    if preamble:
        body = "\n\n".join(preamble) + "\n\n" + body

    if top_text is not None:
        source = "files" if skipped else "file"
    else:
        source = "mixed" if skipped else "generated"
    return body.strip() + "\n", source


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _compose_notes_main(args: argparse.Namespace) -> int:
    version = normalise(args.version)
    root: Path = args.root
    notes = notes_plan(note_versions(p.name for p in root.iterdir()), version, args.previous or "")

    top_text = None
    if notes.top_from_file:
        top_text = _read_text(root / cut_release.RELEASE_NOTES.format(version=version))
    skipped = [
        (v, _read_text(root / cut_release.RELEASE_NOTES.format(version=v))) for v in notes.skipped
    ]
    body, source = compose_body(version, top_text, _read_text(Path(args.generated)), skipped)
    Path(args.out).write_bytes(body.encode("utf-8"))

    print(f"source={source}")
    print(f"versions={' '.join([version] + notes.skipped)}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="check the tag an item editor release is being cut as, or compose its notes",
    )
    parser.add_argument("--tag", help="the tag to cut, as typed")
    parser.add_argument("--tree", default=None, help="the version the tree holds (default: read APP_VERSION)")
    parser.add_argument("--existing", nargs="*", default=[], help="tags that already exist, as names or refs")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--compose-notes", action="store_true", help="compose a release body instead")
    parser.add_argument("--version", help="the version being tagged (compose-notes mode)")
    parser.add_argument("--previous", default=None, help="the previous v* tag, or empty (compose-notes mode)")
    parser.add_argument("--generated", help="GitHub's generated notes (compose-notes mode)")
    parser.add_argument("--out", help="where to write the composed body (compose-notes mode)")
    args = parser.parse_args(argv)

    if args.compose_notes:
        if not args.version or not args.generated or not args.out:
            parser.error("--compose-notes needs --version, --generated and --out")
        if args.tag or args.existing:
            parser.error("--compose-notes does not take --tag or --existing")
        return _compose_notes_main(args)

    if args.version or args.generated or args.out:
        parser.error("--version/--generated/--out are only for --compose-notes")
    if not args.tag:
        parser.error("give --tag, or --compose-notes")

    chosen = plan(args.tag, args.existing, args.tree or cut_release.current(args.root))
    # Bare lines, appended straight to $GITHUB_OUTPUT. Nothing is printed on a
    # refusal, so a workflow that ignored the exit code still has no version.
    print(f"version={chosen.version}")
    print(f"tag={chosen.tag}")
    print(f"bump={'true' if chosen.bump else 'false'}")
    print(f"previous={chosen.previous}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
