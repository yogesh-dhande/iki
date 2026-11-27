"""Read one official strain name from MGI_Strain.rpt, indexed by its MGI accession."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import iki_action as iki


def read_strain(path: Path, accession: str) -> tuple[str, str]:
    # MGI documents these three tab-separated fields as ID, strain name and strain type.
    # https://www.informatics.jax.org/downloads/reports/index.html
    selected = accession.encode("ascii")
    found = None
    with path.open("rb") as report:
        for number, row in enumerate(report, start=1):
            fields = row.rstrip(b"\r\n").split(b"\t")
            if fields[0] != selected:
                continue
            if found is not None:
                raise ValueError(f"{accession} occurs more than once in the report.")
            if len(fields) != 3 or any(not field.strip() for field in fields):
                raise ValueError(f"Row {number} needs an MGI ID, a strain name and a strain type.")
            found = fields[1].decode("utf-8"), hashlib.sha256(row).hexdigest()
    if found is None:
        raise ValueError(f"The report has no strain with accession {accession}.")
    return found


def register(run: dict[str, Any]) -> dict[str, Any]:
    params = run["params"]
    file_id = params["file"]
    accession = params["mgi_id"]
    try:
        name, digest = read_strain(Path(run["files"][file_id]["path"]), accession)
    except ValueError as error:
        raise iki.Refused(str(error)) from None
    values = {"catalogue": "mgi", "mgi_id": accession, "nomenclature": name}
    return {
        "creates": [
            {
                "type": "iki/mouse_strains.MouseStrain",
                "values": values,
                "origin": {
                    "source": "MGI strain nomenclature report",
                    "id": accession,
                    "file": file_id,
                    "sha256": digest,
                },
            }
        ],
    }


if __name__ == "__main__":
    iki.main(register)
