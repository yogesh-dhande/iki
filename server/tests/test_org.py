"""Teams, projects, members and people: what each page does, and the rules that always hold."""

import pytest
from api_client import Api, problem
from browser import Browser, Response, path_of
from conftest import allow_test_flush
from org_world import World, build_world, post

from iki_server.accounts.models import User, UserSession
from iki_server.audit.models import AuditEvent
from iki_server.org import permissions, services
from iki_server.org.models import (
    OrgRole,
    OrgRoleAssignment,
    Project,
    ProjectMembership,
    ProjectRole,
    Team,
    TeamMembership,
)
from iki_server.org.permissions import Action

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def text(response: Response) -> str:
    return str(response.content.decode())


def follow(browser: Browser, response: Response) -> str:
    return text(browser.get(path_of(response)))


# --- The whole path: team -> member -> project -> invitation --------------------
# Project pages are the web app's: these drive the API calls it makes, with the
# session and CSRF token. What the pages say is tested in web/app and in the
# browser tests.


def api(world: World, principal: str) -> Api:
    return Api(world.browser(principal))


def project_id(world: World) -> str:
    return str(world.project.pk)


def test_org_admin_to_project_in_a_few_steps(world: World) -> None:
    admin = world.browser("org_admin")
    created = post(admin, "/teams/", {"name": "Genomics", "description": "Sequencing"})
    assert path_of(created) == "/teams/genomics/"
    assert "Created Genomics. Add its members next." in follow(admin, created)

    # Email matching ignores case, as the identity provider might send either.
    added = post(admin, "/teams/genomics/", {"person": "OUTSIDER@example.com"})
    assert "Added Outsider to Genomics." in follow(admin, added)

    scientist = api(world, "outsider")
    assert scientist.get("/me").json()["can"]["create_projects"] is True
    genomics = Team.objects.get(slug="genomics")
    created = scientist.post("/projects", {"team": str(genomics.pk), "name": "Variant Calls"})
    assert created.status_code == 201, created.content
    project = created.json()
    assert (project["slug"], project["my_role"]) == ("variant-calls", "admin")
    assert scientist.browser.get("/projects/variant-calls/").status_code == 200

    viewer = User.objects.get(email="viewer@example.com")
    invited = scientist.put(
        f"/projects/{project['id']}/members/users/{viewer.pk}", {"role": "viewer"}
    )
    assert invited.status_code == 200, invited.content
    colleague = api(world, "viewer")
    mine = colleague.get("/projects", slug="variant-calls").json()["items"]
    assert [p["my_role"] for p in mine] == ["viewer"]


def test_only_team_members_can_create_projects(world: World) -> None:
    outsider = api(world, "outsider")
    assert outsider.get("/me").json()["can"]["create_projects"] is False
    assert outsider.get("/teams", can_create_projects=True).json()["items"] == []
    refused = outsider.post("/projects", {"team": str(world.team.pk), "name": "Sneaky"})
    assert refused.status_code in (403, 404)
    assert not Project.objects.filter(name="Sneaky").exists()


def test_project_names_are_unique_per_team_and_slugs_everywhere(world: World) -> None:
    member = api(world, "team_member")
    body = problem(
        member.post("/projects", {"team": str(world.team.pk), "name": "kinase atlas"}),
        409,
        "refused",
    )
    message = "Structural Biology already has a project called “kinase atlas”."
    assert body["detail"] == message
    assert body["errors"] == [{"pointer": "#/name", "detail": message}]
    other = Project.objects.create(team=world.other_team, name="Kinase Atlas")
    assert other.slug == "kinase-atlas-2"


def test_unknown_and_deactivated_people_cant_be_added(world: World) -> None:
    admin = api(world, "admin")
    # The app looks people up by address first, and says why it can't add them.
    assert admin.get("/users", email="x@example.com").json()["items"] == []
    [inactive] = admin.get("/users", email="inactive@example.com").json()["items"]
    assert inactive["active"] is False
    # The API refuses them anyway.
    path = f"/projects/{project_id(world)}/members/users/{inactive['id']}"
    body = problem(admin.put(path, {"role": "viewer"}), 409, "refused")
    assert "deactivated" in body["detail"]


# --- Project members -----------------------------------------------------------


