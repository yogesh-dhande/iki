"""Every relative link in the repository's Markdown goes to a file that's there: ADRs,
READMEs and the spec cite each other by path, and a renamed file breaks them silently."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: Inline links, excluding images. Angle-bracket destinations can contain spaces and parentheses.
LINK = re.compile(r'(?<!!)\[[^\]\n]*\]\((?:<([^<>\r\n]*)>|([^)\s]+))(?:\s+"[^"]*")?\)')
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


def broken(path: Path, *, root: Path = ROOT) -> list[str]:
    text = CODE.sub("", FENCE.sub("", path.read_text(encoding="utf-8")))
    found = []
    for bracketed, bare in LINK.findall(text):
        target = bracketed or bare
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith(("#", "//")):
            continue  # a URL, or a place in this file
        file = target.split("#", 1)[0]
        resolved = root / file.lstrip("/") if file.startswith("/") else path.parent / file
        if not resolved.resolve().is_relative_to(root):
            continue  # GitHub's own pages, such as ../../issues/new from the root
        if not resolved.exists():
            found.append(target)
    return found


def test_angle_bracket_link_destinations(tmp_path: Path) -> None:
    page = tmp_path / "guide.md"
    (tmp_path / "Guide (v2).md").touch()
    page.write_text(
        "[Article](<https://doi.org/10.1016/0076-6879(89)80108-9>)\n"
        "[Elsewhere](<//example.com/path>)\n"
        '[Guide](<Guide (v2).md#example> "Guide")\n'
        "[Missing](<Missing guide (v2).md>)\n"
    )
    assert broken(page, root=tmp_path) == ["Missing guide (v2).md"]


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


def test_the_adr_index_lists_each_decision_once() -> None:
    index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    numbers = re.findall(r"^\| \[(\d{4})\]", index, re.MULTILINE)
    repeated = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not repeated, f"docs/adr/README.md lists these more than once: {repeated}"
    assert numbers == sorted(numbers), "docs/adr/README.md lists decisions out of order"
    separators = re.findall(r"^\| -+ +\|", index, re.MULTILINE)
    assert len(separators) == 1, "docs/adr/README.md's index needs exactly one header row"
