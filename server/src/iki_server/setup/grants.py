"""Setup grants: what a browser holds after opening a setup link.

A grant lives in the session and points at the exchanged token. It ends when
setup finishes, when it expires, or as soon as someone creates a new setup
link, because that revokes the token behind it.
"""

from dataclasses import dataclass

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from iki_server.accounts.models import IdentityProvider
from iki_server.accounts.session_policy import SETUP_SESSION_KEY
from iki_server.setup.models import SetupToken, hash_token

SESSION_KEY = SETUP_SESSION_KEY
_CACHE_ATTR = "_iki_setup_grant"


@dataclass(frozen=True)
class SetupGrant:
    token: SetupToken
    #: The provider being configured in the wizard, if any yet.
    identity_provider_id: str | None

    @property
    def recovery(self) -> bool:
        return self.token.recovery

    def identity_provider(self) -> IdentityProvider | None:
        if not self.identity_provider_id:
            return None
        return IdentityProvider.objects.filter(pk=self.identity_provider_id).first()


def exchange(request: HttpRequest, token: str) -> SetupGrant | None:
    """Trades a setup token for a grant in this session; ``None`` if the token is invalid."""
    now = timezone.now()
    with transaction.atomic():
        record = (
            SetupToken.objects.select_for_update()
            .filter(token_hash=hash_token(token.strip()))
            .first()
        )
        if (
            record is None
            or record.revoked_at is not None
            or record.exchanged_at is not None
            or record.expires_at <= now
        ):
            return None
        record.exchanged_at = now
        record.save(update_fields=["exchanged_at"])
    # A fresh session key: nothing from before the exchange carries the grant.
    request.session.cycle_key()
    request.session[SESSION_KEY] = {"token": str(record.pk), "identity_provider": None}
    grant = SetupGrant(token=record, identity_provider_id=None)
    setattr(request, _CACHE_ATTR, grant)
    return grant


def current(request: HttpRequest) -> SetupGrant | None:
    """The valid setup grant in this request's session, if there is one."""
    if hasattr(request, _CACHE_ATTR):
        return getattr(request, _CACHE_ATTR)  # type: ignore[no-any-return]
    grant = _load(request)
    setattr(request, _CACHE_ATTR, grant)
    return grant


def _load(request: HttpRequest) -> SetupGrant | None:
    session = getattr(request, "session", None)
    if session is None:
        return None
    data = session.get(SESSION_KEY)
    if not isinstance(data, dict) or not isinstance(data.get("token"), str):
        return None
    record = SetupToken.objects.filter(pk=data["token"]).first()
    expires_at = record.grant_expires_at if record else None
    if (
        record is None
        or record.revoked_at is not None
        or expires_at is None
        or expires_at <= timezone.now()
    ):
        session.pop(SESSION_KEY, None)
        return None
    return SetupGrant(token=record, identity_provider_id=data.get("identity_provider"))


def set_identity_provider(request: HttpRequest, provider: IdentityProvider) -> None:
    grant = current(request)
    if grant is None:
        raise PermissionError("no setup grant")
    request.session[SESSION_KEY] = {
        "token": str(grant.token.pk),
        "identity_provider": str(provider.pk),
    }
    setattr(
        request, _CACHE_ATTR, SetupGrant(token=grant.token, identity_provider_id=str(provider.pk))
    )


def end(request: HttpRequest) -> None:
    """Ends the grant for good: revokes its token and removes it from the session."""
    grant = current(request)
    if grant is not None:
        SetupToken.objects.filter(pk=grant.token.pk, revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
    request.session.pop(SESSION_KEY, None)
    setattr(request, _CACHE_ATTR, None)
