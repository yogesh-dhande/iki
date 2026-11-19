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
        raise SystemExit(f"FoldCloud answered {error.code}: {said}") from None


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


def chain_ids(pdb: str) -> list[str]:
    """The chains of a PDB file's first model, in the order its atoms are: each ATOM or
    HETATM record's chain ID (column 22), stopping at its first ENDMDL."""
    found: dict[str, None] = {}
    for line in pdb.splitlines():
        if line.startswith("ENDMDL"):
            break
        if line.startswith(("ATOM  ", "HETATM")) and len(line) > 21 and line[21].strip():
            found[line[21]] = None
    return list(found)


def finish(run: dict[str, Any]) -> dict[str, Any]:
    resume = run["resume"]
    answer = resume["answer"] or call("GET", f"/v1/predictions/{resume['job']}")
    pdb = answer["output"]["pdb"]
    Path(os.environ["IKI_OUT_DIR"], "predicted.pdb").write_text(pdb)
    model = answer.get("model", run["params"]["model"])
    protein = resume["state"]["protein"]
    # One protein in, so each chain FoldCloud returns is it: one, or a multimer's copies.
    chains = chain_ids(pdb)
    messages = [
        {
            "level": "info",
            "text": f"Predicted by {model}, as FoldCloud's job {resume['job']}. The version "
            "FoldCloud reported is in the run's record of its calls.",
        }
    ]
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


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text())
    output = finish(run) if run.get("resume") else submit(run)
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output})
    )


if __name__ == "__main__":
    main()
