"""Plugins' rules, checked at every save (ADR 0127).

A type's rules are its own, its ancestors' and those extensions of them declare
(:func:`rules_of`). A ``check`` is CEL over the record as it will be saved, evaluated by
iki-wasm as the validator type-checked it and as forms evaluate it in browsers; ``unique``
and ``no_overlap`` rules are the database's (:mod:`iki_server.entities.constraints`).

Every write of a record's values comes through :func:`broken`: a create or an edit by a
person, an agent, a run's results or a bulk import. On a create every check runs; on an
edit, only those reading a field it changes, as Kubernetes ratchets its validation: a
record saved before a rule existed can still change in other ways, and a deploy reports
it (:func:`recheck`) rather than leaving it to fail later.

A check that's false refuses the save, or, for a rule that holds, keeps it only with a
reason and holds the record for review (:mod:`iki_server.entities.holds`). One that can't
be worked out because a value it reads is empty passes, as SQL's CHECK does with NULL;
one that can't be worked out otherwise breaks, saying so.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

import structlog

from iki_server.entities import registry
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid, shape, to_json
from iki_spec import rules as spec_rules
from iki_spec.model import Field, Rule

if TYPE_CHECKING:
    from iki_server.entities.values import Shape

__all__ = [
    "Broken",
    "Held",
    "Saving",
    "apart",
    "broken",
    "cel_value",
    "check_failure",
    "checks_of",
    "kept",
    "raise_broken",
    "record_variables",
    "render",
    "rules_of",
]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Broken:
    """A rule a save breaks: what to tell whoever saves it, and where it points."""

    rule: Rule
    message: str
    #: The fields it points at, in a form and the API's errors.
    fields: tuple[str, ...]

    @property
    def held(self) -> bool:
        """It holds the record for review, rather than refuse the save (ADR 0127)."""
        return bool(self.rule.hold)


class Held(Invalid):
    """A save that breaks only rules that hold: it's kept if whoever saves it says why."""


@dataclass(frozen=True)
class Saving:
    """A record about to be saved, as :func:`broken` checks it."""

    #: A field's value as it will be saved, as the database stores it.
    values: Callable[[str], Any]
    #: The fields the save changes, or None for a new record: every check runs.
    changed: frozenset[str] | None = None
    id: uuid.UUID | None = None
    hrid: str = ""


# --- What applies -------------------------------------------------------------------------------


@cache
def rules_of(type_name: str) -> tuple[Rule, ...]:
    """Every rule a type's records keep: its ancestors', its own, and those extensions of
    any of them declare."""
    entity_type = registry.types()[type_name]
    chain = [*reversed(registry.ancestors(entity_type)), entity_type]
    names = {t.qualified_name for t in chain}
    out = [r for t in chain for r in t.rules.values()]
    for extension in registry.extensions():
        if extension.extends_type in names:
            out.extend(extension.rules.values())
    return tuple(out)


@cache
def checks_of(type_name: str) -> tuple[Rule, ...]:
    """A type's ``check`` rules."""
    return tuple(r for r in rules_of(type_name) if r.kind == "check")


def ref_fields(name: str) -> tuple[str, Mapping[str, Field]] | None:
    """What a reference to ``name`` reads of the record it refers to, as the validator
    gives it: its fields, or its registry entry's (ADR 0115)."""
    found = registry.types().get(name)
    if found is None:
        return None
    if name == "iki/core.Entity":
        # Any record at all: it has no fields of its own, nor a table.
        return found.label, {}
    form = shape(name)
    # A reference to a type with an identity is to its registry entry (ADR 0115).
    fields = form.entry_fields() if form.has_entries else form.fields()
    return found.label, fields


@cache
def record_variables(type_name: str) -> dict[str, Any]:
    """What a type's rules read: its records' schema, as the validator checked them."""
    entity_type = registry.types()[type_name]
    record = spec_rules.record_schema(entity_type.label, shape(type_name).fields(), ref_fields)
    return spec_rules.rule_variables(record)


