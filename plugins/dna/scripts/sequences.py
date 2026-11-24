"""The DNA plugin's sequence calculations: `composition` for DNA sequences, `primers` for
primers.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does.

GC content counts G, C and S (G or C) among the bases whose pairing is known (A, C,
G, T, U, S and W); other IUPAC codes, such as N, are left out of it. A sequence with
none has no GC content.

Melting temperature uses SantaLucia's unified nearest-neighbour parameters (PNAS 95,
1460; 1998) with his salt correction, at 50 mM Na+, sodium alone: no Mg2+ or dNTPs. Its
strands are 50 nM in all, his CT, the total strand concentration: 25 nM each of the
primer and its complement, so Tm = ΔH / (ΔS + R ln(CT/4)), or 50 nM of a primer that
pairs with itself, at CT (his equation 3). That's not Primer3's default buffer, which
adds 1.5 mM Mg2+ and 0.6 mM dNTPs and gives a Tm some 5 to 6 °C higher; a PCR buffer's Tm
needs those. It's computed only for primers of A, C, G and T alone.
"""

import json
import math
import os
import sys
from functools import cache
from pathlib import Path

import wasmtime

GC = frozenset("GCS")
KNOWN = frozenset("ACGTUSW")

COMPLEMENT = str.maketrans("ACGT", "TGCA")
MEASUREMENT = Path(__file__).resolve().parent.parent / "data" / "oligo-measurement.wasm"


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


def primer(bases: str) -> dict[str, float]:
    values = composition(bases)
    tm = melting_temperature(bases)
    if tm is not None:
        values["melting_temperature"] = tm
    return values


def main() -> None:
    kind = sys.argv[1]
    compute, name = {"composition": (composition, "sequence"), "primers": (primer, "primer")}[kind]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    entities = run["inputs"][name]
    updates = []
    for done, entity in enumerate(entities, start=1):
        values = compute(entity["values"]["bases"])
        updates.append({"entity": entity["id"], "values": values})
        print(f"{entity['hrid']}: {', '.join(f'{k} {v}' for k, v in values.items())}")
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {name: updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
