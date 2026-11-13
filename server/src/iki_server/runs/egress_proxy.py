"""The egress proxy: how runs reach the internet, and only what their action declares
(ADR 0053).

Workers' pods reach nothing on the internet themselves (their NetworkPolicy). A run whose
action declares hosts gets ``HTTPS_PROXY`` pointing here, with its attempt's job ID and
lease token as credentials. For each ``CONNECT host:443`` the proxy:

- checks the credentials belong to an attempt that holds its lease, and that its action
  declares the host (:mod:`iki_server.runs.egress`);
- resolves the host itself and connects only to public addresses (or networks the
  operator allowed), to the address it checked, so a rebinding answer can't reach
  inside the firewall. With an upstream proxy, it asks that proxy instead;
- reads the TLS ClientHello and closes the tunnel unless it names that host, so a
  connection to an allowed host can't be steered to another on the same address;
- closes the tunnel as soon as the attempt ends, or after five idle minutes;
- keeps, with the attempt, each host reached or refused, with connections and bytes.

Plain HTTP is refused: everything a run fetches is encrypted end to end, and the proxy
never sees it.
"""

import asyncio
import base64
import contextlib
import ipaddress
import signal
import socket
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import structlog
from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections

from iki_server.core import addresses
from iki_server.runs import egress

logger = structlog.get_logger("iki.egress")

HEAD_LIMIT = 8 * 1024
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
HELLO_TIMEOUT = 10.0
HELLO_LIMIT = 64 * 1024
IDLE_TIMEOUT = 300.0
CHECK_EVERY = 15.0
#: Tunnels one attempt may hold open through one proxy at once.
MAX_TUNNELS = 32
CHUNK = 64 * 1024

REASONS = {
    200: "Connection established",
    400: "Bad Request",
    403: "Forbidden",
    407: "Proxy Authentication Required",
    429: "Too Many Requests",
    502: "Bad Gateway",
}


class Refused(Exception):  # noqa: N818 - what happened: the proxy refused
    def __init__(self, status: int, why: str) -> None:
        super().__init__(why)
        self.status = status
        self.why = why


