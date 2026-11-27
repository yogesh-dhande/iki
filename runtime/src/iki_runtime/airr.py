"""AIRR rearrangements: the AIRR Community's tab-separated format for V(D)J calls.

The format is the AIRR Data Representation's Rearrangement schema,
https://docs.airr-community.org/en/stable/datarep/rearrangements.html: a header row
naming each column, then one row per rearrangement, cells separated by tabs. A file
may be compressed (see :mod:`iki_runtime.text`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Collection, Iterable, Iterator

from iki_runtime.text import open_text

__all__ = ["LOCI", "Rearrangements", "locus", "read", "rearrangements"]

#: The loci the schema's ``locus`` names (its enum): the immunoglobulin heavy, iota,
#: kappa and lambda chains, and the T-cell receptor's alpha, beta, delta and gamma.
LOCI = frozenset({"IGH", "IGI", "IGK", "IGL", "TRA", "TRB", "TRD", "TRG"})


class Rearrangements:
    """A file's rearrangements: its ``header``, and its rows, each a dict of its cells by
    column, as an iterator. Raises ``ValueError`` for a row whose cells don't match the
    header, saying which row, from 1."""

    def __init__(self, lines: Iterable[str], required: Collection[str]) -> None:
        self._lines = (line.rstrip("\r\n") for line in lines)
        self.header: list[str] = next((line.split("\t") for line in self._lines if line), [])
        if not self.header or "" in self.header or len(set(self.header)) != len(self.header):
            raise ValueError("The AIRR file's first row must name distinct, nonempty columns.")
        missing = [name for name in required if name not in self.header]
        if missing:
            raise ValueError(f"The AIRR file has no {', '.join(missing)} column.")

    def __iter__(self) -> Iterator[dict[str, str]]:
        number = 0
        for line in self._lines:
            if not line:
                continue
            number += 1
            cells = line.split("\t")
            if len(cells) != len(self.header):
                raise ValueError(
                    f"AIRR row {number} has {len(cells)} columns, expected {len(self.header)}."
                )
            yield dict(zip(self.header, cells))


def rearrangements(
    lines: Iterable[str], required: Collection[str] = ("sequence_id",)
) -> Rearrangements:
    """The rearrangements in ``lines``, an AIRR file's text, which must have the columns
    ``required`` names. Raises ``ValueError`` if its header doesn't name distinct
    columns or lacks one of them."""
    return Rearrangements(lines, required)


def read(
    path: str | Path, required: Collection[str] = ("sequence_id",)
) -> Iterator[dict[str, str]]:
    """The rows of the AIRR file at ``path``, compressed or not (see
    :func:`rearrangements`)."""
    with open_text(path) as source:
        yield from rearrangements(source, required)


def locus(row: dict[str, str]) -> str | None:
    """The row's locus: its ``locus`` cell, which the schema lets be empty, or else the
    locus its V and J calls share. A call names its gene, and a gene's first three
    letters are its locus (``IGHV3-23*01`` is IGH's), so when both calls are given and
    every gene they name is of one locus, that's the row's. Otherwise None."""
    given = (row.get("locus") or "").strip()
    if given:
        return given
    v, j = (row.get("v_call") or "").strip(), (row.get("j_call") or "").strip()
    if not v or not j:
        return None
    prefixes = {call.strip()[:3] for call in f"{v},{j}".split(",") if call.strip()}
    if len(prefixes) == 1 and prefixes <= LOCI:
        return prefixes.pop()
    return None
