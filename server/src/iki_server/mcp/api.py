"""Tools call Iki's REST API in-process, as the agent: with its token, through all of
Django's middleware, marked as made through MCP.

The mark is a key in the ASGI scope, not a header, so no client can set it.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from django.conf import settings
from django.core.handlers.asgi import ASGIHandler
from mcp.server.mcpserver.exceptions import ToolError
from oauth2_provider.oauth2_validators import validate_resource_as_url_prefix

from iki_server.api.security import MCP_AGENT_KEY
from iki_server.mcp import bridge

#: The ASGI scope key that marks a request as an MCP tool's.
VIA_KEY = "iki.via"

#: Headers an API call made for a tool keeps from the agent's request.
_KEPT = frozenset(
    {
        b"host",
        b"authorization",
        b"user-agent",
        b"x-forwarded-for",
        b"x-forwarded-proto",
        b"iki-agent-model",
    }
)


@dataclass(frozen=True)
class Agent:
    """The MCP request a tool is answering: who sent it, and how it reached Iki."""

    scope: Mapping[str, Any]


_agent: ContextVar[Agent] = ContextVar("iki_mcp_agent")


@contextmanager
def acting_for(scope: Mapping[str, Any]) -> Iterator[None]:
    token = _agent.set(Agent(scope=scope))
    try:
        yield
    finally:
        _agent.reset(token)


@cache
def _django() -> ASGIHandler:
    return ASGIHandler()


def web_url(web_path: str) -> str:
    """The web app's address for `web_path`, as the person reaches Iki: its public URL
    if set, else the host and scheme the agent's request came by."""
    if settings.IKI_PUBLIC_URL:
        return settings.IKI_PUBLIC_URL.rstrip("/") + web_path
    outer = _agent.get().scope
    # The server trusts X-Forwarded-Proto only from its proxies, and sets the scheme.
    host = dict(outer.get("headers", [])).get(b"host", b"localhost").decode("latin-1")
    return f"{outer.get('scheme', 'http')}://{host}{web_path}"


def path(template: str, **parts: str | int) -> str:
    """An API path, each part escaped: ``path("/entities/{ref}", ref="PRT-000001")``."""
    return template.format(**{k: quote(str(v), safe="") for k, v in parts.items()})


Query = Mapping[str, str | int | float | bool | Sequence[str] | None]


def _query(query: Query | None) -> bytes:
    pairs = [
        (key, str(v).lower() if isinstance(v, bool) else str(v))
        for key, value in (query or {}).items()
        if value is not None
        for v in ([value] if isinstance(value, str | int | float | bool) else value)
    ]
    return urlencode(pairs).encode()


async def call(
    method: str,
    api_path: str,
    *,
    query: Query | None = None,
    body: Any = None,  # noqa: ANN401 - JSON
    if_match: str | None = None,
) -> Any:  # noqa: ANN401 - JSON
    """The API's answer, or a ``ToolError`` saying what was wrong, as its problem did."""
    agent = _agent.get()
    outer = agent.scope
    headers = [(k, v) for k, v in outer.get("headers", []) if k.lower() in _KEPT]
    headers += [(b"accept", b"application/json")]
    if if_match is not None:
        headers += [(b"if-match", if_match.encode())]
    payload = b""
    if body is not None:
        payload = json.dumps(body).encode()
        headers += [(b"content-type", b"application/json")]
    full = "/api/v1" + api_path
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": outer.get("scheme", "http"),
        "path": full,
        "raw_path": full.encode(),
        "root_path": "",
        "query_string": _query(query),
        "headers": headers,
        "client": outer.get("client"),
        "server": outer.get("server"),
        VIA_KEY: "mcp",
        MCP_AGENT_KEY: outer.get(MCP_AGENT_KEY),
    }
    answer = await bridge.call(_django(), scope, payload)
    data = await answer.read()
    parsed = json.loads(data) if data else None
    if answer.status >= 400:
        raise ToolError(problem_text(answer.status, parsed))
    return parsed


def problem_text(status: int, problem: Any) -> str:  # noqa: ANN401 - JSON
    """A problem as an agent reads it: what was wrong, and where."""
    if not isinstance(problem, dict):
        return f"Iki answered {status}."
    lines = [str(problem.get("detail") or problem.get("title") or f"Iki answered {status}.")]
    code = problem.get("code")
    lines[0] += f" ({status}{', ' + code if code else ''})"
    lines += [
        f"- {error.get('pointer', '#')}: {error.get('detail', '')}"
        for error in problem.get("errors") or []
        if isinstance(error, dict)
    ]
    return "\n".join(lines)


def resource_allowed(request_uri: str, audiences: list[str]) -> bool:
    """Whether a token is for this request (RFC 8707): its resources must include the
    address, as usual; and a token for the MCP server itself is good for the API calls
    its tools make, and for nothing else."""
    if validate_resource_as_url_prefix(request_uri, audiences):
        return True
    if _agent.get(None) is None:
        return False
    parts = urlsplit(request_uri)
    if not parts.path.startswith("/api/"):
        return False
    server = f"{parts.scheme}://{parts.netloc}/mcp"
    return any(a.rstrip("/") == server for a in audiences)
