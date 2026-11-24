"""Signing for a change to a record's state (ADR 0126), as the person signs in again
(ADR 0124).

A plugin's type may say that setting one of its states needs ``signatures``: the state is
set only once each is given, on the same revision of the record. Each signer starts a
:class:`Signing` in the web app (:func:`begin`), signs in again at the identity provider,
and the answer finishes it (:func:`finish`), which signs what they read. The last one sets
the state. A signature counts while the record stays at the revision it signed; once the
record changes, it has lapsed, and the change needs it again. What a change needs comes
from ``changes``; whether one person may give two of its signatures, or in what order,
is the plugin's to say.

Each signature is Ed25519 over a canonical document (``proof``), by this deployment's
key (``signing_keys``), naming the SHA-256 of the record's content at that revision as
its history keeps it. :func:`verify` checks each part again, from the database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from allauth.socialaccount.models import SocialAccount
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from iki_server.accounts.models import IdentityProvider, User
from iki_server.audit import log
from iki_server.audit.models import AuditEvent
from iki_server.bundles import keys
from iki_server.core.ids import uuid7
from iki_server.entities import requests, states
from iki_server.entities.models import Entity, ProjectLink, Revision, RevisionKind
from iki_server.entities.values import Invalid, shape
from iki_server.org import lab_roles, permissions
from iki_server.org.models import Project
from iki_server.org.permissions import Action
from iki_server.org.services import Refused
from iki_server.plugins.models import HistoryChange
from iki_server.signatures import changes, proof, signing_keys
from iki_server.signatures.changes import Change, roles_for
from iki_server.signatures.models import (
    Acknowledgment,
    Certification,
    Signature,
    Signing,
    Status,
    Withdrawal,
)
from iki_spec.model import SIGNATURE_MEANINGS
from iki_spec.model import Signature as Wanted

#: What each person agrees to before their first signature (21 CFR 11.100).
ACKNOWLEDGMENT = (
    "My electronic signature in Iki is the legally binding equivalent of my handwritten "
    "signature. It's mine alone: I won't let anyone else use it, and I'll sign only what I "
    "mean to."
)
#: How long someone has to sign in again once they start signing.
SIGNING_TIME = dt.timedelta(minutes=10)
#: The longest reason a signature keeps.
MAX_REASON = 2000
#: Failed re-authentications that lock someone's signing, within :data:`FAILURE_WINDOW`
#: (21 CFR 11.300(d)).
MAX_FAILURES = 3
FAILURE_WINDOW = dt.timedelta(minutes=15)


class Stale(Exception):  # noqa: N818 - reads as "the revision was stale"
    def __init__(self, current: int, *, entry_revision: int | None = None) -> None:
        self.current = current
        if entry_revision is not None:
            super().__init__(f"Its registry entry is at revision {entry_revision} now.")
        else:
            super().__init__(f"It’s at revision {current} now.")


# --- Who may sign -------------------------------------------------------------------------------


def certified() -> Certification | None:
    """The organization's certification in force (21 CFR 11.100(c)), if one's recorded."""
    return Certification.objects.select_related("recorded_by").order_by("-recorded_at").first()


def acknowledged(user: User) -> bool:
    return Acknowledgment.objects.filter(user=user).exists()


def _lock_events(user: User) -> AuditEvent | None:
    return (
        AuditEvent.objects.filter(
            action__in=("signature.locked", "signature.unlocked"),
            target_type="user",
            target_id=str(user.pk),
        )
        .order_by("-occurred_at")
        .first()
    )


def locked_out(user: User) -> bool:
    """Whether failed attempts locked this person's signing, until an Org Admin unlocks it."""
    last = _lock_events(user)
    return last is not None and last.action == "signature.locked"


def identity(user: User) -> tuple[IdentityProvider, SocialAccount] | None:
    """The enabled identity provider, and who the person is there, if they've signed in
    through it."""
    provider = IdentityProvider.objects.filter(is_enabled=True).first()
    if provider is None:
        return None
    account = SocialAccount.objects.filter(user=user, provider=provider.provider_id).first()
    return (provider, account) if account is not None else None


def why_cant_sign(user: User) -> str | None:
    """Why this person can't sign anything now, whatever the record; None if they can."""
    if permissions.token_limits(user) is not None or log.current_agent_info() is not None:
        return (
            "Signatures are given by people in Iki’s web app, signing in again: no token, app "
            "or AI agent can sign."
        )
    if user.is_service_account:
        return "Service accounts can’t sign: a signature is one person’s."
    if not user.is_active:
        return "Your account is deactivated."
    if not user.name.strip():
        return (
            "Your identity provider doesn’t give Iki your name, which a signature shows: ask "
            "your IT team to send it."
        )
    if locked_out(user):
        return (
            "Your signing is locked after failed attempts to sign in again: an Org Admin can "
            "unlock it."
        )
    if certified() is None:
        return (
            "Nobody signs in Iki until an Org Admin records the organization’s certification "
            "to the FDA that its electronic signatures bind as handwritten ones do "
            "(21 CFR 11.100(c))."
        )
    if identity(user) is None:
        return "You haven’t signed in through this organization’s identity provider."
    return None


@dataclass(frozen=True)
class Slot:
    """One of the signatures a change needs, on a record now: who gave it, if anyone, and
    why this person may not give it now."""

    index: int
    wanted: Wanted
    roles: tuple[str, ...]
    #: The signature that counts for it, on the record's current revision.
    signature: Signature | None
    #: Why the person may not give it now; None if they may.
    why_not: str | None
    #: A signature given for it that no longer counts, and why.
    lapsed: str | None = None


