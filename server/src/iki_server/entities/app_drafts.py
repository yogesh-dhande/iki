"""Apps' drafts (ADR 0090): a spec to look at before saving it.

An agent writing an app for its person drafts it, and the person opens the page it
answers with to see it as it would be, with what they may read. A draft is checked as
saving it would be, belongs to that one person, and lasts a day: it's a preview, not a
record, so it has no history and isn't audited. Saving the app is what's recorded.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, registry
from iki_server.entities.models import AppDraft, Entity
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused

#: How long a draft can be opened.
LASTS = timedelta(days=1)
#: The most drafts one person has at a time: making another drops their oldest.
MAX_PER_PERSON = 20
#: The largest spec a draft keeps, as JSON.
MAX_BYTES = 256 * 1024


def _about(user: User, project: Project, spec: dict[str, Any], ref: str | None) -> Entity | None:
    """The entity an app about a type is previewed on: one of that type in the project."""
    about = spec.get("entity")
    if about is None:
        if ref:
            raise Refused("This app isn’t about an entity, so it takes none.", field="entity")
        return None
    wanted = about.get("type", "")
    if not ref:
        raise Refused(f"This app is about one {wanted}: say which to show it on.", field="entity")
    readable = access.visible(user).filter(links__project=project)
    try:
        found = readable.filter(pk=UUID(ref)).first()
    except ValueError:
        found = readable.filter(hrid=ref.strip().upper()).first()
    if found is None:
        raise Refused(
            f"There’s no entity {ref} in {project.name} that you can read.", field="entity"
        )
    if not registry.is_a(found.type, wanted):
        raise Refused(f"{found.hrid} isn’t a {wanted}.", field="entity")
    return found


def create(
    user: User, project: Project, name: str, spec: dict[str, Any], about: str | None = None
) -> AppDraft:
    """A draft of an app for the user, whose spec ``app_specs.check`` found nothing
    wrong with. Their expired drafts go, and their oldest if they have too many."""
    if project.is_archived:
        raise Refused("Restore the project before changing it.")
    if len(json.dumps(spec)) > MAX_BYTES:
        raise Refused(f"The spec is over {MAX_BYTES // 1024} KB as JSON.", field="spec")
    entity = _about(user, project, spec, about)
    now = timezone.now()
    with transaction.atomic():
        mine = AppDraft.objects.filter(created_by=user)
        mine.filter(expires_at__lte=now).delete()
        keep = mine.order_by("-created_at").values("pk")[: MAX_PER_PERSON - 1]
        mine.exclude(pk__in=keep).delete()
        return AppDraft.objects.create(
            project=project,
            created_by=user,
            agent=audit.current_agent(),
            name=name,
            spec=spec,
            about=entity,
            expires_at=now + LASTS,
        )


def find(user: User, draft_id: UUID) -> AppDraft | None:
    """The user's draft, while it lasts and they can still read its project (and the
    entity it's about). No one else's."""
    found = (
        AppDraft.objects.select_related("project", "agent", "about")
        .filter(pk=draft_id, created_by=user, expires_at__gt=timezone.now())
        .first()
    )
    if found is None:
        return None
    readable = access.visible(user).filter(links__project=found.project)
    if found.about is not None and not readable.filter(pk=found.about.pk).exists():
        return None
    if not permissions.can(user, Action.PROJECT_READ, found.project):
        return None
    return found


def prune() -> int:
    """Deletes expired drafts, for ``iki-server maintain``."""
    deleted, _ = AppDraft.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted
