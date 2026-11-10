"""Calling ``/api/v1`` the way Iki's own pages do: the session plus the CSRF token."""

import json
from typing import Any

from browser import Browser, Response
from django.conf import settings
from django.utils.crypto import get_random_string

BASE = "/api/v1"


class Api:
    def __init__(self, browser: Browser | None = None) -> None:
        self.browser = browser or Browser()
        self.browser.raise_request_exception = False

    def _token(self) -> str:
        if settings.CSRF_COOKIE_NAME not in self.browser.cookies:
            self.browser.cookies[settings.CSRF_COOKIE_NAME] = get_random_string(32)
        return self.browser.cookies[settings.CSRF_COOKIE_NAME].value

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        csrf: bool = True,
        raw: str | None = None,
        headers: dict[str, str] | None = None,
        **query: Any,
    ) -> Response:
        headers = dict(headers or {})
        if csrf and method != "GET":
            headers["X-CSRFToken"] = self._token()
        data = raw if raw is not None else ("" if body is None else json.dumps(body))
        return self.browser.generic(
            method,
            BASE + path,
            data,
            content_type="application/json",
            headers=headers,
            query_params=query or None,
        )

    def get(self, path: str, **query: Any) -> Response:
        return self.call("GET", path, **query)

    def post(self, path: str, body: Any = None, **kw: Any) -> Response:
        return self.call("POST", path, body, **kw)

    def put(self, path: str, body: Any = None, **kw: Any) -> Response:
        return self.call("PUT", path, body, **kw)

    def patch(self, path: str, body: Any = None, **kw: Any) -> Response:
        return self.call("PATCH", path, body, **kw)

    def delete(self, path: str, **kw: Any) -> Response:
        return self.call("DELETE", path, **kw)


def problem(response: Response, status: int, code: str) -> dict[str, Any]:
    """Asserts the response is a problem with this status and code, and returns it."""
    assert response.status_code == status, (response.status_code, response.content[:500])
    assert response["Content-Type"] == "application/problem+json", response["Content-Type"]
    body: dict[str, Any] = response.json()
    assert body["status"] == status and body["code"] == code, body
    assert body["type"] == "about:blank" and body["title"] and body["detail"], body
    return body
