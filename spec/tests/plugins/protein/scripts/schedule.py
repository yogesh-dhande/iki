"""Schedules each assay request it's given: a stand-in for a core's planning tool.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does.
"""

import json
import os
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
updates = [
    {"entity": request["id"], "values": {"scheduled_on": "2026-12-01", "core_notes": "Planned."}}
    for request in run["inputs"]["request"]
]
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"request": updates}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
