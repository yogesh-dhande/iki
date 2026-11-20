"""What a run's ``creates`` may say, as the server checks it before writing anything (ADR
0044): the checks that need only the action and the types, not a database.

The server (:mod:`iki_server.runs.results`) and ``iki plugin test`` (ADR 0093) both run
these, so a plugin's action test fails where a real run's output would be refused:

- each create is of a type the action declares it creates (or a subtype), no more than
  its cardinality allows
- each key is used once, and a ``{"$create": key}`` names an earlier create's
- ``fills`` names values the create gives, of its registry entry's facts, not its identity
- a creates file's line (ADR 0056) is of a type the action declares, many of, refers only
  to the keys of ``creates``, and has no key or ``fills`` of its own

Each problem is reported with a JSON pointer into the output, as ``#/creates/0/fills/1``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from iki_spec import schemas
from iki_spec.model import Action, Creates, EntityType, Extension, Field, split_entry

__all__ = [
    "Problem",
    "SpecTypes",
    "Types",
    "check_creates",
    "check_creates_file",
    "created_refs",
    "creates_line",
    "declared",
    "fills",
    "keys",
    "pointer",
]

#: Says what's wrong, and where: a JSON pointer and a detail.
Report = Callable[[str, str], None]


@dataclass(frozen=True)
class Problem:
    pointer: str
    detail: str


class Types(Protocol):
    """What the checks need of the types a deployment has installed."""

    def is_a(self, type_name: str, wanted: str) -> bool:
        """Whether ``type_name`` is ``wanted`` or one of its subtypes."""
        ...

    def ref_fields(self, type_name: str) -> frozenset[str]:
        """Its ref fields and lists of refs; none if it isn't installed."""
        ...

    def fillable(self, type_name: str) -> tuple[frozenset[str], frozenset[str]] | None:
        """Its identity's fields, and its registry entry's; None if it isn't installed."""
        ...


def pointer(*parts: object) -> str:
    """A JSON pointer into the output, as problems name where they are."""
    return "#/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _is_created_ref(value: object) -> bool:
    return (
        isinstance(value, dict) and set(value) == {"$create"} and isinstance(value["$create"], str)
    )


def created_refs(item: Mapping[str, Any], types: Types) -> list[tuple[str, str, tuple[int, ...]]]:
    """Each ``{"$create": key}`` in a create's ref fields (and lists of refs): (field, key,
    list index if any)."""
    found: list[tuple[str, str, tuple[int, ...]]] = []
    type_name = item.get("type")
    refs = types.ref_fields(type_name) if isinstance(type_name, str) else frozenset()
    for name, value in (item.get("values") or {}).items():
        if name not in refs:
            continue
        if _is_created_ref(value):
            found.append((name, value["$create"], ()))
        elif isinstance(value, list):
            found.extend(
                (name, v["$create"], (i,)) for i, v in enumerate(value) if _is_created_ref(v)
            )
    return found


def declared(
    action: Action,
    item: Mapping[str, Any],
    at: tuple[object, ...],
    counts: dict[int, int] | None,
    types: Types,
    report: Report,
) -> tuple[int, Creates] | None:
    """Which of the action's declared creates the item is, and its number: None, reported,
    if it declares none of its type, or the item is one more than its cardinality allows.
    ``counts``: how many of each so far; None for a creates file's line, which can't be one
    of a cardinality of one."""
    found = next(
        ((n, c) for n, c in enumerate(action.creates) if types.is_a(item["type"], c.type)),
        None,
    )
    if found is None:
        allowed = ", ".join(c.type for c in action.creates) or "nothing"
        report(
            pointer(*at, "type"),
            f"The action doesn't declare that it creates {item['type']} (it creates: {allowed}).",
        )
        return None
    number, effect = found
    if effect.cardinality == "one":
        if counts is None:
            report(
                pointer(*at),
                f"The action creates at most one {effect.type} per run: give it in `creates`.",
            )
            return None
        counts[number] = counts.get(number, 0) + 1
        if counts[number] > 1:
            report(pointer(*at), f"The action creates at most one {effect.type} per run.")
            return None
    return found


def fills(
    item: Mapping[str, Any], at: tuple[object, ...], types: Types, report: Report
) -> frozenset[str]:
    """The fields a create's ``fills`` names: each one of its values, and its entry's."""
    names = frozenset(item.get("fills", ()))
    found = types.fillable(item["type"]) if names else None
    if found is None:
        return names
    identity, entry = found
    for number, name in enumerate(item.get("fills", ())):
        if name not in item["values"]:
            report(pointer(*at, "fills", number), f"It gives no value for `{name}`.")
        elif name in identity:
            report(
                pointer(*at, "fills", number),
                f"`{name}` is its identity, which an entry registered already has.",
            )
        elif name not in entry:
            report(
                pointer(*at, "fills", number),
                f"`{name}` isn't its registry entry's, so there's nothing to fill.",
            )
    return names


