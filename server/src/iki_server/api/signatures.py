"""Electronic signatures under 21 CFR Part 11 (ADR 0126, ADR 0124).

- A state that needs ``signatures`` is set by signing for it: ``POST
  /entities/{ref}/signatures`` starts one signature, in the web app's
  browser session only, and answers where to send the person to sign in again. The
  identity provider's answer signs it; ``GET /signings/{id}`` says how it went. The last
  signature sets the state.
- ``GET /entities/{ref}/signatures`` lists a record's signatures, each with what it
  means as people read it (``manifestation``) and where it stands; ``verify=true``
  checks each again. ``GET /signatures/{id}/verification`` checks one.
- No token, app or AI agent can sign: they read and verify.
"""

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.urls import reverse
from ninja import Field, Query, Router, Schema, Status

from iki_server.accounts.models import User
from iki_server.api import preconditions
from iki_server.api.common import caller, person, require
from iki_server.api.entities import _find
from iki_server.api.preconditions import IfMatch
from iki_server.api.problems import Problem
from iki_server.api.schemas import PersonRef
from iki_server.entities import access
from iki_server.entities.models import Entity
from iki_server.entities.values import Invalid
from iki_server.org.permissions import Action
from iki_server.signatures import proof, services, signing_keys
from iki_server.signatures.models import Signature, Signing

router = Router(tags=["signatures"])

Standing = Literal["pending", "carried", "withdrawn", "lapsed"]


class SignedChangeOut(Schema):
    """What a signature signs for: setting the record's state field to a state."""

    field: str
    field_label: str
    from_: str = Field(alias="from", description="The state it was in.")
    from_label: str
    to: str = Field(description="The state it signs for setting.")
    to_label: str


class SignatureAuthenticationOut(Schema):
    protocol: Literal["oidc", "saml"]
    issuer: str
    subject: str = Field(description="Who the identity provider says signed in again.")
    authenticated_at: datetime = Field(description="When they signed in again.")
    methods: list[str] = Field(description="How, as the provider says (OIDC's `amr`).")
    context: str = Field(description="OIDC's `acr`, or SAML's AuthnContextClassRef.")


class SignatureCheckOut(Schema):
    name: Literal["document", "key", "signature", "row", "content", "record", "change", "review"]
    ok: bool
    detail: str


class SignatureVerificationOut(Schema):
    valid: bool = Field(description="Every check passed.")
    checks: list[SignatureCheckOut]
    verified_at: datetime


class SignatureOut(Schema):
    """A person's electronic signature on a revision of a record (21 CFR 11.50, 11.70)."""

    id: UUID
    manifestation: str = Field(
        description="As people read it: the signer's printed name, the date and time (UTC), "
        "and what it means. “Approved by Ada Lovelace on 2026-11-18 at 14:03:22 UTC. "
        "Meaning: QA approval.”"
    )
    record: UUID
    hrid: str
    revision: int = Field(description="The revision of the record it signs.")
    entry_revision: int | None = Field(
        description="For a record of a registry entry: the entry's revision it signs."
    )
    change: SignedChangeOut
    index: int = Field(description="Which of the change's signatures, from 0.")
    of: int = Field(description="How many the change needed when it was signed.")
    meaning: Literal[
        "authorship", "review", "approval", "verification", "responsibility", "witness"
    ]
    meaning_label: str
    label: str = Field(description="How the plugin names it: “QA approval”.")
    statement: str = Field(description="What the signer attested to.")
    roles: list[str] = Field(description="The lab roles its signer had to hold: any one.")
    signer: PersonRef
    signer_name: str = Field(description="The signer's name as it was signed.")
    signer_email: str
    signed_at: datetime
    reason: str
    authentication: SignatureAuthenticationOut
    deployment: str = Field(description="The deployment it was signed on.")
    key: str = Field(description="The Ed25519 key that signed it: `ed25519:...`.")
    status: Standing = Field(
        description="`carried`: the state was set with it. `pending`: setting it "
        "waits for others. `withdrawn`: its signer took it back. `lapsed`: the record "
        "changed after it, so it counts for nothing."
    )
    status_detail: str
    carried_by: int | None = Field(description="The revision that set the state with it.")
    document: str | None = Field(
        description="The exact canonical JSON (iki/signature/v1) when the reader can identify "
        "the entry it names."
    )
    signature: str = Field(description="The Ed25519 signature of `document`, base64url.")
    verification: SignatureVerificationOut | None = Field(
        None, description="With `verify=true`: each check made again, now."
    )


