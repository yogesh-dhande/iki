"""Test helpers that drive Iki the way a browser and a person would."""

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.test import Client
from fake_idp import FakeIdP

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

    Response = _MonkeyPatchedWSGIResponse
else:
    Response = Any

CSRF_INPUT = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


class Browser(Client):
    """Django's test client over https, with CSRF checks enforced like in production."""

    def __init__(self) -> None:
        super().__init__(enforce_csrf_checks=True)

    def generic(
        self,
        method: str,
        path: Any,
        data: Any = "",
        content_type: str | None = "application/octet-stream",
        secure: bool = True,
        *,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[Any, Any] | None = None,
        **extra: Any,
    ) -> "Response":
        if method not in ("GET", "HEAD", "OPTIONS"):
            # Browsers send Origin on every POST; Django's CSRF check relies on it over https.
            extra.setdefault("HTTP_ORIGIN", "https://testserver")
        return super().generic(
            method,
            path,
            data,
            content_type,
            secure=True,
            headers=headers,
            query_params=query_params,
            **extra,
        )

    def submit(
        self, page_url: str, data: dict[str, str] | None = None, action: str | None = None
    ) -> Response:
        """Loads ``page_url``, then posts its form (to ``action`` if given) with the CSRF token."""
        page = self.get(page_url)
        assert page.status_code == 200, (page_url, page.status_code)
        match = CSRF_INPUT.search(page.content.decode())
        assert match, f"no CSRF token on {page_url}"
        return self.post(
            action or page_url, {"csrfmiddlewaretoken": match.group(1), **(data or {})}
        )


def path_of(response: Response) -> str:
    return urlsplit(response["Location"]).path


def sign_in_at_idp(
    browser: Browser, idp: FakeIdP, start: Response, claims: dict[str, Any]
) -> Response:
    """Follows a redirect to the provider, signs in there, and returns Iki's callback response."""
    assert start.status_code == 302, start.status_code
    authorization_url = start["Location"]
    assert authorization_url.startswith(idp.discovery()["authorization_endpoint"]), (
        authorization_url
    )
    callback = urlsplit(idp.authorize(authorization_url, claims))
    return browser.get(f"{callback.path}?{callback.query}")


def setup_link_token(*args: str) -> str:
    """Runs ``iki-server setup_link`` and returns the token from the printed link."""
    from io import StringIO

    from django.core.management import call_command

    out = StringIO()
    call_command("setup_link", "--url", "https://testserver", *args, stdout=out)
    match = re.search(r"https://testserver/setup/#(\S+)", out.getvalue())
    assert match, out.getvalue()
    return match.group(1)


def complete_setup(
    browser: Browser,
    idp: FakeIdP,
    claims: dict[str, Any],
    organization: str = "Example Bio",
    recover: bool = False,
) -> Response:
    """Runs the whole first-run wizard and returns the final response."""
    token = setup_link_token(*(["--recover"] if recover else []))
    response = browser.submit("/setup/", {"token": token})
    assert response.status_code == 302, response.content.decode()
    if not recover:
        assert path_of(response) == "/setup/organization/"
        response = browser.submit("/setup/organization/", {"organization_name": organization})
        assert path_of(response) == "/setup/identity-provider/"
    response = browser.submit(
        "/setup/identity-provider/",
        {
            "display_name": "Test SSO",
            "issuer": idp.issuer,
            "client_id": idp.client_id,
            "client_secret": idp.client_secret,
        },
    )
    assert response.status_code == 302, response.content.decode()
    assert path_of(response) == "/setup/verify/"
    verify = browser.get("/setup/verify/")
    action = re.search(r'<form method="post" action="([^"]+)"', verify.content.decode())
    assert action
    start = browser.submit("/setup/verify/", {"next": "/setup/complete/"}, action=action.group(1))
    callback = sign_in_at_idp(browser, idp, start, claims)
    assert callback.status_code == 302, callback.content.decode()
    assert path_of(callback) == "/setup/complete/"
    return browser.submit("/setup/complete/")


def sign_in(browser: Browser, idp: FakeIdP, claims: dict[str, Any]) -> Response:
    """Signs in from the login page; returns the callback response."""
    page = browser.get("/login/")
    action = re.search(r'<form method="post" action="([^"]+)"', page.content.decode())
    assert action, page.content.decode()
    start = browser.submit("/login/", {"next": "/"}, action=action.group(1))
    return sign_in_at_idp(browser, idp, start, claims)
