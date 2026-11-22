"""Compare sampled current values to proposed fields, projecting generated migrations (ADR 0181)."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import replace
from typing import Any
from uuid import UUID

from django.core.management.base import CommandError
from django.db import connection

from iki_server.plugins.makemigrations import linear_conversion
from iki_server.plugins.preflight_types import Finding, Record
from iki_spec import units
from iki_spec.model import EntityType, Field
from iki_spec.units import UnitError
from iki_spec.validate import value_problems


def amount_unit(lineage: Iterable[EntityType]) -> str | None:
    """A record's ledger unit comes from the nearest declaration (amounts.services.spec)."""
    return next((t.amount.unit for t in lineage if t.amount is not None), None)


def amount_findings(
    old_unit: str | None, new_unit: str | None, rows: list[Record]
) -> list[Finding]:
    """A change to ledger metadata generates no conversion for existing balances (ADR 0139)."""
    from iki_server.amounts.models import Amount

    if old_unit == new_unit:
        return []
    records = {UUID(r.id): r for r in rows}
    ledger_rows: dict[str, list[Record]] = defaultdict(list)
    for pk, unit in Amount.objects.filter(pk__in=records).values_list("pk", "unit"):
        if unit != new_unit:
            ledger_rows[unit].append(records[pk])
    review = _Review()
    for unit, affected in sorted(ledger_rows.items()):
        if new_unit is None:
            message = (
                "The proposed type removes the amount declaration. "
                f"These ledgers still hold balances in {unit}."
            )
        else:
            message = (
                f"These ledgers stay in {unit}. Changing existing balances to {new_unit} "
                "requires a data migration."
            )
            try:
                factor = units.convert(1, unit, new_unit)
            except UnitError:
                message += " The units have no direct conversion."
            else:
                message += f" 1 {unit} equals {factor!r} {new_unit}."
        review.add("unit", "amount", message, True, sorted(affected, key=lambda r: (r.hrid, r.id)))
    if not ledger_rows and new_unit is not None:
        previous = f" (formerly {old_unit})" if old_unit else ""
        review.add(
            "unit",
            "amount",
            f"New balances are recorded in {new_unit}{previous}.",
            False,
            (),
            include_empty=True,
        )
    return review.findings


def field_findings(
    old_fields: dict[str, Field], proposed_fields: dict[str, Field], rows: list[Record]
) -> tuple[list[Finding], list[Record]]:
    """The records touched by each change and the projected values for checking rules.

    A record appears once per finding even when several items of one of its lists fail.
    The copy preserves the currently stored values for other parts of the report.
    """
    projected = [replace(row, values=dict(row.values)) for row in rows]
    review = _Review()
    review.fields(old_fields, proposed_fields, projected)
    return review.findings, projected


