"""Outside APIs (ADR 0130): which projects may call each, what people agree to send, the
certificates the egress proxy serves them with, each call's record and cost, and runs
parked on their jobs.

An action that calls an outside API runs only in a project where an Org Admin approved
that API, for the host and purpose its plugin names, and only once the person launching
it agreed to what it sends. Its command calls the API through the egress proxy, which adds
the organization's key and records each call. A command that submitted a long job parks:
the proxy's poller polls the job and, once it's done, the run resumes in a new attempt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import partial
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.conf import settings
from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone
from django.utils.crypto import salted_hmac

from iki_server.accounts.models import Kind, User
from iki_server.audit import log as audit
from iki_server.entities import registry
from iki_server.entities.values import shape
from iki_server.org.models import OrgRole, Project
from iki_server.org.services import Refused
from iki_server.runs import catalog, endings, live, secrets
from iki_server.runs.models import (
    ActionRun,
    ApiApproval,
    ApiCall,
    ApiConsent,
    CostKind,
    CostRecord,
    EgressGatewayKey,
    Job,
    JobState,
    ParkOutcome,
    RunPark,
    RunStatus,
)
from iki_spec import apis as spec_apis
from iki_spec.compute import ApiPrice
from iki_spec.model import Action, ApiUse, OutsideApi, api_use

__all__ = [
    "ConsentRequired",
    "NotApproved",
    "approval",
    "check_approved",
    "consent",
    "statements",
    "used",
]

#: How recently a proxy must have said it has its key for leases to sign for it.
KEY_FRESH = timedelta(minutes=10)
#: Larger state a parking command gives is refused.
MAX_STATE = 64 * 1024
#: Larger answers aren't kept for the resuming attempt, nor read for what they report.
MAX_ANSWER = 1024 * 1024
#: Polls in a row that may fail (no answer, 429 or 5xx) before the park ends.
MAX_FAILURES = 20
#: How long a replica's poller has a due park to itself while it polls.
POLL_LEASE = timedelta(minutes=2)
#: How old a Standard Webhooks callback may be.
CALLBACK_TOLERANCE = 300


# --- What actions send -------------------------------------------------------------------------


def used(action: Action) -> list[tuple[ApiUse, OutsideApi]]:
    """The outside APIs `action` calls, each with what it sends."""
    return [(use, use.api) for use in action.apis if use.api is not None]


def run_uses(run: ActionRun) -> list[tuple[ApiUse, OutsideApi]]:
    """The outside APIs a run's action called, as it was when the run was launched."""
    uses = [api_use(u) for u in run.spec.get("apis") or ()]
    return [(use, use.api) for use in uses if use.api is not None]


def _lower(label: str) -> str:
    """A label as it reads mid-sentence: "Sequence" as "sequence", but "GC content" kept."""
    if len(label) > 1 and label[1].isupper():
        return label
    return label[:1].lower() + label[1:]


