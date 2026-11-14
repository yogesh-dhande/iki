"""The example LIMS plugin's import: the proteins the LIMS added or changed since the last
run, each as a protein to create with its `lims_id`.

The action matches on `lims_id` (`match_on`), so Iki updates the protein a record
already has instead of making another. A run the nightly trigger launched asks only
for what changed since the last one that succeeded (`run.trigger.since`); one
launched by hand asks for every record. Past a thousand, the creates go to a creates
file, which Iki writes in bulk.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from lims import Client, LimsError, host, record_url, run_input

INLINE = 1000
CREATES_FILE = "creates.jsonl"
SEQUENCE = re.compile(r"[ACDEFGHIKLMNPQRSTVWYBZXUOJ*]+")


class Creates:
    """output.json's `creates` while they're few, then a creates file."""

    def __init__(self) -> None:
        self.held: list[dict[str, Any]] = []
        self.file: TextIO | None = None
        self.count = 0

    def add(self, create: dict[str, Any]) -> None:
        self.count += 1
        if self.file is None and len(self.held) < INLINE:
            self.held.append(create)
            return
        if self.file is None:
            self.file = (Path(os.environ["IKI_OUT_DIR"]) / CREATES_FILE).open("w", encoding="utf-8")
            self.file.writelines(json.dumps(c, separators=(",", ":")) + "\n" for c in self.held)
            self.held = []
        self.file.write(json.dumps(create, separators=(",", ":")) + "\n")

    def output(self) -> dict[str, Any]:
        if self.file is None:
            return {"creates": self.held}
        self.file.close()
        return {"creates_file": {"$file": CREATES_FILE}}


def create(item: dict[str, Any], lims_host: str, retrieved: str) -> dict[str, Any]:
    lims_id = str(item.get("id") or "")
    sequence = re.sub(r"\s+", "", str(item.get("sequence") or "")).upper()
    if not lims_id:
        raise LimsError("The LIMS listed a protein without an `id`.")
    if not SEQUENCE.fullmatch(sequence):
        raise LimsError(f"{lims_id}'s sequence isn't one-letter amino acid codes.")
    values = {"lims_id": lims_id, "sequence": sequence}
    if item.get("name"):
        values["name"] = str(item["name"])
    origin = {
        "source": "LIMS",
        "id": lims_id,
        "url": record_url(lims_host, lims_id),
        "retrieved_at": retrieved,
    }
    if item.get("modified_at"):
        origin["version"] = f"modified {item['modified_at']}"[:100]
    return {"type": "protein.Protein", "values": values, "origin": origin}


def main() -> None:
    run = run_input()
    since = (run.get("run", {}).get("trigger") or {}).get("since")
    creates = Creates()
    try:
        lims_host = host(run)
        retrieved = datetime.now(UTC).isoformat(timespec="seconds")
        print(
            f"Asking {lims_host} for proteins changed since {since}"
            if since
            else f"Asking {lims_host} for every protein",
            flush=True,
        )
        for item in Client(lims_host).proteins(since):
            creates.add(create(item, lims_host, retrieved))
            if creates.count % 1000 == 0:
                print(f"{creates.count:,} records", flush=True)
    except LimsError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    print(f"{creates.count:,} records", flush=True)
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **creates.output()}
    output["metrics"] = {"records": creates.count}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
