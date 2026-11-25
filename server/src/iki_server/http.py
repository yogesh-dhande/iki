"""Redirects for HTTP clients that send credentials or private request bodies."""

from urllib.parse import urlsplit

import requests


def _origin(url: str | None) -> tuple[str, str, int]:
    parts = urlsplit(url or "")
    ports = {"http": 80, "https": 443}
    if (
        parts.scheme not in ports
        or not parts.hostname
        or parts.username is not None
        or "\\" in parts.netloc
        or "%" in parts.netloc
    ):
        raise ValueError("Invalid HTTP origin")
    return (
        parts.scheme,
        parts.hostname,
        parts.port if parts.port is not None else ports[parts.scheme],
    )


def same_origin_redirects[S: requests.Session](session: S) -> S:
    """Check the URL Requests prepared for each redirect, including a replay of a POST."""
    session.max_redirects = 5
    rebuild_auth = session.rebuild_auth

    def check(prepared_request: requests.PreparedRequest, response: requests.Response) -> None:
        try:
            allowed = _origin(prepared_request.url) == _origin(response.request.url)
        except ValueError:
            allowed = False
        if not allowed:
            raise requests.exceptions.InvalidURL("Refused a redirect to a different HTTP origin.")
        rebuild_auth(prepared_request, response)

    # Requests calls this hook after resolving Location and before dispatching the next hop.
    session.rebuild_auth = check  # type: ignore[method-assign]
    return session