def _join(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


def sends(action: Action, use: ApiUse) -> list[str]:
    """What a use of an API sends, in words: "each Protein's sequence", "the recycles param"."""
    parts: list[str] = []
    for name, wanted in use.inputs.items():
        put = action.inputs.get(name)
        if put is None:
            continue
        known = registry.types().get(put.type)
        noun = (known.label if known is not None and known.label else "") or put.type.rpartition(
            "."
        )[2]
        try:
            fields = shape(put.type).fields()
        except (KeyError, LookupError):
            fields = {}
        labels = [
            _lower(fields[f].label) if f in fields and fields[f].label else f.replace("_", " ")
            for f in wanted
        ]
        parts.append(f"each {noun}'s {_join(labels)}")
    if use.params:
        labels = [
            _lower(getattr(action.params.get(p), "label", "") or p.replace("_", " "))
            for p in use.params
        ]
        parts.append(f"the {_join(labels)} param{'s' if len(labels) > 1 else ''}")
    return parts


def statement(action: Action, use: ApiUse, api: OutsideApi) -> str:
    """What the person launching is told before data leaves for `api`."""
    host = api.host or "a host this deployment hasn't set"
    return f"This sends your data to {api.label} ({host}): {_join(sends(action, use))}. " + (
        api.purpose
    )


def statements(action: Action) -> list[dict[str, Any]]:
    """Each API the action calls, with what it sends and what the person is told: what a
    launch's consent records, and what the catalog and approvals show."""
    return [
        {
            "api": api.qualified_name,
            "label": api.label,
            "host": api.host,
            "purpose": api.purpose,
            "inputs": {name: list(fields) for name, fields in use.inputs.items()},
            "params": list(use.params),
            "statement": statement(action, use, api),
        }
        for use, api in used(action)
    ]


def priced(action: Action | ActionRun) -> bool:
    """Whether the action calls an API this deployment prices: its runs are costly, as
    container and VM runs are (ADR 0071), on any tier."""
    prices = catalog.compute().prices
    if isinstance(action, ActionRun):
        return any(api.qualified_name in prices for _, api in run_uses(action))
    return any(api.qualified_name in prices for _, api in used(action))


def unavailable(action: Action) -> str | None:
    """Why this deployment can't run the action's calls to outside APIs, if it can't."""
    if not used(action):
        return None
    for _, api in used(action):
        if not api.host:
            return (
                f"This deployment hasn't set {api.label}'s host. An Iki administrator can set it "
                f"in the {api.plugin} plugin's settings."
            )
        credential = api.credential
        if credential is not None and not secrets.is_set(api.plugin, credential.secret):
            return (
                f"It calls {api.label}, whose key this deployment hasn't set. An Iki "
                "administrator can add it to the run secrets as "
                f"`{secrets.key(api.plugin, credential.secret)}`."
            )
    if action.tier == "vm":
        proxy = settings.IKI_RUNS_VM_EGRESS_PROXY_URL
    else:
        proxy = settings.IKI_EGRESS_PROXY_URL
    if not proxy:
        return (
            "It calls outside APIs, and this deployment gives runs no network access. An Iki "
            "administrator can turn on the egress proxy."
        )
    if not fresh_keys().exists():
        return (
            "It calls outside APIs, and the egress proxy hasn't said it can call them. An Iki "
            "administrator can give it a gateway key (egressProxy.gatewayKey)."
        )
    return None


# --- Approvals ---------------------------------------------------------------------------------


def approval(project: Project, api: OutsideApi) -> ApiApproval | None:
    """The project's approval of `api`, if it has one for the host and purpose the API names
    now: one for another host or purpose doesn't count."""
    if not api.host:
        return None
    return ApiApproval.objects.filter(
        project=project,
        api=api.qualified_name,
        host=api.host,
        purpose=api.purpose,
        revoked_at__isnull=True,
    ).first()


def admins() -> list[User]:
    """Who can approve outside APIs, as a refusal names them."""
    return list(
        User.objects.filter(
            org_roles__role=OrgRole.ORG_ADMIN, is_active=True, kind=Kind.PERSON
        ).order_by("name", "email")[:5]
    )


class NotApproved(Refused):
    """A launch in a project where an API its action calls isn't approved."""

    code = "outside_api_not_approved"

    def __init__(self, project: Project, apis: Sequence[OutsideApi]) -> None:
        names = _join([f"{a.label} ({a.host or 'no host set'})" for a in apis])
        verb = "isn't" if len(apis) == 1 else "aren't"
        who = [u.name or u.email for u in admins()]
        super().__init__(
            f"{names} {verb} approved for {project.name}. An Org Admin approves outside APIs "
            "in Settings → Outside APIs" + (f": {_join(who)}." if who else ".")
        )
        self.extensions = {"apis": [a.qualified_name for a in apis], "approvers": who}


def check_approved(project: Project, action: Action) -> None:
    """Raises :class:`NotApproved` unless every API the action calls is approved here."""
    missing = [api for _, api in used(action) if approval(project, api) is None]
    if missing:
        raise NotApproved(project, missing)


def approve(user: User, project: Project, api: OutsideApi, *, note: str = "") -> ApiApproval:
    """Approves `api` in `project`, for the host and purpose it names now. The caller checks
    the user may (an Org Admin, in the web app)."""
    if not api.host:
        raise Refused(
            f"This deployment hasn't set {api.label}'s host, so there's nothing to approve."
        )
    with transaction.atomic():
        current = (
            ApiApproval.objects.select_for_update()
            .filter(project=project, api=api.qualified_name, revoked_at__isnull=True)
            .first()
        )
        if current is not None:
            if current.host == api.host and current.purpose == api.purpose:
                return current
            # It named another host or purpose: that approval ends, and this one replaces it.
            current.revoked_at = timezone.now()
            current.revoked_by = user
            current.save(update_fields=["revoked_at", "revoked_by"])
        made = ApiApproval.objects.create(
            project=project,
            api=api.qualified_name,
            label=api.label,
            host=api.host,
            purpose=api.purpose,
            note=note.strip(),
            approved_by=user,
        )
        audit.record(
            "outside_api.approved",
            actor=user,
            target=made,
            project=project,
            data={"api": api.qualified_name, "host": api.host, "purpose": api.purpose},
        )
    return made


def revoke(user: User, project: Project, api: str) -> ApiApproval:
    """Ends the project's approval of `api`: launches are refused, running attempts' next
    calls too, and parked runs end at their next poll."""
    with transaction.atomic():
        current = (
            ApiApproval.objects.select_for_update()
            .filter(project=project, api=api, revoked_at__isnull=True)
            .first()
        )
        if current is None:
            raise Refused(f"{api} isn't approved for {project.name}.")
        current.revoked_at = timezone.now()
        current.revoked_by = user
        current.save(update_fields=["revoked_at", "revoked_by"])
        audit.record(
            "outside_api.revoked",
            actor=user,
            target=current,
            project=project,
            data={"api": api, "host": current.host},
        )
        # Its parked runs end at once: the poller asks the API to cancel their jobs.
        plugin, _, name = api.rpartition(".")
        RunPark.objects.filter(
            run__project=project,
            run__action__startswith=f"{plugin}.",
            api=name,
            ended_at__isnull=True,
        ).update(next_poll_at=Now())
    return current


# --- Consent -----------------------------------------------------------------------------------


class ConsentRequired(Refused):
    """A launch that sends data to outside APIs, without the person agreeing to it."""

    code = "consent_required"

    def __init__(self, given: list[dict[str, Any]]) -> None:
        super().__init__(
            " ".join(s["statement"] for s in given)
            + " Launch again with `consent` naming "
            + _join([f"`{s['api']}`" for s in given])
            + " to agree."
        )
        self.extensions = {"consent": given}


def consent(
    user: User,
    project: Project,
    launched: str,
    given: list[dict[str, Any]],
    agreed: Sequence[str],
    *,
    covered_by: ApiConsent | None = None,
) -> ApiConsent | None:
    """The consent a launch that sends `given` gives: the one it's covered by (a workflow's,
    for its steps), or a new one when `agreed` names each API. Raises
    :class:`ConsentRequired` otherwise. Call inside the launch's transaction."""
    if not given:
        return None
    wanted = {s["api"] for s in given}
    if covered_by is not None and wanted <= {s["api"] for s in covered_by.statements}:
        return covered_by
    if not wanted <= set(agreed):
        raise ConsentRequired(given)
    return record_consent(user, project, launched, given)


def record_consent(
    user: User,
    project: Project,
    launched: str,
    given: list[dict[str, Any]],
    *,
    via: str | None = None,
    client: str | None = None,
    agent: Any = audit.UNSET,  # noqa: ANN401 - an Agent, None, or UNSET for the request's
) -> ApiConsent:
    made = ApiConsent.objects.create(
        project=project,
        launched=launched,
        given_by=user,
        via=via or audit.current_via(),
        client=audit.current_oauth_client() if client is None else client,
        agent=audit.current_agent() if agent is audit.UNSET else agent,
        statements=given,
    )
    audit.record(
        "outside_api.consented",
        actor=user,
        target=made,
        project=project,
        data={"launched": launched, "apis": [s["api"] for s in given]},
        agent=made.agent,
    )
    return made


# --- The proxy's certificates ------------------------------------------------------------------


def fingerprint(public_key: Any) -> str:  # noqa: ANN401 - any of cryptography's public keys
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()


def register_key(public_pem: str) -> str:
    """An egress proxy says it has this key (as it starts, and while it runs): leases sign
    their attempts' certificates for it."""
    key = serialization.load_pem_public_key(public_pem.encode())
    found = fingerprint(key)
    EgressGatewayKey.objects.update_or_create(
        fingerprint=found,
        defaults={"public_key": public_pem, "last_seen_at": timezone.now()},
        create_defaults={"public_key": public_pem},
    )
    return found


def fresh_keys() -> Any:  # noqa: ANN401 - a QuerySet
    return EgressGatewayKey.objects.filter(last_seen_at__gte=timezone.now() - KEY_FRESH)


def issue(hosts: Sequence[str], until: datetime) -> dict[str, Any]:
    """A certificate authority for one attempt, and a certificate for each of `hosts` for
    each egress proxy's key. The CA's private key is thrown away when this returns, so it
    can't sign anything else, and it's constrained to `hosts` besides."""
    now = timezone.now()
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Iki"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Iki outside APIs, for one attempt"),
        ]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(until)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
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
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=[x509.DNSName(h) for h in hosts], excluded_subtrees=None
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    leaves: dict[str, dict[str, str]] = {}
    for row in fresh_keys():
        public = serialization.load_pem_public_key(row.public_key.encode())
        for host in hosts:
            leaf = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(name)
                .public_key(public)  # type: ignore[arg-type]
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(until)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        content_commitment=False,
                        key_encipherment=isinstance(public, rsa.RSAPublicKey),
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=False,
                        crl_sign=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
                )
                .sign(ca_key, hashes.SHA256())
            )
            leaves.setdefault(row.fingerprint, {})[host] = leaf.public_bytes(
                serialization.Encoding.PEM
            ).decode()
    del ca_key
    return {"ca": ca.public_bytes(serialization.Encoding.PEM).decode(), "leaves": leaves}


