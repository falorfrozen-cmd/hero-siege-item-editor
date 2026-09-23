r"""The two small pieces `editor-release.yml` needs: normalise the typed tag,
and turn PyInstaller's output into the release assets.

Run it:

    py tools/release_ci.py tag --tag v2.15.5
    py tools/release_ci.py package --root . --out dist_out

**`tag`** normalises a typed version exactly as `tools/editor_tag.py` does and
prints `tag=` / `version=` for `$GITHUB_OUTPUT`. It does not refuse a taken tag
or compare against the tree; that is the tag workflow's job.

**`package`** copies `dist/HeroSiegeItemEditor.exe` to
`HeroSiegeItemEditor-v<APP_VERSION>.exe` (`HeroSiegeItemEditor-v2.15.4-s10.exe`)
and writes `<that name>.sha256` next to it, in the shape every release since
v2.8 has used: `<hash> *<name>`, no trailing newline. The hub's
`catalog/sources.toml` matches the exe by `^HeroSiegeItemEditor-v[0-9][^/]*\.exe$`
and verifies it against the published `.sha256`, so neither name may drift.
The last line printed is the exe's sha256.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cut_release  # noqa: E402
import editor_tag  # noqa: E402

BUILT_EXE = Path("dist") / "HeroSiegeItemEditor.exe"


def asset_name(app_version: str) -> str:
    return f"HeroSiegeItemEditor-v{app_version}.exe"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_tag(raw: str) -> int:
    try:
        version = editor_tag.normalise(raw)
    except SystemExit as refusal:
        print(refusal, file=sys.stderr)
        return 1
    print(f"tag={editor_tag.PREFIX}{version}")
    print(f"version={version}")
    return 0


def cmd_package(root: Path, out: Path) -> int:
    built = root / BUILT_EXE
    if not built.is_file():
        print(f"{built} does not exist -- run PyInstaller first", file=sys.stderr)
        return 1

    name = asset_name(cut_release.app_version(root))
    out.mkdir(parents=True, exist_ok=True)
    target = out / name
    shutil.copyfile(built, target)

    digest = sha256_file(target)
    (out / f"{name}.sha256").write_bytes(f"{digest} *{name}".encode("ascii"))

    print(f"asset={name}")
    print(f"size={target.stat().st_size}")
    print(digest)
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_tag = sub.add_parser("tag", help="normalise a typed tag/version")
    p_tag.add_argument("--tag", required=True)

    p_pkg = sub.add_parser("package", help="name and checksum the built exe")
    p_pkg.add_argument("--root", type=Path, default=cut_release.ROOT)
    p_pkg.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.command == "tag":
        return cmd_tag(args.tag)
    return cmd_package(args.root, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
