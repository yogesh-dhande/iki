"""Live updates: the Server-Sent Events stream at /api/v1/events, fed by LISTEN/NOTIFY."""

import asyncio
import contextlib
import http.client
import json
import signal
import time
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connection
from django.test import AsyncClient, Client
from org_world import World, build_world
from real_server import app_role, serve, server_env
from test_oauth import signed_in_token

from iki_server.accounts.models import User, UserSession
from iki_server.accounts.session_policy import LAST_SEEN
from iki_server.notifications import kinds, services
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.realtime import hub as hub_module
from iki_server.realtime import views
from iki_server.realtime.hub import Stream, hub

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
async def world() -> World:
    return await sync_to_async(build_world)()


async def signed_in(user: User) -> AsyncClient:
    client = AsyncClient()
    await client.aforce_login(user)
    return client


class Events:
    """Reads one stream event by event, as a browser would.

    A reader task pulls chunks; closing cancels it, which is what Django's
    ASGI handler does when the client goes away.
    """

    def __init__(self, response: Any) -> None:
        self.response = response
        self.chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.buffer = b""
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        async for chunk in self.response.streaming_content:
            await self.chunks.put(chunk)
        await self.chunks.put(None)

    async def next(self, timeout: float = 5.0) -> tuple[str, dict[str, Any]] | None:
        """The next event (name, data), skipping keep-alives; None when the stream ends."""
        while True:
            while b"\n\n" in self.buffer:
                block, self.buffer = self.buffer.split(b"\n\n", 1)
                fields = dict(
                    line.split(": ", 1) for line in block.decode().splitlines() if ": " in line
                )
                if "event" in fields:
                    return fields["event"], json.loads(fields.get("data", "{}"))
            chunk = await asyncio.wait_for(self.chunks.get(), timeout)
            if chunk is None:
                return None
            self.buffer += chunk

    async def close(self) -> None:
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader


async def open_stream(client: AsyncClient, **headers: str) -> Events:
    response = await client.get("/api/v1/events", headers=headers, secure=True)
    assert response.status_code == 200, response.content
    assert response["Content-Type"] == "text/event-stream; charset=utf-8"
    assert response["Cache-Control"] == "no-store"
    assert response["X-Accel-Buffering"] == "no"
    events = Events(response)
    assert await events.next() is not None  # ready
    await asyncio.wait_for(hub().listening.wait(), 5)
    return events


async def notify(user: User, title: str, project: Project | None = None) -> str:
    sent = await sync_to_async(services.notify)(
        [user], kinds.PROJECT_ADDED, title=title, path="/", project=project
    )
    return str(sent[0].pk)


async def test_anonymous_callers_are_asked_to_sign_in() -> None:
    response = await AsyncClient().get("/api/v1/events", secure=True)
    assert response.status_code == 401
    assert response["Content-Type"] == "application/problem+json"
    assert "WWW-Authenticate" in response


async def test_the_stream_starts_with_the_unread_count_then_says_what_arrives(
    world: World,
) -> None:
    viewer = world.users["viewer"]
    await notify(viewer, "Before")
    client = await signed_in(viewer)
    response = await client.get("/api/v1/events", secure=True)
    events = Events(response)
    assert await events.next() == ("ready", {"unread": 1})
    await asyncio.wait_for(hub().listening.wait(), 5)
    try:
        sent = await notify(viewer, "Hello")
        assert await events.next() == ("notification", {"id": sent, "unread": 2})
        await sync_to_async(services.mark_all_read)(viewer)
        assert await events.next() == ("notifications_read", {"unread": 0})
    finally:
        await events.close()
    assert hub().count() == 0


async def test_streams_only_hear_about_their_own_person(world: World) -> None:
    viewer, editor = world.users["viewer"], world.users["editor"]
    events = await open_stream(await signed_in(viewer))
    try:
        await notify(editor, "Not yours")
        mine = await notify(viewer, "Yours")
        assert await events.next() == ("notification", {"id": mine, "unread": 1})
    finally:
        await events.close()


async def test_token_streams_hear_only_what_their_token_may_see(world: World) -> None:
    editor = world.users["editor"]
    other = await sync_to_async(Project.objects.create)(team=world.team, name="Second Screen")
    await sync_to_async(ProjectMembership.objects.create)(
        project=other, user=editor, role=ProjectRole.EDITOR
    )
    _, tokens = await sync_to_async(signed_in_token)(world, scope="read", projects=[world.project])
    events = await open_stream(AsyncClient(), Authorization=f"Bearer {tokens['access_token']}")
    try:
        await notify(editor, "Other project", project=other)
        allowed = await notify(editor, "This project", project=world.project)
        assert await events.next() == ("notification", {"id": allowed, "unread": 1})
    finally:
        await events.close()


