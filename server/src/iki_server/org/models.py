"""How the organization is structured: org roles, teams and projects.

One Iki installation serves one organization (ADR 0002). People belong to
teams; every project belongs to one team, which sets its compute limits and
pays for its costs. Access to a project's data comes only from project
memberships, granted to a person directly or to a whole team (ADR 0024).

Every authorization decision goes through :mod:`iki_server.org.permissions`.
Nothing else reads these tables to decide what someone may do.
"""

from typing import Any, ClassVar
from uuid import UUID

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.text import slugify

from iki_server.accounts.models import CASE_INSENSITIVE
from iki_server.core.ids import uuid7

SLUG_MAX_LENGTH = 60


class OrgRole(models.TextChoices):
    ORG_ADMIN = "org_admin", "Org Admin"
    COMPUTE_ADMIN = "compute_admin", "Compute Admin"
    AUDITOR = "auditor", "Auditor"


ORG_ROLE_DESCRIPTIONS = {
    OrgRole.ORG_ADMIN: "Manages people, teams and settings. Sees every project’s name and "
    "members, but reads project data only as a member.",
    OrgRole.COMPUTE_ADMIN: "Manages compute: templates, worker pools, builds, volumes and cost.",
    OrgRole.AUDITOR: "Reads the audit log.",
}


class ProjectRole(models.IntegerChoices):
    """Project roles. Each includes everything the roles below it can do."""

    VIEWER = 10, "Viewer"
    EDITOR = 20, "Editor"
    RUNNER = 30, "Runner"
    ADMIN = 40, "Admin"


PROJECT_ROLE_DESCRIPTIONS = {
    ProjectRole.VIEWER: "Reads entities, runs and views.",
    ProjectRole.EDITOR: "Also edits entities and runs actions on backend workers and "
    "people’s own machines.",
    ProjectRole.RUNNER: "Also runs actions on containers and cloud VMs, which cost more.",
    ProjectRole.ADMIN: "Also manages the project, its settings and its members.",
}


class OrgRoleAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="org_roles"
    )
    role = models.CharField(max_length=32, choices=OrgRole.choices)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iki_org_role"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["user", "role"], name="iki_org_role_once"),
        ]

    def __str__(self) -> str:
        return f"{self.user} is {self.get_role_display()}"


def unique_slug(name: str, model: type[models.Model], fallback: str) -> str:
    """A URL slug from ``name``, made unique with a numeric suffix."""
    base = slugify(name)[: SLUG_MAX_LENGTH - 4].strip("-") or fallback
    slug, n = base, 2
    while model._default_manager.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100, unique=True, db_collation=CASE_INSENSITIVE)
    #: Set once at creation, so links keep working after a rename.
    slug = models.SlugField(max_length=SLUG_MAX_LENGTH, unique=True, editable=False)
    description = models.TextField(max_length=1000, blank=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="TeamMembership",
        through_fields=("team", "user"),
        related_name="teams",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    #: Archived teams can't get new projects. Existing access is unchanged.
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_team"
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.slug:
            self.slug = unique_slug(self.name, Team, "team")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class TeamMembership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships"
    )
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iki_team_membership"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["team", "user"], name="iki_team_member_once"),
        ]

    def __str__(self) -> str:
        return f"{self.user} in {self.team}"


class Registry(models.Model):
    """An identity boundary (ADR 0115): within it, an identity (a sequence, a structure) is
    one registry entry with one ID, and every project is in exactly one. The default one
    is every team's; Org Admins add more, for a CRO's clients or a partner program, each
    used by the teams they name."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    name = models.CharField(max_length=100, unique=True, db_collation=CASE_INSENSITIVE)
    #: Set once at creation, so links keep working after a rename.
    slug = models.SlugField(max_length=SLUG_MAX_LENGTH, unique=True, editable=False)
    description = models.TextField(max_length=2000, blank=True)
    #: The one new projects are in unless they're given another. Every team may use it.
    is_default = models.BooleanField(default=False)
    #: Every team's projects may use it; otherwise only those of ``teams``.
    all_teams = models.BooleanField(
        default=False, db_comment="Whether every team's projects may use it (ADR 0115)."
    )
    teams = models.ManyToManyField(
        Team, blank=True, related_name="registries", db_table="iki_registry_team"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iki_registry"
        db_table_comment = "Identity boundaries: one entry per identity in each (ADR 0115)."
        ordering = ("-is_default", "name")
        verbose_name_plural = "registries"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["is_default"], condition=Q(is_default=True), name="iki_registry_one_default"
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(all_teams=True),
                name="iki_registry_default_is_everyones",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.slug:
            self.slug = unique_slug(self.name, Registry, "registry")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def default(cls) -> "Registry":
        """The default registry, made the first time it's needed."""
        found = cls.objects.filter(is_default=True).first()
        if found is not None:
            return found
        registry, _ = cls.objects.get_or_create(
            is_default=True,
            defaults={
                "name": DEFAULT_REGISTRY_NAME,
                "all_teams": True,
                "description": DEFAULT_REGISTRY_DESCRIPTION,
            },
        )
        return registry


