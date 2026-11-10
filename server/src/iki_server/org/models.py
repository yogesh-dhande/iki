"""How the organization is structured: org roles, teams and projects.

One Iki installation serves one organization (ADR 0002). People belong to
teams; every project belongs to one team, which sets its compute limits and
pays for its costs. Access to a project's data comes only from project
memberships, granted to a person directly or to a whole team (ADR 0024).

Every authorization decision goes through :mod:`iki_server.org.permissions`.
Nothing else reads these tables to decide what someone may do.
"""

from typing import ClassVar

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
