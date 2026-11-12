"""Gives each sample with no storage the plugin's `default_storage` setting, or with
``--tidy``, writes each sample's storage location the same way.

Like every action's command, it reads the run from $IKI_INPUT, reports progress with
`::progress` lines, and writes its results to $IKI_OUTPUT.
"""

import json
import os
import re
import sys
from pathlib import Path


def tidy(storage: str) -> str:
    """Spaces and temperatures written one way: '  -80C   freezer ' → '-80 °C freezer'."""
    words = " ".join(storage.split())
    return re.sub(r"(-?\d+)\s*°?\s*[cC]\b", r"\1 °C", words)


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    default = run["settings"]["default_storage"]
    tidying = "--tidy" in sys.argv[1:]
    samples = run["inputs"]["sample"]
    updates = []
    for done, sample in enumerate(samples, start=1):
        storage = sample["values"].get("storage")
        wanted = tidy(storage) if tidying and storage else storage or default
        if wanted != storage:
            updates.append({"entity": sample["id"], "values": {"storage": wanted}})
            print(f"{sample['hrid']}: stored in {wanted}")
        print(f"::progress {done / len(samples):.2f} {done} of {len(samples)}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
