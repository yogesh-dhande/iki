"""Choosing a type's review procedure (ADR 0302): a project's choice, by its Admin, and the
deployment's default for every project that hasn't chosen, by an Org Admin with a reason.

Any installed procedure that fits the type's needs can be chosen (ADR 0301); one that
doesn't is refused with what it misses. A choice binds each of the procedure's slots to
a lab role, by default its ``iki/lab_roles`` role (ADR 0304). A change supersedes the
current choice, which is kept for the records pinned under it, and is in the audit log.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models.functions import Now

from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.org import lab_roles, permissions
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.reviews import pins, sync
from iki_server.reviews.models import Choice, Procedure
from iki_spec import procedures
from iki_spec.model import ReviewProcedure, ReviewSpec

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.org.models import Project

__all__ = ["MAX_REASON", "choose", "clear", "fitting"]

MAX_REASON = 2000


def _require(user: User, project: Project | None) -> None:
    if project is None:
        permissions.require(user, Action.ORG_MANAGE)
    else:
        permissions.require(user, Action.PROJECT_MANAGE, project)


def _reviewed(type_name: str) -> ReviewSpec:
    found = pins.review_of(type_name)
    if found is None:
        label = registry.types()[type_name].label if type_name in registry.types() else type_name
        raise Refused(f"{label} isn't reviewed: it has no review procedure to choose.")
    review = found[1]
    if review.by is not None:
        label = registry.types()[type_name].label
        raise Refused(
            f"{label} follows the review procedure of its {review.by}: choose that one's.",
            field="entity_type",
        )
    return review


def fitting(type_name: str) -> dict[str, list[str]]:
    """Each installed procedure a plugin declares now, with the flows that fit
    ``type_name`` (those with no problems)."""
    review = _reviewed(type_name)
    label = registry.types()[type_name].label
    out: dict[str, list[str]] = {}
    for qualified, procedure in sync.declared().items():
        flows = [
            f for f in procedure.flows if not procedures.misfits(review, label, procedure, [f])
        ]
        if flows:
            out[qualified] = flows
    return out


def _reason(reason: str, *, required: bool) -> str:
    reason = reason.strip()
    if required and not reason:
        raise Refused(
            "Say why: a deployment's default is a controlled procedure change, kept with "
            "its reason.",
            field="reason",
        )
    if len(reason) > MAX_REASON:
        raise Refused(f"Say why in {MAX_REASON} characters or fewer.", field="reason")
    return reason


def _binding(
    procedure: ReviewProcedure,
    given: Mapping[str, str] | None,
) -> dict[str, str]:
    roles = lab_roles.declared()
    bound = procedures.default_binding(procedure, roles)
    for slot, role in (given or {}).items():
        if slot not in procedure.slots:
            raise Refused(
                f"{procedure.qualified_name} has no slot `{slot}`: it has "
                + ", ".join(f"`{s}`" for s in procedure.slots)
                + ".",
                field="binding",
            )
        if role not in roles:
            raise Refused(f"There's no lab role `{role}`.", field="binding")
        bound[slot] = role
    unbound = [s for s in procedure.slots if s not in bound]
    if unbound:
        raise Refused(
            "Choose who plays "
            + ", ".join(procedure.slots[s].label for s in unbound)
            + ": no lab role of that name is installed.",
            field="binding",
        )
    return bound


def choose(
    user: User,
    *,
    project: Project | None,
    type_name: str,
    procedure: str,
    flow: str,
    binding: Mapping[str, str] | None = None,
    reason: str = "",
    starter: str = "",
) -> Choice:
    """``project``'s choice for ``type_name`` (None: the deployment's default), replacing
    the current one. Raises :class:`Refused` for a procedure that doesn't fit."""
    _require(user, project)
    review = _reviewed(type_name)
    reason = _reason(reason, required=project is None)
    declared = sync.declared()
    found = declared.get(procedure)
    if found is None or not Procedure.objects.filter(qualified=procedure).exists():
        raise Refused(f"There's no review procedure {procedure} installed.", field="procedure")
    if flow not in found.flows:
        raise Refused(
            f"{procedure} has no flow `{flow}`: it has "
            + ", ".join(f"`{f}`" for f in found.flows)
            + ".",
            field="flow",
        )
    label = registry.types()[type_name].label
    misfits = procedures.misfits(review, label, found, [flow])
    if misfits:
        raise Refused(" ".join(misfits), field="procedure")
    bound = _binding(found, binding)
    with transaction.atomic():
        previous = (
            Choice.objects.select_for_update()
            .filter(project=project, entity_type=type_name, superseded_at__isnull=True)
            .first()
        )
        if previous is not None:
            Choice.objects.filter(pk=previous.pk).update(superseded_at=Now())
        choice = Choice.objects.create(
            project=project,
            entity_type=type_name,
            procedure_id=procedure,
            flow=flow,
            binding=bound,
            starter=starter,
            reason=reason,
            chosen_by=user,
        )
        audit.record(
            "review.default_set" if project is None else "review.procedure_chosen",
            actor=user,
            target=choice,
            project=project,
            data={
                "type": type_name,
                "procedure": procedure,
                "flow": flow,
                "binding": bound,
                **({"starter": starter} if starter else {}),
                **({"reason": reason} if reason else {}),
                **(
                    {"was": {"procedure": previous.procedure_id, "flow": previous.flow}}
                    if previous is not None
                    else {}
                ),
            },
        )
    return choice


def clear(user: User, *, project: Project | None, type_name: str, reason: str = "") -> bool:
    """Ends ``project``'s choice for ``type_name`` (None: the deployment's default). Its
    records keep their procedure; new ones follow whatever applies next, or are refused.
    Returns whether there was one."""
    _require(user, project)
    reason = _reason(reason, required=project is None)
    with transaction.atomic():
        current = (
            Choice.objects.select_for_update()
            .filter(project=project, entity_type=type_name, superseded_at__isnull=True)
            .first()
        )
        if current is None:
            return False
        Choice.objects.filter(pk=current.pk).update(superseded_at=Now())
        audit.record(
            "review.default_cleared" if project is None else "review.choice_cleared",
            actor=user,
            target=current,
            project=project,
            data={
                "type": type_name,
                "procedure": current.procedure_id,
                "flow": current.flow,
                **({"reason": reason} if reason else {}),
            },
        )
    return True
