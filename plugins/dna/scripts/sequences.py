"""The DNA plugin's sequence calculations: `composition` for DNA sequences, `primers` for
primers.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does.

GC content counts G, C and S (G or C) among the bases whose pairing is known (A, C,
G, T, U, S and W); other IUPAC codes, such as N, are left out of it. A sequence with
none has no GC content.

A primer's own checks -- its melting temperature in the buffer a PCR runs in, the hairpin
it folds into, the dimer it forms with another copy of itself, how firmly its 3' end sits
and its longest run of one base -- are computed by the packaged Rust module (ADR 0201,
`oligos.py`), not here: they are the same numbers the server and the web app quote.

`melting_temperature` is the same module asked for the conditions a primer's own Tm is
quoted under instead (`Conditions::PRIMER`: SantaLucia 1998 at 50 mM Na+, no Mg2+ or
dNTPs, 25 nM of each strand, or 50 nM of a primer that pairs with itself), which is what
the mutagenesis and guide designs compare against. It is not a second implementation.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

import oligos

GC = frozenset("GCS")
KNOWN = frozenset("ACGTUSW")
COMPLEMENT = str.maketrans("ACGT", "TGCA")
#: The module's request fields for `Conditions::PRIMER`: every field left out is that
#: constant's, so an empty object asks for exactly it.
PRIMER_CONDITIONS: dict[str, Any] = {}
#: What the module reports per oligo, and the field each value goes in. A value the
#: module leaves out -- an oligo with ambiguity codes has no thermodynamics, and an oligo
#: that folds into nothing has no hairpin -- leaves its field alone.
CHECKED = {
    "melting_temperature": "melting_temperature",
    "end_stability": "end_stability",
    "longest_run": "longest_run",
}


def gc_content(bases: str) -> float | None:
    counted = [base for base in bases.upper() if base in KNOWN]
    if not counted:
        return None
    return round(100 * sum(base in GC for base in counted) / len(counted), 2)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def melting_temperature(bases: str) -> float | None:
    """°C under `Conditions::PRIMER`, or None for a primer with other bases than A, C, G
    and T, or fewer than two."""
    bases = bases.upper()
    if len(bases) < 2 or set(bases) - set("ACGT"):
        return None
    answer = oligos.call(
        "iki_check_oligos",
        {"oligos": [{"hrid": "tm", "name": "", "bases": bases}], "conditions": PRIMER_CONDITIONS},
    )
    if "error" in answer:
        raise RuntimeError(f"The melting temperature was refused: {answer['error']}")
    tm = answer["oligos"][0].get("melting_temperature")
    return None if tm is None else round(tm, 1)


def composition(bases: str) -> dict[str, float]:
    values: dict[str, float] = {"length": len(bases)}
    gc = gc_content(bases)
    if gc is not None:
        values["gc_content"] = gc
    return values


def primers(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each primer's length and GC content, and the checks the module computes, in one
    call: compiling the module costs more than folding a plate of oligos."""
    asked = [
        {
            "hrid": entity["hrid"],
            "name": entity.get("values", {}).get("name") or "",
            "bases": entity["values"]["bases"],
        }
        for entity in entities
    ]
    answer = oligos.call("iki_check_oligos", {"oligos": asked})
    if "error" in answer:
        raise RuntimeError(f"The primer checks were refused: {answer['error']}")
    by_hrid = {checked["oligo"]: checked for checked in answer["oligos"]}
    out = []
    for entity in entities:
        values: dict[str, Any] = composition(entity["values"]["bases"])
        checked = by_hrid.get(entity["hrid"], {})
        for key, field in CHECKED.items():
            if checked.get(key) is not None:
                values[field] = (
                    round(checked[key], 2) if isinstance(checked[key], float) else checked[key]
                )
        for kind, field in (
            ("hairpin", "hairpin_free_energy"),
            ("self_dimer", "self_dimer_free_energy"),
        ):
            structure = checked.get(kind)
            if structure is not None:
                values[field] = round(structure["free_energy"], 2)
        out.append(values)
    return out


def main() -> None:
    kind = sys.argv[1]
    name = {"composition": "sequence", "primers": "primer"}[kind]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    entities = run["inputs"][name]
    computed = (
        primers(entities)
        if kind == "primers"
        else [composition(entity["values"]["bases"]) for entity in entities]
    )
    updates = []
    for done, (entity, values) in enumerate(zip(entities, computed, strict=True), start=1):
        updates.append({"entity": entity["id"], "values": values})
        print(f"{entity['hrid']}: {', '.join(f'{k} {v}' for k, v in values.items())}")
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {name: updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
