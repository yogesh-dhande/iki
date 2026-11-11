"""One ASGI app answering inside another's request: the MCP SDK's app inside a Django
view, and Iki's API inside an MCP tool."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
#: An ASGI app: (scope, receive, send). Loosely typed, as Django and Starlette each
#: type theirs a little differently.
App = Callable[[Any, Any, Any], Awaitable[None]]


@dataclass
class Answer:
    status: int
    headers: list[tuple[bytes, bytes]]
    #: The body as the app sends it; reading it to the end lets the app finish.
    body: AsyncIterator[bytes]

    def header(self, name: str) -> str | None:
        wanted = name.lower().encode()
        return next((v.decode("latin-1") for k, v in self.headers if k.lower() == wanted), None)

    async def read(self) -> bytes:
        return b"".join([chunk async for chunk in self.body])


async def call(app: App, scope: Scope, body: bytes) -> Answer:
    """Sends ``app`` one request, and returns once it starts answering."""
    sent = False
    gone = asyncio.Event()
    queue: asyncio.Queue[Message | None] = asyncio.Queue()

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        await queue.put(message)

    async def run() -> None:
        try:
            await app(scope, receive, send)
        finally:
            await queue.put(None)

    task = asyncio.ensure_future(run())
    start = await queue.get()
    if start is None or start["type"] != "http.response.start":
        await task  # raises what the app raised
        raise RuntimeError("The app finished without answering.")

    async def chunks() -> AsyncIterator[bytes]:
        try:
            while (message := await queue.get()) is not None:
                if message["type"] == "http.response.body":
                    yield message.get("body", b"")
                    if not message.get("more_body", False):
                        break
        finally:
            gone.set()
            await task

    return Answer(status=start["status"], headers=list(start.get("headers", [])), body=chunks())
