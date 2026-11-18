"""Notes on each sample that its protein changed: what a trigger that gives it a protein's
samples (ADR 0123) does with them.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does.
"""

import json
import os
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
(protein,) = run["inputs"]["protein"]
updates = [
    {"entity": sample["id"], "values": {"notes": f"{protein['hrid']} changed."}}
    for sample in run["inputs"]["samples"]
]
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"samples": updates}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
