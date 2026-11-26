"""Record a sample's storage location the first time it is assigned."""

import json
import os
from pathlib import Path


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    [sample] = run["inputs"]["sample"]
    updates = []
    if not sample["values"].get("storage"):
        updates.append({"entity": sample["id"], "values": {"storage": run["params"]["location"]}})
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
