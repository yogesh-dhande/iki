"""Teams, projects, members and people: what each page does, and the rules that always hold."""

import pytest
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


def test_org_admin_to_project_in_a_few_steps(world: World) -> None:
    admin = world.browser("org_admin")
    created = post(admin, "/teams/", {"name": "Genomics", "description": "Sequencing"})
    assert path_of(created) == "/teams/genomics/"
    assert "Created Genomics. Add its members next." in follow(admin, created)

    # Email matching ignores case, as the identity provider might send either.
    added = post(admin, "/teams/genomics/", {"person": "OUTSIDER@example.com"})
    assert "Added Outsider to Genomics." in follow(admin, added)

    scientist = world.browser("outsider")
    home = text(scientist.get("/"))
    assert "No projects yet" in home and "Create a project" in home
    created = post(
        scientist,
        "/projects/new/",
        {"team": str(Team.objects.get(slug="genomics").pk), "name": "Variant Calls"},
    )
    assert path_of(created) == "/projects/variant-calls/"
    page = follow(scientist, created)
    assert "Created Variant Calls. You’re its Admin." in page
    assert "<strong>Admin</strong>" in page

    invited = post(
        scientist,
        "/projects/variant-calls/members/",
        {"add": "person", "person-person": "viewer@example.com", "person-role": "10"},
    )
    assert "Viewer is now a Viewer." in follow(scientist, invited)
    colleague = world.browser("viewer")
    assert "Variant Calls" in text(colleague.get("/"))
    assert colleague.get("/projects/variant-calls/").status_code == 200


def test_new_project_page_explains_how_to_get_a_team(world: World) -> None:
    page = text(world.browser("outsider").get("/projects/new/"))
    assert "you’re not in a team yet" in page
    assert "Ask an Org Admin" in page
    assert "Create a team" in text(world.browser("org_admin").get("/projects/new/"))


def test_project_names_are_unique_per_team_and_slugs_everywhere(world: World) -> None:
    member = world.browser("team_member")
    response = post(member, "/projects/new/", {"team": str(world.team.pk), "name": "kinase atlas"})
    assert response.status_code == 200
    assert "Structural Biology already has a project called “kinase atlas”." in text(response)
    other = Project.objects.create(team=world.other_team, name="Kinase Atlas")
    assert other.slug == "kinase-atlas-2"


def test_unknown_and_deactivated_people_cant_be_added(world: World) -> None:
    admin = world.browser("admin")
    url = f"/projects/{world.project.slug}/members/"
    unknown = post(admin, url, {"add": "person", "person-person": "x@example.com"})
    assert "No one with that email address has signed in to Iki yet." in text(unknown)
    inactive = post(admin, url, {"add": "person", "person-person": "inactive@example.com"})
    assert "That person’s account is deactivated." in text(inactive)


# --- Project members -----------------------------------------------------------


def test_admins_add_teams_change_roles_and_remove_people(world: World) -> None:
    admin = world.browser("admin")
    team = Team.objects.create(name="Imaging")
    TeamMembership.objects.create(team=team, user=world.users["outsider"])
    url = f"/projects/{world.project.slug}/members/"
    added = post(admin, url, {"add": "team", "team-team": str(team.pk), "team-role": "30"})
    assert "Everyone in Imaging is now a Runner." in follow(admin, added)
    assert permissions.project_role(world.users["outsider"], world.project) == ProjectRole.RUNNER

    viewer = world.project.memberships.get(user=world.users["viewer"])
    changed = post(admin, f"{url}{viewer.pk}/", {"action": "change", "role": "20"})
    assert "Viewer is now an Editor." in follow(admin, changed)
    removed = post(admin, f"{url}{viewer.pk}/", {"action": "remove"})
    assert "Removed Viewer from Kinase Atlas." in follow(admin, removed)
    assert not world.project.memberships.filter(user=world.users["viewer"]).exists()


