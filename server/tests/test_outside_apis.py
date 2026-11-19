"""Outside APIs (ADR 0130): Org Admins approve each one per project, people consent to what's
sent before launching, the egress proxy calls the API with the organization's key and
records each call, and runs waiting on a long job park, polled, without holding a worker.

No test calls a real outside API: a stand-in serves HTTPS on localhost."""

import base64
import hashlib
import hmac
import json
import socket
import ssl
import struct
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from api_client import problem
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from django.test import Client, override_settings
from django.utils import timezone
from org_world import World, build_world
from test_api_entities import api_as
from test_egress import LOOPBACK, PROXY, authorization, connect, serving, until
from test_files import run_tasks
from test_oauth import bearer, signed_in_token
from test_run_results import _upload
from test_runs import launch, protein
from test_workers import keep_connection, worker

from iki_server.audit import log as audit
from iki_server.audit.models import AuditEvent, Via
from iki_server.org.services import Refused
from iki_server.runs import api_gateway, budgets, catalog, outside, reuse, services
from iki_server.runs.models import (
    ActionRun,
    ApiApproval,
    ApiCall,
    ApiConsent,
    Budget,
    CostKind,
    CostRecord,
    Job,
    JobState,
    ParkOutcome,
    RunOutput,
    RunPark,
    RunStatus,
)

__all__ = ["keep_connection", "serving"]  # the fixtures, used here too

ACTION = "acme/protein.predict_hosted"
API = "acme/protein.fold"
HOST = "fold.models.example"
KEY = "fold-test-key"
HOOK = base64.b64encode(b"a webhook secret, 32 bytes long!").decode()
PDB = "ATOM      1  N   MET A   1      11.104  13.207   2.100  1.00  0.00           N\n"
STATEMENT = (
    "This sends your data to FoldCloud (fold.models.example): each Protein's sequence and the "
    "recycles param. Predicts protein structures with FoldCloud's hosted models."
)


# --- The deployment ----------------------------------------------------------------------------


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An egress proxy with a gateway key, the API's key and webhook secret in the run
    secrets, and a price for its calls. Returns the gateway key's file."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "acme__protein.FOLD_API_KEY").write_text(f"{KEY}\n")
    (secrets / "acme__protein.FOLD_WEBHOOK_SECRET").write_text(f"whsec_{HOOK}")
    compute = json.loads(Path(catalog._compute_file()).read_text())  # type: ignore[arg-type]
    compute["prices"] = {
        API: {
            "api": API,
            "currency": "USD",
            "per_call": 0.02,
            "usage": {"gpu_seconds": 0.001},
        }
    }
    file = tmp_path / "compute.json"
    file.write_text(json.dumps(compute))
    monkeypatch.setenv("IKI_COMPUTE_FILE", str(file))
    catalog.compute.cache_clear()
    gateway = tmp_path / "gateway.pem"
    gateway.write_bytes(_pem(_key()))
    with override_settings(
        IKI_EGRESS_PROXY_URL=PROXY,
        IKI_RUN_SECRETS_DIR=str(secrets),
        IKI_PUBLIC_URL="https://iki.example",
        IKI_EGRESS_GATEWAY_KEY_FILE=str(gateway),
    ):
        yield gateway
    catalog.compute.cache_clear()


@pytest.fixture
def world(deployment: Path) -> World:
    made = build_world()
    outside.register_key(api_gateway.Gateway(str(deployment)).public_pem)
    return made


def approve(world: World) -> None:
    answer = api_as(world, "org_admin").post(
        f"/outside-apis/{API}/approvals",
        {"project": str(world.project.pk), "note": "Our DPA with FoldCloud covers it."},
    )
    assert answer.status_code == 201, answer.content


def hosted(world: World, principal: str = "editor", **body: Any) -> Any:
    api = api_as(world, principal)
    made = protein(api, world.project, name=uuid.uuid4().hex[:6].translate(_LETTERS))
    return launch(api, world.project, {"protein": made["id"]}, ACTION, **body)


_LETTERS = str.maketrans("0123456789abcdef", "ACDEFGHIKLMNPQRS")


