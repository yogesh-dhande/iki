"""Users, sessions and their lifetime rules."""

import time
from datetime import timedelta
from io import StringIO

import pytest
from browser import Browser, complete_setup, sign_in
from django.core.management import call_command
from django.db import IntegrityError
from django.utils import timezone
from fake_idp import FakeIdP

from iki_server.accounts import session_policy
from iki_server.accounts.models import User, UserSession

pytestmark = pytest.mark.django_db

ADMIN = {"sub": "admin-1", "email": "ada@example.com", "name": "Ada Lovelace"}
USER = {"sub": "u-1", "email": "grace@example.com", "given_name": "Grace", "family_name": "Hopper"}


def test_email_addresses_are_unique_ignoring_case() -> None:
    User.objects.create_user("Grace@Example.com")
    assert User.objects.get(email="grace@EXAMPLE.com").email == "Grace@example.com"
    with pytest.raises(IntegrityError):
        User.objects.create_user("GRACE@example.com")


def test_there_are_no_local_passwords() -> None:
    user = User.objects.create_user("grace@example.com")
    assert not user.has_usable_password()
    with pytest.raises(NotImplementedError, match="setup_link"):
        User.objects.create_superuser("root@example.com", "hunter2")


def test_primary_keys_are_uuid7() -> None:
    user = User.objects.create_user("grace@example.com")
    assert user.pk.version == 7


def test_names_are_built_from_the_identity_providers_claims(
    client: Browser, fake_idp: FakeIdP
) -> None:
    complete_setup(client, fake_idp, ADMIN)
    sign_in(Browser(), fake_idp, USER)
    user = User.objects.get(email="grace@example.com")
    assert user.name == "Grace Hopper"
    assert user.get_short_name() == "Grace"


@pytest.fixture
def signed_in(client: Browser, fake_idp: FakeIdP) -> tuple[Browser, User]:
    complete_setup(client, fake_idp, ADMIN)
    browser = Browser()
    sign_in(browser, fake_idp, USER)
    return browser, User.objects.get(email="grace@example.com")


def test_sessions_are_linked_to_their_user(signed_in: tuple[Browser, User]) -> None:
    browser, user = signed_in
    key = browser.cookies["__Host-iki_session"].value
    assert UserSession.objects.get(session_key=key).user == user


def test_deactivating_a_user_ends_their_sessions_at_once(signed_in: tuple[Browser, User]) -> None:
    browser, user = signed_in
    assert browser.get("/").status_code == 200
    user.is_active = False
    user.save()
    assert not UserSession.objects.filter(user=user).exists()
    assert browser.get("/").status_code == 302


def test_sessions_end_after_their_absolute_lifetime(
    signed_in: tuple[Browser, User], settings: object
) -> None:
    browser, _ = signed_in
    session = browser.session
    session[session_policy.AUTHENTICATED_AT] = time.time() - 24 * 3600 - 1
    session.save()
    assert browser.get("/").status_code == 302


def test_activity_moves_the_idle_expiry_forward(signed_in: tuple[Browser, User]) -> None:
    browser, _ = signed_in
    key = browser.cookies["__Host-iki_session"].value
    soon = timezone.now() + timedelta(minutes=5)
    session = browser.session
    session[session_policy.LAST_SEEN] = time.time() - 120
    session.save()
    UserSession.objects.filter(session_key=key).update(expire_date=soon)
    assert browser.get("/").status_code == 200
    expiry = UserSession.objects.get(session_key=key).expire_date
    assert expiry > timezone.now() + timedelta(hours=7)


def test_idle_sessions_expire(signed_in: tuple[Browser, User]) -> None:
    browser, _ = signed_in
    key = browser.cookies["__Host-iki_session"].value
    UserSession.objects.filter(session_key=key).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )
    assert browser.get("/").status_code == 302


def test_sessions_started_before_sign_in_are_short_lived(
    client: Browser, fake_idp: FakeIdP
) -> None:
    from iki_server.accounts.models import IdentityProvider

    complete_setup(client, fake_idp, ADMIN)
    browser = Browser()
    url = f"/accounts/oidc/{IdentityProvider.objects.get().provider_id}/login/"
    assert browser.submit("/login/", action=url).status_code == 302
    key = browser.cookies["__Host-iki_session"].value
    expiry = UserSession.objects.get(session_key=key).expire_date
    assert expiry <= timezone.now() + timedelta(seconds=session_policy.ANONYMOUS_SESSION_SECONDS)


def test_clear_expired_sessions(signed_in: tuple[Browser, User]) -> None:
    UserSession.objects.update(expire_date=timezone.now() - timedelta(seconds=1))
    out = StringIO()
    call_command("clear_expired_sessions", stdout=out)
    assert not UserSession.objects.exists()
    assert "Deleted" in out.getvalue()


def test_signed_out_page_redirects_signed_in_users_home(signed_in: tuple[Browser, User]) -> None:
    browser, _ = signed_in
    assert browser.get("/signed-out/")["Location"] == "/"
    assert browser.get("/login/?next=/setup/done/")["Location"] == "/setup/done/"


def test_maintenance_clears_expired_sessions_and_old_rate_limit_windows(
    signed_in: tuple[Browser, User],
) -> None:
    from iki_server.core.models import RateLimitCounter
    from iki_server.web.ratelimit import RateLimiter

    expired = UserSession.objects.update(expire_date=timezone.now() - timedelta(seconds=1))
    assert expired
    limiter = RateLimiter("test", limit_per_minute=5)
    limiter.check("1.2.3.4")
    RateLimitCounter.objects.create(
        key="test:old", window_start=timezone.now() - timedelta(hours=2), count=3
    )
    out = StringIO()
    call_command("maintain", stdout=out)
    assert f"Deleted {expired} expired session(s), 1 old rate-limit window(s)," in out.getvalue()
    assert not UserSession.objects.exists()
    keys = set(RateLimitCounter.objects.values_list("key", flat=True))
    assert "test:1.2.3.4" in keys and "test:old" not in keys
