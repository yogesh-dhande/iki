"""The example LIMS plugin's hold: each aliquot and result derived from a donor whose
consent was withdrawn, put on hold, saying why.

What's on hold already keeps its reason; it's written again only if it had none. The run
takes nothing off hold: that's for a person, who says why.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from lims import run_input


def held(entity: dict[str, Any], why: str) -> dict[str, Any] | None:
    """The update that puts it on hold, or None if it's on hold with a reason already."""
    values = entity.get("values", {})
    if values.get("on_hold") and values.get("hold_reason"):
        return None
    return {"entity": entity["id"], "values": {"on_hold": True, "hold_reason": why}}


def main() -> None:
    run = run_input()
    inputs = run.get("inputs", {})
    (donor,) = inputs["donor"]
    why = f"{donor['values'].get('code') or donor['hrid']} withdrew their consent."
    updates: dict[str, list[dict[str, Any]]] = {}
    for name in ("aliquots", "results"):
        found = [u for e in inputs.get(name, []) if (u := held(e, why)) is not None]
        if found:
            updates[name] = found
        print(f"{len(found)} of {len(inputs.get(name, []))} {name} put on hold", flush=True)
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": updates}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