def response(status: int, why: str = "") -> bytes:
    body = f"{why}\n".encode() if why else b""
    lines = [f"HTTP/1.1 {status} {REASONS[status]}"]
    if status != 200:
        lines += [
            "Content-Type: text/plain; charset=utf-8",
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
    if status == 407:
        lines.append('Proxy-Authenticate: Basic realm="Iki egress"')
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    headers: dict[str, str]


def parse_head(data: bytes) -> Request:
    lines = data.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise Refused(400, "That isn't an HTTP request.")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, colon, value = line.partition(":")
        if not colon:
            raise Refused(400, "That isn't an HTTP request.")
        headers[name.strip().lower()] = value.strip()
    return Request(parts[0].upper(), parts[1], headers)


def connect_target(target: str) -> tuple[str, int]:
    """The host and port of ``CONNECT host:port``. Hosts are named, never addresses: an
    action declares names."""
    host, colon, port = target.rpartition(":")
    if not colon or not port.isdigit() or not 0 < int(port) < 65536 or not host:
        raise Refused(400, "CONNECT needs a host and port, such as example.com:443.")
    bare = host.strip("[]")
    with contextlib.suppress(ValueError):
        ipaddress.ip_address(bare)
        raise Refused(403, "Name the host: runs reach hosts by name, not by address.")
    return egress.normal(host), int(port)


def server_name(hello: bytes) -> str | None:
    """The host a TLS ClientHello (its handshake message) names in its SNI extension."""
    try:
        if hello[0] != 0x01:
            return None
        length = int.from_bytes(hello[1:4])
        body = hello[4 : 4 + length]
        at = 2 + 32  # version, random
        at += 1 + body[at]  # session ID
        at += 2 + int.from_bytes(body[at : at + 2])  # cipher suites
        at += 1 + body[at]  # compression methods
        end = at + 2 + int.from_bytes(body[at : at + 2])
        at += 2
        while at + 4 <= end:
            kind = int.from_bytes(body[at : at + 2])
            size = int.from_bytes(body[at + 2 : at + 4])
            at += 4
            if kind == 0:  # server_name
                names = body[at : at + size]
                cursor = 2
                while cursor + 3 <= len(names):
                    name_type = names[cursor]
                    name_length = int.from_bytes(names[cursor + 1 : cursor + 3])
                    cursor += 3
                    if name_type == 0:
                        return egress.normal(names[cursor : cursor + name_length].decode("ascii"))
                    cursor += name_length
                return None
            at += size
    except (IndexError, UnicodeDecodeError):
        return None
    return None


async def read_hello(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """The TLS records that carry the client's ClientHello, as they came (to send on),
    and the handshake message they carry. Empty when it isn't TLS."""
    raw = bytearray()
    message = bytearray()
    while len(raw) < HELLO_LIMIT:
        header = await reader.readexactly(5)
        raw += header
        if header[0] != 0x16:  # handshake
            return bytes(raw), b""
        size = int.from_bytes(header[3:5])
        fragment = await reader.readexactly(size)
        raw += fragment
        message += fragment
        if len(message) >= 4 and len(message) >= 4 + int.from_bytes(message[1:4]):
            return bytes(raw), bytes(message)
    return bytes(raw), b""


@dataclass
class Tunnel:
    job_id: uuid.UUID
    host: str
    port: int
    #: Whether `job_id` is a setup build's (ADR 0075).
    setup: bool = False
    sent: int = 0
    received: int = 0
    active_at: float = field(default_factory=time.monotonic)


async def _copy(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    tunnel: Tunnel,
    *,
    upstream: bool,
) -> None:
    while data := await reader.read(CHUNK):
        writer.write(data)
        await writer.drain()
        tunnel.active_at = time.monotonic()
        if upstream:
            tunnel.sent += len(data)
        else:
            tunnel.received += len(data)
    if writer.can_write_eof():
        with contextlib.suppress(OSError):
            writer.write_eof()


def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is not None:
        with contextlib.suppress(Exception):
            writer.close()


class Proxy:
    def __init__(
        self,
        *,
        upstream: str = "",
        networks: list[addresses.Network] | None = None,
        resolve: Callable[[str, int], Awaitable[list[tuple[str, int]]]] | None = None,
    ) -> None:
        self.upstream = urlsplit(upstream) if upstream else None
        self.networks = list(networks or ())
        self._resolve = resolve or self._getaddrinfo
        self.open: Counter[uuid.UUID] = Counter()
        self.tunnels: set[asyncio.Task[None]] = set()

    # --- Each connection ------------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None  # noqa: S101 - start_server runs this in a task
        self.tunnels.add(task)
        try:
            await self._handle(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            self.tunnels.discard(task)
            _close(writer)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            request = parse_head(head)
        except (asyncio.LimitOverrunError, ValueError):
            await self._refuse(writer, Refused(400, "The request's head is too long."))
            return
        except Refused as refusal:
            await self._refuse(writer, refusal)
            return
        authorization = request.headers.get("proxy-authorization")
        try:
            if request.method != "CONNECT":
                parts = urlsplit(request.target)
                decision = await _db(
                    egress.decide,
                    authorization,
                    parts.hostname or "",
                    parts.port or 80,
                    tunnel=False,
                )
                raise Refused(decision.status, decision.refused)
            host, port = connect_target(request.target)
            decision = await _db(egress.decide, authorization, host, port)
            if not decision.allowed:
                raise Refused(decision.status, decision.refused)
        except Refused as refusal:
            await self._refuse(writer, refusal)
            return
        assert decision.job_id is not None  # noqa: S101 - an allowed decision names it
        await self._tunnel(
            reader, writer, Tunnel(decision.job_id, host, port, setup=decision.setup), authorization
        )

    async def _refuse(
        self, writer: asyncio.StreamWriter, refusal: Refused, tunnel: Tunnel | None = None
    ) -> None:
        if tunnel is not None:
            await _db(
                egress.record,
                tunnel.job_id,
                tunnel.host,
                tunnel.port,
                refusal.why,
                setup=tunnel.setup,
            )
        logger.info(
            "egress.refused",
            status=refusal.status,
            reason=refusal.why,
            job_id=str(tunnel.job_id) if tunnel else None,
            host=tunnel.host if tunnel else None,
        )
        writer.write(response(refusal.status, refusal.why))
        with contextlib.suppress(ConnectionError):
            await writer.drain()

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        tunnel: Tunnel,
        authorization: str | None,
    ) -> None:
        if self.open[tunnel.job_id] >= MAX_TUNNELS:
            why = f"This attempt already has {MAX_TUNNELS} connections open through the proxy."
            await self._refuse(writer, Refused(429, why), tunnel)
            return
        self.open[tunnel.job_id] += 1
        remote: asyncio.StreamWriter | None = None
        try:
            try:
                remote_reader, remote = await self._connect(tunnel.host, tunnel.port)
            except Refused as refusal:
                await self._refuse(writer, refusal, tunnel)
                return
            writer.write(response(200))
            await writer.drain()
            raw, hello = await asyncio.wait_for(read_hello(reader), HELLO_TIMEOUT)
            named = server_name(hello) if hello else None
            if named != tunnel.host:
                why = f"The connection to {tunnel.host} didn't start a TLS handshake for it" + (
                    f" (it named {named})." if named else "."
                )
                await _db(
                    egress.record, tunnel.job_id, tunnel.host, tunnel.port, why, setup=tunnel.setup
                )
                logger.info("egress.refused", job_id=str(tunnel.job_id), reason=why)
                return
            await _db(egress.record, tunnel.job_id, tunnel.host, tunnel.port, setup=tunnel.setup)
            logger.info("egress.connected", job_id=str(tunnel.job_id), host=tunnel.host)
            remote.write(raw)
            await remote.drain()
            tunnel.sent += len(raw)
            await self._pipe(reader, writer, remote_reader, remote, tunnel, authorization)
        finally:
            self.open[tunnel.job_id] -= 1
            if self.open[tunnel.job_id] <= 0:
                del self.open[tunnel.job_id]
            _close(remote)
            if tunnel.sent or tunnel.received:
                await _db(
                    egress.count,
                    tunnel.job_id,
                    tunnel.host,
                    tunnel.port,
                    tunnel.sent,
                    tunnel.received,
                    setup=tunnel.setup,
                )

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remote_reader: asyncio.StreamReader,
        remote: asyncio.StreamWriter,
        tunnel: Tunnel,
        authorization: str | None,
    ) -> None:
        copies: asyncio.Future[Any] = asyncio.gather(
            _copy(reader, remote, tunnel, upstream=True),
            _copy(remote_reader, writer, tunnel, upstream=False),
        )
        watch: asyncio.Future[Any] = asyncio.ensure_future(self._watch(tunnel, authorization))
        try:
            await asyncio.wait((copies, watch), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (copies, watch):
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, ConnectionError, OSError):
                await copies
            with contextlib.suppress(asyncio.CancelledError):
                await watch
        logger.info(
            "egress.closed",
            job_id=str(tunnel.job_id),
            host=tunnel.host,
            sent=tunnel.sent,
            received=tunnel.received,
        )

    async def _watch(self, tunnel: Tunnel, authorization: str | None) -> None:
        """Returns, closing the tunnel, once it's idle too long or its attempt has ended."""
        while True:
            await asyncio.sleep(CHECK_EVERY)
            if time.monotonic() - tunnel.active_at > IDLE_TIMEOUT:
                return
            held = await _db(egress.still_held, tunnel.job_id, authorization, setup=tunnel.setup)
            if not held:
                return

    # --- Reaching the host ----------------------------------------------------------------

    async def _getaddrinfo(self, host: str, port: int) -> list[tuple[str, int]]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return list(dict.fromkeys((str(info[4][0]), port) for info in infos))

    async def _connect(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.upstream is not None:
            return await self._through_upstream(host, port)
        try:
            found = await asyncio.wait_for(self._resolve(host, port), CONNECT_TIMEOUT)
        except (OSError, TimeoutError):
            raise Refused(502, f"{host} doesn't resolve.") from None
        if not found:
            raise Refused(502, f"{host} doesn't resolve.")
        for address, _ in found:
            if not addresses.allowed(address, self.networks):
                # All or nothing: a mix of public and private answers is an attack.
                raise Refused(
                    403,
                    f"{host} resolves to an address runs can't reach ({address}). Ask your "
                    "Iki administrator if it's inside your network.",
                )
        last: Exception | None = None
        for address, at in found:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, at), CONNECT_TIMEOUT)
            except (OSError, TimeoutError) as error:
                last = error
        raise Refused(502, f"Couldn't connect to {host}: {_describe(last)}.")

    async def _through_upstream(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        upstream = self.upstream
        assert upstream is not None and upstream.hostname and upstream.port  # noqa: S101
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(upstream.hostname, upstream.port), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError) as error:
            raise Refused(502, f"Couldn't reach the upstream proxy: {_describe(error)}.") from None
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if upstream.username is not None:
            secret = f"{unquote(upstream.username)}:{unquote(upstream.password or '')}"
            lines.append(f"Proxy-Authorization: Basic {base64.b64encode(secret.encode()).decode()}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        try:
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            _close(writer)
            raise Refused(502, f"The upstream proxy didn't answer for {host}.") from None
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if status.split(" ")[1:2] != ["200"]:
            _close(writer)
            raise Refused(502, f"The upstream proxy refused {host}: {status}.")
        return reader, writer

    # --- Serving --------------------------------------------------------------------------

    async def serve(
        self,
        host: str,
        port: int,
        *,
        stop: asyncio.Event,
        grace: float = 25.0,
        ready: Callable[[int], None] | None = None,
    ) -> None:
        server = await asyncio.start_server(self.handle, host, port, limit=HEAD_LIMIT)
        bound = server.sockets[0].getsockname()[1]
        logger.info("egress.listening", host=host, port=bound)
        if ready is not None:
            ready(bound)
        async with server:
            await stop.wait()
            server.close()
            if self.tunnels:
                logger.info("egress.draining", tunnels=len(self.tunnels))
                _, pending = await asyncio.wait(set(self.tunnels), timeout=grace)
                for task in pending:
                    task.cancel()


async def _db[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Runs a database call in a thread, as the proxy's loop must never block on one."""

    def call() -> R:
        close_old_connections()
        return function(*args, **kwargs)

    return await sync_to_async(call, thread_sensitive=False)()


def _describe(error: Exception | None) -> str:
    if isinstance(error, TimeoutError):
        return "timed out"
    if isinstance(error, OSError) and error.strerror:
        return error.strerror.lower()
    return str(error) if error else "no address answered"


def run(host: str, port: int, *, grace: float) -> None:
    """Serves until SIGTERM or Ctrl-C, then lets open tunnels finish for `grace` seconds."""
    proxy = Proxy(
        upstream=settings.IKI_EGRESS_UPSTREAM_PROXY,
        networks=settings.IKI_EGRESS_ALLOWED_NETWORKS,
    )

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await proxy.serve(host, port, stop=stop, grace=grace)

    asyncio.run(main())