def test_members_see_the_member_list_read_only(world: World) -> None:
    page = text(world.browser("viewer").get(f"/projects/{world.project.slug}/members/"))
    assert "admin@example.com" in page and "Chemistry" in page
    assert 'name="action" value="remove"' not in page
    assert "What each role can do" in page


def test_the_last_admin_cant_leave_or_step_down(world: World) -> None:
    ProjectMembership.objects.filter(project=world.project, user=world.users["inactive"]).delete()
    admin = world.browser("admin")
    mine = world.project.memberships.get(user=world.users["admin"])
    url = f"/projects/{world.project.slug}/members/{mine.pk}/"
    for data in ({"action": "remove"}, {"action": "change", "role": "30"}):
        page = follow(admin, post(admin, url, data))
        assert "A project needs at least one Admin." in page
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
    admin = world.browser("org_admin")
    page = text(admin.get(f"/projects/{world.project.slug}/"))
    assert "You’re not a member of this project" in page
    assert "Add me as Admin" in page
    home = text(admin.get("/"))
    assert "can also see 1 project you’re not a member of" in home

    joined = post(admin, f"/projects/{world.project.slug}/join/")
    assert "You’re now an Admin of Kinase Atlas." in follow(admin, joined)
    user = world.users["org_admin"]
    assert permissions.can(user, Action.PROJECT_READ, world.project)
    assert admin.get(f"/projects/{world.project.slug}/settings/").status_code == 200


def test_orphaned_projects_point_org_admins_at_recovery(world: World) -> None:
    orphan = Project.objects.create(team=world.team, name="Orphan")
    page = text(world.browser("org_admin").get(f"/projects/{orphan.slug}/"))
    assert "This project has no Admins." in page


# --- Archiving -----------------------------------------------------------------


def test_archived_projects_are_read_only_until_restored(world: World) -> None:
    admin = world.browser("admin")
    url = f"/projects/{world.project.slug}/settings/"
    post(admin, url, {"action": "archive"})
    world.project.refresh_from_db()
    assert world.project.is_archived
    assert "archived and read-only" in text(admin.get(f"/projects/{world.project.slug}/"))
    assert world.project.name not in text(admin.get("/"))  # home shows active projects only

    refused = post(admin, url, {"name": "Renamed", "description": ""})
    assert "Restore the project before changing it." in follow(admin, refused)
    post(admin, url, {"action": "restore"})
    saved = post(admin, url, {"name": "Kinase Atlas 2", "description": "New"})
    assert path_of(saved) == f"/projects/{world.project.slug}/"  # the slug survives renames
    world.project.refresh_from_db()
    assert (world.project.name, world.project.is_archived) == ("Kinase Atlas 2", False)


def test_archived_teams_are_offered_to_no_one_for_new_projects(world: World) -> None:
    admin = world.browser("org_admin")
    post(admin, f"/teams/{world.team.slug}/settings/", {"action": "archive"})
    member = world.browser("team_member")
    assert "you’re not in a team yet" in text(member.get("/projects/new/"))
    response = post(member, "/projects/new/", {"team": str(world.team.pk), "name": "Late"})
    assert response.status_code == 200
    assert not Project.objects.filter(name="Late").exists()


# --- People and org roles ------------------------------------------------------


def test_people_page_lists_everyone_with_roles(world: World) -> None:
    page = text(world.browser("org_admin").get("/people/"))
    assert "compute_admin@example.com" in page
    assert "Compute Admin" in page
    assert "Deactivated" in page


def test_people_link_only_for_org_admins(world: World) -> None:
    assert 'href="/people/"' in text(world.browser("org_admin").get("/projects/"))
    assert 'href="/people/"' not in text(world.browser("admin").get("/projects/"))


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
    admin = world.browser("org_admin")
    post(admin, f"/projects/{world.project.slug}/join/")
    event = AuditEvent.objects.get(action="project.role_granted")
    assert event.actor == world.users["org_admin"]
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
