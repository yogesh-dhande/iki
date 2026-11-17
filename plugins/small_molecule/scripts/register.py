"""The small_molecule plugin's registration from SMILES: `smiles` holds one compound a line.

Each line is its SMILES, then, after a space or tab, its name. Blank lines and lines
starting with # are left out, and so is a first line that's a header (`SMILES Name`).
Each structure is standardised and identified as `chemistry` says, and registered as a
compound; one already in Iki is reused, as is a line that's the same compound as an
earlier one. With `batches`, each line is also a batch of its compound, with the salt
and solvent its parent was taken from, and its formula weight.

A line that can't be registered fails the run, which names it and says why, so nothing
is half registered; or, with `on_error: skip`, is left out, saying so.
"""

from __future__ import annotations

import re
from typing import Any

from chemistry import (
    Creates,
    Messages,
    UnreadableError,
    batch_values,
    compound_values,
    fail,
    from_smiles,
    run_input,
    standardise,
    write_output,
)

MAX_LINES = 5_000
#: Failing, the run names this many lines, then says how many more there are.
NAMED = 20


def lines(text: str) -> list[tuple[int, str, str]]:
    """(line number, SMILES, name) for each line that holds a compound."""
    found = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip().removeprefix("﻿")
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[ \t]+", line, maxsplit=1)
        smiles = parts[0]
        name = parts[1].strip() if len(parts) > 1 else ""
        if not found and smiles.lower() == "smiles":
            continue
        found.append((number, smiles, name))
    return found


def register(run: dict[str, Any]) -> dict[str, Any]:
    params = run.get("params") or {}
    parent = bool(params.get("parent", False))
    batches = bool(params.get("batches", False))
    skip = params.get("on_error", "fail") == "skip"
    given = lines(params.get("smiles") or "")
    if not given:
        raise UnreadableError("Give at least one SMILES, such as CC(=O)Oc1ccccc1C(=O)O.")
    if len(given) > MAX_LINES:
        raise UnreadableError(
            f"That's {len(given):,} compounds, more than the {MAX_LINES:,} one run "
            "registers: split them, or import an SDF file."
        )
    creates = Creates(inline=batches)
    messages = Messages()
    problems: list[str] = []
    for done, (number, smiles, name) in enumerate(given, start=1):
        try:
            registered = standardise(from_smiles(smiles), parent)
        except UnreadableError as why:
            problems.append(f"Line {number}: {why}.")
            if skip:
                messages.add("warning", f"Line {number} was left out: {why}.")
            continue
        key = f"line-{number}"
        creates.add(
            {
                "type": "iki/small_molecule.Compound",
                "key": key,
                "values": compound_values(registered, name),
            }
        )
        if batches:
            values = {"compound": {"$create": key}, **batch_values(registered)}
            creates.add({"type": "iki/small_molecule.CompoundBatch", "values": values})
        for warning in registered.warnings:
            messages.add("warning", f"Line {number}: {warning}")
        taken = ", ".join(
            f"{getattr(registered, kind)} x{getattr(registered, kind + '_equivalents'):g}"
            for kind in ("salt", "solvate")
            if getattr(registered, kind)
        )
        print(
            f"Line {number}: {registered.inchikey} {registered.smiles}"
            + (f" ({taken} taken off)" if taken else "")
        )
        if done % 500 == 0:
            print(f"::progress {done / len(given):.2f} {done:,} of {len(given):,}", flush=True)
    if problems and not skip:
        shown = problems[:NAMED]
        more = f" And {len(problems) - NAMED:,} more." if len(problems) > NAMED else ""
        raise UnreadableError(
            f"{len(problems):,} of {len(given):,} lines can't be registered, so none was. "
            + " ".join(shown)
            + more
        )
    if len(problems) == len(given):
        raise UnreadableError(
            "None of its lines could be registered. " + " ".join(problems[:NAMED])
        )
    return {
        **creates.output(),
        "metrics": {"lines": len(given), "skipped": len(problems)},
        "messages": messages.output(),
    }


def main() -> None:
    try:
        output = register(run_input())
    except UnreadableError as why:
        fail(str(why))
        return
    write_output(output)


if __name__ == "__main__":
    main()
