"""The protein plugin's imports: reads proteins from a FASTA file.

It reads the run input from $IKI_INPUT and writes the proteins to create, each with
where it came from, to $IKI_OUTPUT, as every action's command does. A record that
can't be read fails the whole import with a `::error` line that says which and why,
so nothing is half imported.

FASTA headers give a name (the first word) and a description (the rest). UniProt's
headers (`>sp|P69905|HBA_HUMAN Hemoglobin subunit alpha OS=Homo sapiens OX=9606
GN=HBA1 PE=1 SV=2`) give the entry name, the protein's name, its accession, organism
and gene. NCBI's (`>NP_000549.1 hemoglobin subunit alpha [Homo sapiens]`) give the
organism in brackets.

Residues are the 20 standard amino acids, U, O, and B, Z, J or X for ambiguous or
unknown ones. A final stop (*) is left off. A stop inside a sequence, a gap, or a
letter that isn't a residue fails the import.

Files are read a line at a time, so they may hold millions of records. Past a thousand
(or a megabyte of them), the creates go in a creates file (`creates_file`, one per
line), which Iki checks and then writes all at once, or not at all (ADR 0056).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

RESIDUES = re.compile(r"^[ACDEFGHIKLMNPQRSTVWYUOBZJX]*$")
UNIPROT = re.compile(
    r"^(?:sp|tr)\|(?P<accession>[A-Z0-9]+(?:-[0-9]+)?)\|(?P<entry>\S+)(?: (?P<rest>.*))?$"
)
#: As the uniprot_accession field takes them: P69905, or P69905-2 for an isoform.
ACCESSION = re.compile(
    r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-[0-9]+)?$"
)
#: UniProt's `KEY=value` fields, after the protein's name.
UNIPROT_FIELDS = re.compile(r" (OS|OX|GN|PE|SV)=")
ORGANISM_IN_BRACKETS = re.compile(r"^(?P<description>.*?)\s*\[(?P<organism>[^\[\]]+)\]$")
#: The most records one run imports.
MAX_RECORDS = 10_000_000
#: Up to these many (and bytes of them), creates go in output.json; past them, in a file.
INLINE_RECORDS = 1_000
INLINE_BYTES = 1_000_000
CREATES_FILE = "creates.ndjson"
MAX_MESSAGES = 99
LIMITS = {"name": 255, "description": 10_000, "organism": 200, "gene": 100}


class UnreadableError(Exception):
    """A record that can't be imported, and why."""


@dataclass
class Record:
    #: Its name in the file, which the run's output names it by.
    label: str
    values: dict[str, Any]
    text: str
    warnings: list[str] = field(default_factory=list)
    #: UniProt's sequence version (SV=2), where the header gives it.
    version: str = ""


# --- Residues ------------------------------------------------------------------------------------


def clean_residues(label: str, raw: str) -> str:
    residues = re.sub(r"[\s0-9]", "", raw).upper().removesuffix("*")
    if not residues:
        raise UnreadableError(f"{label} has no residues.")
    if not RESIDUES.match(residues):
        position, found = next((i, r) for i, r in enumerate(residues, 1) if not RESIDUES.match(r))
        if found == "*":
            raise UnreadableError(
                f"{label} has a stop (*) at residue {position}: import the protein up to it."
            )
        if found in "-.":
            raise UnreadableError(
                f"{label} has a gap ({found}) at residue {position}: import unaligned sequences."
            )
        raise UnreadableError(
            f"{label} has {found!r} at residue {position}, which isn't an amino acid's code."
        )
    return residues


def fit(values: dict[str, Any], record: Record) -> dict[str, Any]:
    """Values cut to what the fields take, saying what was cut."""
    out = {}
    for name, value in values.items():
        limit = LIMITS.get(name)
        if isinstance(value, str) and limit is not None and len(value) > limit:
            record.warnings.append(f"{record.label}: its {name} was cut to {limit} characters.")
            value = value[:limit]
        if value not in (None, ""):
            out[name] = value
    return out


# --- Headers -------------------------------------------------------------------------------------


