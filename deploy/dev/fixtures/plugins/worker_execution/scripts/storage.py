"""Write the container fixture's default location, or normalize a supplied location."""

import json
import os
import re
import sys
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
updates = []
for sample in run["inputs"]["sample"]:
    storage = sample["values"].get("storage")
    if storage and "--tidy" not in sys.argv[1:]:
        continue
    words = " ".join((storage or "-80 °C freezer").split())
    normalized = re.sub(r"(-?\d+)\s*°?\s*[cC]\b", r"\1 °C", words)
    if normalized != storage:
        updates.append({"entity": sample["id"], "values": {"storage": normalized}})
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