async def test_tokens_need_the_read_scope(world: World) -> None:
    _, tokens = await sync_to_async(signed_in_token)(world, scope="write")
    response = await AsyncClient().get(
        "/api/v1/events",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        secure=True,
    )
    assert response.status_code == 403
    assert json.loads(response.content)["code"] == "insufficient_scope"
    bad = await AsyncClient().get(
        "/api/v1/events", headers={"Authorization": "Bearer nope"}, secure=True
    )
    assert bad.status_code == 401


async def test_the_stream_ends_when_the_session_does(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "HEARTBEAT", 0.2)
    viewer = world.users["viewer"]
    events = await open_stream(await signed_in(viewer))
    await sync_to_async(UserSession.objects.filter(user=viewer).delete)()
    assert await events.next() == ("signed_out", {})
    assert await events.next() is None


async def test_the_stream_ends_when_the_person_is_deactivated(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "HEARTBEAT", 0.2)
    viewer = world.users["viewer"]
    events = await open_stream(await signed_in(viewer))
    await sync_to_async(User.objects.filter(pk=viewer.pk).update)(is_active=False)
    assert await events.next() == ("signed_out", {})


async def test_streams_end_after_a_while_so_browsers_reconnect(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "MAX_LIFETIME", 0.3)
    events = await open_stream(await signed_in(world.users["viewer"]))
    assert await events.next() is None


async def test_streams_dont_keep_the_session_alive(world: World) -> None:
    viewer = world.users["viewer"]
    client = await signed_in(viewer)
    session = await sync_to_async(UserSession.objects.get)(user=viewer)
    data = await sync_to_async(session.get_decoded)()
    before = data.get(LAST_SEEN)
    events = await open_stream(client)
    await events.close()
    session = await sync_to_async(UserSession.objects.get)(user=viewer)
    assert (await sync_to_async(session.get_decoded)()).get(LAST_SEEN) == before


async def test_one_person_can_have_only_so_many_tabs_streaming(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "MAX_STREAMS_PER_USER", 1)
    client = await signed_in(world.users["viewer"])
    events = await open_stream(client)
    try:
        response = await client.get("/api/v1/events", secure=True)
        assert response.status_code == 429
        assert response["Retry-After"] == "30"
    finally:
        await events.close()


async def test_streams_resync_after_the_hub_loses_its_connection(world: World) -> None:
    events = await open_stream(await signed_in(world.users["viewer"]))
    try:

        def drop_listener() -> None:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND application_name = 'iki-server-realtime'"
                )

        await sync_to_async(drop_listener)()
        # Events may have been missed while it was away: the page refetches.
        assert await events.next(timeout=10) == ("resync", {})
    finally:
        await events.close()


def test_a_stuck_stream_is_told_to_resync_instead_of_growing() -> None:
    async def fill() -> list[dict[str, Any]]:
        stream = Stream("someone")
        for n in range(hub_module.QUEUE_SIZE + 5):
            stream.put({"event": "notification", "data": {"id": n}})
        return [stream.queue.get_nowait() for _ in range(stream.queue.qsize())]

    queued = asyncio.run(fill())
    assert queued[0] == {"event": "resync", "data": {}}
    assert len(queued) <= hub_module.QUEUE_SIZE


def test_events_carry_identifiers_not_content() -> None:
    from iki_server.realtime import events

    with pytest.raises(ValueError, match="identifiers, not content"):
        events.publish(
            User(pk="00000000-0000-0000-0000-000000000000").pk, "x", {"blob": "x" * 8000}
        )


def test_shutting_down_ends_streams_at_once(django_db_blocker: Any, tmp_path: Any) -> None:
    """uvicorn waits for open responses; streams end, so the pod stops in seconds."""
    world = build_world()
    client = Client()
    client.force_login(world.users["viewer"])
    cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
    with django_db_blocker.unblock(), app_role() as role:
        env = server_env(role, tmp_path, IKI_SECRET_KEY=settings.SECRET_KEY)
        with serve(env) as server:
            host, port = server.url.removeprefix("http://").split(":")

            def connect() -> http.client.HTTPConnection:
                conn = http.client.HTTPConnection(host, int(port), timeout=10)
                conn.request("GET", "/api/v1/events", headers={"Cookie": f"iki_session={cookie}"})
                response = conn.getresponse()
                assert response.status == 200
                assert b"event: ready" in response.read1()
                return conn

            # A tab that closes frees its stream.
            connect().close()
            deadline = time.monotonic() + 10
            while '"reason": "disconnected"' not in server.text:
                assert time.monotonic() < deadline, server.text
                time.sleep(0.1)

            conn = connect()
            stopped = time.monotonic()
            server.process.send_signal(signal.SIGTERM)
            # uvicorn re-raises SIGTERM after shutting down, so the status reflects it.
            assert server.process.wait(timeout=20) in (0, -signal.SIGTERM)
            assert time.monotonic() - stopped < 10
            assert '"reason": "shutdown"' in server.text
            conn.close()
