"""Records held by a rule a save broke, until someone reviews them (ADR 0127).

A rule with ``hold`` is a specification, such as a result's limits: a result outside
them is recorded, never refused (an out-of-specification result is data, and refusing it
invites testing into compliance), but only with a reason, and the record is held. A hold
is cleared by a review: a person, not an agent or a service account, with one of the
rule's lab roles in the record's project, and not whoever saved it, says why it may stand,
as the FDA's guidance on OOS results has a supervisor review the investigation.

A held record keeps its hold if it's edited back within the rule: the review is of what
happened. Saving it outside the rule again adds to the same hold. While any hold is open
the record can't be set to a state that locks it, so a lot isn't released while a result
is being investigated.

A deploy that finds records saved before a hold rule breaking it holds them, as the
system, and a run's or a bulk import's results hold with a reason saying which run.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from iki_server.audit import log
from iki_server.entities.models import Entity, RuleHold
from iki_server.entities.values import Invalid
from iki_server.notifications import kinds
from iki_server.org import lab_roles, permissions
from iki_server.org.services import Refused

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.entities.checking import Broken

__all__ = [
    "HELD",
    "MAX_REASON",
    "blocks_locking",
    "hold",
    "open_holds",
    "review",
    "reviewers",
    "why_not_review",
]

#: The longest reason a hold, or its review, keeps.
MAX_REASON = 2000

HELD = kinds.register(
    kinds.Kind(
        "rules.held",
        "A record is held for review",
        "A save breaks a rule that holds records, such as a result outside its "
        "specification, and you have a lab role that reviews it (ADR 0127).",
        group="Work",
    )
)


def open_holds(entity: Entity | Any) -> QuerySet[RuleHold]:  # noqa: ANN401 - an Entity or its ID
    """A record's holds no one has reviewed yet, oldest first."""
    pk = entity.pk if isinstance(entity, Entity) else entity
    return RuleHold.objects.filter(entity_id=pk, reviewed_at__isnull=True).order_by("held_at")


def hold(
    entity: Entity,
    broken: Sequence[Broken],
    *,
    user: User | None,
    reason: str,
    revision: int,
    tell: bool = True,
) -> list[RuleHold]:
    """Holds the record by each of ``broken``'s rules, for the save that made revision
    ``revision``: a new hold, or an open one it adds to. Call it in the save's transaction.
    Tells the audit log, and unless not to ``tell``, the rules' reviewers."""
    reason = reason.strip()
    assert reason, "A hold needs a reason."  # noqa: S101 - the services ask for one first
    agent = log.current_agent() if user is not None else None
    out = []
    for b in broken:
        current = open_holds(entity).filter(rule=b.rule.key).select_for_update().first()
        if current is not None:
            current.last_revision = revision
            current.message = b.message
            current.save(update_fields=["last_revision", "message"])
            out.append(current)
            continue
        made = RuleHold.objects.create(
            entity=entity,
            rule=b.rule.key,
            label=b.rule.label[:100],
            message=b.message,
            roles=list(b.rule.hold),
            revision=revision,
            last_revision=revision,
            held_by=user,
            held_agent=agent,
            reason=reason,
        )
        out.append(made)
        log.record(
            "entity.held",
            actor=user,
            target=entity,
            project=entity.owner_project,
            data={
                "hold": str(made.pk),
                "rule": made.rule,
                "label": made.label,
                "message": made.message,
                "revision": revision,
                "reason": reason,
            },
        )
        _emit("entity.held", entity, made, user)
        if tell:
            _tell(entity, made, user)
    return out


def hold_data(made: RuleHold) -> dict[str, Any]:
    """A hold as webhooks give it: not its message, which can quote the record's values
    (receivers read those through the API, as ``entity_data`` says)."""
    return {
        "id": str(made.pk),
        "rule": made.rule,
        "label": made.label,
        "reviewed_by": list(made.roles),
        "revision": made.revision,
        "reason": made.reason,
        "held_at": made.held_at.isoformat() if hasattr(made.held_at, "isoformat") else None,
        "reviewed_at": made.reviewed_at.isoformat() if made.reviewed_at else None,
        "review_reason": made.review_reason or None,
    }


def _emit(event: str, entity: Entity, made: RuleHold, user: User | None) -> None:
    from iki_server.webhooks import events
    from iki_server.webhooks.services import emit

    data = {
        "entity": events.entity_data(entity),
        "hold": hold_data(made),
        "actor": events.actor_data(user),
        "agent": log.agent_data(log.current_agent() if user is not None else None),
    }
    emit(event, data, project=entity.owner_project)


def _tell(entity: Entity, made: RuleHold, user: User | None) -> None:
    from iki_server.notifications import services as notifications

    project = entity.owner_project
    if project is None:
        return
    who = reviewers(project, made.roles)
    if user is not None:
        who = who.exclude(pk=user.pk)
    notifications.notify(
        list(who),
        HELD,
        title=f"{entity.hrid} is held for review: {made.label}",
        body=made.message,
        path=f"/entities/{entity.pk}",
        project=project,
        actor=user,
    )


def reviewers(project: Any, roles: Iterable[str]) -> QuerySet[Any]:  # noqa: ANN401 - a Project
    """Active people with one of ``roles`` in the project: who may review a hold."""
    found: QuerySet[Any] | None = None
    for role in roles:
        holders = permissions.holders_of(project, role)
        found = holders if found is None else found | holders
    if found is None:
        from iki_server.accounts.models import User

        return User.objects.none()
    return found.distinct()


