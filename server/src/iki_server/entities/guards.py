"""Guards on setting a record's state (ADR 0128).

A state says who may set its field to it (``set_by``, ADR 0125); its ``guards`` say
when. Each is a CEL condition, type-checked when its plugin is validated and evaluated by
iki-wasm here and in browsers alike, over:

- ``actor``: who's setting it: the person (their ID), the agent acting for them (ADR
  0072), their lab roles in the record's project, and whether it's a service account;
- ``record``: the record's values, every state field among them, with what its
  references refer to;
- ``state``: the change: which field, from which state and to which;
- ``history`` and ``last``: each state field's changes, oldest first, and the latest
  change into each of its states (null if it's never been in it), each with who made it,
  through which agent, when and why. A record's initial state is a change at its creation;
- ``edits``: who set which of the record's value fields since it last entered a state;
- ``holds``: the rules holding it for review (ADR 0127);
- ``covered_subjects``: the commissioned record IDs covered by this result.

So a plugin can say which state may follow which (``state.from == "approved"``), keep
people apart (``last.status.in_review.by != actor.person``: four eyes), and tie fields
together (a lot isn't released while it's quarantined). Every guard of the state must be
true; one that's false refuses the change with its message, and so does one that can't be
worked out, whatever the reason: a guard fails closed.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from iki_server.entities import checking, registry, requests
from iki_server.entities.history import FieldNames
from iki_server.entities.models import Entity, Revision, RevisionKind, RuleHold
from iki_server.entities.values import shape
from iki_spec import rules as spec_rules
from iki_spec.model import Field, Guard, State

if TYPE_CHECKING:
    from iki_server.accounts.models import User

__all__ = ["Ask", "Refusal", "evaluate", "refusals", "refusals_of", "variables_of"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Refusal:
    """A guard that refuses a change: which, and what to tell whoever tries it."""

    guard: Guard
    message: str


def _plugin_of(type_name: str, field: str) -> str:
    return shape(type_name).part_of(field).plugin


@cache
def variables_of(type_name: str, field: str) -> dict[str, Any]:
    """What the guards of a state field read, as its plugin's validation checked them."""
    from iki_server.org import lab_roles

    entity_type = registry.types()[type_name]
    fields = shape(type_name).fields()
    record = spec_rules.record_schema(entity_type.label, fields, checking.ref_fields)
    states = {n: f for n, f in fields.items() if f.type == "state"}
    roles = spec_rules.role_names(lab_roles.declared(), _plugin_of(type_name, field))
    return spec_rules.guard_variables(record, states, roles, fields[field])


def _change(revision: Revision, frm: str, to: str) -> dict[str, Any]:
    agent = revision.agent
    return {
        "from": frm,
        "to": to,
        "by": str(revision.user_id) if revision.user_id else "",
        "agent": {"name": agent.client_name, "client_id": agent.client_id} if agent else None,
        "at": revision.created_at.isoformat().replace("+00:00", "Z"),
        "reason": revision.reason,
    }


def _revisions(ids: Iterable[UUID], *, with_edits: bool) -> dict[UUID, list[Revision]]:
    """The revisions guards read of each of the records, in order: one query for a page."""
    kinds = [RevisionKind.CREATED, RevisionKind.STATE_CHANGED]
    if with_edits:
        kinds.extend([RevisionKind.EDITED, RevisionKind.MIGRATED])
    found: dict[UUID, list[Revision]] = defaultdict(list)
    for revision in (
        Revision.objects.filter(entity_id__in=set(ids), kind__in=kinds)
        .select_related("agent")
        .defer("submitted")
        .order_by("entity_id", "number")
    ):
        found[revision.entity_id].append(revision)
    return found


_EDITS = frozenset({RevisionKind.EDITED, RevisionKind.MIGRATED})


