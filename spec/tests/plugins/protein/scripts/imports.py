"""Creates a protein for each record of the FASTA file in the `file` param, saying where
it came from."""

import hashlib
import json
import os
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
file_id = run["params"]["file"]
text = Path(run["files"][file_id]["path"]).read_text(encoding="utf-8")
creates = []
for chunk in text.split(">")[1:]:
    header, _, body = chunk.partition("\n")
    name = header.split()[0]
    creates.append(
        {
            "type": "protein.Protein",
            "values": {"name": name, "sequence": "".join(body.split())},
            "origin": {
                "source": "FASTA file",
                "id": name,
                "file": file_id,
                "sha256": hashlib.sha256((">" + chunk).encode()).hexdigest(),
            },
        }
    )
print(f"Read {len(creates)} records.")
output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": creates}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
