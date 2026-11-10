"""allauth adapters: sign-in through the configured identity provider only.

- Identity providers come from :class:`IdentityProvider` (secrets encrypted
  at rest), not from allauth's own table or from settings.
- An account is matched by the provider's subject identifier only. Iki never
  links accounts by email address, which would let anyone who controls a
  matching address at another provider take the account over.
- The user's name and email follow the identity provider on every sign-in.
"""

from typing import Any

import structlog
from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialLogin
from django.http import HttpRequest
from django.shortcuts import render

from iki_server.accounts.models import IdentityProvider, User

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


def _claims(sociallogin: SocialLogin) -> dict[str, Any]:
    data = sociallogin.account.extra_data or {}
    claims: dict[str, Any] = {**data.get("id_token", {}), **data.get("userinfo", {})}
    return claims


def display_name(claims: dict[str, Any]) -> str:
    name = claims.get("name")
    if not name:
        name = " ".join(p for p in (claims.get("given_name"), claims.get("family_name")) if p)
    return str(name or claims.get("preferred_username") or "")[:200]


def sign_in_error(request: HttpRequest, reason: str, status: int = 403) -> ImmediateHttpResponse:
    logger.warning("sign_in_refused", reason=reason)
    return ImmediateHttpResponse(
        render(request, "accounts/sign_in_error.html", {"reason": reason}, status=status)
    )


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def list_apps(
        self, request: HttpRequest | None, provider: str | None = None, client_id: str | None = None
    ) -> list[Any]:
        apps = []
        for idp in _available_providers(request):
            if provider not in (None, "openid_connect", idp.provider_id):
                continue
            if client_id is not None and idp.client_id != client_id:
                continue
            apps.append(idp.to_social_app())
        return apps

    def is_open_for_signup(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        return True

    def is_auto_signup_allowed(self, request: HttpRequest, sociallogin: SocialLogin) -> bool:
        return True

    def populate_user(self, request: HttpRequest, sociallogin: SocialLogin, data: Any) -> User:  # noqa: ANN401
        user: User = sociallogin.user
        claims = _claims(sociallogin)
        user.email = User.objects.normalize_email(claims.get("email") or "")
        user.name = display_name(claims)
        return user

    def pre_social_login(self, request: HttpRequest, sociallogin: SocialLogin) -> None:
        claims = _claims(sociallogin)
        email = User.objects.normalize_email(str(claims.get("email") or "")).strip()
        if not email:
            raise sign_in_error(request, "no_email")
        existing_email_owner = User.objects.filter(email=email)
        if sociallogin.is_existing:
            user: User = sociallogin.user
            existing_email_owner = existing_email_owner.exclude(pk=user.pk)
            if existing_email_owner.exists():
                raise sign_in_error(request, "email_in_use")
            name = display_name(claims)
            if (user.email, user.name) != (email, name):
                user.email, user.name = email, name
                user.save(update_fields=["email", "name"])
        elif existing_email_owner.exists():
            raise sign_in_error(request, "email_in_use")
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
        request.session[LOGIN_PROVIDER_SESSION_KEY] = str(provider.pk)

    def on_authentication_error(
        self,
        request: HttpRequest,
        provider: Any,  # noqa: ANN401
        error: str | None = None,
        exception: Exception | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> None:
        # Log what went wrong for operators; users see a generic message.
        logger.warning(
            "sign_in_failed",
            provider=getattr(getattr(provider, "app", None), "provider_id", None),
            error=error,
            exception=type(exception).__name__ if exception else None,
        )
