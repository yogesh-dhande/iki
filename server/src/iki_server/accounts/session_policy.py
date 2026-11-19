"""Session lifetime rules.

- **Idle timeout** (``IKI_SESSION_IDLE_TIMEOUT_MINUTES``, default 8 hours):
  ``SESSION_COOKIE_AGE``. The session's expiry in the database moves forward
  as the user is active, refreshed at most once a minute to spare writes.
- **Absolute lifetime** (``IKI_SESSION_MAX_AGE_HOURS``, default 24 hours):
  however active, a session ends this long after sign-in.
- **Before sign-in**, a session only carries sign-in state (the OIDC state
  and PKCE verifier) and lasts ``ANONYMOUS_SESSION_SECONDS``.
- **Only what the person does is activity** (21 CFR Part 11 §11.10(d), ADR 0153).
  Pages make requests by themselves: their live-update streams reconnect, and
  they poll and read again what changed. None of these moves the idle timeout
  forward, so an unattended tab still signs out on time. Event streams are
  :func:`passive` views; the web app sends ``Iki-Background: 1`` on any other
  request made while the person isn't using the page.
- The cookie itself lasts until the browser closes.
"""

import time
from collections.abc import Callable
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
#: The header the web app sends on requests it makes while nobody is using the page: they
#: don't count as the person being active.
BACKGROUND_HEADER = "Iki-Background"
ANONYMOUS_SESSION_SECONDS = 15 * 60


def passive[V: Callable[..., Any]](view: V) -> V:
    """Marks a view that pages call by themselves, such as an event stream, which
    reconnects on its own: calling it is never the person being active."""
    view.iki_passive = True  # type: ignore[attr-defined]
    return view


def is_background(request: HttpRequest, view: Callable[..., Any]) -> bool:
    """Whether the request was made without the person doing anything."""
    return bool(getattr(view, "iki_passive", False)) or (
        request.headers.get(BACKGROUND_HEADER) == "1"
    )


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

    def process_view(
        self,
        request: HttpRequest,
        view: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """The person was active: move the idle timeout forward, unless the page made the
        request by itself."""
        if not request.user.is_authenticated or is_background(request, view):
            return
        session = request.session
        now = time.time()
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
