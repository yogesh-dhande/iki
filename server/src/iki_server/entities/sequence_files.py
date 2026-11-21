"""A sequence record as the files other tools read: GenBank and FASTA (ADR 0169).

These are pure functions of what a record held at one revision, gathered by
:mod:`iki_server.entities.exports`, so the same revision is always the same file.

GenBank is written as INSDC's flat file writes it (NCBI's GenBank release notes, section
3.4, and the INSDC Feature Table Definition, version 11.3):

- LOCUS in its columns: the name (the record's, or its HRID where the name isn't one
  LOCUS can hold), the length, ``ss-`` for a single-stranded molecule (plain ``DNA``
  for a double-stranded one, as NCBI writes most), the molecule, ``linear`` or
  ``circular``, and the revision's date. The division is left blank: it's an INSDC
  database's classification of a record it holds, which Iki doesn't know.
- DEFINITION, ACCESSION and VERSION (only when the record names an INSDC accession), and
  SOURCE and ORGANISM from its organism.
- COMMENT says it was exported from Iki, which record and revision, and what of the
  record GenBank can't say: a feature left out, and why.
- FEATURES: a ``source`` feature for its organism, then each feature with its location
  (its parts, joined or ordered, each on its strand, with partial ends and sites between
  two bases) and qualifiers. A part across a circle's origin is ``join(x..L,1..y)``; on a
  linear molecule nothing reads across its ends, so such a feature is left out, saying
  so. A feature's name goes in ``/label`` when no qualifier gives it, which is where
  SnapGene, Benchling and Geneious read a feature's name.
- ORIGIN, in lower case, sixty bases a line in groups of ten.

FASTA is a header and the bases: ``>HRID name description [topology=circular]``. The
first word is the identifier, as FASTA's is; ``[topology=…]`` is NCBI's source modifier,
which Iki's import reads back.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

#: INSDC's months, as LOCUS writes a date.
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
#: A name LOCUS can hold: 16 characters, none of them a space (NCBI's columns 13 to 28).
_LOCUS_NAME = re.compile(r"[!-~]{1,16}")
#: A feature key INSDC's table can hold: up to 15 characters (columns 6 to 20).
_KEY = re.compile(r"[A-Za-z0-9_'*-]{1,15}")
#: A qualifier's name, as INSDC writes them.
_QUALIFIER = re.compile(r"[A-Za-z0-9_]{1,40}")
#: Qualifiers INSDC writes without quotes: numbers, symbols and locations.
_UNQUOTED = frozenset(
    {
        "anticodon",
        "citation",
        "codon_start",
        "compare",
        "direction",
        "estimated_length",
        "mod_base",
        "number",
        "rpt_type",
        "rpt_unit_range",
        "tag_peptide",
        "transl_except",
        "transl_table",
    }
)
#: Qualifiers a feature may repeat whose values can't hold "; ": the import keeps a
#: repeated qualifier as its values joined by "; ", so these are written one a value again.
_REPEATED = frozenset({"db_xref", "EC_number", "old_locus_tag", "transl_except"})
#: The qualifiers a feature's name is read from on import, first to last.
_NAMED_BY = ("label", "gene", "locus_tag", "product")
WIDTH = 79
INDENT = " " * 21


@dataclass(frozen=True)
class Organism:
    """The taxon a record is from, as GenBank's source feature names it."""

    name: str
    taxon_id: int | None


@dataclass(frozen=True)
class SequenceFile:
    """What a record held at one revision, as a sequence file writes it."""

    hrid: str
    name: str
    bases: str
    topology: str  # linear or circular
    revision: int
    date: date
    molecule: str = "DNA"  # DNA or RNA
    strandedness: str | None = None  # double, single, or None if the type doesn't say
    description: str = ""
    accession: str = ""
    organism: Organism | None = None
    features: tuple[dict[str, Any], ...] = ()
    #: Where its sequence is another record's (a plasmid's sequence), that record's HRID.
    sequence_of: str = ""
    latest: bool = True


@dataclass
class Written:
    """A file's bytes, and what of the record it couldn't say."""

    content: bytes
    left_out: list[str] = field(default_factory=list)


# --- GenBank -------------------------------------------------------------------------------------


def genbank(record: SequenceFile) -> Written:
    left_out: list[str] = []
    length = len(record.bases)
    circular = record.topology == "circular"
    features = [
        written
        for feature in record.features
        if (written := _feature(feature, length, circular=circular, left_out=left_out))
    ]
    lines = [_locus(record), *_wrapped("DEFINITION", _definition(record))]
    if record.accession:
        unversioned = record.accession.split(".", 1)[0]
        lines += [f"ACCESSION   {unversioned}", f"VERSION     {record.accession}"]
    organism = record.organism.name if record.organism and record.organism.name else "."
    lines += [
        "KEYWORDS    .",
        f"SOURCE      {organism}",
        f"  ORGANISM  {organism}",
        "            .",
    ]
    lines += _wrapped("COMMENT", _comment(record, left_out))
    lines.append("FEATURES             Location/Qualifiers")
    if record.organism is not None:
        lines += _source(record.organism, length)
    for feature in features:
        lines += feature
    lines.append("ORIGIN")
    lines += _origin(record.bases)
    lines.append("//")
    return Written(("\n".join(lines) + "\n").encode("utf-8"), left_out)


