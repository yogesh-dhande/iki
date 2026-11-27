"""`apply_mutagenesis`: the changes a design states, checked against the template, made,
and the primers that make them designed and registered (ADR 0204).

It reads the run and writes its results with Iki's action helper, iki_action.py, and
reports progress with `::progress` lines.

`scripts/mutations.py` reads the changes and applies them; `scripts/primers.py` designs
the primers. What this does is the run: check what was asked for, register the mutant as a
DNA sequence derived from the design and each primer as a primer derived from it too, and
say what the changes do to the protein where the design says where the coding region is.
"""

from __future__ import annotations

from typing import Any

import iki_action as iki
import mutations
import primers
from sequences import reverse_complement

#: The translation tables a design may name, by the NCBI number each is (ADR 0172 uses
#: the same names in `find_orfs`).
TABLES = {
    "standard": 1,
    "bacterial_archaeal_plant_plastid": 11,
    "vertebrate_mitochondrial": 2,
    "yeast_mitochondrial": 3,
    "mold_protozoan_coelenterate_mitochondrial": 4,
    "invertebrate_mitochondrial": 5,
    "alternative_yeast_nuclear": 12,
}
#: How a protocol is said in words, for the record and for a primer's description.
PROTOCOLS = {
    "overlapping": (
        "overlapping primers (the QuikChange design: Braman, Papworth and Greener, "
        "Methods Mol Biol 57:31, 1996)"
    ),
    "partially_overlapping": (
        "partially overlapping primers (Zheng, Baumann and Reymond, Nucleic Acids Res "
        "32:e115, 2004; Liu and Naismith, BMC Biotechnol 8:91, 2008)"
    ),
    "back_to_back": (
        "back-to-back primers, amplifying the circle outwards to be ligated blunt "
        "(Hemsley et al., Nucleic Acids Res 17:6545, 1989)"
    ),
}


class Stop(Exception):  # noqa: N818 - reads as "stop: why"
    """A run that can't go on, said in words a lab can act on."""


def progress(done: int, total: int) -> None:
    print(f"::progress {done / total:.2f} {done} of {total}", flush=True)


def values_of(entity: dict[str, Any]) -> dict[str, Any]:
    found: dict[str, Any] = entity.get("values") or {}
    return found


def one_included(entity: dict[str, Any], name: str, what: str) -> dict[str, Any]:
    found = (entity.get("included") or {}).get(name) or []
    if len(found) != 1:
        raise Stop(f"{entity['hrid']} has no {what}: its {name} names {len(found)} sequences.")
    one: dict[str, Any] = found[0]
    return one


def rules_from(params: dict[str, Any], protocol: str) -> primers.Rules:
    """The primer rules a run asked for, over the protocols' published defaults."""
    given = primers.Rules(protocol=protocol)
    return primers.Rules(
        protocol=protocol,
        shortest=int(params.get("shortest_primer") or given.shortest),
        longest=int(params.get("longest_primer") or given.longest),
        flank=int(params.get("least_flank") or given.flank),
        min_gc=float(params.get("min_primer_gc") or given.min_gc),
        quikchange_tm=float(params.get("quikchange_tm") or given.quikchange_tm),
        least_own=int(params.get("least_own_end") or given.least_own),
        least_edge=int(params.get("least_edge") or given.least_edge),
        gap=(
            float(params.get("least_tm_gap") or given.gap[0]),
            float(params.get("most_tm_gap") or given.gap[1]),
        ),
        anneal_tm=float(params.get("anneal_tm") or given.anneal_tm),
        gc_clamp=bool(params.get("gc_clamp", given.gc_clamp)),
    )


def coding_of(values: dict[str, Any], length: int) -> tuple[int, int, str] | None:
    """Where the coding region is, 0-based and end exclusive, or None where the design
    doesn't say."""
    start, end = values.get("coding_start"), values.get("coding_end")
    if start is None or end is None:
        return None
    start, end = int(start), int(end)
    if start < 1 or end > length or end < start:
        raise Stop(
            f"The coding region {start}..{end} isn't inside the template's {length:,} bases."
        )
    if (end - start + 1) % 3:
        raise Stop(
            f"The coding region {start}..{end} is {end - start + 1:,} bases, which isn't a "
            "whole number of codons."
        )
    return start - 1, end, str(values.get("coding_strand") or "forward")


