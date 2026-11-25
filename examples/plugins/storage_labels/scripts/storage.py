"""Assign the configured storage code to each sample with an empty storage location."""

import json
import os
from pathlib import Path


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    updates = [
        {"entity": sample["id"], "values": {"storage": run["settings"]["default_storage"]}}
        for sample in run["inputs"]["sample"]
        if not sample["values"].get("storage")
    ]
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
