"""Wakes workers waiting in ``POST /worker/v1/lease`` when a job is queued on their tier.

Like the realtime hub, each web process holds one dedicated LISTEN
connection however many workers are waiting, and none while nobody is.
A waiter that misses a notification (the connection dropped) loses nothing:
it looks again when its wait times out, and every waiter is woken when the
connection comes back.
"""

import asyncio
import contextlib
import weakref
from dataclasses import dataclass, field
from typing import Any

import psycopg
import structlog
from django.db import connection

from iki_server.runs.queue import CHANNEL

logger = structlog.get_logger("iki.runs")


@dataclass(eq=False)
class Waker:
    #: tier → the event its waiters wait on; replaced each time it's set.
    events: dict[str, asyncio.Event] = field(default_factory=dict)
    waiting: int = 0
    #: Set while LISTEN is active (tests wait for it).
    listening: asyncio.Event = field(default_factory=asyncio.Event)
    closing: asyncio.Event = field(default_factory=asyncio.Event)
    _listener: asyncio.Task[None] | None = None

    async def wait(self, tier: str, timeout: float) -> bool:
        """Waits up to `timeout` seconds for a job on `tier`. True if one was queued."""
        if self._listener is None or self._listener.done():
            self._listener = asyncio.get_running_loop().create_task(self._listen())
        event = self.events.setdefault(tier, asyncio.Event())
        self.waiting += 1
        queued = asyncio.ensure_future(event.wait())
        closing = asyncio.ensure_future(self.closing.wait())
        try:
            await asyncio.wait(
                {queued, closing}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            return event.is_set()
        finally:
            queued.cancel()
            closing.cancel()
            self.waiting -= 1

    def wake(self, tier: str | None) -> None:
        """Wakes the tier's waiters, or everyone's for None."""
        tiers = list(self.events) if tier is None else [tier]
        for each in tiers:
            event = self.events.pop(each, None)
            if event is not None:
                event.set()

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
                        self.wake(None)
                    connected_before = True
                    async for notify in conn.notifies():
                        self.wake(notify.payload)
            except (psycopg.Error, OSError) as error:
                logger.warning("runs_listen_error", error=str(error), retry_in=delay)
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
    params["application_name"] = "iki-server-jobs"
    return params


_wakers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Waker]" = weakref.WeakKeyDictionary()


def waker() -> Waker:
    loop = asyncio.get_running_loop()
    found = _wakers.get(loop)
    if found is None:
        found = _wakers[loop] = Waker()
    return found


def shutting_down() -> None:
    """Ends every wait in this process, so the server can stop. Call from the loop."""
    for each in list(_wakers.values()):
        each.closing.set()