def pending(entities: Sequence[Entity]) -> dict[Any, list[Signature]]:
    """The signatures given on each record's current revision, not withdrawn: one query.
    A record of a registry entry needs them on the entry's current revision too."""
    if not entities:
        return {}
    current = {e.pk: e.revision for e in entities}
    entry_revisions = _entry_revisions(entities)
    q = Q()
    for e in entities:
        q |= Q(entity_id=e.pk, revision=e.revision)
    out: dict[Any, list[Signature]] = defaultdict(list)
    for s in (
        Signature.objects.filter(q, withdrawal__isnull=True)
        .select_related("signer")
        .order_by("signed_at")
    ):
        if current.get(s.entity_id) == s.revision and s.entry_revision == entry_revisions.get(
            s.entity_id
        ):
            out[s.entity_id].append(s)
    return out


def _entry_revisions(entities: Iterable[Entity]) -> dict[Any, int | None]:
    entries = {e.entry_id for e in entities if e.entry_id is not None}
    found = dict(Entity.objects.filter(pk__in=entries).values_list("pk", "revision"))
    return {e.pk: found.get(e.entry_id) if e.entry_id else None for e in entities}


def _locked_entry_revision(entity: Entity) -> int | None:
    """Keep the entry's shared facts fixed through the signing transaction."""
    if entity.entry_id is None:
        return None
    revision: int = (
        Entity.objects.select_for_update(no_key=True)
        .values_list("revision", flat=True)
        .get(pk=entity.entry_id)
    )
    return revision


def _qualifies(user: User, owner: Project, roles: tuple[str, ...]) -> bool:
    return user.is_active and bool(permissions.lab_roles(user, owner) & set(roles))


def slots(
    user: User,
    entity: Entity,
    change: Change,
    *,
    given: Sequence[Signature] = (),
    cant: str | None = None,
    values: Mapping[str, Any] | None = None,
) -> list[Slot]:
    """The signatures ``change`` needs on the record now, each with whether ``user`` may
    give it. ``given``: the record's :func:`pending` signatures; ``cant``:
    :func:`why_cant_sign`; ``values``: the record's, as stored, for its guards, if read.

    Core asks only that each is its signer's own act, by someone holding its role, on
    this revision: whether one person may give two, or in what order they're given, is
    the plugin's to say, as a rule on the change."""
    owner = requests.field_project(entity, change.field)
    counted: dict[int, Signature] = {}
    lapsed: dict[int, str] = {}
    for s in given:
        if (s.field, s.state) != (change.field, change.to):
            continue
        if s.index >= len(change.signatures) or s.index in counted:
            continue
        wanted = change.signatures[s.index]
        if s.meaning != wanted.meaning:
            continue  # the plugin changed what it means since
        if owner is not None and _qualifies(s.signer, owner, roles_for(change, wanted)):
            counted[s.index] = s
        else:
            who = s.signer.name or s.signer.email
            lapsed[s.index] = (
                f"{who}’s signature no longer counts: they no longer hold the "
                + _either([lab_roles.label(r) for r in roles_for(change, wanted)])
                + f" role in {owner.name if owner else 'the project'}, or their account was "
                "deactivated."
            )
    out: list[Slot] = []
    for index, wanted in enumerate(change.signatures):
        roles = roles_for(change, wanted)
        signature = counted.get(index)
        why: str | None
        if signature is not None:
            why = f"{signature.signer.name or signature.signer.email} gave it."
        elif cant is not None and cant.startswith("Signatures are given by people"):
            why = cant
        elif (
            base := states.refusal(user, entity, roles, change.to_label, field=change.field)
        ) is not None:
            why = base[0]
        elif cant is not None:
            why = cant
        else:
            # The change's guards and holds (ADRs 0127, 0128), with this signer acting: at
            # each signature, so nobody signs a change they'd refuse, and again at the
            # last, which makes it.
            why = _condition(user, entity, change, values, counted)
        out.append(Slot(index, wanted, roles, signature, why, lapsed.get(index)))
    return out


def _condition(
    user: User,
    entity: Entity,
    change: Change,
    values: Mapping[str, Any] | None,
    counted: Mapping[int, Signature],
) -> str | None:
    """Why its guards or holds refuse the change, with ``user`` signing for it and the
    signatures that count for it so far, which its guards read as ``signatures``."""
    assert change.how is not None  # noqa: S101 - changes.find made it
    given = [
        {
            "index": s.index,
            "meaning": s.meaning,
            "by": str(s.signer_id),
            "at": s.signed_at.isoformat().replace("+00:00", "Z"),
        }
        for _, s in sorted(counted.items())
    ]
    state = change.how.states[change.to]
    why = states.condition(user, entity, change.field, state, values, given)
    if why is not None or not _requires_review(entity, change):
        return why
    if ProjectLink.objects.filter(
        entity=entity, project_id=F("via_request__owner_project_id")
    ).exists():
        return "This result was already delivered. Release a new result to send a new review."
    if _review_reference(entity, entity.revision, change.field, change.current) is None:
        return _NEEDS_REVIEW
    return None


_NEEDS_REVIEW = (
    "Release needs a signed review of this revision. Submit the result for review again."
)


def _requires_review(entity: Entity, change: Change) -> bool:
    spec = requests.result_spec(entity.type)
    return (
        spec is not None and change.how is not None and spec.requires_review(change.how, change.to)
    )


