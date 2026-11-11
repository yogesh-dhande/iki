"""Saving searches, sharing them, and who may change them.

- **Anyone who can read a project** can save searches of it, for themselves.
- **Sharing** one with everyone who can read the project takes Editor there,
  so viewers can't fill a project's list.
- **Changing** a search is its owner's. A project's Admins can remove a shared
  one, to keep the project's list tidy, but not change it.

A search is checked when it's saved, as the person saving it, so a saved
search always worked once. Types change, so it's checked again each time it
runs. Shared searches are audited: they're what a project's people see.
"""

from collections.abc import Mapping
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.entities import search
from iki_server.org import permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.searches.models import SavedSearch

#: Per person per project, and shared ones per project.
MAX_OWN = 100
MAX_SHARED = 100

#: What a saved search keeps: a search without its project (the saved search's) or paging.
KEYS = ("type", "exact_type", "archived", "q", "where", "sort", "descending")


class Refused(Exception):  # noqa: N818 - reads as "the change was refused"
    """The change can't be made: ``status`` (403, 409 or 422) and why."""

    def __init__(
        self, status: int, detail: str, errors: list[tuple[str, str]] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors or []


def visible(user: User, project: Project) -> QuerySet[SavedSearch]:
    """The project's searches the user sees: their own, and shared ones."""
    return SavedSearch.objects.filter(project=project).filter(Q(created_by=user) | Q(shared=True))


def can_change(user: User, saved: SavedSearch) -> bool:
    return bool(saved.created_by_id == user.pk)


def can_delete(user: User, saved: SavedSearch) -> bool:
    return can_change(user, saved) or (
        saved.shared and permissions.can(user, Action.PROJECT_MANAGE, saved.project)
    )


def clean(user: User, project: Project, query: Mapping[str, Any]) -> dict[str, Any]:
    """The query as it's kept, once it's been checked as ``user``. Raises :class:`Refused`."""
    kept = {
        k: query[k]
        for k in KEYS
        if query.get(k) not in (None, "")
        # Off is the default for these; archived=false means "current only".
        and not (k in ("exact_type", "descending") and query[k] is False)
    }
    try:
        search.queryset(user, search.Search(project=project.pk, **kept))
    except search.SearchError as error:
        raise Refused(
            422,
            "This search is invalid.",
            [(p.replace("#/", "#/query/", 1), d) for p, d in error.errors],
        ) from error
    return kept


def _may_share(user: User, project: Project) -> None:
    if not permissions.can(user, Action.PROJECT_EDIT, project):
        raise Refused(403, "Sharing a search with the project takes the Editor role there.")


def _check_room(user: User, project: Project, *, shared: bool) -> None:
    if SavedSearch.objects.filter(project=project, created_by=user).count() >= MAX_OWN:
        raise Refused(409, f"You have {MAX_OWN} saved searches here. Delete one first.")
    if shared and SavedSearch.objects.filter(project=project, shared=True).count() >= MAX_SHARED:
        raise Refused(409, f"This project has {MAX_SHARED} shared searches. Remove one first.")


def _named(name: str) -> str:
    name = name.strip()
    if not name:
        raise Refused(422, "Give the search a name.", [("#/name", "can't be blank.")])
    return name


def _taken(error: IntegrityError) -> Refused:
    if "iki_saved_search_name_once" in str(error):
        return Refused(409, "You already have a saved search with this name here.")
    raise error


def create(
    user: User, project: Project, *, name: str, query: Mapping[str, Any], shared: bool = False
) -> SavedSearch:
    name = _named(name)
    kept = clean(user, project, query)
    if shared:
        _may_share(user, project)
    with transaction.atomic():
        # One at a time per project, so the limits hold.
        Project.objects.select_for_update().get(pk=project.pk)
        _check_room(user, project, shared=shared)
        try:
            with transaction.atomic():
                saved = SavedSearch.objects.create(
                    project=project, name=name, query=kept, shared=shared, created_by=user
                )
        except IntegrityError as error:
            raise _taken(error) from error
        if shared:
            audit.record(
                "saved_search.shared",
                actor=user,
                target=saved,
                project=project,
                data={"name": name},
            )
    return saved


def update(
    user: User,
    saved: SavedSearch,
    *,
    name: str | None = None,
    query: Mapping[str, Any] | None = None,
    shared: bool | None = None,
) -> SavedSearch:
    if not can_change(user, saved):
        raise Refused(403, "Only the person who saved a search can change it.")
    project = saved.project
    with transaction.atomic():
        Project.objects.select_for_update().get(pk=project.pk)
        saved = SavedSearch.objects.select_for_update().get(pk=saved.pk)
        changes: dict[str, Any] = {}
        if name is not None and _named(name) != saved.name:
            changes["name"] = [saved.name, _named(name)]
            saved.name = _named(name)
        if query is not None:
            kept = clean(user, project, query)
            if kept != saved.query:
                changes["query"] = True
                saved.query = kept
        if shared is not None and shared != saved.shared:
            if shared:
                _may_share(user, project)
                if SavedSearch.objects.filter(project=project, shared=True).count() >= MAX_SHARED:
                    raise Refused(
                        409, f"This project has {MAX_SHARED} shared searches. Remove one first."
                    )
            changes["shared"] = [saved.shared, shared]
            was_shared, saved.shared = saved.shared, shared
        else:
            was_shared = saved.shared
        if not changes:
            return saved
        saved.updated_at = timezone.now()
        try:
            with transaction.atomic():
                saved.save()
        except IntegrityError as error:
            raise _taken(error) from error
        if saved.shared and not was_shared:
            action = "saved_search.shared"
        elif was_shared and not saved.shared:
            action = "saved_search.unshared"
        elif saved.shared:
            action = "saved_search.updated"
        else:
            return saved
        audit.record(
            action,
            actor=user,
            target=saved,
            project=project,
            data={
                "name": saved.name,
                **({"previous_name": changes["name"][0]} if "name" in changes else {}),
                **({"query_changed": True} if "query" in changes else {}),
            },
        )
    return saved


def delete(user: User, saved: SavedSearch) -> None:
    if not can_delete(user, saved):
        raise Refused(
            403,
            "Only the person who saved a search, or the project’s Admins if it’s shared, "
            "can remove it.",
        )
    with transaction.atomic():
        if saved.shared:
            audit.record(
                "saved_search.removed",
                actor=user,
                target=saved,
                project=saved.project,
                data={"name": saved.name, "owner": saved.created_by.email},
            )
        saved.delete()
