"""Inspect BigSMILES v1.1 stochastic objects without expanding the polymer ensemble.

The BigSMILES specification defines the repeat fragments, implicit end groups and
terminal descriptors: https://olsenlabmit.github.io/BigSMILES/docs/line_notation.html .
RDKit reads each deterministic SMILES graph with its descriptors replaced by dummy atoms.
The stored fragments always come directly from the submitted text.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from rdkit import Chem, rdBase

# Both the registration and the computed JSON fields have these bounds. Limit each
# individual RDKit parse as well, including the depth of nested branches.
MAX_CHARS = 20_000
MAX_FRAGMENTS = 1024
MAX_SMILES = 2048
MAX_BRANCH_DEPTH = 64
BOUNDARY = re.compile(r"\[(?:[$<>][0-9]*)?\]")
DESCRIPTOR = re.compile(r"\[([$<>])([0-9]*)\]")
OBJECT = re.compile(r"\{([^{}]*)\}")


class UnsupportedError(ValueError):
    """A valid or potentially valid notation outside the inspector's supported subset."""


class Descriptor(NamedTuple):
    kind: str
    index: int

    def partner(self) -> Descriptor:
        return Descriptor({"<": ">", ">": "<", "$": "$"}[self.kind], self.index)


def _descriptor(text: str) -> Descriptor:
    # BigSMILES treats the first descriptor as unnumbered. Normalise optional
    # numeric spelling for comparison only, retaining it in the original fragments.
    return Descriptor(text[1], int(text[2:-1] or "1"))


def _smiles(source: str) -> Chem.Mol:
    if len(source) > MAX_SMILES:
        raise UnsupportedError("A SMILES fragment exceeds the inspector's size limit.")
    depth = 0
    for char in source:
        if char == "(":
            depth += 1
            if depth > MAX_BRANCH_DEPTH:
                raise UnsupportedError("A SMILES fragment exceeds the branch depth limit.")
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("A SMILES branch closes before it opens.")
    if depth:
        raise ValueError("A SMILES branch is unclosed.")
    params = Chem.SmilesParserParams()
    params.allowCXSMILES = False
    params.parseName = False
    params.removeHs = False  # [H] may itself be an end group.
    with rdBase.BlockLogs():
        molecule = Chem.MolFromSmiles(source, params)
    if molecule is None:
        raise ValueError("RDKit could not read the fragment's atoms, bonds or ring closures.")
    return molecule


def _fragment(source: str, *, end_group: bool) -> list[tuple[Descriptor, float]]:
    if not source:
        raise ValueError("An empty fragment occurs in a stochastic object.")
    if "." in source:
        raise UnsupportedError("Disconnected repeat or end-group fragments are outside this check.")
    markers = list(DESCRIPTOR.finditer(source))
    # Number the placeholders by *occurrence* so two different descriptors on the
    # same atom still resolve to their own attachment bonds in the RDKit graph.
    replacements = iter(f"[{n}*]" for n in range(1, len(markers) + 1))
    molecule = _smiles(DESCRIPTOR.sub(lambda _: next(replacements), source))
    if not markers and not end_group:
        raise UnsupportedError("Repeat-unit shorthand needs explicit bonding descriptors to check.")
    if end_group and len(markers) != 1:
        raise ValueError("An implicit end-group fragment needs exactly one bonding descriptor.")
    if not end_group and len(markers) < 2:
        raise UnsupportedError("The inspector needs two or more explicit descriptors per repeat.")
    connections = []
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() != 0:
            continue
        if atom.GetDegree() != 1 or atom.GetNeighbors()[0].GetAtomicNum() == 0:
            raise ValueError("Each bonding descriptor must attach to one chemical atom.")
        marker = markers[atom.GetIsotope() - 1]
        connections.append((_descriptor(marker[0]), atom.GetBonds()[0].GetBondTypeAsDouble()))
    return connections


