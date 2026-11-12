"""Token exchange (RFC 8693) for unattended agents acting for a person (ADR 0073).

A service account's credential, allowed by an Org Admin to act for people,
authenticates itself and names a person's delegation to it (``subject_token``, the
delegation's grant ID). It gets an access token for the person, within the
delegation's scopes and its own, and the answer names it as the actor (``act``).
"""

import json
from typing import Any
from uuid import UUID

from django.utils import timezone
from oauthlib.oauth2 import Server as OAuthlibServer
from oauthlib.oauth2.rfc6749 import errors
from oauthlib.oauth2.rfc6749.grant_types import ClientCredentialsGrant

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
DELEGATION = "urn:ikisci:params:oauth:token-type:delegation"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105 - a type URN


def delegation_for(client: Any, grant: str) -> Any:  # noqa: ANN401 - a Consent, or None
    """The person's delegation to the client that `grant` names, if it's in force: not
    expired, and its person active."""
    from iki_server.oauth.models import Consent

    try:
        grant_id = UUID(grant)
    except ValueError:
        return None
    found = (
        Consent.objects.filter(client=client, grant=grant_id, user__is_active=True)
        .select_related("user")
        .first()
    )
    if found is None or found.expires_at is None or found.expires_at <= timezone.now():
        return None
    return found


class TokenExchangeGrant(ClientCredentialsGrant):
    """``grant_type=urn:ietf:params:oauth:grant-type:token-exchange``, for delegations."""

    def create_token_response(self, request: Any, token_handler: Any) -> tuple[Any, str, int]:  # noqa: ANN401
        headers, body, status = super().create_token_response(request, token_handler)
        if status != 200:
            return headers, body, status
        token = json.loads(body)
        token["issued_token_type"] = ACCESS_TOKEN
        # The actor, as RFC 8693 §4.1 names it; the token's subject is the person.
        token["act"] = {"sub": request.client.client_id}
        return headers, json.dumps(token), status

    def validate_token_request(self, request: Any) -> None:  # noqa: ANN401
        if getattr(request, "grant_type", None) != GRANT_TYPE:
            raise errors.UnsupportedGrantTypeError(request=request)
        for param in ("subject_token", "subject_token_type"):
            if not getattr(request, param, None):
                raise errors.InvalidRequestError(f"Request is missing {param}.", request=request)
        if request.subject_token_type != DELEGATION:
            raise errors.InvalidRequestError(
                f"subject_token_type must be {DELEGATION}: the person's delegation.",
                request=request,
            )
        if getattr(request, "actor_token", None):
            raise errors.InvalidRequestError(
                "Iki doesn't take actor_token: the client authenticates as the actor.",
                request=request,
            )
        # The parent checks the client and scopes as client credentials do, which only a
        # service account's credential may use; the validator allows this grant only to
        # credentials an Org Admin let act for people.
        request.grant_type = "client_credentials"
        try:
            super().validate_token_request(request)
        finally:
            request.grant_type = GRANT_TYPE
        self.validate_grant_type(request)
        delegation = delegation_for(request.client, request.subject_token)
        if delegation is None or not delegation.client.is_usable(request):
            raise errors.InvalidGrantError(
                "No delegation in force names this credential: it may have expired or been "
                "revoked.",
                request=request,
            )
        allowed = set(delegation.scope_list)
        wanted = set(request.scopes)
        if not wanted <= allowed:
            if getattr(request, "scope", None):
                raise errors.InvalidScopeError(
                    "The delegation doesn't allow every scope asked for.", request=request
                )
            wanted &= allowed  # none asked for: what both allow
        if not wanted:
            raise errors.InvalidScopeError(
                "The delegation and the credential have no scope in common.", request=request
            )
        request.scopes = sorted(wanted)
        request.user = delegation.user


class Server(OAuthlibServer):
    """oauthlib's server, with token exchange beside its grants."""

    def __init__(self, request_validator: Any, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(request_validator, *args, **kwargs)
        self._grant_types[GRANT_TYPE] = TokenExchangeGrant(request_validator)
