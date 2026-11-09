"""The first-run wizard, end to end against a fake OpenID Connect provider."""

from datetime import timedelta

import pytest
from browser import Browser, complete_setup, path_of, setup_link_token, sign_in, sign_in_at_idp
from django.core.management import CommandError, call_command
from django.utils import timezone
from fake_idp import FakeIdP

from iki_server.accounts.models import IdentityProvider, User
from iki_server.setup.models import Installation, SetupToken

pytestmark = pytest.mark.django_db

ADMIN = {"sub": "admin-1", "email": "Ada@Example.com", "name": "Ada Lovelace"}
SCIENTIST = {"sub": "sci-1", "email": "rosalind@example.com", "name": "Rosalind Franklin"}


def test_fresh_install_sends_everyone_to_setup(client: Browser) -> None:
    assert path_of(client.get("/")) == "/setup/"
    login = client.get("/login/")
    assert login.status_code == 503
    assert "isn’t set up yet" in login.content.decode()


def test_wizard_configures_sso_and_makes_the_first_person_to_sign_in_admin(
    client: Browser, fake_idp: FakeIdP
) -> None:
    response = complete_setup(client, fake_idp, ADMIN)
    assert path_of(response) == "/setup/done/"
    assert "Iki is ready" in client.get("/setup/done/").content.decode()

    installation = Installation.get()
    assert installation.is_set_up
    assert installation.organization_name == "Example Bio"
    provider = IdentityProvider.objects.get()
    assert provider.is_enabled
    assert provider.client_secret == fake_idp.client_secret
    assert fake_idp.client_secret not in provider.client_secret_encrypted
    admin = User.objects.get()
    # The domain is normalized to lower case; the local part keeps its case.
    assert (admin.email, admin.name) == ("Ada@example.com", "Ada Lovelace")
    assert admin.is_superuser and admin.is_staff
    assert not admin.has_usable_password()
    # The setup link is spent and the grant is gone.
    assert SetupToken.objects.get().revoked_at is not None
    assert path_of(client.get("/setup/organization/")) == "/setup/"
    # PKCE: the token request carried a verifier the provider checked.
    assert fake_idp.token_requests[-1]["code_verifier"]


def test_people_sign_in_after_setup_without_becoming_admins(
    client: Browser, fake_idp: FakeIdP
) -> None:
    complete_setup(client, fake_idp, ADMIN)
    other = Browser()
    assert path_of(other.get("/")) == "/login/"
    page = other.get("/login/").content.decode()
    assert "Sign in to Example Bio" in page
    assert "Continue with Test SSO" in page
    callback = sign_in(other, fake_idp, SCIENTIST)
    assert path_of(callback) == "/"
    home = other.get("/")
    assert home.status_code == 200
    assert "Welcome, Rosalind" in home.content.decode()
    user = User.objects.get(email="rosalind@example.com")
    assert not user.is_superuser and not user.is_staff


def test_setup_link_works_once(client: Browser, fake_idp: FakeIdP) -> None:
    token = setup_link_token()
    assert client.submit("/setup/", {"token": token}).status_code == 302
    other = Browser()
    response = other.submit("/setup/", {"token": token})
    assert response.status_code == 400
    assert "invalid, expired or already used" in response.content.decode()


def test_expired_setup_link_is_rejected(client: Browser) -> None:
    token = setup_link_token()
    SetupToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.submit("/setup/", {"token": token}).status_code == 400


def test_a_new_setup_link_ends_earlier_wizard_sessions(client: Browser) -> None:
    client.submit("/setup/", {"token": setup_link_token()})
    assert client.get("/setup/organization/").status_code == 200
    setup_link_token()
    assert path_of(client.get("/setup/organization/")) == "/setup/"


def test_setup_grant_expires(client: Browser) -> None:
    client.submit("/setup/", {"token": setup_link_token()})
    SetupToken.objects.update(exchanged_at=timezone.now() - timedelta(minutes=61))
    assert path_of(client.get("/setup/organization/")) == "/setup/"


def test_wizard_pages_need_a_grant(client: Browser) -> None:
    for page in ("organization", "identity-provider", "verify", "complete"):
        assert path_of(client.get(f"/setup/{page}/")) == "/setup/"


def test_unfinished_provider_is_only_usable_by_the_wizard(
    client: Browser, fake_idp: FakeIdP
) -> None:
    client.submit("/setup/", {"token": setup_link_token()})
    client.submit("/setup/organization/", {"organization_name": "Example Bio"})
    client.submit(
        "/setup/identity-provider/",
        {
            "display_name": "Test SSO",
            "issuer": fake_idp.issuer,
            "client_id": fake_idp.client_id,
            "client_secret": fake_idp.client_secret,
        },
    )
    provider = IdentityProvider.objects.get()
    assert not provider.is_enabled
    stranger = Browser()
    response = stranger.submit("/setup/", action=f"/accounts/oidc/{provider.provider_id}/login/")
    assert response.status_code == 404


