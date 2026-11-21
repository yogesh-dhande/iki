"""The plasmid plugin's commands: `register` a plasmid from its bases, `annotate` one from
the library of common vector parts, `orfs` its open reading frames, `primer_sites` where
primers bind it, `pcr` what a PCR amplifies, `assemble` what a cloning plan makes, `check`
a plan against the sequence its clone turned out to have, and `digest` a plan's parts
virtually, listing every fragment.

Each reads the run input from $IKI_INPUT, reports progress with `::progress` lines, says
why it can't go on with `::error`, and writes its results to $IKI_OUTPUT, as every
action's command does.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import cloning
import library
import orfs as orf_finder
import priming

HERE = Path(__file__).resolve().parent
FEATURES = HERE.parent / "data" / "features.json"
#: The methods Iki works out. Gateway recombines at att sites and TOPO joins through a
#: topoisomerase: both are recorded, and neither is simulated here.
SIMULATED = {"digest", "restriction_ligation", "golden_gate", "gibson"}


class Stop(Exception):  # noqa: N818 - reads as "the run stops here"
    """Why the run can't go on, as its last `::error` line."""


def progress(done: int, total: int) -> None:
    print(f"::progress {done / max(total, 1):.2f} {done} of {total}")


def values_of(entity: dict[str, Any]) -> dict[str, Any]:
    return entity.get("values") or {}


