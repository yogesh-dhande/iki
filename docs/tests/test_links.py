"""Every relative link in the repository's Markdown goes to a file that's there: ADRs,
READMEs and the spec cite each other by path, and a renamed file breaks them silently."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: ``[text](target)``, not images, and not inside inline code.
LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE = re.compile(r"`[^`\n]*`")
FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)


def markdown() -> list[Path]:
    """The Markdown files git tracks, or would: not dependencies' or build output."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],  # noqa: S607 - git from the PATH
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / name for name in listed if (ROOT / name).is_file()]


def broken(path: Path) -> list[str]:
    text = CODE.sub("", FENCE.sub("", path.read_text(encoding="utf-8")))
    found = []
    for target in LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue  # a URL, or a place in this file
        file = target.split("#", 1)[0]
        resolved = ROOT / file.lstrip("/") if file.startswith("/") else path.parent / file
        if not resolved.resolve().is_relative_to(ROOT):
            continue  # GitHub's own pages, such as ../../issues/new from the root
        if not resolved.exists():
            found.append(target)
    return found


def test_relative_links_go_to_files_that_are_there() -> None:
    files = markdown()
    assert len(files) > 50  # ADRs, READMEs, the spec
    problems = {str(p.relative_to(ROOT)): links for p in files if (links := broken(p))}
    assert problems == {}


def test_every_adr_is_in_the_index() -> None:
    adrs = ROOT / "docs" / "adr"
    index = (adrs / "README.md").read_text(encoding="utf-8")
    missing = [
        p.name
        for p in sorted(adrs.glob("[0-9][0-9][0-9][0-9]-*.md"))
        if p.name != "0000-template.md" and f"]({p.name})" not in index
    ]
    assert not missing, f"Add these to docs/adr/README.md: {missing}"
