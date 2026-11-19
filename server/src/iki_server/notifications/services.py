"""Sending notifications, reading them, and how people choose to be told.

A notification about records (ADR 0129) says which, and is told with others: while a
person hasn't read or been emailed one of its kind in its project, a new one about another
record joins it ("3 Samples: Ready for QA review") rather than arriving apart, and its
email waits a little (``IKI_NOTIFICATION_BATCH_SECONDS``) for more. So a change to 500
records, or 500 changes in a minute, tells each person once or twice, not 500 times.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.notifications import kinds
from iki_server.notifications.kinds import Audience, Kind
from iki_server.notifications.models import (
    Delivery,
    Notification,
    NotificationRecord,
    NotificationSetting,
)
from iki_server.org import permissions
from iki_server.org.models import OrgRole, OrgRoleAssignment, Project
from iki_server.realtime import events

if TYPE_CHECKING:
    from iki_server.audit.models import Agent
    from iki_server.entities.models import Entity

TITLE_LIMIT = 300
BODY_LIMIT = 4000
#: How long a notification about records takes in others of its kind, unread.
FOLD_WINDOW = dt.timedelta(minutes=10)


def email_enabled() -> bool:
    """Whether this deployment sends email (``IKI_EMAIL_URL`` is set)."""
    return bool(settings.IKI_EMAIL_ENABLED)


def batch_delay() -> dt.timedelta:
    """How long an email about records waits for others to join it."""
    return dt.timedelta(seconds=settings.IKI_NOTIFICATION_BATCH_SECONDS)


def org_admins() -> QuerySet[User]:
    ids = OrgRoleAssignment.objects.filter(role=OrgRole.ORG_ADMIN).values("user")
    return User.objects.filter(pk__in=ids, is_active=True)


@dataclass(frozen=True)
class Fold:
    """How a notification about records reads once it's about ``count`` of them: its
    title, body and path."""

    many: Callable[[int], tuple[str, str, str]]


def notify(
    recipients: Iterable[User],
    kind: Kind | str,
    *,
    title: str,
    body: str = "",
    path: str = "",
    project: Project | None = None,
    actor: User | None = None,
    agent: Agent | None = None,
    about: Sequence[Entity] = (),
    fold: Fold | None = None,
) -> list[Notification]:
    """Tells each recipient, in Iki and by email if they want it.

    Call it in the transaction of the change: notifications, their emails and
    the live updates exist exactly when the change does. Nobody is told about
    their own action, and service accounts and deactivated people aren't told.
    ``about``: the records it's about, which each must still be able to read to see it;
    with ``fold``, it joins the person's open notification of its kind.
    """
    kind = kinds.get(kind if isinstance(kind, str) else kind.key)
    people = [
        user
        for user in {u.pk: u for u in recipients}.values()
        if user.is_active and not user.is_service_account and (actor is None or user.pk != actor.pk)
    ]
    if not people:
        return []
    chosen = deliveries(people, kind, project)
    return [
        deliver(
            user,
            kind,
            chosen[user.pk],
            title=title,
            body=body,
            path=path,
            project=project,
            actor=actor,
            agent=agent,
            about=about,
            fold=fold,
        )
        for user in people
        if chosen[user.pk] != Delivery.OFF
    ]


#: How :func:`told` left a person's notifications: a new one, their open one now about
#: more, or their open one as it was, which told them of it all already.
NEW, UPDATED, UNCHANGED = "new", "updated", "unchanged"


@dataclass(frozen=True)
class Told:
    notification: Notification
    #: NEW, UPDATED or UNCHANGED.
    how: str


def deliver(
    user: User,
    kind: Kind,
    delivery: Delivery,
    *,
    title: str,
    body: str = "",
    path: str = "",
    project: Project | None = None,
    actor: User | None = None,
    agent: Agent | None = None,
    about: Sequence[Entity] = (),
    fold: Fold | None = None,
) -> Notification:
    """Tells one person, who's to be told (``delivery`` isn't off): a new notification, or,
    with ``fold``, the one they have open of its kind in the project, about more records."""
    return told(
        user,
        kind,
        delivery,
        title=title,
        body=body,
        path=path,
        project=project,
        actor=actor,
        agent=agent,
        about=about,
        fold=fold,
    ).notification


def told(
    user: User,
    kind: Kind,
    delivery: Delivery,
    *,
    title: str,
    body: str = "",
    path: str = "",
    project: Project | None = None,
    actor: User | None = None,
    agent: Agent | None = None,
    about: Sequence[Entity] = (),
    naming: Sequence[Entity] = (),
    fold: Fold | None = None,
) -> Told:
    """:func:`deliver`, saying how. ``naming``: records it names beside those it's about,
    such as the result whose release it tells of: listed with it, and not counted. One
    their open notification doesn't name yet is news, so it updates that one, as a new
    comment updates a thread on GitHub, rather than being lost in it."""
    if path and (not path.startswith("/") or path.startswith("//")):
        raise ValueError("Notification paths are paths on this site, such as /projects/x/.")
    from iki_server.notifications.tasks import send_email

    with transaction.atomic():
        if fold is not None and about:
            joined = _join(user, kind, project, about, naming, fold, actor, agent)
            if joined is not None:
                return joined
        notification = Notification.objects.create(
            recipient=user,
            kind=kind.key,
            title=title[:TITLE_LIMIT],
            body=body[:BODY_LIMIT],
            path=path,
            project=project,
            entity=about[0] if len(about) == 1 else None,
            count=max(len(about), 1),
            actor=actor,
            agent=agent,
        )
        if about or naming:
            NotificationRecord.objects.bulk_create(
                NotificationRecord(notification=notification, entity=e)
                for e in {e.pk: e for e in [*about, *naming]}.values()
            )
        if delivery == Delivery.EMAIL and email_enabled():
            if fold is not None and batch_delay():
                send_email.using(run_after=timezone.now() + batch_delay()).enqueue(
                    str(notification.pk)
                )
            else:
                send_email.enqueue(str(notification.pk))
        events.publish(user.pk, "notification", {"id": str(notification.pk)})
    return Told(notification, NEW)


def _join(
    user: User,
    kind: Kind,
    project: Project | None,
    about: Sequence[Entity],
    naming: Sequence[Entity],
    fold: Fold,
    actor: User | None,
    agent: Agent | None,
) -> Told | None:
    """The person's open notification of the kind in the project, now about ``about`` too,
    and naming ``naming``: unread, not emailed, and recent. None if they have none."""
    open_one = (
        Notification.objects.select_for_update(of=("self",))
        .filter(
            recipient=user,
            kind=kind.key,
            project=project,
            read_at__isnull=True,
            emailed_at__isnull=True,
            created_at__gte=timezone.now() - FOLD_WINDOW,
        )
        .order_by("-created_at")
        .first()
    )
    if open_one is None:
        return None
    have = set(
        NotificationRecord.objects.filter(notification=open_one).values_list("entity", flat=True)
    )
    new = [e for e in {e.pk: e for e in about}.values() if e.pk not in have]
    named = [
        e
        for e in {e.pk: e for e in naming}.values()
        if e.pk not in have and e.pk not in {n.pk for n in new}
    ]
    if not new and not named:
        return Told(open_one, UNCHANGED)  # told of these already
    NotificationRecord.objects.bulk_create(
        NotificationRecord(notification=open_one, entity=e) for e in [*new, *named]
    )
    if new:
        open_one.count = (open_one.count if have else 0) + len(new)
        title, body, path = fold.many(open_one.count)
        open_one.title, open_one.body, open_one.path = title[:TITLE_LIMIT], body[:BODY_LIMIT], path
        open_one.entity = None
    if open_one.actor_id != (actor.pk if actor else None) or open_one.agent_id != (
        agent.pk if agent else None
    ):
        open_one.actor, open_one.agent = None, None
    open_one.save(update_fields=["count", "title", "body", "path", "entity", "actor", "agent"])
    events.publish(user.pk, "notification", {"id": str(open_one.pk)})
    return Told(open_one, UPDATED)


# --- Reading ------------------------------------------------------------------


def inbox(user: User) -> QuerySet[Notification]:
    """The notifications the user may see now.

    Notifications about a project disappear when the person can no longer
    see it, except the one telling them they were removed; one about a record, when they
    can no longer read it. A token limited to some projects sees only notifications about
    those projects.
    """
    from iki_server.entities import access

    mine = Notification.objects.filter(recipient=user)
    limits = permissions.token_limits(user)
    readable = Q(entity__isnull=True) | Q(entity__in=access.visible(user).values("pk"))
    if limits is not None:
        if "read" not in limits.scopes:
            return mine.none()
        if limits.projects is not None:
            return (
                mine.filter(project__in=limits.projects)
                .filter(Q(project__in=permissions.visible_projects(user).values("pk")))
                .filter(readable)
            )
    return mine.filter(
        Q(project__isnull=True)
        | Q(kind=kinds.PROJECT_REMOVED.key)
        | Q(project__in=permissions.visible_projects(user).values("pk"))
    ).filter(readable)


def records_of(
    user: User, notifications: Sequence[Notification], limit: int = 20
) -> dict[UUID, list[Entity]]:
    """The records each notification is about that ``user`` can read now, up to ``limit``
    each, in the order they were told of."""
    from iki_server.entities import access
    from iki_server.entities.models import Entity

    ids = [n.pk for n in notifications if n.count > 1 or n.entity_id is not None]
    if not ids:
        return {}
    readable = access.visible(user).filter(pk=OuterRef("entity"))
    rows = (
        NotificationRecord.objects.filter(notification__in=ids)
        .filter(Exists(readable))
        .order_by("notification", "id")
        .values_list("notification", "entity")
    )
    wanted: dict[UUID, list[UUID]] = {}
    for notification, entity in rows:
        found = wanted.setdefault(notification, [])
        if len(found) < limit:
            found.append(entity)
    entities = Entity.objects.in_bulk([e for es in wanted.values() for e in es])
    return {n: [entities[e] for e in es if e in entities] for n, es in wanted.items()}


def unread_count(user: User) -> int:
    return inbox(user).filter(read_at__isnull=True).count()


def mark_read(user: User, notification: Notification, read: bool) -> None:
    if (notification.read_at is not None) == read:
        return
    notification.read_at = timezone.now() if read else None
    with transaction.atomic():
        notification.save(update_fields=["read_at"])
        events.publish(user.pk, "notifications_read", {"unread": unread_count(user)})


def mark_all_read(user: User, *, up_to: dt.datetime | None = None) -> int:
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


# --- How people choose to be told -----------------------------------------------


def kinds_for(user: User) -> list[Kind]:
    """The kinds this person can receive, in settings-page order."""
    admin = OrgRole.ORG_ADMIN in permissions.org_roles(user)
    return [k for k in kinds.every() if k.audience is Audience.EVERYONE or admin]


def default_of(kind: Kind) -> Delivery:
    return Delivery.EMAIL if kind.email else Delivery.IN_APP


def _allowed(kind: Kind, delivery: Delivery) -> Delivery:
    """A choice as it applies: off only where it can be (a plugin may have made a
    notification required since someone turned it off)."""
    return Delivery.IN_APP if delivery == Delivery.OFF and not kind.can_turn_off else delivery


def deliveries(users: Iterable[User], kind: Kind, project: Project | None) -> dict[UUID, Delivery]:
    """How each person is to be told of ``kind`` in ``project``: their choice there, else
    their choice everywhere, else the kind's default. Email only to people with an
    address."""
    users = list(users)
    ids = [u.pk for u in users]
    rows = NotificationSetting.objects.filter(user__in=ids, kind=kind.key).filter(
        Q(project__isnull=True) | Q(project=project)
        if project is not None
        else Q(project__isnull=True)
    )
    everywhere: dict[UUID, str] = {}
    here: dict[UUID, str] = {}
    found = cast(
        "list[tuple[UUID, UUID | None, str]]",
        list(rows.values_list("user", "project", "delivery")),
    )
    for user_id, project_id, delivery in found:
        if project_id is None:
            everywhere[user_id] = delivery
        else:
            here[user_id] = delivery
    out: dict[UUID, Delivery] = {}
    for user in users:
        chosen = here.get(user.pk) or everywhere.get(user.pk)
        delivery = _allowed(kind, Delivery(chosen)) if chosen else default_of(kind)
        if delivery == Delivery.EMAIL and not user.email:
            delivery = Delivery.IN_APP
        out[user.pk] = delivery
    return out


def choices(user: User) -> dict[str, Delivery]:
    """Kind → how this person is told of it, everywhere."""
    chosen = dict(
        NotificationSetting.objects.filter(user=user, project__isnull=True).values_list(
            "kind", "delivery"
        )
    )
    return {
        k.key: _allowed(k, Delivery(chosen[k.key])) if k.key in chosen else default_of(k)
        for k in kinds_for(user)
    }


def project_choices(user: User) -> list[NotificationSetting]:
    """The person's choices for single projects they can still see, by project and kind."""
    known = {k.key for k in kinds_for(user)}
    return [
        row
        for row in NotificationSetting.objects.filter(user=user, project__isnull=False)
        .filter(project__in=permissions.visible_projects(user).values("pk"))
        .select_related("project")
        .order_by("project__name", "kind")
        if row.kind in known
    ]


