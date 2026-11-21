"""The DNA plugin's imports: `file` reads DNA sequences from a FASTA, GenBank or
SnapGene file.

It reads the run input from $IKI_INPUT and writes the sequences to create, each with
where it came from, to $IKI_OUTPUT, as every action's command does. A record that
can't be read fails the whole import with a `::error` line that says which and why,
so nothing is half imported.

GenBank records give their name, definition, accession and version, organism (by the
NCBI Taxonomy ID in its main source feature: its entry is made the first time, ADR 0140),
topology, strandedness (if LOCUS says ss-DNA or ds-DNA; ms-, mixed, fails the import) and
features. Its ORIGIN must have as many bases as LOCUS says (`5028 bp`), or the import
fails: a record missing a line of them would be another molecule under its accession. A
feature's location is kept as its first and last base and its strand, with its parts when
it has several (`join(...)`, or `order(...)`, kept as its operator), lies between two
bases (`5^6`), or has partial ends (`<1`, `>99`, kept on its parts); such locations
keep GenBank's own text in the `location` qualifier too. A location that reads across the
origin -- off the end of the record and back to base 1, as INSDC writes a gene across a
circular genome's origin -- is kept on a circular record and left out of a linear one,
saying so: a linear molecule has two ends, and nothing of it reads across them. A record
of several source features is of the one marked /focus or /transgenic, or else the one
spanning it all; the others are kept as features. Features on other records
(`J00194.1:100..202`) are left out, with a warning. A RefSeq model's (XM_, XR_) COMMENT
says NCBI's annotation pipeline predicted it, with which gene prediction method: its
origin says so, as NCBI's word (ADR 0106). Other records don't say how their sequence
was made, so theirs don't.

FASTA headers give a name (the first word) and a description (the rest). A word of
the header that looks like an INSDC accession with its version (`L09137.2`,
`NM_000014.6`), including inside NCBI's `gb|…|` style, is kept as the accession.
FASTA doesn't say whether a sequence is circular, so the `topology` param does, unless
the header says, as NCBI's source modifier `[topology=circular]` does.

A SnapGene file (`.dna`, told by its first bytes) holds one sequence: its bases, whether
it's circular and double-stranded, its features, its primers (each a Primer bound to it,
and a `primer_bind` feature where SnapGene shows it binding), and notes, of which its
description and accession are kept. What it holds that a DNA sequence has no place for
(methylation, a linear molecule's overhangs, SnapGene's history, reads aligned to it) is
left out, and the run says so; the file is kept, as the record's origin.

Files are read a line at a time, so they may hold millions of records. Past a thousand
(or a megabyte of them), the creates go in a creates file (`creates_file`, one per
line), which Iki checks and then writes all at once, or not at all (ADR 0056).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from xml.etree import ElementTree

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

BASES = re.compile(r"^[ACGTRYSWKMBDHVN]*$")
#: A nucleotide accession without its version, as INSDC and RefSeq write them: letters and
#: digits (L09137, CP009273, AAAA01000001), or RefSeq's two letters and an underscore before
#: digits (NM_000546) or before an INSDC accession (NZ_CP009273, NZ_AAAA01000001).
ACCESSION_BODY = r"(?:[A-Z]{2}_[A-Z]{0,6}|[A-Z]{1,6})[0-9]{5,11}"
#: One with its version, as a FASTA header names it.
ACCESSION = re.compile(rf"^{ACCESSION_BODY}\.[0-9]+$")
#: What a DNA sequence's accession field takes: letters, digits, _ and ., from a letter or
#: a digit, so the placeholder "." other tools write for none isn't one.
ACCESSION_FIELD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]*")
#: The most records one run imports.
MAX_RECORDS = 10_000_000
#: Up to these many (and bytes of them), creates go in output.json; past them, in a file.
INLINE_RECORDS = 1_000
INLINE_BYTES = 1_000_000
CREATES_FILE = "creates.ndjson"
MAX_MESSAGES = 99
MAX_FEATURES = 10_000
MAX_QUALIFIERS = 50
MAX_PARTS = 1_000
MAX_QUALIFIER = 10_000
LIMITS = {"name": 255, "description": 10_000, "accession": 64}
#: How NCBI's COMMENT starts for a RefSeq model, a record its annotation pipeline predicted.
MODEL = "MODEL REFSEQ: This record is predicted by automated computational analysis"


#: A name an organism's scientific_name takes (iki/core.Organism): one line, from a letter or
#: a digit, or NCBI's [ for a genus it hasn't placed ([Clostridium] scindens) or ' for an
#: informal name ('Nostoc azollae' 0708).
SCIENTIFIC_NAME = re.compile(r"[A-Za-z0-9\['][^\t\r\n]{0,254}")


class UnreadableError(Exception):
    """A record that can't be imported, and why."""


@dataclass
class Record:
    #: Its name in the file, which the run's output names it by.
    label: str
    values: dict[str, Any]
    #: What it was read from, which its origin's SHA-256 is of: text, or a file's bytes.
    text: str | bytes
    warnings: list[str] = field(default_factory=list)
    #: How the record says its sequence was made, if it does (ADR 0106).
    made: dict[str, Any] | None = None
    #: The organism it's from, by its NCBI Taxonomy ID and name, if the record says.
    organism: Organism | None = None
    #: Where its topology came from, for the run's log: its LOCUS line, its header...
    topology_from: str = ""
    #: The primers its file holds, each a Primer to create bound to it (a SnapGene file's).
    primers: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Organism:
    """A taxon, as a record names it: its entry in Iki is found, or made, by its ID."""

    taxon_id: int
    name: str

    @property
    def key(self) -> str:
        return f"taxon:{self.taxon_id}"

    def create(self) -> dict[str, Any]:
        values: dict[str, Any] = {"taxon_id": self.taxon_id}
        if SCIENTIFIC_NAME.fullmatch(self.name):
            values["scientific_name"] = self.name
        # A name only fills an entry that has none: an old file may give a taxon's old name.
        fills = ["scientific_name"] if "scientific_name" in values else []
        return {
            "type": "iki/core.Organism",
            "key": self.key,
            "values": values,
            **({"fills": fills} if fills else {}),
        }


# --- Bases ---------------------------------------------------------------------------------------


def clean_bases(label: str, raw: str) -> str:
    bases = re.sub(r"[\s0-9]", "", raw).upper()
    if not bases:
        raise UnreadableError(f"{label} has no bases.")
    if not BASES.match(bases):
        position, found = next((i, b) for i, b in enumerate(bases, 1) if not BASES.match(b))
        if found == "U":
            raise UnreadableError(f"{label} has U at base {position}: it's RNA, not DNA.")
        if found in "-.":
            raise UnreadableError(
                f"{label} has a gap ({found}) at base {position}: import unaligned sequences."
            )
        raise UnreadableError(
            f"{label} has {found!r} at base {position}, which isn't an IUPAC base."
        )
    return bases


