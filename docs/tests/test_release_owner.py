"""Everything that fetches, verifies or links to Iki's releases names the GitHub account
that hosts the repository, and only that one: an image, chart, download or checkout
under any other account doesn't exist. When the repository moves, change OWNER and the
places this lists."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNER = "yogesh-dhande"
#: ``ghcr.io/<owner>/iki-server``, ``iki-worker``, a first-party plugin's image such as
#: ``iki-protein-structure-tools``, or ``charts/iki`` (a deployment's own images are under
#: its own account), ``github.com/<owner>/iki``, a
#: workflow's ``repository: <owner>/iki``, and the shell examples' ``OWNER=<owner>``.
NAMED = re.compile(
    r"ghcr\\?\.io/(?P<a>[\w.-]+)/(?:iki-server|iki-worker|iki-protein-structure-tools|charts/iki)\b(?!-)"
    r"|github\\?\.com/(?P<b>[\w.-]+)/iki\b(?![\w-])"
    r"|repository: (?P<c>[\w.-]+)/iki\b"
    r"|\bOWNER=(?P<d>[\w.-]+)"
)
#: Decisions record what was decided then; they aren't changed afterwards.
HISTORY = "docs/adr/"


def tracked() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607 - git from the PATH
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    kept = (name for name in listed if not name.startswith(HISTORY))
    return [ROOT / name for name in kept if (ROOT / name).is_file()]


def owners() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in tracked():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in NAMED.finditer(text):
            owner = next(group for group in match.groups() if group)
            if not owner.startswith("$"):
                found.setdefault(owner, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_release_location_names_the_repositorys_owner() -> None:
    found = owners()
    others = {owner: sorted(paths) for owner, paths in found.items() if owner != OWNER}
    assert not others, others
    assert len(found.get(OWNER, ())) > 10
