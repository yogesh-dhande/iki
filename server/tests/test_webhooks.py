"""Webhooks: signing, where Iki will send, events from changes, delivery and its log."""

import datetime as dt
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from api_client import Api, problem
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.utils import timezone
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.audit.models import AuditEvent
from iki_server.notifications.models import Notification
from iki_server.org import services as org
from iki_server.org.models import ProjectRole
from iki_server.setup.models import Installation
from iki_server.tasks.models import TaskRecord
from iki_server.tasks.runner import Runner
from iki_server.webhooks import maintenance, sender, services, signing
from iki_server.webhooks.models import Delivery, DeliveryStatus, DisabledReason, Webhook
from iki_server.webhooks.tasks import ATTEMPTS, SCHEDULE

pytestmark = pytest.mark.django_db

LOOPBACK = [ipaddress.ip_network("127.0.0.0/8")]


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def local(settings: Any) -> None:
    """Lets Iki send to receivers on this machine, over plain HTTP, as development does."""
    settings.IKI_WEBHOOK_ALLOWED_NETWORKS = LOOPBACK
    settings.IKI_WEBHOOK_ALLOW_HTTP = True


class Receiver:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 204
        self.delay = 0.0
        self.location = ""


def _handler(receiver: Receiver) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            receiver.requests.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": body}
            )
            time.sleep(receiver.delay)
            self.send_response(receiver.status)
            if receiver.location:
                self.send_header("Location", receiver.location)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


@pytest.fixture
def receiver(local: None) -> Iterator[tuple[Receiver, str]]:
    found = Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(found))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield found, f"http://127.0.0.1:{server.server_address[1]}/hooks/iki?token=abc"
    server.shutdown()
    server.server_close()


def run_tasks() -> int:
    return Runner(queues=["default"]).run_ready()


def make(world: World, url: str, events: list[str], *, org_level: bool = False) -> Webhook:
    hook, _ = services.create(
        project=None if org_level else world.project,
        url=url,
        description="LIMS",
        chosen=events,
        by=world.users["org_admin" if org_level else "admin"],
    )
    return hook


# --- Signing --------------------------------------------------------------------


def test_signatures_match_the_standard_webhooks_reference() -> None:
    # The example from the Standard Webhooks specification.
    secret = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    body = b'{"test": 2432232314}'
    assert (
        signing.signature(secret, "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, body)
        == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
    )


def test_new_secrets_are_256_bits_and_differ() -> None:
    first, second = signing.new_secret(), signing.new_secret()
    assert first != second
    assert first.startswith("whsec_")
    assert len(signing._key(first)) == 32


# --- Where Iki sends --------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "172.16.3.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",  # noqa: S104 - an address Iki must refuse, not one it binds
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.5",  # IPv4-mapped
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "2002:0a00:0001::1",  # 6to4 of 10.0.0.1
        "not-an-address",
    ],
)
def test_private_and_special_addresses_are_refused(address: str) -> None:
    assert not sender.allowed(address)


@pytest.mark.parametrize("address", ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert sender.allowed(address)


def test_operators_can_allow_their_networks(settings: Any) -> None:
    settings.IKI_WEBHOOK_ALLOWED_NETWORKS = [ipaddress.ip_network("10.20.0.0/16")]
    assert sender.allowed("10.20.3.4")
    assert not sender.allowed("10.21.3.4")
    assert not sender.allowed("169.254.169.254")


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://hooks.example.com/x", "Use an https:// address."),
        ("ftp://hooks.example.com/x", "Use an https:// address"),
        ("https://user:pass@hooks.example.com/x", "Leave credentials out"),
        ("https://hooks.example.com/x#top", "Leave out the #fragment"),
        ("https://localhost/x", "other systems can reach"),
        ("https://api.localhost/x", "other systems can reach"),
        ("https://169.254.169.254/latest/meta-data", "public addresses"),
        ("https://[::1]/x", "public addresses"),
        ("https://hooks.example.com:99999/x", "port"),
        ("https:///x", "host name"),
    ],
)
def test_addresses_iki_never_sends_to_are_refused_when_saved(url: str, message: str) -> None:
    with pytest.raises(sender.Refused, match=message):
        sender.check_url(url)


