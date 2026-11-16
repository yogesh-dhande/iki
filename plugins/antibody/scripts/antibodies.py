"""The antibody plugin's own calculations: `assemble`, an antibody from its chains.

It reads the run input from $IKI_INPUT and writes its results to $IKI_OUTPUT, as every
action's command does. A run that can't do what it was asked says why in an `::error`
line and exits 1.

An antibody is its chains: a heavy chain and a light chain for a whole antibody (igg)
or one arm of one (fab), and one chain for a single domain (vhh) or for both variable
domains on one chain (scfv). An isotype is a constant region's, so only those with one
take it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

#: Formats with a heavy and a light chain, and with a constant region (an isotype).
PAIRED = {"igg": "A whole antibody (IgG)", "fab": "A Fab"}
#: Formats with one chain.
SINGLE = {"vhh": "A VHH", "scfv": "An scFv"}
MAX_NAME = 255


class RefusedError(Exception):
    """What a run can't do, and why."""


def _label(entity: dict[str, Any]) -> str:
    return str(entity["values"].get("name") or entity["hrid"])


def assembled(
    heavy: dict[str, Any], light: dict[str, Any] | None, params: dict[str, Any]
) -> dict[str, Any]:
    """The antibody the chains make, as a create. Raises :class:`RefusedError`."""
    form = params.get("format") or "igg"
    isotype = params.get("isotype")
    if light is not None and light["id"] == heavy["id"]:
        raise RefusedError(f"{_label(heavy)} can't be both its heavy and its light chain.")
    if form in PAIRED and light is None:
        raise RefusedError(
            f"{PAIRED[form]} has a light chain: give one, or choose vhh or scfv for an "
            "antibody of one chain."
        )
    if form in SINGLE and light is not None:
        raise RefusedError(f"{SINGLE[form]} has one chain: leave out the light chain.")
    if isotype and form not in PAIRED:
        what = SINGLE.get(form, "This format")
        raise RefusedError(f"{what} has no constant region, so no isotype: leave it out.")
    name = params.get("name") or (
        f"{_label(heavy)} / {_label(light)}" if light is not None else _label(heavy)
    )
    values: dict[str, Any] = {
        "heavy_chain": heavy["id"],
        "name": name[:MAX_NAME],
        "format": form,
    }
    if light is not None:
        values["light_chain"] = light["id"]
    for key in ("isotype", "target"):
        if params.get(key):
            values[key] = params[key]
    derived = [heavy["id"], *([light["id"]] if light is not None else [])]
    return {"type": "antibody.Antibody", "values": values, "derived_from": derived}


def assemble(run: dict[str, Any]) -> dict[str, Any]:
    inputs = run["inputs"]
    (heavy,) = inputs["heavy_chain"]
    lights = inputs.get("light_chain") or []
    create = assembled(heavy, lights[0] if lights else None, run.get("params") or {})
    print(f"{create['values']['name']}: {create['values']['format']}")
    return {"creates": [create]}


def main() -> None:
    kind = sys.argv[1]
    compute = {"assemble": assemble}[kind]
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        result = compute(run)
    except RefusedError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **result}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
