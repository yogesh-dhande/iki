"""``GET /api/v1/events``: the live-update stream (Server-Sent Events).

Pages open one stream per tab with the browser session; the SDK and CLI can
open one with a bearer token (``read`` scope). Events say *that* something
changed, with identifiers and counts, and each one is checked against the
caller's permissions before it's sent. A stream:

- starts with ``ready`` (the unread notification count), then sends
  ``notification``, ``notifications_read`` and ``resync`` as they happen;
- sends a comment every ``HEARTBEAT`` seconds, so proxies keep it open, and
  checks each time that the session or token is still valid, ending with
  ``signed_out`` when it isn't;
- ends after ``MAX_LIFETIME`` (and when the server shuts down). Browsers'
  EventSource reconnects by itself, to whichever pod is up.

It holds no database connection while waiting: each check borrows one from
the pool and returns it.
"""

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections
from django.http import HttpRequest, HttpResponseBase, StreamingHttpResponse
from django.utils import timezone
from oauth2_provider.oauth2_backends import get_oauthlib_core

from iki_server.accounts.models import User, UserSession
from iki_server.accounts.session_policy import AUTHENTICATED_AT
from iki_server.api.problems import Problem, response
from iki_server.api.security import ANONYMOUS_LIMIT, POLICY, BearerAuth, _limit
from iki_server.notifications import services as notifications
from iki_server.org import permissions
from iki_server.realtime.hub import Hub, Stream, hub
from iki_server.web.ratelimit import RateLimiter, client_key

HEARTBEAT = 20.0
MAX_LIFETIME = 30 * 60.0
#: Open tabs one person may have, per web process.
MAX_STREAMS_PER_USER = 20
#: Streams one web process holds. Beyond it, clients are sent to try again.
MAX_STREAMS = 2000
#: Milliseconds browsers wait before reconnecting.
RETRY_MS = 5000

Check = Callable[[], bool]

logger = structlog.get_logger("iki.realtime")


async def db[T](fn: Callable[..., T], *args: Any) -> T:  # noqa: ANN401
    """Runs database code in a worker thread, then returns its connection to the pool."""

    def run() -> T:
        try:
            return fn(*args)
        finally:
            close_old_connections()

    return await sync_to_async(run, thread_sensitive=True)()


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _bearer(request: HttpRequest, token: str) -> tuple[User, Check]:
    user = BearerAuth().authenticate(request, token)
    limits = permissions.token_limits(user)
    if limits is not None and "read" not in limits.scopes:
        raise Problem(
            403,
            "This needs the read scope, which your token doesn’t have.",
            code="insufficient_scope",
            headers={"WWW-Authenticate": 'Bearer error="insufficient_scope", scope="read"'},
        )

    def still_valid() -> bool:
        valid, _ = get_oauthlib_core().verify_request(request, scopes=[])
        return valid and User.objects.filter(pk=user.pk, is_active=True).exists()

    return user, still_valid


def _session(request: HttpRequest) -> tuple[User, Check]:
    user = request.user
    if not (user.is_authenticated and user.is_active):
        _limit(request, RateLimiter("api_anonymous", ANONYMOUS_LIMIT), client_key(request))
        raise Problem(401)
    _limit(request, RateLimiter(POLICY, settings.IKI_API_RATE_LIMIT_PER_MINUTE), f"user:{user.pk}")
    key = request.session.session_key
    started = request.session.get(AUTHENTICATED_AT, 0)

    def still_valid() -> bool:
        if time.time() - started > settings.IKI_SESSION_MAX_AGE_SECONDS:
            return False
        return (
            UserSession.objects.filter(session_key=key, expire_date__gt=timezone.now()).exists()
            and User.objects.filter(pk=user.pk, is_active=True).exists()
        )

    return user, still_valid


def _authenticate(request: HttpRequest) -> tuple[User, Check]:
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer ":
        return _bearer(request, header[7:].strip())
    return _session(request)


def _unread(user: User) -> int:
    return notifications.unread_count(user)


def _visible(user: User, notification_id: str) -> bool:
    return notifications.inbox(user).filter(pk=notification_id).exists()


async def events(request: HttpRequest) -> HttpResponseBase:
    if request.method not in ("GET", "HEAD"):
        problem = Problem(405, headers={"Allow": "GET"})
        return response(problem)
    try:
        await request.auser()
        user, still_valid = await db(_authenticate, request)
    except Problem as problem:
        return response(problem)
    streams = hub()
    if streams.closing.is_set() or streams.count() >= MAX_STREAMS:
        return response(
            Problem(503, "Live updates are busy. Try again shortly.", headers={"Retry-After": "5"})
        )
    if streams.count(user.pk) >= MAX_STREAMS_PER_USER:
        return response(
            Problem(
                429,
                "Too many tabs are getting live updates. Close some, or reload this one later.",
                headers={"Retry-After": "30"},
            )
        )
    unread = await db(_unread, user)
    stream = streams.subscribe(user.pk)
    reply = StreamingHttpResponse(
        _run(streams, stream, user, still_valid, unread),
        content_type="text/event-stream; charset=utf-8",
    )
    reply["Cache-Control"] = "no-store"
    # nginx and similar proxies would otherwise buffer the stream.
    reply["X-Accel-Buffering"] = "no"
    return reply


async def _run(
    streams: Hub, stream: Stream, user: User, still_valid: Check, unread: int
) -> AsyncIterator[bytes]:
    loop = asyncio.get_running_loop()
    # Spread reconnects out, so pods restarted together aren't hit at once.
    deadline = loop.time() + MAX_LIFETIME * random.uniform(0.9, 1.0)  # noqa: S311
    opened, reason = loop.time(), "disconnected"
    logger.info("realtime_stream_opened", user=str(user.pk), streams=streams.count())
    try:
        yield f"retry: {RETRY_MS}\n\n".encode() + sse("ready", {"unread": unread})
        next_check = loop.time() + HEARTBEAT
        while True:
            wait = max(min(next_check, deadline) - loop.time(), 0)
            getting = asyncio.ensure_future(stream.queue.get())
            closing = asyncio.ensure_future(streams.closing.wait())
            try:
                done, _ = await asyncio.wait(
                    {getting, closing}, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                getting.cancel()
                closing.cancel()
            if streams.closing.is_set():
                reason = "shutdown"
                return
            if getting in done:
                message = await _prepare(user, getting.result())
                if message is not None:
                    yield message
                continue
            if loop.time() >= deadline:
                reason = "lifetime"
                return
            if not await db(still_valid):
                reason = "signed_out"
                yield sse("signed_out", {})
                return
            yield b": keep-alive\n\n"
            next_check = loop.time() + HEARTBEAT
    finally:
        streams.unsubscribe(stream)
        logger.info(
            "realtime_stream_ended",
            user=str(user.pk),
            reason=reason,
            seconds=round(loop.time() - opened, 1),
        )


async def _prepare(user: User, item: dict[str, Any]) -> bytes | None:
    """The event as sent, or None if this caller mayn't see it."""
    event, data = item["event"], item.get("data", {})
    if event == "notification":
        notification_id = str(data.get("id", ""))
        if not await db(_visible, user, notification_id):
            return None
        return sse(event, {"id": notification_id, "unread": await db(_unread, user)})
    if event == "notifications_read":
        return sse(event, {"unread": await db(_unread, user)})
    if event == "resync":
        return sse(event, {})
    return None
