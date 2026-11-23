"""Read a UTF-8 strain catalogue into one Iki run output."""

import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

COLUMNS = {"authority", "accession", "taxon_id", "scientific_name", "kind", "name", "genotype"}


def catalogue(path: Path, file_id: str) -> list[dict[str, Any]]:
    # Keep the original line endings for the digest of each CSV record, including
    # embedded newlines in quoted cells. Excel sometimes writes a UTF-8 BOM.
    with path.open(encoding="utf-8-sig", newline="") as stream:
        lines = stream.readlines()
    reader = csv.reader(lines, strict=True)
    headers = next(reader, [])
    if len(headers) != len(COLUMNS) or set(headers) != COLUMNS:
        raise ValueError("The header must contain each of " + ", ".join(sorted(COLUMNS)) + " once.")

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
        row = dict(zip(headers, (cell.strip() for cell in cells), strict=True))
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


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    file_id = run["params"]["file"]
    given = run["files"][file_id]
    try:
        creates = catalogue(Path(given["path"]), file_id)
    except (csv.Error, ValueError) as exc:
        print(f"::error {given['name']}: {exc}", flush=True)
        raise SystemExit(1) from exc
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps({"creates": creates}, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
