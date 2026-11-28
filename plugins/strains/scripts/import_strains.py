"""Read a UTF-8 strain catalogue into one Iki run output."""

import csv
import hashlib
import re
from pathlib import Path
from typing import Any

import iki_action as iki

#: Each value a strain takes from the file, and the header Iki's own exchange format gives it.
FIELDS = ("authority", "accession", "taxon_id", "scientific_name", "kind", "name", "genotype")
#: Values a run can give for every row instead of a column.
CONSTANTS = ("authority", "kind")
#: Values that can be left out of a file altogether.
OPTIONAL = ("name", "genotype")


def columns(headers: list[str], params: dict[str, Any]) -> tuple[dict[str, int], dict[str, str]]:
    """Where each field is in a row, and the values given for every row instead."""
    constant = {f: params[f] for f in CONSTANTS if params.get(f)}
    where: dict[str, int] = {}
    named: dict[str, str] = {}
    for f in FIELDS:
        header = params.get(f"{f}_column") or f
        found = [i for i, h in enumerate(headers) if h.strip() == header]
        if len(found) > 1:
            raise ValueError(f"The header has more than one {header} column.")
        if f in constant:
            if found:
                raise ValueError(f"Give {f} in the {header} column or for every row, not both.")
            continue
        if not found:
            if f in OPTIONAL and header == f:
                continue
            given = ", or give it for every row" if f in CONSTANTS else ""
            raise ValueError(f"The file has no {header} column for {f}{given}.")
        if header in named.values():
            other = next(k for k, v in named.items() if v == header)
            raise ValueError(f"{other} and {f} name the same column, {header}.")
        named[f] = header
        where[f] = found[0]
    return where, constant


def catalogue(
    path: Path, file_id: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    # Keep the original line endings for the digest of each CSV record, including
    # embedded newlines in quoted cells. Excel sometimes writes a UTF-8 BOM.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        lines = stream.readlines()
    reader = csv.reader(lines, strict=True)
    headers = next(reader, [])
    where, constant = columns(headers, params or {})

    creates: list[dict[str, Any]] = []
    taxa: dict[int, str] = {}
    strains: set[tuple[str, str]] = set()
    while True:
        start = reader.line_num
        cells = next(reader, None)
        if cells is None:
            break
        if not cells:
            continue
        line = start + 1
        if len(cells) != len(headers):
            raise ValueError(f"Line {line} has {len(cells)} cells. Expected {len(headers)}.")
        row = {f: cells[i].strip() for f, i in where.items()}
        row.update(constant)
        for f in OPTIONAL:
            row.setdefault(f, "")
        authority, accession = row["authority"], row["accession"]
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", authority):
            raise ValueError(f"Line {line} needs an uppercase collection or lab code.")
        if not re.fullmatch(r"\S{1,128}", accession):
            raise ValueError(f"Line {line} needs an accession without spaces.")
        if row["kind"] not in {"bacterium", "yeast"}:
            raise ValueError(f"Line {line} needs bacterium or yeast in the kind column.")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", row["taxon_id"]):
            raise ValueError(f"Line {line} needs a positive NCBI Taxonomy ID.")
        taxon_id = int(row["taxon_id"])
        scientific_name = row["scientific_name"]
        if not re.fullmatch(r"[A-Za-z0-9\['][^\t\r\n]{0,254}", scientific_name):
            raise ValueError(f"Line {line} needs a scientific name on one line.")
        if len(row["name"]) > 255 or len(row["genotype"]) > 4000:
            raise ValueError(f"Line {line} exceeds 255 characters for name or 4000 for genotype.")
        if any(char in row["genotype"] for char in "\t\r\n"):
            raise ValueError(f"Line {line} needs a genotype designation on a single line.")
        if (authority, accession) in strains:
            raise ValueError(f"Line {line} repeats {authority} {accession}.")
        strains.add((authority, accession))
        if len(strains) > 1000:
            raise ValueError("Split catalogues with more than 1000 strains into smaller files.")

        taxon_key = f"taxon_{taxon_id}"
        if taxon_id not in taxa:
            taxa[taxon_id] = scientific_name
            creates.append(
                {
                    "type": "iki/core.Organism",
                    "key": taxon_key,
                    "fills": ["scientific_name"],
                    "values": {"taxon_id": taxon_id, "scientific_name": scientific_name},
                }
            )
        elif taxa[taxon_id] != scientific_name:
            raise ValueError(f"Line {line} gives a second scientific name for taxon {taxon_id}.")

        values = {
            k: row[k] for k in ("authority", "accession", "kind", "name", "genotype") if row[k]
        }
        creates.append(
            {
                "type": "iki/strains.BaseStrain",
                "values": {**values, "organism": {"$create": taxon_key}},
                "origin": {
                    "source": "Strain catalogue CSV",
                    "id": f"{authority} {accession}",
                    "file": file_id,
                    "sha256": hashlib.sha256(
                        "".join(lines[start : reader.line_num]).encode()
                    ).hexdigest(),
                },
            }
        )
    if not strains:
        raise ValueError("The catalogue contains no strain rows.")
    return creates


def register(run: iki.Run) -> dict[str, Any]:
    params = run.params
    file_id = params["file"]
    given = run["files"][file_id]
    try:
        creates = catalogue(run.file(file_id), file_id, params)
    except (csv.Error, ValueError) as exc:
        raise iki.Refused(f"{given['name']}: {exc}") from exc
    return {"creates": creates}


if __name__ == "__main__":
    iki.main(register)