def included(entity: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return (entity.get("included") or {}).get(name) or []


def one_included(entity: dict[str, Any], name: str, what: str) -> dict[str, Any]:
    found = included(entity, name)
    if len(found) != 1:
        raise Stop(f"{entity['hrid']} has no {what}: its {name} names {len(found)} sequences.")
    return found[0]


def label_of(sequence: dict[str, Any]) -> str:
    """How a part is named in a junction: its name, with its ID."""
    name = values_of(sequence).get("name")
    return f"{name} ({sequence['hrid']})" if name else sequence["hrid"]


# --- register ------------------------------------------------------------------------


def register(run: dict[str, Any]) -> dict[str, Any]:
    params = run["params"]
    bases = params["bases"].strip().upper()
    if not bases:
        raise Stop("A plasmid needs its bases.")
    # A plasmid is a circle unless it's said to be linear, as GenBank's LOCUS line and
    # SnapGene record it. The same bases each way are two molecules, so this is part of
    # what's registered, and the plasmid's topology is the sequence's (ADR 0168).
    topology = params.get("topology") or "circular"
    sequence: dict[str, Any] = {
        "bases": bases,
        "topology": topology,
        "strandedness": "double",
    }
    plasmid: dict[str, Any] = {"sequence": {"$create": "sequence"}, "topology": topology}
    for name in ("name",):
        if params.get(name):
            sequence[name] = plasmid[name] = params[name]
    if params.get("accession"):
        sequence["accession"] = params["accession"]
    if params.get("description"):
        plasmid["description"] = params["description"]
    if params.get("addgene_id"):
        plasmid["addgene_id"] = params["addgene_id"]
        plasmid["source"] = "addgene"
    print(f"{len(bases)} bp, {topology} and double-stranded: registering its sequence and plasmid")
    return {
        "creates": [
            {"type": "iki/dna.DNASequence", "key": "sequence", "values": sequence},
            {"type": "iki/plasmid.Plasmid", "values": plasmid},
        ]
    }


# --- annotate ------------------------------------------------------------------------


def annotate(run: dict[str, Any]) -> dict[str, Any]:
    parts = library.Library.read(FEATURES)
    least = float(run["params"].get("min_identity", 0.9))
    plasmids = run["inputs"]["plasmid"]
    updates, messages = [], []
    for done, plasmid in enumerate(plasmids, start=1):
        try:
            sequence = one_included(plasmid, "sequence", "sequence")
        except Stop as why:
            messages.append({"level": "error", "text": str(why), "entity": plasmid["id"]})
            continue
        bases = values_of(sequence)["bases"]
        topology = values_of(sequence).get("topology") or "linear"
        circular = topology == "circular"
        hits = library.annotate(bases, circular, parts, min_identity=least)
        values: dict[str, Any] = {
            "annotated_features": library.features(hits, parts),
            "annotated_with": (
                f"iki/plasmid features {parts.version} ({len(parts.parts)} parts), "
                f"at least {round(100 * least)}% identity"
            ),
            "replication_origins": [],
            "bacterial_resistance": [],
            "selection_markers": [],
            "promoters": [],
        }
        values.update(library.implied(hits))
        # Its length and its topology are its registry entry's, the same for every
        # project: a run fills each where it's empty, and never writes over what the
        # entry has (ADR 0115).
        if values_of(plasmid).get("length") is None:
            values["length"] = len(bases)
        if values_of(plasmid).get("topology") is None:
            values["topology"] = topology
        updates.append({"entity": plasmid["id"], "values": values})
        found = ", ".join(f"{hit.part.name} {hit.start}..{hit.end}" for hit in hits) or "nothing"
        print(f"{plasmid['hrid']}: {len(bases)} bp, {topology}, {len(hits)} parts found: {found}")
        progress(done, len(plasmids))
    return {"updates": {"plasmid": updates}, "messages": messages}


# --- open reading frames and primer binding sites (ADR 0169) -------------------------

#: What each `genetic_code` param is, as NCBI numbers its translation tables.
TABLES = {
    "standard": 1,
    "bacterial_archaeal_plant_plastid": 11,
    "vertebrate_mitochondrial": 2,
    "yeast_mitochondrial": 3,
    "mold_protozoan_coelenterate_mitochondrial": 4,
    "invertebrate_mitochondrial": 5,
    "alternative_yeast_nuclear": 12,
}


def _sequence_of(plasmid: dict[str, Any]) -> tuple[str, bool]:
    """A plasmid's bases and whether they are a circle, from its sequence entry."""
    sequence = one_included(plasmid, "sequence", "sequence")
    values = values_of(sequence)
    return values["bases"].upper(), (values.get("topology") or "linear") == "circular"


def find_orfs(run: dict[str, Any]) -> dict[str, Any]:
    params = run["params"]
    least = int(params.get("least_codons", orf_finder.LEAST_CODONS))
    code = str(params.get("genetic_code", "standard"))
    table = TABLES[code]
    alternative = bool(params.get("alternative_starts", False))
    plasmids = run["inputs"]["plasmid"]
    updates, messages = [], []
    found_all, longest = 0, 0
    for done, plasmid in enumerate(plasmids, start=1):
        try:
            bases, circular = _sequence_of(plasmid)
        except Stop as why:
            messages.append({"level": "error", "text": str(why), "entity": plasmid["id"]})
            continue
        found = orf_finder.find(
            bases,
            circular,
            table_number=table,
            alternative_starts=alternative,
            least=least,
        )
        found_all += len(found)
        longest = max([longest, *[orf.codons for orf in found]])
        updates.append(
            {
                "entity": plasmid["id"],
                "values": {
                    "orfs": [orf.value() for orf in found],
                    "orfs_found_with": orf_finder.described(table, alternative, least, circular),
                },
            }
        )
        across = sum(1 for orf in found if orf.end < orf.start)
        print(
            f"{plasmid['hrid']}: {len(found)} ORFs of at least {least} codons in six frames, "
            f"table {table}" + (f", {across} across its origin" if across else "")
        )
        for orf in found:
            print(
                f"  frame {orf.frame:+d} {orf.start}..{orf.end} ({orf.strand}), "
                f"{orf.codons} codons from {orf.start_codon}"
            )
        progress(done, len(plasmids))
    return {
        "updates": {"plasmid": updates},
        "messages": messages,
        "metrics": {"orfs": found_all, "longest": longest},
    }


def _primers(run: dict[str, Any]) -> list[dict[str, Any]]:
    given = []
    for primer in run["inputs"].get("primers") or []:
        values = values_of(primer)
        given.append(
            {
                "hrid": primer["hrid"],
                "name": values.get("name") or primer["hrid"],
                "bases": values["bases"].upper(),
            }
        )
    return given


def find_primer_sites(run: dict[str, Any]) -> dict[str, Any]:
    params = run["params"]
    anchor = int(params.get("least_three_prime_match", priming.ANCHOR))
    most = int(params.get("most_mismatch_weight", priming.MOST_PENALTY))
    plasmid = run["inputs"]["plasmid"][0]
    primers = _primers(run)
    bases, circular = _sequence_of(plasmid)
    short = [p["hrid"] for p in primers if len(p["bases"]) < priming.SHORTEST]
    sites = priming.find_sites(bases, circular, primers, anchor=anchor, most_penalty=most)
    found_with = (
        f"placed by their {anchor} 3' bases, mismatches weighing at most {most} "
        f"(3 within the last {priming.THREE_PRIME} bases, 1 beyond); "
        f"Tm by {priming.CONDITIONS_TEXT}"
    )
    messages = []
    if short:
        messages.append(
            {
                "level": "warning",
                "text": (
                    f"{', '.join(short)} {'is' if len(short) == 1 else 'are'} shorter than "
                    f"{priming.SHORTEST} bases, which binds too many places to say anything, "
                    "so no site is reported for it."
                ),
                "entity": plasmid["id"],
            }
        )
    bound = {site.hrid for site in sites}
    silent = [p["hrid"] for p in primers if p["hrid"] not in bound and p["hrid"] not in short]
    if silent:
        messages.append(
            {
                "level": "warning",
                "text": (
                    f"{', '.join(silent)} {'binds' if len(silent) == 1 else 'bind'} nowhere on "
                    f"{plasmid['hrid']} under this 3' match and mismatch weight: allow more "
                    "mismatches, or a shorter 3' match, to see weaker sites."
                ),
                "entity": plasmid["id"],
            }
        )
    exact = sum(1 for site in sites if not site.mismatches)
    print(
        f"{plasmid['hrid']}: {len(sites)} binding sites of {len(primers)} primers "
        f"({exact} exact), {len(bases)} bp {'circular' if circular else 'linear'}"
    )
    for site in sites:
        how = (
            "exactly"
            if not site.mismatches
            else ", ".join(
                f"{mine}->{theirs} {away} from its 3' end" for away, mine, theirs in site.mismatches
            )
        )
        print(
            f"  {site.hrid} {site.strand} {site.start}..{site.end}, 3' end at "
            f"{site.three_prime}, {site.annealed} of {len(site.bases)} bases paired {how}"
            + (f", Tm {site.tm} C" if site.tm is not None else "")
        )
    return {
        "updates": {
            "plasmid": [
                {
                    "entity": plasmid["id"],
                    "values": {
                        "primer_binding_sites": [site.value() for site in sites],
                        "primer_binding_sites_found_with": found_with,
                    },
                }
            ]
        },
        "messages": messages,
        "metrics": {"sites": len(sites), "exact": exact},
    }


# --- PCR -----------------------------------------------------------------------------

#: Cleared when a PCR gives no one product: what an earlier run worked out says nothing
#: about this reaction, and nobody must read it as current.
NO_PRODUCT: dict[str, Any] = {
    "product_length": None,
    "forward_melting_temperature": None,
    "reverse_melting_temperature": None,
    "products_found": None,
}


def _site_for(sites: list[priming.Site], primer: dict[str, Any], strand: str) -> list[priming.Site]:
    return [s for s in sites if s.hrid == primer["hrid"] and s.strand == strand]


def simulate_pcr(run: dict[str, Any]) -> dict[str, Any]:
    params = run["params"]
    pcr = run["inputs"]["pcr"][0]
    template = one_included(pcr, "template", "template")
    bases = values_of(template)["bases"].upper()
    circular = (values_of(template).get("topology") or "linear") == "circular"
    forward = one_included(pcr, "forward_primer", "forward primer")
    reverse = one_included(pcr, "reverse_primer", "reverse primer")
    pair = [
        {
            "hrid": one["hrid"],
            "name": values_of(one).get("name") or one["hrid"],
            "bases": values_of(one)["bases"].upper(),
        }
        for one in (forward, reverse)
    ]
    anchor = int(params.get("least_three_prime_match", priming.ANCHOR))
    most = int(params.get("most_mismatch_weight", priming.MOST_PENALTY))
    longest = int(params.get("longest_product", priming.LONGEST_PRODUCT))
    sites = priming.find_sites(bases, circular, pair, anchor=anchor, most_penalty=most)
    ahead = _site_for(sites, pair[0], "forward")
    back = _site_for(sites, pair[1], "reverse")
    _refuse_unless_both_bind(pcr, template, pair, sites, ahead, back, circular)
    made = priming.products(bases, circular, [*ahead, *back], longest=longest)
    if not made:
        raise Stop(
            f"{pair[0]['hrid']} and {pair[1]['hrid']} both bind {template['hrid']}, but no pair "
            "of their sites faces the other with a product between them"
            + (
                ": the forward primer's 3' end lies after the reverse primer's, so they point "
                "away from each other. Swap them, or give the primers for the other strand."
                if not circular
                else f", or none is within {longest:,} bp."
            )
        )
    print(
        f"{pcr['hrid']}: {pair[0]['hrid']} and {pair[1]['hrid']} on {template['hrid']} "
        f"({len(bases)} bp, {'circular' if circular else 'linear'}) amplify {len(made)} "
        f"molecule{'' if len(made) == 1 else 's'}"
    )
    if len(made) > 1:
        return _several(pcr, made)
    product = made[0]
    return _one_product(pcr, template, product, len(made))


def _refuse_unless_both_bind(
    pcr: dict[str, Any],
    template: dict[str, Any],
    pair: list[dict[str, Any]],
    sites: list[priming.Site],
    ahead: list[priming.Site],
    back: list[priming.Site],
    circular: bool,
) -> None:
    """Why the two primers can't amplify anything, where one of them doesn't bind the
    strand it must: a reaction that makes nothing is said plainly, naming the primer."""
    for primer, wanted, found in ((pair[0], "forward", ahead), (pair[1], "reverse", back)):
        if found:
            continue
        other = [s for s in sites if s.hrid == primer["hrid"]]
        if other:
            raise Stop(
                f"{primer['hrid']} binds {template['hrid']} only on the "
                f"{other[0].strand} strand, and {pcr['hrid']} has it as the {wanted} primer, "
                "which binds the other: the two primers of a PCR bind opposite strands and "
                "point at each other. Swap the forward and reverse primers."
            )
        raise Stop(
            f"{primer['hrid']} doesn't bind {template['hrid']} "
            f"({'circular' if circular else 'linear'}): its 3' end pairs nowhere under this 3' "
            "match and mismatch weight, so nothing is amplified. Check the primer and the "
            "template, or allow more mismatches."
        )


def _several(pcr: dict[str, Any], made: list[priming.Product]) -> dict[str, Any]:
    """Several products, so none is registered: the reaction doesn't say which it means,
    and a band on a gel settles it."""
    lengths = ", ".join(f"{len(one.bases):,} bp" for one in made)
    for one in made:
        print(
            f"  {len(one.bases):,} bp from {one.template_start} to {one.template_end} "
            f"({one.forward.hrid} 3' end {one.forward.three_prime}, {one.reverse.hrid} 3' end "
            f"{one.reverse.three_prime})"
        )
    return {
        "updates": {
            "pcr": [
                {
                    "entity": pcr["id"],
                    "values": {**NO_PRODUCT, "products_found": len(made)},
                }
            ]
        },
        "messages": [
            {
                "level": "warning",
                "text": (
                    f"The primers amplify {len(made)} molecules of this template ({lengths}), so "
                    "none is registered: the reaction doesn't say which. Use primers that bind "
                    "one place each, or raise the 3' match."
                ),
                "entity": pcr["id"],
            }
        ],
        "metrics": {"products": len(made), "length": len(made[0].bases)},
    }


def _one_product(
    pcr: dict[str, Any], template: dict[str, Any], product: priming.Product, count: int
) -> dict[str, Any]:
    name = values_of(pcr).get("name") or pcr["hrid"]
    across = product.template_end < product.template_start
    ahead, back = product.forward, product.reverse
    print(
        f"  {product.length:,} bp from {product.template_start} to {product.template_end}"
        + (" across the template's origin" if across else "")
        + f"; {ahead.hrid} 3' end at {ahead.three_prime} (Tm {ahead.tm} C, {ahead.tail} bp 5' "
        f"tail), {back.hrid} 3' end at {back.three_prime} (Tm {back.tm} C, {back.tail} bp 5' "
        "tail)"
    )
    description = (
        f"Amplified from {template['hrid']} by {pcr['hrid']}, "
        f"{product.template_start} to {product.template_end}"
        + (" across its origin" if across else "")
        + f", with {ahead.hrid} and {back.hrid}."
    )
    return {
        "creates": [
            {
                "type": "iki/dna.DNASequence",
                "key": "product",
                "derived_from": [pcr["id"]],
                "values": {
                    "bases": product.bases,
                    # A PCR product has two ends, whatever its template was (ADR 0168).
                    "topology": "linear",
                    "strandedness": "double",
                    "name": name,
                    "description": description,
                },
            }
        ],
        "updates": {
            "pcr": [
                {
                    "entity": pcr["id"],
                    "values": {
                        "product_length": product.length,
                        "forward_melting_temperature": ahead.tm,
                        "reverse_melting_temperature": back.tm,
                        "tm_method": priming.CONDITIONS_TEXT,
                        "products_found": count,
                    },
                }
            ]
        },
        "metrics": {"products": count, "length": product.length},
    }


# --- assemble and check --------------------------------------------------------------


def _fragments(
    assembly: dict[str, Any],
    method: str,
    enzymes: list[str],
    least: int,
    backbone_fragment: str = "longest",
) -> list[list[cloning.Fragment]]:
    """Each part of the plan, as the fragments it can give the reaction."""
    backbone = one_included(assembly, "backbone", "backbone")
    # A digest cuts one molecule and keeps a band: the inserts of an assembly have no
    # part in it, and the type's rules refuse a plan that names any.
    parts = [backbone] if method == "digest" else [backbone, *included(assembly, "inserts")]
    out: list[list[cloning.Fragment]] = []
    for part in parts:
        bases = values_of(part)["bases"].upper()
        circular = values_of(part).get("topology") == "circular"
        name = label_of(part)
        if method == "digest":
            fragments = cloning.digest(name, bases, circular, enzymes)
            if not fragments:
                raise Stop(
                    f"{part['hrid']} isn't cut by {', '.join(enzymes)}, and it's circular, so "
                    "the digest leaves no linear molecule: name an enzyme that cuts it."
                )
        elif method in ("restriction_ligation", "golden_gate"):
            fragments = cloning.digest(name, bases, circular, enzymes)
            if not fragments:
                raise Stop(
                    f"{part['hrid']} isn't cut by {', '.join(enzymes)}, and it's circular, so "
                    "nothing of it can go into a ligation."
                )
        elif enzymes:
            fragments = cloning.digest(name, bases, circular, enzymes)
            if not fragments:
                raise Stop(
                    f"{part['hrid']} is circular and isn't cut by {', '.join(enzymes)}: a Gibson "
                    "assembly joins linear fragments."
                )
        elif circular:
            raise Stop(
                f"{part['hrid']} is circular, and the plan names no enzyme to linearise it: a "
                "Gibson assembly joins linear fragments, so name the enzyme that cuts it."
            )
        else:
            fragments = [cloning.Fragment(name, bases, cloning.End(0, ""), cloning.End(0, ""))]
        fragments = [f for f in fragments if len(f.bases) >= (least if method == "gibson" else 1)]
        # The backbone goes into the reaction as the band cut out of the gel: its longest
        # fragment, which is what a vector digest is purified down to. A plan whose
        # backbone contributes another of its fragments asks for `any` instead.
        if part is backbone and backbone_fragment == "longest" and len(fragments) > 1:
            kept = max(fragments, key=lambda f: len(f.bases))
            print(
                f"{part['hrid']}: cut into {len(fragments)} fragments; its longest, "
                f"{len(kept.bases)} bp, goes into the reaction"
            )
            fragments = [kept]
        out.append(fragments)
        print(f"{part['hrid']}: {len(bases)} bp, {len(fragments)} fragment(s) into the reaction")
    return out


def _candidates(assembly: dict[str, Any], params: dict[str, Any]) -> list[cloning.Assembled]:
    """Every molecule the plan makes, or why it makes none. More than one, and the plan
    alone doesn't say which: `check` settles that against the product, `assemble` can't.

    A plan makes a circle where its parts close one, and that is what it makes: a ligase
    or an assembly mix leaves the closed molecule to transform. Where they can't close,
    what the reaction leaves is linear -- a Golden Gate or Gibson construct whose outer
    ends don't pair, a cut vector with no insert -- and that is what the plan makes. A
    digest makes a linear molecule by definition: the band kept from the gel. Whichever
    it is, the topology follows from the method and the parts, and nobody states it
    (ADR 0168).
    """
    method = values_of(assembly)["method"]
    enzymes = list(values_of(assembly).get("enzymes") or [])
    least = int(params.get("min_overlap", cloning.LEAST_OVERLAP))
    if method not in SIMULATED:
        raise Stop(
            f"A {method.replace('_', ' ')} assembly is recorded, not worked out: Iki simulates "
            "restriction digests, restriction-ligation, Golden Gate and Gibson assemblies."
        )
    choices = _fragments(
        assembly, method, enzymes, least, str(params.get("backbone_fragment", "longest"))
    )
    if method == "digest":
        # The digest's own fragments are what it makes, each a linear molecule.
        return [
            cloning.Assembled(bases=fragment.bases, junctions=[], topology="linear")
            for fragment in choices[0]
        ]
    if method == "gibson":
        made = cloning.gibson(choices, least=least)
        in_a_line = cloning.gibson_in_a_line(choices, least=least)
        how = f"sharing at least {least} bases at their ends"
    else:
        made = cloning.ligate(choices)
        in_a_line = cloning.ligate_in_a_line(choices)
        how = f"ligated after cutting with {', '.join(enzymes)}"
    if not made and not in_a_line:
        raise Stop(
            f"The plan's parts make nothing {how}: no arrangement of one fragment from each "
            "part joins all the way round, nor end to end into a linear molecule."
        )
    return made or in_a_line


def _made(assembly: dict[str, Any], params: dict[str, Any]) -> cloning.Assembled:
    """The one molecule the plan makes, or why there isn't one."""
    made = _candidates(assembly, params)
    if len(made) > 1:
        lengths = _lengths(made)
        what = "circles" if made[0].circular else "linear molecules"
        how = (
            "see each band with Digest a plan's parts virtually, keep the longest, or give "
            "the product and check the plan against it."
            if values_of(assembly)["method"] == "digest"
            else "cut with enzymes that leave different overhangs, or give the product and "
            "check the plan against it."
        )
        raise Stop(
            f"The plan's parts make {len(made)} different {what} ({lengths}), so it doesn't say "
            f"which: {how}"
        )
    return made[0]


def _lengths(made: list[cloning.Assembled]) -> str:
    """The molecules' lengths, largest first, as a gel reads them."""
    return ", ".join(f"{n} bp" for n in sorted((len(one.bases) for one in made), reverse=True))


def assemble(run: dict[str, Any]) -> dict[str, Any]:
    assembly = run["inputs"]["assembly"][0]
    if values_of(assembly).get("product"):
        raise Stop(
            f"{assembly['hrid']} has a product already: check the plan against it (Check an "
            "assembly) rather than making another."
        )
    made = _made(assembly, run["params"])
    name = values_of(assembly).get("name") or assembly["hrid"]
    method = values_of(assembly)["method"]
    how = "digest" if method == "digest" else f"{method.replace('_', ' ')} assembly"
    what = "circle" if made.circular else "linear molecule"
    print(f"{assembly['hrid']}: the plan makes a {len(made.bases)} bp {what} by {how}")
    for junction in made.junctions:
        print(f"  junction at {junction['at']}: {junction['from']} to {junction['to']}")
    creates: list[dict[str, Any]] = [
        {
            "type": "iki/dna.DNASequence",
            "key": "made",
            "derived_from": [assembly["id"]],
            "values": {
                "bases": made.bases,
                "topology": made.topology,
                "strandedness": "double",
                "name": name,
                "description": f"Made by {assembly['hrid']}, a {how}.",
            },
        }
    ]
    # A plasmid record is registered for a circle the plan closes, which is a vector. A
    # linear molecule a plan makes is a cut vector or an assembled fragment, on its way
    # to something else, so it is registered as the DNA sequence it is: a linear plasmid
    # -- pGKL1, a Borrelia plasmid -- is registered as a plasmid by whoever knows it is
    # one (Register a plasmid, topology linear).
    if made.circular:
        creates.append(
            {
                "type": "iki/plasmid.Plasmid",
                "derived_from": [assembly["id"]],
                "values": {
                    "sequence": {"$create": "made"},
                    "name": name,
                    "topology": made.topology,
                },
            }
        )
    else:
        print(
            f"  it has two ends, so it is registered as a DNA sequence: a {how}'s linear "
            "product is a fragment or a cut vector, not a vector of its own"
        )
    return {
        "creates": creates,
        # What the plan makes is a prediction, so the plan keeps the molecule's length,
        # topology and junctions, and what it makes is its in the graph
        # (``derived_from``). Its ``product`` is the sequence the clone turned out to
        # have, which whoever sequenced it states; ``check_assembly`` then compares the
        # two.
        "updates": {
            "assembly": [
                {
                    "entity": assembly["id"],
                    "values": {
                        "simulated_length": len(made.bases),
                        "simulated_topology": made.topology,
                        "junctions": made.junctions,
                    },
                }
            ]
        },
    }


#: Cleared when a check can't say whether the plan makes its product: a result that no
#: longer stands mustn't be what a review reads (nor gate a transition to Checked).
NO_RESULT: dict[str, Any] = {
    "matches_product": None,
    "simulated_length": None,
    "simulated_topology": None,
    "junctions": None,
}


def check(run: dict[str, Any]) -> dict[str, Any]:
    assemblies = run["inputs"]["assembly"]
    updates, messages = [], []
    for done, assembly in enumerate(assemblies, start=1):
        try:
            product = one_included(assembly, "product", "product")
            candidates = _candidates(assembly, run["params"])
        except Stop as why:
            # No result, so the old one goes: whatever a run found of an earlier plan
            # says nothing of this one, and Checked must not be reached on it.
            messages.append({"level": "error", "text": str(why), "entity": assembly["id"]})
            updates.append({"entity": assembly["id"], "values": dict(NO_RESULT)})
            continue
        values = values_of(product)
        stated = values["bases"].upper()
        # A molecule is its bases, its topology and how many strands it has (ADR 0138).
        # What the plan makes has a topology of its own, from its method and its parts: a
        # product of another topology, or a single-stranded one, is another molecule
        # however its bases read, and a linear product is read on either strand but never
        # from another origin, since it has two ends of its own.
        topology = values.get("topology") or "linear"
        strands = values.get("strandedness") or "double"
        makes = candidates[0].topology
        circles = candidates[0].circular
        what = "circles" if circles else "linear molecules"
        same_kind = topology == makes and strands == "double"
        found = [
            one
            for one in candidates
            if same_kind and cloning.same_molecule(one.bases, stated, circular=circles)
        ]
        lengths = _lengths(candidates)
        if found:
            # The product says which molecule the plan made, even where the plan alone
            # didn't: the one it is.
            made = found[0]
            values_out: dict[str, Any] = {
                "simulated_length": len(made.bases),
                "simulated_topology": made.topology,
                "matches_product": True,
                "junctions": made.junctions,
            }
            read_as = (
                "the same molecule from any origin and on either strand"
                if circles
                else "the same linear molecule, read from either strand"
            )
            if len(candidates) == 1:
                print(
                    f"{assembly['hrid']}: the plan makes {product['hrid']}, {len(made.bases)} bp, "
                    + read_as
                )
            else:
                print(
                    f"{assembly['hrid']}: the plan makes {len(candidates)} different {what} "
                    f"({lengths}), and {product['hrid']}, {len(made.bases)} bp, is one of them: "
                    "the plan explains the clone, and doesn't predict it on its own"
                )
                messages.append(
                    {
                        "level": "warning",
                        "text": (
                            f"The plan makes {len(candidates)} different {what}, and "
                            f"{product['hrid']} is one of them, so the plan explains the clone. "
                            f"The plan on its own doesn't say which of the {what} it makes: cut "
                            "with enzymes that leave different overhangs to settle that."
                        ),
                        "entity": assembly["id"],
                    }
                )
        else:
            # It isn't what the plan makes. Where the plan makes several molecules, which
            # of them it meant is still unsettled, so no length or junctions are claimed.
            one_molecule = len(candidates) == 1
            values_out = {
                "matches_product": False,
                "simulated_length": len(candidates[0].bases) if one_molecule else None,
                "simulated_topology": makes if one_molecule else None,
                "junctions": candidates[0].junctions if one_molecule else None,
            }
            if not same_kind:
                was = f"{topology} and {strands}-stranded"
                leaves = (
                    "closes a circular double-stranded molecule"
                    if circles
                    else "leaves a linear double-stranded molecule"
                )
                print(
                    f"{assembly['hrid']}: the plan {leaves}, and {product['hrid']} is {was}, so "
                    "it isn't what the plan makes however its bases read"
                )
            elif one_molecule:
                made = candidates[0]
                print(
                    f"{assembly['hrid']}: the plan makes a {len(made.bases)} bp {makes} molecule, "
                    f"and {product['hrid']} has {len(stated)} bp"
                    + (
                        ": the same length, so they differ in their bases"
                        if len(made.bases) == len(stated)
                        else f", {abs(len(made.bases) - len(stated))} bp apart"
                    )
                )
            else:
                print(
                    f"{assembly['hrid']}: the plan makes {len(candidates)} different {what} "
                    f"({lengths}), and {product['hrid']}, {len(stated)} bp, is none of them"
                )
        updates.append({"entity": assembly["id"], "values": values_out})
        progress(done, len(assemblies))
    return {"updates": {"assembly": updates}, "messages": messages}


# --- the virtual digest --------------------------------------------------------------

#: The most fragments a plan keeps, all its parts' together.
MOST_BANDS = 500


def digest(run: dict[str, Any]) -> dict[str, Any]:
    """Each part of a plan cut with its enzymes, every fragment listed largest first, as
    SnapGene's and Benchling's virtual digests list them: a result on the plan, however
    many bands there are, where `assemble` makes one molecule or none."""
    assembly = run["inputs"]["assembly"][0]
    enzymes = list(values_of(assembly).get("enzymes") or [])
    if not enzymes:
        raise Stop(f"{assembly['hrid']} names no enzymes, so there's nothing to digest it with.")
    backbone = one_included(assembly, "backbone", "backbone")
    method = values_of(assembly)["method"]
    parts = [backbone] if method == "digest" else [backbone, *included(assembly, "inserts")]
    rows: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    cut_with = ", ".join(enzymes)
    for done, part in enumerate(parts, start=1):
        bases = values_of(part)["bases"]
        circular = values_of(part).get("topology") == "circular"
        name = label_of(part)
        found = cloning.bands(bases, circular, enzymes)
        if len(rows) + len(found) > MOST_BANDS:
            raise Stop(
                f"{name} cuts into {len(found)} fragments, more than the {MOST_BANDS} a plan "
                "keeps: digest it with fewer enzymes, or ones that cut less often."
            )
        if not found:
            text = f"{name} isn't cut by {cut_with}: it stays a circle, which is no fragment."
            print(text)
            messages.append({"level": "warning", "text": text, "entity": assembly["id"]})
        else:
            sizes = ", ".join(str(b.length) for b in found)
            print(f"{name}: {len(found)} fragments with {cut_with}: {sizes} bp")
        rows += [
            {
                "part": name,
                "length": b.length,
                "start": b.start,
                "end": b.end,
                "left": b.left,
                "right": b.right,
            }
            for b in found
        ]
        progress(done, len(parts))
    return {
        "updates": {"assembly": [{"entity": assembly["id"], "values": {"fragments": rows}}]},
        "messages": messages,
    }


# --- the command ---------------------------------------------------------------------

COMMANDS = {
    "register": register,
    "annotate": annotate,
    "orfs": find_orfs,
    "primer_sites": find_primer_sites,
    "pcr": simulate_pcr,
    "assemble": assemble,
    "check": check,
    "digest": digest,
}


def main() -> None:
    command = COMMANDS[sys.argv[1]]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        output = command(run)
    except (Stop, cloning.Impossible, orf_finder.Refused, priming.Refused) as why:
        print(f"::error {why}")
        raise SystemExit(1) from why
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
