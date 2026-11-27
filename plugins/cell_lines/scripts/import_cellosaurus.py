"""Register one Cellosaurus record, with the supplier reference on the material."""

import hashlib
import re

import iki_action as iki


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


def register(run: iki.Run) -> iki.Output:
    params = run.params
    file_id = params["file"]
    data = run.file(file_id).read_bytes()
    supplier = (params.get("supplier") or "").strip() or None
    catalogue_id = (params.get("catalogue_id") or "").strip() or None
    values = parental_line(data, supplier, catalogue_id)
    origin = {
        "source": "Cellosaurus",
        "id": values["cellosaurus_id"],
        "file": file_id,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    out = run.output()
    line = out.create("ParentalLine", values, key="line", origin=origin)
    if supplier is not None:
        out.create(
            "CellLineSource",
            {"supplier": supplier, "catalogue_id": catalogue_id, "line": line},
            origin=origin,
        )
    return out


if __name__ == "__main__":
    iki.main(register, refuse=ValueError)
