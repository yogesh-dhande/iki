"""Reports each sample's purity: a stand-in for reading it off an instrument.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does.
"""

import json
import os
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
updates = [
    {"entity": sample["id"], "values": {"purity": 98.5}} for sample in run["inputs"]["sample"]
]
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
