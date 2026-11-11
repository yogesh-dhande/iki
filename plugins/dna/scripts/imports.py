"""The DNA plugin's imports: `file` reads DNA sequences from a FASTA or GenBank file.

It reads the run input from $IKI_INPUT and writes the sequences to create, each with
where it came from, to $IKI_OUTPUT, as every action's command does. A record that
can't be read fails the whole import with a `::error` line that says which and why,
so nothing is half imported.

GenBank records give their name, definition, accession and version, organism,
topology and features. A feature's location is kept as its first and last base and
its strand; locations with several parts (`join(...)`), fuzzy ends (`<1`, `>99`) or
between two bases (`5^6`) keep GenBank's own text in the `location` qualifier too.
Features on other records (`J00194.1:100..202`) are left out, with a warning.

FASTA headers give a name (the first word) and a description (the rest). A word of
the header that looks like an INSDC accession with its version (`L09137.2`,
`NM_000014.6`), including inside NCBI's `gb|…|` style, is kept as the accession.
FASTA doesn't say whether a sequence is circular, so the `topology` param does.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

BASES = re.compile(r"^[ACGTRYSWKMBDHVN]*$")
ACCESSION = re.compile(r"^[A-Z]{1,6}_?[0-9]{5,9}\.[0-9]+$")
MAX_RECORDS = 10_000
MAX_FEATURES = 10_000
MAX_QUALIFIERS = 50
MAX_QUALIFIER = 10_000
LIMITS = {"name": 255, "description": 10_000, "organism": 200, "accession": 64}


class UnreadableError(Exception):
    """A record that can't be imported, and why."""


@dataclass
class Record:
    #: Its name in the file, which the run's output names it by.
    label: str
    values: dict[str, Any]
    text: str
    warnings: list[str] = field(default_factory=list)


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


def fasta(text: str, topology: str) -> list[Record]:
    records: list[Record] = []
    header: str | None = None
    lines: list[str] = []
    start = 0

    def close(end: int) -> None:
        if header is None:
            return
        words = header.split(None, 1)
        # NCBI's old `gb|L09137.2|` style names the record by its accession.
        name = (accession_in(words[0]) or words[0]) if words else ""
        label = f"Record {len(records) + 1}" + (f" ({name})" if name else "")
        record = Record(label, {}, text[start:end])
        record.values = fit(
            {
                "name": name,
                "description": words[1].strip() if len(words) > 1 else "",
                "accession": accession_in(name),
                "bases": clean_bases(label, "".join(lines)),
                "topology": topology,
            },
            record,
        )
        records.append(record)

    offset = 0
    for line in text.splitlines(keepends=True):
        if line.startswith(">"):
            close(offset)
            header, lines, start = line[1:].strip(), [], offset
        elif line.strip() and not line.startswith(";"):
            if header is None:
                raise UnreadableError("This FASTA file has bases before its first '>' header.")
            lines.append(line)
        offset += len(line)
    close(offset)
    return records


def accession_in(name: str) -> str:
    for word in name.split("|"):
        if ACCESSION.match(word):
            return word
    return ""


# --- GenBank -------------------------------------------------------------------------------------


def genbank(text: str) -> list[Record]:
    records: list[Record] = []
    start = 0
    lines: list[str] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        if not lines and not line.strip():
            start = offset + len(line)
        else:
            lines.append(line)
        offset += len(line)
        if line.startswith("//"):
            records.append(genbank_record(len(records) + 1, lines, text[start:offset]))
            lines, start = [], offset
    if any(line.strip() for line in lines):
        raise UnreadableError(
            f"Record {len(records) + 1} doesn't end with '//': is the file cut short?"
        )
    return records


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
    record.values = fit(
        {
            "name": name,
            "description": " ".join(sections.get("DEFINITION", [])).strip().removesuffix("."),
            "organism": organism,
            "accession": accession if re.fullmatch(r"[A-Za-z0-9_.]+", accession) else "",
            "bases": bases,
            "topology": topology,
            "features": _features(record, features, len(bases), topology),
        },
        record,
    )
    return record


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


def _strand(location: str) -> str:
    """`reverse` for `complement(…)`, and for a join or order of complemented parts."""
    if location.startswith("complement("):
        return "reverse"
    inner = re.fullmatch(r"(?:join|order)\((.*)\)", location)
    if inner and all(part.startswith("complement(") for part in inner[1].split(",")):
        return "reverse"
    return "forward"


def _feature(
    record: Record, key: str, location: str, raw: list[str], length: int, topology: str
) -> dict[str, Any] | None:
    if ":" in location:
        record.warnings.append(
            f"{record.label}: a {key} on another record ({location}) was left out."
        )
        return None
    strand = _strand(location)
    numbers = [int(n) for n in re.findall(r"[0-9]+", location)]
    if not numbers or not all(1 <= n <= length for n in numbers):
        record.warnings.append(f"{record.label}: a {key} at {location!r} was left out.")
        return None
    # Forward parts run in order, and may wrap the origin of a circular sequence (an end
    # before the start); otherwise it spans its lowest base to its highest.
    first, last = numbers[0], numbers[-1]
    if strand == "reverse" or (first > last and topology != "circular"):
        first, last = min(numbers), max(numbers)
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


def read(path: Path, topology: str) -> tuple[str, list[Record]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise UnreadableError("It isn't a text file: give a FASTA or GenBank file.") from None
    first = next((line for line in text.splitlines() if line.strip()), "")
    if first.startswith(">"):
        return "FASTA file", fasta(text, topology)
    if first.startswith("LOCUS"):
        return "GenBank file", genbank(text)
    raise UnreadableError(
        "It isn't FASTA (records start with '>') or GenBank (records start with LOCUS)."
    )


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    file_id = run["params"]["file"]
    given = run["files"][file_id]
    try:
        source, records = read(Path(given["path"]), run["params"].get("topology", "linear"))
        if not records:
            raise UnreadableError("It holds no records.")
        if len(records) > MAX_RECORDS:
            raise UnreadableError(
                f"It holds {len(records):,} records, more than the {MAX_RECORDS:,} one run "
                "imports: split it."
            )
    except UnreadableError as why:
        print(f"::error {given['name']}: {why}", flush=True)
        sys.exit(1)

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

    finish(records, origin)


def finish(records: list[Record], origin: Callable[[Record], dict[str, Any]]) -> None:
    """Writes the sequences to create, each with where it came from, to $IKI_OUTPUT."""
    creates = []
    messages = []
    for done, record in enumerate(records, start=1):
        creates.append(
            {"type": "dna.DNASequence", "values": record.values, "origin": origin(record)}
        )
        messages.extend({"level": "warning", "text": w[:2000]} for w in record.warnings)
        print(f"{record.label}: {len(record.values['bases']):,} bp")
        if done % 100 == 0 or done == len(records):
            print(f"::progress {done / len(records):.2f} {done} of {len(records)}", flush=True)
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": creates,
        "metrics": {"records": len(records)},
        "messages": messages[:99]
        + (
            [{"level": "warning", "text": f"And {len(messages) - 99:,} more warnings."}]
            if len(messages) > 99
            else []
        ),
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
