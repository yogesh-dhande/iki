"""Iki's rules on top of django-oauth-toolkit's request validation."""

from datetime import timedelta
from typing import Any

from django.utils import timezone
from oauth2_provider.models import get_device_grant_model
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc8628.grant_types.device_code import DeviceCodeGrant

from iki_server.oauth import exchange, redirects

#: Polling this much sooner than the interval allows for network jitter.
POLL_TOLERANCE = timedelta(seconds=1)


class IkiValidator(OAuth2Validator):
    def validate_device_code(
        self,
        client_id: str,
        code: str,
        request: Any,  # noqa: ANN401
        *args: object,
        **kwargs: object,
    ) -> str | None:
        """RFC 8628 §3.4-3.5: where a device code stands, for the client polling with it.

        The code must belong to that client and not have expired. An approved
        code is good for one token response: the first poll to see it approved
        consumes it, so a copied code can't mint more tokens later.
        """
        device_grants = get_device_grant_model()
        grant = (
            device_grants.objects.select_related("user")
            .filter(device_code=code, client_id=client_id)
            .first()
        )
        if grant is None:
            return None
        if grant.is_expired():
            return DeviceCodeGrant.DEVICE_CODE_EXPIRED
        if grant.status == device_grants.DENIED:
            return DeviceCodeGrant.DEVICE_CODE_DENIED
        now = timezone.now()
        if grant.status == device_grants.AUTHORIZATION_PENDING:
            too_soon = now - grant.last_checked < timedelta(seconds=grant.interval) - POLL_TOLERANCE
            device_grants.objects.filter(pk=grant.pk).update(last_checked=now)
            if too_soon:
                return DeviceCodeGrant.DEVICE_CODE_SLOW_DOWN
            return DeviceCodeGrant.DEVICE_CODE_PENDING
        if grant.status != device_grants.AUTHORIZED or grant.user is None:
            return None
        approved = device_grants.objects.filter(pk=grant.pk, status=device_grants.AUTHORIZED)
        consumed = approved.update(status=device_grants.EXPIRED, last_checked=now)
        if not consumed or not grant.user.is_active:
            return None
        request.user = grant.user
        request.scopes = grant.scope.split()
        return DeviceCodeGrant.DEVICE_CODE_AUTHORIZED

    def validate_grant_type(
        self,
        client_id: str,
        grant_type: str,
        client: Any,  # noqa: ANN401
        request: Any,  # noqa: ANN401
        *args: object,
        **kwargs: object,
    ) -> bool:
        # Client credentials only ever act as a service account.
        if (
            grant_type == "client_credentials"
            and getattr(client, "service_account_id", None) is None
        ):
            return False
        # Token exchange: only credentials allowed to act for people (ADR 0073).
        if grant_type == exchange.GRANT_TYPE:
            return bool(getattr(client, "service_account_id", None) and client.acts_for_people)
        return bool(
            super().validate_grant_type(client_id, grant_type, client, request, *args, **kwargs)
        )

    def validate_redirect_uri(
        self,
        client_id: str,
        redirect_uri: str,
        request: Any,  # noqa: ANN401
        *args: object,
        **kwargs: object,
    ) -> bool:
        # However the client was stored (an older row, a metadata document),
        # plain http goes nowhere but loopback.
        if not redirects.is_allowed(redirect_uri):
            return False
        return bool(
            super().validate_redirect_uri(client_id, redirect_uri, request, *args, **kwargs)
        )

    def save_bearer_token(self, token: Any, request: Any, *args: object, **kwargs: object) -> None:  # noqa: ANN401
        super().save_bearer_token(token, request, *args, **kwargs)
        client = request.client
        if client is not None:
            type(client).objects.filter(pk=client.pk).update(last_used_at=timezone.now())
