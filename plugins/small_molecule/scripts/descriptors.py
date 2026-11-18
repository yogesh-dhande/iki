"""The small_molecule plugin's calculations: `compounds` for compounds' descriptors,
`batches` for batches' formula weights.

Descriptors are RDKit's, from each compound's SMILES (see `chemistry.descriptors`), with
its standard InChI. A compound whose InChIKey isn't the one its SMILES gives was made
some other way than by the plugin's actions (by hand, through the API, with a typo), so
its run fails, naming both keys, and the rest of a batch go on.

A batch's formula weight is its compound's molecular weight, from the compound's SMILES,
with its salt and solvent (`chemistry.formula_weight`).
"""

from __future__ import annotations

import sys
from typing import Any

from chemistry import (
    Messages,
    UnreadableError,
    descriptors,
    formula_weight,
    from_smiles,
    identify,
    run_input,
    write_output,
)


def compounds(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["compound"]
    updates = []
    messages = Messages()
    for done, entity in enumerate(entities, start=1):
        values = entity["values"]
        try:
            mol = from_smiles(values["smiles"])
            inchi, key, _ = identify(mol)
        except UnreadableError as why:
            messages.add("error", f"{entity['hrid']}: {why}.", entity["id"])
            continue
        if key != values["inchikey"]:
            messages.add(
                "error",
                f"{entity['hrid']}'s InChIKey is {values['inchikey']}, but its SMILES gives "
                f"{key}: it isn't the compound its structure is. Register its structure "
                "with register_compounds instead.",
                entity["id"],
            )
            continue
        computed = {"inchi": inchi, **descriptors(mol)}
        updates.append({"entity": entity["id"], "values": computed})
        print(
            f"{entity['hrid']}: {computed['molecular_formula']}, "
            f"{computed['molecular_weight']} g/mol, logP {computed['logp']}, "
            f"TPSA {computed['tpsa']} Å²"
        )
        print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    return {"updates": {"compound": updates}, "messages": messages.output()}


def batches(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["batch"]
    updates = []
    messages = Messages()
    for entity in entities:
        values = entity["values"]
        found = (entity.get("included") or {}).get("compound") or []
        if not found:
            messages.add("error", f"{entity['hrid']} has no compound.", entity["id"])
            continue
        try:
            mol = from_smiles(found[0]["values"]["smiles"])
        except UnreadableError as why:
            messages.add("error", f"{found[0]['hrid']}: {why}.", entity["id"])
            continue
        weight = formula_weight(
            mol,
            values.get("salt"),
            values.get("salt_equivalents"),
            values.get("solvate"),
            values.get("solvate_equivalents"),
        )
        updates.append({"entity": entity["id"], "values": {"formula_weight": round(weight, 3)}})
        print(f"{entity['hrid']} of {found[0]['hrid']}: {weight:.3f} g/mol")
    return {"updates": {"batch": updates}, "messages": messages.output()}


def main() -> None:
    compute = {"compounds": compounds, "batches": batches}[sys.argv[1]]
    write_output(compute(run_input()))


if __name__ == "__main__":
    main()
