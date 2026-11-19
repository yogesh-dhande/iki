"""How SCIM provisioning shapes sign-in, teams and the admin pages (ADR 0026)."""

import json
import re
from typing import Any
from uuid import UUID

import pytest
from allauth.socialaccount.models import SocialAccount
from browser import Browser, complete_saml_setup, complete_setup, path_of, saml_sign_in, sign_in
from django.test import Client
from fake_idp import FakeIdP
from fake_saml_idp import FakeSamlIdP
from org_world import post

from iki_server.accounts import linking
from iki_server.accounts.models import IdentityProvider, Protocol, User, UserSession
from iki_server.audit.models import AuditEvent
from iki_server.org import services as org_services
from iki_server.org.models import Team, TeamMembership
from iki_server.scim import tokens
from iki_server.scim.models import ScimGroup

pytestmark = pytest.mark.django_db

ADMIN = {"sub": "admin-1", "email": "ada@example.com", "name": "Ada Lovelace"}
#: A sign-in whose address the provider verified, as Okta and Keycloak say of one.
ROSALIND = {"sub": "r-1", "email": "rosalind@example.com", "email_verified": True}


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


def placeholder(email: str) -> User:
    """Someone a bundle from another deployment named, whom nobody here was (ADR 0096)."""
    made = User(email=email, name="Rosalind Franklin", is_active=False, imported_from="lab-a")
    made.set_unusable_password()
    made.save()
    return made


def test_signing_in_takes_over_ones_placeholder(admin_browser: Browser, fake_idp: FakeIdP) -> None:
    stood_in = placeholder("rosalind@example.com")
    browser = Browser()
    assert path_of(sign_in(browser, fake_idp, ROSALIND)) == "/"
    assert str(browser.session["_auth_user_id"]) == str(stood_in.pk)
    stood_in.refresh_from_db()
    assert stood_in.is_active and not stood_in.is_placeholder
    claimed = AuditEvent.objects.get(action="user.placeholder_claimed")
    assert claimed.data == {"from": "lab-a"}
    assert User.objects.filter(email="rosalind@example.com").count() == 1


def test_provisioning_takes_over_a_placeholder(token: str, fake_idp: FakeIdP) -> None:
    stood_in = placeholder("rosalind@example.com")
    assert provision(token, "rosalind@example.com", "Rosalind Franklin") == str(stood_in.pk)
    stood_in.refresh_from_db()
    assert stood_in.is_active and stood_in.is_provisioned and not stood_in.is_placeholder
    browser = Browser()
    assert path_of(sign_in(browser, fake_idp, ROSALIND)) == "/"
    assert str(browser.session["_auth_user_id"]) == str(stood_in.pk)


def test_a_placeholder_isnt_activated_by_hand(admin_browser: Browser, admin: User) -> None:
    stood_in = placeholder("rosalind@example.com")
    with pytest.raises(org_services.Refused, match="stands for someone from lab-a"):
        org_services.set_active(stood_in, True, by=admin)
    stood_in.refresh_from_db()
    assert not stood_in.is_active


def test_while_scim_is_on_a_placeholder_isnt_taken_over_by_signing_in(
    token: str, fake_idp: FakeIdP
) -> None:
    stood_in = placeholder("rosalind@example.com")
    response = sign_in(Browser(), fake_idp, ROSALIND)
    refused(response, "haven’t been given access yet")
    stood_in.refresh_from_db()
    assert stood_in.is_placeholder and not stood_in.is_active


def test_a_provisioned_persons_first_sign_in_links_to_their_account(
    token: str, fake_idp: FakeIdP
) -> None:
    user_id = provision(token, "rosalind@example.com", "Rosalind Franklin")
    browser = Browser()
    assert path_of(sign_in(browser, fake_idp, {**ROSALIND, "email": "Rosalind@example.com"})) == "/"
    assert User.objects.filter(email__iexact="rosalind@example.com").count() == 1
    assert str(browser.session["_auth_user_id"]) == user_id
    linked = AuditEvent.objects.get(action="user.identity_linked")
    assert linked.target_id == user_id
    assert linked.data["matched_by"] == "email"
    # From then on the subject identifier decides, as for anyone else, verified or not.
    again = Browser()
    assert path_of(sign_in(again, fake_idp, {"sub": "r-1", "email": "rosalind@example.com"})) == "/"
    assert str(again.session["_auth_user_id"]) == user_id


def test_linking_also_matches_the_scim_user_name(token: str, fake_idp: FakeIdP) -> None:
    user_id = provision(
        token, "rfranklin@corp.example.com", emails=[{"value": "rosalind@example.com"}]
    )
    assert User.objects.get(pk=user_id).email == "rosalind@example.com"
    browser = Browser()
    sign_in(browser, fake_idp, {**ROSALIND, "email": "rfranklin@corp.example.com"})
    assert str(browser.session["_auth_user_id"]) == user_id