# --- The catalog -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_action_says_what_it_sends_and_where(world: World) -> None:
    action = api_as(world, "viewer").get(f"/actions/{ACTION}").json()
    assert action["available"] is True, action["unavailable_reason"]
    assert action["apis"] == [
        {
            "api": API,
            "label": "FoldCloud",
            "host": HOST,
            "purpose": "Predicts protein structures with FoldCloud's hosted models.",
            "inputs": {"protein": ["sequence"]},
            "params": ["recycles"],
            "statement": STATEMENT,
            "step": None,
            "priced": True,
            "parks": True,
        }
    ]


@pytest.mark.django_db
def test_an_action_cant_run_without_its_key_or_a_gateway(world: World, deployment: Path) -> None:
    from django.conf import settings

    Path(settings.IKI_RUN_SECRETS_DIR, "acme__protein.FOLD_API_KEY").unlink()
    action = api_as(world).get(f"/actions/{ACTION}").json()
    assert action["available"] is False
    assert "`acme__protein.FOLD_API_KEY`" in action["unavailable_reason"]
    Path(settings.IKI_RUN_SECRETS_DIR, "acme__protein.FOLD_API_KEY").write_text(KEY)
    from iki_server.runs.models import EgressGatewayKey

    EgressGatewayKey.objects.update(last_seen_at=timezone.now() - timedelta(hours=1))
    action = api_as(world).get(f"/actions/{ACTION}").json()
    assert "gateway key" in action["unavailable_reason"]


# --- Approval ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_launch_waits_for_an_org_admin_to_approve_the_api(world: World) -> None:
    refused = problem(hosted(world, consent=[API]), 409, "outside_api_not_approved")
    assert refused["detail"].startswith(
        f"FoldCloud ({HOST}) isn't approved for {world.project.name}. An Org Admin approves "
        "outside APIs in Settings → Outside APIs: "
    )
    assert refused["apis"] == [API]
    assert world.users["org_admin"].name in refused["approvers"]

    # Only Org Admins approve, and only in the web app: no token, so no agent.
    problem(
        api_as(world, "admin").post(
            f"/outside-apis/{API}/approvals", {"project": str(world.project.pk)}
        ),
        403,
        "forbidden",
    )
    _, tokens = signed_in_token(world, "org_admin", scope="read write admin")
    answer = bearer(
        tokens["access_token"],
        "POST",
        f"/outside-apis/{API}/approvals",
        {"project": str(world.project.pk)},
    )
    assert answer.status_code == 403 and answer.json()["code"] == "web_only"

    approve(world)
    event = AuditEvent.objects.get(action="outside_api.approved")
    assert event.data == {
        "api": API,
        "host": HOST,
        "purpose": "Predicts protein structures with FoldCloud's hosted models.",
    }
    assert hosted(world, consent=[API]).status_code == 201

    listed = api_as(world, "org_admin").get("/outside-apis").json()
    (fold,) = [a for a in listed if a["api"] == API]
    assert fold["credential"]["set"] is True
    assert fold["price"] == {"per_call": 0.02, "usage": {"gpu_seconds": 0.001}, "currency": "USD"}
    assert [a["project_name"] for a in fold["approvals"]] == [world.project.name]
    assert fold["approvals"][0]["current"] is True
    problem(api_as(world, "viewer").get("/outside-apis"), 403, "forbidden")
    mine = api_as(world, "viewer").get(f"/projects/{world.project.pk}/outside-apis").json()
    assert [a["approvals"][0]["current"] for a in mine if a["api"] == API] == [True]


@pytest.mark.django_db
def test_an_approval_stops_counting_once_the_api_names_another_host_or_purpose(
    world: World,
) -> None:
    approve(world)
    ApiApproval.objects.update(purpose="Something else.")
    problem(hosted(world, consent=[API]), 409, "outside_api_not_approved")
    approve(world)  # again, for the purpose it names now
    assert ApiApproval.objects.filter(revoked_at__isnull=True).count() == 1
    assert hosted(world, consent=[API]).status_code == 201