@cache
def digest_of(type_name: str) -> str:
    """What a type's records are checked against: its checks, and its plugins' versions
    (a migration may have changed their values)."""
    entity_type = registry.types()[type_name]
    plugins = registry.plugins()
    chain = [entity_type, *registry.ancestors(entity_type)]
    owners = sorted(
        {t.plugin for t in chain} | {r.plugin for r in rules_of(type_name)} & set(plugins)
    )
    data = {
        "checks": [
            [r.key, r.check, r.when, r.message, list(r.hold), list(r.fields)]
            for r in checks_of(type_name)
        ],
        "plugins": [[p, plugins[p].version] for p in owners if p in plugins],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# --- Values as CEL reads them -------------------------------------------------------------------


def cel_value(f: Field, value: Any, *, from_json: bool = False) -> Any:  # noqa: ANN401 - storage or JSON in, JSON out
    """A stored value (or its JSON form) as an expression reads it, including dates as
    midnight UTC. CEL has timestamps, not dates."""
    if value is None:
        return None
    if f.is_rows:
        assert f.items is not None  # noqa: S101 - lists always have items
        return [
            {
                n: cel_value(cell, row.get(n), from_json=from_json)
                for n, cell in f.items.fields.items()
            }
            for row in value
        ]
    if f.type == "list":
        assert f.items is not None  # noqa: S101 - lists always have items
        return [cel_value(f.items, item, from_json=from_json) for item in value]
    out = value if from_json else to_json(f, value)
    if f.type == "date":
        return f"{out}T00:00:00Z"
    return out


def _is_refs(f: Field) -> bool:
    return f.type == "list" and f.items is not None and f.items.type == "ref"


def referenced_by(
    fields: Mapping[str, Field], wanted: Iterable[str], getters: Sequence[Callable[[str], Any]]
) -> dict[Any, dict[str, Any]]:
    """Each record a save's references read refers to, as an expression reads it: every
    member of a list, however long, as its field's own limit allows it to be. A rule over
    a list sees all of it, or (:func:`unread`) isn't worked out."""
    ids: set[Any] = set()
    for values in getters:
        ids |= _referenced_ids(fields, wanted, values)
    return _load(ids)


def _referenced_ids(
    fields: Mapping[str, Field], wanted: Iterable[str], values: Callable[[str], Any]
) -> set[Any]:
    """The records one record's references among ``wanted`` refer to."""
    ids: set[Any] = set()
    for name in wanted:
        f = fields.get(name)
        if f is None:
            continue
        value = values(name)
        if f.type == "ref" and value is not None:
            ids.add(value)
        elif _is_refs(f) and value:
            ids.update(value)
    return ids


def _load(ids: set[Any]) -> dict[Any, dict[str, Any]]:
    """Each of the records, as an expression reads it."""
    from iki_server.entities.services import current_values_of

    if not ids:
        return {}
    # A record of a type no longer installed (its plugin purged, its row kept) can't be
    # read: :func:`unread` says which fields refer to one.
    installed = registry.types()
    found = [e for e in Entity.objects.filter(pk__in=ids) if e.type in installed]
    values_of = current_values_of(found)
    out: dict[Any, dict[str, Any]] = {}
    for entity in found:
        form = shape(entity.type)
        own = form.entry_fields() if entity.registry_id is not None else form.fields()
        stored = values_of.get(entity.pk, {})
        record = {"id": str(entity.pk), "hrid": entity.hrid}
        for name, f in own.items():
            value = stored.get(name)
            # Its own references are their IDs: an expression reads one step, no further.
            record[name] = cel_value(f, value)
        out[entity.pk] = record
    return out


def record_json(
    form: Shape,
    values: Callable[[str], Any],
    reads: Iterable[str],
    referenced: Mapping[Any, dict[str, Any]],
    *,
    entity_id: uuid.UUID | None = None,
    hrid: str = "",
) -> dict[str, Any]:
    """The record an expression reads as ``record``: the fields it reads, with what its
    references refer to."""
    fields = form.fields()
    out: dict[str, Any] = {"id": str(entity_id) if entity_id else "", "hrid": hrid}
    for name in reads:
        f = fields.get(name)
        if f is None:
            continue
        value = values(name)
        if f.type == "ref":
            out[name] = referenced.get(value) if value is not None else None
        elif _is_refs(f):
            out[name] = [referenced[v] for v in (value or ()) if v in referenced]
        else:
            out[name] = cel_value(f, value)
    return out


def unread(
    form: Shape,
    values: Callable[[str], Any],
    reads: Iterable[str],
    referenced: Mapping[Any, dict[str, Any]],
) -> dict[str, int]:
    """The fields of ``reads`` that refer to records :func:`referenced_by` couldn't read,
    with how many: one whose type is no longer installed. A rule reading one isn't worked
    out on what's left, which could keep it or break it wrongly: it can't be checked."""
    fields = form.fields()
    out: dict[str, int] = {}
    for name in reads:
        f = fields.get(name)
        if f is None:
            continue
        value = values(name)
        if f.type == "ref":
            missing = int(value is not None and value not in referenced)
        elif _is_refs(f):
            missing = sum(1 for v in value or () if v not in referenced)
        else:
            continue
        if missing:
            out[name] = missing
    return out


def unread_text(fields: Mapping[str, int]) -> str:
    """Why a rule reading these :func:`unread` fields can't be checked."""
    said = ", ".join(
        f"{n} refers to {k} record{'s' * (k != 1)} it can’t read" for n, k in fields.items()
    )
    return f"{said}, so it isn’t worked out on the rest."


def now_values(now: dt.datetime | None = None) -> dict[str, str]:
    """``now`` and ``today`` (midnight UTC), as expressions read them."""
    moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "now": moment.isoformat().replace("+00:00", "Z"),
        "today": day.isoformat().replace("+00:00", "Z"),
    }


