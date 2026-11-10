"""allauth adapters: sign-in through the configured identity provider only.

- Identity providers come from :class:`IdentityProvider` (secrets encrypted
  at rest), not from allauth's own table or from settings.
- An account is matched by the provider's subject identifier. Iki never
  links a sign-in to an existing account by email address, which would let
  anyone who controls a matching address take the account over. The one
  exception is someone the identity provider itself provisioned over SCIM:
  their first sign-in through the enabled provider links to the account SCIM
  created for them, matched by email or SCIM ``userName`` (ADR 0026).
- While SCIM provisioning is on, only provisioned people have accounts. A
  sign-in from anyone else is refused and recorded.
- The user's name and email follow the identity provider on every sign-in.
"""

from dataclasses import dataclass
from typing import Any

import structlog
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.db.models import Q
from django.http import HttpRequest
from django.shortcuts import render

from iki_server.accounts.models import IdentityProvider, Protocol, User
from iki_server.audit import log as audit

logger = structlog.get_logger("iki.accounts")

#: Session key: the identity provider the current user signed in with.
LOGIN_PROVIDER_SESSION_KEY = "_iki_login_provider"


class AccountAdapter(DefaultAccountAdapter):
    def add_message(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        """Iki's pages say what happened themselves; allauth's generic messages are dropped."""

    def is_open_for_signup(self, request: HttpRequest) -> bool:
        # No local sign-up. Accounts are created on first sign-in through SSO.
        return False


def _available_providers(request: HttpRequest | None) -> list[IdentityProvider]:
    """The enabled provider, plus the one being set up if this session holds a setup grant."""
    from iki_server.setup import grants

    providers = list(IdentityProvider.objects.filter(is_enabled=True))
    grant = grants.current(request) if request is not None else None
    if grant is not None:
        pending = grant.identity_provider()
        if pending is not None and all(p.pk != pending.pk for p in providers):
            providers.append(pending)
    return providers


@dataclass(frozen=True)
class Identity:
    """Who the identity provider says signed in."""

    email: str
    name: str


def _claims(sociallogin: SocialLogin) -> dict[str, Any]:
    data = sociallogin.account.extra_data or {}
    claims: dict[str, Any] = {**data.get("id_token", {}), **data.get("userinfo", {})}
    return claims


def display_name(claims: dict[str, Any]) -> str:
    name = claims.get("name")
    if not name:
        name = " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
    return str(name or claims.get("preferred_username") or "")[:200]


def _identity(sociallogin: SocialLogin, data: dict[str, Any] | None = None) -> Identity:
    """The email and name from this sign-in, for either protocol.

    ``data`` is allauth's extraction of the response (for SAML, mapped from
    attributes or the NameID). It is only available while the response is
    being read, so :meth:`SocialAccountAdapter.populate_user` keeps the
    result on the sign-in for :meth:`~SocialAccountAdapter.pre_social_login`.
    """
    kept: Identity | None = getattr(sociallogin, "_iki_identity", None)
    if kept is not None and data is None:
        return kept
    if _is_saml(sociallogin):
        data = data or {}
        claims = {
            "name": data.get("name"),
            "given_name": data.get("first_name"),
            "family_name": data.get("last_name"),
        }
        email = str(data.get("email") or "")
    else:
        claims = _claims(sociallogin)
        email = str(claims.get("email") or "")
    identity = Identity(
        email=User.objects.normalize_email(email).strip(), name=display_name(claims)
    )
    sociallogin._iki_identity = identity
    return identity


def _is_saml(sociallogin: SocialLogin) -> bool:
    return IdentityProvider.objects.filter(
        provider_id=sociallogin.account.provider, protocol=Protocol.SAML
    ).exists()


def sign_in_error(
    request: HttpRequest, reason: str, status: int = 403, email: str = ""
) -> ImmediateHttpResponse:
    """Refuses a sign-in that the identity provider authenticated.

    Only reachable after a real sign-in at the provider, so recording it can't
    be used to flood the audit log. (Failed or forged callbacks are only
    logged; see ``on_authentication_error``.)
    """
    audit.record("user.sign_in_refused", data={"reason": reason, "email": email})
    return ImmediateHttpResponse(
        render(request, "accounts/sign_in_error.html", {"reason": reason}, status=status)
    )


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def list_apps(
        self, request: HttpRequest | None, provider: str | None = None, client_id: str | None = None
    ) -> list[Any]:
        apps = []
        providers = _available_providers(request)
        # SAML providers share one set of URLs, so only one may answer. The
        # provider being set up comes last, and wins in the session setting it up.
        saml = [p for p in providers if p.is_saml][-1:]
        for idp in [p for p in providers if not p.is_saml] + saml:
            if provider not in (None, idp.allauth_provider, idp.provider_id):
                continue
            app = idp.to_social_app()
            if client_id is not None and app.client_id != client_id:
                continue
            apps.append(app)
        return apps

    def is_open_for_signup(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        return True

    def is_auto_signup_allowed(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        return True

    def populate_user(self, request: HttpRequest, sociallogin: SocialLogin, data: Any) -> User:  # noqa: ANN401
        user: User = sociallogin.user
        identity = _identity(sociallogin, data if isinstance(data, dict) else {})
        user.email, user.name = identity.email, identity.name
        return user

    def pre_social_login(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        from iki_server.scim.tokens import scim_enabled
        from iki_server.setup import grants

        provider = next(
            (
                p
                for p in _available_providers(request)
                if p.provider_id == sociallogin.account.provider
            ),
            None,
        )
        if provider is None:  # pragma: no cover - allauth only calls us for a listed app
            raise sign_in_error(request, "unknown_provider")
        identity = _identity(sociallogin)
        email, name = identity.email, identity.name
        if not email:
            raise sign_in_error(request, "no_email")
        email_owners = User.objects.filter(email=email)
        if sociallogin.is_existing:
            user: User = sociallogin.user
            if email_owners.exclude(pk=user.pk).exists():
                raise sign_in_error(request, "email_in_use", email=email)
        else:
            provisioned = (
                User.objects.filter(provisioned_at__isnull=False)
                .filter(Q(email=email) | Q(scim_user_name=email))
                .first()
            )
            if provisioned is not None:
                if (
                    email_owners.exclude(pk=provisioned.pk).exists()
                    or SocialAccount.objects.filter(
                        user=provisioned, provider=provider.provider_id
                    ).exists()
                ):
                    raise sign_in_error(request, "email_in_use", email=email)
                sociallogin.connect(request, provisioned)
                audit.record(
                    "user.identity_linked",
                    actor=provisioned,
                    target=provisioned,
                    data={"provider_id": provider.provider_id, "protocol": provider.protocol},
                )
                user = provisioned
            elif email_owners.exists():
                raise sign_in_error(request, "email_in_use", email=email)
            else:
                grant = grants.current(request)
                setting_up = grant is not None and grant.identity_provider() == provider
                if scim_enabled() and not setting_up:
                    raise sign_in_error(request, "not_provisioned", email=email)
                user = sociallogin.user
        if sociallogin.is_existing and (user.email, user.name) != (email, name):
            user.email, user.name = email, name
            user.save(update_fields=["email", "name"])
        request.session[LOGIN_PROVIDER_SESSION_KEY] = str(provider.pk)

    def on_authentication_error(
        self,
        request: HttpRequest,
        provider: Any,  # noqa: ANN401
        error: str | None = None,
        exception: Exception | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        # Log what went wrong for operators; users see a generic message. Not an
        # audit event: anyone can send a forged callback, and the audit log
        # must not be something an unauthenticated caller can grow.
        logger.warning(
            "sign_in_failed",
            provider=getattr(getattr(provider, "app", None), "provider_id", None),
            error=error,
            exception=type(exception).__name__ if exception else None,
        )