def _review_reference(
    entity: Entity, number: int, field: str, state: str, *, trusted: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """The review that made exactly this revision. Passing trusted keys checks the proofs
    too (when signing or verifying), beyond locating the review for the offered actions."""
    carried = (
        Revision.objects.filter(
            entity=entity,
            number=number,
            kind=RevisionKind.STATE_CHANGED,
            state_field=field,
            state_to=state,
            changed_fields=[field],
        )
        .values_list("signatures", flat=True)
        .first()
    )
    if not carried:
        return None
    rows = Signature.objects.filter(
        entity=entity, revision=number - 1, field=field, state=state
    ).in_bulk(carried)
    if any(sid not in rows for sid in carried):
        return None
    if trusted is not None:
        if any(not check.ok for row in rows.values() for check in verify(row, trusted)):
            return None
        if not _carries_content(rows[carried[0]], entity):
            return None
    return proof.review_reference(
        str(entity.pk), number, field, state, [parsed(rows[sid]) for sid in carried]
    )


def _carries_content(signature: Signature, entity: Entity) -> bool:
    """Compare the complete resulting snapshot with what was signed, read in the same
    schema and units. Only the revision number and this one state value may differ."""
    try:
        migrations = since(signature, entity)
        expected = proof.migrated(json.loads(signature.content), migrations)
        field = signature.field
        for migration in migrations:
            if migration["kind"] == "rename" and migration["from"] == field:
                field = migration["to"]
        expected["revision"] = number = signature.revision + 1
        expected["values"][field] = signature.state
        actual = content_of(entity, number, signature.entry_revision)
        return proof.canonical(expected) == proof.canonical(actual)
    except (KeyError, TypeError, ValueError):
        return False


def _either(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


# --- Starting -----------------------------------------------------------------------------------


def _safe_path(path: str) -> bool:
    return (
        path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and all(ord(c) > 31 for c in path)
    )


def _session_hash(session_key: str) -> str:
    return hashlib.sha256(session_key.encode()).hexdigest()


def begin(
    user: User,
    session_key: str,
    entity: Entity,
    field: str,
    state: str,
    *,
    index: int | None,
    expected_revision: int,
    expected_entry_revision: int | None,
    reason: str,
    return_to: str,
    acknowledge: bool,
) -> Signing:
    """Starts giving one of the signatures setting state field ``field`` to ``state``
    needs, on the record, for the person in this browser session: they sign in again
    next. Raises :class:`PermissionDenied` when they may not give it, :class:`Refused`
    when the record can't take it now, :class:`Invalid` for missing signing input,
    and :class:`Stale` when a reviewed revision is no longer current."""
    if not session_key:
        raise PermissionDenied(why_cant_sign(user) or "Signing needs a browser session.")
    cant = why_cant_sign(user)
    if cant is not None and cant.startswith("Signatures are given by people"):
        raise PermissionDenied(cant)
    with transaction.atomic():
        locked = (
            Entity.objects.select_for_update(of=("self",))
            .select_related("owner_project")
            .get(pk=entity.pk)
        )
        if locked.revision != expected_revision:
            raise Stale(locked.revision)
        entry_revision = _locked_entry_revision(locked)
        if entry_revision != expected_entry_revision:
            if entry_revision is None:
                raise Invalid({"entry_revision": ["This record has no registry entry."]})
            if expected_entry_revision is None:
                raise Invalid(
                    {"entry_revision": ["Send the registry entry revision you reviewed."]}
                )
            raise Stale(locked.revision, entry_revision=entry_revision)
        change = changes.find(locked, field, state)
        given = pending([locked]).get(locked.pk, [])
        found = slots(user, locked, change, given=given, cant=cant)
        if index is None:
            slot = next((s for s in found if s.why_not is None), None) or next(
                (s for s in found if s.signature is None), found[0]
            )
        elif 0 <= index < len(found):
            slot = found[index]
        else:
            raise Refused(f"Setting it to {change.to_label} has no signature {index}.")
        if slot.why_not is not None:
            owner = requests.field_project(locked, change.field)
            if owner is not None and _qualifies(user, owner, slot.roles):
                raise Refused(slot.why_not)
            raise PermissionDenied(slot.why_not)
        reason = reason.strip()
        problems: dict[str, list[str]] = {}
        if change.reason == "required" and not reason:
            problems["reason"] = [f"Setting it to {change.to_label} needs a reason: say why."]
        if len(reason) > MAX_REASON:
            problems["reason"] = [f"Say why in at most {MAX_REASON} characters."]
        first = not acknowledged(user)
        if first and not acknowledge:
            problems["acknowledge"] = [
                "Before your first signature, agree that it binds you as your handwritten "
                "signature would."
            ]
        if not _safe_path(return_to):
            problems["return_to"] = ["Give a path on this site to come back to."]
        if problems:
            raise Invalid(problems)
        found_identity = identity(user)
        assert found_identity is not None  # noqa: S101 - why_cant_sign checked it
        signing = Signing.objects.create(
            user=user,
            session=_session_hash(session_key),
            entity=locked,
            revision=locked.revision,
            entry_revision=entry_revision,
            entry_revision_reviewed=entry_revision is not None,
            field=change.field,
            state=change.to,
            index=slot.index,
            reason=reason,
            return_to=return_to[:2000],
            acknowledged=ACKNOWLEDGMENT if first else "",
            provider=found_identity[0],
            expires_at=timezone.now() + SIGNING_TIME,
        )
        log.record(
            "signature.requested",
            actor=user,
            target=locked,
            project=locked.owner_project,
            data={
                "signing": str(signing.pk),
                "field": change.field,
                "from": change.current,
                "to": change.to,
                "index": slot.index,
                "meaning": slot.wanted.meaning,
                "revision": locked.revision,
                "entry_revision": entry_revision,
            },
        )
    return signing


def started(signing_id: Any, user: User, session_key: str) -> Signing | None:  # noqa: ANN401 - a UUID
    """The person's signing that's waiting for them to sign in again in this session."""
    signing = (
        Signing.objects.filter(pk=signing_id, user=user, status=Status.STARTED)
        .select_related("provider", "entity")
        .first()
    )
    if signing is None or signing.session != _session_hash(session_key):
        return None
    return signing


# --- Finishing ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """What the identity provider said when the person signed in again."""

    protocol: str
    issuer: str
    subject: str
    authenticated_at: dt.datetime
    #: How they signed in, as the provider says: OIDC's ``amr``.
    methods: tuple[str, ...] = ()
    #: OIDC's ``acr``, or SAML's AuthnContextClassRef.
    context: str = ""


class Failure(Exception):  # noqa: N818 - reads as "a failure"
    """Signing in again didn't sign: why, for the person, and whether it counts toward
    locking their signing (an attempt someone else might be making)."""

    def __init__(self, code: str, message: str, *, counts: bool = False) -> None:
        self.code = code
        self.message = message
        self.counts = counts
        super().__init__(message)


def finish(signing: Signing, evidence: Evidence) -> Signing:
    """Signs what the person started signing, now the identity provider says they signed
    in again: checked again, against the record as it is now. Returns the signing, signed
    or failed, and never raises for the person's own failure."""
    try:
        with transaction.atomic():
            s = (
                Signing.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(pk=signing.pk)
            )
            if s.status != Status.STARTED:
                raise Failure("finished", "This signing is already finished.", counts=True)
            if s.expires_at <= timezone.now():
                raise Failure("expired", "It took too long: start signing again.")
            _check_evidence(s, evidence)
            _sign(s, evidence)
            return s
    except Failure as failure:
        return fail(signing, failure)


def fail(signing: Signing, failure: Failure) -> Signing:
    """Records why signing in again didn't sign, and locks the person's signing after too
    many attempts that might be someone else's (21 CFR 11.300(d))."""
    from iki_server.signatures import alerts

    with transaction.atomic():
        s = (
            Signing.objects.select_for_update(of=("self",))
            .select_related("user", "entity")
            .get(pk=signing.pk)
        )
        if s.status == Status.STARTED:
            s.status = Status.CANCELLED if failure.code == "cancelled" else Status.FAILED
            s.message = failure.message
            s.finished_at = timezone.now()
            s.save(update_fields=["status", "message", "finished_at"])
        entity = s.entity
        log.record(
            "signature.failed",
            actor=s.user,
            target=entity,
            project=entity.owner_project,
            data={
                "signing": str(s.pk),
                "field": s.field,
                "state": s.state,
                "index": s.index,
                "revision": s.revision,
                "code": failure.code,
                "reason": failure.message,
                "counts": failure.counts,
            },
        )
        if failure.counts:
            alerts.attempt(s.user, entity, failure.message)
            since = timezone.now() - FAILURE_WINDOW
            last = _lock_events(s.user)
            if last is not None and last.occurred_at > since:
                since = last.occurred_at
            failures = AuditEvent.objects.filter(
                action="signature.failed",
                actor=s.user,
                occurred_at__gte=since,
                data__counts=True,
            ).count()
            if failures >= MAX_FAILURES and not locked_out(s.user):
                alerts.lock(s.user, failures)
    return s


def _check_evidence(s: Signing, evidence: Evidence) -> None:
    """That the person who signed in again is the one signing, and signed in just now."""
    account = SocialAccount.objects.filter(user=s.user, provider=s.provider.provider_id).first()
    if account is None or account.uid != evidence.subject:
        raise Failure(
            "someone_else",
            "Someone else signed in at the identity provider, so it didn’t sign: a signature "
            "is only its signer’s. Sign in there as yourself.",
            counts=True,
        )
    # The provider's clock may be a little ahead of or behind Iki's.
    skew = dt.timedelta(seconds=60)
    if evidence.authenticated_at < s.created_at - skew:
        raise Failure(
            "not_again",
            "The identity provider didn’t make you sign in again: it used an earlier sign-in. "
            "An Org Admin should check that it honours a request to sign in again "
            "(prompt=login, ForceAuthn).",
        )
    if evidence.authenticated_at > timezone.now() + skew:
        raise Failure(
            "clock",
            "The identity provider says you signed in later than now: its clock or Iki’s is wrong.",
        )


def _sign(s: Signing, evidence: Evidence) -> Signature:
    """Checks it all again and signs, under the record's lock; the last of the signatures
    a change needs makes it."""
    if s.entry_revision is not None and not s.entry_revision_reviewed:
        raise Failure(
            "changed",
            "Read the record again and start a new signing to confirm the registry entry revision.",
        )
    user = s.user
    seen = history_seen()
    locked = (
        Entity.objects.select_for_update(of=("self",))
        .select_related("owner_project")
        .get(pk=s.entity_id)
    )
    if locked.revision != s.revision or _locked_entry_revision(locked) != s.entry_revision:
        raise Failure(
            "changed",
            f"{locked.hrid} changed while you were signing in, so it didn’t sign what you "
            "read. Read it again, then sign.",
        )
    try:
        change = changes.find(locked, s.field, s.state)
    except Refused as refused:
        raise Failure("refused", str(refused)) from None
    given = pending([locked]).get(locked.pk, [])
    slot = slots(user, locked, change, given=given, cant=why_cant_sign(user))[s.index]
    if slot.why_not is not None:
        raise Failure("refused", slot.why_not)
    if s.acknowledged and not acknowledged(user):
        Acknowledgment.objects.create(user=user, statement=s.acknowledged)
        log.record(
            "signature.acknowledged", actor=user, target=user, data={"statement": s.acknowledged}
        )
    content_text = canonical_content(locked, s.revision, s.entry_revision)
    if history_seen() != seen:
        # A deploy's migration renamed or converted what history holds while this read it:
        # what was read can't be said to be before it, or after (ADR 0148).
        raise Failure(
            "changed",
            "Iki was updated while you were signing in, so it didn’t sign. Sign again.",
        )
    key, private = signing_keys.current()
    signed_at = timezone.now()
    signature_id = uuid7()
    meaning = SIGNATURE_MEANINGS[slot.wanted.meaning]
    document: dict[str, Any] = {
        "format": proof.DOCUMENT_FORMAT,
        "id": str(signature_id),
        "deployment": deployment(),
        "record": {
            "id": str(locked.pk),
            "type": locked.type,
            "hrid": locked.hrid,
            "revision": s.revision,
            "entry": (
                {"id": str(locked.entry_id), "revision": s.entry_revision}
                if locked.entry_id is not None
                else None
            ),
            "content_sha256": proof.sha256(content_text),
        },
        "change": {
            "field": change.field,
            "field_label": change.field_label,
            "from": change.current,
            "from_label": change.current_label,
            "to": change.to,
            "to_label": change.to_label,
        },
        "signature": {
            "index": slot.index,
            "of": len(change.signatures),
            "meaning": slot.wanted.meaning,
            "meaning_label": meaning.label,
            "label": slot.wanted.label,
            "statement": slot.wanted.statement,
            "roles": list(slot.roles),
        },
        "signer": {"id": str(user.pk), "name": user.name, "email": user.email},
        "reason": s.reason,
        "signed_at": _instant(signed_at),
        "authentication": {
            "protocol": evidence.protocol,
            "issuer": evidence.issuer,
            "subject": evidence.subject,
            "authenticated_at": _instant(evidence.authenticated_at),
            "methods": list(evidence.methods),
            "context": evidence.context,
        },
        "key": key.public,
    }
    if _requires_review(locked, change):
        reference = _review_reference(
            locked, s.revision, change.field, change.current, trusted=signing_keys.trusted()
        )
        if reference is None:
            raise Failure("review", _NEEDS_REVIEW)
        document["reviewed"] = reference
    text = proof.canonical(document)
    signature = Signature.objects.create(
        id=signature_id,
        entity=locked,
        revision=s.revision,
        entry_revision=s.entry_revision,
        field=change.field,
        state=change.to,
        index=slot.index,
        meaning=slot.wanted.meaning,
        signer=user,
        signer_ref=str(user.pk),
        signed_at=signed_at,
        document=text,
        signature=private.sign(text.encode()),
        key=key.public,
        content=content_text,
        deployment=deployment(),
        history_seen=seen,
    )
    s.status = Status.SIGNED
    s.signature = signature
    s.finished_at = signed_at
    s.save(update_fields=["status", "signature", "finished_at"])
    log.record(
        "signature.signed",
        actor=user,
        target=locked,
        project=locked.owner_project,
        data={
            "signature": str(signature.pk),
            "field": change.field,
            "from": change.current,
            "to": change.to,
            "index": slot.index,
            "meaning": slot.wanted.meaning,
            "revision": s.revision,
            "content_sha256": document["record"]["content_sha256"],
            "authenticated_at": document["authentication"]["authenticated_at"],
            "protocol": evidence.protocol,
            "reason": s.reason,
        },
    )
    after = slots(user, locked, change, given=[*given, signature])
    if all(x.signature is not None for x in after):
        carried = tuple(x.signature.pk for x in after if x.signature is not None)
        changes.make(locked, change, user, _joined(after, s), carried)
        if "reviewed" in document and not _carries_content(signature, locked):
            raise Failure(
                "review", "The released values differ from the review. Nothing was released."
            )
    return signature


def _joined(after: list[Slot], s: Signing) -> str:
    """The change's reason: each signer's, in its signatures' order."""
    reasons = {x.index: x.signature.pk for x in after if x.signature is not None}
    said: dict[Any, str] = dict(
        Signing.objects.filter(signature_id__in=reasons.values()).values_list(
            "signature_id", "reason"
        )
    )
    said[s.signature_id] = s.reason
    return "\n".join(r for i in sorted(reasons) if (r := said.get(reasons[i], "").strip()))


def _instant(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def deployment() -> str:
    """This deployment's name, as its signatures and bundles give it."""
    from django.conf import settings

    return str(settings.IKI_ENVIRONMENT)


# --- Content ------------------------------------------------------------------------------------


def content_of(
    entity: Entity, revision: int, entry_revision: int | None, *, own: bool = False
) -> dict[str, Any]:
    """The record's content at a revision (``proof.content``), from its history tables:
    every field it has had, as history keeps it, with each quantity's unit, and for a record
    of a registry entry, the entry's at ``entry_revision``, unless ``own``: the record's own
    fields only. The entry is the one the record named at that revision: it may have moved
    to another since, when its entry was superseded (ADR 0115)."""
    from iki_server.bundles.export import _json
    from iki_server.entities.services import entry_at
    from iki_server.files.models import File

    form = shape(entity.type)
    entry = entry_at(entity, revision, entry_revision) if entity.entry_id is not None else None
    values: dict[str, Any] = {}
    units: dict[str, str] = {}
    file_ids: set[str] = set()
    entry_fields: set[str] = set()
    for part in form.parts:
        if part.entry:
            if own or entry is None or entry_revision is None:
                continue
            key, at = entry
            entry_fields.update(part.fields)
        else:
            key, at = entity.pk, revision
        row = (
            part.history._default_manager.filter(entity_id=key, revision__lte=at)
            .order_by("-revision")
            .values()
            .first()
        )
        if row is None:
            continue
        for name, f in part.fields.items():
            value = _json(row.get(name))
            values[name] = value
            measured = f.items if f.type == "list" and f.items is not None else f
            if measured.type == "quantity" and measured.unit:
                units[name] = measured.unit
            is_file = f.type == "file" or (f.items is not None and f.items.type == "file")
            if is_file and value:
                file_ids.update(value if isinstance(value, list) else [value])
            elif f.is_rows and value and f.items is not None:
                # A list of rows' files (ADR 0137), kept in its history as their IDs.
                cells = [n for n, c in f.items.fields.items() if c.type == "file"]
                file_ids.update(r[n] for r in value for n in cells if r.get(n))
    files = dict(
        File.objects.filter(pk__in=file_ids).values_list("pk", "blob__sha256").order_by("pk")
    )
    return proof.content(
        record=str(entity.pk),
        type_name=entity.type,
        revision=revision,
        entry=(
            {"id": str(entry[0]), "revision": entry_revision}
            if entry is not None and entity.entry_id is not None
            else None
        ),
        values=values,
        units=units,
        files={str(k): v for k, v in files.items()},
        entry_fields=entry_fields,
    )


def canonical_content(entity: Entity, revision: int, entry_revision: int | None) -> str:
    return proof.canonical(content_of(entity, revision, entry_revision))


def history_seen() -> int:
    """How far plugins' migrations' changes to kept history have got (ADR 0148)."""
    return int(HistoryChange.objects.aggregate(last=Max("pk"))["last"] or 0)


def since(signature: Signature, entity: Entity) -> list[dict[str, Any]]:
    """What has been done to the record's kept history since ``signature`` was signed, in
    order: plugins' migrations here renamed its fields, converted its quantities to other
    units, renamed its type (ADR 0148). For an imported one, first what it was read through
    when it came (``read_through``): what was done where it came from, and how the import
    named here the people and registry entries it names (ADR 0096), which this deployment
    vouched for then with its signing key. Only migrations' changes to the tables the
    record's fields are in count, as those tables were named then. Raises
    :class:`proof.Unexplained` for anything this deployment's signing key didn't sign."""
    kept = history_changes(entity, signature.history_seen)
    own = signing_keys.mine() if kept or signature.read_through else set()
    for row in kept:
        if row.key not in own or not keys.verify(row.key, bytes(row.signature), row.signed()):
            raise proof.Unexplained(
                f"a change to its history recorded since ({row.kind} of `{row.name}`) "
                "isn’t signed by this deployment’s key, as a migration’s is."
            )
    try:
        before = proof.vouched(
            signature.read_through or None,
            {"id": signature.pk, "record": signature.entity_id, "revision": signature.revision},
            dict.fromkeys(own, deployment()),
        )
    except proof.Unexplained:
        raise proof.Unexplained(
            "what was done to its history before it came here isn’t signed by this "
            "deployment’s key, as an import’s is."
        ) from None
    return [*before, *(r.change() for r in kept)]


def read_through(signature: Mapping[str, Any], changes: list[dict[str, Any]]) -> dict[str, str]:
    """``changes``, what was done to a signature's record's history since it was signed,
    vouched for by this deployment's signing key (:func:`proof.read_through`): for a
    bundle to carry, or an import to keep. ``signature``: its ``id``, ``record`` and
    ``revision``. Empty if nothing was done."""
    if not changes:
        return {}
    key, private = signing_keys.current()
    document = proof.canonical(
        proof.read_through(
            signature=signature,
            changes=changes,
            deployment=deployment(),
            key=key.public,
            made_at=_instant(timezone.now()),
        )
    )
    return {
        "document": document,
        "key": key.public,
        "signature": proof.b64(private.sign(document.encode())),
    }


def history_changes(entity: Entity, seen: int) -> list[HistoryChange]:
    """What plugins' migrations have done to the record's kept history since ``seen``
    (:func:`history_seen`), in the order they did it (ADR 0148): only changes to the tables
    its fields are in, as those tables were named then."""
    rows = list(HistoryChange.objects.filter(pk__gt=seen).order_by("-pk"))
    parts = {p.name for p in shape(entity.type).parts}
    kept: list[HistoryChange] = []
    for row in rows:  # newest first, so each is matched to the tables as named then
        if row.kind == HistoryChange.Kind.RETYPE:
            named = {_retyped(p, row.to, row.name) for p in parts}
            if named != parts:
                kept.append(row)
                parts = named
        elif row.part in parts:
            kept.append(row)
    return kept[::-1]


def _retyped(part: str, new: str, old: str) -> str:
    """A part's name (``acme/lab.Sample``, ``acme/lims:acme/lab.Sample#entry``) before its
    type ``old`` was renamed ``new``."""
    named, hash_, entry = part.partition("#")
    plugin, colon, of = named.rpartition(":")
    return f"{plugin}{colon}{old if of == new else of}{hash_}{entry}"


# --- Withdrawing --------------------------------------------------------------------------------


def withdraw(user: User, signature: Signature, reason: str) -> Withdrawal:
    """The signer takes back their signature, while the change waits for others."""
    reason = reason.strip()
    with transaction.atomic():
        locked = Entity.objects.select_for_update(of=("self",)).get(pk=signature.entity_id)
        if signature.signer_id != user.pk:
            raise PermissionDenied("Only its signer withdraws a signature.")
        if permissions.token_limits(user) is not None:
            raise PermissionDenied("A signature is withdrawn by its signer, in Iki’s web app.")
        if Withdrawal.objects.filter(signature=signature).exists():
            raise Refused("You withdrew it already.")
        if Revision.objects.filter(signatures__contains=[signature.pk]).exists():
            raise Refused(
                "The state it signed for was set with it, so it stays: setting another state "
                "reverses it."
            )
        if locked.revision != signature.revision or (
            _locked_entry_revision(locked) != signature.entry_revision
        ):
            raise Refused(f"It lapsed when {locked.hrid} changed: it counts for nothing now.")
        if not reason:
            raise Invalid({"reason": ["Withdrawing a signature needs a reason."]})
        if len(reason) > MAX_REASON:
            raise Invalid({"reason": [f"Say why in at most {MAX_REASON} characters."]})
        withdrawal = Withdrawal.objects.create(signature=signature, by=user, reason=reason)
        log.record(
            "signature.withdrawn",
            actor=user,
            target=locked,
            project=locked.owner_project,
            data={
                "signature": str(signature.pk),
                "field": signature.field,
                "state": signature.state,
                "index": signature.index,
                "reason": reason,
            },
        )
    return withdrawal


# --- What a signature is now --------------------------------------------------------------------


@dataclass(frozen=True)
class State:
    """Where a signature stands: ``carried`` by the revision that set the state it signed
    for (``revision``), ``pending`` while that waits for others, ``withdrawn`` by its
    signer, or ``lapsed`` because the record changed after it."""

    status: str
    detail: str
    revision: int | None = None


def standing(signatures: Sequence[Signature]) -> dict[Any, State]:
    """Each signature's :class:`State`: a few queries for them all."""
    if not signatures:
        return {}
    ids = [s.pk for s in signatures]
    carried: dict[Any, int] = {}
    for number, listed in Revision.objects.filter(
        entity_id__in={s.entity_id for s in signatures},
        kind=RevisionKind.STATE_CHANGED,
        signatures__overlap=ids,
    ).values_list("number", "signatures"):
        for i in listed:
            carried[i] = number
    withdrawn = {
        w.signature_id: w
        for w in Withdrawal.objects.filter(signature_id__in=ids).select_related("by")
    }
    entities = {
        e.pk: e
        for e in Entity.objects.filter(pk__in={s.entity_id for s in signatures}).only(
            "pk", "hrid", "revision", "entry_id"
        )
    }
    entry_revisions = _entry_revisions(entities.values())
    out: dict[Any, State] = {}
    for s in signatures:
        entity = entities[s.entity_id]
        if s.pk in carried:
            out[s.pk] = State(
                "carried", f"It set the state, at revision {carried[s.pk]}.", carried[s.pk]
            )
        elif s.pk in withdrawn:
            w = withdrawn[s.pk]
            out[s.pk] = State(
                "withdrawn",
                f"Its signer withdrew it on {_shown(w.withdrawn_at)}"
                + (f": {w.reason}" if w.reason else "."),
            )
        elif entity.revision != s.revision or entry_revisions[entity.pk] != s.entry_revision:
            out[s.pk] = State(
                "lapsed",
                f"{entity.hrid} changed after it was signed, so it counts for nothing now.",
            )
        else:
            out[s.pk] = State("pending", "Setting the state waits for its other signatures.")
    return out


def _shown(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%d at %H:%M:%S UTC")


def manifestation(document: dict[str, Any]) -> str:
    """The signature as people read it (21 CFR 11.50): the signer's printed name, the date
    and time, and what it means. From the signed document, so it says what was signed."""
    sig = document.get("signature") or {}
    meaning = SIGNATURE_MEANINGS.get(sig.get("meaning", ""))
    verb = meaning.verb if meaning else "Signed"
    signer = document.get("signer") or {}
    when = dt.datetime.fromisoformat(str(document.get("signed_at", "")).replace("Z", "+00:00"))
    label = sig.get("label") or (meaning.label if meaning else "")
    who = signer.get("name") or signer.get("email")
    return f"{verb} by {who} on {_shown(when)}. Meaning: {label}."


def parsed(signature: Signature) -> dict[str, Any]:
    try:
        found = json.loads(signature.document)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


# --- Verifying ----------------------------------------------------------------------------------


def verify(signature: Signature, trusted: dict[str, str] | None = None) -> list[proof.Check]:
    """Checks a signature again, from the database: its key is one this deployment trusts,
    the key signed its document, its columns say what the document says, the content kept
    with it is what was signed, and the record's history still holds that content."""
    trusted = trusted if trusted is not None else signing_keys.trusted()
    raw = bytes(signature.signature)
    checks = proof.check_document(signature.document, raw, trusted)
    document = parsed(signature)
    if not document:
        return checks
    record = document.get("record") or {}
    sig = document.get("signature") or {}
    entity = Entity.objects.filter(pk=signature.entity_id).first()
    expected = {
        "id": (document.get("id"), str(signature.pk)),
        "record": (record.get("id"), str(signature.entity_id)),
        "revision": (record.get("revision"), signature.revision),
        "entry revision": (
            (record.get("entry") or {}).get("revision"),
            signature.entry_revision,
        ),
        "field": ((document.get("change") or {}).get("field"), signature.field),
        "state": ((document.get("change") or {}).get("to"), signature.state),
        "signature": (sig.get("index"), signature.index),
        "meaning": (sig.get("meaning"), signature.meaning),
        "signer": ((document.get("signer") or {}).get("id"), signature.signer_ref),
        "time": (document.get("signed_at"), _instant(signature.signed_at)),
        "key": (document.get("key"), signature.key),
    }
    wrong = [name for name, (a, b) in expected.items() if a != b]
    checks.append(
        proof.Check(
            "row",
            not wrong,
            "Its record, revision, change, meaning, signer and time are what was signed."
            if not wrong
            else f"Its {_and(wrong)} aren’t what was signed: it was changed, or moved from "
            "another record.",
        )
    )
    checks.append(proof.check_content(document, signature.content))
    if entity is not None:
        checks.append(_record_check(signature, entity))
        if "reviewed" in document:
            reference = _review_reference(
                entity,
                signature.revision,
                signature.field,
                (document.get("change") or {}).get("from", ""),
                trusted=trusted,
            )
            checks.append(proof.review_check(document["reviewed"], reference))
    revision = Revision.objects.filter(
        entity_id=signature.entity_id, signatures__contains=[signature.pk]
    ).first()
    if revision is not None:
        moved = document.get("change") or {}
        ok = (revision.number, *changes.carried(revision)) == (
            signature.revision + 1,
            moved.get("field"),
            moved.get("from"),
            moved.get("to"),
        )
        if "reviewed" in document and entity is not None:
            ok = ok and _carries_content(signature, entity)
        checks.append(
            proof.Check(
                "change",
                ok,
                f"Revision {revision.number} set it to {moved.get('to_label')} with it, as it "
                "signed."
                if ok
                else f"Revision {revision.number} carries it, but isn’t the change it signed.",
            )
        )
    return checks


def _record_check(signature: Signature, entity: Entity) -> proof.Check:
    if signature.bundle_import_id is not None:
        return _imported_check(signature, entity)
    try:
        now: dict[str, Any] | None = content_of(
            entity, signature.revision, signature.entry_revision
        )
    except LookupError:
        now = None
    try:
        changes = since(signature, entity)
        then = proof.migrated(json.loads(signature.content), changes)
    except proof.Unexplained as unexplained:
        return _changed(signature, entity, [], str(unexplained))
    except (ValueError, TypeError, KeyError):
        return _changed(signature, entity, [])
    if now is not None and proof.canonical(then) == proof.canonical(now):
        return proof.Check("record", True, _holds(signature, entity, changes))
    return _changed(signature, entity, proof.differences(then, now or {}))


def _holds(signature: Signature, entity: Entity, changes: list[dict[str, Any]]) -> str:
    said = f"{entity.hrid}’s revision {signature.revision} still holds what was signed"
    if not changes:
        return said + "."
    return f"{said}, read through what was done to it since: {proof.described(changes)}."


def _imported_check(signature: Signature, entity: Entity) -> proof.Check:
    """For a signature an import brought: the record's own fields here are what was signed,
    read through what was done since, naming people and registry entries as they are here.
    Its entry's fields signed are bound by the signature and its content's SHA-256 alone:
    this registry keeps its own history of the entry, with its own revisions."""
    try:
        changes = since(signature, entity)
        signed = proof.migrated(json.loads(signature.content), changes)
        now = content_of(entity, signature.revision, signature.entry_revision, own=True)
    except proof.Unexplained as unexplained:
        return _changed(signature, entity, [], str(unexplained))
    except (ValueError, TypeError, KeyError, LookupError):
        return _changed(signature, entity, [])
    entry_fields = set(signed.get("entry_fields") or ())
    values = {k: v for k, v in signed.get("values", {}).items() if k not in entry_fields}
    units = {k: u for k, u in signed.get("units", {}).items() if k in values}
    named = _strings(values)
    files = {k: v for k, v in signed.get("files", {}).items() if k in named}
    if (
        proof.canonical(values) == proof.canonical(now["values"])
        and units == now["units"]
        and files == now["files"]
    ):
        said = _holds(signature, entity, changes)
        if entry_fields and entity.entry_id:
            said = (
                said[:-1] + "; its entry’s values signed are bound by the signature alone, "
                "since this registry keeps its own history of the entry."
            )
        return proof.Check("record", True, said)
    return _changed(signature, entity, proof.differences({"values": values, "units": units}, now))


def _strings(value: Any) -> set[str]:  # noqa: ANN401 - JSON
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {s for v in value.values() for s in _strings(v)}
    if isinstance(value, list):
        return {s for v in value for s in _strings(v)}
    return set()


def _changed(
    signature: Signature, entity: Entity, changed: list[str], why: str = ""
) -> proof.Check:
    return proof.Check(
        "record",
        False,
        f"{entity.hrid}’s revision {signature.revision} no longer holds what was signed"
        + (f": {why}" if why else "")
        + (f": {_and([f'`{c}`' for c in changed])} changed." if changed and not why else "")
        + ("" if why or changed else ".")
        + " Its history was changed outside Iki.",
    )


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def missing(entity: Entity) -> list[str]:
    """The revisions that set a state with signatures that aren't there: gone from the
    database, or another record's or revision's. Empty, unless something outside Iki
    changed them."""
    listed = [
        (number, i)
        for number, ids in Revision.objects.filter(
            entity=entity, kind=RevisionKind.STATE_CHANGED
        ).values_list("number", "signatures")
        for i in ids or ()
    ]
    present = {
        pk: (entity_id, revision)
        for pk, entity_id, revision in Signature.objects.filter(
            pk__in=[i for _, i in listed]
        ).values_list("pk", "entity_id", "revision")
    }
    out = []
    for number, i in listed:
        if i not in present:
            out.append(f"Revision {number} was made with signature {i}, which is gone.")
        elif present[i] != (entity.pk, number - 1):
            out.append(
                f"Revision {number} was made with signature {i}, which signed another "
                "record or revision."
            )
    return out


# --- The organization ---------------------------------------------------------------------------


def certify(by: User, submitted_on: dt.date, reference: str) -> Certification:
    """An Org Admin records the organization's certification to the FDA (21 CFR 11.100(c))."""
    if not permissions.can(by, Action.ORG_MANAGE):
        raise PermissionDenied("Recording the organization’s certification needs an Org Admin.")
    reference = reference.strip()
    if not reference:
        raise Invalid({"reference": ["Say where the letter is kept, or how it was sent."]})
    if submitted_on > timezone.localdate():
        raise Invalid({"submitted_on": ["It was sent on or before today."]})
    with transaction.atomic():
        found = Certification.objects.create(
            submitted_on=submitted_on, reference=reference[:500], recorded_by=by
        )
        log.record(
            "signature.certified",
            actor=by,
            data={"submitted_on": submitted_on.isoformat(), "reference": found.reference},
        )
    return found


def unlock(by: User, user: User) -> None:
    """An Org Admin unlocks someone's signing after failed attempts locked it."""
    if not permissions.can(by, Action.ORG_MANAGE):
        raise PermissionDenied("Unlocking someone’s signing needs an Org Admin.")
    if not locked_out(user):
        raise Refused(f"{user.name or user.email}’s signing isn’t locked.")
    log.record("signature.unlocked", actor=by, target=user)
