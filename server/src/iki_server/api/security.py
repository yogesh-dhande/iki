"""Who is calling the API, and how often they may.

Two ways in:

- **OAuth access tokens** (ADR 0028), in ``Authorization: Bearer``, for the
  SDK, the CLI, MCP clients and service accounts. A token can do only what
  both its scopes (and project choice) and its user's roles allow.
- **The browser session**, which Iki's own pages use. Unsafe methods need
  the CSRF token, as forms do: pages carry it in a ``csrf-token`` meta tag
  (the cookie itself is HttpOnly).

Every caller is rate limited: signed-in callers per account, others per
client address.
"""

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from ninja.security import APIKeyCookie, HttpBearer
from ninja.utils import check_csrf
from oauth2_provider.oauth2_backends import get_oauthlib_core

from iki_server.api.problems import Problem
from iki_server.audit import log as audit
from iki_server.oauth.scopes import RESERVED
from iki_server.org import permissions
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


def bearer_challenge(request: HttpRequest, error: str | None = None, **params: str) -> str:
    """A WWW-Authenticate challenge pointing at the protected resource metadata (RFC 9728)."""
    metadata = request.build_absolute_uri("/.well-known/oauth-protected-resource")
    parts = ['realm="iki"', f'resource_metadata="{metadata}"']
    if error:
        parts.append(f'error="{error}"')
    parts += [f'{key}="{value}"' for key, value in params.items()]
    return "Bearer " + ", ".join(parts)


def _invalid_token(request: HttpRequest) -> Problem:
    return Problem(
        401,
        "The access token is invalid, expired or revoked. Get a new one.",
        code="invalid_token",
        headers={"WWW-Authenticate": bearer_challenge(request, "invalid_token")},
    )


#: The ASGI scope key in which the MCP endpoint passes its tools' API calls what it knows
#: of the agent: its session and the software it says it is. Not a header, so no client
#: can set it.
MCP_AGENT_KEY = "iki.mcp_agent"

#: The header in which an agent may say what model it is (ADR 0072).
MODEL_HEADER = "Iki-Agent-Model"


def reported(value: str) -> str:
    """Something an agent says about itself, as stored: printable, and not too long."""
    return "".join(c for c in value if c.isprintable()).strip()[:255]


def agent_of(request: HttpRequest, client: Any, consent: Any) -> audit.AgentInfo:  # noqa: ANN401
    """The agent a token's request comes through: its client, the person's grant, and
    what the MCP endpoint and the agent say of it."""
    mcp = getattr(request, "scope", {}).get(MCP_AGENT_KEY) or {}
    return audit.AgentInfo(
        client_id=client.client_id,
        client_name=client.display_name(),
        grant=consent.grant if consent is not None else None,
        session=str(mcp.get("session", "")),
        software=reported(str(mcp.get("software", ""))),
        model=reported(request.headers.get(MODEL_HEADER, "")),
    )


class BearerAuth(HttpBearer):
    """OAuth access tokens issued by Iki's authorization server."""

    def authenticate(self, request: HttpRequest, token: str) -> Any:  # noqa: ANN401
        from iki_server.oauth.models import Consent

        valid, oauth_request = get_oauthlib_core().verify_request(request, scopes=[])
        if not valid or oauth_request is None:
            _limit(request, RateLimiter("api_anonymous", ANONYMOUS_LIMIT), client_key(request))
            raise _invalid_token(request)
        client = oauth_request.client
        user = oauth_request.user or client.service_account
        if user is None or not user.is_active:
            raise _invalid_token(request)
        scopes = frozenset(oauth_request.scopes)
        if not scopes - RESERVED.keys():
            # Such as iki-worker's token, which only registers a worker (ADR 0058).
            raise Problem(
                403,
                "This token is only for registering a worker, not for Iki’s API.",
                code="insufficient_scope",
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="read"'},
            )
        projects = None
        consent = None
        # A person's token from a service account's credential: one it got by acting for
        # them, under their delegation (ADR 0073).
        account = client.service_account if oauth_request.user is not None else None
        if client.service_account_id is None or account is not None:
            consent = (
                Consent.objects.filter(user=user, client=client)
                .prefetch_related("projects")
                .first()
            )
            if consent is None or consent.expired:
                raise _invalid_token(request)
            # A narrowed consent applies to tokens issued before it changed.
            scopes &= frozenset(consent.scope_list)
            if not consent.all_projects:
                projects = frozenset(p.pk for p in consent.projects.all())
        if account is not None:
            if not (client.acts_for_people and account.is_active):
                raise _invalid_token(request)
            # Only where the service account may go too.
            theirs = frozenset(permissions.visible_projects(account).values_list("pk", flat=True))
            projects = theirs if projects is None else projects & theirs
        permissions.limit_to(
            user,
            permissions.TokenLimits(scopes=scopes, projects=projects, alongside=account),
        )
        request.iki_oauth_client = client  # type: ignore[attr-defined]
        request.iki_delegation = consent if account is not None else None  # type: ignore[attr-defined]
        audit.note_agent(agent_of(request, client, consent))
        _limit(
            request, RateLimiter(POLICY, settings.IKI_API_RATE_LIMIT_PER_MINUTE), f"user:{user.pk}"
        )
        return user


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
