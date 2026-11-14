"""The example LIMS plugin's write-back: each protein's Iki ID and name, written to its
LIMS record (the one its `lims_id` names).

Iki changes nothing, so the run writes no entities; it keeps what it wrote to each
record in `sent.json`, with the run. A protein without a LIMS record is skipped, with
a warning. A record the LIMS refuses fails the run, after those
before it were written: running it again writes them again, which changes nothing.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from lims import Client, LimsError, host, run_input


def main() -> None:
    run = run_input()
    proteins = run.get("inputs", {}).get("proteins", [])
    messages = []
    sent: list[dict[str, object]] = []
    try:
        client = Client(host(run))
        for protein in proteins:
            values = protein.get("values", {})
            lims_id = values.get("lims_id")
            if not lims_id:
                messages.append(
                    {"level": "warning", "text": f"{protein['hrid']} has no LIMS record: skipped."}
                )
                continue
            fields = {"iki_id": protein["hrid"], "name": values.get("name")}
            client.update(lims_id, fields)
            sent.append({"lims_id": lims_id, "fields": fields})
            print(f"{protein['hrid']} → {lims_id}", flush=True)
    except LimsError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    finally:
        # What reached the LIMS, even when a later record failed.
        kept = Path(os.environ["IKI_OUT_DIR"]) / "sent.json"
        kept.write_text(json.dumps(sent, indent=2) + "\n", encoding="utf-8")
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "metrics": {"records": len(sent)},
        **({"messages": messages[:100]} if messages else {}),
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