class SignaturesOut(Schema):
    signatures: list[SignatureOut]
    missing: list[str] = Field(
        description="Signatures the record's states were set with that are gone from "
        "the database. Empty, unless something outside Iki deleted them."
    )


class SigningIn(Schema):
    field: str = Field(
        max_length=63,
        description="The state field (ADR 0125), such as status: a key of `can.states`.",
    )
    state: str = Field(max_length=50, description="The state you sign for setting it to.")
    index: int | None = Field(
        None,
        ge=0,
        le=4,
        description="Which of its signatures you give, from 0. Without it, the first you may.",
    )
    entry_revision: int | None = Field(
        None,
        ge=1,
        description="Required for a record of a registry entry. Send `entry.revision` from "
        "the record you reviewed.",
    )
    reason: str = Field(
        "", max_length=2000, description="Why: the state may require it. It's signed."
    )
    acknowledge: bool = Field(
        False,
        description="Before your first signature: that you agree to `acknowledgment` in GET "
        "/me/signing.",
    )
    return_to: str = Field(
        max_length=2000,
        description="The path on this site to come back to once the identity provider "
        "answers, with `?signing=` and the signing's ID.",
    )


class SigningOut(Schema):
    """An attempt to sign: started, then signed or failed once the identity provider
    answers (ADR 0124)."""

    id: UUID
    status: Literal["started", "signed", "failed", "cancelled"]
    message: str = Field(description="Why it failed or was cancelled, for the person.")
    record: UUID
    hrid: str
    field: str = Field(description="The state field it signs for setting.")
    field_label: str
    to: str = Field(description="The state it signs for.")
    to_label: str
    index: int
    sign_in_url: str = Field(
        description="Where to send the browser to sign in again at the identity provider, "
        "while it's started."
    )
    expires_at: datetime
    signature: UUID | None
    set: bool = Field(description="Its signature was the last, and set the state.")
    state: str | None = Field(description="The record's state field's state now.")


class MySigningOut(Schema):
    """Whether you can sign now, and what you agree to before your first signature."""

    can_sign: bool
    why_not: str | None
    acknowledged: bool = Field(description="You agreed to `acknowledgment` already.")
    acknowledgment: str
    provider: str | None = Field(description="The identity provider you sign in again at.")
    locked: bool = Field(description="Failed attempts locked your signing.")


class CertificationOut(Schema):
    submitted_on: date
    reference: str
    recorded_by: PersonRef
    recorded_at: datetime


class CertificationState(Schema):
    certification: CertificationOut | None = Field(
        description="The organization's certification to the FDA (21 CFR 11.100(c)) in force. "
        "Until an Org Admin records one, nobody signs."
    )


class CertificationIn(Schema):
    submitted_on: date = Field(description="When the organization sent its letter to the FDA.")
    reference: str = Field(
        min_length=1,
        max_length=500,
        description="Where it's kept, or how it was sent: “QA-DOC-0042, sent to FDA ORA”.",
    )


class SigningKeyOut(Schema):
    key: str
    created_at: datetime
    retired_at: datetime | None


class SigningKeysOut(Schema):
    deployment: str
    keys: list[SigningKeyOut] = Field(
        description="This deployment's Ed25519 keys for signatures, oldest first: check a "
        "signature offline against one (`iki bundle verify --key`)."
    )


class WithdrawalIn(Schema):
    reason: str = Field(max_length=2000, description="Why you withdraw the pending signature.")


# --- The organization ---------------------------------------------------------------------------


