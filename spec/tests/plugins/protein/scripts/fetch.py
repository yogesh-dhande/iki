"""Creates the protein UniProt holds under the `accession` param, saying where it came
from. It reaches rest.uniprot.org through HTTPS_PROXY, as its action declares."""

import hashlib
import json
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
accession = run["params"]["accession"]
url = f"https://rest.uniprot.org/uniprotkb/{accession}.fasta"
with urllib.request.urlopen(url, timeout=30) as response:
    text = response.read().decode()
header, _, body = text.partition("\n")
output = {
    "apiVersion": "iki/v1",
    "kind": "RunOutput",
    "creates": [
        {
            "type": "protein.Protein",
            "values": {"name": accession, "sequence": "".join(body.split())},
            "origin": {
                "source": "UniProtKB",
                "id": accession,
                "url": f"https://rest.uniprot.org/uniprotkb/{accession}",
                "retrieved_at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
        }
    ],
}
Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
