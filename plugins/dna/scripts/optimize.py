"""The `optimize_codons` command: a coding region rewritten in a host's own codons.

It reads the run input from $IKI_INPUT, reports progress with `::progress` lines, says
why it can't go on with `::error`, and writes its results to $IKI_OUTPUT, as every
action's command does.

The science is in `codons.py`, which says where every rule and every number comes from.
This reads the design off the record, works the region out of the template, calls the
optimizer, and writes what it found: the optimized sequence as a DNA sequence derived
from the design, and on the design itself what moved and what is still broken.

The product is the **whole template with the coding region rewritten**, not the region
alone: optimizing a gene registered on its own gives the optimized gene, and optimizing a
gene inside a vector gives the vector with that gene optimized. A synonymous substitution
never changes a length, so the product is the template's length and every coordinate
outside the region still points where it did -- which is why the template's annotated
features come across unchanged.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import codons

HERE = Path(__file__).resolve().parent
USAGE = HERE.parent / "data" / "codon_usage.json"

#: What each `genetic_code` value is, as NCBI numbers its translation tables; the same
#: names the plasmid plugin's ORF finder offers (ADR 0172).
TABLES = {
    "standard": 1,
    "bacterial_archaeal_plant_plastid": 11,
    "vertebrate_mitochondrial": 2,
    "yeast_mitochondrial": 3,
    "mold_protozoan_coelenterate_mitochondrial": 4,
    "invertebrate_mitochondrial": 5,
    "alternative_yeast_nuclear": 12,
}


class Stop(Exception):  # noqa: N818 - reads as "the run stops here"
    """Why the run can't go on, as its last `::error` line."""


def progress(done: int, total: int) -> None:
    print(f"::progress {done / max(total, 1):.2f} {done} of {total}")


def values_of(entity: dict[str, Any]) -> dict[str, Any]:
    return entity.get("values") or {}


def one_included(entity: dict[str, Any], name: str, what: str) -> dict[str, Any]:
    found = (entity.get("included") or {}).get(name) or []
    if len(found) != 1:
        raise Stop(f"{entity['hrid']} has no {what}: its {name} names {len(found)} sequences.")
    return found[0]


def constraints_from(params: dict[str, Any]) -> codons.Constraints:
    return codons.Constraints(
        rare_codon_threshold=float(params.get("rare_codon_threshold", 0.1)),
        gc_min=float(params.get("gc_min", 30.0)),
        gc_max=float(params.get("gc_max", 70.0)),
        gc_window=int(params.get("gc_window", 50)),
        gc_window_min=float(params.get("gc_window_min", 25.0)),
        gc_window_max=float(params.get("gc_window_max", 75.0)),
        longest_homopolymer=int(params.get("longest_homopolymer", 6)),
        longest_repeat=int(params.get("longest_repeat", 15)),
        longest_hairpin_stem=int(params.get("longest_hairpin_stem", 10)),
        hairpin_loop=int(params.get("hairpin_loop", 30)),
        avoid_host_motifs=bool(params.get("avoid_host_motifs", True)),
    )


def region_of(design: dict[str, Any], bases: str) -> tuple[int, int, str, str]:
    """The coding region of the template: where it starts and ends on the forward strand
    (from 1, inclusive) and the bases as they are read, 5' to 3' on their own strand.

    A region of a linear or circular molecule is read along the numbering, and never
    across the end of the bases: a coding sequence wrapping a circle's origin is given as
    the sequence written from another origin, which is the same molecule (ADR 0138).
    """
    values = values_of(design)
    start = int(values.get("coding_start") or 1)
    end = int(values.get("coding_end") or len(bases))
    strand = values.get("coding_strand") or "forward"
    if start > end:
        raise Stop(
            f"The coding region starts at {start} and ends at {end}: give a region that "
            "reads along the sequence. A gene on the other strand is read from its lowest "
            "base to its highest with coding_strand reverse."
        )
    if end > len(bases):
        raise Stop(
            f"The coding region ends at {end}, past the template's {len(bases)} bases: "
            "give a region that lies on it."
        )
    read = bases[start - 1 : end]
    if strand == "reverse":
        read = codons.reverse_complement(read)
    return start, end, strand, read


def replaced(bases: str, start: int, end: int, strand: str, written: str) -> str:
    """The template with the region's bases replaced by the optimized ones, written back
    on the strand the region is read on."""
    piece = codons.reverse_complement(written) if strand == "reverse" else written
    return bases[: start - 1] + piece + bases[end:]


