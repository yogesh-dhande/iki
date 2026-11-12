"""The protein plugin's sequence calculations: `properties` for proteins, `translate` for
DNA sequences.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does.

Properties are Biopython's ProtParam, which follows ExPASy's ProtParam tool:

- molecular weight: the average isotopic masses of the residues, plus one water;
- isoelectric point: Bjellqvist's pK values, with the N- and C-terminal residues' own;
- extinction coefficients at 280 nm in water: Pace's values for W, Y and cystine (1995),
  with every pair of cysteines forming a cystine, and with none;
- GRAVY: the mean of the residues' Kyte-Doolittle hydropathy values (1982);
- instability index: Guruprasad's dipeptide weights (1990).

All but the length need a sequence of the 20 standard amino acids alone, and a final
stop (*) is left out of them. For any other sequence, the run warns which residue
stopped it and sets the length alone.

Translation uses NCBI's genetic codes. A codon with ambiguous bases (N, R, Y, ...)
becomes the amino acid every base it could be gives, or B, Z, J or X when they differ.
Bases left over past the last whole codon are left out.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from Bio.Data import CodonTable
from Bio.Seq import Seq
from Bio.SeqUtils.ProtParam import ProteinAnalysis

STANDARD = frozenset("ACDEFGHIKLMNPQRSTVWY")
#: The longest name a protein takes (core.Sequence's).
MAX_NAME = 255


def nonstandard(sequence: str) -> tuple[int, str] | None:
    """Where the first residue outside the 20 standard amino acids is, and which."""
    return next(((i, r) for i, r in enumerate(sequence, 1) if r not in STANDARD), None)


def properties(sequence: str) -> tuple[dict[str, float], str | None]:
    """Its properties, and a warning when only its length could be computed."""
    sequence = sequence.upper()
    values: dict[str, float] = {"length": len(sequence)}
    residues = sequence.removesuffix("*")
    found = nonstandard(residues)
    if not residues:
        return values, "It has no residues, so only its length was set."
    if found is not None:
        position, residue = found
        what = "a stop (*)" if residue == "*" else residue
        return values, (
            f"It has {what} at residue {position}, so only its length was set: the other "
            "properties need the 20 standard amino acids alone."
        )
    analysis = ProteinAnalysis(residues)
    reduced, oxidized = analysis.molar_extinction_coefficient()
    values |= {
        "molecular_weight": round(analysis.molecular_weight(), 2),
        "isoelectric_point": round(analysis.isoelectric_point(), 2),
        "extinction_coefficient_oxidized": oxidized,
        "extinction_coefficient_reduced": reduced,
        "gravy": round(analysis.gravy(), 3),
        "instability_index": round(analysis.instability_index(), 2),
    }
    return values, None


def compute_properties(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["protein"]
    updates = []
    messages = []
    for done, entity in enumerate(entities, start=1):
        values, warning = properties(entity["values"]["sequence"])
        updates.append({"entity": entity["id"], "values": values})
        if warning is not None:
            messages.append({"level": "warning", "text": warning, "entity": entity["id"]})
        print(f"{entity['hrid']}: {', '.join(f'{k} {v}' for k, v in values.items())}")
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    return {"updates": {"protein": updates}, "messages": messages}


# --- Translation ---------------------------------------------------------------------------------


def code(table: int) -> str:
    """The genetic code's name, or a ValueError for a number NCBI doesn't use."""
    found = CodonTable.unambiguous_dna_by_id.get(table)
    if found is None:
        numbers = ", ".join(str(n) for n in sorted(CodonTable.unambiguous_dna_by_id))
        raise ValueError(f"NCBI has no genetic code {table}: choose one of {numbers}.")
    return str(found.names[0])


def translation(bases: str, strand: str, frame: int, table: int, to_stop: bool) -> str:
    dna = Seq(bases.upper())
    if strand == "reverse":
        dna = dna.reverse_complement()
    dna = dna[frame - 1 :]
    dna = dna[: len(dna) - len(dna) % 3]
    return str(dna.translate(table=table, to_stop=to_stop))


def translate(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    strand = params.get("strand", "forward")
    frame = int(params.get("frame", 1))
    table = int(params.get("table", 1))
    to_stop = bool(params.get("to_stop", True))
    try:
        name = code(table)
    except ValueError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    sign = "+" if strand == "forward" else "-"
    entities = run["inputs"]["sequence"]
    creates = []
    messages = []
    for done, entity in enumerate(entities, start=1):
        sequence = translation(entity["values"]["bases"], strand, frame, table, to_stop)
        label = entity["values"].get("name") or entity["hrid"]
        if not sequence.removesuffix("*"):
            messages.append(
                {
                    "level": "error",
                    "text": f"{label} gives no residues in frame {sign}{frame}"
                    + (": it starts with a stop codon." if to_stop else "."),
                    "entity": entity["id"],
                }
            )
            print(f"{entity['hrid']}: no residues")
        else:
            values: dict[str, Any] = {
                "name": f"{label} ({sign}{frame})"[:MAX_NAME],
                "description": (
                    f"Translated from {entity['hrid']}, frame {sign}{frame}, "
                    f"with NCBI's genetic code {table} ({name})."
                ),
                "sequence": sequence,
                "encoded_by": entity["id"],
            }
            if entity["values"].get("organism"):
                values["organism"] = entity["values"]["organism"]
            creates.append(
                {"type": "protein.Protein", "values": values, "derived_from": [entity["id"]]}
            )
            print(f"{entity['hrid']}: {len(sequence):,} residues")
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    return {"creates": creates, "messages": messages}


def main() -> None:
    kind = sys.argv[1]
    compute = {"properties": compute_properties, "translate": translate}[kind]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **compute(run)}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
