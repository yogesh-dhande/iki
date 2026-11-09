"""The first-run wizard.

1. **Open the setup link** created by ``iki-server setup_link`` (cluster
   access is the proof of authority). The token becomes a setup grant in
   this browser's session.
2. **Name the organization.**
3. **Connect the identity provider** (OIDC). Iki checks the provider's
   discovery document before saving anything.
4. **Sign in through it.** This proves the configuration works end to end.
5. **Finish**: the provider is enabled and the person who signed in becomes
   the first administrator.

There is no local administrator account at any point. In recovery mode
(``setup_link --recover``) the same steps reconfigure sign-in on an
installation that is already set up.
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

import structlog
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from iki_server.accounts.adapters import LOGIN_PROVIDER_SESSION_KEY
from iki_server.accounts.models import IdentityProvider, User, new_provider_id
from iki_server.accounts.oidc import DiscoveryError, discover
from iki_server.setup import grants
from iki_server.setup.forms import IdentityProviderForm, OrganizationForm, TokenForm
from iki_server.setup.models import Installation
from iki_server.web import csp
from iki_server.web.ratelimit import RateLimiter, client_key

logger = structlog.get_logger("iki.setup")

#: Setup codes one client address can try per minute (per web pod).
exchange_limiter = RateLimiter(limit_per_minute=10)

STEPS = ("Organization", "Identity provider", "Sign in", "Finish")
_PENDING_PROVIDER_ID = "_iki_setup_provider_id"

View = Callable[..., HttpResponse]


def requires_grant(view: View) -> View:
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ANN401
        if grants.current(request) is None:
            return redirect("setup:start")
        return view(request, *args, **kwargs)

    return never_cache(require_http_methods(["GET", "POST"])(wrapper))


def _render(
    request: HttpRequest, template: str, step: int, context: dict[str, Any], status: int = 200
) -> HttpResponse:
    grant = grants.current(request)
    return render(
        request,
        template,
        {"steps": STEPS, "step": step, "recovery": bool(grant and grant.recovery), **context},
        status=status,
    )


def _redirect_uri(request: HttpRequest, provider_id: str) -> str:
    path = reverse("openid_connect_callback", kwargs={"provider_id": provider_id})
    return request.build_absolute_uri(path)


@never_cache
@require_http_methods(["GET", "POST"])
def start(request: HttpRequest) -> HttpResponse:
    installation = Installation.get()
    grant = grants.current(request)
    if grant is not None:
        return redirect("setup:identity_provider" if grant.recovery else "setup:organization")
    form = TokenForm(request.POST or None)
    if request.method == "POST":
        if not exchange_limiter.allow(client_key(request)):
            form.add_error(None, "Too many attempts. Wait a minute and try again.")
        elif form.is_valid():
            grant = grants.exchange(request, form.cleaned_data["token"])
            if grant is not None and installation.is_set_up and not grant.recovery:
                grants.end(request)
                grant = None
            if grant is not None:
                logger.info("setup_started", recovery=grant.recovery)
                return redirect(
                    "setup:identity_provider" if grant.recovery else "setup:organization"
                )
            logger.warning("setup_link_rejected")
            form.add_error(
                "token",
                "This setup link is invalid, expired or already used. "
                "Create a new one with iki-server setup_link.",
            )
    template = (
        "setup/already_set_up.html"
        if installation.is_set_up and request.method == "GET"
        else "setup/start.html"
    )
    return render(
        request,
        template,
        {"form": form, "steps": STEPS, "step": 0},
        status=400 if form.errors else 200,
    )


@requires_grant
def organization(request: HttpRequest) -> HttpResponse:
    installation = Installation.get()
    form = OrganizationForm(
        request.POST or None, initial={"organization_name": installation.organization_name}
    )
    if request.method == "POST" and form.is_valid():
        installation.organization_name = form.cleaned_data["organization_name"]
        installation.save(update_fields=["organization_name", "updated_at"])
        return redirect("setup:identity_provider")
    return _render(
        request, "setup/organization.html", 1, {"form": form}, status=400 if form.errors else 200
    )


@requires_grant
def identity_provider(request: HttpRequest) -> HttpResponse:
    grant = grants.current(request)
    assert grant is not None  # noqa: S101 - guaranteed by @requires_grant
    current = grant.identity_provider() or (
        IdentityProvider.objects.filter(is_enabled=True).first() if grant.recovery else None
    )
    provider_id = (
        current.provider_id
        if current
        else request.session.setdefault(_PENDING_PROVIDER_ID, new_provider_id())
    )
    initial = (
        {
            "display_name": current.display_name,
            "issuer": current.issuer,
            "client_id": current.client_id,
        }
        if current
        else {}
    )
    form = IdentityProviderForm(
        request.POST or None, initial=initial, has_secret=current is not None
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        existing = IdentityProvider.objects.filter(issuer=data["issuer"]).first()
        if existing is None and not data["client_secret"]:
            form.add_error("client_secret", "Enter the client secret.")
        else:
            try:
                metadata = discover(data["issuer"], allow_http=not settings.HTTPS)
            except DiscoveryError as error:
                form.add_error("issuer", str(error))
            else:
                provider = existing or IdentityProvider(
                    issuer=data["issuer"], provider_id=provider_id
                )
                provider.display_name = data["display_name"]
                provider.client_id = data["client_id"]
                if data["client_secret"]:
                    provider.client_secret = data["client_secret"]
                provider.token_auth_method = metadata.token_auth_method
                provider.authorization_endpoint = metadata.authorization_endpoint
                provider.save()
                grants.set_identity_provider(request, provider)
                request.session.pop(_PENDING_PROVIDER_ID, None)
                logger.info(
                    "identity_provider_saved",
                    provider_id=provider.provider_id,
                    issuer=provider.issuer,
                )
                return redirect("setup:verify")
    return _render(
        request,
        "setup/identity_provider.html",
        2,
        {"form": form, "redirect_uri": _redirect_uri(request, provider_id)},
        status=400 if form.errors else 200,
    )


def _signed_in_with(request: HttpRequest, provider: IdentityProvider) -> bool:
    return bool(
        request.user.is_authenticated
        and request.session.get(LOGIN_PROVIDER_SESSION_KEY) == str(provider.pk)
    )


@requires_grant
def verify(request: HttpRequest) -> HttpResponse:
    grant = grants.current(request)
    provider = grant.identity_provider() if grant else None
    if provider is None:
        return redirect("setup:identity_provider")
    if _signed_in_with(request, provider):
        return redirect("setup:complete")
    return _render(
        request,
        "setup/verify.html",
        3,
        {
            "provider": provider,
            "redirect_uri": _redirect_uri(request, provider.provider_id),
            "sign_in_url": reverse(
                "openid_connect_login", kwargs={"provider_id": provider.provider_id}
            ),
            "next": reverse("setup:complete"),
        },
    )


@requires_grant
def complete(request: HttpRequest) -> HttpResponse:
    grant = grants.current(request)
    provider = grant.identity_provider() if grant else None
    if provider is None or not _signed_in_with(request, provider):
        return redirect("setup:verify")
    user: User = request.user  # type: ignore[assignment]
    if request.method == "POST":
        recovery = bool(grant and grant.recovery)
        with transaction.atomic():
            now = timezone.now()
            IdentityProvider.objects.exclude(pk=provider.pk).filter(is_enabled=True).update(
                is_enabled=False, enabled_at=None
            )
            IdentityProvider.objects.filter(pk=provider.pk).update(is_enabled=True, enabled_at=now)
            user.is_staff = user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
            installation = Installation.get()
            if installation.setup_completed_at is None:
                installation.setup_completed_at = now
                installation.save(update_fields=["setup_completed_at", "updated_at"])
            grants.end(request)
        csp.clear_cache()
        logger.info(
            "setup_completed",
            recovery=recovery,
            provider_id=provider.provider_id,
            administrator_id=str(user.pk),
        )
        return redirect("setup:done")
    return _render(request, "setup/complete.html", 4, {"provider": provider})


@never_cache
@require_http_methods(["GET"])
def done(request: HttpRequest) -> HttpResponse:
    if not (Installation.get().is_set_up and request.user.is_authenticated):
        return redirect("home")
    return render(request, "setup/done.html", {"steps": STEPS, "step": 5})
