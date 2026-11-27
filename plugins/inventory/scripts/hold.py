"""Carry a container's withdrawal reason to its derived stock."""

import json
import os
from pathlib import Path


def main() -> None:
    given = json.loads(Path(os.environ["IKI_INPUT"]).read_text())
    inputs = given["inputs"]
    (source,) = inputs["source"]
    changes = []
    if source["values"].get("on_hold"):
        why = (given.get("params") or {}).get("reason") or source["values"]["hold_reason"]
        reason = f"Withdrawn stock {source['hrid']}: {why.strip()}"
        for tube in inputs.get("tubes", []):
            if tube["values"].get("on_hold"):
                continue
            changes.append(
                {
                    "entity": tube["id"],
                    "require_unchanged": True,
                    "values": {"on_hold": True, "hold_reason": reason},
                }
            )
    updates = {}
    if changes:
        # Check the source revision in the same transaction as the descendant holds.
        # Writing the existing reason changes no value if the source is still current.
        updates["source"] = [
            {
                "entity": source["id"],
                "require_unchanged": True,
                "values": {"hold_reason": source["values"]["hold_reason"]},
            }
        ]
        updates["tubes"] = changes
    output = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "updates": updates,
    }
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
