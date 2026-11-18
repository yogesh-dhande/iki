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


def finish(run: dict[str, Any]) -> dict[str, Any]:
    resume = run["resume"]
    answer = resume["answer"] or call("GET", f"/v1/predictions/{resume['job']}")
    pdb = answer["output"]["pdb"]
    Path(os.environ["IKI_OUT_DIR"], "predicted.pdb").write_text(pdb)
    model = answer.get("model", run["params"]["model"])
    return {
        "creates": [
            {
                "type": "iki/protein.ProteinStructure",
                "values": {
                    "structure": {"$file": "predicted.pdb", "media_type": "chemical/x-pdb"},
                    "name": f"{resume['state']['hrid']}, predicted by {model}",
                    "method": "predicted",
                    "chains": [resume["state"]["protein"]],
                },
            }
        ],
        "messages": [
            {
                "level": "info",
                "text": f"Predicted by {model}, as FoldCloud's job {resume['job']}.",
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text())
    output = finish(run) if run.get("resume") else submit(run)
    Path(os.environ["IKI_OUTPUT"]).write_text(
        json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output})
    )


if __name__ == "__main__":
    main()
