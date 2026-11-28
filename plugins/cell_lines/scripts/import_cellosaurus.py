"""Register one Cellosaurus record, with the supplier reference on the material."""

import hashlib
import json
import os
import re
import sys
from pathlib import Path

#: The resources Cellosaurus classes as "Cell line collections (Providers)": the
#: suppliers and collections whose DR cross-reference is a catalogue number for the
#: material. Every other resource it cross-references (an ontology, a database, a
#: sample archive or an encyclopedia, such as BTO, BioSample or Wikidata) names the
#: line elsewhere, not a source of it. From cellosaurus_xrefs.txt, release 57.0 of
#: 22 October 2026: https://ftp.expasy.org/databases/cellosaurus/cellosaurus_xrefs.txt
PROVIDERS = frozenset(
    {
        "Abcam", "Abeomics", "ABM", "AddexBio", "ATCC", "BCRC", "BCRJ", "BEI_Resources",
        "CancerTools", "CBA", "CCLV", "CCTCC", "Cell_Biolabs", "CLS", "Coriell", "DGRC",
        "DiscoverX", "DSHB", "DSMZ", "EBiSC", "ECACC", "EDITGENE", "Evercyte", "FCDI",
        "GeneCopoeia", "Genomeditech", "GenScript", "HIVReagentProgram", "Horizon_Discovery",
        "Hysigen", "IBRC", "ICLC", "Imanis", "Innoprot", "InvivoGen", "IZSLER", "JCRB", "KCB",
        "KCLB", "Kerafast", "KYinno", "Millipore", "MMRRC", "NCBI_Iran", "NCI-DCTD",
        "NCI-PDMR", "NHCDR", "NISES", "NRFC", "RCB", "Revvity/PerkinElmer", "RIKEN_BRC_EPD",
        "Rockland", "RSCB", "Sigma-Aldrich", "TKG", "TNGB", "Ubigene", "WiCell", "Ximbio",
    }
)  # fmt: skip

#: The line codes of a Cellosaurus reference entry, which a cell line's own page export
#: lists after its entry: https://ftp.expasy.org/databases/cellosaurus/cellosaurus_refs.txt
REFERENCE_CODES = frozenset({"RX", "RA", "RG", "RT", "RL"})


def entries(lines: list[str]) -> list[list[str]]:
    """The file's entries, each ending in its // line."""
    if not lines or lines[-1].strip() != "//":
        raise ValueError("Supply a Cellosaurus text export: each entry ends in //.")
    found: list[list[str]] = [[]]
    for line in lines[:-1]:
        if line.strip() == "//":
            found.append([])
        elif line.strip():
            found[-1].append(line)
    return found


def cell_line_entry(lines: list[str]) -> list[str]:
    """The one cell line entry of a Cellosaurus text export. A line's page exports it
    followed by the references it cites, each its own entry; those are skipped."""
    lines_of = entries(lines)
    cell_lines = [e for e in lines_of if any(line.startswith("ID   ") for line in e)]
    others = [
        e
        for e in lines_of
        if e not in cell_lines and not all(line[:2] in REFERENCE_CODES for line in e)
    ]
    if others:
        raise ValueError(
            "The file has an entry that is neither a cell line nor a reference: supply a "
            "Cellosaurus text export of one cell line."
        )
    if len(cell_lines) != 1:
        accessions = [
            line[5:].strip() for e in cell_lines for line in e if line.startswith("AC   ")
        ]
        raise ValueError(
            f"The file has {len(cell_lines)} cell line entries"
            + (f" ({', '.join(accessions)})" if accessions else "")
            + ": supply the export of one cell line."
        )
    return cell_lines[0]


def parental_line(data: bytes, supplier: str | None, catalogue_id: str | None) -> dict[str, str]:
    # Cellosaurus TXT uses ID for the primary name, AC for the primary accession
    # and DR for database cross-references. https://www.cellosaurus.org/CVCL_0579.txt
    lines = cell_line_entry(data.decode("utf-8-sig").splitlines())
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
    if supplier not in PROVIDERS:
        raise ValueError(
            f"Cellosaurus doesn't list {supplier} as a cell line supplier or collection, so "
            "its cross-reference isn't a catalogue number for the material. Give a supplier "
            "such as ATCC or DSMZ, or leave both blank and record the source yourself."
        )
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