def test_admins_add_teams_change_roles_and_remove_people(world: World) -> None:
    admin = api(world, "admin")
    team = Team.objects.create(name="Imaging")
    TeamMembership.objects.create(team=team, user=world.users["outsider"])
    members = f"/projects/{project_id(world)}/members"
    granted = admin.put(f"{members}/teams/{team.pk}", {"role": "runner"})
    assert granted.status_code == 200, granted.content
    assert permissions.project_role(world.users["outsider"], world.project) == ProjectRole.RUNNER

    viewer = world.users["viewer"]
    changed = admin.put(f"{members}/users/{viewer.pk}", {"role": "editor"})
    assert changed.json()["role"] == "editor"
    membership = world.project.memberships.get(user=viewer)
    assert admin.delete(f"{members}/{membership.pk}").status_code == 204
    assert not world.project.memberships.filter(user=viewer).exists()


def test_members_see_the_member_list_read_only(world: World) -> None:
    viewer = api(world, "viewer")
    members = viewer.get(f"/projects/{project_id(world)}/members").json()["items"]
    assert "admin@example.com" in [m["user"]["email"] for m in members if m["user"]]
    assert "Chemistry" in [m["team"]["name"] for m in members if m["team"]]
    project = viewer.get(f"/projects/{project_id(world)}").json()
    assert project["can"] == {
        "read": True,
        "manage": False,
        "manage_members": False,
        "manage_webhooks": False,
    }
    outsider = world.users["outsider"]
    path = f"/projects/{project_id(world)}/members/users/{outsider.pk}"
    problem(viewer.put(path, {"role": "admin"}), 403, "forbidden")


def test_the_last_admin_cant_leave_or_step_down(world: World) -> None:
    ProjectMembership.objects.filter(project=world.project, user=world.users["inactive"]).delete()
    admin = api(world, "admin")
    mine = world.project.memberships.get(user=world.users["admin"])
    members = f"/projects/{project_id(world)}/members"
    for refused in (
        admin.delete(f"{members}/{mine.pk}"),
        admin.put(f"{members}/users/{world.users['admin'].pk}", {"role": "runner"}),
    ):
        body = problem(refused, 409, "refused")
        assert body["detail"].startswith("A project needs at least one Admin.")
    mine.refresh_from_db()
    assert mine.role == ProjectRole.ADMIN


def test_a_deactivated_admin_doesnt_count_as_the_other_admin(world: World) -> None:
    # world.project has "admin" plus a deactivated admin.
    membership = world.project.memberships.get(user=world.users["admin"])
    with pytest.raises(services.Refused):
        services.remove_project_membership(membership, by=world.users["admin"])


def test_a_team_with_admin_role_counts_as_another_admin(world: World) -> None:
    ProjectMembership.objects.filter(project=world.project, user=world.users["inactive"]).delete()
    world.project.memberships.filter(team=world.other_team).update(role=ProjectRole.ADMIN)
    membership = world.project.memberships.get(user=world.users["admin"])
    services.remove_project_membership(membership, by=world.users["admin"])


# --- Org Admins and project data ------------------------------------------------


def test_org_admins_see_projects_but_not_their_data_until_they_join(world: World) -> None:
    admin = api(world, "org_admin")
    project = admin.get(f"/projects/{project_id(world)}").json()
    assert project["my_role"] is None
    assert project["can"] == {
        "read": False,
        "manage": False,
        "manage_members": True,
        "manage_webhooks": False,
    }

    me = world.users["org_admin"]
    path = f"/projects/{project_id(world)}/members/users/{me.pk}"
    assert admin.put(path, {"role": "admin"}).status_code == 200
    assert permissions.can(me, Action.PROJECT_READ, world.project)
    project = admin.get(f"/projects/{project_id(world)}").json()
    assert project["can"] == {
        "read": True,
        "manage": True,
        "manage_members": True,
        "manage_webhooks": True,
    }


def test_org_admins_can_recover_orphaned_projects(world: World) -> None:
    orphan = Project.objects.create(team=world.team, name="Orphan")
    admin = api(world, "org_admin")
    assert admin.get(f"/projects/{orphan.pk}/members").json()["items"] == []
    me = world.users["org_admin"]
    joined = admin.put(f"/projects/{orphan.pk}/members/users/{me.pk}", {"role": "admin"})
    assert joined.status_code == 200


# --- Archiving -----------------------------------------------------------------