def keys(
    item: Mapping[str, Any], at: tuple[object, ...], seen: set[str], types: Types, report: Report
) -> None:
    """A create's ``$create`` references, each to an earlier create's key, and its own key,
    which no earlier create has. ``seen``: the earlier creates' keys, which it adds to."""
    for name, key, where in created_refs(item, types):
        if key not in seen:
            report(
                pointer(*at, "values", name, *where),
                f"No earlier create has the key {key!r}: a create can refer only to those "
                "before it.",
            )
    if "key" in item:
        if item["key"] in seen:
            report(pointer(*at, "key"), f"Another create has the key {item['key']!r}.")
        seen.add(item["key"])


def check_creates(
    action: Action, creates: Sequence[Mapping[str, Any]], types: Types
) -> list[Problem]:
    """What's wrong with an output's ``creates``, as the server would refuse it, of what
    needs no database."""
    out: list[Problem] = []

    def report(at: str, detail: str) -> None:
        out.append(Problem(at, detail))

    counts: dict[int, int] = {}
    seen: set[str] = set()
    for index, item in enumerate(creates):
        at = ("creates", index)
        if not isinstance(item.get("type"), str) or not isinstance(item.get("values"), dict):
            continue  # the output's schema says so
        keys(item, at, seen, types, report)
        if declared(action, item, at, counts, types, report) is not None:
            fills(item, at, types, report)
    return out


def creates_line(
    action: Action,
    item: Mapping[str, Any],
    at: tuple[object, ...],
    keys: frozenset[str],
    types: Types,
    report: Report,
) -> tuple[int, Creates] | None:
    """Which of the action's declared creates a line of a creates file (ADR 0056) is, as
    :func:`declared` says; None, reported, if it isn't one. ``keys``: those of the output's
    ``creates``, the only ones a line may refer to (ADR 0140)."""
    for name, key, where in created_refs(item, types):
        if key not in keys:
            report(
                pointer(*at, "values", name, *where),
                f"No create in `creates` has the key {key!r}: a line can refer only to those.",
            )
            return None
    found = declared(action, item, at, None, types, report)
    if found is None:
        return None
    if "key" in item:
        report(
            pointer(*at, "key"),
            "A creates file's lines can't refer to one another: give the creates they "
            "refer to in `creates`, with a `key`, and refer to them with `$create`.",
        )
        return None
    if "fills" in item:
        report(
            pointer(*at, "fills"),
            "A creates file's lines can't fill entries: give the creates that do in `creates`.",
        )
        return None
    return found


def check_creates_file(
    action: Action,
    lines: Iterable[str | bytes],
    creates: Sequence[Mapping[str, Any]],
    types: Types,
) -> list[Problem]:
    """What's wrong with a creates file's lines (ADR 0056), as the server would refuse them,
    of what needs no database: each line JSON, a create as ``run-output`` has them, and one
    :func:`creates_line` accepts. Problems point at ``#/creates_file/<line>``, from 1;
    blank lines are skipped. ``creates``: the output's own."""
    out: list[Problem] = []

    def report(at: str, detail: str) -> None:
        out.append(Problem(at, detail))

    keys = frozenset(c["key"] for c in creates if isinstance(c.get("key"), str))
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        at = ("creates_file", number)
        try:
            item = json.loads(raw)
        except ValueError as error:
            report(pointer(*at), f"The line isn't JSON: {error}.")
            continue
        issues = schemas.check("run-output", item, part="create")
        for issue in issues:
            report(pointer(*at, *issue.path), issue.message)
        if not issues:
            creates_line(action, item, at, keys, types, report)
    return out


class SpecTypes:
    """:class:`Types` from manifests: a plugin's types, its dependencies' and the core
    plugin's, and their extensions."""

    def __init__(self, types: Mapping[str, EntityType], extensions: Sequence[Extension]) -> None:
        self.types = types
        self.extensions = extensions

    def _chain(self, type_name: str) -> list[EntityType]:
        """The type and its parents, nearest first, without ``iki/core.Entity``."""
        out: list[EntityType] = []
        current = self.types.get(type_name)
        while current is not None and current.qualified_name != "iki/core.Entity":
            out.append(current)
            current = self.types.get(current.extends) if current.extends else None
        return out

    def is_a(self, type_name: str, wanted: str) -> bool:
        if wanted in (type_name, "iki/core.Entity"):
            return True
        return any(t.qualified_name == wanted for t in self._chain(type_name)[1:])

    def _fields(self, type_name: str) -> list[tuple[dict[str, Field], tuple[str, ...]]]:
        chain = self._chain(type_name)
        names = {t.qualified_name for t in chain}
        own = [(t.fields, t.identity) for t in chain]
        return own + [(e.fields, ()) for e in self.extensions if e.extends_type in names]

    def ref_fields(self, type_name: str) -> frozenset[str]:
        return frozenset(
            name
            for fields, _ in self._fields(type_name)
            for name, f in fields.items()
            if not f.removed
            and (
                f.type == "ref"
                or (f.type == "list" and f.items is not None and f.items.type == "ref")
            )
        )

    def fillable(self, type_name: str) -> tuple[frozenset[str], frozenset[str]] | None:
        chain = self._chain(type_name)
        if not chain:
            return None
        identity = next((t.identity for t in chain if t.identity), ())
        entry = frozenset(
            name
            for fields, own in self._fields(type_name)
            for name, f in split_entry(fields, own)[1].items()
            if not f.removed
        )
        return frozenset(identity), entry
