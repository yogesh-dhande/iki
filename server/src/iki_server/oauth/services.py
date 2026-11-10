"""Approving clients, granting and revoking access, and service-account credentials.

Callers check permissions; these functions keep the invariants and record
every change in the audit log, in the same transaction.

Revoking anything deletes the affected access and refresh tokens at once,
so it takes effect on the next request, not when tokens expire.
"""

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from oauth2_provider.models import get_access_token_model, get_refresh_token_model

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.oauth.models import Approval, Client, Consent
from iki_server.oauth.scopes import SCOPES, ordered
from iki_server.org.models import Project
from iki_server.org.services import Refused
from iki_server.setup.models import Installation

AccessToken = get_access_token_model()
RefreshToken = get_refresh_token_model()


def _revoke(tokens: dict[str, Any]) -> int:
    """Deletes the matching access and refresh tokens. Returns how many access tokens."""
    RefreshToken.objects.filter(**tokens).delete()
    deleted, _ = AccessToken.objects.filter(**tokens).delete()
    return int(deleted)


def revoke_user_tokens(user: User) -> int:
    """Ends every OAuth token the person (or service account) holds."""
    revoked = _revoke({"user": user})
    revoked += _revoke({"application__service_account": user})
    return revoked


# --- Approval ------------------------------------------------------------------


def requires_approval() -> bool:
    return Installation.get().require_oauth_app_approval


def initial_approval() -> Approval:
    return Approval.PENDING if requires_approval() else Approval.APPROVED


def request_approval(client: Client, *, by: User) -> None:
    """Someone asks an Org Admin to approve a client they want to use."""
    if client.approval != Approval.PENDING or client.requested_by_id is not None:
        return
    with transaction.atomic():
        client.requested_by, client.requested_at = by, timezone.now()
        client.save(update_fields=["requested_by", "requested_at"])
        audit.record("oauth.client_approval_requested", actor=by, target=client)


def set_approval(client: Client, approval: Approval, *, by: User) -> None:
    if client.first_party or client.service_account_id is not None:
        raise Refused("Iki’s own apps and service-account credentials don’t need approval.")
    if client.approval == approval:
        return
    with transaction.atomic():
        before = client.approval
        client.approval = approval
        client.approval_changed_by, client.approval_changed_at = by, timezone.now()
        client.save(update_fields=["approval", "approval_changed_by", "approval_changed_at"])
        revoked = 0 if approval == Approval.APPROVED else _revoke({"application": client})
        audit.record(
            "oauth.client_approved" if approval == Approval.APPROVED else "oauth.client_blocked",
            actor=by,
            target=client,
            data={"previous": before, "tokens_revoked": revoked},
        )


def set_requires_approval(required: bool, *, by: User) -> None:
    installation = Installation.get()
    if installation.require_oauth_app_approval == required:
        return
    with transaction.atomic():
        installation.require_oauth_app_approval = required
        installation.save(update_fields=["require_oauth_app_approval", "updated_at"])
        audit.record("oauth.approval_policy_changed", actor=by, data={"required": required})


# --- Consent -------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    scopes: list[str]
    all_projects: bool
    projects: list[Project]


def grant(user: User, client: Client, choice: Grant) -> Consent:
    """Records what the person allows the client to do, replacing any earlier choice."""
    scopes = ordered(choice.scopes)
    if not scopes:
        raise Refused("Choose at least one kind of access.")
    if not choice.all_projects and not choice.projects:
        raise Refused("Choose the projects it may use, or allow all your projects.")
    with transaction.atomic():
        consent, created = Consent.objects.update_or_create(
            user=user,
            client=client,
            defaults={"scopes": " ".join(scopes), "all_projects": choice.all_projects},
        )
        consent.projects.set([] if choice.all_projects else choice.projects)
        audit.record(
            "oauth.consent_granted",
            actor=user,
            target=client,
            data={
                "scopes": scopes,
                "projects": "all"
                if choice.all_projects
                else sorted(str(p.pk) for p in choice.projects),
                "changed": not created,
            },
        )
    return consent


def revoke_consent(consent: Consent, *, by: User) -> int:
    with transaction.atomic():
        revoked = _revoke({"user": consent.user, "application": consent.client})
        consent.delete()
        audit.record(
            "oauth.consent_revoked",
            actor=by,
            target=consent.client,
            data={"user": consent.user.email, "tokens_revoked": revoked},
        )
    return revoked


def consent_for(user: User, client: Client) -> Consent | None:
    return Consent.objects.filter(user=user, client=client).prefetch_related("projects").first()


# --- Service-account credentials -----------------------------------------------


@dataclass(frozen=True)
class NewCredential:
    client: Client
    #: Shown once; only a hash is stored.
    secret: str


def create_credential(account: User, name: str, scopes: list[str], *, by: User) -> NewCredential:
    if not account.is_service_account:
        raise TypeError("credentials belong to service accounts")
    if not account.is_active:
        raise Refused(f"{account.name} is deactivated.")
    chosen = ordered(scopes)
    if not chosen or not set(chosen) <= set(SCOPES):
        raise Refused("Choose at least one kind of access.")
    if "admin" in chosen:
        raise Refused("Service accounts can’t administer the organization.")
    from oauth2_provider.generators import generate_client_secret

    secret = generate_client_secret()
    with transaction.atomic():
        client = Client(
            name=name.strip() or f"{account.name} credential",
            client_type=Client.CLIENT_CONFIDENTIAL,
            authorization_grant_type=Client.GRANT_CLIENT_CREDENTIALS,
            client_secret=secret,
            hash_client_secret=True,
            service_account=account,
            allowed_scopes=" ".join(chosen),
            approval=Approval.APPROVED,
        )
        client.full_clean(exclude=["client_secret", "redirect_uris", "post_logout_redirect_uris"])
        client.save()
        audit.record(
            "oauth.credential_created",
            actor=by,
            target=account,
            data={"client_id": client.client_id, "scopes": chosen},
        )
    return NewCredential(client=client, secret=secret)


def revoke_credential(client: Client, *, by: User) -> None:
    account = client.service_account
    if account is None:
        raise TypeError("not a service-account credential")
    with transaction.atomic():
        revoked = _revoke({"application": client})
        client_id = client.client_id
        client.delete()
        audit.record(
            "oauth.credential_revoked",
            actor=by,
            target=account,
            data={"client_id": client_id, "tokens_revoked": revoked},
        )


def credentials(account: User) -> QuerySet[Client]:
    return Client.objects.filter(service_account=account).order_by("created")
