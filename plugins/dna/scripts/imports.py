"""The DNA plugin's imports: `file` reads DNA sequences from a FASTA or GenBank file.

It reads the run input from $IKI_INPUT and writes the sequences to create, each with
where it came from, to $IKI_OUTPUT, as every action's command does. A record that
can't be read fails the whole import with a `::error` line that says which and why,
so nothing is half imported.

GenBank records give their name, definition, accession and version, organism,
topology and features. A feature's location is kept as its first and last base and
its strand; locations with several parts (`join(...)`), fuzzy ends (`<1`, `>99`) or
between two bases (`5^6`) keep GenBank's own text in the `location` qualifier too.
Features on other records (`J00194.1:100..202`) are left out, with a warning. A RefSeq
model's (XM_, XR_) COMMENT says NCBI's annotation pipeline predicted it, with which gene
prediction method: its origin says so, as NCBI's word (ADR 0106). Other records don't
say how their sequence was made, so theirs don't.

FASTA headers give a name (the first word) and a description (the rest). A word of
the header that looks like an INSDC accession with its version (`L09137.2`,
`NM_000014.6`), including inside NCBI's `gb|…|` style, is kept as the accession.
FASTA doesn't say whether a sequence is circular, so the `topology` param does.

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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

BASES = re.compile(r"^[ACGTRYSWKMBDHVN]*$")
#: A nucleotide accession without its version, as INSDC and RefSeq write them: letters and
#: digits (L09137, CP009273, AAAA01000001), or RefSeq's two letters and an underscore before
#: digits (NM_000546) or before an INSDC accession (NZ_CP009273, NZ_AAAA01000001).
ACCESSION_BODY = r"(?:[A-Z]{2}_[A-Z]{0,6}|[A-Z]{1,6})[0-9]{5,11}"
#: One with its version, as a FASTA header names it.
ACCESSION = re.compile(rf"^{ACCESSION_BODY}\.[0-9]+$")
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
LIMITS = {"name": 255, "description": 10_000, "organism": 200, "accession": 64}
#: How NCBI's COMMENT starts for a RefSeq model, a record its annotation pipeline predicted.
MODEL = "MODEL REFSEQ: This record is predicted by automated computational analysis"


class UnreadableError(Exception):
    """A record that can't be imported, and why."""


