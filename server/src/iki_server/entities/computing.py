"""Computed fields worked out by an expression (ADR 0297), at every save.

A field declared ``computed: {cel: …}`` holds what its CEL expression gives for the record
as it will be saved: its own values, its ancestors' and its extensions', never what its
references refer to, its registry entry's, its states or the clock, which the validator
refuses. So the value a revision holds can be worked out again from that revision.

Every write of a record's values works its computed fields out before its rules are
checked (:mod:`iki_server.entities.checking`), in the same module that evaluates them:

- :func:`fill`, for a create (``services.prepare``) and a chunk of a bulk or spreadsheet
  import (``runs.imports.breaking``), with the record's new values;
- :func:`changes`, for an edit or a run's results (``services.update``), with what the
  edit changes over what the record holds;
- :func:`rework`, for a deploy whose plugins changed what a type's expressions are, and for
  a bundle's records once they're written: each record that isn't locked, and whose value
  differs, gets a ``migrated`` revision by no one, via the system, as a plugin migration's
  values do. A locked record keeps the value it was locked with: what was signed or
  released stays as it was, and it takes the new value at its first save once it's
  unlocked.

An expression that reads an empty value, or can't be worked out (a division by zero, text
that isn't one of an enum's values), leaves the field empty, as a rule that reads an empty
value passes (ADR 0127). A value its field's limits refuse (``min``, ``max``, a length) is
refused as a typed one would be, naming the field.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import structlog

from iki_server.entities import registry
from iki_server.entities.values import Invalid, Refs, shape, to_db
from iki_spec import rules as spec_rules
from iki_spec.model import Field

__all__ = [
    "REFUSED",
    "Reworked",
    "changes",
    "digest_of",
    "fields_of",
    "fill",
    "reason",
    "rework",
    "rework_brought",
]

log = structlog.get_logger(__name__)

#: Records worked out again at a time, by a deploy or a bundle.
CHUNK = 500


@cache
def fields_of(type_name: str) -> tuple[Field, ...]:
    """A type's fields worked out by an expression, its ancestors' and extensions'
    included, each after those it reads."""
    fields = shape(type_name).fields()
    return tuple(fields[n] for n in spec_rules.computed_order(fields))


@cache
def _variables(type_name: str) -> dict[str, Any]:
    return spec_rules.computed_variables(
        registry.types()[type_name].label, shape(type_name).fields()
    )


def digest_of(type_name: str) -> str:
    """What a type's records' computed values are worked out by: its expressions, and the
    versions of the plugins whose fields they're on or read (a migration of an extension's
    values may have changed what they read)."""
    worked = fields_of(type_name)
    form = shape(type_name)
    names = {f.name for f in worked} | {n for f in worked for n in f.cel_reads}
    plugins = registry.plugins()
    owners = sorted({form.part_of(n).plugin for n in names if n in form.fields()})
    data = {
        "fields": [[f.name, f.type, f.unit, f.cel] for f in worked],
        "plugins": [[p, plugins[p].version] for p in owners if p in plugins],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


#: Why no one, person or run, sets a field worked out by an expression.
REFUSED = "is worked out by Iki from the record's other values: no one sets it."


def _stored(f: Field, result: Any) -> Any:  # noqa: ANN401 - JSON in, a model value out
    """An expression's value as its field stores it: a time given to a date field is the
    date it falls on in UTC. Raises ``ValueError`` with what its field refuses."""
    from iki_server.entities.models import Entity

    if result is None:
        return None
    if f.type == "date" and isinstance(result, str):
        result = result[:10]
    return to_db(f, result, Refs(Entity.objects.none()))


def _work_out(
    type_name: str,
    getters: Sequence[Callable[[str], Any]],
    *,
    raising: bool = True,
) -> list[dict[str, Any]]:
    """Each record's computed values, as stored, for records whose values ``getters``
    give (as stored). One evaluation per field, for every record at once, each field after
    those it reads. Raises :class:`Invalid` with a value its field refuses, naming the
    record's place in ``getters`` when there's more than one; or, with ``raising`` off,
    leaves such a value as it was."""
    from iki_server.entities.checking import cel_value

    worked = fields_of(type_name)
    out: list[dict[str, Any]] = [{} for _ in getters]
    if not worked or not getters:
        return out
    fields = shape(type_name).fields()
    variables = _variables(type_name)

    def current(i: int, name: str) -> Any:  # noqa: ANN401 - a model value
        return out[i][name] if name in out[i] else getters[i](name)

    for f in worked:
        assert f.cel is not None  # noqa: S101 - only computed ones
        contexts = [
            {
                "record": {
                    name: cel_value(fields[name], current(i, name))
                    for name in f.cel_reads
                    if name in fields
                }
            }
            for i in range(len(getters))
        ]
        found = spec_rules.evaluate(
            variables,
            [(f.cel, spec_rules.VALUE, spec_rules.computed_result(f))],
            contexts,
        )
        errors: dict[str, list[str]] = {}
        for i, (outcome,) in enumerate(found):
            if outcome.error is not None and not outcome.empty:
                log.warning(
                    "entities.computed_unworkable",
                    type=type_name,
                    field=f.name,
                    error=outcome.error,
                )
            try:
                out[i][f.name] = _stored(f, outcome.result if outcome.error is None else None)
            except ValueError as error:
                if not raising:
                    continue
                where = f"{i}/{f.name}" if len(getters) > 1 else f.name
                errors[where] = [f"is worked out as {outcome.result}, which {error}"]
        if errors:
            raise Invalid(errors)
    return out


def fill(type_name: str, given: Sequence[dict[str, Any]]) -> None:
    """Works out new records' computed values, into each one's values as stored. Raises
    :class:`Invalid` with a value its field refuses (keyed ``index/field`` for many)."""
    if not fields_of(type_name):
        return
    for values, worked in zip(given, _work_out(type_name, [g.get for g in given]), strict=True):
        values.update(worked)


def changes(
    type_name: str, current: Callable[[str], Any], before: Callable[[str], Any]
) -> dict[str, Any]:
    """The computed values an edit changes: worked out from the record as it will be
    (``current``), those that differ from what it holds (``before``)."""
    if not fields_of(type_name):
        return {}
    (worked,) = _work_out(type_name, [current])
    return {n: v for n, v in worked.items() if v != before(n)}


@dataclass
class Reworked:
    """What working a type's records out again found."""

    records: int = 0
    #: Records given a revision with their new values, by field.
    changed: dict[str, int] = field(default_factory=dict)
    #: Locked records whose values differ, kept as they were locked, by field.
    locked: dict[str, int] = field(default_factory=dict)