def callback_token(job_id: UUID, api: str) -> str:
    return salted_hmac(
        "iki.outside-api-callback", f"{job_id}:{api}", algorithm="sha256"
    ).hexdigest()


def callback_url(job_id: UUID, api: str) -> str:
    base = settings.IKI_PUBLIC_URL.rstrip("/")
    return f"{base}/api/v1/outside-apis/callbacks/{job_id}/{api}/{callback_token(job_id, api)}"


def for_payload(job: Job, run: ActionRun) -> dict[str, Any]:
    """For an attempt whose action calls outside APIs: its CA, made at its first lease, and
    each API's address and callback, as ``job.apis`` in its input (ADR 0130)."""
    uses = run_uses(run)
    if not uses:
        return {}
    if not job.gateway:
        hosts = sorted({api.host for _, api in uses if api.host})
        until = timezone.now() + timedelta(seconds=run.timeout) + timedelta(hours=1)
        job.gateway = issue(hosts, until)
        job.save(update_fields=["gateway"])
    calls: list[dict[str, str]] = []
    for use, api in uses:
        call = {"name": use.name, "url": f"https://{api.host}"}
        if api.jobs is not None and api.jobs.webhook is not None and settings.IKI_PUBLIC_URL:
            call["callback"] = callback_url(job.pk, use.name)
        calls.append(call)
    return {"apis": {"ca": job.gateway["ca"], "calls": calls}}


