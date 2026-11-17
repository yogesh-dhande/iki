"""A project's dashboard (ADR 0090): the app its page opens with, chosen by its Admins.

It's one of the project's own apps, not one about an entity (those are tabs on their
entities' pages), and readers see it only while they can read it, it's still in the
project, and it isn't archived. Choosing and clearing it are audited.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.db.models.functions import Now

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import access, registry
from iki_server.entities.models import Entity, ProjectDashboard
from iki_server.org.models import Project
from iki_server.org.services import Refused

APP = "iki/core.App"


def _apps(user: User, project: Project) -> QuerySet[Entity]:
    """The project's apps the user can read that could be its dashboard."""
    model = cast("type[Entity]", registry.model(APP))
    return (
        access.visible_of(user, model)
        .filter(links__project=project, archived_at__isnull=True)
        # The type's own field, in a Q, as search filters a type's table.
        .exclude(Q(spec__has_key="entity"))
        .distinct()
    )


def choices(user: User, project: Project) -> QuerySet[Entity]:
    """The apps the user could make the project's dashboard, by name."""
    return _apps(user, project).order_by(F("name"), "hrid")


def of(user: User, project: Project) -> tuple[ProjectDashboard | None, Entity | None]:
    """The project's dashboard, and its app if the user can still read it there."""
    chosen = ProjectDashboard.objects.select_related("set_by").filter(project=project).first()
    if chosen is None:
        return None, None
    return chosen, _apps(user, project).filter(pk=chosen.app_id).first()


def _app(user: User, project: Project, ref: str) -> Entity:
    """The app by ID or HRID, if it can be the project's dashboard; else why not."""
    readable = access.visible(user)
    try:
        found = readable.filter(pk=UUID(ref)).first()
    except ValueError:
        found = readable.filter(hrid=ref.strip().upper()).first()
    if found is None or found.type != APP:
        raise Refused(f"There’s no app {ref} that you can read.", field="app")
    if not found.links.filter(project=project).exists():
        raise Refused(f"{found.hrid} isn’t in {project.name}. Add it first.", field="app")
    if found.archived_at is not None:
        raise Refused(f"{found.hrid} is archived. Restore it first.", field="app")
    if not _apps(user, project).filter(pk=found.pk).exists():
        raise Refused(
            f"{found.hrid} is about one entity at a time, so it’s a tab on their pages, "
            "not a dashboard.",
            field="app",
        )
    return found


def choose(project: Project, ref: str, *, by: User) -> Entity:
    """Makes the app the project's dashboard, replacing any other."""
    if project.is_archived:
        raise Refused("Restore the project before changing it.")
    app = _app(by, project, ref)
    with transaction.atomic():
        # One change at a time per project, so the audit says what each replaced.
        Project.objects.select_for_update().filter(pk=project.pk).first()
        chosen = ProjectDashboard.objects.filter(project=project).first()
        if chosen is not None and chosen.app_id == app.pk:
            return app
        before = Entity.objects.filter(pk=chosen.app_id).first() if chosen else None
        ProjectDashboard.objects.update_or_create(
            project=project, defaults={"app": app, "set_by": by, "set_at": Now()}
        )
        audit.record(
            "project.dashboard_set",
            actor=by,
            target=project,
            project=project,
            data={"app": [before.hrid if before else None, app.hrid]},
        )
    return app


def clear(project: Project, *, by: User) -> None:
    """The project's page opens without an app."""
    if project.is_archived:
        raise Refused("Restore the project before changing it.")
    with transaction.atomic():
        Project.objects.select_for_update().filter(pk=project.pk).first()
        chosen = ProjectDashboard.objects.filter(project=project).first()
        if chosen is None:
            return
        before = Entity.objects.filter(pk=chosen.app_id).first()
        chosen.delete()
        audit.record(
            "project.dashboard_cleared",
            actor=by,
            target=project,
            project=project,
            data={"app": [before.hrid if before else None, None]},
        )
