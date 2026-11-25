"""The events webhooks receive, and how audited changes become them.

Every change Iki makes is audited in its transaction (ADR 0025); the
changes listed in ``FROM_AUDIT`` also become webhook events there, so a
webhook message exists exactly when its change does, whichever way the
change was made (the web, the API, SCIM, a command).

Payloads follow the Standard Webhooks shape, ``{"type", "timestamp",
"data"}``. ``data`` holds stable representations of what changed, plus
``actor``: who changed it (null for Iki itself or the identity provider).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db import models

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.audit.models import AuditEvent
from iki_server.org.models import Project, ProjectRole, Team


@dataclass(frozen=True)
class EventType:
    key: str
    description: str
    #: Sent to the project's webhooks.
    project: bool
    #: Sent to the organization's webhooks.
    org: bool


EVENTS: dict[str, EventType] = {}


def _register(*events: EventType) -> None:
    for event in events:
        EVENTS[event.key] = event


PING = "ping"

_register(
    EventType(PING, "A test message: when the webhook is added, and when you ask.", True, True),
    EventType("project.created", "Someone creates a project.", project=False, org=True),
    EventType(
        "project.updated", "Someone renames or redescribes the project.", project=True, org=True
    ),
    EventType("project.archived", "Someone archives the project.", project=True, org=True),
    EventType("project.restored", "Someone restores the project.", project=True, org=True),
    EventType(
        "project.sharing_allowed",
        "Someone allows sharing the project's entities with other projects.",
        project=True,
        org=True,
    ),
    EventType(
        "project.sharing_restricted",
        "Someone stops the project's entities being shared with other projects.",
        project=True,
        org=True,
    ),
    EventType(
        "project.member_added",
        "Someone gives a person or team a role in the project.",
        project=True,
        org=True,
    ),
    EventType(
        "project.member_role_changed",
        "Someone changes a person's or team's role in the project.",
        project=True,
        org=True,
    ),
    EventType(
        "project.member_removed",
        "Someone removes a person or team from the project.",
        project=True,
        org=True,
    ),
    EventType(
        "project.lab_role_granted",
        "Someone gives a member of the project, a person or a team, a lab role such as QA.",
        project=True,
        org=True,
    ),
    EventType(
        "project.lab_role_revoked",
        "Someone takes a lab role away from a member of the project. Removing a member takes "
        "theirs away too, told as project.member_removed with their lab_roles.",
        project=True,
        org=True,
    ),
    EventType("entity.created", "Someone, or a run, creates an entity.", project=True, org=True),
    EventType("entity.updated", "Someone, or a run, changes an entity.", project=True, org=True),
    EventType(
        "entity.imported",
        "A run imports entities from a file, too many to tell of one by one: one event for "
        "all of them, in place of entity.created, or entity.updated for the records it "
        "matched, for each.",
        project=True,
        org=True,
    ),
    EventType(
        "bundle.imported",
        "An Org Admin imports a bundle from another deployment: one event for all its "
        "entities, with counts, in place of entity.created or entity.updated for each.",
        project=True,
        org=True,
    ),
    EventType("entity.archived", "Someone archives an entity.", project=True, org=True),
    EventType("entity.restored", "Someone restores an entity.", project=True, org=True),
    EventType(
        "entity.state_changed",
        "Someone sets one of a record's state fields, with `state`: which field, and the "
        "states it went from and to (ADR 0125).",
        project=True,
        org=True,
    ),
    EventType(
        "entity.held",
        "A save, a run's results or a deploy's check breaks a rule that holds records for "
        "review, such as a result outside its specification (ADR 0127): `hold`, with the "
        "rule and the reason it was saved anyway.",
        project=True,
        org=True,
    ),
    EventType(
        "entity.hold_reviewed",
        "Someone reviews a record's hold (ADR 0127), with why it may stand: `hold`.",
        project=True,
        org=True,
    ),
    EventType(
        "entity.amount_moved",
        "A record's amount changes (ADR 0139). Organization webhooks identify the record, "
        "movement kind, actor and agent. Project webhooks also give the change and balance "
        "in the record's unit, the requested amount, purpose, reason, operation and "
        "counterpart. A counterpart outside the project is `unreadable`.",
        project=True,
        org=True,
    ),
    EventType(
        "storage.moved",
        "A record moves in storage or changes custody (ADR 0180). Organization webhooks "
        "identify the record, movement kind, actor and agent. Project webhooks also give "
        "its previous and new container and position (null when hidden from the project), "
        "custodian, operation and reason.",
        project=True,
        org=True,
    ),
    EventType(
        "request.receipt_completed",
        "A service completes a receipt of a request's material (ADR 0180): the `request`, "
        "the `service`, and how many of its lines were matched, missing, extra, damaged or "
        "rejected (`outcomes`).",
        project=True,
        org=True,
    ),
    EventType(
        "storage.reconciled",
        "Someone completes a reconciliation of a container's contents with what's recorded "
        "in it (ADR 0180): how many were matched, missing, unexpected or in the wrong "
        "position (`findings`), and how many moved.",
        project=True,
        org=True,
    ),
    EventType(
        "entity.assigned",
        "Someone names people in a record's assignment field, or takes them off it (ADR "
        "0122): `field`, and who was `added` and `removed`.",
        project=True,
        org=True,
    ),
    EventType("entity.linked", "Someone adds an entity to the project.", project=True, org=True),
    EventType(
        "request.submitted",
        "Someone sends one of the project's requests to another project's service (ADR 0132): "
        "`service`, and the records it sent.",
        project=True,
        org=True,
    ),
    EventType(
        "request.received",
        "A request comes to the project, as a service, from another project (ADR 0132): "
        "`asking_project`, and the records it sent.",
        project=True,
        org=True,
    ),
    EventType(
        "request.result_released",
        "A service releases a result of one of the project's requests back to it (ADR 0132): "
        "the result, its `request` and `service`.",
        project=True,
        org=True,
    ),
    EventType(
        "project.intake_changed",
        "Someone changes the request types the project takes as a service, or who from (ADR 0132).",
        project=True,
        org=True,
    ),
    EventType(
        "entity.unlinked", "Someone takes an entity out of the project.", project=True, org=True
    ),
    EventType(
        "entry.updated",
        "A registry entry the project has a record of gets values (ADR 0115): filled in by a "
        "holder, or a fact changed by a registrar.",
        project=True,
        org=True,
    ),
    EventType(
        "entry.superseded",
        "A registrar supersedes a registry entry the project has a record of with another.",
        project=True,
        org=True,
    ),
    EventType(
        "budget.threshold_reached",
        "Spending on runs passes one of a budget's warning levels for the month: the "
        "project's own budget, for a project's webhooks.",
        project=True,
        org=True,
    ),
    EventType("team.created", "Someone creates a team.", project=False, org=True),
    EventType("team.updated", "Someone renames or redescribes a team.", project=False, org=True),
    EventType("team.archived", "Someone archives a team.", project=False, org=True),
    EventType("team.restored", "Someone restores a team.", project=False, org=True),
    EventType(
        "team.member_added",
        "Someone, or the identity provider, adds a person to a team.",
        project=False,
        org=True,
    ),
    EventType(
        "team.member_removed",
        "Someone, or the identity provider, removes a person from a team.",
        project=False,
        org=True,
    ),
    EventType(
        "user.created",
        "A person signs in for the first time, or the identity provider provisions them.",
        project=False,
        org=True,
    ),
    EventType("user.deactivated", "Someone deactivates a person.", project=False, org=True),
    EventType("user.activated", "Someone reactivates a person.", project=False, org=True),
    EventType("org_role.granted", "Someone gives a person an org role.", project=False, org=True),
    EventType("org_role.revoked", "Someone takes an org role away.", project=False, org=True),
)


def for_scope(scope: str) -> list[EventType]:
    """The events a webhook of this scope (``project`` or ``org``) may subscribe to."""
    return [e for e in EVENTS.values() if (e.project if scope == "project" else e.org)]


# --- Representations ---------------------------------------------------------------


def _time(value: Any) -> str | None:  # noqa: ANN401 - a datetime or None
    return value.isoformat() if value else None


def team_data(team: Team) -> dict[str, Any]:
    return {
        "id": str(team.pk),
        "name": team.name,
        "slug": team.slug,
        "description": team.description,
        "archived": team.is_archived,
    }


def project_data(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.pk),
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "team": {"id": str(project.team.pk), "name": project.team.name, "slug": project.team.slug},
        "archived": project.is_archived,
        "allow_sharing": project.allow_sharing,
        "created_at": _time(project.created_at),
    }


def user_data(user: User) -> dict[str, Any]:
    return {
        "id": str(user.pk),
        "email": user.email,
        "name": user.get_full_name(),
        "kind": "service" if user.is_service_account else "person",
        "active": user.is_active,
    }


def entity_data(entity: Any) -> dict[str, Any]:  # noqa: ANN401 - an Entity
    """Identifiers only, never values: receivers read those through the API, with their own
    access, so a webhook can't send a project's data out of Iki (ADR 0035)."""
    return {
        "id": str(entity.pk),
        "hrid": entity.hrid,
        "type": entity.type,
        "revision": entity.revision,
        "owner_project": {"id": str(entity.owner_project_id)},
        "archived": entity.archived_at is not None,
        "entry": {"id": str(entity.entry_id)} if entity.entry_id is not None else None,
    }


def entry_data(entry: Any) -> dict[str, Any]:  # noqa: ANN401 - an Entity
    """A registry entry's identifiers (ADR 0115): never its values, nor who holds it."""
    return {
        "id": str(entry.pk),
        "hrid": entry.hrid,
        "type": entry.type,
        "revision": entry.revision,
        "registry": {"id": str(entry.registry_id)},
        "superseded": entry.superseded_by_id is not None,
    }


