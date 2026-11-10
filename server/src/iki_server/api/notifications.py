"""Your notifications, and which you get by email."""

from datetime import datetime
from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpRequest
from ninja import Field, Query, Router, Schema

from iki_server.api.common import caller, require_scope
from iki_server.api.pagination import PageParams, page
from iki_server.api.problems import Problem
from iki_server.api.schemas import Page
from iki_server.notifications import services
from iki_server.notifications.models import Notification

router = Router(tags=["notifications"])


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


class NotificationOut(Schema):
    id: UUID
    kind: str = Field(description="What it's about, such as project.added.")
    title: str
    body: str
    path: str = Field(description="Where it leads in Iki (a path on this site), or empty.")
    project: NotificationProject | None
    actor: NotificationActor | None = Field(description="Who caused it, if anyone did.")
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


class EmailKindOut(Schema):
    kind: str
    label: str
    description: str
    group: str
    email: bool


class EmailSettingsOut(Schema):
    email_available: bool = Field(
        description="Whether this organization sends email. If not, choices are kept for later."
    )
    address: str = Field(description="Where emails go: your address from the identity provider.")
    kinds: list[EmailKindOut]


class EmailSettingsIn(Schema):
    email: dict[str, bool] = Field(
        description="Kind → whether to email it. Kinds left out keep their setting."
    )


def _inbox(request: HttpRequest) -> QuerySet[Notification]:
    user = caller(request)
    require_scope(user, "read")
    return services.inbox(user).select_related("project", "actor")


def _mine(request: HttpRequest, notification_id: UUID) -> Notification:
    found = _inbox(request).filter(pk=notification_id).first()
    if found is None:
        raise Problem(404, "No such notification.")
    return found


@router.get("/notifications", response=Page[NotificationOut], summary="Your notifications")
def notifications(request: HttpRequest, filters: Query[InboxFilters]) -> dict[str, Any]:
    """Newest first. Live updates (``GET /api/v1/events``) say when new ones arrive."""
    found = _inbox(request)
    if filters.unread is not None:
        found = found.filter(read_at__isnull=filters.unread)
    return page(found, filters, newest_first=True)


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
def mark(request: HttpRequest, notification_id: UUID, body: ReadIn) -> Notification:
    found = _mine(request, notification_id)
    user = caller(request)
    require_scope(user, "write")
    services.mark_read(user, found, body.read)
    return found


def _email_settings(request: HttpRequest) -> dict[str, Any]:
    user = caller(request)
    choices = services.email_choices(user)
    return {
        "email_available": services.email_enabled(),
        "address": user.email,
        "kinds": [
            {
                "kind": kind.key,
                "label": kind.label,
                "description": kind.description,
                "group": kind.group,
                "email": choices[kind.key],
            }
            for kind in services.kinds_for(user)
        ],
    }


@router.get(
    "/me/notification-email",
    response=EmailSettingsOut,
    summary="Which notifications you get by email",
)
def email_settings(request: HttpRequest) -> dict[str, Any]:
    require_scope(caller(request), "read")
    return _email_settings(request)


@router.patch(
    "/me/notification-email",
    response=EmailSettingsOut,
    summary="Choose which notifications you get by email",
)
def change_email_settings(request: HttpRequest, body: EmailSettingsIn) -> dict[str, Any]:
    user = caller(request)
    require_scope(user, "write")
    if user.is_service_account:
        raise Problem(409, "Service accounts don’t get notifications.", code="refused")
    try:
        services.set_email_choices(user, body.email)
    except ValueError as error:
        raise Problem(
            422, "Some fields are invalid.", errors=[{"pointer": "#/email", "detail": str(error)}]
        ) from error
    return _email_settings(request)
