"""People in records (ADR 0122): who a field naming a person can name, and how they're shown.

A field of ``type: person`` (or a list of them) names people by their user ID. Only
someone who could do the record's work can be named:

- a person, not a service account or a placeholder an import made (ADR 0096);
- active;
- able to read the record's project, through a role of their own or a team's;
- holding the field's lab role there, if it has one (ADR 0116).

Those rules hold when a value is written, by anyone or anything. Afterwards people
leave: they're deactivated, removed from the project, or lose the role. The record
keeps whoever it named, as its history must, and :func:`why_not` says why they
couldn't be named now, which is what flags the record for reassignment and takes it
off their worklist (:mod:`iki_server.entities.assignments`).

A refusal never says more than the caller could find out on the project's Members
page: naming someone who isn't in the project reads the same whether or not they
exist. A person's email is shown with their name only to people who could see it
there: colleagues in the record's project (:func:`shown`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.db.models import Exists, Q, QuerySet

from iki_server.accounts.models import Kind, User
from iki_server.org import lab_roles, permissions
from iki_server.org.models import LabRoleGrant, Project, ProjectMembership, ProjectRole
from iki_spec.model import Field, person_role

__all__ = [
    "Namer",
    "eligible",
    "nameable",
    "shown",
    "why_not",
]

#: The most people a list naming people may hold.
MAX_PEOPLE = 50


def nameable(project: Project, role: str | None = None) -> QuerySet[User]:
    """The people a record of ``project`` can name in a field limited to ``role``."""
    people = permissions.members_with(project, ProjectRole.VIEWER).filter(
        kind=Kind.PERSON, imported_from=""
    )
    if role is not None:
        if role not in lab_roles.declared():
            return people.none()
        people = people.filter(pk__in=permissions.holders_of(project, role).values("pk"))
    return people


def why_not(person: User, project: Project, role: str | None = None) -> str | None:
    """Why ``person`` couldn't be named in ``project`` now, in a few words, or None if they
    can. For someone a record names already: the reason it needs reassigning."""
    if person.kind != Kind.PERSON:
        return "a service account"
    if not person.is_active:
        return "deactivated"
    if person.imported_from:
        return f"not signed in here: imported from {person.imported_from}"
    if not nameable(project).filter(pk=person.pk).exists():
        return f"no longer in {project.name}"
    if role is not None and not nameable(project, role).filter(pk=person.pk).exists():
        return f"no longer {lab_roles.label(role)} in {project.name}"
    return None


def eligible(person: Any, project: Any, role: str | None = None) -> Q:  # noqa: ANN401 - expressions
    """A condition true where ``person`` (a user's ID, or an ``OuterRef`` to one) could be
    named in ``project`` (likewise) now: what :func:`nameable` says, in SQL."""
    grants = ProjectMembership.objects.filter(project_id=project)
    member = Exists(grants.filter(user_id=person)) | Exists(
        grants.filter(team__memberships__user_id=person)
    )
    active = Exists(
        User.objects.filter(pk=person, is_active=True, kind=Kind.PERSON, imported_from="")
    )
    condition = Q(active) & Q(member)
    if role is not None:
        held = LabRoleGrant.objects.filter(membership__project_id=project, role=role)
        condition &= Q(
            Exists(held.filter(membership__user_id=person))
            | Exists(held.filter(membership__team__memberships__user_id=person))
        )
        if role not in lab_roles.declared():
            condition &= Q(pk__in=[])
    return condition


# --- Writing --------------------------------------------------------------------------------------


class Namer:
    """Finds the people values name, in a project, so writing one never says more about
    someone than the project's Members page would."""

    def __init__(self, project: Project | None) -> None:
        self.project = project
        #: (role, how they were named) → their ID, or None for nobody who can be named.
        self._found: dict[tuple[str | None, str], uuid.UUID | None] = {}
        #: Whom the value being written names already, who may stay named.
        self._kept: set[uuid.UUID] = set()

    @contextmanager
    def keeping(self, kept: Iterable[Any]) -> Iterator[None]:
        """Inside, whoever ``kept`` names may be named, though they couldn't be anew: the
        people a field names already, who left or lost the role since."""
        before = self._kept
        self._kept = {uuid.UUID(str(k)) for k in kept}
        try:
            yield
        finally:
            self._kept = before

    def person(self, f: Field, value: Any) -> uuid.UUID:  # noqa: ANN401 - JSON
        """The person ``value`` names (their ID, or their email address), who ``f`` can name.
        Raises ``ValueError`` with what's wrong."""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must name a person: their ID, or their email address.")
        key = value.strip()
        if self._kept:
            try:
                if uuid.UUID(key) in self._kept:
                    return uuid.UUID(key)
            except ValueError:
                pass
        if self.project is None:
            raise ValueError("can't name anyone here.")
        role = person_role(f)
        self.look_up(role, [key])
        found = self._found[(role, key.casefold())]
        if found is not None:
            return found
        raise ValueError(self.refusal(role, key))

    def look_up(self, role: str | None, keys: Iterable[str]) -> None:
        """Finds those of ``keys`` (IDs or email addresses) not looked up yet, in one query."""
        assert self.project is not None  # noqa: S101 - callers check
        wanted = {k.strip().casefold(): k.strip() for k in keys}
        wanted = {k: v for k, v in wanted.items() if (role, k) not in self._found}
        if not wanted:
            return
        ids: list[uuid.UUID] = []
        emails: list[str] = []
        for key in wanted.values():
            try:
                ids.append(uuid.UUID(key))
            except ValueError:
                emails.append(key)
        found = nameable(self.project, role).filter(Q(pk__in=ids) | Q(email__in=emails))
        by_key: dict[str, uuid.UUID] = {}
        for pk, email in found.values_list("pk", "email"):
            by_key[str(pk)] = pk
            by_key[email.casefold()] = pk
        for folded in wanted:
            self._found[(role, folded)] = by_key.get(folded)

    def refusal(self, role: str | None, key: str) -> str:
        """Why ``key`` can't be named: the same for someone outside the project as for nobody,
        and naming a member only when they're in the project, as its Members page does."""
        assert self.project is not None  # noqa: S101 - callers check
        where = self.project.name
        if role is not None:
            self.look_up(None, [key])
            member = self._found[(None, key.casefold())]
            label = lab_roles.label(role)
            if member is not None:
                name = User.objects.filter(pk=member).values_list("name", flat=True).first()
                who = f"{name} doesn't" if name else "they don't"
                return f"must name someone with the {label} role in {where}: {who} have it."
            return (
                f"isn't someone who can be named here: name an active person in {where} "
                f"with the {label} role."
            )
        return f"isn't someone who can be named here: name an active person in {where}."


# --- Reading --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shown:
    """A person as a record shows them: who, and their email when the reader may see it."""

    id: uuid.UUID
    name: str
    email: str | None
    active: bool


def shown(
    reader: User,
    ids: Iterable[Any],
    projects: Mapping[Any, Any],
) -> dict[uuid.UUID, Shown]:
    """The people with these IDs, as ``reader`` sees them in records. ``projects``: each
    person's ID → the IDs of the projects whose records name them, that the reader can
    read the members of (their own colleagues there see their email)."""
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return {}
    people = list(
        User.objects.filter(pk__in=wanted).only("pk", "name", "email", "is_active", "kind")
    )
    shared = _colleagues(reader, people, projects)
    out = {}
    for person in people:
        visible = person.pk == reader.pk or person.pk in shared
        out[person.pk] = Shown(
            id=person.pk,
            name=person.name or (person.email if visible else "Someone without a name"),
            email=person.email if visible and person.kind == Kind.PERSON else None,
            active=person.is_active,
        )
    return out


def _colleagues(reader: User, people: list[User], projects: Mapping[Any, Any]) -> set[uuid.UUID]:
    """Of ``people``, those who can read one of the projects ``projects`` names for them,
    which the reader can read too."""
    readable = {
        p.pk
        for p in permissions.visible_projects(reader).filter(my_role__isnull=False)  # type: ignore[misc]
    }
    wanted: dict[Any, set[Any]] = {}
    for person in people:
        mine = {p for p in projects.get(person.pk, ()) if p in readable}
        if mine:
            wanted[person.pk] = mine
    if not wanted:
        return set()
    every = {p for ps in wanted.values() for p in ps}
    grants = ProjectMembership.objects.filter(project_id__in=every)
    rows = set(grants.filter(user_id__in=wanted).values_list("user_id", "project_id")) | set(
        grants.filter(team__memberships__user_id__in=wanted).values_list(
            "team__memberships__user_id", "project_id"
        )
    )
    return {person for person, project in rows if project in wanted.get(person, set())}
