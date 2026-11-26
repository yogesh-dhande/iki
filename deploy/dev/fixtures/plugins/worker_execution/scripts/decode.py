"""Read the stamped labels from the setup version mounted on this worker."""

import json
import os
import sys
from pathlib import Path

codes = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
updates = [
    {"entity": sample["id"], "values": {"storage": codes[sample["values"]["storage"]]}}
    for sample in run["inputs"]["sample"]
]
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