@pytest.mark.django_db
def test_revoking_refuses_launches_and_works_with_a_token(world: World) -> None:
    approve(world)
    _, tokens = signed_in_token(world, "org_admin", scope="read write admin")
    gone = bearer(
        tokens["access_token"], "DELETE", f"/outside-apis/{API}/approvals/{world.project.pk}"
    )
    assert gone.status_code == 204, gone.content
    assert AuditEvent.objects.filter(action="outside_api.revoked").count() == 1
    problem(hosted(world, consent=[API]), 409, "outside_api_not_approved")


# --- Consent -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_launching_needs_consent_to_what_is_sent(world: World) -> None:
    approve(world)
    refused = problem(hosted(world), 409, "consent_required")
    assert refused["detail"].startswith(STATEMENT)
    assert [s["statement"] for s in refused["consent"]] == [STATEMENT]
    assert ActionRun.objects.count() == 0

    made = hosted(world, consent=[API])
    assert made.status_code == 201, made.content
    run = made.json()
    assert run["apis"] == [
        {
            "api": API,
            "label": "FoldCloud",
            "host": HOST,
            "purpose": "Predicts protein structures with FoldCloud's hosted models.",
        }
    ]
    assert run["consent"]["given_by"]["id"] == str(world.users["editor"].pk)
    assert run["consent"]["via"] == Via.WEB
    assert run["consent"]["agent"] is None
    assert [s["statement"] for s in run["consent"]["statements"]] == [STATEMENT]
    assert AuditEvent.objects.filter(action="outside_api.consented").count() == 1
    # Nothing in the consent can change afterwards.
    assert getattr(ApiConsent, "APPEND_ONLY", False) is True


@pytest.mark.django_db
def test_an_agent_doesnt_consent_its_person_does_by_approving(world: World) -> None:
    approve(world)
    editor = world.users["editor"]
    made = protein(api_as(world), world.project, name="MKVAGT")
    action = catalog.action(ACTION)
    assert action is not None
    with audit.using(audit.Context(via=Via.MCP, oauth_client="Notebook (Claude)")):
        run = services.launch(
            editor, action, world.project, {"protein": [uuid.UUID(made["id"])]}, {}
        )
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.consent_id is None
    request = api_as(world).get(f"/run-approvals/{run.approval_id}").json()
    assert [s["statement"] for s in request["consent"]] == [STATEMENT]
    approved = api_as(world).post(f"/run-approvals/{run.approval_id}/approve")
    assert approved.status_code == 200, approved.content
    run.refresh_from_db()
    assert run.status == RunStatus.QUEUED
    assert run.consent is not None
    assert (run.consent.given_by, run.consent.client) == (editor, "Notebook (Claude)")


# --- Certificates and calls --------------------------------------------------------------------


def _ca(name: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _key()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = timezone.now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _leaf(
    ca: tuple[ec.EllipticCurvePrivateKey, x509.Certificate], host: str
) -> tuple[bytes, bytes]:
    key = _key()
    now = timezone.now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca[1].subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca[0].public_key()), False
        )
        .sign(ca[0], hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), _pem(key)


class StandIn:
    """FoldCloud, as a stand-in on localhost: it takes a prediction, answers polls, and
    remembers what each request carried. It checks the API key when given one."""

    def __init__(self, tmp_path: Path, host: str = HOST, api_key: str | None = KEY) -> None:
        self.ca = _ca("Stand-in CA")
        self.ca_pem = self.ca[1].public_bytes(serialization.Encoding.PEM).decode()
        cert, key = _leaf(self.ca, host)
        (tmp_path / "standin.pem").write_bytes(cert)
        (tmp_path / "standin.key").write_bytes(key)
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []
        self.status = "running"
        #: Cleared, a prediction waits to be answered until it's set again.
        self.answering = threading.Event()
        self.answering.set()
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: Any) -> None:
                pass

            def _answer(self, status: int, body: dict[str, Any], **headers: str) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name.replace("_", "-"), value)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                stand_in.requests.append(("POST", self.path, dict(self.headers), body))
                if api_key and self.headers.get("Authorization") != f"Bearer {api_key}":
                    self._answer(401, {"error": "bad key"})
                elif self.path.partition("?")[0] == "/v1/predictions":
                    stand_in.answering.wait(10)
                    self._answer(201, {"id": "job-1", "model": "fold-2"}, x_model_version="2.1")
                elif self.path == "/v1/predictions/job-1/cancel":
                    self._answer(200, {"status": "canceled"})
                else:
                    self._answer(404, {"error": "no such path"})

            def do_GET(self) -> None:
                stand_in.requests.append(("GET", self.path, dict(self.headers), b""))
                if api_key and self.headers.get("Authorization") != f"Bearer {api_key}":
                    self._answer(401, {"error": "bad key"})
                    return
                body: dict[str, Any] = {"id": "job-1", "status": stand_in.status, "model": "fold-2"}
                if stand_in.status == "succeeded":
                    body |= {"output": {"pdb": PDB}, "usage": {"gpu_seconds": 120}}
                if stand_in.status == "failed":
                    body |= {"error": "The sequence has an unknown residue."}
                self._answer(200, body, x_model_version="2.1")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tmp_path / "standin.pem", tmp_path / "standin.key")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def trust(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=self.ca_pem)
        return context

    def resolve(self) -> Callable[[str, int], Any]:
        async def resolve(host: str, port: int) -> list[tuple[str, int]]:
            return [("127.0.0.1", self.port)]

        return resolve


