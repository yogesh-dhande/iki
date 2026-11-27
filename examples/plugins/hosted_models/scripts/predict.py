"""Predicts a protein's structure with FoldCloud, a model behind an outside API (ADR 0130).

The first attempt submits the protein's sequence and parks: Iki polls the job, holding no
worker, and FoldCloud calls back when it's done. Iki then runs this again with the job's
final answer as ``resume`` in input.json, and it imports the predicted structure.

It calls FoldCloud at its real address, through the egress proxy Iki gives it, without a
key: the proxy adds the organization's, and records each call. Only Python's standard
library: urllib trusts the CA Iki gives the attempt through SSL_CERT_FILE.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import iki_action as iki

API = os.environ.get("IKI_API_FOLDCLOUD_URL", "")


def call(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - IKI_API_FOLDCLOUD_URL is https
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as answer:  # noqa: S310
            found: dict[str, Any] = json.load(answer)
            return found
    except urllib.error.HTTPError as error:
        said = error.read().decode("utf-8", "replace")[:500]
        raise iki.Refused(f"FoldCloud answered {error.code}: {said}") from None


def submit(run: dict[str, Any]) -> dict[str, Any]:
    protein = run["inputs"]["protein"][0]
    body: dict[str, Any] = {
        "sequence": protein["values"]["sequence"],
        "model": run["params"]["model"],
        "num_recycles": run["params"]["num_recycles"],
    }
    callback = os.environ.get("IKI_API_FOLDCLOUD_CALLBACK_URL")
    if callback:
        body["webhook"] = callback
    job = call("POST", "/v1/predictions", body)
    print(f"FoldCloud took the prediction as job {job['id']}.", flush=True)
    return {
        "park": {
            "api": "foldcloud",
            "job": job["id"],
            "state": {"protein": protein["id"], "hrid": protein["hrid"]},
        }
    }


#: The standard amino acids' residue names in PDB files, with selenocysteine, pyrrolysine
#: and an unknown one (wwPDB's Chemical Component Dictionary).
AMINO_ACIDS = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE", "LEU", "LYS",
    "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL", "SEC", "PYL", "UNK",
})  # fmt: skip
#: The atoms of an amino acid's backbone: a HETATM residue with them is a modified amino
#: acid in a protein chain, as selenomethionine (MSE) is.
BACKBONE = frozenset({"N", "CA", "C"})


def chain_ids(pdb: str) -> list[str]:
    """The protein chains of a PDB file's first model, in the order its atoms are: each
    ATOM or HETATM record's chain ID (column 22), stopping at its first ENDMDL, for the
    chains with an amino acid, as an ATOM record (a C-alpha trace's too), or two or more
    residues with an amino acid's backbone (modified ones, as HETATM). A chain of solvent
    or ligands, which a predictor may give its own ID, isn't the protein."""
    order: dict[str, None] = {}
    standard: set[str] = set()
    atoms: dict[str, dict[str, set[str]]] = {}
    for line in pdb.splitlines():
        if line.startswith("ENDMDL"):
            break
        if not (line.startswith(("ATOM  ", "HETATM")) and len(line) > 26 and line[21].strip()):
            continue
        chain = line[21]
        order[chain] = None
        if line.startswith("ATOM  ") and line[17:20].strip() in AMINO_ACIDS:
            standard.add(chain)
        residue = line[17:27]  # its name, chain, number and insertion code
        atoms.setdefault(chain, {}).setdefault(residue, set()).add(line[12:16].strip())
    return [
        chain
        for chain in order
        if chain in standard
        or sum(names >= BACKBONE for names in atoms.get(chain, {}).values()) >= 2
    ]


def other_chains(pdb: str, proteins: list[str]) -> list[str]:
    """The first model's chains that aren't the protein: solvent or ligands."""
    found: dict[str, None] = {}
    for line in pdb.splitlines():
        if line.startswith("ENDMDL"):
            break
        if line.startswith(("ATOM  ", "HETATM")) and len(line) > 21 and line[21].strip():
            found[line[21]] = None
    return [c for c in found if c not in proteins]


def finish(run: dict[str, Any]) -> dict[str, Any]:
    resume = run["resume"]
    answer = resume["answer"] or call("GET", f"/v1/predictions/{resume['job']}")
    pdb = answer["output"]["pdb"]
    Path(os.environ["IKI_OUT_DIR"], "predicted.pdb").write_text(pdb)
    model = answer.get("model", run["params"]["model"])
    protein = resume["state"]["protein"]
    # One protein in, so each protein chain FoldCloud returns is it: one, or a multimer's
    # copies. Its other chains, solvent or ligands, aren't.
    chains = chain_ids(pdb)
    messages = [
        {
            "level": "info",
            "text": f"Predicted by {model}, as FoldCloud's job {resume['job']}. The version "
            "FoldCloud reported is in the run's record of its calls.",
        }
    ]
    if others := other_chains(pdb, chains):
        listed = ", ".join(others)
        messages.append(
            {
                "level": "info",
                "text": f"FoldCloud's file also has chain{'s' if len(others) > 1 else ''} "
                f"{listed}, with no amino acids (solvent or ligands): "
                f"{'they are' if len(others) > 1 else 'it is'} not the protein, so "
                "the structure's chains leave it out.",
            }
        )
    if not chains:
        messages.append(
            {
                "level": "warning",
                "text": "FoldCloud's file names no chain, so the structure has no chain IDs: "
                "its secondary structure can't be assigned by chain.",
            }
        )
    return {
        "creates": [
            {
                "type": "iki/protein.ProteinStructure",
                "values": {
                    "structure": {"$file": "predicted.pdb", "media_type": "chemical/x-pdb"},
                    "name": f"{resume['state']['hrid']}, predicted by {model}",
                    "method": "predicted",
                    "chains": [protein] * len(chains) if chains else [protein],
                    **({"chain_ids": chains} if chains else {}),
                },
                # Where it's from, and how it was made (ADR 0106): by FoldCloud's model, as
                # its answer says.
                "origin": {
                    "source": "FoldCloud",
                    "id": str(resume["job"])[:255],
                    "made": {
                        "kind": "computational",
                        "method": "Predicted by FoldCloud",
                        "software": [{"name": f"FoldCloud {model}"[:100]}],
                    },
                },
            }
        ],
        "messages": messages,
    }


def predict(run: iki.Run) -> dict[str, Any]:
    return finish(run) if run.get("resume") else submit(run)


if __name__ == "__main__":
    iki.main(predict)
