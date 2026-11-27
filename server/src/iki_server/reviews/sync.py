"""Installs each plugin's review procedures, and ties reviewed records to them (ADR 0302).

On every deploy (``sync_plugins``), each procedure version a plugin declares is kept in
:class:`~iki_server.reviews.models.Procedure` with its flows and their states. A version
already installed must be declared as it was: a plugin changes a procedure in a new
version, so records pinned to the old one keep the procedure they were made under. One
that's no longer declared stays, as do its records.

Then each reviewed type's own table gets a composite foreign key from its
``review_procedure``, ``review_flow`` and review state to the installed flow states (or,
for a type that only opens work, to the flows), and a check that every row is pinned.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from psycopg import sql

from iki_server.entities import registry
from iki_server.reviews.models import Flow, FlowState, Procedure
from iki_spec.model import EntityType, ReviewProcedure, procedure_from_dict, procedure_to_dict

__all__ = ["Changed", "Table", "install", "install_procedures", "tables"]

#: The constraints' names start with this; those no table needs are dropped.
PREFIX = "iki_review__"


class Changed(Exception):  # noqa: N818 - reads as "the procedure changed"
    """A plugin declares an installed procedure version otherwise than it was."""


#: The procedure's parts whose order is part of it: slots and states as it declares them,
#: which is the order setup lists and the flow is drawn in, and its flows as offered.
ORDERED = ("slots", "states", "flows")


def definition(procedure: ReviewProcedure) -> dict[str, Any]:
    """What's kept of a procedure version: all it declares, but not where its file is. A
    JSON column doesn't keep its keys' order, so the order of each part is kept too."""
    data = procedure_to_dict(procedure)
    data.pop("file", None)
    data["order"] = {part: list(data[part]) for part in ORDERED}
    return data


def from_definition(data: Mapping[str, Any]) -> ReviewProcedure:
    """The procedure version :func:`definition` kept, its parts in its own order."""
    order = data["order"]
    return procedure_from_dict(
        {
            **{k: v for k, v in data.items() if k != "order"},
            **{part: {name: data[part][name] for name in order[part]} for part in ORDERED},
            "file": None,
        }
    )


def digest(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def declared() -> dict[str, ReviewProcedure]:
    """Every procedure version the installed plugins declare, by qualified name."""
    return {q: p for plugin in registry.plugins().values() for q, p in plugin.procedures.items()}


@transaction.atomic
def install_procedures() -> list[str]:
    """Keeps each declared version, refusing one declared otherwise than it was installed.
    Returns the newly installed versions: all of them, or none."""
    installed = {p.qualified: p.digest for p in Procedure.objects.select_for_update()}
    added: list[str] = []
    for qualified, procedure in sorted(declared().items()):
        data = definition(procedure)
        found = digest(data)
        if qualified in installed:
            if installed[qualified] != found:
                raise Changed(
                    f"{qualified} is installed, and {procedure.plugin} now declares it "
                    "otherwise. Records are pinned to it as it was: declare the change as "
                    f"version {procedure.version + 1}, and keep version {procedure.version} "
                    "as it is."
                )
            continue
        row = Procedure.objects.create(
            qualified=qualified,
            plugin=procedure.plugin,
            name=procedure.name,
            version=procedure.version,
            label=procedure.label,
            definition=data,
            digest=found,
        )
        for name, flow in procedure.flows.items():
            Flow.objects.create(procedure=row, name=name, label=flow.label)
            FlowState.objects.bulk_create(
                FlowState(
                    procedure=row,
                    flow=name,
                    state=state,
                    locks=procedure.states[state].locks,
                )
                for state in flow.states
            )
        added.append(qualified)
    return added


@dataclass(frozen=True)
class Table:
    """A reviewed type's own table, and its columns that hold the pin."""

    name: str
    procedure: str
    flow: str
    #: The review state's column; None for a type that only opens work.
    state: str | None

    @property
    def constraint(self) -> str:
        return PREFIX + hashlib.sha256(self.name.encode()).hexdigest()[:20]

    @property
    def definition(self) -> str:
        if self.state is None:
            return (
                f"FOREIGN KEY ({self.procedure}, {self.flow}) REFERENCES "
                "iki_review_flow(procedure_id, name)"
            )
        return (
            f"FOREIGN KEY ({self.procedure}, {self.flow}, {self.state}) REFERENCES "
            "iki_review_flow_state(procedure_id, flow, state)"
        )


def _owners() -> Iterator[EntityType]:
    """The types that declare ``review``: their own tables hold their lineage's pins."""
    for t in registry.types().values():
        if t.review is not None and t.qualified_name != "iki/core.Entity":
            yield t


def _column(model: Any, name: str) -> str:  # noqa: ANN401 - a generated model
    return str(model._meta.get_field(name).column)


def tables() -> list[Table]:
    out = []
    for t in _owners():
        assert t.review is not None  # noqa: S101 - _owners are reviewed
        model = registry.model(t.qualified_name)
        out.append(
            Table(
                name=model._meta.db_table,
                procedure=_column(model, "review_procedure"),
                flow=_column(model, "review_flow"),
                state=None if t.review.opens else _column(model, t.review.field),
            )
        )
    return out


def install(cursor: Any, wanted: Iterable[Table]) -> list[str]:  # noqa: ANN401 - Django's cursor
    """Each of ``wanted``'s foreign key and check, made where new or changed; drops the
    rest. Rows that don't fit are refused by Postgres as the key is made."""
    cursor.execute(
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
        # Not LIKE, whose `_` matches any character: the reviews app's own tables are
        # iki_reviews_*, and Postgres names their NOT NULL constraints too.
        "FROM pg_constraint WHERE starts_with(conname, %s)",
        [PREFIX],
    )
    current: dict[str, tuple[str, str]] = {row[1]: (row[0], row[2]) for row in cursor.fetchall()}
    needed: dict[str, Table] = {}
    for t in wanted:
        needed[t.constraint] = t
        needed[t.constraint + "_pinned"] = t
    for name in sorted(set(current) - set(needed)):
        table, _ = current[name]
        cursor.execute(
            sql.SQL("ALTER TABLE {} DROP CONSTRAINT IF EXISTS {}").format(
                sql.SQL(table), sql.Identifier(name)
            )
        )
    for t in {t.name: t for t in needed.values()}.values():
        pinned = (
            f"CHECK ((({t.procedure} IS NOT NULL) AND ({t.flow} IS NOT NULL)"
            + (f" AND ({t.state} IS NOT NULL)" if t.state else "")
            + "))"
        )
        for name, definition in (
            (t.constraint, f"{t.definition} ON DELETE RESTRICT"),
            (t.constraint + "_pinned", pinned),
        ):
            existing = current.get(name)
            if existing is not None and _same(existing[1], definition):
                continue
            cursor.execute(
                sql.SQL("ALTER TABLE {} DROP CONSTRAINT IF EXISTS {}").format(
                    sql.Identifier(t.name), sql.Identifier(name)
                )
            )
            cursor.execute(
                sql.SQL("ALTER TABLE {} ADD CONSTRAINT {} " + definition).format(
                    sql.Identifier(t.name), sql.Identifier(name)
                )
            )
    return sorted({t.name for t in needed.values()})


def _same(found: str, wanted: str) -> bool:
    """Whether Postgres's rendering of a constraint is the one wanted, ignoring spacing,
    quoting and parentheses, which it adds and removes."""

    def plain(text: str) -> str:
        return "".join(c for c in text.lower() if c not in ' "()')

    return plain(found) == plain(wanted)
