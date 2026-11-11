"""Signing in with the device flow, and keeping and renewing the sign-in."""

import json
import os
import stat
import sys
import webbrowser
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fake_iki import URL, FakeIki, MemoryKeychain, config, isolated, keychain, store_with

from iki import auth
from iki.errors import IkiError, NotSignedIn, SignInFailed

__all__ = ["config", "isolated", "keychain"]  # the fixtures, used here

# --- Addresses ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("https://iki.example.com", "https://iki.example.com"),
        ("https://IKI.example.com/", "https://iki.example.com"),
        ("iki.example.com", "https://iki.example.com"),
        ("https://iki.example.com:8443", "https://iki.example.com:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://iki.localhost", "http://iki.localhost"),
    ],
)
def test_addresses_are_kept_one_way(given: str, kept: str) -> None:
    assert auth.normalize_url(given) == kept


def test_plain_http_is_refused_except_on_this_machine() -> None:
    with pytest.raises(IkiError, match="plain HTTP, where your sign-in could be read"):
        auth.normalize_url("http://iki.example.com")


@pytest.mark.parametrize("given", ["https://iki.example.com/api/v1", "ftp://iki", "https://"])
def test_anything_but_an_address_is_refused(given: str) -> None:
    with pytest.raises(IkiError):
        auth.normalize_url(given)


# --- Where sign-ins are kept -------------------------------------------------------------------


def credentials(**changes: Any) -> auth.Credentials:
    base = {
        "url": URL,
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 4102444800.0,
        "scope": "read",
    }
    return auth.Credentials(**{**base, **changes})


def test_sign_ins_go_in_the_system_keychain(config: Path, keychain: MemoryKeychain) -> None:
    store = auth.Store(config)
    assert store.save(credentials()) == "your system keychain"
    assert json.loads(keychain.saved[("ikisci", URL)])["access_token"] == "a"
    assert not store.file.exists()
    assert store.load(URL) == credentials()
    store.delete(URL)
    assert store.load(URL) is None


def test_without_a_keychain_they_go_in_a_file_only_you_can_read(config: Path) -> None:
    store = auth.Store(config)
    assert store.save(credentials()) == str(config / "credentials.json")
    assert store.load(URL) == credentials()
    if os.name == "posix":
        assert stat.S_IMODE(store.file.stat().st_mode) == 0o600
        assert stat.S_IMODE(config.stat().st_mode) == 0o700
    store.save(credentials(url="https://other.example.com"))
    store.delete(URL)
    assert store.load(URL) is None
    assert store.load("https://other.example.com") is not None