#: What the default registry is called until an Org Admin renames it.
DEFAULT_REGISTRY_NAME = "Main"
DEFAULT_REGISTRY_DESCRIPTION = (
    "The organization's registry. Projects are in it unless an Org Admin gives them another."
)


def default_registry() -> Any:  # noqa: ANN401 - a UUID, as Django's field defaults are
    """New projects' registry, unless they're given another."""
    return Registry.default().pk


class Registrar(models.Model):
    """Someone who curates a registry (ADR 0115): changes its entries' facts once they're
    set, and supersedes mistaken entries. Org Admins appoint them, a registry at a time."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    registry = models.ForeignKey(Registry, on_delete=models.CASCADE, related_name="registrars")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="registrar_of"
    )
    appointed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    appointed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iki_registrar"
        db_table_comment = "Who curates each registry's facts and supersessions (ADR 0115)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["registry", "user"], name="iki_registrar_once"),
        ]

    def __str__(self) -> str:
        return f"{self.user} curates {self.registry}"


def registry_of(project: "Project") -> UUID:
    """A project's registry's ID: always set, though django-stubs, for its default, can't
    tell."""
    found = project.registry_id
    assert found is not None  # noqa: S101 - the column is NOT NULL
    return found


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="projects")
    name = models.CharField(max_length=100, db_collation=CASE_INSENSITIVE)
    #: Set once at creation, so links keep working after a rename.
    slug = models.SlugField(max_length=SLUG_MAX_LENGTH, unique=True, editable=False)
    description = models.TextField(max_length=2000, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    #: Archived projects are read-only until a project Admin restores them.
    archived_at = models.DateTimeField(null=True, blank=True)
    #: Off for confidential work (a CRO's or partner's data, a blinded study): its entities
    #: can't be shared with other projects, by a link, a run, a reference or a duplicate.
    allow_sharing = models.BooleanField(
        default=True,
        db_comment="Whether its entities may be shared with other projects (ADR 0067).",
    )
    #: Its identity boundary (ADR 0115): what it registers is one entry with every other
    #: project's in the registry. Fixed once it has entities.
    registry = models.ForeignKey(
        Registry, on_delete=models.PROTECT, related_name="projects", default=default_registry
    )

    class Meta:
        db_table = "iki_project"
        ordering = ("name",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=["team", "name"], name="iki_project_name_per_team"),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args: object, **kwargs: object) -> None:
        if not self.slug:
            self.slug = unique_slug(self.name, Project, "project")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class LegalHold(models.Model):
    """A project kept whole for a legal or regulatory matter (ADR 0092): while a hold is on,
    nothing in the project is deleted, by retention or a purge. Org Admins place and release
    holds; each is audited. A project can be under several, one per matter."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="legal_holds")
    #: The matter it's for, such as a case, an inspection or a request's reference.
    matter = models.CharField(max_length=200)
    note = models.TextField(max_length=2000, blank=True)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    placed_at = models.DateTimeField(auto_now_add=True)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iki_legal_hold"
        db_table_comment = "Projects kept whole for a legal or regulatory matter (ADR 0092)."
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["project", "matter"],
                condition=Q(released_at__isnull=True),
                name="iki_legal_hold_once_per_matter",
            ),
            models.CheckConstraint(
                condition=Q(released_at__isnull=True, released_by__isnull=True)
                | Q(released_at__isnull=False, released_by__isnull=False),
                name="iki_legal_hold_released_by_someone",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.matter} ({self.project})"


class ProjectMembership(models.Model):
    """A project role granted to one person or to every member of a team.

    Someone's role in a project is the highest of their direct grant and
    the grants of every team they belong to.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="project_memberships",
    )
    team = models.ForeignKey(
        Team, null=True, blank=True, on_delete=models.CASCADE, related_name="project_memberships"
    )
    role = models.PositiveSmallIntegerField(choices=ProjectRole.choices)
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iki_project_membership"
        ordering = ("-role", "added_at")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(user__isnull=False, team__isnull=True)
                | Q(user__isnull=True, team__isnull=False),
                name="iki_project_member_is_user_or_team",
            ),
            models.CheckConstraint(
                condition=Q(role__in=ProjectRole.values), name="iki_project_role_valid"
            ),
            models.UniqueConstraint(
                fields=["project", "user"],
                condition=Q(user__isnull=False),
                name="iki_project_user_once",
            ),
            models.UniqueConstraint(
                fields=["project", "team"],
                condition=Q(team__isnull=False),
                name="iki_project_team_once",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user or self.team} is {self.get_role_display()} of {self.project}"
