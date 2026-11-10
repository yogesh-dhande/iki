"""Creating, revoking and checking the SCIM bearer token."""

import hashlib
import secrets
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iki_server.accounts.models import User
from iki_server.audit import log as audit
from iki_server.scim.models import ScimToken

PREFIX = "iki_scim_"
#: ``last_used_at`` is written at most this often, not on every request.
USE_RESOLUTION = timedelta(minutes=1)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def scim_enabled() -> bool:
    """SCIM provisioning is on while a token is live."""
    return ScimToken.objects.filter(revoked_at__isnull=True).exists()


def live() -> ScimToken | None:
    return ScimToken.objects.filter(revoked_at__isnull=True).first()


def create(*, by: User) -> str:
    """Revokes the live token, if any, and returns a new one. Shown once."""
    token = PREFIX + secrets.token_urlsafe(32)
    with transaction.atomic():
        _revoke_live(by=by)
        record = ScimToken.objects.create(token_hash=_hash(token), hint=token[-4:], created_by=by)
        audit.record("scim.token_created", actor=by, target=record)
    return token


def revoke(*, by: User) -> bool:
    with transaction.atomic():
        return _revoke_live(by=by)


def _revoke_live(*, by: User) -> bool:
    current = ScimToken.objects.select_for_update().filter(revoked_at__isnull=True).first()
    if current is None:
        return False
    current.revoked_at = timezone.now()
    current.save(update_fields=["revoked_at"])
    audit.record("scim.token_revoked", actor=by, target=current)
    return True


def authenticate(header: str) -> ScimToken | None:
    """The live token named by an ``Authorization: Bearer`` header, or None."""
    scheme, _, value = header.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value.startswith(PREFIX) or len(value) > 200:
        return None
    token = ScimToken.objects.filter(token_hash=_hash(value), revoked_at__isnull=True).first()
    if token is None:
        return None
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at >= USE_RESOLUTION:
        ScimToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now
    return token