def test_a_broken_keychain_falls_back_to_the_file(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Locked(MemoryKeychain):
        def set_password(self, service: str, user: str, password: str) -> None:
            raise RuntimeError("The keychain is locked")

    monkeypatch.setattr(auth, "_keyring", Locked)
    store = auth.Store(config)
    assert store.save(credentials()) == str(store.file)
    assert store.load(URL) == credentials()


def test_moving_to_the_keychain_forgets_the_file(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = auth.Store(config)
    store.save(credentials())
    found = MemoryKeychain()
    monkeypatch.setattr(auth, "_keyring", lambda: found)
    store.save(credentials(access_token="b"))
    assert json.loads(store.file.read_text()) == {}


def test_the_last_deployment_is_remembered(config: Path) -> None:
    assert auth.remembered_url(config) is None
    auth._remember_url(URL, config)
    assert auth.remembered_url(config) == URL


# --- The device flow ---------------------------------------------------------------------------


def started(**extra: Any) -> dict[str, Any]:
    return {
        "device_code": "device-1",
        "user_code": "WDJB-MJHT",
        "verification_uri": f"{URL}/oauth/device/",
        "expires_in": 600,
        "interval": 5,
        **extra,
    }


def oauth_error(error: str) -> httpx2.Response:
    return httpx2.Response(400, json={"error": error})


def login(fake: FakeIki, **kw: Any) -> tuple[auth.Credentials, list[str]]:
    said: list[str] = []
    with httpx2.Client(transport=httpx2.MockTransport(fake.handle)) as http:
        signed_in = auth.device_login(
            http,
            URL,
            scopes=kw.pop("scopes", auth.DEFAULT_SCOPES),
            open_browser=kw.pop("open_browser", False),
            say=said.append,
            sleep=fake.slept.append,
            **kw,
        )
    return signed_in, said


def test_it_signs_in_with_a_code_entered_in_the_browser() -> None:
    fake = FakeIki()
    fake.on("POST", "/oauth/device-authorization/", started())
    fake.on(
        "POST",
        "/oauth/token/",
        oauth_error("authorization_pending"),
        oauth_error("slow_down"),
        {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600, "scope": "read run"},
    )
    signed_in, said = login(fake)
    assert said == [f"To sign in to Iki, open {URL}/oauth/device/ and enter the code WDJB-MJHT"]
    asked = fake.sent("POST", "/oauth/device-authorization/")[0]
    assert asked.content == b"client_id=iki-cli&scope=read+write+run"
    polled = fake.sent("POST", "/oauth/token/")[0].content.decode()
    assert "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Adevice_code" in polled
    assert "device_code=device-1" in polled
    # It waits the interval, and longer once asked to slow down.
    assert fake.slept == [5, 5, 10]
    # What the person allowed in the browser, which can be less than was asked.
    assert (signed_in.access_token, signed_in.refresh_token, signed_in.scope) == (
        "a1",
        "r1",
        "read run",
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("access_denied", "declined in the browser"),
        ("expired_token", "expired before it was entered"),
        ("invalid_client", "Signing in failed: invalid_client"),
    ],
)
def test_it_says_why_signing_in_failed(error: str, message: str) -> None:
    fake = FakeIki()
    fake.on("POST", "/oauth/device-authorization/", started())
    fake.on("POST", "/oauth/token/", oauth_error(error))
    with pytest.raises(SignInFailed, match=message):
        login(fake)


def test_a_code_nobody_enters_expires() -> None:
    fake = FakeIki()
    fake.on("POST", "/oauth/device-authorization/", started(expires_in=12))
    fake.on("POST", "/oauth/token/", oauth_error("authorization_pending"))
    clock = iter(range(0, 1000, 5))
    with pytest.raises(SignInFailed, match="WDJB-MJHT expired"):
        login(fake, clock=lambda: float(next(clock)))


def test_something_other_than_iki_is_said_to_be_so() -> None:
    fake = FakeIki()
    with pytest.raises(SignInFailed, match="Is it an Iki deployment"):
        login(fake)


def test_an_iki_that_refuses_the_app_says_so() -> None:
    fake = FakeIki()
    fake.on("POST", "/oauth/device-authorization/", oauth_error("invalid_client"))
    with pytest.raises(SignInFailed, match="refused to start a sign-in: invalid_client"):
        login(fake)


def test_it_opens_the_browser_only_in_front_of_the_person(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    fake = FakeIki()
    fake.on(
        "POST",
        "/oauth/device-authorization/",
        started(verification_uri_complete=f"{URL}/oauth/device/?user_code=WDJB-MJHT"),
    )
    fake.on("POST", "/oauth/token/", {"access_token": "a", "expires_in": 60})

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    login(fake, open_browser=True)
    assert opened == [f"{URL}/oauth/device/?user_code=WDJB-MJHT"]

    # Not on a server you've connected to, where it would open there.
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 22 10.0.0.2 51000")
    login(fake, open_browser=True)
    # Nor on Linux without a display, where a text browser would take over the terminal.
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    login(fake, open_browser=True)
    assert len(opened) == 1


# --- Renewing ----------------------------------------------------------------------------------


def renewing(fake: FakeIki, store: auth.Store, signed_in: auth.Credentials) -> auth.Credentials:
    with httpx2.Client(transport=httpx2.MockTransport(fake.handle)) as http:
        return auth.renew(http, signed_in, store)


def test_renewing_replaces_the_refresh_token(config: Path) -> None:
    store = store_with(config)
    fake = FakeIki()
    fake.on(
        "POST",
        "/oauth/token/",
        {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600},
    )
    fresh = renewing(fake, store, store.load(URL))  # type: ignore[arg-type]
    assert (fresh.access_token, fresh.refresh_token) == ("access-2", "refresh-2")
    assert store.load(URL) == fresh
    sent = fake.sent("POST", "/oauth/token/")[0].content.decode()
    assert sent == "grant_type=refresh_token&refresh_token=refresh-1&client_id=iki-cli"


def test_a_sign_in_another_process_renewed_is_used_as_it_is(config: Path) -> None:
    """Refresh tokens work once: using the old one again would end the sign-in. So a
    process that finds the stored one renewed since it read it uses that."""
    store = store_with(config, access_token="access-1", expires_at=0.0)
    stale = store.load(URL)
    assert stale is not None
    store.save(
        auth.Credentials(
            url=URL,
            access_token="access-2",
            refresh_token="refresh-2",
            expires_at=4102444800.0,
            scope="read",
        )
    )
    fake = FakeIki()
    assert renewing(fake, store, stale).access_token == "access-2"
    assert fake.requests == []


def test_a_sign_in_that_was_ended_is_forgotten(config: Path) -> None:
    store = store_with(config)
    fake = FakeIki()
    fake.on("POST", "/oauth/token/", oauth_error("invalid_grant"))
    with pytest.raises(NotSignedIn, match=r"expired or was ended: run iki\.login"):
        renewing(fake, store, store.load(URL))  # type: ignore[arg-type]
    assert store.load(URL) is None


def test_signing_out_ends_both_tokens_at_the_server() -> None:
    fake = FakeIki()
    fake.on("POST", "/oauth/revoke/", httpx2.Response(200))
    with httpx2.Client(transport=httpx2.MockTransport(fake.handle)) as http:
        auth.revoke(http, credentials())
    assert [r.content for r in fake.sent("POST", "/oauth/revoke/")] == [
        b"token=r&token_type_hint=refresh_token&client_id=iki-cli",
        b"token=a&token_type_hint=access_token&client_id=iki-cli",
    ]
