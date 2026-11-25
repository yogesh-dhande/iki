"""Redirect handling for Google API calls and the separate credential transport."""

import requests
from google.auth.transport.requests import AuthorizedSession, Request

from iki_server.http import same_origin_redirects


def credential_request() -> Request:
    return Request(session=same_origin_redirects(requests.Session()))


def restrict_google_session(session: AuthorizedSession) -> None:
    # Configure the SDK's sessions in place to keep their retry and mTLS adapters.
    same_origin_redirects(session)
    same_origin_redirects(session._auth_request.session)
