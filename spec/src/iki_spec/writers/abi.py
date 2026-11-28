"""The writer ABIs this Iki runs (ADR 0230), and the request each one is given.

A module says which ABI it reads: ``iki_abi`` returns it. The host runs every ABI from
:data:`OLDEST` to :data:`NEWEST`, and builds each call's request for the module's own ABI
with that ABI's encoder, so a module keeps getting the bytes it was written for. Within an
ABI the request for a record at a revision never changes; that is what makes an export
reproducible. So a new request field is a new ABI, and what changes is that older modules
keep working:

1. give :class:`Record` or :class:`Export` the new value, which every encoder is handed;
2. add the new ABI's encoder to :data:`ENCODERS`, writing the field, and raise
   :data:`NEWEST`;
3. test that the new ABI's request has it and that the older ABIs' requests are byte for
   byte what they were (``spec/tests/test_writer_abi.py``).

An ABI leaves only with a new LTS line, by raising :data:`OLDEST`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "ENCODERS",
    "NEWEST",
    "OLDEST",
    "Abi1",
    "Encoder",
    "Export",
    "Record",
    "encoder",
    "unsupported",
]


@dataclass(frozen=True)
class Export:
    """The file a writer is starting, as the host knows it, whatever the ABI."""

    writer: str
    format: str
    records: int


@dataclass(frozen=True)
class Record:
    """A record at one revision, as the host gathered it for a writer, whatever the ABI:
    the values of the roles its type maps, and the roles it withheld because the person
    exporting can't read what they refer to."""

    id: uuid.UUID
    hrid: str
    name: str
    type: str
    revision: int
    revised_at: datetime
    roles: Mapping[str, Any] = field(default_factory=dict)
    withheld: Sequence[str] = ()


class Encoder(Protocol):
    """One ABI's requests: the JSON each call's request is, before it's made canonical."""

    abi: int

    def begin(self, export: Export) -> dict[str, Any]: ...

    def record(self, record: Record) -> dict[str, Any]: ...

    def end(self) -> dict[str, Any]: ...


class Abi1:
    """ABI 1 (ADR 0230): ``begin`` is ``{abi, format, records, writer}``, ``record`` the
    record's ``{hrid, id, name, revised_at, revision, roles, type, withheld}`` and
    ``end`` is ``{}``."""

    abi = 1

    def begin(self, export: Export) -> dict[str, Any]:
        return {
            "abi": self.abi,
            "format": export.format,
            "records": export.records,
            "writer": export.writer,
        }

    def record(self, record: Record) -> dict[str, Any]:
        stamp = record.revised_at.astimezone(UTC).isoformat(timespec="seconds")
        return {
            "hrid": record.hrid,
            "id": str(record.id),
            "name": record.name,
            "revised_at": stamp.replace("+00:00", "Z"),
            "revision": record.revision,
            "roles": dict(record.roles),
            "type": record.type,
            "withheld": sorted(record.withheld),
        }

    def end(self) -> dict[str, Any]:
        return {}


#: Each ABI this Iki runs, by number: every one from :data:`OLDEST` to :data:`NEWEST`.
ENCODERS: dict[int, Encoder] = {1: Abi1()}
#: The oldest ABI this Iki's LTS line runs, and the newest it runs.
OLDEST = 1
NEWEST = 1


def _range() -> str:
    return f"ABI {OLDEST}" if OLDEST == NEWEST else f"ABIs {OLDEST} to {NEWEST}"


def unsupported(abi: int) -> str | None:
    """Why this Iki can't run a module that reads ``abi``; None when it can."""
    if abi < OLDEST:
        return (
            f"It reads ABI {abi}, which this Iki no longer runs: it runs {_range()}. "
            "Build it against a newer iki-writer."
        )
    if abi > NEWEST:
        return (
            f"It reads ABI {abi}, newer than this Iki runs ({_range()}): it needs a newer "
            "Iki, or a build against an older iki-writer."
        )
    return None


def encoder(abi: int) -> Encoder:
    """The encoder of ``abi``. Raises ValueError when this Iki doesn't run it."""
    if (why := unsupported(abi)) is not None:
        raise ValueError(why)
    return ENCODERS[abi]