def for_resume(job: Job) -> dict[str, Any]:
    """For an attempt that resumes a parked run: the job, what the command saved, and the
    last poll's answer, as ``resume`` in its input."""
    park = job.resumes
    if park is None:
        return {}
    return {
        "resume": {
            "api": park.api,
            "job": park.external_id,
            "state": park.state,
            "status": park.status,
            "answer": park.answer,
            "parked_at": park.parked_at.isoformat(),
        }
    }


# --- What the proxy calls ----------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What the proxy needs to make one call for an attempt, or why it mayn't."""

    refused: str = ""
    job_id: UUID | None = None
    run_id: UUID | None = None
    name: str = ""
    api: OutsideApi | None = None
    #: The header and value the proxy adds; None when the API takes no key.
    credential: tuple[str, str] | None = None


def leaf(job_id: UUID, key: str, host: str) -> str | None:
    """The certificate the attempt's CA signed for `host` with the proxy's key `key`."""
    gateway = Job.objects.filter(pk=job_id).values_list("gateway", flat=True).first() or {}
    found = (gateway.get("leaves") or {}).get(key, {}).get(host)
    return found if isinstance(found, str) else None


def target(job_id: UUID, name: str, *, park: bool = False, cancelling: bool = False) -> Target:
    """The API `name` of the attempt's run, with its key, if it may call it now: its
    attempt holds its lease (or, for the poller, its run is parked), and the project's
    approval stands. Read at each call, so revoking or rotating takes effect at once.
    Asking the API to cancel a job it already has needs no approval: it sends nothing
    but the job's ID."""
    from iki_server.runs.egress import HELD

    job = Job.objects.select_related("run__project").filter(pk=job_id).first()
    if job is None:
        return Target("The attempt is gone.")
    if not park and (
        job.state not in HELD
        or job.lease_expires_at is None
        or job.lease_expires_at <= timezone.now()
    ):
        return Target("The attempt has ended.")
    run = job.run
    found = next(((u, a) for u, a in run_uses(run) if u.name == name), None)
    if found is None:
        return Target(f"{run.action} doesn't call `{name}`.")
    _, api = found
    if not cancelling and approval(run.project, api) is None:
        return Target(
            f"{api.label} ({api.host}) isn't approved for {run.project.name} any more. An Org "
            "Admin approves outside APIs in Settings → Outside APIs."
        )
    credential: tuple[str, str] | None = None
    if api.credential is not None:
        key = secrets.value(api.plugin, api.credential.secret)
        if key is None:
            return Target(
                f"This deployment hasn't set {api.label}'s key "
                f"(`{secrets.key(api.plugin, api.credential.secret)}`)."
            )
        credential = (api.credential.header, f"{api.credential.prefix}{key}")
    return Target("", job.pk, run.pk, name, api, credential)


