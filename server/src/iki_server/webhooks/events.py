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
    EventType("entity.created", "Someone, or a run, creates an entity.", project=True, org=True),
    EventType("entity.updated", "Someone, or a run, changes an entity.", project=True, org=True),
    EventType(
        "entity.imported",
        "A run imports entities from a file, too many to tell of one by one: one event for "
        "all of them, in place of entity.created for each.",
        project=True,
        org=True,
    ),
    EventType("entity.archived", "Someone archives an entity.", project=True, org=True),
    EventType("entity.restored", "Someone restores an entity.", project=True, org=True),
    EventType("entity.linked", "Someone adds an entity to the project.", project=True, org=True),
    EventType(
        "entity.unlinked", "Someone takes an entity out of the project.", project=True, org=True
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
        return "project.member_removed", data
    previous = event.data.get("previous_role")
    if previous is None:
        return "project.member_added", data
    data["previous_role"] = _ROLE_NAMES.get(str(previous))
    return "project.member_role_changed", data


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


FROM_AUDIT: dict[str, Builder] = {
    "budget.threshold_reached": _budget,
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