def run(given: dict[str, Any]) -> dict[str, Any]:
    tables = codons.read_usage(json.loads(USAGE.read_text()))
    params = given["params"]
    constraints = constraints_from(params)
    designs = given["inputs"]["optimization"]
    creates: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    changed_all, unmet_all = 0, 0
    best_cai: float | None = None
    for done, design in enumerate(designs, start=1):
        values = values_of(design)
        host = tables.get(str(values.get("host")))
        if host is None:
            raise Stop(
                f"{design['hrid']} names the host {values.get('host')}, which has no codon "
                f"usage table here: choose one of {', '.join(sorted(tables))}."
            )
        source_key = values.get("source_organism")
        source = tables.get(str(source_key)) if source_key else None
        table_number = TABLES[str(values.get("genetic_code") or "standard")]
        template = one_included(design, "template", "template")
        bases = values_of(template)["bases"].upper()
        start, end, strand, read = region_of(design, bases)
        enzymes = tuple(values.get("avoid_enzymes") or ())
        motifs = tuple(values.get("avoid_motifs") or ())
        asked = codons.Constraints(
            rare_codon_threshold=constraints.rare_codon_threshold,
            gc_min=constraints.gc_min,
            gc_max=constraints.gc_max,
            gc_window=constraints.gc_window,
            gc_window_min=constraints.gc_window_min,
            gc_window_max=constraints.gc_window_max,
            longest_homopolymer=constraints.longest_homopolymer,
            longest_repeat=constraints.longest_repeat,
            longest_hairpin_stem=constraints.longest_hairpin_stem,
            hairpin_loop=constraints.hairpin_loop,
            avoid_enzymes=enzymes,
            avoid_motifs=motifs,
            avoid_host_motifs=constraints.avoid_host_motifs,
        )
        try:
            design_found = codons.optimize(
                read,
                host,
                table_number=table_number,
                strategy=str(values.get("strategy") or "highest_usage"),
                constraints=asked,
                source_usage=source,
            )
        except codons.Refused as why:
            raise Stop(f"{design['hrid']}: {why}") from why
        product = replaced(bases, start, end, strand, design_found.bases)
        # The design's name if the lab gave it one; otherwise the template's, said to be
        # in this host's codons, which is what a reader of the product needs to see.
        called = values_of(template).get("name")
        name = values.get("name") or (f"{called}, {host.common} codons" if called else None)
        name = name or design["hrid"]
        where = f"{start}..{end}" + (" on the other strand" if strand == "reverse" else "")
        description = (
            f"{host.name} codons for {template['hrid']} {where}, by {design['hrid']}: "
            f"{design_found.codons_changed} of {design_found.codons} codons written "
            "differently, the protein unchanged."
        )
        creates.append(
            {
                "type": "iki/dna.DNASequence",
                "key": f"optimized-{done}",
                "derived_from": [design["id"]],
                "values": {
                    "bases": product,
                    # A synonymous rewrite changes no length and no end, so the molecule
                    # the product is, is the molecule the template was.
                    "topology": values_of(template).get("topology") or "linear",
                    "strandedness": values_of(template).get("strandedness") or "double",
                    "name": name,
                    "description": description,
                    # The region's length is unchanged, so every feature of the template
                    # still lies where it did.
                    "features": values_of(template).get("features") or [],
                },
            }
        )
        protein = design_found.protein.rstrip("*")
        updates.append(
            {
                "entity": design["id"],
                "values": {
                    "codons": design_found.codons,
                    "codons_changed": design_found.codons_changed,
                    "protein_length": len(protein),
                    "source_cai": _rounded(design_found.source_cai),
                    "optimized_cai": _rounded(design_found.optimized_cai),
                    "cai_reference": host.described,
                    "source_gc_content": _rounded(design_found.source_gc, 2),
                    "optimized_gc_content": _rounded(design_found.optimized_gc, 2),
                    "constraints_met": not design_found.unmet,
                    "unmet_constraints": [v.value() for v in design_found.unmet],
                    "codon_changes": design_found.changes,
                    "optimized_with": codons.described(
                        host,
                        design_found.strategy,
                        table_number,
                        asked,
                        source,
                    ),
                },
            }
        )
        changed_all += design_found.codons_changed
        unmet_all += len(design_found.unmet)
        if design_found.optimized_cai is not None:
            best_cai = max(best_cai or 0.0, design_found.optimized_cai)
        print(
            f"{design['hrid']}: {design_found.codons} codons of {template['hrid']} {where} "
            f"for {host.described}; {design_found.codons_changed} changed, CAI "
            f"{_say(design_found.source_cai)} -> {_say(design_found.optimized_cai)}, GC "
            f"{_say(design_found.source_gc, 1)}% -> {_say(design_found.optimized_gc, 1)}%"
        )
        for violation in design_found.unmet:
            print(
                f"  still broken: {violation.kind} {violation.start}..{violation.end}, "
                f"{violation.detail}"
            )
        if design_found.unmet:
            kinds = codons.counted(design_found.unmet)
            told = ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
            messages.append(
                {
                    "level": "warning",
                    "text": (
                        f"The optimized gene still breaks {told}: no synonymous codon "
                        "removes them without breaking something else. The design is "
                        "registered as it is, with each one placed; widen the band, allow "
                        "a longer repeat, or optimize fewer constraints at once."
                    ),
                    "entity": design["id"],
                }
            )
        progress(done, len(designs))
    return {
        "creates": creates,
        "updates": {"optimization": updates},
        "messages": messages,
        "metrics": {
            "codons_changed": changed_all,
            "cai_after": round(best_cai, 4) if best_cai is not None else 0,
            "unmet": unmet_all,
        },
    }


def _rounded(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _say(value: float | None, places: int = 3) -> str:
    return "none" if value is None else f"{value:.{places}f}"


def main() -> None:
    given = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        found = run(given)
    except (Stop, codons.Refused) as why:
        print(f"::error {why}")
        sys.exit(1)
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **found}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
