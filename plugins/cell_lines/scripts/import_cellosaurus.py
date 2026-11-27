"""Register one Cellosaurus record, with the supplier reference on the material."""

import hashlib
import json
import os
import re
import sys
from pathlib import Path


def parental_line(data: bytes, supplier: str | None, catalogue_id: str | None) -> dict[str, str]:
    # Cellosaurus TXT uses ID for the primary name, AC for the primary accession
    # and DR for database cross-references. https://www.cellosaurus.org/CVCL_0579.txt
    lines = data.decode("utf-8-sig").splitlines()
    if not lines or lines[-1].strip() != "//" or any(line.strip() == "//" for line in lines[:-1]):
        raise ValueError("Supply one complete Cellosaurus text record ending in //.")
    names = [line[5:].strip() for line in lines if line.startswith("ID   ")]
    accessions = [line[5:].strip() for line in lines if line.startswith("AC   ")]
    if len(names) != 1 or not 1 <= len(names[0]) <= 255:
        raise ValueError("The record needs one ID line containing the cell line name.")
    if len(accessions) != 1 or not re.fullmatch(r"CVCL_[A-Z0-9]{4}", accessions[0]):
        raise ValueError(
            "The record needs one AC line containing its primary Cellosaurus accession."
        )
    found = {"name": names[0], "catalogue": "cellosaurus", "cellosaurus_id": accessions[0]}
    if supplier is None and catalogue_id is None:
        return found
    if supplier is None or catalogue_id is None:
        raise ValueError("Give both the supplier and its catalogue ID, or neither.")
    for line in lines:
        if line.startswith("DR   "):
            provider, _, accession = line[5:].partition(";")
            if provider.strip() == supplier and accession.strip() == catalogue_id:
                return found
    raise ValueError("The supplier and catalogue ID do not occur together in this record.")


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    params = run["params"]
    file_id = params["file"]
    data = Path(run["files"][file_id]["path"]).read_bytes()
    try:
        supplier = (params.get("supplier") or "").strip() or None
        catalogue_id = (params.get("catalogue_id") or "").strip() or None
        values = parental_line(data, supplier, catalogue_id)
    except ValueError as error:
        print(f"::error {error}", flush=True)
        sys.exit(1)
    origin = {
        "source": "Cellosaurus",
        "id": values["cellosaurus_id"],
        "file": file_id,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    creates = [
        {"type": "iki/cell_lines.ParentalLine", "key": "line", "values": values, "origin": origin}
    ]
    if supplier is not None:
        creates.append(
            {
                "type": "iki/cell_lines.CellLineSource",
                "values": {
                    "supplier": supplier,
                    "catalogue_id": catalogue_id,
                    "line": {"$create": "line"},
                },
                "origin": origin,
            }
        )
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": creates}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