class _Review:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(
        self,
        kind: str,
        subject: str,
        message: str,
        blocking: bool,
        rows: Iterable[Record],
        *,
        include_empty: bool = False,
    ) -> None:
        examples = {row.id: row.example for row in rows}
        if examples or include_empty:
            self.findings.append(
                Finding(
                    kind, subject, message, blocking, len(examples), list(examples.values())[:5]
                )
            )

    def fields(
        self,
        before: dict[str, Field],
        after: dict[str, Field],
        rows: list[Record],
        prefix: str = "",
    ) -> None:
        for name, field in after.items():
            previous = before.get(name)
            if field.removed or previous == field:
                continue
            self.field(
                previous if previous and not previous.removed else None, field, rows, prefix + name
            )

        # An entire state field can be retired into another one. Its moves follow any
        # initialization of the destination column, as in RetireStateField.
        for name, old in before.items():
            target = after.get(name)
            if old.removed or (target is not None and not target.removed):
                continue
            if old.type == "state":
                move = target.moved_to if target else None
                destination = after.get(move.field) if move else None
                destinations = (
                    {
                        source: state
                        for source, state in move.states
                        if state in destination.live_states
                    }
                    if move and destination and not destination.removed
                    else {}
                )
                self.states(name, old.live_states, rows, move.field if move else name, destinations)
            for row in rows:
                row.values.pop(name, None)

    def states(
        self,
        name: str,
        removed: Iterable[str],
        rows: list[Record],
        destination: str,
        moves: dict[str, str],
    ) -> None:
        for state in sorted(removed):
            affected = [r for r in rows if r.values.get(name) == state]
            target = moves.get(state)
            message = (
                f"Records in {state} move to {destination}.{target}."
                if target
                else f"Records in {state} need a destination state."
            )
            self.add("state", name, message, not target, affected)
            if target:
                for row in affected:
                    row.values[destination] = target

    def field(self, old: Field | None, field: Field, rows: list[Record], subject: str) -> None:
        name = field.name
        if old and (old.type == "state") != (field.type == "state"):
            self.add(
                "state",
                subject,
                "Changing between a state field and another kind requires a new field.",
                True,
                rows,
                include_empty=True,
            )
            return
        required = field.required or field.type == "state"
        if old is None or (required and not old.required and old.type != "state"):
            default = (
                field.initial
                if field.type == "state"
                else (field.default if field.has_default else None)
            )
            missing = [r for r in rows if r.values.get(name) is None]
            if default is not None:
                self.add(
                    "backfill",
                    subject,
                    f"The migration fills missing values with {default!r}.",
                    False,
                    missing,
                )
                for row in missing:
                    row.values[name] = default
            elif required:
                self.add("backfill", subject, "A required field needs a value.", True, missing)

        present = [row for row in rows if row.values.get(name) is not None]
        if field.type == "state" and old and old.type == "state":
            moves = {
                n: st.moved_to
                for n, st in field.states.items()
                if st.removed and st.moved_to and st.moved_to in field.live_states
            }
            self.states(name, set(old.live_states) - set(field.live_states), present, name, moves)
            return

        if field.type == "list" and field.items:
            self.items(old, field, present, subject)
            return

        if field.type == "quantity" and old and old.type == "quantity" and old.unit != field.unit:
            present = self.convert(old, field, present, subject)

        if field.type == "json" and field.schema and (old is None or old.schema != field.schema):
            self.add(
                "review",
                subject,
                "Check these values against the proposed JSON schema.",
                True,
                present,
            )
            return

        # The targets of a reference or a file, or the lab role of a person, need
        # related data. The stored ID alone cannot establish that they will pass.
        if field.type in ("ref", "person", "file"):
            if old and (old.type, old.to, old.role, old.max_size, old.media_types) != (
                field.type,
                field.to,
                field.role,
                field.max_size,
                field.media_types,
            ):
                self.add(
                    "review",
                    subject,
                    "Check the referenced records against the new field requirements.",
                    True,
                    present,
                )
            return

        if field.pattern and (old is None or old.pattern != field.pattern):
            self.pattern(field, subject, present)

        # User-authored patterns use PostgreSQL's engine above, bounded by the
        # preflight transaction's statement timeout. Only known formats run here.
        scalar = replace(field, pattern=None) if field.pattern else field
        failed = (r for r in present if _invalid(scalar, r.values[name]))
        if field.type == "enum":
            self.add(
                "enum", subject, f"Values must be one of {', '.join(field.values)}.", True, failed
            )
        else:
            self.add(
                "value",
                subject,
                "Stored values fail the proposed field requirements.",
                True,
                failed,
            )

    def pattern(self, field: Field, subject: str, rows: list[Record]) -> None:
        candidates = [r for r in rows if isinstance(r.values[field.name], str)]
        if not candidates:
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT i FROM unnest(%s::text[]) WITH ORDINALITY AS candidate(value, i) "
                "WHERE value !~ %s ORDER BY i",
                [[r.values[field.name] for r in candidates], f"^(?:{field.pattern})$"],
            )
            failed = [candidates[i - 1] for (i,) in cursor.fetchall()]
        self.add("pattern", subject, "Stored values fail the proposed pattern.", True, failed)

    def convert(self, old: Field, field: Field, rows: list[Record], subject: str) -> list[Record]:
        assert old.unit and field.unit  # noqa: S101 - validated quantities have canonical units
        try:
            scale, offset = linear_conversion(old.unit, field.unit, subject)
        except (CommandError, UnitError, OverflowError) as error:
            self.add("unit", subject, str(error), True, rows, include_empty=True)
            return []
        self.add(
            "unit",
            subject,
            f"{old.unit} to {field.unit}: new = old × {scale!r} {offset:+}.",
            False,
            rows,
            include_empty=True,
        )
        valid = []
        failed = []
        for row in rows:
            value = row.values[field.name] * scale + offset
            if math.isfinite(value):
                row.values[field.name] = value
                valid.append(row)
            else:
                row.values[field.name] = None
                failed.append(row)
        self.add(
            "value",
            subject,
            "The converted quantity exceeds the range of a finite number.",
            True,
            failed,
        )
        return valid

    def items(self, old: Field | None, field: Field, rows: list[Record], subject: str) -> None:
        item = field.items
        assert item is not None  # noqa: S101 - the caller checked
        name = field.name
        arrays = [r for r in rows if isinstance(r.values[name], list)]
        self.add(
            "value",
            subject,
            "Values must be lists within the proposed length bounds.",
            True,
            (
                r
                for r in rows
                if not isinstance(r.values[name], list)
                or (field.min_items is not None and len(r.values[name]) < field.min_items)
                or (field.max_items is not None and len(r.values[name]) > field.max_items)
            ),
        )
        previous = old.items if old and old.type == "list" else None
        if previous == item:
            return
        # Flatten a list of items to check its changed fields in a batch. Keep their
        # owning record on each item, and restore their order in the projection.
        for row in arrays:
            row.values[name] = list(row.values[name])
        slots = [(r, i) for r in arrays for i in range(len(r.values[name]))]
        if item.type == "row":
            malformed = [r for r, i in slots if not isinstance(r.values[name][i], dict)]
            self.add(
                "value",
                subject,
                "List items must contain the proposed row fields.",
                True,
                malformed,
            )
            slots = [(r, i) for r, i in slots if isinstance(r.values[name][i], dict)]
            items = [replace(r, values=dict(r.values[name][i])) for r, i in slots]
            self.fields(previous.fields if previous else {}, item.fields, items, subject + ".")
            if previous and (item.roles, item.closed, item.by) != (
                previous.roles,
                previous.closed,
                previous.by,
            ):
                self.add(
                    "review",
                    subject,
                    "Check these rows against the proposed role requirements.",
                    True,
                    arrays,
                )
            for (r, i), cell in zip(slots, items, strict=True):
                r.values[name][i] = cell.values
        else:
            items = [replace(r, values={"item": r.values[name][i]}) for r, i in slots]
            self.field(previous, replace(item, name="item"), items, subject)
            for (r, i), cell in zip(slots, items, strict=True):
                r.values[name][i] = cell.values["item"]


def _invalid(field: Field, value: Any) -> bool:  # noqa: ANN401 - a stored value
    if isinstance(value, (dt.date, dt.datetime)):
        value = value.isoformat()
    if field.type == "datetime":
        try:
            return not isinstance(value, str) or dt.datetime.fromisoformat(value).tzinfo is None
        except ValueError:
            return True
    return (
        field.type in ("float", "quantity")
        and isinstance(value, float)
        and not math.isfinite(value)
    ) or bool(value_problems(field, value))
