"""Starters plugins declare (``kind: Starter``, ADR 0303): a project's Admin applies one to
choose a type's review procedure and flow (ADR 0302) and give the lab roles its slots
are bound to (ADR 0304).

Nothing here restates the procedure. The roles a flow requires are those bound to the
slots its states name; who must be different people is read from its states' ``maker``
and ``independent_of`` (:func:`iki_spec.procedures.staffing`), and the chosen people
are checked to be able to fill every slot with those pairs apart.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction

from iki_server.accounts.models import Kind, User
from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.org import lab_roles, permissions, services
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.reviews import choices, pins, sync
from iki_server.reviews.models import Choice
from iki_server.setup.starters import PlanChanged
from iki_spec import procedures
from iki_spec.model import ReviewProcedure, Starter

__all__ = [
    "PlanChanged",
    "apply",
    "binding",
    "clear_default",
    "declared",
    "describe",
    "preview",
    "set_default",
]

MINIMUM = {"viewer": ProjectRole.VIEWER, "editor": ProjectRole.EDITOR}


def binding(
    starter: Starter, procedure: ReviewProcedure, slots: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Each of the procedure's slots, to the lab role that plays it (ADR 0304): as
    ``slots`` choose, else as the starter binds it, else the ``iki/lab_roles`` role of its
    name. Raises :class:`Refused` for a slot it hasn't or a role no plugin declares."""
    bound = procedures.default_binding(procedure, lab_roles.declared())
    for slot, given in starter.slots.items():
        if given.role is not None:
            bound[slot] = given.role
    roles = lab_roles.declared()
    for slot, role in (slots or {}).items():
        if slot not in procedure.slots:
            raise Refused(
                f"{procedure.qualified_name} has no slot `{slot}`: it has "
                + ", ".join(f"`{s}`" for s in procedure.slots)
                + ".",
                field="slots",
            )
        if role not in roles:
            raise Refused(f"There's no lab role `{role}`.", field="slots")
        bound[slot] = role
    return bound


def _staffing(starter: Starter, procedure: ReviewProcedure, flow: str) -> procedures.Staffing:
    """Who ``flow`` needs for the starter's type: a derived record's only its last step."""
    review = pins.review_of(starter.governs)
    return procedures.staffing(procedure, flow, review[1] if review is not None else None)


def _used(starter: Starter, procedure: ReviewProcedure) -> list[str]:
    """The slots any of its flows needs someone for, in the procedure's order."""
    used = {s for f in starter.flows for s in _staffing(starter, procedure, f).slots}
    return [s for s in procedure.slots if s in used]


def declared() -> dict[str, tuple[Starter, ReviewProcedure]]:
    """The starters an active plugin declares whose type, procedure and roles are
    installed, by qualified name."""
    types = registry.types()
    detached = registry.detached()
    installed = sync.declared()
    roles = lab_roles.declared()
    out: dict[str, tuple[Starter, ReviewProcedure]] = {}
    for plugin in registry.plugins().values():
        if plugin.name in detached:
            continue
        for qualified, starter in plugin.starters.items():
            procedure = installed.get(starter.procedure)
            governed = types.get(starter.governs)
            if procedure is None or governed is None or governed.plugin in detached:
                continue
            bound = binding(starter, procedure)
            if all(bound.get(s) in roles for s in _used(starter, procedure)):
                out[qualified] = (starter, procedure)
    return out


def _found(qualified: str) -> tuple[Starter, ReviewProcedure]:
    from django.http import Http404

    found = declared().get(qualified)
    if found is None:
        raise Http404("This starter is not installed.")
    return found


