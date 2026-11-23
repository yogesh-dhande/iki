"""One read-only plugin schema comparison (ADR 0181)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Record:
    id: str
    hrid: str
    project: str | None
    registry: str | None
    entry: str | None
    values: dict[str, Any]

    @property
    def example(self) -> str:
        # Records of the same registry entry share an HRID across projects.
        return f"{self.hrid} ({self.id})" if self.entry else self.hrid


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    message: str
    blocking: bool
    count: int
    examples: list[str]
    sampled: bool = False


@dataclass
class TypeReport:
    type: str
    total: int
    checked: int
    sampled: bool
    sampled_checks: list[str]
    findings: list[Finding]


@dataclass(frozen=True)
class Report:
    plugin: str
    version: str
    configuration_revision: str
    candidate_revision: str
    types: list[TypeReport]

    @property
    def blocking(self) -> bool:
        return any(t.sampled or any(f.blocking for f in t.findings) for t in self.types)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "blocking": self.blocking}