def cost(price: ApiPrice | None, usage: Mapping[str, Any], *, per_call: bool) -> Decimal | None:
    """What a call cost at `price`: its price per call, if it's charged as one, and each
    unit of usage it reported. None when the API has no price here."""
    if price is None:
        return None
    total = Decimal(0)
    if per_call and price.per_call is not None:
        total += Decimal(str(price.per_call))
    for name, each in price.usage.items():
        value = usage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            total += Decimal(str(value)) * Decimal(str(each))
    return total.quantize(Decimal("0.000001"), ROUND_HALF_UP)


@dataclass
class Call:
    """One call, as the proxy saw it."""

    job_id: UUID
    api: str
    host: str
    kind: str
    method: str
    path: str
    started_at: datetime
    duration_ms: int
    status: int | None = None
    error: str = ""
    request_bytes: int = 0
    response_bytes: int = 0
    request_sha256: str = ""
    response_sha256: str = ""
    model: str = ""
    version: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    park_id: UUID | None = None
    #: Whether the call is charged its price per call.
    charged: bool = False


def record(call: Call) -> ApiCall:
    """Keeps a call with its run, and what it cost against the run's project and team."""
    job = Job.objects.select_related("run__project").get(pk=call.job_id)
    run = job.run
    price = catalog.compute().prices.get(call.api)
    amount = cost(price, call.usage, per_call=call.charged)
    made = ApiCall.objects.create(
        run=run,
        job=job,
        park_id=call.park_id,
        api=call.api,
        host=call.host,
        kind=call.kind,
        method=call.method[:16],
        path=call.path[:2000],
        status=call.status,
        error=call.error[:500],
        started_at=call.started_at,
        duration_ms=max(call.duration_ms, 0),
        request_bytes=call.request_bytes,
        response_bytes=call.response_bytes,
        request_sha256=call.request_sha256,
        response_sha256=call.response_sha256,
        model=str(call.model)[:200],
        version=str(call.version)[:200],
        parameters=call.parameters,
        usage=call.usage,
        cost=amount,
        currency=price.currency if amount is not None and price is not None else "",
    )
    if amount is not None and price is not None:
        CostRecord.objects.create(
            kind=CostKind.API,
            call=made,
            api=call.api,
            job=job,
            run=run,
            project=run.project,
            team_id=run.project.team_id,
            action=run.action,
            template="",
            tier=job.tier,
            started_at=call.started_at,
            ended_at=call.started_at + timedelta(milliseconds=call.duration_ms),
            seconds=Decimal(0),
            currency=price.currency,
            amount=amount.quantize(Decimal("0.0001"), ROUND_HALF_UP),
        )
    return made