def _flow_out(
    starter: Starter, procedure: ReviewProcedure, flow: str, bound: Mapping[str, str]
) -> dict[str, Any]:
    review = pins.review_of(starter.governs)
    spec = review[1] if review is not None else None
    plan = _staffing(starter, procedure, flow)
    return {
        "id": flow,
        "label": procedure.flows[flow].label,
        "result_type": starter.governs,
        "states": list(procedures.route(procedure.flows[flow], spec)),
        "required_roles": sorted({bound[s] for s in plan.slots}),
        "independent_roles": sorted({bound[s] for s in plan.distinct_slots}),
        "summary": starter.summaries.get(flow) or procedure.flows[flow].summary,
        "steps": [
            {"state": st, "label": procedure.states[st].label, "slots": list(slots)}
            for st, slots in plan.steps.items()
        ],
        "apart": sorted(sorted(pair, key=list(plan.steps).index) for pair in plan.apart),
    }


def _made_by(choice: Choice | None, starter: Starter) -> bool:
    return choice is not None and choice.starter.rpartition("@")[0] == starter.qualified_name


def _current(project: Project | None, starter: Starter) -> Choice | None:
    """The current choice for the starter's type in ``project`` (None: the deployment's
    default), whichever starter or person made it."""
    return (
        Choice.objects.filter(
            project=project, entity_type=starter.governs, superseded_at__isnull=True
        )
        .select_related("chosen_by")
        .first()
    )


def _choice_out(choice: Choice | None, starter: Starter) -> dict[str, Any] | None:
    if choice is None:
        return None
    return {
        "procedure": choice.procedure_id,
        "flow": choice.flow,
        "version": choice.starter.rpartition("@")[2] if _made_by(choice, starter) else None,
        "slots": dict(choice.binding),
        "chosen_by": choice.chosen_by.name or choice.chosen_by.email,
        "chosen_at": choice.chosen_at,
        "reason": choice.reason,
    }


def _plural(label: str) -> str:
    """A type's plural label as a sentence has it: "proteomics results", but "QC samples"."""
    return label if label[1:2].isupper() else label[:1].lower() + label[1:]


def describe(
    starter: Starter, procedure: ReviewProcedure, project: Project | None
) -> dict[str, Any]:
    """The starter as ``GET /starters`` lists it, with the current choices for its type: the
    project's and the deployment's default, whoever made them."""
    labels = lab_roles.labels()
    ours = binding(starter, procedure)
    chosen = _current(project, starter) if project is not None else None
    applied = chosen if _made_by(chosen, starter) else None
    default = _current(None, starter)
    # Setup starts from the project's choice by it, else the deployment's default by it
    # (ADR 0302), else as the starter binds its slots.
    start = applied if applied is not None else default if _made_by(default, starter) else None
    bound = {**ours, **(start.binding if start is not None else {})}
    governed = registry.types()[starter.governs]
    used = _used(starter, procedure)

    def minimum(slot: str) -> str:
        return starter.slots[slot].minimum if slot in starter.slots else "viewer"

    # One row per role: a role playing two slots needs the higher of their minimums.
    roles: dict[str, str] = {}
    for slot in used:
        role = bound[slot]
        roles[role] = max(roles.get(role, "viewer"), minimum(slot), key=MINIMUM.__getitem__)
    return {
        "id": starter.qualified_name,
        "version": starter.version,
        "label": starter.label,
        "description": starter.description or "",
        "governs": _plural(governed.label_plural or governed.label + "s"),
        "procedure": starter.procedure,
        "suggested": starter.suggested,
        "flows": [_flow_out(starter, procedure, f, bound) for f in starter.flows],
        "types": [starter.governs],
        "roles": [
            {"id": role, "label": labels.get(role, role), "minimum_project_role": least}
            for role, least in roles.items()
        ],
        "slots": [
            {
                "id": slot,
                "label": procedure.slots[slot].label,
                "description": procedure.slots[slot].description or "",
                "role": bound[slot],
                "starter_role": ours[slot],
                "minimum_project_role": minimum(slot),
            }
            for slot in used
        ],
        "applied": {"version": applied.starter.rpartition("@")[2], "flow": applied.flow}
        if applied is not None
        else None,
        "chosen": _choice_out(chosen, starter),
        "default": _choice_out(default, starter),
    }


