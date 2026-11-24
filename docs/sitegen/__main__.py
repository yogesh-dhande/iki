"""``python -m sitegen generate`` before the site's build; ``check`` after it."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sitegen import calls, check, examples
from sitegen.repo import DIST, GENERATED, ORIGIN, REPOSITORY


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m sitegen")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("generate", help="Write what the site's pages read from the code")
    checking = commands.add_parser("check", help="Check a built site's links and policy")
    checking.add_argument("dist", nargs="?", type=Path, default=DIST)
    checking.add_argument("--origin", default=ORIGIN, help="Where the site is served")
    checking.add_argument("--repository", default=REPOSITORY)
    checking.add_argument("--frames", default="/viewers/", help="Viewers' frames, by path")
    given = parser.parse_args(argv)
    if given.command == "generate":
        for path in [*examples.write(), calls.write()]:
            sys.stdout.write(f"Wrote {path.relative_to(GENERATED)}\n")
        return 0
    site = check.Site(given.dist, given.origin, given.repository, given.frames)
    found = check.problems(site)
    for problem in found:
        sys.stderr.write(f"{problem}\n")
    if found:
        sys.stderr.write(f"The built site has {len(found)} problems.\n")
        return 1
    pages = len(list(site.dist.rglob("*.html")))
    sys.stdout.write(f"The built site's {pages} pages link only to what's there.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