def why_not_review(user: User, entity: Entity, made: RuleHold) -> tuple[str, bool] | None:
    """Why ``user`` may not review the hold, and whether it's for who they are (True)
    rather than the hold's own condition (False); None if they may."""
    if made.reviewed_at is not None:
        return "It's been reviewed already.", False
    project = entity.owner_project
    if project is None:
        return "It's a registry entry: its projects' records are held, not it.", False
    if not permissions.can(user, permissions.Action.PROJECT_SET_STATE, project):
        return f"It needs a role in {project.name}.", True
    # A token can't, whatever its scopes: an agent doesn't sign off its own work, nor
    # anyone's (as runs' approvals, ADR 0013).
    if permissions.token_limits(user) is not None or getattr(user, "is_service_account", False):
        return "A person reviews holds, in Iki, not an agent or a service account.", True
    if not permissions.lab_roles(user, project) & set(made.roles):
        roles = " or ".join(lab_roles.label(r) for r in made.roles)
        return f"It needs someone with the {roles} role in {project.name}.", True
    if made.held_by_id is not None and made.held_by_id == user.pk:
        return "Someone else reviews it: you saved what held it.", True
    return None


def review(user: User, entity: Entity, hold_id: Any, reason: str) -> RuleHold:  # noqa: ANN401 - a UUID
    """Reviews a hold: it's cleared, with who, when and why. Raises
    :class:`PermissionDenied` when ``user`` may not, ``RuleHold.DoesNotExist`` when the
    record has no such hold, :class:`Refused` when it can't be now,
    and :class:`Invalid` without a reason."""
    reason = reason.strip()
    if not reason:
        raise Invalid({"reason": ["Reviewing a hold needs a reason: say why it may stand."]})
    if len(reason) > MAX_REASON:
        raise Invalid({"reason": [f"Say why in {MAX_REASON} characters or fewer."]})
    with transaction.atomic():
        locked = (
            Entity.objects.select_for_update(of=("self",))
            .select_related("owner_project")
            .get(pk=entity.pk)
        )
        made = RuleHold.objects.select_for_update().filter(entity=locked, pk=hold_id).first()
        if made is None:
            raise RuleHold.DoesNotExist("It has no such hold.")
        refusal = why_not_review(user, locked, made)
        if refusal is not None:
            why, forbidden = refusal
            raise PermissionDenied(why) if forbidden else Refused(why)
        made.reviewed_by = user
        made.reviewed_at = timezone.now()
        made.review_reason = reason
        made.save(update_fields=["reviewed_by", "reviewed_at", "review_reason"])
        log.record(
            "entity.hold_reviewed",
            actor=user,
            target=locked,
            project=locked.owner_project,
            data={
                "hold": str(made.pk),
                "rule": made.rule,
                "label": made.label,
                "reason": reason,
                "held_by": str(made.held_by_id) if made.held_by_id else None,
            },
        )
        _emit("entity.hold_reviewed", locked, made, user)
        return made


def blocks_locking(entity: Entity) -> str | None:
    """Why the record can't be set to a state that locks it now: the holds open on it."""
    labels = list(open_holds(entity).values_list("label", flat=True)[:5])
    if not labels:
        return None
    named = ", ".join(f"“{label}”" for label in labels)
    return (
        f"{entity.hrid} is held for review ({named}): it can't be set to a state that locks "
        "it until its holds are reviewed."
    )


def run_reason(run: Any) -> str:  # noqa: ANN401 - an ActionRun
    """Why a run's results are saved though they break a rule that holds: they're what it
    measured, kept for review."""
    return f"Recorded by {run.hrid}, a run of {run.action}: its results are kept as they are."


#: Records a bulk import's check holds at a time.
IMPORT_CHUNK = 500


def hold_imported(run: Any) -> int:  # noqa: ANN401 - an ActionRun
    """Holds the records a bulk import (ADR 0056) created that break rules that hold, as
    each line was checked to: by the run, with its reason. One notification for all of
    them, to each rule's reviewers. Returns how many it held."""
    from iki_server.entities import checking, services
    from iki_server.notifications import services as notifications
    from iki_server.runs.models import EffectKind, RunEffect

    created = RunEffect.objects.filter(run=run, kind=EffectKind.CREATED).values("entity_id")
    entities = Entity.objects.filter(pk__in=created).select_related("owner_project")
    types = [t for t in entities.values_list("type", flat=True).distinct() if _holding(t)]
    if not types:
        return 0
    reason = run_reason(run)
    count = 0
    roles: set[str] = set()
    for type_name in sorted(types):
        ids = list(entities.filter(type=type_name).values_list("pk", flat=True))
        for start in range(0, len(ids), IMPORT_CHUNK):
            chunk = list(entities.filter(pk__in=ids[start : start + IMPORT_CHUNK]))
            values = services.current_values_of(chunk)
            savings = [
                checking.Saving(values.get(e.pk, {}).get, id=e.pk, hrid=e.hrid) for e in chunk
            ]
            for entity, found in zip(chunk, checking.broken(type_name, savings), strict=True):
                held = [b for b in found if b.held]
                if held:
                    hold(
                        entity,
                        held,
                        user=run.requested_by,
                        reason=reason,
                        revision=entity.revision,
                        tell=False,
                    )
                    roles.update(r for b in held for r in b.rule.hold)
                    count += 1
    if count:
        notifications.notify(
            list(reviewers(run.project, roles)),
            HELD,
            title=f"{run.hrid} held {count:,} records for review",
            body="Their results break rules that hold records, such as specifications.",
            path=f"/runs/{run.hrid}/",
            project=run.project,
            actor=run.requested_by,
        )
    return count


def _holding(type_name: str) -> bool:
    from iki_server.entities import checking, registry

    return type_name in registry.types() and any(r.hold for r in checking.checks_of(type_name))
