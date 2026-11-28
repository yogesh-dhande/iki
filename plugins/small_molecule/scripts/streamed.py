"""A run input read with one of its inputs a record at a time, so a search over the most
compounds its action takes (100,000, each with its Molfile, InChI and descriptors) never
holds them all: ``json.loads`` of such an input is several times the file's size in memory,
more than the action's.

The run input is JSON (run-input/v1). This reads it a megabyte at a time, decoding each
value with the standard library's decoder; the streamed input's records are each decoded
on their own and handed on, never kept.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

CHUNK = 1 << 20
SPACE = re.compile(r"[ \t\n\r]*")
DECODER = json.JSONDecoder()


class _Reader:
    """A JSON text, read a chunk at a time: what's left of it is ``buffer[at:]``."""

    def __init__(self, source: TextIO) -> None:
        self.source = source
        self.buffer = ""
        self.at = 0
        self.ended = False

    def _more(self) -> bool:
        piece = "" if self.ended else self.source.read(CHUNK)
        if not piece:
            self.ended = True
            return False
        self.buffer = self.buffer[self.at :] + piece
        self.at = 0
        return True

    def peek(self) -> str:
        """The next character that isn't white space, or "" at the end."""
        while True:
            self.at = SPACE.match(self.buffer, self.at).end()  # type: ignore[union-attr]
            if self.at < len(self.buffer):
                return self.buffer[self.at]
            if not self._more():
                return ""

    def take(self, character: str) -> None:
        if self.peek() != character:
            raise ValueError(f"The run input isn't JSON: expected {character!r}.")
        self.at += 1

    def comma(self, closing: str) -> bool:
        """Past a separating comma, whether another member follows before ``closing``."""
        if self.peek() == ",":
            self.at += 1
            return True
        return self.peek() != closing

    def value(self) -> Any:  # noqa: ANN401 - any JSON value
        """The next whole value, read on until the buffer holds all of it."""
        self.peek()
        while True:
            try:
                value, end = DECODER.raw_decode(self.buffer, self.at)
            except json.JSONDecodeError:
                if self._more():
                    continue
                raise
            # A number may go on past the buffer: read on, and decode it again.
            if (
                end == len(self.buffer)
                and not isinstance(value, dict | list | str)
                and self._more()
            ):
                continue
            self.at = end
            return value


def _walk(source: TextIO, name: str, run: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Each record of input ``name``, in order, filling ``run`` with everything else."""
    reader = _Reader(source)
    reader.take("{")
    while reader.peek() != "}":
        key = reader.value()
        reader.take(":")
        if key != "inputs":
            run[key] = reader.value()
        else:
            inputs: dict[str, Any] = {}
            reader.take("{")
            while reader.peek() != "}":
                input_name = reader.value()
                reader.take(":")
                if input_name != name:
                    inputs[input_name] = reader.value()
                else:
                    inputs[input_name] = []
                    reader.take("[")
                    while reader.peek() != "]":
                        yield reader.value()
                        if not reader.comma("]"):
                            break
                    reader.take("]")
                if not reader.comma("}"):
                    break
            reader.take("}")
            run["inputs"] = inputs
        if not reader.comma("}"):
            break
    reader.take("}")


def run_input(path: Path, name: str) -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    """The run input without input ``name``'s records (an empty list in their place), and
    an iterator over them, which reads the file again. Each is the record as the run input
    gives it."""
    run: dict[str, Any] = {}
    with path.open(encoding="utf-8") as source:
        for _ in _walk(source, name, run):
            pass

    def records() -> Iterator[dict[str, Any]]:
        with path.open(encoding="utf-8") as source:
            yield from _walk(source, name, {})

    return run, records()
