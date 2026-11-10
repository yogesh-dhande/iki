"""Session lifetime rules.

- **Idle timeout** (``IKI_SESSION_IDLE_TIMEOUT_MINUTES``, default 8 hours):
  ``SESSION_COOKIE_AGE``. The session's expiry in the database moves forward
  as the user is active, refreshed at most once a minute to spare writes.
- **Absolute lifetime** (``IKI_SESSION_MAX_AGE_HOURS``, default 24 hours):
  however active, a session ends this long after sign-in.
- **Before sign-in**, a session only carries sign-in state (the OIDC state
  and PKCE verifier) and lasts ``ANONYMOUS_SESSION_SECONDS``.
- **Background requests aren't activity.** A page's live-update stream
  reconnects by itself, so it never moves the idle timeout forward: an
  unattended tab still signs out on time.
- The cookie itself lasts until the browser closes.
"""

import time
from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin

#: Session key holding a setup grant (see iki_server.setup.grants).
SETUP_SESSION_KEY = "_iki_setup"

logger = structlog.get_logger("iki.accounts")

AUTHENTICATED_AT = "_iki_authenticated_at"
LAST_SEEN = "_iki_last_seen"
REFRESH_INTERVAL_SECONDS = 60
#: Requests a page makes by itself, which don't count as the person being active.
PASSIVE_PATHS = frozenset({"/api/v1/events"})
ANONYMOUS_SESSION_SECONDS = 15 * 60


def start(request: HttpRequest, **_: Any) -> None:  # noqa: ANN401 - signal receiver
    """``user_logged_in`` receiver: starts the absolute-lifetime clock."""
    now = time.time()
    request.session[AUTHENTICATED_AT] = now
    request.session[LAST_SEEN] = now
    request.session.set_expiry(0)  # browser-session cookie; idle expiry is server side


class SessionPolicyMiddleware(MiddlewareMixin):
    """Runs after ``AuthenticationMiddleware``."""

    def process_request(self, request: HttpRequest) -> None:
        session = request.session
        if not request.user.is_authenticated:
            return
        now = time.time()
        authenticated_at = session.get(AUTHENTICATED_AT)
        if not isinstance(authenticated_at, int | float) or (
            now - authenticated_at > settings.IKI_SESSION_MAX_AGE_SECONDS
        ):
            logger.info("session_expired", reason="max_age")
            logout(request)
            return
        if request.path in PASSIVE_PATHS:
            return
        last_seen = session.get(LAST_SEEN, 0)
        if not isinstance(last_seen, int | float) or now - last_seen >= REFRESH_INTERVAL_SECONDS:
            session[LAST_SEEN] = now  # marks the session modified, so its expiry moves forward

    def process_response(self, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        session = getattr(request, "session", None)
        user = getattr(request, "user", None)
        if (
            session is not None
            and session.modified
            and not (user is not None and user.is_authenticated)
            and SETUP_SESSION_KEY not in session  # the setup wizard has its own time limit
            and session.get_expiry_age() > ANONYMOUS_SESSION_SECONDS
        ):
            session.set_expiry(ANONYMOUS_SESSION_SECONDS)
        return response