def fit(values: dict[str, Any], record: Record) -> dict[str, Any]:
    """Values cut to what the fields take, saying what was cut."""
    out = {}
    for name, value in values.items():
        limit = LIMITS.get(name)
        if isinstance(value, str) and limit is not None and len(value) > limit:
            record.warnings.append(f"{record.label}: its {name} was cut to {limit} characters.")
            value = value[:limit]
        if value not in (None, "", []):
            out[name] = value
    return out


# --- FASTA ---------------------------------------------------------------------------------------


def fasta(lines: Iterable[str], topology: str) -> Iterator[Record]:
    """Each record of a FASTA file, from its lines (with their line ends), as it's read."""
    count = 0
    header: str | None = None
    raw: list[str] = []
    bases: list[str] = []

    def record() -> Record:
        words = header.split(None, 1) if header is not None else []
        # NCBI's old `gb|L09137.2|` style names the record by its accession.
        name = (accession_in(words[0]) or words[0]) if words else ""
        label = f"Record {count}" + (f" ({name})" if name else "")
        found = Record(label, {}, "".join(raw))
        description = words[1].strip() if len(words) > 1 else ""
        # NCBI's source modifier (`[topology=circular]`), as Iki's FASTA export writes it:
        # the header knows, as a GenBank record's LOCUS line does, so it wins.
        said = TOPOLOGY_MODIFIER.search(description)
        if said:
            description = " ".join(TOPOLOGY_MODIFIER.sub(" ", description).split())
        found.topology_from = "its header" if said else "the topology param for FASTA records"
        found.values = fit(
            {
                "name": name,
                "description": description,
                "accession": accession_in(name),
                "bases": clean_bases(label, "".join(bases)),
                "topology": said[1].lower() if said else topology,
            },
            found,
        )
        return found

    for line in lines:
        if line.startswith(">"):
            if header is not None:
                yield record()
            count += 1
            header, raw, bases = line[1:].strip(), [], []
        elif line.strip() and not line.startswith(";"):
            if header is None:
                raise UnreadableError("This FASTA file has bases before its first '>' header.")
            bases.append(line)
        if header is not None:
            raw.append(line)
    if header is not None:
        yield record()


#: NCBI's FASTA source modifier for topology, in a header's description (its key and
#: value in any case, as NCBI's submission tools read them).
TOPOLOGY_MODIFIER = re.compile(r"\[\s*topology\s*=\s*(circular|linear)\s*\]", re.IGNORECASE)


def accession_in(name: str) -> str:
    for word in name.split("|"):
        if ACCESSION.match(word):
            return word
    return ""


# --- GenBank -------------------------------------------------------------------------------------


def genbank(lines: Iterable[str]) -> Iterator[Record]:
    """Each record of a GenBank file, from its lines (with their line ends), as it's read."""
    count = 0
    held: list[str] = []
    for line in lines:
        if held or line.strip():
            held.append(line)
        if line.startswith("//"):
            count += 1
            yield genbank_record(count, held, "".join(held))
            held = []
    if any(line.strip() for line in held):
        raise UnreadableError(f"Record {count + 1} doesn't end with '//': is the file cut short?")


def genbank_record(number: int, lines: list[str], text: str) -> Record:
    if not lines[0].startswith("LOCUS"):
        raise UnreadableError(f"Record {number} doesn't start with a LOCUS line.")
    locus = lines[0].split()
    name = locus[1] if len(locus) > 1 else ""
    label = f"Record {number}" + (f" ({name})" if name else "")
    record = Record(label, {}, text)
    sections: dict[str, list[str]] = {}
    current = ""
    features: list[str] = []
    origin: list[str] = []
    for line in lines[1:]:
        if line.startswith("//"):
            break
        keyword = line[:12].strip() if not line.startswith(" ") else ""
        if keyword:
            current = keyword.split()[0]
            if current == "FEATURES":
                continue
            if current == "ORIGIN":
                continue
            sections.setdefault(current, []).append(line[12:].rstrip())
        elif current == "FEATURES":
            features.append(line.rstrip("\r\n"))
        elif current == "ORIGIN":
            origin.append(line)
        elif current:
            sections.setdefault(current, []).append(line.strip())
    # VERSION names the accession with its version (L09137.2); old files may lack it.
    named = " ".join(sections.get("VERSION", []) or sections.get("ACCESSION", [])).split()
    accession = named[0] if named else ""
    bases = clean_bases(label, "".join(origin))
    declared = declared_length(locus[2:])
    if declared is not None and declared != len(bases):
        raise UnreadableError(
            f"{label} has {len(bases)} bases, but its LOCUS line says it has {declared}: "
            + ("is its ORIGIN cut short?" if len(bases) < declared else "is its LOCUS line wrong?")
        )
    parsed = _parse_features(features)
    organism_line = _organism(sections.get("SOURCE", []))
    source = _primary_source(record, parsed, organism_line, len(bases))
    topology = "circular" if "circular" in (w.lower() for w in locus[2:]) else "linear"
    values = {
        "name": name,
        "description": " ".join(sections.get("DEFINITION", [])).strip().removesuffix("."),
        # A bare "." (Benchling's and Biopython's ACCESSION for a record with none) says
        # there isn't one: an accession starts with a letter or a digit.
        "accession": accession if ACCESSION_FIELD.fullmatch(accession) else "",
        "bases": bases,
        "topology": topology,
        "features": _features(record, parsed, source, len(bases), topology),
    }
    stranded = strandedness(locus[2:])
    if stranded == "mixed":
        raise UnreadableError(
            f"{label} is mixed-stranded (LOCUS says ms-), partly single and partly double, "
            "which a DNA sequence's strandedness can't say yet: as either, it would be "
            "registered as another molecule. Leave it out of what you import."
        )
    if stranded:
        values["strandedness"] = stranded
    record.values = fit(values, record)
    record.topology_from = "its LOCUS line"
    record.made = made(sections.get("COMMENT", []))
    if source is not None:
        record.organism = source.organism
        if record.organism and source.name and not SCIENTIFIC_NAME.fullmatch(source.name):
            record.warnings.append(
                f"{label}: its organism's name, {source.name[:200]}, isn't one an organism's "
                f"scientific name can be, so its organism has only its NCBI Taxonomy ID, "
                f"{source.taxon}."
            )
    return record


def declared_length(locus: list[str]) -> int | None:
    """How many bases LOCUS says the record has (`5028 bp`), if it says. GenBank gives it
    for every record, and the ORIGIN must have that many: a record missing a line of its
    bases would otherwise be registered, under its accession, as a molecule it isn't."""
    for number, unit in itertools.pairwise(locus):
        if unit == "bp" and number.isdigit():
            return int(number)
    return None


#: LOCUS's molecule types (INSDC): DNA, RNA, or a kind of RNA, maybe with ss-, ds- or ms-.
MOLECULE = re.compile(r"^(ss-|ds-|ms-)?([a-z]*RNA|DNA)$", re.IGNORECASE)