def test_a_provisioned_account_links_to_only_one_identity(token: str, fake_idp: FakeIdP) -> None:
    provision(token, "rosalind@example.com")
    sign_in(Browser(), fake_idp, ROSALIND)
    response = sign_in(Browser(), fake_idp, {**ROSALIND, "sub": "impostor"})
    refused(response, "linked to a different identity")


def test_deactivated_provisioned_people_cant_sign_in(token: str, fake_idp: FakeIdP) -> None:
    user_id = provision(token, "rosalind@example.com", active=False)
    browser = Browser()
    sign_in(browser, fake_idp, ROSALIND)
    assert "_auth_user_id" not in browser.session
    assert not User.objects.get(pk=user_id).is_active


def test_provisioned_people_link_through_saml_by_its_subject(
    client: Browser, saml_idp: FakeSamlIdP
) -> None:
    complete_saml_setup(client, saml_idp, "ada-1", {"email": "ada@example.com"})
    token = tokens.create(by=User.objects.get())
    grace = provision(token, "grace@example.com", "Grace Hopper")
    alan = provision(token, "alan@example.com", "Alan Turing", externalId="alan-1")
    # A SAML assertion says nothing of whether its email attribute was verified.
    refused(
        saml_sign_in(Browser(), saml_idp, "grace-1", {"email": "grace@example.com"}),
        "didn’t confirm that your email address is yours",
    )
    # Its subject is what the provider vouches for: Entra and Okta send it as userName too,
    browser = Browser()
    response = saml_sign_in(browser, saml_idp, "grace@example.com", {"email": "grace@example.com"})
    assert path_of(response) == "/"
    assert str(browser.session["_auth_user_id"]) == grace
    # or as externalId, when it's the person's ID at the provider.
    browser = Browser()
    assert path_of(saml_sign_in(browser, saml_idp, "alan-1", {"email": "alan@example.com"})) == "/"
    assert str(browser.session["_auth_user_id"]) == alan
    matched = AuditEvent.objects.filter(action="user.identity_linked").order_by("occurred_at")
    assert [e.data["matched_by"] for e in matched] == ["user_name", "external_id"]
    refused(
        saml_sign_in(Browser(), saml_idp, "eve-1", {"email": "eve@example.com"}),
        "haven’t been given access yet",
    )


def test_saml_links_by_an_address_it_says_it_verified(
    client: Browser, saml_idp: FakeSamlIdP
) -> None:
    complete_saml_setup(client, saml_idp, "ada-1", {"email": "ada@example.com"})
    token = tokens.create(by=User.objects.get())
    grace = provision(token, "grace@example.com", "Grace Hopper")
    browser = Browser()
    attributes = {"email": "grace@example.com", "http://schemas.auth0.com/email_verified": "true"}
    assert path_of(saml_sign_in(browser, saml_idp, "auth0|grace", attributes)) == "/"
    assert str(browser.session["_auth_user_id"]) == grace


# --- Which account a first sign-in is ----------------------------------------------------


@pytest.mark.parametrize("verified", [False, "false", None], ids=["false", "string", "absent"])
def test_an_address_the_provider_didnt_verify_links_no_provisioned_account(
    token: str, fake_idp: FakeIdP, verified: object
) -> None:
    user_id = provision(token, "rosalind@example.com", "Rosalind Franklin")
    claims = {**ROSALIND, "sub": "someone-else", "email_verified": verified}
    response = sign_in(Browser(), fake_idp, claims)
    refused(response, "didn’t confirm that your email address is yours")
    assert not SocialAccount.objects.filter(user_id=user_id).exists()
    event = AuditEvent.objects.get(action="user.sign_in_refused")
    assert event.data == {"reason": "email_unverified", "email": "rosalind@example.com"}
    # Once the provider vouches for the address, the account links.
    browser = Browser()
    assert path_of(sign_in(browser, fake_idp, ROSALIND)) == "/"
    assert str(browser.session["_auth_user_id"]) == user_id


@pytest.mark.parametrize("verified", [False, None], ids=["false", "absent"])
def test_an_address_the_provider_didnt_verify_claims_no_placeholder(
    admin_browser: Browser, fake_idp: FakeIdP, verified: object
) -> None:
    stood_in = placeholder("rosalind@example.com")
    response = sign_in(Browser(), fake_idp, {**ROSALIND, "email_verified": verified})
    refused(response, "didn’t confirm that your email address is yours")
    stood_in.refresh_from_db()
    assert stood_in.is_placeholder and not stood_in.is_active
    assert not SocialAccount.objects.filter(user=stood_in).exists()


def test_the_providers_user_id_links_whatever_the_address(token: str, fake_idp: FakeIdP) -> None:
    # Okta sends its user ID as SCIM's externalId and as the sign-in's subject.
    user_id = provision(token, "rosalind@example.com", externalId="00u1rosalind")
    browser = Browser()
    claims = {"sub": "00u1rosalind", "email": "rosalind@example.com"}
    assert path_of(sign_in(browser, fake_idp, claims)) == "/"
    assert str(browser.session["_auth_user_id"]) == user_id
    linked = AuditEvent.objects.get(action="user.identity_linked")
    assert linked.data["matched_by"] == "external_id"


