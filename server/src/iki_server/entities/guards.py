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
- ``holds``: the rules holding it for review (ADR 0127).

So a plugin can say which state may follow which (``state.from == "approved"``), keep
people apart (``last.status.in_review.by != actor.person``: four eyes), and tie fields
together (a lot isn't released while it's quarantined). Every guard of the state must be
true; one that's false refuses the change with its message, and so does one that can't be
worked out, whatever the reason: a guard fails closed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

import structlog

from iki_server.entities import checking, registry
from iki_server.entities.models import Entity, Revision, RevisionKind, RuleHold
from iki_server.entities.values import shape
from iki_spec import rules as spec_rules
from iki_spec.model import Field, Guard, State

if TYPE_CHECKING:
    from iki_server.accounts.models import User

__all__ = ["Refusal", "refusals", "variables_of"]

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


def _change(revision: Any, field: str, frm: str, to: str) -> dict[str, Any]:  # noqa: ANN401 - a Revision
    agent = revision.agent
    return {
        "from": frm,
        "to": to,
        "by": str(revision.user_id) if revision.user_id else "",
        "agent": {"name": agent.client_name, "client_id": agent.client_id} if agent else None,
        "at": revision.created_at.isoformat().replace("+00:00", "Z"),
        "reason": revision.reason,
    }


def _history(entity: Entity, states: Mapping[str, Field]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    revisions = (
        Revision.objects.filter(
            entity_id=entity.pk, kind__in=[RevisionKind.CREATED, RevisionKind.STATE_CHANGED]
        )
        .select_related("agent")
        .order_by("number")
    )
    for revision in revisions:
        if revision.kind == RevisionKind.CREATED:
            # Where it started: each state field's initial state, as its creation set it.
            for name, f in states.items():
                if f.initial:
                    out[name].append(_change(revision, name, "", f.initial))
        elif revision.state_field in states:
            out[revision.state_field].append(
                _change(revision, revision.state_field, revision.state_from, revision.state_to)
            )
    return {name: out.get(name, []) for name in states}


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
    history = _history(entity, state_fields)
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
        "holds": held,
        "signatures": list(signatures),
    }
    current = values.get(field) or ""
    expressions: list[tuple[str, str]] = []
    at: list[tuple[State, Guard, int, int | None, int]] = []
    for state in guarded:
        for guard in state.guards.values():
            check = len(expressions)
            expressions.append((guard.check, spec_rules.CONDITION))
            when = None
            if guard.when is not None:
                when = len(expressions)
                expressions.append((guard.when, spec_rules.CONDITION))
            message = len(expressions)
            expressions.append((guard.message, spec_rules.MESSAGE))
            at.append((state, guard, check, when, message))
    contexts = [
        {**base, "state": {"field": field, "from": current, "to": state.name}} for state in guarded
    ]
    results = spec_rules.evaluate(variables_of(type_name, field), expressions, contexts)
    out: dict[str, Refusal] = {}
    for index, state in enumerate(guarded):
        outcomes = results[index]
        for owner, guard, check, when, message in at:
            if owner is not state or state.name in out:
                continue
            if when is not None:
                w = outcomes[when]
                if w.error is None and not w.value:
                    continue
                if w.error is not None and w.empty:
                    continue
            result = outcomes[check]
            if result.error is None and result.value:
                continue
            text = outcomes[message].text or guard.message
            if result.error is not None and not result.empty:
                log.warning(
                    "entities.guard_unworkable",
                    type=type_name,
                    guard=guard.name,
                    error=result.error,
                )
                text = f"{text} (It couldn’t be checked: {result.error})"
            out[state.name] = Refusal(guard, text)
    return out
