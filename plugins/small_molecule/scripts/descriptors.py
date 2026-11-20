"""The small_molecule plugin's calculations: `compounds` for compounds' descriptors,
`batches` for batches' formula weights.

Descriptors are RDKit's, from each compound's SMILES (see `chemistry.descriptors`), with
its standard InChI. A compound whose InChIKey isn't the one its SMILES gives was made
some other way than by the plugin's actions (by hand, through the API, with a typo), so
its run fails, naming both keys, and the rest of a batch go on.

A batch's formula weight is its compound's molecular weight, from the compound's SMILES,
with its salt and solvent (`chemistry.formula_weight`). A batch whose structure as drawn
has parts that are neither its compound nor a salt or solvent Iki knows has none: what
they add to the material isn't known (`chemistry.weight_withheld`). Nor has one whose
salt's equivalents are blank when charge balance doesn't say how many
(`chemistry.unstated_equivalents`), such as a calcium salt of a compound with two acids.
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
    standardise,
    unstated_equivalents,
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
        try:
            left_out = _left_out(values.get("structure_as_drawn"), found[0]["values"]["smiles"])
        except UnreadableError as why:
            messages.add("error", f"{entity['hrid']}'s structure as drawn: {why}.", entity["id"])
            continue
        if left_out:
            parts = ", ".join(left_out)
            one = len(left_out) == 1
            messages.add(
                "warning",
                f"{entity['hrid']} has no formula weight: its structure as drawn has {parts}, "
                f"which {'is' if one else 'are'} neither {found[0]['hrid']} nor a salt or "
                f"solvent Iki knows, so what {'it adds' if one else 'they add'} to the "
                "material isn't known.",
                entity["id"],
            )
            if values.get("formula_weight") is not None:
                updates.append({"entity": entity["id"], "values": {"formula_weight": None}})
            continue
        salt = values.get("salt")
        weight = formula_weight(
            mol,
            salt,
            values.get("salt_equivalents"),
            values.get("solvate"),
            values.get("solvate_equivalents"),
        )
        if salt and values.get("salt_equivalents") is None:
            balance = unstated_equivalents(mol, salt)
            said = f"salt, {salt}, has no equivalents. {balance.why}"
            if weight is None:
                messages.add(
                    "warning",
                    f"{entity['hrid']} has no formula weight: its {said}: give its salt "
                    f"equivalents, {balance.example}.",
                    entity["id"],
                )
            elif balance.why:
                messages.add("info", f"{entity['hrid']}'s {said}.", entity["id"])
        if weight is None:
            if values.get("formula_weight") is not None:
                updates.append({"entity": entity["id"], "values": {"formula_weight": None}})
            continue
        updates.append({"entity": entity["id"], "values": {"formula_weight": round(weight, 3)}})
        print(f"{entity['hrid']} of {found[0]['hrid']}: {weight:.3f} g/mol")
    return {"updates": {"batch": updates}, "messages": messages.output()}


def _left_out(drawn: str | None, compound: str) -> list[str]:
    """The parts of a batch's structure as drawn that are neither its compound, nor a salt
    or solvent Iki knows, as its registration left them out. A compound registered whole
    is the structure drawn, so nothing of it was."""
    if not drawn or drawn == compound:
        return []
    return standardise(from_smiles(drawn), parent=True).left_out


def main() -> None:
    compute = {"compounds": compounds, "batches": batches}[sys.argv[1]]
    write_output(compute(run_input()))


if __name__ == "__main__":
    main()
