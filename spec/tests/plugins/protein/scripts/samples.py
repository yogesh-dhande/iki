"""Creates a sample for each row of the CSV file in the `file` param (label, purity), or
updates the sample with that label, saying where each came from."""

import csv
import json
import os
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
file_id = run["params"]["file"]
with Path(run["files"][file_id]["path"]).open(encoding="utf-8", newline="") as rows:
    creates = [
        {
            "type": "acme/protein.Sample",
            "values": {"label": row["label"], "purity": float(row["purity"])},
            "origin": {"source": "CSV file", "id": row["label"], "file": file_id},
        }
        for row in csv.DictReader(rows)
    ]
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": creates}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