def _history(
    entity: Entity,
    states: Mapping[str, Field],
    values: Mapping[str, Any],
    *,
    with_edits: bool,
    revisions: Sequence[Revision] | None = None,
    names: FieldNames | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, list[dict[str, Any]]]]]:
    """``revisions``: the record's (:func:`_revisions`), read if not given; ``names``:
    its type's (:meth:`FieldNames.for_type`)."""
    names = FieldNames.for_type(entity.type) if names is None else names
    if revisions is None:
        revisions = _revisions([entity.pk], with_edits=with_edits).get(entity.pk, [])
    history: dict[str, list[dict[str, Any]]] = {name: [] for name in states}
    edits: list[dict[str, Any]] = []
    # An entry points into the one chronological edit list. Re-entry replaces its offset.
    starts: dict[tuple[str, str], int] = {}
    created = None
    for revision in revisions:
        if not with_edits and revision.kind in _EDITS:
            continue
        if revision.kind == RevisionKind.CREATED:
            created = revision
        elif (name := names.current(revision.state_field, revision.history_seen)) in states:
            history[name].append(_change(revision, revision.state_from, revision.state_to))
            starts[name, revision.state_to] = len(edits)
        if with_edits and (fields := [n for n in names.changed(revision) if n not in states]):
            edits.append(
                {"by": str(revision.user_id) if revision.user_id else "", "fields": fields}
            )
    if created is not None:
        # Where this record actually started, even after a change to the type's default.
        for name, f in states.items():
            initial = history[name][0]["from"] if history[name] else values.get(name, f.initial)
            if initial:
                history[name].insert(0, _change(created, "", initial))
                starts.setdefault((name, initial), 0)
    return history, {
        name: {s: edits[starts.get((name, s), len(edits)) :] for s in f.states}
        for name, f in states.items()
        if with_edits
    }


def _actor(user: User, roles: frozenset[str], plugin: str) -> dict[str, Any]:
    from iki_server.audit import log as audit

    agent = audit.current_agent_info()
    service = bool(getattr(user, "is_service_account", False))
    return {
        "person": "" if service else str(user.pk),
        "agent": {"name": agent.client_name, "client_id": agent.client_id} if agent else None,
        "roles": spec_rules.role_names(roles, plugin),
        "service_account": service,
    }


def _draws(ids: Iterable[UUID]) -> dict[UUID, list[dict[str, str]]]:
    """When each of the records was drawn from, in order: one query for a page."""
    from iki_server.amounts.models import Movement, MovementKind

    found: dict[UUID, list[dict[str, str]]] = defaultdict(list)
    for used_in, at in (
        Movement.objects.filter(used_in_id__in=set(ids), kind=MovementKind.DRAWN)
        .order_by("created_at", "id")
        .values_list("used_in_id", "created_at")
    ):
        found[used_in].append({"on": checking.now_values(at)["today"]})
    return found


@dataclass(frozen=True)
class Ask:
    """A record's state field, and states it could be set to: their guards to check.
    ``values``: the record's, as the database stores them; ``signatures``: those given
    for the change so far, for a signed state (``{index, meaning, by, at}``, ADR 0126)."""

    entity: Entity
    field: str
    states: Sequence[State]
    values: Mapping[str, Any]
    signatures: Sequence[Mapping[str, Any]] = ()


def refusals(
    user: User,
    entity: Entity,
    field: str,
    states: Sequence[State],
    values: Mapping[str, Any],
    signatures: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Refusal]:
    """For each of ``states`` the record's ``field`` could be set to, the first of its
    guards that refuses ``user`` setting it, by state. ``values``: the record's, as the
    database stores them; ``signatures``: those given for the change so far, for a
    signed state (``{index, meaning, by, at}``, ADR 0126)."""
    return refusals_of(user, [Ask(entity, field, states, values, signatures)])[0]


