"""Service accounts: what they are allowed to be, and the pages that manage them."""

import pytest
from api_client import Api, problem
from org_world import World, build_world, post

from iki_server.accounts import service_accounts
from iki_server.accounts.models import SERVICE_ACCOUNT_DOMAIN, Kind, User
from iki_server.audit.models import AuditEvent
from iki_server.org import permissions, services
from iki_server.org.models import OrgRole, ProjectRole
from iki_server.org.permissions import Action
from iki_server.scim import services as scim
from iki_server.scim.services import Profile, ScimError

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


def test_created_with_an_address_that_can_never_be_a_person(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("LIMS sync", "Copies plates from the LIMS.", by=admin)
    assert account.kind == Kind.SERVICE and account.is_service_account
    assert account.email == f"lims-sync@{SERVICE_ACCOUNT_DOMAIN}"
    assert not account.has_usable_password()
    second = service_accounts.create("LIMS  sync!", "", by=admin)
    assert second.email == f"lims-sync-2@{SERVICE_ACCOUNT_DOMAIN}"
    event = AuditEvent.objects.get(action="service_account.created", target_id=str(account.pk))
    assert event.actor == admin


def test_names_are_unique_ignoring_case(world: World) -> None:
    admin = world.users["org_admin"]
    service_accounts.create("Plate reader 3", "", by=admin)
    with pytest.raises(services.Refused, match="already a service account"):
        service_accounts.create("PLATE READER 3", "", by=admin)


def test_renaming_keeps_the_address_and_is_audited(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("Plate reader", "", by=admin)
    service_accounts.update(account, name="Plate reader 3", description="Lab 2", by=admin)
    account.refresh_from_db()
    assert account.name == "Plate reader 3"
    assert account.email.startswith("plate-reader@")
    event = AuditEvent.objects.get(action="service_account.updated")
    assert event.data == {"name": ["Plate reader", "Plate reader 3"], "description": ["", "Lab 2"]}


def test_can_hold_project_roles_and_some_org_roles(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("Pipeline", "", by=admin)
    services.grant_project_role(world.project, ProjectRole.RUNNER, user=account, by=admin)
    assert permissions.can(account, Action.RUN_CONTAINER, world.project)
    services.set_org_roles(account, {OrgRole.AUDITOR, OrgRole.COMPUTE_ADMIN}, by=admin)
    with pytest.raises(services.Refused, match="can’t be Org Admins"):
        services.set_org_roles(account, {OrgRole.ORG_ADMIN}, by=admin)
    with pytest.raises(services.Refused, match="can’t be Org Admins"):
        services.grant_org_role(account, OrgRole.ORG_ADMIN, by=admin)


def test_do_not_join_teams(world: World) -> None:
    account = service_accounts.create("Pipeline", "", by=world.users["org_admin"])
    with pytest.raises(services.Refused, match="don’t join teams"):
        services.add_team_member(world.team, account, by=world.users["org_admin"])


def test_deactivating_removes_all_access(world: World) -> None:
    admin = world.users["org_admin"]
    account = service_accounts.create("Pipeline", "", by=admin)
    services.grant_project_role(world.project, ProjectRole.EDITOR, user=account, by=admin)
    services.set_active(account, False, by=admin)
    account = User.objects.get(pk=account.pk)
    assert not permissions.can(account, Action.PROJECT_READ, world.project)


def test_scim_cannot_take_one_over(world: World) -> None:
    account = service_accounts.create("Pipeline", "", by=world.users["org_admin"])
    with pytest.raises(ScimError):
        scim.provision(Profile(user_name=account.email, email=account.email))
    account.refresh_from_db()
    assert not account.is_provisioned


def test_the_database_refuses_a_person_shaped_service_account(world: World) -> None:
    from django.db import IntegrityError, transaction

    with transaction.atomic(), pytest.raises(IntegrityError):
        User.objects.create(kind=Kind.SERVICE, email="robot@example.com", name="Robot")


# --- Pages ---------------------------------------------------------------------


def test_org_admins_create_and_manage_them_from_the_people_pages(world: World) -> None:
    browser = world.browser("org_admin")
    people = browser.get("/people/")
    assert b"Service accounts" in people.content
    created = post(browser, "/service-accounts/", {"name": "LIMS sync", "description": "x"})
    account = User.objects.get(kind=Kind.SERVICE)
    assert created["Location"] == f"/service-accounts/{account.pk}/"
    page = browser.get(created["Location"])
    assert account.email.encode() in page.content
    assert b"can\xe2\x80\x99t be Org Admins" in page.content
    assert b'value="org_admin"' not in page.content

    post(browser, created["Location"], {"action": "roles", "roles": "auditor"})
    assert set(account.org_roles.values_list("role", flat=True)) == {"auditor"}
    post(browser, created["Location"], {"action": "deactivate"})
    account.refresh_from_db()
    assert not account.is_active
    # Not listed among people, and the person page sends you to the right place.
    browser.get(created["Location"])  # shows the message
    assert account.email.encode() not in browser.get("/people/").content
    assert browser.get(f"/people/{account.pk}/")["Location"] == created["Location"]


def test_duplicate_names_are_shown_on_the_form(world: World) -> None:
    browser = world.browser("org_admin")
    post(browser, "/service-accounts/", {"name": "LIMS sync"})
    again = post(browser, "/service-accounts/", {"name": "lims SYNC"})
    assert again.status_code == 200
    assert b"already a service account" in again.content


def test_project_admins_add_them_by_address(world: World) -> None:
    account = service_accounts.create("Pipeline", "", by=world.users["org_admin"])
    api = Api(world.browser("admin"))
    [found] = api.get("/users", email=account.email).json()["items"]
    assert found["kind"] == "service"
    path = f"/projects/{world.project.pk}/members/users/{found['id']}"
    assert api.put(path, {"role": "runner"}).status_code == 200
    assert world.project.memberships.get(user=account).role == ProjectRole.RUNNER


# --- API -----------------------------------------------------------------------


def test_managed_over_the_api(world: World) -> None:
    api = Api(world.browser("org_admin"))
    created = api.post("/service-accounts", {"name": "LIMS sync", "description": "Plates"})
    assert created.status_code == 201
    body = created.json()
    assert body["kind"] == "service" and body["email"].endswith("@" + SERVICE_ACCOUNT_DOMAIN)
    assert body["created_by"]["email"] == "org_admin@example.com"
    assert body["org_roles"] == [] and body["active"] is True
    path = f"/service-accounts/{body['id']}"
    changed = api.patch(path, {"description": "Plates and tubes", "active": False}).json()
    assert changed["description"] == "Plates and tubes" and changed["active"] is False
    assert [a["name"] for a in api.get("/service-accounts").json()["items"]] == ["LIMS sync"]
    problem(api.post("/service-accounts", {"name": "lims sync"}), 409, "refused")
    roles = api.put(f"/users/{body['id']}/org-roles", {"roles": ["org_admin"]})
    problem(roles, 409, "refused")
    problem(api.get(f"/service-accounts/{world.users['viewer'].pk}"), 404, "not_found")


def test_failed_patches_change_nothing(world: World) -> None:
    admin = world.users["org_admin"]
    service_accounts.create("Taken", "", by=admin)
    account = service_accounts.create("Pipeline", "", by=admin)
    api = Api(world.browser("org_admin"))
    response = api.patch(f"/service-accounts/{account.pk}", {"name": "taken", "active": False})
    problem(response, 409, "refused")
    account.refresh_from_db()
    assert account.is_active and account.name == "Pipeline"
