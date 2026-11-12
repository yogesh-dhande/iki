"""``/mcp``: the MCP endpoint (Streamable HTTP), and its protected resource metadata.

Django answers first: host checks, request logs, the audit channel (MCP). The view
checks the bearer token as the API does and the Origin header (MCP's guard against
DNS rebinding), then hands the request to the SDK. Without a valid token it answers
401 with a challenge pointing at the protected resource metadata (RFC 9728), where
MCP clients learn how to sign in.
"""

import json
from typing import Any

from asgiref.sync import sync_to_async
from django.core import signing
from django.db import close_old_connections
from django.http import HttpRequest, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from iki_server.api.problems import Problem, response
from iki_server.api.security import MCP_AGENT_KEY, BearerAuth, bearer_challenge
from iki_server.core.ids import uuid7
from iki_server.mcp import bridge, runtime
from iki_server.mcp.api import acting_for

#: Headers of the SDK's answer that Django sets itself, or that mustn't pass on.
_DROPPED = frozenset({"content-length", "transfer-encoding", "connection"})


def _challenge(request: HttpRequest, error: str | None = None) -> HttpResponse:
    problem = Problem(
        401,
        "Sign in with OAuth to use Iki's MCP server."
        if error is None
        else "The access token is invalid, expired or revoked. Get a new one.",
        code="invalid_token" if error else "unauthorized",
        headers={"WWW-Authenticate": bearer_challenge(request, error)},
    )
    return response(problem)


def _same_origin(request: HttpRequest) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # not a browser: agents send none
    return origin == f"{request.scheme}://{request.get_host()}"


def _authenticate(request: HttpRequest, token: str) -> Any:  # noqa: ANN401 - a User
    try:
        return BearerAuth().authenticate(request, token)
    finally:
        close_old_connections()


# --- Sessions (ADR 0072) ---------------------------------------------------------------------

SESSION_HEADER = "Mcp-Session-Id"
_SESSION_SALT = "iki.mcp.session"
#: As long as a refresh token: a client that keeps going past it starts a new session.
SESSION_DAYS = 30
#: Where MCP 2026-07-28 requests carry the client's ``clientInfo``.
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"


def _message(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _software(info: Any) -> str:  # noqa: ANN401 - JSON
    """What a ``clientInfo`` says the client is: "name version"."""
    if not isinstance(info, dict):
        return ""
    return " ".join(str(info[k]) for k in ("name", "version") if info.get(k))[:255]


def _whose(request: HttpRequest) -> dict[str, str]:
    client = request.iki_oauth_client  # type: ignore[attr-defined]  # BearerAuth set it
    return {"c": client.client_id, "u": str(request.user_pk)}  # type: ignore[attr-defined]


def new_session(request: HttpRequest, software: str) -> tuple[str, dict[str, str]]:
    """A session for an ``initialize``: its ID, signed, carrying what the client said it
    is and bound to the token's client and person, and what tools' calls are told."""
    session = uuid7().hex
    signed = signing.dumps(
        {**_whose(request), "s": session, "i": software}, salt=_SESSION_SALT, compress=True
    )
    return signed, {"session": session, "software": software}


def known_session(request: HttpRequest, signed: str) -> dict[str, str] | None:
    """The session a request names, if Iki issued it to this client and person."""
    try:
        found = signing.loads(signed, salt=_SESSION_SALT, max_age=SESSION_DAYS * 86400)
    except signing.BadSignature:
        return None
    if not isinstance(found, dict) or {k: found.get(k) for k in ("c", "u")} != _whose(request):
        return None
    return {"session": str(found.get("s", "")), "software": str(found.get("i", ""))}


@csrf_exempt
async def endpoint(request: HttpRequest) -> HttpResponseBase:
    if not _same_origin(request):
        return response(Problem(403, "Iki's MCP server doesn't take requests from web pages."))
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return _challenge(request)
    try:
        user = await sync_to_async(_authenticate, thread_sensitive=True)(request, token)
    except Problem as problem:
        if problem.status == 401:
            return _challenge(request, "invalid_token")
        return response(problem)
    app = runtime.app()
    if app is None:
        return response(Problem(503, "The MCP server is starting. Try again in a moment."))
    request.user_pk = user.pk  # type: ignore[attr-defined]
    message = _message(request.body)
    found = message.get("params")
    params: dict[str, Any] = found if isinstance(found, dict) else {}
    issued: str | None = None
    signed = request.headers.get(SESSION_HEADER)
    if message.get("method") == "initialize":
        issued, agent = new_session(request, _software(params.get("clientInfo")))
    elif signed:
        known = known_session(request, signed)
        if known is None:
            # MCP clients start a new session when theirs is unknown.
            return response(Problem(404, "This MCP session isn't Iki's, or has ended."))
        agent = known
    else:
        meta = params.get("_meta")
        info = meta.get(CLIENT_INFO_META) if isinstance(meta, dict) else None
        agent = {"software": _software(info)}
    scope: Any = request.scope  # type: ignore[attr-defined]  # an ASGI request's
    scope[MCP_AGENT_KEY] = agent
    # The SDK serves each request on its own (stateless): Iki keeps the sessions.
    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"mcp-session-id"]
    inner = {**scope, "headers": headers, "path": "/", "raw_path": b"/", "root_path": ""}
    with acting_for(scope):
        answer = await bridge.call(app, inner, request.body)
        content_type = answer.header("content-type") or "application/json"
        if content_type.startswith("text/event-stream"):
            reply: HttpResponseBase = StreamingHttpResponse(
                answer.body, status=answer.status, content_type=content_type
            )
        else:
            reply = HttpResponse(
                await answer.read(), status=answer.status, content_type=content_type
            )
    for key, value in answer.headers:
        name = key.decode("latin-1")
        if name.lower() not in _DROPPED and name.lower() != "content-type":
            reply[name] = value.decode("latin-1")
    if issued is not None and answer.status == 200:
        reply[SESSION_HEADER] = issued
    return reply