def refusals_of(
    user: User,
    asks: Sequence[Ask],
    held: Mapping[UUID, Sequence[RuleHold]] | None = None,
) -> list[dict[str, Refusal]]:
    """:func:`refusals` for each of ``asks``, a page of records' at once: what their guards
    read is read once for all of them, and they're evaluated together. ``held``: the
    records' open holds (:func:`holds.open_of`), read if not given."""
    from iki_server.entities import holds
    from iki_server.org import permissions

    out: list[dict[str, Refusal]] = [{} for _ in asks]
    work: list[tuple[int, Ask, list[State], _Reading]] = []
    for i, ask in enumerate(asks):
        guarded = [s for s in ask.states if s.guards]
        if guarded and ask.entity.owner_project is not None:
            work.append((i, ask, guarded, _reading(ask.entity.type, guarded)))
    if not work:
        return out
    ids = {ask.entity.pk for _, ask, _, _ in work}
    held = holds.open_of(ids) if held is None else held
    revisions = _revisions(ids, with_edits=any(r.edits for *_, r in work))
    names = {t: FieldNames.for_type(t) for t in {ask.entity.type for _, ask, _, _ in work}}
    drawn = _draws({ask.entity.pk for _, ask, _, r in work if r.draws})
    # What their references refer to, one read per type for all of its records.
    referenced: dict[str, dict[Any, dict[str, Any]]] = {}
    for type_name in names:
        of_type = [(ask, r) for _, ask, _, r in work if ask.entity.type == type_name]
        referenced[type_name] = checking.referenced_by(
            shape(type_name).fields(),
            sorted({n for _, r in of_type for n in r.reads}),
            [ask.values.get for ask, _ in of_type],
        )
    now = checking.now_values()
    # Evaluated together, a call for each type and field: their guards read the same.
    groups: dict[tuple[str, str], _Group] = defaultdict(_Group)
    for i, ask, guarded, reading in work:
        entity, values = ask.entity, ask.values
        type_name = entity.type
        form = shape(type_name)
        refs = referenced[type_name]
        record = checking.record_json(
            form, values.get, reading.reads, refs, entity_id=entity.pk, hrid=entity.hrid
        )
        history, edits = _history(
            entity,
            reading.states,
            values,
            with_edits=reading.edits,
            revisions=revisions.get(entity.pk, []),
            names=names[type_name],
        )
        last = {
            name: {
                s: next((c for c in reversed(changes) if c["to"] == s), None)
                for s in reading.states[name].states
            }
            for name, changes in history.items()
        }
        project = entity.owner_project
        assert project is not None  # noqa: S101 - only a project's records are asked
        roles = permissions.lab_roles(user, project)
        base = {
            "record": record,
            **now,
            "actor": _actor(user, roles, _plugin_of(type_name, ask.field)),
            "history": history,
            "last": last,
            "edits": edits,
            "holds": [h.rule for h in held.get(entity.pk, ())],
            "draws": drawn.get(entity.pk, []),
            "covered_subjects": requests.covered_subjects(entity, values)
            if reading.covered
            else [],
            "signatures": list(ask.signatures),
        }
        current = values.get(ask.field) or ""
        group = groups[type_name, ask.field]
        lost = checking.unread(form, values.get, reading.reads, refs)
        for state in guarded:
            key = (i, state.name)
            group.guards[key] = tuple(state.guards.values())
            group.contexts[key] = {
                **base,
                "state": {"field": ask.field, "from": current, "to": state.name},
            }
            group.missing[key] = lost
    for (type_name, name), group in groups.items():
        found = evaluate(
            type_name, variables_of(type_name, name), group.guards, group.contexts, group.missing
        )
        for (i, to), refusal in found.items():
            out[i][to] = refusal
    return out


@dataclass(frozen=True)
class _Reading:
    """What a record's guarded states' guards read."""

    #: Its state fields.
    states: Mapping[str, Field]
    #: The fields its guards read, and its state fields.
    reads: tuple[str, ...]
    edits: bool
    draws: bool
    covered: bool