def strandedness(locus: list[str]) -> str | None:
    """What LOCUS says of the molecule's strands: `single` for ss-DNA and for RNA (an mRNA
    is kept as its cDNA's bases, GenBank's T alphabet, but the molecule is one strand),
    `double` for ds-DNA and ds-RNA, and `mixed` for ms- (partly single, partly double),
    which a sequence's strandedness can't say. Plain DNA doesn't say, so the field keeps
    its default."""
    for word in locus:
        found = MOLECULE.match(word)
        if not found:
            continue
        prefix, kind = (found[1] or "").lower(), found[2].upper()
        if prefix == "ms-":
            return "mixed"  # neither: the import refuses it
        if prefix == "ds-":
            return "double"
        if prefix == "ss-" or kind.endswith("RNA"):
            return "single"
        return None
    return None


def made(comment: list[str]) -> dict[str, Any] | None:
    """How a record's COMMENT says its sequence was made, read from it, never guessed (ADR
    0106): a RefSeq model's (XM_, XR_), which NCBI's annotation pipeline predicted, with the
    gene prediction method and the pipeline's version it names. Other records don't say."""
    text = " ".join(" ".join(comment).split())
    if MODEL not in text:
        return None
    found: dict[str, Any] = {"kind": "computational"}
    software: list[dict[str, str]] = []
    method = re.search(r"gene prediction method: ([A-Za-z0-9][A-Za-z0-9_+-]*)", text)
    if method:
        found["method"] = "gene prediction"
        software.append({"name": method[1][:100]})
    annotation = structured(comment, "Genome-Annotation-Data")
    pipeline = annotation.get("Annotation Pipeline", "")[:100].strip()
    if pipeline:
        tool = {"name": pipeline}
        version = annotation.get("Annotation Software Version", "")[:100].strip()
        if version:
            tool["version"] = version
        software.append(tool)
    if software:
        found["software"] = software
    return found


def structured(comment: list[str], name: str) -> dict[str, str]:
    """A structured comment's fields (`Key :: value`, a value going on over lines), from
    between its `##name-START##` and `##name-END##`."""
    out: dict[str, str] = {}
    inside = False
    key = ""
    for raw in comment:
        line = raw.strip()
        if line == f"##{name}-START##":
            inside = True
        elif line == f"##{name}-END##":
            break
        elif inside and "::" in line:
            key, _, value = line.partition("::")
            key = key.strip()
            out[key] = value.strip()
        elif inside and key and line:
            out[key] = f"{out[key]} {line}"
    return out


def _organism(source: list[str]) -> str:
    for line in source:
        if line.strip().startswith("ORGANISM"):
            return line.strip().removeprefix("ORGANISM").strip()
    return ""


@dataclass
class Source:
    """A source feature: the organism a stretch of the record is from."""

    location: str
    qualifiers: list[str]
    #: Its /organism, or the record's ORGANISM line if it's the record's only source.
    name: str
    #: The NCBI Taxonomy ID its /db_xref gives, if it gives one.
    taxon: int | None

    @property
    def organism(self) -> Organism | None:
        return Organism(self.taxon, self.name) if self.taxon is not None else None


def _parse_features(lines: list[str]) -> list[tuple[str, str, list[str]]]:
    """GenBank's feature table: each feature's key (columns 6 to 20), its location after
    it, and its /qualifiers, each line continuing the one before."""
    parsed: list[tuple[str, str, list[str]]] = []
    for line in lines:
        if len(line) > 5 and line[5] != " ":
            key, _, location = line[5:].partition(" ")
            parsed.append((key.strip(), location.strip(), []))
        elif parsed:
            body = line[21:] if len(line) > 21 else line.strip()
            key, location, qualifiers = parsed[-1]
            if body.startswith("/"):
                qualifiers.append(body)
            elif qualifiers:
                # Translations wrap mid-sequence; text wraps between words.
                joint = "" if qualifiers[-1].startswith("/translation") else " "
                qualifiers[-1] += joint + body.strip()
            else:
                parsed[-1] = (key, location + body.strip(), qualifiers)
    return parsed


def _values(raw: list[str], name: str) -> list[str]:
    """Each value of the qualifier ``name`` (``/name="value"``), its quotes taken off; one
    with no value (``/focus``) gives an empty one."""
    out = []
    for item in raw:
        key, equals, value = item[1:].partition("=")
        if key.strip() != name:
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1].replace('""', '"')
        out.append(value if equals else "")
    return out


def _primary_source(
    record: Record, parsed: list[tuple[str, str, list[str]]], organism_line: str, length: int
) -> Source | None:
    """The source feature of the organism the record is of, as INSDC's feature table says
    which: the one with /focus or /transgenic, which names the main organism of a record
    with several; or the only one; or the only one that spans the whole record. Its name
    and taxon are read together, from it. When none is, the record's organism is left out,
    saying why: one source's name with another's taxon would be a third organism."""
    sources = []
    for key, location, raw in parsed:
        if key != "source":
            continue
        names = _values(raw, "organism")
        taxa = {
            int(found[1])
            for value in _values(raw, "db_xref")
            if (found := re.fullmatch(r"taxon:([0-9]{1,10})", value)) and int(found[1]) > 0
        }
        taxon = taxa.pop() if len(taxa) == 1 else None
        sources.append(Source(location, raw, names[0] if names else "", taxon))
    if len(sources) == 1 and not sources[0].name:
        sources[0].name = organism_line
    marked = [s for s in sources if _values(s.qualifiers, "focus")] or [
        s for s in sources if _values(s.qualifiers, "transgenic")
    ]
    whole = [s for s in sources if _whole(s.location, length)]
    chosen: Source | None = None
    if len(marked) == 1:
        chosen = marked[0]
    elif not marked and len(sources) == 1:
        chosen = sources[0]
    elif not marked and len(whole) == 1:
        chosen = whole[0]
    if chosen is None:
        if sources:
            record.warnings.append(
                f"{record.label}: its organism is left out: of its {len(sources)} source "
                "features, not one alone is marked as its main organism (/focus or "
                "/transgenic) or spans the whole record. Each is kept as a feature."
            )
        elif organism_line:
            record.warnings.append(
                f"{record.label}: its organism, {organism_line[:200]}, has no NCBI Taxonomy "
                "ID (it has no source feature), so it's left out."
            )
        return None
    if chosen.taxon is None:
        named = chosen.name or organism_line
        record.warnings.append(
            f"{record.label}: its organism"
            + (f", {named[:200]}," if named else "")
            + ' has no NCBI Taxonomy ID (its source feature\'s /db_xref="taxon:…"), so '
            "it's left out."
        )
    return chosen


def _whole(location: str, length: int) -> bool:
    """Whether a source's location is the whole record, ``1..n``."""
    found = re.fullmatch(r"<?1\.\.>?([0-9]+)", location.strip())
    return found is not None and int(found[1]) == length