def _certification() -> dict[str, Any]:
    found = services.certified()
    if found is None:
        return {"certification": None}
    return {
        "certification": {
            "submitted_on": found.submitted_on,
            "reference": found.reference,
            "recorded_by": found.recorded_by,
            "recorded_at": found.recorded_at,
        }
    }


@router.get(
    "/signatures/certification",
    response=CertificationState,
    summary="The organization's certification",
)
def get_certification(request: HttpRequest) -> dict[str, Any]:
    caller(request)
    return _certification()


@router.post(
    "/signatures/certification",
    response=CertificationState,
    summary="Record the organization's certification",
)
def certify(request: HttpRequest, payload: CertificationIn) -> dict[str, Any]:
    """Records that the organization certified to the FDA that its electronic signatures
    are the legally binding equivalent of handwritten ones (21 CFR 11.100(c)). Org Admins
    only; each is kept, and the latest is in force."""
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    try:
        services.certify(me, payload.submitted_on, payload.reference)
    except Invalid as error:
        raise _invalid(error) from None
    return _certification()


# --- Reading ------------------------------------------------------------------------------------


def _out(user: User, signatures: list[Signature], *, verify: bool = False) -> list[dict[str, Any]]:
    standing = services.standing(signatures)
    trusted = signing_keys.trusted() if verify else None
    documents = {s.pk: services.parsed(s) for s in signatures}
    entries: dict[UUID, UUID] = {}
    unavailable: set[UUID] = set()
    for s in signatures:
        try:
            if entry := services.signed_entry(s, documents[s.pk]):
                entries[s.pk] = entry
        except (ValueError, KeyError, TypeError):
            unavailable.add(s.pk)
    readable = access.readable_reference_ids(user, entries.values())
    out = []
    for s in signatures:
        document = documents[s.pk]
        sig = document.get("signature") or {}
        signer = document.get("signer") or {}
        moved = document.get("change") or {}
        auth = document.get("authentication") or {}
        state = standing[s.pk]
        item: dict[str, Any] = {
            "id": s.pk,
            "manifestation": services.manifestation(document) if document else "",
            "record": s.entity_id,
            "hrid": s.entity.hrid,
            "revision": s.revision,
            "entry_revision": s.entry_revision,
            "change": {
                "field": s.field,
                "field_label": moved.get("field_label", s.field),
                "from": moved.get("from", ""),
                "from_label": moved.get("from_label", ""),
                "to": s.state,
                "to_label": moved.get("to_label", s.state),
            },
            "index": s.index,
            "of": sig.get("of", 1),
            "meaning": s.meaning,
            "meaning_label": sig.get("meaning_label", s.meaning),
            "label": sig.get("label", ""),
            "statement": sig.get("statement", ""),
            "roles": sig.get("roles", []),
            "signer": s.signer,
            "signer_name": signer.get("name", ""),
            "signer_email": signer.get("email", ""),
            "signed_at": s.signed_at,
            "reason": document.get("reason", ""),
            "authentication": {
                "protocol": auth.get("protocol", "oidc"),
                "issuer": auth.get("issuer", ""),
                "subject": auth.get("subject", ""),
                "authenticated_at": auth.get("authenticated_at") or s.signed_at,
                "methods": auth.get("methods", []),
                "context": auth.get("context", ""),
            },
            "deployment": s.deployment,
            "key": s.key,
            "status": state.status,
            "status_detail": state.detail,
            "carried_by": state.revision,
            "document": (
                s.document
                if s.pk not in unavailable and (s.pk not in entries or entries[s.pk] in readable)
                else None
            ),
            "signature": proof.b64(bytes(s.signature)),
            "verification": _verification(s, trusted) if trusted is not None else None,
        }
        out.append(item)
    return out


def _verification(signature: Signature, trusted: dict[str, str] | None = None) -> dict[str, Any]:
    from django.utils import timezone

    checks = services.verify(signature, trusted)
    return {
        "valid": all(c.ok for c in checks),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        "verified_at": timezone.now(),
    }


