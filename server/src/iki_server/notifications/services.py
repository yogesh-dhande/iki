"""Sending notifications, reading them, and email preferences."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.notifications import kinds
from iki_server.notifications.kinds import Audience, Kind
from iki_server.notifications.models import EmailPreference, Notification
from iki_server.org import permissions
from iki_server.org.models import OrgRole, OrgRoleAssignment, Project
from iki_server.realtime import events

TITLE_LIMIT = 300
BODY_LIMIT = 4000


def email_enabled() -> bool:
    """Whether this deployment sends email (``IKI_EMAIL_URL`` is set)."""
    return bool(settings.IKI_EMAIL_ENABLED)


def org_admins() -> QuerySet[User]:
    ids = OrgRoleAssignment.objects.filter(role=OrgRole.ORG_ADMIN).values("user")
    return User.objects.filter(pk__in=ids, is_active=True)


def notify(
    recipients: Iterable[User],
    kind: Kind | str,
    *,
    title: str,
    body: str = "",
    path: str = "",
    project: Project | None = None,
    actor: User | None = None,
) -> list[Notification]:
    """Tells each recipient, in Iki and by email if they want it.

    Call it in the transaction of the change: notifications, their emails and
    the live updates exist exactly when the change does. Nobody is told about
    their own action, and service accounts and deactivated people aren't told.
    """
    kind = kinds.get(kind) if isinstance(kind, str) else kinds.get(kind.key)
    if path and (not path.startswith("/") or path.startswith("//")):
        raise ValueError("Notification paths are paths on this site, such as /projects/x/.")
    people: dict[UUID, User] = {}
    for user in recipients:
        if (
            user.is_active
            and not user.is_service_account
            and (actor is None or user.pk != actor.pk)
        ):
            people[user.pk] = user
    if not people:
        return []
    from iki_server.notifications.tasks import send_email

    with transaction.atomic():
        created = Notification.objects.bulk_create(
            Notification(
                recipient=user,
                kind=kind.key,
                title=title[:TITLE_LIMIT],
                body=body[:BODY_LIMIT],
                path=path,
                project=project,
                actor=actor,
            )
            for user in people.values()
        )
        emailed = wants_email(people.values(), kind) if email_enabled() else set()
        for notification in created:
            if notification.recipient_id in emailed:
                send_email.enqueue(str(notification.pk))
            events.publish(notification.recipient_id, "notification", {"id": str(notification.pk)})
    return created


# --- Reading ------------------------------------------------------------------


def inbox(user: User) -> QuerySet[Notification]:
    """The notifications the user may see now.

    Notifications about a project disappear when the person can no longer
    see it, except the one telling them they were removed. A token limited to
    some projects sees only notifications about those projects.
    """
    mine = Notification.objects.filter(recipient=user)
    limits = permissions.token_limits(user)
    if limits is not None:
        if "read" not in limits.scopes:
            return mine.none()
        if limits.projects is not None:
            return mine.filter(project__in=limits.projects).filter(
                Q(project__in=permissions.visible_projects(user).values("pk"))
            )
    return mine.filter(
        Q(project__isnull=True)
        | Q(kind=kinds.PROJECT_REMOVED.key)
        | Q(project__in=permissions.visible_projects(user).values("pk"))
    )


def unread_count(user: User) -> int:
    return inbox(user).filter(read_at__isnull=True).count()


def mark_read(user: User, notification: Notification, read: bool) -> None:
    if (notification.read_at is not None) == read:
        return
    notification.read_at = timezone.now() if read else None
    with transaction.atomic():
        notification.save(update_fields=["read_at"])
        events.publish(user.pk, "notifications_read", {"unread": unread_count(user)})


def mark_all_read(user: User, *, up_to: datetime | None = None) -> int:
    """Marks everything unread as read, or only what arrived by ``up_to`` (what
    the person had seen), so a notification arriving meanwhile stays unread."""
    unread = inbox(user).filter(read_at__isnull=True)
    if up_to is not None:
        unread = unread.filter(created_at__lte=up_to)
    with transaction.atomic():
        count = Notification.objects.filter(pk__in=unread.values("pk")).update(
            read_at=timezone.now()
        )
        if count:
            events.publish(user.pk, "notifications_read", {"unread": unread_count(user)})
    return count


# --- Email preferences --------------------------------------------------------


def kinds_for(user: User) -> list[Kind]:
    """The kinds this person can receive, in preferences-page order."""
    admin = OrgRole.ORG_ADMIN in permissions.org_roles(user)
    return [k for k in kinds.KINDS.values() if k.audience is Audience.EVERYONE or admin]


def email_choices(user: User) -> dict[str, bool]:
    """Kind → whether it's emailed to this person."""
    chosen = dict(EmailPreference.objects.filter(user=user).values_list("kind", "email"))
    return {k.key: chosen.get(k.key, k.email) for k in kinds_for(user)}


def wants_email(users: Iterable[User], kind: Kind) -> set[UUID]:
    candidates = {u.pk for u in users if u.email}
    choices = dict(
        EmailPreference.objects.filter(user__in=candidates, kind=kind.key).values_list(
            "user", "email"
        )
    )
    return {pk for pk in candidates if choices.get(pk, kind.email)}


def set_email_choices(user: User, choices: dict[str, bool]) -> dict[str, bool]:
    """Saves the person's choices for the kinds named; others keep theirs."""
    allowed = {k.key: k for k in kinds_for(user)}
    unknown = sorted(set(choices) - set(allowed))
    if unknown:
        raise ValueError(f"No such notification kinds: {', '.join(unknown)}.")
    before = email_choices(user)
    changed: dict[str, Any] = {
        key: {"from": before[key], "to": value}
        for key, value in choices.items()
        if before[key] != value
    }
    with transaction.atomic():
        for key, value in choices.items():
            EmailPreference.objects.update_or_create(user=user, kind=key, defaults={"email": value})
        if changed:
            audit.record(
                "notifications.email_preferences_changed", actor=user, target=user, data=changed
            )
    return email_choices(user)
