"""``/mcp``: the MCP endpoint (Streamable HTTP), and its protected resource metadata.

Django answers first: host checks, request logs, the audit channel (MCP). The view
checks the bearer token as the API does and the Origin header (MCP's guard against
DNS rebinding), then hands the request to the SDK. Without a valid token it answers
401 with a challenge pointing at the protected resource metadata (RFC 9728), where
MCP clients learn how to sign in.
"""

from typing import Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.http import HttpRequest, HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt

from iki_server.api.problems import Problem, response
from iki_server.api.security import BearerAuth, bearer_challenge
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


@csrf_exempt
async def endpoint(request: HttpRequest) -> HttpResponseBase:
    if not _same_origin(request):
        return response(Problem(403, "Iki's MCP server doesn't take requests from web pages."))
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return _challenge(request)
    try:
        await sync_to_async(_authenticate, thread_sensitive=True)(request, token)
    except Problem as problem:
        if problem.status == 401:
            return _challenge(request, "invalid_token")
        return response(problem)
    app = runtime.app()
    if app is None:
        return response(Problem(503, "The MCP server is starting. Try again in a moment."))
    scope: Any = request.scope  # type: ignore[attr-defined]  # an ASGI request's
    inner = {**scope, "path": "/", "raw_path": b"/", "root_path": ""}
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
    return reply
