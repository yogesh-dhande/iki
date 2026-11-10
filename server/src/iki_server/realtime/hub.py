"""One LISTEN connection per web process, fanning events out to its open streams.

The hub starts with the first stream and holds a single dedicated Postgres
connection however many streams are open. If the connection drops, it
reconnects with backoff and tells every stream to ``resync``: events may
have been missed, so pages refetch what they show.
"""

import asyncio
import contextlib
import json
import weakref
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import psycopg
import structlog
from django.db import connection

from iki_server.realtime.events import CHANNEL

logger = structlog.get_logger("iki.realtime")

#: Events a stream holds before it gives up on them and asks for a resync.
QUEUE_SIZE = 100


@dataclass(eq=False)
class Stream:
    user_id: str
    queue: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE)
    )

    def put(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A stuck client: drop what's queued and have it refetch instead.
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait({"event": "resync", "data": {}})


@dataclass(eq=False)
class Hub:
    streams: dict[str, set[Stream]] = field(default_factory=lambda: defaultdict(set))
    #: Set when the server is shutting down: streams end so it can stop.
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    #: Set while LISTEN is active (tests wait for it).
    listening: asyncio.Event = field(default_factory=asyncio.Event)
    _listener: asyncio.Task[None] | None = None

    def subscribe(self, user_id: UUID) -> Stream:
        stream = Stream(str(user_id))
        self.streams[stream.user_id].add(stream)
        if self._listener is None or self._listener.done():
            self._listener = asyncio.get_running_loop().create_task(self._listen())
        return stream

    def unsubscribe(self, stream: Stream) -> None:
        mine = self.streams.get(stream.user_id)
        if mine is not None:
            mine.discard(stream)
            if not mine:
                del self.streams[stream.user_id]

    def count(self, user_id: UUID | None = None) -> int:
        if user_id is not None:
            return len(self.streams.get(str(user_id), ()))
        return sum(len(s) for s in self.streams.values())

    def deliver(self, payload: str) -> None:
        try:
            message = json.loads(payload)
            user, event, data = message["user"], message["event"], message.get("data", {})
        except (ValueError, KeyError, TypeError):
            logger.warning("realtime_bad_payload")
            return
        for stream in list(self.streams.get(user, ())):
            stream.put({"event": event, "data": data})

    def broadcast(self, event: str) -> None:
        for streams in list(self.streams.values()):
            for stream in list(streams):
                stream.put({"event": event, "data": {}})

    async def _listen(self) -> None:
        delay, connected_before = 1.0, False
        while not self.closing.is_set():
            try:
                params = _listen_params()
                async with await psycopg.AsyncConnection.connect(**params, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    self.listening.set()
                    delay = 1.0
                    if connected_before:
                        self.broadcast("resync")
                    connected_before = True
                    logger.info("realtime_listening")
                    async for notify in conn.notifies():
                        self.deliver(notify.payload)
            except (psycopg.Error, OSError) as error:
                logger.warning("realtime_listen_error", error=str(error), retry_in=delay)
            finally:
                self.listening.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.closing.wait(), delay)
            delay = min(delay * 2, 30.0)

    async def close(self) -> None:
        self.closing.set()
        if self._listener is not None:
            self._listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener


def _listen_params() -> dict[str, Any]:
    params = connection.get_connection_params()
    for key in ("cursor_factory", "context", "pool"):
        params.pop(key, None)
    params["application_name"] = "iki-server-realtime"
    return params


#: One hub per event loop (a server has one; tests may make several).
_hubs: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Hub]" = weakref.WeakKeyDictionary()


def hub() -> Hub:
    loop = asyncio.get_running_loop()
    found = _hubs.get(loop)
    if found is None:
        found = _hubs[loop] = Hub()
    return found


def shutting_down() -> None:
    """Ends every stream in this process, so the server can stop. Call from the loop."""
    for each in list(_hubs.values()):
        each.closing.set()