def _locus(record: SequenceFile) -> str:
    name = locus_name(record)
    stranded = "ss-" if record.strandedness == "single" else "   "
    day = record.date
    when = f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year:04d}"
    return (
        f"LOCUS       {name:<16} {len(record.bases):>11} bp {stranded}{record.molecule:<6}  "
        f"{record.topology:<8}     {when}"
    )


def locus_name(record: SequenceFile) -> str:
    """The record's name, its spaces as underscores, where LOCUS can hold it; else its
    HRID, which it always can."""
    name = "_".join(record.name.split())
    return name if _LOCUS_NAME.fullmatch(name) else record.hrid


def _definition(record: SequenceFile) -> str:
    text = " ".join(record.description.split())
    if not text:
        return "."
    return text if text.endswith(".") else f"{text}."


def _comment(record: SequenceFile, left_out: list[str]) -> list[str]:
    which = f"revision {record.revision}" + ("" if record.latest else ", not its latest")
    said = [f"Exported from Iki: {record.hrid} ({which})."]
    if record.sequence_of:
        said[0] = (
            f"Exported from Iki: {record.hrid} ({which}), whose sequence is {record.sequence_of}."
        )
    if locus_name(record) != "_".join(record.name.split()):
        said.append(f"Name: {' '.join(record.name.split())}.")
    strandless = sum(1 for f in record.features if (f.get("strand") or "none") == "none")
    if strandless:
        said.append(
            f"{strandless} feature{'s have' if strandless > 1 else ' has'} no strand in Iki, "
            "which GenBank can't say: written without complement(), they read as on the "
            "forward strand."
        )
    said += left_out
    return said


def _source(organism: Organism, length: int) -> list[str]:
    qualifiers: list[tuple[str, str]] = []
    if organism.name:
        qualifiers.append(("organism", organism.name))
    if organism.taxon_id is not None:
        qualifiers.append(("db_xref", f"taxon:{organism.taxon_id}"))
    return _feature_lines("source", f"1..{length}", qualifiers)


def _feature(
    feature: dict[str, Any], length: int, *, circular: bool, left_out: list[str]
) -> list[str] | None:
    kind = str(feature.get("type") or "misc_feature")
    named = str(feature.get("name") or "")
    what = f"a {kind}" + (f" ({named})" if named else "")
    try:
        location = location_of(feature, length, circular=circular)
    except _Unwritable as why:
        left_out.append(f"Left out {what}: {why}")
        return None
    qualifiers: list[tuple[str, str]] = []
    given = {
        str(k): "" if v is None else str(v) for k, v in (feature.get("qualifiers") or {}).items()
    }
    if not _KEY.fullmatch(kind):
        left_out.append(
            f"Written as a misc_feature: {what}, whose type GenBank's feature table can't "
            "hold as a key; its type is in its /note."
        )
        qualifiers.append(("note", f"Iki feature type: {kind}"))
        kind = "misc_feature"
    names = ("organism",) if kind == "source" else _NAMED_BY
    if named and not any(given.get(q) == named for q in names):
        qualifiers.append(("label", named))
    for name, value in given.items():
        if name == "location":
            continue  # Iki's copy of the location the import read, which is written above
        if not _QUALIFIER.fullmatch(name):
            left_out.append(f"Left out {what}'s qualifier {name!r}: GenBank can't name one so.")
            continue
        if name in _REPEATED:
            qualifiers += [(name, one) for one in value.split("; ")]
        else:
            qualifiers.append((name, value))
    return _feature_lines(kind, location, qualifiers)


class _Unwritable(ValueError):  # noqa: N818 - caught here, never raised beyond
    """A location GenBank can't write along this molecule."""


def location_of(feature: dict[str, Any], length: int, *, circular: bool) -> str:
    """A feature's INSDC location along a molecule of ``length`` bases: its parts in the
    order they're read (a reverse-strand feature's last first), or its start to its end."""
    parts = feature.get("parts") or [
        {"start": feature["start"], "end": feature["end"], "strand": feature.get("strand")}
    ]
    texts = []
    for part in parts:
        start, end = int(part["start"]), int(part["end"])
        if not (1 <= start <= length and 1 <= end <= length):
            raise _Unwritable(f"it lies beyond the sequence's {length:,} bases ({start}..{end}).")
        texts.append(_part(part, length, circular=circular))
    strands = {_strand(p) for p in parts}
    operator = "order" if feature.get("operator") == "order" else "join"
    if strands == {"reverse"}:
        # INSDC writes a reverse-strand feature as the complement of its parts low to high.
        inner = [_part({**p, "strand": "forward"}, length, circular=circular) for p in parts]
        inner.reverse()
        joined = _joined(operator, inner)
        return f"complement({joined})"
    pieces = [
        f"complement({text})" if _strand(p) == "reverse" else text
        for p, text in zip(parts, texts, strict=True)
    ]
    return _joined(operator, pieces)


