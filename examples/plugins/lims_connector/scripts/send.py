"""The example LIMS connector's write-back: each protein's Iki ID and name, written to its
LIMS record (the one its `lims_id` names).

Iki changes nothing, so the run writes no entities; it keeps what it wrote to each
record in `sent.json`, with the run. A protein without a LIMS record is skipped, with
a warning. A record the LIMS refuses fails the run, after those
before it were written: running it again writes them again, which changes nothing.
"""

from __future__ import annotations

import json

import iki_action as iki
from lims import Client, LimsError, host


def send(run: iki.Run) -> iki.Output:
    out = run.output()
    sent: list[dict[str, object]] = []
    try:
        client = Client(host(run))
        for protein in run.inputs("proteins"):
            values = protein.get("values", {})
            lims_id = values.get("lims_id")
            if not lims_id:
                out.warning(f"{protein['hrid']} has no LIMS record: skipped.")
                continue
            fields = {"iki_id": protein["hrid"], "name": values.get("name")}
            client.update(lims_id, fields)
            sent.append({"lims_id": lims_id, "fields": fields})
            print(f"{protein['hrid']} → {lims_id}", flush=True)
    finally:
        # What reached the LIMS, even when a later record failed.
        kept = run.out_dir / "sent.json"
        kept.write_text(json.dumps(sent, indent=2) + "\n", encoding="utf-8")
    out.metric("records", len(sent))
    return out


def main() -> None:
    iki.main(send, refuse=LimsError)


if __name__ == "__main__":
    main()
