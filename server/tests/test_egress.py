"""Egress (ADR 0053): runs reach the hosts their action declares, through the egress proxy,
and nothing else. What each attempt reached, or was refused, is kept for its run."""

import asyncio
import base64
import contextlib
import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from api_client import problem
from django.test import override_settings
from org_world import World
from test_api_entities import api_as
from test_runs import launch, world
from test_workers import keep_connection, worker

from iki_server.runs import egress, egress_proxy
from iki_server.runs.models import Job, JobEgress, JobState

__all__ = ["keep_connection", "world"]  # the fixtures, used here too

FETCH = "protein.fetch_proteins"
HOST = "rest.uniprot.org"
PROXY = "http://iki-egress-proxy:3128"


def fetching(world: World) -> dict[str, Any]:
    """A fetch run, leased: its job as the worker got it."""
    made = launch(api_as(world, "runner"), world.project, {}, FETCH, params={"accession": "P00519"})
    assert made.status_code == 201, made.content
    job: dict[str, Any] = worker(world).lease().json()
    return job


def authorization(job: dict[str, Any]) -> str:
    secret = f"{job['job']['id']}:{job['job']['lease_token']}"
    return "Basic " + base64.b64encode(secret.encode()).decode()


# --- Which runs get the proxy ------------------------------------------------------------------


@pytest.mark.django_db
def test_actions_that_reach_the_internet_cant_run_without_the_proxy(world: World) -> None:
    (action,) = [a for a in api_as(world).get("/actions").json() if a["name"] == FETCH]
    assert action["network"] == [HOST]
    assert action["available"] is False
    assert action["unavailable_reason"] == (
        "It reaches rest.uniprot.org on the internet, and this deployment gives runs no "
        "network access. An Iki administrator can turn on the egress proxy."
    )
    refused = launch(api_as(world, "runner"), world.project, {}, FETCH, params={"accession": "X"})
    assert problem(refused, 409, "refused")["detail"] == action["unavailable_reason"]


@pytest.mark.django_db
@override_settings(IKI_EGRESS_PROXY_URL=PROXY)
def test_runs_that_declare_hosts_get_the_proxy_with_their_attempts_credentials(
    world: World,
) -> None:
    job = fetching(world)
    assert job["job"]["proxy"] == (
        f"http://{job['job']['id']}:{job['job']['lease_token']}@iki-egress-proxy:3128"
    )
    assert egress.decide(authorization(job), HOST, 443).allowed


# --- What the proxy lets through ---------------------------------------------------------------


@pytest.mark.django_db
@override_settings(IKI_EGRESS_PROXY_URL=PROXY)
def test_the_proxy_lets_an_attempt_reach_only_what_its_action_declares(world: World) -> None:
    job = fetching(world)
    given = authorization(job)
    assert egress.decide(given, "REST.uniprot.org.", 443).allowed
    refusals = {
        ("www.uniprot.org", 443, True): (
            403,
            "protein.fetch_proteins doesn't declare www.uniprot.org. An action reaches only "
            "the hosts its plugin lists under `network`.",
        ),
        (HOST, 80, True): (403, "Runs reach hosts over HTTPS, on port 443, only."),
        (HOST, 80, False): (403, "Runs reach hosts over HTTPS only: use an https:// address."),
    }
    for (host, port, tunnel), expected in refusals.items():
        decision = egress.decide(given, host, port, tunnel=tunnel)
        assert (decision.status, decision.refused) == expected
    kept = JobEgress.objects.filter(job_id=job["job"]["id"]).values_list("host", "port", "refused")
    assert sorted(kept) == sorted((h, p, why) for (h, p, _), (_, why) in refusals.items())

    # Credentials that aren't an attempt's, or an attempt that has ended, reach nothing.
    assert egress.decide(None, HOST, 443).status == 407
    assert egress.decide("Basic bm90OnVzZWZ1bA==", HOST, 443).status == 407
    Job.objects.filter(pk=job["job"]["id"]).update(state=JobState.SUCCEEDED)
    ended = egress.decide(given, HOST, 443)
    assert (ended.status, ended.refused) == (
        407,
        "These credentials are for an attempt that has ended.",
    )