def test_archived_projects_are_read_only_until_restored(world: World) -> None:
    admin = api(world, "admin")
    path = f"/projects/{project_id(world)}"
    assert admin.patch(path, {"archived": True}).json()["archived"] is True
    world.project.refresh_from_db()
    assert world.project.is_archived

    body = problem(admin.patch(path, {"name": "Renamed"}), 409, "refused")
    assert body["detail"] == "Restore the project before changing it."
    admin.patch(path, {"archived": False})
    saved = admin.patch(path, {"name": "Kinase Atlas 2", "description": "New"}).json()
    assert saved["slug"] == world.project.slug  # the slug survives renames
    world.project.refresh_from_db()
    assert (world.project.name, world.project.is_archived) == ("Kinase Atlas 2", False)


def test_archived_teams_are_offered_to_no_one_for_new_projects(world: World) -> None:
    admin = world.browser("org_admin")
    post(admin, f"/teams/{world.team.slug}/settings/", {"action": "archive"})
    member = api(world, "team_member")
    assert member.get("/me").json()["can"]["create_projects"] is False
    assert member.get("/teams", can_create_projects=True).json()["items"] == []
    refused = member.post("/projects", {"team": str(world.team.pk), "name": "Late"})
    assert refused.status_code >= 400
    assert not Project.objects.filter(name="Late").exists()


# --- People and org roles ------------------------------------------------------


def test_people_page_lists_everyone_with_roles(world: World) -> None:
    page = text(world.browser("org_admin").get("/people/"))
    assert "compute_admin@example.com" in page
    assert "Compute Admin" in page
    assert "Deactivated" in page


def test_people_link_only_for_org_admins(world: World) -> None:
    assert 'href="/people/"' in text(world.browser("org_admin").get("/teams/"))
    assert 'href="/people/"' not in text(world.browser("admin").get("/teams/"))


def test_org_admins_grant_and_revoke_org_roles(world: World) -> None:
    admin = world.browser("org_admin")
    viewer = world.users["viewer"]
    saved = post(admin, f"/people/{viewer.pk}/", {"roles": ["auditor", "compute_admin"]})
    assert "Saved Viewer’s org roles." in follow(admin, saved)
    assert permissions.org_roles(viewer) == {OrgRole.AUDITOR, OrgRole.COMPUTE_ADMIN}
    post(admin, f"/people/{viewer.pk}/", {})
    assert not OrgRoleAssignment.objects.filter(user=viewer).exists()


def test_the_last_org_admin_cant_step_down(world: World) -> None:
    admin = world.browser("org_admin")
    me = world.users["org_admin"]
    page = follow(admin, post(admin, f"/people/{me.pk}/", {"roles": "auditor"}))
    assert "Iki needs at least one Org Admin." in page
    assert OrgRoleAssignment.objects.filter(user=me, role=OrgRole.ORG_ADMIN).exists()
    # A deactivated Org Admin doesn't count; an active second one does.
    services.grant_org_role(world.users["viewer"], OrgRole.ORG_ADMIN, by=me)
    post(admin, f"/people/{me.pk}/", {"roles": "auditor"})
    assert not OrgRoleAssignment.objects.filter(user=me, role=OrgRole.ORG_ADMIN).exists()


def test_deactivating_someone_signs_them_out_everywhere(world: World) -> None:
    victim = world.browser("viewer")
    assert victim.get("/").status_code == 200
    assert UserSession.objects.filter(user=world.users["viewer"]).exists()

    admin = world.browser("org_admin")
    done = post(admin, f"/people/{world.users['viewer'].pk}/", {"action": "deactivate"})
    assert "Deactivated Viewer." in follow(admin, done)
    assert not UserSession.objects.filter(user=world.users["viewer"]).exists()
    assert path_of(victim.get("/")).startswith("/login/")

    post(admin, f"/people/{world.users['viewer'].pk}/", {"action": "activate"})
    assert User.objects.get(pk=world.users["viewer"].pk).is_active
    # Memberships survive, so access comes back with the account.
    assert world.project.memberships.filter(user=world.users["viewer"]).exists()


def test_nobody_deactivates_themselves_or_the_last_org_admin(world: World) -> None:
    me = world.users["org_admin"]
    admin = world.browser("org_admin")
    page = follow(admin, post(admin, f"/people/{me.pk}/", {"action": "deactivate"}))
    assert "You can’t deactivate yourself." in page
    other = world.users["viewer"]
    services.grant_org_role(other, OrgRole.ORG_ADMIN, by=me)
    services.set_org_roles(me, set(), by=me)
    with pytest.raises(services.Refused, match="at least one active Org Admin"):
        services.set_active(other, False, by=world.users["compute_admin"])