# --- Checking -----------------------------------------------------------------------------------


def _applies(rule: Rule, changed: frozenset[str] | None) -> bool:
    return changed is None or bool(set(rule.reads) & changed) or not rule.reads


def broken(type_name: str, savings: Sequence[Saving]) -> list[list[Broken]]:
    """The checks each record about to be saved breaks: false, or not worked out for a
    reason other than an empty value. Each record's are in its rules' order."""
    checks = checks_of(type_name)
    if not checks or not savings:
        return [[] for _ in savings]
    form = shape(type_name)
    fields = form.fields()
    applying = [[r for r in checks if _applies(r, s.changed)] for s in savings]
    used = {r.key: r for rules in applying for r in rules}
    if not used:
        return [[] for _ in savings]
    rules = [r for r in checks if r.key in used]
    reads = sorted({name for r in rules for name in r.reads})
    expressions: list[tuple[str, str]] = []
    at: dict[str, tuple[int, int | None, int]] = {}
    for rule in rules:
        assert rule.check is not None  # noqa: S101 - a check rule
        check = len(expressions)
        expressions.append((rule.check, spec_rules.CONDITION))
        when = None
        if rule.when is not None:
            when = len(expressions)
            expressions.append((rule.when, spec_rules.CONDITION))
        message = len(expressions)
        expressions.append((rule.message, spec_rules.MESSAGE))
        at[rule.key] = (check, when, message)
    times = now_values()
    contexts: list[dict[str, Any]] = []
    missing: list[dict[str, int]] = []
    for group in _groups(fields, reads, savings):
        referenced = _load(set().union(*(ids for _, ids in group)))
        for s, _ in group:
            record = record_json(form, s.values, reads, referenced, entity_id=s.id, hrid=s.hrid)
            contexts.append({"record": record, **times})
            missing.append(unread(form, s.values, reads, referenced))
    results = spec_rules.evaluate(record_variables(type_name), expressions, contexts)
    out: list[list[Broken]] = []
    for outcomes, rules_here, gone in zip(results, applying, missing, strict=True):
        found = []
        for rule in rules_here:
            if lost := {n: k for n, k in gone.items() if n in rule.reads}:
                log.warning("entities.rule_unworkable", rule=rule.key, type=type_name, unread=lost)
                text = f"“{rule.label}” couldn’t be checked: {unread_text(lost)}"
                found.append(Broken(rule, text, rule.fields or rule.reads))
                continue
            check, when, message = at[rule.key]
            why = check_failure(outcomes[check], outcomes[when] if when is not None else None)
            if why is None:
                continue
            if why == "":
                text = outcomes[message].text or f"It breaks “{rule.label}”."
            else:
                log.warning("entities.rule_unworkable", rule=rule.key, type=type_name, error=why)
                text = f"“{rule.label}” couldn’t be checked: {why}"
            found.append(Broken(rule, text, rule.fields or rule.reads))
        out.append(found)
    return out


#: The most records one lookup loads for many saves' rules, as a bulk import checks a
#: chunk of them: past it, the next saves have a lookup of their own. Each save's own
#: references are loaded whole, however many.
MAX_LOOKUP = 20_000


