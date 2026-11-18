"""The egress proxy's side of outside APIs (ADR 0130).

For a host that's an outside API of the attempt's action, the proxy doesn't tunnel. It
answers the CONNECT, ends the TLS connection itself with the certificate the attempt's own
CA signed for that host and the proxy's key, and reads each HTTP/1.1 request. For each, it
checks again that the attempt holds its lease and that the project's approval stands,
adds the API's key from the run secrets, asks for the answer uncompressed, and makes the
call over TLS it verifies, to an address it checked. The answer goes back as it comes,
streamed answers too. Each call is recorded: never its body, but SHA-256 hashes of what
went and came, what the answer says answered, the declared parameters and usage, and the
cost.

It also polls the jobs runs parked on (:func:`Gateway.poll`), with the same keys and
records, and asks APIs to cancel the jobs of runs that ended while parked.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import ssl
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import h11
import structlog
from cryptography.hazmat.primitives import serialization

from iki_server.runs import egress, outside
from iki_spec import apis as spec_apis

if TYPE_CHECKING:
    from iki_server.runs.egress_proxy import Proxy, Tunnel

logger = structlog.get_logger("iki.egress")

CHUNK = 64 * 1024
#: How long a call may go without a byte either way.
IDLE_TIMEOUT = 300.0
HANDSHAKE_TIMEOUT = 10.0
#: How much of a request or answer is read for what it reports.
READ_LIMIT = outside.MAX_ANSWER
#: Streamed answers' events kept for what they report: the first and the last.
FIRST_EVENTS, LAST_EVENTS = 20, 100
#: How often an idle poller looks for due parks, and a proxy says it has its key.
POLL_IDLE = 2.0
REGISTER_EVERY = 300.0
#: Headers that are the connection's, not the request's or answer's.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
        "expect",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _db[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    from iki_server.runs.egress_proxy import _db as call

    return await call(function, *args, **kwargs)


class _Reading:
    """What's kept of a body as it passes: its size and hash, and up to READ_LIMIT of it,
    or a streamed answer's first and last events, for what it reports."""

    def __init__(self, *, events: bool = False) -> None:
        self.size = 0
        self.hash = hashlib.sha256()
        self.kept = bytearray()
        self.events = events
        self._line = bytearray()
        self.first: list[Any] = []
        self.last: collections.deque[Any] = collections.deque(maxlen=LAST_EVENTS)

    def add(self, data: bytes) -> None:
        self.size += len(data)
        self.hash.update(data)
        if self.events:
            self._lines(data)
        elif len(self.kept) <= READ_LIMIT:
            self.kept += data[: READ_LIMIT + 1 - len(self.kept)]

    def _lines(self, data: bytes) -> None:
        self._line += data
        *lines, rest = self._line.split(b"\n")
        self._line = bytearray(rest[:READ_LIMIT])
        for line in lines:
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if len(self.first) < FIRST_EVENTS:
                self.first.append(event)
            else:
                self.last.append(event)

    def documents(self) -> list[Any]:
        """The JSON it held: the document, or the streamed events in order."""
        if self.events:
            return [*self.first, *self.last]
        if not self.kept or len(self.kept) > READ_LIMIT:
            return []
        try:
            return [json.loads(self.kept)]
        except ValueError:
            return []


async def _next(connection: h11.Connection, reader: asyncio.StreamReader) -> Any:  # noqa: ANN401 - h11's events
    while True:
        event = connection.next_event()
        if event is not h11.NEED_DATA:
            return event
        data = await asyncio.wait_for(reader.read(CHUNK), IDLE_TIMEOUT)
        connection.receive_data(data)


def _send(connection: h11.Connection, writer: asyncio.StreamWriter, event: Any) -> None:  # noqa: ANN401
    data = connection.send(event)
    if data:
        writer.write(data)


def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is not None:
        with contextlib.suppress(Exception):
            writer.close()


def _plain(status: int, text: str) -> list[Any]:
    body = f"{text}\n".encode()
    return [
        h11.Response(
            status_code=status,
            headers=[
                ("content-type", "text/plain; charset=utf-8"),
                ("content-length", str(len(body))),
                ("connection", "close"),
            ],
        ),
        h11.Data(data=body),
        h11.EndOfMessage(),
    ]


@dataclass
class _Upstream:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    connection: h11.Connection = field(default_factory=lambda: h11.Connection(h11.CLIENT))


