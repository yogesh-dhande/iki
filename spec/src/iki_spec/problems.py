"""Problems found in plugin manifests: what's wrong, where, and how to fix it."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: A position inside a document: mapping keys and list indexes from its root.
Path = tuple[str | int, ...]


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, order=True)
class Location:
    """A 1-based line and column in a file."""

    line: int
    column: int


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a plugin.

    `code` is a stable, documented identifier (for example `unknown-unit`),
    so tools can filter on it. `file` is relative to the plugin's directory.
    """

    code: str
    message: str
    severity: Severity = Severity.ERROR
    file: str | None = None
    location: Location | None = None
    path: Path = ()
    hint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR

    def sort_key(self) -> tuple[str, int, int, str]:
        line, column = (self.location.line, self.location.column) if self.location else (0, 0)
        return (self.file or "", line, column, self.code)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "hint": self.hint,
            "file": self.file,
            "line": self.location.line if self.location else None,
            "column": self.location.column if self.location else None,
            "path": list(self.path),
        }


def describe_path(path: Path) -> str:
    """`fields.name.type` style, for messages."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += ("." if out else "") + part
    return out
