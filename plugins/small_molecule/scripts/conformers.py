"""The small_molecule plugin's conformers: generates a compound's three-dimensional
conformers with RDKit and minimises them, into a conformer set.

From the compound's SMILES, with its hydrogens added, RDKit embeds `count` conformers by
distance geometry with ETKDG version 3 (Wang et al., 2020: torsion preferences from
experimental structures, and knowledge of small rings and macrocycles), seeded from the
run's seed (IKI_SEED), so a run reproduced exactly makes the same ones. Conformers closer
than `prune_rms` Å (heavy atoms' RMSD, after aligning) to one already kept are dropped. Each
is then minimised in the MMFF94 force field (Halgren, 1996) for up to `iterations` steps;
with 0, they're left as embedded, ranked by their MMFF94 energies. A compound MMFF94 has
no parameters for isn't minimised, and its conformers have no energies and are in the
order they were embedded; the run says so, and so does the set's method. They're written
lowest energy first, to an SDF file and as the models of a PDB file for the structure
viewer.

It runs on one thread, so a run's results don't depend on how many CPUs its machine has.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import iki_action as iki
from chemistry import Messages, UnreadableError, from_smiles
from rdkit import Chem
from rdkit.Chem import rdDistGeom, rdForceFieldHelpers

SDF = "conformers.sdf"
PDB = "conformers.pdb"
MAX_ATOMS = 500


def generate(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    count = int(params.get("count", 10))
    prune = float(params.get("prune_rms", 0.5))
    iterations = int(params.get("iterations", 2000))
    seed = int(os.environ.get("IKI_SEED") or run.get("run", {}).get("seed") or 0)
    (entity,) = run["inputs"]["compound"]
    label = entity["values"].get("name") or entity["hrid"]
    mol = from_smiles(entity["values"]["smiles"])
    if len(Chem.GetMolFrags(mol)) > 1:
        raise UnreadableError(
            f"{entity['hrid']} has more than one part (a salt, say): register its parent, "
            "and generate its conformers"
        )
    mol = Chem.AddHs(mol)
    if mol.GetNumAtoms() > MAX_ATOMS:
        raise UnreadableError(
            f"{entity['hrid']} has {mol.GetNumAtoms():,} atoms with its hydrogens, more than "
            f"the {MAX_ATOMS} conformers are generated for"
        )
    embedding = rdDistGeom.ETKDGv3()
    embedding.randomSeed = seed
    embedding.pruneRmsThresh = prune
    embedding.numThreads = 1
    print(f"::progress 0.1 Embedding {count} conformers of {entity['hrid']}", flush=True)
    ids = list(rdDistGeom.EmbedMultipleConfs(mol, numConfs=count, params=embedding))
    if not ids:
        raise UnreadableError(f"RDKit couldn't embed {entity['hrid']} in three dimensions")
    messages = Messages()
    embedded = f"RDKit ETKDGv3, {count} embedded, pruned at {prune:g} Å heavy-atom RMSD"
    if rdForceFieldHelpers.MMFFHasAllMoleculeParams(mol) and iterations == 0:
        # Left as embedded, as the parameter says: their energies only rank them.
        results = rdForceFieldHelpers.MMFFOptimizeMoleculeConfs(
            mol, numThreads=1, maxIters=0, mmffVariant="MMFF94"
        )
        energies = [energy for _, energy in results]
        method = f"{embedded}; not minimised, ranked by their MMFF94 energies as embedded"
    elif rdForceFieldHelpers.MMFFHasAllMoleculeParams(mol):
        print(f"::progress 0.5 Minimising {len(ids)} in MMFF94", flush=True)
        results = rdForceFieldHelpers.MMFFOptimizeMoleculeConfs(
            mol, numThreads=1, maxIters=iterations, mmffVariant="MMFF94"
        )
        energies = [energy for _, energy in results]
        unfinished = sum(1 for done, _ in results if done != 0)
        if unfinished:
            messages.add(
                "warning",
                f"{unfinished} of {len(ids)} conformers hadn't converged after {iterations:,} "
                "steps of minimising.",
            )
        method = f"{embedded}; minimised in MMFF94, up to {iterations:,} steps"
    else:
        energies = []
        messages.add(
            "warning",
            f"MMFF94 has no parameters for some of {entity['hrid']}'s atoms, so its "
            "conformers weren't minimised.",
        )
        method = f"{embedded}; not minimised (MMFF94 has no parameters for it), in no order"
    order = sorted(range(len(ids)), key=lambda i: energies[i] if energies else i)
    out = Path(os.environ["IKI_OUT_DIR"])
    lowest = energies[order[0]] if energies else 0.0
    with Chem.SDWriter(str(out / SDF)) as writer:
        for rank, i in enumerate(order, start=1):
            mol.SetProp("_Name", f"{label} conformer {rank}")
            mol.SetProp("hrid", entity["hrid"])
            if energies:
                mol.SetProp("mmff94_energy", f"{energies[i]:.4f}")
                mol.SetProp("relative_energy", f"{energies[i] - lowest:.4f}")
            writer.write(mol, confId=ids[i])
    models = []
    for rank, i in enumerate(order, start=1):
        # Without CONECT records (flavor 2): the viewer bonds atoms as they lie.
        block = Chem.MolToPDBBlock(mol, confId=ids[i], flavor=2)
        atoms = "".join(f"{line}\n" for line in block.splitlines() if line != "END")
        models.append(f"MODEL     {rank:4d}\n{atoms}ENDMDL\n")
    (out / PDB).write_text("".join(models) + "END\n", encoding="utf-8")
    ranked = [round(energies[i], 4) for i in order] if energies else []
    values: dict[str, Any] = {
        "compound": entity["id"],
        "conformers": {"$file": SDF, "media_type": "chemical/x-mdl-sdfile"},
        "structure": {"$file": PDB, "media_type": "chemical/x-pdb"},
        "method": method,
        "count": len(ids),
    }
    if ranked:
        values["energies"] = ranked
        values["energy_window"] = round(ranked[-1] - ranked[0], 4)
    print(
        f"{entity['hrid']}: {len(ids)} conformers"
        + (f", {ranked[0]:.2f} kcal/mol lowest" if ranked else "")
    )
    print("::progress 1.0 Written", flush=True)
    return {
        "creates": [{"type": "iki/small_molecule.ConformerSet", "values": values}],
        "metrics": {"conformers": len(ids)},
        "messages": messages.output(),
    }


def conformers(run: iki.Run) -> dict[str, Any]:
    try:
        return generate(run)
    except UnreadableError as why:
        raise iki.Refused(f"{why}.") from None


if __name__ == "__main__":
    iki.main(conformers)
