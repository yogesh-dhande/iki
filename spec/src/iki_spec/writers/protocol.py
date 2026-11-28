"""How Iki talks to a writer's process (ADR 0230): length-prefixed frames, and a session.

A frame is a 4-byte big-endian length and that many bytes. An export is:

1. the server's header, JSON: ``{"writer", "sha256", "memory", "deadline"}`` (and, for a
   local child only, ``"path"``, the module's file); the child answers
   ``{"ready": true, "abi"}``, the ABI the module reads (one this Iki runs, so the server
   builds each request for it, :mod:`iki_spec.writers.abi`), or
   ``{"error": {"code", "message"}}`` and exits;
2. for each call, a JSON frame ``{"call": "begin" | "record" | "end"}`` and the request's
   bytes, canonical JSON, in a frame of their own; the child answers
   ``{"status", "notes", "fuel"}`` and the bytes the call adds, in a frame of their own, or
   ``{"failed": {"code", "message"}}`` and exits.

The request's bytes go into the module's memory as they came, so nothing between the
server and the module parses a record. The sandbox's launcher reads only the header, for
its deadline. A child serves one export and exits; ``begin`` may follow ``end`` in it, which
is how a ZIP of one file per record is written by one child.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any

__all__ = [
    "MAX_FRAME",
    "Closed",
    "Session",
    "SessionFailed",
    "Unavailable",
    "canonical",
    "read_frame",
    "read_json",
    "write_frame",
    "write_json",
]

#: The largest frame: a call's output, or a request, with room for its header.
MAX_FRAME = 64 * 1024 * 1024 + 64 * 1024
_LENGTH = struct.Struct(">I")


class Closed(Exception):  # noqa: N818 - the other end went away
    """The other end closed the stream, or sent a frame past :data:`MAX_FRAME`."""


def canonical(value: object) -> bytes:
    """``value`` as the ABI's canonical JSON: keys sorted, no insignificant whitespace,
    UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def write_frame(stream: IO[bytes], payload: bytes) -> None:
    stream.write(_LENGTH.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def _exactly(stream: IO[bytes], n: int) -> bytes:
    chunks = []
    while n:
        chunk = stream.read(n)
        if not chunk:
            raise Closed("The stream ended.")
        chunks.append(chunk)
        n -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: IO[bytes], limit: int = MAX_FRAME) -> bytes:
    (length,) = _LENGTH.unpack(_exactly(stream, _LENGTH.size))
    if length > limit:
        raise Closed(f"A frame of {length:,} bytes is over the limit.")
    return _exactly(stream, length)


def write_json(stream: IO[bytes], value: Mapping[str, Any]) -> None:
    write_frame(stream, canonical(value))


def read_json(stream: IO[bytes], limit: int = 1024 * 1024) -> dict[str, Any]:
    raw = read_frame(stream, limit)
    try:
        value = json.loads(raw)
    except ValueError:
        raise Closed("A frame wasn't JSON.") from None
    if not isinstance(value, dict):
        raise Closed("A frame wasn't a JSON object.")
    return value


class Unavailable(Exception):  # noqa: N818 - said as "the sandbox is unavailable"
    """The sandbox couldn't take the export: it's down, full, or hasn't this writer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionFailed(Exception):  # noqa: N818 - the writer failed, as it says
    """The writer failed partway: a trap, a limit, the deadline. ``code`` says which."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Answer:
    status: int
    output: bytes
    notes: list[dict[str, Any]]
    fuel: int


class Session:
    """One export's conversation with a writer's process, over its two streams."""

    def __init__(self, reader: IO[bytes], writer: IO[bytes]) -> None:
        self._in = reader
        self._out = writer

    def open(self, header: Mapping[str, Any]) -> int:
        """Sends the header; returns the ABI the module reads. Raises :class:`Unavailable`
        when the process can't run it."""
        try:
            write_json(self._out, header)
            answer = read_json(self._in)
        except (Closed, OSError) as error:
            raise Unavailable(
                "sandbox_unavailable", f"The sandbox didn't answer: {error}"
            ) from None
        if answer.get("ready") is True:
            abi = answer.get("abi")
            if not isinstance(abi, int) or isinstance(abi, bool):
                raise Unavailable("sandbox_unavailable", "The sandbox didn't say the ABI.")
            return abi
        why = answer.get("error") or answer.get("failed") or {}
        raise Unavailable(
            str(why.get("code") or "sandbox_unavailable"),
            str(why.get("message") or "The sandbox refused the export."),
        )

    def call(self, entry: str, request: bytes) -> Answer:
        """One ABI call. Raises :class:`SessionFailed`, or :class:`Unavailable` if the
        process went away without saying why."""
        try:
            write_json(self._out, {"call": entry})
            write_frame(self._out, request)
            answer = read_json(self._in)
            if "failed" in answer:
                failed = answer["failed"] or {}
                raise SessionFailed(
                    str(failed.get("code") or "trapped"),
                    str(failed.get("message") or "The writer failed."),
                )
            output = read_frame(self._in)
        except (Closed, OSError) as error:
            raise Unavailable(
                "sandbox_unavailable", f"The sandbox stopped answering: {error}"
            ) from None
        status, notes, fuel = answer.get("status"), answer.get("notes"), answer.get("fuel")
        if status not in (0, 1) or not isinstance(notes, list) or not isinstance(fuel, int):
            raise SessionFailed("abi", "The sandbox's answer was malformed.")
        return Answer(status, output, notes, fuel)
