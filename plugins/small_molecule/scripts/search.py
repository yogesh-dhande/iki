"""The small_molecule plugin's searches over the compounds a run is given: `similar` finds
those like a query compound, `substructure` those with a substructure, each into a
collection of what it found.

Similarity is the Tanimoto coefficient of Morgan fingerprints (radius 2, the circular
environment ECFP4 describes, folded into 2,048 bits), as RDKit computes them from each
compound's SMILES; the collection lists the compounds at or above the threshold, most
similar first, and the run keeps a table of every one's similarity. A substructure is a
SMARTS pattern (a SMILES is one too), matched as RDKit matches it, with stereochemistry
if the run asks; the collection lists the compounds it matches, in the order given.

A collection holds at most 10,000: past that, the most similar (or the first) are kept,
and the run says so.
"""

from __future__ import annotations

import csv
import os
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamed
from chemistry import (
    Messages,
    UnreadableError,
    cut,
    fail,
    from_smiles,
    rdkit_messages,
    why_not,
    write_output,
)
from rdkit import Chem, DataStructs
from rdkit.Chem import rdFingerprintGenerator

RADIUS = 2
BITS = 2048
MAX_MEMBERS = 10_000
TABLE = "similarity.csv"
MAX_DESCRIPTION = 10_000
#: The description names this many, most similar first.
NAMED = 25


@dataclass(frozen=True)
class Compound:
    """What a search keeps of a compound: never its structure, which is read, searched and
    let go, nor its other values."""

    id: str
    hrid: str
    name: str
    smiles: str


def _compounds(
    records: Iterable[dict[str, Any]], messages: Messages
) -> Iterator[tuple[Compound, Chem.Mol]]:
    """Each compound, read from its SMILES, one at a time."""
    for entity in records:
        values = entity["values"]
        try:
            mol = from_smiles(values["smiles"])
        except UnreadableError as why:
            messages.add("warning", f"{entity['hrid']} was left out: {why}.", entity["id"])
            continue
        yield Compound(entity["id"], entity["hrid"], values.get("name", ""), values["smiles"]), mol


def _run() -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    return streamed.run_input(Path(os.environ["IKI_INPUT"]), "compounds")


def _collection(name: str, description: str, members: list[str]) -> dict[str, Any]:
    return {
        "type": "iki/core.Collection",
        "values": {
            "name": cut(name, 200),
            "description": description[:MAX_DESCRIPTION],
            "members": members,
        },
    }


def similar(run: dict[str, Any], records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    params = run.get("params") or {}
    threshold = float(params.get("threshold", 0.7))
    messages = Messages()
    given = (run["inputs"].get("query") or [None])[0]
    smiles = (params.get("smiles") or "").strip()
    if (given is None) == (not smiles):
        raise UnreadableError("Give a query compound, or a query's SMILES, but not both")
    if given is not None:
        query = from_smiles(given["values"]["smiles"])
        called = given["values"].get("name") or given["hrid"]
    else:
        query = from_smiles(smiles)
        called = smiles
    generator = rdFingerprintGenerator.GetMorganGenerator(radius=RADIUS, fpSize=BITS)
    wanted = generator.GetFingerprint(query)
    scored = []
    for compound, mol in _compounds(records, messages):
        similarity = DataStructs.TanimotoSimilarity(wanted, generator.GetFingerprint(mol))
        # Compared and ranked whole: rounded first, 13/29 (0.44828) would pass 0.4483.
        scored.append((similarity, compound))
    scored.sort(key=lambda s: -s[0])
    hits = [(s, c) for s, c in scored if s >= threshold]
    if len(hits) > MAX_MEMBERS:
        messages.add(
            "warning",
            f"{len(hits):,} are similar enough; the collection holds the {MAX_MEMBERS:,} most "
            "similar.",
        )
        hits = hits[:MAX_MEMBERS]
    with (Path(os.environ["IKI_OUT_DIR"]) / TABLE).open("w", newline="", encoding="utf-8") as out:
        table = csv.writer(out)
        table.writerow(["hrid", "name", "smiles", "tanimoto"])
        for similarity, compound in scored:
            table.writerow([compound.hrid, compound.name, compound.smiles, similarity])
    named = ", ".join(f"{c.hrid} {s:.2f}" for s, c in hits[:NAMED])
    more = f", and {len(hits) - NAMED:,} more" if len(hits) > NAMED else ""
    description = (
        f"{len(hits):,} of {len(scored):,} compounds with a Tanimoto similarity of at least "
        f"{threshold:g} to {called}, by Morgan fingerprints (radius {RADIUS}, {BITS:,} bits), "
        "most similar first" + (f": {named}{more}." if hits else ".")
    )
    name = params.get("name") or f"Like {called} (Tanimoto ≥ {threshold:g})"
    for similarity, compound in hits[:10]:
        print(f"{compound.hrid}: {similarity:.4f}")
    return {
        "creates": [_collection(name, description, [c.id for _, c in hits])],
        "metrics": {
            "hits": len(hits),
            "searched": len(scored),
            "best": round(scored[0][0], 4) if scored else 0.0,
        },
        "messages": messages.output(),
    }


def substructure(run: dict[str, Any], records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    params = run.get("params") or {}
    smarts = (params.get("smarts") or "").strip()
    chirality = bool(params.get("chirality", False))
    with rdkit_messages() as said:
        pattern = Chem.MolFromSmarts(smarts) if smarts else None
    if pattern is None:
        raise UnreadableError(f"{smarts!r} isn't SMARTS RDKit can read: {why_not(said, 'syntax')}")
    messages = Messages()
    searched = 0
    hits = []
    for compound, mol in _compounds(records, messages):
        searched += 1
        if mol.HasSubstructMatch(pattern, useChirality=chirality):
            hits.append(compound)
    if len(hits) > MAX_MEMBERS:
        messages.add(
            "warning",
            f"{len(hits):,} have it; the collection holds the first {MAX_MEMBERS:,}.",
        )
        hits = hits[:MAX_MEMBERS]
    description = (
        f"{len(hits):,} of {searched:,} compounds with the substructure {smarts}"
        + (", stereochemistry included" if chirality else "")
        + ", as RDKit matches SMARTS."
    )
    name = params.get("name") or f"With {smarts}"
    for compound in hits[:10]:
        print(f"{compound.hrid}: has it")
    return {
        "creates": [_collection(name, description, [c.id for c in hits])],
        "metrics": {"hits": len(hits), "searched": searched},
        "messages": messages.output(),
    }


def main() -> None:
    search = {"similar": similar, "substructure": substructure}[sys.argv[1]]
    try:
        output = search(*_run())
    except UnreadableError as why:
        fail(f"{why}.")
        return
    write_output(output)


if __name__ == "__main__":
    main()
