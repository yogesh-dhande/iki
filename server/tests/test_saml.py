"""SAML 2.0 sign-in, end to end against a fake identity provider that signs real responses."""

import re
from datetime import timedelta
from typing import Any

import pytest
from browser import (
    Browser,
    Response,
    complete_saml_setup,
    path_of,
    post_saml_response,
    saml_sign_in,
    saml_start,
    setup_link_token,
)
from fake_idp import FakeIdP
from fake_saml_idp import EMAIL_FORMAT, AuthnRequest, FakeSamlIdP, make_key_and_certificate
from onelogin.saml2.constants import OneLogin_Saml2_Constants

from iki_server.accounts.models import IdentityProvider, Protocol, User
from iki_server.audit.models import AuditEvent
from iki_server.org.models import OrgRole, OrgRoleAssignment

pytestmark = pytest.mark.django_db

#: Attribute names as Microsoft Entra ID sends them.
ENTRA = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/"
ADA = {
    f"{ENTRA}emailaddress": "ada@example.com",
    f"{ENTRA}givenname": "Ada",
    f"{ENTRA}surname": "Lovelace",
}


@pytest.fixture
def set_up(client: Browser, saml_idp: FakeSamlIdP) -> FakeSamlIdP:
    response = complete_saml_setup(client, saml_idp, "ada-1", ADA)
    assert path_of(response) == "/setup/done/"
    return saml_idp


def start_sign_in(browser: Browser) -> tuple[Browser, AuthnRequest]:
    page = browser.get("/login/")
    action = re.search(r'<form method="post" action="([^"]+)"', page.content.decode())
    assert action
    request = saml_start(browser, browser.submit("/login/", {"next": "/"}, action=action.group(1)))
    return browser, request


# --- Setup -------------------------------------------------------------------------


def test_wizard_sets_up_saml_and_pins_the_signing_certificate(
    client: Browser, saml_idp: FakeSamlIdP
) -> None:
    response = complete_saml_setup(client, saml_idp, "ada-1", ADA)
    assert path_of(response) == "/setup/done/"
    provider = IdentityProvider.objects.get()
    assert provider.protocol == Protocol.SAML
    assert provider.is_enabled
    assert provider.issuer == saml_idp.entity_id
    assert provider.authorization_endpoint == saml_idp.sso_url
    assert provider.saml_metadata_url == saml_idp.metadata_url
    assert provider.saml_certificate.startswith("-----BEGIN CERTIFICATE-----")
    assert provider.saml_certificate_expires_at is not None
    assert (provider.client_id, provider.client_secret_encrypted) == ("", "")
    admin = User.objects.get()
    assert (admin.email, admin.name) == ("ada@example.com", "Ada Lovelace")
    assert OrgRoleAssignment.objects.filter(user=admin, role=OrgRole.ORG_ADMIN).exists()
    enabled = AuditEvent.objects.get(action="identity_provider.enabled")
    assert enabled.data["protocol"] == "saml"


def test_wizard_accepts_pasted_metadata(client: Browser, saml_idp: FakeSamlIdP) -> None:
    response = complete_saml_setup(
        client, saml_idp, "ada-1", ADA, metadata={"metadata_xml": saml_idp.metadata()}
    )
    assert path_of(response) == "/setup/done/"
    assert IdentityProvider.objects.get().saml_metadata_url == ""


def test_wizard_shows_what_to_enter_at_the_provider(client: Browser, saml_idp: FakeSamlIdP) -> None:
    client.submit("/setup/", {"token": setup_link_token()})
    page = client.get("/setup/identity-provider/?protocol=saml").content.decode()
    assert "https://testserver/accounts/saml/sso/acs/" in page
    assert "https://testserver/accounts/saml/sso/metadata/" in page
    assert "Sign the <strong>assertion</strong>" in page


