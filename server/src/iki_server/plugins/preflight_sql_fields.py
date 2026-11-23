"""Count changes to declarative fields in the deployed schema (ADR 0181)."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.management.base import CommandError
from psycopg import sql

from iki_server.entities.values import FOREIGN_KEYS, is_list_table
from iki_server.plugins.makemigrations import linear_conversion
from iki_server.plugins.preflight_projection import fill_default, project
from iki_server.plugins.preflight_types import Finding, Record
from iki_server.plugins.preflight_values import field_findings
from iki_spec import helm
from iki_spec.model import FORMATS, Field
from iki_spec.units import UnitError

if TYPE_CHECKING:
    from iki_server.plugins.preflight_sql import Scope


@dataclass(frozen=True)
class Check:
    kind: str
    subject: str
    message: str
    blocking: bool
    predicate: sql.Composable
    include_empty: bool = False


def invalid_value(
    f: Field, value: sql.Composable, *, include_pattern: bool = True
) -> sql.Composable | None:
    """The generated column's bound, membership or text checks; nulls are checked separately."""
    invalid: list[sql.Composable] = []
    if f.type in ("float", "quantity", "int"):
        if f.min is not None:
            invalid.append(sql.SQL("{} < {}").format(value, sql.Literal(f.min)))
        if f.max is not None:
            invalid.append(sql.SQL("{} > {}").format(value, sql.Literal(f.max)))
        if f.type != "int":
            invalid.append(
                sql.SQL("NOT ({} > '-Infinity'::float8 AND {} < 'Infinity'::float8)").format(
                    value, value
                )
            )
    elif f.type == "enum":
        invalid.append(sql.SQL("{} <> ALL({}::text[])").format(value, sql.Literal(list(f.values))))
    elif f.type in ("string", "text"):
        low = 1 if f.min_length is None else f.min_length
        if low:
            invalid.append(sql.SQL("char_length({}) < {}").format(value, sql.Literal(low)))
        if f.max_length is not None:
            invalid.append(sql.SQL("char_length({}) > {}").format(value, sql.Literal(f.max_length)))
        if f.type == "string":
            invalid.append(sql.SQL("{} ~ {}").format(value, sql.Literal(r"[\r\n]")))
        if f.format:
            invalid.append(sql.SQL("{} !~ {}").format(value, sql.Literal(FORMATS[f.format])))
        if f.pattern and include_pattern:
            invalid.append(_pattern(value, f.pattern))
    return sql.SQL(" OR ").join(invalid) if invalid else None


def _pattern(value: sql.Composable, pattern: str) -> sql.Composable:
    return sql.SQL("{} !~ {}").format(value, sql.Literal(f"^(?:{pattern})$"))


def _state_checks(
    old: Field, target: Field | None, after: Mapping[str, Field], value: sql.Composable
) -> Iterable[Check]:
    destination: Field | None
    if target is not None and not target.removed:
        destination = target
        moves = {n: state.moved_to for n, state in target.states.items() if state.removed}
        removed = set(old.live_states) - set(target.live_states)
    else:
        move = target.moved_to if target else None
        destination = after.get(move.field) if move else None
        moves = dict(move.states) if move else {}
        removed = set(old.live_states)
    for state in sorted(removed):
        to = moves.get(state)
        if destination and not destination.removed and to in destination.live_states:
            message = f"Records in {state} move to {destination.name}.{to}."
            blocking = False
        else:
            message = f"Records in {state} need a destination state."
            blocking = True
        yield Check(
            "state",
            old.name,
            message,
            blocking,
            sql.SQL("{} = {}").format(value, sql.Literal(state)),
        )


def _field_checks(
    scope: Scope, name: str, before: dict[str, Field], after: dict[str, Field]
) -> Iterable[Check]:
    old, new = before.get(name), after.get(name)
    if old == new:
        return
    # Normalized lists always read as a list (empty when no rows), and have no scalar column.
    normalized = old is not None and is_list_table(old)
    raw = sql.SQL("TRUE") if normalized else scope.column(name)
    if new is None or new.removed:
        if old and old.type == "state":
            yield from _state_checks(old, new, after, raw)
        return
    if old and (old.type == "state") != (new.type == "state"):
        yield Check(
            "state",
            name,
            "Changing between a state field and another kind requires a new field.",
            True,
            sql.SQL("TRUE"),
            True,
        )
        return
    if old is None and is_list_table(new):
        # Creating the new child table inserts no rows, including when a default
        # supplies a list to newly created records. Existing owners read as [].
        needs_items = bool(
            new.min_items or (new.items and any(r.min for r in new.items.roles.values()))
        )
        if new.required or needs_items:
            message = (
                "Existing records will have empty lists and need items."
                if needs_items
                else "Existing records will have empty lists."
            )
            yield Check("backfill", name, message, needs_items, sql.SQL("TRUE"))
        return

    required = new.required or new.type == "state"
    default = fill_default(old, new)
    if (
        not normalized
        and (old is None or (required and not old.required and old.type != "state"))
        and (required or default is not None)
    ):
        message = (
            f"The migration fills missing values with {default!r}."
            if default is not None
            else "A required field needs a value."
        )
        yield Check("backfill", name, message, default is None, sql.SQL("{} IS NULL").format(raw))
    if new.type == "state":
        if old:
            yield from _state_checks(old, new, after, raw)
        return
    if new.type == "list" or (old is None and default is None):
        return

    present = sql.SQL("TRUE") if default is not None else sql.SQL("{} IS NOT NULL").format(raw)
    if new.type in FOREIGN_KEYS:
        if old and (old.type, old.to, old.role, old.max_size, old.media_types) != (
            new.type,
            new.to,
            new.role,
            new.max_size,
            new.media_types,
        ):
            yield Check(
                "review",
                name,
                "Check the referenced records against the new field requirements.",
                True,
                present,
            )
        return
    if new.type == "json" and new.schema and (old is None or old.schema != new.schema):
        yield Check(
            "review", name, "Check these values against the proposed JSON schema.", True, present
        )
        return

    converted = bool(old and new.type == old.type == "quantity" and old.unit != new.unit)
    if old and converted:
        try:
            scale, offset = linear_conversion(old.unit or "", new.unit or "", name)
        except (CommandError, UnitError, OverflowError) as error:
            yield Check("unit", name, str(error), True, present, True)
            return
        message = f"{old.unit} to {new.unit}: new = old × {scale!r} {offset:+}."
        yield Check("unit", name, message, False, present, True)

    value = project(name, before, after, scope.column)
    if (
        new.type in ("string", "text")
        and new.pattern
        and (old is None or old.pattern != new.pattern)
    ):
        yield Check(
            "pattern",
            name,
            "Stored values fail the proposed pattern.",
            True,
            _pattern(value, new.pattern),
        )
    failed = invalid_value(new, value, include_pattern=False)
    if old and (old.type != new.type or converted):
        lost = sql.SQL("({}) AND ({} IS NULL)").format(present, value)
        failed = lost if failed is None else sql.SQL("({}) OR ({})").format(lost, failed)
    if failed is not None:
        kind = "enum" if new.type == "enum" else "value"
        message = (
            f"Values must be one of {', '.join(new.values)}."
            if new.type == "enum"
            else "Stored values fail the proposed field requirements."
        )
        yield Check(kind, name, message, True, failed)