def _object(body: str) -> tuple[list[str], list[str], tuple[float | None, float | None]]:
    left = BOUNDARY.match(body)
    right = re.search(r"\[(?:[$<>][0-9]*)?\]$", body)
    if left is None or right is None or left.end() > right.start():
        raise ValueError("A stochastic object needs both terminal descriptors and a repeat unit.")
    units, separator, ends = body[left.end() : right.start()].partition(";")
    if ";" in ends:
        raise ValueError("A stochastic object has more than one end-group separator.")
    repeats = units.split(",")
    # The v1.1 grammar also permits an empty end-group list after the separator.
    end_groups = ends.split(",") if separator and ends else []
    if len(repeats) + len(end_groups) > MAX_FRAGMENTS:
        raise UnsupportedError("The stochastic object exceeds the fragment limit.")
    repeat_bonds: dict[Descriptor, float] = {}
    for fragment in repeats:
        for descriptor, order in _fragment(fragment, end_group=False):
            if repeat_bonds.setdefault(descriptor, order) != order:
                raise ValueError("A bonding descriptor has inconsistent bond orders.")
    available = set(repeat_bonds)
    for fragment in end_groups:
        ((descriptor, order),) = _fragment(fragment, end_group=True)
        if repeat_bonds.get(descriptor.partner()) != order:
            raise ValueError("An implicit end group needs a matching bond on a repeat unit.")
        available.add(descriptor)

    def terminal_order(text: str) -> float | None:
        if text == "[]":
            return None
        descriptor = _descriptor(text)
        available.add(descriptor)
        if descriptor.partner() not in repeat_bonds:
            raise ValueError("A terminal descriptor needs a matching bond on a repeat unit.")
        return repeat_bonds[descriptor.partner()]

    terminals = terminal_order(left[0]), terminal_order(right[0])
    for descriptor, order in repeat_bonds.items():
        partner = descriptor.partner()
        if partner not in available or repeat_bonds.get(partner, order) != order:
            raise ValueError(
                "A repeat-unit descriptor has no complementary bond of the same order."
            )
    return repeats, end_groups, terminals


def _outside(skeleton: str, terminals: list[tuple[float | None, float | None]]) -> None:
    """Read the deterministic structure, checking each object's inward-facing boundaries."""
    molecule = _smiles(skeleton)
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() != 0:
            continue
        before, after = terminals[atom.GetIsotope() - 1]
        seen: set[bool] = set()
        for bond in atom.GetBonds():
            if bond.IsInRing():
                raise UnsupportedError(
                    "Ring closures through a stochastic object are outside this check."
                )
            left = bond.GetOtherAtomIdx(atom.GetIdx()) < atom.GetIdx()
            if left in seen:
                raise UnsupportedError(
                    "Several exterior bonds on one object end are outside this check."
                )
            seen.add(left)
            expected = before if left else after
            if expected is None or bond.GetBondTypeAsDouble() != expected:
                raise ValueError("An exterior bond needs a matching nonempty terminal descriptor.")
        # A nonempty terminal with no written exterior atom implies a single-bonded H.
        for left, order in ((True, before), (False, after)):
            if left not in seen and order not in (None, 1.0):
                raise ValueError("An implicit terminal hydrogen requires a single bond.")


def inspect(source: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stochastic_objects": None,
        "repeat_units": None,
        "implicit_end_groups": None,
    }
    try:
        if len(source) > MAX_CHARS:
            raise UnsupportedError("The notation exceeds the inspector's size limit.")
        if not source or not source.isascii() or any(c.isspace() for c in source):
            raise ValueError("Give a single nonempty BigSMILES string without whitespace.")
        if "[#" in source or "{#" in source or re.search(r"\[[$<>]\d*\[", source):
            raise UnsupportedError("Named fragments and ladder descriptors are outside this check.")
        if any(c in source for c in "*|"):
            raise UnsupportedError("Wildcard atoms and extended notation are outside this check.")
        depth = 0
        nested = False
        for char in source:
            if char == "{":
                depth += 1
                nested |= depth > 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    raise ValueError("A stochastic object closes before it opens.")
        if depth:
            raise ValueError("A stochastic object is unclosed.")
        if nested:
            raise UnsupportedError("Nested stochastic objects are outside this check.")
        terminals = []
        units = []
        ends = []
        skeleton = []
        last = 0
        for number, match in enumerate(OBJECT.finditer(source), start=1):
            repeats, end_groups, terminal = _object(match[1])
            terminals.append(terminal)
            units.extend({"object": number, "fragment": s} for s in repeats)
            ends.extend({"object": number, "fragment": s} for s in end_groups)
            if len(units) > MAX_FRAGMENTS or len(ends) > MAX_FRAGMENTS:
                raise UnsupportedError("The notation exceeds the fragment limit.")
            skeleton.extend([source[last : match.start()], f"[{number}*]"])
            last = match.end()
        if not terminals:
            raise ValueError("Give a BigSMILES string containing at least one stochastic object.")
        skeleton.append(source[last:])
        _outside("".join(skeleton), terminals)
    except UnsupportedError as error:
        return {**result, "notation_status": "unsupported", "notation_note": str(error)}
    except ValueError as error:
        return {**result, "notation_status": "invalid", "notation_note": str(error)}
    return {
        "notation_status": "checked",
        "notation_note": "Checked the explicit descriptors, repeat fragments and end-group bonds.",
        "stochastic_objects": len(terminals),
        "repeat_units": units,
        "implicit_end_groups": ends,
    }


def compute(run: Mapping[str, Any]) -> dict[str, Any]:
    updates = [
        {"entity": polymer["id"], "values": inspect(polymer["values"]["bigsmiles"])}
        for polymer in run["inputs"]["polymer"]
    ]
    return {"updates": {"polymer": updates}}


if __name__ == "__main__":
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", **compute(run)}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")
