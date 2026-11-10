"""Security headers, CSP, cookies and CSRF on server-rendered pages."""

import pytest
from browser import Browser, complete_setup, sign_in
from django.utils.csp import CSP
from fake_idp import FakeIdP

pytestmark = pytest.mark.django_db

ADMIN = {"sub": "admin-1", "email": "ada@example.com", "name": "Ada Lovelace"}


def csp_directives(header: str) -> dict[str, list[str]]:
    directives = {}
    for part in header.split(";"):
        name, *values = part.split()
        directives[name] = values
    return directives


def test_pages_get_a_strict_content_security_policy(client: Browser) -> None:
    csp = csp_directives(client.get("/setup/")[CSP.HEADER_ENFORCE])
    assert csp["default-src"] == ["'none'"]
    assert csp["script-src"] == ["'self'"]
    assert csp["style-src"] == ["'self'"]
    assert csp["connect-src"] == ["'self'"]
    assert csp["img-src"] == ["'self'", "data:", "blob:"]
    assert csp["form-action"] == ["'self'"]
    assert csp["frame-ancestors"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    assert csp["object-src"] == ["'none'"]
    assert csp["require-trusted-types-for"] == ["'script'"]
    assert csp["trusted-types"] == ["'none'"]
    assert "upgrade-insecure-requests" in csp
    assert csp["report-to"] == ["csp"]
    for values in csp.values():
        assert "'unsafe-inline'" not in values
        assert "'unsafe-eval'" not in values


def test_sign_in_page_lets_its_form_reach_only_the_identity_provider(
    client: Browser, fake_idp: FakeIdP
) -> None:
    complete_setup(client, fake_idp, ADMIN)
    csp = csp_directives(Browser().get("/login/")[CSP.HEADER_ENFORCE])
    assert csp["form-action"] == ["'self'", fake_idp.base]


def test_templates_have_no_inline_scripts_styles_or_handlers(client: Browser) -> None:
    from pathlib import Path

    import iki_server.web

    templates = Path(iki_server.web.__file__).parent / "templates"
    for template in templates.rglob("*.html"):
        if template.name.startswith("email"):
            # Emails aren't pages: no CSP applies, and mail clients need inline styles.
            assert "<script" not in template.read_text(), template
            continue
        text = template.read_text()
        scripts = text.replace('<script type="module" src=', "").replace("<script src=", "")
        assert "<script" not in scripts, template
        assert "<style" not in text, template
        assert " style=" not in text, template
        assert " onclick=" not in text and " onload=" not in text, template


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("Strict-Transport-Security", "max-age=63072000; includeSubDomains; preload"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "same-origin"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
        ("Cross-Origin-Embedder-Policy", "require-corp"),
        ("Origin-Agent-Cluster", "?1"),
        ("X-Permitted-Cross-Domain-Policies", "none"),
    ],
)
def test_security_headers(client: Browser, header: str, value: str) -> None:
    assert client.get("/setup/")[header] == value


@pytest.mark.parametrize("path", ["/setup/", "/no-such-page/"])
def test_pages_are_never_cached(client: Browser, path: str) -> None:
    assert "no-store" in client.get(path)["Cache-Control"]


def test_permissions_policy_denies_powerful_features(client: Browser) -> None:
    policy = client.get("/setup/")["Permissions-Policy"]
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert f"{feature}=()" in policy


def test_plain_http_is_redirected_to_https() -> None:
    from django.test import Client

    response = Client().get("/setup/")
    assert response.status_code == 301
    assert response["Location"] == "https://testserver/setup/"


def test_static_files_are_cached_but_pages_are_not(client: Browser) -> None:
    css = client.get("/static/iki/iki.css")
    assert css.status_code == 200
    assert "no-store" not in css.get("Cache-Control", "")
    assert css["Cross-Origin-Resource-Policy"] == "same-origin"


def test_session_and_csrf_cookies_are_host_only_secure_and_http_only(
    client: Browser, fake_idp: FakeIdP
) -> None:
    complete_setup(client, fake_idp, ADMIN)
    user = Browser()
    sign_in(user, fake_idp, {"sub": "u-1", "email": "u@example.com"})
    session = user.cookies["__Host-iki_session"]
    assert session["secure"] and session["httponly"]
    assert session["samesite"] == "Lax"
    assert session["path"] == "/"
    assert not session["domain"]
    assert not session["max-age"], "the cookie should end with the browser session"
    csrf = user.cookies["__Host-iki_csrf"]
    assert csrf["secure"] and csrf["httponly"]
    assert csrf["samesite"] == "Strict"


def test_sign_in_rotates_the_session_key(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    user = Browser()
    user.get("/setup/")  # anonymous page with a form, so a session may exist
    before = user.cookies.get("__Host-iki_session")
    sign_in(user, fake_idp, {"sub": "u-1", "email": "u@example.com"})
    after = user.cookies["__Host-iki_session"].value
    assert before is None or before.value != after


def test_sign_out_needs_post_with_csrf(client: Browser, fake_idp: FakeIdP) -> None:
    complete_setup(client, fake_idp, ADMIN)
    assert client.get("/logout/").status_code == 405
    forged = client.post("/logout/")
    assert forged.status_code == 403
    assert client.get("/").status_code == 200
    response = client.submit("/teams/", action="/logout/")
    assert response["Location"] == "/signed-out/"
    assert client.get("/").status_code == 302
    assert "signed out of Iki" in client.get("/signed-out/").content.decode()


def test_cross_origin_posts_are_rejected(client: Browser) -> None:
    page = client.get("/setup/").content.decode()
    token = page.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
    response = client.post(
        "/setup/", {"csrfmiddlewaretoken": token, "token": "x"}, HTTP_ORIGIN="https://evil.example"
    )
    assert response.status_code == 403
    assert "This form expired" in response.content.decode()


def test_sign_in_starts_only_with_post(client: Browser, fake_idp: FakeIdP) -> None:
    from iki_server.accounts.models import IdentityProvider

    complete_setup(client, fake_idp, ADMIN)
    provider = IdentityProvider.objects.get()
    response = Browser().get(f"/accounts/oidc/{provider.provider_id}/login/")
    assert response.status_code == 200  # a confirmation page, not a redirect
    assert "Continue with Test SSO" in response.content.decode()


def test_starting_sign_ins_is_rate_limited(client: Browser, fake_idp: FakeIdP) -> None:
    from iki_server.accounts.models import IdentityProvider
    from iki_server.accounts.views import sign_in_limiter

    complete_setup(client, fake_idp, ADMIN)
    sign_in_limiter.reset()
    url = f"/accounts/oidc/{IdentityProvider.objects.get().provider_id}/login/"
    statuses = [Browser().submit("/login/", action=url).status_code for _ in range(21)]
    assert statuses[:20] == [302] * 20
    assert statuses[20] == 429


@pytest.mark.parametrize(
    "path", ["/accounts/signup/", "/accounts/social/signup/", "/accounts/connections/"]
)
def test_local_account_pages_do_not_exist(client: Browser, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/accounts/password/reset/", "/accounts/email/", "/accounts/3rdparty/", "/admin/"],
)
def test_allauth_and_admin_pages_are_not_mounted(client: Browser, path: str) -> None:
    assert client.get(path).status_code == 404