def _groups(
    fields: Mapping[str, Field], reads: Sequence[str], savings: Sequence[Saving]
) -> list[list[tuple[Saving, set[Any]]]]:
    """``savings`` in order, in groups whose references come to at most MAX_LOOKUP records
    together, or to a single save's."""
    out: list[list[tuple[Saving, set[Any]]]] = []
    group: list[tuple[Saving, set[Any]]] = []
    count = 0
    for saving in savings:
        ids = _referenced_ids(fields, reads, saving.values)
        if group and count + len(ids) > MAX_LOOKUP:
            out.append(group)
            group, count = [], 0
        group.append((saving, ids))
        count += len(ids)
    if group:
        out.append(group)
    return out


def check_failure(check: spec_rules.Outcome, when: spec_rules.Outcome | None) -> str | None:
    """Why a rule is broken: "" when its check is false, the error when it can't be worked
    out for a reason other than an empty value; None when it's kept or doesn't apply."""
    if when is not None:
        if when.error is not None:
            return None if when.empty else when.error
        if not when.value:
            return None
    if check.error is not None:
        return None if check.empty else check.error
    return None if check.value else ""


def raise_broken(
    found: Sequence[Broken], *, hold_reason: str, errors: Mapping[str, list[str]] | None = None
) -> list[Broken]:
    """Raises for what a save breaks: :class:`Invalid` for a rule that refuses, or
    :class:`Held` for rules that hold when no reason was given. Returns those that hold,
    to hold the record by, when there's a reason."""
    if not found and not errors:
        return []
    refusing = [b for b in found if not b.held]
    holding = [b for b in found if b.held]
    if errors or refusing or (holding and not hold_reason.strip()):
        out: dict[str, list[str]] = {k: list(v) for k, v in (errors or {}).items()}
        shown = list(found)
        for b in shown:
            # A rule that points at no field is the record's as a whole: "".
            for name in b.fields or ("",):
                out.setdefault(name, []).append(b.message)
        if not errors and not refusing:
            raise Held(out, broken=shown)
        raise Invalid(out, broken=shown)
    return holding


def render(type_name: str, rules: Sequence[Rule], saving: Saving) -> list[str]:
    """Each rule's message for a record, its ``{{ }}`` filled in."""
    if all("{{" not in r.message for r in rules):
        return [r.message for r in rules]
    form = shape(type_name)
    reads = sorted({n for r in rules for n in r.reads})
    referenced = referenced_by(form.fields(), reads, [saving.values])
    context = {
        "record": record_json(
            form, saving.values, reads, referenced, entity_id=saving.id, hrid=saving.hrid
        ),
        **now_values(),
    }
    results = spec_rules.evaluate(
        record_variables(type_name), [(r.message, spec_rules.MESSAGE) for r in rules], [context]
    )[0]
    return [o.text or r.message for o, r in zip(results, rules, strict=True)]


def apart(
    project_id: Any,  # noqa: ANN401 - a UUID
    form: Shape,
    values: Mapping[str, Any],
    *,
    entity: uuid.UUID | None = None,
    changed: frozenset[str] | None = None,
) -> list[Broken]:
    """The ``unique`` and ``no_overlap`` rules a record with these values would break
    against another of the project's, each naming the other: so a person is told, before
    the database refuses it."""
    from iki_server.entities import constraints

    found = constraints.taken(project_id, form, values, entity=entity, changed=changed)
    if not found:
        return []
    type_name = form.entity_type.qualified_name
    texts = render(type_name, [c.rule for c, _ in found], Saving(values.get, id=entity))
    out = []
    for (c, hrid), text in zip(found, texts, strict=True):
        if c.rule.kind == "no_overlap":
            told = f"{text} It overlaps {hrid}."
        else:
            told = f"{text} {hrid} has the same."
        out.append(Broken(c.rule, told, c.rule.fields or c.rule.reads))
    return out


def kept(entity: Entity, changed: frozenset[str], found: Sequence[Broken]) -> None:
    """After a save: the record no longer breaks the rules it was reported for (ADR 0127)
    that this save checked and found kept."""
    from iki_server.entities.models import RuleBreach

    reported = set(RuleBreach.objects.filter(entity_id=entity.pk).values_list("rule", flat=True))
    if not reported:
        return
    still = {b.rule.key for b in found}
    checked = {r.key for r in rules_of(entity.type) if _applies(r, changed)}
    cleared = (reported & checked) - still
    if cleared:
        RuleBreach.objects.filter(entity_id=entity.pk, rule__in=cleared).delete()