def set_default(
    by: User,
    qualified: str,
    version: str,
    flow: str,
    *,
    slots: Mapping[str, str] | None,
    reason: str,
) -> dict[str, Any]:
    """The deployment's default for the starter's type (ADR 0302): an Org Admin's, with a
    reason, for every project that hasn't chosen."""
    permissions.require(by, Action.ORG_MANAGE)
    starter, procedure = _found(qualified)
    _version(starter, version)
    if flow not in starter.flows:
        raise Refused("Choose one of the starter's review flows.", field="flow")
    choices.choose(
        by,
        project=None,
        type_name=starter.governs,
        procedure=starter.procedure,
        flow=flow,
        binding=binding(starter, procedure, slots),
        reason=reason,
        starter=f"{qualified}@{version}",
    )
    return {"starter": qualified, "default": _choice_out(_current(None, starter), starter)}


def clear_default(by: User, qualified: str, *, reason: str) -> dict[str, Any]:
    """Ends the deployment's default for the starter's type, whichever starter set it."""
    permissions.require(by, Action.ORG_MANAGE)
    starter, _ = _found(qualified)
    choices.clear(by, project=None, type_name=starter.governs, reason=reason)
    return {"starter": qualified, "default": None}


def _version(starter: Starter, version: str) -> None:
    if version != starter.version:
        raise Refused(
            f"The installed version of {starter.qualified_name} is {starter.version}. "
            "Preview it before applying.",
            field="version",
        )


RoleAssignments = Mapping[str, Sequence[UUID]]


def _members(
    project: Project,
    starter: Starter,
    procedure: ReviewProcedure,
    flow: str,
    assignments: dict[str, set[UUID]],
    bound: Mapping[str, str],
) -> dict[UUID, ProjectMembership]:
    plan = _staffing(starter, procedure, flow)
    offered = {bound[s] for s in _used(starter, procedure)}
    for role in assignments:
        if role not in offered:
            raise Refused(f"{role} is not a role in this starter.", field="role_assignments")
    for state, slots in plan.steps.items():
        if not any(assignments.get(bound[s]) for s in slots):
            names = sorted({lab_roles.label(bound[s]) for s in slots})
            raise Refused(
                f"Choose a project member for {' or '.join(names)}: they move records to "
                f"{procedure.states[state].label}.",
                field="role_assignments",
            )
    member_ids = {pk for group in assignments.values() for pk in group}
    memberships = (
        ProjectMembership.objects.filter(
            project=project, pk__in=member_ids, user__kind=Kind.PERSON, user__is_active=True
        )
        .select_related("user", "project")
        .prefetch_related("lab_roles")
        .in_bulk()
    )
    if memberships.keys() != member_ids:
        raise Refused(
            "Choose active people with a direct membership in the project.",
            field="role_assignments",
        )
    minimums = {
        bound[s]: max(
            (
                MINIMUM[starter.slots[t].minimum]
                for t in plan.slots
                if bound[t] == bound[s] and t in starter.slots
            ),
            default=ProjectRole.VIEWER,
        )
        for s in plan.slots
    }
    for role, group in assignments.items():
        minimum = minimums.get(role, ProjectRole.VIEWER)
        for key in group:
            member = memberships[key]
            if (
                member.role < minimum
                and (permissions.project_role(member.user, project) or 0) < minimum
            ):
                raise Refused(
                    f"{lab_roles.label(role)} requires {minimum.label} or higher.",
                    field="role_assignments",
                )
    people = {
        s: {memberships[k].user_id for k in assignments.get(bound[s], ())} for s in plan.slots
    }
    if not procedures.can_staff(plan, people):
        apart = sorted({bound[s] for s in plan.distinct_slots})
        names = [lab_roles.label(r) for r in apart]
        raise Refused(
            "Choose enough different people that "
            + ", ".join(names[:-1])
            + f" and {names[-1]} can each be someone else: "
            + "the procedure has each sign independently of the others.",
            field="role_assignments",
        )
    return memberships


@dataclass
class _Plan:
    starter: Starter
    procedure: ReviewProcedure
    current: Choice | None
    members: dict[UUID, ProjectMembership]
    grants: dict[UUID, set[str]]
    result: dict[str, Any]


