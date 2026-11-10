"""A small organization for authorization tests: teams, projects, and one person per role."""

from collections.abc import Mapping
from dataclasses import dataclass

from browser import Browser, Response
from django.conf import settings
from django.utils import timezone
from django.utils.crypto import get_random_string

from iki_server.accounts.models import User
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Team,
    TeamMembership,
)
from iki_server.setup.models import Installation

#: Everyone in the world, by the role they play. ``anonymous`` is not signed in.
PRINCIPALS = (
    "anonymous",
    "inactive",
    "outsider",
    "team_member",
    "org_admin",
    "compute_admin",
    "auditor",
    "viewer",
    "editor",
    "runner",
    "admin",
    "via_team",
    "mixed",
)


@dataclass
class World:
    team: Team
    """Owns both projects. Only ``team_member`` belongs to it."""
    other_team: Team
    """Has an Editor grant on both projects. ``via_team`` and ``mixed`` belong to it."""
    project: Project
    archived: Project
    users: dict[str, User]

    def user(self, principal: str) -> User | None:
        return self.users.get(principal)

    def browser(self, principal: str) -> Browser:
        browser = Browser()
        user = self.user(principal)
        if user is not None:
            browser.force_login(user)
        return browser


def build_world() -> World:
    Installation.objects.update_or_create(
        pk=1, defaults={"organization_name": "Example Bio", "setup_completed_at": timezone.now()}
    )
    users = {
        name: User.objects.create_user(f"{name}@example.com", name.replace("_", " ").title())
        for name in PRINCIPALS
        if name != "anonymous"
    }
    team = Team.objects.create(name="Structural Biology")
    other_team = Team.objects.create(name="Chemistry")
    project = Project.objects.create(team=team, name="Kinase Atlas")
    archived = Project.objects.create(team=team, name="Old Screens", archived_at=timezone.now())

    TeamMembership.objects.create(team=team, user=users["team_member"])
    for name in ("via_team", "mixed"):
        TeamMembership.objects.create(team=other_team, user=users[name])
    for name, role in (
        ("org_admin", OrgRole.ORG_ADMIN),
        ("compute_admin", OrgRole.COMPUTE_ADMIN),
        ("auditor", OrgRole.AUDITOR),
        ("inactive", OrgRole.ORG_ADMIN),
    ):
        OrgRoleAssignment.objects.create(user=users[name], role=role)
    for p in (project, archived):
        for name, project_role in (
            ("viewer", ProjectRole.VIEWER),
            ("editor", ProjectRole.EDITOR),
            ("runner", ProjectRole.RUNNER),
            ("admin", ProjectRole.ADMIN),
            ("mixed", ProjectRole.VIEWER),
            ("inactive", ProjectRole.ADMIN),
        ):
            ProjectMembership.objects.create(project=p, user=users[name], role=project_role)
        ProjectMembership.objects.create(project=p, team=other_team, role=ProjectRole.EDITOR)

    users["inactive"].is_active = False
    users["inactive"].save()
    return World(team, other_team, project, archived, users)


def post(browser: Browser, url: str, data: Mapping[str, object] | None = None) -> Response:
    """POSTs with a valid CSRF token, as a page's form would."""
    if settings.CSRF_COOKIE_NAME not in browser.cookies:
        browser.cookies[settings.CSRF_COOKIE_NAME] = get_random_string(32)
    token = browser.cookies[settings.CSRF_COOKIE_NAME].value
    return browser.post(url, {"csrfmiddlewaretoken": token, **(data or {})})
