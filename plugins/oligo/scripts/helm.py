"""HELM read for its chemistry: each polymer's monomers, how they're joined, and the bonds
between polymers, from HELM 2.04's grammar (Pistoia Alliance, 2018).

Iki reads and canonicalises HELM with iki-wasm, which a HELM field's value has passed by
the time an action sees it (ADR 0144). This reads only what building the molecule needs,
and refuses what has no one structure: a monomer that's one of several or a mixture, a
repeat of a range of counts, an unknown monomer or position, a polymer group, a monomer in
in-line SMILES. Positions are counted as HELM counts them, and as iki-wasm does: each
monomer, a branch's too, with a group of units once (5.2.3); a repeat of a fixed count is
written out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The types of polymer HELM has.
TYPES = ("PEPTIDE", "RNA", "CHEM", "BLOB")


class HelmError(ValueError):
    """Why HELM can't be built into a molecule."""


@dataclass
class Node:
    """A monomer in a polymer: its ID, and the backbone monomer it hangs from, if it's a
    branch (a nucleotide's base)."""

    id: str
    #: Its index in its polymer's nodes, from 0.
    index: int
    #: The index of the backbone monomer it hangs from, for a branch.
    branch_of: int | None
    #: The unit it's in (its nucleotide, for RNA), from 0.
    unit: int


@dataclass
class Polymer:
    id: str
    type: str
    nodes: list[Node] = field(default_factory=list)
    #: Each position as written (from 1) to its node's index; None inside a repeat.
    positions: dict[int, int | None] = field(default_factory=dict)

    @property
    def units(self) -> int:
        return (self.nodes[-1].unit + 1) if self.nodes else 0


@dataclass(frozen=True)
class End:
    polymer: str
    position: int
    attachment: str  # R1, R2, … or pair


@dataclass(frozen=True)
class Connection:
    one: End
    other: End

    @property
    def pair(self) -> bool:
        return self.one.attachment == "pair"


@dataclass
class Helm:
    polymers: list[Polymer]
    connections: list[Connection]

    def polymer(self, name: str) -> Polymer:
        for p in self.polymers:
            if p.id == name:
                return p
        raise HelmError(f"It has no polymer {name}.")


def _sections(text: str) -> list[str]:
    """The text between the four `$` outside brackets, braces and quotes, and the version."""
    cuts, square, curly, quoted = [], 0, 0, False
    for i, c in enumerate(text):
        if c == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif c == "[":
            square += 1
        elif c == "]":
            square -= 1
        elif c == "{" and not square:
            curly += 1
        elif c == "}" and not square:
            curly -= 1
        elif c == "$" and not square and not curly:
            cuts.append(i)
    if len(cuts) != 4:
        raise HelmError("HELM has four sections, each ended by “$”.")
    bounds = [-1, *cuts]
    parts = [text[bounds[i] + 1 : bounds[i + 1]] for i in range(4)]
    return [*parts, text[cuts[3] + 1 :]]


def _split(text: str, by: str) -> list[str]:
    """``text`` split at ``by`` outside brackets, parentheses and quotes."""
    out, depth, quoted, start = [], 0, False, 0
    for i, c in enumerate(text):
        if c == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif c in "[(":
            depth += 1
        elif c in "])":
            depth -= 1
        elif c == by and depth == 0:
            out.append(text[start:i])
            start = i + 1
    out.append(text[start:])
    return out


_ANNOTATION = re.compile(r'"[^"]*"')
_REPEAT = re.compile(r"'(\d+)(?:-(\d+))?'$")


class _Builder:
    def __init__(self, polymer: Polymer) -> None:
        self.p = polymer
        self.position = 0

    def node(self, monomer: str, branch_of: int | None, unit: int) -> int:
        index = len(self.p.nodes)
        self.p.nodes.append(Node(monomer, index, branch_of, unit))
        return index


def _monomers(text: str, where: str) -> list[tuple[str, bool]]:
    """A unit's monomers in order, each with whether it's a branch: `R(A)P` is R, then A as
    a branch, then P."""
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "[":
            depth, j = 1, i + 1
            while j < len(text) and depth:
                depth += {"[": 1, "]": -1}.get(text[j], 0)
                j += 1
            inner = text[i + 1 : j - 1]
            if "*" in inner or "|$" in inner:
                raise HelmError(
                    f"{where} has a monomer in in-line SMILES ([{inner}]): register it as a "
                    "monomer, so its structure is the registry's, and use its symbol."
                )
            out.append((inner, False))
            i = j
        elif c == "(":
            depth, j = 1, i + 1
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            inner = text[i + 1 : j - 1]
            if "," in inner or "+" in inner:
                raise HelmError(
                    f"{where} has a monomer that's one of several, or a mixture ({inner}): "
                    "a molecule's weight and formula need each monomer known."
                )
            branch = _monomers(inner, where)
            if len(branch) != 1 or not out:
                raise HelmError(f"{where}: a branch is one monomer after the one it hangs from.")
            out.append((branch[0][0], True))
            i = j
        else:
            out.append((c, False))
            i += 1
    for monomer, _ in out:
        if monomer in ("*", "_", "X", "N", "?"):
            raise HelmError(
                f"{where} has an unknown monomer ({monomer}): a molecule's weight and "
                "formula need each monomer known."
            )
    return out


