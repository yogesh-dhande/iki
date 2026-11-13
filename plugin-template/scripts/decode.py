"""Writes each sample's storage location in full from the lab's code for it, using the
codes the `reference` environment's setup wrote to its volume, read-only here."""

import json
import os
from pathlib import Path

CODES = Path("/data/storage-codes/codes.json")


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    codes = json.loads(CODES.read_text(encoding="utf-8"))
    samples = run["inputs"]["sample"]
    updates = []
    for done, sample in enumerate(samples, start=1):
        storage = sample["values"].get("storage") or ""
        wanted = codes.get(storage.strip().lower())
        if wanted and wanted != storage:
            updates.append({"entity": sample["id"], "values": {"storage": wanted}})
            print(f"{sample['hrid']}: {storage} is {wanted}")
        print(f"::progress {done / len(samples):.2f} {done} of {len(samples)}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"sample": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