def _features(
    record: Record,
    parsed: list[tuple[str, str, list[str]]],
    primary: Source | None,
    length: int,
    topology: str,
) -> list[dict[str, Any]]:
    """The record's features but its main source, which is its organism. Other sources,
    stretches of it from other organisms, are kept as features named by their organism, so
    their spans and taxa stay with it."""
    out: list[dict[str, Any]] = []
    for key, location, raw in parsed:
        if key == "source" and primary is not None and raw is primary.qualifiers:
            continue
        if len(out) == MAX_FEATURES:
            record.warnings.append(f"{record.label}: only its first {MAX_FEATURES} features.")
            break
        feature = _feature(record, key, location, raw, length, topology)
        if feature is not None:
            out.append(feature)
    return out


class LocationError(ValueError):
    """A location this reading of INSDC's grammar doesn't take."""


#: A part's place: `a..b` (either end maybe beyond the base it names: `<a`, `>b`), `a^b`
#: (between two bases), or a single base (maybe `<a` or `>a`).
RANGE = re.compile(r"(<)?([0-9]+)\.\.(>)?([0-9]+)")
BETWEEN = re.compile(r"([0-9]+)\^([0-9]+)")
BASE = re.compile(r"([<>])?([0-9]+)")


def _top_level(text: str) -> list[str]:
    """`text` split at the commas outside parentheses."""
    out, depth, here = [], 0, 0
    for at, c in enumerate(text):
        depth += (c == "(") - (c == ")")
        if c == "," and depth == 0:
            out.append(text[here:at])
            here = at + 1
    out.append(text[here:])
    return [part.strip() for part in out]


def parts_of(location: str, strand: str = "forward") -> list[dict[str, Any]]:
    """A location's parts in the order they're read (5' to 3' of what the feature is: a
    complement's parts last first), each with its strand, as INSDC's feature table writes
    them: `complement(…)`, `join(…)` and `order(…)`, around ranges, single bases and sites
    between two bases (`5^6`, flagged `between`). An end beyond the base it names (`<2`,
    `>9`: INSDC's partial ends) is flagged `partial_start` or `partial_end`, as the start
    or end it's on."""
    location = location.strip()
    for wrapper in ("complement", "join", "order"):
        if location.startswith(f"{wrapper}(") and location.endswith(")"):
            inner = location[len(wrapper) + 1 : -1]
            if wrapper == "complement":
                flipped = "reverse" if strand == "forward" else "forward"
                return list(reversed(parts_of(inner, flipped)))
            return [part for piece in _top_level(inner) for part in parts_of(piece, strand)]
    if found := RANGE.fullmatch(location):
        part = {"start": int(found[2]), "end": int(found[4]), "strand": strand}
        return [_partial(part, start=bool(found[1]), end=bool(found[3]))]
    if found := BETWEEN.fullmatch(location):
        return [{"start": int(found[1]), "end": int(found[2]), "strand": strand, "between": True}]
    if found := BASE.fullmatch(location):
        part = {"start": int(found[2]), "end": int(found[2]), "strand": strand}
        return [_partial(part, start=found[1] == "<", end=found[1] == ">")]
    raise LocationError(location)


def _partial(part: dict[str, Any], *, start: bool, end: bool) -> dict[str, Any]:
    if start:
        part["partial_start"] = True
    if end:
        part["partial_end"] = True
    return part


def operator_of(location: str) -> str:
    """How a location's parts go together: `order` (found in that order, nothing said of
    joining them) or `join` (one molecule, them joined), INSDC's two operators, which one
    location can't mix. A location of one part is `join`'s, as it's one stretch."""
    ordered, joined = "order(" in location, "join(" in location
    if ordered and joined:
        raise LocationError(location)
    return "order" if ordered else "join"


def extent(parts: list[dict[str, Any]], length: int, topology: str) -> tuple[int, int]:
    """The stretch a feature's parts lie within, first base to last: on a circular sequence
    the shortest way round that holds them all, which crosses the origin (an end before
    the start) only when that's shorter; otherwise its lowest base to its highest."""
    stretches: list[tuple[int, int]] = []
    for part in parts:
        a, b = part["start"], part["end"]
        stretches.extend([(a, length), (1, b)] if a > b else [(a, b)])
    stretches.sort()
    if topology != "circular" or len(stretches) == 1:
        return stretches[0][0], max(b for _, b in stretches)
    merged: list[list[int]] = []
    for a, b in stretches:
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    # The widest stretch none of them cover, round the circle, is what the feature isn't.
    gaps = [(merged[i + 1][0] - merged[i][1] - 1, i) for i in range(len(merged) - 1)]
    gaps.append((length - merged[-1][1] + merged[0][0] - 1, len(merged) - 1))
    _, after = max(gaps)
    return merged[(after + 1) % len(merged)][0], merged[after][1]


def _feature(
    record: Record, key: str, location: str, raw: list[str], length: int, topology: str
) -> dict[str, Any] | None:
    if ":" in location:
        record.warnings.append(
            f"{record.label}: a {key} on another record ({location}) was left out."
        )
        return None
    try:
        operator = operator_of(location)
        parts = parts_of(location)
    except LocationError:
        operator, parts = "join", []
    circle = topology == "circular"

    def in_the_record(part: dict[str, Any]) -> bool:
        a, b = part["start"], part["end"]
        return 1 <= a <= length and 1 <= b <= length

    def reads_across_the_origin() -> bool:
        """Whether the location reads across the origin: a range that ends before it
        starts, a join whose parts run off the end and back to base 1
        (`join(2306..3215,1..1623)`, as INSDC writes a gene across a circular genome's
        origin), or a site between the last base and the first (`100^1`). Only a circle
        has an origin to cross; parts merely out of order along the molecule don't."""
        if not all(in_the_record(part) for part in parts):
            return False
        if any(part.get("between") for part in parts):
            return any((part["start"], part["end"]) == (length, 1) for part in parts)
        first, last = extent(parts, length, "circular")
        return last < first

    def fits(part: dict[str, Any]) -> bool:
        a, b = part["start"], part["end"]
        if not (1 <= a <= length and 1 <= b <= length):
            return False
        if part.get("between"):
            # Between two neighbouring bases, or round the origin between the last and first.
            return b == a + 1 or (circle and (a, b) == (length, 1))
        return a <= b or circle  # a range ending before it starts crosses the origin

    # A location that reads across the origin says the molecule is a circle, and the
    # LOCUS line says it isn't: the two can't both be right, so the feature is refused
    # with what is wrong with it rather than with the general message.
    if not circle and parts and reads_across_the_origin():
        record.warnings.append(
            f"{record.label}: a {key} at {location!r} reads across the origin, which only a "
            "circular molecule has, and its LOCUS line says linear. It was left out: say the "
            "molecule is circular, or give the feature's place on the linear one."
        )
        return None
    if not parts or not all(fits(part) for part in parts):
        record.warnings.append(f"{record.label}: a {key} at {location!r} was left out.")
        return None
    strands = {part["strand"] for part in parts}
    strand = strands.pop() if len(strands) == 1 else "mixed"
    first, last = extent(parts, length, topology)
    qualifiers: dict[str, str] = {}
    for item in raw:
        name, _, value = item[1:].partition("=")
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1].replace('""', '"')
        if name in qualifiers:
            qualifiers[name] += "; " + value
        elif len(qualifiers) < MAX_QUALIFIERS:
            qualifiers[name] = value
    if not re.fullmatch(r"(complement\()?[0-9]+(\.\.[0-9]+)?\)?", location):
        qualifiers["location"] = location  # several parts, fuzzy, or between two bases
    for name, value in qualifiers.items():
        if len(value) > MAX_QUALIFIER:
            qualifiers[name] = value[:MAX_QUALIFIER]
            record.warnings.append(f"{record.label}: a {key}'s /{name} was cut.")
    feature: dict[str, Any] = {"type": key[:64], "start": first, "end": last, "strand": strand}
    if operator == "order" and len(parts) > 1:
        feature["operator"] = "order"
    partial = any(p.get("partial_start") or p.get("partial_end") for p in parts)
    if len(parts) > 1 or parts[0].get("between") or partial:
        # Where it lies, part by part: a join's exons, not its introns; a site, no base;
        # ends that go on beyond the bases they name.
        feature["parts"] = parts[:MAX_PARTS]
        if len(parts) > MAX_PARTS:
            record.warnings.append(f"{record.label}: a {key}'s first {MAX_PARTS} parts only.")
    # Another organism's stretch of the record is named by it.
    named = ("organism",) if key == "source" else ("label", "gene", "locus_tag", "product")
    label = next((qualifiers[q] for q in named if q in qualifiers), "")
    if label:
        feature["name"] = label[:200]
    if qualifiers:
        feature["qualifiers"] = qualifiers
    return feature


