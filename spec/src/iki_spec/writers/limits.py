"""A writer's limits (ADR 0230): Iki's, and a deployment's within them.

Apart from :mod:`iki_spec.writers.runtime`, which needs Wasmtime, so the server's settings
can check a deployment's limits without loading it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MiB = 1024 * 1024
#: The most memory a writer may have; a plugin asks for less with ``memory``.
MAX_MEMORY = 256 * MiB
DEFAULT_MEMORY = 64 * MiB
#: Fuel each call gets: a base, and more for each byte of its request.
FUEL_BASE = 1_000_000_000
FUEL_PER_BYTE = 2_000
#: The most bytes one call may add to a file, and the largest request.
MAX_OUTPUT = 64 * MiB
MAX_REQUEST = 64 * MiB
#: Notes one call may make, and how long each may be.
MAX_NOTES = 1_000
MAX_NOTE_CHARS = 500


@dataclass(frozen=True)
class Limits:
    """A deployment's limits on a writer's calls (ADR 0230): Iki's by default. A deployment
    may lower any of them, and may raise the fuel; the server says which in each export's
    header, and the sandbox enforces them."""

    #: Fuel each call gets, and more for each byte of its request.
    fuel_base: int = FUEL_BASE
    fuel_per_byte: int = FUEL_PER_BYTE
    #: The most bytes one call may add to a file, and the largest request.
    max_output: int = MAX_OUTPUT
    max_request: int = MAX_REQUEST
    #: Notes one call may make, and how long each may be.
    max_notes: int = MAX_NOTES
    max_note_chars: int = MAX_NOTE_CHARS

    def __post_init__(self) -> None:
        for name in ("fuel_base", "fuel_per_byte", "max_output", "max_request", "max_notes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"A writer's {name} is a whole number, not {value!r}.")
        if self.fuel_base < 1:
            raise ValueError("A writer's fuel_base is at least 1.")
        for name, most in (
            ("max_output", MAX_OUTPUT),
            ("max_request", MAX_REQUEST),
            ("max_notes", MAX_NOTES),
            ("max_note_chars", MAX_NOTE_CHARS),
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= most:
                raise ValueError(
                    f"A writer's {name} may be lowered, not raised: from 1 to {most:,}, "
                    f"not {value!r}."
                )

    @classmethod
    def of(cls, given: Any) -> Limits:  # noqa: ANN401 - JSON
        """As an export's header gives them: its keys, each Iki's when left out. Raises
        :class:`ValueError`."""
        if given is None:
            return DEFAULT_LIMITS
        if not isinstance(given, dict) or set(given) - set(cls.__dataclass_fields__):
            raise ValueError("A writer's limits are an object of the limits this Iki has.")
        return cls(**given)

    def header(self) -> dict[str, int]:
        """As an export's header gives them: only those that aren't Iki's."""
        return {
            name: getattr(self, name)
            for name, field in self.__dataclass_fields__.items()
            if getattr(self, name) != field.default
        }


DEFAULT_LIMITS = Limits()