def reason(type_name: str, why: str) -> str:
    """Why a type's records' computed values were worked out again: the plugins whose
    expressions did it, at their versions, and ``why``."""
    plugins = registry.plugins()
    form = shape(type_name)
    owners = sorted({form.part_of(f.name).plugin for f in fields_of(type_name)})
    named = ", ".join(f"{p} {plugins[p].version}" if p in plugins else p for p in owners)
    return f"Worked out by {named}: {why}"


def rework_brought(entity_ids: Iterable[Any], why: str) -> Reworked:
    """Works out again the computed values of records a bundle brought (ADR 0096), which
    they hold as they were made there, by this deployment's expressions."""
    from iki_server.entities.models import Entity

    done = Reworked()
    by_type: dict[str, list[Any]] = {}
    for pk, type_name in Entity.objects.filter(
        pk__in=list(entity_ids), registry_id__isnull=True
    ).values_list("pk", "type"):
        by_type.setdefault(type_name, []).append(pk)
    for type_name, ids in sorted(by_type.items()):
        if type_name not in registry.types() or not fields_of(type_name):
            continue
        found = rework(type_name, ids, reason(type_name, why))
        done.records += found.records
        for name, count in found.changed.items():
            done.changed[name] = done.changed.get(name, 0) + count
        for name, count in found.locked.items():
            done.locked[name] = done.locked.get(name, 0) + count
    return done


def rework(type_name: str, entity_ids: Iterable[Any] | None, reason: str) -> Reworked:
    """Works the computed values of a type's records out again (``entity_ids`` of them, or
    all), giving each that differs and isn't locked a revision, by no one, via the system,
    for ``reason``. Call it in a transaction."""
    from iki_server.entities.models import Entity
    from iki_server.entities.services import current_values_of, reworked

    done = Reworked()
    if not fields_of(type_name):
        return done
    records = Entity.objects.filter(
        type=type_name, registry_id__isnull=True, purged_at__isnull=True
    ).order_by("pk")
    if entity_ids is not None:
        records = records.filter(pk__in=list(entity_ids))
    last = None
    while True:
        chunk = list((records.filter(pk__gt=last) if last is not None else records)[:CHUNK])
        if not chunk:
            break
        last = chunk[-1].pk
        done.records += len(chunk)
        values = current_values_of(chunk)
        getters = [values.get(e.pk, {}).get for e in chunk]
        for entity, got, worked in zip(
            chunk, getters, _work_out(type_name, getters, raising=False), strict=True
        ):
            differing = {n: v for n, v in worked.items() if v != got(n)}
            if not differing:
                continue
            tally = done.locked if entity.locked_by else done.changed
            for name in differing:
                tally[name] = tally.get(name, 0) + 1
            if not entity.locked_by:
                reworked(entity, differing, reason)
    return done