def _joined(operator: str, pieces: list[str]) -> str:
    if len(pieces) == 1 and "," not in pieces[0]:
        return pieces[0]
    flat = [p for piece in pieces for p in _top_level(piece)] if operator == "join" else pieces
    return f"{operator}({','.join(flat)})"


def _top_level(text: str) -> list[str]:
    """A part across the origin is written as two ranges; joined, they're two of its parts."""
    if text.startswith("complement("):
        return [text]
    return text.split(",")


def _strand(part: dict[str, Any]) -> str:
    return "reverse" if part.get("strand") == "reverse" else "forward"


def _part(part: dict[str, Any], length: int, *, circular: bool) -> str:
    start, end = int(part["start"]), int(part["end"])
    early = "<" if part.get("partial_start") else ""
    late = ">" if part.get("partial_end") else ""
    if part.get("between"):
        if end == start + 1 or (circular and (start, end) == (length, 1)):
            return f"{start}^{end}"
        raise _Unwritable(f"a site between {start} and {end}, which aren't neighbours.")
    if start <= end:
        if start == end and not (early or late):
            return f"{start}"
        # A partial end on one base is written as a range, as NCBI writes it (21737..>21737).
        return f"{early}{start}..{late}{end}"
    if not circular:
        raise _Unwritable(
            f"it reads across the ends of a linear molecule ({start}..{end}), which only a "
            "circle's origin allows."
        )
    first = f"{early}{start}..{length}" if start < length else f"{early}{start}"
    second = f"1..{late}{end}" if end > 1 else f"{late}1"
    return f"{first},{second}"


def _feature_lines(kind: str, location: str, qualifiers: Iterable[tuple[str, str]]) -> list[str]:
    lines = _wrap_location(f"     {kind:<16}", location)
    for name, value in qualifiers:
        lines += _qualifier(name, value)
    return lines


def _wrap_location(lead: str, location: str) -> list[str]:
    """A location over lines, broken after commas, as INSDC breaks them."""
    lines, line = [], lead
    for piece in re.findall(r"[^,]*,|[^,]+$", location):
        if len(line) + len(piece) > WIDTH and line.strip() and line != lead:
            lines.append(line)
            line = INDENT
        line += piece
    lines.append(line)
    return lines


def _qualifier(name: str, value: str) -> list[str]:
    text = " ".join(value.split())
    if not text and not value:
        return [f"{INDENT}/{name}"]
    if name in _UNQUOTED and text and '"' not in text and " " not in text:
        body = f"/{name}={text}"
    else:
        body = f'/{name}="{text.replace(chr(34), chr(34) * 2)}"'
    if name == "translation":
        width = WIDTH - len(INDENT)
        return [INDENT + body[i : i + width] for i in range(0, len(body), width)]
    return [INDENT + line for line in _words(body, WIDTH - len(INDENT))]


def _words(text: str, width: int) -> Iterator[str]:
    """Text over lines of ``width``, broken only between words, so reading it back with a
    space at each break gives the same text. A word longer than a line has one to itself."""
    line = ""
    for word in text.split(" "):
        if line and len(line) + 1 + len(word) > width:
            yield line
            line = word
        else:
            line = f"{line} {word}" if line else word
    yield line


def _wrapped(keyword: str, text: str | list[str]) -> list[str]:
    """A header's text, its keyword in columns 1 to 12 and the text after it: each of a
    list's paragraphs from a line of its own."""
    pieces = text if isinstance(text, list) else [text]
    out: list[str] = []
    for piece in pieces:
        for line in _words(" ".join(piece.split()), WIDTH - 12):
            out.append(f"{(keyword if not out else ''):<12}{line}")
    return out


def _origin(bases: str) -> list[str]:
    lower = bases.lower().replace("u", "t")
    return [
        f"{i + 1:>9} " + " ".join(lower[j : j + 10] for j in range(i, min(i + 60, len(lower)), 10))
        for i in range(0, len(lower), 60)
    ]


# --- FASTA ---------------------------------------------------------------------------------------


def fasta(record: SequenceFile) -> Written:
    words = [record.hrid]
    name = " ".join(record.name.split())
    if name and name != record.hrid:
        words.append(name)
    description = " ".join(record.description.split())
    if description:
        words.append(description)
    words.append(f"[topology={record.topology}]")
    lines = [">" + " ".join(words)]
    lines += [record.bases[i : i + 70] for i in range(0, len(record.bases), 70)]
    left_out = []
    if record.features:
        count = len(record.features)
        which = "its feature isn't" if count == 1 else f"its {count:,} features aren't"
        left_out.append(f"FASTA holds no features: {which} in it.")
    if record.strandedness == "single":
        left_out.append("FASTA can't say a molecule is single-stranded.")
    return Written(("\n".join(lines) + "\n").encode("utf-8"), left_out)
