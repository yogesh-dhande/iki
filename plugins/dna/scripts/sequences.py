"""The DNA plugin's sequence calculations: `composition` for DNA sequences, `primers` for
primers.

It reads the run and writes its results with Iki's action helper, iki_action.py, and
reports progress with `::progress` lines.

GC content counts G, C and S (G or C) among the bases whose pairing is known (A, C,
G, T, U, S and W); other IUPAC codes, such as N, are left out of it. A sequence with
none has no GC content.

A primer carries two melting temperatures, each under its own stated conditions.

`melting_temperature` is the packaged oligo measurement, the same number the server and the
web app quote for any stretch of DNA (ADR 0191): SantaLucia's unified nearest-neighbour
parameters (PNAS 95, 1460; 1998) with his salt correction, at 50 mM Na+ alone, no Mg2+ or
dNTPs. Its strands are 50 nM in all, his CT, the total strand concentration: 25 nM each of
the primer and its complement, so Tm = ΔH / (ΔS + R ln(CT/4)), or 50 nM of a primer that
pairs with itself, at CT (his equation 3). The mutagenesis and guide designs compare
against it. It's computed only for primers of A, C, G and T alone.

The rest of a primer's checks -- `buffer_melting_temperature`, its Tm in the buffer a PCR
runs in (Primer3's default: 1.5 mM Mg2+ and 0.6 mM dNTPs besides the 50 mM Na+, corrected
for the magnesium by Owczarzy et al. 2008), the hairpin it folds into, the dimer it forms
with another copy of itself, how firmly its 3' end sits and its longest run of one base --
are computed by the packaged checks module (ADR 0201, `oligos.py`), not here.
"""

import math
from functools import cache
from pathlib import Path
from typing import Any

import iki_action as iki
import oligos
import wasmtime

GC = frozenset("GCS")
KNOWN = frozenset("ACGTUSW")
COMPLEMENT = str.maketrans("ACGT", "TGCA")
MEASUREMENT = Path(__file__).resolve().parent.parent / "data" / "oligo-measurement.wasm"
#: What the checks module reports per oligo, and the field each value goes in. A value the
#: module leaves out -- an oligo with ambiguity codes has no thermodynamics, and an oligo
#: that folds into nothing has no hairpin -- leaves its field alone.
CHECKED = {
    "melting_temperature": "buffer_melting_temperature",
    "end_stability": "end_stability",
    "longest_run": "longest_run",
}


@cache
def _measurement_module() -> tuple[wasmtime.Engine, wasmtime.Module]:
    """Compile the action archive's pinned module once per process for a batch of records."""
    engine = wasmtime.Engine()
    return engine, wasmtime.Module.from_file(engine, str(MEASUREMENT))


def gc_content(bases: str) -> float | None:
    counted = [base for base in bases.upper() if base in KNOWN]
    if not counted:
        return None
    return round(100 * sum(base in GC for base in counted) / len(counted), 2)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def melting_temperature(bases: str) -> float | None:
    """°C, or None for a primer with other bases than A, C, G and T, or fewer than two."""
    if not bases.isascii() or len(bases) < 2:
        return None
    engine, module = _measurement_module()
    with wasmtime.Store(engine) as store:
        exports = wasmtime.Instance(store, module, []).exports(store)
        memory = exports["memory"]
        allocate = exports["iki_input"]
        measure = exports["iki_melting_temperature_value"]
        if not (
            isinstance(memory, wasmtime.Memory)
            and isinstance(allocate, wasmtime.Func)
            and isinstance(measure, wasmtime.Func)
        ):
            raise RuntimeError("The packaged oligo measurement has an incompatible ABI.")
        encoded = bases.encode("ascii")
        at = allocate(store, len(encoded))
        if not at:
            raise ValueError("The oligo exceeds the measurement module’s 64 MiB input limit.")
        memory.write(store, encoded, at)
        result = float(measure(store))
        return None if math.isnan(result) else result


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
        bases = entity["values"]["bases"]
        values: dict[str, Any] = composition(bases)
        tm = melting_temperature(bases)
        if tm is not None:
            values["melting_temperature"] = tm
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


def compute(kind: str, run: iki.Run) -> iki.Output:
    name = {"composition": "sequence", "primers": "primer"}[kind]
    entities = run.inputs(name)
    computed = (
        primers(entities)
        if kind == "primers"
        else [composition(entity["values"]["bases"]) for entity in entities]
    )
    out = run.output()
    for done, (entity, values) in enumerate(zip(entities, computed, strict=True), start=1):
        out.update(name, entity, values)
        print(f"{entity['hrid']}: {', '.join(f'{k} {v}' for k, v in values.items())}")
        iki.progress(done / len(entities), f"{done} of {len(entities)}")
    return out


if __name__ == "__main__":
    iki.main({kind: lambda run, k=kind: compute(k, run) for kind in ("composition", "primers")})