def _plan(
    project: Project,
    qualified: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    by: User,
    slots: Mapping[str, str] | None,
) -> _Plan:
    permissions.require(by, Action.PROJECT_MANAGE, project)
    if project.is_archived:
        raise Refused("Restore the project before applying a starter.", field="project")
    starter, procedure = _found(qualified)
    _version(starter, version)
    if flow not in starter.flows:
        raise Refused("Choose one of the starter's review flows.", field="flow")
    bound = binding(starter, procedure, slots)
    selected = {role: set(ids) for role, ids in role_assignments.items()}
    members = _members(project, starter, procedure, flow, selected, bound)
    held = {key: {g.role for g in m.lab_roles.all()} for key, m in members.items()}
    grants: dict[UUID, set[str]] = defaultdict(set)
    missing: dict[str, list[str]] = {}
    assignments: dict[str, list[str]] = {}
    for role, group in sorted(selected.items()):
        assignments[role] = sorted(str(pk) for pk in group)
        for pk in sorted(group):
            if role not in held[pk]:
                grants[pk].add(role)
                missing.setdefault(role, []).append(str(pk))
    current = Choice.objects.filter(
        project=project, entity_type=starter.governs, superseded_at__isnull=True
    ).first()
    stamp = f"{qualified}@{version}"
    result: dict[str, Any] = {
        "starter": qualified,
        "version": version,
        "project": str(project.pk),
        "flow": flow,
        "result_type": starter.governs,
        "changed": current is None
        or current.starter != stamp
        or current.flow != flow
        or current.procedure_id != starter.procedure
        or current.binding != bound
        or bool(grants),
        "role_assignments": assignments,
        "slots": bound,
        "grants_to_add": missing,
        "replaces": (
            {
                "procedure": current.procedure_id,
                "flow": current.flow,
                "starter": current.starter or None,
            }
            if current is not None and current.starter != stamp
            else None
        ),
    }
    before = (
        {"starter": current.starter, "procedure": current.procedure_id, "flow": current.flow}
        if current is not None
        else None
    )
    text = json.dumps(
        {**result, "before": before}, sort_keys=True, separators=(",", ":"), default=str
    )
    result["plan"] = hashlib.sha256(text.encode()).hexdigest()
    return _Plan(starter, procedure, current, members, grants, result)


def preview(
    project: Project,
    qualified: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    *,
    by: User,
    slots: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return _plan(project, qualified, version, flow, role_assignments, by, slots).result


@transaction.atomic
def apply(
    project: Project,
    qualified: str,
    version: str,
    flow: str,
    role_assignments: RoleAssignments,
    *,
    by: User,
    plan: str,
    slots: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Applies what a preview returned (ADR 0207): the project's choice of procedure and
    flow for the starter's type, and the lab role grants its people need. Records already
    made keep their procedure (ADR 0302)."""
    project = Project.objects.select_for_update().get(pk=project.pk)
    permissions.forget(by)
    planned = _plan(project, qualified, version, flow, role_assignments, by, slots)
    result = planned.result
    if not result["changed"]:
        return result
    if result["plan"] != plan:
        raise PlanChanged(result)
    for key, roles in planned.grants.items():
        membership = planned.members[key]
        held = {g.role for g in membership.lab_roles.all()}
        services.set_lab_roles(membership, held | roles, by=by)
    starter = planned.starter
    choices.choose(
        by,
        project=project,
        type_name=starter.governs,
        procedure=starter.procedure,
        flow=flow,
        binding=result["slots"],
        starter=f"{qualified}@{version}",
    )
    audit.record(
        "starter.applied",
        actor=by,
        project=project,
        data={
            "starter": qualified,
            "before": result["replaces"],
            "after": {"version": version, "flow": flow, "procedure": starter.procedure},
            "role_assignments": result["role_assignments"],
            "grants_added": result["grants_to_add"],
            "plan": result["plan"],
        },
    )
    return result