# --- SnapGene ------------------------------------------------------------------------------------
#
# A SnapGene file (`.dna`) is a run of packets, each a type byte, its length (4 bytes,
# big-endian) and its data, the first SnapGene's 14-byte header ("SnapGene", then its
# sequence type: 1 for DNA). SnapGene doesn't publish the format; this reads what the
# files SnapGene writes hold, as Biopython's reader and others describe them and as the
# files in the tests show. A file holds one sequence.

#: How a SnapGene file starts: its first packet, 14 bytes, which say "SnapGene".
SNAPGENE = b"\x09\x00\x00\x00\x0eSnapGene"
#: Packets by type, as the run's messages name them.
PACKETS = {
    0x00: "DNA",
    0x05: "primers",
    0x06: "notes",
    0x08: "sequence properties",
    0x0A: "features",
    0x11: "alignable sequences",
}
#: SnapGene's own caches and display settings, which follow from the bases or are how its
#: window shows them: restriction sites found, enzyme sets and which enzymes it shows, the
#: colours it draws bases in.
DISPLAY = frozenset({0x02, 0x03, 0x0D, 0x0E, 0x14, 0x1C})
#: SnapGene's history of the operations that made the sequence (its history tree and nodes).
HISTORY = frozenset({0x07, 0x0B, 0x1D, 0x23})
#: What other packets hold, as far as it's known, for the run's message.
UNREAD = {0x10: "an alignable sequence", 0x12: "sequencing traces", 0x13: "uracil positions"}
#: The DNA packet's flags beyond its topology (bit 0) and strands (bit 1): how the strain
#: it's grown in methylates it, as SnapGene marks it for its restriction analysis.
METHYLATION = ((0x04, "Dam"), (0x08, "Dcm"), (0x10, "EcoKI"))
#: A primer's most bases, as iki/dna.Primer takes them.
PRIMER_BASES = 200


