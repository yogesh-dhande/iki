"""Predicts a structure with FoldCloud: submits the protein's sequence, parks while the job
runs, and on resume imports the structure the API answered with."""

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text())
base = os.environ["IKI_API_FOLD_URL"]
resume = run.get("resume")
if resume is None:
    protein = run["inputs"]["protein"][0]
    body = {
        "sequence": protein["values"]["sequence"],
        "recycles": run["params"]["recycles"],
        "webhook": os.environ.get("IKI_API_FOLD_CALLBACK_URL"),
    }
    request = urllib.request.Request(  # noqa: S310 - IKI_API_FOLD_URL is https
        f"{base}/v1/predictions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as answer:  # noqa: S310 - https only
        job = json.load(answer)
    state = {"protein": protein["id"]}
    output: dict[str, Any] = {"park": {"api": "fold", "job": job["id"], "state": state}}
else:
    (Path(os.environ["IKI_OUT_DIR"]) / "predicted.pdb").write_text(
        resume["answer"]["output"]["pdb"]
    )
    output = {
        "creates": [
            {
                "type": "acme/protein.ProteinStructure",
                "values": {
                    "structure": {"$file": "predicted.pdb", "media_type": "chemical/x-pdb"},
                    "method": "predicted",
                },
            }
        ]
    }
Path(os.environ["IKI_OUTPUT"]).write_text(
    json.dumps({"apiVersion": "iki/v1", "kind": "RunOutput", **output})
)
