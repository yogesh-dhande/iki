"""Your notifications, how you're told of each kind, and the records you watch."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema

from iki_server.api.common import caller, require_scope
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.notifications import kinds, services, watches
from iki_server.notifications.models import Notification

router = Router(tags=["notifications"])

DeliveryName = Literal["email", "in_app", "off"]


class NotificationProject(Schema):
    id: UUID
    name: str
    slug: str


class NotificationActor(Schema):
    id: UUID
    name: str

    @staticmethod
    def resolve_name(user: Any) -> str:  # noqa: ANN401
        return str(user.get_full_name())


class NotificationAgent(Schema):
    """The agent that made the change, for the actor (ADR 0072)."""

    client_id: str
    name: str = Field(description="The app's name, as it was when it acted.")

    @staticmethod
    def resolve_name(agent: Any) -> str:  # noqa: ANN401
        return str(agent.client_name)


class NotificationRecordOut(Schema):
    id: UUID
    hrid: str
    type: str


class NotificationOut(Schema):
    id: UUID
    kind: str = Field(
        description="What it's about, such as project.added, or a type's notification "
        "(ADR 0129), such as iki/example_lims.Sample.in_review."
    )
    kind_label: str = Field(description="What people's settings call its kind.")
    title: str
    body: str
    path: str = Field(description="Where it leads in Iki (a path on this site), or empty.")
    project: NotificationProject | None
    actor: NotificationActor | None = Field(description="Who caused it, if anyone did.")
    agent: NotificationAgent | None = Field(
        description="The agent that caused it for the actor, if one did."
    )
    count: int = Field(
        description="How many records it's about, once several are told of together."
    )
    records: list[NotificationRecordOut] = Field(
        description="The records it's about that you can read now, the first 20, with those "
        "whose change it tells of, such as each released result of a request."
    )
    created_at: datetime
    read_at: datetime | None


class InboxFilters(PageParams):
    unread: bool | None = Field(None, description="true: only unread ones.")


class SummaryOut(Schema):
    unread: int


class ReadIn(Schema):
    read: bool


class MarkAllIn(Schema):
    up_to: datetime | None = Field(
        None,
        description=(
            "Only those created by then: the newest you've seen. Ones that arrive "
            "meanwhile stay unread."
        ),
    )


class MarkAllOut(Schema):
    marked: int
    unread: int


class KindOut(Schema):
    kind: str
    label: str
    description: str
    group: str
    type: str | None = Field(
        description="The type that declares it (ADR 0129), or null for one of Iki's own."
    )
    required: bool = Field(description="Its plugin requires it: it can't be turned off in Iki.")
    can_turn_off: bool = Field(
        description="Whether it can be off: only a type's notification that isn't required."
    )
    per_project: bool = Field(description="Whether it can be chosen for one project.")
    default: DeliveryName = Field(description="How it's told to those who haven't chosen.")
    delivery: DeliveryName = Field(
        description="How you're told of it: in Iki and by email, in Iki only, or not at all."
    )


class ProjectChoiceOut(Schema):
    project: NotificationProject
    kind: str
    delivery: DeliveryName


class SettingsOut(Schema):
    email_available: bool = Field(
        description="Whether this organization sends email. If not, choices are kept for later."
    )
    address: str = Field(description="Where emails go: your address from the identity provider.")
    kinds: list[KindOut]
    projects: list[ProjectChoiceOut] = Field(
        description="How you're told of a type's notifications in single projects, which "
        "overrides your choice everywhere there."
    )


class ProjectChoiceIn(Schema):
    project: UUID
    kind: str
    delivery: DeliveryName | None = Field(
        description="null: as you chose everywhere, in this project too."
    )


class SettingsIn(Schema):
    kinds: dict[str, DeliveryName] = Field(
        default_factory=dict,
        description="Kind → how you're told of it everywhere. Kinds left out keep theirs.",
    )
    projects: list[ProjectChoiceIn] = Field(
        default_factory=list,
        description="How you're told of a type's notification in one project. Others keep theirs.",
    )


class WatchOut(Schema):
    mode: Literal["default", "watching", "muted"] = Field(
        description="default: you're told what names you (a field, its creator, your lab "
        "role). watching: also what its type tells its watchers. muted: nothing but what its "
        "plugin requires."
    )
    can_watch: bool = Field(description="Whether its type tells its watchers anything.")
    can_mute: bool = Field(description="Whether its type sends anything that can be muted.")


class WatchIn(Schema):
    mode: Literal["default", "watching", "muted"]


def _inbox(request: HttpRequest) -> QuerySet[Notification]:
    user = caller(request)
    require_scope(user, "read")
    return services.inbox(user).select_related("project", "actor", "agent")


def _mine(request: HttpRequest, notification_id: UUID) -> Notification:
    found = _inbox(request).filter(pk=notification_id).first()
    if found is None:
        raise Problem(404, "No such notification.")
    return found


def _out(user: Any, notifications: list[Notification]) -> list[dict[str, Any]]:  # noqa: ANN401
    records = services.records_of(user, notifications)
    out = []
    for n in notifications:
        kind = kinds.find(n.kind)
        out.append(
            {
                "id": n.pk,
                "kind": n.kind,
                "kind_label": kind.label if kind is not None else n.kind,
                "title": n.title,
                "body": n.body,
                "path": n.path,
                "project": n.project,
                "actor": n.actor,
                "agent": n.agent,
                "count": n.count,
                "records": [
                    {"id": e.pk, "hrid": e.hrid, "type": e.type} for e in records.get(n.pk, [])
                ],
                "created_at": n.created_at,
                "read_at": n.read_at,
            }
        )
    return out


@router.get("/notifications", response=Page[NotificationOut], summary="Your notifications")
def notifications(request: HttpRequest, filters: Query[InboxFilters]) -> dict[str, Any]:
    """Newest first. Live updates (``GET /api/v1/events``) say when new ones arrive."""
    found = _inbox(request)
    if filters.unread is not None:
        found = found.filter(read_at__isnull=filters.unread)
    paged = page(found, filters, newest_first=True)
    paged["items"] = _out(caller(request), list(paged["items"]))
    return paged


@router.get("/notifications/summary", response=SummaryOut, summary="How many are unread")
def summary(request: HttpRequest) -> dict[str, int]:
    return {"unread": _inbox(request).filter(read_at__isnull=True).count()}


# Before /notifications/{notification_id}, which would otherwise match "read".
@router.post("/notifications/read", response=MarkAllOut, summary="Mark all read")
def mark_all(request: HttpRequest, body: MarkAllIn) -> dict[str, int]:
    user = caller(request)
    require_scope(user, "read")
    require_scope(user, "write")
    marked = services.mark_all_read(user, up_to=body.up_to)
    return {"marked": marked, "unread": services.unread_count(user)}


@router.patch(
    "/notifications/{notification_id}",
    response=NotificationOut,
    summary="Mark a notification read or unread",
)
def mark(request: HttpRequest, notification_id: UUID, body: ReadIn) -> dict[str, Any]:
    found = _mine(request, notification_id)
    user = caller(request)
    require_scope(user, "write")
    services.mark_read(user, found, body.read)
    return _out(user, [found])[0]


def _settings(request: HttpRequest) -> dict[str, Any]:
    user = caller(request)
    chosen = services.choices(user)
    return {
        "email_available": services.email_enabled(),
        "address": user.email,
        "kinds": [
            {
                "kind": kind.key,
                "label": kind.label,
                "description": kind.description,
                "group": kind.group,
                "type": kind.type or None,
                "required": kind.required,
                "can_turn_off": kind.can_turn_off,
                "per_project": kind.declared,
                "default": services.default_of(kind).value,
                "delivery": chosen[kind.key].value,
            }
            for kind in services.kinds_for(user)
        ],
        "projects": [
            {"project": row.project, "kind": row.kind, "delivery": row.delivery}
            for row in services.project_choices(user)
        ],
    }


@router.get(
    "/me/notification-settings",
    response=SettingsOut,
    summary="How you're told of each kind of notification",
)
def notification_settings(request: HttpRequest) -> dict[str, Any]:
    require_scope(caller(request), "read")
    return _settings(request)


@router.patch(
    "/me/notification-settings",
    response=SettingsOut,
    summary="Choose how you're told of notifications",
)
def change_notification_settings(request: HttpRequest, body: SettingsIn) -> dict[str, Any]:
    """Each kind in Iki and by email, in Iki only, or (for a type's notification its plugin
    doesn't require, ADR 0129) not at all: everywhere, or in one project, which overrides
    your choice everywhere there. Changes are audited."""
    user = caller(request)
    require_scope(user, "write")
    if user.is_service_account:
        raise Problem(409, "Service accounts don’t get notifications.", code="refused")
    try:
        services.set_choices(
            user,
            body.kinds,
            [(c.project, c.kind, c.delivery) for c in body.projects],
        )
    except services.Invalid as error:
        raise Problem(
            422,
            "Some fields are invalid.",
            errors=[{"pointer": pointer, "detail": detail} for pointer, detail in error.errors],
        ) from error
    return _settings(request)


def _record(request: HttpRequest, ref: str, project: UUID | None) -> Any:  # noqa: ANN401 - an Entity
    from iki_server.api.entities import _find

    user = caller(request)
    require_scope(user, "read")
    entity = _find(user, ref, project)
    if entity.registry_id is not None:
        raise Problem(409, "A registry entry isn't watched: its projects' records are.")
    return entity


def _watch_out(watching: watches.Watching) -> dict[str, Any]:
    return {
        "mode": watching.mode,
        "can_watch": watching.can_watch,
        "can_mute": watching.can_mute,
    }


@router.get("/entities/{ref}/watch", response=WatchOut, summary="Whether you watch a record")
def watch(request: HttpRequest, ref: str, project: UUID | None = None) -> dict[str, Any]:
    entity = _record(request, ref, project)
    return _watch_out(watches.of(caller(request), entity))


@router.put("/entities/{ref}/watch", response=WatchOut, summary="Watch a record, mute it, or stop")
def choose_watch(
    request: HttpRequest, ref: str, body: WatchIn, project: UUID | None = None
) -> dict[str, Any]:
    """Watching it, you're told what its type tells its watchers (ADR 0129), beside what
    names you. Muting it, you're told nothing of it but what its plugin requires. Either
    is audited, and needs you to be able to read it."""
    entity = _record(request, ref, project)
    user = caller(request)
    require_scope(user, "write")
    try:
        return _watch_out(watches.choose(user, entity, body.mode))
    except watches.Refused as refused:
        raise Problem(409, str(refused), code="refused") from None
