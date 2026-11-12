"""Gives each sample with no storage the plugin's `default_storage` setting.

Like every action's command, it reads the run from $IKI_INPUT, reports progress with
`::progress` lines, and writes its results to $IKI_OUTPUT.
"""

import json
import os
from pathlib import Path


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    default = run["settings"]["default_storage"]
    samples = run["inputs"]["sample"]
    updates = []
    for done, sample in enumerate(samples, start=1):
        if not sample["values"].get("storage"):
            updates.append({"entity": sample["id"], "values": {"storage": default}})
            print(f"{sample['hrid']}: stored in {default}")
        print(f"::progress {done / len(samples):.2f} {done} of {len(samples)}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