def header_values(header: str) -> tuple[dict[str, str], str]:
    """The values a FASTA header gives, and UniProt's sequence version if it's UniProt's."""
    uniprot = UNIPROT.match(header)
    if uniprot is not None:
        rest = uniprot["rest"] or ""
        parts = UNIPROT_FIELDS.split(f" {rest}")
        description = parts[0].strip()
        fields = dict(zip(parts[1::2], (p.strip() for p in parts[2::2]), strict=True))
        values = {
            "name": uniprot["entry"],
            "description": description,
            "uniprot_accession": uniprot["accession"]
            if ACCESSION.match(uniprot["accession"])
            else "",
            "organism": fields.get("OS", ""),
            "gene": fields.get("GN", ""),
        }
        return values, fields.get("SV", "")
    words = header.split(None, 1)
    name = words[0] if words else ""
    description = words[1].strip() if len(words) > 1 else ""
    organism = ""
    bracketed = ORGANISM_IN_BRACKETS.match(description)
    if bracketed is not None:
        description, organism = bracketed["description"], bracketed["organism"].strip()
    return {"name": name, "description": description, "organism": organism}, ""


# --- FASTA ---------------------------------------------------------------------------------------


def fasta(lines: Iterable[str]) -> Iterator[Record]:
    """Each record of a FASTA file, from its lines (with their line ends), as it's read."""
    count = 0
    header: str | None = None
    raw: list[str] = []
    residues: list[str] = []

    def record() -> Record:
        assert header is not None  # noqa: S101 - only called after a header
        values, version = header_values(header)
        name = values.get("uniprot_accession") or values["name"]
        label = f"Record {count}" + (f" ({name})" if name else "")
        found = Record(label, {}, "".join(raw), version=version)
        values["sequence"] = clean_residues(label, "".join(residues))
        found.values = fit(values, found)
        return found

    for line in lines:
        if line.startswith(">"):
            if header is not None:
                yield record()
            count += 1
            header, raw, residues = line[1:].strip(), [], []
        elif line.strip() and not line.startswith(";"):
            if header is None:
                raise UnreadableError("This FASTA file has residues before its first '>' header.")
            residues.append(line)
        if header is not None:
            raw.append(line)
    if header is not None:
        yield record()


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
                    f"Line {number + 1} isn't UTF-8 text: give a FASTA file."
                ) from None
            yield line.removeprefix("﻿") if number == 0 else line


def read(path: Path, counted: list[int]) -> Iterator[Record]:
    """The file's records as they're read."""
    lines = text_lines(path, counted)
    first = ""
    skipped: list[str] = []
    for line in lines:
        skipped.append(line)
        if line.strip():
            first = line
            break
    if not first.startswith(">"):
        raise UnreadableError("It isn't FASTA: records start with '>'.")

    def every() -> Iterator[str]:
        yield from skipped
        yield from lines

    return fasta(every())


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    file_id = run["params"]["file"]
    given = run["files"][file_id]
    size = max(int(given.get("size") or 0), 1)
    done = [0]
    try:
        records = read(Path(given["path"]), done)

        def origin(record: Record) -> dict[str, Any]:
            return {
                "source": "FASTA file",
                "id": record.values.get("name") or record.label,
                "file": file_id,
                "sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
            }

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
    """Writes the proteins to create, each with where it came from, to $IKI_OUTPUT (or,
    past a thousand, to a creates file it names). Returns how many."""
    creates = _Creates()
    messages: list[dict[str, str]] = []
    warned = 0
    done = 0
    for done, record in enumerate(records, start=1):
        if done > MAX_RECORDS:
            raise UnreadableError(
                f"It holds more than the {MAX_RECORDS:,} records one run imports: split it."
            )
        creates.add({"type": "protein.Protein", "values": record.values, "origin": origin(record)})
        for warning in record.warnings:
            warned += 1
            if len(messages) < MAX_MESSAGES:
                messages.append({"level": "warning", "text": warning[:2000]})
        if done <= 1000:
            print(f"{record.label}: {len(record.values['sequence']):,} residues")
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