def test_finishing_needs_sign_in_through_the_new_provider(
    client: Browser, fake_idp: FakeIdP
) -> None:
    client.submit("/setup/", {"token": setup_link_token()})
    client.submit("/setup/organization/", {"organization_name": "Example Bio"})
    client.submit(
        "/setup/identity-provider/",
        {
            "display_name": "Test SSO",
            "issuer": fake_idp.issuer,
            "client_id": fake_idp.client_id,
            "client_secret": fake_idp.client_secret,
        },
    )
    assert path_of(client.get("/setup/complete/")) == "/setup/verify/"


def test_bad_issuer_is_reported_without_saving(client: Browser, fake_idp: FakeIdP) -> None:
    client.submit("/setup/", {"token": setup_link_token()})
    client.submit("/setup/organization/", {"organization_name": "Example Bio"})
    response = client.submit(
        "/setup/identity-provider/",
        {
            "display_name": "Test SSO",
            "issuer": fake_idp.issuer + "/wrong",
            "client_id": fake_idp.client_id,
            "client_secret": fake_idp.client_secret,
        },
    )
    assert response.status_code == 400
    assert "returned HTTP 404" in response.content.decode()
    assert not IdentityProvider.objects.exists()


def test_setup_link_refuses_once_set_up_unless_recovering(
    client: Browser, fake_idp: FakeIdP
) -> None:
    complete_setup(client, fake_idp, ADMIN)
    with pytest.raises(CommandError, match="already set up"):
        call_command("setup_link", "--url", "https://testserver")
    assert "already set up" in Browser().get("/setup/").content.decode()


def test_recovery_reconfigures_sign_in(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    old_secret = fake_idp.client_secret
    fake_idp.client_secret = "rotated-secret"
    operator = Browser()
    response = complete_setup(operator, fake_idp, SCIENTIST, recover=True)
    assert path_of(response) == "/setup/done/"
    provider = IdentityProvider.objects.get()
    assert provider.client_secret == "rotated-secret" != old_secret
    assert User.objects.get(email="rosalind@example.com").is_superuser


def test_setup_link_needs_a_base_url(settings: object) -> None:
    with pytest.raises(CommandError, match="IKI_PUBLIC_URL"):
        call_command("setup_link")


def test_callback_without_state_fails_safely(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    provider = IdentityProvider.objects.get()
    attacker = Browser()
    response = attacker.get(
        f"/accounts/oidc/{provider.provider_id}/login/callback/?code=stolen&state=forged"
    )
    assert response.status_code == 401
    assert "Couldn’t sign you in" in response.content.decode()
    assert attacker.get("/").status_code == 302


def test_signing_in_twice_reuses_the_account(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    first, second = Browser(), Browser()
    sign_in(first, fake_idp, SCIENTIST)
    sign_in(second, fake_idp, {**SCIENTIST, "name": "Rosalind E. Franklin"})
    user = User.objects.get(email="rosalind@example.com")
    assert user.name == "Rosalind E. Franklin"
    assert User.objects.count() == 2


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"sub": "x-1", "name": "No Email"}, "didn’t share your email address"),
        ({"sub": "x-2", "email": "ada@example.com"}, "already uses your email address"),
    ],
)
def test_sign_in_is_refused_when_the_identity_is_unusable(
    client: Browser, fake_idp: FakeIdP, claims: dict[str, str], reason: str
) -> None:
    complete_setup(client, fake_idp, ADMIN)
    other = Browser()
    response = sign_in(other, fake_idp, claims)
    assert response.status_code == 403
    assert reason in response.content.decode()
    assert other.get("/").status_code == 302


def test_deactivated_users_cannot_sign_in(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    sign_in(Browser(), fake_idp, SCIENTIST)
    User.objects.filter(email="rosalind@example.com").update(is_active=False)
    other = Browser()
    response = sign_in(other, fake_idp, SCIENTIST)
    assert "deactivated" in response.content.decode() or path_of(response) == "/accounts/inactive/"
    assert other.get("/").status_code == 302


def test_sign_in_uses_the_redirect_back_to_iki(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    other = Browser()
    page = other.get("/login/")
    action = page.content.decode().split('<form method="post" action="')[1].split('"')[0]
    start = other.submit("/login/", {"next": "https://evil.example/"}, action=action)
    callback = sign_in_at_idp(other, fake_idp, start, SCIENTIST)
    assert callback["Location"] == "/"