def _resolving(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    def fake(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_a_name_resolving_to_a_private_address_is_refused_when_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolving(monkeypatch, "10.0.0.7")
    result = sender.post("https://rebind.example.com/x", b"{}", {})
    assert result.status_code is None
    assert "resolves to an address Iki doesn’t send to (10.0.0.7)" in result.error


def test_one_private_answer_among_public_ones_refuses_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resolving(monkeypatch, "93.184.215.14", "169.254.169.254")
    result = sender.post("https://mixed.example.com/x", b"{}", {})
    assert "169.254.169.254" in result.error


def test_a_name_that_doesnt_resolve_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    assert sender.post("https://gone.example.com/x", b"{}", {}).error == (
        "gone.example.com doesn’t resolve."
    )


def test_plain_http_needs_development_settings(settings: Any) -> None:
    settings.IKI_WEBHOOK_ALLOW_HTTP = False
    with pytest.raises(sender.Refused):
        sender.check_url("http://hooks.example.com/x")


# --- Sending -------------------------------------------------------------------------


def test_sends_json_to_the_address_given(receiver: tuple[Receiver, str]) -> None:
    found, url = receiver
    result = sender.post(url, b'{"a":1}', {"webhook-id": "msg_1"})
    assert result.ok and result.status_code == 204 and result.error == ""
    (request,) = found.requests
    assert request["path"] == "/hooks/iki?token=abc"
    assert request["body"] == b'{"a":1}'
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["User-Agent"] == "Iki-Webhooks/1"
    assert request["headers"]["webhook-id"] == "msg_1"


def test_redirects_are_failures_and_not_followed(receiver: tuple[Receiver, str]) -> None:
    found, url = receiver
    found.status, found.location = 302, "http://169.254.169.254/latest/meta-data"
    result = sender.post(url, b"{}", {})
    assert not result.ok
    assert result.status_code == 302
    assert "doesn’t follow redirects" in result.error
    assert len(found.requests) == 1


def test_error_statuses_are_failures(receiver: tuple[Receiver, str]) -> None:
    found, url = receiver
    found.status = 503
    result = sender.post(url, b"{}", {})
    assert (result.ok, result.status_code, result.error) == (
        False,
        503,
        "The receiver answered 503.",
    )


def test_slow_receivers_time_out(
    receiver: tuple[Receiver, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    found, url = receiver
    found.delay = 1.5
    monkeypatch.setattr(sender, "TIMEOUT_SECONDS", 0.5)
    result = sender.post(url, b"{}", {})
    assert result.status_code is None
    assert result.error == "No answer within 0 seconds."


def test_refused_connections_fail_cleanly(local: None) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    result = sender.post(f"http://127.0.0.1:{port}/", b"{}", {})
    assert result.status_code is None
    assert result.error == "The receiver refused the connection."


# --- HTTPS -------------------------------------------------------------------------------


def _certificates(directory: Path, name: str) -> tuple[Path, Path, Path]:
    """A private CA, and a certificate for ``name`` it signed."""
    now = dt.datetime.now(dt.UTC)

    def cert(subject: str, key: Any, issuer: Any, issuer_key: Any, ca: bool) -> x509.Certificate:
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=not ca,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=ca,
                    crl_sign=ca,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        if not ca:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(subject)]), critical=False
            )
        return builder.sign(issuer_key, hashes.SHA256())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example Bio Internal CA")])
    ca = cert("Example Bio Internal CA", ca_key, ca_name, ca_key, ca=True)
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = cert(name, leaf_key, ca_name, ca_key, ca=False)
    paths = directory / "ca.pem", directory / "leaf.pem", directory / "leaf.key"
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


@pytest.fixture
def tls_receiver(
    tmp_path: Path, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Receiver, str, Path]]:
    """An HTTPS receiver at lims.example.test, with a certificate from a private CA."""
    settings.IKI_WEBHOOK_ALLOWED_NETWORKS = LOOPBACK
    ca, leaf, key = _certificates(tmp_path, "lims.example.test")
    found = Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(found))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(leaf, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    _resolving(monkeypatch, "127.0.0.1")
    sender._ssl_context.cache_clear()
    yield found, f"https://lims.example.test:{port}/hooks", ca
    sender._ssl_context.cache_clear()
    server.shutdown()
    server.server_close()


def test_https_certificates_are_verified(tls_receiver: tuple[Receiver, str, Path]) -> None:
    found, url, _ = tls_receiver
    result = sender.post(url, b"{}", {})
    assert result.error == "The receiver’s TLS certificate isn’t trusted."
    assert found.requests == []


def test_an_internal_ca_can_be_trusted(
    tls_receiver: tuple[Receiver, str, Path], settings: Any
) -> None:
    found, url, ca = tls_receiver
    settings.IKI_WEBHOOK_CA_FILE = str(ca)
    result = sender.post(url, b"{}", {})
    assert result.ok, result.error
    # Connected to the address checked, with the real name for TLS and Host.
    assert found.requests[0]["headers"]["Host"] == url.split("/")[2]


# --- Events --------------------------------------------------------------------------


def deliveries(hook: Webhook) -> list[Delivery]:
    return list(hook.deliveries.exclude(event="ping").order_by("id"))


def test_project_changes_reach_the_projects_webhooks_and_the_orgs(world: World) -> None:
    mine = make(world, "https://hooks.example.com/p", ["project.updated"])
    theirs = make(world, "https://hooks.example.com/o", ["project.updated"], org_level=True)
    other = org.create_project(world.team, "Other Atlas", "", by=world.users["team_member"])
    elsewhere, _ = services.create(
        project=other,
        url="https://hooks.example.com/x",
        description="",
        chosen=["project.updated"],
        by=world.users["team_member"],
    )
    org.update_project(world.project, "Kinase Atlas 2", "Kinases", by=world.users["admin"])

    (sent,) = deliveries(mine)
    assert [d.payload for d in deliveries(theirs)] == [sent.payload]
    assert deliveries(elsewhere) == []
    assert sent.event == "project.updated"
    assert set(sent.payload) == {"type", "timestamp", "data"}
    data = sent.payload["data"]
    assert data["project"]["name"] == "Kinase Atlas 2"
    assert data["changes"]["name"] == {"from": "Kinase Atlas", "to": "Kinase Atlas 2"}
    assert data["actor"]["email"] == "admin@example.com"


def test_only_chosen_events_are_sent(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p", ["project.archived"])
    org.update_project(world.project, "Renamed", "", by=world.users["admin"])
    assert deliveries(hook) == []
    org.set_project_archived(world.project, True, by=world.users["admin"])
    assert [d.event for d in deliveries(hook)] == ["project.archived"]


def test_membership_changes_say_who_and_which_role(world: World) -> None:
    hook = make(
        world,
        "https://hooks.example.com/p",
        ["project.member_added", "project.member_role_changed", "project.member_removed"],
    )
    outsider = world.users["outsider"]
    org.grant_project_role(
        world.project, ProjectRole.VIEWER, user=outsider, by=world.users["admin"]
    )
    org.grant_project_role(
        world.project, ProjectRole.EDITOR, user=outsider, by=world.users["admin"]
    )
    membership = world.project.memberships.get(user=outsider)
    org.remove_project_membership(membership, by=world.users["admin"])
    org.grant_project_role(
        world.project, ProjectRole.VIEWER, team=world.team, by=world.users["admin"]
    )

    sent = deliveries(hook)
    assert [d.event for d in sent] == [
        "project.member_added",
        "project.member_role_changed",
        "project.member_removed",
        "project.member_added",
    ]
    added = sent[0].payload["data"]
    assert added["member"] == {
        "type": "user",
        "user": {
            "id": str(outsider.pk),
            "email": "outsider@example.com",
            "name": "Outsider",
            "kind": "person",
            "active": True,
        },
    }
    assert added["role"] == "viewer"
    assert sent[1].payload["data"]["previous_role"] == "viewer"
    assert sent[1].payload["data"]["role"] == "editor"
    assert sent[3].payload["data"]["member"]["team"]["slug"] == "structural-biology"


def test_org_events_reach_org_webhooks_only(world: World) -> None:
    hook = make(
        world,
        "https://hooks.example.com/o",
        ["team.member_added", "project.created", "org_role.granted", "user.deactivated"],
        org_level=True,
    )
    project_hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    org.add_team_member(world.team, world.users["outsider"], by=world.users["org_admin"])
    org.create_project(world.team, "Fresh", "", by=world.users["team_member"])
    org.set_active(world.users["editor"], False, by=world.users["org_admin"])
    sent = {d.event: d.payload["data"] for d in deliveries(hook)}
    assert set(sent) == {"team.member_added", "project.created", "user.deactivated"}
    assert sent["team.member_added"]["user"]["email"] == "outsider@example.com"
    assert sent["team.member_added"]["team"]["name"] == "Structural Biology"
    assert sent["user.deactivated"]["user"]["active"] is False
    assert deliveries(project_hook) == []


def test_project_webhooks_can_only_choose_project_events(world: World) -> None:
    with pytest.raises(services.Refused, match=r"team\.created") as refused:
        make(world, "https://hooks.example.com/p", ["team.created"])
    assert refused.value.field == "events"


def test_turned_off_webhooks_get_nothing(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    services.update(hook, by=world.users["admin"], active=False)
    org.update_project(world.project, "Renamed", "", by=world.users["admin"])
    assert deliveries(hook) == []


def test_a_rolled_back_change_sends_nothing(world: World) -> None:
    from django.db import transaction

    hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    with pytest.raises(RuntimeError), transaction.atomic():
        org.update_project(world.project, "Renamed", "", by=world.users["admin"])
        raise RuntimeError
    assert deliveries(hook) == []
    assert (
        not TaskRecord.objects.filter(task_path__endswith="deliver")
        .exclude(args__0__in=[str(d.pk) for d in hook.deliveries.all()])
        .exists()
    )


# --- Delivering ------------------------------------------------------------------------


def test_a_new_webhook_is_pinged_with_a_verifiable_signature(
    world: World, receiver: tuple[Receiver, str]
) -> None:
    found, url = receiver
    hook, secret = services.create(
        project=world.project,
        url=url,
        description="",
        chosen=["project.updated"],
        by=world.users["admin"],
    )
    assert run_tasks() == 1
    (request,) = found.requests
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert signing.verify(secret, headers, request["body"])
    assert abs(int(headers["webhook-timestamp"]) - time.time()) < 60
    body = json.loads(request["body"])
    assert body["type"] == "ping"
    assert body["data"]["webhook"]["id"] == str(hook.pk)
    assert body["data"]["project"]["slug"] == "kinase-atlas"
    (ping,) = hook.deliveries.all()
    assert headers["webhook-id"] == ping.message_id
    assert ping.status == DeliveryStatus.SUCCEEDED
    assert ping.attempts[0]["status_code"] == 204
    assert ping.finished_at is not None


def test_failures_retry_on_the_schedule_then_give_up(
    world: World, receiver: tuple[Receiver, str]
) -> None:
    found, url = receiver
    found.status = 500
    hook = make(world, url, ["project.updated"])
    run_tasks()
    (ping,) = hook.deliveries.all()
    assert ping.status == DeliveryStatus.PENDING
    task = TaskRecord.objects.get(args=[str(ping.pk)])
    assert task.status == "READY"
    delay = (task.run_after - timezone.now()).total_seconds()
    assert SCHEDULE[0].total_seconds() - 2 < delay <= SCHEDULE[0].total_seconds()
    hook.refresh_from_db()
    assert hook.failing_since is not None

    for _ in range(ATTEMPTS - 1):
        TaskRecord.objects.filter(pk=task.pk).update(run_after=timezone.now())
        run_tasks()
    ping.refresh_from_db()
    assert ping.status == DeliveryStatus.FAILED
    assert len(ping.attempts) == ATTEMPTS
    assert {a["error"] for a in ping.attempts} == {"The receiver answered 500."}
    assert len(found.requests) == ATTEMPTS


def test_a_success_clears_the_failing_mark(world: World, receiver: tuple[Receiver, str]) -> None:
    found, url = receiver
    found.status = 500
    hook = make(world, url, ["project.updated"])
    run_tasks()
    found.status = 200
    TaskRecord.objects.update(run_after=timezone.now())
    run_tasks()
    hook.refresh_from_db()
    assert hook.failing_since is None
    assert hook.deliveries.get().status == DeliveryStatus.SUCCEEDED


def test_webhooks_failing_for_days_are_turned_off_and_their_admins_told(
    world: World, receiver: tuple[Receiver, str]
) -> None:
    found, url = receiver
    found.status = 500
    hook = make(world, url, ["project.updated"])
    Webhook.objects.filter(pk=hook.pk).update(
        failing_since=timezone.now() - services.DISABLE_AFTER - dt.timedelta(minutes=1)
    )
    run_tasks()
    hook.refresh_from_db()
    assert not hook.active
    assert hook.disabled_reason == DisabledReason.FAILING
    told = Notification.objects.filter(kind="webhook.disabled")
    assert {n.recipient.email for n in told} == {"admin@example.com"}
    assert told[0].path == f"/projects/kinase-atlas/webhooks/{hook.pk}/"
    assert "127.0.0.1" in told[0].title
    event = AuditEvent.objects.get(action="webhook.disabled")
    assert event.actor is None and event.project == world.project
    # Nothing more is sent to it.
    TaskRecord.objects.update(run_after=timezone.now())
    sent = len(found.requests)
    run_tasks()
    assert len(found.requests) == sent


def test_deliveries_to_a_webhook_turned_off_meanwhile_stop(
    world: World, receiver: tuple[Receiver, str]
) -> None:
    found, url = receiver
    hook = make(world, url, ["project.updated"])
    services.update(hook, by=world.users["admin"], active=False)
    run_tasks()
    assert found.requests == []
    ping = hook.deliveries.get()
    assert ping.status == DeliveryStatus.FAILED
    assert ping.attempts[-1]["error"] == "The webhook is turned off."


def test_redelivering_sends_the_same_message_signed_afresh(
    world: World, receiver: tuple[Receiver, str]
) -> None:
    found, url = receiver
    hook = make(world, url, ["project.updated"])
    run_tasks()
    first = hook.deliveries.get()
    again = services.redeliver(first, by=world.users["admin"])
    run_tasks()
    assert again.pk != first.pk
    assert again.message_id == first.message_id
    assert again.redelivered_by == world.users["admin"]
    assert [r["body"] for r in found.requests] == [found.requests[0]["body"]] * 2
    assert AuditEvent.objects.filter(action="webhook.redelivered").count() == 1


def test_rotation_signs_with_both_secrets_for_a_day(
    world: World, receiver: tuple[Receiver, str]
) -> None:
    found, url = receiver
    hook, old = services.create(
        project=world.project,
        url=url,
        description="",
        chosen=["project.updated"],
        by=world.users["admin"],
    )
    new, expires = services.rotate_secret(hook, by=world.users["admin"])
    assert new != old
    assert expires - timezone.now() > dt.timedelta(hours=23)
    run_tasks()
    request = found.requests[-1]
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert len(headers["webhook-signature"].split()) == 2
    assert signing.verify(new, headers, request["body"])
    assert signing.verify(old, headers, request["body"])

    Webhook.objects.filter(pk=hook.pk).update(previous_secret_expires_at=timezone.now())
    services.ping(hook, by=world.users["admin"])
    run_tasks()
    request = found.requests[-1]
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert signing.verify(new, headers, request["body"])
    assert not signing.verify(old, headers, request["body"])


def test_secrets_are_encrypted_at_rest(world: World) -> None:
    hook, secret = services.create(
        project=world.project,
        url="https://hooks.example.com/p",
        description="",
        chosen=["project.updated"],
        by=world.users["admin"],
    )
    hook.refresh_from_db()
    assert secret not in hook.secret
    assert services.secrets_for(hook) == [secret]


# --- Governance -------------------------------------------------------------------------


def test_org_admins_can_turn_project_webhooks_off_and_only_they_back_on(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    services.update(hook, by=world.users["org_admin"], active=False)
    hook.refresh_from_db()
    assert hook.disabled_reason == DisabledReason.ORG_ADMIN
    with pytest.raises(services.Refused, match="only they can turn it on"):
        services.update(hook, by=world.users["admin"], active=True)
    with pytest.raises(services.Refused, match="not change it"):
        services.update(hook, by=world.users["org_admin"], description="mine now")
    services.update(hook, by=world.users["org_admin"], active=True)
    hook.refresh_from_db()
    assert hook.active and hook.disabled_reason == DisabledReason.NONE


def test_turning_off_project_webhooks_for_the_org_stops_them(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    services.set_project_webhooks_allowed(False, by=world.users["org_admin"])
    org.update_project(world.project, "Renamed", "", by=world.users["admin"])
    assert deliveries(hook) == []
    with pytest.raises(services.Refused, match="only lets Org Admins"):
        make(world, "https://hooks.example.com/q", ["project.updated"])
    # Turning one off, or deleting it, is still allowed.
    services.update(hook, by=world.users["admin"], active=False)
    services.delete(hook, by=world.users["admin"])
    assert AuditEvent.objects.filter(action="webhooks.project_policy_changed").exists()
    assert not Installation.get().project_webhooks


def test_audit_records_the_host_not_the_address(world: World) -> None:
    make(world, "https://hooks.example.com/p?token=s3cret", ["project.updated"])
    event = AuditEvent.objects.get(action="webhook.created")
    assert event.data == {"host": "hooks.example.com", "events": ["project.updated"]}
    assert "s3cret" not in event.target_label


def test_there_is_a_limit_per_project(world: World) -> None:
    for n in range(services.MAX_WEBHOOKS):
        make(world, f"https://hooks.example.com/{n}", ["project.updated"])
    with pytest.raises(services.Refused, match="already 20 webhooks"):
        make(world, "https://hooks.example.com/one-more", ["project.updated"])


# --- The API -------------------------------------------------------------------------------


def test_the_secret_is_shown_once(world: World) -> None:
    api = Api(world.browser("admin"))
    created = api.post(
        f"/projects/{world.project.pk}/webhooks",
        {"url": "https://hooks.example.com/p", "events": ["project.updated"]},
    )
    assert created.status_code == 201, created.content
    body = created.json()
    assert body["secret"].startswith("whsec_")
    assert body["last_delivery"]["event"] == "ping"
    shown = api.get(f"/webhooks/{body['id']}").json()
    assert "secret" not in shown
    listed = api.get(f"/projects/{world.project.pk}/webhooks").json()["items"]
    assert [w["id"] for w in listed] == [body["id"]]
    assert "secret" not in listed[0]


def test_refused_addresses_point_at_the_field(world: World) -> None:
    api = Api(world.browser("admin"))
    refused = api.post(
        f"/projects/{world.project.pk}/webhooks",
        {"url": "https://10.0.0.1/hook", "events": ["project.updated"]},
    )
    found = problem(refused, 409, "refused")
    assert found["errors"][0]["pointer"] == "#/url"


def test_deliveries_show_what_was_sent(world: World, receiver: tuple[Receiver, str]) -> None:
    _, url = receiver
    hook = make(world, url, ["project.updated"])
    run_tasks()
    api = Api(world.browser("admin"))
    (summary,) = api.get(f"/webhooks/{hook.pk}/deliveries").json()["items"]
    assert summary["status"] == "succeeded"
    assert "payload" not in summary
    detail = api.get(f"/webhooks/{hook.pk}/deliveries/{summary['id']}").json()
    assert detail["payload"]["type"] == "ping"
    assert detail["attempts"][0]["status_code"] == 204
    again = api.post(f"/webhooks/{hook.pk}/deliveries/{summary['id']}/redeliver")
    assert again.status_code == 202
    assert again.json()["message_id"] == summary["message_id"]
    assert api.get(f"/webhooks/{hook.pk}/deliveries", status="failed").json()["items"] == []


def test_org_admins_see_where_project_webhooks_send_but_not_what(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p?token=s3cret", ["project.updated"])
    api = Api(world.browser("org_admin"))
    (listed,) = api.get("/project-webhooks").json()["items"]
    assert listed["host"] == "hooks.example.com"
    assert "url" not in listed
    assert "s3cret" not in json.dumps(listed)
    assert api.get(f"/webhooks/{hook.pk}/deliveries").status_code == 404
    off = api.patch(f"/webhooks/{hook.pk}", {"active": False})
    assert off.status_code == 200
    assert off.json()["disabled_reason"] == "org_admin"
    assert off.json()["url"] == "https://hooks.example.com/…"
    assert "s3cret" not in off.content.decode()


def test_tokens_need_the_webhooks_scope(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    _, tokens = signed_in_token(world, "admin", scope="read write")
    refused = bearer(tokens["access_token"], "GET", f"/webhooks/{hook.pk}")
    problem(refused, 403, "insufficient_scope")
    listed = bearer(tokens["access_token"], "GET", f"/projects/{world.project.pk}/webhooks")
    problem(listed, 403, "insufficient_scope")
    _, tokens = signed_in_token(world, "admin", scope="read webhooks")
    assert bearer(tokens["access_token"], "GET", f"/webhooks/{hook.pk}").status_code == 200


def test_the_event_catalog_lists_each_scopes_events(world: World) -> None:
    api = Api(world.browser("viewer"))
    project_events = {e["type"] for e in api.get("/webhook-events").json()}
    org_events = {e["type"] for e in api.get("/webhook-events", scope="org").json()}
    assert "project.updated" in project_events and "team.created" not in project_events
    assert {"team.created", "project.created", "user.created"} <= org_events
    assert "ping" not in project_events | org_events


# --- Housekeeping ---------------------------------------------------------------------------


def test_old_deliveries_are_deleted_and_stale_ones_closed(world: World) -> None:
    hook = make(world, "https://hooks.example.com/p", ["project.updated"])
    ping = hook.deliveries.get()
    Delivery.objects.filter(pk=ping.pk).update(created_at=timezone.now() - dt.timedelta(days=3))
    assert maintenance.prune() == 0
    ping.refresh_from_db()
    assert ping.status == DeliveryStatus.FAILED
    assert ping.attempts[-1]["error"] == "Iki gave up."
    Delivery.objects.filter(pk=ping.pk).update(created_at=timezone.now() - dt.timedelta(days=31))
    assert maintenance.prune() == 1