@pytest.mark.parametrize(
    ("host", "covered"),
    [
        ("eutils.ncbi.nlm.nih.gov", True),
        ("a.b.ncbi.nlm.nih.gov", True),
        ("ncbi.nlm.nih.gov", False),
        ("evilncbi.nlm.nih.gov", False),
        ("rest.uniprot.org", True),
        ("rest.uniprot.org.evil.com", False),
    ],
)
def test_a_wildcard_covers_subdomains_only(host: str, covered: bool) -> None:
    assert egress.declares(["*.ncbi.nlm.nih.gov", "rest.uniprot.org"], host) is covered


# --- The proxy, serving ------------------------------------------------------------------------


def client_hello(host: str) -> bytes:
    """The first bytes a TLS client sends to `host`."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=host)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


class Remote:
    """A server standing in for a host on the internet: it answers whatever comes first."""

    def __init__(self, answer: bytes = b"hello") -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.got: list[bytes] = []
        self.answer = answer
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                peer, _ = self.socket.accept()
            except OSError:
                return
            with peer:
                self.got.append(peer.recv(65536))
                peer.sendall(self.answer)
                # Held open until the other side closes, as a real server keeps a connection.
                with contextlib.suppress(OSError):
                    while peer.recv(65536):
                        pass


@pytest.fixture
def serving() -> Iterator[Callable[..., int]]:
    """Starts a proxy in a thread of its own; returns its port."""
    stops: list[tuple[asyncio.AbstractEventLoop, asyncio.Event, threading.Thread]] = []

    def start(**options: Any) -> int:
        proxy = egress_proxy.Proxy(**options)
        bound: list[int] = []
        ready = threading.Event()
        loop = asyncio.new_event_loop()
        stop = asyncio.Event()

        def on_ready(port: int) -> None:
            bound.append(port)
            ready.set()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                proxy.serve(
                    "127.0.0.1",
                    0,
                    stop=stop,
                    grace=1,
                    ready=on_ready,
                )
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert ready.wait(10)
        stops.append((loop, stop, thread))
        return bound[0]

    yield start
    for loop, stop, thread in stops:
        loop.call_soon_threadsafe(stop.set)
        thread.join(10)


def connect(port: int, head: str) -> tuple[socket.socket, bytes]:
    conversation = socket.create_connection(("127.0.0.1", port), timeout=10)
    conversation.sendall(head.encode())
    answer = b""
    while b"\r\n\r\n" not in answer:
        chunk = conversation.recv(4096)
        if not chunk:
            break
        answer += chunk
    return conversation, answer


def tunnel_to(port: int, job: dict[str, Any], host: str = HOST) -> tuple[socket.socket, bytes]:
    return connect(
        port,
        f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n"
        f"Proxy-Authorization: {authorization(job)}\r\n\r\n",
    )


def to(remote: Remote) -> Callable[[str, int], Any]:
    async def resolve(host: str, port: int) -> list[tuple[str, int]]:
        return [("127.0.0.1", remote.port)]

    return resolve


def until(check: Callable[[], bool], seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]


@pytest.mark.django_db(transaction=True)
@override_settings(IKI_EGRESS_PROXY_URL=PROXY)
def test_the_proxy_tunnels_tls_to_a_declared_host_and_counts_it(
    world: World, serving: Callable[..., int]
) -> None:
    job = fetching(world)
    remote = Remote()
    port = serving(networks=LOOPBACK, resolve=to(remote))
    conversation, answer = tunnel_to(port, job)
    with conversation:
        assert answer.startswith(b"HTTP/1.1 200 ")
        hello = client_hello(HOST)
        conversation.sendall(hello)
        assert conversation.recv(5) == b"hello"
    assert remote.got == [hello]
    until(lambda: JobEgress.objects.filter(job_id=job["job"]["id"], bytes_received=5).exists())
    row = JobEgress.objects.get(job_id=job["job"]["id"])
    assert (row.host, row.port, row.refused, row.connections) == (HOST, 443, "", 1)
    assert row.bytes_sent == len(hello)


@pytest.mark.django_db(transaction=True)
@override_settings(IKI_EGRESS_PROXY_URL=PROXY)
def test_the_proxy_refuses_what_it_shouldnt_reach_and_says_why(
    world: World, serving: Callable[..., int]
) -> None:
    job = fetching(world)
    remote = Remote()

    # A host that resolves inside the firewall.
    inside = serving(resolve=to(remote))
    conversation, answer = tunnel_to(inside, job)
    conversation.close()
    assert answer.startswith(b"HTTP/1.1 403 ")
    assert b"rest.uniprot.org resolves to an address runs can't reach (127.0.0.1)" in answer

    port = serving(networks=LOOPBACK, resolve=to(remote))
    # A TLS handshake for another host, through a tunnel to a declared one.
    conversation, answer = tunnel_to(port, job)
    with conversation:
        assert answer.startswith(b"HTTP/1.1 200 ")
        conversation.sendall(client_hello("evil.example.com"))
        assert conversation.recv(5) == b""
    # Plain HTTP, and no credentials.
    conversation, answer = connect(
        port,
        f"GET http://{HOST}/ HTTP/1.1\r\nHost: {HOST}\r\n"
        f"Proxy-Authorization: {authorization(job)}\r\n\r\n",
    )
    conversation.close()
    assert answer.startswith(b"HTTP/1.1 403 ")
    conversation, answer = connect(port, f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n")
    conversation.close()
    assert answer.startswith(b"HTTP/1.1 407 ")
    assert b'Proxy-Authenticate: Basic realm="Iki egress"' in answer
    # Addresses, not names.
    conversation, answer = connect(port, "CONNECT 93.184.216.34:443 HTTP/1.1\r\n\r\n")
    conversation.close()
    assert b"Name the host" in answer

    # The handshake for another host never reached the one connected to.
    until(lambda: bool(remote.got))
    assert remote.got == [b""]
    refused = set(
        JobEgress.objects.filter(job_id=job["job"]["id"]).values_list("refused", flat=True)
    )
    assert refused == {
        "rest.uniprot.org resolves to an address runs can't reach (127.0.0.1). Ask your Iki "
        "administrator if it's inside your network.",
        "The connection to rest.uniprot.org didn't start a TLS handshake for it (it named "
        "evil.example.com).",
        "Runs reach hosts over HTTPS only: use an https:// address.",
    }


@pytest.mark.django_db(transaction=True)
@override_settings(IKI_EGRESS_PROXY_URL=PROXY)
def test_a_tunnel_closes_once_its_attempt_ends(
    world: World, serving: Callable[..., int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(egress_proxy, "CHECK_EVERY", 0.1)
    job = fetching(world)
    port = serving(networks=LOOPBACK, resolve=to(Remote()))
    conversation, answer = tunnel_to(port, job)
    with conversation:
        assert answer.startswith(b"HTTP/1.1 200 ")
        conversation.sendall(client_hello(HOST))
        assert conversation.recv(5) == b"hello"
        Job.objects.filter(pk=job["job"]["id"]).update(state=JobState.CANCELLED)
        assert conversation.recv(5) == b""


class UpstreamProxy(Remote):
    """A corporate proxy: it takes CONNECT, with its own credentials, then answers."""

    def _serve(self) -> None:
        while True:
            try:
                peer, _ = self.socket.accept()
            except OSError:
                return
            with peer:
                head = b""
                while b"\r\n\r\n" not in head:
                    head += peer.recv(4096)
                self.got.append(head)
                peer.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                peer.recv(65536)
                peer.sendall(self.answer)
                with contextlib.suppress(OSError):
                    while peer.recv(65536):
                        pass


@pytest.mark.django_db(transaction=True)
@override_settings(IKI_EGRESS_PROXY_URL=PROXY)
def test_the_proxy_can_go_out_through_a_corporate_proxy(
    world: World, serving: Callable[..., int]
) -> None:
    job = fetching(world)
    corporate = UpstreamProxy()
    port = serving(upstream=f"http://iki:s%40cret@127.0.0.1:{corporate.port}")
    conversation, answer = tunnel_to(port, job)
    with conversation:
        assert answer.startswith(b"HTTP/1.1 200 ")
        conversation.sendall(client_hello(HOST))
        assert conversation.recv(5) == b"hello"
    (head,) = corporate.got
    assert head.startswith(b"CONNECT rest.uniprot.org:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic " + base64.b64encode(b"iki:s@cret") in head


def test_reads_the_host_a_tls_client_hello_names() -> None:
    hello = client_hello("eutils.ncbi.nlm.nih.gov")
    assert egress_proxy.server_name(hello[5:]) == "eutils.ncbi.nlm.nih.gov"
    assert egress_proxy.server_name(b"\x01\x00") is None
    assert egress_proxy.server_name(b"GET / HTTP/1.1") is None