def read_region(bases: str, where: tuple[int, int, str]) -> str:
    start, end, strand = where
    found = bases[start:end]
    return reverse_complement(found) if strand == "reverse" else found


def consequences(
    template: str, mutant: str, where: tuple[int, int, str], table_number: int, moved: int
) -> list[dict[str, str]]:
    """What the changes do to the protein: one entry, since a design's changes are made
    together and the protein they give is one protein.

    The coding region's end moves by how much the changes before it added or took away,
    so that the same gene is read in the mutant; a change that leaves it no longer a
    whole number of codons is a frameshift, which is named as one.
    """
    start, end, strand = where
    before = mutations.translate(read_region(template, where), mutations.genetic_code(table_number))
    after_region = read_region(mutant, (start, end + moved, strand))
    if len(after_region) % 3:
        # The reading frame itself moved: translate as far as the bases go and name where
        # the new frame stops.
        after = mutations.translate(after_region, mutations.genetic_code(table_number))
        effect, name = mutations.frameshift(before, after)
        return [{"effect": effect, "name": name}]
    after = mutations.translate(after_region, mutations.genetic_code(table_number))
    effect, name = mutations.protein_change(before, after)
    return [{"effect": effect, "name": name}]


def designed_for(
    mutant: str,
    change: mutations.Stated,
    moved: int,
    rules: primers.Rules,
    circular: bool,
) -> list[primers.Designed]:
    """The primers that make one change, where it lies in the mutant."""
    start = change.start + moved
    stop = start + len(change.bases)
    replaced, written = change.replaced, len(change.bases)
    mismatched = written if written == replaced else 0
    inserted = 0 if written == replaced else written
    design = primers.DESIGNS[rules.protocol]
    return design(mutant, start, stop, rules, circular, mismatched=mismatched, inserted=inserted)


