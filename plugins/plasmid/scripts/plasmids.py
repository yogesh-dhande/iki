"""The plasmid plugin's commands: `register` a plasmid from its bases, `annotate` one from
the library of common vector parts, `assemble` what a cloning plan makes, and `check` a
plan against the sequence its clone turned out to have.

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

HERE = Path(__file__).resolve().parent
FEATURES = HERE.parent / "data" / "features.json"
#: The methods Iki works out. Gateway recombines at att sites and TOPO joins through a
#: topoisomerase: both are recorded, and neither is simulated here.
SIMULATED = {"restriction_ligation", "golden_gate", "gibson"}


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
    sequence: dict[str, Any] = {
        "bases": bases,
        "topology": "circular",
        "strandedness": "double",
    }
    plasmid: dict[str, Any] = {"sequence": {"$create": "sequence"}}
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
    print(f"{len(bases)} bp, circular and double-stranded: registering its sequence and plasmid")
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
        circular = values_of(sequence).get("topology") == "circular"
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
        # Its length is its registry entry's, the same for every project: a run fills it
        # where it's empty, and never writes over what the entry has (ADR 0115).
        if values_of(plasmid).get("length") is None:
            values["length"] = len(bases)
        updates.append({"entity": plasmid["id"], "values": values})
        found = ", ".join(f"{hit.part.name} {hit.start}..{hit.end}" for hit in hits) or "nothing"
        print(f"{plasmid['hrid']}: {len(bases)} bp, {len(hits)} parts found: {found}")
        progress(done, len(plasmids))
    return {"updates": {"plasmid": updates}, "messages": messages}


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
    parts = [backbone, *included(assembly, "inserts")]
    out: list[list[cloning.Fragment]] = []
    for part in parts:
        bases = values_of(part)["bases"].upper()
        circular = values_of(part).get("topology") == "circular"
        name = label_of(part)
        if method in ("restriction_ligation", "golden_gate"):
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


def _made(assembly: dict[str, Any], params: dict[str, Any]) -> cloning.Assembled:
    """The one circle the plan makes, or why there isn't one."""
    method = values_of(assembly)["method"]
    enzymes = list(values_of(assembly).get("enzymes") or [])
    least = int(params.get("min_overlap", cloning.LEAST_OVERLAP))
    if method not in SIMULATED:
        raise Stop(
            f"A {method.replace('_', ' ')} assembly is recorded, not worked out: Iki simulates "
            "restriction-ligation, Golden Gate and Gibson assemblies."
        )
    choices = _fragments(
        assembly, method, enzymes, least, str(params.get("backbone_fragment", "longest"))
    )
    if method == "gibson":
        made = cloning.gibson(choices, least=least)
        how = f"sharing at least {least} bases at their ends"
    else:
        made = cloning.ligate(choices)
        how = f"ligated after cutting with {', '.join(enzymes)}"
    if not made:
        raise Stop(
            f"The plan's parts make no circle {how}: no arrangement of one fragment from each "
            "part joins all the way round."
        )
    if len(made) > 1:
        lengths = ", ".join(f"{len(one.bases)} bp" for one in made)
        raise Stop(
            f"The plan's parts make {len(made)} different circles ({lengths}), so it doesn't say "
            "which: cut with enzymes that leave different overhangs, or give the product and "
            "check the plan against it."
        )
    return made[0]


def assemble(run: dict[str, Any]) -> dict[str, Any]:
    assembly = run["inputs"]["assembly"][0]
    if values_of(assembly).get("product"):
        raise Stop(
            f"{assembly['hrid']} has a product already: check the plan against it (Check an "
            "assembly) rather than making another."
        )
    made = _made(assembly, run["params"])
    name = values_of(assembly).get("name") or assembly["hrid"]
    how = values_of(assembly)["method"].replace("_", " ")
    print(f"{assembly['hrid']}: the plan makes a {len(made.bases)} bp circle by {how}")
    for junction in made.junctions:
        print(f"  junction at {junction['at']}: {junction['from']} to {junction['to']}")
    return {
        "creates": [
            {
                "type": "iki/dna.DNASequence",
                "key": "made",
                "derived_from": [assembly["id"]],
                "values": {
                    "bases": made.bases,
                    "topology": "circular",
                    "strandedness": "double",
                    "name": name,
                    "description": f"Made by {assembly['hrid']}, a {how} assembly.",
                },
            },
            {
                "type": "iki/plasmid.Plasmid",
                "derived_from": [assembly["id"]],
                "values": {"sequence": {"$create": "made"}, "name": name},
            },
        ],
        # What the plan makes is a prediction, so the plan keeps the circle's length and
        # junctions, and what it makes is its in the graph (``derived_from``). Its
        # ``product`` is the sequence the clone turned out to have, which whoever
        # sequenced it states; ``check_assembly`` then compares the two.
        "updates": {
            "assembly": [
                {
                    "entity": assembly["id"],
                    "values": {
                        "simulated_length": len(made.bases),
                        "junctions": made.junctions,
                    },
                }
            ]
        },
    }


def check(run: dict[str, Any]) -> dict[str, Any]:
    assemblies = run["inputs"]["assembly"]
    updates, messages = [], []
    for done, assembly in enumerate(assemblies, start=1):
        try:
            product = one_included(assembly, "product", "product")
            made = _made(assembly, run["params"])
        except Stop as why:
            messages.append({"level": "error", "text": str(why), "entity": assembly["id"]})
            continue
        stated = values_of(product)["bases"].upper()
        matches = cloning.same_molecule(made.bases, stated)
        updates.append(
            {
                "entity": assembly["id"],
                "values": {
                    "simulated_length": len(made.bases),
                    "matches_product": matches,
                    "junctions": made.junctions,
                },
            }
        )
        if matches:
            print(
                f"{assembly['hrid']}: the plan makes {product['hrid']}, {len(made.bases)} bp, "
                "the same molecule from any origin and on either strand"
            )
        else:
            print(
                f"{assembly['hrid']}: the plan makes a {len(made.bases)} bp circle, and "
                f"{product['hrid']} has {len(stated)} bp"
                + (
                    ": the same length, so they differ in their bases"
                    if len(made.bases) == len(stated)
                    else f", {abs(len(made.bases) - len(stated))} bp apart"
                )
            )
        progress(done, len(assemblies))
    return {"updates": {"assembly": updates}, "messages": messages}


# --- the command ---------------------------------------------------------------------

COMMANDS = {"register": register, "annotate": annotate, "assemble": assemble, "check": check}


def main() -> None:
    command = COMMANDS[sys.argv[1]]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        output = command(run)
    except (Stop, cloning.Impossible) as why:
        print(f"::error {why}")
        raise SystemExit(1) from why
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output}), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