@pytest.fixture
def stand_in(tmp_path: Path) -> Iterator[StandIn]:
    found = StandIn(tmp_path)
    yield found
    found.server.shutdown()
    found.server.server_close()


def _through(
    port: int, job: dict[str, Any], method: str, path: str, body: bytes = b""
) -> tuple[int, dict[str, str], bytes]:
    """A command's call, through the proxy, trusting only its attempt's CA."""
    conversation, answer = connect(
        port,
        f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n"
        f"Proxy-Authorization: {authorization(job)}\r\n\r\n",
    )
    assert answer.startswith(b"HTTP/1.1 200 "), answer
    context = ssl.create_default_context(cadata=job["job"]["apis"]["ca"])
    with context.wrap_socket(conversation, server_hostname=HOST) as tls:
        request = (
            f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
            f"Accept-Encoding: gzip\r\nAuthorization: Bearer stolen\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode() + body
        tls.sendall(request)
        data = b""
        while chunk := tls.recv(65536):
            data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return int(lines[0].split(" ")[1]), headers, rest


@pytest.mark.django_db(transaction=True)
def test_a_run_calls_the_api_through_the_proxy_parks_and_resumes(
    world: World, deployment: Path, stand_in: StandIn, serving: Callable[..., int], s3: Any
) -> None:
    approve(world)
    made = hosted(world, consent=[API])
    assert made.status_code == 201, made.content
    pod = worker(world)
    job = pod.lease().json()

    # The attempt gets its own CA, constrained to the API's host, and the API's address.
    ca = x509.load_pem_x509_certificate(job["job"]["apis"]["ca"].encode())
    constraints = ca.extensions.get_extension_for_class(x509.NameConstraints).value
    assert [n.value for n in constraints.permitted_subtrees or ()] == [HOST]
    calls = job["job"]["apis"]["calls"]
    assert calls[0]["name"] == "fold" and calls[0]["url"] == f"https://{HOST}"
    assert calls[0]["callback"].startswith(
        f"https://iki.example/api/v1/outside-apis/callbacks/{job['job']['id']}/fold/"
    )
    assert "secrets" not in job["job"]  # the key never reaches the worker

    gateway = api_gateway.Gateway(str(deployment), client_tls=stand_in.trust())
    port = serving(networks=LOOPBACK, resolve=stand_in.resolve(), gateway=gateway)
    sent = json.dumps({"sequence": "MKV", "recycles": 3}).encode()
    status, headers, body = _through(port, job, "POST", "/v1/predictions?trace=secret", sent)
    assert status == 201, body
    assert json.loads(body) == {"id": "job-1", "model": "fold-2"}
    assert headers["x-model-version"] == "2.1"
    (_, path, carried, got) = stand_in.requests[0]
    assert path == "/v1/predictions?trace=secret"  # the API gets the query; Iki keeps only the path
    # The proxy added the organization's key, replacing what the command sent, and asked
    # for the answer uncompressed.
    carried = {name.lower(): value for name, value in carried.items()}
    assert carried["authorization"] == f"Bearer {KEY}"
    assert carried["accept-encoding"] == "identity"
    assert got == sent

    call = ApiCall.objects.get(kind="call")
    assert (call.api, call.host, call.method, call.path, call.status) == (
        API,
        HOST,
        "POST",
        "/v1/predictions",
        201,
    )
    assert call.request_sha256 == hashlib.sha256(sent).hexdigest()
    assert call.response_sha256 == hashlib.sha256(body).hexdigest()
    assert (call.model, call.version, call.parameters) == ("fold-2", "2.1", {"/recycles": 3})
    assert call.cost == Decimal("0.020000")
    record = CostRecord.objects.get(kind=CostKind.API)
    assert (record.api, record.amount, record.currency) == (API, Decimal("0.0200"), "USD")

    # It parks: the worker is free, and the run waits on the job.
    waiting = {"level": "info", "text": "Submitted to FoldCloud as job-1."}
    parking = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "park": {
            "api": "fold",
            "job": "job-1",
            "state": {"protein": made.json()["inputs"][0]["entity"]},
        },
        "messages": [waiting],
    }
    parked = pod.report(job, "complete", output=parking)
    assert parked.json()["run_status"] == RunStatus.PARKED
    assert Job.objects.get(pk=job["job"]["id"]).state == JobState.PARKED
    run = api_as(world).get(f"/runs/{made.json()['hrid']}").json()
    assert run["status"] == "parked"
    assert run["park"]["job"] == "job-1" and run["park"]["outcome"] == ""
    assert run["messages"] == [{**waiting, "entity": None}]

    # A signed callback makes the poller ask now; the job is still running.
    park = RunPark.objects.get()
    RunPark.objects.update(next_poll_at=timezone.now() + timedelta(hours=1))
    stamp, ident, payload = (
        str(int(time.time())),
        "msg_1",
        b'{"id": "job-1", "status": "succeeded"}',
    )
    signature = base64.b64encode(
        hmac.new(
            base64.b64decode(HOOK), f"{ident}.{stamp}.".encode() + payload, hashlib.sha256
        ).digest()
    ).decode()
    url = calls[0]["callback"].removeprefix("https://iki.example")
    unsigned = Client().post(url, payload, content_type="application/json", secure=True)
    assert unsigned.status_code == 401
    woke = Client().post(
        url,
        payload,
        content_type="application/json",
        headers={
            "webhook-id": ident,
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{signature}",
        },
        secure=True,
    )
    assert woke.status_code == 202, woke.content
    until(lambda: RunPark.objects.get(pk=park.pk).polls >= 1)
    assert RunPark.objects.get(pk=park.pk).status == "running"
    assert ActionRun.objects.get().status == RunStatus.PARKED

    # Once it's done, the run resumes in a new attempt with the poll's answer.
    stand_in.status = "succeeded"
    RunPark.objects.update(next_poll_at=timezone.now())
    until(lambda: ActionRun.objects.get().status == RunStatus.QUEUED)
    park.refresh_from_db()
    assert park.outcome == ParkOutcome.DONE
    poll = ApiCall.objects.filter(kind="poll").latest("id")
    assert poll.usage == {"gpu_seconds": 120}
    assert poll.cost == Decimal("0.120000")  # usage only: polls aren't charged per call
    resumed = pod.lease().json()
    assert resumed["run"]["attempt"] == 2
    assert resumed["resume"]["job"] == "job-1"
    assert resumed["resume"]["answer"]["output"]["pdb"] == PDB
    assert resumed["resume"]["state"] == {"protein": made.json()["inputs"][0]["entity"]}
    calls_page = api_as(world).get(f"/runs/{made.json()['hrid']}/api-calls").json()
    assert [c["kind"] for c in calls_page["items"]][:1] == ["call"]
    assert {c["kind"] for c in calls_page["items"]} == {"call", "poll"}

    # The resumed attempt finishes with the structure, through the worker API like the first.
    upload = _upload(pod, resumed, PDB.encode())
    run_tasks()  # Iki reads the bytes back
    upload = pod.report(resumed, f"uploads/{upload['id']}/complete").json()
    assert upload["state"] == "ready", upload
    predicted = {"level": "info", "text": "Predicted with fold-2."}
    results = {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": "acme/protein.ProteinStructure",
                "values": {"structure": str(upload["file"]), "method": "predicted"},
            }
        ],
        "messages": [predicted],
    }
    finished = pod.report(resumed, "complete", output=results)
    assert finished.status_code == 200, finished.content
    assert finished.json()["run_status"] == RunStatus.SUCCEEDED
    run = api_as(world).get(f"/runs/{made.json()['hrid']}").json()
    assert run["status"] == "succeeded", run["error"]
    assert run["effect_counts"]["created"] == 1
    # The run's output is its last attempt's; each attempt keeps its own, as it was sent.
    assert run["messages"] == [{**predicted, "entity": None}]
    kept = {
        o.job.attempt: o.document
        for o in RunOutput.objects.filter(run_id=made.json()["id"]).select_related("job")
    }
    assert kept == {1: parking, 2: results}
    assert [
        (a["attempt"], a["state"])
        for a in api_as(world).get(f"/runs/{made.json()['hrid']}/attempts").json()
    ] == [(1, "parked"), (2, "succeeded")]


