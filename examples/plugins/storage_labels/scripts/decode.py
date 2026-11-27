"""Writes each sample's storage location in full from the lab's code for it, using the
codes the `reference` environment's setup wrote to its volume, read-only here."""

import json
from pathlib import Path

import iki_action as iki

CODES = Path("/data/storage-codes/codes.json")


def decode(run: iki.Run) -> iki.Output:
    codes = json.loads(CODES.read_text(encoding="utf-8"))
    out = run.output()
    for sample in run.inputs("sample"):
        storage = sample.values.get("storage") or ""
        wanted = codes.get(storage.strip().lower())
        if wanted and wanted != storage:
            out.update("sample", sample, {"storage": wanted})
    return out


if __name__ == "__main__":
    iki.main(decode)