def run(given: dict[str, Any]) -> dict[str, Any]:
    params = given["params"]
    designs = given["inputs"]["mutagenesis"]
    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    messages: list[dict[str, str]] = []
    made = 0
    for done, design in enumerate(designs, start=1):
        values = values_of(design)
        template = one_included(design, "template", "template")
        bases = (values_of(template).get("bases") or "").upper()
        if not bases:
            raise Stop(f"{template['hrid']} has no bases.")
        circular = values_of(template).get("topology") == "circular"
        names = [str(each) for each in (values.get("changes") or [])]
        if not names:
            raise Stop(f"{design['hrid']} states no changes.")
        try:
            changes = mutations.read(names, bases, circular)
        except mutations.Refused as why:
            raise Stop(f"{design['hrid']}: {why}") from why
        mutant, origin = mutations.applied(bases, changes)
        protocol = str(values.get("primer_design") or "overlapping")
        rules = rules_from(params, protocol if protocol != "none" else "overlapping")
        # Where each change lies in the mutant: the ones before it have already moved it.
        moved = 0
        placed: list[tuple[mutations.Stated, int]] = []
        for change in changes:
            placed.append((change, moved))
            moved += change.difference
        found: list[tuple[primers.Designed, mutations.Stated]] = []
        if protocol != "none" and not origin:
            for change, before in placed:
                try:
                    found += [
                        (primer, change)
                        for primer in designed_for(mutant, change, before, rules, circular)
                    ]
                except primers.Impossible as why:
                    raise Stop(f"{design['hrid']}, {change.said}: {why}") from why
        elif protocol != "none" and origin:
            messages.append(
                {
                    "level": "warning",
                    "text": (
                        f"{design['hrid']}: a change runs across the template's origin, so the "
                        "mutant is written from that change and no primers were designed for "
                        "it. Register the mutant and design the primers against it."
                    ),
                    "entity": design["id"],
                }
            )
        product_key = f"mutant-{done}"
        name = values.get("name") or values_of(template).get("name") or design["hrid"]
        said = ", ".join(change.said for change in changes)
        creates.append(
            {
                "type": "iki/dna.DNASequence",
                "key": product_key,
                "derived_from": [design["id"]],
                "values": {
                    "bases": mutant,
                    "topology": values_of(template).get("topology") or "linear",
                    "strandedness": values_of(template).get("strandedness") or "double",
                    "name": f"{name}, {said}"
                    if len(said) < 150
                    else f"{name}, {len(changes)} changes",
                    "description": (
                        f"{template['hrid']} with {said}, by {design['hrid']}."
                        + (
                            f" Written from base {origin + 1:,} of the template, since a change "
                            "runs across its origin."
                            if origin
                            else ""
                        )
                    ),
                },
            }
        )
        for which, (primer, change) in enumerate(found, start=1):
            creates.append(
                {
                    "type": "iki/dna.Primer",
                    "key": f"primer-{done}-{which}",
                    "derived_from": [design["id"]],
                    "values": {
                        "bases": primer.bases,
                        "direction": primer.direction,
                        "target": template["id"],
                        "name": f"{name} {change.said} {primer.role}",
                        "description": _said(primer, change, protocol),
                    },
                }
            )
        table_number = TABLES[str(values.get("genetic_code") or "standard")]
        where = coding_of(values, len(bases))
        protein = (
            consequences(bases, mutant, where, table_number, len(mutant) - len(bases))
            if where and not origin
            else []
        )
        updates.append(
            {
                "entity": design["id"],
                "values": {
                    "product_length": len(mutant),
                    "changes_made": [
                        {
                            "stated": change.said,
                            "name": mutations.named_change(bases, change, circular),
                            "kind": change.kind,
                            "start": change.start + 1,
                            "end": (change.stop - 1) % len(bases) + 1,
                            "difference": change.difference,
                        }
                        for change in changes
                    ],
                    "primers": [
                        {
                            "change": change.said,
                            "role": primer.role,
                            "bases": primer.bases,
                            "direction": primer.direction,
                            "length": primer.length,
                            "matched": primer.matched,
                            "measures": [
                                {"what": what, "value": round(value, 1)}
                                for what, value in primer.measures
                            ],
                        }
                        for primer, change in found
                    ],
                    "protein_changes": protein,
                    "primers_designed": len(found),
                    "designed_with": _how(protocol, rules),
                },
            }
        )
        made += len(found)
        print(
            f"{design['hrid']}: {len(changes)} change(s) in {template['hrid']}, "
            f"{len(mutant):,} bp, {len(found)} primer(s)"
        )
        progress(done, len(designs))
    return {
        "updates": {"mutagenesis": updates},
        "creates": creates,
        "messages": messages,
        "metrics": {
            "changes": sum(len(values_of(d).get("changes") or []) for d in designs),
            "primers": made,
        },
    }


def _said(primer: primers.Designed, change: mutations.Stated, protocol: str) -> str:
    measures = "; ".join(f"{what} {value:.1f} °C" for what, value in primer.measures)
    notes = "".join(f" {note}." for note in primer.notes)
    return (
        f"The {primer.role} primer for {change.said}, by {PROTOCOLS[protocol]}. "
        f"{primer.length} bases, {primer.matched} of them matching the template"
        + (f", {primer.tail} carried on its 5' end" if primer.tail else "")
        + (f". {measures}" if measures else "")
        + notes
    )


def _how(protocol: str, rules: primers.Rules) -> str:
    if protocol == "none":
        return "the changes applied; no primers designed"
    parts = [PROTOCOLS[protocol], f"{rules.shortest}-{rules.longest} bases"]
    if protocol == "overlapping":
        parts += [
            f"the change at least {rules.flank} bases from either end",
            f"at least {rules.min_gc:g}% GC",
            f"at least {rules.quikchange_tm:g} °C by the kit manual's formula",
        ]
    elif protocol == "partially_overlapping":
        parts += [
            f"at least {rules.least_own} bases of its own at each 3' end",
            f"at least {rules.least_edge} bases between the change and each 5' end",
            (
                f"the non-overlapping part {rules.gap[0]:g}-{rules.gap[1]:g} °C above the shared "
                f"part by Wetmur's formula at {rules.potassium * 1000:g} mM K+"
            ),
        ]
    else:
        parts += [
            f"annealing at {rules.anneal_tm:g} °C by SantaLucia's nearest-neighbour model",
            "the product phosphorylated and ligated blunt",
        ]
    if rules.gc_clamp:
        parts.append("each ending in G or C")
    return "; ".join(parts)


if __name__ == "__main__":
    iki.main(run, refuse=(Stop, mutations.Refused, primers.Impossible))