class Invalid(ValueError):  # noqa: N818 - reads as "the choices are invalid"
    """What's wrong with someone's choices: (pointer, detail) pairs."""

    def __init__(self, errors: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(d for _, d in errors))
        self.errors = errors


def set_choices(
    user: User,
    everywhere: Mapping[str, str | None] | None = None,
    projects: Sequence[tuple[UUID, str, str | None]] = (),
) -> None:
    """Saves the person's choices: for the kinds named everywhere, and for single projects
    (project, kind, delivery; None to follow their choice everywhere there). Others keep
    theirs. Each change is audited."""
    allowed = {k.key: k for k in kinds_for(user)}
    errors: list[tuple[str, str]] = []
    wanted: list[tuple[Project | None, Kind, Delivery | None]] = []

    def check(pointer: str, key: str, value: str | None) -> tuple[Kind, Delivery | None] | None:
        kind = allowed.get(key)
        if kind is None:
            errors.append((pointer, f"There's no notification kind {key}."))
            return None
        if value is None:
            return kind, None
        try:
            delivery = Delivery(value)
        except ValueError:
            errors.append((pointer, "must be email, in_app or off."))
            return None
        if delivery == Delivery.OFF and not kind.can_turn_off:
            errors.append(
                (
                    pointer,
                    f"“{kind.label}” can't be turned off"
                    + (": its plugin requires it." if kind.required else ", only emailed or not."),
                )
            )
            return None
        return kind, delivery

    for key, value in (everywhere or {}).items():
        if value is None:
            errors.append((f"#/kinds/{key}", "must be email, in_app or off."))
            continue
        found = check(f"#/kinds/{key}", key, value)
        if found is not None:
            wanted.append((None, *found))
    visible = {p.pk: p for p in permissions.visible_projects(user)}
    for index, (project_id, key, chosen) in enumerate(projects):
        project = visible.get(project_id)
        if project is None:
            errors.append((f"#/projects/{index}/project", "There's no such project you can see."))
            continue
        found = check(f"#/projects/{index}/kind", key, chosen)
        if found is not None:
            if not found[0].declared:
                errors.append(
                    (
                        f"#/projects/{index}/kind",
                        f"“{found[0].label}” is chosen for every project, not one.",
                    )
                )
                continue
            wanted.append((project, *found))
    if errors:
        raise Invalid(errors)
    changed: list[dict[str, Any]] = []
    with transaction.atomic():
        for project, kind, delivery in wanted:
            row = NotificationSetting.objects.filter(
                user=user, kind=kind.key, project=project
            ).first()
            before = row.delivery if row is not None else None
            after = delivery.value if delivery is not None else None
            if project is None and after == default_of(kind).value and before is None:
                continue
            if before == after:
                continue
            if delivery is None:
                NotificationSetting.objects.filter(
                    user=user, kind=kind.key, project=project
                ).delete()
            elif row is None:
                NotificationSetting.objects.create(
                    user=user, kind=kind.key, project=project, delivery=delivery
                )
            else:
                row.delivery = delivery
                row.save(update_fields=["delivery"])
            changed.append(
                {
                    "kind": kind.key,
                    **({"project": str(project.pk)} if project is not None else {}),
                    "from": before or (default_of(kind).value if project is None else None),
                    "to": after,
                }
            )
        if changed:
            audit.record(
                "notifications.preferences_changed",
                actor=user,
                target=user,
                data={"changes": changed},
            )