def reported(
    api: OutsideApi,
    request: Sequence[Any],
    answers: Sequence[Any],
    headers: Mapping[str, str],
    *,
    usage: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What a call's answer says answered, the request's declared parameters, and its usage,
    each value shortened to what a record keeps."""
    found_usage = {
        name: spec_apis.short(value)
        for name, at in (api.usage if usage is None else usage).items()
        if (value := spec_apis.locate(at, answers, headers)) is not None
    }
    return {
        "model": _text(spec_apis.locate(api.model, answers, headers)) if api.model else "",
        "version": _text(spec_apis.locate(api.version, answers, headers)) if api.version else "",
        "parameters": {
            at: spec_apis.short(value)
            for at in api.parameters
            if (value := spec_apis.locate(at, request, {})) is not None
        },
        "usage": found_usage,
    }


def _text(value: Any) -> str:  # noqa: ANN401 - JSON
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


# --- Parking -----------------------------------------------------------------------------------


class InvalidPark(Exception):  # noqa: N818 - what it is
    pass


def park(job: Job, run: ActionRun, given: Any) -> ActionRun:  # noqa: ANN401 - JSON
    """The command submitted a job to an outside API and parked: the attempt ends `parked`,
    its worker is free, and the run waits, polled, until the job is done. Call inside the
    transaction completing the attempt, holding both."""
    from iki_server.runs import queue

    try:
        use, api, external, state = _parking(run, given)
    except InvalidPark as invalid:
        error = {"code": "invalid_output", "message": str(invalid)}
        return queue.finish(job, run, JobState.FAILED, RunStatus.FAILED, error)
    assert api.jobs is not None  # noqa: S101 - checked
    now = timezone.now()
    RunPark.objects.create(
        run=run,
        job=job,
        api=use.name,
        external_id=external,
        state=state,
        next_poll_at=now + timedelta(seconds=api.jobs.every),
        deadline=now + timedelta(seconds=api.jobs.timeout),
    )
    job.state, job.finished_at, job.error = JobState.PARKED, now, None
    job.lease_token = job.lease_expires_at = None
    job.save(update_fields=["state", "finished_at", "error", "lease_token", "lease_expires_at"])
    run.status = RunStatus.PARKED
    run.save(update_fields=["status"])
    audit.record(
        "run.parked",
        actor=None,
        target=run,
        project=run.project,
        data={"api": api.qualified_name, "job": external},
    )
    live.changed(run.pk, run.batch_id)
    return run


def _parking(run: ActionRun, given: Any) -> tuple[ApiUse, OutsideApi, str, dict[str, Any]]:  # noqa: ANN401
    if not isinstance(given, dict):
        raise InvalidPark("`park` must be an object, with `api` and `job`.")
    name = given.get("api")
    found = next(((u, a) for u, a in run_uses(run) if u.name == name), None)
    if found is None:
        raise InvalidPark(f"The run parked on `{name}`, which its action doesn't call.")
    use, api = found
    if api.jobs is None:
        raise InvalidPark(f"`{name}` has no `jobs` in its plugin, so a run can't park on it.")
    external = given.get("job")
    if not isinstance(external, str) or not spec_apis.JOB_ID.fullmatch(external):
        raise InvalidPark(
            "`park.job` must be the API's ID for the job: up to 200 letters, digits and . _ : ~ -."
        )
    state = given.get("state") or {}
    if not isinstance(state, dict):
        raise InvalidPark("`park.state` must be an object.")
    if len(json.dumps(state).encode()) > MAX_STATE:
        raise InvalidPark(f"`park.state` is over {MAX_STATE // 1024} KiB.")
    return use, api, external, state


@dataclass(frozen=True)
class Due:
    """A park the poller should act on: poll its job, or ask the API to cancel it."""

    park_id: UUID
    job_id: UUID
    name: str
    api: OutsideApi
    external_id: str
    #: "poll" or "cancel".
    what: str


def take_due(limit: int = 10) -> list[Due]:
    """Parks due a poll, or a cancel, taken for this replica for a while. Runs out of time,
    cancelled, or whose API is no longer approved end here; their jobs are cancelled at the
    API next, where it says how."""
    now = timezone.now()
    out: list[Due] = []
    with transaction.atomic():
        parks = list(
            RunPark.objects.select_for_update(skip_locked=True, of=("self",))
            .select_related("run__project")
            .filter(ended_at__isnull=True, next_poll_at__lte=now)
            .order_by("next_poll_at")[:limit]
        )
        for p in parks:
            found = next(((u, a) for u, a in run_uses(p.run) if u.name == p.api), None)
            if found is None or found[1].jobs is None:
                _end_park(p, ParkOutcome.ENDED, "Its action no longer calls this API.")
                continue
            _, api = found
            assert api.jobs is not None  # noqa: S101 - checked
            if not p.run.finished:
                why = _ending(p, api, now)
                if why is not None:
                    status, code, message = why
                    _end_run(p.run, status, {"code": code, "message": message})
                    p.cancel_requested_at = now
                    p.message = message[:2000]
            elif p.cancel_requested_at is None:
                p.cancel_requested_at = now
            if p.cancel_requested_at is not None:
                if not api.jobs.cancel:
                    _end_park(p, _outcome(p.run), p.message)
                    continue
                p.next_poll_at = now + POLL_LEASE
                p.save(update_fields=["next_poll_at", "cancel_requested_at", "message"])
                out.append(Due(p.pk, p.job_id, p.api, api, p.external_id, "cancel"))
                continue
            p.next_poll_at = now + POLL_LEASE
            p.save(update_fields=["next_poll_at"])
            out.append(Due(p.pk, p.job_id, p.api, api, p.external_id, "poll"))
    return out


def _ending(p: RunPark, api: OutsideApi, now: datetime) -> tuple[str, str, str] | None:
    if now >= p.deadline:
        return (
            RunStatus.TIMED_OUT,
            "timed_out",
            f"{api.label}'s job didn't finish within "
            f"{_duration(api.jobs.timeout if api.jobs else 0)}.",
        )
    if approval(p.run.project, api) is None:
        return (
            RunStatus.FAILED,
            "outside_api_not_approved",
            f"{api.label} ({api.host}) stopped being approved for {p.run.project.name} while "
            "the run waited on it.",
        )
    return None


def _duration(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''}"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    minutes = max(seconds // 60, 1)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _outcome(run: ActionRun) -> str:
    if run.status == RunStatus.CANCELLED:
        return ParkOutcome.CANCELLED
    if run.status == RunStatus.TIMED_OUT:
        return ParkOutcome.TIMED_OUT
    return ParkOutcome.ENDED


def _end_park(p: RunPark, outcome: str, message: str) -> None:
    p.ended_at = timezone.now()
    p.outcome = outcome
    p.message = message[:2000]
    p.save(update_fields=["ended_at", "outcome", "message", "cancel_status", "cancel_requested_at"])


def _end_run(run: ActionRun, status: str, error: dict[str, Any] | None) -> None:
    locked = ActionRun.objects.select_for_update().get(pk=run.pk)
    if locked.finished:
        return
    locked.status = status
    locked.finished_at = timezone.now()
    locked.error = error
    locked.save(update_fields=["status", "finished_at", "error"])
    run.status, run.finished_at, run.error = locked.status, locked.finished_at, locked.error
    endings.ended(locked)
    transaction.on_commit(partial(live.changed, locked.pk, locked.batch_id))


def cancelled(park_id: UUID, call: Call | None) -> None:
    """The poller asked the API to cancel the park's job (or couldn't): the park ends."""
    with transaction.atomic():
        p = RunPark.objects.select_for_update(of=("self",)).select_related("run").get(pk=park_id)
        if p.ended_at is not None:
            return
        if call is not None:
            record(call)
            p.cancel_status = call.status
        _end_park(p, _outcome(p.run), p.message)


def polled(park_id: UUID, call: Call, answer: Any, headers: Mapping[str, str]) -> str:  # noqa: ANN401
    """Records a poll of the park's job and acts on its answer: resumes the run when the job
    is done, fails it when the job failed, else polls again later. Returns what happened."""
    from iki_server.runs import queue

    with transaction.atomic():
        p = (
            RunPark.objects.select_for_update(of=("self",))
            .select_related("run__project")
            .get(pk=park_id)
        )
        if p.ended_at is not None:
            return "ended"
        found = next(((u, a) for u, a in run_uses(p.run) if u.name == p.api), None)
        assert found is not None and found[1].jobs is not None  # noqa: S101 - taken as due
        _, api = found
        jobs = api.jobs
        assert jobs is not None  # noqa: S101 - checked
        now = timezone.now()
        p.polls += 1
        p.last_polled_at = now
        status = None
        if call.status is not None and 200 <= call.status < 300 and answer is not None:
            located = spec_apis.locate(jobs.status, [answer], headers)
            status = None if located is None else _text(located)
        final = status is not None and (status in jobs.done or status in jobs.failed)
        if final:
            # Usage the API reports for the job as a whole is read from the answer that ends it.
            call.usage = {
                **call.usage,
                **reported(api, [], [answer], headers, usage=jobs.usage)["usage"],
            }
        record(call)
        woken = p.woken_at is not None and p.woken_at >= call.started_at
        if call.status is None or call.status == 429 or call.status >= 500:
            p.failures += 1
            if p.failures >= MAX_FAILURES:
                message = f"Iki couldn't ask {api.label} about the job {p.failures} times in a row."
                _end_run(
                    p.run, RunStatus.FAILED, {"code": "outside_api_failed", "message": message}
                )
                _end_park(p, ParkOutcome.ENDED, message)
                return "ended"
            backoff = min(jobs.every * 2 ** min(p.failures, 10), 3600)
            p.next_poll_at = now + timedelta(seconds=backoff)
            p.save(update_fields=["polls", "last_polled_at", "failures", "next_poll_at"])
            return "retry"
        if call.status >= 300 or status is None:
            message = (
                f"{api.label} answered {call.status} when asked about the job."
                if call.status >= 300
                else f"{api.label}'s answer about the job didn't say its state at `{jobs.status}`."
            )
            _end_run(p.run, RunStatus.FAILED, {"code": "outside_api_failed", "message": message})
            _end_park(p, ParkOutcome.ENDED, message)
            return "ended"
        p.status = status[:100]
        p.failures = 0
        if status in jobs.done:
            p.answer = answer if len(json.dumps(answer).encode()) <= MAX_ANSWER else None
            p.save(update_fields=["polls", "last_polled_at", "failures", "status", "answer"])
            _end_park(p, ParkOutcome.DONE, "")
            _resume(p)
            transaction.on_commit(partial(queue.notify, p.job.tier))
            return "done"
        if status in jobs.failed:
            said = spec_apis.locate(jobs.message, [answer], headers) if jobs.message else None
            message = f"{api.label} says the job {status}" + (
                f": {_text(said)[:1500]}" if said else "."
            )
            p.save(update_fields=["polls", "last_polled_at", "failures", "status"])
            _end_run(p.run, RunStatus.FAILED, {"code": "outside_api_failed", "message": message})
            _end_park(p, ParkOutcome.FAILED, message)
            return "failed"
        p.next_poll_at = now if woken else now + timedelta(seconds=jobs.every)
        p.save(update_fields=["polls", "last_polled_at", "failures", "status", "next_poll_at"])
        return "waiting"


def _resume(p: RunPark) -> None:
    """A new attempt for the parked run, with the job's answer. It isn't a retry: the run
    gets one more attempt for it."""
    run = ActionRun.objects.select_for_update().get(pk=p.run_id)
    last = p.job
    run.max_attempts += 1
    run.status = RunStatus.QUEUED
    run.save(update_fields=["max_attempts", "status"])
    Job.objects.create(
        run=run,
        attempt=last.attempt + 1,
        priority=last.priority,
        project_id=last.project_id,
        tier=last.tier,
        environment=last.environment,
        labels=last.labels,
        resources=last.resources,
        lifecycle=last.lifecycle,
        outside=True,
        resumes=p,
        queued_at=Now(),
    )
    audit.record(
        "run.resumed",
        actor=None,
        target=run,
        project=run.project,
        data={"api": p.api, "job": p.external_id, "status": p.status},
    )
    transaction.on_commit(partial(live.changed, run.pk, run.batch_id))


def cancel_parked(run: ActionRun) -> None:
    """The run was cancelled while parked: the poller asks the API to cancel its job. Call
    inside the cancelling transaction, once the run has ended."""
    RunPark.objects.filter(run=run, ended_at__isnull=True).update(
        cancel_requested_at=Now(), next_poll_at=Now()
    )


# --- Callbacks ---------------------------------------------------------------------------------


class BadCallback(Exception):  # noqa: N818 - what it is
    pass


def callback(job_id: UUID, name: str, token: str, headers: Mapping[str, str], body: bytes) -> bool:
    """An API called back for a run's job. One signed with its webhook secret makes the
    poller poll the job now; its body is never used. Returns whether a park was woken;
    raises :class:`BadCallback` for a callback that isn't the API's."""
    if not hmac.compare_digest(token, callback_token(job_id, name)):
        raise BadCallback("No such callback.")
    job = Job.objects.select_related("run").filter(pk=job_id).first()
    if job is None:
        raise BadCallback("No such callback.")
    found = next(((u, a) for u, a in run_uses(job.run) if u.name == name), None)
    if found is None or found[1].jobs is None or found[1].jobs.webhook is None:
        raise BadCallback("No such callback.")
    api = found[1]
    hook = api.jobs.webhook if api.jobs else None
    assert hook is not None  # noqa: S101 - checked
    secret = secrets.value(api.plugin, hook.secret)
    if secret is None:
        raise BadCallback(f"This deployment hasn't set {api.label}'s webhook secret.")
    lowered = {k.lower(): v for k, v in headers.items()}
    if hook.signature == "standard-webhooks":
        ok = verify_standard(secret, lowered, body)
    else:
        ok = verify_hmac(secret, lowered.get(hook.header.lower(), ""), body)
    if not ok:
        raise BadCallback("The callback's signature doesn't verify.")
    now = timezone.now()
    woken = RunPark.objects.filter(job=job, api=name, ended_at__isnull=True).update(
        woken_at=now, next_poll_at=now
    )
    return woken > 0


def verify_standard(secret: str, headers: Mapping[str, str], body: bytes) -> bool:
    """Standard Webhooks: ``v1,<base64 HMAC-SHA256 of id.timestamp.body>``, by a key given
    as ``whsec_<base64>``, sent within five minutes."""
    ident = headers.get("webhook-id", "")
    stamp = headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    if not ident or not stamp.isdigit() or not signatures:
        return False
    if abs(time.time() - int(stamp)) > CALLBACK_TOLERANCE:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        key = secret.encode()
    signed = f"{ident}.{stamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for given in signatures.split():
        version, _, value = given.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def verify_hmac(secret: str, header: str, body: bytes) -> bool:
    """HMAC-SHA256 of the body, in hex, optionally after ``sha256=``."""
    given = header.strip().removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(given) and hmac.compare_digest(given.lower(), expected)