def test_inactive_people_get_no_new_roles(world: World) -> None:
    inactive = world.users["inactive"]
    with pytest.raises(services.Refused, match="deactivated"):
        services.grant_project_role(world.project, ProjectRole.VIEWER, user=inactive, by=inactive)
    with pytest.raises(services.Refused, match="deactivated"):
        services.add_team_member(world.team, inactive, by=world.users["org_admin"])
    with pytest.raises(services.Refused, match="deactivated"):
        services.set_org_roles(inactive, {OrgRole.AUDITOR}, by=world.users["org_admin"])


# --- Structure -----------------------------------------------------------------


def test_project_memberships_are_for_exactly_one_person_or_team(world: World) -> None:
    from django.db import IntegrityError, transaction

    for fields in ({}, {"user": world.users["outsider"], "team": world.team}):
        with pytest.raises(IntegrityError), transaction.atomic():
            ProjectMembership.objects.create(project=world.project, role=10, **fields)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProjectMembership.objects.create(
            project=world.project, user=world.users["outsider"], role=99
        )


def test_teams_with_projects_cant_be_deleted(world: World) -> None:
    from django.db.models import ProtectedError

    with pytest.raises(ProtectedError):
        world.team.delete()


def test_changes_are_audited(world: World) -> None:
    by = world.users["org_admin"]
    team = services.create_team("Proteomics", "", by=by)
    services.add_team_member(team, world.users["outsider"], by=by)
    project = services.create_project(team, "Mass Spec", "", by=world.users["outsider"])
    services.set_project_archived(project, True, by=world.users["outsider"])
    events = list(AuditEvent.objects.order_by("occurred_at", "id"))
    assert [e.action for e in events] == [
        "team.created",
        "team.member_added",
        "project.created",
        "project.archived",
    ]
    assert (events[0].actor, events[0].team, events[0].target_label) == (by, team, "Proteomics")
    assert events[1].target_id == str(world.users["outsider"].pk)
    assert events[2].project == project


def test_refused_changes_leave_no_audit_event(world: World) -> None:
    ProjectMembership.objects.filter(project=world.project, user=world.users["inactive"]).delete()
    membership = world.project.memberships.get(user=world.users["admin"])
    with pytest.raises(services.Refused):
        services.remove_project_membership(membership, by=world.users["admin"])
    assert not AuditEvent.objects.exists()


def test_org_admins_joining_a_project_is_audited_as_self_granted(world: World) -> None:
    me = world.users["org_admin"]
    api(world, "org_admin").put(
        f"/projects/{project_id(world)}/members/users/{me.pk}", {"role": "admin"}
    )
    event = AuditEvent.objects.get(action="project.role_granted")
    assert event.actor == me
    assert event.project == world.project
    assert event.data == {"role": "Admin", "self": True}
    assert event.via == "web"


def test_role_changes_record_the_previous_role(world: World) -> None:
    services.grant_project_role(
        world.project, ProjectRole.RUNNER, user=world.users["viewer"], by=world.users["admin"]
    )
    event = AuditEvent.objects.get()
    assert event.data == {"role": "Runner", "previous_role": "Viewer"}
    # Granting the same role again changes nothing and records nothing.
    services.grant_project_role(
        world.project, ProjectRole.RUNNER, user=world.users["viewer"], by=world.users["admin"]
    )
    assert AuditEvent.objects.count() == 1


def test_renames_record_what_changed(world: World) -> None:
    services.update_project(world.project, "Kinase Atlas", "New text", by=world.users["admin"])
    assert AuditEvent.objects.get().data == {"description": ["", "New text"]}


@pytest.mark.django_db(transaction=True)
def test_the_first_admin_from_before_org_roles_becomes_an_org_admin() -> None:
    """Installations set up before org roles had a superuser; the migration converts them."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    try:
        executor.migrate([("iki_accounts", "0001_initial"), ("iki_org", None)])
        old_apps = executor.loader.project_state([("iki_accounts", "0001_initial")]).apps
        old_user = old_apps.get_model("iki_accounts", "User")
        old_user.objects.create(email="ada@example.com", is_superuser=True, password="!")
        old_user.objects.create(email="rosalind@example.com", password="!")
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        allow_test_flush()
    roles = set(OrgRoleAssignment.objects.values_list("user__email", "role"))
    assert roles == {("ada@example.com", "org_admin")}