def actor_data(actor: User | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {"id": str(actor.pk), "email": actor.email, "name": actor.get_full_name()}


_ROLE_NAMES = {str(role.label): role.name.lower() for role in ProjectRole}


def _member(target: models.Model | None) -> dict[str, Any]:
    if isinstance(target, Team):
        return {"type": "team", "team": team_data(target)}
    if isinstance(target, User):
        return {"type": "user", "user": user_data(target)}
    return {"type": "unknown"}


# --- From audit events ---------------------------------------------------------------

#: What an audited change sends: (event type, data) or None to send nothing.
Builder = Callable[[AuditEvent, models.Model | None], tuple[str, dict[str, Any]] | None]


def _project(key: str) -> Builder:
    def build(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(target, Project):
            return None
        data: dict[str, Any] = {"project": project_data(target)}
        if key == "project.updated":
            data["changes"] = {
                field: {"from": change[0], "to": change[1]}
                for field, change in event.data.items()
                if isinstance(change, list) and len(change) == 2
            }
        return key, data

    return build


def _membership(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]]:
    data: dict[str, Any] = {
        "project": project_data(event.project) if event.project else None,
        "member": _member(target),
        "role": _ROLE_NAMES.get(str(event.data.get("role"))),
    }
    if event.action == "project.member_removed":
        data["lab_roles"] = list(event.data.get("lab_roles", []))
        return "project.member_removed", data
    previous = event.data.get("previous_role")
    if previous is None:
        return "project.member_added", data
    data["previous_role"] = _ROLE_NAMES.get(str(previous))
    return "project.member_role_changed", data


def _lab_role(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]]:
    return event.action, {
        "project": project_data(event.project) if event.project else None,
        "member": _member(target),
        "lab_role": {"name": event.data.get("role"), "label": event.data.get("label")},
    }