def test_an_id_two_accounts_share_links_nobody(token: str, fake_idp: FakeIdP) -> None:
    provision(token, "rosalind@example.com", externalId="shared")
    provision(token, "grace@example.com", externalId="shared")
    response = sign_in(Browser(), fake_idp, {**ROSALIND, "sub": "shared"})
    assert response.status_code == 403
    assert not SocialAccount.objects.filter(uid="shared").exists()


def test_recovery_links_the_operator_by_their_address(token: str, fake_idp: FakeIdP) -> None:
    # Whoever completes break-glass recovery proved cluster access, so their address links
    # them to the account the identity provider provisioned, verified or not.
    user_id = provision(token, "ops@example.com")
    browser = Browser()
    response = complete_setup(
        browser, fake_idp, {"sub": "ops-1", "email": "ops@example.com"}, recover=True
    )
    assert path_of(response) == "/setup/done/"
    assert User.objects.filter(email="ops@example.com").count() == 1
    assert SocialAccount.objects.get(uid="ops-1").user_id == UUID(user_id)


ENTRA_TENANT = "6f1c2b9e-0d6a-4c3b-8e5f-1a2d9c0b7e6f"


def entra(protocol: str = Protocol.OIDC) -> IdentityProvider:
    issuer = (
        f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0"
        if protocol == Protocol.OIDC
        else f"https://sts.windows.net/{ENTRA_TENANT}/"
    )
    return IdentityProvider(issuer=issuer, protocol=protocol)


def test_entra_ids_tenant_comes_from_its_issuer() -> None:
    assert linking.entra_tenant(f"https://login.microsoftonline.com/{ENTRA_TENANT}/v2.0") == (
        ENTRA_TENANT
    )
    assert linking.entra_tenant(f"https://sts.windows.net/{ENTRA_TENANT}/") == ENTRA_TENANT
    assert linking.entra_tenant("https://login.microsoftonline.com/common/v2.0") is None
    assert linking.entra_tenant(f"https://evil.example/{ENTRA_TENANT}/v2.0") is None
    assert linking.entra_tenant("https://acme.okta.com") is None


def test_entra_id_links_by_its_object_id_in_the_issuers_tenant() -> None:
    oid = "1c0a4d3e-5b6f-4a7b-8c9d-0e1f2a3b4c5d"
    # Entra's sub is different for each app; its object ID is the person's, as SCIM sends it.
    claims = {"sub": "pairwise-sub", "oid": oid, "tid": ENTRA_TENANT, "email": "r@example.com"}
    vouched = linking.from_oidc(claims, entra())
    assert vouched.ids == {"pairwise-sub", oid}
    assert not vouched.email_verified
    other_tenant = linking.from_oidc({**claims, "tid": "0" * 8 + ENTRA_TENANT[8:]}, entra())
    assert oid not in other_tenant.ids
    okta = IdentityProvider(issuer="https://acme.okta.com", protocol=Protocol.OIDC)
    assert oid not in linking.from_oidc(claims, okta).ids


def test_entra_ids_xms_edov_vouches_for_its_address_only_from_entra() -> None:
    claims = {"sub": "s", "oid": "o", "tid": ENTRA_TENANT, "xms_edov": True}
    assert linking.from_oidc(claims, entra()).email_verified
    okta = IdentityProvider(issuer="https://acme.okta.com", protocol=Protocol.OIDC)
    assert not linking.from_oidc(claims, okta).email_verified
    assert linking.from_oidc({"sub": "s", "email_verified": "true"}, okta).email_verified


def test_entra_id_links_by_its_object_id_over_saml() -> None:
    oid = "1c0a4d3e-5b6f-4a7b-8c9d-0e1f2a3b4c5d"
    data = {"object_id": oid, "tenant_id": ENTRA_TENANT}
    vouched = linking.from_saml(
        "rosalind@corp.example", data, "rosalind@example.com", entra("saml")
    )
    assert vouched.ids == {"rosalind@corp.example", oid}
    assert vouched.user_name == "rosalind@corp.example"
    assert not vouched.email_verified  # the subject isn't the address
    assert linking.from_saml("r@example.com", {}, "R@example.com", entra("saml")).email_verified


def test_an_entra_object_id_finds_the_account_scim_made(token: str) -> None:
    oid = "1c0a4d3e-5b6f-4a7b-8c9d-0e1f2a3b4c5d"
    user_id = provision(token, "rosalind@example.com", externalId=oid)
    claims = {"sub": "pairwise-sub", "oid": oid.upper(), "tid": ENTRA_TENANT}
    link = linking.provisioned_account(linking.from_oidc(claims, entra()), "")
    assert link is not None
    assert (str(link.account.pk), link.matched_by) == (user_id, "external_id")


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
    assert 'href="/settings/sign-in/"' in admin_browser.get("/teams/").content.decode()