def field_checks(scope: Scope, old: dict[str, Field], new: dict[str, Field]) -> list[Finding]:
    """One aggregate pass counts the predicates; only positive counts need an example lookup."""
    from iki_server.plugins.preflight_sql_arrays import array_checks

    checks = [
        check
        for name in sorted(old.keys() | new.keys())
        for check in _field_checks(scope, name, old, new)
    ]
    checks.extend(array_checks(scope, old, new))
    counts = scope.counts([c.predicate for c in checks])
    return [
        Finding(
            c.kind,
            c.subject,
            c.message,
            c.blocking,
            count,
            scope.examples(c.predicate) if count else [],
        )
        for c, count in zip(checks, counts, strict=True)
        if count or c.include_empty
    ]


def _helm(field: Field) -> bool:
    return field.format == "helm" or (field.items is not None and field.items.format == "helm")


def _item_needs_check(old: Field | None, new: Field | None) -> bool:
    """Only changes affecting stored row cells or reference items require loading the list."""
    if new is None or new.removed:
        return bool(old and old.type == "state")
    if old is None:
        return new.required or new.has_default or new.type == "state"
    if old.type != new.type or (new.required and not old.required):
        return True
    if new.type == "row":
        return (old.roles, old.closed, old.by) != (new.roles, new.closed, new.by) or any(
            _item_needs_check(old.fields.get(n), new.fields.get(n))
            for n in old.fields.keys() | new.fields.keys()
        )
    constraints = (
        "unit",
        "min",
        "max",
        "min_length",
        "max_length",
        "values",
        "format",
        "pattern",
        "schema",
        "to",
        "role",
        "max_size",
        "media_types",
        "states",
    )
    return any(getattr(old, key) != getattr(new, key) for key in constraints)


def sampled_fields(old: Mapping[str, Field], new: Mapping[str, Field]) -> set[str]:
    """Checks on normalized lists and the HELM grammar need to inspect complete values."""
    out = set()
    for name, f in new.items():
        previous = old.get(name)
        if not previous or f.removed or previous == f:
            continue
        if _helm(f) and not _helm(previous):
            out.add(name)
        if (
            f.type == "list"
            and (is_list_table(f) or is_list_table(previous))
            and (
                previous.type != f.type
                or (previous.min_items, previous.max_items) != (f.min_items, f.max_items)
                or _item_needs_check(previous.items, f.items)
            )
        ):
            out.add(name)
    return out


def projection_inputs(after: Mapping[str, Field], reads: Collection[str]) -> set[str]:
    """A retired state field can supply the value of another field that a rule reads."""
    return set(reads) | {
        n for n, f in after.items() if f.removed and f.moved_to and f.moved_to.field in reads
    }


def sampled_findings(
    old: dict[str, Field], new: dict[str, Field], rows: list[Record], *, reads: set[str]
) -> tuple[list[Finding], list[Record]]:
    """Project the values expression checks read; report only checks outside SQL."""
    sampled = sampled_fields(old, new)
    required = projection_inputs(new, reads | sampled)
    lists = {n for n in sampled if is_list_table(old[n]) or is_list_table(new[n])}
    findings, projected = field_findings(
        {k: v for k, v in old.items() if k in required},
        {k: v for k, v in new.items() if k in required},
        rows,
        verify=lists,
    )
    findings = [
        f
        for f in findings
        if f.subject.split(".", 1)[0] in lists and not (f.subject in lists and f.kind == "backfill")
    ]
    for name in sorted(sampled - lists):
        f = new[name]
        failed = []
        for r in projected:
            value = r.values.get(name)
            items = value if f.type == "list" and isinstance(value, list) else [value]
            if any(isinstance(item, str) and helm.problem(item) is not None for item in items):
                failed.append(r)
        if failed:
            findings.append(
                Finding(
                    "format",
                    name,
                    "Values do not parse as HELM.",
                    True,
                    len(failed),
                    [r.example for r in failed[:5]],
                )
            )
    return findings, projected