def _metadata_error(client: Browser, data: dict[str, str]) -> str:
    client.submit("/setup/", {"token": setup_link_token()})
    response = client.submit(
        "/setup/identity-provider/?protocol=saml",
        {"protocol": "saml", "display_name": "Test SAML", **data},
    )
    assert response.status_code == 400
    assert not IdentityProvider.objects.exists()
    errors = re.findall(r'class="field-error"[^>]*>([^<]+)', response.content.decode())
    return " ".join(errors)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda xml, idp: "<nope", "valid SAML metadata XML"),
        (
            lambda xml, idp: re.sub(
                r"<md:KeyDescriptor.*?</md:KeyDescriptor>", "", xml, flags=re.S
            ),
            "no signing certificate",
        ),
        (lambda xml, idp: xml.replace("HTTP-Redirect", "HTTP-POST"), "HTTP-Redirect binding"),
        (lambda xml, idp: idp.metadata(make_key_and_certificate(days=-1)[1]), "expired"),
        (lambda xml, idp: idp.metadata(make_key_and_certificate(bits=1024)[1]), "RSA-1024"),
    ],
    ids=["not-xml", "no-certificate", "no-redirect-binding", "expired", "weak-key"],
)
def test_unusable_metadata_is_explained_and_nothing_is_saved(
    client: Browser, saml_idp: FakeSamlIdP, change: object, message: str
) -> None:
    xml = change(saml_idp.metadata(), saml_idp)  # type: ignore[operator]
    assert message in _metadata_error(client, {"metadata_xml": xml})


def test_metadata_url_problems_are_explained(client: Browser, saml_idp: FakeSamlIdP) -> None:
    error = _metadata_error(client, {"metadata_url": f"{saml_idp.entity_id}/missing"})
    assert "returned HTTP 404" in error


def test_metadata_needs_exactly_one_source(client: Browser, saml_idp: FakeSamlIdP) -> None:
    assert "Enter the metadata URL" in _metadata_error(client, {})
    both = {"metadata_url": saml_idp.metadata_url, "metadata_xml": saml_idp.metadata()}
    assert "not both" in _metadata_error(client, both)


def test_oidc_and_saml_providers_may_share_an_identifier(
    client: Browser, saml_idp: FakeSamlIdP, fake_idp: FakeIdP
) -> None:
    # Keycloak's realm URL is both its OIDC issuer and its SAML entity ID.
    IdentityProvider.objects.create(
        protocol=Protocol.OIDC,
        issuer=saml_idp.entity_id,
        display_name="Keycloak OIDC",
        authorization_endpoint=saml_idp.sso_url,
    )
    response = complete_saml_setup(client, saml_idp, "ada-1", ADA)
    assert path_of(response) == "/setup/done/"
    assert IdentityProvider.objects.filter(issuer=saml_idp.entity_id).count() == 2


# --- Signing in ----------------------------------------------------------------------


def test_people_sign_in_with_saml(set_up: FakeSamlIdP) -> None:
    browser = Browser()
    login = browser.get("/login/")
    page = login.content.decode()
    assert "Continue with Test SAML" in page
    assert 'action="/accounts/saml/sso/login/"' in page
    assert set_up.base in login["Content-Security-Policy"]
    response = saml_sign_in(
        browser,
        set_up,
        "rosalind-1",
        {"email": "rosalind@example.com", "displayName": "Rosalind Franklin"},
    )
    assert path_of(response) == "/"
    assert "Welcome, Rosalind" in browser.get("/").content.decode()
    user = User.objects.get(email="rosalind@example.com")
    assert user.name == "Rosalind Franklin"
    assert OrgRoleAssignment.objects.filter(user=user).count() == 0


def test_the_email_can_come_from_an_email_format_name_id(set_up: FakeSamlIdP) -> None:
    browser, request = start_sign_in(Browser())
    response = post_saml_response(
        browser,
        set_up.respond(request, name_id="grace@example.com", name_id_format=EMAIL_FORMAT),
        request.relay_state,
    )
    assert path_of(response) == "/"
    assert User.objects.filter(email="grace@example.com").exists()


def test_the_same_subject_signs_into_the_same_account_and_profile_follows(
    set_up: FakeSamlIdP,
) -> None:
    ada = User.objects.get()
    browser = Browser()
    renamed = {**ADA, f"{ENTRA}emailaddress": "ada.king@example.com", f"{ENTRA}surname": "King"}
    assert path_of(saml_sign_in(browser, set_up, "ada-1", renamed)) == "/"
    ada.refresh_from_db()
    assert (ada.email, ada.name) == ("ada.king@example.com", "Ada King")
    assert User.objects.count() == 1


def test_no_email_is_refused(set_up: FakeSamlIdP) -> None:
    response = saml_sign_in(Browser(), set_up, "anon-1", {"displayName": "Nobody"})
    assert response.status_code == 403
    assert "didn’t share your email address" in response.content.decode()