def _reading(type_name: str, guarded: Sequence[State]) -> _Reading:
    fields = shape(type_name).fields()
    states = {n: f for n, f in fields.items() if f.type == "state" and not f.removed}
    sources = [
        source
        for s in guarded
        for g in s.guards.values()
        for source in (g.check, g.when or "", g.message)
    ]
    return _Reading(
        states,
        tuple(
            sorted({n for s in guarded for g in s.guards.values() for n in g.reads} | set(states))
        ),
        any("edits" in s for s in sources),
        any("draws" in s for s in sources),
        any("covered_subjects" in s for s in sources),
    )


@dataclass
class _Group:
    """The guards of one type's state field, for each record and state: one evaluation."""

    guards: dict[tuple[int, str], Sequence[Guard]] = field(default_factory=dict)
    contexts: dict[tuple[int, str], Mapping[str, Any]] = field(default_factory=dict)
    missing: dict[tuple[int, str], Mapping[str, int]] = field(default_factory=dict)


#: About how much of the guards' contexts one evaluation takes, in bytes of JSON: a page
#: of records whose guards read long lists is evaluated in parts, well inside the
#: module's input limit (:func:`iki_spec.wasm.call`).
EVALUATION_BYTES = 8 * 1024 * 1024


def evaluate[K: Hashable](
    type_name: str,
    variables: Mapping[str, Any],
    guards: Mapping[K, Sequence[Guard]],
    contexts: Mapping[K, Mapping[str, Any]],
    missing: Mapping[K, Mapping[str, int]],
) -> dict[K, Refusal]:
    """Evaluate the guards for each proposed operation and return its first refusal.
    ``missing``: by operation, the fields its record refers to records it couldn't read
    (:func:`checking.unread`); an operation not in it has none."""
    expressions: list[tuple[str, str]] = []
    at: dict[Guard, tuple[int, int | None, int]] = {}
    for guard in dict.fromkeys(g for group in guards.values() for g in group):
        check = len(expressions)
        expressions.append((guard.check, spec_rules.CONDITION))
        when = None
        if guard.when is not None:
            when = len(expressions)
            expressions.append((guard.when, spec_rules.CONDITION))
        message = len(expressions)
        expressions.append((guard.message, spec_rules.MESSAGE))
        at[guard] = check, when, message

    results: list[list[spec_rules.Outcome]] = []
    for part in _parts(list(contexts.values())):
        results.extend(spec_rules.evaluate(variables, expressions, part))
    out: dict[K, Refusal] = {}
    for key, outcomes in zip(contexts, results, strict=True):
        unread = missing.get(key, {})
        for guard in guards[key]:
            check, when, message = at[guard]
            text = outcomes[message].text or guard.message
            if lost := {n: k for n, k in unread.items() if n in guard.reads}:
                # Worked out on part of a list, it could let the change through wrongly.
                log.warning(
                    "entities.guard_unworkable", type=type_name, guard=guard.name, unread=lost
                )
                out[key] = Refusal(
                    guard, f"{text} (It couldn’t be checked: {checking.unread_text(lost)})"
                )
                break
            result = outcomes[check]
            if when is not None:
                w = outcomes[when]
                if w.empty or (w.error is None and not w.value):
                    continue
                if w.error is not None:
                    result = w
            if result.error is None and result.value:
                continue
            if result.error is not None and not result.empty:
                log.warning(
                    "entities.guard_unworkable",
                    type=type_name,
                    guard=guard.name,
                    error=result.error,
                )
                text = f"{text} (It couldn’t be checked: {result.error})"
            out[key] = Refusal(guard, text)
            break
    return out


def _parts(contexts: list[Mapping[str, Any]]) -> Iterator[list[Mapping[str, Any]]]:
    """The contexts in order, in parts of about :data:`EVALUATION_BYTES` each."""
    part: list[Mapping[str, Any]] = []
    size = 0
    for context in contexts:
        length = len(json.dumps(context))
        if part and size + length > EVALUATION_BYTES:
            yield part
            part, size = [], 0
        part.append(context)
        size += length
    if part:
        yield part