class SignatureListParams(Schema):
    verify: bool = Field(False, description="Check each signature again, now.")


@router.get(
    "/entities/{ref}/signatures",
    response=SignaturesOut,
    summary="A record's electronic signatures",
    by_alias=True,
)
def list_signatures(
    request: HttpRequest, ref: str, params: Query[SignatureListParams], project: UUID | None = None
) -> dict[str, Any]:
    """Every signature given on the record, newest first, each with its manifestation and
    where it stands; with `verify=true`, each checked again. `missing` lists any the
    record's states were set with that are gone."""
    me = caller(request)
    entity = _find(me, ref, project)
    found = list(
        Signature.objects.filter(entity=entity)
        .select_related("signer", "entity")
        .order_by("-signed_at")
    )
    return {
        "signatures": _out(me, found, verify=params.verify),
        "missing": services.missing(entity),
    }


def _signature(me: User, signature_id: UUID) -> Signature:
    """A signature on a record the caller can read; else 404, as if it didn't exist."""
    found = Signature.objects.filter(pk=signature_id).select_related("signer", "entity").first()
    if found is None:
        raise Problem(404)
    try:
        _find(me, str(found.entity_id))
    except Problem:
        # The same answer as for an ID that names nothing: its ID alone grants nothing.
        raise Problem(404) from None
    return found


@router.get(
    "/signatures/{signature_id}", response=SignatureOut, summary="A signature", by_alias=True
)
def get_signature(request: HttpRequest, signature_id: UUID) -> dict[str, Any]:
    me = caller(request)
    return _out(me, [_signature(me, signature_id)])[0]


@router.get(
    "/signatures/{signature_id}/verification",
    response=SignatureVerificationOut,
    summary="Check a signature again",
)
def verify_signature(request: HttpRequest, signature_id: UUID) -> dict[str, Any]:
    """Checks, now, from the database: the key is one this deployment trusts, it signed
    the document, the signature's record, revision, signer and time are the document's,
    the content kept with it is what was signed, and the record's history still holds
    that content. Anything changed outside Iki, even in SQL, fails a check."""
    return _verification(_signature(caller(request), signature_id))


@router.post(
    "/signatures/{signature_id}/withdrawal",
    response=SignatureOut,
    summary="Withdraw your signature",
    by_alias=True,
)
def withdraw_signature(
    request: HttpRequest, signature_id: UUID, payload: WithdrawalIn
) -> dict[str, Any]:
    """Takes back your signature, with a reason, while setting the state waits for others.
    Once the state is set with it, it stays."""
    me = caller(request)
    found = _signature(me, signature_id)
    try:
        services.withdraw(me, found, payload.reason)
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except Invalid as error:
        raise _invalid(error) from None
    return _out(me, [_signature(me, signature_id)])[0]


def _invalid(error: Invalid) -> Problem:
    return Problem(
        422,
        "The signature needs more.",
        errors=[
            {"pointer": f"#/{key}", "detail": " ".join(messages)}
            for key, messages in error.errors.items()
        ],
    )


# --- Signing ------------------------------------------------------------------------------------


@router.post(
    "/entities/{ref}/signatures",
    response={201: SigningOut},
    summary="Start signing for a state",
    openapi_extra=preconditions.changes(201, tagged=False),
)
def start_signing(
    request: HttpRequest,
    ref: str,
    payload: SigningIn,
    response: HttpResponse,
    project: UUID | None = None,
    if_match: IfMatch = None,
) -> Any:  # noqa: ANN401 - a status and its body
    """Starts giving one of the signatures setting the record's state needs (ADR 0126),
    as the person in
    the web app's browser session: send the browser to `sign_in_url`, where the identity
    provider signs them in again, however recently they did (ADR 0124). Send the ETag of
    the record you reviewed as If-Match and, if it has a registry entry, send its
    `entry.revision` as `entry_revision`. Both revisions are checked when signing starts
    and finishes. The identity provider's answer comes back to `return_to`. Tokens,
    apps and AI agents are refused with 403."""
    me = caller(request)
    entity = _find(me, ref, project)
    expected = preconditions.expected(if_match)
    session_key = "" if "HTTP_AUTHORIZATION" in request.META else request.session.session_key
    try:
        signing = services.begin(
            me,
            session_key or "",
            entity,
            payload.field,
            payload.state,
            index=payload.index,
            expected_revision=expected,
            expected_entry_revision=payload.entry_revision,
            reason=payload.reason,
            return_to=payload.return_to,
            acknowledge=payload.acknowledge,
        )
    except PermissionDenied as denied:
        raise Problem(403, str(denied), code="cannot_sign") from None
    except services.Stale as stale:
        raise preconditions.stale(
            stale.current,
            f"It changed since you read it. {stale} Read it again before signing.",
        ) from None
    except Invalid as error:
        raise _invalid(error) from None
    return Status(201, _signing_out(signing))


