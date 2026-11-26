"""Ranks each request by the project that asked, from the core's ranking (ADR 0232).

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does.
"""

import json
import os
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
ranks = {r["values"]["project_slug"]: r["values"]["rank"] for r in run["inputs"]["ranks"]}
updates = [
    {
        "entity": request["id"],
        "values": {"client_rank": ranks.get(request["request"]["asking_project"]["slug"])},
    }
    for request in run["inputs"]["requests"]
]
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"requests": updates}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
