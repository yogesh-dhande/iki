"""Watching a record, or muting it (ADR 0129), as GitHub's subscriptions to an issue and
Jira's watchers do.

Someone who can read a record can **watch** it: they're told what its type tells its
watchers (``notify: { watchers: true }``), beside what names them. Or they can **mute** it:
they're told nothing of it but what its plugin requires. Without either, they're told
what names them: as a field's person, its creator, or a lab role. Each choice is audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction

from iki_server.audit import log as audit
from iki_server.notifications import declared
from iki_server.notifications.models import Watch, WatchMode

if TYPE_CHECKING:
    from iki_server.accounts.models import User
    from iki_server.entities.models import Entity

DEFAULT = "default"


@dataclass(frozen=True)
class Watching:
    """A person's choice about a record, and what its type lets them choose."""

    mode: str
    #: Whether its type tells its watchers anything.
    can_watch: bool
    #: Whether its type sends anything that isn't required, to mute.
    can_mute: bool


def of(user: User, entity: Entity) -> Watching:
    found = Watch.objects.filter(user=user, entity=entity).values_list("mode", flat=True).first()
    sent = declared.of_type(entity.type)
    return Watching(
        mode=found or DEFAULT,
        can_watch=any(d.n.notify_watchers for d in sent),
        can_mute=any(not d.n.required for d in sent),
    )


class Refused(ValueError):  # noqa: N818 - reads as "the choice is refused"
    """Why the choice can't be made."""


def choose(user: User, entity: Entity, mode: str) -> Watching:
    """Watches the record, mutes it, or goes back to being told what names them
    (``default``). The caller has checked that ``user`` can read it."""
    current = of(user, entity)
    if user.is_service_account:
        raise Refused("Service accounts aren't told of anything.")
    if mode == WatchMode.WATCHING and not current.can_watch:
        raise Refused("Its type tells its watchers nothing, so there's nothing to watch.")
    if mode == WatchMode.MUTED and not current.can_mute:
        raise Refused("Its type sends nothing that can be muted.")
    if mode == current.mode:
        return current
    with transaction.atomic():
        if mode == DEFAULT:
            Watch.objects.filter(user=user, entity=entity).delete()
        else:
            Watch.objects.update_or_create(user=user, entity=entity, defaults={"mode": mode})
        audit.record(
            "notifications.watch_changed",
            actor=user,
            target=entity,
            project=entity.owner_project,
            data={"from": current.mode, "to": mode},
        )
    return of(user, entity)