def _refused(browser: Browser, response: Response) -> None:
    assert response.status_code != 302 or path_of(response) != "/"
    assert path_of(browser.get("/")) == "/login/"


@pytest.mark.parametrize(
    "knobs",
    [
        {"sign_assertion": False},
        {"sign_assertion": False, "sign_response": True},
        {"key": make_key_and_certificate()[0]},
        {"tamper": True},
        {"audience": "https://elsewhere.example.com/saml"},
        {"expires_in": timedelta(minutes=-10)},
        {"digest": OneLogin_Saml2_Constants.SHA1},
    ],
    ids=[
        "unsigned",
        "only-the-response-signed",
        "signed-by-another-key",
        "tampered-after-signing",
        "for-another-audience",
        "expired",
        "sha1-digest",
    ],
)
def test_untrustworthy_responses_are_refused(set_up: FakeSamlIdP, knobs: dict[str, Any]) -> None:
    browser, request = start_sign_in(Browser())
    response = post_saml_response(
        browser,
        set_up.respond(
            request, name_id="mallory-1", attributes={"email": "eve@example.com"}, **knobs
        ),
        request.relay_state,
    )
    _refused(browser, response)
    assert not User.objects.filter(email__in=["eve@example.com", "mallory@example.com"]).exists()
    # A forged or broken response is logged, not audited: anyone can post one.
    assert not AuditEvent.objects.filter(action="user.sign_in_refused").exists()


def test_identity_provider_initiated_sign_in_is_refused(set_up: FakeSamlIdP) -> None:
    browser = Browser()
    response = post_saml_response(
        browser, set_up.respond(None, name_id="ada-1", attributes={"email": "ada@example.com"})
    )
    _refused(browser, response)


def test_a_response_cant_be_replayed(set_up: FakeSamlIdP) -> None:
    browser, request = start_sign_in(Browser())
    saml_response = set_up.respond(request, name_id="ada-1", attributes=ADA)
    assert path_of(post_saml_response(browser, saml_response, request.relay_state)) == "/"
    thief = Browser()
    _refused(thief, post_saml_response(thief, saml_response, request.relay_state))


def test_a_response_only_works_in_the_browser_that_started_signing_in(
    set_up: FakeSamlIdP,
) -> None:
    _, request = start_sign_in(Browser())
    other = Browser()
    response = post_saml_response(
        other,
        set_up.respond(request, name_id="ada-1", attributes=ADA),
        request.relay_state,
    )
    _refused(other, response)


def test_service_provider_metadata_is_published(set_up: FakeSamlIdP) -> None:
    response = Browser().get("/accounts/saml/sso/metadata/")
    assert response.status_code == 200
    body = response.content.decode()
    assert 'entityID="https://testserver/accounts/saml/sso/metadata/"' in body
    assert 'Location="https://testserver/accounts/saml/sso/acs/"' in body


def test_single_logout_isnt_offered(set_up: FakeSamlIdP) -> None:
    assert Browser().get("/accounts/saml/sso/sls/").status_code == 404


def test_saml_endpoints_404_without_a_saml_provider(client: Browser) -> None:
    assert client.get("/accounts/saml/sso/metadata/").status_code == 404


def test_posting_responses_is_rate_limited(set_up: FakeSamlIdP) -> None:
    from iki_server.accounts.views import acs_limiter

    acs_limiter.reset()
    browser = Browser()
    statuses = [
        browser.post("/accounts/saml/sso/acs/", {"SAMLResponse": "x"}).status_code
        for _ in range(21)
    ]
    assert statuses[:20] == [302] * 20
    assert statuses[20] == 429


def test_recovery_can_switch_to_saml_and_back(
    client: Browser, saml_idp: FakeSamlIdP, fake_idp: FakeIdP
) -> None:
    from browser import complete_setup

    complete_setup(client, fake_idp, {"sub": "ada-oidc", "email": "ada@example.com", "name": "Ada"})
    oidc = IdentityProvider.objects.get()
    # Someone with no Iki account yet completes recovery through the new SAML provider.
    response = complete_saml_setup(
        Browser(), saml_idp, "grace-1", {"email": "grace@example.com"}, recover=True
    )
    assert path_of(response) == "/setup/done/"
    oidc.refresh_from_db()
    assert not oidc.is_enabled
    assert IdentityProvider.objects.get(is_enabled=True).protocol == Protocol.SAML