@pytest.mark.django_db(transaction=True)
def test_a_call_whose_command_disconnects_is_recorded_with_what_it_cost(
    world: World, deployment: Path, stand_in: StandIn, serving: Callable[..., int], s3: Any
) -> None:
    """The API answers, and charges, after the command has gone, say at a timeout of its
    own: the call is still recorded, with its cost, as every call is (ADR 0130)."""
    approve(world)
    assert hosted(world, consent=[API]).status_code == 201
    job = worker(world).lease().json()
    gateway = api_gateway.Gateway(str(deployment), client_tls=stand_in.trust())
    port = serving(networks=LOOPBACK, resolve=stand_in.resolve(), gateway=gateway)
    stand_in.answering.clear()
    conversation, answer = connect(
        port,
        f"CONNECT {HOST}:443 HTTP/1.1\r\nHost: {HOST}:443\r\n"
        f"Proxy-Authorization: {authorization(job)}\r\n\r\n",
    )
    assert answer.startswith(b"HTTP/1.1 200 "), answer
    context = ssl.create_default_context(cadata=job["job"]["apis"]["ca"])
    tls = context.wrap_socket(conversation, server_hostname=HOST)
    sent = json.dumps({"sequence": "MKV", "recycles": 3}).encode()
    tls.sendall(
        (
            f"POST /v1/predictions HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(sent)}\r\n\r\n"
        ).encode()
        + sent
    )
    until(lambda: bool(stand_in.requests))  # it reached the API
    # The command goes, resetting its connection, before the API answers.
    tls.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    tls.close()
    time.sleep(0.5)  # the proxy sees it go
    stand_in.answering.set()

    until(lambda: ApiCall.objects.filter(kind="call").exists())
    call = ApiCall.objects.get(kind="call")
    assert (call.status, call.model, call.cost) == (201, "fold-2", Decimal("0.020000"))
    assert call.request_sha256 == hashlib.sha256(sent).hexdigest()
    assert call.error.startswith("The command disconnected before the answer reached it in full")
    record = CostRecord.objects.get(kind=CostKind.API)
    assert (record.api, record.amount) == (API, Decimal("0.0200"))


