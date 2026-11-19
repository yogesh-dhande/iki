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
from pathlib import Path

GC = frozenset("GCS")
KNOWN = frozenset("ACGTUSW")

#: ΔH (kcal/mol) and ΔS (cal/K/mol) for each pair of neighbours, 5'→3' on one strand.
NEIGHBOURS = {
    "AA": (-7.9, -22.2), "AT": (-7.2, -20.4), "TA": (-7.2, -21.3), "CA": (-8.5, -22.7),
    "GT": (-8.4, -22.4), "CT": (-7.8, -21.0), "GA": (-8.2, -22.2), "CG": (-10.6, -27.2),
    "GC": (-9.8, -24.4), "GG": (-8.0, -19.9),
}  # fmt: skip
#: Initiation, by the base pair at each end.
INITIATION = {"G": (0.1, -2.8), "C": (0.1, -2.8), "A": (2.3, 4.1), "T": (2.3, 4.1)}
SYMMETRY = (0.0, -1.4)
COMPLEMENT = str.maketrans("ACGT", "TGCA")
GAS_CONSTANT = 1.987  # cal/K/mol
SODIUM = 0.05  # mol/L
#: SantaLucia's CT, all the strands together (mol/L): 25 nM each of the primer and its
#: complement, or 50 nM of a primer that pairs with itself.
STRANDS = 50e-9


def gc_content(bases: str) -> float | None:
    counted = [base for base in bases.upper() if base in KNOWN]
    if not counted:
        return None
    return round(100 * sum(base in GC for base in counted) / len(counted), 2)


def reverse_complement(bases: str) -> str:
    return bases.translate(COMPLEMENT)[::-1]


def melting_temperature(bases: str) -> float | None:
    """°C, or None for a primer with other bases than A, C, G and T, or fewer than two."""
    bases = bases.upper()
    if len(bases) < 2 or set(bases) - set("ACGT"):
        return None
    enthalpy = INITIATION[bases[0]][0] + INITIATION[bases[-1]][0]
    entropy = INITIATION[bases[0]][1] + INITIATION[bases[-1]][1]
    for i in range(len(bases) - 1):
        pair = bases[i : i + 2]
        dh, ds = NEIGHBOURS.get(pair) or NEIGHBOURS[reverse_complement(pair)]
        enthalpy += dh
        entropy += ds
    # One strand pairs with itself, at CT; otherwise with an equal amount of its
    # complement, at CT/4 (SantaLucia 1998, equation 3).
    if bases == reverse_complement(bases):
        enthalpy += SYMMETRY[0]
        entropy += SYMMETRY[1]
        strands = STRANDS
    else:
        strands = STRANDS / 4
    entropy += 0.368 * (len(bases) - 1) * math.log(SODIUM)
    kelvin = 1000 * enthalpy / (entropy + GAS_CONSTANT * math.log(strands))
    return round(kelvin - 273.15, 1)


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
