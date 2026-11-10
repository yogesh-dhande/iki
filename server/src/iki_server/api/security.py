"""Who is calling the API, and how often they may.

For now the API accepts the browser session, which Iki's own pages use.
Unsafe methods need the CSRF token, as forms do: pages carry it in a
``csrf-token`` meta tag (the cookie itself is HttpOnly). OAuth access
tokens, for the SDK, the CLI, MCP clients and service accounts, come next
(ADR 0027).

Every caller is rate limited: signed-in callers per account, others per
client address.
"""

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from ninja.security import APIKeyCookie
from ninja.utils import check_csrf

from iki_server.api.problems import Problem
from iki_server.web.ratelimit import Decision, RateLimiter, client_key

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Requests a client address may make per minute without signing in.
ANONYMOUS_LIMIT = 60
#: Name of the rate-limit policy in the RateLimit headers.
POLICY = "api"


def _limit(request: HttpRequest, limiter: RateLimiter, key: str) -> None:
    decision = limiter.check(key)
    request.iki_rate_limit = decision  # type: ignore[attr-defined]
    if not decision.allowed:
        raise Problem(429, headers={"Retry-After": str(decision.retry_after)})


def rate_limit_headers(decision: Decision) -> dict[str, str]:
    """RateLimit and RateLimit-Policy (draft-ietf-httpapi-ratelimit-headers)."""
    reset = max(decision.retry_after, 0)
    return {
        "RateLimit-Policy": f'"{POLICY}";q={decision.limit};w=60',
        "RateLimit": f'"{POLICY}";r={decision.remaining};t={reset}',
    }


class SessionAuth(APIKeyCookie):
    """The browser session, with CSRF protection for unsafe methods."""

    param_name = settings.SESSION_COOKIE_NAME

    def _get_key(self, request: HttpRequest) -> str | None:
        return request.COOKIES.get(self.param_name)

    def authenticate(self, request: HttpRequest, key: str | None) -> Any:  # noqa: ANN401
        user = request.user
        if not (user.is_authenticated and user.is_active):
            _limit(request, RateLimiter("api_anonymous", ANONYMOUS_LIMIT), client_key(request))
            return None
        if request.method not in SAFE_METHODS and check_csrf(request):
            raise Problem(
                403,
                "The CSRF token is missing or wrong. Send the token from the page's "
                "csrf-token meta tag in the X-CSRFToken header.",
                code="csrf_failed",
            )
        _limit(
            request, RateLimiter(POLICY, settings.IKI_API_RATE_LIMIT_PER_MINUTE), f"user:{user.pk}"
        )
        return user