@pytest.mark.django_db
def test_a_poll_that_says_the_job_failed_fails_the_run(world: World) -> None:
    run, park = _parked(world)
    call = outside.Call(
        job_id=park.job_id,
        api=API,
        host=HOST,
        kind="poll",
        method="GET",
        path="/v1/predictions/job-1",
        started_at=timezone.now(),
        duration_ms=5,
        status=200,
        park_id=park.pk,
    )
    answer = {"status": "failed", "error": "The sequence has an unknown residue."}
    assert outside.polled(park.pk, call, answer, {}) == "failed"
    run.refresh_from_db()
    assert run.status == RunStatus.FAILED
    assert run.error == {
        "code": "outside_api_failed",
        "message": "FoldCloud says the job failed: The sequence has an unknown residue.",
    }
    # Its only output says what it waited on: there are no results to reproduce.
    assert reuse.blocked(run, run.plugin_sha, run.plugin_version) == (
        f"{run.hrid} ended without results to compare with."
    )


@pytest.mark.django_db
def test_a_bundle_carries_who_agreed_and_each_call(world: World) -> None:
    from iki_server.bundles import export

    run, park = _parked(world)
    call = outside.Call(
        job_id=park.job_id,
        api=API,
        host=HOST,
        kind="poll",
        method="GET",
        path="/v1/predictions/job-1",
        started_at=timezone.now(),
        duration_ms=5,
        status=200,
        park_id=park.pk,
    )
    outside.polled(park.pk, call, {"status": "failed", "error": "No."}, {"x-model-version": "2.1"})
    run.refresh_from_db()
    (line,) = export._run_lines(export._Found(runs={run.pk: run}))
    assert line["consent"]["given_by"] == str(world.users["editor"].pk)
    assert [s["statement"] for s in line["consent"]["statements"]] == [STATEMENT]
    (poll,) = line["api_calls"]
    assert (poll["kind"], poll["path"], poll["status"]) == ("poll", "/v1/predictions/job-1", 200)


