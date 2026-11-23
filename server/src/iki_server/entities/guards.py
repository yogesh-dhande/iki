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
- ``holds``: the rules holding it for review (ADR 0127).

So a plugin can say which state may follow which (``state.from == "approved"``), keep
people apart (``last.status.in_review.by != actor.person``: four eyes), and tie fields
together (a lot isn't released while it's quarantined). Every guard of the state must be
true; one that's false refuses the change with its message, and so does one that can't be
worked out, whatever the reason: a guard fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import structlog

from iki_server.entities import checking, registry
from iki_server.entities.models import Entity, Revision, RevisionKind, RuleHold
from iki_server.entities.values import shape
from iki_server.plugins.models import HistoryChange
from iki_spec import rules as spec_rules
from iki_spec.model import Field, Guard, State

if TYPE_CHECKING:
    from iki_server.accounts.models import User

__all__ = ["Refusal", "evaluate", "refusals", "variables_of"]

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


def _history(
    entity: Entity, states: Mapping[str, Field], values: Mapping[str, Any], *, with_edits: bool
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, list[dict[str, Any]]]]]:
    from iki_server.signatures.services import history_changes

    renames = (
        [
            change
            for change in history_changes(entity, 0)
            if change.kind == HistoryChange.Kind.RENAME and not change.part.endswith("#entry")
        ]
        if with_edits
        else []
    )

    def current_name(name: str, at: datetime) -> str:
        # Append-only revisions keep the spelling at the time. The history table now
        # has the name the intervening migrations gave it, as a field-specific guard does.
        for change in renames:
            if change.made_at > at and name == change.name:
                name = change.to
        return name

    history: dict[str, list[dict[str, Any]]] = {name: [] for name in states}
    edits: list[dict[str, Any]] = []
    # An entry points into the one chronological edit list. Re-entry replaces its offset.
    starts: dict[tuple[str, str], int] = {}
    kinds = [RevisionKind.CREATED, RevisionKind.STATE_CHANGED]
    if with_edits:
        kinds.append(RevisionKind.EDITED)
    revisions = Revision.objects.filter(entity_id=entity.pk, kind__in=kinds).select_related("agent")
    created = None
    for revision in revisions.order_by("number"):
        if revision.kind == RevisionKind.CREATED:
            created = revision
        elif (name := current_name(revision.state_field, revision.created_at)) in states:
            history[name].append(_change(revision, revision.state_from, revision.state_to))
            starts[name, revision.state_to] = len(edits)
        if with_edits:
            names = (current_name(n, revision.created_at) for n in revision.changed_fields)
            if fields := [n for n in names if n not in states]:
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
    from iki_server.org import permissions

    guarded = [s for s in states if s.guards]
    if not guarded or entity.owner_project is None:
        return {}
    type_name = entity.type
    form = shape(type_name)
    fields = form.fields()
    state_fields = {n: f for n, f in fields.items() if f.type == "state" and not f.removed}
    reads = sorted(
        {n for s in guarded for g in s.guards.values() for n in g.reads} | set(state_fields)
    )
    referenced = checking.referenced_by(fields, reads, [values.get])
    record = checking.record_json(
        form, values.get, reads, referenced, entity_id=entity.pk, hrid=entity.hrid
    )
    missing = checking.unread(form, values.get, reads, referenced)
    # Only guards mentioning the variable need the record's ordinary edit revisions.
    with_edits = any(
        "edits" in source
        for s in guarded
        for g in s.guards.values()
        for source in (g.check, g.when or "", g.message)
    )
    history, edits = _history(entity, state_fields, values, with_edits=with_edits)
    last = {
        name: {
            s: next((c for c in reversed(changes) if c["to"] == s), None)
            for s in state_fields[name].states
        }
        for name, changes in history.items()
    }
    held = list(
        RuleHold.objects.filter(entity_id=entity.pk, reviewed_at__isnull=True).values_list(
            "rule", flat=True
        )
    )
    roles = permissions.lab_roles(user, entity.owner_project)
    plugin = _plugin_of(type_name, field)
    base = {
        "record": record,
        **checking.now_values(),
        "actor": _actor(user, roles, plugin),
        "history": history,
        "last": last,
        "edits": edits,
        "holds": held,
        "signatures": list(signatures),
    }
    current = values.get(field) or ""
    contexts = {
        state.name: {**base, "state": {"field": field, "from": current, "to": state.name}}
        for state in guarded
    }
    return evaluate(
        type_name,
        variables_of(type_name, field),
        {state.name: tuple(state.guards.values()) for state in guarded},
        contexts,
        missing,
    )


def evaluate(
    type_name: str,
    variables: Mapping[str, Any],
    guards: Mapping[str, Sequence[Guard]],
    contexts: Mapping[str, Mapping[str, Any]],
    missing: Mapping[str, int],
) -> dict[str, Refusal]:
    """Evaluate the guards for each proposed operation and return its first refusal."""
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

    results = spec_rules.evaluate(variables, expressions, list(contexts.values()))
    out: dict[str, Refusal] = {}
    for key, outcomes in zip(contexts, results, strict=True):
        for guard in guards[key]:
            check, when, message = at[guard]
            text = outcomes[message].text or guard.message
            if lost := {n: k for n, k in missing.items() if n in guard.reads}:
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
