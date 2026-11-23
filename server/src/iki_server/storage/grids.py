"""A container's positions (ADR 0180): rows and columns, how they're named, and the order
they're numbered and filled in.

``letter_number`` names rows A to Z, then AA, AB, … as a spreadsheet's columns are, and
columns from 1: a 96-well plate's are A1 to H12. ``number`` numbers positions from 1 in
the grid's order: a 9 × 9 box's are 1 to 81, along each row (``by_row``) or down each
column (``by_column``)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

#: A position as people write it: A1, b12, AA3; or 17.
_LETTER_NUMBER = re.compile(r"^\s*([A-Za-z]{1,2})\s*0*([1-9][0-9]?)\s*$")
_NUMBER = re.compile(r"^\s*0*([1-9][0-9]{0,3})\s*$")


def letters(n: int) -> str:
    """The row's letters: 1 is A, 26 is Z, 27 is AA."""
    out = ""
    while n > 0:
        n, rest = divmod(n - 1, 26)
        out = chr(ord("A") + rest) + out
    return out


def row_of(text: str) -> int:
    n = 0
    for ch in text.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


class NotAPosition(ValueError):  # noqa: N818 - reads as "it's not a position"
    """A position that isn't one of a container's, saying why."""


@dataclass(frozen=True)
class Shape:
    rows: int
    columns: int
    labels: str = "letter_number"
    order: str = "by_row"

    @property
    def size(self) -> int:
        return self.rows * self.columns

    def index(self, row: int, column: int) -> int:
        """Its place in the grid's order, from 0."""
        if self.order == "by_column":
            return (column - 1) * self.rows + (row - 1)
        return (row - 1) * self.columns + (column - 1)

    def at(self, index: int) -> tuple[int, int]:
        """The position at ``index`` (from 0) in the grid's order."""
        if self.order == "by_column":
            column, row = divmod(index, self.rows)
        else:
            row, column = divmod(index, self.columns)
        return row + 1, column + 1

    def label(self, row: int, column: int) -> str:
        if self.labels == "number":
            return str(self.index(row, column) + 1)
        return f"{letters(row)}{column}"

    def positions(self) -> Iterator[tuple[int, int]]:
        """Every position, in the grid's order."""
        for index in range(self.size):
            yield self.at(index)

    def contains(self, row: int, column: int) -> bool:
        return 1 <= row <= self.rows and 1 <= column <= self.columns

    def parse(self, text: str) -> tuple[int, int]:
        """The row and column ``text`` names, or :class:`NotAPosition` saying why not."""
        if self.labels == "number":
            found = _NUMBER.match(text)
            if found is None:
                raise NotAPosition(f"“{text}” isn't a position: they're numbered 1 to {self.size}.")
            index = int(found.group(1)) - 1
            if index >= self.size:
                raise NotAPosition(f"{text} isn't a position: they're numbered 1 to {self.size}.")
            return self.at(index)
        found = _LETTER_NUMBER.match(text)
        last = self.label(self.rows, self.columns)
        if found is None:
            raise NotAPosition(f"“{text}” isn't a position: they're A1 to {last}.")
        row, column = row_of(found.group(1)), int(found.group(2))
        if not self.contains(row, column):
            raise NotAPosition(
                f"{found.group(1).upper()}{column} isn't a position: they're A1 to {last}."
            )
        return row, column