@pytest.mark.django_db
def test_a_park_past_its_timeout_ends_and_the_api_is_asked_to_cancel(world: World) -> None:
    run, park = _parked(world)
    RunPark.objects.update(deadline=timezone.now(), next_poll_at=timezone.now())
    (due,) = outside.take_due()
    assert due.what == "cancel"
    run.refresh_from_db()
    assert run.status == RunStatus.TIMED_OUT
    outside.cancelled(park.pk, None)
    park.refresh_from_db()
    assert park.outcome == ParkOutcome.TIMED_OUT


@pytest.mark.django_db
def test_cancelling_a_parked_run_ends_it_at_once(world: World) -> None:
    run, park = _parked(world)
    services.cancel(world.users["editor"], run)
    run.refresh_from_db()
    assert run.status == RunStatus.CANCELLED
    assert Job.objects.get(pk=park.job_id).state == JobState.PARKED
    (due,) = outside.take_due()
    assert due.what == "cancel"


@pytest.mark.django_db
def test_a_spent_budget_refuses_runs_that_call_priced_apis_on_any_tier(world: World) -> None:
    approve(world)
    Budget.objects.create(
        amount=Decimal("0.01"), currency="USD", enforce=True, created_by=world.users["org_admin"]
    )
    _, park = _parked(world)
    outside.record(
        outside.Call(
            job_id=park.job_id,
            api=API,
            host=HOST,
            kind="call",
            method="POST",
            path="/v1/predictions",
            started_at=timezone.now(),
            duration_ms=5,
            status=201,
            charged=True,
        )
    )
    action = catalog.action(ACTION)
    assert action is not None
    with pytest.raises(Refused, match="runs that call priced outside APIs"):
        budgets.check_launch(world.project, action)


def _parked(world: World) -> tuple[ActionRun, RunPark]:
    """A run that parked on job-1."""
    if not ApiApproval.objects.exists():
        approve(world)
    made = hosted(world, consent=[API])
    assert made.status_code == 201, made.content
    pod = worker(world)
    job = pod.lease().json()
    answer = pod.report(
        job,
        "complete",
        output={
            "apiVersion": "iki/v1",
            "kind": "RunOutput",
            "park": {"api": "fold", "job": "job-1"},
        },
    )
    assert answer.json()["run_status"] == RunStatus.PARKED, answer.content
    return ActionRun.objects.get(pk=made.json()["id"]), RunPark.objects.get(
        run_id=made.json()["id"]
    )


@pytest.mark.django_db
def test_a_park_that_isnt_one_of_its_apis_fails(world: World) -> None:
    approve(world)
    made = hosted(world, consent=[API])
    pod = worker(world)
    job = pod.lease().json()
    answer = pod.report(
        job,
        "complete",
        output={"apiVersion": "iki/v1", "kind": "RunOutput", "park": {"api": "folds", "job": "j"}},
    )
    assert answer.json()["run_status"] == RunStatus.FAILED
    assert "doesn't call" in answer.json()["message"]
    assert made.status_code == 201