class _Text(HTMLParser):
    """The text of SnapGene's rich text (`<html><body>…</body></html>`), its line breaks
    and paragraphs kept as line ends, its comments left out."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "div", "li"):
            self.parts.append("\n")


def plain(text: str | None, *, lines: bool = False) -> str:
    """SnapGene's text as plain text: rich text's tags taken out, and its spaces made one;
    with ``lines``, its line ends kept (a description's), else made spaces too."""
    if not text:
        return ""
    if text.lstrip()[:5].lower() == "<html":
        parser = _Text()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    if not lines:
        return " ".join(text.split())
    kept = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Each packet's type and data, checking each is all there."""
    at = 0
    first = True
    while at < len(data):
        if len(data) - at < 5:
            raise UnreadableError("It's cut short: it ends in the middle of a packet's header.")
        kind = data[at]
        (length,) = struct.unpack(">I", data[at + 1 : at + 5])
        body = data[at + 5 : at + 5 + length]
        if len(body) < length:
            name = "first" if first else PACKETS.get(kind, f"0x{kind:02x}")
            raise UnreadableError(
                f"It's cut short: its {name} packet says it has {length:,} bytes, and the "
                f"file ends after {len(body):,}."
            )
        first = False
        yield kind, body
        at += 5 + length


def _xml(kind: int, body: bytes) -> ElementTree.Element:
    """A packet's XML. SnapGene writes no DTDs, so one that declares a DOCTYPE or entities
    isn't read at all: entities are how XML is made to expand without end."""
    name = PACKETS.get(kind, f"0x{kind:02x}")
    if re.search(rb"<!(DOCTYPE|ENTITY)", body, re.IGNORECASE):
        raise UnreadableError(
            f"Its {name} packet declares a DOCTYPE or entities, which a SnapGene file "
            "doesn't: it isn't read."
        )
    try:
        return ElementTree.fromstring(body)  # noqa: S314 - no DTD, so no entities to expand
    except ElementTree.ParseError as why:
        raise UnreadableError(f"Its {name} packet isn't XML that can be read ({why}).") from None


def snapgene(data: bytes, file_name: str) -> Record:
    """The one sequence a SnapGene file holds."""
    packets: dict[int, list[bytes]] = {}
    for kind, body in _packets(data):
        packets.setdefault(kind, []).append(body)
    (header,) = packets.pop(0x09)[:1]
    (sequence_type,) = struct.unpack(">H", header[8:10])
    if sequence_type != 1:
        raise UnreadableError(
            f"It's a SnapGene file, but not of DNA (its sequence type is {sequence_type}; a "
            ".dna file's is 1), such as a protein (.prot) or RNA (.rna) file."
        )
    if 0x00 not in packets:
        if 0x01 in packets:
            raise UnreadableError(
                "It keeps its bases compressed (packet 0x01), which this import can't read: "
                "save it from SnapGene as GenBank and import that."
            )
        raise UnreadableError("It has no DNA packet, so it has no bases.")
    if len(packets[0x00]) > 1:
        raise UnreadableError("It has more than one DNA packet.")
    notes = _xml(0x06, packets[0x06][0]) if 0x06 in packets else None
    # A SnapGene file names no sequence: SnapGene shows its file's name, or a map label.
    name = file_name[:-4] if file_name.lower().endswith(".dna") else file_name
    if notes is not None and notes.findtext("UseCustomMapLabel", "").strip() == "1":
        name = plain(notes.findtext("CustomMapLabel")) or name
    record = Record(name, {}, data)
    say = record.warnings.append

    dna_packet = packets.pop(0x00)[0]
    flags, raw = (dna_packet[0], dna_packet[1:]) if dna_packet else (0, b"")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise UnreadableError("Its DNA packet's bases aren't letters.") from None
    bases = clean_bases(name, text)
    length = len(bases)
    topology = "circular" if flags & 0x01 else "linear"
    methylated = [who for bit, who in METHYLATION if flags & bit]
    if methylated:
        say(
            f"{name}: SnapGene says the strain it's grown in methylates it "
            f"({_listed(methylated)}), which isn't kept: it's the host's doing, not the "
            "sequence's."
        )
    if flags & ~0x1F:
        say(f"{name}: its DNA packet has flags this import doesn't read (0x{flags & ~0x1F:02x}).")
    if any(c.islower() for c in text) and any(c.isupper() for c in text):
        say(
            f"{name}: its bases are in both upper and lower case, which SnapGene shows but "
            "reads as the same bases. They're kept in upper case."
        )

    features: list[dict[str, Any]] = []
    for body in packets.pop(0x0A, []):
        for element in _xml(0x0A, body).iter("Feature"):
            if len(features) == MAX_FEATURES:
                say(f"{name}: only its first {MAX_FEATURES} features.")
                break
            found = _snapgene_feature(record, element, length, topology)
            if found is not None:
                features.append(found)
    for body in packets.pop(0x05, []):
        features.extend(_snapgene_primers(record, _xml(0x05, body), length, topology))

    values: dict[str, Any] = {
        "name": name,
        "bases": bases,
        "topology": topology,
        "strandedness": "double" if flags & 0x02 else "single",
        "features": features,
    }
    if notes is not None:
        values.update(_snapgene_notes(record, notes))
    packets.pop(0x06, None)
    for body in packets.pop(0x08, []):
        if topology == "linear":
            _snapgene_ends(record, _xml(0x08, body))

    if any(kind in HISTORY for kind in packets):
        say(
            f"{name}: SnapGene's history of how it was made isn't kept; the file is, as the "
            "record's origin."
        )
    aligned = 0
    for body in packets.pop(0x11, []):
        aligned += sum(1 for _ in _xml(0x11, body).iter("Sequence"))
    if aligned:
        say(
            f"{name}: the {aligned} sequence{'s' if aligned != 1 else ''} aligned to it in "
            "SnapGene, such as sequencing reads, aren't kept: align them to it with "
            "align_reads."
        )
    unread = sorted(k for k in packets if k not in DISPLAY and k not in HISTORY and k != 0x09)
    if unread:
        listed = ", ".join(
            f"{UNREAD[k]} (packet 0x{k:02x})" if k in UNREAD else f"packet 0x{k:02x}"
            for k in unread
        )
        say(f"{name}: parts of the SnapGene file this import doesn't read were left out: {listed}.")
    record.values = fit(values, record)
    record.topology_from = "its SnapGene file"
    return record


def _listed(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _described(element: ElementTree.Element) -> str:
    kind = element.get("type") or "misc_feature"
    named = plain(element.get("name"))
    return f"a {kind}" + (f" ({named})" if named else "")


def _snapgene_parts(
    record: Record,
    ranges: list[tuple[int, int]],
    strand: str,
    length: int,
    topology: str,
    what: str,
    where: str,
) -> list[dict[str, Any]] | None:
    """A feature's parts from its segments (from 1, ends included), in the order they're
    read: a reverse-strand one's last first. A segment that ends before it starts goes
    round the origin, two parts on a circle, as INSDC writes it (`join(x..L,1..y)`); a
    linear molecule has no origin, so such a feature is left out, saying so, as one off
    its bases is."""
    parts: list[dict[str, Any]] = []
    for a, b in ranges:
        if not (1 <= a <= length and 1 <= b <= length):
            record.warnings.append(
                f"{record.label}: {what} at {where} isn't on its {length:,} bases, so it was "
                "left out."
            )
            return None
        if a > b:
            if topology != "circular":
                record.warnings.append(
                    f"{record.label}: {what} at {where} reads across the origin, which only "
                    "a circular molecule has, and the SnapGene file says it's linear. It was "
                    "left out: say the molecule is circular, or give the feature's place on "
                    "the linear one."
                )
                return None
            parts.append({"start": a, "end": length, "strand": strand})
            parts.append({"start": 1, "end": b, "strand": strand})
        else:
            parts.append({"start": a, "end": b, "strand": strand})
    if strand == "reverse":
        # Each part's own pieces stay in their order round the circle, and the parts are
        # read from the last; a part split at the origin reads from its end piece first.
        parts.reverse()
    return parts


def _placed(
    kind: str, parts: list[dict[str, Any]], strand: str, length: int, topology: str
) -> dict[str, Any]:
    first, last = extent(parts, length, topology)
    feature: dict[str, Any] = {"type": kind[:64], "start": first, "end": last, "strand": strand}
    if len(parts) > 1:
        feature["parts"] = parts[:MAX_PARTS]
    return feature


def _snapgene_feature(
    record: Record, element: ElementTree.Element, length: int, topology: str
) -> dict[str, Any] | None:
    say = record.warnings.append
    what = _described(element)
    kind = element.get("type") or "misc_feature"
    direction = (element.get("directionality") or "0").strip()
    strand = {"1": "forward", "2": "reverse"}.get(direction, "none")
    if direction == "3":
        say(
            f"{record.label}: {what} is marked as running both ways, which a feature's "
            "strand can't say, so it's kept with no strand."
        )
    segments = [s for s in element.iter("Segment") if (s.get("type") or "standard") != "gap"]
    ranges: list[tuple[int, int]] = []
    for s in segments:
        found = re.fullmatch(r"\s*([0-9]+)\s*-\s*([0-9]+)\s*", s.get("range") or "")
        if not found:
            say(f"{record.label}: {what} has a segment at {s.get('range')!r}, so it was left out.")
            return None
        ranges.append((int(found[1]), int(found[2])))
    if not ranges:
        say(f"{record.label}: {what} has no segments, so it was left out.")
        return None
    where = ", ".join(f"{a}-{b}" for a, b in ranges)
    parts = _snapgene_parts(record, ranges, strand, length, topology, what, where)
    if parts is None:
        return None
    named = [plain(s.get("name")) for s in segments if plain(s.get("name"))]
    if named:
        say(
            f"{record.label}: {what}'s segment names ({', '.join(named)}) aren't kept, as a "
            "feature's parts have none."
        )
    if element.get("cleavageArrows"):
        say(
            f"{record.label}: {what}'s cleavage sites ({element.get('cleavageArrows')}) "
            "aren't kept."
        )
    qualifiers: dict[str, str] = {}
    for q in element.iter("Q"):
        key = (q.get("name") or "").strip()
        given = [
            str(v.get("int"))
            if v.get("int") is not None
            else plain(v.get("text") if v.get("text") is not None else v.get("predef"))
            for v in q.iter("V")
        ]
        value = "; ".join(v for v in given if v)
        if not key:
            continue
        if key in qualifiers:
            qualifiers[key] += "; " + value
        elif len(qualifiers) < MAX_QUALIFIERS:
            qualifiers[key] = value
    for key, value in qualifiers.items():
        if len(value) > MAX_QUALIFIER:
            qualifiers[key] = value[:MAX_QUALIFIER]
            say(f"{record.label}: a {kind}'s /{key} was cut.")
    feature = _placed(kind, parts, strand, length, topology)
    if len(parts) > MAX_PARTS:
        say(f"{record.label}: a {kind}'s first {MAX_PARTS} parts only.")
    label = plain(element.get("name")) or next(
        (qualifiers[q] for q in ("label", "gene", "locus_tag", "product") if q in qualifiers), ""
    )
    if label:
        feature["name"] = label[:200]
    if qualifiers:
        feature["qualifiers"] = qualifiers
    return feature


def _number(text: str | None) -> float:
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def _snapgene_primers(
    record: Record, primers: ElementTree.Element, length: int, topology: str
) -> list[dict[str, Any]]:
    """Each primer's binding sites as `primer_bind` features, and each primer as a Primer to
    create. SnapGene keeps sites a primer anneals to below the file's hybridization settings
    (too few bases, or too low a Tm) but doesn't show them, so neither does the import.
    Binding sites count from 0, where features count from 1."""
    say = record.warnings.append
    settings = primers.find("HybridizationParams")
    least = int(_number(settings.get("minContinuousMatchLen"))) if settings is not None else 0
    coolest = _number(settings.get("minMeltingTemperature")) if settings is not None else 0.0
    features: list[dict[str, Any]] = []
    for primer in primers.iter("Primer"):
        name = plain(primer.get("name"))
        called = name or "without a name"
        sites: dict[tuple[str, str], ElementTree.Element] = {}
        for site in primer.iter("BindingSite"):
            sites.setdefault((site.get("location") or "", site.get("boundStrand") or "0"), site)
        strands: set[str] = set()
        weak: list[str] = []
        for (location, bound), site in sites.items():
            found = re.fullmatch(r"\s*([0-9]+)\s*-\s*([0-9]+)\s*", location)
            strand = "reverse" if bound.strip() == "1" else "forward"
            if not found:
                say(f"{record.label}: primer {called}'s binding site at {location!r} was left out.")
                continue
            a, b = int(found[1]) + 1, int(found[2]) + 1
            annealed = site.get("annealedBases")
            tm = site.get("meltingTemperature")
            if (annealed is not None and len(annealed) < least) or (
                tm is not None and _number(tm) < coolest
            ):
                weak.append(f"{a}..{b} ({strand})")
                continue
            parts = _snapgene_parts(
                record,
                [(a, b)],
                strand,
                length,
                topology,
                f"primer {called}'s binding site",
                f"{a}..{b}",
            )
            if parts is None:
                continue
            feature = _placed("primer_bind", parts, strand, length, topology)
            if name:
                feature["name"] = name[:200]
            features.append(feature)
            strands.add(strand)
        if weak:
            say(
                f"{record.label}: primer {called} binds at {', '.join(weak)} below the file's "
                f"hybridization settings (at least {least} bases annealed, at a Tm of at least "
                f"{coolest:g} °C). SnapGene doesn't show such sites, so they were left out."
            )
        raw = re.sub(r"\s", "", primer.get("sequence") or "").upper()
        why = ""
        if not raw:
            why = "it has no bases"
        elif len(raw) > PRIMER_BASES:
            why = f"it's {len(raw):,} bases, and a primer is at most {PRIMER_BASES}"
        elif not BASES.match(raw):
            position, letter = next((i, c) for i, c in enumerate(raw, 1) if not BASES.match(c))
            why = f"its bases have {letter!r} at base {position}, which isn't an IUPAC base"
        if why:
            say(
                f"{record.label}: primer {called} isn't kept as a primer: {why}. Its binding "
                "sites are kept as features."
            )
            continue
        values: dict[str, Any] = {"name": name[:255], "bases": raw}
        description = plain(primer.get("description"), lines=True)
        if description:
            values["description"] = description[:10_000]
        if len(strands) == 1:
            values["direction"] = strands.pop()
        record.primers.append({k: v for k, v in values.items() if v})
    return features


def _snapgene_notes(record: Record, notes: ElementTree.Element) -> dict[str, Any]:
    """What SnapGene's notes say that a DNA sequence has a field for, saying what it
    hasn't. Its dates, who made it and SnapGene's own IDs are the file's, which is kept."""
    say = record.warnings.append
    out: dict[str, Any] = {}
    description = plain(notes.findtext("Description"), lines=True)
    comments = plain(notes.findtext("Comments"), lines=True)
    if comments and comments != description:
        description = f"{description}\n\nComments: {comments}" if description else comments
    if description:
        out["description"] = description
    accession = plain(notes.findtext("AccessionNumber"))
    if accession:
        if ACCESSION_FIELD.fullmatch(accession):
            out["accession"] = accession
        else:
            say(
                f"{record.label}: its accession number, {accession[:200]}, isn't one an "
                "accession can be (letters, digits, _ and .), so it's left out."
            )
    organism = plain(notes.findtext("Organism"))
    if organism:
        say(
            f"{record.label}: its organism, {organism[:200]}, has no NCBI Taxonomy ID (a "
            "SnapGene file gives only its name), so it's left out."
        )
    unkept: list[str] = []
    kind = plain(notes.findtext("Type"))
    if kind:
        unkept.append(f"its type ({kind[:100]})")
    if plain(notes.findtext("ConfirmedExperimentally")) == "1":
        unkept.append("that it was confirmed experimentally")
    host = plain(notes.findtext("TransformedInto"))
    if host and host.lower() != "unspecified":
        unkept.append(f"what it was transformed into ({host[:100]})")
    references = notes.findall("References/Reference")
    if references:
        pubmed = [r.get("pubMedID") for r in references if r.get("pubMedID")]
        unkept.append(
            f"its {len(references)} reference{'s' if len(references) != 1 else ''}"
            + (f" (PubMed {', '.join(str(p) for p in pubmed)})" if pubmed else "")
        )
    if unkept:
        say(
            f"{record.label}: these of its SnapGene notes aren't kept, as a DNA sequence has "
            "no field for them (the file is kept, as the record's origin): "
            f"{', '.join(unkept)}."
        )
    return out


def _snapgene_ends(record: Record, properties: ElementTree.Element) -> None:
    """A linear molecule's ends as SnapGene gives them: an overhang (its stickiness) or a
    modification, which a linear duplex, read as fully paired (ADR 0168), can't hold."""
    given: list[str] = []
    for end in ("Upstream", "Downstream"):
        sticky = plain(properties.findtext(f"{end}Stickiness"))
        if sticky and sticky != "0":
            given.append(f"an {end.lower()} stickiness of {sticky}")
    for end in ("Upstream", "Downstream"):
        modified = plain(properties.findtext(f"{end}Modification"))
        if modified and modified != "Unmodified":
            given.append(f"an {end.lower()} modification ({modified})")
    if given:
        record.warnings.append(
            f"{record.label}: SnapGene gives its ends {_listed(given)}, which isn't kept: Iki "
            "reads a linear molecule as fully paired, with plain ends (ADR 0168)."
        )


# --- The run -------------------------------------------------------------------------------------


def text_lines(path: Path, counted: list[int]) -> Iterator[str]:
    """The file's lines as text, counting the bytes read so far in ``counted[0]``."""
    with path.open("rb") as given:
        for number, raw in enumerate(given):
            counted[0] += len(raw)
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise UnreadableError(
                    f"Line {number + 1} isn't UTF-8 text: give a FASTA, GenBank or SnapGene file."
                ) from None
            yield line.removeprefix("\ufeff") if number == 0 else line


def read(
    path: Path, topology: str, counted: list[int], name: str = ""
) -> tuple[str, Iterator[Record]]:
    """What kind of file it is, and its records as they're read. A SnapGene file is told by
    its first bytes, and read whole: it holds one sequence, and is binary."""
    with path.open("rb") as given:
        start = given.read(len(SNAPGENE))
    if start[:5] == SNAPGENE[:5] and start[5:] and SNAPGENE[5:].startswith(start[5:]):
        data = path.read_bytes()
        counted[0] = len(data)
        return "SnapGene file", iter([snapgene(data, name or path.name)])
    lines = text_lines(path, counted)
    skipped: list[str] = []
    for line in lines:
        skipped.append(line)
        if line.strip():
            break
    first = skipped[-1] if skipped else ""
    every = itertools.chain(skipped, lines)
    if first.startswith(">"):
        return "FASTA file", fasta(every, topology)
    if first.startswith("LOCUS"):
        return "GenBank file", genbank(every)
    raise UnreadableError(
        "It isn't FASTA (records start with '>'), GenBank (records start with LOCUS) or a "
        "SnapGene file."
    )


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    file_id = run["params"]["file"]
    given = run["files"][file_id]
    size = max(int(given.get("size") or 0), 1)
    done = [0]
    try:
        source, records = read(
            Path(given["path"]), run["params"].get("topology", "linear"), done, given["name"]
        )

        def origin(record: Record) -> dict[str, Any]:
            found = {
                "source": source,
                "id": record.values.get("name") or record.label,
                "file": file_id,
                "sha256": hashlib.sha256(
                    record.text if isinstance(record.text, bytes) else record.text.encode()
                ).hexdigest(),
            }
            if record.values.get("accession"):
                found["version"] = record.values["accession"]
            return found

        count = finish(records, origin, lambda: done[0] / size)
        if not count:
            raise UnreadableError("It holds no records.")
    except UnreadableError as why:
        print(f"::error {given['name']}: {why}", flush=True)
        sys.exit(1)


class _Creates:
    """Where the creates go: output.json while they're few and small, then a creates
    file (``creates_file``, one per line), which a run may fill with millions."""

    def __init__(self) -> None:
        self.held: list[str] = []
        self.size = 0
        self.file: TextIO | None = None
        #: The organisms the creates are from (ADR 0140), each created once, first.
        self.organisms: dict[str, Organism] = {}
        #: Creates with a key others refer to, and those that refer to them (a SnapGene
        #: file's sequence and its primers), which only output.json's `creates` may hold.
        self.keyed: list[dict[str, Any]] = []

    def add(self, create: dict[str, Any], organism: Organism | None = None) -> None:
        if organism is not None:
            self.organisms.setdefault(organism.key, organism)
            create["values"] = {**create["values"], "organism": {"$create": organism.key}}
        if "key" in create or any(
            isinstance(v, dict) and "$create" in v
            for n, v in create["values"].items()
            if n != "organism"
        ):
            self.keyed.append(create)
            return
        line = json.dumps(create, separators=(",", ":"))
        if self.file is None:
            self.held.append(line)
            self.size += len(line)
            if len(self.held) <= INLINE_RECORDS and self.size <= INLINE_BYTES:
                return
            out = Path(os.environ["IKI_OUT_DIR"]) / CREATES_FILE
            self.file = out.open("w", encoding="utf-8")
            self.file.writelines(f"{held}\n" for held in self.held)
            self.held = []
            return
        self.file.write(f"{line}\n")

    def output(self) -> dict[str, Any]:
        organisms = [o.create() for o in self.organisms.values()]
        if self.file is None:
            return {"creates": [*organisms, *self.keyed, *(json.loads(line) for line in self.held)]}
        self.file.close()
        return {
            **({"creates": [*organisms, *self.keyed]} if organisms or self.keyed else {}),
            "creates_file": {"$file": CREATES_FILE},
        }


def finish(
    records: Iterable[Record],
    origin: Callable[[Record], dict[str, Any]],
    progress: Callable[[], float] | None = None,
) -> int:
    """Writes the sequences to create, each with where it came from, to $IKI_OUTPUT (or,
    past a few thousand, to a creates file it names). Returns how many."""
    creates = _Creates()
    messages: list[dict[str, str]] = []
    warned = 0
    done = 0
    for done, record in enumerate(records, start=1):
        if done > MAX_RECORDS:
            raise UnreadableError(
                f"It holds more than the {MAX_RECORDS:,} records one run imports: split it."
            )
        found = origin(record)
        if record.made:
            found["made"] = record.made
        create: dict[str, Any] = {
            "type": "iki/dna.DNASequence",
            "values": record.values,
            "origin": found,
        }
        if record.primers:
            # Its primers are bound to it, as the file binds them.
            create["key"] = f"sequence:{done}"
        creates.add(create, record.organism)
        for primer in record.primers:
            named = primer.get("name") or f"Primer {len(primer['bases'])} b"
            creates.add(
                {
                    "type": "iki/dna.Primer",
                    "values": {**primer, "target": {"$create": create["key"]}},
                    "origin": {**found, "id": named[:255]},
                }
            )
        for warning in record.warnings:
            warned += 1
            if len(messages) < MAX_MESSAGES:
                messages.append({"level": "warning", "text": warning[:2000]})
        if done <= 1000:
            # Where its topology came from, so a GenBank file's never reads as the param's.
            said = f", as {record.topology_from} says" if record.topology_from else ""
            print(
                f"{record.label}: {len(record.values['bases']):,} bp, "
                f"{record.values['topology']}{said}"
            )
        if done % 1000 == 0:
            fraction = progress() if progress is not None else 0.0
            print(f"::progress {min(fraction, 1.0):.2f} {done:,} records", flush=True)
    if done > 1000:
        print(f"… and {done - 1000:,} more records.")
    if warned > MAX_MESSAGES:
        messages.append(
            {"level": "warning", "text": f"And {warned - MAX_MESSAGES:,} more warnings."}
        )
    if done:
        print(f"::progress 1.00 {done:,} records", flush=True)
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        **creates.output(),
        "metrics": {"records": done},
        "messages": messages,
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
    return done


if __name__ == "__main__":
    main()
