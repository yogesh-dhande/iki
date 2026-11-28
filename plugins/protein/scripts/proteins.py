"""The protein plugin's sequence calculations: `properties` for proteins, `translate` for
DNA sequences.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, and
writes its results to $IKI_OUTPUT, as every action's command does.

Properties are Biopython's ProtParam, which follows ExPASy's ProtParam tool:

- length: its amino acids, stops (*) left out;
- molecular weight: the average isotopic masses of the residues, plus one water;
- isoelectric point: the pH at which Bjellqvist's charge model (his pK values, with the
  N- and C-terminal residues' own) gives no net charge, found by bisection, to 0.0001,
  between pHs whose charges differ in sign. Biopython's own search keeps between pH
  4.05 and 12, which cuts off peptides more acidic or basic than that;
- extinction coefficients at 280 nm in water: Pace's values for W, Y and cystine (1995),
  with every pair of cysteines forming a cystine, and with none;
- GRAVY: the mean of the residues' Kyte-Doolittle hydropathy values (1982);
- hydropathy: ExPASy ProtScale's profile of those values, with its defaults: for each
  residue, the mean over a window of nine centred on it, equally weighted. The first and
  last four residues have none (null), as ProtScale draws none for them;
- instability index: Guruprasad's dipeptide weights (1990).

Each property needs residues it has values for, and the stops (*) at its end are left
out of them (M** is M, as it is to its identity). An isoelectric point past pH 0 to 14,
which the model gives only for an extreme composition (charged residues nearly all of one
sign), isn't saved, saying why; it's checked before it's rounded.
All take the 20 standard amino acids. Molecular weight also takes selenocysteine (U) and
pyrrolysine (O), whose masses are known, and J (I or L, which weigh the same).
The isoelectric point and extinction coefficients also take J: neither I nor L is
charged or absorbs at 280 nm. For any other residue, or a stop inside it, the run warns
which residue left which properties out.

Translation uses NCBI's genetic codes. A codon with ambiguous bases (N, R, Y, ...)
becomes the amino acid every base it could be gives, or B, Z, J or X when they differ.
A frame is translated codon by codon, bases past the last whole codon left out,
stopping at the first stop codon or keeping each as `*`. A complete coding sequence is
translated as NCBI's tables say of one, and as Biopython's `cds=True` does: its first
codon must be one of the code's start codons, which gives methionine; its last, a stop,
which is left out; with no other stop and no bases left over. In three codes (27, 28
and 31) a stop codon can also code for an amino acid, so only where it is says which:
a frame in them keeps such codons as amino acids, and says so of a last one that could
have ended it (an ambiguous one, such as TAR, too), and can't stop at the first stop codon.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any

from Bio import BiopythonWarning
from Bio.Data import CodonTable
from Bio.Seq import Seq
from Bio.SeqUtils.IsoelectricPoint import IsoelectricPoint
from Bio.SeqUtils.ProtParam import ProteinAnalysis
from Bio.SeqUtils.ProtParamData import kd
from weights import average_mass, chain_formula

STANDARD = frozenset("ACDEFGHIKLMNPQRSTVWY")
#: The residues each property has values for. J is I or L: both weigh the same, neither
#: is charged (Bjellqvist) nor absorbs at 280 nm (Pace), so J is either for those.
MASS = STANDARD | frozenset("UOJ")
CHARGE_AND_ABSORBANCE = STANDARD | frozenset("J")
#: Each property's fields, what it's called in a warning, and the residues it takes.
NEEDS: tuple[tuple[tuple[str, ...], str, frozenset[str]], ...] = (
    (("molecular_weight",), "molecular weight", MASS),
    (("isoelectric_point",), "isoelectric point", CHARGE_AND_ABSORBANCE),
    (
        ("extinction_coefficient_oxidized", "extinction_coefficient_reduced"),
        "extinction coefficients",
        CHARGE_AND_ABSORBANCE,
    ),
    (("gravy",), "GRAVY", STANDARD),
    (("hydropathy",), "hydropathy", STANDARD),
    (("instability_index",), "instability index", STANDARD),
)
#: The longest name a protein takes (iki/core.Sequence's).
MAX_NAME = 255
#: How closely the isoelectric point is found, in pH.
PI_TOLERANCE = 1e-4


def nonstandard(sequence: str, allowed: frozenset[str] = STANDARD) -> tuple[int, str] | None:
    """Where the first residue outside `allowed` is, and which."""
    return next(((i, r) for i, r in enumerate(sequence, 1) if r not in allowed), None)


#: ProtScale's default window: nine residues, centred on each.
WINDOW = 9


def hydropathy(residues: str) -> list[float | None]:
    """Each residue's Kyte-Doolittle hydropathy, averaged over the window centred on it; None
    for the residues too near either end for a whole window."""
    edge = WINDOW // 2
    if len(residues) < WINDOW:
        return [None] * len(residues)
    profile = ProteinAnalysis(residues).protein_scale(kd, WINDOW, 1.0)
    return [None] * edge + [round(v, 2) for v in profile] + [None] * edge


def isoelectric_point(residues: str) -> float:
    """The pH at which Bjellqvist's model, as Biopython has it, gives the residues no net
    charge. The charge falls as the pH rises, from positive (the N-terminus is always
    there) to negative (so is the C-terminus), so a zero lies between a pH where it's
    positive and one where it's negative; bisection narrows that to `PI_TOLERANCE`."""
    model = IsoelectricPoint(residues)
    low, high = 0.0, 14.0
    # Past either end of the usual scale only for hundreds of one charged residue.
    while model.charge_at_pH(low) <= 0:
        low -= 2.0
    while model.charge_at_pH(high) >= 0:
        high += 2.0
    while high - low > PI_TOLERANCE:
        middle = (low + high) / 2
        if model.charge_at_pH(middle) > 0:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def properties(sequence: str) -> tuple[dict[str, Any], str | None]:
    """Its properties, and a warning when some couldn't be computed."""
    sequence = sequence.upper()
    # Stops at its end end it, however many (M** is M, as its identity is).
    residues = sequence.rstrip("*")
    values: dict[str, Any] = {"length": len(residues.replace("*", ""))}
    if not residues.replace("*", ""):
        return values, "It has no residues, so only its length was set."
    # J weighs, charges and absorbs as I and L both do.
    analysis = ProteinAnalysis(residues.replace("J", "L"))
    compute = {
        "molecular_weight": lambda: {
            "molecular_weight": round(average_mass(chain_formula(residues)), 2)
        },
        # Kept unrounded until it's checked against the scale, then rounded.
        "isoelectric_point": lambda: {"isoelectric_point": isoelectric_point(residues)},
        "extinction_coefficient_oxidized": lambda: dict(
            zip(
                ("extinction_coefficient_reduced", "extinction_coefficient_oxidized"),
                analysis.molar_extinction_coefficient(),
                strict=True,
            )
        ),
        "gravy": lambda: {"gravy": round(analysis.gravy(), 3)},
        "hydropathy": lambda: {"hydropathy": hydropathy(residues)},
        "instability_index": lambda: {"instability_index": round(analysis.instability_index(), 2)},
    }
    stopped: dict[tuple[int, str], list[str]] = {}
    for fields, label, allowed in NEEDS:
        found = nonstandard(residues, allowed)
        if found is None:
            values |= {name: compute[fields[0]]()[name] for name in fields}
        else:
            stopped.setdefault(found, []).append(label)
    beyond = _beyond_scale(values)
    if "isoelectric_point" in values:
        values["isoelectric_point"] = round(values["isoelectric_point"], 2)
    if not stopped:
        return values, beyond
    said = []
    for (position, residue), labels in sorted(stopped.items(), key=lambda item: item[0][0]):
        what = "a stop (*)" if residue == "*" else residue
        listed = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
        has = "have" if len(labels) > 1 or listed.endswith("s") else "has"
        said.append(f"its {listed} {has} no value for {what} at residue {position}")
    text = "; ".join(said)
    many = sum(len(labels) for labels in stopped.values()) > 1
    warning = f"{text[0].upper()}{text[1:]}, so {'they weren' if many else 'it wasn'}'t computed."
    return values, f"{warning} {beyond}" if beyond else warning