def _units(text: str, b: _Builder, where: str, repeated: bool) -> None:
    for raw in _split(text, "."):
        unit = _ANNOTATION.sub("", raw)
        if not unit.strip():
            raise HelmError(
                f"{where} has a unit with no monomer in it: HELM separates monomer units "
                "with “.”, such as R(A)P.R(C)."
            )
        times, ranged = 1, False
        if (m := _REPEAT.search(unit)) is not None:
            low, high = int(m.group(1)), int(m.group(2) or m.group(1))
            if low != high:
                raise HelmError(
                    f"{where} repeats a unit {low} to {high} times: a molecule's weight and "
                    "formula need one count."
                )
            times, unit = low, unit[: m.start()]
            ranged = times > 1
        inside = repeated or ranged
        if unit.startswith("(") and unit.endswith(")") and _closes_at_end(unit):
            if len(_split(unit[1:-1], ",")) > 1 or len(_split(unit[1:-1], "+")) > 1:
                raise HelmError(
                    f"{where} has a monomer that's one of several, or a mixture "
                    f"({unit[1:-1]}): a molecule's weight and formula need each monomer known."
                )
            # A group of units: one position as written, however many monomers it holds
            # or times it's repeated. Its own units' positions aren't the polymer's.
            at = b.position + 1
            before = len(b.p.nodes)
            positions, b.p.positions = b.p.positions, {}
            for _ in range(times):
                _units(unit[1:-1], b, where, True)
            b.p.positions = positions
            b.position = at
            b.p.positions[at] = before if not inside and len(b.p.nodes) == before + 1 else None
            continue
        monomers = _monomers(unit, where)
        first = len(b.p.nodes)
        for copy in range(times):
            unit_index = (b.p.nodes[-1].unit + 1) if b.p.nodes else 0
            backbone: int | None = None
            for k, (monomer, branch) in enumerate(monomers):
                index = b.node(monomer, backbone if branch else None, unit_index)
                if not branch:
                    backbone = index
                if copy == 0:
                    b.p.positions[b.position + k + 1] = None if inside else first + k
        b.position += len(monomers)


def _closes_at_end(text: str) -> bool:
    depth = 0
    for i, c in enumerate(text):
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth == 0 and i < len(text) - 1:
            return False
    return True


_POLYMER = re.compile(r"^(PEPTIDE|RNA|CHEM|BLOB)([1-9][0-9]*)\{(.*)\}$", re.S)
_END = re.compile(r"^(\d+):(R\d+|pair)$")


def read(text: str) -> Helm:
    """``text``, HELM 2 or 1, read for its chemistry. Raises :class:`HelmError`."""
    polymers_text, connections_text, third, _, version = _sections(text.strip())
    if version not in ("", "V2.0"):
        raise HelmError("After the fourth “$” comes V2.0, or nothing for HELM 1.")
    polymers = []
    for raw in _split(polymers_text, "|"):
        found = _POLYMER.match(_ANNOTATION.sub("", raw) if raw.endswith('"') else raw)
        if found is None:
            raise HelmError(f"“{raw[:40]}” isn't a polymer, such as RNA1{{R(A)P}}.")
        kind, number, body = found.groups()
        polymer = Polymer(f"{kind}{number}", kind)
        if kind == "BLOB":
            raise HelmError(
                f"{polymer.id} is a BLOB, which HELM doesn't give a structure: its weight and "
                "formula can't be computed."
            )
        _units(body, _Builder(polymer), polymer.id, False)
        polymers.append(polymer)
    sections = [connections_text]
    if version == "":
        sections.append(third)  # HELM 1 keeps its base pairs here
    elif third:
        raise HelmError(
            "It has polymer groups: a molecule's weight and formula need each polymer known."
        )
    connections = []
    for section in sections:
        for raw in _split(section, "|") if section else []:
            parts = _ANNOTATION.sub("", raw).split(",")
            if len(parts) != 3 or "-" not in parts[2]:
                raise HelmError(f"“{raw}” isn't a connection, such as RNA1,CHEM1,1:R1-1:R2.")
            left, right = parts[2].split("-", 1)
            ends = []
            for polymer, end in ((parts[0], left), (parts[1], right)):
                m = _END.match(end)
                if m is None:
                    raise HelmError(
                        f"The connection {raw} has an end that isn't one monomer's position and "
                        "attachment point, such as 3:R2: a molecule needs each bond known."
                    )
                ends.append(End(polymer, int(m.group(1)), m.group(2)))
            connections.append(Connection(ends[0], ends[1]))
    helm = Helm(polymers, connections)
    for c in connections:
        for side in (c.one, c.other):
            p = helm.polymer(side.polymer)
            if side.position not in p.positions:
                raise HelmError(
                    f"A connection names monomer {side.position} of {p.id}, which has "
                    f"{max(p.positions, default=0)}."
                )
            if p.positions[side.position] is None:
                raise HelmError(
                    f"A connection names monomer {side.position} of {p.id}, inside a repeat: "
                    "write the repeat out to connect one of its monomers."
                )
    return helm


def node_of(helm: Helm, end: End) -> tuple[Polymer, Node]:
    """The polymer and monomer a connection's end is at."""
    p = helm.polymer(end.polymer)
    index = p.positions[end.position]
    assert index is not None  # noqa: S101 - `read` refuses those
    return p, p.nodes[index]
