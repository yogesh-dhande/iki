"""How SCIM provisioning shapes sign-in, teams and the admin pages (ADR 0026)."""

import json
import re
from typing import Any

import pytest
from browser import Browser, complete_saml_setup, complete_setup, path_of, saml_sign_in, sign_in
from django.test import Client
from fake_idp import FakeIdP
from fake_saml_idp import FakeSamlIdP
from org_world import post

from iki_server.accounts.models import User, UserSession
from iki_server.audit.models import AuditEvent
from iki_server.org import services as org_services
from iki_server.org.models import Team, TeamMembership
from iki_server.scim import tokens
from iki_server.scim.models import ScimGroup

pytestmark = pytest.mark.django_db

ADMIN = {"sub": "admin-1", "email": "ada@example.com", "name": "Ada Lovelace"}


def scim(token: str, method: str, path: str, body: Any = None) -> Any:
    response = Client().generic(
        method,
        "/scim/v2" + path,
        json.dumps(body) if body is not None else "",
        content_type="application/scim+json",
        secure=True,
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    return json.loads(response.content) if response.content else response.status_code


def provision(token: str, email: str, name: str = "", **extra: Any) -> str:
    body = {"userName": email, "emails": [{"value": email, "primary": True}], "displayName": name}
    result = scim(token, "POST", "/Users", {**body, **extra})
    return str(result["id"])


@pytest.fixture
def admin_browser(client: Browser, fake_idp: FakeIdP) -> Browser:
    complete_setup(client, fake_idp, ADMIN)
    return client


@pytest.fixture
def admin() -> User:
    return User.objects.get(email="ada@example.com")


@pytest.fixture
def token(admin_browser: Browser, admin: User) -> str:
    return tokens.create(by=admin)


def refused(response: Any, reason_text: str) -> None:
    assert response.status_code == 403, response.status_code
    assert reason_text in response.content.decode()


# --- Who may sign in ------------------------------------------------------------------


def test_while_scim_is_on_only_provisioned_people_get_accounts(
    token: str, fake_idp: FakeIdP
) -> None:
    response = sign_in(Browser(), fake_idp, {"sub": "x-1", "email": "eve@example.com"})
    refused(response, "haven’t been given access yet")
    assert not User.objects.filter(email="eve@example.com").exists()
    event = AuditEvent.objects.get(action="user.sign_in_refused")
    assert event.data == {"reason": "not_provisioned", "email": "eve@example.com"}


def test_people_who_had_accounts_before_scim_keep_signing_in(token: str, fake_idp: FakeIdP) -> None:
    assert path_of(sign_in(Browser(), fake_idp, ADMIN)) == "/"


def test_without_scim_people_get_accounts_on_first_sign_in(
    admin_browser: Browser, fake_idp: FakeIdP
) -> None:
    sign_in(Browser(), fake_idp, {"sub": "r-1", "email": "rosalind@example.com"})
    assert User.objects.filter(email="rosalind@example.com").exists()


def test_a_provisioned_persons_first_sign_in_links_to_their_account(
    token: str, fake_idp: FakeIdP
) -> None:
    user_id = provision(token, "rosalind@example.com", "Rosalind Franklin")
    browser = Browser()
    assert (
        path_of(sign_in(browser, fake_idp, {"sub": "r-1", "email": "Rosalind@example.com"})) == "/"
    )
    assert User.objects.filter(email__iexact="rosalind@example.com").count() == 1
    assert str(browser.session["_auth_user_id"]) == user_id
    linked = AuditEvent.objects.get(action="user.identity_linked")
    assert linked.target_id == user_id
    # From then on the subject identifier decides, as for anyone else.
    again = Browser()
    assert path_of(sign_in(again, fake_idp, {"sub": "r-1", "email": "rosalind@example.com"})) == "/"
    assert str(again.session["_auth_user_id"]) == user_id


def test_linking_also_matches_the_scim_user_name(token: str, fake_idp: FakeIdP) -> None:
    user_id = provision(
        token, "rfranklin@corp.example.com", emails=[{"value": "rosalind@example.com"}]
    )
    assert User.objects.get(pk=user_id).email == "rosalind@example.com"
    browser = Browser()
    sign_in(browser, fake_idp, {"sub": "r-1", "email": "rfranklin@corp.example.com"})
    assert str(browser.session["_auth_user_id"]) == user_id


def test_a_provisioned_account_links_to_only_one_identity(token: str, fake_idp: FakeIdP) -> None:
    provision(token, "rosalind@example.com")
    sign_in(Browser(), fake_idp, {"sub": "r-1", "email": "rosalind@example.com"})
    response = sign_in(Browser(), fake_idp, {"sub": "impostor", "email": "rosalind@example.com"})
    refused(response, "linked to a different identity")


def test_deactivated_provisioned_people_cant_sign_in(token: str, fake_idp: FakeIdP) -> None:
    user_id = provision(token, "rosalind@example.com", active=False)
    browser = Browser()
    sign_in(browser, fake_idp, {"sub": "r-1", "email": "rosalind@example.com"})
    assert "_auth_user_id" not in browser.session
    assert not User.objects.get(pk=user_id).is_active


def test_provisioned_people_link_through_saml_too(client: Browser, saml_idp: FakeSamlIdP) -> None:
    complete_saml_setup(client, saml_idp, "ada-1", {"email": "ada@example.com"})
    token = tokens.create(by=User.objects.get())
    user_id = provision(token, "grace@example.com", "Grace Hopper")
    browser = Browser()
    assert (
        path_of(saml_sign_in(browser, saml_idp, "grace-1", {"email": "grace@example.com"})) == "/"
    )
    assert str(browser.session["_auth_user_id"]) == user_id
    refused(
        saml_sign_in(Browser(), saml_idp, "eve-1", {"email": "eve@example.com"}),
        "haven’t been given access yet",
    )


def test_recovery_isnt_blocked_by_provisioning(token: str, fake_idp: FakeIdP) -> None:
    # Whoever completes break-glass recovery proves cluster access; they may be new.
    response = complete_setup(
        Browser(), fake_idp, {"sub": "ops-1", "email": "ops@example.com"}, recover=True
    )
    assert path_of(response) == "/setup/done/"


# --- Teams from groups ------------------------------------------------------------------


@pytest.fixture
def chemistry(token: str, admin: User) -> tuple[ScimGroup, Team, dict[str, str]]:
    people = {n: provision(token, f"{n}@example.com", n.title()) for n in ("rosalind", "dorothy")}
    group_id = scim(
        token,
        "POST",
        "/Groups",
        {"displayName": "Chemistry", "members": [{"value": people["rosalind"]}]},
    )["id"]
    team = org_services.create_team("Crystallography", "", by=admin)
    org_services.add_team_member(team, User.objects.get(pk=people["dorothy"]), by=admin)
    return ScimGroup.objects.get(pk=group_id), team, people


def members(team: Team) -> set[str]:
    return set(TeamMembership.objects.filter(team=team).values_list("user__email", flat=True))


def test_linking_a_group_shows_who_joins_and_leaves_then_decides_the_team(
    admin_browser: Browser, chemistry: tuple[ScimGroup, Team, dict[str, str]], token: str
) -> None:
    group, team, people = chemistry
    settings_page = admin_browser.get("/settings/sign-in/").content.decode()
    assert "Chemistry" in settings_page and "not linked" in settings_page
    review = admin_browser.get(f"/settings/sign-in/groups/{group.pk}/?team={team.pk}")
    page = review.content.decode()
    assert "Joining (1)" in page and "rosalind@example.com" in page
    assert "Leaving (1)" in page and "dorothy@example.com" in page
    assert members(team) == {"dorothy@example.com"}  # nothing changes until confirmed
    response = post(
        admin_browser,
        f"/settings/sign-in/groups/{group.pk}/",
        {"action": "link", "team": str(team.pk)},
    )
    assert path_of(response) == f"/teams/{team.slug}/"
    assert members(team) == {"rosalind@example.com"}
    linked = AuditEvent.objects.get(action="scim.group_linked")
    assert (linked.actor_label, linked.team_id) == ("ada@example.com", team.pk)
    removed = AuditEvent.objects.get(action="team.member_removed")
    assert removed.data == {"groups": ["Chemistry"]}

    # Group changes at the identity provider now reach the team.
    scim(
        token,
        "PATCH",
        f"/Groups/{group.pk}",
        {"Operations": [{"op": "add", "path": "members", "value": [{"value": people["dorothy"]}]}]},
    )
    assert members(team) == {"rosalind@example.com", "dorothy@example.com"}
    scim(
        token,
        "PATCH",
        f"/Groups/{group.pk}",
        {"Operations": [{"op": "remove", "path": f'members[value eq "{people["rosalind"]}"]'}]},
    )
    assert members(team) == {"dorothy@example.com"}


def test_a_linked_teams_members_cant_be_changed_by_hand(
    admin_browser: Browser, chemistry: tuple[ScimGroup, Team, dict[str, str]], admin: User
) -> None:
    group, team, people = chemistry
    from iki_server.scim import services

    services.link_group(group, team, by=admin)
    page = admin_browser.get(f"/teams/{team.slug}/").content.decode()
    assert "Members come from the identity provider group <strong>Chemistry</strong>" in page
    assert "Add a member" not in page
    assert ">Remove<" not in page
    with pytest.raises(org_services.Refused, match="come from the identity provider group"):
        org_services.add_team_member(team, admin, by=admin)
    response = post(admin_browser, f"/teams/{team.slug}/members/{people['rosalind']}/remove/")
    assert path_of(response) == f"/teams/{team.slug}/"
    assert "rosalind@example.com" in members(team)


def test_unlinking_keeps_members_and_hands_the_team_back(
    admin_browser: Browser, chemistry: tuple[ScimGroup, Team, dict[str, str]], admin: User
) -> None:
    group, team, _ = chemistry
    from iki_server.scim import services

    services.link_group(group, team, by=admin)
    response = post(admin_browser, f"/settings/sign-in/groups/{group.pk}/", {"action": "unlink"})
    assert path_of(response) == "/settings/sign-in/"
    assert members(team) == {"rosalind@example.com"}
    org_services.add_team_member(team, admin, by=admin)
    assert AuditEvent.objects.filter(action="scim.group_unlinked").exists()


def test_a_group_can_become_a_new_team(
    admin_browser: Browser, chemistry: tuple[ScimGroup, Team, dict[str, str]]
) -> None:
    group, _, _ = chemistry
    review = admin_browser.get(f"/settings/sign-in/groups/{group.pk}/?team=new")
    assert "Link Chemistry to a new team" in review.content.decode()
    post(admin_browser, f"/settings/sign-in/groups/{group.pk}/", {"action": "link", "team": "new"})
    team = Team.objects.get(name="Chemistry")
    assert members(team) == {"rosalind@example.com"}


def test_several_groups_can_decide_one_team(
    chemistry: tuple[ScimGroup, Team, dict[str, str]], token: str, admin: User
) -> None:
    group, team, people = chemistry
    from iki_server.scim import services

    other = services.create_group("Biology", "", [User.objects.get(pk=people["dorothy"]).pk])
    services.link_group(group, team, by=admin)
    services.link_group(other, team, by=admin)
    assert members(team) == {"rosalind@example.com", "dorothy@example.com"}
    scim(token, "DELETE", f"/Groups/{other.pk}")
    assert members(team) == {"rosalind@example.com"}
    scim(token, "DELETE", f"/Groups/{group.pk}")
    # The last group gone: the team keeps its members and is managed by hand again.
    assert members(team) == {"rosalind@example.com"}
    org_services.add_team_member(team, admin, by=admin)


def test_deprovisioning_removes_someone_from_linked_teams(
    chemistry: tuple[ScimGroup, Team, dict[str, str]], token: str, admin: User
) -> None:
    group, team, people = chemistry
    from iki_server.scim import services

    services.link_group(group, team, by=admin)
    assert scim(token, "DELETE", f"/Users/{people['rosalind']}") == 204
    assert members(team) == set()


# --- People ---------------------------------------------------------------------------


def test_provisioned_people_are_activated_only_by_the_identity_provider(
    admin_browser: Browser, token: str, admin: User
) -> None:
    user_id = provision(token, "rosalind@example.com", "Rosalind Franklin")
    page = admin_browser.get(f"/people/{user_id}/").content.decode()
    assert "over SCIM" in page
    assert "unassign Iki from" in page
    assert 'value="deactivate"' not in page
    with pytest.raises(org_services.Refused, match="managed by your identity provider"):
        org_services.set_active(User.objects.get(pk=user_id), False, by=admin)


def test_an_org_admin_can_sign_anyone_out_everywhere(
    admin_browser: Browser, token: str, admin: User
) -> None:
    user = User.objects.get(pk=provision(token, "rosalind@example.com", "Rosalind"))
    Client().force_login(user)
    Client().force_login(user)
    response = post(admin_browser, f"/people/{user.pk}/", {"action": "end_sessions"})
    assert path_of(response) == f"/people/{user.pk}/"
    assert not UserSession.objects.filter(user=user).exists()
    event = AuditEvent.objects.get(action="user.sessions_revoked", actor=admin)
    assert event.data == {"sessions": 2, "tokens_revoked": 0, "reason": "signed_out_by_admin"}
    assert user.is_active


# --- The settings page --------------------------------------------------------------------


def test_the_token_is_shown_once_and_replacing_it_revokes_the_old_one(
    admin_browser: Browser,
) -> None:
    page = admin_browser.get("/settings/sign-in/").content.decode()
    assert "Turn on provisioning" in page
    assert "https://testserver/scim/v2/" in page
    created = post(admin_browser, "/settings/sign-in/", {"action": "create_token"})
    assert created.status_code == 200
    first = re.search(r'id="scim-token">([^<]+)<', created.content.decode())
    assert first
    assert created["Cache-Control"].startswith("no-store") or "no-store" in created["Cache-Control"]
    later = admin_browser.get("/settings/sign-in/").content.decode()
    assert first.group(1) not in later
    assert f"…{first.group(1)[-4:]}" in later
    assert scim(first.group(1), "GET", "/Users")["totalResults"] == 0
    replaced = post(admin_browser, "/settings/sign-in/", {"action": "create_token"})
    second = re.search(r'id="scim-token">([^<]+)<', replaced.content.decode())
    assert second
    assert scim(first.group(1), "GET", "/Users")["status"] == "401"
    post(admin_browser, "/settings/sign-in/", {"action": "revoke_token"})
    assert not tokens.scim_enabled()
    actions = list(
        AuditEvent.objects.filter(action__startswith="scim.token").values_list("action", flat=True)
    )
    assert sorted(actions) == [
        "scim.token_created",
        "scim.token_created",
        "scim.token_revoked",
        "scim.token_revoked",
    ]


def test_settings_page_counts_people_not_yet_provisioned(
    admin_browser: Browser, token: str
) -> None:
    provision(token, "rosalind@example.com")
    page = admin_browser.get("/settings/sign-in/").content.decode()
    assert "1 provisioned, 1 active and not provisioned" in page


def test_settings_page_warns_before_the_saml_certificate_expires(
    client: Browser, saml_idp: FakeSamlIdP
) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from iki_server.accounts.models import IdentityProvider

    complete_saml_setup(client, saml_idp, "ada-1", {"email": "ada@example.com"})
    page = client.get("/settings/sign-in/").content.decode()
    assert "SAML 2.0" in page and "expires soon" not in page
    IdentityProvider.objects.update(saml_certificate_expires_at=timezone.now() + timedelta(days=10))
    assert "expires soon" in client.get("/settings/sign-in/").content.decode()


def test_the_nav_shows_sign_in_settings_to_org_admins(admin_browser: Browser) -> None:
    assert 'href="/settings/sign-in/"' in admin_browser.get("/").content.decode()