def _beyond_scale(values: dict[str, Any]) -> str | None:
    """Takes out an isoelectric point past pH 0 to 14, unrounded, saying why; the other
    properties are kept. Bjellqvist's model gives one only for an extreme composition,
    charged residues nearly all of one sign, such as 120 arginines (14.08) or (RAKR)x60
    (14.08): past pH 14 is over 1 M hydroxide, where Henderson-Hasselbalch's charges and
    the pK values, measured by focusing proteins in gels, don't hold. One that rounds to
    0.00 or 14.00 is said to four places, so it's seen to be past them."""
    found = values.get("isoelectric_point")
    if found is None or 0 <= found <= 14:
        return None
    del values["isoelectric_point"]
    side = "above 14" if found > 14 else "below 0"
    shown = f"{found:.2f}" if round(found, 2) not in (0, 14) else f"{found:.4f}"
    return (
        f"Its isoelectric point by Bjellqvist's model would be pH {shown}, {side}, "
        "where the model's pK values don't hold, so it wasn't saved."
    )


def compute_properties(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["protein"]
    updates = []
    messages = []
    for done, entity in enumerate(entities, start=1):
        values, warning = properties(entity["values"]["sequence"])
        updates.append({"entity": entity["id"], "values": values})
        if warning is not None:
            messages.append({"level": "warning", "text": warning, "entity": entity["id"]})
        shown = {k: v for k, v in values.items() if k != "hydropathy"}
        print(f"{entity['hrid']}: {', '.join(f'{k} {v}' for k, v in shown.items())}")
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    return {"updates": {"protein": updates}, "messages": messages}


# --- Translation ---------------------------------------------------------------------------------


def code(table: int) -> CodonTable.CodonTable:
    """The genetic code, or a ValueError for a number NCBI doesn't use."""
    found = CodonTable.unambiguous_dna_by_id.get(table)
    if found is None:
        numbers = ", ".join(str(n) for n in sorted(CodonTable.unambiguous_dna_by_id))
        raise ValueError(f"NCBI has no genetic code {table}: choose one of {numbers}.")
    return found


def dual(table: int) -> dict[str, str]:
    """The code's codons that are a stop or an amino acid by where they are, and the amino
    acid each is elsewhere (NCBI's codes 27, 28 and 31)."""
    found = code(table)
    return {c: found.forward_table[c] for c in found.stop_codons if c in found.forward_table}


#: The bases each IUPAC code stands for (IUBMB, 1985).
IUPAC = {
    "A": "A", "C": "C", "G": "G", "T": "T", "U": "T",
    "R": "AG", "Y": "CT", "S": "CG", "W": "AT", "K": "GT", "M": "AC",
    "B": "CGT", "D": "AGT", "H": "ACT", "V": "ACG", "N": "ACGT",
}  # fmt: skip


def readings_of(codon: str) -> list[str]:
    """Every codon a codon's bases could be, an IUPAC code being any it stands for."""
    if len(codon) != 3:
        return []
    return [
        a + b + c
        for a in IUPAC.get(codon[0], "")
        for b in IUPAC.get(codon[1], "")
        for c in IUPAC.get(codon[2], "")
    ]


def could_end(codon: str, table: int) -> str:
    """If a codon could be, by its bases, one of the code's codons that are a stop or an
    amino acid by where they are (codes 27, 28 and 31), the amino acids it could be, such
    as Q, or Q, W for TRA in code 28; else empty."""
    readings = readings_of(codon)
    either = dual(table)
    found = code(table)
    if not any(each in either for each in readings):
        return ""
    residues = sorted(
        {found.forward_table[each] for each in readings if each in found.forward_table}
    )
    return ", ".join(residues)


def never_stops(codon: str, table: int) -> list[str]:
    """The codons a codon could be that are never a stop in the code: a complete coding
    sequence can't end with it unless there are none."""
    stops = set(code(table).stop_codons)
    return [each for each in readings_of(codon) if each not in stops]


def frame_of(bases: str, strand: str, frame: int) -> Seq:
    """The bases from the frame's first, on the strand chosen."""
    dna = Seq(bases.upper())
    if strand == "reverse":
        dna = dna.reverse_complement()
    return dna[frame - 1 :]


def translation(
    bases: str, strand: str, frame: int, table: int, to_stop: bool, cds: bool = False
) -> str:
    """The frame's translation. As a complete coding sequence (`cds`), a Biopython
    TranslationError says why it isn't one."""
    dna = frame_of(bases, strand, frame)
    if cds:
        return str(dna.translate(table=table, cds=True))
    dna = dna[: len(dna) - len(dna) % 3]
    with warnings.catch_warnings():
        # In codes 27, 28 and 31, Biopython keeps a codon that's a stop or an amino acid as the
        # amino acid, and warns so; the run says it of the one codon where it matters.
        warnings.simplefilter("ignore", BiopythonWarning)
        return str(dna.translate(table=table, to_stop=to_stop))


def translate(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    strand = params.get("strand", "forward")
    frame = int(params.get("frame", 1))
    table = int(params.get("table", 1))
    cds = bool(params.get("complete_cds", False))
    to_stop = bool(params.get("to_stop", True)) and not cds
    try:
        name = str(code(table).names[0])
    except ValueError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    either = dual(table)
    if either and to_stop:
        shown = ", ".join(f"{c} ({either[c]} or a stop)" for c in sorted(either))
        print(
            f"::error In NCBI's genetic code {table}, {shown} can end a protein or code for an "
            "amino acid, by where it is, so it can't stop at the first stop codon: translate "
            "it as a complete coding sequence, or keep each stop codon.",
            flush=True,
        )
        sys.exit(1)
    sign = "+" if strand == "forward" else "-"
    entities = run["inputs"]["sequence"]
    creates = []
    messages = []
    for done, entity in enumerate(entities, start=1):
        label = entity["values"].get("name") or entity["hrid"]
        where = f"frame {sign}{frame}"
        bases = entity["values"]["bases"]
        try:
            sequence = translation(bases, strand, frame, table, to_stop, cds)
        except CodonTable.TranslationError as why:
            messages.append(
                {
                    "level": "error",
                    "text": f"{label} isn't a complete coding sequence in {where}: "
                    + f"{str(why).rstrip('.')}.",
                    "entity": entity["id"],
                }
            )
            print(f"{entity['hrid']}: not a complete coding sequence")
            print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
            continue
        if not sequence.replace("*", ""):
            if len(frame_of(bases, strand, frame)) < 3:
                why = ": it has no whole codon."
            elif to_stop:
                why = ": it starts with a stop codon."
            else:
                why = ": it has only stop codons."
            messages.append(
                {
                    "level": "error",
                    "text": f"{label} gives no residues in {where}{why}",
                    "entity": entity["id"],
                }
            )
            print(f"{entity['hrid']}: no residues")
        else:
            whole = str(frame_of(bases, strand, frame))
            last = whole[: len(whole) - len(whole) % 3][-3:]
            could = could_end(last, table)
            if not cds and could:
                kept = sequence[-1]
                said = (
                    f"{label}'s last codon in {where}, {last}, is {could} or a stop "
                    f"in genetic code {table}, so it was kept as {kept}"
                )
                # Biopython's complete coding sequence ends only with a codon every
                # reading of which is a stop.
                if others := never_stops(last, table):
                    listed = " and ".join(
                        [", ".join(others[:-1]), others[-1]] if others[:-1] else others
                    )
                    said += (
                        ". It can't end a complete coding sequence: "
                        f"{listed}, which it could be, {'is' if len(others) == 1 else 'are'} "
                        "never a stop."
                    )
                else:
                    said += ": translate it as a complete coding sequence to end it there."
                messages.append({"level": "info", "text": said, "entity": entity["id"]})
            how = ", as a complete coding sequence," if cds else ","
            values: dict[str, Any] = {
                "name": f"{label} ({sign}{frame})"[:MAX_NAME],
                "description": (
                    f"Translated from {entity['hrid']}, frame {sign}{frame}{how} "
                    f"with NCBI's genetic code {table} ({name})."
                ),
                "sequence": sequence,
                "encoded_by": entity["id"],
            }
            if entity["values"].get("organism"):
                values["organism"] = entity["values"]["organism"]
            creates.append(
                {"type": "iki/protein.Protein", "values": values, "derived_from": [entity["id"]]}
            )
            print(f"{entity['hrid']}: {len(sequence.replace('*', '')):,} residues")
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