@dataclass
class Record:
    #: Its name in the file, which the run's output names it by.
    label: str
    values: dict[str, Any]
    text: str
    warnings: list[str] = field(default_factory=list)
    #: How the record says its sequence was made, if it does (ADR 0106).
    made: dict[str, Any] | None = None


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
        found.values = fit(
            {
                "name": name,
                "description": words[1].strip() if len(words) > 1 else "",
                "accession": accession_in(name),
                "bases": clean_bases(label, "".join(bases)),
                "topology": topology,
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
    organism = _organism(sections.get("SOURCE", []))
    # VERSION names the accession with its version (L09137.2); old files may lack it.
    named = " ".join(sections.get("VERSION", []) or sections.get("ACCESSION", [])).split()
    accession = named[0] if named else ""
    bases = clean_bases(label, "".join(origin))
    topology = "circular" if "circular" in (w.lower() for w in locus[2:]) else "linear"
    values = {
        "name": name,
        "description": " ".join(sections.get("DEFINITION", [])).strip().removesuffix("."),
        "organism": organism,
        "accession": accession if re.fullmatch(r"[A-Za-z0-9_.]+", accession) else "",
        "bases": bases,
        "topology": topology,
        "features": _features(record, features, len(bases), topology),
    }
    stranded = strandedness(locus[2:])
    if stranded:
        values["strandedness"] = stranded
    record.values = fit(values, record)
    record.made = made(sections.get("COMMENT", []))
    return record


#: LOCUS's molecule types (INSDC): DNA, RNA, or a kind of RNA, maybe with ss-, ds- or ms-.
MOLECULE = re.compile(r"^(ss-|ds-|ms-)?([a-z]*RNA|DNA)$", re.IGNORECASE)


def strandedness(locus: list[str]) -> str | None:
    """What LOCUS says of the molecule's strands: `single` for ss-DNA and for RNA (an mRNA
    is kept as its cDNA's bases, GenBank's T alphabet, but the molecule is one strand),
    `double` for ds-DNA and ds-RNA. Plain DNA doesn't say, so the field keeps its default."""
    for word in locus:
        found = MOLECULE.match(word)
        if not found:
            continue
        prefix, kind = (found[1] or "").lower(), found[2].upper()
        if prefix == "ms-":
            return None  # mixed: neither
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


def _features(record: Record, lines: list[str], length: int, topology: str) -> list[dict[str, Any]]:
    """GenBank's feature table: a key in columns 6 to 20, the location after it, then
    /qualifiers, each line continuing the one before."""
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
    out: list[dict[str, Any]] = []
    for key, location, raw in parsed:
        if key == "source":
            continue  # the whole record: its organism is kept on the sequence
        if len(out) == MAX_FEATURES:
            record.warnings.append(f"{record.label}: only its first {MAX_FEATURES} features.")
            break
        feature = _feature(record, key, location, raw, length, topology)
        if feature is not None:
            out.append(feature)
    return out


class LocationError(ValueError):
    """A location this reading of INSDC's grammar doesn't take."""


#: A part's place: `a..b` (either end maybe fuzzy, `<a`, `>b`), `a^b` (between two bases),
#: or a single base.
RANGE = re.compile(r"<?([0-9]+)\.\.>?([0-9]+)")
BETWEEN = re.compile(r"([0-9]+)\^([0-9]+)")
BASE = re.compile(r"[<>]?([0-9]+)")


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
    between two bases (`5^6`, flagged `between`)."""
    location = location.strip()
    for wrapper in ("complement", "join", "order"):
        if location.startswith(f"{wrapper}(") and location.endswith(")"):
            inner = location[len(wrapper) + 1 : -1]
            if wrapper == "complement":
                flipped = "reverse" if strand == "forward" else "forward"
                return list(reversed(parts_of(inner, flipped)))
            return [part for piece in _top_level(inner) for part in parts_of(piece, strand)]
    if found := RANGE.fullmatch(location):
        return [{"start": int(found[1]), "end": int(found[2]), "strand": strand}]
    if found := BETWEEN.fullmatch(location):
        return [{"start": int(found[1]), "end": int(found[2]), "strand": strand, "between": True}]
    if found := BASE.fullmatch(location):
        return [{"start": int(found[1]), "end": int(found[1]), "strand": strand}]
    raise LocationError(location)


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
        parts = parts_of(location)
    except LocationError:
        parts = []
    circle = topology == "circular"

    def fits(part: dict[str, Any]) -> bool:
        a, b = part["start"], part["end"]
        if not (1 <= a <= length and 1 <= b <= length):
            return False
        if part.get("between"):
            # Between two neighbouring bases, or round the origin between the last and first.
            return b == a + 1 or (circle and (a, b) == (length, 1))
        return a <= b or circle  # a range ending before it starts crosses the origin

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
    if len(parts) > 1 or parts[0].get("between"):
        # Where it lies, part by part: a join's exons, not its introns; a site, no base.
        feature["parts"] = parts[:MAX_PARTS]
        if len(parts) > MAX_PARTS:
            record.warnings.append(f"{record.label}: a {key}'s first {MAX_PARTS} parts only.")
    label = next(
        (qualifiers[q] for q in ("label", "gene", "locus_tag", "product") if q in qualifiers),
        "",
    )
    if label:
        feature["name"] = label[:200]
    if qualifiers:
        feature["qualifiers"] = qualifiers
    return feature


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
                    f"Line {number + 1} isn't UTF-8 text: give a FASTA or GenBank file."
                ) from None
            yield line.removeprefix("\ufeff") if number == 0 else line


def read(path: Path, topology: str, counted: list[int]) -> tuple[str, Iterator[Record]]:
    """What kind of file it is, and its records as they're read."""
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
        "It isn't FASTA (records start with '>') or GenBank (records start with LOCUS)."
    )


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    file_id = run["params"]["file"]
    given = run["files"][file_id]
    size = max(int(given.get("size") or 0), 1)
    done = [0]
    try:
        source, records = read(Path(given["path"]), run["params"].get("topology", "linear"), done)

        def origin(record: Record) -> dict[str, Any]:
            found = {
                "source": source,
                "id": record.values.get("name") or record.label,
                "file": file_id,
                "sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
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

    def add(self, create: dict[str, Any]) -> None:
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
        if self.file is None:
            return {"creates": [json.loads(line) for line in self.held]}
        self.file.close()
        return {"creates_file": {"$file": CREATES_FILE}}


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
        creates.add({"type": "iki/dna.DNASequence", "values": record.values, "origin": found})
        for warning in record.warnings:
            warned += 1
            if len(messages) < MAX_MESSAGES:
                messages.append({"level": "warning", "text": warning[:2000]})
        if done <= 1000:
            print(f"{record.label}: {len(record.values['bases']):,} bp")
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