def _team(key: str) -> Builder:
    def build(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
        team = target if isinstance(target, Team) else event.team
        if team is None:
            return None
        data: dict[str, Any] = {"team": team_data(team)}
        if key == "team.updated":
            data["changes"] = {
                field: {"from": change[0], "to": change[1]}
                for field, change in event.data.items()
                if isinstance(change, list) and len(change) == 2
            }
        if key in ("team.member_added", "team.member_removed") and isinstance(target, User):
            data["user"] = user_data(target)
        return key, data

    return build


def _user(key: str) -> Builder:
    def build(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
        if not isinstance(target, User) or (key == "user.created" and target.is_service_account):
            return None
        data: dict[str, Any] = {"user": user_data(target)}
        if key.startswith("org_role."):
            data["role"] = event.data.get("role")
        return key, data

    return build


def _entity_link(
    event: AuditEvent, target: models.Model | None
) -> tuple[str, dict[str, Any]] | None:
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    return event.action, {"entity": entity_data(target), "project": project_data(event.project)}


def _budget(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
    from iki_server.runs.models import Budget

    if not isinstance(target, Budget):
        return None
    scope: dict[str, Any] = {"kind": "organization"}
    if target.project is not None:
        scope = {"kind": "project", "project": project_data(target.project)}
    elif target.team is not None:
        scope = {"kind": "team", "team": team_data(target.team)}
    keys = ("threshold", "month", "spent", "amount", "currency", "enforce")
    return event.action, {
        "budget": {"id": str(target.pk), "scope": scope},
        **{k: event.data[k] for k in keys if k in event.data},
    }


def _assigned(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    found = {
        str(u.pk): u
        for u in User.objects.filter(
            pk__in=[p["id"] for k in ("added", "removed") for p in event.data.get(k, [])]
        )
    }
    return event.action, {
        "entity": entity_data(target),
        "project": project_data(event.project),
        "field": event.data.get("field"),
        "label": event.data.get("label"),
        **{
            k: [user_data(found[p["id"]]) for p in event.data.get(k, []) if p["id"] in found]
            for k in ("added", "removed")
        },
        "revision": event.data.get("revision"),
        "group": event.data.get("group"),
    }


def _request(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
    """A request sent, received, or a result released (ADR 0132): identifiers, never values."""
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    data: dict[str, Any] = {"entity": entity_data(target), "project": project_data(event.project)}
    for key in ("service", "asking_project"):
        if key in event.data:
            data[key] = {"id": event.data[key]["id"], "name": event.data[key]["name"]}
    if "sent" in event.data:
        data["sent"] = [{"id": i} for i in event.data["sent"]]
    if "request" in event.data:
        data["request"] = {"id": event.data["request"], "hrid": event.data.get("request_hrid")}
    return event.action, data


def _intake(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(target, Project):
        return None
    return event.action, {"project": project_data(target), "changes": event.data}


def _amount_moved(
    event: AuditEvent, target: models.Model | None
) -> tuple[str, dict[str, Any]] | None:
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    keys = (
        "kind",
        "purpose",
        "change",
        "balance",
        "unit",
        "given",
        "counterpart",
        "operation",
        "reason",
    )
    return event.action, {"entity": entity_data(target), **{k: event.data.get(k) for k in keys}}


def _moved(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
    """Where a record went (ADR 0180): identifiers and positions, never values."""
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    keys = ("kind", "from", "to", "custodian", "operation", "reason")
    return event.action, {"entity": entity_data(target), **{k: event.data.get(k) for k in keys}}


def project_movement(data: dict[str, Any], project: Project) -> dict[str, Any]:
    """Keep each endpoint only when the project can read its container."""
    from iki_server.entities import access

    endpoints = {key: data[key] for key in ("from", "to") if data[key] is not None}
    containers = [end["container"] for end in endpoints.values()]
    readable = {
        str(pk)
        for pk in access.in_project(project).filter(pk__in=containers).values_list("pk", flat=True)
    }
    return {
        **data,
        **{key: None for key, end in endpoints.items() if end["container"] not in readable},
    }


def project_amount_movement(data: dict[str, Any], project: Project) -> dict[str, Any]:
    from iki_server.entities import access
    from iki_server.entities.values import UNREADABLE_REFERENCE

    other = data.get("counterpart")
    if other is not None and not access.in_project(project).filter(hrid=other).exists():
        return {**data, "counterpart": UNREADABLE_REFERENCE}
    return data


def _receipt(event: AuditEvent, target: models.Model | None) -> tuple[str, dict[str, Any]] | None:
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    return event.action, {
        "entity": entity_data(target),
        "project": project_data(event.project),
        "receipt": {"id": event.data.get("receipt")},
        "service": event.data.get("service"),
        "outcomes": event.data.get("outcomes"),
    }


def _reconciled(
    event: AuditEvent, target: models.Model | None
) -> tuple[str, dict[str, Any]] | None:
    from iki_server.entities.models import Entity

    if not isinstance(target, Entity) or event.project is None:
        return None
    keys = ("reconciliation", "findings", "moved", "operation")
    return event.action, {"entity": entity_data(target), **{k: event.data.get(k) for k in keys}}


FROM_AUDIT: dict[str, Builder] = {
    "request.submitted": _request,
    "request.received": _request,
    "request.result_released": _request,
    "project.intake_changed": _intake,
    "budget.threshold_reached": _budget,
    "entity.amount_moved": _amount_moved,
    "storage.moved": _moved,
    "request.receipt_completed": _receipt,
    "storage.reconciled": _reconciled,
    "entity.assigned": _assigned,
    "entity.linked": _entity_link,
    "entity.unlinked": _entity_link,
    "project.created": _project("project.created"),
    "project.updated": _project("project.updated"),
    "project.archived": _project("project.archived"),
    "project.restored": _project("project.restored"),
    "project.sharing_allowed": _project("project.sharing_allowed"),
    "project.sharing_restricted": _project("project.sharing_restricted"),
    "project.role_granted": _membership,
    "project.member_removed": _membership,
    "project.lab_role_granted": _lab_role,
    "project.lab_role_revoked": _lab_role,
    "team.created": _team("team.created"),
    "team.updated": _team("team.updated"),
    "team.archived": _team("team.archived"),
    "team.restored": _team("team.restored"),
    "team.member_added": _team("team.member_added"),
    "team.member_removed": _team("team.member_removed"),
    "user.created": _user("user.created"),
    "user.provisioned": _user("user.created"),
    "user.deactivated": _user("user.deactivated"),
    "user.activated": _user("user.activated"),
    "org_role.granted": _user("org_role.granted"),
    "org_role.revoked": _user("org_role.revoked"),
}


def from_audit(event: AuditEvent, target: models.Model | None) -> None:
    """The audit log's listener: sends the change to the webhooks that want it."""
    builder = FROM_AUDIT.get(event.action)
    if builder is None:
        return
    built = builder(event, target)
    if built is None:
        return
    key, data = built
    data["actor"] = actor_data(event.actor)
    data["agent"] = audit.agent_data(event.agent)
    from iki_server.webhooks.services import emit

    project = event.project if EVENTS[key].project else None
    emit(key, data, project=project)