def _signing_out(signing: Signing) -> dict[str, Any]:
    from iki_server.entities import states

    entity = Entity.objects.get(pk=signing.entity_id)
    f = states.state_fields(entity.type).get(signing.field)
    target = f.states.get(signing.state) if f is not None else None
    made = (
        signing.signature_id is not None
        and entity.revision == signing.revision + 1
        and entity.revisions.filter(
            number=signing.revision + 1, signatures__contains=[signing.signature_id]
        ).exists()
    )
    return {
        "id": signing.pk,
        "status": signing.status,
        "message": signing.message,
        "record": entity.pk,
        "hrid": entity.hrid,
        "field": signing.field,
        "field_label": f.label if f is not None else signing.field,
        "to": signing.state,
        "to_label": target.label if target is not None else signing.state,
        "index": signing.index,
        "sign_in_url": reverse("signing_start", kwargs={"signing_id": str(signing.pk)}),
        "expires_at": signing.expires_at,
        "signature": signing.signature_id,
        "set": made,
        "state": states.values_of(entity).get(signing.field),
    }


@router.get("/signings/{signing_id}", response=SigningOut, summary="How a signing went")
def get_signing(request: HttpRequest, signing_id: UUID) -> dict[str, Any]:
    """Your own signings only: another's answers 404."""
    me = caller(request)
    signing = Signing.objects.filter(pk=signing_id, user=me).first()
    if signing is None:
        raise Problem(404)
    return _signing_out(signing)


@router.get("/me/signing", response=MySigningOut, summary="Whether you can sign")
def my_signing(request: HttpRequest) -> dict[str, Any]:
    me = caller(request)
    why = services.why_cant_sign(me)
    found = services.identity(me)
    return {
        "can_sign": why is None,
        "why_not": why,
        "acknowledged": services.acknowledged(me),
        "acknowledgment": services.ACKNOWLEDGMENT,
        "provider": found[0].display_name if found else None,
        "locked": services.locked_out(me),
    }


class UnlockIn(Schema):
    reason: str = Field(
        max_length=2000, description="Why it's safe to unlock: who made the failed attempts."
    )


@router.post("/users/{user_id}/signing/unlock", response={204: None}, summary="Unlock signing")
def unlock_signing(request: HttpRequest, user_id: UUID, payload: UnlockIn) -> Any:  # noqa: ANN401 - a status
    """Unlocks someone's signing after failed attempts to sign as them locked it, saying
    why; the audit log keeps it. Another Org Admin unlocks theirs, signed in to Iki's web
    app: no token, app or AI agent can."""
    me = caller(request)
    require(me, Action.ORG_MANAGE)
    try:
        services.unlock(me, person(user_id), payload.reason)
    except PermissionDenied as denied:
        raise Problem(403, str(denied)) from None
    except Invalid as error:
        raise _invalid(error) from None
    return Status(204, None)


@router.get("/signing-keys", response=SigningKeysOut, summary="This deployment's signing keys")
def signing_keys_list(request: HttpRequest) -> dict[str, Any]:
    from django.conf import settings

    caller(request)
    return {"deployment": settings.IKI_ENVIRONMENT, "keys": signing_keys.own()}
