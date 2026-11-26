"""A compound record as the files chemistry tools read: a Molfile, and an SDF (ADR 0229).

These are pure functions of what a record held at one revision, gathered by
:mod:`iki_server.entities.exports`, so the same revision is always the same file. Iki
doesn't read chemistry here: the structure is the Molfile the compound keeps, which a
worker computed from its SMILES with RDKit (2D coordinates, kekulized, V2000, or V3000
where V2000 can't hold it). It's written as BIOVIA's CTfile Formats (2020) set out:

- A Molfile is its header block, then its connection table to ``M  END``. The header's
  first line is the record's name, cut to the 80 characters it holds, and its third, a
  comment, says which record and revision it is. The second, which names the program
  that wrote the connection table and its dimensions, is kept as that program wrote it.
- An SDF record is a Molfile, then its data items, then ``$$$$``. Each item is a header
  line, ``>  <name>``, its value on the lines after, and a blank line. Its first items
  say which record it is (``IKI_HRID``, ``IKI_UUID``, ``IKI_REVISION``); then come the
  fields chosen, by their names, which a field name SD can hold always is (a letter, then
  letters, digits and underscores); then ``IKI_NOTES``, what the file couldn't hold.

A blank line ends a value, so a value's blank lines are left out, and a line that would
read as the record's end (``$$$$``) or another item's header (``>``) can't be in one: the
field is left out instead, and the notes say so. The CTfile Formats cap a data line at
200 characters, but a long SMILES or InChI is one line wherever it's read: RDKit and
PubChem write it whole, and reading it back joins a value's lines with line breaks, so
Iki writes it whole too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from iki_server.entities.sequence_files import Written

__all__ = ["CompoundFile", "NotAMolfile", "molfile", "sdf"]

#: What a Molfile's header line holds.
HEADER_WIDTH = 80
#: A counts line's version, as the last of its columns says it.
_VERSION = re.compile(r"\bV(2000|3000)\s*$")


class NotAMolfile(ValueError):  # noqa: N818 - reads as "not a Molfile"
    """A compound's structure that isn't a Molfile Iki can write, and why."""


@dataclass(frozen=True)
class CompoundFile:
    """What a compound record held at one revision, as its files write it."""

    hrid: str
    uuid: str
    name: str
    revision: int
    structure: str  # its Molfile, as its type's structure field holds it
    #: The fields chosen for an SDF's data, as text, in order: (field name, value).
    data: tuple[tuple[str, str], ...] = ()
    latest: bool = True


def _lines(record: CompoundFile) -> tuple[list[str], list[str]]:
    """Its Molfile's lines, its header block rewritten, and what it said of them."""
    lines = record.structure.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    end = next((i for i, line in enumerate(lines) if line.startswith("M  END")), None)
    if len(lines) < 4 or end is None or end != len(lines) - 1:
        raise NotAMolfile("its structure isn't a Molfile: it doesn't end with M  END")
    if not _VERSION.search(lines[3]):
        raise NotAMolfile("its structure isn't a Molfile: its counts line says no V2000 or V3000")
    if any(line == "$$$$" for line in lines):
        raise NotAMolfile("its structure holds a $$$$ line, which would end its SDF record")
    said: list[str] = []
    name = " ".join(record.name.split())
    if len(name) > HEADER_WIDTH:
        said.append(
            f"Its name is cut to the {HEADER_WIDTH} characters a Molfile's first line holds: "
            f"{name}."
        )
        name = name[:HEADER_WIDTH].rstrip()
    which = f"revision {record.revision}" + ("" if record.latest else ", not its latest")
    comment = f"Exported from Iki: {record.hrid} ({which})."[:HEADER_WIDTH]
    return [name, lines[1], comment, *lines[3:]], said


def molfile(record: CompoundFile) -> Written:
    """The compound's Molfile, named and said to be this record at this revision."""
    lines, said = _lines(record)
    return Written(("\n".join(lines) + "\n").encode("utf-8"), said)


def sdf(record: CompoundFile) -> Written:
    """One SDF record: its Molfile, which record it is, the fields chosen, and notes."""
    lines, said = _lines(record)
    items = [
        ("IKI_HRID", record.hrid),
        ("IKI_UUID", record.uuid),
        ("IKI_REVISION", str(record.revision)),
    ]
    for name, value in record.data:
        held = [line for line in value.replace("\r\n", "\n").split("\n") if line.strip()]
        if any(line == "$$$$" or line.startswith(">") for line in held):
            said.append(
                f"Left out its {name}: a line of it would read as the end of the record or "
                "another item's header."
            )
            continue
        if len(held) < len(value.strip().split("\n")):
            said.append(
                f"Its {name}'s blank lines are left out: a blank line ends an SD item's value."
            )
        items.append((name, "\n".join(line.rstrip() for line in held)))
    if said:
        items.append(("IKI_NOTES", "\n".join(said)))
    for name, value in items:
        lines += [f">  <{name}>", *([value] if value else []), ""]
    lines.append("$$$$")
    return Written(("\n".join(lines) + "\n").encode("utf-8"), said)