class Gateway:
    """The proxy's key and what it does with it: serve outside APIs' calls, and poll."""

    def __init__(self, key_file: str, *, client_tls: ssl.SSLContext | None = None) -> None:
        self.key_file = key_file
        key = serialization.load_pem_private_key(Path(key_file).read_bytes(), password=None)
        public = key.public_key()
        self.public_pem = public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        self.fingerprint = outside.fingerprint(public)
        self.client_tls = client_tls or ssl.create_default_context()
        self.client_tls.set_alpn_protocols(["http/1.1"])
        self._contexts: dict[tuple[uuid.UUID, str], ssl.SSLContext] = {}

    # --- Serving a command's calls ------------------------------------------------------------

    def _context(self, job_id: uuid.UUID, host: str, certificate: str) -> ssl.SSLContext:
        found = self._contexts.get((job_id, host))
        if found is not None:
            return found
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.set_alpn_protocols(["http/1.1"])
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as file:
            file.write(certificate)
            file.flush()
            context.load_cert_chain(file.name, self.key_file)

        def named(_: Any, name: str | None, __: Any) -> int | None:  # noqa: ANN401
            # Only the host it's for: another name on this connection is refused.
            return None if name is None or egress.normal(name) == host else 112

        context.sni_callback = named
        if len(self._contexts) > 1000:
            self._contexts.clear()
        self._contexts[(job_id, host)] = context
        return context

    async def serve(
        self,
        proxy: Proxy,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        tunnel: Tunnel,
        name: str,
    ) -> None:
        """Serves one CONNECT to an outside API's host: TLS with the command, then its calls."""
        from iki_server.runs.egress_proxy import Refused, response

        certificate = await _db(outside.leaf, tunnel.job_id, self.fingerprint, tunnel.host)
        if certificate is None:
            why = (
                f"This attempt has no certificate for {tunnel.host} from this proxy. It was "
                "leased before the proxy started: it can call the API from its next attempt."
            )
            await proxy.refuse(writer, Refused(502, why), tunnel)
            return
        writer.write(response(200))
        await writer.drain()
        try:
            await asyncio.wait_for(
                writer.start_tls(self._context(tunnel.job_id, tunnel.host, certificate)),
                HANDSHAKE_TIMEOUT,
            )
        except (ssl.SSLError, OSError, TimeoutError) as error:
            why = (
                f"The command didn't finish a TLS handshake for {tunnel.host} ({error}). It must "
                "trust the CA in SSL_CERT_FILE, as Iki sets it."
            )
            await _db(egress.record, tunnel.job_id, tunnel.host, tunnel.port, why[:300])
            logger.info("egress.api_handshake_failed", job_id=str(tunnel.job_id), reason=why)
            return
        await _db(egress.record, tunnel.job_id, tunnel.host, tunnel.port)
        logger.info("egress.api_connected", job_id=str(tunnel.job_id), host=tunnel.host)
        client = h11.Connection(h11.SERVER)
        upstream: _Upstream | None = None
        try:
            while True:
                event = await _next(client, reader)
                if isinstance(event, h11.ConnectionClosed):
                    return
                if not isinstance(event, h11.Request):
                    return
                upstream = await self._call(
                    proxy, client, reader, writer, upstream, tunnel, name, event
                )
                await writer.drain()
                if client.our_state is h11.DONE and client.their_state is h11.DONE:
                    client.start_next_cycle()
                else:
                    return
        except (h11.ProtocolError, asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            return
        finally:
            if upstream is not None:
                _close(upstream.writer)

    async def _call(
        self,
        proxy: Proxy,
        client: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: _Upstream | None,
        tunnel: Tunnel,
        name: str,
        request: h11.Request,
    ) -> _Upstream | None:
        """One call: checked, sent on with the key, answered, recorded. Returns the
        connection to the API, to use again, or None if it can't be."""
        method = request.method.decode("ascii", "replace")
        path = request.target.decode("ascii", "replace").split("?", 1)[0]
        started, clock = _now(), time.monotonic()
        target = await _db(outside.target, tunnel.job_id, name)

        def refuse(status: int, why: str) -> None:
            # Its body, if it has one, isn't read: the answer closes the connection.
            for event in _plain(status, why):
                _send(client, writer, event)

        if target.refused or target.api is None:
            refuse(403, target.refused or "Not allowed.")
            await _db(egress.record, tunnel.job_id, tunnel.host, tunnel.port, target.refused[:300])
            logger.info("egress.api_refused", job_id=str(tunnel.job_id), reason=target.refused)
            if upstream is not None:
                _close(upstream.writer)
            return None
        api = target.api
        headers = [(k.decode().lower(), v.decode("latin-1")) for k, v in request.headers]
        if method == "CONNECT" or any(k == "upgrade" for k, _ in headers):
            refuse(501, "Calls to outside APIs are plain HTTP/1.1 requests: no upgrades.")
            return upstream
        credential = target.credential
        dropped = {*HOP_BY_HOP, "accept-encoding"}
        if credential is not None:
            dropped.add(credential[0].lower())
        sending = [(k, v) for k, v in headers if k not in dropped]
        sending.append(("accept-encoding", "identity"))
        if credential is not None:
            sending.append((credential[0], credential[1]))
        if client.they_are_waiting_for_100_continue:
            _send(client, writer, h11.InformationalResponse(status_code=100, headers=[]))
        sent = _Reading()
        call = outside.Call(
            job_id=tunnel.job_id,
            api=api.qualified_name,
            host=tunnel.host,
            kind="call",
            method=method,
            path=path,
            started_at=started,
            duration_ms=0,
        )
        try:
            if upstream is None:
                upstream = await self._open(proxy, tunnel.host)
            _send(
                upstream.connection,
                upstream.writer,
                h11.Request(method=request.method, target=request.target, headers=sending),
            )
            while True:
                event = await _next(client, reader)
                if isinstance(event, h11.Data):
                    sent.add(bytes(event.data))
                    tunnel.sent += len(event.data)
                    _send(upstream.connection, upstream.writer, h11.Data(data=event.data))
                    await upstream.writer.drain()
                elif isinstance(event, h11.EndOfMessage):
                    _send(upstream.connection, upstream.writer, h11.EndOfMessage())
                    await upstream.writer.drain()
                    break
                else:
                    raise h11.RemoteProtocolError("The request ended early.")
            answer = await _next(upstream.connection, upstream.reader)
            while isinstance(answer, h11.InformationalResponse):
                answer = await _next(upstream.connection, upstream.reader)
            if not isinstance(answer, h11.Response):
                raise ConnectionError("The API closed the connection without answering.")
        except Exception as error:
            from iki_server.runs.egress_proxy import Refused

            if not isinstance(error, (Refused, OSError, TimeoutError, h11.ProtocolError)):
                logger.exception("egress.api_call_failed", job_id=str(tunnel.job_id))
            why = error.why if isinstance(error, Refused) else f"{type(error).__name__}: {error}"
            if client.our_state in (h11.IDLE, h11.SEND_RESPONSE):
                refuse(502, f"Couldn't call {api.label}: {why}")
            call.error = why
            call.request_bytes, call.request_sha256 = sent.size, sent.hash.hexdigest()
            call.duration_ms = int((time.monotonic() - clock) * 1000)
            await _db(outside.record, call)
            if upstream is not None:
                _close(upstream.writer)
            return None
        answer_headers = [(k.decode().lower(), v.decode("latin-1")) for k, v in answer.headers]
        streamed = any(
            k == "content-type" and v.lower().startswith("text/event-stream")
            for k, v in answer_headers
        )
        got = _Reading(events=streamed)
        _send(
            client,
            writer,
            h11.Response(
                status_code=answer.status_code,
                headers=[(k, v) for k, v in answer_headers if k not in HOP_BY_HOP],
                reason=answer.reason,
            ),
        )
        await writer.drain()
        try:
            while True:
                event = await _next(upstream.connection, upstream.reader)
                if isinstance(event, h11.Data):
                    got.add(bytes(event.data))
                    tunnel.received += len(event.data)
                    _send(client, writer, h11.Data(data=event.data))
                    await writer.drain()
                elif isinstance(event, h11.EndOfMessage):
                    _send(client, writer, h11.EndOfMessage())
                    break
                else:
                    raise ConnectionError("The API closed the connection mid-answer.")
        except (OSError, TimeoutError, h11.ProtocolError, ConnectionError) as error:
            call.error = f"The answer was cut short: {error}"[:500]
            _close(upstream.writer)
            upstream = None
        tunnel.active_at = time.monotonic()
        lowered = dict(answer_headers)
        call.status = answer.status_code
        call.request_bytes, call.request_sha256 = sent.size, sent.hash.hexdigest()
        call.response_bytes, call.response_sha256 = got.size, got.hash.hexdigest()
        call.duration_ms = int((time.monotonic() - clock) * 1000)
        found = outside.reported(api, sent.documents(), got.documents(), lowered)
        call.model, call.version = found["model"], found["version"]
        call.parameters, call.usage = found["parameters"], found["usage"]
        call.charged = 200 <= answer.status_code < 300
        await _db(outside.record, call)
        logger.info(
            "egress.api_call",
            job_id=str(tunnel.job_id),
            api=api.qualified_name,
            method=method,
            status=answer.status_code,
        )
        if upstream is not None:
            state = upstream.connection
            if state.our_state is h11.DONE and state.their_state is h11.DONE:
                state.start_next_cycle()
            else:
                _close(upstream.writer)
                upstream = None
        return upstream

    async def _open(self, proxy: Proxy, host: str) -> _Upstream:
        reader, writer = await proxy.connect(host, 443)
        try:
            await asyncio.wait_for(
                writer.start_tls(self.client_tls, server_hostname=host), HANDSHAKE_TIMEOUT
            )
        except BaseException:
            _close(writer)
            raise
        return _Upstream(reader, writer)

    # --- Polling parked runs' jobs ------------------------------------------------------------

    async def register(self, stop: asyncio.Event) -> None:
        """Says this proxy has its key, at start and every few minutes, so leases sign
        attempts' certificates for it."""
        while not stop.is_set():
            try:
                await _db(outside.register_key, self.public_pem)
            except Exception:
                logger.exception("egress.gateway_register_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), REGISTER_EVERY)

    async def poll(self, proxy: Proxy, stop: asyncio.Event) -> None:
        """Polls parked runs' jobs as they fall due, and cancels those of runs that ended."""
        while not stop.is_set():
            try:
                due = await _db(outside.take_due)
            except Exception:
                logger.exception("egress.poll_failed")
                due = []
            if due:
                await asyncio.gather(*(self._act(proxy, d) for d in due), return_exceptions=True)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), POLL_IDLE)

    async def _act(self, proxy: Proxy, due: outside.Due) -> None:
        jobs = due.api.jobs
        assert jobs is not None  # noqa: S101 - only parks on APIs with jobs are due
        cancelling = due.what == "cancel"
        target = await _db(outside.target, due.job_id, due.name, park=True, cancelling=cancelling)
        template = jobs.cancel if cancelling else jobs.poll
        path = spec_apis.job_path(template, due.external_id)
        call = outside.Call(
            job_id=due.job_id,
            api=due.api.qualified_name,
            host=due.api.host,
            kind=due.what,
            method="POST" if cancelling else "GET",
            path=path,
            started_at=_now(),
            duration_ms=0,
            park_id=due.park_id,
        )
        answer: Any = None
        headers: dict[str, str] = {}
        if target.refused:
            call.error = target.refused[:500]
        else:
            answer, headers = await self._request(proxy, target, call)
        if cancelling:
            await _db(outside.cancelled, due.park_id, call)
        else:
            await _db(outside.polled, due.park_id, call, answer, headers)

    async def _request(
        self, proxy: Proxy, target: outside.Target, call: outside.Call
    ) -> tuple[Any, dict[str, str]]:
        """Makes the poller's own call, filling in `call`. Returns the answer's JSON, if it
        was JSON and not too large, and its headers."""
        api = target.api
        assert api is not None  # noqa: S101 - not refused
        clock = time.monotonic()
        headers = [
            ("host", api.host),
            ("user-agent", "Iki"),
            ("accept", "application/json"),
            ("accept-encoding", "identity"),
        ]
        if call.method == "POST":
            headers.append(("content-length", "0"))
        if target.credential is not None:
            headers.append(target.credential)
        upstream: _Upstream | None = None
        got = _Reading()
        lowered: dict[str, str] = {}
        try:
            upstream = await self._open(proxy, api.host)
            connection = upstream.connection
            _send(
                connection,
                upstream.writer,
                h11.Request(method=call.method, target=call.path, headers=headers),
            )
            _send(connection, upstream.writer, h11.EndOfMessage())
            await upstream.writer.drain()
            answer = await _next(connection, upstream.reader)
            while isinstance(answer, h11.InformationalResponse):
                answer = await _next(connection, upstream.reader)
            if not isinstance(answer, h11.Response):
                raise ConnectionError("The API closed the connection without answering.")
            lowered = {k.decode().lower(): v.decode("latin-1") for k, v in answer.headers}
            while True:
                event = await _next(connection, upstream.reader)
                if isinstance(event, h11.Data):
                    got.add(bytes(event.data))
                elif isinstance(event, h11.EndOfMessage):
                    break
                else:
                    raise ConnectionError("The API closed the connection mid-answer.")
            call.status = answer.status_code
        except Exception as error:
            from iki_server.runs.egress_proxy import Refused

            call.error = (
                error.why if isinstance(error, Refused) else f"{type(error).__name__}: {error}"
            )[:500]
        finally:
            if upstream is not None:
                _close(upstream.writer)
        call.duration_ms = int((time.monotonic() - clock) * 1000)
        call.request_sha256 = hashlib.sha256(b"").hexdigest()
        call.response_bytes, call.response_sha256 = got.size, got.hash.hexdigest()
        documents = got.documents()
        if documents:
            found = outside.reported(api, [], documents, lowered, usage={})
            call.model, call.version = found["model"], found["version"]
        return (documents[0] if documents else None), lowered


def load(key_file: str) -> Gateway | None:
    """The gateway for the proxy's key file, if it has one."""
    if not key_file:
        return None
    return Gateway(key_file)


__all__ = ["Gateway", "load"]
