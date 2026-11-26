"""The small_molecule plugin's calculations: `compounds` for compounds' descriptors,
`batches` for batches' formula weights.

Descriptors are RDKit's, from each compound's SMILES (see `chemistry.descriptors`), with
its standard InChI. A compound whose InChIKey isn't the one its SMILES gives was made
some other way than by the plugin's actions (by hand, through the API, with a typo), so
its run fails, naming both keys, and the rest of a batch go on.

Each compound's Molfile is its SMILES laid out in 2D (`chemistry.molfile`), which Iki
writes its Molfile and SDF exports from (ADR 0229). It's kept only if it reads back to the
compound's InChIKey. One whose layout can't be made in time, or would take the run's
output over what it holds, is left without, saying so; its descriptors are written.

A batch's formula weight is its compound's molecular weight, from the compound's SMILES,
with its salt and solvent (`chemistry.formula_weight`). A batch whose structure as drawn
has parts that are neither its compound nor a salt or solvent Iki knows has none: what
they add to the material isn't known (`chemistry.weight_withheld`). Nor has one whose
salt's equivalents are blank when charge balance doesn't say how many
(`chemistry.unstated_equivalents`), such as a calcium salt of a compound with two acids.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from chemistry import (
    LAYOUT_SECONDS,
    MAX_OUTPUT_BYTES,
    Layouts,
    Messages,
    UnreadableError,
    descriptors,
    formula_weight,
    from_smiles,
    identify,
    read_back,
    run_input,
    standardise,
    unstated_equivalents,
    write_output,
)

#: How long a run may spend laying out Molfiles, of the action's 10 minutes.
LAYING_OUT_SECONDS = 6 * 60.0
#: What the updates may take of output.json, leaving room for its messages.
UPDATES_BYTES = MAX_OUTPUT_BYTES - 300_000


def compounds(run: dict[str, Any]) -> dict[str, Any]:
    entities = run["inputs"]["compound"]
    updates = []
    messages = Messages()
    until = time.monotonic() + LAYING_OUT_SECONDS
    size = 0
    with Layouts() as layouts:
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
            size += len(json.dumps({"entity": entity["id"], "values": computed})) + 2
            block = layouts.molfile(values["smiles"], until)
            why_not = _no_molfile(block, key, size, until)
            if why_not:
                messages.add(
                    "warning",
                    f"{entity['hrid']} has no Molfile, so it doesn't export as a Molfile or "
                    f"an SDF yet: {why_not}",
                    entity["id"],
                )
            else:
                computed["molfile"] = block
                size += len(json.dumps(block)) + len(', "molfile": ')
            updates.append({"entity": entity["id"], "values": computed})
            print(
                f"{entity['hrid']}: {computed['molecular_formula']}, "
                f"{computed['molecular_weight']} g/mol, logP {computed['logp']}, "
                f"TPSA {computed['tpsa']} Å²"
            )
            print(f"::progress {done / len(entities):.2f} {done} of {len(entities)}", flush=True)
    return {"updates": {"compound": updates}, "messages": messages.output()}


def _no_molfile(block: str | None, key: str, size: int, until: float) -> str | None:
    """Why a compound's Molfile isn't kept, if it isn't."""
    if block is None:
        if time.monotonic() >= until:
            return (
                f"the run had laid out structures for its {LAYING_OUT_SECONDS / 60:.0f} "
                "minutes. Run compute_descriptors on it again."
            )
        return (
            f"neither CoordGen nor RDKit's own depictor could lay its structure out in "
            f"{LAYOUT_SECONDS:.0f} seconds, in the memory a run has."
        )
    found = read_back(block)
    if found != key:
        return (
            f"its structure laid out, read back, is {found or 'no structure InChI can read'}, "
            f"not {key}."
        )
    if size + len(json.dumps(block)) > UPDATES_BYTES:
        return (
            f"its Molfile, of {len(block):,} characters, would take the run's output over "
            "the 2.5 MB it holds. Run compute_descriptors on it with fewer compounds."
        )
    return None


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
